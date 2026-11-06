"""Owned conversation creation and bounded history; no agent execution."""

import hashlib
import json
import secrets
from typing import Literal
from uuid import uuid4

from pydantic import UUID4, AwareDatetime, ConfigDict, Field, ValidationError

from sous_chef.adapters.cosmos import CosmosRepository, open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound
from sous_chef.domain.conversations import (
    ConversationModel,
    ConversationSummary,
    ConversationTurn,
    CreateConversation,
    HistoryPage,
    HistoryParameters,
    IdempotencyConflict,
    Title,
)
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    HistoryQuery,
    NewRecord,
    PageRequest,
    ProductContainer,
    RecordKind,
    StoredRecord,
)
from sous_chef.services.pagination import HistoryPagination

CHAT = ProductContainer.CHAT_HISTORY


class StoredConversation(ConversationModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID4
    conversation_id: UUID4
    record_kind: Literal["conversation"]
    title: Title
    created_at: AwareDatetime
    updated_at: AwareDatetime
    next_turn_index: int = Field(ge=0, strict=True)
    active_operation_id: UUID4 | None


class StoredTurn(ConversationTurn):
    model_config = ConfigDict(extra="ignore")

    id: UUID4
    record_kind: Literal["turn"]


class CreationReceipt(ConversationModel):
    model_config = ConfigDict(extra="ignore")

    record_kind: Literal["conversationCreation"]
    resource_id: UUID4
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)


class ConversationService(HistoryPagination):
    @staticmethod
    def _conversation(record: StoredRecord) -> ConversationSummary:
        try:
            stored = StoredConversation.model_validate_json(json.dumps(record.body))
            if stored.id != stored.conversation_id:
                raise ValueError
            return ConversationSummary(
                conversation_id=stored.id,
                title=stored.title,
                created_at=stored.created_at,
                updated_at=stored.updated_at,
            )
        except (ValidationError, ValueError):
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None

    @staticmethod
    def _turn(record: StoredRecord, conversation_id: UUID4) -> ConversationTurn:
        try:
            stored = StoredTurn.model_validate_json(
                json.dumps({**record.body, "turnId": record.body["id"]})
            )
            if stored.conversation_id != conversation_id:
                raise ValueError
            return ConversationTurn.model_validate(stored.model_dump(exclude={"id", "record_kind"}))
        except (ValidationError, ValueError):
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None

    async def _replay(
        self,
        repository: CosmosRepository,
        user: AuthenticatedUser,
        receipt: StoredRecord,
        request_hash: str,
    ) -> tuple[ConversationSummary, str]:
        try:
            stored = CreationReceipt.model_validate_json(json.dumps(receipt.body))
        except ValidationError:
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None
        if not secrets.compare_digest(stored.request_hash, request_hash):
            raise IdempotencyConflict
        conversation = await repository.read(user, CHAT, str(stored.resource_id))
        if conversation is None:
            raise OwnedResourceNotFound
        return self._conversation(conversation), conversation.etag

    async def create(
        self, user: AuthenticatedUser, request: CreateConversation, key: str
    ) -> tuple[ConversationSummary, str]:
        receipt_id = (
            "conversation-create:"
            + hashlib.sha256((user.user_id + "\0" + key).encode("utf-8")).hexdigest()
        )
        request_hash = hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()
        async with open_cosmos(self.settings) as repository:
            receipt = await repository.read(user, CHAT, receipt_id)
            if receipt is not None:
                return await self._replay(repository, user, receipt, request_hash)
            conversation_id = uuid4()
            request_record = NewRecord(
                kind=RecordKind.CONVERSATION,
                record_id=conversation_id,
                fields={
                    "conversationId": str(conversation_id),
                    "title": request.title,
                    "nextTurnIndex": 0,
                    "activeOperationId": None,
                },
            )
            try:
                record = await repository.create_conversation(
                    user, request_record, receipt_id=receipt_id, request_hash=request_hash
                )
            except CosmosError as error:
                if error.code != CosmosFailure.EXISTS:
                    raise
                # An atomic competing creation may have won; never create another conversation.
                receipt = await repository.read(user, CHAT, receipt_id)
                if receipt is None:
                    raise
                return await self._replay(repository, user, receipt, request_hash)
            return self._conversation(record), record.etag

    async def list(
        self, user: AuthenticatedUser, parameters: HistoryParameters
    ) -> HistoryPage[ConversationSummary]:
        state, issued_at = self._page(user, parameters, "conversation", None)
        async with open_cosmos(self.settings) as repository:
            page = await repository.query(
                user,
                HistoryQuery(kind="conversation", created_before=state.created_before),
                PageRequest(limit=state.limit, continuation_token=state.continuation_token),
            )
            return HistoryPage(
                items=tuple(self._conversation(record) for record in page.items),
                next_cursor=self._cursor(state, page.continuation_token, issued_at),
            )

    async def turns(
        self, user: AuthenticatedUser, conversation_id: UUID4, parameters: HistoryParameters
    ) -> HistoryPage[ConversationTurn]:
        state, issued_at = self._page(user, parameters, "turn", conversation_id)
        async with open_cosmos(self.settings) as repository:
            conversation = await repository.read(user, CHAT, str(conversation_id))
            if conversation is None:
                raise OwnedResourceNotFound
            self._conversation(conversation)
            page = await repository.query(
                user,
                HistoryQuery(
                    kind="turn",
                    conversation_id=conversation_id,
                    created_before=state.created_before,
                ),
                PageRequest(limit=state.limit, continuation_token=state.continuation_token),
            )
            return HistoryPage(
                items=tuple(self._turn(record, conversation_id) for record in page.items),
                next_cursor=self._cursor(state, page.continuation_token, issued_at),
            )
