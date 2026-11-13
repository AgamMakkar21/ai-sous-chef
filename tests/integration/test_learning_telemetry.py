"""Feedback API to real Cosmos/OpenAI SDKs over synthetic transports."""

import asyncio
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from sous_chef import tracing
from sous_chef.adapters.cosmos import open_cosmos
from sous_chef.app import create_app
from sous_chef.services.learning_recovery import LearningRecovery
from sous_chef.settings import Settings
from sous_chef.telemetry import Event
from sous_chef.tracing import Operation, Telemetry
from test_chat_api import FakeExecutor, documents
from test_chat_api import executor as executor
from test_conversation_api import OWNER
from test_conversation_api import conversation_settings as conversation_settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_feedback import feedback
from test_preference_extraction import Harness, config, response_body
from test_profile_api import onboard
from test_recipe_lifecycle import RECIPES, accept, generate
from test_synchronous_telemetry import capture as capture

from worker import extraction
from worker import function_app as worker

type Capture = tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader]
CANARY = "ASYNC_PRIVATE_CANARY@example.invalid"
NOTE = f"Prefer extra lime. {CANARY}"


@pytest.fixture
def learning_path(
    capture: Capture,
    conversation_settings: Settings,
    auth_headers: dict[str, str],
    executor: FakeExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Harness]]:
    model = Harness(diagnostic=True)
    monkeypatch.setattr(worker, "_settings", lambda: conversation_settings)
    monkeypatch.setattr(
        extraction.ExtractionConfig, "from_environment", classmethod(lambda cls: config())
    )
    monkeypatch.setattr(extraction, "_project_client", model.factory)
    with TestClient(
        create_app(conversation_settings, chat_executor=executor, telemetry=capture[0]),
        headers=auth_headers,
    ) as client:
        onboard(client)
        yield client, model


def _feedback(client: TestClient, executor: FakeExecutor, transport: Transport) -> dict[str, Any]:
    saved = accept(client, generate(client, executor)["generatedRecipeId"], cooked=True)
    result = feedback(client, saved, note=NOTE)
    assert result.status_code == 200, result.text
    return documents(transport, "feedbackRevision")[-1]


def _model_response(event: dict[str, Any]) -> dict[str, Any]:
    return response_body(
        {
            "outcome": "candidates",
            "candidates": [
                {
                    "field": "likes",
                    "value": "lime",
                    "insight": "Prefer lime-forward preparations.",
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "feedbackId": event["feedbackId"],
                            "sourceId": event["historyId"],
                            "sourceRevision": event["feedbackRevision"],
                            "occasionId": event["occasionId"],
                            "signalType": "feedback_note",
                            "direction": "support",
                            "excerpt": event["note"],
                        }
                    ],
                }
            ],
        }
    )


def _dispatch(capture: Capture, event: dict[str, Any]) -> None:
    with capture[0].activate():
        worker.process_batch(
            [event],
            worker.process_feedback,
            quarantine=worker.quarantine_feedback,
            replay_processor=worker.process_replay,
        )


def _consumers(capture: Capture) -> list[ReadableSpan]:
    return [span for span in capture[1].get_finished_spans() if span.name == Operation.LEARNING]


def _events(span: ReadableSpan) -> set[str]:
    return {event.name for event in span.events}


def test_feedback_links_to_one_applied_insight_and_duplicate_is_deduplicated(
    learning_path: tuple[TestClient, Harness],
    capture: Capture,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, model = learning_path
    event = _feedback(client, executor, harness[0])
    model.body = _model_response(event)
    producer = next(
        span for span in capture[1].get_finished_spans() if span.name == Operation.FEEDBACK
    )
    clock = Mock()
    clock.now.return_value = datetime.now(UTC) + timedelta(minutes=10)
    monkeypatch.setattr(tracing, "datetime", clock)
    for _ in range(2):
        _dispatch(capture, event)
    consumers = _consumers(capture)
    assert len(consumers) == 2 and producer.context is not None
    assert Event.LEARNING_LEDGER_COMMITTED in _events(consumers[0])
    assert Event.LEARNING_COMPLETED in _events(consumers[0])
    assert Event.LEARNING_DEDUPLICATED in _events(consumers[1])
    assert Event.LEARNING_LEDGER_COMMITTED not in _events(consumers[1])
    for span in consumers:
        assert span.kind == SpanKind.CONSUMER and span.parent is None
        assert span.context is not None and span.context.trace_id != producer.context.trace_id
        assert len(span.links) == 1
        assert span.links[0].context.trace_id == producer.context.trace_id
        assert span.links[0].context.span_id == producer.context.span_id
        assert span.attributes is not None
        assert span.attributes["asc.feedback.id"] == event["id"]
        assert span.attributes["asc.feedback.revision"] == 1
        lag = span.attributes["asc.learning.processing_lag_ms"]
        assert isinstance(lag, (int, float)) and lag >= 590_000
    assert len(model.requests) == 1
    preferences = documents(harness[0], "profile")[0]["learnedPreferences"]
    assert len(preferences) == preferences[0]["evidenceCount"] == 1
    extraction_span = next(
        span for span in capture[1].get_finished_spans() if span.name == Operation.EXTRACTION
    )
    assert extraction_span.attributes is not None
    assert extraction_span.attributes["gen_ai.usage.input_tokens"] == 500
    assert extraction_span.attributes["gen_ai.usage.output_tokens"] == 100
    assert "traceparent" in model.requests[0].headers
    assert "baggage" not in model.requests[0].headers
    assert "tracestate" not in model.requests[0].headers
    serialized = "".join(span.to_json() for span in capture[1].get_finished_spans())
    metrics = capture[2].get_metrics_data()
    assert metrics is not None
    metric_names = {
        metric.name
        for resource in metrics.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert {
        "asc.learning.events",
        "asc.learning.processing_lag",
        "asc.learning.batch.documents",
        "asc.model.tokens",
    } <= metric_names
    for private in (CANARY, OWNER.user_id, "Prefer lime-forward preparations."):
        assert private not in serialized + caplog.text + str(metrics)
    assert all(event.name != "exception" for span in consumers for event in span.events)


def test_schema_failure_and_authorized_replay_keep_version_and_generation_distinct(
    learning_path: tuple[TestClient, Harness],
    capture: Capture,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, model = learning_path
    event = _feedback(client, executor, harness[0])
    model.body = response_body({"private_invalid_output": CANARY})
    _dispatch(capture, event)
    first = _consumers(capture)[0]
    assert Event.LEARNING_FAILED in _events(first)
    assert first.attributes is not None and first.attributes["asc.learning.generation"] == 0
    assert any(
        Event.LEARNING_SCHEMA_REJECTED in _events(span) for span in capture[1].get_finished_spans()
    )

    async def replay() -> None:
        async with open_cosmos(conversation_settings) as repository:
            assert (
                await LearningRecovery(repository).replay(OWNER, UUID(event["id"]), 1, uuid4())
                == "requested"
            )

    asyncio.run(replay())
    outbox = next(
        item for item in documents(harness[0], "processing") if item.get("role") == "learningReplay"
    )
    model.body = _model_response(event)
    _dispatch(capture, outbox)
    second = _consumers(capture)[1]
    assert {Event.LEARNING_REPLAY, Event.LEARNING_COMPLETED} <= _events(second)
    assert second.attributes is not None and second.attributes["asc.learning.generation"] == 1
    assert (
        first.attributes["asc.feedback.id"] == second.attributes["asc.feedback.id"] == event["id"]
    )
    assert len(second.links) == len(first.links) == 1
    assert second.links[0].context == first.links[0].context
    current = client.get(f"{RECIPES}/{event['historyId']}")
    edited = feedback(client, current, note=f"More lime, please. {CANARY}", key="revision-two")
    assert edited.status_code == 200, edited.text
    revision = documents(harness[0], "feedbackRevision")[-1]
    model.body = _model_response(revision)
    _dispatch(capture, revision)
    third = _consumers(capture)[2]
    assert third.attributes is not None and third.attributes["asc.feedback.revision"] == 2
    assert third.attributes["asc.feedback.id"] != event["id"]
    assert Event.LEARNING_COMPLETED in _events(third)
    assert documents(harness[0], "profile")[0]["learnedPreferences"][0]["evidenceCount"] == 1
    assert CANARY not in caplog.text + "".join(
        span.to_json() for span in capture[1].get_finished_spans()
    )


def test_invalid_revision_is_quarantined_without_extraction_or_raw_payload(
    learning_path: tuple[TestClient, Harness],
    capture: Capture,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = deepcopy(_feedback(learning_path[0], executor, harness[0]))
    event["schemaVersion"] = 2
    event["feedbackRevision"] = 2
    event["traceParent"] = CANARY
    _dispatch(capture, event)
    span = _consumers(capture)[0]
    assert {Event.LEARNING_INVALID, Event.LEARNING_QUARANTINED} <= _events(span)
    assert span.attributes is not None and span.attributes["asc.feedback.revision"] == 2
    assert span.attributes["asc.event.schema_version"] == 2
    assert not span.links and not learning_path[1].requests
    assert any(
        item.get("role") == "learningQuarantine" for item in documents(harness[0], "processing")
    )
    assert CANARY not in span.to_json() + caplog.text


def test_concurrency_retry_is_visible_and_does_not_duplicate_evidence(
    learning_path: tuple[TestClient, Harness],
    capture: Capture,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    event = _feedback(learning_path[0], executor, harness[0])
    learning_path[1].body = _model_response(event)
    harness[0].faults["POST"].append(412)
    with pytest.raises(worker.FeedbackBatchFailed):
        _dispatch(capture, event)
    _dispatch(capture, event)
    first, second = _consumers(capture)
    assert Event.LEARNING_RETRY in _events(first)
    assert first.attributes is not None and first.attributes["error.type"] == "revision_conflict"
    assert Event.LEARNING_COMPLETED in _events(second)
    assert len(learning_path[1].requests) == 1
    assert documents(harness[0], "profile")[0]["learnedPreferences"][0]["evidenceCount"] == 1
