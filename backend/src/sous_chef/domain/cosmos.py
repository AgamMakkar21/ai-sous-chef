"""Internal storage contracts; these are not browser request/response schemas."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import UUID4, AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

MAX_RECORD_BYTES = 256 * 1024
MAX_PAGE_SIZE = 50
MAX_CONTINUATION_LENGTH = 16 * 1024
RESERVED_FIELDS = frozenset(
    {"id", "userId", "recordKind", "schemaVersion", "createdAt", "updatedAt", "ttl"}
)


class ProductContainer(StrEnum):
    USERS = "Users"
    CHAT_HISTORY = "ChatHistory"
    RECIPE_HISTORY = "RecipeHistory"


class RecordKind(StrEnum):
    PROFILE = "profile"
    CONVERSATION = "conversation"
    CONVERSATION_CREATION = "conversationCreation"
    TURN = "turn"
    SEND_OPERATION = "sendOperation"
    RECIPE = "recipe"
    BEHAVIOR = "behavior"
    FEEDBACK_REVISION = "feedbackRevision"
    PROCESSING = "processing"


class StorageInput(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )


class NewRecord(StorageInput):
    kind: RecordKind
    fields: dict[str, JsonValue] = Field(repr=False)
    record_id: UUID4 | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        validate_field_names(self.fields)
        if self.kind == RecordKind.PROFILE and self.record_id is not None:
            raise ValueError("A profile ID is derived from the authenticated user.")
        return self


class RecordRevision(StorageInput):
    etag: str = Field(min_length=1, max_length=1024, pattern=r"^[^\x00-\x20\x7f]+$", repr=False)


class RecordPatch(RecordRevision):
    fields: dict[str, JsonValue] = Field(min_length=1, max_length=9, repr=False)

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        validate_field_names(self.fields)
        return self


def validate_field_names(fields: dict[str, JsonValue]) -> None:
    if any(
        name in RESERVED_FIELDS
        or not name
        or name.startswith("_")
        or "/" in name
        or "~" in name
        or any(ord(character) < 32 for character in name)
        for name in fields
    ):
        raise ValueError("Storage fields cannot replace metadata or contain JSON-pointer paths.")


class HistoryQuery(StorageInput):
    kind: Literal["conversation", "turn", "recipe"]
    conversation_id: UUID4 | None = None
    created_before: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC), repr=False)

    @model_validator(mode="after")
    def validate_conversation(self) -> Self:
        if (self.kind == "turn") != (self.conversation_id is not None):
            raise ValueError("Only a turn query requires a conversation ID.")
        return self


class PageRequest(StorageInput):
    limit: int | None = Field(default=None, ge=1, le=MAX_PAGE_SIZE)
    continuation_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CONTINUATION_LENGTH,
        pattern=r"^[^\r\n\x00]+$",
        repr=False,
    )


@dataclass(frozen=True)
class StoredRecord:
    body: dict[str, JsonValue] = field(repr=False)
    etag: str = field(repr=False)


@dataclass(frozen=True)
class RecordPage:
    items: tuple[StoredRecord, ...] = field(repr=False)
    continuation_token: str | None = field(repr=False)


class CosmosFailure(StrEnum):
    AUTHENTICATION = "cosmos_authentication"
    FORBIDDEN = "cosmos_forbidden"
    THROTTLED = "cosmos_throttled"
    TIMEOUT = "cosmos_timeout"
    UNAVAILABLE = "cosmos_unavailable"
    INVALID_RESULT = "cosmos_invalid_result"
    REJECTED = "cosmos_rejected"
    CONFLICT = "revision_conflict"
    EXISTS = "duplicate_resource"
    TOO_LARGE = "record_too_large"


class CosmosError(RuntimeError):
    """Safe metadata only; an uncertain write must be reconciled, not replayed."""

    def __init__(
        self,
        code: CosmosFailure,
        operation_id: UUID,
        *,
        retry_after_seconds: float | None = None,
        outcome_unknown: bool = False,
    ) -> None:
        self.code = code
        self.operation_id = operation_id
        self.retry_after_seconds = retry_after_seconds
        self.outcome_unknown = outcome_unknown
        super().__init__(f"{code.value}; operation_id={operation_id}.")
