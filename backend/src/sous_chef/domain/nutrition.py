"""Validated FoodData Central MCP evidence; nutrient values are per 100 g."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

FdcId = Annotated[str, StringConstraints(strict=True, pattern=r"^[1-9][0-9]{0,9}$")]
SourceText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)
]


class UsdaModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class UsdaFood(UsdaModel):
    fdc_id: FdcId
    long_desc: SourceText = Field(repr=False)
    shrt_desc: SourceText = Field(repr=False)
    food_group: SourceText = Field(repr=False)
    data_type: Literal["Foundation", "SR Legacy"]
    publication_date: SourceText | None = None


class UsdaNutrientRow(UsdaModel):
    long_desc: SourceText = Field(repr=False)
    shrt_desc: SourceText = Field(repr=False)
    food_group: SourceText = Field(repr=False)
    nutrdesc: SourceText
    units: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=20)]
    # MCP serializes exact source decimals as strings.
    nutr_val: Decimal = Field(ge=0, le=10_000_000, repr=False)
    sr_order: int = Field(strict=True, ge=0, le=100_000)


class UsdaFoodPortion(UsdaModel):
    amount: Decimal = Field(gt=0, le=1_000_000)
    gram_weight: Decimal = Field(gt=0, le=10_000_000)
    measure_unit: SourceText
    modifier: SourceText | None = None


class UsdaFoodProfile(UsdaModel):
    food: UsdaFood
    basis: Literal["per_100_g"] = "per_100_g"
    nutrients: tuple[UsdaNutrientRow, ...] = Field(min_length=1, max_length=256, repr=False)
    portions: tuple[UsdaFoodPortion, ...] = Field(default=(), max_length=128)


class UsdaFoodSearch(UsdaModel):
    foods: tuple[UsdaFood, ...] = Field(max_length=20)
    truncated: bool
