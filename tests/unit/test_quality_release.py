import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from sous_chef.settings import ConfigurationError

from scripts import quality_release
from scripts.deploy_dev import DeliveryError

SHA = "a" * 40


@pytest.fixture
def release_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/dev",
        "GITHUB_SHA": SHA,
        "GITHUB_REPOSITORY": "synthetic/recipes",
        "ASC_RESOURCE_GROUP": "synthetic-dev",
        "ASC_APP_NAME": "synthetic-dev",
        "ASC_FOUNDRY_ACCOUNT_NAME": "synthetic",
        "ASC_FOUNDRY_PROJECT_NAME": "dev",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(quality_release, "command", lambda *args: SHA)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_EVENT_NAME", "workflow_run"),
        ("GITHUB_REF", "refs/heads/main"),
        ("GITHUB_SHA", "b" * 40),
    ],
)
def test_source_checks_reject_untrusted_or_mismatched_execution(
    release_environment: None, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(DeliveryError):
        quality_release.check_source()


@pytest.mark.parametrize("conclusion", ["success", "failure", None])
def test_evaluations_wait_for_exact_successful_deployment(
    release_environment: None, monkeypatch: pytest.MonkeyPatch, conclusion: str | None
) -> None:
    run = {
        "head_sha": SHA,
        "head_branch": "dev",
        "event": "push",
        "path": ".github/workflows/deploy-dev.yml",
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "run_number": 2,
        "run_attempt": 1,
    }

    def command(*args: str) -> str:
        if args[0] == "git":
            return SHA
        assert args[:2] == ("gh", "api")
        assert f"head_sha={SHA}&branch=dev&event=push" in args[2]
        older = run | {"run_number": 1, "status": "completed", "conclusion": "success"}
        return json.dumps({"workflow_runs": [older, run]})

    monkeypatch.setattr(quality_release, "command", command)
    if conclusion == "success":
        quality_release.wait_for_deployment(wait_seconds=0)
    else:
        with pytest.raises(DeliveryError):
            quality_release.wait_for_deployment(wait_seconds=0)


@pytest.mark.parametrize("mode", ["ok", "prod", "wrong_identity", "stale_app"])
def test_release_settings_use_deployed_pins_and_reject_wrong_targets(
    release_environment: None, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    workload = "11111111-1111-1111-1111-111111111111"
    deployed = {
        "ASC_ENVIRONMENT": "dev",
        "ASC_ADAPTER_MODE": "live",
        "ASC_AGENTS_ENABLED": "true",
        "AZURE_CLIENT_ID": "workload-client",
        "ASC_APP_WORKLOAD_PRINCIPAL_ID": workload,
        "ASC_COSMOS_ENDPOINT": "https://synthetic.documents.azure.com",
        "ASC_COSMOS_DATABASE": "synthetic",
        "ASC_FOUNDRY_PROJECT_ENDPOINT": "https://synthetic.services.ai.azure.com/api/projects/dev",
        "ASC_FOUNDRY_ORCHESTRATOR_DEPLOYMENT": "orchestrator",
        "ASC_FOUNDRY_RECIPE_NUTRITION_DEPLOYMENT": "recipe",
        "ASC_FOUNDRY_EXTRACTION_DEPLOYMENT": "extraction",
        "ASC_FOUNDRY_ORCHESTRATOR_AGENT_NAME": "coordinator",
        "ASC_FOUNDRY_ORCHESTRATOR_AGENT_VERSION": "12",
        "ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_NAME": "specialist",
        "ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_VERSION": "19",
        "ASC_FOUNDRY_RECIPE_NUTRITION_MODE": "combined",
        "ASC_USDA_ENABLED": "true",
        "ASC_USDA_API_KEY": "synthetic-key",
        "ASC_TELEMETRY_ENABLED": "true",
        "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET": "not-evaluation-configuration",
    }
    app: dict[str, Any] = {
        "tags": {"environment": "prod" if mode == "prod" else "dev", "project": "ai-sous-chef"},
        "defaultHostName": "synthetic.azurewebsites.net",
        "identity": {
            "userAssignedIdentities": {
                "workload": {
                    "clientId": "workload-client",
                    "principalId": workload if mode != "wrong_identity" else "other",
                }
            }
        },
    }

    def az(*args: str) -> Any:
        assert args[0] == "webapp"
        if args[1] == "show":
            return copy.deepcopy(app)
        assert args[1:4] == ("config", "appsettings", "list")
        return [{"name": name, "value": value} for name, value in deployed.items()]

    requested: list[str] = []

    def open_health(request: Any, **kwargs: Any) -> Any:
        requested.append(request.full_url)
        assert kwargs["timeout"] == 15
        return nullcontext(
            SimpleNamespace(
                status=200, headers={"X-ASC-Commit": "b" * 40 if mode == "stale_app" else SHA}
            )
        )

    monkeypatch.setattr(quality_release, "az", az)
    monkeypatch.setattr(
        quality_release, "build_opener", lambda *args: SimpleNamespace(open=open_health)
    )
    if mode != "ok":
        with pytest.raises(ConfigurationError):
            quality_release.release_settings()
    else:
        settings = quality_release.release_settings()
        assert settings.foundry_orchestrator_agent_version == "12"
        assert settings.foundry_recipe_nutrition_agent_version == "19"
        assert settings.usda_api_key is not None
        assert settings.usda_api_key.get_secret_value() == "synthetic-key"
        assert not settings.telemetry_enabled
        assert "not-evaluation-configuration" not in repr(settings)
        assert requested == ["https://synthetic.azurewebsites.net/health"]
