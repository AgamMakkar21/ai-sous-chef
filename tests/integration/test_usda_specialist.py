"""Real subprocess MCP round-trip through the specialist, using synthetic food data."""

import copy
import json
from pathlib import Path

import pytest
from sous_chef.adapters.foundry_gateway import FoundryGateway
from sous_chef.domain.foundry import FoundryFailure, FoundryGatewayError
from sous_chef.domain.recipes import RecipeGrounding, StructuredRecipe
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.services.recipe_specialist import RecipeSpecialistService
from sous_chef.telemetry import configure_safe_telemetry
from test_orchestrator_gateway import coordination_settings
from test_recipe_specialist import FIXTURE, cooking_context, run, setup
from test_usda_nutrition import config
from test_usda_startup import api_tool


@pytest.mark.parametrize("household_units", [False, True])
def test_apple_yogurt_recipe_uses_verified_mass_nutrition_not_guessed_portions(
    household_units: bool,
) -> None:
    draft = copy.deepcopy(FIXTURE["draft"])
    names = (
        "raw apple with skin",
        "plain nonfat Greek yogurt",
        "honey",
        "raw lemon juice",
        "ground cinnamon",
    )
    quantities = (2, 1, 1, 1, 0.5) if household_units else (300, 200, 20, 15, 2)
    draft["ingredients"] = [
        {
            "lineId": f"food{index}",
            "name": name,
            "quantity": quantity,
            "unit": unit if household_units else "g",
        }
        for index, (name, quantity, unit) in enumerate(
            zip(names, quantities, ("piece", "cup", "tbsp", "tbsp", "tsp"), strict=True)
        )
    ]
    draft["title"] = "Apple and yogurt bowl"
    draft["servings"] = 2
    draft["substitutions"] = []
    draft["instructions"] = [
        {
            "step": 1,
            "text": "Mix the declared ingredients in a bowl.",
            "ingredientLineIds": [line["lineId"] for line in draft["ingredients"]],
        }
    ]
    draft["fitReasons"] = ["A simple fruit and yogurt bowl."]
    draft["adaptations"] = []
    draft["sourceIds"] = ["fixture-apple-yogurt"]
    draft["sourceClaims"] = []
    _, grounding, model = setup(draft)
    grounding.grounding = RecipeGrounding.model_validate(
        {
            "status": "available",
            "recipes": (
                {
                    "source": {
                        "sourceId": "fixture-apple-yogurt",
                        "version": "fixture-v1",
                        "kind": "curated_recipe",
                        "reference": "https://recipes.example.invalid/synthetic-apple-yogurt",
                    },
                    "snippet": (
                        "Synthetic test recipe: mix raw apple with skin, plain nonfat Greek "
                        "yogurt, honey, raw lemon juice and ground cinnamon."
                    ),
                },
            ),
        }
    )
    gateway = FoundryGateway(coordination_settings(), client_factory=model.factory)
    service = RecipeSpecialistService(gateway, grounding, api_tool())
    context = cooking_context()
    context = context.model_copy(
        update={
            "explicit_preferences": context.explicit_preferences.model_copy(
                update={"allergies": ()}
            )
        }
    )
    recipe = run(service, context).recipe
    assert recipe is not None
    assert len(model.requests) == 1
    assert [line.unit for line in recipe.ingredients] == [
        line["unit"] for line in draft["ingredients"]
    ]
    assert [line.quantity for line in recipe.ingredients] == list(quantities)
    if household_units:
        assert recipe.nutrition.status == "unavailable"
        assert not recipe.nutrition.nutrients
        assert not recipe.nutrition.mappings
        assert not recipe.nutrition.sources
        assert [item.reason for item in recipe.nutrition.unknown_components] == [
            "unsupported_unit"
        ] * 5
    else:
        assert recipe.nutrition.status == "known"
        assert [item.amount for item in recipe.nutrition.nutrients] == [
            172,
            10.52,
            34.33,
            0.73,
            3.52,
            42.08,
        ]
        assert recipe.nutrition.coverage[0].whole_recipe_amount == 344
        assert all(
            item.status == "known" and not item.unknown_ingredient_line_ids
            for item in recipe.nutrition.coverage
        )
        assert not recipe.nutrition.unknown_components
        assert [item.usda_food_id for item in recipe.nutrition.mappings] == [
            str(5000001 + index) for index in range(5)
        ]
        assert [item.weight_grams for item in recipe.nutrition.mappings] == [300, 200, 20, 15, 2]
        assert all(source.kind == "usda" for source in recipe.nutrition.sources)


def test_specialist_accepts_verified_resolver_choices_without_an_extra_model_call() -> None:
    _, grounding, model = setup()
    gateway = FoundryGateway(coordination_settings(), client_factory=model.factory)
    service = RecipeSpecialistService(gateway, grounding, UsdaNutritionTool(config("ambiguous")))
    result = run(
        service,
        selected_food_ids={
            "rice": "99901",
            "carrot": "99902",
            "oil": "99903",
        },
    )
    assert result.recipe is not None
    assert result.recipe.nutrition.status == "known"
    assert [mapping.usda_food_id for mapping in result.recipe.nutrition.mappings] == [
        "99901",
        "99902",
        "99903",
    ]
    assert result.recipe.nutrition.nutrients[0].amount == 260
    assert result.recipe.nutrition.coverage[0].whole_recipe_amount == 520
    assert len(model.requests) == 1


@pytest.mark.parametrize(("servings", "energy"), [(1, 520), (2, 260), (4, 130)])
def test_recipe_uses_only_readonly_source_nutrition_with_exact_mappings(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    servings: int,
    energy: int,
) -> None:
    configure_safe_telemetry()
    log = tmp_path / "calls.jsonl"
    draft = copy.deepcopy(FIXTURE["draft"])
    draft["servings"] = servings
    _, grounding, model = setup(draft)
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
    assert recipe.nutrition.servings == servings
    assert recipe.nutrition.nutrients[0].amount == energy
    assert recipe.nutrition.coverage[0].whole_recipe_amount == 520
    assert recipe.nutrition.is_estimate
    assert recipe.nutrition.notice == "Informational estimate, not medical advice."
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
    assert {tuple(call["arguments"]) for call in calls} == {("keyword", "limit"), ("fdc_id",)}
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
