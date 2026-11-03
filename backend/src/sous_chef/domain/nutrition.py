"""The supplied USDA SR MCP contract; nutrient values are per 100 g, not FDC IDs."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NdbNumber = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]{5}$")]
SourceText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)
]


class UsdaModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class UsdaFood(UsdaModel):
    ndb_no: NdbNumber
    long_desc: SourceText = Field(repr=False)
    shrt_desc: SourceText = Field(repr=False)
    food_group: SourceText = Field(repr=False)


class UsdaNutrientRow(UsdaModel):
    long_desc: SourceText = Field(repr=False)
    shrt_desc: SourceText = Field(repr=False)
    food_group: SourceText = Field(repr=False)
    nutrdesc: SourceText
    units: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=20)]
    # asyncpg Decimals are serialized as strings by the supplied server.
    nutr_val: Decimal = Field(ge=0, le=10_000_000, repr=False)
    sr_order: int = Field(strict=True, ge=0, le=100_000)


class UsdaFoodProfile(UsdaModel):
    food: UsdaFood
    basis: Literal["per_100_g"] = "per_100_g"
    nutrients: tuple[UsdaNutrientRow, ...] = Field(min_length=1, max_length=256, repr=False)


class UsdaFoodSearch(UsdaModel):
    foods: tuple[UsdaFood, ...] = Field(max_length=20)
    truncated: bool
