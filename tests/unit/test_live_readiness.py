import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.core.exceptions import HttpResponseError
from sous_chef.adapters.readiness import LiveReadinessProbe
from sous_chef.domain.readiness import DependencyUnavailable
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
        app_workload_principal_id="11111111-1111-1111-1111-111111111111",
        auth_issuer="https://synthetic.ciamlogin.com/tenant/v2.0",
        auth_jwks_uri="https://synthetic.ciamlogin.com/tenant/discovery/v2.0/keys",
        auth_client_id="22222222-2222-2222-2222-222222222222",
        auth_origin="https://synthetic.azurewebsites.net",
    )


@pytest.mark.parametrize("failure", ["none", "cosmos", "foundry", "shared", "disabled", "pinned"])
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
    project.agents.get_version = AsyncMock()
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
