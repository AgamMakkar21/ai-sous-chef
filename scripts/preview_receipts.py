"""Loopback-only test harness: real receipt AI, signed test identity, disk-backed test pantry."""

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import jwt
import pytest
from azure.cosmos.aio import CosmosClient
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt import PyJWKClient
from sous_chef.adapters import cosmos
from sous_chef.app import create_app
from sous_chef.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from conftest import AUTH_AUDIENCE, AUTH_ISSUER  # noqa: E402
from test_cosmos_repository import SyntheticCredential, Transport  # noqa: E402
from test_profile_api import signed_token  # noqa: E402

BANNER = (
    '<aside class="notice" aria-label="Local receipt preview">'
    "<strong>LOCAL RECEIPT PREVIEW</strong> &mdash; isolated test pantry saved on this computer; "
    "real Azure AI reads uploads. No deployed pantry is accessed. "
    "Recipe chat is not connected in this preview.</aside>"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--endpoint", required=True, help="Existing Foundry project endpoint.")
    parser.add_argument("--deployment", default="orchestrator")
    args = parser.parse_args()
    state_dir: Path = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "pantry.json"
    transport = Transport()
    if state_file.exists():
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        transport.revision = saved["revision"]
        transport.documents = {tuple(item["key"]): item["body"] for item in saved["documents"]}
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    public_key["kid"] = "synthetic"
    lock = threading.Lock()

    def persist() -> None:
        temporary = state_dir / "pantry.json.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "revision": transport.revision,
                    "documents": [
                        {"key": list(key), "body": body}
                        for key, body in transport.documents.items()
                    ],
                },
                ensure_ascii=True,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(state_file)

    def factory(*values: Any, **kwargs: Any) -> CosmosClient:
        return CosmosClient(*values, transport=transport, **kwargs)

    settings = Settings(
        environment="local",
        adapter_mode="live",
        agents_enabled=False,
        receipts_enabled=True,
        receipt_model_deployment=args.deployment,
        foundry_project_endpoint=args.endpoint,
        cosmos_endpoint="https://cosmos.example.invalid",
        cosmos_database="sous-chef",
        auth_issuer=AUTH_ISSUER,
        auth_jwks_uri="https://synthetic.ciamlogin.com/discovery/v2.0/keys",
        auth_client_id=AUTH_AUDIENCE,
        auth_origin=f"http://localhost:{args.port}",
        static_dir=ROOT / "backend" / "src" / "sous_chef" / "static",
    )
    if not (settings.static_dir / "index.html").is_file():
        parser.error("Build the frontend first with npm run build.")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(cosmos, "CosmosClient", factory)
        monkeypatch.setattr(
            cosmos, "build_async_credential", lambda _settings: SyntheticCredential()
        )
        monkeypatch.setattr(PyJWKClient, "fetch_data", lambda _self: {"keys": [public_key]})
        with TestClient(create_app(settings), base_url=f"http://localhost:{args.port}") as client:
            headers = {"Authorization": "Bearer " + signed_token(signing_key)}
            profile = client.get("/api/v1/me", headers=headers)
            profile.raise_for_status()
            if not profile.json()["data"]["onboardingCompleted"]:
                response = client.put(
                    "/api/v1/me/onboarding",
                    headers={**headers, "If-Match": profile.headers["etag"]},
                    json={
                        "allergies": [],
                        "dietaryRestrictions": [],
                        "likes": [],
                        "dislikes": [],
                        "preferredCuisines": [],
                        "spicePreference": "mild",
                        "cookingSkill": "beginner",
                        "typicalTimeBudgetMinutes": 30,
                        "nutritionGoals": [],
                    },
                )
                response.raise_for_status()
            persist()
            allowed_hosts = {f"localhost:{args.port}", f"127.0.0.1:{args.port}"}
            allowed_origins = {"http://" + host for host in allowed_hosts}

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, format: str, *values: object) -> None:
                    pass

                def handle_request(self) -> None:
                    if self.headers.get("Host") not in allowed_hosts or (
                        self.headers.get("Origin") is not None
                        and self.headers["Origin"] not in allowed_origins
                    ):
                        self.send_error(403)
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        self.send_error(400)
                        return
                    if not 0 <= length <= 10 * 1024 * 1024:
                        self.send_error(413)
                        return
                    content = self.rfile.read(length)
                    forwarded = {
                        key: value
                        for key, value in self.headers.items()
                        if key.lower()
                        in {"content-type", "if-match", "idempotency-key", "x-csrf-token"}
                    }
                    forwarded["Authorization"] = "Bearer " + signed_token(signing_key)
                    with lock:
                        result = client.request(
                            self.command, self.path, headers=forwarded, content=content
                        )
                        persist()
                        transport.calls.clear()
                    body = result.content
                    if result.headers.get("content-type", "").startswith("text/html"):
                        body = body.replace(b"<body>", ("<body>" + BANNER).encode(), 1)
                    self.send_response(result.status_code)
                    for key, value in result.headers.items():
                        if key.lower() not in {"content-length", "transfer-encoding", "connection"}:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)

                do_GET = handle_request
                do_HEAD = handle_request
                do_POST = handle_request
                do_PATCH = handle_request
                do_PUT = handle_request
                do_DELETE = handle_request

            with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
                print(f"Local receipt preview: http://localhost:{args.port}", flush=True)
                print(
                    "Pantry: isolated local test data. AI: existing Azure deployment.", flush=True
                )
                server.serve_forever()


if __name__ == "__main__":
    main()
