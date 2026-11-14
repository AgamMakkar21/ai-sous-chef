"""USDA-first calorie totals with separately attributed web/model estimates."""

import json
from uuid import UUID

from sous_chef.domain.agent_identity import AgentRole
from sous_chef.domain.recipes import (
    CalorieEstimate,
    CalorieGroundingRequest,
    IngredientCalories,
    Nutrient,
    NutrientCoverage,
    NutritionEvidence,
    NutritionRequest,
)
from sous_chef.services.web_grounding import public_ingredient_name


def calorie_request(
    request: NutritionRequest,
    evidence: NutritionEvidence,
    *,
    operation_id: UUID,
    max_input_tokens: int | None,
    max_output_tokens: int | None,
    use_web: bool = True,
) -> CalorieGroundingRequest | None:
    energy = next((item for item in evidence.coverage if item.name == "energy"), None)
    if energy is None:
        return None
    missing = set(energy.unknown_ingredient_line_ids)
    ingredients = []
    for line in request.ingredients:
        if line.line_id not in missing:
            continue
        name = public_ingredient_name(line.name)
        if name is None:
            continue
        ingredients.append(
            {
                "ingredientLineId": line.line_id,
                "name": name,
                "quantity": float(line.quantity) if line.quantity is not None else None,
                "unit": line.unit,
            }
        )
    if not ingredients:
        return None
    query = "calories kcal per 100 g"
    for name in dict.fromkeys(str(item["name"]) for item in ingredients):
        if len(query) + len(name) + 2 <= 240:
            query += "; " + name
    return CalorieGroundingRequest(
        role=AgentRole.RECIPE_NUTRITION,
        operation_id=operation_id,
        prompt=json.dumps(
            {
                "purpose": "calorie_fallback",
                "useWeb": use_web,
                "query": query,
                "ingredients": ingredients,
            },
            separators=(",", ":"),
        ),
        query=query,
        ingredient_line_ids=tuple(str(item["ingredientLineId"]) for item in ingredients),
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        use_web=use_web,
    )


def with_calorie_estimates(
    evidence: NutritionEvidence, estimates: tuple[CalorieEstimate, ...]
) -> NutritionEvidence:
    if not estimates:
        return evidence
    energy = next(item for item in evidence.coverage if item.name == "energy")
    unknown = set(energy.unknown_ingredient_line_ids)
    if any(item.ingredient_line_id not in unknown for item in estimates):
        raise ValueError("Fallback calories must not replace successful USDA evidence.")
    calories = (
        *evidence.ingredient_calories,
        *(IngredientCalories(**item.model_dump()) for item in estimates),
    )
    covered = tuple(item.ingredient_line_id for item in calories)
    if not set(energy.covered_ingredient_line_ids) <= set(covered):
        raise ValueError("Ingredient-level USDA calories are required before merging estimates.")
    remaining = tuple(
        line_id for line_id in energy.unknown_ingredient_line_ids if line_id not in covered
    )
    total = round(sum(item.amount for item in calories), 2)
    coverage = tuple(
        NutrientCoverage(
            name="energy",
            status="partial" if remaining else "known",
            whole_recipe_amount=total,
            covered_ingredient_line_ids=covered,
            unknown_ingredient_line_ids=remaining,
        )
        if item.name == "energy"
        else item
        for item in evidence.coverage
    )
    nutrients = (
        Nutrient(
            name="energy",
            amount=round(
                total / evidence.servings if evidence.serving_basis == "per_serving" else total,
                2,
            ),
            unit="kcal",
        ),
        *(item for item in evidence.nutrients if item.name != "energy"),
    )
    components = tuple(
        item.model_copy(
            update={
                "unknown_nutrients": tuple(
                    name for name in item.unknown_nutrients if name != "energy"
                )
            }
        )
        if item.ingredient_line_id in covered and "energy" in item.unknown_nutrients
        else item
        for item in evidence.unknown_components
        if item.ingredient_line_id not in covered or set(item.unknown_nutrients) != {"energy"}
    )
    return NutritionEvidence(
        **evidence.model_dump(
            exclude={"status", "nutrients", "coverage", "unknown_components", "ingredient_calories"}
        ),
        status="known" if all(item.status == "known" for item in coverage) else "partial",
        nutrients=nutrients,
        coverage=coverage,
        unknown_components=components,
        ingredient_calories=calories,
    )
