import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from azure.core.exceptions import ServiceResponseError
from fastapi.testclient import TestClient
from sous_chef.adapters.cosmos import CosmosRepository
from sous_chef.app import create_app
from sous_chef.domain.cosmos import StoredRecord
from sous_chef.domain.feedback import FeedbackRevision
from sous_chef.settings import Settings
from test_chat_api import FakeExecutor, documents
from test_chat_api import chat_api as chat_api
from test_chat_api import executor as executor
from test_conversation_api import OWNER
from test_conversation_api import conversation_settings as conversation_settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_recipe_lifecycle import RECIPES, accept, generate

from worker import function_app as feedback_worker

NOTE = "Private note: less salt next time."


def test_persisted_feedback_events_feed_worker_but_current_recipe_and_receipts_do_not(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    saved = accept(chat_api, generate(chat_api, executor)["generatedRecipeId"], cooked=True)
    first = feedback(chat_api, saved)
    assert first.status_code == 200
    second = feedback(chat_api, first, note="Use more lime.", key="edit-feedback")
    assert second.status_code == 200
    events: list[feedback_worker.FeedbackEvent] = []
    records = [
        *documents(harness[0], "recipe"),
        *documents(harness[0], "recipeOperation"),
        *documents(harness[0], "feedbackRevision"),
    ]
    feedback_worker.process_batch(records, events.append)
    assert sorted(event.feedback_revision for event in events) == [1, 2]
    assert len({event.event_key for event in events}) == 2
    assert all(event.user_id == OWNER.user_id for event in events)


def feedback(
    api: TestClient,
    saved: Any,
    *,
    rating: str = "liked",
    note: str | None = NOTE,
    key: str = "feedback",
) -> Any:
    return api.put(
        f"{RECIPES}/{saved.json()['data']['historyId']}/feedback",
        json={"rating": rating, "note": note},
        headers={"Idempotency-Key": key, "If-Match": saved.headers["etag"]},
    )


@pytest.mark.parametrize("rating", ["liked", "fine", "disliked"])
@pytest.mark.parametrize("note", [None, NOTE, "x" * 2000, "  less salt  "])
def test_feedback_round_trips_with_one_immutable_correlated_pending_event(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    rating: str,
    note: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = generate(chat_api, executor)
    saved = accept(chat_api, source["generatedRecipeId"], cooked=True)
    profile = deepcopy(documents(harness[0], "profile"))
    executor.mode = "fail"
    response = feedback(chat_api, saved, rating=rating, note=note)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["rating"] == rating
    assert result["note"] == (note.strip() if note else None)
    assert result["feedbackRevision"] == 1
    assert result["processingStatus"] == "pending" and not result["awaitingFeedback"]
    assert result["recipe"] == source["recipe"]
    assert result["cookedAt"] == saved.json()["data"]["cookedAt"]
    assert response.headers["etag"] != saved.headers["etag"]
    assert response.headers["cache-control"] == "no-store"
    detail = chat_api.get(RECIPES + "/" + result["historyId"])
    assert detail.json() == response.json() and detail.headers["etag"] == response.headers["etag"]
    assert not chat_api.get(RECIPES, params={"awaitingFeedback": "true"}).json()["items"]
    assert chat_api.get(RECIPES, params={"rated": "true"}).json()["items"][0]["rating"] == rating
    stored = documents(harness[0], "feedbackRevision")
    assert len(stored) == 1
    event = stored[0]
    fields = {field.alias for field in FeedbackRevision.model_fields.values()}
    FeedbackRevision.model_validate({key: value for key, value in event.items() if key in fields})
    assert event["recordKind"] == "feedbackRevision" and event["schemaVersion"] == 1
    assert event["id"] == event["feedbackId"]
    assert event["sourceId"] == event["historyId"] == result["historyId"]
    assert event["occasionId"] == source["occasionId"]
    assert event["sourceTurnId"] == source["turnId"]
    assert event["sourceConversationId"] == source["conversationId"]
    assert event["feedbackRevision"] == 1 and event["supersedesFeedbackId"] is None
    assert event["rating"] == rating and event["note"] == result["note"]
    assert event["processingStatus"] == "pending"
    assert event["traceId"] == response.headers["x-trace-id"]
    assert event["observedAt"].endswith("Z") and event["userId"] == OWNER.user_id
    assert documents(harness[0], "profile") == profile
    assert len(executor.calls) == 1
    assert NOTE not in caplog.text and OWNER.user_id not in caplog.text
    assert "personalization" not in response.text.lower()


def test_replay_survives_restart_and_old_keys_cannot_undo_newer_edits(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    saved = accept(chat_api, generate(chat_api, executor)["generatedRecipeId"], cooked=True)
    first = feedback(chat_api, saved)
    assert first.status_code == 200
    original_event = deepcopy(documents(harness[0], "feedbackRevision")[0])
    with TestClient(create_app(conversation_settings), headers=auth_headers) as restarted:
        repeated = feedback(restarted, saved)
        assert repeated.json() == first.json()
        assert repeated.headers["etag"] == first.headers["etag"]
        second = feedback(restarted, first, note="Use more lime.", key="edit-note")
        assert second.status_code == 200 and second.json()["data"]["feedbackRevision"] == 2
        third = feedback(restarted, second, rating="disliked", note=None, key="edit-rating")
        assert third.status_code == 200 and third.json()["data"]["feedbackRevision"] == 3
        assert third.json()["data"]["note"] is None
        replay = feedback(restarted, saved)
        assert replay.json() == third.json() and replay.headers["etag"] == third.headers["etag"]
        mismatch = feedback(restarted, saved, rating="fine")
        assert mismatch.status_code == 409 and mismatch.json()["code"] == "idempotency_conflict"
    events = sorted(documents(harness[0], "feedbackRevision"), key=lambda e: e["feedbackRevision"])
    assert len(events) == 3 and events[0] == original_event
    assert [event["feedbackRevision"] for event in events] == [1, 2, 3]
    for previous, current in zip(events, events[1:], strict=False):
        assert current["supersedesFeedbackId"] == previous["feedbackId"]


@pytest.mark.parametrize("status", ["pending", "processing", "completed", "failed"])
def test_identical_payload_under_new_key_is_not_new_evidence_or_learning_retry(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    status: str,
) -> None:
    saved = accept(chat_api, generate(chat_api, executor)["generatedRecipeId"], cooked=True)
    first = feedback(chat_api, saved)
    assert first.status_code == 200
    documents(harness[0], "recipe")[0]["processingStatus"] = status
    before = deepcopy(documents(harness[0], "feedbackRevision"))
    repeated = feedback(chat_api, first, key="same-values-new-key")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"]["feedbackRevision"] == 1
    assert repeated.json()["data"]["processingStatus"] == status
    assert documents(harness[0], "feedbackRevision") == before
    replay = feedback(chat_api, first, key="same-values-new-key")
    assert replay.json() == repeated.json() and replay.headers["etag"] == repeated.headers["etag"]
    edited = feedback(chat_api, repeated, note=None, key="clear-note")
    assert edited.status_code == 200
    assert edited.json()["data"]["feedbackRevision"] == 2
    assert edited.json()["data"]["processingStatus"] == "pending"


def test_cooked_state_stale_etags_and_cross_recipe_key_reuse_fail_without_new_evidence(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    planned = accept(chat_api, generate(chat_api, executor)["generatedRecipeId"])
    before = deepcopy(harness[0].documents)
    denied = feedback(chat_api, planned)
    assert denied.status_code == 409 and denied.json()["code"] == "feedback_requires_cooking"
    assert harness[0].documents == before
    saved = accept(
        chat_api, generate(chat_api, executor)["generatedRecipeId"], cooked=True, key="save-second"
    )
    first = feedback(chat_api, saved)
    before = deepcopy(harness[0].documents)
    conflict = feedback(chat_api, saved, key="stale")
    assert conflict.status_code == 412 and harness[0].documents == before
    cross = feedback(chat_api, planned)
    assert cross.status_code == 409 and cross.json()["code"] == "idempotency_conflict"
    assert len(documents(harness[0], "feedbackRevision")) == 1
    assert first.json()["data"]["feedbackRevision"] == 1


def test_omitting_note_clears_it_and_returning_to_old_values_is_a_genuine_new_revision(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    saved = accept(chat_api, generate(chat_api, executor)["generatedRecipeId"], cooked=True)
    first = feedback(chat_api, saved)
    path = f"{RECIPES}/{first.json()['data']['historyId']}/feedback"
    cleared = chat_api.put(
        path,
        json={"rating": "liked"},
        headers={"If-Match": first.headers["etag"], "Idempotency-Key": "omit-note"},
    )
    assert cleared.status_code == 200 and cleared.json()["data"]["note"] is None
    assert cleared.json()["data"]["feedbackRevision"] == 2
    restored = feedback(chat_api, cleared, key="restore-original")
    assert restored.status_code == 200 and restored.json()["data"]["feedbackRevision"] == 3
    assert restored.json()["data"]["note"] == NOTE
    assert len(documents(harness[0], "feedbackRevision")) == 3


@pytest.mark.parametrize("fault", [403, 400, 429, 503, "disconnect"])
def test_failed_cosmos_batch_is_not_a_successful_rating_and_has_no_partial_event(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    fault: int | str,
) -> None:
    saved = accept(chat_api, generate(chat_api, executor)["generatedRecipeId"], cooked=True)
    transport = harness[0]
    before = deepcopy(transport.documents)
    transport.faults["POST"].extend(
        [ServiceResponseError("private disconnect") if fault == "disconnect" else fault] * 20
    )
    response = feedback(chat_api, saved)
    assert response.status_code in {429, 502, 503, 504}, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert "etag" not in response.headers and "private disconnect" not in response.text
    assert transport.documents == before and not documents(transport, "feedbackRevision")
    transport.faults["POST"].clear()
    retry = feedback(chat_api, saved)
    assert retry.status_code == 200 and retry.json()["data"]["feedbackRevision"] == 1
    assert len(documents(transport, "feedbackRevision")) == 1


def test_lost_commit_response_is_reconciled_without_replaying_the_batch(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    saved = accept(chat_api, generate(chat_api, executor)["generatedRecipeId"], cooked=True)
    harness[0].write_then_disconnect = True
    response = feedback(chat_api, saved)
    harness[0].write_then_disconnect = False
    assert response.status_code == 200, response.text
    assert feedback(chat_api, saved).json() == response.json()
    assert len(documents(harness[0], "feedbackRevision")) == 1


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_requests_cannot_create_two_versions_from_one_etag(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    same_key: bool,
) -> None:
    saved = accept(chat_api, generate(chat_api, executor)["generatedRecipeId"], cooked=True)
    barrier = Barrier(2)
    commit = CosmosRepository.commit_feedback

    async def concurrent_commit(*args: Any, **kwargs: Any) -> tuple[StoredRecord, ...]:
        await asyncio.to_thread(barrier.wait, 10)
        return await commit(*args, **kwargs)

    monkeypatch.setattr(CosmosRepository, "commit_feedback", concurrent_commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(feedback, chat_api, saved, key="race" if same_key else str(index))
            for index in range(2)
        ]
        results = [future.result(timeout=15) for future in futures]
    assert sorted(result.status_code for result in results) == (
        [200, 200] if same_key else [200, 412]
    )
    assert len(documents(harness[0], "feedbackRevision")) == 1
    assert documents(harness[0], "recipe")[0]["feedbackRevision"] == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"rating": "Liked"},
        {"rating": "Didn't Like"},
        {"rating": None},
        {"rating": 1},
        {"rating": "fine", "note": ""},
        {"rating": "fine", "note": " "},
        {"rating": "fine", "note": 1},
        {"rating": "fine", "note": "x" * 2001},
        {"rating": "fine", "feedbackRevision": 5},
        {"rating": "fine", "processingStatus": "completed"},
        {"rating": "fine", "userId": "foreign"},
    ],
)
def test_invalid_feedback_never_accesses_storage_or_ai(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    body: dict[str, Any],
) -> None:
    before = len(harness[0].calls)
    response = chat_api.put(
        f"{RECIPES}/{uuid4()}/feedback",
        json=body,
        headers={"If-Match": '"etag"', "Idempotency-Key": "x"},
    )
    assert response.status_code == 422, response.text
    assert len(harness[0].calls) == before and not executor.calls


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Idempotency-Key": "x"}, 428),
        ({"If-Match": '"etag"'}, 422),
        ({"Idempotency-Key": "x", "If-Match": "*"}, 422),
        ({"Idempotency-Key": "x", "If-Match": 'W/"etag"'}, 422),
        ({"Idempotency-Key": "x", "If-Match": '"one", "two"'}, 422),
    ],
)
def test_feedback_requires_valid_revision_and_idempotency_headers(
    chat_api: TestClient,
    harness: tuple[Transport, SyntheticCredential],
    headers: dict[str, str],
    status: int,
) -> None:
    before = len(harness[0].calls)
    response = chat_api.put(
        f"{RECIPES}/{uuid4()}/feedback", json={"rating": "fine"}, headers=headers
    )
    assert response.status_code == status, response.text
    assert len(harness[0].calls) == before
