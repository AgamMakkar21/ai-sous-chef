import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from time import time
from typing import Any, Literal
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sous_chef.adapters import cosmos
from sous_chef.app import create_app
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.conversations import HistoryParameters
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    NewRecord,
    ProductContainer,
    RecordKind,
)
from sous_chef.services import conversations
from sous_chef.settings import ConfigurationError, Settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_cosmos_repository import settings as live_settings

ISSUER = "https://synthetic.ciamlogin.com/11111111-1111-1111-1111-111111111111/v2.0"
OWNER = AuthenticatedUser.from_verified_identity(ISSUER, "synthetic-user")
OTHER = AuthenticatedUser.from_verified_identity(ISSUER, "other-user")
CHAT = ProductContainer.CHAT_HISTORY
BASE = "/api/v1/conversations"
TIED_TIME = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def conversation_settings(local_settings: Settings) -> Settings:
    return live_settings(
        **{
            **local_settings.model_dump(),
            "adapter_mode": "live",
            "cosmos_endpoint": "https://cosmos.example.invalid",
            "cosmos_database": "sous-chef",
            "foundry_project_endpoint": "https://project.example.invalid/api/projects/test",
            "foundry_orchestrator_deployment": "synthetic",
            "foundry_recipe_nutrition_deployment": "synthetic",
            "foundry_extraction_deployment": "synthetic",
            "pagination_key": Fernet.generate_key().decode(),
        }
    )


@pytest.fixture
def api(
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    timestamp = cosmos._timestamp
    monkeypatch.setattr(cosmos, "_timestamp", lambda value=None: timestamp(value or TIED_TIME))
    with TestClient(create_app(conversation_settings), headers=auth_headers) as client:
        yield client


def create(client: TestClient, title: str = "Private conversation", key: str | None = None) -> str:
    response = client.post(
        BASE, json={"title": title}, headers={"Idempotency-Key": key or str(uuid4())}
    )
    assert response.status_code == 201, response.text
    assert response.headers["etag"]
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert UUID(data["conversationId"]).version == 4
    assert set(data) == {"conversationId", "title", "createdAt", "updatedAt"}
    assert response.headers["location"] == f"{BASE}/{data['conversationId']}/turns"
    return str(data["conversationId"])


def seed_turns(
    config: Settings,
    user: AuthenticatedUser,
    conversation_id: str,
    indices: tuple[int, ...],
    status: Literal["pending", "completed", "failed", "outcomeUnknown"] = "completed",
) -> list[str]:
    async def scenario() -> list[str]:
        ids = []
        async with cosmos.open_cosmos(config) as repository:
            for index in indices:
                messages: list[Any] = [
                    {
                        "messageId": str(uuid4()),
                        "role": "user",
                        "entityId": user.user_id,
                        "content": f"Private question {conversation_id}/{index}",
                        "timestamp": cosmos._timestamp(),
                    }
                ]
                if status == "completed":
                    messages.append(
                        {
                            "messageId": str(uuid4()),
                            "role": "assistant",
                            "entityId": "synthetic-managed-agent",
                            "content": f"Private answer {conversation_id}/{index}",
                            "timestamp": cosmos._timestamp(),
                        }
                    )
                record = await repository.create(
                    user,
                    NewRecord(
                        kind=RecordKind.TURN,
                        fields={
                            "conversationId": conversation_id,
                            "turnIndex": index,
                            "status": status,
                            "messages": messages,
                            "currentOperationId": str(uuid4()),
                            "providerResponseId": "private-provider-id",
                            "internalPrompt": "private-hidden-prompt",
                        },
                    ),
                )
                ids.append(str(record.body["id"]))
        return ids

    return asyncio.run(scenario())


def pages(client: TestClient, path: str, limit: int = 2) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    parameters: dict[str, Any] = {"limit": limit}
    while True:
        response = client.get(path, params=parameters)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        result = response.json()
        assert set(result) == {"items", "nextCursor"}
        assert len(result["items"]) <= limit
        items.extend(result["items"])
        if result["nextCursor"] is None:
            break
        # A subsequent page can omit the limit; the cursor retains it.
        parameters = {"cursor": result["nextCursor"]}
    return items


def test_creation_is_owned_idempotent_and_has_no_transcript(
    api: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, credential = harness
    key = str(uuid4())
    first = create(api, "  Private title  ", key)
    second = create(api, "Private title", key)
    assert first == second
    records = [
        document
        for (name, owner, _), document in transport.documents.items()
        if name == CHAT.value and owner == OWNER.user_id
    ]
    assert {document["recordKind"] for document in records} == {
        "conversation",
        "conversationCreation",
    }
    assert len(records) == 2
    conversation = transport.documents[(CHAT.value, OWNER.user_id, first)]
    assert conversation["nextTurnIndex"] == 0
    assert conversation["activeOperationId"] is None
    assert conversation["title"] == "Private title"
    listed = api.get(BASE).json()["items"][0]
    assert listed["createdAt"] == conversation["createdAt"] == "2026-01-01T00:00:00.000000Z"
    assert listed["updatedAt"] == conversation["updatedAt"]
    assert "messages" not in conversation
    assert not any(name == "Users" for name, _, _ in transport.documents)
    assert api.get(f"{BASE}/{first}/turns").json() == {"items": [], "nextCursor": None}
    conflict = api.post(BASE, json={"title": "Different"}, headers={"Idempotency-Key": key})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
    assert len(transport.documents) == 2
    assert transport.closed and credential.closed
    for private in (key, "Private title", OWNER.user_id):
        assert private not in caplog.text


def test_concurrent_creation_and_ambiguous_write_replay(
    api: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    transport.block_reads = 2
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create(api, key=key), range(2)))
    assert results[0] == results[1]
    assert len(transport.documents) == 2
    transport.block_reads = 0
    transport.write_then_disconnect = True
    key = str(uuid4())
    response = api.post(BASE, json={}, headers={"Idempotency-Key": key})
    assert response.status_code == 503
    assert "unknown" in response.json()["detail"]
    batches = [call for call in transport.calls if isinstance(call.body, list)]
    assert len(batches) == 3
    replay = api.post(BASE, json={}, headers={"Idempotency-Key": key})
    assert replay.status_code == 201
    assert len(transport.documents) == 4
    assert len([call for call in transport.calls if isinstance(call.body, list)]) == len(batches)


def test_independent_histories_stable_turn_order_and_private_projection(
    api: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, _ = harness
    first = create(api, "First")
    ids = seed_turns(conversation_settings, OWNER, first, (2, 0, 1, 1))
    second = create(api, "Second")
    assert pages(api, f"{BASE}/{second}/turns") == []
    other_ids = seed_turns(conversation_settings, OWNER, second, (0,))
    records = pages(api, f"{BASE}/{first}/turns")
    expected = sorted(
        ids,
        key=lambda item_id: (
            transport.documents[(CHAT.value, OWNER.user_id, item_id)]["turnIndex"],
            item_id,
        ),
    )
    assert [record["turnId"] for record in records] == expected
    assert len(set(record["turnId"] for record in records)) == len(ids)
    assert all(record["conversationId"] == first for record in records)
    assert [record["turnId"] for record in pages(api, f"{BASE}/{second}/turns")] == other_ids
    for record in records:
        assert [message["role"] for message in record["messages"]] == ["user", "assistant"]
        assert "providerResponseId" not in record
        assert "currentOperationId" not in record
        assert "internalPrompt" not in record
        assert "_etag" not in record
        assert "userId" not in record
    assert "Private question" not in caplog.text
    assert "Private answer" not in caplog.text
    assert "private-hidden-prompt" not in caplog.text


@pytest.mark.parametrize("status", ["pending", "failed", "outcomeUnknown"])
def test_incomplete_turns_have_no_claimed_assistant_success(
    api: TestClient,
    conversation_settings: Settings,
    status: Literal["pending", "failed", "outcomeUnknown"],
) -> None:
    conversation_id = create(api)
    seed_turns(conversation_settings, OWNER, conversation_id, (0,), status)
    result = api.get(f"{BASE}/{conversation_id}/turns")
    assert result.status_code == 200
    assert result.json()["items"][0]["status"] == status
    assert [message["role"] for message in result.json()["items"][0]["messages"]] == ["user"]


def test_conversation_pagination_keeps_ties_and_first_upper_bound(
    api: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _ = harness
    ids = [create(api, f"Conversation {index}") for index in range(5)]
    seed_turns(conversation_settings, OWNER, ids[0], (0, 1, 2))
    response = api.get(BASE, params={"limit": 2})
    assert response.status_code == 200
    first = response.json()
    cursor = first["nextCursor"]
    assert cursor and OWNER.user_id not in cursor and ids[0] not in cursor
    assert conversation_settings.pagination_key is not None
    state = json.loads(
        Fernet(conversation_settings.pagination_key.get_secret_value()).decrypt(cursor)
    )
    first_upper = state["createdBefore"]
    timestamp = cosmos._timestamp
    monkeypatch.setattr(
        cosmos,
        "_timestamp",
        lambda value=None: timestamp(value or datetime.now(UTC) + timedelta(minutes=1)),
    )
    new_id = create(api, "Added after traversal began")
    transport.documents[(CHAT.value, OWNER.user_id, ids[0])]["title"] = "Renamed"
    rest = []
    while cursor is not None:
        page = api.get(BASE, params={"cursor": cursor}).json()
        rest.extend(page["items"])
        cursor = page["nextCursor"]
    assert [item["conversationId"] for item in first["items"] + rest] == sorted(ids, reverse=True)
    assert new_id not in {item["conversationId"] for item in first["items"] + rest}
    queries = [
        call for call in transport.calls if isinstance(call.body, dict) and call.body.get("query")
    ]
    conversation_queries = [
        call for call in queries if call.body["parameters"][0]["value"] == "conversation"
    ]
    assert conversation_queries
    assert all(
        json.loads(call.headers["x-ms-documentdb-partitionkey"]) == [OWNER.user_id]
        for call in queries
    )
    assert all(call.body["parameters"][1]["value"] == first_upper for call in conversation_queries)
    assert all("c.recordKind = @kind" in call.body["query"] for call in conversation_queries)
    assert all(call.headers["x-ms-max-item-count"] == "2" for call in conversation_queries)
    assert all(
        set(item) == {"conversationId", "title", "createdAt", "updatedAt"}
        for item in first["items"] + rest
    )


def test_missing_foreign_and_corrupt_ownership_share_safe_404(
    api: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    owned = create(api)
    record = transport.documents[(CHAT.value, OWNER.user_id, owned)]
    foreign_id, corrupt_id, missing_id = (str(uuid4()) for _ in range(3))
    foreign = {**record, "id": foreign_id, "conversationId": foreign_id, "userId": OTHER.user_id}
    transport.documents[(CHAT.value, OTHER.user_id, foreign_id)] = foreign
    transport.documents[(CHAT.value, OWNER.user_id, corrupt_id)] = {
        **foreign,
        "id": corrupt_id,
        "conversationId": corrupt_id,
    }
    for item_id in (foreign_id, corrupt_id, missing_id):
        response = api.get(f"{BASE}/{item_id}/turns")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["traceId"] == response.headers["x-trace-id"]
        assert OTHER.user_id not in response.text
        assert foreign_id not in response.text
    assert all(
        json.loads(call.headers["x-ms-documentdb-partitionkey"]) == [OWNER.user_id]
        for call in transport.item_calls()
    )


@pytest.mark.parametrize(
    "field", ["userId", "conversationId", "createdAt", "messages", "role", "nextTurnIndex"]
)
def test_client_owned_mutation_fields_are_rejected(api: TestClient, field: str) -> None:
    response = api.post(
        BASE, json={field: "private-forged-value"}, headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_input"
    assert "private-forged-value" not in response.text


@pytest.mark.parametrize(
    "query",
    [
        "userId=forged",
        "conversationId=forged",
        "limit=0",
        "limit=51",
        "limit=true",
        "limit=1&limit=2",
    ],
)
def test_invalid_query_and_client_scope_are_rejected(api: TestClient, query: str) -> None:
    response = api.get(BASE + "?" + query)
    assert response.status_code == 422


def test_untrusted_identity_hints_cannot_change_the_scope(
    api: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    item_id = create(api)
    response = api.get(
        BASE,
        headers={
            "X-User-ID": OTHER.user_id,
            "X-MS-CLIENT-PRINCIPAL-ID": OTHER.user_id,
            "X-MS-CLIENT-PRINCIPAL": "forged",
        },
    )
    assert response.status_code == 200
    assert [item["conversationId"] for item in response.json()["items"]] == [item_id]
    assert json.loads(transport.item_calls()[-1].headers["x-ms-documentdb-partitionkey"]) == [
        OWNER.user_id
    ]


def test_cursors_are_authenticated_bound_and_expire_without_refreshing(
    api: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _ = harness
    ids = [create(api) for _ in range(4)]
    seed_turns(conversation_settings, OWNER, ids[0], (0, 1, 2))
    seed_turns(conversation_settings, OWNER, ids[1], (0, 1))
    now = int(time())
    monkeypatch.setattr(conversations, "time", lambda: now)
    cursor = api.get(BASE, params={"limit": 1}).json()["nextCursor"]
    turns_cursor = api.get(f"{BASE}/{ids[0]}/turns", params={"limit": 1}).json()["nextCursor"]
    assert conversation_settings.pagination_key is not None
    cipher = Fernet(conversation_settings.pagination_key.get_secret_value())
    decoded = json.loads(cipher.decrypt(cursor))
    decoded["userId"] = OTHER.user_id
    wrong_user = cipher.encrypt(json.dumps(decoded).encode()).decode()
    invalid = [
        (BASE, "", None),
        (BASE, "not-a-token", None),
        (BASE, cursor[:20] + ("B" if cursor[20] == "A" else "A") + cursor[21:], None),
        (BASE, wrong_user, None),
        (BASE, cursor, 2),
        (BASE, turns_cursor, None),
        (f"{BASE}/{ids[1]}/turns", turns_cursor, None),
    ]
    for path, token, limit in invalid:
        before = len(transport.calls)
        parameters = {"cursor": token}
        if limit is not None:
            parameters["limit"] = str(limit)
        response = api.get(path, params=parameters)
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "invalid_cursor"
        assert len(transport.calls) == before
        if token:
            assert token not in response.text
    now += 3600
    next_cursor = api.get(BASE, params={"cursor": cursor}).json()["nextCursor"]
    assert cipher.extract_timestamp(cursor.encode()) == cipher.extract_timestamp(
        next_cursor.encode()
    )
    now += 86400
    before = len(transport.calls)
    expired = api.get(BASE, params={"cursor": next_cursor})
    assert expired.status_code == 400
    assert len(transport.calls) == before


@pytest.mark.parametrize("title", ["", "   ", "a" * 101, 123])
def test_invalid_titles_and_safe_validation(api: TestClient, title: object) -> None:
    response = api.post(BASE, json={"title": title}, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 422
    assert response.json()["errors"] == []


def test_bad_storage_does_not_become_empty_history_or_leak_content(
    api: TestClient, harness: tuple[Transport, SyntheticCredential], conversation_settings: Settings
) -> None:
    transport, _ = harness
    item_id = create(api)
    turn_id = seed_turns(conversation_settings, OWNER, item_id, (0,), status="pending")[0]
    turn = transport.documents[(CHAT.value, OWNER.user_id, turn_id)]
    turn["messages"].append(
        {**turn["messages"][0], "role": "assistant", "content": "private-invalid-answer"}
    )
    response = api.get(f"{BASE}/{item_id}/turns")
    assert response.status_code == 502
    assert response.json()["code"] == "cosmos_invalid_result"
    assert "private-invalid-answer" not in response.text
    transport.faults["POST"].append(403)
    response = api.get(BASE)
    assert response.status_code == 503
    assert response.json()["code"] == "cosmos_forbidden"


def test_request_body_bound_missing_keys_and_anonymous_access(
    api: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, _ = harness
    for content in (b'{"title":' + b" " * (128 * 1024), b'{"title":not-json}'):
        response = api.post(
            BASE,
            content=content,
            headers={"Content-Type": "application/json", "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == (413 if len(content) > 128 * 1024 else 400)
    response = api.post(BASE, json={})
    assert response.status_code == 422
    before = len(transport.calls)
    response = api.get(BASE, headers={"Authorization": ""})
    assert response.status_code == 401
    assert len(transport.calls) == before


def test_configuration_is_explicit_without_fake_history(
    local_settings: Settings, auth_headers: dict[str, str]
) -> None:
    with TestClient(create_app(local_settings), headers=auth_headers) as client:
        response = client.get(BASE)
    assert response.status_code == 503
    assert response.json()["code"] == "configuration_unavailable"
    assert "PAGINATION_KEY" not in response.text
    with pytest.raises(ConfigurationError, match="PAGINATION_KEY"):
        Settings(environment="local", adapter_mode="local", pagination_key="private-not-a-key")


def test_two_signed_users_reuse_creation_keys_without_sharing_history(
    api: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    transport, _ = harness
    key = str(uuid4())
    first = create(api, "First user's private title", key)
    claims = jwt.decode(
        api.headers["Authorization"].removeprefix("Bearer "),
        options={"verify_signature": False},
    )
    claims["sub"] = "other-user"
    other_headers = {
        "Authorization": "Bearer "
        + jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "synthetic"}),
        "Idempotency-Key": key,
    }
    response = api.post(BASE, json={"title": "Second user's private title"}, headers=other_headers)
    assert response.status_code == 201
    second = response.json()["data"]["conversationId"]
    assert first != second
    listed = api.get(BASE, headers=other_headers)
    assert listed.status_code == 200
    assert [item["conversationId"] for item in listed.json()["items"]] == [second]
    assert "First user's private title" not in listed.text
    assert api.get(f"{BASE}/{first}/turns", headers=other_headers).status_code == 404
    assert api.get(f"{BASE}/{second}/turns").status_code == 404
    assert (CHAT.value, OTHER.user_id, second) in transport.documents
    assert (CHAT.value, OWNER.user_id, first) in transport.documents


def test_cursor_rotation_does_not_break_durable_creation(
    api: TestClient,
    conversation_settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    key = str(uuid4())
    original = create(api, key=key)
    create(api)
    cursor = api.get(BASE, params={"limit": 1}).json()["nextCursor"]
    rotated = conversation_settings.model_copy(
        update={"pagination_key": SecretStr(Fernet.generate_key().decode())}
    )
    with TestClient(create_app(rotated), headers=auth_headers) as restarted:
        assert create(restarted, key=key) == original
        assert restarted.get(BASE, params={"cursor": cursor}).status_code == 400


def test_batch_rejection_is_atomic_and_has_bounded_safe_throttle_retry(
    api: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    transport, credential = harness
    transport.faults["POST"].append(403)
    response = api.post(BASE, json={}, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 503
    assert response.json()["code"] == "cosmos_forbidden"
    assert not transport.documents
    assert transport.closed and credential.closed
    transport.retry_after = "0"
    transport.faults["POST"].extend([429, 429])
    create(api)
    assert len(transport.documents) == 2
    batches = [call for call in transport.calls if isinstance(call.body, list)]
    assert len(batches) == 4


def test_turn_pagination_excludes_later_appends(
    api: TestClient,
    conversation_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id = create(api)
    original_ids = seed_turns(conversation_settings, OWNER, item_id, (0, 1, 2))
    first = api.get(f"{BASE}/{item_id}/turns", params={"limit": 1}).json()
    timestamp = cosmos._timestamp
    monkeypatch.setattr(
        cosmos,
        "_timestamp",
        lambda value=None: timestamp(value or datetime.now(UTC) + timedelta(minutes=1)),
    )
    seed_turns(conversation_settings, OWNER, item_id, (3,))
    ids = [item["turnId"] for item in first["items"]]
    cursor = first["nextCursor"]
    while cursor is not None:
        response = api.get(f"{BASE}/{item_id}/turns", params={"cursor": cursor})
        assert response.status_code == 200
        page = response.json()
        ids.extend(item["turnId"] for item in page["items"])
        cursor = page["nextCursor"]
    assert ids == original_ids


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_cookie_mutations_still_require_origin_and_csrf(
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    auth_headers: dict[str, str],
    environment: Literal["dev", "prod"],
) -> None:
    hosted = conversation_settings.model_copy(update={"environment": environment})
    headers = {
        "X-MS-TOKEN-AAD-ID-TOKEN": auth_headers["Authorization"].removeprefix("Bearer "),
        "Idempotency-Key": str(uuid4()),
    }
    with TestClient(create_app(hosted), base_url="https://testserver", headers=headers) as client:
        assert client.post(BASE, json={}).status_code == 403
        session = client.get("/auth/session")
        assert session.status_code == 200
        client.headers["X-CSRF-Token"] = session.json()["csrfToken"]
        client.headers["Origin"] = "https://wrong-origin.invalid"
        assert client.post(BASE, json={}).status_code == 403
        client.headers["Origin"] = "https://testserver"
        assert client.post(BASE, json={}).status_code == 201


def test_chunked_oversize_body_is_rejected_without_a_content_length(
    api: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"title":"'
        yield b"x" * (128 * 1024)
        yield b'"}'

    before = len(harness[0].calls)
    response = api.post(
        BASE,
        content=chunks(),
        headers={"Content-Type": "application/json", "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "body_too_large"
    assert len(harness[0].calls) == before


@pytest.mark.parametrize("length", ["not-a-number", "-1", "9" * 5000])
def test_malformed_request_length_fails_before_storage(
    api: TestClient, harness: tuple[Transport, SyntheticCredential], length: str
) -> None:
    before = len(harness[0].calls)
    response = api.post(
        BASE, json={}, headers={"Content-Length": length, "Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_input"
    assert len(harness[0].calls) == before


def test_default_and_maximum_page_sizes_are_explicit(
    api: TestClient, harness: tuple[Transport, SyntheticCredential]
) -> None:
    create(api)
    for parameters, expected in (({}, 20), ({"limit": 50}, 50)):
        response = api.get(BASE, params=parameters)
        assert response.status_code == 200
        query = harness[0].item_calls("POST")[-1]
        assert query.headers["x-ms-max-item-count"] == str(expected)


def test_cursor_that_would_exceed_accepted_input_bound_fails_explicitly(
    conversation_settings: Settings,
) -> None:
    service = conversations.ConversationService(conversation_settings)
    state, issued_at = service._page(OWNER, HistoryParameters(), "conversation", None)
    with pytest.raises(CosmosError) as failure:
        service._cursor(state, "\\" * (16 * 1024), issued_at)
    assert failure.value.code == CosmosFailure.INVALID_RESULT


@pytest.mark.parametrize("corruption", ["missing-resource", "invalid-kind", "invalid-hash"])
def test_damaged_creation_receipt_never_starts_another_creation(
    api: TestClient, harness: tuple[Transport, SyntheticCredential], corruption: str
) -> None:
    transport, _ = harness
    key = str(uuid4())
    create(api, key=key)
    receipt = next(
        document
        for document in transport.documents.values()
        if document["recordKind"] == "conversationCreation"
    )
    if corruption == "missing-resource":
        receipt["resourceId"] = str(uuid4())
    elif corruption == "invalid-kind":
        receipt["recordKind"] = "sendOperation"
    else:
        receipt["requestHash"] = "private-malformed-hash"
    batches = len([call for call in transport.calls if isinstance(call.body, list)])
    response = api.post(
        BASE, json={"title": "Private conversation"}, headers={"Idempotency-Key": key}
    )
    assert response.status_code == (404 if corruption == "missing-resource" else 502)
    assert "private-malformed-hash" not in response.text
    assert len(transport.documents) == 2
    assert len([call for call in transport.calls if isinstance(call.body, list)]) == batches
