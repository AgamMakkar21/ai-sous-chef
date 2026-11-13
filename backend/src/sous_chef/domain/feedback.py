"""Explicit feedback input and immutable asynchronous learning events."""

from typing import Literal

from pydantic import UUID4, AwareDatetime, Field

from sous_chef.domain.conversations import ConversationModel
from sous_chef.domain.recipe_lifecycle import LifecycleModel, Rating, Reason


class SubmitFeedback(ConversationModel):
    rating: Rating
    note: Reason | None = Field(default=None, repr=False)


class FeedbackRevision(LifecycleModel):
    feedback_id: UUID4
    source_id: UUID4
    history_id: UUID4
    occasion_id: UUID4
    generated_recipe_id: UUID4
    source_conversation_id: UUID4
    source_turn_id: UUID4
    feedback_revision: int = Field(ge=1, strict=True)
    supersedes_feedback_id: UUID4 | None
    rating: Rating
    note: Reason | None = Field(repr=False)
    provenance: Literal["explicit_user_action"] = "explicit_user_action"
    processing_status: Literal["pending"] = "pending"
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$", repr=False)
    trace_parent: str | None = Field(default=None, repr=False)
    observed_at: AwareDatetime


class FeedbackRequiresCooking(ValueError):
    """Only an explicitly cooked history snapshot can receive feedback."""
