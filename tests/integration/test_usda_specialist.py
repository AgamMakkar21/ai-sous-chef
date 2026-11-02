"""Real subprocess MCP round-trip through the specialist, using synthetic food data."""

import json
from pathlib import Path

import pytest
from sous_chef.adapters.foundry_gateway import FoundryGateway
from sous_chef.domain.foundry import FoundryFailure, FoundryGatewayError
from sous_chef.domain.recipes import StructuredRecipe
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.services.recipe_specialist import RecipeSpecialistService
from sous_chef.telemetry import configure_safe_telemetry
from test_orchestrator_gateway import coordination_settings
from test_recipe_specialist import run, setup
from test_usda_nutrition import config


def test_recipe_uses_only_readonly_source_nutrition_with_exact_mappings(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_safe_telemetry()
    log = tmp_path / "calls.jsonl"
    _, grounding, model = setup()
    gateway = FoundryGateway(coordination_settings(), client_factory=model.factory)
    service = RecipeSpecialistService(gateway, grounding, UsdaNutritionTool(config(log=log)))
    result = run(service)
    recipe = result.recipe
    assert recipe is not None
    assert StructuredRecipe.model_validate_json(recipe.model_dump_json(by_alias=True)) == recipe
    assert recipe.nutrition.status == "known"
    assert [mapping.usda_food_id for mapping in recipe.nutrition.mappings] == [
        "99901",
        "99902",
        "99903",
    ]
    assert recipe.nutrition.nutrients[0].amount == 260  # 100+150+10 g, two servings
    assert [mapping.quantity for mapping in recipe.nutrition.mappings] == [
        line.quantity for line in recipe.ingredients
    ]
    assert recipe.nutrition.sources == tuple(
        source for source in recipe.sources if source.kind == "usda"
    )
    messages = [json.loads(line) for line in log.read_text().splitlines()]
    calls = [message["params"] for message in messages if message["method"] == "tools/call"]
    assert len(calls) == 6 and len(model.requests) == 1
    assert {call["name"] for call in calls} == {"search_foods", "get_nutrition_profile"}
    assert {tuple(call["arguments"]) for call in calls} == {("keyword", "limit"), ("ndb_no",)}
    assert "peanut" not in log.read_text()  # No profile/history/identity enters USDA.
    assert "99901" not in caplog.text and "rice" not in caplog.text


@pytest.mark.parametrize("mode", ["malformed", "timeout"])
def test_source_failure_is_not_replaced_by_model_nutrition(mode: str) -> None:
    _, grounding, model = setup()
    gateway = FoundryGateway(coordination_settings(), client_factory=model.factory)
    service = RecipeSpecialistService(
        gateway,
        grounding,
        UsdaNutritionTool(config(mode, timeout=1)),
    )
    with pytest.raises(FoundryGatewayError) as caught:
        run(service)
    assert caught.value.code == (
        FoundryFailure.TIMEOUT if mode == "timeout" else FoundryFailure.INVALID_RESULT
    )
    assert len(model.requests) == 1
