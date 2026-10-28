"""Backend-enforced routing/revision policy; live recipe/chat composition is ASC-38."""

import asyncio
import json
from math import isfinite
from time import monotonic
from uuid import UUID, uuid4

from pydantic import ValidationError

from sous_chef.domain.agent_identity import AgentRole, AgentTool, ToolDenied, authorize_tool
from sous_chef.domain.context import RequestContext
from sous_chef.domain.food_safety import (
    IngredientLine,
    SafetyProfile,
    Verdict,
    validate_ingredients,
)
from sous_chef.domain.foundry import FoundryGatewayError
from sous_chef.domain.orchestrator import (
    POLICY,
    CoordinationPrompt,
    CoordinationRequest,
    Coordinator,
    OrchestrationResult,
    OrchestratorError,
    OrchestratorFailure,
    RecipeCandidate,
    RecipeSpecialist,
    SpecialistCall,
    TokenUsage,
)
from sous_chef.telemetry import ErrorCategory, Event, operational_event, trace_id


class ExecutionBudget:
    """One request-local budget; never reuse it across users, turns, or requests."""

    def __init__(self, operation_id: UUID, deadline: float = float("inf")) -> None:
        self.operation_id = operation_id
        self.deadline = deadline
        self.model_calls = 0
        self.specialist_calls = 0
        self.tool_calls = 0
        self.web_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.web_allowed = False

    def fail(self) -> None:
        raise OrchestratorError(OrchestratorFailure.BUDGET_EXHAUSTED, self.operation_id)

    def model(self, *, specialist: bool = False) -> None:
        self._check_deadline()
        if (
            self.model_calls >= POLICY.max_model_calls
            or self.output_tokens + POLICY.output_tokens_per_call > POLICY.total_output_tokens
            or self.input_tokens >= POLICY.total_input_tokens
        ):
            self.fail()
        if specialist:
            if self.specialist_calls >= POLICY.max_specialist_calls:
                self.fail()
            authorize_tool(AgentRole.ORCHESTRATOR, AgentTool.RECIPE_SPECIALIST)
            if self.tool_calls >= POLICY.max_tool_calls:
                self.fail()
            self.tool_calls += 1
            self.specialist_calls += 1
        self.model_calls += 1

    def usage(self, usage: TokenUsage) -> None:
        self._check_deadline()
        if (
            usage.input_tokens > POLICY.max_input_tokens
            or usage.output_tokens > POLICY.output_tokens_per_call
            or self.input_tokens + usage.input_tokens > POLICY.total_input_tokens
            or self.output_tokens + usage.output_tokens > POLICY.total_output_tokens
        ):
            self.fail()
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens

    def tool(self, operation: str) -> None:
        self._check_deadline()
        tool = authorize_tool(AgentRole.RECIPE_NUTRITION, operation)
        if self.tool_calls >= POLICY.max_tool_calls:
            self.fail()
        if tool == AgentTool.WEB_IQ:
            if not self.web_allowed or self.web_calls >= POLICY.max_web_calls:
                self.fail()
            self.web_calls += 1
        self.tool_calls += 1

    def _check_deadline(self) -> None:
        if monotonic() >= self.deadline:
            raise OrchestratorError(OrchestratorFailure.TIMEOUT, self.operation_id)


def _candidate_status(candidate: RecipeCandidate, context: RequestContext, minutes: int) -> str:
    explicit = context.explicit_preferences
    safety = validate_ingredients(
        SafetyProfile(
            allergies=explicit.allergies,
            dietary_restrictions=explicit.dietary_restrictions,
            dislikes=explicit.dislikes,
        ),
        tuple(
            IngredientLine(line_id=line.line_id, name=line.name) for line in candidate.ingredients
        ),
    )
    if safety.verdict != Verdict.PASS:
        return "safety_rejected"
    if candidate.total_minutes > minutes:
        return "time_rejected"
    return "candidate"


class OrchestratorService:
    def __init__(self, coordinator: Coordinator, specialist: RecipeSpecialist) -> None:
        self._coordinator = coordinator
        self._specialist = specialist

    async def run(
        self,
        context: RequestContext,
        *,
        operation_id: UUID | None = None,
        deadline: float | None = None,
    ) -> OrchestrationResult:
        operation = operation_id or uuid4()
        if deadline is not None and not isfinite(deadline):
            raise ValueError("The operation deadline must be finite.")
        expires = min(
            deadline if deadline is not None else float("inf"),
            monotonic() + POLICY.deadline_seconds,
        )
        token = trace_id.set(operation.hex)
        started = monotonic()
        try:
            try:
                async with asyncio.timeout_at(expires):
                    if monotonic() >= expires:
                        raise TimeoutError
                    result = await self._run(context, ExecutionBudget(operation, expires), expires)
            except TimeoutError:
                raise OrchestratorError(OrchestratorFailure.TIMEOUT, operation) from None
            except (ValidationError, ToolDenied):
                raise OrchestratorError(OrchestratorFailure.INVALID_RESULT, operation) from None
        except OrchestratorError as exc:
            operational_event(
                Event.ORCHESTRATOR_FAILED,
                category=ErrorCategory(exc.code.value),
                duration_ms=(monotonic() - started) * 1000,
            )
            raise
        except asyncio.CancelledError:
            operational_event(Event.ORCHESTRATOR_CANCELLED)
            raise
        else:
            operational_event(
                Event.ORCHESTRATOR_COMPLETED, duration_ms=(monotonic() - started) * 1000
            )
            return result
        finally:
            trace_id.reset(token)

    async def _run(
        self, context: RequestContext, budget: ExecutionBudget, deadline: float
    ) -> OrchestrationResult:
        previous: SpecialistCall | None = None
        output: str | None = None
        candidate: RecipeCandidate | None = None
        status = "not_requested"
        minutes: int | None = None
        while True:
            request = CoordinationPrompt(
                role=AgentRole.ORCHESTRATOR,
                prompt=context.prompt,
                operation_id=budget.operation_id,
                max_output_tokens=POLICY.output_tokens_per_call,
            )
            budget.model()
            step = await self._coordinator.coordinate(
                CoordinationRequest(
                    request=request,
                    previous_call=previous,
                    tool_output=output,
                    max_input_tokens=min(
                        POLICY.max_input_tokens, POLICY.total_input_tokens - budget.input_tokens
                    ),
                )
            )
            budget.usage(step.usage)
            if step.answer is not None:
                answer = step.answer
                if answer.status == "accept":
                    if candidate is None or status != "candidate":
                        raise OrchestratorError(
                            OrchestratorFailure.INVALID_RESULT, budget.operation_id
                        )
                    return OrchestrationResult(
                        status="candidate", explanation=answer.explanation, candidate=candidate
                    )
                return OrchestrationResult(status=answer.status, explanation=answer.explanation)
            assert step.call is not None
            call = step.call
            if previous is not None and call.call_id == previous.call_id:
                raise OrchestratorError(OrchestratorFailure.INVALID_RESULT, budget.operation_id)
            if minutes is None:
                minutes = call.arguments.time_budget_minutes
            elif call.arguments.time_budget_minutes != minutes:
                raise OrchestratorError(OrchestratorFailure.INVALID_RESULT, budget.operation_id)
            budget.model(specialist=True)
            budget.web_allowed = call.arguments.use_web
            try:
                specialist = await self._specialist.generate(
                    context,
                    call.arguments,
                    operation_id=budget.operation_id,
                    budget=budget,
                    max_input_tokens=min(
                        POLICY.max_input_tokens, POLICY.total_input_tokens - budget.input_tokens
                    ),
                    max_output_tokens=POLICY.output_tokens_per_call,
                    deadline=deadline,
                )
            except FoundryGatewayError:
                raise OrchestratorError(
                    OrchestratorFailure.SPECIALIST_FAILED, budget.operation_id
                ) from None
            budget.usage(specialist.usage)
            candidate = specialist.candidate
            status = _candidate_status(candidate, context, minutes)
            if budget.specialist_calls == POLICY.max_specialist_calls:
                if status != "candidate":
                    raise OrchestratorError(
                        OrchestratorFailure.CANDIDATE_REJECTED, budget.operation_id
                    )
                return OrchestrationResult(
                    status="candidate",
                    explanation=" ".join(candidate.fit_reasons)[:1000],
                    candidate=candidate,
                )
            previous = call
            output = json.dumps(
                {
                    "status": status,
                    "revisions_remaining": POLICY.max_revisions,
                    "candidate": candidate.model_dump(mode="json")
                    if status == "candidate"
                    else None,
                },
                separators=(",", ":"),
            )
