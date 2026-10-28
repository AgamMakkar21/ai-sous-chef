"""Explicit inventory inputs and views; quantities are never inferred."""

from decimal import Decimal
from typing import Annotated, Literal, Self
from unicodedata import category
from uuid import UUID

from pydantic import (
    UUID4,
    AfterValidator,
    AwareDatetime,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import PydanticCustomError

from sous_chef.domain.profiles import ProfileModel

MAX_GROCERIES = 200
GROCERY_FIELDS = frozenset({"name", "quantity", "unit"})
GroceryUnit = Literal[
    "g",
    "kg",
    "ml",
    "l",
    "tsp",
    "tbsp",
    "cup",
    "oz",
    "lb",
    "item",
    "piece",
    "bunch",
    "can",
    "package",
]


def _name(value: str) -> str:
    if any(category(character) in {"Cc", "Cs"} for character in value):
        raise PydanticCustomError("invalid_name", "Use a name without control characters.")
    return value


def _number(value: object) -> object:
    if type(value) not in {int, float, Decimal}:
        raise PydanticCustomError("quantity_type", "Quantity must be a JSON number.")
    return value


def _precision(value: Decimal) -> Decimal:
    parts = value.as_tuple()
    if isinstance(parts.exponent, int):
        # normalize() can round long inputs before their precision is checked.
        excess = min(max(-parts.exponent - 3, 0), len(parts.digits))
        if excess and any(parts.digits[-excess:]):
            raise PydanticCustomError(
                "decimal_max_places", "Quantity must have at most three fractional digits."
            )
    return value


GroceryName = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100),
    AfterValidator(_name),
]
Quantity = Annotated[
    Decimal,
    BeforeValidator(_number, json_schema_input_type=float),
    AfterValidator(_precision),
    Field(gt=0, le=1_000_000, allow_inf_nan=False, json_schema_extra={"multipleOf": 0.001}),
]


class GroceryModel(ProfileModel):
    @field_serializer("quantity", check_fields=False, when_used="json")
    def serialize_quantity(self, value: Decimal | None) -> float | None:
        return float(value) if value is not None else None


class CreateGrocery(GroceryModel):
    model_config = ConfigDict(populate_by_name=False, validate_by_name=False)

    name: GroceryName
    quantity: Quantity | None = None
    unit: GroceryUnit | None = None


class UpdateGrocery(GroceryModel):
    model_config = ConfigDict(
        populate_by_name=False, validate_by_name=False, json_schema_extra={"minProperties": 1}
    )

    name: GroceryName | SkipJsonSchema[None] = None
    quantity: Quantity | None = None
    unit: GroceryUnit | None = None

    @field_validator("name")
    @classmethod
    def no_null_name(cls, value: str | None) -> str:
        if value is None:
            raise PydanticCustomError("null_not_allowed", "Omit the name instead of sending null.")
        return value

    @model_validator(mode="after")
    def nonempty_update(self) -> Self:
        if not self.model_fields_set:
            raise PydanticCustomError("empty_update", "Supply at least one grocery field.")
        return self


class GroceryRow(GroceryModel):
    grocery_id: UUID4
    name: GroceryName
    quantity: Quantity | None
    unit: GroceryUnit | None
    ingredient_id: (
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)] | None
    ) = None
    updated_at: AwareDatetime


class GroceryEnvelope(ProfileModel):
    data: GroceryRow


class GroceryList(ProfileModel):
    items: tuple[GroceryRow, ...] = Field(max_length=MAX_GROCERIES)
    next_cursor: None = None


class DuplicateGrocery(ValueError):
    def __init__(self, grocery_id: UUID) -> None:
        self.grocery_id = grocery_id
        super().__init__("A grocery with this name and unit already exists.")


class GroceryLimitReached(ValueError):
    """The bounded inventory has no room for another row."""
