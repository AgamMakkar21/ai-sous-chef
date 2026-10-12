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
        if response.status_code != 200 or "/health" not in response.json()["paths"]:
            raise RuntimeError("Installed-wheel API schema is missing the health contract.")
    print("Installed-wheel health, readiness and API schema checks passed.")


if __name__ == "__main__":
    main()
