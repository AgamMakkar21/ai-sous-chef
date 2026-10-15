"""Partition-scoped async Cosmos primitives with conditional, non-replayed writes."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from time import monotonic
from typing import TypeVar
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
from azure.cosmos.exceptions import CosmosClientTimeoutError, CosmosHttpResponseError
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

T = TypeVar("T")
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_SYSTEM_FIELDS = frozenset({"_rid", "_self", "_etag", "_attachments", "_ts"})
_KINDS = {
    ProductContainer.USERS: {RecordKind.PROFILE},
    ProductContainer.CHAT_HISTORY: {
        RecordKind.CONVERSATION,
        RecordKind.TURN,
        RecordKind.SEND_OPERATION,
    },
    ProductContainer.RECIPE_HISTORY: {
        RecordKind.RECIPE,
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
    def _retry_after(error: CosmosHttpResponseError, attempt: int) -> float:
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

    async def _run(
        self,
        action: Callable[[], Awaitable[T]],
        *,
        deadline: float,
        read_only: bool = True,
    ) -> T:
        operation_id = uuid4()
        context = trace_id.set(trace_id.get() or str(operation_id))
        started = monotonic()
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    for attempt in range(self._settings.cosmos_max_attempts):
                        try:
                            if monotonic() >= deadline:
                                raise TimeoutError
                            result = await action()
                            if monotonic() >= deadline:
                                raise TimeoutError
                            return result
                        except CosmosHttpResponseError as error:
                            status = error.status_code
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
                raw = await self._containers[container].read_item(item=item_id, partition_key=scope)
            except CosmosHttpResponseError as error:
                if error.status_code == 404 and not error.sub_status:
                    return None
                raise
            record = self._record(raw, user, container)
            if record.body["id"] != item_id:
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4())
            return record

        return await self._run(action, deadline=deadline)

    async def create(
        self,
        user: AuthenticatedUser,
        request: NewRecord,
        *,
        deadline: float | None = None,
    ) -> StoredRecord:
        deadline = self._deadline(deadline)
        scope = self._scope(user)
        request = NewRecord.model_validate(request)
        container = next(name for name, kinds in _KINDS.items() if request.kind in kinds)
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

        async def action() -> StoredRecord:
            raw = await self._containers[container].create_item(body=body, retry_write=0)
            record = self._record(raw, user, container)
            if record.body["id"] != body["id"]:
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=True)
            return record

        return await self._run(action, deadline=deadline, read_only=False)

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
            "learnedPreferences" in request.fields and len(request.fields) > 1
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
            )
            record = self._record(raw, user, container)
            if record.body["id"] != item_id:
                raise CosmosError(CosmosFailure.INVALID_RESULT, uuid4(), outcome_unknown=True)
            return record

        return await self._run(action, deadline=deadline, read_only=False)

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
            )

        await self._run(action, deadline=deadline, read_only=False)

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
        if query.conversation_id is not None:
            sql += " AND c.conversationId = @conversation"
            parameters.append({"name": "@conversation", "value": str(query.conversation_id)})
            sql += " ORDER BY c.recordKind ASC, c.conversationId ASC, c.turnIndex ASC, c.id ASC"
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
                if record.body["recordKind"] != query.kind or (
                    query.conversation_id is not None
                    and record.body.get("conversationId") != str(query.conversation_id)
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

        return await self._run(action, deadline=deadline)


@asynccontextmanager
async def open_cosmos(
    settings: Settings, *, deadline: float | None = None
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
    deadline = min(
        deadline if deadline is not None else float("inf"),
        monotonic() + settings.cosmos_operation_timeout_seconds,
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
            connection_timeout=settings.cosmos_operation_timeout_seconds,
            read_timeout=settings.cosmos_operation_timeout_seconds,
        )
        try:
            database = client.get_database_client(settings.cosmos_database)
            containers = {
                name: database.get_container_client(name.value) for name in ProductContainer
            }
            repository = CosmosRepository(settings, containers, deadline=deadline)

            async def initialize() -> None:
                await client.__aenter__()
                for container in containers.values():
                    properties = await container.read()
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

            await repository._run(initialize, deadline=deadline)
            yield repository
        finally:
            await client.close()
