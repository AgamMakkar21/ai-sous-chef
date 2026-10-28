import asyncio
import json
import time
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ServiceRequestError, ServiceResponseError
from azure.core.pipeline.transport import AsyncHttpResponse, AsyncHttpTransport, HttpRequest
from azure.cosmos.aio import CosmosClient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue, ValidationError
from sous_chef.adapters import cosmos
from sous_chef.api.authentication import CurrentUser
from sous_chef.api.problems import register_problem_handlers
from sous_chef.app import create_app
from sous_chef.check_cosmos import CosmosSmokeError, cleanup_smoke, run_smoke
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
    RecordPatch,
)
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import configure_safe_telemetry, trace_id

ALICE = AuthenticatedUser.from_verified_identity("synthetic-issuer", "alice")
BOB = AuthenticatedUser.from_verified_identity("synthetic-issuer", "bob")
USERS = ProductContainer.USERS
RECIPES = ProductContainer.RECIPE_HISTORY


def settings(**overrides: Any) -> Settings:
    return Settings(
        **{
            "environment": "local",
            "adapter_mode": "live",
            "cosmos_endpoint": "https://cosmos.example.invalid",
            "cosmos_database": "sous-chef",
            "foundry_project_endpoint": "https://project.example.invalid/api/projects/test",
            "foundry_orchestrator_deployment": "synthetic",
            "foundry_recipe_nutrition_deployment": "synthetic",
            "foundry_extraction_deployment": "synthetic",
            **overrides,
        }
    )


class SyntheticCredential:
    def __init__(self) -> None:
        self.closed = False

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken("synthetic-token", int(time.time()) + 3600)

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "SyntheticCredential":
        self.closed = False
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


class Response(AsyncHttpResponse):
    def __init__(
        self, request: HttpRequest, status: int, body: object, headers: dict[str, str]
    ) -> None:
        super().__init__(request, None)
        self.status_code = status
        self.headers = {"content-type": "application/json", **headers}
        self.content_type = "application/json"
        self.reason = "Synthetic response"
        self._body = json.dumps(body).encode()

    def body(self) -> bytes:
        return self._body

    async def load_body(self) -> None:
        pass


@dataclass
class Call:
    method: str
    path: str
    headers: dict[str, str]
    body: Any
    at: float


class Transport(AsyncHttpTransport[HttpRequest, AsyncHttpResponse]):
    """Stateful service double below the actual pinned Cosmos SDK."""

    def __init__(self) -> None:
        self.closed = False
        self.cancelled = False
        self.hang_started = asyncio.Event()
        self.calls: list[Call] = []
        self.documents: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.faults: dict[str, deque[Any]] = defaultdict(deque)
        self.retry_after = "1"
        self.revision = 0
        self.block_reads = 0
        self.read_count = 0
        self.barrier = asyncio.Event()
        self.bad_partition = False
        self.setup_fault: int | str | None = None
        self.write_then_disconnect = False
        self.setup_delay = 0.0
        self.fault_headers: dict[str, str] | None = None
        self.empty_query_pages = 0
        self.malformed_json = False
        self.query_continuation: str | None = None
        self.container_overrides: dict[str, Any] = {}

    async def open(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "Transport":
        await self.open()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def item_calls(self, method: str | None = None) -> list[Call]:
        return [
            call
            for call in self.calls
            if "/docs" in call.path and (method is None or call.method == method)
        ]

    async def send(self, request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
        path = unquote(urlsplit(request.url).path).rstrip("/")
        body: Any = None
        if request.body:
            assert isinstance(request.body, str | bytes)
            body = json.loads(request.body)
        headers = {key.lower(): value for key, value in request.headers.items()}
        call = Call(request.method, path, headers, body, time.monotonic())
        self.calls.append(call)

        def reply(status: int, result: object, **extra: str) -> Response:
            response = Response(request, status, result, extra)
            if self.malformed_json and "/docs" in path and 200 <= status < 300:
                response._body = b"secret-private-not-json"
            return response

        async def fault(value: Any) -> Response:
            if isinstance(value, BaseException):
                raise value
            if value == "hang":
                self.hang_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return reply(
                value,
                {"code": "SyntheticFailure", "message": "secret-private-service-body"},
                **(
                    self.fault_headers
                    if self.fault_headers is not None
                    else {"x-ms-retry-after-ms": self.retry_after}
                ),
            )

        if "/docs" not in path and self.setup_fault is not None:
            return await fault(self.setup_fault)
        if "/docs" not in path and self.setup_delay:
            await asyncio.sleep(self.setup_delay)
        if not path:
            location = {
                "name": "synthetic-region",
                "databaseAccountEndpoint": "https://cosmos.example.invalid/",
            }
            return reply(
                200,
                {
                    "id": "synthetic",
                    "writableLocations": [location],
                    "readableLocations": [location],
                    "consistencyPolicy": {"defaultConsistencyLevel": "Session"},
                },
            )
        parts = path.split("/")
        container = parts[4] if len(parts) >= 5 and parts[3] == "colls" else None
        if container is not None and len(parts) == 5 and request.method == "GET":
            return reply(
                200,
                {
                    "id": container,
                    "_rid": "AQAAAAIAAAA=",
                    "_self": "dbs/AQAAAA==/colls/AQAAAAIAAAA=/",
                    "_etag": '"metadata"',
                    "partitionKey": {
                        "paths": ["/wrong"] if self.bad_partition else ["/userId"],
                        "kind": "Hash",
                        "version": 2,
                    },
                    **self.container_overrides,
                },
            )
        assert container is not None and len(parts) >= 6 and parts[5] == "docs", call
        if self.faults[request.method]:
            return await fault(self.faults[request.method].popleft())
        partition = json.loads(headers["x-ms-documentdb-partitionkey"])[0]
        if request.method == "POST" and isinstance(body, list):
            documents = [operation["resourceBody"] for operation in body]
            assert all(operation["operationType"] == "Create" for operation in body)
            assert all(document["userId"] == partition for document in documents)
            keys = [(container, partition, document["id"]) for document in documents]
            conflicts = [index for index, key in enumerate(keys) if key in self.documents]
            if conflicts:
                return reply(
                    200,
                    [
                        {"statusCode": 409 if index == conflicts[0] else 424}
                        for index in range(len(documents))
                    ],
                )
            saved = []
            for key, document in zip(keys, documents, strict=True):
                self.revision += 1
                stored = {**deepcopy(document), "_etag": f'"{self.revision}"'}
                self.documents[key] = stored
                saved.append({"statusCode": 201, "resourceBody": stored})
            if self.write_then_disconnect:
                raise ServiceResponseError("secret-private-lost-batch-response")
            return reply(200, saved)
        if request.method == "POST" and body.get("query"):
            assert headers.get("x-ms-documentdb-isquery", "").lower() == "true"
            if self.empty_query_pages:
                self.empty_query_pages -= 1
                return reply(200, {"Documents": []}, **{"x-ms-continuation": "0"})
            parameters = {item["name"]: item["value"] for item in body["parameters"]}
            items = [
                deepcopy(document)
                for (name, owner, _), document in self.documents.items()
                if name == container
                and owner == partition
                and document["recordKind"] == parameters["@kind"]
                and document["createdAt"] <= parameters["@upper"]
                and (
                    "@conversation" not in parameters
                    or document.get("conversationId") == parameters["@conversation"]
                )
            ]
            if parameters["@kind"] == "turn":
                items.sort(key=lambda item: (item["turnIndex"], item["id"]))
            else:
                items.sort(key=lambda item: (item["createdAt"], item["id"]), reverse=True)
            offset = int(headers.get("x-ms-continuation", "0"))
            limit = int(headers["x-ms-max-item-count"])
            page = items[offset : offset + limit]
            continuation = (
                {"x-ms-continuation": str(offset + limit)} if offset + limit < len(items) else {}
            )
            if self.query_continuation is not None:
                continuation["x-ms-continuation"] = self.query_continuation
            return reply(200, {"Documents": page, "_count": len(page)}, **continuation)
        item_id = parts[6] if len(parts) == 7 else body["id"]
        key = (container, partition, item_id)
        current = self.documents.get(key)
        if request.method == "GET":
            snapshot = deepcopy(current)
            if self.block_reads:
                self.read_count += 1
                if self.read_count == self.block_reads:
                    self.barrier.set()
                await self.barrier.wait()
            return reply(200 if snapshot else 404, snapshot or {"code": "NotFound"})
        if request.method == "POST":
            if current is not None:
                return reply(409, {"code": "Conflict"})
            self.revision += 1
            document = {**deepcopy(body), "_etag": f'"{self.revision}"'}
            self.documents[key] = document
            if self.write_then_disconnect:
                raise ServiceResponseError("secret-private-lost-write-response")
            return reply(201, document, etag=document["_etag"])
        if current is None:
            return reply(404, {"code": "NotFound"})
        if headers.get("if-match") != current["_etag"]:
            return reply(412, {"code": "PreconditionFailed"})
        if request.method == "DELETE":
            del self.documents[key]
            return reply(204, {})
        assert request.method == "PATCH", call
        for operation in body["operations"]:
            assert operation["op"] == "set"
            current[operation["path"][1:]] = operation["value"]
        self.revision += 1
        current["_etag"] = f'"{self.revision}"'
        if self.write_then_disconnect:
            raise ServiceResponseError("secret-private-lost-patch-response")
        return reply(200, current, etag=current["_etag"])


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> tuple[Transport, SyntheticCredential]:
    monkeypatch.delenv("ASC_APPROVE_COSMOS_SMOKE", raising=False)
    transport = Transport()
    credential = SyntheticCredential()
    monkeypatch.setattr(cosmos, "build_async_credential", lambda _settings: credential)

    def factory(*args: Any, **kwargs: Any) -> CosmosClient:
        return CosmosClient(*args, transport=transport, **kwargs)

    monkeypatch.setattr(cosmos, "CosmosClient", factory)
    configure_safe_telemetry()
    return transport, credential


def recipe(item_id: UUID | None = None) -> NewRecord:
    return NewRecord(
        kind=RecordKind.RECIPE,
        record_id=item_id,
        fields={"title": "synthetic private recipe"},
    )


def profile() -> NewRecord:
    return NewRecord(
        kind=RecordKind.PROFILE,
        fields={
            "groceries": [{"name": "synthetic beans", "quantity": 1}],
            "explicitPreferences": {"allergies": ["synthetic allergen"]},
            "learnedPreferences": [],
            "onboardingCompleted": False,
        },
    )


def test_real_sdk_partitioned_crud_and_conditional_delete(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, credential = harness

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            item = await repository.create(ALICE, recipe())
            item_id = str(item.body["id"])
            assert await repository.read(BOB, RECIPES, item_id) is None
            assert await repository.read(ALICE, RECIPES, str(uuid4())) is None
            loaded = await repository.read(ALICE, RECIPES, item_id)
            assert loaded is not None and loaded.etag == item.etag
            updated = await repository.patch(
                ALICE, RECIPES, item_id, RecordPatch(etag=item.etag, fields={"rating": "liked"})
            )
            assert updated.body["title"] == "synthetic private recipe"
            await repository.delete(ALICE, RECIPES, item_id, updated.etag)
            assert await repository.read(ALICE, RECIPES, item_id) is None

    asyncio.run(scenario())
    assert transport.closed and credential.closed
    assert all(
        json.loads(call.headers["x-ms-documentdb-partitionkey"])[0] in {ALICE.user_id, BOB.user_id}
        for call in transport.item_calls()
    )
    assert len(transport.item_calls("DELETE")) == 1


def test_two_real_sdk_etag_writes_conflict_without_losing_unrelated_fields(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            original = await repository.create(ALICE, profile())
            transport.block_reads = 2
            results = await asyncio.gather(
                repository.patch(
                    ALICE,
                    USERS,
                    ALICE.user_id,
                    RecordPatch(etag=original.etag, fields={"groceries": [{"name": "new beans"}]}),
                ),
                repository.patch(
                    ALICE,
                    USERS,
                    ALICE.user_id,
                    RecordPatch(etag=original.etag, fields={"learnedPreferences": ["synthetic"]}),
                ),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, CosmosError)]
            assert len(errors) == 1 and errors[0].code == CosmosFailure.CONFLICT
            latest = await repository.read(ALICE, USERS, ALICE.user_id)
            assert latest is not None
            assert latest.body["explicitPreferences"] == original.body["explicitPreferences"]
            assert latest.body["onboardingCompleted"] is False
            remaining: dict[str, JsonValue] = (
                {"groceries": [{"name": "new beans"}]}
                if latest.body["groceries"] == original.body["groceries"]
                else {"learnedPreferences": ["synthetic"]}
            )
            completed = await repository.patch(
                ALICE, USERS, ALICE.user_id, RecordPatch(etag=latest.etag, fields=remaining)
            )
            assert completed.body["groceries"] == [{"name": "new beans"}]
            assert completed.body["learnedPreferences"] == ["synthetic"]
            assert completed.body["explicitPreferences"] == original.body["explicitPreferences"]

    asyncio.run(scenario())
    assert len(transport.item_calls("PATCH")) == 3


def test_real_sdk_pagination_is_bounded_partitioned_and_has_stable_ties(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings(cosmos_page_size=2)) as repository:
            ids = [UUID(int=(4 << 76) | (2 << 62) | index) for index in range(1, 6)]
            for item_id in ids:
                await repository.create(ALICE, recipe(item_id))
            await repository.create(BOB, recipe(ids[0]))
            for body in transport.documents.values():
                body["createdAt"] = "2026-01-01T00:00:00.000000Z"
            query = HistoryQuery(kind="recipe", created_before=datetime(2026, 1, 2, tzinfo=UTC))
            first = await repository.query(ALICE, query, PageRequest())
            assert len(first.items) == 2 and first.continuation_token is not None
            second = await repository.query(
                ALICE, query, PageRequest(continuation_token=first.continuation_token)
            )
            third = await repository.query(
                ALICE, query, PageRequest(continuation_token=second.continuation_token)
            )
            assert len(second.items) == 2
            assert len(third.items) == 1 and third.continuation_token is None
            all_items = first.items + second.items + third.items
            assert [item.body["id"] for item in all_items] == [str(item) for item in reversed(ids)]
            assert all(item.body["userId"] == ALICE.user_id for item in all_items)
            empty = await repository.query(BOB, HistoryQuery(kind="conversation"), PageRequest())
            assert not empty.items and empty.continuation_token is None
            queries = [call for call in transport.item_calls("POST") if call.body.get("query")]
            assert len(queries) == 4
            assert all(call.headers["x-ms-max-item-count"] == "2" for call in queries)
            assert "c.id DESC" in queries[0].body["query"]
            assert len(queries[0].body["parameters"]) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "code", "attempts"),
    [
        (400, CosmosFailure.REJECTED, 1),
        (401, CosmosFailure.AUTHENTICATION, 1),
        (403, CosmosFailure.FORBIDDEN, 1),
        (408, CosmosFailure.TIMEOUT, 3),
        (409, CosmosFailure.EXISTS, 1),
        (412, CosmosFailure.CONFLICT, 1),
        (429, CosmosFailure.THROTTLED, 3),
        (500, CosmosFailure.UNAVAILABLE, 3),
        (503, CosmosFailure.UNAVAILABLE, 3),
        (504, CosmosFailure.TIMEOUT, 3),
    ],
)
def test_real_sdk_failure_translation_and_exact_retry_bound(
    harness: tuple[Transport, SyntheticCredential],
    status: int,
    code: CosmosFailure,
    attempts: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, credential = harness
    transport.faults["GET"].extend([status] * 12)

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            await repository.read(ALICE, RECIPES, str(uuid4()))

    with pytest.raises(CosmosError) as caught:
        asyncio.run(scenario())
    assert caught.value.code == code
    assert len(transport.item_calls("GET")) == attempts
    assert caught.value.__cause__ is None
    assert "secret-private" not in str(caught.value)
    assert "secret-private" not in caplog.text
    assert ALICE.user_id not in caplog.text
    assert transport.closed and credential.closed


def test_throttle_guidance_wait_and_remaining_deadline_are_respected(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    transport.retry_after = "30"
    transport.faults["GET"].append(429)

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            assert await repository.read(ALICE, RECIPES, str(uuid4())) is None
            transport.retry_after = "2000"
            transport.faults["GET"].append(429)
            started = time.monotonic()
            with pytest.raises(CosmosError) as caught:
                await repository.read(ALICE, RECIPES, str(uuid4()), deadline=started + 0.03)
            assert caught.value.code == CosmosFailure.THROTTLED
            assert caught.value.retry_after_seconds == 2
            assert time.monotonic() - started < 0.2

    asyncio.run(scenario())
    calls = transport.item_calls("GET")
    assert len(calls) == 3
    assert calls[1].at - calls[0].at >= 0.03


@pytest.mark.parametrize(
    "fault", [503, ServiceRequestError("private"), ServiceResponseError("private")]
)
def test_uncertain_writes_are_never_retried(
    harness: tuple[Transport, SyntheticCredential], fault: Any
) -> None:
    transport, credential = harness
    transport.faults["POST"].append(fault)

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            await repository.create(ALICE, recipe())

    with pytest.raises(CosmosError) as caught:
        asyncio.run(scenario())
    assert caught.value.outcome_unknown
    assert len(transport.item_calls("POST")) == 1
    assert transport.closed and credential.closed


def test_committed_write_with_lost_response_is_not_replayed(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    transport.write_then_disconnect = True
    item_id = uuid4()

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            await repository.create(ALICE, recipe(item_id))

    with pytest.raises(CosmosError) as caught:
        asyncio.run(scenario())
    assert caught.value.outcome_unknown
    assert (RECIPES.value, ALICE.user_id, str(item_id)) in transport.documents
    assert len(transport.item_calls("POST")) == 1


@pytest.mark.parametrize("during_setup", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("startup_delay", [0, 0.08])
def test_deadline_and_caller_cancellation_close_all_resources(
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    during_setup: bool,
    cancel: bool,
    startup_delay: float,
) -> None:
    transport, credential = harness
    original_enter = CosmosClient.__aenter__

    async def enter_after_startup(client: CosmosClient) -> CosmosClient:
        # Cold SDK initialization can block the event loop before its first I/O.
        time.sleep(startup_delay)
        return await original_enter(client)

    monkeypatch.setattr(CosmosClient, "__aenter__", enter_after_startup)
    if during_setup:
        transport.setup_fault = "hang"
    else:
        transport.faults["GET"].append("hang")

    async def scenario() -> None:
        async def operation() -> None:
            async with cosmos.open_cosmos(
                settings(cosmos_operation_timeout_seconds=0.05)
            ) as repository:
                await repository.read(ALICE, RECIPES, str(uuid4()))

        loop = asyncio.get_running_loop()
        now = loop.time()
        with monkeypatch.context() as clock:
            # Advance both deadline clocks only after the intended I/O is blocked.
            clock.setattr(loop, "time", lambda: now)
            clock.setattr(cosmos, "monotonic", lambda: now)
            task = asyncio.create_task(operation())
            started = asyncio.create_task(transport.hang_started.wait())
            try:
                await asyncio.wait((task, started), return_when=asyncio.FIRST_COMPLETED)
                if task.done():
                    await task
                    pytest.fail("The operation completed before the intended blocked I/O.")
                if cancel:
                    task.cancel()
                else:
                    now += 0.05
                with pytest.raises(asyncio.CancelledError if cancel else CosmosError) as caught:
                    await task
                if not cancel:
                    assert isinstance(caught.value, CosmosError)
                    assert caught.value.code == CosmosFailure.TIMEOUT
            finally:
                for pending in (task, started):
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(task, started, return_exceptions=True)

    previous = trace_id.get()
    asyncio.run(scenario())
    assert trace_id.get() == previous
    assert transport.cancelled and transport.closed and credential.closed


def test_real_startup_deadline_closes_resources_before_item_io(
    harness: tuple[Transport, SyntheticCredential], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, credential = harness
    original_enter = CosmosClient.__aenter__

    async def slow_enter(client: CosmosClient) -> CosmosClient:
        time.sleep(0.08)
        return await original_enter(client)

    monkeypatch.setattr(CosmosClient, "__aenter__", slow_enter)

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings(cosmos_operation_timeout_seconds=0.05)):
            pytest.fail("Startup exhausted the real deadline; no repository may be yielded.")

    with pytest.raises(CosmosError) as caught:
        asyncio.run(scenario())
    assert caught.value.code == CosmosFailure.TIMEOUT
    assert not transport.item_calls()
    assert not transport.cancelled
    assert transport.closed
    assert credential.closed


def test_missing_container_and_wrong_partition_configuration_fail_explicitly(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, credential = harness
    transport.setup_fault = 404

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()):
            pytest.fail("Initialization must not succeed.")

    with pytest.raises(CosmosError):
        asyncio.run(scenario())
    assert transport.closed and credential.closed
    transport.setup_fault = None
    transport.bad_partition = True
    with pytest.raises(ConfigurationError):
        asyncio.run(scenario())
    assert transport.closed and credential.closed


def test_foreign_record_is_hidden_before_etag_or_mutation(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            item = await repository.create(ALICE, recipe())
            item_id = str(item.body["id"])
            transport.documents[(RECIPES.value, ALICE.user_id, item_id)]["userId"] = BOB.user_id
            with pytest.raises(OwnedResourceNotFound):
                await repository.patch(
                    ALICE, RECIPES, item_id, RecordPatch(etag="wrong", fields={"rating": "liked"})
                )
            with pytest.raises(OwnedResourceNotFound):
                await repository.delete(ALICE, RECIPES, item_id, "wrong")
            with pytest.raises(OwnedResourceNotFound):
                await repository.query(ALICE, HistoryQuery(kind="recipe"), PageRequest())

    asyncio.run(scenario())
    assert not transport.item_calls("PATCH") and not transport.item_calls("DELETE")


@pytest.mark.parametrize("field", ["id", "userId", "recordKind", "ttl", "_etag", "a/b", "a~b"])
def test_metadata_and_json_pointer_override_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        NewRecord(kind=RecordKind.RECIPE, fields={field: "private"})
    with pytest.raises(ValidationError):
        RecordPatch(etag='"1"', fields={field: "private"})


@pytest.mark.parametrize("limit", [0, -1, 51, True, 1.5, "20"])
def test_invalid_page_limits_do_not_silently_expand(limit: Any) -> None:
    with pytest.raises(ValidationError):
        PageRequest(limit=limit)


def test_size_limits_and_mutated_input_are_checked_before_writes(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            with pytest.raises(CosmosError) as caught:
                await repository.create(
                    ALICE,
                    NewRecord(
                        kind=RecordKind.RECIPE, fields={"text": "é" * (MAX_RECORD_BYTES // 2)}
                    ),
                )
            assert caught.value.code == CosmosFailure.TOO_LARGE
            request = recipe()
            request.fields["ttl"] = 1
            with pytest.raises(ValidationError):
                await repository.create(ALICE, request)
            assert not transport.item_calls("POST")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (CosmosFailure.AUTHENTICATION, 503),
        (CosmosFailure.FORBIDDEN, 503),
        (CosmosFailure.CONFLICT, 412),
        (CosmosFailure.EXISTS, 409),
        (CosmosFailure.TOO_LARGE, 413),
        (CosmosFailure.THROTTLED, 429),
        (CosmosFailure.TIMEOUT, 504),
        (CosmosFailure.UNAVAILABLE, 503),
        (CosmosFailure.INVALID_RESULT, 502),
        (CosmosFailure.REJECTED, 502),
    ],
)
def test_safe_problem_mapping(code: CosmosFailure, expected: int) -> None:
    app = FastAPI()
    register_problem_handlers(app)

    @app.get("/")
    async def route() -> None:
        raise CosmosError(code, uuid4(), retry_after_seconds=0.25, outcome_unknown=True)

    with TestClient(app) as client:
        result = client.get("/")
    assert result.status_code == expected
    assert result.json()["code"] == code.value
    assert result.json()["traceId"] == result.headers["X-Trace-ID"]
    assert "Retry-After" not in result.headers
    assert "reconcile" in result.json()["detail"].lower()
    assert result.headers["Cache-Control"] == "no-store"


def test_only_verified_request_scope_reaches_real_sdk(
    harness: tuple[Transport, SyntheticCredential],
    local_settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    transport, _ = harness
    app = create_app(local_settings)
    item_id = uuid4()

    @app.post("/synthetic")
    async def create(user: CurrentUser) -> dict[str, str]:
        async with cosmos.open_cosmos(settings()) as repository:
            item = await repository.create(user, recipe(item_id))
            return {"id": str(item.body["id"]), "etag": item.etag}

    @app.get("/synthetic")
    async def read(user: CurrentUser) -> dict[str, JsonValue]:
        async with cosmos.open_cosmos(settings()) as repository:
            item = await repository.read(user, RECIPES, str(item_id))
            return user.require_owned(item.body if item is not None else None)

    @app.patch("/synthetic")
    async def patch(request: RecordPatch, user: CurrentUser) -> dict[str, str]:
        async with cosmos.open_cosmos(settings()) as repository:
            item = await repository.patch(user, RECIPES, str(item_id), request)
            return {"etag": item.etag}

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/synthetic", headers={"X-User-ID": BOB.user_id}).status_code == 401
        client.headers.update(
            {**auth_headers, "X-User-ID": BOB.user_id, "X-MS-CLIENT-PRINCIPAL-ID": BOB.user_id}
        )
        result = client.post("/synthetic", json={"userId": BOB.user_id})
        assert result.status_code == 200
        original_etag = result.json()["etag"]
        owned = client.get("/synthetic", params={"userId": BOB.user_id})
        assert owned.status_code == 200 and owned.json()["userId"] != BOB.user_id
        assert (
            client.patch(
                "/synthetic",
                json={"etag": original_etag, "fields": {"userId": BOB.user_id}},
            ).status_code
            == 422
        )
        assert (
            client.patch(
                "/synthetic", json={"etag": original_etag, "fields": {"rating": "liked"}}
            ).status_code
            == 200
        )
        conflict = client.patch(
            "/synthetic", json={"etag": original_etag, "fields": {"rating": "disliked"}}
        )
        assert conflict.status_code == 412 and conflict.json()["code"] == "revision_conflict"
        owner = owned.json()["userId"]
    assert all(
        json.loads(call.headers["x-ms-documentdb-partitionkey"]) == [owner]
        for call in transport.item_calls()
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("recordKind", []),
        ("schemaVersion", True),
        ("schemaVersion", 1.0),
        ("createdAt", "not-a-timestamp"),
        ("createdAt", "2026-01-01"),
        ("_etag", ""),
        ("_etag", "private\r\nheader"),
        ("title", float("nan")),
    ],
)
def test_malformed_service_records_fail_safely(
    harness: tuple[Transport, SyntheticCredential], name: str, value: Any
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            original = await repository.create(ALICE, recipe())
            item_id = str(original.body["id"])
            transport.documents[(RECIPES.value, ALICE.user_id, item_id)][name] = value
            with pytest.raises(CosmosError) as caught:
                await repository.read(ALICE, RECIPES, item_id)
            assert caught.value.code == CosmosFailure.INVALID_RESULT
            assert "private" not in str(caught.value)

    asyncio.run(scenario())


def test_exact_serialized_size_boundary_and_oversized_patch(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            original = await repository.create(
                ALICE, NewRecord(kind=RecordKind.RECIPE, fields={"text": ""})
            )
            room = MAX_RECORD_BYTES - cosmos._size(original.body)
            maximum = await repository.patch(
                ALICE,
                RECIPES,
                str(original.body["id"]),
                RecordPatch(etag=original.etag, fields={"text": "x" * room}),
            )
            assert cosmos._size(maximum.body) == MAX_RECORD_BYTES
            with pytest.raises(CosmosError) as caught:
                await repository.patch(
                    ALICE,
                    RECIPES,
                    str(original.body["id"]),
                    RecordPatch(etag=maximum.etag, fields={"text": "x" * (room + 1)}),
                )
            assert caught.value.code == CosmosFailure.TOO_LARGE
            assert len(transport.item_calls("PATCH")) == 1

    asyncio.run(scenario())


def test_setup_and_io_use_one_budget(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    transport.setup_delay = 0.025
    transport.faults["GET"].append("hang")

    async def scenario() -> None:
        started = time.monotonic()
        with pytest.raises(CosmosError) as caught:
            async with cosmos.open_cosmos(
                settings(cosmos_operation_timeout_seconds=0.2)
            ) as repository:
                await repository.read(ALICE, RECIPES, str(uuid4()))
        assert caught.value.code == CosmosFailure.TIMEOUT
        assert time.monotonic() - started < 0.28

    asyncio.run(scenario())


@pytest.mark.parametrize("retry_after", ["-1", "nan", "inf", "not-a-delay"])
def test_bad_retry_guidance_is_not_retried(
    harness: tuple[Transport, SyntheticCredential], retry_after: str
) -> None:
    transport, _ = harness
    transport.retry_after = retry_after
    transport.faults["GET"].append(429)

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            await repository.read(ALICE, RECIPES, str(uuid4()))

    with pytest.raises(CosmosError) as caught:
        asyncio.run(scenario())
    assert caught.value.code == CosmosFailure.INVALID_RESULT
    assert len(transport.item_calls("GET")) == 1


def test_write_throttling_is_safe_to_retry_but_stale_revision_is_not(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    transport.faults["POST"].extend([429, 429])

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            original = await repository.create(ALICE, profile())
            assert len(transport.item_calls("POST")) == 3
            with pytest.raises(ValueError, match="learned-state"):
                await repository.patch(
                    ALICE,
                    USERS,
                    ALICE.user_id,
                    RecordPatch(
                        etag=original.etag,
                        fields={"learnedPreferences": [], "explicitPreferences": {}},
                    ),
                )
            with pytest.raises(CosmosError) as caught:
                await repository.delete(ALICE, USERS, ALICE.user_id, '"stale"')
            assert caught.value.code == CosmosFailure.CONFLICT
            assert not transport.item_calls("PATCH") and not transport.item_calls("DELETE")

    asyncio.run(scenario())


def test_environment_bounds_are_readable_from_process_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASC_COSMOS_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("ASC_COSMOS_PAGE_SIZE", "50")
    monkeypatch.setenv("ASC_COSMOS_OPERATION_TIMEOUT_SECONDS", "1.5")
    configured = settings()
    assert configured.cosmos_max_attempts == 2
    assert configured.cosmos_page_size == 50
    assert configured.cosmos_operation_timeout_seconds == 1.5
    for field, value in [
        ("cosmos_max_attempts", 0),
        ("cosmos_max_attempts", 6),
        ("cosmos_page_size", 51),
        ("cosmos_operation_timeout_seconds", 6),
        ("cosmos_operation_timeout_seconds", float("nan")),
    ]:
        with pytest.raises(ValidationError):
            settings(**{field: value})


@pytest.mark.parametrize("environment", ["local", "prod"])
def test_live_smoke_rejects_other_environments_before_any_io(
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    transport, _ = harness
    monkeypatch.setenv("ASC_APPROVE_COSMOS_SMOKE", "1")
    with pytest.raises(ConfigurationError):
        asyncio.run(run_smoke(settings(environment=environment)))
    assert not transport.calls


def test_live_smoke_and_recoverable_cleanup_through_real_sdk_double(
    harness: tuple[Transport, SyntheticCredential], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, _ = harness
    configured = settings(
        environment="dev", cosmos_endpoint="https://ai-sous-chef-dev-synthetic.documents.azure.com"
    )
    with pytest.raises(ConfigurationError):
        asyncio.run(run_smoke(configured))
    assert not transport.calls
    monkeypatch.setenv("ASC_APPROVE_COSMOS_SMOKE", "1")
    run_id = asyncio.run(run_smoke(configured))
    assert not transport.documents
    asyncio.run(cleanup_smoke(configured, run_id))
    assert not transport.documents
    transport.faults["DELETE"].append(403)
    with pytest.raises(CosmosSmokeError, match="Cleanup incomplete"):
        asyncio.run(run_smoke(configured))
    assert len(transport.documents) == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_query_sdk_layers_do_not_multiply_retries(
    harness: tuple[Transport, SyntheticCredential], status: int
) -> None:
    transport, _ = harness
    transport.faults["POST"].extend([status] * 12)

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            await repository.query(ALICE, HistoryQuery(kind="recipe"), PageRequest())

    with pytest.raises(CosmosError):
        asyncio.run(scenario())
    assert len(transport.item_calls("POST")) == 3


def test_empty_service_pages_are_followed_without_losing_the_continuation(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            for _ in range(3):
                await repository.create(ALICE, recipe())
            transport.empty_query_pages = 1
            query = HistoryQuery(kind="recipe")
            first = await repository.query(ALICE, query, PageRequest(limit=2))
            assert len(first.items) == 2 and first.continuation_token is not None
            second = await repository.query(
                ALICE,
                query,
                PageRequest(limit=2, continuation_token=first.continuation_token),
            )
            assert len(second.items) == 1 and second.continuation_token is None

    asyncio.run(scenario())


@pytest.mark.parametrize("write", [False, True])
def test_invalid_http_json_is_a_safe_distinct_failure(
    harness: tuple[Transport, SyntheticCredential],
    caplog: pytest.LogCaptureFixture,
    write: bool,
) -> None:
    transport, credential = harness

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            if write:
                transport.malformed_json = True
                await repository.create(ALICE, recipe())
            else:
                original = await repository.create(ALICE, recipe())
                transport.malformed_json = True
                await repository.read(ALICE, RECIPES, str(original.body["id"]))

    with pytest.raises(CosmosError) as caught:
        asyncio.run(scenario())
    assert caught.value.code == CosmosFailure.INVALID_RESULT
    assert caught.value.outcome_unknown == write
    assert "secret-private" not in str(caught.value)
    assert "secret-private" not in caplog.text
    assert len(transport.item_calls("POST")) == 1
    assert len(transport.documents) == 1
    assert transport.closed and credential.closed


@pytest.mark.parametrize("http_date", [False, True])
def test_standard_retry_after_guidance_is_preserved(
    harness: tuple[Transport, SyntheticCredential], http_date: bool
) -> None:
    transport, _ = harness
    value = (
        format_datetime(datetime.now(UTC) + timedelta(seconds=3), usegmt=True) if http_date else "3"
    )
    transport.fault_headers = {"Retry-After": value}
    transport.faults["GET"].append(429)

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            await repository.read(ALICE, RECIPES, str(uuid4()), deadline=time.monotonic() + 0.02)

    with pytest.raises(CosmosError) as caught:
        asyncio.run(scenario())
    assert caught.value.code == CosmosFailure.THROTTLED
    assert caught.value.retry_after_seconds is not None
    assert 0 < caught.value.retry_after_seconds <= 3
    assert len(transport.item_calls("GET")) == 1


@pytest.mark.parametrize(
    "token", ["private\r\n", "x" * (16 * 1024 + 1)], ids=["header-injection", "oversized"]
)
def test_malformed_returned_tokens_fail_safely(
    harness: tuple[Transport, SyntheticCredential], token: str
) -> None:
    transport, _ = harness
    transport.query_continuation = token

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()) as repository:
            await repository.create(ALICE, recipe())
            await repository.query(ALICE, HistoryQuery(kind="recipe"), PageRequest())

    with pytest.raises(CosmosError) as caught:
        asyncio.run(scenario())
    assert caught.value.code == CosmosFailure.INVALID_RESULT
    assert token not in str(caught.value)
    assert len(transport.item_calls("POST")) == 2


@pytest.mark.parametrize(
    "properties",
    [
        {"partitionKey": {"paths": ["/userId"], "kind": "Hash", "version": 1}},
        {"partitionKey": None},
        {"defaultTtl": -1},
        {"defaultTtl": 600},
    ],
)
def test_incompatible_container_metadata_fails_before_item_access(
    harness: tuple[Transport, SyntheticCredential], properties: dict[str, Any]
) -> None:
    transport, credential = harness
    transport.container_overrides = properties

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings()):
            pytest.fail("An incompatible container cannot be used.")

    with pytest.raises(ConfigurationError, match="Hash v2 and no TTL"):
        asyncio.run(scenario())
    assert not transport.item_calls()
    assert transport.closed and credential.closed


def test_already_expired_deadline_closes_without_network_io(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, credential = harness

    async def scenario() -> None:
        async with cosmos.open_cosmos(settings(), deadline=time.monotonic() - 1):
            pytest.fail("An expired unit of work cannot start.")

    with pytest.raises(CosmosError) as caught:
        asyncio.run(scenario())
    assert caught.value.code == CosmosFailure.TIMEOUT
    assert not transport.calls
    assert transport.closed and credential.closed
