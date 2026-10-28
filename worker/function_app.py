"""Host-only health probe. Feedback triggers and extraction are intentionally absent."""

import json
import os
import re
from pathlib import Path

import azure.functions as func

app = func.FunctionApp()


def validate_configuration() -> None:
    client = os.environ.get("AZURE_CLIENT_ID", "")
    if not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", client):
        raise RuntimeError("Host requires its own managed identity client ID.")
    for prefix in ("AzureWebJobsStorage", "FeedbackCosmos", "FeedbackLeases"):
        if (
            os.environ.get(prefix)
            or os.environ.get(f"{prefix}__credential") != "managedidentity"
            or os.environ.get(f"{prefix}__clientId") != client
        ):
            raise RuntimeError("Host connections must use the configured managed identity.")
    if not re.fullmatch(r"[a-z0-9]{3,24}", os.environ.get("AzureWebJobsStorage__accountName", "")):
        raise RuntimeError("Host requires an identity-based storage account name.")
    endpoint = os.environ.get("FeedbackCosmos__accountEndpoint", "")
    if not re.fullmatch(
        r"https://[a-z0-9-]+\.documents\.azure\.com(?::443)?/?", endpoint
    ) or endpoint != os.environ.get("FeedbackLeases__accountEndpoint"):
        raise RuntimeError("Host requires matching, valid Cosmos and lease endpoints.")
    expected = {
        "ASC_ENVIRONMENT": "dev",
        "ASC_COSMOS_DATABASE": "sous-chef",
        "ASC_FEEDBACK_CONTAINER": "RecipeHistory",
        "ASC_LEASE_CONTAINER": "leases",
        "FUNCTIONS_EXTENSION_VERSION": "~4",
        "FUNCTIONS_WORKER_RUNTIME": "python",
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Host runtime, environment or container configuration does not match.")


@app.function_name(name="host_health")
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def host_health(req: func.HttpRequest) -> func.HttpResponse:
    validate_configuration()
    release = json.loads(Path(__file__).with_name("release.json").read_text(encoding="utf-8"))
    sha = release.get("commit", "")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("Host release metadata is invalid.")
    return func.HttpResponse(
        '{"status":"ok","scope":"host-only"}',
        mimetype="application/json",
        headers={"X-ASC-Commit": sha, "Cache-Control": "no-store"},
    )
