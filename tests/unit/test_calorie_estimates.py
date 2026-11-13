import asyncio
import json
from time import monotonic
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sous_chef.domain.recipe_safety import render_recipe
from sous_chef.domain.recipes import CalorieEstimate, NutritionEvidence, StructuredRecipe
from sous_chef.services.calorie_estimates import calorie_request, with_calorie_estimates
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
