"""Synthetic-only observations through production orchestration, not a deployed bypass."""

import asyncio
import json
import sys
from pathlib import Path
from time import monotonic
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sous_chef.adapters.foundry_gateway import FoundryGateway
from sous_chef.adapters.usda_mcp import UsdaMcpConfig
from sous_chef.domain.context import RequestContext
from sous_chef.domain.foundry import FoundryFailure
from sous_chef.domain.groceries import GroceryRow
from sous_chef.domain.orchestrator import (
    CoordinationRequest,
    CoordinationStep,
    OrchestrationResult,
    OrchestratorAnswer,
    OrchestratorError,
    SpecialistArguments,
    SpecialistCall,
    TokenUsage,
)
from sous_chef.domain.recipes import (
    CalorieEstimate,
    CalorieGroundingRequest,
    CombinedRecipeGenerationRequest,
    GroundedRecipeGenerationRequest,
    NutritionEvidence,
    NutritionRequest,
    NutritionTool,
    RecipeDraft,
    RecipeGenerationRequest,
    RecipeGrounding,
    RecipeToolError,
    WebGrounding,
    WebGroundingRequest,
)
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.services.orchestrator import OrchestratorService
from sous_chef.services.recipe_specialist import RecipeSpecialistService
from sous_chef.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
Metric = Literal[
    "relevance", "groundedness", "grocery_utilization", "feasibility", "controlled_creativity"
]
METRICS: tuple[Metric, ...] = (
    "relevance",
    "groundedness",
    "grocery_utilization",
    "feasibility",
    "controlled_creativity",
)


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Case(EvidenceModel):
    id: str
    request: str
    expected: tuple[Literal["candidate", "clarification", "unable", "error"], ...]
    web: Literal["required", "forbidden"]
    fault: Literal["none", "nutrition_timeout", "missing_nutrition"] = "none"
    minutes: int = Field(default=30, ge=5, le=240)
    groceries: tuple[GroceryRow, ...] | None = None
    nutrition_goals: tuple[str, ...] | None = None
    require_shopping: bool = False
    measure_pantry_utilization: bool = True
    judge_metrics: tuple[Metric, ...] = METRICS

    def context(self, defaults: RequestContext) -> RequestContext:
        preferences = defaults.explicit_preferences.model_copy(
            update={
                "typical_time_budget_minutes": self.minutes,
                "nutrition_goals": self.nutrition_goals
                if self.nutrition_goals is not None
                else defaults.explicit_preferences.nutrition_goals,
            }
        )
        return defaults.model_copy(
            update={
                "current_request": self.request,
                "explicit_preferences": preferences,
                "groceries": self.groceries if self.groceries is not None else defaults.groceries,
            }
        )


class ToolEvent(EvidenceModel):
    name: Literal["foundry_iq", "usda", "web_iq"]
    status: Literal["started", "completed", "error"] = "started"
    fault_injected: bool = False


class Trace(EvidenceModel):
    tools: list[ToolEvent] = Field(default_factory=list)
    model_attempts: int = 0
    usage: list[TokenUsage] = Field(default_factory=list)
    specialist_arguments: list[SpecialistArguments] = Field(default_factory=list)
    grounding: list[RecipeGrounding] = Field(default_factory=list)
    nutrition_requests: list[NutritionRequest] = Field(default_factory=list)
    nutrition: list[NutritionEvidence] = Field(default_factory=list)
    web: list[WebGrounding] = Field(default_factory=list)


class Observation(EvidenceModel):
    case_id: str
    result: OrchestrationResult | None = None
    error_code: str | None = None
    trace: Trace
    duration_ms: float = Field(ge=0)


class Gateway(Protocol):
    async def estimate_calories(
        self, request: CalorieGroundingRequest
    ) -> tuple[tuple[CalorieEstimate, ...], TokenUsage]: ...

    async def coordinate(self, request: CoordinationRequest) -> CoordinationStep: ...

    async def generate_recipe(
        self, request: RecipeGenerationRequest
    ) -> tuple[RecipeDraft, TokenUsage]: ...

    async def generate_grounded_recipe(
        self, request: GroundedRecipeGenerationRequest
    ) -> tuple[RecipeDraft, RecipeGrounding, TokenUsage]: ...

    async def ground_web(
        self, request: WebGroundingRequest
    ) -> tuple[WebGrounding | None, TokenUsage]: ...

    async def generate_combined_recipe(
        self, request: CombinedRecipeGenerationRequest
    ) -> tuple[RecipeDraft, RecipeGrounding, WebGrounding | None, TokenUsage]: ...


class ObservedGateway:
    def __init__(self, gateway: Gateway, trace: Trace) -> None:
        self.gateway, self.trace = gateway, trace

    async def estimate_calories(
        self, request: CalorieGroundingRequest
    ) -> tuple[tuple[CalorieEstimate, ...], TokenUsage]:
        event = ToolEvent(name="web_iq")
        self.trace.tools.append(event)
        self.trace.model_attempts += 1
        try:
            result = await self.gateway.estimate_calories(request)
        except BaseException:
            event.status = "error"
            raise
        event.status = "completed"
        self.trace.usage.append(result[1])
        return result

    async def coordinate(self, request: CoordinationRequest) -> CoordinationStep:
        self.trace.model_attempts += 1
        step = await self.gateway.coordinate(request)
        self.trace.usage.append(step.usage)
        if step.call is not None:
            self.trace.specialist_arguments.append(step.call.arguments)
        return step

    async def generate_recipe(
        self, request: RecipeGenerationRequest
    ) -> tuple[RecipeDraft, TokenUsage]:
        self.trace.model_attempts += 1
        result = await self.gateway.generate_recipe(request)
        self.trace.usage.append(result[1])
        return result

    async def generate_grounded_recipe(
        self, request: GroundedRecipeGenerationRequest
    ) -> tuple[RecipeDraft, RecipeGrounding, TokenUsage]:
        event = ToolEvent(name="foundry_iq")
        self.trace.tools.append(event)
        self.trace.model_attempts += 1
        try:
            result = await self.gateway.generate_grounded_recipe(request)
        except BaseException:
            event.status = "error"
            raise
        event.status = "completed"
        self.trace.grounding.append(result[1])
        self.trace.usage.append(result[2])
        return result

    async def ground_web(
        self, request: WebGroundingRequest
    ) -> tuple[WebGrounding | None, TokenUsage]:
        event = ToolEvent(name="web_iq")
        self.trace.tools.append(event)
        self.trace.model_attempts += 1
        try:
            result = await self.gateway.ground_web(request)
        except BaseException:
            event.status = "error"
            raise
        event.status = "completed"
        if result[0] is not None:
            self.trace.web.append(result[0])
        self.trace.usage.append(result[1])
        return result

    async def generate_combined_recipe(
        self, request: CombinedRecipeGenerationRequest
    ) -> tuple[RecipeDraft, RecipeGrounding, WebGrounding | None, TokenUsage]:
        events = [ToolEvent(name="foundry_iq")]
        if request.web_query is not None:
            events.append(ToolEvent(name="web_iq"))
        self.trace.tools.extend(events)
        self.trace.model_attempts += 1
        try:
            result = await self.gateway.generate_combined_recipe(request)
        except BaseException:
            for event in events:
                event.status = "error"
            raise
        for event in events:
            event.status = "completed"
        self.trace.grounding.append(result[1])
        if result[2] is not None:
            self.trace.web.append(result[2])
        self.trace.usage.append(result[3])
        return result


class ObservedNutrition:
    def __init__(self, tool: NutritionTool, trace: Trace, fault: str) -> None:
        self.tool, self.trace, self.fault = tool, trace, fault

    async def lookup(self, request: NutritionRequest, *, deadline: float) -> NutritionEvidence:
        event = ToolEvent(name="usda", fault_injected=self.fault != "none")
        self.trace.tools.append(event)
        self.trace.nutrition_requests.append(request)
        try:
            if self.fault == "nutrition_timeout":
                raise RecipeToolError(FoundryFailure.TIMEOUT)
            if self.fault == "missing_nutrition":
                result = NutritionEvidence(
                    status="unavailable",
                    serving_basis="per_serving",
                    servings=request.servings,
                    nutrients=(),
                    mappings=(),
                    sources=(),
                )
            else:
                result = await self.tool.lookup(request, deadline=deadline)
        except BaseException:
            event.status = "error"
            raise
        event.status = "completed"
        self.trace.nutrition.append(result)
        return result


class ReplayGateway:
    """Fixed test responses exercise the harness; they are not model-quality measurements."""

    def __init__(self, case: Case) -> None:
        self.case = case
        self.fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "recipe-specialist.json").read_text(encoding="utf-8")
        )

    async def estimate_calories(
        self, request: CalorieGroundingRequest
    ) -> tuple[tuple[CalorieEstimate, ...], TokenUsage]:
        raise AssertionError("This quality replay has no calorie fallback fixture.")

    async def coordinate(self, request: CoordinationRequest) -> CoordinationStep:
        usage = TokenUsage(input_tokens=100, output_tokens=50)
        if self.case.id == "unrealistic-time":
            return CoordinationStep(
                usage=usage,
                answer=OrchestratorAnswer(
                    status="clarification",
                    explanation="A whole raw turkey cannot roast in five minutes. Change the time?",
                ),
            )
        if request.previous_call is not None:
            return CoordinationStep(
                usage=usage,
                answer=OrchestratorAnswer(status="accept", explanation="Fits the time budget."),
            )
        return CoordinationStep(
            usage=usage,
            call=SpecialistCall(
                call_id="synthetic-call",
                arguments=SpecialistArguments(
                    requirements=self.case.request,
                    time_budget_minutes=self.case.minutes,
                    use_web=self.case.web == "required",
                    web_query="current FDA food recall information"
                    if self.case.web == "required"
                    else None,
                ),
            ),
        )

    async def generate_recipe(
        self, request: RecipeGenerationRequest
    ) -> tuple[RecipeDraft, TokenUsage]:
        return (
            RecipeDraft.model_validate_json(json.dumps(self.fixture["draft"])),
            TokenUsage(input_tokens=400, output_tokens=200),
        )

    async def generate_grounded_recipe(
        self, request: GroundedRecipeGenerationRequest
    ) -> tuple[RecipeDraft, RecipeGrounding, TokenUsage]:
        draft, usage = await self.generate_recipe(request)
        return (
            draft,
            RecipeGrounding.model_validate_json(json.dumps(self.fixture["grounding"])),
            usage,
        )

    async def ground_web(self, request: WebGroundingRequest) -> tuple[WebGrounding, TokenUsage]:
        return (
            WebGrounding.model_validate_json(
                json.dumps(
                    {
                        "claims": [
                            {
                                "text": "Synthetic recall fixture; not current FDA information.",
                                "sourceUrl": "https://fda.example.invalid/synthetic",
                            }
                        ],
                        "citations": [
                            {
                                "url": "https://fda.example.invalid/synthetic",
                                "title": "Synthetic recall fixture",
                            }
                        ],
                        "retrievedAt": "2026-01-01T00:00:00Z",
                    }
                )
            ),
            TokenUsage(input_tokens=100, output_tokens=50),
        )

    async def generate_combined_recipe(
        self, request: CombinedRecipeGenerationRequest
    ) -> tuple[RecipeDraft, RecipeGrounding, WebGrounding | None, TokenUsage]:
        draft, grounding, usage = await self.generate_grounded_recipe(request)
        web = None
        if request.web_query is not None:
            web, _ = await self.ground_web(
                WebGroundingRequest(
                    role=request.role,
                    operation_id=request.operation_id,
                    prompt=request.web_query,
                    max_input_tokens=2000,
                    max_output_tokens=request.max_output_tokens,
                )
            )
        return draft, grounding, web, usage


def dependencies(case: Case, settings: Settings | None) -> tuple[Gateway, NutritionTool]:
    if settings is not None:
        return FoundryGateway(settings), UsdaNutritionTool.from_settings(settings)
    return ReplayGateway(case), UsdaNutritionTool(
        UsdaMcpConfig(
            Path(sys.executable),
            ROOT / "tests" / "fixtures" / "usda_mcp_server.py",
            {"TEST_USDA_MODE": "varied"},
            10,
        )
    )


async def capture(case: Case, defaults: RequestContext, settings: Settings | None) -> Observation:
    trace = Trace()
    gateway, nutrition = dependencies(case, settings)
    observed = ObservedGateway(gateway, trace)
    service = OrchestratorService(
        observed,
        RecipeSpecialistService(
            observed,
            None,
            ObservedNutrition(nutrition, trace, case.fault),
            combined=settings is None or settings.foundry_recipe_nutrition_mode == "combined",
        ),
    )
    started = monotonic()
    result, error = None, None
    try:
        result = await service.run(case.context(defaults))
    except OrchestratorError as exc:
        error = exc.code.value
    except asyncio.CancelledError:
        raise
    except Exception:
        # Provider exception text can include credentials or request bodies.
        error = "capture_error"
    return Observation(
        case_id=case.id,
        result=result,
        error_code=error,
        trace=trace,
        duration_ms=(monotonic() - started) * 1000,
    )
