"""Partition-scoped async Cosmos primitives with conditional, non-replayed writes."""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from time import monotonic
from typing import Literal, TypeVar
from uuid import uuid4

from azure.core import MatchConditions
from azure.core.exceptions import (
    ClientAuthenticationError,
    DecodeError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.cosmos.aio import ContainerProxy, CosmosClient
from azure.cosmos.documents import ConnectionPolicy
from azure.cosmos.exceptions import (
    CosmosBatchOperationError,
    CosmosClientTimeoutError,
    CosmosHttpResponseError,
)
from opentelemetry import trace
from pydantic import JsonValue, TypeAdapter, ValidationError

from sous_chef.adapters.credentials import build_async_credential
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound
from sous_chef.domain.cosmos import (
    MAX_RECORD_BYTES,
    CosmosError,
    CosmosFailure,
    HistoryQuery,
    NewRecord,
    PageRequest,
    ProductContainer,
    RecordKind,
    RecordPage,
    RecordPatch,
    RecordRevision,
    StoredRecord,
)
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import ErrorCategory, Event, operational_event, trace_id
from sous_chef.tracing import Operation, cosmos_response, cosmos_throttled, observed

T = TypeVar("T")
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_SYSTEM_FIELDS = frozenset({"_rid", "_self", "_etag", "_attachments", "_ts"})
_KINDS = {
    ProductContainer.USERS: {RecordKind.PROFILE},
    ProductContainer.CHAT_HISTORY: {
        RecordKind.CONVERSATION,
        RecordKind.CONVERSATION_CREATION,
        RecordKind.TURN,
        RecordKind.SEND_OPERATION,
    },
    ProductContainer.RECIPE_HISTORY: {
        RecordKind.RECIPE,
        RecordKind.RECIPE_OPERATION,
        RecordKind.BEHAVIOR,
        RecordKind.FEEDBACK_REVISION,
        RecordKind.PROCESSING,
    },
}


def _timestamp(value: datetime | None = None) -> str:
    return (
        (value or datetime.now(UTC))
        .astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _size(body: dict[str, JsonValue]) -> int:
    application_record = {key: value for key, value in body.items() if key not in _SYSTEM_FIELDS}
    return len(json.dumps(application_record, allow_nan=False).encode("utf-8"))


class CosmosRepository:
    def __init__(
        self,
        settings: Settings,
        containers: Mapping[ProductContainer, ContainerProxy],
        *,
        deadline: float | None = None,
    ) -> None:
        self._settings = settings
        self._containers = containers
        self._scope_deadline = deadline

    def _deadline(self, deadline: float | None) -> float:
        if deadline is not None and not isfinite(deadline):
            raise ValueError("A deadline must be a finite monotonic timestamp.")
        own_deadline = monotonic() + self._settings.cosmos_operation_timeout_seconds
        if self._scope_deadline is not None:
            own_deadline = min(own_deadline, self._scope_deadline)
        return min(deadline, own_deadline) if deadline is not None else own_deadline

    @staticmethod
    def _scope(user: AuthenticatedUser) -> str:
        if (
            not isinstance(user, AuthenticatedUser)
            or not isinstance(user.user_id, str)
            or not user.user_id
        ):
            raise ValueError("An authenticated user context is required.")
        return user.user_id

    @staticmethod
    def _identifier(item_id: str) -> None:
        if (
            not isinstance(item_id, str)
            or not item_id
            or len(item_id) > 255
            or any(character in item_id for character in "/\\?#")
            or any(ord(character) < 32 for character in item_id)
        ):
            raise ValueError("A bounded Cosmos item ID is required.")

    @staticmethod
    def _record(raw: object, user: AuthenticatedUser, container: ProductContainer) -> StoredRecord:
        if not isinstance(raw, dict):
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
        user.require_owned(raw)
        try:
            body = _JSON_OBJECT.validate_python(raw, strict=True)
        except ValidationError:
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None
        item_id = body.get("id")
        etag = body.get("_etag")
        kind = body.get("recordKind")
        if (
            not isinstance(item_id, str)
            or not isinstance(etag, str)
            or not etag
            or not isinstance(kind, str)
            or kind not in _KINDS[container]
            or type(body.get("schemaVersion")) is not int
            or body.get("schemaVersion") != 1
            or not isinstance(body.get("createdAt"), str)
        ):
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
        try:
            RecordRevision(etag=etag)
            CosmosRepository._identifier(item_id)
            created_at = body["createdAt"]
            if not isinstance(created_at, str) or datetime.fromisoformat(created_at).tzinfo is None:
                raise ValueError
            oversized = _size(body) > MAX_RECORD_BYTES
        except (ValueError, OverflowError):
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None
        if container == ProductContainer.USERS and body["id"] != user.user_id:
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
        if oversized:
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
        return StoredRecord(body, etag)

    @staticmethod
    def _failure(status: int | None) -> CosmosFailure:
        if status is None:
            return CosmosFailure.UNAVAILABLE
        return {
            401: CosmosFailure.AUTHENTICATION,
            403: CosmosFailure.FORBIDDEN,
            409: CosmosFailure.EXISTS,
            412: CosmosFailure.CONFLICT,
            413: CosmosFailure.TOO_LARGE,
            429: CosmosFailure.THROTTLED,
            408: CosmosFailure.TIMEOUT,
            504: CosmosFailure.TIMEOUT,
        }.get(
            status,
            CosmosFailure.UNAVAILABLE if status >= 500 else CosmosFailure.REJECTED,
        )

    @staticmethod
    def _retry_after(
        error: CosmosHttpResponseError | CosmosBatchOperationError, attempt: int
    ) -> float:
        headers = {key.lower(): value for key, value in error.headers.items()}
        try:
            if "x-ms-retry-after-ms" in headers:
                delay = float(headers["x-ms-retry-after-ms"]) / 1000
            elif "retry-after" in headers:
                value = headers["retry-after"]
                try:
                    delay = float(value)
                except ValueError:
                    date = parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        raise ValueError from None
                    delay = max(0.0, (date - datetime.now(UTC)).total_seconds())
            else:
                delay = min(0.1 * 2**attempt, 1.0)
            if not isfinite(delay) or delay < 0:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None
        return delay

    @observed(Operation.COSMOS)
    async def _run(
        self,
        action: Callable[[], Awaitable[T]],
        *,
        deadline: float,
        read_only: bool = True,
        operation_name: Literal[
            "read", "create", "batch", "patch", "delete", "query", "initialize"
        ] = "read",
        container: ProductContainer | None = None,
    ) -> T:
        operation_id = uuid4()
        context = trace_id.set(trace_id.get() or str(operation_id))
        started = monotonic()
        span = trace.get_current_span()
        span.set_attribute("db.system.name", "cosmosdb")
        span.set_attribute("db.operation.name", operation_name)
        if container is not None:
            span.set_attribute("db.collection.name", container.value)
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    for attempt in range(self._settings.cosmos_max_attempts):
                        span.set_attribute("asc.cosmos.attempt_count", attempt + 1)
                        try:
                            if monotonic() >= deadline:
                                raise TimeoutError
                            result = await action()
                            if monotonic() >= deadline:
                                raise TimeoutError
                            return result
                        except (CosmosHttpResponseError, CosmosBatchOperationError) as error:
                            status = error.status_code
                            cosmos_response(error.headers, None)
                            if status == 429:
                                cosmos_throttled()
                            retryable = status == 429 or (
                                read_only and status in {408, 500, 502, 503, 504}
                            )
                            delay = self._retry_after(error, attempt) if retryable else None
                            if (
                                not retryable
                                or attempt + 1 == self._settings.cosmos_max_attempts
                                or (delay is not None and delay >= deadline - monotonic())
                            ):
                                raise CosmosError(
                                    self._failure(status),
                                    operation_id,
                                    retry_after_seconds=delay,
                                    outcome_unknown=not read_only
                                    and (status is None or status == 408 or status >= 500),
                                ) from None
                            await asyncio.sleep(delay if delay is not None else 0)
                        except (ServiceRequestError, ServiceResponseError):
                            if not read_only or attempt + 1 == self._settings.cosmos_max_attempts:
                                raise CosmosError(
                                    CosmosFailure.UNAVAILABLE,
                                    operation_id,
                                    outcome_unknown=not read_only,
                                ) from None
                            await asyncio.sleep(min(0.1 * 2**attempt, 1.0))
                    raise AssertionError("The configured attempt bound must be positive.")
            except (TimeoutError, CosmosClientTimeoutError):
                raise CosmosError(
                    CosmosFailure.TIMEOUT, operation_id, outcome_unknown=not read_only
                ) from None
            except ClientAuthenticationError:
                raise CosmosError(CosmosFailure.AUTHENTICATION, operation_id) from None
            except (DecodeError, json.JSONDecodeError, UnicodeDecodeError):
                raise CosmosError(
                    CosmosFailure.INVALID_RESULT, operation_id, outcome_unknown=not read_only
                ) from None
        except CosmosError as error:
            operational_event(
                Event.COSMOS_FAILED,
                category=ErrorCategory(error.code.value),
                duration_ms=(monotonic() - started) * 1000,
            )
            raise CosmosError(
                error.code,
                operation_id,
                retry_after_seconds=error.retry_after_seconds,
                outcome_unknown=error.outcome_unknown
                or (not read_only and error.code == CosmosFailure.INVALID_RESULT),
            ) from None
        finally:
            trace_id.reset(context)

    async def read(
        self,
        user: AuthenticatedUser,
        container: ProductContainer,
        item_id: str,
        *,
        deadline: float | None = None,
    ) -> StoredRecord | None:
        deadline = self._deadline(deadline)
        scope = self._scope(user)
        self._identifier(item_id)

        async def action() -> StoredRecord | None:
            try:
                raw = await self._containers[container].read_item(
                    item=item_id, partition_key=scope, response_hook=cosmos_response
                )
            except CosmosHttpResponseError as error:
                if error.status_code == 404 and not error.sub_status:
                    cosmos_response(error.headers, None)
                    return None
                raise
            record = self._record(raw, user, container)
            if record.body["id"] != item_id:
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
            return record

        return await self._run(action, deadline=deadline, container=container)

    def _new_body(self, user: AuthenticatedUser, request: NewRecord) -> dict[str, JsonValue]:
        scope = self._scope(user)
        now = _timestamp()
        body: dict[str, JsonValue] = {
            **deepcopy(request.fields),
            "id": (
                scope if request.kind == RecordKind.PROFILE else str(request.record_id or uuid4())
            ),
            "userId": scope,
            "recordKind": request.kind.value,
            "schemaVersion": 1,
            "createdAt": now,
            "updatedAt": now,
        }
        if _size(body) > MAX_RECORD_BYTES:
            raise CosmosError(CosmosFailure.TOO_LARGE, uuid4())
        return body

    async def create_conversation(
        self,
        user: AuthenticatedUser,
        request: NewRecord,
        *,
        receipt_id: str,
        request_hash: str,
    ) -> StoredRecord:
        """Commit metadata and durable creation deduplication in the same user partition."""
        deadline = self._deadline(None)
        request = NewRecord.model_validate(request)
        if (
            request.kind != RecordKind.CONVERSATION
            or re.fullmatch(r"conversation-create:[0-9a-f]{64}", receipt_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
        ):
            raise ValueError("A conversation and bounded creation receipt are required.")
        body = self._new_body(user, request)
        receipt = {
            **self._new_body(
                user,
                NewRecord(
                    kind=RecordKind.CONVERSATION_CREATION,
                    fields={"resourceId": body["id"], "requestHash": request_hash},
                ),
            ),
            "id": receipt_id,
        }

        async def action() -> StoredRecord:
            results = await self._containers[ProductContainer.CHAT_HISTORY].execute_item_batch(
                batch_operations=[("create", (receipt,)), ("create", (body,))],
                partition_key=user.user_id,
                retry_write=0,
                response_hook=cosmos_response,
            )
            if len(results) != 2 or any(
                not isinstance(result, dict) or result.get("statusCode") != 201
                for result in results
            ):
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=True)
            saved_receipt = self._record(
                results[0].get("resourceBody"), user, ProductContainer.CHAT_HISTORY
            )
            record = self._record(
                results[1].get("resourceBody"), user, ProductContainer.CHAT_HISTORY
            )
            if (
                saved_receipt.body["id"] != receipt_id
                or saved_receipt.body["recordKind"] != RecordKind.CONVERSATION_CREATION
                or saved_receipt.body.get("resourceId") != body["id"]
                or saved_receipt.body.get("requestHash") != request_hash
                or record.body["id"] != body["id"]
                or record.body["recordKind"] != RecordKind.CONVERSATION
            ):
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=True)
            return record

        return await self._run(
            action,
            deadline=deadline,
            read_only=False,
            operation_name="batch",
            container=ProductContainer.CHAT_HISTORY,
        )

    async def create(
        self,
        user: AuthenticatedUser,
        request: NewRecord,
        *,
        deadline: float | None = None,
    ) -> StoredRecord:
        deadline = self._deadline(deadline)
        request = NewRecord.model_validate(request)
        if request.kind == RecordKind.CONVERSATION_CREATION:
            raise ValueError("Conversation creation receipts must be committed with metadata.")
        container = next(name for name, kinds in _KINDS.items() if request.kind in kinds)
        body = self._new_body(user, request)

        async def action() -> StoredRecord:
            raw = await self._containers[container].create_item(
                body=body, retry_write=0, response_hook=cosmos_response
            )
            record = self._record(raw, user, container)
            if record.body["id"] != body["id"]:
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=True)
            return record

        return await self._run(
            action, deadline=deadline, read_only=False, operation_name="create", container=container
        )

    async def commit_chat(
        self,
        user: AuthenticatedUser,
        *,
        creates: tuple[NewRecord, ...] = (),
        patches: tuple[tuple[StoredRecord, RecordPatch], ...],
        deadline: float | None = None,
    ) -> tuple[StoredRecord, ...]:
        """Atomically guard conversation admission/completion with its turn and receipt."""
        if not patches:
            raise ValueError("A chat transition requires one bounded conditional batch.")
        return await self._commit_history(
            user,
            ProductContainer.CHAT_HISTORY,
            creates,
            patches,
            create_kinds={RecordKind.TURN, RecordKind.SEND_OPERATION},
            patch_kinds={RecordKind.CONVERSATION, RecordKind.TURN, RecordKind.SEND_OPERATION},
            deadline=deadline,
        )

    async def commit_recipe(
        self,
        user: AuthenticatedUser,
        *,
        creates: tuple[NewRecord, ...],
        patches: tuple[tuple[StoredRecord, RecordPatch], ...] = (),
    ) -> tuple[StoredRecord, ...]:
        """Commit a lifecycle effect and its durable receipt in the same partition."""
        if sum(record.kind == RecordKind.RECIPE_OPERATION for record in creates) != 1 or any(
            set(patch.fields) != {"cookedAt"} for _, patch in patches
        ):
            raise ValueError("A lifecycle batch requires one receipt and only cooking patches.")
        return await self._commit_history(
            user,
            ProductContainer.RECIPE_HISTORY,
            creates,
            patches,
            create_kinds={RecordKind.RECIPE, RecordKind.BEHAVIOR, RecordKind.RECIPE_OPERATION},
            patch_kinds={RecordKind.RECIPE},
        )

    async def commit_feedback(
        self,
        user: AuthenticatedUser,
        *,
        creates: tuple[NewRecord, ...],
        patches: tuple[tuple[StoredRecord, RecordPatch], ...],
    ) -> tuple[StoredRecord, ...]:
        """Atomically persist current feedback, an optional revision, and one receipt."""
        if (
            sum(record.kind == RecordKind.RECIPE_OPERATION for record in creates) != 1
            or sum(record.kind == RecordKind.FEEDBACK_REVISION for record in creates) > 1
            or len(patches) != 1
            or set(patches[0][1].fields)
            != {"rating", "note", "feedbackRevision", "processingStatus"}
        ):
            raise ValueError("A feedback batch requires one receipt and one feedback patch.")
        return await self._commit_history(
            user,
            ProductContainer.RECIPE_HISTORY,
            creates,
            patches,
            create_kinds={RecordKind.RECIPE_OPERATION, RecordKind.FEEDBACK_REVISION},
            patch_kinds={RecordKind.RECIPE},
        )

    async def _commit_history(
        self,
        user: AuthenticatedUser,
        container: ProductContainer,
        creates: tuple[NewRecord, ...],
        patches: tuple[tuple[StoredRecord, RecordPatch], ...],
        *,
        create_kinds: set[RecordKind],
        patch_kinds: set[RecordKind],
        deadline: float | None = None,
    ) -> tuple[StoredRecord, ...]:
        deadline = self._deadline(deadline)
        scope = self._scope(user)
        if not creates and not patches or len(creates) + len(patches) > 3:
            raise ValueError("A history transition requires one bounded batch.")
        bodies = []
        operations: list[
            tuple[str, tuple[object, ...]] | tuple[str, tuple[object, ...], dict[str, object]]
        ] = []
        for request in creates:
            request = NewRecord.model_validate(request)
            if request.kind not in create_kinds:
                raise ValueError("This record kind cannot be created in this batch.")
            body = self._new_body(user, request)
            bodies.append(body)
            operations.append(("create", (body,)))
        for current, patch in patches:
            current = self._record(current.body, user, container)
            patch = RecordPatch.model_validate(patch)
            if current.body["recordKind"] not in patch_kinds or current.etag != patch.etag:
                raise ValueError("A history patch requires its current owned revision.")
            changes = {**deepcopy(patch.fields), "updatedAt": _timestamp()}
            body = {**current.body, **changes}
            if _size(body) > MAX_RECORD_BYTES:
                raise CosmosError(CosmosFailure.TOO_LARGE, uuid4())
            bodies.append(body)
            operations.append(
                (
                    "patch",
                    (
                        body["id"],
                        [
                            {"op": "set", "path": "/" + name, "value": value}
                            for name, value in changes.items()
                        ],
                    ),
                    {"if_match_etag": patch.etag},
                )
            )
        if len({str(body["id"]) for body in bodies}) != len(bodies):
            raise ValueError("A history batch must address distinct records.")

        async def action() -> tuple[StoredRecord, ...]:
            results = await self._containers[container].execute_item_batch(
                batch_operations=operations,
                partition_key=scope,
                retry_write=0,
                response_hook=cosmos_response,
            )
            if len(results) != len(bodies):
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=True)
            records: list[StoredRecord] = []
            for result, body in zip(results, bodies, strict=True):
                if not isinstance(result, dict) or result.get("statusCode") not in {200, 201}:
                    raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=True)
                record = self._record(result.get("resourceBody"), user, container)
                if any(
                    record.body.get(key) != value
                    for key, value in body.items()
                    if key not in _SYSTEM_FIELDS
                ):
                    raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=True)
                records.append(record)
            return tuple(records)

        return await self._run(
            action, deadline=deadline, read_only=False, operation_name="batch", container=container
        )

    async def commit_learning(
        self,
        user: AuthenticatedUser,
        *,
        receipt: NewRecord,
        window: NewRecord | tuple[StoredRecord, RecordPatch],
        source: StoredRecord,
    ) -> tuple[StoredRecord, ...]:
        """Commit a receipt/window and check the current source in one partition."""
        if (
            receipt.kind != RecordKind.PROCESSING
            or receipt.fields.get("role") != "feedbackApplication"
            or source.body.get("recordKind") != "recipe"
        ):
            raise ValueError("Learning requires a typed processing receipt.")
        creates: tuple[NewRecord, ...] = (receipt,)
        patches: tuple[tuple[StoredRecord, RecordPatch], ...] = (
            (
                source,
                RecordPatch(
                    etag=source.etag,
                    fields={"processingStatus": source.body.get("processingStatus")},
                ),
            ),
        )
        if isinstance(window, NewRecord):
            if window.kind != RecordKind.PROCESSING or window.fields.get("role") != (
                "learningWindow"
            ):
                raise ValueError("Learning requires a typed processing window.")
            creates += (window,)
        else:
            record, patch = window
            if record.body.get("role") != "learningWindow" or set(patch.fields) != {
                "generation",
                "sources",
            }:
                raise ValueError("Only the learning generation/window can change.")
            patches += (window,)
        return await self._commit_history(
            user,
            ProductContainer.RECIPE_HISTORY,
            creates,
            patches,
            create_kinds={RecordKind.PROCESSING},
            patch_kinds={RecordKind.PROCESSING, RecordKind.RECIPE},
        )

    async def generated_recipe(
        self, user: AuthenticatedUser, generated_recipe_id: str
    ) -> StoredRecord:
        """Resolve exactly one owned completed source, not a browser-provided snapshot."""
        scope = self._scope(user)
        self._identifier(generated_recipe_id)

        async def action() -> StoredRecord:
            items = self._containers[ProductContainer.CHAT_HISTORY].query_items(
                query=(
                    "SELECT TOP 2 * FROM c WHERE c.recordKind = @kind"
                    " AND c.status = @status AND c.generatedRecipeId = @recipe"
                ),
                parameters=[
                    {"name": "@kind", "value": "turn"},
                    {"name": "@status", "value": "completed"},
                    {"name": "@recipe", "value": generated_recipe_id},
                ],
                partition_key=scope,
                max_item_count=2,
                response_hook=cosmos_response,
            )
            records: list[StoredRecord] = []
            async for raw in items:
                record = self._record(raw, user, ProductContainer.CHAT_HISTORY)
                if (
                    records
                    or record.body["recordKind"] != "turn"
                    or record.body.get("status") != "completed"
                    or record.body.get("generatedRecipeId") != generated_recipe_id
                ):
                    raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
                records.append(record)
            if not records:
                raise OwnedResourceNotFound
            return records[0]

        return await self._run(
            action,
            deadline=self._deadline(None),
            operation_name="query",
            container=ProductContainer.CHAT_HISTORY,
        )

    async def patch(
        self,
        user: AuthenticatedUser,
        container: ProductContainer,
        item_id: str,
        request: RecordPatch,
        *,
        deadline: float | None = None,
    ) -> StoredRecord:
        deadline = self._deadline(deadline)
        request = RecordPatch.model_validate(request)
        if container == ProductContainer.USERS and (
            "learnedPreferences" in request.fields
            and set(request.fields)
            not in (
                {"learnedPreferences"},
                {"learnedPreferences", "learningGeneration"},
            )
        ):
            raise ValueError("A learned-state patch cannot change other profile sections.")
        current = await self.read(user, container, item_id, deadline=deadline)
        if current is None:
            raise OwnedResourceNotFound
        if current.etag != request.etag:
            raise CosmosError(CosmosFailure.CONFLICT, uuid4())
        changes: dict[str, JsonValue] = {**deepcopy(request.fields), "updatedAt": _timestamp()}
        projected = {**current.body, **changes}
        if _size(projected) > MAX_RECORD_BYTES:
            raise CosmosError(CosmosFailure.TOO_LARGE, uuid4())
        operations = [
            {"op": "set", "path": "/" + name, "value": value} for name, value in changes.items()
        ]

        async def action() -> StoredRecord:
            raw = await self._containers[container].patch_item(
                item=item_id,
                partition_key=user.user_id,
                patch_operations=operations,
                etag=request.etag,
                match_condition=MatchConditions.IfNotModified,
                retry_write=0,
                response_hook=cosmos_response,
            )
            record = self._record(raw, user, container)
            if record.body["id"] != item_id:
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=True)
            return record

        return await self._run(
            action, deadline=deadline, read_only=False, operation_name="patch", container=container
        )

    async def delete(
        self,
        user: AuthenticatedUser,
        container: ProductContainer,
        item_id: str,
        etag: str,
        *,
        deadline: float | None = None,
    ) -> None:
        RecordRevision(etag=etag)
        deadline = self._deadline(deadline)
        current = await self.read(user, container, item_id, deadline=deadline)
        if current is None:
            raise OwnedResourceNotFound
        if current.etag != etag:
            raise CosmosError(CosmosFailure.CONFLICT, uuid4())

        async def action() -> None:
            await self._containers[container].delete_item(
                item=item_id,
                partition_key=user.user_id,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
                retry_write=0,
                response_hook=cosmos_response,
            )

        await self._run(
            action, deadline=deadline, read_only=False, operation_name="delete", container=container
        )

    async def query(
        self,
        user: AuthenticatedUser,
        query: HistoryQuery,
        page: PageRequest,
        *,
        deadline: float | None = None,
    ) -> RecordPage:
        deadline = self._deadline(deadline)
        scope = self._scope(user)
        query = HistoryQuery.model_validate(query)
        page = PageRequest.model_validate(page)
        container = (
            ProductContainer.RECIPE_HISTORY
            if query.kind == "recipe"
            else ProductContainer.CHAT_HISTORY
        )
        sql = "SELECT * FROM c WHERE c.recordKind = @kind AND c.createdAt <= @upper"
        parameters: list[dict[str, object]] = [
            {"name": "@kind", "value": query.kind},
            {"name": "@upper", "value": _timestamp(query.created_before)},
        ]
        if query.recipe_filter != "all":
            sql += " AND IS_DEFINED(c.cookedAt) AND NOT IS_NULL(c.cookedAt)"
            if query.recipe_filter == "awaitingFeedback":
                sql += " AND IS_NULL(c.rating)"
            else:
                sql += " AND c.rating IN (@liked, @fine, @disliked)"
                parameters.extend(
                    {"name": "@" + rating, "value": rating}
                    for rating in ("liked", "fine", "disliked")
                )
        if query.conversation_id is not None:
            sql += " AND c.conversationId = @conversation"
            parameters.append({"name": "@conversation", "value": str(query.conversation_id)})
            if query.completed_only:
                sql += " AND c.status = @status"
                parameters.append({"name": "@status", "value": "completed"})
            direction = "DESC" if query.completed_only else "ASC"
            sql += (
                f" ORDER BY c.recordKind {direction}, c.conversationId {direction},"
                f" c.turnIndex {direction}, c.id {direction}"
            )
        else:
            sql += " ORDER BY c.recordKind ASC, c.createdAt DESC, c.id DESC"
        limit = page.limit if page.limit is not None else self._settings.cosmos_page_size

        async def action() -> RecordPage:
            items = self._containers[container].query_items(
                query=sql,
                parameters=parameters,
                partition_key=scope,
                max_item_count=limit,
                continuation_token_limit=8,
                response_hook=cosmos_response,
            )
            pages = items.by_page(continuation_token=page.continuation_token)
            try:
                results = await anext(pages)
            except StopAsyncIteration:
                return RecordPage((), None)
            records: list[StoredRecord] = []
            async for raw in results:
                if len(records) == limit:
                    raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
                record = self._record(raw, user, container)
                if (
                    record.body["recordKind"] != query.kind
                    or (
                        query.conversation_id is not None
                        and record.body.get("conversationId") != str(query.conversation_id)
                    )
                    or (query.completed_only and record.body.get("status") != "completed")
                    or (
                        query.recipe_filter != "all"
                        and (
                            record.body.get("cookedAt") is None
                            or (
                                query.recipe_filter == "awaitingFeedback"
                                and (
                                    "rating" not in record.body or record.body["rating"] is not None
                                )
                            )
                            or (
                                query.recipe_filter == "rated"
                                and record.body.get("rating") not in ("liked", "fine", "disliked")
                            )
                        )
                    )
                ):
                    raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
                records.append(record)
            token = items.get_response_headers().get("x-ms-continuation")
            if token == "":
                token = None
            try:
                token = PageRequest(continuation_token=token).continuation_token
            except ValidationError:
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4()) from None
            return RecordPage(tuple(records), token)

        return await self._run(
            action, deadline=deadline, operation_name="query", container=container
        )


@asynccontextmanager
async def open_cosmos(
    settings: Settings,
    *,
    deadline: float | None = None,
    containers: tuple[ProductContainer, ...] = tuple(ProductContainer),
    lifetime_seconds: float | None = None,
) -> AsyncIterator[CosmosRepository]:
    """Open one short unit of work; setup and its operations share a deadline."""
    if (
        settings.adapter_mode != "live"
        or not settings.cosmos_endpoint
        or not settings.cosmos_database
    ):
        raise ConfigurationError("Cosmos access requires live mode and its endpoint and database.")
    policy = ConnectionPolicy()
    # The SDK's retry_total=0 does not clear its separate throttling defaults.
    policy.RetryOptions = type(policy.RetryOptions)(max_retry_attempt_count=0)
    if deadline is not None and not isfinite(deadline):
        raise ValueError("A deadline must be a finite monotonic timestamp.")
    if lifetime_seconds is not None and (
        not isfinite(lifetime_seconds) or not 0 < lifetime_seconds <= 60
    ):
        raise ValueError("A Cosmos unit of work must be bounded to sixty seconds.")
    deadline = min(
        deadline if deadline is not None else float("inf"),
        monotonic()
        + (
            lifetime_seconds
            if lifetime_seconds is not None
            else settings.cosmos_operation_timeout_seconds
        ),
    )
    async with build_async_credential(settings) as credential:
        client = CosmosClient(
            str(settings.cosmos_endpoint),
            credential=credential,
            connection_policy=policy,
            retry_total=0,
            retry_connect=0,
            retry_read=0,
            retry_status=0,
            retry_write=0,
            enable_endpoint_discovery=False,
            availability_strategy=False,
            logging_enable=False,
            tracing_options={"enabled": False},
            connection_timeout=settings.cosmos_operation_timeout_seconds,
            read_timeout=settings.cosmos_operation_timeout_seconds,
        )
        try:
            database = client.get_database_client(settings.cosmos_database)
            proxies = {name: database.get_container_client(name.value) for name in containers}
            repository = CosmosRepository(settings, proxies, deadline=deadline)

            async def initialize() -> None:
                await client.__aenter__()
                for container in proxies.values():
                    properties = await container.read(response_hook=cosmos_response)
                    partition = properties.get("partitionKey")
                    if not isinstance(partition, dict) or (
                        partition.get("paths") != ["/userId"]
                        or partition.get("kind") != "Hash"
                        or partition.get("version") != 2
                        or properties.get("defaultTtl") is not None
                    ):
                        raise ConfigurationError(
                            "Cosmos product containers require /userId, Hash v2 and no TTL."
                        )

            await repository._run(
                initialize, deadline=repository._deadline(deadline), operation_name="initialize"
            )
            yield repository
        finally:
            await client.close()
