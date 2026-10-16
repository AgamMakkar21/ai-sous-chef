"""Run with python -I after installing the built wheel, not the editable source."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sous_chef import __file__ as package_file
from sous_chef.app import create_app
from sous_chef.settings import Settings


def main() -> None:
    if package_file is None or not Path(package_file).is_relative_to(Path(sys.prefix)):
        raise RuntimeError("Smoke check must import the installed wheel from the environment.")
    settings = Settings(environment="local", adapter_mode="local", _env_file=None)
    with TestClient(create_app(settings)) as client:
        for path, expected in (
            ("/health", {"status": "ok"}),
            ("/ready", {"status": "ready"}),
        ):
            response = client.get(path)
            if response.status_code != 200 or response.json() != expected:
                raise RuntimeError(f"Installed-wheel smoke check failed for {path}.")
        response = client.get("/openapi.json")
        expected_routes = {
            "/health": {"get"},
            "/api/v1/conversations": {"get", "post"},
            "/api/v1/conversations/{conversation_id}/turns": {"get"},
        }
        if response.status_code != 200 or any(
            not methods.issubset(response.json()["paths"].get(path, {}))
            for path, methods in expected_routes.items()
        ):
            raise RuntimeError("Installed-wheel API schema is missing required routes.")
        for path in ("/api/v1/conversations", "/api/v1/conversations/invalid/turns"):
            if client.get(path).status_code != 401:
                raise RuntimeError(
                    "Installed-wheel conversation routes must require authentication."
                )
    print("Installed-wheel health, readiness, API schema and authentication checks passed.")


if __name__ == "__main__":
    main()
