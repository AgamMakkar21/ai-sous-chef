"""Conservative food-name and household-measure normalization for estimates."""

import re
from decimal import Decimal

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
}
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
}
_SINGULARS = {
    "apples": "apple",
    "carrots": "carrot",
    "onions": "onion",
    "tomatoes": "tomato",
    "peppers": "pepper",
    "limes": "lime",
    "lemons": "lemon",
    "cloves": "clove",
    "wedges": "wedge",
    "eggs": "egg",
    "potatoes": "potato",
    "sugars": "sugar",
    "breasts": "breast",
    "thighs": "thigh",
    "wings": "wing",
    "drumsticks": "drumstick",
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
_FORMS = {
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
    return {_SINGULARS.get(word, word) for word in re.findall(r"[^\W_]+", value.casefold())}


def recipe_food_query(name: str) -> str:
    value = name.casefold()
    for original, equivalent in (
        ("neutral oil", "canola oil"),
        ("jasmine rice", "white long grain rice"),
        ("bell peppers?", "sweet pepper"),
        ("cilantro", "coriander leaves"),
        ("red chili flakes", "red cayenne pepper"),
        ("ground cumin", "cumin seed"),
        ("lime wedges?", "lime raw"),
        ("uncooked", "raw"),
        ("skinless", "meat only"),
    ):
        value = re.sub(r"\b" + original + r"\b", equivalent, value)
    words = [
        _SINGULARS.get(word, word)
        for word in re.findall(r"[^\W_]+", value)
        if word not in _PREPARATION
    ]
    if "rice" in words:
        words = ["raw" if word == "dry" else word for word in words]
    if "chicken" in words and set(words) <= {"chicken", "raw"}:
        words = ["chicken", "meat", "raw"]
    if words == ["water"]:
        words = ["water", "bottled", "generic"]
    elif words == ["tomato"]:
        words = ["tomato", "red", "ripe"]
    return " ".join(dict.fromkeys(words))


def recipe_food_match(food: UsdaFood, query: str) -> bool:
    requested = food_words(query)
    candidate = food_words(food.long_desc)
    if not requested or not requested <= candidate:
        return False
    allowed_forms = requested & _FORMS or {"raw"}
    if (candidate & _FORMS) - allowed_forms:
        return False
    if (candidate & {"babyfood", "restaurant", "soup", "sauce"}) - requested:
        return False
    if (
        candidate & {"liver", "heart", "kidney", "bran", "extract", "concentrate", "ground"}
    ) - requested:
        return False
    if candidate & {"chicken", "turkey", "duck"} and "skin" in candidate - requested:
        return False
    return not any(word in food.food_group.casefold() for word in ("baby", "restaurant", "meals"))


def ingredient_weight(
    name: str, quantity: Decimal, unit: str, profile: UsdaFoodProfile, *, estimates: bool
) -> tuple[Decimal, str] | None:
    if unit in MASS:
        return quantity * MASS[unit], "Measured weight"
    requested = food_words(name)
    source_words = food_words(profile.food.long_desc)
    portions: list[tuple[int, Decimal, str]] = []
    for portion in profile.portions:
        if portion.measure_unit == "undetermined":
            label = portion.modifier or portion.measure_unit
        else:
            label = f"{portion.measure_unit} {portion.modifier or ''}".strip()
        words = re.findall(r"[^\W_]+", label.casefold())
        units = {_UNIT_ALIASES.get(word, word) for word in words}
        portion_unit = next((value for value in VOLUME if value in units), None)
        grams = portion.gram_weight / portion.amount
        detail = f"USDA portion: {portion.amount} {label} = {portion.gram_weight} g"
        if unit in VOLUME and portion_unit is not None:
            score = 10 if unit == portion_unit else 0
            score += len(food_words(label) & requested)
            portions.append((score, quantity * VOLUME[unit] / VOLUME[portion_unit] * grams, detail))
        elif estimates and unit in {"item", "piece"}:
            count_words = food_words(label)
            explicit = requested & {"wedge", "clove", "slice"}
            expected = explicit
            if not expected:
                if "garlic" in source_words:
                    expected = {"clove"}
                elif source_words & {
                    "apple",
                    "lime",
                    "lemon",
                    "onion",
                    "carrot",
                    "pepper",
                    "egg",
                    "potato",
                }:
                    expected = {"fruit", "medium", "small", "large", "whole", "piece"}
            if count_words & expected and portion_unit is None:
                # Never turn a wedge, slice, package or serving into a whole item.
                if (count_words & {"wedge", "slice", "clove"}) - expected:
                    continue
                size = requested & {"small", "medium", "large"} or {"medium"}
                score = 10 if size & count_words else 0
                score += len(count_words & requested)
                portions.append((score, quantity * grams, detail))
            elif (
                "wedge" in requested
                and source_words & {"lime", "lemon"}
                and "fruit" in count_words
                and portion_unit is None
            ):
                portions.append(
                    (-1, quantity * grams / 8, f"Estimated 8 wedges per fruit; {detail}")
                )
    if portions:
        _, grams, basis = max(portions, key=lambda item: item[0])
        return (grams, basis) if grams <= 1_000_000_000 else None
    if estimates and unit in VOLUME:
        # Narrow, food-specific fallbacks, not a universal mL-to-gram assumption.
        density = None
        if "oil" in source_words and "sauce" not in source_words:
            density = Decimal("0.92")
        elif requested == {"water"}:
            density = Decimal(1)
        elif "juice" in source_words and source_words & {"lime", "lemon"}:
            density = Decimal("1.03")
        if density is not None:
            grams = quantity * VOLUME[unit] * density
            if grams <= 1_000_000_000:
                return grams, (
                    f"Food-specific density estimate: {density} g/mL; US household measures"
                )
    return None
