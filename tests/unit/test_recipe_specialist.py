import asyncio
import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sous_chef.adapters.foundry_gateway import FoundryGateway
from sous_chef.domain.agent_identity import AgentRole, IdentityError
from sous_chef.domain.context import RequestContext
from sous_chef.domain.foundry import FoundryFailure, FoundryGatewayError
from sous_chef.domain.groceries import GroceryRow
from sous_chef.domain.orchestrator import (
    POLICY,
    OrchestratorError,
    OrchestratorFailure,
    SpecialistArguments,
    SpecialistResult,
)
from sous_chef.domain.recipes import (
    NutritionEvidence,
    NutritionRequest,
    RecipeDraft,
    RecipeGenerationRequest,
    RecipeGrounding,
    RecipeToolError,
    StructuredRecipe,
)
from sous_chef.publish_orchestrator import publish_agent
from sous_chef.recipe_definition import RECIPE_INSTRUCTIONS, build_recipe_definition
from sous_chef.services.context import _encoding
from sous_chef.services.orchestrator import ExecutionBudget, OrchestratorService
from sous_chef.services.recipe_grounding import retrieval_query
from sous_chef.services.recipe_specialist import RecipeSpecialistService
from sous_chef.settings import ConfigurationError
from sous_chef.telemetry import configure_safe_telemetry, trace_id
from test_foundry_gateway import Harness, response_body
from test_orchestrator import Coordinator, answer, call, context
from test_orchestrator_gateway import body, coordination_settings, function_output, project

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "recipe-specialist.json").read_text(encoding="utf-8")
)


class FixtureTools:
    """Explicit offline tool doubles, never selected by application/runtime configuration."""

    def __init__(self) -> None:
        self.grounding = RecipeGrounding.model_validate_json(json.dumps(FIXTURE["grounding"]))
        self.evidence = NutritionEvidence.model_validate_json(json.dumps(FIXTURE["nutrition"]))
        self.queries: list[str] = []
        self.nutrition_requests: list[NutritionRequest] = []
        self.failure: FoundryFailure | None = None
        self.hang = False
        self.cancelled = False

    async def retrieve(self, requirements: str, *, deadline: float) -> RecipeGrounding:
        assert monotonic() < deadline
        self.queries.append(requirements)
        if self.failure is not None:
            raise RecipeToolError(self.failure)
        try:
            if self.hang:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self.grounding

    async def lookup(self, request: NutritionRequest, *, deadline: float) -> NutritionEvidence:
        assert monotonic() < deadline
        self.nutrition_requests.append(request)
        return self.evidence


def grocery(name: str, quantity: int | None, unit: str | None = "g") -> GroceryRow:
    return GroceryRow.model_validate_json(
        json.dumps(
            {
                "groceryId": str(uuid4()),
                "name": name,
                "quantity": quantity,
                "unit": unit,
                "updatedAt": datetime.now(UTC).isoformat(),
            }
        )
    )


def cooking_context() -> RequestContext:
    data = context("Use my rice and carrots for a beginner dinner in 30 minutes.")
    return data.model_copy(
        update={
            "explicit_preferences": data.explicit_preferences.model_copy(
                update={
                    "allergies": ("peanut", "milk"),
                    "nutrition_goals": ("high protein",),
                }
            ),
            "groceries": (grocery("rice", 200), grocery("carrot", 100)),
        }
    )


def setup(
    draft: dict[str, Any] | None = None, *, response: dict[str, Any] | None = None
) -> tuple[RecipeSpecialistService, FixtureTools, Harness]:
    message = response_body()["output"][0]
    message["content"][0]["text"] = json.dumps(FIXTURE["draft"] if draft is None else draft)
    transport = Harness(body=body(message) if response is None else response)
    tools = FixtureTools()
    gateway = FoundryGateway(coordination_settings(), client_factory=transport.factory)
    return RecipeSpecialistService(gateway, tools, tools), tools, transport


def run(
    service: RecipeSpecialistService,
    data: RequestContext | None = None,
    *,
    budget: ExecutionBudget | None = None,
    deadline: float | None = None,
    max_input_tokens: int = POLICY.max_input_tokens,
) -> SpecialistResult:
    operation = uuid4() if budget is None else budget.operation_id
    return asyncio.run(
        service.generate(
            cooking_context() if data is None else data,
            SpecialistArguments(
                requirements="A mild rice dinner.", time_budget_minutes=30, use_web=False
            ),
            operation_id=operation,
            budget=ExecutionBudget(operation) if budget is None else budget,
            max_input_tokens=max_input_tokens,
            max_output_tokens=POLICY.output_tokens_per_call,
            deadline=monotonic() + 60 if deadline is None else deadline,
        )
    )


def test_fixture_covers_inventory_substitution_time_skill_and_attributed_nutrition() -> None:
    service, tools, transport = setup()
    data = cooking_context()
    result = run(service, data)
    recipe = result.recipe
    assert recipe is not None
    assert StructuredRecipe.model_validate_json(recipe.model_dump_json(by_alias=True)) == recipe
    assert recipe.total_minutes == 25 and recipe.skill == "beginner" and recipe.servings == 2
    assert [line.inventory_status for line in recipe.ingredients] == [
        "available",
        "missing",
        "missing",
    ]
    assert recipe.ingredients[0].ingredient_id == "ingredient:rice"
    assert recipe.ingredients[0].grocery_ids == (data.groceries[0].grocery_id,)
    assert [(item.ingredient_line_id, item.quantity) for item in recipe.shopping_items] == [
        ("carrot", Decimal(50)),
        ("oil", Decimal(10)),
    ]
    assert recipe.substitutions[0].replaces == "butter"
    assert recipe.substitutions[0].ingredient_line_id == "oil"
    assert recipe.nutrition.status == "known"
    assert recipe.nutrition.nutrients == tools.evidence.nutrients
    assert recipe.nutrition.mappings == tools.evidence.mappings
    assert recipe.nutrition.notice == "Informational estimate, not medical advice."
    assert recipe.nutrition.serving_basis == "per_serving"
    assert [source.source_id for source in recipe.sources] == ["fixture-rice", "fixture-usda"]
    assert (
        tools.nutrition_requests[0].ingredients
        == RecipeDraft.model_validate_json(json.dumps(FIXTURE["draft"])).ingredients
    )
    assert len(transport.requests) == 1
    assert result.usage.input_tokens == 400 and result.usage.output_tokens == 200
    assert result.candidate.title == recipe.title


def test_single_pinned_stateless_invocation_is_private_and_has_no_callable_tools(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_safe_telemetry()
    service, tools, transport = setup()
    data = cooking_context()
    original_trace = trace_id.get()
    run(service, data)
    assert trace_id.get() == original_trace
    request = json.loads(transport.requests[0].content)
    assert request["agent_reference"] == {
        "type": "agent_reference",
        "name": "recipe-agent",
        "version": "3",
    }
    assert request["tools"] == [] and request["tool_choice"] == "none"
    assert request["store"] is False and request["parallel_tool_calls"] is False
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"] == RecipeDraft.model_json_schema()
    assert "model" not in request and "previous_response_id" not in request
    assert "conversation" not in request
    assert request["input"].count(data.current_request) == 1
    assert '"learnedPreferences":[]' in request["input"]
    assert '"allergies":["peanut","milk"]' in request["input"]
    assert tools.queries == [
        retrieval_query(
            data,
            SpecialistArguments(
                requirements="A mild rice dinner.", time_budget_minutes=30, use_web=False
            ),
        )
    ]
    assert data.current_request not in caplog.text
    assert "Simple carrot rice" not in caplog.text
    assert "fixture-usda" not in caplog.text
    events = [json.loads(record.getMessage()) for record in caplog.records]
    invoked = next(event for event in events if event["event"] == "recipe_invoked")
    assert invoked["agent_role"] == "recipe_nutrition"
    assert (
        invoked["agent_principal_id"]
        == coordination_settings().foundry_recipe_nutrition_agent_principal_id
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("totalMinutes", 31, FoundryFailure.REJECTED),
        ("skill", "advanced", FoundryFailure.REJECTED),
        ("sourceIds", ["invented-citation"], FoundryFailure.INVALID_RESULT),
        ("servings", 0, FoundryFailure.INVALID_RESULT),
        ("schemaVersion", 3, FoundryFailure.INVALID_RESULT),
        ("nutrition", {"calories": 100}, FoundryFailure.INVALID_RESULT),
    ],
)
def test_invalid_or_unfit_drafts_fail_explicitly_without_repair(
    field: str, value: Any, code: FoundryFailure
) -> None:
    draft = copy.deepcopy(FIXTURE["draft"])
    draft[field] = value
    service, tools, transport = setup(draft)
    with pytest.raises(FoundryGatewayError) as caught:
        run(service)
    assert caught.value.code == code
    assert len(transport.requests) == 1
    assert tools.nutrition_requests == []
    assert "Simple carrot rice" not in str(caught.value)


@pytest.mark.parametrize("ingredient", ["butter", "peanut butter"])
def test_taste_or_nutrition_goals_cannot_override_hard_restrictions(ingredient: str) -> None:
    draft = copy.deepcopy(FIXTURE["draft"])
    draft["ingredients"][2]["name"] = ingredient
    service, tools, _ = setup(draft)
    data = cooking_context()
    data = data.model_copy(
        update={"current_request": f"Use {ingredient} anyway; I want more protein and flavor."}
    )
    with pytest.raises(FoundryGatewayError) as caught:
        run(service, data)
    assert caught.value.code == FoundryFailure.REJECTED
    assert not tools.nutrition_requests


@pytest.mark.parametrize("status", ["partial", "unavailable"])
def test_missing_nutrition_is_not_fabricated_or_reported_as_a_complete_total(status: str) -> None:
    service, tools, _ = setup()
    evidence = copy.deepcopy(FIXTURE["nutrition"])
    evidence["status"] = status
    if status == "partial":
        evidence["mappings"] = evidence["mappings"][:1]
    else:
        evidence.update(nutrients=[], mappings=[], sources=[])
    tools.evidence = NutritionEvidence.model_validate_json(json.dumps(evidence))
    result = run(service)
    assert result.recipe is not None
    assert result.recipe.nutrition.status == status
    assert result.recipe.nutrition.nutrients == tools.evidence.nutrients


def test_missing_curated_grounding_stops_before_generation() -> None:
    service, tools, transport = setup()
    tools.grounding = RecipeGrounding(status="unavailable", recipes=())
    with pytest.raises(FoundryGatewayError) as caught:
        run(service)
    assert caught.value.code == FoundryFailure.UNAVAILABLE
    assert not transport.requests and not tools.nutrition_requests


def test_empty_index_is_distinct_from_unavailable_grounding() -> None:
    service, tools, transport = setup()
    tools.grounding = RecipeGrounding(status="empty", recipes=())
    with pytest.raises(FoundryGatewayError) as caught:
        run(service)
    assert caught.value.code == FoundryFailure.NO_RESULTS
    assert not transport.requests and not tools.nutrition_requests


@pytest.mark.parametrize(
    "claim",
    [
        {"sourceId": "fixture-rice", "quote": "This adapted recipe is allergen-free."},
        {"sourceId": "foreign", "quote": "Beginner preparation, 25 minutes."},
        {
            "sourceId": "fixture-rice",
            "quote": "Beginner preparation, 25 minutes.",
            "claim": "This adapted recipe is proven safe.",
        },
    ],
)
def test_citations_cannot_support_invented_quotes_or_adaptation_claims(
    claim: dict[str, str],
) -> None:
    draft = copy.deepcopy(FIXTURE["draft"])
    draft["sourceClaims"] = [claim]
    service, tools, _ = setup(draft)
    with pytest.raises(FoundryGatewayError) as caught:
        run(service)
    assert caught.value.code == FoundryFailure.INVALID_RESULT
    assert not tools.nutrition_requests


def test_web_grounding_is_not_silently_ignored_before_asc34() -> None:
    service, tools, transport = setup()
    with pytest.raises(FoundryGatewayError) as caught:
        asyncio.run(
            service.generate(
                cooking_context(),
                SpecialistArguments(
                    requirements="Current food news", time_budget_minutes=30, use_web=True
                ),
                operation_id=uuid4(),
                budget=ExecutionBudget(uuid4()),
                max_input_tokens=16000,
                max_output_tokens=1500,
                deadline=monotonic() + 60,
            )
        )
    assert caught.value.code == FoundryFailure.UNAVAILABLE
    assert not tools.queries and not transport.requests


@pytest.mark.parametrize(
    "change", ["amount", "unit", "servings", "missing_mapping", "extra_mapping"]
)
def test_mismatched_nutrition_evidence_is_never_attached(change: str) -> None:
    service, tools, _ = setup()
    evidence = copy.deepcopy(FIXTURE["nutrition"])
    if change == "amount":
        evidence["mappings"][0]["quantity"] = 200
    elif change == "unit":
        evidence["mappings"][0]["unit"] = "cup"
    elif change == "servings":
        evidence["servings"] = 1
    elif change == "missing_mapping":
        evidence["mappings"].pop()
    else:
        evidence["mappings"][0]["ingredientLineId"] = "invented"
    tools.evidence = NutritionEvidence.model_validate_json(json.dumps(evidence))
    with pytest.raises(FoundryGatewayError) as caught:
        run(service)
    assert caught.value.code == FoundryFailure.INVALID_RESULT


@pytest.mark.parametrize(
    "groceries",
    [
        (grocery("rice", None),),
        (grocery("rice", 200, "cup"),),
        (grocery("rice", 200, None),),
    ],
)
def test_unknown_inventory_or_incompatible_units_never_prove_ownership(
    groceries: tuple[GroceryRow, ...],
) -> None:
    service, _, _ = setup()
    data = cooking_context().model_copy(update={"groceries": groceries})
    recipe = run(service, data).recipe
    assert recipe is not None
    assert recipe.ingredients[0].inventory_status == "unknown"
    assert "rice" not in {item.ingredient_line_id for item in recipe.shopping_items}


def test_omitted_inventory_and_raw_cooked_aliases_do_not_become_missing_or_available() -> None:
    service, _, _ = setup()
    data = cooking_context().model_copy(
        update={"groceries": (grocery("cooked rice", 500),), "omitted_grocery_count": 1}
    )
    recipe = run(service, data).recipe
    assert recipe is not None
    assert recipe.ingredients[0].inventory_status == "unknown"
    assert recipe.shopping_items == ()


def test_repeated_ingredient_lines_do_not_reuse_the_same_groceries() -> None:
    draft = copy.deepcopy(FIXTURE["draft"])
    draft["ingredients"][1]["name"] = "rice"
    service, tools, _ = setup(draft)
    tools.evidence = NutritionEvidence(
        status="unavailable",
        serving_basis="per_serving",
        servings=2,
        nutrients=(),
        mappings=(),
        sources=(),
    )
    recipe = run(service).recipe
    assert recipe is not None
    assert recipe.ingredients[0].inventory_status == "available"
    assert recipe.ingredients[1].inventory_status == "missing"
    assert recipe.shopping_items[0].quantity == Decimal(50)


@pytest.mark.parametrize("quantity", [None, 0.001, 1_000_000])
def test_supported_quantity_boundaries_remain_numeric_or_explicitly_unknown(
    quantity: float | None,
) -> None:
    draft = copy.deepcopy(FIXTURE["draft"])
    draft["ingredients"][0]["quantity"] = quantity
    parsed = RecipeDraft.model_validate_json(json.dumps(draft))
    encoded = json.loads(parsed.model_dump_json(by_alias=True))
    assert encoded["ingredients"][0]["quantity"] == quantity


@pytest.mark.parametrize("quantity", [0, -1, 0.0001, 1_000_001, "100", True])
def test_invalid_quantities_cannot_be_used_for_inventory_or_nutrition(quantity: Any) -> None:
    draft = copy.deepcopy(FIXTURE["draft"])
    draft["ingredients"][0]["quantity"] = quantity
    with pytest.raises(ValidationError):
        RecipeDraft.model_validate_json(json.dumps(draft))


def test_unavailable_nutrition_cannot_contain_a_zero_or_fake_citation() -> None:
    evidence = copy.deepcopy(FIXTURE["nutrition"])
    evidence["status"] = "unavailable"
    evidence["nutrients"][0]["amount"] = 0
    with pytest.raises(ValidationError):
        NutritionEvidence.model_validate_json(json.dumps(evidence))


def test_tool_calls_share_budget_and_fail_before_an_unreserved_call() -> None:
    service, tools, transport = setup()
    budget = ExecutionBudget(uuid4())
    budget.model(specialist=True)
    result = run(service, budget=budget)
    assert result.recipe is not None
    assert budget.tool_calls == 3 and budget.model_calls == 1
    assert budget.web_calls == 0
    budget.tool_calls = POLICY.max_tool_calls
    with pytest.raises(OrchestratorError) as caught:
        run(service, budget=budget)
    assert caught.value.code == OrchestratorFailure.BUDGET_EXHAUSTED
    assert len(tools.queries) == 1 and len(transport.requests) == 1


def test_token_preflight_stops_before_the_managed_invocation() -> None:
    service, tools, transport = setup()
    with pytest.raises(FoundryGatewayError) as caught:
        run(service, max_input_tokens=1)
    assert caught.value.code == FoundryFailure.REJECTED
    assert len(tools.queries) == 1 and transport.requests == []


def test_aggregate_tool_token_ceiling_accepts_4000_but_rejects_4001() -> None:
    encoding = _encoding()
    text = json.dumps("\u4e00" * (POLICY.max_tool_result_tokens - 2), ensure_ascii=False)
    assert len(encoding.encode(text, disallowed_special=())) == 4000
    request = RecipeGenerationRequest(
        role=AgentRole.RECIPE_NUTRITION,
        prompt=text,
        grounding_json=text,
        max_input_tokens=16000,
        max_output_tokens=1500,
    )
    FoundryGateway._check_recipe_input(request)
    larger = json.dumps("\u4e00" * (POLICY.max_tool_result_tokens - 1), ensure_ascii=False)
    assert len(encoding.encode(larger, disallowed_special=())) == 4001
    with pytest.raises(FoundryGatewayError) as caught:
        FoundryGateway._check_recipe_input(
            request.model_copy(update={"prompt": larger, "grounding_json": larger})
        )
    assert caught.value.code == FoundryFailure.REJECTED


@pytest.mark.parametrize(
    "response",
    [
        body(function_output()),
        response_body(),  # Missing provider usage.
        body(),
        body(response_body()["output"][0]),  # Non-JSON text.
        body(
            {
                "type": "message",
                "id": "m",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "refusal", "refusal": "private-refusal"}],
            }
        ),
        {**body(response_body()["output"][0]), "status": "incomplete"},
    ],
)
def test_invalid_responses_and_unaccounted_tool_calls_never_become_success(
    response: dict[str, Any],
) -> None:
    service, tools, transport = setup(response=response)
    with pytest.raises(FoundryGatewayError) as caught:
        run(service)
    assert caught.value.code == FoundryFailure.INVALID_RESULT
    assert len(transport.requests) == 1 and not tools.nutrition_requests


def test_timeout_and_cancellation_stop_without_replacement_calls() -> None:
    service, tools, transport = setup()
    tools.hang = True
    with pytest.raises(FoundryGatewayError) as caught:
        run(service, deadline=monotonic() + 0.02)
    assert caught.value.code == FoundryFailure.TIMEOUT
    assert tools.cancelled and not transport.requests

    async def cancel() -> None:
        tools.cancelled = False
        operation = asyncio.create_task(
            service.generate(
                cooking_context(),
                SpecialistArguments(requirements="rice", time_budget_minutes=30, use_web=False),
                operation_id=uuid4(),
                budget=ExecutionBudget(uuid4()),
                max_input_tokens=16000,
                max_output_tokens=1500,
                deadline=monotonic() + 60,
            )
        )
        await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation

    asyncio.run(cancel())
    assert tools.cancelled and not transport.requests


@pytest.mark.parametrize("failure", [FoundryFailure.THROTTLED, FoundryFailure.AUTHENTICATION])
def test_tool_failures_are_explicit_and_never_retried(failure: FoundryFailure) -> None:
    service, tools, transport = setup()
    tools.failure = failure
    with pytest.raises(FoundryGatewayError) as caught:
        run(service)
    assert caught.value.code == failure
    assert len(tools.queries) == 1 and not transport.requests


def test_orchestrator_preserves_full_recipe_but_reviews_only_bounded_projection() -> None:
    service, _, _ = setup()
    coordinator = Coordinator(call(), answer())
    result = asyncio.run(OrchestratorService(coordinator, service).run(cooking_context()))
    assert result.recipe is not None and result.candidate is not None
    assert result.recipe.title == result.candidate.title
    assert result.status == "candidate"
    assert "nutrition" not in json.loads(coordinator.requests[1].tool_output or "")["candidate"]


def test_last_allowed_revision_preserves_the_full_recipe() -> None:
    service, _, _ = setup()
    coordinator = Coordinator(call(), call(call_id="revision"))
    result = asyncio.run(OrchestratorService(coordinator, service).run(cooking_context()))
    assert result.status == "candidate" and result.recipe is not None


def test_source_definition_uses_reviewed_safety_policy_and_strict_schema() -> None:
    definition = build_recipe_definition("recipe-gpt54", "approved-mvp-baseline").as_dict()
    assert definition["model"] == "recipe-gpt54"
    assert definition["rai_config"] == {"rai_policy_name": "approved-mvp-baseline"}
    assert definition["tools"] == [] and definition["tool_choice"] == "none"
    assert definition["text"]["format"]["schema"] == RecipeDraft.model_json_schema()
    assert "allergies" in RECIPE_INSTRUCTIONS and "nutrition" in RECIPE_INSTRUCTIONS
    models: tuple[type[RecipeDraft], ...] = (RecipeDraft, StructuredRecipe)
    for model in models:
        schema = model.model_json_schema()
        for item in [schema, *schema.get("$defs", {}).values()]:
            if item.get("type") == "object":
                assert item["additionalProperties"] is False
                assert set(item["required"]) == set(item["properties"])
    with pytest.raises(ValueError):
        build_recipe_definition("", "approved-mvp-baseline")


def test_recipe_publication_versions_the_existing_distinct_identity_without_activation() -> None:
    config = type(coordination_settings()).model_validate(
        coordination_settings().model_dump()
        | {
            "foundry_recipe_nutrition_rai_policy_name": "approved-mvp-baseline",
            "foundry_project_endpoint": "https://synthetic.services.ai.azure.com/api/projects/recipes",
            "foundry_project_resource_id": (
                "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/synthetic"
                "/providers/Microsoft.CognitiveServices/accounts/synthetic/projects/recipes"
            ),
            "foundry_iq_search_endpoint": "https://synthetic.search.windows.net",
            "foundry_iq_index_name": "fixture-recipes",
            "foundry_iq_knowledge_source_name": "fixture-source",
            "foundry_iq_knowledge_base_name": "fixture-knowledge",
            "foundry_iq_connection_name": "fixture-mcp",
        }
    )
    client = project()
    assert publish_agent(config, client, AgentRole.RECIPE_NUTRITION) == "7"
    invocation = client.agents.create_version.call_args.kwargs
    assert invocation["agent_name"] == "recipe-agent"
    assert invocation["metadata"]["asc_model"] == "gpt-5.4"
    assert invocation["metadata"]["asc_policy_version"] == "3"
    assert invocation["definition"].as_dict()["tools"][0]["project_connection_id"] == "fixture-mcp"
    assert len(invocation["metadata"]["asc_definition_sha256"]) == 64
    client.agents.create.assert_not_called()
    client.agents.create_version.reset_mock()
    with pytest.raises(ConfigurationError):
        publish_agent(coordination_settings(), client, AgentRole.RECIPE_NUTRITION)
    client.agents.create_version.assert_not_called()
    identity = client.agents.get("recipe-agent")
    client.agents.get.side_effect = lambda name: identity
    with pytest.raises(IdentityError):
        publish_agent(config, client, AgentRole.RECIPE_NUTRITION)


@pytest.mark.parametrize("mutation", ["duplicate", "instruction", "substitution", "ordering"])
def test_draft_references_are_validated(mutation: str) -> None:
    draft = copy.deepcopy(FIXTURE["draft"])
    if mutation == "duplicate":
        draft["ingredients"][1]["lineId"] = "rice"
    elif mutation == "instruction":
        draft["instructions"][0]["ingredientLineIds"] = ["undeclared"]
    elif mutation == "substitution":
        draft["substitutions"][0]["ingredientLineId"] = "undeclared"
    else:
        draft["instructions"][0]["step"] = 2
    with pytest.raises(ValidationError):
        RecipeDraft.model_validate_json(json.dumps(draft))
