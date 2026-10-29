"""Versioned specialist drafts, grounded recipe results and bounded tool contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol, Self
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from sous_chef.domain.agent_identity import AgentRole
from sous_chef.domain.foundry import FoundryFailure, FoundryRequest
from sous_chef.domain.groceries import GroceryModel, GroceryUnit, Quantity
from sous_chef.domain.profiles import Skill

if TYPE_CHECKING:
    from sous_chef.domain.orchestrator import TokenUsage

Text = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=240)
]
ReferenceId = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
]
LineId = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
MeasuredQuantity = Annotated[Quantity, Field(strict=False)]
NUTRITION_NOTICE: Final = "Informational estimate, not medical advice."


class RecipeModel(GroceryModel):
    model_config = ConfigDict(strict=True)


class RecipeSource(RecipeModel):
    source_id: ReferenceId
    version: Text
    kind: Literal["curated_recipe", "usda"]
    reference: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)
    ] = Field(repr=False)


class CuratedRecipe(RecipeModel):
    source: RecipeSource
    snippet: str = Field(min_length=1, max_length=1500, pattern=r"\S", repr=False)

    @model_validator(mode="after")
    def curated_source(self) -> Self:
        if self.source.kind != "curated_recipe":
            raise ValueError("Recipe grounding requires a curated recipe source.")
        return self


class RecipeGrounding(RecipeModel):
    status: Literal["available", "unavailable"]
    recipes: tuple[CuratedRecipe, ...] = Field(max_length=3, repr=False)

    @model_validator(mode="after")
    def grounding_state(self) -> Self:
        ids = [recipe.source.source_id for recipe in self.recipes]
        if (self.status == "available") != bool(ids) or len(ids) != len(set(ids)):
            raise ValueError("Available grounding requires unique cited recipes.")
        return self


class DraftIngredient(RecipeModel):
    line_id: LineId
    name: str = Field(min_length=1, max_length=100, pattern=r"\S", repr=False)
    quantity: MeasuredQuantity | None
    unit: GroceryUnit | None


class RecipeInstruction(RecipeModel):
    step: int = Field(ge=1, le=30)
    text: str = Field(min_length=1, max_length=500, pattern=r"\S", repr=False)
    ingredient_line_ids: tuple[LineId, ...] = Field(max_length=30)


class Substitution(RecipeModel):
    replaces: Text = Field(repr=False)
    ingredient_line_id: LineId
    reason: Text = Field(repr=False)


class RecipeDraft(RecipeModel):
    """Only adaptable cooking content is generated; factual enrichment is backend-owned."""

    schema_version: Literal[1]
    title: str = Field(min_length=1, max_length=160, pattern=r"\S", repr=False)
    servings: int = Field(ge=1, le=20)
    total_minutes: int = Field(ge=1, le=1440)
    skill: Skill
    ingredients: tuple[DraftIngredient, ...] = Field(min_length=1, max_length=30, repr=False)
    substitutions: tuple[Substitution, ...] = Field(max_length=30, repr=False)
    instructions: tuple[RecipeInstruction, ...] = Field(min_length=1, max_length=30, repr=False)
    fit_reasons: tuple[Text, ...] = Field(min_length=1, max_length=10, repr=False)
    adaptations: tuple[Text, ...] = Field(max_length=10, repr=False)
    source_ids: tuple[ReferenceId, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def references(self) -> Self:
        ids = {line.line_id for line in self.ingredients}
        used = {line_id for step in self.instructions for line_id in step.ingredient_line_ids}
        if (
            len(ids) != len(self.ingredients)
            or used != ids
            or any(
                len(step.ingredient_line_ids) != len(set(step.ingredient_line_ids))
                for step in self.instructions
            )
            or [step.step for step in self.instructions]
            != list(range(1, len(self.instructions) + 1))
            or any(item.ingredient_line_id not in ids for item in self.substitutions)
            or len(self.source_ids) != len(set(self.source_ids))
        ):
            raise ValueError("Recipe references must be unique, complete and ordered.")
        return self


class RecipeIngredient(DraftIngredient):
    ingredient_id: ReferenceId | None
    inventory_status: Literal["available", "missing", "unknown"]
    grocery_ids: tuple[UUID, ...] = Field(max_length=200)


class ShoppingItem(RecipeModel):
    ingredient_line_id: LineId
    quantity: MeasuredQuantity | None
    unit: GroceryUnit | None


class Nutrient(RecipeModel):
    name: Literal["energy", "protein", "carbohydrate", "fat", "fiber", "sodium"]
    amount: float = Field(ge=0, le=10_000_000)
    unit: Literal["kcal", "g", "mg"]

    @model_validator(mode="after")
    def nutrient_unit(self) -> Self:
        expected = "kcal" if self.name == "energy" else "mg" if self.name == "sodium" else "g"
        if self.unit != expected:
            raise ValueError("Use the specified nutrient unit.")
        return self


class NutritionMapping(RecipeModel):
    ingredient_line_id: LineId
    quantity: MeasuredQuantity
    unit: GroceryUnit
    source_id: ReferenceId
    usda_food_id: ReferenceId


class NutritionEvidence(RecipeModel):
    """Tool-calculated amounts for exactly the requested recipe, never model estimates."""

    status: Literal["known", "partial", "unavailable"]
    serving_basis: Literal["per_serving", "whole_recipe"]
    servings: int = Field(ge=1, le=20)
    nutrients: tuple[Nutrient, ...] = Field(max_length=6)
    mappings: tuple[NutritionMapping, ...] = Field(max_length=30)
    sources: tuple[RecipeSource, ...] = Field(max_length=9)

    @model_validator(mode="after")
    def evidence_state(self) -> Self:
        source_ids = {source.source_id for source in self.sources}
        mapping_ids = [mapping.ingredient_line_id for mapping in self.mappings]
        nutrients = [nutrient.name for nutrient in self.nutrients]
        if (
            len(source_ids) != len(self.sources)
            or len(mapping_ids) != len(set(mapping_ids))
            or len(nutrients) != len(set(nutrients))
            or any(source.kind != "usda" for source in self.sources)
            or any(mapping.source_id not in source_ids for mapping in self.mappings)
            or source_ids != {mapping.source_id for mapping in self.mappings}
        ):
            raise ValueError("Nutrition requires unique USDA sources and ingredient mappings.")
        if self.status == "unavailable":
            if self.nutrients or self.mappings or self.sources:
                raise ValueError("Unavailable nutrition must not contain invented amounts.")
        elif not self.nutrients or not self.mappings or not self.sources:
            raise ValueError("Known or partial nutrition requires attributed tool evidence.")
        return self


class RecipeNutrition(NutritionEvidence):
    notice: Literal["Informational estimate, not medical advice."]


class StructuredRecipe(RecipeDraft):
    """An unsaved candidate, NOT a final safety approval or a cooked recipe."""

    ingredients: tuple[RecipeIngredient, ...] = Field(min_length=1, max_length=30, repr=False)
    shopping_items: tuple[ShoppingItem, ...] = Field(max_length=30, repr=False)
    nutrition: RecipeNutrition = Field(repr=False)
    sources: tuple[RecipeSource, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def enrichment_references(self) -> Self:
        ingredients = {line.line_id: line for line in self.ingredients}
        missing = {line.line_id for line in self.ingredients if line.inventory_status == "missing"}
        shopping = [item.ingredient_line_id for item in self.shopping_items]
        recipe_sources = tuple(source for source in self.sources if source.kind == "curated_recipe")
        nutrition_sources = tuple(source for source in self.sources if source.kind == "usda")
        if (
            set(shopping) != missing
            or len(shopping) != len(missing)
            or {source.source_id for source in recipe_sources} != set(self.source_ids)
            or nutrition_sources != self.nutrition.sources
            or len({source.source_id for source in self.sources}) != len(self.sources)
            or self.nutrition.servings != self.servings
        ):
            raise ValueError("Recipe enrichment must reference the actual recipe and sources.")
        mapped = {mapping.ingredient_line_id for mapping in self.nutrition.mappings}
        if self.nutrition.status == "known" and mapped != set(ingredients):
            raise ValueError("Known nutrition requires every ingredient to be mapped.")
        for mapping in self.nutrition.mappings:
            line = ingredients.get(mapping.ingredient_line_id)
            if line is None or line.quantity != mapping.quantity or line.unit != mapping.unit:
                raise ValueError("Nutrition amounts must match the generated recipe.")
        for item in self.shopping_items:
            line = ingredients[item.ingredient_line_id]
            if item.unit != line.unit or (
                line.quantity is not None
                and (item.quantity is None or item.quantity > line.quantity)
            ):
                raise ValueError("Missing purchases must use the recipe's quantities and units.")
        return self


class NutritionRequest(RecipeModel):
    servings: int = Field(ge=1, le=20)
    ingredients: tuple[DraftIngredient, ...] = Field(min_length=1, max_length=30, repr=False)


class RecipeGenerationRequest(FoundryRequest):
    role: Literal[AgentRole.RECIPE_NUTRITION]
    prompt: str = Field(min_length=1, max_length=131072, pattern=r"\S", repr=False)
    max_input_tokens: int = Field(ge=1, le=16_000)
    grounding_json: str = Field(min_length=1, max_length=16000, repr=False)


class RecipeGenerator(Protocol):
    async def generate_recipe(
        self, request: RecipeGenerationRequest
    ) -> tuple[RecipeDraft, TokenUsage]: ...


class RecipeGroundingTool(Protocol):
    async def retrieve(self, requirements: str, *, deadline: float) -> RecipeGrounding: ...


class NutritionTool(Protocol):
    async def lookup(self, request: NutritionRequest, *, deadline: float) -> NutritionEvidence: ...


class RecipeToolError(RuntimeError):
    def __init__(self, code: FoundryFailure) -> None:
        self.code = code
        super().__init__(code.value)
