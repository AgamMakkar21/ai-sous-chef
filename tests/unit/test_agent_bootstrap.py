import copy
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentVersionDetails,
    ApiErrorResponse,
    MCPTool,
    MCPToolFilter,
    PromptAgentDefinition,
)
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceResponseError
from sous_chef import bootstrap_agents
from sous_chef.bootstrap_agents import bootstrap
from sous_chef.domain.agent_identity import AgentRole, IdentityError
from sous_chef.publish_orchestrator import AgentSource, prepare_agent_source
from sous_chef.recipe_definition import RecipePhase
from sous_chef.settings import ConfigurationError, Settings
from test_agent_identity import APP, ORCH, RECIPE
from test_foundry_gateway import settings


def config(**changes: Any) -> Settings:
    return settings(
        **{
            "app_workload_principal_id": APP,
            "foundry_project_endpoint": "https://synthetic.services.ai.azure.com/api/projects/recipes",
            "foundry_project_resource_id": (
                "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/synthetic"
                "/providers/Microsoft.CognitiveServices/accounts/synthetic/projects/recipes"
            ),
            "foundry_recipe_nutrition_rai_policy_name": "asc-responsible-ai-v1",
            "foundry_iq_search_endpoint": "https://synthetic.search.windows.net",
            "foundry_iq_index_name": "fixture-recipes",
            "foundry_iq_knowledge_source_name": "fixture-source",
            "foundry_iq_knowledge_base_name": "fixture-knowledge",
            "foundry_iq_connection_name": "fixture-mcp",
        }
        | changes
    )


def version(source: AgentSource) -> AgentVersionDetails:
    return AgentVersionDetails(
        {
            "name": source.name,
            "version": "7",
            "status": "active",
            "draft": False,
            "definition": source.definition.as_dict(),
            "metadata": source.metadata,
        }
    )


class Harness:
    def __init__(self, settings: Settings) -> None:
        self.config = settings
        self.identities = {"orchestrator-agent": ORCH, "recipe-agent": RECIPE}
        self.versions: dict[str, AgentVersionDetails] = {}
        self.disabled: set[str] = set()
        self.project = Mock(spec=AIProjectClient, agents=Mock())
        self.project.agents.get.side_effect = self.get
        self.project.agents.create_version.side_effect = self.create
        self.project.agents.get_version.side_effect = self.get_version

    def get(self, name: str) -> SimpleNamespace:
        if name not in self.versions:
            error = ResourceNotFoundError("synthetic missing agent")
            error.status_code = 404
            raise error
        principal, client = self.identities[name]
        return SimpleNamespace(
            name=name,
            state="disabled" if name in self.disabled else "enabled",
            instance_identity=SimpleNamespace(principal_id=principal, client_id=client),
            versions=SimpleNamespace(latest=self.versions[name]),
        )

    def create(self, *, agent_name: str, **kwargs: Any) -> AgentVersionDetails:
        assert agent_name not in self.versions
        result = AgentVersionDetails(
            {
                "name": agent_name,
                "version": "7",
                "status": "active",
                "draft": False,
                "definition": kwargs["definition"].as_dict(),
                "metadata": kwargs["metadata"],
            }
        )
        self.versions[agent_name] = result
        return result

    def get_version(self, name: str, number: str) -> AgentVersionDetails:
        assert number == "7"
        return self.versions[name]

    def seed(self, role: AgentRole, *, phase: RecipePhase = RecipePhase.GENERATION) -> None:
        source = prepare_agent_source(self.config, role, phase=phase)
        self.versions[source.name] = version(source)


def test_creates_both_reviewed_definitions_and_verifies_readback_without_activation() -> None:
    settings = config()
    before = settings.model_dump()
    harness = Harness(settings)
    result = bootstrap(settings, harness.project)
    assert set(result) == set(AgentRole)
    assert result[AgentRole.ORCHESTRATOR].principal_id == ORCH[0]
    assert result[AgentRole.RECIPE_NUTRITION].principal_id == RECIPE[0]
    assert harness.project.agents.create_version.call_count == 2
    coordinator = harness.versions["orchestrator-agent"].definition.as_dict()
    specialist = harness.versions["recipe-agent"].definition.as_dict()
    assert coordinator["tools"][0]["name"] == "recipe_specialist"
    assert coordinator["tools"][0]["strict"] is True
    assert specialist["tools"] == []
    assert specialist["tool_choice"] == "none"
    assert specialist["text"]["format"]["strict"] is True
    assert (
        coordinator["rai_config"]
        == specialist["rai_config"]
        == {
            "rai_policy_name": (
                "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/synthetic"
                "/providers/Microsoft.CognitiveServices/accounts/synthetic"
                "/raiPolicies/asc-responsible-ai-v1"
            )
        }
    )
    for name in harness.versions:
        harness.project.agents.get_version.assert_any_call(name, "7")
    assert settings.model_dump() == before
    harness.project.agents.delete.assert_not_called()
    harness.project.agents.enable.assert_not_called()
    harness.project.agents.update_details.assert_not_called()


@pytest.mark.parametrize("seeded", [[], [AgentRole.ORCHESTRATOR], list(AgentRole)])
def test_rerun_reuses_only_matching_sources_and_creates_only_missing_agents(
    seeded: list[AgentRole],
) -> None:
    harness = Harness(config())
    for role in seeded:
        harness.seed(role)
    bootstrap(harness.config, harness.project)
    assert harness.project.agents.create_version.call_count == 2 - len(seeded)
    harness.project.agents.create_version.reset_mock()
    bootstrap(harness.config, harness.project)
    harness.project.agents.create_version.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "local"},
        {"environment": "prod"},
        {"agents_enabled": False},
        {"app_workload_principal_id": None},
        {"foundry_orchestrator_agent_name": None},
        {"foundry_recipe_nutrition_agent_name": "orchestrator-agent"},
        {"foundry_recipe_nutrition_rai_policy_name": None},
        {"foundry_recipe_nutrition_rai_policy_name": "other-account/policy"},
        {"foundry_project_resource_id": None},
        {
            "foundry_project_endpoint": (
                "https://other-account.services.ai.azure.com/api/projects/recipes"
            )
        },
        {"foundry_iq_connection_name": None},
    ],
)
def test_invalid_configuration_fails_before_remote_reads_or_writes(changes: dict[str, Any]) -> None:
    harness = Harness(config(**changes))
    with pytest.raises(ConfigurationError):
        bootstrap(harness.config, harness.project)
    harness.project.agents.get.assert_not_called()
    harness.project.agents.create_version.assert_not_called()


@pytest.mark.parametrize(
    "change", ["definition", "fingerprint", "version", "name", "status", "draft"]
)
def test_existing_agent_drift_fails_before_creating_the_other_agent(change: str) -> None:
    harness = Harness(config())
    harness.seed(AgentRole.ORCHESTRATOR)
    existing = harness.versions["orchestrator-agent"]
    if change == "definition":
        existing.definition = PromptAgentDefinition(
            model="synthetic-model", instructions="synthetic drift"
        )
    elif change == "fingerprint":
        existing.metadata = {"asc_definition_sha256": "wrong"}
    elif change == "version":
        existing.version = "latest"
    elif change == "name":
        existing.name = "wrong-agent"
    elif change == "status":
        existing.status = "creating"
    else:
        existing.draft = True
    with pytest.raises(ConfigurationError):
        bootstrap(harness.config, harness.project)
    harness.project.agents.create_version.assert_not_called()


@pytest.mark.parametrize("fault", ["disabled", "legacy", "workload", "malformed", "wrong-pin"])
def test_existing_identity_failure_prevents_any_creation(fault: str) -> None:
    settings = config(
        **({"foundry_orchestrator_agent_principal_id": RECIPE[0]} if fault == "wrong-pin" else {})
    )
    harness = Harness(settings)
    harness.seed(AgentRole.ORCHESTRATOR)
    if fault == "disabled":
        harness.disabled.add("orchestrator-agent")
    elif fault == "legacy":
        original = harness.get

        def legacy(name: str) -> SimpleNamespace:
            agent = original(name)
            agent.instance_identity = None
            return agent

        harness.project.agents.get.side_effect = legacy
    elif fault == "workload":
        harness.identities["orchestrator-agent"] = (APP, ORCH[1])
    elif fault == "malformed":
        harness.identities["orchestrator-agent"] = ("", ORCH[1])
    with pytest.raises(IdentityError):
        bootstrap(settings, harness.project)
    harness.project.agents.create_version.assert_not_called()


def test_shared_existing_identities_are_rejected_without_writes() -> None:
    harness = Harness(config())
    for role in AgentRole:
        harness.seed(role)
    harness.identities["recipe-agent"] = ORCH
    with pytest.raises(IdentityError):
        bootstrap(harness.config, harness.project)
    harness.project.agents.create_version.assert_not_called()


def test_shared_new_identities_do_not_produce_a_successful_bootstrap() -> None:
    harness = Harness(config())
    harness.identities["recipe-agent"] = ORCH
    with pytest.raises(IdentityError):
        bootstrap(harness.config, harness.project)
    assert harness.project.agents.create_version.call_count == 2
    harness.project.agents.delete.assert_not_called()


def test_non_404_not_found_error_is_not_treated_as_permission_to_create() -> None:
    harness = Harness(config())
    error = ResourceNotFoundError("synthetic ambiguous absence")
    error.status_code = 403
    harness.project.agents.get.side_effect = error
    with pytest.raises(ResourceNotFoundError):
        bootstrap(harness.config, harness.project)
    harness.project.agents.create_version.assert_not_called()


@pytest.mark.parametrize("error_type", [HttpResponseError, ServiceResponseError])
def test_read_failures_are_not_treated_as_absent_agents(error_type: type[Exception]) -> None:
    harness = Harness(config())
    harness.project.agents.get.side_effect = error_type("synthetic private failure")
    with pytest.raises(error_type):
        bootstrap(harness.config, harness.project)
    harness.project.agents.create_version.assert_not_called()


def test_ambiguous_create_is_not_retried_and_can_be_reconciled_on_a_later_run() -> None:
    harness = Harness(config())

    def lost_response(**kwargs: Any) -> AgentVersionDetails:
        harness.create(**kwargs)
        raise ServiceResponseError("synthetic committed response lost")

    harness.project.agents.create_version.side_effect = lost_response
    with pytest.raises(ServiceResponseError):
        bootstrap(harness.config, harness.project)
    assert harness.project.agents.create_version.call_count == 1
    assert set(harness.versions) == {"orchestrator-agent"}
    harness.project.agents.create_version.side_effect = harness.create
    result = bootstrap(harness.config, harness.project)
    assert len(result) == 2
    assert harness.project.agents.create_version.call_count == 2


def test_created_version_is_verified_again_after_identity_reads() -> None:
    harness = Harness(config())
    original = harness.get_version

    def drift(name: str, number: str) -> AgentVersionDetails:
        result = copy.deepcopy(original(name, number))
        result.definition = PromptAgentDefinition(
            model="synthetic-model", instructions="synthetic drift"
        )
        return result

    harness.project.agents.get_version.side_effect = drift
    with pytest.raises(ConfigurationError):
        bootstrap(harness.config, harness.project)


def test_inspection_reports_only_difference_paths_without_creating_or_printing_values() -> None:
    harness = Harness(config())
    harness.seed(AgentRole.ORCHESTRATOR)
    actual = harness.versions["orchestrator-agent"]
    assert isinstance(actual.definition, PromptAgentDefinition)
    actual.definition.instructions = "private provider instructions"
    actual.definition.temperature = 1
    actual.metadata = {"asc_definition_sha256": "private provider metadata"}
    report = bootstrap_agents.inspect_sources(harness.config, harness.project)
    coordinator = report["orchestrator"]
    assert isinstance(coordinator, dict)
    assert coordinator["definition_difference_paths"] == [
        "definition.instructions",
        "definition.temperature (added)",
    ]
    assert coordinator["status"] == "active" and coordinator["version"] == "7"
    assert report["recipe_nutrition"] == {"exists": False}
    assert "private" not in str(report)
    harness.project.agents.create_version.assert_not_called()
    harness.project.agents.delete.assert_not_called()
    assert bootstrap_agents.definition_difference_paths(
        {"tools": [{"type": "function"}]},
        {"tools": [{"type": "mcp", "private-key": "private-value"}, {}]},
    ) == [
        "definition.tools (length)",
        "definition.tools[0].<unrecognized-field> (added)",
        "definition.tools[0].type",
    ]


def test_cli_inspection_never_enters_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["bootstrap_agents", "--inspect"])
    monkeypatch.setattr(bootstrap_agents, "load_settings", lambda: config())
    monkeypatch.setattr(bootstrap_agents, "AzureCliCredential", MagicMock())
    monkeypatch.setattr(bootstrap_agents, "AIProjectClient", MagicMock())
    inspection = Mock(return_value={})
    creation = Mock()
    monkeypatch.setattr(bootstrap_agents, "inspect_sources", inspection)
    monkeypatch.setattr(bootstrap_agents, "bootstrap", creation)
    bootstrap_agents.main()
    inspection.assert_called_once()
    creation.assert_not_called()


def test_inspection_recognizes_exact_retrieval_only_filter_encoding() -> None:
    harness = Harness(config())
    harness.seed(AgentRole.RECIPE_NUTRITION, phase=RecipePhase.RETRIEVAL)
    definition = harness.versions["recipe-agent"].definition
    assert isinstance(definition, PromptAgentDefinition) and definition.tools is not None
    tool = definition.tools[0]
    assert isinstance(tool, MCPTool)
    tool.allowed_tools = MCPToolFilter(tool_names=["knowledge_base_retrieve"])
    report = bootstrap_agents.inspect_sources(
        harness.config, harness.project, phase=RecipePhase.RETRIEVAL
    )
    specialist = report["recipe_nutrition"]
    assert isinstance(specialist, dict)
    assert specialist["retrieval_only_filter_encoding"] is True
    assert specialist["definition_matches"] is True
    tool.allowed_tools = MCPToolFilter(tool_names=["knowledge_base_retrieve"], read_only=True)
    report = bootstrap_agents.inspect_sources(
        harness.config, harness.project, phase=RecipePhase.RETRIEVAL
    )
    specialist = report["recipe_nutrition"]
    assert isinstance(specialist, dict)
    assert specialist["retrieval_only_filter_encoding"] is False
    harness.project.agents.create_version.assert_not_called()


def test_cli_requires_explicit_owner_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["bootstrap_agents"])
    client = Mock()
    monkeypatch.setattr(bootstrap_agents, "AIProjectClient", client)
    with pytest.raises(SystemExit) as error:
        bootstrap_agents.main()
    assert error.value.code == 2
    client.assert_not_called()


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, None])
def test_cli_failure_hides_provider_details_and_never_claims_success(
    status: int | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["bootstrap_agents", "--approve-dev-bootstrap"])
    monkeypatch.setattr(bootstrap_agents, "load_settings", lambda: config())
    monkeypatch.setattr(bootstrap_agents, "AzureCliCredential", MagicMock())
    monkeypatch.setattr(bootstrap_agents, "AIProjectClient", MagicMock())
    failure = HttpResponseError("private endpoint")
    failure.status_code = status
    failure.add_note("private request body")
    failure.add_note("bootstrap step=create_version role=orchestrator")
    monkeypatch.setattr(bootstrap_agents, "bootstrap", Mock(side_effect=failure))
    with pytest.raises(SystemExit) as error:
        bootstrap_agents.main()
    assert "private endpoint" not in str(error.value)
    assert "private request body" not in str(error.value)
    assert "step=create_version role=orchestrator" in str(error.value)
    if status is not None:
        assert f"HTTP {status}" in str(error.value)
    else:
        assert "No HTTP status was available" in str(error.value)
    assert "partially completed" in str(error.value)
    assert "no release was activated" in str(error.value)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "step", ["read_agent", "read_version", "create_version", "resolve_identities"]
)
def test_request_step_preserves_errors_and_attaches_only_local_context(
    step: bootstrap_agents.RequestStep,
) -> None:
    failure = ServiceResponseError("private provider response")
    with pytest.raises(ServiceResponseError) as caught:
        with bootstrap_agents.request_step(step, AgentRole.ORCHESTRATOR):
            raise failure
    assert caught.value is failure
    detail = bootstrap_agents.azure_failure_detail(failure)
    assert f"step={step} role=orchestrator" in detail
    assert "No HTTP status was available" in detail
    assert "private provider response" not in detail


@pytest.mark.parametrize("param", ["definition.rai_config.rai_policy_name", "private value"])
def test_failure_details_allow_only_known_request_field_identifiers(param: str) -> None:
    failure = HttpResponseError("private response")
    failure.status_code = 400
    failure.model = ApiErrorResponse(
        {
            "error": {
                "code": "private value",
                "message": "private response",
                "param": param,
                "additionalInfo": {"private": "private value"},
            }
        }
    )
    detail = bootstrap_agents.azure_failure_detail(failure)
    assert "private" not in detail
    assert ("Rejected field:" in detail) == (param != "private value")


def test_cli_prints_only_verified_source_pins_and_identity_ids(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["bootstrap_agents", "--approve-dev-bootstrap"])
    monkeypatch.setattr(bootstrap_agents, "load_settings", lambda: config())
    credential = Mock()
    client = Mock()
    monkeypatch.setattr(bootstrap_agents, "AzureCliCredential", credential)
    monkeypatch.setattr(bootstrap_agents, "AIProjectClient", client)
    harness = Harness(config())
    client.return_value.__enter__ = Mock(return_value=harness.project)
    client.return_value.__exit__ = Mock(return_value=False)
    credential.return_value.__enter__ = Mock(return_value=credential)
    credential.return_value.__exit__ = Mock(return_value=False)
    bootstrap_agents.main()
    output = capsys.readouterr().out
    assert '"orchestrator": {' in output and '"recipe_nutrition": {' in output
    assert output.count('"version": "7"') == 2
    assert ORCH[0] in output and RECIPE[0] in output
    assert "app invocation remains unchanged" in output
    assert "synthetic.services.ai.azure.com" not in output
    assert client.call_args.kwargs["api_version"] == "v1"
    assert client.call_args.kwargs["retry_total"] == 0
