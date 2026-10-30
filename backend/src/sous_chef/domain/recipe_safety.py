"""Fail-closed final recipe policy, independent of models and grounding adapters."""

import re
from enum import StrEnum

from pydantic import ValidationError

from sous_chef.domain.food_safety import (
    IngredientLine,
    SafetyProfile,
    Verdict,
    validate_ingredients,
)
from sous_chef.domain.ingredient_catalog import (
    DESCRIPTORS,
    Trait,
    catalog_entries,
    resolve,
    tokenize,
)
from sous_chef.domain.recipes import StructuredRecipe

POLICY_VERSION = "1"
GUIDANCE = "https://www.cdc.gov/food-safety/prevention/index.html"
UNABLE = (
    "I couldn't complete a recipe that passes the current ingredient and cooking-safety "
    "checks within the allowed attempts. No recipe was approved or saved."
)
HANDLING_NOTICE = (
    "Wash hands and utensils; rinse produce under running water. Keep raw animal foods "
    "separate from ready-to-eat foods. Refrigerate perishables in shallow containers within "
    "2 hours (1 hour above 90 F); keep the refrigerator at 40 F or below."
)


class RecipeSafetyFailure(StrEnum):
    STRUCTURE = "invalid_recipe_structure"
    INGREDIENT = "recipe_ingredient_rejected"
    PROSE = "recipe_prose_unsupported"
    COOKING = "recipe_cooking_rejected"
    PROFILE = "recipe_profile_unavailable"


class RecipeSafetyError(ValueError):
    """Only a bounded category; never include ingredients, profile data or rejected text."""

    def __init__(self, code: RecipeSafetyFailure) -> None:
        self.code = code
        super().__init__(code.value)


_ALIASES = {tokenize(alias) for entry in catalog_entries() for alias in entry.aliases}
_HEAT = frozenset({"cook", "boil", "simmer", "steam", "saute", "bake", "roast", "soften", "heat"})
_ACTIONS = _HEAT | {
    "chop",
    "slice",
    "dice",
    "peel",
    "mix",
    "combine",
    "stir",
    "add",
    "garnish",
    "top",
    "serve",
}
_COMBINE = frozenset({"mix", "combine", "stir", "add", "garnish", "top", "serve"})
_NEUTRAL = frozenset(
    """a an the and with for in to of simple quick easy mild spicy dinner lunch breakfast
    dish bowl salad soup beginner intermediate advanced technique cooking plan minute
    fit fits time budget use instead respect dairy restriction replace replaces
    seasoning can be adapted adaptation preference available grocery ingredient
    substitution nutritious meal preparation""".split()
)
_POULTRY = {"chicken", "turkey", "duck"}
_CUTS = {"beef", "pork", "lamb", "veal", "steak"}
_FINFISH = {"fish", "salmon", "tuna", "cod", "tilapia", "trout", "halibut", "mackerel", "sardine"}
_ANIMAL_DESCRIPTORS = frozenset(
    {"fresh", "raw", "whole", "boneless", "skinless", "lean", "ground", "minced"}
)
_STARCH = {"rice", "quinoa", "polenta", "pasta", "spaghetti", "penne", "macaroni", "potato"}
_PREPARATION = DESCRIPTORS - {"and", "or", "with"}
_TEMPERATURE = re.compile(
    r"cook (?:the )?(.+?) to an internal temperature of (\d{3})\s*(?:\u00b0\s*)?f, "
    r"measured with a food thermometer(?:, then rest for ([3-9]|[1-9]\d) minutes)?",
    re.IGNORECASE,
)


def _reject(code: RecipeSafetyFailure) -> None:
    raise RecipeSafetyError(code)


def _base(name: str) -> tuple[str, ...]:
    return tuple(word for word in tokenize(name) if word not in _PREPARATION)


def _heat_requirement(name: str) -> tuple[int, bool] | None:
    words = set(tokenize(name))
    base = _base(name)
    if len(base) != 1 or words - set(base) - _ANIMAL_DESCRIPTORS:
        _reject(RecipeSafetyFailure.COOKING)
    if base[0] in _POULTRY:
        return 165, False
    if base[0] in _CUTS:
        return (160, False) if words & {"ground", "minced"} else (145, True)
    if base[0] in _FINFISH and not words & {"ground", "minced"}:
        return 145, False
    _reject(RecipeSafetyFailure.COOKING)
    return None


def _metadata(recipe: StructuredRecipe, names: dict[tuple[str, ...], str]) -> None:
    exclusions: list[tuple[str, ...]] = []
    lines = {line.line_id: line for line in recipe.ingredients}
    for sub in recipe.substitutions:
        if not sub.replaces.isascii() or not resolve(sub.replaces).is_known:
            _reject(RecipeSafetyFailure.PROSE)
        replacement = lines[sub.ingredient_line_id].name
        exclusions.extend(
            (
                tokenize(f"use {replacement} instead of {sub.replaces}"),
                tokenize(f"{replacement} replaces {sub.replaces}"),
            )
        )
    phrases = sorted((*names, *exclusions), key=len, reverse=True)
    texts = (
        recipe.title,
        *recipe.fit_reasons,
        *recipe.adaptations,
        *(sub.reason for sub in recipe.substitutions),
    )
    for text in texts:
        if not text.isascii():
            _reject(RecipeSafetyFailure.PROSE)
        words = tokenize(text)
        index = 0
        while index < len(words):
            phrase = next(
                (phrase for phrase in phrases if words[index : index + len(phrase)] == phrase),
                None,
            )
            if phrase:
                index += len(phrase)
            elif words[index] in _NEUTRAL or (
                words[index].isdigit()
                and 0 < int(words[index]) <= recipe.total_minutes
                and index + 1 < len(words)
                and words[index + 1] == "minute"
            ):
                index += 1
            else:
                _reject(RecipeSafetyFailure.PROSE)


def _foods(text: str, names: dict[tuple[str, ...], str]) -> set[str]:
    if "raw" in tokenize(text):
        _reject(RecipeSafetyFailure.COOKING)
    parts = re.split(r",\s*|\s+(?:and|with|in|into|over|to)\s+", text)
    ids: set[str] = set()
    for part in parts:
        part = re.sub(r"^(?:the |with the |with )", "", part.strip())
        line_id = names.get(tokenize(part))
        if line_id is None:
            _reject(RecipeSafetyFailure.PROSE)
        else:
            ids.add(line_id)
    return ids


def validate_final_recipe(recipe: StructuredRecipe, profile: SafetyProfile) -> StructuredRecipe:
    """Return a fresh schema-checked recipe, never a reusable safety certificate.

    Only the documented controlled instruction grammar is supported. Unknown prose is
    rejected rather than relying on a keyword blacklist or an LLM's safety assertion.
    """
    try:
        checked = StructuredRecipe.model_validate_json(
            recipe.model_dump_json(by_alias=True, warnings=False)
        )
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecipeSafetyError(RecipeSafetyFailure.STRUCTURE) from None
    safety = validate_ingredients(
        profile,
        tuple(IngredientLine(line_id=line.line_id, name=line.name) for line in checked.ingredients),
    )
    if safety.verdict != Verdict.PASS:
        _reject(RecipeSafetyFailure.INGREDIENT)
    names: dict[tuple[str, ...], str] = {}
    temperatures: dict[str, tuple[int, bool]] = {}
    requires_heat: set[str] = set()
    for line in checked.ingredients:
        if not re.fullmatch(r"[A-Za-z][A-Za-z -]*", line.name):
            _reject(RecipeSafetyFailure.INGREDIENT)
        resolution = resolve(line.name)
        base = _base(line.name)
        if not resolution.is_known or base not in _ALIASES:
            _reject(RecipeSafetyFailure.INGREDIENT)
        traits = {trait for entry in resolution.entries for trait in entry.contains}
        if (
            any(entry.may_contain for entry in resolution.entries)
            or traits & {Trait.EGG, Trait.MILK, Trait.SHELLFISH}
            or set(base) & {"flour", "dough", "sprout"}
            or any(
                entry.ingredient_id in {"ingredient:wheat-flour", "ingredient:rice-flour"}
                for entry in resolution.entries
            )
        ):
            _reject(RecipeSafetyFailure.COOKING)
        if traits & {Trait.MEAT, Trait.FISH}:
            requirement = _heat_requirement(line.name)
            assert requirement is not None
            temperatures[line.line_id] = requirement
        elif set(tokenize(line.name)) & {"cooked", "roasted"}:
            temperatures[line.line_id] = (165, False)
        if set(base) & _STARCH:
            requires_heat.add(line.line_id)
        if any(entry.ingredient_id == "ingredient:chickpea" for entry in resolution.entries):
            if "canned" not in tokenize(line.name):
                _reject(RecipeSafetyFailure.COOKING)
        for phrase in {base, tokenize(line.name)}:
            if phrase in names and names[phrase] != line.line_id:
                _reject(RecipeSafetyFailure.INGREDIENT)
            names[phrase] = line.line_id
    _metadata(checked, names)
    cooked: set[str] = set()
    for step in checked.instructions:
        text = step.text.strip().rstrip(".")
        if not text.replace("\u00b0", "").isascii():
            _reject(RecipeSafetyFailure.PROSE)
        temperature = _TEMPERATURE.fullmatch(text)
        mentioned: set[str] = set()
        if temperature:
            mentioned = _foods(temperature[1], names)
            if len(mentioned) != 1:
                _reject(RecipeSafetyFailure.COOKING)
            line_id = next(iter(mentioned))
            minimum, rest = temperatures.get(line_id, (0, False))
            if (
                not minimum
                or not minimum <= int(temperature[2]) <= 212
                or (rest and temperature[3] is None)
                or (temperature[3] is not None and int(temperature[3]) > checked.total_minutes)
            ):
                _reject(RecipeSafetyFailure.COOKING)
            cooked.add(line_id)
        else:
            for clause in re.split(r",?\s+then\s+", text, flags=re.IGNORECASE):
                action, separator, foods = clause.partition(" ")
                action = action.casefold()
                if not separator or action not in _ACTIONS:
                    _reject(RecipeSafetyFailure.PROSE)
                foods = re.sub(r"\s+until (?:tender|soft)$", "", foods, flags=re.IGNORECASE)
                duration = re.search(r"\s+for ([1-9]\d*) minutes?$", foods, re.IGNORECASE)
                if duration:
                    if int(duration[1]) > checked.total_minutes:
                        _reject(RecipeSafetyFailure.COOKING)
                    foods = foods[: duration.start()]
                ids = _foods(foods, names)
                if action in _COMBINE and (ids & temperatures.keys()) - cooked:
                    _reject(RecipeSafetyFailure.COOKING)
                if action not in _HEAT and len(ids) > 1 and (ids & temperatures.keys()) - cooked:
                    _reject(RecipeSafetyFailure.COOKING)
                if action == "serve" and (ids & requires_heat) - cooked:
                    _reject(RecipeSafetyFailure.COOKING)
                if action in _HEAT:
                    cooked.update(ids - temperatures.keys())
                mentioned.update(ids)
        if mentioned != set(step.ingredient_line_ids):
            _reject(RecipeSafetyFailure.STRUCTURE)
    if (temperatures.keys() | requires_heat) - cooked:
        _reject(RecipeSafetyFailure.COOKING)
    render_recipe(checked)
    return checked


def render_recipe(recipe: StructuredRecipe) -> str:
    """Call only with a freshly checked recipe; never append model-authored commentary."""
    ingredients = "\n".join(
        f"- {line.name}"
        + (f": {line.quantity} {line.unit}" if line.quantity is not None and line.unit else "")
        for line in recipe.ingredients
    )
    steps = "\n".join(f"{step.step}. {step.text}" for step in recipe.instructions)
    text = (
        f"{recipe.title}\n{recipe.servings} servings; {recipe.total_minutes} minutes.\n\n"
        f"Ingredients\n{ingredients}\n\nInstructions\n{steps}\n\n{HANDLING_NOTICE}"
    )
    if len(text) > 4000:
        _reject(RecipeSafetyFailure.STRUCTURE)
    return text
