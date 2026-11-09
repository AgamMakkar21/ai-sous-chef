import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.ai.projects.models import AgentVersionDetails
from azure.core.exceptions import HttpResponseError
from sous_chef.adapters.foundry_iq import RecipeKnowledgeConfig
from sous_chef.adapters.readiness import LiveReadinessProbe
from sous_chef.domain.readiness import DependencyUnavailable
from sous_chef.recipe_definition import (
    RecipePhase,
    build_recipe_definition,
    recipe_knowledge_tool,
    recipe_source_metadata,
)
from sous_chef.settings import Settings


def settings() -> Settings:
    return Settings(
        environment="dev",
        adapter_mode="live",
        cosmos_endpoint="https://synthetic.documents.azure.com/",
        cosmos_database="synthetic",
        foundry_project_endpoint="https://synthetic.services.ai.azure.com/api/projects/dev",
        foundry_orchestrator_deployment="orchestrator",
        foundry_recipe_nutrition_deployment="recipe-nutrition",
        foundry_extraction_deployment="extraction",
        foundry_orchestrator_agent_name="orchestrator",
        foundry_orchestrator_agent_version="2",
        foundry_recipe_nutrition_agent_name="recipe",
        foundry_recipe_nutrition_agent_version="3",
        foundry_recipe_nutrition_retrieval_agent_version="4",
        foundry_recipe_nutrition_web_agent_version="5",
        foundry_project_resource_id=(
            "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/synthetic"
            "/providers/Microsoft.CognitiveServices/accounts/synthetic/projects/dev"
        ),
        foundry_recipe_nutrition_rai_policy_name="approved-policy",
        foundry_iq_search_endpoint="https://synthetic.search.windows.net",
        foundry_iq_index_name="fixture-recipes",
        foundry_iq_knowledge_source_name="fixture-source",
        foundry_iq_knowledge_base_name="fixture-knowledge",
        foundry_iq_connection_name="fixture-mcp",
        app_workload_principal_id="11111111-1111-1111-1111-111111111111",
        auth_issuer="https://synthetic.ciamlogin.com/tenant/v2.0",
        auth_jwks_uri="https://synthetic.ciamlogin.com/tenant/discovery/v2.0/keys",
        auth_client_id="22222222-2222-2222-2222-222222222222",
        auth_origin="https://synthetic.azurewebsites.net",
    )


@pytest.mark.parametrize("drift", [False, True])
def test_combined_readiness_checks_only_the_single_reviewed_specialist_source(drift: bool) -> None:
    config = settings().model_copy(
        update={
            "foundry_recipe_nutrition_mode": "combined",
            "foundry_recipe_nutrition_retrieval_agent_version": None,
            "foundry_recipe_nutrition_web_agent_version": None,
        }
    )
    knowledge = RecipeKnowledgeConfig.from_settings(config)
    definition = build_recipe_definition(
        "recipe-nutrition",
        "approved-policy",
        phase=RecipePhase.COMBINED,
        knowledge_tool=recipe_knowledge_tool(knowledge.mcp_endpoint, knowledge.connection_name),
    )
    metadata = recipe_source_metadata(definition, RecipePhase.COMBINED)
    if drift:
        metadata["asc_recipe_phase"] = "retrieval"
    source = AgentVersionDetails(
        {
            "name": "recipe",
            "version": "3",
            "status": "active",
            "draft": False,
            "definition": definition.as_dict(),
            "metadata": metadata,
        }
    )
    credential, cosmos_context, project_context = AsyncMock(), AsyncMock(), AsyncMock()
    cosmos, project = MagicMock(), MagicMock()
    cosmos_context.__aenter__.return_value = cosmos
    project_context.__aenter__.return_value = project
    cosmos.get_database_client.return_value.get_container_client.return_value.read = AsyncMock()
    details = []
    for role in ("orchestrator", "recipe"):
        agent = MagicMock()
        agent.state = "enabled"
        agent.instance_identity.principal_id = role
        agent.instance_identity.client_id = role + "-client"
        details.append(agent)
    project.agents.get = AsyncMock(side_effect=details)
    project.agents.get_version = AsyncMock(side_effect=[MagicMock(), source])
    with (
        patch("sous_chef.adapters.readiness.build_async_credential", return_value=credential),
        patch("sous_chef.adapters.readiness.CosmosClient", return_value=cosmos_context),
        patch("sous_chef.adapters.readiness.AIProjectClient", return_value=project_context),
    ):
        if drift:
            with pytest.raises(DependencyUnavailable):
                asyncio.run(LiveReadinessProbe(config).check())
        else:
            asyncio.run(LiveReadinessProbe(config).check())
    assert project.agents.get_version.await_count == 2
    project.get_openai_client.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["none", "cosmos", "foundry", "shared", "disabled", "pinned", "phase"]
)
@pytest.mark.parametrize("session_auth", [False, True])
def test_live_probe_reads_only_metadata_and_fails_closed(failure: str, session_auth: bool) -> None:
    config = settings()
    if session_auth:
        config = Settings(
            **(
                config.model_dump()
                | {
                    "auth_mode": "app_service",
                    "auth_microsoft_tenant_id": "11111111-1111-1111-1111-111111111111",
                    "auth_issuer": None,
                    "auth_jwks_uri": None,
                    "auth_client_id": None,
                }
            )
        )
    if failure == "pinned":
        config = config.model_copy(
            update={
                "foundry_orchestrator_agent_principal_id": "33333333-3333-3333-3333-333333333333",
            }
        )
    credential, cosmos_context, project_context = AsyncMock(), AsyncMock(), AsyncMock()
    cosmos, project = MagicMock(), MagicMock()
    cosmos_context.__aenter__.return_value = cosmos
    project_context.__aenter__.return_value = project
    container = cosmos.get_database_client.return_value.get_container_client.return_value
    container.read = AsyncMock()
    if failure == "cosmos":
        container.read.side_effect = HttpResponseError("private-token")
    details = []
    for role in ("orchestrator", "recipe"):
        agent = MagicMock()
        agent.state = "disabled" if failure == "disabled" else "enabled"
        agent.instance_identity.principal_id = "shared" if failure == "shared" else role
        agent.instance_identity.client_id = role + "-client"
        details.append(agent)
    project.agents.get = AsyncMock(side_effect=details)
    sources = [
        AgentVersionDetails(
            {
                "name": "orchestrator",
                "version": "2",
                "status": "active",
                "draft": False,
                "definition": {
                    "kind": "prompt",
                    "model": "orchestrator",
                    "instructions": "approved",
                },
                "metadata": {},
            }
        )
    ]
    knowledge = RecipeKnowledgeConfig.from_settings(config)
    for number, phase in zip(
        ("3", "4", "5"),
        (
            RecipePhase.GENERATION,
            RecipePhase.RETRIEVAL,
            RecipePhase.WEB,
        ),
        strict=True,
    ):
        definition = build_recipe_definition(
            "recipe-nutrition",
            "approved-policy",
            phase=phase,
            knowledge_tool=recipe_knowledge_tool(knowledge.mcp_endpoint, knowledge.connection_name)
            if phase == RecipePhase.RETRIEVAL
            else None,
        )
        metadata = recipe_source_metadata(definition, phase)
        if failure == "phase" and phase == RecipePhase.RETRIEVAL:
            metadata["asc_recipe_phase"] = "generation"
        sources.append(
            AgentVersionDetails(
                {
                    "name": "recipe",
                    "version": number,
                    "status": "active",
                    "draft": False,
                    "definition": definition.as_dict(),
                    "metadata": metadata,
                }
            )
        )
    project.agents.get_version = AsyncMock(side_effect=sources)
    if failure == "foundry":
        project.agents.get_version.side_effect = HttpResponseError("private-token")
    with (
        patch("sous_chef.adapters.readiness.build_async_credential", return_value=credential),
        patch("sous_chef.adapters.readiness.CosmosClient", return_value=cosmos_context),
        patch("sous_chef.adapters.readiness.AIProjectClient", return_value=project_context),
    ):
        if failure == "none":
            asyncio.run(LiveReadinessProbe(config).check())
            database = cosmos.get_database_client.return_value
            assert [call.args[0] for call in database.get_container_client.call_args_list] == [
                "Users",
                "ChatHistory",
                "RecipeHistory",
            ]
            assert [call.args for call in project.agents.get_version.call_args_list] == [
                ("orchestrator", "2"),
                ("recipe", "3"),
                ("recipe", "4"),
                ("recipe", "5"),
            ]
        else:
            with pytest.raises(DependencyUnavailable, match="verification failed"):
                asyncio.run(LiveReadinessProbe(config).check())
        container.query_items.assert_not_called()
        container.read_item.assert_not_called()
        project.get_openai_client.assert_not_called()
        if failure != "cosmos":
            project_context.__aexit__.assert_awaited()
        cosmos_context.__aexit__.assert_awaited()
        credential.__aexit__.assert_awaited()


def test_unconfigured_live_probe_does_not_attempt_credentials() -> None:
    config = settings().model_copy(update={"foundry_orchestrator_agent_version": None})
    with patch("sous_chef.adapters.readiness.build_async_credential") as credential:
        with pytest.raises(DependencyUnavailable, match="incomplete"):
            asyncio.run(LiveReadinessProbe(config).check())
        credential.assert_not_called()


@pytest.mark.parametrize("cosmos_failure", [False, True])
@pytest.mark.parametrize("stale_agent_pins", [False, True])
def test_app_only_probe_keeps_cosmos_checks_without_contacting_foundry(
    cosmos_failure: bool, stale_agent_pins: bool
) -> None:
    values = settings().model_dump() | {"agents_enabled": False}
    if not stale_agent_pins:
        values.update(
            {
                "foundry_project_endpoint": None,
                "foundry_orchestrator_deployment": None,
                "foundry_recipe_nutrition_deployment": None,
                "foundry_extraction_deployment": None,
                "foundry_orchestrator_agent_name": None,
                "foundry_recipe_nutrition_agent_name": None,
                "foundry_orchestrator_agent_version": None,
                "foundry_recipe_nutrition_agent_version": None,
            }
        )
    config = Settings(**values)
    credential, context = AsyncMock(), AsyncMock()
    cosmos = MagicMock()
    context.__aenter__.return_value = cosmos
    database = cosmos.get_database_client.return_value
    container = database.get_container_client.return_value
    container.read = AsyncMock()
    if cosmos_failure:
        container.read.side_effect = HttpResponseError("private-token")
    with (
        patch("sous_chef.adapters.readiness.build_async_credential", return_value=credential),
        patch("sous_chef.adapters.readiness.CosmosClient", return_value=context),
        patch("sous_chef.adapters.readiness.AIProjectClient") as foundry,
    ):
        if cosmos_failure:
            with pytest.raises(DependencyUnavailable, match="verification failed"):
                asyncio.run(LiveReadinessProbe(config).check())
        else:
            asyncio.run(LiveReadinessProbe(config).check())
            assert [call.args[0] for call in database.get_container_client.call_args_list] == [
                "Users",
                "ChatHistory",
                "RecipeHistory",
            ]
        foundry.assert_not_called()
        container.read_item.assert_not_called()
        container.query_items.assert_not_called()
        context.__aexit__.assert_awaited()
        credential.__aexit__.assert_awaited()


@pytest.mark.parametrize("missing", ["auth_issuer", "app_workload_principal_id"])
def test_app_only_still_requires_authentication_and_workload_configuration(missing: str) -> None:
    config = settings().model_copy(update={"agents_enabled": False, missing: None})
    with patch("sous_chef.adapters.readiness.build_async_credential") as credential:
        with pytest.raises(DependencyUnavailable, match="incomplete"):
            asyncio.run(LiveReadinessProbe(config).check())
        credential.assert_not_called()
