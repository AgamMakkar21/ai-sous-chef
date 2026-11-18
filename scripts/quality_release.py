"""Read-only dev release checks for the synthetic evaluation runner."""

import json
import os
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts.deploy_dev import DeliveryError, az, command, required

if TYPE_CHECKING:
    from sous_chef.settings import Settings


def check_source() -> None:
    if (
        os.environ.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch"}
        or os.environ.get("GITHUB_REF") != "refs/heads/dev"
    ):
        raise DeliveryError("Evaluations require a dev push or manual dev dispatch.")
    expected = os.environ.get("GITHUB_SHA", "")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", expected)
        or command("git", "rev-parse", "HEAD").strip() != expected
    ):
        raise DeliveryError("Evaluation checkout must match the exact dev source.")


def wait_for_deployment(*, wait_seconds: float = 1800) -> None:
    check_source()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise DeliveryError("Set the evaluation repository.")
    source = os.environ["GITHUB_SHA"]
    deadline = time.monotonic() + wait_seconds
    while True:
        runs = json.loads(
            command(
                "gh",
                "api",
                f"repos/{repository}/actions/workflows/deploy-dev.yml/runs"
                f"?head_sha={source}&branch=dev&event=push&per_page=5",
            )
        )["workflow_runs"]
        if runs:
            latest = max(runs, key=lambda run: (run["run_number"], run["run_attempt"]))
            if (
                latest["head_sha"] != source
                or latest["head_branch"] != "dev"
                or latest["event"] != "push"
                or latest["path"].split("@")[0] != ".github/workflows/deploy-dev.yml"
            ):
                raise DeliveryError("Deployment evidence must match the exact dev source.")
            if latest["status"] == "completed":
                if latest["conclusion"] != "success":
                    raise DeliveryError("The exact-source dev deployment did not succeed.")
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeliveryError("Timed out waiting for the exact-source dev deployment.")
        time.sleep(min(15, remaining))


def release_settings() -> "Settings":
    from sous_chef.settings import ConfigurationError, Settings

    if os.environ.get("GITHUB_ACTIONS") == "true":
        check_source()
    app = az("webapp", "show", "-g", required("ASC_RESOURCE_GROUP"), "-n", required("ASC_APP_NAME"))
    deployed = {
        item["name"]: item["value"]
        for item in az(
            "webapp",
            "config",
            "appsettings",
            "list",
            "-g",
            required("ASC_RESOURCE_GROUP"),
            "-n",
            required("ASC_APP_NAME"),
        )
    }
    endpoint = (
        f"https://{required('ASC_FOUNDRY_ACCOUNT_NAME')}.services.ai.azure.com"
        f"/api/projects/{required('ASC_FOUNDRY_PROJECT_NAME')}"
    )
    principals = [
        identity["principalId"]
        for identity in app.get("identity", {}).get("userAssignedIdentities", {}).values()
        if identity.get("clientId") == deployed.get("AZURE_CLIENT_ID")
    ]
    if (
        app.get("tags", {}).get("environment") != "dev"
        or app.get("tags", {}).get("project") != "ai-sous-chef"
        or deployed.get("ASC_ENVIRONMENT") != "dev"
        or deployed.get("ASC_ADAPTER_MODE") != "live"
        or deployed.get("ASC_AGENTS_ENABLED") != "true"
        or deployed.get("ASC_FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/") != endpoint
        or len(principals) != 1
        or principals[0] != deployed.get("ASC_APP_WORKLOAD_PRINCIPAL_ID")
    ):
        raise ConfigurationError(
            "Synthetic evaluations require matching deployed dev identities and pins."
        )
    hostname = app.get("defaultHostName", "")
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.azurewebsites\.net", hostname):
        raise ConfigurationError("Synthetic evaluations require the canonical dev hostname.")

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    source = command("git", "rev-parse", "HEAD").strip()
    with build_opener(NoRedirect()).open(
        Request(f"https://{hostname}/health"), timeout=15
    ) as response:
        if response.status != 200 or response.headers.get("X-ASC-Commit") != source:
            raise ConfigurationError("The live dev app must serve the exact evaluated source.")
    configuration: dict[str, Any] = {
        name: deployed[f"ASC_{name.upper()}"]
        for name in Settings.model_fields
        if (
            name.startswith(("foundry_", "cosmos_"))
            or name in {"app_workload_principal_id", "usda_enabled", "usda_api_key"}
        )
        and f"ASC_{name.upper()}" in deployed
    }
    settings = Settings(_env_file=None, environment="dev", adapter_mode="live", **configuration)
    if not settings.usda_enabled:
        raise ConfigurationError("Synthetic evaluations require the deployed USDA integration.")
    return settings


if __name__ == "__main__":
    try:
        wait_for_deployment()
    except (DeliveryError, KeyError, OSError, ValueError):
        raise SystemExit(
            "Synthetic evaluation source checks failed; no cloud login is allowed."
        ) from None
