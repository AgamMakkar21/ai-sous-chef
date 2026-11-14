import sys
from unittest.mock import MagicMock

import pytest
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from sous_chef import publish_orchestrator
from sous_chef.adapters.foundry_gateway import FoundryGateway
from sous_chef.domain.agent_identity import AgentRole
from sous_chef.domain.recipes import CombinedRecipeOutput, RecipeDraft, WebDraft
from sous_chef.publish_orchestrator import prepare_agent_source
from sous_chef.recipe_definition import RecipePhase, verify_recipe_source
from sous_chef.settings import ConfigurationError
from test_agent_bootstrap import Harness, config, version


@pytest.mark.parametrize("phase", list(RecipePhase))
def test_persisted_phase_has_only_its_capabilities_and_strict_output(phase: RecipePhase) -> None:
    source = prepare_agent_source(config(), AgentRole.RECIPE_NUTRITION, phase=phase)
    definition = source.definition.as_dict()
    assert source.name == "recipe-agent"
    assert source.metadata["asc_recipe_phase"] == phase.value
    assert source.metadata["asc_policy_version"] == (
        "12"
        if phase == RecipePhase.COMBINED
        else "11"
        if phase == RecipePhase.WEB
        else "2"
        if phase == RecipePhase.CALORIE
        else "8"
    )
    assert definition["text"]["format"]["strict"] is True
    expected_schema = (
        CombinedRecipeOutput
        if phase == RecipePhase.COMBINED
        else WebDraft
        if phase in (RecipePhase.WEB, RecipePhase.CALORIE)
        else RecipeDraft
    )
    schema = definition["text"]["format"]["schema"]
    assert schema == expected_schema.model_json_schema(
        mode="serialization"
        if phase in (RecipePhase.COMBINED, RecipePhase.WEB, RecipePhase.CALORIE)
        else "validation"
    )
    if phase in (RecipePhase.COMBINED, RecipePhase.WEB, RecipePhase.CALORIE):
        objects = [schema, *schema["$defs"].values()]
        for item in objects:
            if item.get("type") == "object":
                assert set(item["required"]) == set(item["properties"])
                assert item["additionalProperties"] is False
    assert definition["tool_choice"] == (
        "none" if phase in (RecipePhase.GENERATION, RecipePhase.CALORIE) else "required"
    )
    expected_type = {
        RecipePhase.GENERATION: [],
        RecipePhase.RETRIEVAL: ["mcp"],
        RecipePhase.WEB: ["web_search"],
        RecipePhase.COMBINED: ["mcp", "web_search"],
        RecipePhase.CALORIE: [],
    }[phase]
    assert [tool["type"] for tool in definition["tools"]] == expected_type
    tool = None
    if phase in (RecipePhase.RETRIEVAL, RecipePhase.COMBINED):
        assert source.definition.tools is not None
        assert isinstance(source.definition.tools[0], MCPTool)
        tool = source.definition.tools[0]
    verify_recipe_source(version(source), phase, knowledge_tool=tool)


@pytest.mark.parametrize(
    "fault", ["metadata", "extra-tool", "choice", "inactive", "draft", "household-policy"]
)
def test_phase_drift_is_rejected_before_release_or_readiness(fault: str) -> None:
    source = prepare_agent_source(config(), AgentRole.RECIPE_NUTRITION)
    stored = version(source)
    if fault == "metadata":
        stored.metadata["asc_recipe_phase"] = "web"
    elif fault == "extra-tool":
        web = prepare_agent_source(config(), AgentRole.RECIPE_NUTRITION, phase=RecipePhase.WEB)
        stored.definition = web.definition
    elif fault == "choice":
        assert isinstance(stored.definition, PromptAgentDefinition)
        stored.definition.tool_choice = "auto"
    elif fault == "inactive":
        stored.status = "inactive"
    elif fault == "household-policy":
        assert isinstance(stored.definition, PromptAgentDefinition)
        assert stored.definition.instructions is not None
        stored.definition.instructions = stored.definition.instructions.replace(
            "Choose quantities for newly composed recipes in grams (g).",
            "Choose quantities for newly composed recipes in household measures.",
        )
    else:
        stored.draft = True
    with pytest.raises(ConfigurationError):
        verify_recipe_source(stored, RecipePhase.GENERATION)


@pytest.mark.parametrize(
    "field",
    [
        "foundry_recipe_nutrition_agent_version",
        "foundry_recipe_nutrition_retrieval_agent_version",
        "foundry_recipe_nutrition_web_agent_version",
    ],
)
def test_gateway_requires_every_explicit_phase_pin(field: str) -> None:
    with pytest.raises(ConfigurationError):
        FoundryGateway(config(**{field: None}))


def test_phase_versions_cannot_share_a_source_pin() -> None:
    with pytest.raises(ConfigurationError):
        config(foundry_recipe_nutrition_retrieval_agent_version="3")


@pytest.mark.parametrize("phase", list(RecipePhase))
def test_owner_verifies_explicit_phase_source_without_publication(
    phase: RecipePhase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = Harness(config())
    harness.seed(AgentRole.ORCHESTRATOR)
    harness.seed(AgentRole.RECIPE_NUTRITION, phase=phase)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish_orchestrator",
            "--agent",
            "recipe_nutrition",
            "--phase",
            phase.value,
            "--verify-version",
            "7",
        ],
    )
    monkeypatch.setattr(publish_orchestrator, "load_settings", lambda: harness.config)
    factory = MagicMock()
    factory.return_value.__enter__.return_value = harness.project
    monkeypatch.setattr(publish_orchestrator, "AIProjectClient", factory)
    monkeypatch.setattr(publish_orchestrator, "AzureCliCredential", MagicMock())
    publish_orchestrator.main()
    assert f"Verified recipe_nutrition {phase.value} source version 7." in capsys.readouterr().out
    harness.project.agents.create_version.assert_not_called()
    harness.project.get_openai_client.assert_not_called()


def test_owner_cannot_publish_specialist_phase_to_orchestrator() -> None:
    with pytest.raises(ConfigurationError):
        prepare_agent_source(config(), AgentRole.ORCHESTRATOR, phase=RecipePhase.WEB)
