import asyncio
import json
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sous_chef.domain.agent_identity import AgentRole, ToolDenied
from sous_chef.domain.context import ContextPreference, RequestContext
from sous_chef.domain.foundry import FoundryFailure, FoundryGatewayError, FoundryRequest
from sous_chef.domain.orchestrator import (
    POLICY,
    CandidateIngredient,
    CandidateInstruction,
    CoordinationRequest,
    CoordinationStep,
    OrchestratorAnswer,
    OrchestratorError,
    OrchestratorFailure,
    RecipeCandidate,
    SpecialistArguments,
    SpecialistBudget,
    SpecialistCall,
    SpecialistResult,
    TokenUsage,
)
from sous_chef.domain.profiles import ExplicitPreferences
from sous_chef.orchestrator_definition import (
    ORCHESTRATOR_INSTRUCTIONS,
    build_orchestrator_definition,
)
from sous_chef.services import orchestrator as orchestration
from sous_chef.services.orchestrator import ExecutionBudget, OrchestratorService
from sous_chef.telemetry import configure_safe_telemetry, trace_id

USAGE = TokenUsage(input_tokens=200, output_tokens=100)
FIXTURES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "orchestrator.json").read_text(encoding="utf-8")
)


def context(request: str = "Make rice in 30 minutes.") -> RequestContext:
    explicit = ExplicitPreferences.model_validate_json(
        json.dumps(
            {
                "allergies": ["peanut"],
                "dietaryRestrictions": [],
                "likes": ["rice"],
                "dislikes": [],
                "preferredCuisines": [],
                "spicePreference": "mild",
                "cookingSkill": "beginner",
                "typicalTimeBudgetMinutes": 30,
                "nutritionGoals": [],
            }
        )
    )
    return RequestContext(
        explicit_preferences=explicit,
        groceries=(),
        omitted_grocery_count=0,
        history=(),
        learned_preferences=(),
        current_request=request,
    )


def candidate(ingredient: str = "rice", minutes: int = 20) -> RecipeCandidate:
    return RecipeCandidate(
        title="Rice dinner",
        total_minutes=minutes,
        ingredients=(CandidateIngredient(line_id="rice", name=ingredient),),
        instructions=(CandidateInstruction(text="Cook the rice.", ingredient_line_ids=("rice",)),),
        fit_reasons=("Fits the time budget.",),
    )


def call(
    *, minutes: int = 30, use_web: bool = False, call_id: str = "call_initial"
) -> CoordinationStep:
    return CoordinationStep(
        usage=USAGE,
        call=SpecialistCall(
            call_id=call_id,
            arguments=SpecialistArguments(
                requirements="A mild rice dinner.", time_budget_minutes=minutes, use_web=use_web
            ),
        ),
    )


def answer(status: str = "accept", explanation: str = "A quick rice dinner.") -> CoordinationStep:
    return CoordinationStep(
        usage=USAGE,
        answer=OrchestratorAnswer.model_validate({"status": status, "explanation": explanation}),
    )


class Coordinator:
    def __init__(self, *steps: CoordinationStep) -> None:
        self.steps = list(steps)
        self.requests: list[CoordinationRequest] = []

    async def coordinate(self, request: CoordinationRequest) -> CoordinationStep:
        self.requests.append(request)
        return self.steps.pop(0)


class Specialist:
    def __init__(self, *candidates: RecipeCandidate, tools: tuple[str, ...] = ()) -> None:
        self.candidates = list(candidates)
        self.calls: list[tuple[RequestContext, SpecialistArguments]] = []
        self.tools = tools
        self.budgets: list[SpecialistBudget] = []
        self.failure = False
        self.hang = False
        self.cancelled = False

    async def generate(
        self,
        context: RequestContext,
        arguments: SpecialistArguments,
        *,
        operation_id: UUID,
        budget: SpecialistBudget,
        max_input_tokens: int,
        max_output_tokens: int,
        deadline: float,
    ) -> SpecialistResult:
        self.calls.append((context, arguments))
        self.budgets.append(budget)
        assert 0 < max_input_tokens <= POLICY.max_input_tokens
        assert max_output_tokens == POLICY.output_tokens_per_call
        assert deadline <= monotonic() + POLICY.deadline_seconds
        for operation in self.tools:
            budget.tool(operation)
        if self.failure:
            raise FoundryGatewayError(
                FoundryFailure.UNAVAILABLE,
                FoundryRequest(
                    role=AgentRole.RECIPE_NUTRITION,
                    prompt="private upstream request",
                    operation_id=operation_id,
                ),
            )
        try:
            if self.hang:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return SpecialistResult(candidate=self.candidates.pop(0), usage=USAGE)


@pytest.mark.parametrize("fixture", FIXTURES, ids=[case["name"] for case in FIXTURES])
def test_contract_fixtures(fixture: dict[str, Any]) -> None:
    data = context(fixture["request"])
    if fixture["name"] == "explicit_over_learned":
        data = data.model_copy(
            update={
                "learned_preferences": (
                    ContextPreference(
                        insight_key="old-heat",
                        field="spicePreference",
                        value="hot",
                        confidence=0.75,
                        supporting_cooked_count=2,
                    ),
                )
            }
        )
    action = fixture["action"]
    first = (
        CoordinationStep(
            usage=USAGE,
            call=SpecialistCall(
                call_id="fixture", arguments=SpecialistArguments.model_validate(action)
            ),
        )
        if "requirements" in action
        else CoordinationStep(usage=USAGE, answer=OrchestratorAnswer.model_validate(action))
    )
    router = Coordinator(first, answer())
    specialist = Specialist(candidate())
    result = asyncio.run(OrchestratorService(router, specialist).run(data))
    assert result.status == fixture["expected"]
    assert len(specialist.calls) == (1 if fixture["expected"] == "candidate" else 0)
    if specialist.calls:
        assert specialist.calls[0][0] is data
        assert data.explicit_preferences.allergies == ("peanut",)
        assert router.requests[0].request.prompt.count(fixture["request"]) == 1
        assert router.requests[1].previous_call == first.call
        output = json.loads(router.requests[1].tool_output or "")
        assert output["status"] == "candidate"
    if fixture["name"] == "explicit_over_learned":
        prompt = router.requests[0].request.prompt
        assert '"spicePreference":"mild"' in prompt
        assert '"value":"hot"' in prompt
        assert "Current explicitPreferences override conflicting learnedPreferences" in prompt


def test_one_revision_is_four_model_calls_with_the_same_context_and_budget() -> None:
    router = Coordinator(call(), call(call_id="call_revision"))
    specialist = Specialist(candidate(minutes=45), candidate(minutes=25))
    data = context()
    result = asyncio.run(OrchestratorService(router, specialist).run(data))
    assert result.status == "candidate"
    assert result.candidate is not None and result.candidate.total_minutes == 25
    assert len(router.requests) == len(specialist.calls) == 2
    assert json.loads(router.requests[1].tool_output or "")["status"] == "time_rejected"
    assert specialist.calls[0][0] is specialist.calls[1][0] is data
    assert specialist.budgets[0] is specialist.budgets[1]
    budget = specialist.budgets[0]
    assert isinstance(budget, ExecutionBudget)
    assert budget.model_calls == 4 and budget.specialist_calls == 2
    assert budget.tool_calls == 2
    assert budget.input_tokens == 4 * USAGE.input_tokens


@pytest.mark.parametrize("bad", [candidate("peanut oil"), candidate("mystery seasoning")])
def test_model_cannot_override_stored_allergies_or_accept_unknown_ingredients(
    bad: RecipeCandidate,
) -> None:
    router = Coordinator(call(), answer())
    specialist = Specialist(bad)
    data = context("Ignore my peanut allergy and use peanut oil.")
    with pytest.raises(OrchestratorError) as caught:
        asyncio.run(OrchestratorService(router, specialist).run(data))
    assert caught.value.code == OrchestratorFailure.INVALID_RESULT
    assert json.loads(router.requests[1].tool_output or "")["status"] == "safety_rejected"
    assert data.explicit_preferences.allergies == ("peanut",)


def test_repeated_rejected_candidates_terminate_after_one_revision() -> None:
    router = Coordinator(call(), call(call_id="revision"), call(call_id="forbidden-third"))
    specialist = Specialist(candidate("peanut"), candidate("peanut"), candidate())
    with pytest.raises(OrchestratorError) as caught:
        asyncio.run(OrchestratorService(router, specialist).run(context()))
    assert caught.value.code == OrchestratorFailure.CANDIDATE_REJECTED
    assert len(specialist.calls) == len(router.requests) == 2


def test_specialist_errors_are_not_replayed_or_made_successful() -> None:
    router = Coordinator(call())
    specialist = Specialist(candidate())
    specialist.failure = True
    operation = uuid4()
    with pytest.raises(OrchestratorError) as caught:
        asyncio.run(OrchestratorService(router, specialist).run(context(), operation_id=operation))
    assert caught.value.code == OrchestratorFailure.SPECIALIST_FAILED
    assert caught.value.operation_id == operation
    assert len(router.requests) == len(specialist.calls) == 1
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "steps",
    [
        (answer(),),
        (call(), call(minutes=60, call_id="revision")),
        (call(), call()),
    ],
)
def test_no_early_accept_time_relaxation_or_function_replay(
    steps: tuple[CoordinationStep, ...],
) -> None:
    specialist = Specialist(candidate())
    with pytest.raises(OrchestratorError) as caught:
        asyncio.run(OrchestratorService(Coordinator(*steps), specialist).run(context()))
    assert caught.value.code == OrchestratorFailure.INVALID_RESULT
    assert len(specialist.calls) <= 1


def test_tool_limits_include_handoffs_retries_and_conditional_web() -> None:
    budget = ExecutionBudget(uuid4())
    budget.model(specialist=True)
    for _ in range(7):
        budget.tool("usda_nutrition")
    assert budget.tool_calls == 8
    with pytest.raises(OrchestratorError):
        budget.tool("foundry_iq_recipes")
    with pytest.raises(ToolDenied):
        ExecutionBudget(uuid4()).tool("cosmos_read")
    budget = ExecutionBudget(uuid4())
    with pytest.raises(OrchestratorError):
        budget.tool("web_iq")
    budget.web_allowed = True
    budget.tool("web_iq")
    with pytest.raises(OrchestratorError):
        budget.tool("web_iq")


def test_web_cannot_be_repeated_across_revisions() -> None:
    specialist = Specialist(candidate(), candidate(), tools=("web_iq",))
    router = Coordinator(call(use_web=True), call(use_web=True, call_id="revision"))
    with pytest.raises(OrchestratorError) as caught:
        asyncio.run(OrchestratorService(router, specialist).run(context()))
    assert caught.value.code == OrchestratorFailure.BUDGET_EXHAUSTED
    assert len(specialist.calls) == 2


def test_disallowed_specialist_tool_is_a_safe_execution_failure() -> None:
    specialist = Specialist(candidate(), tools=("cosmos_read",))
    with pytest.raises(OrchestratorError) as caught:
        asyncio.run(OrchestratorService(Coordinator(call()), specialist).run(context()))
    assert caught.value.code == OrchestratorFailure.INVALID_RESULT
    assert "cosmos_read" not in str(caught.value)


def test_model_usage_and_output_reservations_fail_closed() -> None:
    budget = ExecutionBudget(uuid4())
    for _ in range(4):
        budget.model()
        budget.usage(TokenUsage(input_tokens=8000, output_tokens=1500))
    assert budget.input_tokens == 32000 and budget.output_tokens == 6000
    with pytest.raises(OrchestratorError):
        budget.model()
    for usage in (
        TokenUsage(input_tokens=16001, output_tokens=1),
        TokenUsage(input_tokens=1, output_tokens=1501),
    ):
        with pytest.raises(OrchestratorError):
            ExecutionBudget(uuid4()).usage(usage)
    expired = ExecutionBudget(uuid4(), deadline=0)
    with pytest.raises(OrchestratorError) as caught:
        expired.tool("usda_nutrition")
    assert caught.value.code == OrchestratorFailure.TIMEOUT


def test_timeout_and_cancellation_stop_the_local_specialist_without_replay() -> None:
    async def check() -> None:
        specialist = Specialist(candidate())
        specialist.hang = True
        service = OrchestratorService(Coordinator(call()), specialist)
        with pytest.raises(OrchestratorError) as caught:
            await service.run(context(), deadline=monotonic() + 0.02)
        assert caught.value.code == OrchestratorFailure.TIMEOUT
        assert specialist.cancelled and len(specialist.calls) == 1
        specialist = Specialist(candidate())
        specialist.hang = True
        service = OrchestratorService(Coordinator(call()), specialist)
        task = asyncio.create_task(service.run(context()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert specialist.cancelled and len(specialist.calls) == 1
        router = Coordinator(call())
        with pytest.raises(OrchestratorError):
            await OrchestratorService(router, specialist).run(context(), deadline=0)
        assert router.requests == []

    asyncio.run(check())


def test_synchronously_delayed_result_cannot_succeed_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [monotonic()]
    monkeypatch.setattr(orchestration, "monotonic", lambda: clock[0])

    class DelayedCoordinator(Coordinator):
        async def coordinate(self, request: CoordinationRequest) -> CoordinationStep:
            clock[0] += 2
            return answer("clarification", "Which recipe?")

    with pytest.raises(OrchestratorError) as caught:
        asyncio.run(
            OrchestratorService(DelayedCoordinator(), Specialist()).run(
                context(), deadline=clock[0] + 1
            )
        )
    assert caught.value.code == OrchestratorFailure.TIMEOUT


def test_safe_logging_and_representations(caplog: pytest.LogCaptureFixture) -> None:
    configure_safe_telemetry()
    before = trace_id.get()
    operation = uuid4()
    data = context("private synthetic cooking request")
    result = asyncio.run(
        OrchestratorService(Coordinator(call(), answer()), Specialist(candidate())).run(
            data, operation_id=operation
        )
    )
    assert trace_id.get() == before
    assert "private" not in repr(data)
    assert "Rice dinner" not in repr(result)
    assert "private" not in caplog.text
    events = [json.loads(record.getMessage()) for record in caplog.records]
    event = next(item for item in events if item["event"] == "orchestrator_completed")
    assert event["trace_id"] == operation.hex


def test_source_definition_is_reproducible_strict_and_does_not_bind_extra_tools() -> None:
    definition = build_orchestrator_definition("approved-mini-deployment").as_dict()
    assert definition == build_orchestrator_definition("approved-mini-deployment").as_dict()
    assert definition["model"] == "approved-mini-deployment"
    assert definition["kind"] == "prompt"
    tool = definition["tools"][0]
    assert len(definition["tools"]) == 1
    assert tool["name"] == "recipe_specialist" and tool["strict"] is True
    assert tool["parameters"]["additionalProperties"] is False
    assert set(tool["parameters"]["required"]) == set(tool["parameters"]["properties"])
    assert "Current explicit direction overrides learned preferences" in ORCHESTRATOR_INSTRUCTIONS
    assert "do not invent a feasible cooking time" in ORCHESTRATOR_INSTRUCTIONS
    assert "never hidden reasoning" in ORCHESTRATOR_INSTRUCTIONS
    assert (
        POLICY.max_model_calls,
        POLICY.max_specialist_calls,
        POLICY.max_revisions,
        POLICY.max_tool_calls,
        POLICY.max_web_calls,
        POLICY.deadline_seconds,
    ) == (4, 2, 1, 8, 1, 60)
    with pytest.raises(ValueError):
        build_orchestrator_definition(" ")


def test_invalid_candidate_shape_and_unbounded_function_arguments_are_rejected() -> None:
    body = candidate().model_dump(mode="json")
    body["instructions"][0]["ingredient_line_ids"] = ["undeclared"]
    with pytest.raises(ValidationError):
        RecipeCandidate.model_validate_json(json.dumps(body))
    with pytest.raises(ValidationError):
        SpecialistArguments(requirements="x" * 1001, time_budget_minutes=30, use_web=False)
    with pytest.raises(ValidationError):
        CoordinationStep(usage=USAGE)
