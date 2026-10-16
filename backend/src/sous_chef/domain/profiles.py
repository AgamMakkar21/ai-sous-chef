"""Explicit profile inputs and public views; learned state is not client-writable."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import PydanticCustomError

from sous_chef.domain.food_safety import supports_restriction


class ProfileModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
    )

    @field_serializer("created_at", "updated_at", check_fields=False, when_used="json")
    def serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


PreferenceText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100)
]


def _allergy(value: str) -> str:
    if not supports_restriction(value, allergy=True):
        raise PydanticCustomError(
            "unsupported_safety_constraint", "This safety constraint is not supported."
        )
    return value


def _restriction(value: str) -> str:
    if not supports_restriction(value):
        raise PydanticCustomError(
            "unsupported_safety_constraint", "This safety constraint is not supported."
        )
    return value


Allergies = Annotated[
    tuple[Annotated[PreferenceText, AfterValidator(_allergy)], ...], Field(max_length=50)
]
Restrictions = Annotated[
    tuple[Annotated[PreferenceText, AfterValidator(_restriction)], ...], Field(max_length=20)
]
Tastes = Annotated[tuple[PreferenceText, ...], Field(max_length=50)]
ShortSelections = Annotated[tuple[PreferenceText, ...], Field(max_length=20)]
Spice = Literal["none", "mild", "medium", "hot"]
Skill = Literal["beginner", "intermediate", "advanced"]
TimeBudget = Annotated[int, Field(ge=5, le=240, strict=True)]


class ExplicitPreferences(ProfileModel):
    model_config = ConfigDict(populate_by_name=False, validate_by_name=False)

    allergies: Allergies
    dietary_restrictions: Restrictions
    likes: Tastes
    dislikes: Tastes
    preferred_cuisines: ShortSelections
    spice_preference: Spice
    cooking_skill: Skill
    typical_time_budget_minutes: TimeBudget
    nutrition_goals: ShortSelections


PREFERENCE_FIELDS = frozenset(to_camel(name) for name in ExplicitPreferences.model_fields)


class PreferenceUpdate(ProfileModel):
    model_config = ConfigDict(
        populate_by_name=False, validate_by_name=False, json_schema_extra={"minProperties": 1}
    )

    # None represents omission internally, not an accepted JSON null.
    allergies: Allergies | SkipJsonSchema[None] = None
    dietary_restrictions: Restrictions | SkipJsonSchema[None] = None
    likes: Tastes | SkipJsonSchema[None] = None
    dislikes: Tastes | SkipJsonSchema[None] = None
    preferred_cuisines: ShortSelections | SkipJsonSchema[None] = None
    spice_preference: Spice | SkipJsonSchema[None] = None
    cooking_skill: Skill | SkipJsonSchema[None] = None
    typical_time_budget_minutes: TimeBudget | SkipJsonSchema[None] = None
    nutrition_goals: ShortSelections | SkipJsonSchema[None] = None

    @field_validator("*")
    @classmethod
    def no_explicit_null(cls, value: object) -> object:
        if value is None:
            raise PydanticCustomError("null_not_allowed", "Omit a field instead of sending null.")
        return value

    @model_validator(mode="after")
    def nonempty_update(self) -> Self:
        if not self.model_fields_set:
            raise PydanticCustomError("empty_update", "Supply at least one explicit preference.")
        return self


class ProfileView(ProfileModel):
    user_id: str
    onboarding_completed: bool = Field(strict=True)
    explicit_preferences: ExplicitPreferences | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def complete_state(self) -> Self:
        if self.onboarding_completed and self.explicit_preferences is None:
            raise ValueError("A completed profile requires complete explicit preferences.")
        return self


class ProfileEnvelope(ProfileModel):
    data: ProfileView


class OnboardingRequired(ValueError):
    """The user must explicitly complete onboarding before this operation."""


def require_onboarding(profile: ProfileView) -> ExplicitPreferences:
    if not profile.onboarding_completed or profile.explicit_preferences is None:
        raise OnboardingRequired
    return profile.explicit_preferences
