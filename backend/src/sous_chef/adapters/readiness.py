from azure.ai.projects.aio import AIProjectClient
from azure.core.exceptions import AzureError
from azure.cosmos.aio import CosmosClient

from sous_chef.adapters.credentials import build_async_credential
from sous_chef.adapters.foundry_iq import RecipeKnowledgeConfig
from sous_chef.domain.agent_identity import AgentIdentity, AgentRole, IdentityError, verify_distinct
from sous_chef.domain.readiness import DependencyUnavailable
from sous_chef.recipe_definition import RecipePhase, recipe_knowledge_tool, verify_recipe_source
from sous_chef.settings import ConfigurationError, Settings


class LocalReadinessProbe:
    async def check(self) -> None:
        return None


class UnconfiguredReadinessProbe:
    async def check(self) -> None:
        raise DependencyUnavailable("Live dependency configuration is incomplete.")


class LiveReadinessProbe:
    """Read metadata with the workload identity; never generate text or read user documents."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def check(self) -> None:
        settings = self._settings
        targets = (
            (
                AgentRole.ORCHESTRATOR,
                settings.foundry_orchestrator_agent_name,
                settings.foundry_orchestrator_agent_version,
                settings.foundry_orchestrator_agent_principal_id,
            ),
            (
                AgentRole.RECIPE_NUTRITION,
                settings.foundry_recipe_nutrition_agent_name,
                settings.foundry_recipe_nutrition_agent_version,
                settings.foundry_recipe_nutrition_agent_principal_id,
            ),
        )
        if (
            settings.cosmos_database is None
            or (
                settings.auth_origin is None
                if settings.auth_mode == "app_service"
                else settings.auth_issuer is None
            )
            or settings.app_workload_principal_id is None
            or (
                settings.agents_enabled
                and (
                    any(not name or not version for _, name, version, _ in targets)
                    or settings.foundry_recipe_nutrition_mode == "phased"
                    and (
                        not settings.foundry_recipe_nutrition_retrieval_agent_version
                        or not settings.foundry_recipe_nutrition_web_agent_version
                    )
                )
            )
        ):
            raise DependencyUnavailable("Live dependency configuration is incomplete.")
        try:
            async with build_async_credential(settings) as credential:
                async with CosmosClient(
                    str(settings.cosmos_endpoint), credential=credential, retry_total=0
                ) as cosmos:
                    database = cosmos.get_database_client(settings.cosmos_database)
                    for container_name in ("Users", "ChatHistory", "RecipeHistory"):
                        await database.get_container_client(container_name).read()
                if settings.agents_enabled:
                    async with AIProjectClient(
                        endpoint=str(settings.foundry_project_endpoint),
                        credential=credential,
                        api_version="v1",
                        retry_total=0,
                    ) as project:
                        identities = []
                        for role, name, version, expected in targets:
                            assert name is not None and version is not None
                            details = await project.agents.get(name)
                            instance = details.instance_identity
                            if (
                                details.state != "enabled"
                                or instance is None
                                or (
                                    expected is not None
                                    and expected.lower() != instance.principal_id.lower()
                                )
                            ):
                                raise DependencyUnavailable("Agent identity verification failed.")
                            identities.append(
                                AgentIdentity(role, name, instance.principal_id, instance.client_id)
                            )
                            source = await project.agents.get_version(name, version)
                            if role == AgentRole.RECIPE_NUTRITION:
                                if source.name != name or source.version != version:
                                    raise DependencyUnavailable(
                                        "Specialist phase pin verification failed."
                                    )
                                knowledge = RecipeKnowledgeConfig.from_settings(settings)
                                if settings.foundry_recipe_nutrition_mode == "combined":
                                    verify_recipe_source(
                                        source,
                                        RecipePhase.COMBINED,
                                        knowledge_tool=recipe_knowledge_tool(
                                            knowledge.mcp_endpoint,
                                            knowledge.connection_name,
                                        ),
                                    )
                                    continue
                                verify_recipe_source(source, RecipePhase.GENERATION)
                                for phase, pin in (
                                    (
                                        RecipePhase.RETRIEVAL,
                                        settings.foundry_recipe_nutrition_retrieval_agent_version,
                                    ),
                                    (
                                        RecipePhase.WEB,
                                        settings.foundry_recipe_nutrition_web_agent_version,
                                    ),
                                ):
                                    assert pin is not None
                                    phase_source = await project.agents.get_version(name, pin)
                                    if phase_source.name != name or phase_source.version != pin:
                                        raise DependencyUnavailable(
                                            "Specialist phase pin verification failed."
                                        )
                                    verify_recipe_source(
                                        phase_source,
                                        phase,
                                        knowledge_tool=recipe_knowledge_tool(
                                            knowledge.mcp_endpoint, knowledge.connection_name
                                        )
                                        if phase == RecipePhase.RETRIEVAL
                                        else None,
                                    )
                        verify_distinct(identities, settings.app_workload_principal_id)
        except (AzureError, IdentityError, ConfigurationError):
            raise DependencyUnavailable("Live dependency verification failed.") from None
