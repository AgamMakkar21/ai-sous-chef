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
AI_RECIPE_NOTICE = (
    "AI-knowledge recipe: recipe sources were unavailable. "
    "This recipe is not externally verified; nutrition sources are listed separately."
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
    fit_notes = (
        "\n\nRecipe notes\n" + "\n".join(f"- {note}" for note in recipe.fit_notes)
        if recipe.fit_notes
        else ""
    )
    text = (
        f"{recipe.title}\n{recipe.servings} servings; {recipe.total_minutes} minutes."
        f"{fit_notes}\n\n"
        f"Ingredients\n{ingredients}\n\nInstructions\n{steps}\n\n{HANDLING_NOTICE}"
    )
    if recipe.generation_source == "ai_knowledge":
        text = f"{AI_RECIPE_NOTICE}\n\n{text}"
    labels = ["USDA"] if recipe.nutrition.mappings else []
    fallback_sources = {item.source for item in recipe.nutrition.ingredient_calories} | {
        item.source for item in recipe.nutrition.nutrient_estimates
    }
    for source, label in (("web_iq", "Web estimate"), ("ai_knowledge", "AI estimate")):
        if source in fallback_sources:
            labels.append(label)
    summary = f"\n\nNutrition [{', '.join(labels) if labels else 'Unavailable'}]"
    energy = next((item for item in recipe.nutrition.nutrients if item.name == "energy"), None)
    if energy is not None:
        partial = "Partial: " if recipe.nutrition.status == "partial" else ""
        summary += (
            f": {partial}{energy.amount:g} kcal {recipe.nutrition.serving_basis.replace('_', ' ')}."
        )
    if len(text) > 4000:
        text = (
            (f"{AI_RECIPE_NOTICE}\n\n" if recipe.generation_source == "ai_knowledge" else "")
            + f"{recipe.title}\n{recipe.servings} servings; {recipe.total_minutes} minutes."
            + f"{fit_notes}\n\n"
            "See the recipe card for the complete ingredients, instructions, nutrition "
            f"and sources.\n\n{HANDLING_NOTICE}" + summary
        )
    elif len(text) + len(summary) <= 4000:
        text += summary
    return text
