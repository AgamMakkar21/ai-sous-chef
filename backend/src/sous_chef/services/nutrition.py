"""Deterministic recipe estimates from attributed FoodData Central evidence."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal
from time import monotonic
from typing import Literal

from opentelemetry import trace
from pydantic import ValidationError

from sous_chef.adapters.usda_mcp import UsdaMcpClient, UsdaMcpConfig, UsdaMcpSession, open_usda
from sous_chef.domain.foundry import FoundryFailure
from sous_chef.domain.nutrition import UsdaFoodProfile, UsdaFoodSearch
from sous_chef.domain.nutrition_inputs import MASS, VOLUME, ingredient_weight, recipe_food_query
from sous_chef.domain.recipes import (
    Nutrient,
    NutrientCoverage,
    NutrientName,
    NutritionEvidence,
    NutritionMapping,
    NutritionRequest,
    NutritionUnknownComponent,
    RecipeSource,
    RecipeToolError,
)
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import ErrorCategory, Event, operational_event
from sous_chef.tracing import Operation, observed

_NUTRIENTS: tuple[tuple[str, NutrientName, Literal["kcal", "g", "mg"]], ...] = (
    ("Energy", "energy", "kcal"),
    ("Protein", "protein", "g"),
    ("Carbohydrate, by difference", "carbohydrate", "g"),
    ("Total lipid (fat)", "fat", "g"),
    ("Fiber, total dietary", "fiber", "g"),
    ("Sodium, Na", "sodium", "mg"),
)
MAX_FOODS = 30


def _rounded(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class UsdaNutritionTool:
    def __init__(self, config: UsdaMcpConfig) -> None:
        self.config = config
        self._client: UsdaMcpClient | None = None

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        self._client = UsdaMcpClient(self.config)
        try:
            await self.check(startup=True)
            yield
        finally:
            await self._client.close()
            self._client = None

    @asynccontextmanager
    async def _session(
        self, deadline: float, *, ingredient_count: int = 1
    ) -> AsyncIterator[UsdaMcpSession]:
        timeout = min(30, self.config.timeout_seconds * ((ingredient_count + 8) // 9))
        manager = (
            self._client.session(deadline=deadline, timeout_seconds=timeout)
            if self._client
            else open_usda(self.config, deadline=deadline, timeout_seconds=timeout)
        )
        async with manager as session:
            yield session

    @classmethod
    def from_settings(cls, settings: Settings) -> "UsdaNutritionTool":
        return cls(UsdaMcpConfig.from_settings(settings))

    async def search(self, name: str, *, deadline: float) -> UsdaFoodSearch:
        """Public source candidates for user/agent resolution; never fabricated mappings."""
        try:
            async with self._session(deadline) as session:
                return await session.search(name)
        except RecipeToolError as error:
            operational_event(Event.USDA_FAILED, category=ErrorCategory(error.code.value))
            raise

    async def check(self, *, startup: bool = False) -> None:
        """Check public food data, not just MCP initialization."""
        started = monotonic()
        try:
            if startup:
                assert self._client is not None
                await self._client.start()
            async with self._session(monotonic() + self.config.timeout_seconds) as session:
                candidates = await session.search("Carrots, raw")
                if not candidates.foods:
                    raise RecipeToolError(FoundryFailure.NO_RESULTS)
                await session.profile(candidates.foods[0])
        except RecipeToolError as error:
            operational_event(
                Event.USDA_FAILED,
                category=ErrorCategory(error.code.value),
                duration_ms=(monotonic() - started) * 1000,
            )
            raise ConfigurationError(
                f"USDA startup check failed ({error.code.value}); check ASC_USDA_API_KEY, "
                "FoodData Central availability and API quota."
            ) from None
        operational_event(Event.USDA_COMPLETED, duration_ms=(monotonic() - started) * 1000)

    @observed(Operation.USDA)
    async def lookup(self, request: NutritionRequest, *, deadline: float) -> NutritionEvidence:
        started = monotonic()
        try:
            result = await self._lookup(request, deadline=deadline)
        except ValidationError:
            operational_event(
                Event.USDA_FAILED,
                category=ErrorCategory.FOUNDRY_INVALID_RESULT,
                duration_ms=(monotonic() - started) * 1000,
            )
            raise RecipeToolError(FoundryFailure.INVALID_RESULT) from None
        except RecipeToolError as error:
            operational_event(
                Event.USDA_FAILED,
                category=ErrorCategory(error.code.value),
                duration_ms=(monotonic() - started) * 1000,
            )
            raise
        trace.get_current_span().set_attribute("asc.nutrition.status", result.status)
        operational_event(
            Event.USDA_COMPLETED if result.status == "known" else Event.USDA_UNAVAILABLE,
            category=None if result.status == "known" else ErrorCategory.FOUNDRY_NO_RESULTS,
            duration_ms=(monotonic() - started) * 1000,
        )
        return result

    async def _lookup(self, request: NutritionRequest, *, deadline: float) -> NutritionEvidence:
        profiles: dict[str, UsdaFoodProfile | None] = {}
        sources: dict[str, RecipeSource] = {}
        mappings: list[NutritionMapping] = []
        unknown: list[NutritionUnknownComponent] = []
        totals = {name: Decimal(0) for _, name, _ in _NUTRIENTS}
        covered: dict[NutrientName, list[str]] = {name: [] for _, name, _ in _NUTRIENTS}
        async with self._session(deadline, ingredient_count=len(request.ingredients)) as session:
            for line in request.ingredients:
                if line.quantity is None:
                    unknown.append(
                        NutritionUnknownComponent(
                            ingredient_line_id=line.line_id, reason="missing_quantity"
                        )
                    )
                    continue
                if line.unit not in {*MASS, *VOLUME, "piece", "item"}:
                    unknown.append(
                        NutritionUnknownComponent(
                            ingredient_line_id=line.line_id,
                            reason="missing_unit" if line.unit is None else "unsupported_unit",
                        )
                    )
                    continue
                selected = request.selected_food_ids.get(line.line_id)
                query = (
                    recipe_food_query(line.name)
                    if request.allow_estimates and selected is None
                    else line.name
                )
                key = " ".join(query.casefold().split()) + ":" + (selected or "")
                if key not in profiles:
                    if len(profiles) >= MAX_FOODS:
                        unknown.append(
                            NutritionUnknownComponent(
                                ingredient_line_id=line.line_id, reason="lookup_limit"
                            )
                        )
                        continue
                    try:
                        profiles[key] = await session.food(
                            query, food_id=selected, approximate=request.allow_estimates
                        )
                    except RecipeToolError as error:
                        if error.code != FoundryFailure.NO_RESULTS:
                            raise
                        profiles[key] = None
                profile = profiles[key]
                if profile is None:
                    unknown.append(
                        NutritionUnknownComponent(
                            ingredient_line_id=line.line_id, reason="food_unavailable"
                        )
                    )
                    continue
                assert line.unit is not None
                weight = ingredient_weight(
                    line.name,
                    line.quantity,
                    line.unit,
                    profile,
                    estimates=request.allow_estimates,
                )
                if weight is None:
                    unknown.append(
                        NutritionUnknownComponent(
                            ingredient_line_id=line.line_id, reason="unsupported_unit"
                        )
                    )
                    continue
                grams, conversion_basis = weight
                expected_units = {label: unit for label, _, unit in _NUTRIENTS}
                if any(
                    row.nutrdesc in expected_units
                    and row.units != expected_units[row.nutrdesc]
                    and (row.nutrdesc, row.units) != ("Energy", "kJ")
                    for row in profile.nutrients
                ):
                    raise RecipeToolError(FoundryFailure.INVALID_RESULT)
                values = {
                    row.nutrdesc: row.nutr_val
                    for row in profile.nutrients
                    if any(
                        row.nutrdesc == name and row.units == unit for name, _, unit in _NUTRIENTS
                    )
                }
                missing = tuple(name for label, name, _ in _NUTRIENTS if label not in values)
                if missing:
                    unknown.append(
                        NutritionUnknownComponent(
                            ingredient_line_id=line.line_id,
                            reason="missing_nutrients",
                            unknown_nutrients=missing,
                        )
                    )
                if not values:
                    continue
                for label, name, _ in _NUTRIENTS:
                    if label in values:
                        totals[name] += values[label] * grams / Decimal(100)
                        covered[name].append(line.line_id)
                food_id = profile.food.fdc_id
                source_id = f"usda:fdc:{food_id}"
                sources[source_id] = RecipeSource(
                    source_id=source_id,
                    kind="usda",
                    version=(
                        f"{profile.food.data_type}; published "
                        f"{profile.food.publication_date or 'date unspecified'}"
                    ),
                    reference=f"https://fdc.nal.usda.gov/food-details/{food_id}/nutrients",
                )
                mappings.append(
                    NutritionMapping(
                        ingredient_line_id=line.line_id,
                        quantity=line.quantity,
                        unit=line.unit,
                        source_id=source_id,
                        usda_food_id=food_id,
                        weight_grams=float(grams),
                        food_description=profile.food.long_desc,
                        conversion_basis=conversion_basis,
                        approximate_match=request.allow_estimates and selected is None,
                    )
                )
        nutrients = tuple(
            Nutrient(name=name, amount=_rounded(totals[name] / request.servings), unit=unit)
            for _, name, unit in _NUTRIENTS
            if covered[name]
        )
        coverage = tuple(
            NutrientCoverage(
                name=name,
                status="known"
                if len(covered[name]) == len(request.ingredients)
                else "partial"
                if covered[name]
                else "unavailable",
                whole_recipe_amount=_rounded(totals[name]) if covered[name] else None,
                covered_ingredient_line_ids=tuple(covered[name]),
                unknown_ingredient_line_ids=tuple(
                    line.line_id
                    for line in request.ingredients
                    if line.line_id not in covered[name]
                ),
            )
            for _, name, _ in _NUTRIENTS
        )
        return NutritionEvidence(
            status="known"
            if all(item.status == "known" for item in coverage)
            else "partial"
            if nutrients
            else "unavailable",
            serving_basis="per_serving",
            servings=request.servings,
            nutrients=nutrients,
            mappings=tuple(mappings),
            sources=tuple(sources.values()),
            coverage=coverage,
            unknown_components=tuple(unknown),
        )
