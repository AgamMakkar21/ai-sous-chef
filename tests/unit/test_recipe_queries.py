import json
from copy import deepcopy
from datetime import UTC, datetime
from time import time
from typing import Any, Literal
from uuid import uuid4

import pytest
from azure.core.pipeline.transport import AsyncHttpResponse, HttpRequest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sous_chef.services import pagination
from sous_chef.settings import Settings
from test_chat_api import FakeExecutor, documents
from test_chat_api import chat_api as chat_api
from test_chat_api import executor as executor
from test_conversation_api import OWNER, create
from test_conversation_api import conversation_settings as conversation_settings
from test_cosmos_repository import Response, SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_recipe_lifecycle import RECIPES, accept, generate

Rating = Literal["liked", "fine", "disliked"]
STAMP = "2026-01-01T00:00:00.000000Z"
NOTE = "Private stored cooking note."


def seed_history(
    transport: Transport,
    template: dict[str, Any],
    *,
    cooked: bool = True,
    rating: Rating | None = None,
) -> dict[str, Any]:
    """Seed complete historical records; feedback writes belong to a later API."""
    history_id = str(uuid4())
    record = {
        **deepcopy(template),
        "id": history_id,
        "historyId": history_id,
        "generatedRecipeId": str(uuid4()),
        "occasionId": str(uuid4()),
        "createdAt": STAMP,
        "cookedAt": template["savedAt"] if cooked else None,
        "rating": rating,
        "note": NOTE if rating else None,
        "feedbackRevision": 1 if rating else 0,
        "processingStatus": "completed" if rating else "notEligible",
    }
    transport.documents[("RecipeHistory", record["userId"], history_id)] = record
    return record


@pytest.fixture
def histories(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> list[dict[str, Any]]:
    source = generate(chat_api, executor)
    saved = accept(chat_api, source["generatedRecipeId"])
    assert saved.status_code == 201
    transport = harness[0]
    template = documents(transport, "recipe")[0]
    template["createdAt"] = STAMP
    return [
        template,
        *(seed_history(transport, template) for _ in range(3)),
        *(
            seed_history(transport, template, rating=rating)
            for rating in ("liked", "fine", "disliked")
        ),
    ]


@pytest.mark.parametrize("view", [None, "awaitingFeedback", "rated"])
def test_empty_history_is_not_a_missing_cursor_error(
    chat_api: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    executor: FakeExecutor,
    view: str | None,
) -> None:
    before = deepcopy(harness[0].documents)
    response = chat_api.get(RECIPES, params={view: "true"} if view else {})
    assert response.status_code == 200
    assert response.json() == {"items": [], "nextCursor": None}
    assert response.headers["cache-control"] == "no-store"
    assert harness[0].documents == before and not executor.calls


@pytest.mark.parametrize("view", [None, "awaitingFeedback", "rated"])
def test_owned_lists_are_concise_filtered_bounded_and_stable_across_tied_pages(
    chat_api: TestClient,
    histories: list[dict[str, Any]],
    harness: tuple[Transport, SyntheticCredential],
    executor: FakeExecutor,
    view: str | None,
) -> None:
    transport = harness[0]
    expected = [
        record["id"]
        for record in sorted(histories, key=lambda record: record["id"], reverse=True)
        if view is None
        or view == "awaitingFeedback"
        and record["cookedAt"] is not None
        and record["rating"] is None
        or view == "rated"
        and record["rating"] is not None
    ]
    before, calls = deepcopy(transport.documents), len(executor.calls)
    since = len(transport.item_calls())
    params = {view: "true"} if view else {}
    cursor = None
    actual = []
    for _ in range(5):
        response = chat_api.get(
            RECIPES, params={**params, **({"cursor": cursor} if cursor else {"limit": "2"})}
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        page = response.json()
        assert 1 <= len(page["items"]) <= 2
        for summary in page["items"]:
            record = next(record for record in histories if record["id"] == summary["historyId"])
            assert summary == {
                "historyId": record["id"],
                "title": record["recipe"]["title"],
                "savedAt": record["savedAt"],
                "cookedAt": record["cookedAt"],
                "state": "cooked" if record["cookedAt"] else "planned",
                "awaitingFeedback": record["cookedAt"] is not None and record["rating"] is None,
                "rating": record["rating"],
            }
            actual.append(summary["historyId"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
        assert OWNER.user_id not in cursor and NOTE not in response.text
    else:
        pytest.fail("Recipe pagination did not terminate within the seeded record count.")
    assert actual == expected and len(actual) == len(set(actual))
    assert transport.documents == before and len(executor.calls) == calls
    reads = transport.item_calls()[since:]
    assert all(
        call.method == "POST"
        or call.method == "GET"
        and call.path.endswith(f"/Users/docs/{OWNER.user_id}")
        for call in reads
    )
    queries = [call for call in reads if call.method == "POST"]
    assert queries and all(call.method == "POST" and call.body.get("query") for call in queries)
    for call in queries:
        assert "/RecipeHistory/docs" in call.path
        assert json.loads(call.headers["x-ms-documentdb-partitionkey"]) == [OWNER.user_id]
        assert call.headers.get("x-ms-documentdb-query-enablecrosspartition") != "true"
        assert call.headers["x-ms-max-item-count"] == "2"
        assert call.body["query"].endswith("ORDER BY c.recordKind ASC, c.createdAt DESC, c.id DESC")


def test_detail_returns_only_the_stored_snapshot_even_after_source_and_profile_change(
    chat_api: TestClient,
    histories: list[dict[str, Any]],
    harness: tuple[Transport, SyntheticCredential],
    executor: FakeExecutor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = harness[0]
    record = histories[-1]
    expected_recipe = deepcopy(record["recipe"])
    documents(transport, "turn")[0]["recipe"]["title"] = "Changed generated source"
    profile = documents(transport, "profile")[0]
    profile["explicitPreferences"]["dislikes"] = ["rice"]
    profile["onboardingCompleted"] = False
    before, calls = deepcopy(transport.documents), len(executor.calls)
    since = len(transport.item_calls())
    response = chat_api.get(f"{RECIPES}/{record['id']}")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["recipe"] == expected_recipe
    assert data["recipe"]["nutrition"] == expected_recipe["nutrition"]
    assert data["note"] == NOTE and data["rating"] == record["rating"]
    assert data["savedAt"] == record["savedAt"] and data["cookedAt"] == record["cookedAt"]
    assert data["state"] == "cooked" and data["awaitingFeedback"] is False
    assert data["feedbackRevision"] == 1 and data["processingStatus"] == "completed"
    assert response.headers["etag"] == record["_etag"]
    assert response.headers["cache-control"] == "no-store"
    for private in (
        "userId",
        "_etag",
        "safetyProfileRevision",
        "safetyPolicyVersion",
        "recordKind",
    ):
        assert private not in data
    assert transport.documents == before and len(executor.calls) == calls
    reads = transport.item_calls()[since:]
    assert len(reads) == 2 and all(call.method == "GET" for call in reads)
    assert reads[0].path.endswith(f"/Users/docs/{OWNER.user_id}")
    assert reads[1].path.endswith(f"/RecipeHistory/docs/{record['id']}")
    assert NOTE not in caplog.text and expected_recipe["title"] not in caplog.text


def test_planned_detail_matches_acceptance_location_without_becoming_feedback_ready(
    chat_api: TestClient, histories: list[dict[str, Any]]
) -> None:
    record = histories[0]
    response = chat_api.get(f"{RECIPES}/{record['id']}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "planned" and data["awaitingFeedback"] is False
    assert data["cookedAt"] is None and data["rating"] is None
    assert data["recipe"] == record["recipe"]


def test_new_records_wait_for_refresh_and_filtered_updates_do_not_shift_continuation(
    chat_api: TestClient,
    histories: list[dict[str, Any]],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport = harness[0]
    pending = sorted(histories[1:4], key=lambda record: record["id"], reverse=True)
    first = chat_api.get(RECIPES, params={"awaitingFeedback": "true", "limit": 1}).json()
    assert first["items"][0]["historyId"] == pending[0]["id"]
    pending[0].update(rating="liked", feedbackRevision=1, note=NOTE, processingStatus="pending")
    added = seed_history(transport, histories[0])
    added["createdAt"] = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    second = chat_api.get(
        RECIPES, params={"awaitingFeedback": "true", "cursor": first["nextCursor"]}
    ).json()
    third = chat_api.get(
        RECIPES, params={"awaitingFeedback": "true", "cursor": second["nextCursor"]}
    ).json()
    assert [page["items"][0]["historyId"] for page in (second, third)] == [
        record["id"] for record in pending[1:]
    ]
    assert third["nextCursor"] is None
    refreshed = chat_api.get(RECIPES, params={"awaitingFeedback": "true"}).json()["items"]
    assert refreshed[0]["historyId"] == added["id"]
    assert pending[0]["id"] not in {record["historyId"] for record in refreshed}


def test_recipe_cursors_bind_user_view_limit_and_original_expiry_before_storage(
    chat_api: TestClient,
    histories: list[dict[str, Any]],
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time())
    monkeypatch.setattr(pagination, "time", lambda: now)
    cursors = {
        view: chat_api.get(
            RECIPES, params={**({view: "true"} if view else {}), "limit": "1"}
        ).json()["nextCursor"]
        for view in (None, "awaitingFeedback", "rated")
    }
    assert conversation_settings.pagination_key is not None
    cipher = Fernet(conversation_settings.pagination_key.get_secret_value())
    cursor = cursors[None]
    decoded = json.loads(cipher.decrypt(cursor))
    decoded["userId"] = "another-user"
    foreign = cipher.encrypt(json.dumps(decoded).encode()).decode()
    create(chat_api)
    conversation_cursor = chat_api.get("/api/v1/conversations", params={"limit": 1}).json()[
        "nextCursor"
    ]
    invalid = [
        {"cursor": ""},
        {"cursor": "not-a-token"},
        {"cursor": "\u00ff"},
        {"cursor": cursor[:20] + ("B" if cursor[20] == "A" else "A") + cursor[21:]},
        {"cursor": foreign},
        {"cursor": cursor, "limit": "2"},
        {"cursor": conversation_cursor},
        *(
            {**({view: "true"} if view else {}), "cursor": token}
            for view in cursors
            for origin, token in cursors.items()
            if origin != view
        ),
    ]
    transport = harness[0]
    for params in invalid:
        before = len(transport.calls)
        response = chat_api.get(RECIPES, params=params)
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "invalid_cursor"
        assert response.headers["cache-control"] == "no-store"
        assert len(transport.calls) == before
        if params["cursor"]:
            assert params["cursor"] not in response.text
    now += 3600
    next_cursor = chat_api.get(RECIPES, params={"cursor": cursor}).json()["nextCursor"]
    assert cipher.extract_timestamp(cursor.encode()) == cipher.extract_timestamp(
        next_cursor.encode()
    )
    now += 86400
    before = len(transport.calls)
    expired = chat_api.get(RECIPES, params={"cursor": next_cursor})
    assert expired.status_code == 400 and expired.json()["code"] == "invalid_cursor"
    assert len(transport.calls) == before


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=51",
        "limit=abc",
        "awaitingFeedback=invalid",
        "rated=invalid",
        "rated=true&awaitingFeedback=true",
        "limit=1&limit=2",
        "rated=true&rated=false",
        "userId=foreign",
        "awaiting_feedback=true",
        "cursor=" + "a" * (24 * 1024 + 1),
    ],
)
def test_invalid_list_parameters_fail_safely_before_storage(
    chat_api: TestClient, harness: tuple[Transport, SyntheticCredential], query: str
) -> None:
    transport = harness[0]
    before = len(transport.calls)
    response = chat_api.get(RECIPES + "?" + query)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_input"
    assert response.json()["errors"] == []
    assert response.headers["cache-control"] == "no-store"
    assert len(transport.calls) == before


def test_unknown_ids_internal_records_and_detail_query_hints_are_safe(
    chat_api: TestClient,
    histories: list[dict[str, Any]],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport = harness[0]
    unknown = chat_api.get(f"{RECIPES}/{uuid4()}")
    behavior_id = documents(transport, "behavior")[0]["id"]
    internal = chat_api.get(f"{RECIPES}/{behavior_id}")
    assert unknown.status_code == internal.status_code == 404
    for response in (unknown, internal):
        assert response.json()["code"] == "not_found"
        assert response.headers["cache-control"] == "no-store"
        assert behavior_id not in response.text and "_etag" not in response.text
    before = len(transport.calls)
    invalid_id = chat_api.get(f"{RECIPES}/not-a-uuid")
    invalid_query = chat_api.get(f"{RECIPES}/{histories[0]['id']}", params={"userId": "foreign"})
    assert invalid_id.status_code == invalid_query.status_code == 422
    assert len(transport.calls) == before


@pytest.mark.parametrize("view", ["awaitingFeedback", "rated"])
def test_provider_records_outside_the_requested_view_fail_closed(
    chat_api: TestClient,
    histories: list[dict[str, Any]],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    view: str,
) -> None:
    transport = harness[0]
    original = transport.send
    poison = histories[0] if view == "awaitingFeedback" else histories[1]

    async def wrong_page(request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
        response = await original(request, **kwargs)
        if request.method == "POST" and request.body:
            assert isinstance(request.body, str | bytes)
            body = json.loads(request.body)
            if isinstance(body, dict) and "NOT IS_NULL(c.cookedAt)" in body.get("query", ""):
                return Response(request, 200, {"Documents": [poison], "_count": 1}, {})
        return response

    monkeypatch.setattr(transport, "send", wrong_page)
    response = chat_api.get(RECIPES, params={view: "true"})
    assert response.status_code == 502
    assert response.json()["code"] == "cosmos_invalid_result"
    assert poison["id"] not in response.text


@pytest.mark.parametrize("field", ["historyId", "recipe", "feedbackRevision"])
def test_corrupt_stored_history_is_not_reported_as_empty_or_leaked(
    chat_api: TestClient,
    histories: list[dict[str, Any]],
    field: str,
) -> None:
    record = histories[-1]
    record[field] = (
        str(uuid4()) if field == "historyId" else {"private": NOTE} if field == "recipe" else 0
    )
    for path in (RECIPES, f"{RECIPES}/{record['id']}"):
        response = chat_api.get(path)
        assert response.status_code == 502
        assert response.json()["code"] == "cosmos_invalid_result"
        assert NOTE not in response.text and record["id"] not in response.text


def test_empty_cosmos_service_page_does_not_end_the_history_traversal(
    chat_api: TestClient,
    histories: list[dict[str, Any]],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    harness[0].empty_query_pages = 1
    response = chat_api.get(RECIPES, params={"rated": "true", "limit": 2})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2 and response.json()["nextCursor"]
