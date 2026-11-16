"""Current-profile allergy checks and recipe integrity, not cooking certification."""

from enum import StrEnum

from pydantic import ValidationError

from sous_chef.domain.food_safety import (
    IngredientLine,
    SafetyProfile,
    Verdict,
    validate_declared_allergies,
)
from sous_chef.domain.recipes import StructuredRecipe

POLICY_VERSION = "4"
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


def validate_final_recipe(recipe: StructuredRecipe, profile: SafetyProfile) -> StructuredRecipe:
    """Revalidate structure and current allergies without judging cooking methods."""
    try:
        checked = StructuredRecipe.model_validate_json(
            recipe.model_dump_json(by_alias=True, warnings=False)
        )
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise RecipeSafetyError(RecipeSafetyFailure.STRUCTURE) from None
    safety = validate_declared_allergies(
        profile.allergies,
        tuple(IngredientLine(line_id=line.line_id, name=line.name) for line in checked.ingredients),
    )
    if safety.verdict != Verdict.PASS:
        _reject(RecipeSafetyFailure.INGREDIENT)
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
    if any(item.source != "usda_mcp" for item in recipe.nutrition.ingredient_calories):
        energy = next(item for item in recipe.nutrition.nutrients if item.name == "energy")
        labels = {"web_iq": "Grounding with Bing", "ai_knowledge": "AI knowledge"}
        sources = " / ".join(
            labels[source]
            for source in labels
            if any(item.source == source for item in recipe.nutrition.ingredient_calories)
        )
        summary = (
            f"\n\nCalories: {energy.amount:g} kcal "
            f"{recipe.nutrition.serving_basis.replace('_', ' ')}."
            f" Includes approximate {sources} values, not USDA-verified; see ingredient sources."
        )
        if len(text) + len(summary) <= 4000:
            text += summary
    if recipe.nutrition.nutrient_estimates:
        sources = " / ".join(
            name
            for source, name in (
                ("web_iq", "Grounding with Bing"),
                ("ai_knowledge", "AI knowledge"),
            )
            if any(item.source == source for item in recipe.nutrition.nutrient_estimates)
        )
        summary = (
            f"\n\nMissing USDA nutrients were filled with approximate {sources} "
            "estimates, not USDA-verified; see ingredient nutrient sources."
        )
        if len(text) + len(summary) <= 4000:
            text += summary
    if len(text) > 4000:
        _reject(RecipeSafetyFailure.STRUCTURE)
    return text
