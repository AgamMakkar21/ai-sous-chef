"""Authenticated, owned history cursors with a fixed traversal lifetime."""

from datetime import UTC, datetime
from time import time
from typing import Literal
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from pydantic import UUID4, AwareDatetime, Field, ValidationError

from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.conversations import (
    MAX_CURSOR_LENGTH,
    ConversationModel,
    HistoryParameters,
    InvalidCursor,
)
from sous_chef.domain.cosmos import MAX_PAGE_SIZE, CosmosError, CosmosFailure, PageRequest
from sous_chef.settings import ConfigurationError, Settings

HistoryScope = Literal["conversation", "turn", "recipe", "recipe-pending", "recipe-rated"]


class CursorState(ConversationModel):
    version: Literal[1] = 1
    user_id: str = Field(repr=False)
    kind: HistoryScope
    conversation_id: UUID4 | None
    limit: int = Field(ge=1, le=MAX_PAGE_SIZE, strict=True)
    created_before: AwareDatetime
    continuation_token: str | None = Field(default=None, repr=False)


class HistoryPagination:
    def __init__(self, settings: Settings) -> None:
        if settings.pagination_key is None:
            raise ConfigurationError("Set ASC_PAGINATION_KEY before using history APIs.")
        self.settings = settings
        self.cipher = Fernet(settings.pagination_key.get_secret_value().encode("ascii"))

    def _page(
        self,
        user: AuthenticatedUser,
        parameters: HistoryParameters,
        kind: HistoryScope,
        conversation_id: UUID4 | None,
    ) -> tuple[CursorState, int]:
        now = int(time())
        if parameters.cursor is None:
            return (
                CursorState(
                    user_id=user.user_id,
                    kind=kind,
                    conversation_id=conversation_id,
                    limit=(
                        parameters.limit
                        if parameters.limit is not None
                        else self.settings.cosmos_page_size
                    ),
                    created_before=datetime.now(UTC),
                ),
                now,
            )
        try:
            token = parameters.cursor.encode("ascii")
            state = CursorState.model_validate_json(
                self.cipher.decrypt_at_time(token, ttl=86400, current_time=now)
            )
            if (
                state.user_id != user.user_id
                or state.kind != kind
                or state.conversation_id != conversation_id
                or (parameters.limit is not None and parameters.limit != state.limit)
            ):
                raise InvalidCursor
            PageRequest(continuation_token=state.continuation_token)
            if state.continuation_token is None:
                raise InvalidCursor
            return state, self.cipher.extract_timestamp(token)
        except (InvalidToken, UnicodeError, ValidationError):
            raise InvalidCursor from None

    def _cursor(self, state: CursorState, token: str | None, issued_at: int) -> str | None:
        if token is None:
            return None
        next_state = state.model_copy(update={"continuation_token": token})
        cursor = self.cipher.encrypt_at_time(
            next_state.model_dump_json(by_alias=True).encode("utf-8"), current_time=issued_at
        ).decode("ascii")
        if len(cursor) > MAX_CURSOR_LENGTH:
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
        return cursor
