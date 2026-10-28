"""Durable chat admission and completion; never replay an uncertain invocation."""

import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

from pydantic import UUID4, AwareDatetime, ConfigDict, Field, JsonValue, ValidationError

from sous_chef.adapters.cosmos import CosmosRepository, open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound
from sous_chef.domain.chat import (
    ChatExecution,
    ChatExecutor,
    ExecutionFailed,
    OperationView,
    SendConflict,
    SendMessage,
    TurnStatus,
    ValidatedReply,
)
from sous_chef.domain.conversations import ConversationModel, IdempotencyConflict, TurnMessage
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    NewRecord,
    ProductContainer,
    RecordKind,
    RecordPatch,
    StoredRecord,
)
from sous_chef.domain.profiles import OnboardingRequired, require_onboarding
from sous_chef.services.conversations import ConversationService, StoredConversation
from sous_chef.services.profiles import _view as profile_view
from sous_chef.settings import ConfigurationError, Settings

CHAT = ProductContainer.CHAT_HISTORY
SEND_TIMEOUT_SECONDS = 60
FailureCode = Literal["execution_failed", "invalid_assistant_response", "execution_outcome_unknown"]


class StoredOperation(ConversationModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID4
    record_kind: Literal["sendOperation"]
    conversation_id: UUID4
    turn_id: UUID4
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    status: TurnStatus
    expires_at: AwareDatetime
    failure_code: FailureCode | None = None


def _operation(record: StoredRecord, conversation_id: UUID4) -> StoredOperation:
    try:
        operation = StoredOperation.model_validate_json(json.dumps(record.body))
        if operation.conversation_id != conversation_id:
            raise ValueError
        return operation
    except (ValidationError, ValueError):
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None


async def _owned(
    repository: CosmosRepository, user: AuthenticatedUser, conversation_id: UUID4
) -> StoredRecord:
    record = await repository.read(user, CHAT, str(conversation_id))
    if record is None:
        raise OwnedResourceNotFound
    ConversationService._conversation(record)
    return record


async def _turn(
    repository: CosmosRepository, user: AuthenticatedUser, operation: StoredOperation
) -> StoredRecord:
    record = await repository.read(user, CHAT, str(operation.turn_id))
    if record is None:
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
    ConversationService._turn(record, operation.conversation_id)
    return record


def _view(record: StoredRecord, turn: StoredRecord, conversation_id: UUID4) -> OperationView:
    operation = _operation(record, conversation_id)
    public_turn = ConversationService._turn(turn, conversation_id)
    current = turn.body.get("currentOperationId") == str(operation.id)
    if (
        public_turn.turn_id != operation.turn_id
        or (current and public_turn.status != operation.status)
        or (not current and operation.status != "failed")
        or ((operation.failure_code is None) != (operation.status in {"pending", "completed"}))
    ):
        raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
    return OperationView(
        operation_id=operation.id,
        conversation_id=operation.conversation_id,
        turn_id=operation.turn_id,
        status=operation.status,
        failure_code=operation.failure_code,
        turn=public_turn,
    )


class ChatService:
    def __init__(self, settings: Settings, executor: ChatExecutor | None = None) -> None:
        self.settings = settings
        self.executor = executor

    async def _finish(
        self,
        repository: CosmosRepository,
        user: AuthenticatedUser,
        operation_record: StoredRecord,
        conversation_id: UUID4,
        *,
        status: TurnStatus,
        reply: ValidatedReply | None = None,
        failure: FailureCode | None = None,
    ) -> OperationView:
        operation = _operation(operation_record, conversation_id)
        conversation = await _owned(repository, user, operation.conversation_id)
        turn = await _turn(repository, user, operation)
        if operation.status != "pending":
            return _view(operation_record, turn, operation.conversation_id)
        if conversation.body.get("activeOperationId") != str(operation.id) or turn.body.get(
            "currentOperationId"
        ) != str(operation.id):
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
        public_turn = ConversationService._turn(turn, operation.conversation_id)
        messages = public_turn.messages
        if reply is not None:
            messages += (
                TurnMessage(
                    message_id=uuid4(),
                    role="assistant",
                    entity_id=reply.entity_id,
                    content=reply.content,
                    timestamp=datetime.now(UTC),
                ),
            )
        turn_fields: dict[str, JsonValue] = {
            "status": status,
            "messages": [message.model_dump(mode="json", by_alias=True) for message in messages],
            "generatedRecipeId": str(reply.generated_recipe_id)
            if reply and reply.generated_recipe_id
            else None,
        }
        try:
            records = await repository.commit_chat(
                user,
                patches=(
                    (
                        conversation,
                        RecordPatch(
                            etag=conversation.etag,
                            fields={
                                "activeOperationId": str(operation.id)
                                if status == "outcomeUnknown"
                                else None
                            },
                        ),
                    ),
                    (turn, RecordPatch(etag=turn.etag, fields=turn_fields)),
                    (
                        operation_record,
                        RecordPatch(
                            etag=operation_record.etag,
                            fields={
                                "status": status,
                                "failureCode": failure,
                                "providerResponseId": reply.provider_response_id if reply else None,
                            },
                        ),
                    ),
                ),
            )
        except CosmosError as error:
            if error.code != CosmosFailure.CONFLICT and not error.outcome_unknown:
                raise
            # A committed batch may have lost its response, or a deadline reconciler won.
            saved = await repository.read(user, CHAT, str(operation.id))
            if (
                saved is not None
                and _operation(saved, operation.conversation_id).status != "pending"
            ):
                return _view(
                    saved,
                    await _turn(repository, user, operation),
                    operation.conversation_id,
                )
            raise
        return _view(records[2], records[1], operation.conversation_id)

    async def _existing(
        self,
        repository: CosmosRepository,
        user: AuthenticatedUser,
        record: StoredRecord,
        conversation_id: UUID4,
        request_hash: str | None = None,
    ) -> OperationView:
        operation = _operation(record, conversation_id)
        if request_hash is not None and not secrets.compare_digest(
            operation.request_hash, request_hash
        ):
            raise IdempotencyConflict
        if operation.status == "pending" and datetime.now(UTC) >= operation.expires_at:
            return await self._finish(
                repository,
                user,
                record,
                conversation_id,
                status="outcomeUnknown",
                failure="execution_outcome_unknown",
            )
        return _view(record, await _turn(repository, user, operation), conversation_id)

    async def inspect(
        self, user: AuthenticatedUser, conversation_id: UUID4, operation_id: UUID4
    ) -> OperationView:
        async with open_cosmos(self.settings) as repository:
            await _owned(repository, user, conversation_id)
            record = await repository.read(user, CHAT, str(operation_id))
            if record is None or record.body.get("conversationId") != str(conversation_id):
                raise OwnedResourceNotFound
            return await self._existing(repository, user, record, conversation_id)

    async def send(
        self,
        user: AuthenticatedUser,
        conversation_id: UUID4,
        request: SendMessage,
        key: str,
    ) -> OperationView:
        deadline = monotonic() + SEND_TIMEOUT_SECONDS
        expires_at = datetime.now(UTC) + timedelta(seconds=SEND_TIMEOUT_SECONDS)
        identity = f"{user.user_id}\0{conversation_id}\0send\0{key}"
        operation_id = UUID(bytes=hashlib.sha256(identity.encode()).digest()[:16], version=4)
        request_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        async with open_cosmos(self.settings, deadline=deadline) as repository:
            conversation = await _owned(repository, user, conversation_id)
            existing = await repository.read(user, CHAT, str(operation_id))
            if existing is not None:
                return await self._existing(
                    repository, user, existing, conversation_id, request_hash
                )
            metadata = StoredConversation.model_validate_json(json.dumps(conversation.body))
            if metadata.active_operation_id is not None:
                raise SendConflict
            profile = await repository.read(user, ProductContainer.USERS, user.user_id)
            if profile is None:
                raise OnboardingRequired
            preferences = require_onboarding(profile_view(profile))
            if self.executor is None:
                raise ConfigurationError("Chat execution is not configured for this release.")
            creates: tuple[NewRecord, ...]
            patches: tuple[tuple[StoredRecord, RecordPatch], ...]
            if request.retry_of_turn_id is None:
                turn_id = uuid4()
                message = TurnMessage(
                    message_id=uuid4(),
                    role="user",
                    entity_id=user.user_id,
                    content=request.content,
                    timestamp=datetime.now(UTC),
                )
                turn_request = NewRecord(
                    kind=RecordKind.TURN,
                    record_id=turn_id,
                    fields={
                        "conversationId": str(conversation_id),
                        "turnIndex": metadata.next_turn_index,
                        "status": "pending",
                        "messages": [message.model_dump(mode="json", by_alias=True)],
                        "currentOperationId": str(operation_id),
                    },
                )
                creates = (turn_request,)
                patches = ()
                next_index = metadata.next_turn_index + 1
            else:
                turn_id = request.retry_of_turn_id
                turn = await repository.read(user, CHAT, str(turn_id))
                if turn is None or turn.body.get("conversationId") != str(conversation_id):
                    raise OwnedResourceNotFound
                public_turn = ConversationService._turn(turn, conversation_id)
                if (
                    public_turn.status != "failed"
                    or public_turn.messages[0].content != request.content
                ):
                    raise SendConflict
                creates = ()
                patches = (
                    (
                        turn,
                        RecordPatch(
                            etag=turn.etag,
                            fields={"status": "pending", "currentOperationId": str(operation_id)},
                        ),
                    ),
                )
                next_index = metadata.next_turn_index
            operation_request = NewRecord(
                kind=RecordKind.SEND_OPERATION,
                record_id=operation_id,
                fields={
                    "conversationId": str(conversation_id),
                    "turnId": str(turn_id),
                    "requestHash": request_hash,
                    "status": "pending",
                    "expiresAt": expires_at.isoformat(),
                    "failureCode": None,
                },
            )
            try:
                await repository.commit_chat(
                    user,
                    creates=(*creates, operation_request),
                    patches=(
                        (
                            conversation,
                            RecordPatch(
                                etag=conversation.etag,
                                fields={
                                    "nextTurnIndex": next_index,
                                    "activeOperationId": str(operation_id),
                                },
                            ),
                        ),
                        *patches,
                    ),
                )
            except CosmosError as error:
                if error.code not in {CosmosFailure.CONFLICT, CosmosFailure.EXISTS}:
                    raise
                existing = await repository.read(user, CHAT, str(operation_id))
                if existing is not None:
                    return await self._existing(
                        repository, user, existing, conversation_id, request_hash
                    )
                raise SendConflict from None

        # Admission is durable before execution. Reserve time for the final atomic write.
        reply = None
        failure: FailureCode | None = None
        status: TurnStatus = "completed"
        try:
            async with asyncio.timeout_at(deadline - 5):
                reply = await self.executor.execute(
                    ChatExecution(
                        user=user,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        operation_id=operation_id,
                        content=request.content,
                        explicit_preferences=preferences,
                        deadline=deadline - 5,
                    )
                )
                reply = ValidatedReply.model_validate(reply)
        except ExecutionFailed:
            reply = None
            status, failure = "failed", "execution_failed"
        except ValidationError:
            reply = None
            status, failure = "failed", "invalid_assistant_response"
        except TimeoutError:
            reply = None
            status, failure = "outcomeUnknown", "execution_outcome_unknown"

        async with open_cosmos(self.settings, deadline=deadline) as repository:
            for attempt in range(3):
                operation_record = await repository.read(user, CHAT, str(operation_id))
                if operation_record is None:
                    raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
                try:
                    return await self._finish(
                        repository,
                        user,
                        operation_record,
                        conversation_id,
                        status=status,
                        reply=reply,
                        failure=failure,
                    )
                except CosmosError as error:
                    if (
                        error.code
                        not in {
                            CosmosFailure.UNAVAILABLE,
                            CosmosFailure.TIMEOUT,
                            CosmosFailure.CONFLICT,
                        }
                        or attempt == 2
                        or monotonic() + 0.1 >= deadline
                    ):
                        raise
                    # Re-read before another ETag-guarded completion, never invoke again.
                    await asyncio.sleep(0.1)
            raise AssertionError("The completion attempt bound must be positive.")
