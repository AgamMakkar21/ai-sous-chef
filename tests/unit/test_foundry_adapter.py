from typing import Any

import pytest
from sous_chef.adapters.foundry import FoundryInvocationError, FoundryModelClient, ModelRole
from sous_chef.settings import ConfigurationError, Settings


class FakeResponses:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def create(self, *, model: str, input: str, max_output_tokens: int) -> Any:
        self.calls.append({"model": model, "input": input, "max": max_output_tokens})
        if self.error is not None:
            raise self.error

        class Response:
            output_text = "ready"

        return Response()


class FakeOpenAI:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "dev",
        "adapter_mode": "live",
        "cosmos_endpoint": "https://cosmos.example.invalid/",
        "cosmos_database": "db",
        "foundry_project_endpoint": "https://foundry.example.invalid/api/projects/test",
        "foundry_orchestrator_deployment": "orch-deploy",
        "foundry_recipe_nutrition_deployment": "recipe-deploy",
        "foundry_extraction_deployment": "extract-deploy",
    }
    values.update(overrides)
    return Settings(**values)


def test_each_role_calls_its_own_configured_deployment() -> None:
    responses = FakeResponses()
    endpoints: list[str] = []

    def factory(endpoint: str) -> FakeOpenAI:
        endpoints.append(endpoint)
        return FakeOpenAI(responses)

    client = FoundryModelClient(make_settings(), factory)
    results = client.check_all()

    assert set(results) == set(ModelRole)
    assert [call["model"] for call in responses.calls] == [
        "orch-deploy",
        "recipe-deploy",
        "extract-deploy",
    ]
    assert endpoints == ["https://foundry.example.invalid/api/projects/test"]


def test_missing_deployment_is_a_configuration_error() -> None:
    settings = make_settings(
        adapter_mode="local", environment="local", foundry_extraction_deployment=None
    )
    with pytest.raises(ConfigurationError, match="DEPLOYMENT"):
        FoundryModelClient(settings)


def test_auth_failure_is_reported_without_leaking_details() -> None:
    secret = "token=secret-private-value"
    client = FoundryModelClient(
        make_settings(),
        lambda _endpoint: FakeOpenAI(FakeResponses(PermissionError(secret))),
    )
    with pytest.raises(FoundryInvocationError, match="orchestrator.*PermissionError") as error:
        client.invoke(ModelRole.ORCHESTRATOR, "hello")
    assert "secret-private" not in str(error.value)
    assert error.value.__cause__ is None


def test_client_factory_failure_is_reported_as_invocation_error() -> None:
    def factory(_endpoint: str) -> FakeOpenAI:
        raise RuntimeError("secret-private-endpoint")

    client = FoundryModelClient(make_settings(), factory)
    with pytest.raises(FoundryInvocationError, match="RuntimeError") as error:
        client.invoke(ModelRole.EXTRACTION, "hello")
    assert "secret-private" not in str(error.value)
