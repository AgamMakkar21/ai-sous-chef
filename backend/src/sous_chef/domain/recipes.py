"""Versioned specialist drafts, grounded recipe results and bounded tool contracts."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol, Self, runtime_checkable
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from sous_chef.domain.agent_identity import AgentRole
from sous_chef.domain.foundry import FoundryFailure, FoundryRequest
from sous_chef.domain.groceries import GroceryModel, GroceryUnit, Quantity
from sous_chef.domain.nutrition import FdcId
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
NutrientName = Literal["energy", "protein", "carbohydrate", "fat", "fiber", "sodium"]
NUTRIENT_NAMES: Final[tuple[NutrientName, ...]] = (
    "energy",
    "protein",
    "carbohydrate",
    "fat",
    "fiber",
    "sodium",
)


class RecipeModel(GroceryModel):
    model_config = ConfigDict(strict=True, json_schema_serialization_defaults_required=True)


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
    status: Literal["available", "empty", "unavailable"]
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


class RecipeSourceClaim(RecipeModel):
    """Only an exact evidence excerpt is attributed; adaptations have no citations."""

    source_id: ReferenceId
    quote: Text = Field(repr=False)


class RecipeDraft(RecipeModel):
    """Only adaptable cooking content is generated; factual enrichment is backend-owned."""

    schema_version: Literal[2]
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
    source_claims: tuple[RecipeSourceClaim, ...] = Field(max_length=10, repr=False)

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
            or any(claim.source_id not in self.source_ids for claim in self.source_claims)
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
    name: NutrientName
    amount: float = Field(ge=0, le=10_000_000)
    unit: Literal["kcal", "g", "mg"]

    @model_validator(mode="after")
    def nutrient_unit(self) -> Self:
        expected = "kcal" if self.name == "energy" else "mg" if self.name == "sodium" else "g"
        if self.unit != expected:
            raise ValueError("Use the specified nutrient unit.")
        return self


class QuantitySource(RecipeModel):
    reference: str = Field(min_length=1, max_length=500, repr=False)
    quote: str = Field(min_length=1, max_length=1500, repr=False)
    servings: int = Field(ge=1, le=100)
    weight_grams: float = Field(gt=0, le=100_000, allow_inf_nan=False)


class QuantityEstimate(RecipeModel):
    weight_grams: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    sources: tuple[QuantitySource, ...] = Field(min_length=1, max_length=3, repr=False)

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        if len({source.reference for source in self.sources}) != len(self.sources):
            raise ValueError("Quantity estimates require distinct recipe sources.")
        return self


class NutritionMapping(RecipeModel):
    ingredient_line_id: LineId
    quantity: MeasuredQuantity | None
    unit: GroceryUnit | None
    source_id: ReferenceId
    usda_food_id: ReferenceId
    weight_grams: float | None = Field(default=None, gt=0, le=1_000_000_000)
    food_description: str | None = Field(default=None, min_length=1, max_length=500)
    conversion_basis: str | None = Field(default=None, min_length=1, max_length=500)
    approximate_match: bool = False
    quantity_estimate: QuantityEstimate | None = None

    @model_validator(mode="after")
    def attributed_quantity(self) -> Self:
        if self.quantity_estimate is not None:
            if self.weight_grams != self.quantity_estimate.weight_grams:
                raise ValueError("Estimated weight must match its quantity evidence.")
        elif self.quantity is None or self.unit is None:
            raise ValueError("Missing amounts require a source-backed quantity estimate.")
        return self


class NutrientCoverage(RecipeModel):
    name: NutrientName
    status: Literal["known", "partial", "unavailable"]
    whole_recipe_amount: float | None = Field(ge=0, le=200_000_000)
    covered_ingredient_line_ids: tuple[LineId, ...] = Field(max_length=30)
    unknown_ingredient_line_ids: tuple[LineId, ...] = Field(max_length=30)

    @model_validator(mode="after")
    def coverage_state(self) -> Self:
        covered = set(self.covered_ingredient_line_ids)
        unknown = set(self.unknown_ingredient_line_ids)
        expected = "partial" if covered and unknown else "known" if covered else "unavailable"
        if (
            not covered | unknown
            or covered & unknown
            or len(covered) != len(self.covered_ingredient_line_ids)
            or len(unknown) != len(self.unknown_ingredient_line_ids)
            or len(covered | unknown) > 30
            or bool(covered) != (self.whole_recipe_amount is not None)
            or self.status != expected
        ):
            raise ValueError("Nutrient coverage must distinguish attributed amounts from unknowns.")
        return self


class IngredientCalories(RecipeModel):
    """Calories for this ingredient's entire recipe quantity, not per 100 g."""

    ingredient_line_id: LineId
    amount: float = Field(ge=0, le=10_000_000)
    source: Literal["usda_mcp", "web_iq", "ai_knowledge"]
    reference: str | None = Field(default=None, min_length=1, max_length=500, repr=False)
    basis: str = Field(min_length=1, max_length=500, pattern=r"\S", repr=False)

    @model_validator(mode="after")
    def attribution(self) -> Self:
        if self.source == "ai_knowledge":
            if self.reference is not None:
                raise ValueError("AI knowledge estimates must not invent a citation.")
        else:
            if self.reference is None:
                raise ValueError("USDA and web calories require a source reference.")
            WebClaim(text=self.basis, source_url=self.reference)
        return self


class CalorieEstimate(IngredientCalories):
    source: Literal["web_iq", "ai_knowledge"]


class NutritionUnknownComponent(RecipeModel):
    ingredient_line_id: LineId
    reason: Literal[
        "missing_quantity",
        "missing_unit",
        "unsupported_unit",
        "food_unavailable",
        "lookup_limit",
        "missing_nutrients",
        "service_unavailable",
    ]
    unknown_nutrients: tuple[NutrientName, ...] = Field(
        default=NUTRIENT_NAMES, min_length=1, max_length=6
    )

    @model_validator(mode="after")
    def unique_nutrients(self) -> Self:
        if len(set(self.unknown_nutrients)) != len(self.unknown_nutrients):
            raise ValueError("Unknown nutrients must be unique.")
        return self


class NutritionEvidence(RecipeModel):
    """USDA-calculated nutrition with separately attributed recipe-weight assumptions."""

    status: Literal["known", "partial", "unavailable"]
    serving_basis: Literal["per_serving", "whole_recipe"]
    servings: int = Field(ge=1, le=20)
    nutrients: tuple[Nutrient, ...] = Field(max_length=6)
    mappings: tuple[NutritionMapping, ...] = Field(max_length=30)
    sources: tuple[RecipeSource, ...] = Field(max_length=30)
    is_estimate: Literal[True] = True
    notice: Literal["Informational estimate, not medical advice."] = NUTRITION_NOTICE
    coverage: tuple[NutrientCoverage, ...] = Field(default=(), max_length=6)
    unknown_components: tuple[NutritionUnknownComponent, ...] = Field(default=(), max_length=30)
    ingredient_calories: tuple[IngredientCalories, ...] = Field(default=(), max_length=30)

    @model_validator(mode="after")
    def evidence_state(self) -> Self:
        source_ids = {source.source_id for source in self.sources}
        mapping_ids = [mapping.ingredient_line_id for mapping in self.mappings]
        nutrients = [nutrient.name for nutrient in self.nutrients]
        calorie_ids = {item.ingredient_line_id for item in self.ingredient_calories}
        if (
            len(source_ids) != len(self.sources)
            or len(mapping_ids) != len(set(mapping_ids))
            or len(nutrients) != len(set(nutrients))
            or any(source.kind != "usda" for source in self.sources)
            or any(mapping.source_id not in source_ids for mapping in self.mappings)
            or source_ids != {mapping.source_id for mapping in self.mappings}
            or len(calorie_ids) != len(self.ingredient_calories)
        ):
            raise ValueError("Nutrition requires unique USDA sources and ingredient mappings.")
        if self.status == "unavailable":
            if self.nutrients or self.mappings or self.sources or self.ingredient_calories:
                raise ValueError("Unavailable nutrition must not contain invented amounts.")
        elif not self.nutrients or not (self.mappings or self.ingredient_calories):
            raise ValueError("Known or partial nutrition requires attributed tool evidence.")
        for item in self.ingredient_calories:
            mapping = next(
                (row for row in self.mappings if row.ingredient_line_id == item.ingredient_line_id),
                None,
            )
            if item.source == "usda_mcp" and (
                mapping is None
                or item.reference
                != next(
                    source.reference
                    for source in self.sources
                    if source.source_id == mapping.source_id
                )
            ):
                raise ValueError(
                    "USDA calories must reference the ingredient's actual USDA mapping."
                )
        if self.coverage:
            names = {item.name for item in self.coverage}
            line_ids = set(
                self.coverage[0].covered_ingredient_line_ids
                + self.coverage[0].unknown_ingredient_line_ids
            )
            covered_ids = {
                line_id for item in self.coverage for line_id in item.covered_ingredient_line_ids
            }
            unknown_pairs = {
                (line_id, item.name)
                for item in self.coverage
                for line_id in item.unknown_ingredient_line_ids
            }
            component_pairs = {
                (item.ingredient_line_id, name)
                for item in self.unknown_components
                for name in item.unknown_nutrients
            }
            expected_status = (
                "known"
                if all(item.status == "known" for item in self.coverage)
                else "partial"
                if self.nutrients
                else "unavailable"
            )
            if (
                len(names) != 6
                or covered_ids != set(mapping_ids) | calorie_ids
                or any(
                    not set(item.covered_ingredient_line_ids) <= set(mapping_ids)
                    for item in self.coverage
                    if item.name != "energy"
                )
                or (
                    bool(self.ingredient_calories)
                    and next(
                        item for item in self.coverage if item.name == "energy"
                    ).covered_ingredient_line_ids
                    != tuple(item.ingredient_line_id for item in self.ingredient_calories)
                )
                or any(
                    set(item.covered_ingredient_line_ids + item.unknown_ingredient_line_ids)
                    != line_ids
                    for item in self.coverage
                )
                or set(nutrients)
                != {item.name for item in self.coverage if item.status != "unavailable"}
                or unknown_pairs != component_pairs
                or len({item.ingredient_line_id for item in self.unknown_components})
                != len(self.unknown_components)
                or self.status != expected_status
            ):
                raise ValueError(
                    "Nutrition coverage must account for every ingredient and nutrient."
                )
            if self.ingredient_calories:
                energy_coverage = next(item for item in self.coverage if item.name == "energy")
                energy = next(item for item in self.nutrients if item.name == "energy")
                total = sum(item.amount for item in self.ingredient_calories)
                expected = total / self.servings if self.serving_basis == "per_serving" else total
                if (
                    energy_coverage.whole_recipe_amount is None
                    or abs(energy_coverage.whole_recipe_amount - total) > 0.011
                    or abs(energy.amount - expected) > 0.011
                ):
                    raise ValueError("Calorie totals must match the ingredient-level evidence.")
        elif self.unknown_components:
            raise ValueError("Unknown components require detailed nutrient coverage.")
        elif self.ingredient_calories:
            raise ValueError("Ingredient calories require nutrient coverage.")
        return self


class RecipeNutrition(NutritionEvidence):
    """Recipe and ingredient lookups carry the same informational-estimate contract."""


class WebClaim(RecipeModel):
    text: str = Field(min_length=1, max_length=500, pattern=r"\S", repr=False)
    source_url: str = Field(min_length=1, max_length=500, repr=False)

    @model_validator(mode="after")
    def public_reference(self) -> Self:
        url = urlsplit(self.source_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.port not in (None, 443)
        ):
            raise ValueError("Web claims require HTTPS citations without credentials.")
        return self


class WebDraft(RecipeModel):
    claims: tuple[WebClaim, ...] = Field(max_length=3, repr=False)
    calorie_estimates: tuple[CalorieEstimate, ...] = Field(default=(), max_length=30, repr=False)


class CombinedRecipeDraft(RecipeDraft):
    web_claims: tuple[WebClaim, ...] = Field(max_length=3, repr=False)


class CombinedRecipeOutput(RecipeModel):
    recipe: CombinedRecipeDraft | None
    calorie_estimates: tuple[CalorieEstimate, ...] = Field(max_length=30, repr=False)


class WebCitation(RecipeModel):
    url: str = Field(min_length=1, max_length=500, repr=False)
    title: Text = Field(repr=False)


class WebGrounding(WebDraft):
    claims: tuple[WebClaim, ...] = Field(min_length=1, max_length=3, repr=False)
    citations: tuple[WebCitation, ...] = Field(min_length=1, max_length=3, repr=False)
    retrieved_at: datetime

    @model_validator(mode="after")
    def attributed_claims(self) -> Self:
        urls = [citation.url for citation in self.citations]
        if (
            set(urls) != {claim.source_url for claim in self.claims}
            or len(urls) != len(set(urls))
            or self.retrieved_at.tzinfo is None
        ):
            raise ValueError("Web claims require unique attributable citations and retrieval time.")
        return self


class StructuredRecipe(RecipeDraft):
    """An unsaved candidate, NOT a final safety approval or a cooked recipe."""

    ingredients: tuple[RecipeIngredient, ...] = Field(min_length=1, max_length=30, repr=False)
    shopping_items: tuple[ShoppingItem, ...] = Field(max_length=30, repr=False)
    nutrition: RecipeNutrition = Field(repr=False)
    sources: tuple[RecipeSource, ...] = Field(min_length=1, max_length=33)
    web_grounding: WebGrounding | None = Field(repr=False)

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
        if self.nutrition.coverage:
            coverage = self.nutrition.coverage[0]
            if set(
                coverage.covered_ingredient_line_ids + coverage.unknown_ingredient_line_ids
            ) != set(ingredients):
                raise ValueError("Nutrition coverage must match the generated recipe.")
        if any(
            item.ingredient_line_id not in ingredients
            for item in self.nutrition.ingredient_calories
        ):
            raise ValueError("Ingredient calories must reference the generated recipe.")
        for mapping in self.nutrition.mappings:
            line = ingredients.get(mapping.ingredient_line_id)
            if line is None or line.quantity != mapping.quantity or line.unit != mapping.unit:
                raise ValueError("Nutrition amounts must match the generated recipe.")
            if mapping.quantity_estimate is not None:
                references = {source.reference for source in recipe_sources}
                if self.web_grounding is not None:
                    references.update(citation.url for citation in self.web_grounding.citations)
                if any(
                    source.reference not in references
                    for source in mapping.quantity_estimate.sources
                ):
                    raise ValueError("Quantity assumptions must cite this recipe's evidence.")
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
    selected_food_ids: dict[LineId, FdcId] = Field(default_factory=dict, max_length=30)
    allow_estimates: bool = False
    quantity_estimates: dict[LineId, QuantityEstimate] = Field(default_factory=dict, max_length=30)

    @model_validator(mode="after")
    def unique_lines(self) -> Self:
        if len({line.line_id for line in self.ingredients}) != len(self.ingredients):
            raise ValueError("Nutrition ingredient line IDs must be unique.")
        if not self.selected_food_ids.keys() <= {line.line_id for line in self.ingredients}:
            raise ValueError("Selected USDA foods must reference requested ingredient lines.")
        if self.quantity_estimates and (
            not self.allow_estimates
            or not self.quantity_estimates.keys() <= {line.line_id for line in self.ingredients}
        ):
            raise ValueError("Quantity estimates require recipe opt-in and existing ingredients.")
        return self


class RecipeGenerationRequest(FoundryRequest):
    role: Literal[AgentRole.RECIPE_NUTRITION]
    prompt: str = Field(min_length=1, max_length=131072, pattern=r"\S", repr=False)
    max_input_tokens: int = Field(ge=1, le=16_000)
    grounding_json: str = Field(min_length=1, max_length=16000, repr=False)


class GroundedRecipeGenerationRequest(RecipeGenerationRequest):
    retrieval_query: str = Field(min_length=1, max_length=2000, repr=False)


class CombinedRecipeGenerationRequest(GroundedRecipeGenerationRequest):
    web_query: str | None = Field(min_length=1, max_length=240, default=None, repr=False)


class WebGroundingRequest(FoundryRequest):
    role: Literal[AgentRole.RECIPE_NUTRITION]
    prompt: str = Field(min_length=1, max_length=240, pattern=r"\S", repr=False)
    max_input_tokens: int = Field(default=2000, ge=1, le=2000)
    allow_empty: bool = False


class CalorieGroundingRequest(WebGroundingRequest):
    prompt: str = Field(min_length=1, max_length=12000, pattern=r"\S", repr=False)
    query: str = Field(min_length=1, max_length=240, pattern=r"\S", repr=False)
    ingredient_line_ids: tuple[LineId, ...] = Field(min_length=1, max_length=30)
    max_input_tokens: int = Field(default=4000, ge=1, le=16_000)


class RecipeGenerator(Protocol):
    async def estimate_calories(
        self, request: CalorieGroundingRequest
    ) -> tuple[tuple[CalorieEstimate, ...], TokenUsage]: ...

    async def ground_web(
        self, request: WebGroundingRequest
    ) -> tuple[WebGrounding | None, TokenUsage]: ...

    async def generate_recipe(
        self, request: RecipeGenerationRequest
    ) -> tuple[RecipeDraft, TokenUsage]: ...

    async def generate_grounded_recipe(
        self, request: GroundedRecipeGenerationRequest
    ) -> tuple[RecipeDraft, RecipeGrounding, TokenUsage]: ...


class RecipeGroundingTool(Protocol):
    async def retrieve(self, requirements: str, *, deadline: float) -> RecipeGrounding: ...


@runtime_checkable
class CombinedRecipeGenerator(Protocol):
    async def generate_combined_recipe(
        self,
        request: CombinedRecipeGenerationRequest,
    ) -> tuple[RecipeDraft, RecipeGrounding, WebGrounding | None, TokenUsage]: ...


class NutritionTool(Protocol):
    async def lookup(self, request: NutritionRequest, *, deadline: float) -> NutritionEvidence: ...


class RecipeToolError(RuntimeError):
    def __init__(self, code: FoundryFailure) -> None:
        self.code = code
        super().__init__(code.value)
