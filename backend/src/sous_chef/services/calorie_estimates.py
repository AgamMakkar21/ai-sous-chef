"""USDA-first nutrition with source-backed estimates for missing ingredient nutrients."""

import json
from uuid import UUID

from sous_chef.domain.agent_identity import AgentRole
from sous_chef.domain.recipes import (
    CalorieEstimate,
    CalorieGroundingRequest,
    IngredientCalories,
    IngredientNutrientEstimate,
    Nutrient,
    NutrientCoverage,
    NutrientGroundingRequest,
    NutritionEvidence,
    NutritionRequest,
)
from sous_chef.services.web_grounding import public_ingredient_name


def nutrient_request(
    request: NutritionRequest,
    evidence: NutritionEvidence,
    *,
    operation_id: UUID,
) -> NutrientGroundingRequest | None:
    ingredients = []
    gaps = {}
    for line in request.ingredients:
        missing = tuple(
            item.name
            for item in evidence.coverage
            if line.line_id in item.unknown_ingredient_line_ids
        )
        if not missing:
            continue
        name = public_ingredient_name(line.name)
        if name is None:
            continue
        gaps[line.line_id] = missing
        ingredients.append(
            {
                "ingredientLineId": line.line_id,
                "name": name,
                "quantity": float(line.quantity) if line.quantity is not None else None,
                "unit": line.unit,
                "missingNutrients": missing,
            }
        )
    if not ingredients:
        return None
    query = "energy protein carbohydrate fat fiber sodium nutrition per 100 g"
    for name in dict.fromkeys(str(item["name"]) for item in ingredients):
        if len(query) + len(name) + 2 <= 240:
            query += "; " + name
    return NutrientGroundingRequest(
        role=AgentRole.RECIPE_NUTRITION,
        operation_id=operation_id,
        prompt=json.dumps(
            {
                "purpose": "nutrition_fallback",
                "useWeb": True,
                "query": query,
                "ingredients": ingredients,
            },
            separators=(",", ":"),
        ),
        query=query,
        ingredient_line_ids=tuple(gaps),
        missing_nutrients=gaps,
    )


def with_nutrient_estimates(
    evidence: NutritionEvidence, estimates: tuple[IngredientNutrientEstimate, ...]
) -> NutritionEvidence:
    if not estimates:
        return evidence
    missing = {
        (line_id, item.name)
        for item in evidence.coverage
        for line_id in item.unknown_ingredient_line_ids
    }
    pairs = [
        (estimate.ingredient_line_id, item.name)
        for estimate in estimates
        for item in estimate.nutrients
    ]
    if len(pairs) != len(set(pairs)) or not set(pairs) <= missing:
        raise ValueError(
            "Web nutrients must only fill missing values, never replace USDA evidence."
        )
    calories = tuple(
        CalorieEstimate(
            ingredient_line_id=estimate.ingredient_line_id,
            amount=item.amount,
            source="web_iq",
            reference=estimate.reference,
            basis=estimate.basis,
        )
        for estimate in estimates
        for item in estimate.nutrients
        if item.name == "energy"
    )
    evidence = with_calorie_estimates(evidence, calories)
    non_energy = tuple(
        IngredientNutrientEstimate(
            **estimate.model_dump(exclude={"nutrients"}),
            nutrients=tuple(item for item in estimate.nutrients if item.name != "energy"),
        )
        for estimate in estimates
        if any(item.name != "energy" for item in estimate.nutrients)
    )
    coverage = []
    nutrients = {item.name: item for item in evidence.nutrients}
    for item in evidence.coverage:
        additions = [
            (estimate.ingredient_line_id, nutrient)
            for estimate in non_energy
            for nutrient in estimate.nutrients
            if nutrient.name == item.name
        ]
        if not additions:
            coverage.append(item)
            continue
        covered = (*item.covered_ingredient_line_ids, *(line_id for line_id, _ in additions))
        unknown = tuple(
            line_id for line_id in item.unknown_ingredient_line_ids if line_id not in covered
        )
        total = round((item.whole_recipe_amount or 0) + sum(row.amount for _, row in additions), 2)
        coverage.append(
            NutrientCoverage(
                name=item.name,
                status="partial" if unknown else "known",
                whole_recipe_amount=total,
                covered_ingredient_line_ids=covered,
                unknown_ingredient_line_ids=unknown,
            )
        )
        nutrients[item.name] = Nutrient(
            name=item.name,
            amount=round(
                total / evidence.servings if evidence.serving_basis == "per_serving" else total, 2
            ),
            unit=additions[0][1].unit,
        )
    components = tuple(
        item.model_copy(update={"unknown_nutrients": remaining})
        for item in evidence.unknown_components
        if (
            remaining := tuple(
                name
                for name in item.unknown_nutrients
                if (item.ingredient_line_id, name) not in pairs
            )
        )
    )
    return NutritionEvidence(
        **evidence.model_dump(
            exclude={"status", "nutrients", "coverage", "unknown_components", "nutrient_estimates"}
        ),
        status="known" if all(item.status == "known" for item in coverage) else "partial",
        nutrients=tuple(nutrients[item.name] for item in coverage if item.name in nutrients),
        coverage=tuple(coverage),
        unknown_components=components,
        nutrient_estimates=(*evidence.nutrient_estimates, *non_energy),
    )


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
