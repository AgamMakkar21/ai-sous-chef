"""Explicit recipe intent and cooking contracts; never inferred feedback."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    UUID4,
    AwareDatetime,
    Field,
    StringConstraints,
    computed_field,
    field_serializer,
    model_validator,
)

from sous_chef.domain.conversations import ConversationModel
from sous_chef.domain.recipes import StructuredRecipe

Reason = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2000)
]
BehaviorKind = Literal["saved", "rejected", "revisionRequested"]


class SaveRecipe(ConversationModel):
    model_config = {"populate_by_name": False}

    generated_recipe_id: UUID4
    cooked: bool = Field(default=False, strict=True)


class ConfirmCooking(ConversationModel):
    pass


class RejectRecipe(ConversationModel):
    reason: Reason | None = Field(default=None, repr=False)


class ReviseRecipe(ConversationModel):
    reason: Reason = Field(repr=False)


class LifecycleModel(ConversationModel):
    @field_serializer("saved_at", "cooked_at", "observed_at", check_fields=False, when_used="json")
    def lifecycle_timestamp(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class RecipeHistory(LifecycleModel):
    history_id: UUID4
    generated_recipe_id: UUID4
    occasion_id: UUID4
    supersedes_recipe_id: UUID4 | None
    source_conversation_id: UUID4
    source_turn_id: UUID4
    recipe: StructuredRecipe = Field(repr=False)
    accepted: Literal[True] = True
    saved_at: AwareDatetime
    cooked_at: AwareDatetime | None
    rating: Literal["liked", "fine", "disliked"] | None = None
    note: Reason | None = Field(default=None, repr=False)
    feedback_revision: int = Field(default=0, ge=0, strict=True)
    processing_status: Literal["notEligible", "pending", "processing", "completed", "failed"]

    @model_validator(mode="after")
    def truthful_state(self) -> "RecipeHistory":
        if (
            self.cooked_at is not None
            and self.cooked_at < self.saved_at
            or (self.rating is None) != (self.feedback_revision == 0)
            or self.cooked_at is None
            and (self.rating is not None or self.note is not None)
        ):
            raise ValueError("Feedback requires a truthful cooked state and semantic revision.")
        return self

    @computed_field
    def state(self) -> Literal["planned", "cooked"]:
        return "planned" if self.cooked_at is None else "cooked"

    @computed_field
    def awaiting_feedback(self) -> bool:
        return self.cooked_at is not None and self.rating is None


class RecipeBehavior(LifecycleModel):
    behavior_id: UUID4
    kind: BehaviorKind
    source_id: UUID4
    generated_recipe_id: UUID4
    occasion_id: UUID4
    source_conversation_id: UUID4
    source_turn_id: UUID4
    history_id: UUID4 | None = None
    reason: Reason | None = Field(default=None, repr=False)
    behavior_revision: Literal[1] = 1
    provenance: Literal["explicit_user_action"] = "explicit_user_action"
    observed_at: AwareDatetime


class RecipeEnvelope(ConversationModel):
    data: RecipeHistory


class BehaviorEnvelope(ConversationModel):
    data: RecipeBehavior


class RecipeLifecycleConflict(ValueError):
    """An existing snapshot needs a conditional cooking transition."""
