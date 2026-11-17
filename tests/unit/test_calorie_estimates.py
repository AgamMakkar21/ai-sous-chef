import asyncio
import json
from decimal import Decimal
from time import monotonic
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sous_chef.domain.recipe_safety import render_recipe
from sous_chef.domain.recipes import (
    CalorieEstimate,
    IngredientNutrientEstimate,
    Nutrient,
    NutritionEvidence,
    StructuredRecipe,
)
from sous_chef.services.calorie_estimates import (
    calorie_request,
    nutrient_request,
    with_calorie_estimates,
    with_nutrient_estimates,
)
from sous_chef.services.nutrition import UsdaNutritionTool
from test_recipe_safety import full_recipe
from test_usda_nutrition import config, ingredient, request


def partial() -> NutritionEvidence:
    payload = request(ingredient(), ingredient("carrot", 150, line_id="carrot")).model_copy(
        update={"allow_estimates": True}
    )
    return asyncio.run(
        UsdaNutritionTool(config("fail_carrot")).lookup(payload, deadline=monotonic() + 10)
    )


def test_residual_recovery_sums_raw_values_instead_of_repeatedly_rounding_totals() -> None:
    payload = request(
        *(
            ingredient(name, line_id=line_id).model_copy(update={"quantity": Decimal("0.1")})
            for name, line_id in (("rice", "rice"), ("carrot", "carrot"), ("olive oil", "oil"))
        ),
        servings=1,
    )
    original = asyncio.run(
        UsdaNutritionTool(config("partial_protein")).lookup(payload, deadline=monotonic() + 10)
    )
    assert original.status == "partial"
    estimates = tuple(
        IngredientNutrientEstimate(
            ingredient_line_id=line_id,
            nutrients=(Nutrient(name="protein", amount=0.004, unit="g"),),
            source="ai_knowledge",
            reference=None,
            basis="Synthetic estimate for the specified whole ingredient portion.",
        )
        for line_id in ("carrot", "oil")
    )
    first = with_nutrient_estimates(original, estimates[:1])
    remaining = nutrient_request(payload, first, operation_id=uuid4(), use_web=False)
    assert remaining is not None and remaining.missing_nutrients == {"oil": ("protein",)}
    final = with_nutrient_estimates(first, estimates[1:])
    assert final.status == "known" and not final.unknown_components
    assert next(item.amount for item in final.nutrients if item.name == "protein") == 0.01
    assert final.mappings == original.mappings and final.sources == original.sources


def test_maximum_multilingual_ingredient_input_fits_the_recovery_prompt_without_escaping() -> None:
    lines = tuple(
        ingredient(
            "\u98df" * 100,
            line_id=f"ingredient-{index:02d}".ljust(64, "x"),
        )
        for index in range(30)
    )
    payload = request(*lines)
    evidence = asyncio.run(
        UsdaNutritionTool(config("no_food")).lookup(payload, deadline=monotonic() + 10)
    )
    recovery = nutrient_request(payload, evidence, operation_id=uuid4(), use_web=False)
    assert recovery is not None and len(recovery.prompt) <= 12000
    content = json.loads(recovery.prompt)
    assert len(content["ingredients"]) == 30
    assert all(item["name"] == "\u98df" * 100 for item in content["ingredients"])


@pytest.mark.parametrize("legacy_mapping", [False, True])
def test_web_fallback_fills_all_missing_nutrients_without_replacing_usda(
    legacy_mapping: bool,
) -> None:
    evidence = partial()
    if legacy_mapping:
        evidence = NutritionEvidence.model_validate_json(
            evidence.model_copy(
                update={
                    "mappings": tuple(
                        mapping.model_copy(update={"nutrients": ()})
                        for mapping in evidence.mappings
                    )
                }
            ).model_dump_json()
        )
    payload = request(ingredient(), ingredient("carrot", 150, line_id="carrot"))
    fallback = nutrient_request(payload, evidence, operation_id=uuid4())
    assert fallback is not None
    assert set(fallback.missing_nutrients) == {"carrot"}
    assert set(fallback.missing_nutrients["carrot"]) == {item.name for item in evidence.coverage}
    estimate = IngredientNutrientEstimate(
        ingredient_line_id="carrot",
        nutrients=tuple(
            Nutrient(
                name=item.name,
                amount=60 if item.name == "energy" else 3,
                unit=("kcal" if item.name == "energy" else "mg" if item.name == "sodium" else "g"),
            )
            for item in evidence.coverage
        ),
        reference="https://source.example.invalid/nutrition",
        basis="Synthetic cited nutrients for the full 150 g portion.",
    )
    merged = with_nutrient_estimates(evidence, (estimate,))
    assert merged.status == "known"
    assert not merged.unknown_components
    assert all(not item.unknown_ingredient_line_ids for item in merged.coverage)
    assert merged.mappings == evidence.mappings and merged.sources == evidence.sources
    assert merged.ingredient_calories[0] == evidence.ingredient_calories[0]
    assert merged.ingredient_calories[1].source == "web_iq"
    assert {item.name for item in merged.nutrient_estimates[0].nutrients} == {
        "protein",
        "carbohydrate",
        "fat",
        "fiber",
        "sodium",
    }
    assert next(item.amount for item in merged.nutrients if item.name == "protein") == (
        next(item.amount for item in evidence.nutrients if item.name == "protein") + 1.5
    )
    with pytest.raises(ValueError, match="never replace"):
        with_nutrient_estimates(
            evidence, (estimate.model_copy(update={"ingredient_line_id": "rice"}),)
        )
    corrupted = merged.model_dump()
    corrupted["nutrients"][1]["amount"] = 999
    with pytest.raises(ValidationError, match="totals must (include|match)"):
        NutritionEvidence.model_validate(corrupted)


def test_fallback_preserves_usda_values_and_only_fills_missing_calories() -> None:
    evidence = partial()
    estimate = CalorieEstimate(
        ingredient_line_id="carrot",
        amount=60,
        source="ai_knowledge",
        reference=None,
        basis="Synthetic estimate for the specified 150 g carrot portion.",
    )
    merged = with_calorie_estimates(evidence, (estimate,))
    assert merged.mappings == evidence.mappings
    assert merged.sources == evidence.sources
    assert merged.ingredient_calories[0] == evidence.ingredient_calories[0]
    assert merged.nutrients[0].amount == 130
    assert merged.coverage[0].whole_recipe_amount == 260
    assert merged.status == "partial"
    assert all(item.name != "energy" for item in merged.coverage if item.status == "unavailable")
    assert merged.unknown_components[0].unknown_nutrients == (
        "protein",
        "carbohydrate",
        "fat",
        "fiber",
        "sodium",
    )
    with pytest.raises(ValueError, match="must not replace"):
        with_calorie_estimates(
            evidence, (estimate.model_copy(update={"ingredient_line_id": "rice"}),)
        )
    corrupted = merged.model_dump()
    corrupted["nutrients"][0]["amount"] = 999
    with pytest.raises(ValidationError, match="totals must match"):
        NutritionEvidence.model_validate(corrupted)


def test_ai_fills_only_remaining_pairs_and_preserves_usda_and_web_values() -> None:
    evidence = partial()
    payload = request(ingredient(), ingredient("carrot", 150, line_id="carrot"))
    web = IngredientNutrientEstimate(
        ingredient_line_id="carrot",
        nutrients=(Nutrient(name="protein", amount=3, unit="g"),),
        reference="https://source.example.invalid/nutrition",
        basis="Synthetic cited protein for the whole portion.",
    )
    merged = with_nutrient_estimates(evidence, (web,))
    fallback = nutrient_request(payload, merged, operation_id=uuid4(), use_web=False)
    assert fallback is not None and not fallback.use_web
    assert "protein" not in fallback.missing_nutrients["carrot"]
    ai = IngredientNutrientEstimate(
        ingredient_line_id="carrot",
        nutrients=tuple(
            Nutrient(
                name=name,
                amount=60 if name == "energy" else 2,
                unit="kcal" if name == "energy" else "mg" if name == "sodium" else "g",
            )
            for name in fallback.missing_nutrients["carrot"]
        ),
        source="ai_knowledge",
        reference=None,
        basis="Approximate AI knowledge estimate for the specified 150 g portion.",
    )
    complete = with_nutrient_estimates(merged, (ai,))
    assert complete.status == "known" and not complete.unknown_components
    assert complete.mappings == evidence.mappings
    assert complete.nutrient_estimates[0] == web
    assert {item.source for item in complete.nutrient_estimates} == {"web_iq", "ai_knowledge"}
    assert complete.ingredient_calories[-1].source == "ai_knowledge"
    with pytest.raises(ValueError, match="never replace"):
        with_nutrient_estimates(complete, (web,))
    corrupted = complete.model_dump()
    corrupted["nutrient_estimates"][1]["reference"] = "https://invented.example.invalid"
    with pytest.raises(ValidationError):
        NutritionEvidence.model_validate(corrupted)


@pytest.mark.parametrize("serving_basis", ["per_serving", "whole_recipe"])
def test_web_only_nutrient_totals_use_the_whole_portion_and_cannot_be_inflated(
    serving_basis: str,
) -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config("error")).lookup(
            request().model_copy(update={"allow_estimates": True}), deadline=monotonic() + 10
        )
    ).model_copy(update={"serving_basis": serving_basis})
    estimate = IngredientNutrientEstimate(
        ingredient_line_id="rice",
        nutrients=tuple(
            Nutrient(
                name=item.name,
                amount=100 if item.name == "energy" else 4,
                unit="kcal" if item.name == "energy" else "mg" if item.name == "sodium" else "g",
            )
            for item in evidence.coverage
        ),
        reference="https://source.example.invalid/nutrition",
        basis="Synthetic cited nutrients for the whole 100 g portion.",
    )
    merged = with_nutrient_estimates(evidence, (estimate,))
    assert merged.status == "known" and not merged.mappings
    assert next(item.amount for item in merged.nutrients if item.name == "protein") == (
        2 if serving_basis == "per_serving" else 4
    )
    corrupted = merged.model_dump()
    next(item for item in corrupted["coverage"] if item["name"] == "protein")[
        "whole_recipe_amount"
    ] = 8
    next(item for item in corrupted["nutrients"] if item["name"] == "protein")["amount"] = (
        4 if serving_basis == "per_serving" else 8
    )
    with pytest.raises(ValidationError, match="totals must (include|match)"):
        NutritionEvidence.model_validate(corrupted)


def test_fallback_query_only_uses_public_food_names_and_preserves_explicit_forms() -> None:
    evidence = partial()
    payload = request(
        ingredient(),
        ingredient("cooked carrot", None, line_id="carrot"),
    )
    fallback = calorie_request(
        payload,
        evidence,
        operation_id=uuid4(),
        max_input_tokens=6000,
        max_output_tokens=1500,
    )
    assert fallback is not None
    assert fallback.ingredient_line_ids == ("carrot",)
    assert "cooked" in fallback.query and "carrot" in fallback.query
    assert "rice" not in fallback.prompt and '"quantity":null' in fallback.prompt
    private = payload.model_copy(
        update={
            "ingredients": (
                ingredient(),
                ingredient("carrot for person@example.invalid", line_id="carrot"),
            )
        }
    )
    assert (
        calorie_request(
            private, evidence, operation_id=uuid4(), max_input_tokens=6000, max_output_tokens=1500
        )
        is None
    )


def test_fallback_covers_rice_and_spice_forms_without_relabeling_missing_macros() -> None:
    names = (
        "basmati rice, dry",
        "cumin seeds",
        "ground coriander",
        "garam masala",
        "red chili powder",
    )
    payload = request(
        *(ingredient(name, 10, line_id=f"food{index}") for index, name in enumerate(names))
    ).model_copy(update={"allow_estimates": True})
    evidence = asyncio.run(UsdaNutritionTool(config()).lookup(payload, deadline=monotonic() + 10))
    fallback = calorie_request(
        payload,
        evidence,
        operation_id=uuid4(),
        max_input_tokens=16000,
        max_output_tokens=1500,
    )
    assert fallback is not None
    data = json.loads(fallback.prompt)
    assert [item["name"] for item in data["ingredients"]] == list(names)
    assert fallback.ingredient_line_ids == tuple(line.line_id for line in payload.ingredients)
    assert all(name in fallback.query for name in names)
    estimates = tuple(
        CalorieEstimate(
            ingredient_line_id=line.line_id,
            amount=30,
            source="ai_knowledge",
            reference=None,
            basis="Synthetic calorie estimate for this 10 g portion.",
        )
        for line in payload.ingredients
    )
    merged = with_calorie_estimates(evidence, estimates)
    energy = next(item for item in merged.coverage if item.name == "energy")
    assert energy.status == "known" and not energy.unknown_ingredient_line_ids
    assert energy.whole_recipe_amount == 150
    assert all("energy" not in item.unknown_nutrients for item in merged.unknown_components)
    assert all(item.status == "unavailable" for item in merged.coverage if item.name != "energy")
    assert not merged.mappings and not merged.sources


@pytest.mark.parametrize(
    "name",
    [
        "unlisted ingredient variety 742",
        "regional spice mixture, freshly toasted",
        "unfamiliar grain (uncooked)",
        "ingredient with a brand-new preparation",
        "\u672a\u767b\u9332\u306e\u98df\u6750",
    ],
)
def test_fallback_is_not_limited_to_known_foods_or_preparation_words(name: str) -> None:
    payload = request(ingredient(name, 37, unit="g", line_id="carrot"))
    fallback = calorie_request(
        payload,
        partial(),
        operation_id=uuid4(),
        max_input_tokens=6000,
        max_output_tokens=1500,
    )
    assert fallback is not None
    assert json.loads(fallback.prompt)["ingredients"] == [
        {"ingredientLineId": "carrot", "name": name, "quantity": 37, "unit": "g"}
    ]
    assert name in fallback.query


def test_query_length_limit_does_not_drop_ingredients_from_the_fallback_request() -> None:
    payload = request(
        *(
            ingredient(f"unlisted regional ingredient number {index}", 10, line_id=f"food{index}")
            for index in range(30)
        )
    ).model_copy(update={"allow_estimates": True})
    evidence = asyncio.run(
        UsdaNutritionTool(config("no_food")).lookup(payload, deadline=monotonic() + 10)
    )
    fallback = calorie_request(
        payload,
        evidence,
        operation_id=uuid4(),
        max_input_tokens=16000,
        max_output_tokens=1500,
    )
    assert fallback is not None and len(fallback.query) <= 240
    assert len(json.loads(fallback.prompt)["ingredients"]) == 30
    assert fallback.ingredient_line_ids == tuple(line.line_id for line in payload.ingredients)


def test_empty_fallback_keeps_unknown_calories_instead_of_inventing_zero() -> None:
    payload = request(ingredient("unlisted food"))
    evidence = asyncio.run(
        UsdaNutritionTool(config("no_food")).lookup(payload, deadline=monotonic() + 10)
    )
    assert with_calorie_estimates(evidence, ()) is evidence
    assert not evidence.nutrients and not evidence.ingredient_calories


@pytest.mark.parametrize(
    "name",
    [
        "garam masala for person@example.invalid",
        "ingredient for patient 123456789",
        "basmati rice dry https://private.example.invalid",
        "red chili powder for my diagnosis",
    ],
)
def test_fallback_does_not_send_obvious_private_name_suffixes(name: str) -> None:
    payload = request(ingredient(name, line_id="carrot"))
    assert (
        calorie_request(
            payload, partial(), operation_id=uuid4(), max_input_tokens=6000, max_output_tokens=1500
        )
        is None
    )


def test_optional_calorie_summary_does_not_break_a_near_limit_recipe() -> None:
    data = full_recipe(
        names=("rice", "carrot"), instructions=("Cook the rice and carrot.",) * 8
    ).model_dump(mode="json")
    for line, line_id in zip(data["ingredients"], ("rice", "carrot"), strict=True):
        line["line_id"] = line_id
    data["ingredients"][1]["quantity"] = 150
    for step in data["instructions"]:
        step["ingredient_line_ids"] = ["rice", "carrot"]
    remaining = 3990 - len(render_recipe(StructuredRecipe.model_validate_json(json.dumps(data))))
    for step in data["instructions"]:
        padding = min(remaining, 500 - len(step["text"]))
        step["text"] += " " * padding
        remaining -= padding
    assert remaining == 0
    evidence = with_calorie_estimates(
        partial(),
        (
            CalorieEstimate(
                ingredient_line_id="carrot",
                amount=60,
                source="ai_knowledge",
                reference=None,
                basis="Synthetic estimate for the specified 150 g carrot portion.",
            ),
        ),
    )
    data["nutrition"] = evidence.model_dump(mode="json")
    data["sources"].extend(source.model_dump(mode="json") for source in evidence.sources)
    recipe = StructuredRecipe.model_validate_json(json.dumps(data))
    assert len(render_recipe(recipe)) == 3990
    assert recipe.nutrition.ingredient_calories[-1].source == "ai_knowledge"
