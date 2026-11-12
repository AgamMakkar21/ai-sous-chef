"""Real subprocess MCP round-trip through the specialist, using synthetic food data."""

import asyncio
import copy
import json
from pathlib import Path
from time import monotonic

import pytest
from sous_chef.adapters.foundry_gateway import FoundryGateway
from sous_chef.domain.foundry import FoundryFailure, FoundryGatewayError
from sous_chef.domain.recipes import NutritionRequest, RecipeGrounding, StructuredRecipe
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.services.recipe_specialist import RecipeSpecialistService
from sous_chef.telemetry import configure_safe_telemetry
from test_orchestrator_gateway import coordination_settings
from test_recipe_specialist import FIXTURE, cooking_context, run, setup
from test_usda_nutrition import config
from test_usda_startup import api_tool


@pytest.mark.parametrize(("quantity", "unit"), [(None, None), (2, "piece")])
def test_cited_recipe_quantity_fallback_preserves_original_amount_and_uses_usda(
    quantity: int | None,
    unit: str | None,
) -> None:
    draft = copy.deepcopy(FIXTURE["draft"])
    draft.update(
        title="Chicken dinner",
        ingredients=[{"lineId": "chicken", "name": "chicken", "quantity": quantity, "unit": unit}],
        instructions=[{"step": 1, "text": "Cook chicken.", "ingredientLineIds": ["chicken"]}],
        substitutions=[],
        sourceClaims=[],
    )
    _, grounding, model = setup(draft)
    source = grounding.grounding.recipes[0].source
    grounding.grounding = RecipeGrounding.model_validate(
        {
            "status": "available",
            "recipes": (
                {
                    "source": source,
                    "snippet": "Chicken dinner\nServes 4\nIngredient: 500 g chicken",
                },
            ),
        }
    )
    service = RecipeSpecialistService(
        FoundryGateway(coordination_settings(), client_factory=model.factory), grounding, api_tool()
    )
    recipe = run(service).recipe
    assert recipe is not None and recipe.nutrition.status == "known"
    assert recipe.ingredients[0].quantity == quantity and recipe.ingredients[0].unit == unit
    mapping = recipe.nutrition.mappings[0]
    assert mapping.weight_grams == 250 and mapping.quantity_estimate is not None
    assert mapping.usda_food_id == "2345678"
    assert mapping.quantity_estimate.sources[0].reference == source.reference
    assert mapping.quantity_estimate.sources[0].servings == 4
    assert "Assumed 250 g" in (mapping.conversion_basis or "")
    assert next(n.amount for n in recipe.nutrition.nutrients if n.name == "energy") == 150
    assert StructuredRecipe.model_validate_json(recipe.model_dump_json(by_alias=True)) == recipe


@pytest.mark.parametrize(
    ("name", "unit", "reason", "food_id"),
    [
        ("chicken, boneless pieces", "g", None, "2345678"),
        ("chicken, boneless chunks", "g", None, "2345678"),
        ("raw chicken, boneless piece", "g", None, "2345678"),
        ("chicken breasts, boneless chunks", "g", None, "2345678"),
        ("ground chicken", "g", None, "2345681"),
        ("chicken, boneless pieces", "piece", "unsupported_unit", None),
        ("cooked chicken, boneless pieces", "g", "food_unavailable", None),
    ],
)
def test_generic_cut_words_do_not_hide_chicken_nutrition_or_invent_piece_weights(
    name: str, unit: str, reason: str | None, food_id: str | None
) -> None:
    request = NutritionRequest.model_validate(
        {
            "servings": 2,
            "allow_estimates": True,
            "ingredients": ({"line_id": "chicken", "name": name, "quantity": 200, "unit": unit},),
        }
    )
    evidence = asyncio.run(api_tool().lookup(request, deadline=monotonic() + 10))
    if reason is not None:
        assert evidence.status == "unavailable"
        assert evidence.unknown_components[0].reason == reason
        assert not evidence.nutrients
    else:
        assert evidence.status == "known"
        assert len(evidence.nutrients) == 6
        assert not evidence.unknown_components
        mapping = evidence.mappings[0]
        assert mapping.usda_food_id == food_id
        assert mapping.food_description == (
            "Chicken, ground, raw" if food_id == "2345681" else "Chicken, breast, meat only, raw"
        )
        assert next(n.amount for n in evidence.nutrients if n.name == "energy") == (
            143 if food_id == "2345681" else 120
        )
        assert mapping.weight_grams == 200 and mapping.quantity == 200
        assert mapping.unit == "g"
    assert request.ingredients[0].name == name


def test_preparation_names_and_household_units_cover_a_nineteen_ingredient_recipe() -> None:
    foods = (
        ("shrimp, raw, peeled and deveined", 200, "g"),
        ("tofu, firm, drained and cubed", 150, "g"),
        ("jasmine rice dry", 1, "cup"),
        ("neutral oil", 2, "tbsp"),
        ("onion sliced", 100, "g"),
        ("garlic minced", 2, "piece"),
        ("fresh ginger minced", 10, "g"),
        ("red bell pepper sliced", 100, "g"),
        ("tomatoes chopped", 150, "g"),
        ("water", 250, "ml"),
        ("lime juice", 1, "tbsp"),
        ("fish sauce", 10, "g"),
        ("soy sauce", 15, "g"),
        ("brown sugar", 10, "g"),
        ("red chili flakes", 1, "g"),
        ("ground cumin", 1, "g"),
        ("cornstarch", 5, "g"),
        ("cilantro chopped", 5, "g"),
        ("lime wedges", 2, "piece"),
    )
    request = NutritionRequest.model_validate(
        {
            "servings": 2,
            "allow_estimates": True,
            "ingredients": tuple(
                {"line_id": f"food{i}", "name": name, "quantity": amount, "unit": unit}
                for i, (name, amount, unit) in enumerate(foods)
            ),
        }
    )
    evidence = asyncio.run(api_tool().lookup(request, deadline=monotonic() + 10))
    assert evidence.status == "known"
    assert len(evidence.sources) == len(evidence.mappings) == 19
    assert not evidence.unknown_components
    assert all(item.status == "known" for item in evidence.coverage)
    assert [item.weight_grams for item in evidence.mappings][2:6] == [185, 28, 100, 6]
    assert evidence.mappings[-1].weight_grams == 16  # Wedges, not two whole limes.
    assert evidence.mappings[-1].conversion_basis is not None
    assert evidence.mappings[-1].conversion_basis.startswith("Estimated 8 wedges per fruit")
    energy = next(item.amount for item in evidence.nutrients if item.name == "energy")
    assert 650 < energy < 900
    assert all(item.approximate_match and item.food_description for item in evidence.mappings)
    assert evidence.mappings[9].conversion_basis is not None
    assert "density estimate" in evidence.mappings[9].conversion_basis


@pytest.mark.parametrize(
    ("name", "grams", "energy"),
    [("jasmine rice dry", 185, 675.25), ("white long grain rice cooked", 158, 205.4)],
)
def test_dry_and_cooked_rice_keep_different_source_forms_and_portion_weights(
    name: str, grams: int, energy: float
) -> None:
    request = NutritionRequest.model_validate(
        {
            "servings": 1,
            "allow_estimates": True,
            "ingredients": ({"line_id": "rice", "name": name, "quantity": 1, "unit": "cup"},),
        }
    )
    evidence = asyncio.run(api_tool().lookup(request, deadline=monotonic() + 10))
    assert evidence.status == "known"
    assert evidence.mappings[0].weight_grams == grams
    assert evidence.nutrients[0].amount == energy


@pytest.mark.parametrize("household_units", [False, True])
def test_household_and_mass_recipes_produce_equivalent_source_portion_estimates(
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
    if household_units:
        assert all(
            item.conversion_basis and item.conversion_basis.startswith("USDA portion:")
            for item in recipe.nutrition.mappings
        )


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
