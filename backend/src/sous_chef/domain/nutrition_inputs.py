"""Conservative food-name and household-measure normalization for estimates."""

import re
from decimal import Decimal

from sous_chef.domain.ingredient_catalog import singularize
from sous_chef.domain.nutrition import UsdaFood, UsdaFoodProfile

MASS = {
    "g": Decimal(1),
    "kg": Decimal(1000),
    "oz": Decimal("28.349523125"),
    "lb": Decimal("453.59237"),
}
VOLUME = {
    "ml": Decimal(1),
    "l": Decimal(1000),
    "tsp": Decimal("4.92892159375"),
    "tbsp": Decimal("14.78676478125"),
    "cup": Decimal("236.5882365"),
    "fl oz": Decimal("29.5735295625"),
    "qt": Decimal("946.352946"),
    "gal": Decimal("3785.411784"),
}
COUNT = {"item": Decimal(1), "piece": Decimal(1), "dozen": Decimal(12)}
_UNIT_ALIASES = {
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
    "cups": "cup",
    "milliliter": "ml",
    "milliliters": "ml",
    "liter": "l",
    "liters": "l",
    "quart": "qt",
    "quarts": "qt",
    "gallon": "gal",
    "gallons": "gal",
    "doz": "dozen",
    "dozens": "dozen",
}
_PREPARATION = {
    "fresh",
    "freshly",
    "finely",
    "coarsely",
    "thinly",
    "roughly",
    "peeled",
    "deveined",
    "drained",
    "chopped",
    "diced",
    "sliced",
    "minced",
    "cubed",
    "grated",
    "crushed",
    "rinsed",
    "washed",
    "trimmed",
    "divided",
    "boneless",
    "piece",
    "pieces",
    "chunk",
    "chunks",
    "and",
    "small",
    "medium",
    "large",
}
FOOD_FORMS = {
    "raw",
    "cooked",
    "roasted",
    "fried",
    "boiled",
    "baked",
    "grilled",
    "steamed",
    "stewed",
    "broiled",
    "sauteed",
    "dried",
    "dehydrated",
}


def food_words(value: str) -> set[str]:
    return {_food_word(word) for word in re.findall(r"[^\W_]+", value.casefold())}


def _food_word(word: str) -> str:
    return word[:-2] if word.endswith("atoes") else singularize(word)


def recipe_food_query(name: str) -> str:
    value = name.casefold()
    for original, equivalent in (("uncooked", "raw"), ("skinless", "meat only")):
        value = re.sub(r"\b" + original + r"\b", equivalent, value)
    words = [_food_word(word) for word in re.findall(r"[^\W_]+", value) if word not in _PREPARATION]
    return " ".join(dict.fromkeys(words))


def recipe_food_match(food: UsdaFood, query: str) -> bool:
    requested = food_words(query)
    candidate = food_words(food.long_desc)
    prefix, separator, _ = food.long_desc.partition(",")
    category = food_words(prefix)
    if separator and category <= food_words(food.food_group) and not category & requested:
        candidate -= category
    if not requested or not requested <= candidate:
        return False
    allowed_forms = requested & FOOD_FORMS or {"raw"}
    if (candidate & FOOD_FORMS) - allowed_forms:
        return False
    if (candidate & {"babyfood", "restaurant", "soup", "sauce"}) - requested:
        return False
    if (
        candidate & {"liver", "heart", "kidney", "bran", "extract", "concentrate", "ground"}
    ) - requested:
        return False
    if "skin" in candidate - requested:
        return False
    descriptors = {
        "meat",
        "only",
        "breast",
        "thigh",
        "leg",
        "wing",
        "drumstick",
        "fillet",
        "bottled",
        "generic",
        "red",
        "ripe",
        "raw",
    }
    if candidate - requested - descriptors:
        identity_parts = [food_words(part) for part in food.long_desc.split(",")]
        if not any(part <= requested and part - FOOD_FORMS for part in identity_parts):
            return False
        if (candidate & {"with", "and", "added"}) - requested:
            return False
    return not any(word in food.food_group.casefold() for word in ("baby", "restaurant", "meals"))


def ingredient_weight(
    name: str, quantity: Decimal, unit: str, profile: UsdaFoodProfile, *, estimates: bool
) -> tuple[Decimal, str] | None:
    if unit in MASS:
        return quantity * MASS[unit], "Measured weight"
    requested = food_words(name)
    portions: list[tuple[int, Decimal, str]] = []
    for portion in profile.portions:
        if portion.measure_unit == "undetermined":
            label = portion.modifier or portion.measure_unit
        else:
            label = f"{portion.measure_unit} {portion.modifier or ''}".strip()
        words = re.findall(r"[^\W_]+", label.casefold())
        units = {_UNIT_ALIASES.get(word, word) for word in words}
        if re.search(r"\b(?:fl\.?\s*oz|fluid\s+ounces?)\b", label, flags=re.IGNORECASE):
            units.add("fl oz")
        portion_unit = next((value for value in VOLUME if value in units), None)
        grams = portion.gram_weight / portion.amount
        detail = f"USDA portion: {portion.amount} {label} = {portion.gram_weight} g"
        if unit in VOLUME and portion_unit is not None:
            score = 10 if unit == portion_unit else 0
            score += len(food_words(label) & requested)
            portions.append((score, quantity * VOLUME[unit] / VOLUME[portion_unit] * grams, detail))
        elif estimates and unit in COUNT:
            count_words = food_words(label)
            explicit = requested & {"wedge", "clove", "slice", "pod", "stalk", "sprig", "leaf"}
            expected = explicit
            if not expected:
                expected = {"fruit", "medium", "small", "large", "whole", "piece", "item", "dozen"}
            if count_words & expected and portion_unit is None:
                # Never turn a wedge, slice, package or serving into a whole item.
                if (
                    count_words & {"package", "serving", "portion", "bunch", "can"}
                    or (count_words & {"wedge", "slice", "clove", "pod", "stalk", "sprig", "leaf"})
                    - expected
                ):
                    continue
                size = requested & {"small", "medium", "large"} or {"medium"}
                score = 10 if size & count_words else 0
                score += len(count_words & requested)
                portion_count = COUNT["dozen"] if "dozen" in units else Decimal(1)
                portions.append((score, quantity * COUNT[unit] / portion_count * grams, detail))
    if portions:
        _, grams, basis = max(portions, key=lambda item: item[0])
        return (grams, basis) if grams <= 1_000_000_000 else None
    return None
