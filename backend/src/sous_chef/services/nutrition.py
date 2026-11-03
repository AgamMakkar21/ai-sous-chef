"""Deterministic recipe estimates from attributed USDA SR per-100-g evidence."""

from decimal import Decimal
from time import monotonic

from pydantic import ValidationError

from sous_chef.adapters.usda_mcp import UsdaMcpConfig, open_usda
from sous_chef.domain.foundry import FoundryFailure
from sous_chef.domain.nutrition import UsdaFoodProfile, UsdaFoodSearch
from sous_chef.domain.recipes import (
    Nutrient,
    NutritionEvidence,
    NutritionMapping,
    NutritionRequest,
    RecipeSource,
    RecipeToolError,
)
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import ErrorCategory, Event, operational_event

_MASS = {
    "g": Decimal(1),
    "kg": Decimal(1000),
    "oz": Decimal("28.349523125"),
    "lb": Decimal("453.59237"),
}
_NUTRIENTS = (
    ("Energy", "energy", "kcal"),
    ("Protein", "protein", "g"),
    ("Carbohydrate, by difference", "carbohydrate", "g"),
    ("Total lipid (fat)", "fat", "g"),
    ("Fiber, total dietary", "fiber", "g"),
    ("Sodium, Na", "sodium", "mg"),
)
MAX_FOODS = 9


class UsdaNutritionTool:
    def __init__(self, config: UsdaMcpConfig) -> None:
        self.config = config

    @classmethod
    def from_settings(cls, settings: Settings) -> "UsdaNutritionTool":
        return cls(UsdaMcpConfig.from_settings(settings))

    async def search(self, name: str, *, deadline: float) -> UsdaFoodSearch:
        """Public source candidates for user/agent resolution; never fabricated mappings."""
        try:
            async with open_usda(self.config, deadline=deadline) as session:
                return await session.search(name)
        except RecipeToolError as error:
            operational_event(Event.USDA_FAILED, category=ErrorCategory(error.code.value))
            raise

    async def check(self) -> None:
        """Check public food data, not just MCP initialization."""
        request = NutritionRequest.model_validate(
            {
                "servings": 1,
                "ingredients": (
                    {"line_id": "startup", "name": "Carrots, raw", "quantity": 100, "unit": "g"},
                ),
            }
        )
        try:
            evidence = await self.lookup(
                request, deadline=monotonic() + self.config.timeout_seconds
            )
        except RecipeToolError as error:
            raise ConfigurationError(
                f"USDA startup check failed ({error.code.value}); check protected database "
                "configuration, TLS, firewall access and the pinned dependency."
            ) from None
        if evidence.status == "unavailable":
            raise ConfigurationError(
                "USDA startup check found no nutrition for Carrots, raw; "
                "check the imported SR data."
            )

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
        totals: dict[str, Decimal] | None = None
        async with open_usda(self.config, deadline=deadline) as session:
            for line in request.ingredients:
                if line.quantity is None or line.unit not in _MASS:
                    continue
                selected = request.selected_food_ids.get(line.line_id)
                key = " ".join(line.name.casefold().split()) + ":" + (selected or "")
                if key not in profiles:
                    if len(profiles) >= MAX_FOODS:
                        continue
                    try:
                        profiles[key] = await session.food(line.name, food_id=selected)
                    except RecipeToolError as error:
                        if error.code != FoundryFailure.NO_RESULTS:
                            raise
                        profiles[key] = None
                profile = profiles[key]
                if profile is None:
                    continue
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
                if not values:
                    continue
                factor = line.quantity * _MASS[line.unit] / Decimal(100) / request.servings
                amounts = {name: amount * factor for name, amount in values.items()}
                totals = (
                    amounts
                    if totals is None
                    else {
                        name: amount + amounts[name]
                        for name, amount in totals.items()
                        if name in amounts
                    }
                )
                food_id = profile.food.ndb_no
                source_id = f"usda:sr:{food_id}"
                sources[source_id] = RecipeSource(
                    source_id=source_id,
                    kind="usda",
                    version="SR (release unspecified)",
                    reference="https://www.ars.usda.gov/Services/docs.htm?docid=8964",
                )
                mappings.append(
                    NutritionMapping(
                        ingredient_line_id=line.line_id,
                        quantity=line.quantity,
                        unit=line.unit,
                        source_id=source_id,
                        usda_food_id=food_id,
                    )
                )
        nutrients = tuple(
            Nutrient.model_validate({"name": name, "amount": float(totals[label]), "unit": unit})
            for label, name, unit in _NUTRIENTS
            if totals and label in totals
        )
        if not nutrients:
            return NutritionEvidence(
                status="unavailable",
                serving_basis="per_serving",
                servings=request.servings,
                nutrients=(),
                mappings=(),
                sources=(),
            )
        return NutritionEvidence(
            status="known"
            if len(mappings) == len(request.ingredients) and len(nutrients) == len(_NUTRIENTS)
            else "partial",
            serving_basis="per_serving",
            servings=request.servings,
            nutrients=nutrients,
            mappings=tuple(mappings),
            sources=tuple(sources.values()),
        )
