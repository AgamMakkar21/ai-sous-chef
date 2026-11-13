"""Serving-scaled ingredient mass from cited comparable recipes, not model memory."""

import re
from decimal import Decimal
from statistics import median

from sous_chef.domain.nutrition_inputs import FOOD_FORMS, MASS, food_words, recipe_food_query
from sous_chef.domain.recipes import (
    DraftIngredient,
    QuantityEstimate,
    QuantitySource,
    RecipeDraft,
    RecipeGrounding,
    WebGrounding,
)

_SERVINGS = re.compile(
    r"\b(?:serves|servings?|yield)\s*[:=]?\s*(\d{1,3})\b|\b(\d{1,3})\s+servings?\b",
    re.IGNORECASE,
)
_SERVING_RANGE = re.compile(
    r"\b(?:serves|servings?|yield)\s*[:=]?\s*\d+\s*(?:-|to|\u2013)\s*\d+"
    r"|\b\d+\s*(?:-|to|\u2013)\s*\d+\s+servings?\b",
    re.IGNORECASE,
)
_AMOUNT = re.compile(
    r"(?<![\w.,/\-\u2013\u2014])"
    r"(?:(?P<range_start>\d+(?:\.\d+)?)\s*(?:to|[-\u2013\u2014])\s*)?"
    r"(?P<quantity>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>kilograms?|kg|grams?|g|ounces?|oz|pounds?|lbs?)\b\s+"
    r"(?P<food>[^;\n.!]+)",
    re.IGNORECASE,
)
_UNITS = {
    "gram": "g",
    "grams": "g",
    "kilogram": "kg",
    "kilograms": "kg",
    "ounce": "oz",
    "ounces": "oz",
    "pound": "lb",
    "pounds": "lb",
    "lbs": "lb",
}
_CUTS = {"breast", "thigh", "leg", "wing", "drumstick"}
_MAX_GRAMS_PER_SERVING = {"salt": 10, "oil": 60, "butter": 60, "sugar": 100, "honey": 100}


def needs_quantity_estimate(line: DraftIngredient) -> bool:
    return (
        line.quantity is None
        or line.unit is None
        or line.unit
        in {
            "piece",
            "item",
            "bunch",
            "can",
            "package",
        }
    )


def _compatible(name: str, source_name: str) -> bool:
    requested = food_words(recipe_food_query(name))
    candidate = food_words(recipe_food_query(source_name))
    if (requested & FOOD_FORMS or {"raw"}) != (candidate & FOOD_FORMS or {"raw"}):
        return False
    requested -= FOOD_FORMS | {"meat", "only"}
    candidate -= FOOD_FORMS | {"meat", "only"}
    return bool(requested) and requested <= candidate and candidate <= requested | _CUTS


def _source_amount(name: str, reference: str, text: str) -> QuantitySource | None:
    if _SERVING_RANGE.search(text):
        return None
    servings = {int(match.group(1) or match.group(2)) for match in _SERVINGS.finditer(text)}
    if len(servings) != 1 or not 1 <= next(iter(servings)) <= 100:
        return None
    portions = servings.pop()
    amounts: set[Decimal] = set()
    for match in _AMOUNT.finditer(text):
        source_name = re.split(r"\bfor\s+\d+\s+servings?\b", match.group("food"), flags=re.I)[0]
        if _compatible(name, source_name.strip(" ,:")):
            if match.group("range_start") is not None:
                return None
            unit = match.group("unit").casefold()
            amounts.add(Decimal(match.group("quantity")) * MASS[_UNITS.get(unit, unit)])
    if len(amounts) != 1:
        return None
    grams = amounts.pop()
    words = food_words(recipe_food_query(name))
    maximum = min(
        (limit for food, limit in _MAX_GRAMS_PER_SERVING.items() if food in words),
        default=1000,
    )
    if not Decimal("0.001") <= grams / portions <= maximum or grams > 100_000:
        return None
    return QuantitySource(
        reference=reference, quote=text, servings=portions, weight_grams=float(grams)
    )


def recipe_quantity_estimates(
    draft: RecipeDraft, grounding: RecipeGrounding, web: WebGrounding | None
) -> dict[str, QuantityEstimate]:
    evidence = [
        (recipe.source.reference, recipe.snippet)
        for recipe in grounding.recipes
        if recipe.source.source_id in draft.source_ids
    ]
    if web is not None:
        evidence.extend((claim.source_url, claim.text) for claim in web.claims)
    estimates: dict[str, QuantityEstimate] = {}
    for line in draft.ingredients:
        if not needs_quantity_estimate(line):
            continue
        sources: dict[str, QuantitySource] = {}
        conflicting = False
        for reference, text in evidence:
            source = _source_amount(line.name, reference, text)
            if source is not None:
                existing = sources.get(reference)
                if existing is not None and (
                    existing.weight_grams / existing.servings
                    != source.weight_grams / source.servings
                ):
                    conflicting = True
                sources[reference] = source
        if conflicting or not sources:
            continue
        all_amounts = [
            Decimal(str(source.weight_grams)) / source.servings for source in sources.values()
        ]
        if max(all_amounts) > min(all_amounts) * Decimal("1.5"):
            continue
        cited_sources = tuple(sources.values())[:3]
        per_serving = [
            Decimal(str(source.weight_grams)) / source.servings for source in cited_sources
        ]
        estimates[line.line_id] = QuantityEstimate(
            weight_grams=float(median(per_serving) * draft.servings),
            sources=cited_sources,
        )
    return estimates
