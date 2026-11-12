import json
import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock

import azure.functions as func
import pytest
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.feedback import FeedbackRevision
from sous_chef.services.recipe_lifecycle import _identity

from worker import function_app as worker

OWNER = "a" * 64
HISTORY = "11111111-1111-4111-8111-111111111111"
STAMP = "2026-01-01T00:00:00.000000Z"


def record(revision: int = 1, owner: str = OWNER) -> dict[str, Any]:
    user = AuthenticatedUser(owner)
    feedback_id = _identity(user, "recipe-feedback", f"{HISTORY}:{revision}")
    event = FeedbackRevision(
        feedback_id=feedback_id,
        source_id=HISTORY,
        history_id=HISTORY,
        occasion_id="22222222-2222-4222-8222-222222222222",
        generated_recipe_id="33333333-3333-4333-8333-333333333333",
        source_conversation_id="44444444-4444-4444-8444-444444444444",
        source_turn_id="55555555-5555-4555-8555-555555555555",
        feedback_revision=revision,
        supersedes_feedback_id=(
            _identity(user, "recipe-feedback", f"{HISTORY}:{revision - 1}")
            if revision > 1
            else None
        ),
        rating="liked",
        note="Private synthetic feedback: use more lime.",
        trace_id="c" * 32,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return {
        **event.model_dump(mode="json", by_alias=True),
        "id": str(feedback_id),
        "userId": owner,
        "recordKind": "feedbackRevision",
        "schemaVersion": 1,
        "createdAt": STAMP,
        "updatedAt": STAMP,
        "_etag": '"synthetic"',
    }


@pytest.mark.parametrize("rating", ["liked", "fine", "disliked"])
@pytest.mark.parametrize("note", [None, "Private synthetic feedback."])
def test_eligible_feedback_reaches_processor_with_exact_owned_evidence(
    rating: str, note: str | None
) -> None:
    document = record() | {"rating": rating, "note": note}
    original = deepcopy(document)
    handler = Mock()
    worker.process_batch([func.Document.from_dict(document)], handler)
    event = handler.call_args.args[0]
    assert handler.call_count == 1
    assert event.user_id == OWNER
    assert event.feedback_id == document["id"]
    assert event.history_id == document["sourceId"] == HISTORY
    assert event.feedback_revision == 1
    assert event.rating == rating and event.note == note
    assert event.occasion_id == document["occasionId"]
    assert event.source_conversation_id == document["sourceConversationId"]
    assert event.source_turn_id == document["sourceTurnId"]
    assert event.generated_recipe_id == document["generatedRecipeId"]
    assert event.trace_id == document["traceId"]
    assert document == original
    assert OWNER not in repr(event) and "Private" not in repr(event)


def test_unrelated_records_and_processing_status_writes_never_invoke_processor() -> None:
    documents = [
        record() | {"recordKind": kind, "processingStatus": status}
        for kind in (
            "profile",
            "groceries",
            "conversation",
            "turn",
            "recipe",
            "recipeOperation",
            "behavior",
            "processing",
        )
        for status in ("pending", "processing", "completed", "failed")
    ]
    documents += [
        record() | {"processingStatus": status}
        for status in ("notEligible", "processing", "completed", "failed")
    ]
    documents.append(record() | {"updatedAt": "2026-01-02T00:00:00Z"})
    handler = Mock()
    worker.process_batch(documents, handler)
    handler.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"schemaVersion": 2},
        {"schemaVersion": True},
        {"schemaVersion": 1.0},
        {"userId": "foreign"},
        {"userId": "b" * 64},
        {"feedbackRevision": 0},
        {"feedbackRevision": True},
        {"feedbackRevision": "1"},
        {"feedbackRevision": 2},
        {"rating": None},
        {"rating": "Liked"},
        {"rating": ["liked"]},
        {"note": " "},
        {"note": 1},
        {"note": "x" * 2001},
        {"note": " unnormalized "},
        {"provenance": "inferred"},
        {"processingStatus": None},
        {"processingStatus": {}},
        {"traceId": "private-trace"},
        {"id": HISTORY},
        {"feedbackId": HISTORY},
        {"sourceId": "22222222-2222-4222-8222-222222222222"},
        {"occasionId": "private-bad-id"},
        {"supersedesFeedbackId": HISTORY},
        {"observedAt": "2026-01-01"},
        {"createdAt": "not-a-date"},
        {"updatedAt": None},
        {"ttl": 3600},
    ],
)
def test_invalid_feedback_is_not_acknowledged_or_sent_to_extraction(
    changes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = Mock()
    warning = Mock()
    monkeypatch.setattr(logging, "warning", warning)
    with pytest.raises(worker.FeedbackBatchFailed) as failure:
        worker.process_batch([record() | changes], handler)
    handler.assert_not_called()
    assert failure.value.__cause__ is None and failure.value.__suppress_context__
    assert "private" not in str(failure.value).lower()
    message = warning.call_args.args[0]
    assert OWNER not in message and "Private" not in message
    logged = json.loads(message)
    assert logged["invalid"] == 1 and logged["failed"] == 0


@pytest.mark.parametrize("field", ["note", "supersedesFeedbackId", "historyId", "observedAt"])
def test_missing_required_feedback_fields_remain_pending(field: str) -> None:
    document = record()
    del document[field]
    handler = Mock()
    with pytest.raises(worker.FeedbackBatchFailed):
        worker.process_batch([document], handler)
    handler.assert_not_called()


def test_one_poison_or_processor_failure_does_not_skip_the_rest_of_a_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = Mock(side_effect=[RuntimeError("Private exception and prompt"), None])
    warning = Mock()
    monkeypatch.setattr(logging, "warning", warning)
    with pytest.raises(worker.FeedbackBatchFailed):
        worker.process_batch(
            [record() | {"note": 17}, record(), record(2)],
            handler,
        )
    assert [call.args[0].feedback_revision for call in handler.call_args_list] == [1, 2]
    message = warning.call_args.args[0]
    counts = json.loads(message)
    assert counts == {
        "event": "feedback_batch_pending",
        "received": 3,
        "ignored": 0,
        "dispatched": 1,
        "invalid": 1,
        "failed": 1,
        "quarantined": 0,
    }
    assert "Private" not in message and OWNER not in message


def test_delivery_metadata_does_not_change_event_keys_but_owner_and_revision_do() -> None:
    handler = Mock()
    first = record()
    worker.process_batch([first, first], handler)
    worker.process_batch([first | {"_etag": '"new"', "_ts": 123}], handler)
    worker.process_batch([record(2), record(owner="b" * 64)], handler)
    keys = [call.args[0].event_key for call in handler.call_args_list]
    assert keys[0] == keys[1] == keys[2]
    assert len({keys[0], keys[3], keys[4]}) == 3
    assert handler.call_args_list[3].args[0].supersedes_feedback_id == first["id"]


def test_unconfigured_processor_cannot_acknowledge_feedback_or_mutate_it() -> None:
    document = record()
    before = deepcopy(document)
    with pytest.raises(worker.FeedbackBatchFailed):
        worker.process_batch([document], worker.process_feedback)
    assert document == before


def test_empty_batch_never_invokes_processor() -> None:
    handler = Mock()
    worker.process_batch([], handler)
    handler.assert_not_called()
