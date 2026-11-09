"""Compose the managed agents; durable admission and final delivery remain in ChatService."""

from uuid import uuid4

from sous_chef.adapters.foundry_gateway import FoundryGateway
from sous_chef.adapters.foundry_iq import RecipeKnowledgeConfig
from sous_chef.domain.chat import CLARIFICATIONS, ChatExecution, ExecutionFailed, ValidatedReply
from sous_chef.domain.foundry import FoundryFailure, FoundryGatewayError
from sous_chef.domain.orchestrator import OrchestratorError, OrchestratorFailure
from sous_chef.domain.recipe_safety import UNABLE
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.services.orchestrator import OrchestratorService
from sous_chef.services.recipe_specialist import RecipeSpecialistService
from sous_chef.settings import ConfigurationError, Settings


class RecipeChatExecutor:
    def __init__(self, orchestrator: OrchestratorService, entity_id: str) -> None:
        self._orchestrator = orchestrator
        self._entity_id = entity_id

    @classmethod
    def from_settings(
        cls, settings: Settings, *, nutrition: UsdaNutritionTool | None = None
    ) -> "RecipeChatExecutor":
        gateway = FoundryGateway(settings)
        if any(
            principal is None
            for principal in (
                settings.app_workload_principal_id,
                settings.foundry_orchestrator_agent_principal_id,
                settings.foundry_recipe_nutrition_agent_principal_id,
            )
        ):
            raise ConfigurationError("Recipe chat requires distinct workload and agent pins.")
        RecipeKnowledgeConfig.from_settings(settings)
        if nutrition is None:
            nutrition = UsdaNutritionTool.from_settings(settings)
        specialist = RecipeSpecialistService(
            gateway,
            None,
            nutrition,
            combined=settings.foundry_recipe_nutrition_mode == "combined",
        )
        assert settings.foundry_orchestrator_agent_name is not None
        return cls(
            OrchestratorService(gateway, specialist),
            settings.foundry_orchestrator_agent_name,
        )

    async def execute(self, request: ChatExecution) -> ValidatedReply:
        try:
            result = await self._orchestrator.run(
                request.context,
                operation_id=request.operation_id,
                deadline=request.deadline,
            )
        except OrchestratorError as error:
            if error.code == OrchestratorFailure.TIMEOUT or error.outcome_unknown:
                raise TimeoutError("Recipe execution outcome is unknown.") from None
            raise ExecutionFailed("Recipe execution failed.") from None
        except FoundryGatewayError as error:
            if error.code in {FoundryFailure.TIMEOUT, FoundryFailure.UNAVAILABLE}:
                raise TimeoutError("Recipe execution outcome is unknown.") from None
            raise ExecutionFailed("Recipe execution failed.") from None

        if result.status == "candidate":
            if result.recipe is None:
                raise ExecutionFailed("A complete structured recipe is required.")
            return ValidatedReply(
                content="Recipe awaiting final delivery validation.",
                kind="recipe",
                entity_id=self._entity_id,
                generated_recipe_id=uuid4(),
                recipe=result.recipe,
            )
        return ValidatedReply(
            content=UNABLE if result.status == "unable" else CLARIFICATIONS["request"],
            kind=result.status,
            entity_id=self._entity_id,
        )
