from types import SimpleNamespace
from typing import Any

import pytest
from sous_chef.adapters.agent_identity import resolve_agent_identities
from sous_chef.domain.agent_identity import (
    AgentRole,
    IdentityError,
    ToolDenied,
    authorize_tool,
)
from sous_chef.settings import ConfigurationError, Settings

ORCH = ("11111111-1111-1111-1111-111111111111", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
RECIPE = ("22222222-2222-2222-2222-222222222222", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
APP = "33333333-3333-3333-3333-333333333333"


class FakeAgents:
    def __init__(self, identities: dict[str, tuple[str, str] | None]) -> None:
        self._identities = identities

    def get(self, agent_name: str) -> Any:
        ids = self._identities[agent_name]
        instance = SimpleNamespace(principal_id=ids[0], client_id=ids[1]) if ids else None
        return SimpleNamespace(instance_identity=instance)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "dev",
        "adapter_mode": "local",
        "foundry_orchestrator_agent_name": "orchestrator",
        "foundry_recipe_nutrition_agent_name": "recipe-nutrition",
        "app_workload_principal_id": APP,
    }
    values.update(overrides)
    if values["adapter_mode"] == "local":
        values["environment"] = "local"
    return Settings(**values)


def test_each_agent_is_bound_to_its_own_identity() -> None:
    agents = FakeAgents({"orchestrator": ORCH, "recipe-nutrition": RECIPE})
    identities = {i.role: i for i in resolve_agent_identities(make_settings(), agents)}
    assert identities[AgentRole.ORCHESTRATOR].principal_id == ORCH[0]
    assert identities[AgentRole.RECIPE_NUTRITION].principal_id == RECIPE[0]
    assert (
        identities[AgentRole.ORCHESTRATOR].principal_id
        != identities[AgentRole.RECIPE_NUTRITION].principal_id
    )


def test_shared_identity_is_rejected() -> None:
    agents = FakeAgents({"orchestrator": ORCH, "recipe-nutrition": ORCH})
    with pytest.raises(IdentityError, match="share"):
        resolve_agent_identities(make_settings(), agents)


def test_agent_using_app_service_identity_is_rejected() -> None:
    agents = FakeAgents({"orchestrator": (APP, ORCH[1]), "recipe-nutrition": RECIPE})
    with pytest.raises(IdentityError, match="App Service"):
        resolve_agent_identities(make_settings(), agents)


def test_legacy_agent_without_identity_is_rejected() -> None:
    agents = FakeAgents({"orchestrator": None, "recipe-nutrition": RECIPE})
    with pytest.raises(IdentityError, match="legacy"):
        resolve_agent_identities(make_settings(), agents)


def test_pinned_principal_must_match_the_deployed_agent() -> None:
    agents = FakeAgents({"orchestrator": ORCH, "recipe-nutrition": RECIPE})
    settings = make_settings(foundry_orchestrator_agent_principal_id=RECIPE[0])
    with pytest.raises(IdentityError, match="wrong identity"):
        resolve_agent_identities(settings, agents)


def test_lookup_failure_does_not_leak_details() -> None:
    class Broken:
        def get(self, agent_name: str) -> Any:
            raise RuntimeError("secret-detail")

    with pytest.raises(IdentityError) as exc:
        resolve_agent_identities(make_settings(), Broken())
    assert "secret-detail" not in str(exc.value)


def test_missing_agent_names_are_a_configuration_error() -> None:
    settings = make_settings(foundry_orchestrator_agent_name=None)
    with pytest.raises(ConfigurationError):
        resolve_agent_identities(settings, FakeAgents({}))


def test_settings_reject_duplicate_principal_ids() -> None:
    with pytest.raises(ConfigurationError, match="distinct"):
        make_settings(
            foundry_orchestrator_agent_principal_id=ORCH[0],
            foundry_recipe_nutrition_agent_principal_id=ORCH[0],
        )


@pytest.mark.parametrize(
    ("role", "operation"),
    [
        (AgentRole.ORCHESTRATOR, "recipe_specialist"),
        (AgentRole.RECIPE_NUTRITION, "foundry_iq_recipes"),
        (AgentRole.RECIPE_NUTRITION, "usda_nutrition"),
        (AgentRole.RECIPE_NUTRITION, "web_iq"),
    ],
)
def test_allowed_tool_calls_work(role: AgentRole, operation: str) -> None:
    assert authorize_tool(role, operation).value == operation


@pytest.mark.parametrize(
    ("role", "operation"),
    [
        (AgentRole.ORCHESTRATOR, "usda_nutrition"),
        (AgentRole.ORCHESTRATOR, "web_iq"),
        (AgentRole.RECIPE_NUTRITION, "recipe_specialist"),
        (AgentRole.ORCHESTRATOR, "cosmos_read"),
        (AgentRole.RECIPE_NUTRITION, "cosmos_write"),
    ],
)
def test_unrelated_operations_are_denied(role: AgentRole, operation: str) -> None:
    with pytest.raises(ToolDenied):
        authorize_tool(role, operation)
