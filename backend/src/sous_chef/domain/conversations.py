"""Conversation input and public history contracts, separate from storage metadata."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    UUID4,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel

from sous_chef.domain.cosmos import MAX_PAGE_SIZE

MAX_CURSOR_LENGTH = 24 * 1024


class ConversationModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
    )

    @field_serializer(
        "created_at",
        "updated_at",
        "timestamp",
        "created_before",
        check_fields=False,
        when_used="json",
    )
    def serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


Title = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100)
]


class CreateConversation(ConversationModel):
    model_config = ConfigDict(populate_by_name=False)

    title: Title = "New conversation"


class HistoryParameters(ConversationModel):
    model_config = ConfigDict(populate_by_name=False)

    limit: int | None = Field(default=None, ge=1, le=MAX_PAGE_SIZE)
    cursor: str | None = Field(default=None, max_length=MAX_CURSOR_LENGTH, repr=False)


class ConversationSummary(ConversationModel):
    conversation_id: UUID4
    title: Title
    created_at: AwareDatetime
    updated_at: AwareDatetime


class TurnMessage(ConversationModel):
    message_id: UUID4
    role: Literal["user", "assistant"]
    entity_id: str = Field(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$", repr=False)
    content: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=4000)
    ] = Field(repr=False)
    timestamp: AwareDatetime


class ConversationTurn(ConversationModel):
    turn_id: UUID4
    conversation_id: UUID4
    turn_index: int = Field(ge=0, strict=True)
    status: Literal["pending", "completed", "failed", "outcomeUnknown"]
    messages: tuple[TurnMessage, ...] = Field(min_length=1, max_length=2, repr=False)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_exchange(self) -> Self:
        roles = tuple(message.role for message in self.messages)
        expected = ("user", "assistant") if self.status == "completed" else ("user",)
        if roles != expected:
            raise ValueError("A turn must contain only its public exchange in status order.")
        return self


class ConversationEnvelope(ConversationModel):
    data: ConversationSummary


class HistoryPage[T](ConversationModel):
    items: tuple[T, ...]
    next_cursor: str | None = Field(repr=False)


class InvalidCursor(ValueError):
    """An invalid, expired or incorrectly scoped public cursor."""


class IdempotencyConflict(ValueError):
    """One creation key was reused with a different canonical request."""
