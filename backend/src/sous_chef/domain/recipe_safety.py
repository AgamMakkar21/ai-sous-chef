"""Current-profile allergy checks and recipe integrity, not cooking certification."""

import re
import unicodedata
from enum import StrEnum

from pydantic import ValidationError

from sous_chef.domain.food_safety import (
    FindingKind,
    IngredientLine,
    SafetyProfile,
    Verdict,
    validate_ingredients,
)
from sous_chef.domain.ingredient_catalog import resolve, tokenize
from sous_chef.domain.recipes import StructuredRecipe

POLICY_VERSION = "3"
GUIDANCE = "https://www.cdc.gov/food-safety/prevention/index.html"
UNABLE = (
    "I couldn't complete a recipe that fits your request and stored allergies "
    "within the allowed attempts. "
    "Please try a different dish or ingredients. No recipe was saved."
)
HANDLING_NOTICE = (
    "Wash hands and utensils; rinse produce under running water. Keep raw animal foods "
    "separate from ready-to-eat foods. Refrigerate perishables in shallow containers within "
    "2 hours (1 hour above 90 F); keep the refrigerator at 40 F or below."
)


class RecipeSafetyFailure(StrEnum):
    STRUCTURE = "invalid_recipe_structure"
    INGREDIENT = "recipe_ingredient_rejected"
    PROFILE = "recipe_profile_unavailable"


class RecipeSafetyError(ValueError):
    """Only a bounded category; never include ingredients, profile data or rejected text."""

    def __init__(self, code: RecipeSafetyFailure) -> None:
        self.code = code
        super().__init__(code.value)


def _reject(code: RecipeSafetyFailure) -> None:
    raise RecipeSafetyError(code)


def _check_prose(recipe: StructuredRecipe, profile: SafetyProfile) -> None:
    lines = {line.line_id: line for line in recipe.ingredients}
    exclusions: list[tuple[str, str]] = []
    for sub in recipe.substitutions:
        if resolve(sub.replaces).is_known:
            replacement = lines[sub.ingredient_line_id].name
            for phrase in (
                f"use {replacement} instead of {sub.replaces}",
                f"{replacement} replaces {sub.replaces}",
            ):
                exclusions.append((" ".join(tokenize(phrase)), " ".join(tokenize(replacement))))
    metadata = (
        recipe.title,
        *recipe.fit_reasons,
        *recipe.adaptations,
        *(sub.reason for sub in recipe.substitutions),
    )
    texts: list[str] = []
    for text in metadata:
        normalized = " ".join(tokenize(text))
        for phrase, replacement in exclusions:
            normalized = re.sub(
                r"\b" + re.escape(phrase) + r"(?: to respect (?:the )?[a-z]+ restriction)?\b",
                replacement,
                normalized,
            )
        texts.append(normalized)
    texts.extend(step.text for step in recipe.instructions)
    for text in (
        *(line.name for line in recipe.ingredients),
        *metadata,
        *(step.text for step in recipe.instructions),
    ):
        if any(
            char.isalpha() and not char.isascii() for char in unicodedata.normalize("NFKD", text)
        ):
            _reject(RecipeSafetyFailure.INGREDIENT)
    for text in texts:
        result = validate_ingredients(profile, (IngredientLine(line_id="prose", name=text),))
        # Ordinary prose is not an ingredient name; only actual restriction findings apply.
        if any(finding.kind != FindingKind.UNKNOWN_INGREDIENT for finding in result.findings):
            _reject(RecipeSafetyFailure.INGREDIENT)


def validate_final_recipe(recipe: StructuredRecipe, profile: SafetyProfile) -> StructuredRecipe:
    """Revalidate structure and current allergies without judging cooking methods."""
    try:
        checked = StructuredRecipe.model_validate_json(
            recipe.model_dump_json(by_alias=True, warnings=False)
        )
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecipeSafetyError(RecipeSafetyFailure.STRUCTURE) from None
    allergy_profile = SafetyProfile(allergies=profile.allergies)
    safety = validate_ingredients(
        allergy_profile,
        tuple(IngredientLine(line_id=line.line_id, name=line.name) for line in checked.ingredients),
    )
    if safety.verdict != Verdict.PASS:
        _reject(RecipeSafetyFailure.INGREDIENT)
    if profile.allergies:
        _check_prose(checked, allergy_profile)
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
