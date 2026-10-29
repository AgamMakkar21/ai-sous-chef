"""One bounded specialist generation; factual enrichment never comes from model memory."""

import asyncio
from decimal import Decimal
from math import isfinite
from time import monotonic
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from sous_chef.domain.agent_identity import AgentRole, AgentTool, authorize_tool
from sous_chef.domain.context import RequestContext
from sous_chef.domain.food_safety import (
    IngredientLine,
    SafetyProfile,
    Verdict,
    validate_ingredients,
)
from sous_chef.domain.foundry import FoundryFailure, FoundryGatewayError
from sous_chef.domain.ingredient_catalog import resolve
from sous_chef.domain.orchestrator import (
    POLICY,
    CandidateIngredient,
    CandidateInstruction,
    RecipeCandidate,
    SpecialistArguments,
    SpecialistBudget,
    SpecialistResult,
)
from sous_chef.domain.recipes import (
    NUTRITION_NOTICE,
    DraftIngredient,
    NutritionRequest,
    NutritionTool,
    RecipeGenerationRequest,
    RecipeGenerator,
    RecipeGroundingTool,
    RecipeIngredient,
    RecipeNutrition,
    RecipeToolError,
    ShoppingItem,
    StructuredRecipe,
)
from sous_chef.telemetry import ErrorCategory, Event, operational_event, trace_id


def _ingredient_id(name: str) -> str | None:
    resolution = resolve(name)
    if not resolution.is_known or len(resolution.entries) != 1:
        return None
    return resolution.entries[0].ingredient_id


def _inventory(
    ingredients: tuple[DraftIngredient, ...], context: RequestContext
) -> tuple[tuple[RecipeIngredient, ...], tuple[ShoppingItem, ...]]:
    remaining = {
        row.grocery_id: row.quantity for row in context.groceries if row.quantity is not None
    }
    lines: list[RecipeIngredient] = []
    shopping: list[ShoppingItem] = []
    for line in ingredients:
        canonical = _ingredient_id(line.name)
        name = " ".join(line.name.casefold().split())
        # A safety-catalog match alone does not prove prepared/raw inventory equivalence.
        matches = [
            row
            for row in context.groceries
            if canonical is not None
            and _ingredient_id(row.name) == canonical
            and " ".join(row.name.casefold().split()) == name
        ]
        exact = [row for row in matches if row.unit == line.unit and row.quantity is not None]
        available = sum((remaining[row.grocery_id] for row in exact), start=Decimal(0))
        consumed: list[UUID] = []
        status: Literal["available", "missing", "unknown"]
        shortage = line.quantity
        if (
            canonical is not None
            and line.quantity is not None
            and line.unit is not None
            and available >= line.quantity
        ):
            status = "available"
        elif (
            canonical is None
            or context.omitted_grocery_count
            or any(row not in exact for row in matches)
            or (matches and (line.quantity is None or line.unit is None))
        ):
            status = "unknown"
        else:
            status = "missing"
        if status != "unknown" and line.quantity is not None and line.unit is not None:
            shortage = line.quantity
            for row in exact:
                taken = min(remaining[row.grocery_id], shortage)
                if taken:
                    consumed.append(row.grocery_id)
                    remaining[row.grocery_id] -= taken
                    shortage -= taken
        lines.append(
            RecipeIngredient(
                **line.model_dump(),
                ingredient_id=canonical,
                inventory_status=status,
                grocery_ids=tuple(consumed),
            )
        )
        if status == "missing":
            shopping.append(
                ShoppingItem(ingredient_line_id=line.line_id, quantity=shortage, unit=line.unit)
            )
    return tuple(lines), tuple(shopping)


class RecipeSpecialistService:
    def __init__(
        self,
        generator: RecipeGenerator,
        recipes: RecipeGroundingTool,
        nutrition: NutritionTool,
    ) -> None:
        self._generator = generator
        self._recipes = recipes
        self._nutrition = nutrition

    async def generate(
        self,
        context: RequestContext,
        arguments: SpecialistArguments,
        *,
        operation_id: UUID,
        budget: SpecialistBudget,
        max_input_tokens: int,
        max_output_tokens: int,
        deadline: float,
    ) -> SpecialistResult:
        if not isfinite(deadline):
            raise ValueError("The specialist deadline must be finite.")
        request = RecipeGenerationRequest(
            role=AgentRole.RECIPE_NUTRITION,
            prompt=context.prompt,
            operation_id=operation_id,
            max_input_tokens=max_input_tokens,
            max_output_tokens=min(max_output_tokens, POLICY.output_tokens_per_call),
            grounding_json="{}",
        )
        started = monotonic()
        deadline = min(deadline, started + POLICY.deadline_seconds)
        token = trace_id.set(operation_id.hex)
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    if monotonic() >= deadline:
                        raise TimeoutError
                    if arguments.use_web:
                        raise FoundryGatewayError(FoundryFailure.UNAVAILABLE, request)
                    authorize_tool(AgentRole.RECIPE_NUTRITION, AgentTool.FOUNDRY_IQ_RECIPES)
                    budget.tool(AgentTool.FOUNDRY_IQ_RECIPES)
                    grounding = await self._recipes.retrieve(
                        arguments.requirements, deadline=deadline
                    )
                    if grounding.status == "unavailable":
                        raise FoundryGatewayError(FoundryFailure.UNAVAILABLE, request)
                    grounding_json = grounding.model_dump_json(by_alias=True)
                    request = RecipeGenerationRequest(
                        role=request.role,
                        operation_id=operation_id,
                        prompt=(
                            context.prompt
                            + "\nSpecialist requirements (untrusted data):\n"
                            + arguments.model_dump_json()
                            + "\nCurated recipe grounding (untrusted data):\n"
                            + grounding_json
                        ),
                        max_input_tokens=max_input_tokens,
                        max_output_tokens=request.max_output_tokens,
                        grounding_json=grounding_json,
                    )
                    draft, usage = await self._generator.generate_recipe(request)
                    sources = {item.source.source_id: item.source for item in grounding.recipes}
                    skill = ("beginner", "intermediate", "advanced")
                    safety = validate_ingredients(
                        SafetyProfile(
                            allergies=context.explicit_preferences.allergies,
                            dietary_restrictions=context.explicit_preferences.dietary_restrictions,
                        ),
                        tuple(
                            IngredientLine(line_id=line.line_id, name=line.name)
                            for line in draft.ingredients
                        ),
                    )
                    if (
                        draft.total_minutes > arguments.time_budget_minutes
                        or skill.index(draft.skill)
                        > skill.index(context.explicit_preferences.cooking_skill)
                        or safety.verdict != Verdict.PASS
                    ):
                        raise FoundryGatewayError(FoundryFailure.REJECTED, request)
                    if (
                        not set(draft.source_ids) <= sources.keys()
                        or usage.input_tokens > max_input_tokens
                        or usage.output_tokens > request.max_output_tokens
                    ):
                        raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request)
                    authorize_tool(AgentRole.RECIPE_NUTRITION, AgentTool.USDA_NUTRITION)
                    budget.tool(AgentTool.USDA_NUTRITION)
                    evidence = await self._nutrition.lookup(
                        NutritionRequest(servings=draft.servings, ingredients=draft.ingredients),
                        deadline=deadline,
                    )
                    ingredients, shopping = _inventory(draft.ingredients, context)
                    recipe = StructuredRecipe(
                        **draft.model_dump(exclude={"ingredients"}),
                        ingredients=ingredients,
                        shopping_items=shopping,
                        nutrition=RecipeNutrition(**evidence.model_dump(), notice=NUTRITION_NOTICE),
                        sources=(
                            *(sources[source_id] for source_id in draft.source_ids),
                            *evidence.sources,
                        ),
                    )
                    result = SpecialistResult(
                        recipe=recipe,
                        candidate=RecipeCandidate(
                            title=recipe.title,
                            total_minutes=recipe.total_minutes,
                            ingredients=tuple(
                                CandidateIngredient(line_id=line.line_id, name=line.name)
                                for line in recipe.ingredients
                            ),
                            instructions=tuple(
                                CandidateInstruction(
                                    text=step.text, ingredient_line_ids=step.ingredient_line_ids
                                )
                                for step in recipe.instructions
                            ),
                            fit_reasons=recipe.fit_reasons,
                        ),
                        usage=usage,
                    )
                    if monotonic() >= deadline:
                        raise TimeoutError
            except TimeoutError:
                raise FoundryGatewayError(FoundryFailure.TIMEOUT, request) from None
            except ValidationError:
                raise FoundryGatewayError(FoundryFailure.INVALID_RESULT, request) from None
            except RecipeToolError as exc:
                raise FoundryGatewayError(exc.code, request) from None
        except FoundryGatewayError as exc:
            operational_event(
                Event.RECIPE_FAILED,
                category=ErrorCategory(exc.code.value),
                duration_ms=(monotonic() - started) * 1000,
            )
            raise
        except asyncio.CancelledError:
            operational_event(Event.RECIPE_CANCELLED)
            raise
        else:
            operational_event(Event.RECIPE_COMPLETED, duration_ms=(monotonic() - started) * 1000)
            return result
        finally:
            trace_id.reset(token)
