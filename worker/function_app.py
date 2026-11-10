"""Feedback-only change-feed admission; extraction and application remain deferred."""

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

import azure.functions as func

app = func.FunctionApp()

_UUID_FIELDS = (
    "feedbackId",
    "historyId",
    "occasionId",
    "generatedRecipeId",
    "sourceConversationId",
    "sourceTurnId",
)


@dataclass(frozen=True, repr=False)
class FeedbackEvent:
    event_key: str
    user_id: str
    feedback_id: str
    history_id: str
    occasion_id: str
    generated_recipe_id: str
    source_conversation_id: str
    source_turn_id: str
    feedback_revision: int
    supersedes_feedback_id: str | None
    rating: str
    note: str | None
    observed_at: datetime
    trace_id: str


class InvalidFeedback(ValueError):
    def __init__(self) -> None:
        super().__init__("Invalid feedback revision.")


class FeedbackBatchFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Feedback batch remains pending; inspect aggregate worker diagnostics.")


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidFeedback
    try:
        parsed = UUID(value)
    except ValueError:
        raise InvalidFeedback from None
    if parsed.version != 4 or str(parsed) != value:
        raise InvalidFeedback
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise InvalidFeedback
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidFeedback from None
    if parsed.utcoffset() is None:
        raise InvalidFeedback
    return parsed


def _feedback_id(user: str, history: str, revision: int) -> str:
    digest = hashlib.sha256(f"{user}\0recipe-feedback\0{history}:{revision}".encode()).digest()
    return str(UUID(bytes=digest[:16], version=4))


def eligible_feedback(document: Mapping[str, object]) -> FeedbackEvent | None:
    if document.get("recordKind") != "feedbackRevision":
        return None
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 1:
        raise InvalidFeedback
    status = document.get("processingStatus")
    if status in ("notEligible", "processing", "completed", "failed"):
        return None
    if status != "pending":
        raise InvalidFeedback
    created = _timestamp(document.get("createdAt"))
    updated = _timestamp(document.get("updatedAt"))
    if updated != created:
        return None
    owner = document.get("userId")
    revision = document.get("feedbackRevision")
    rating = document.get("rating")
    note = document.get("note")
    trace = document.get("traceId")
    if (
        not isinstance(owner, str)
        or not re.fullmatch(r"[0-9a-f]{64}", owner)
        or type(revision) is not int
        or revision < 1
        or rating not in ("liked", "fine", "disliked")
        or "note" not in document
        or note is not None
        and (
            not isinstance(note, str)
            or not note.strip()
            or note != note.strip()
            or len(note) > 2000
        )
        or document.get("provenance") != "explicit_user_action"
        or not isinstance(trace, str)
        or not re.fullmatch(r"[0-9a-f]{32}", trace)
    ):
        raise InvalidFeedback
    identifiers = {name: _uuid(document.get(name)) for name in _UUID_FIELDS}
    history = identifiers["historyId"]
    feedback = identifiers["feedbackId"]
    previous = _feedback_id(owner, history, revision - 1) if revision > 1 else None
    if (
        document.get("id") != feedback
        or document.get("sourceId") != history
        or feedback != _feedback_id(owner, history, revision)
        or document.get("supersedesFeedbackId") != previous
        or "supersedesFeedbackId" not in document
        or document.get("ttl") is not None
    ):
        raise InvalidFeedback
    key = hashlib.sha256(f"feedback:v1\0{owner}\0{history}\0{revision}".encode()).hexdigest()
    return FeedbackEvent(
        event_key=key,
        user_id=owner,
        feedback_id=feedback,
        history_id=history,
        occasion_id=identifiers["occasionId"],
        generated_recipe_id=identifiers["generatedRecipeId"],
        source_conversation_id=identifiers["sourceConversationId"],
        source_turn_id=identifiers["sourceTurnId"],
        feedback_revision=revision,
        supersedes_feedback_id=previous,
        rating=str(rating),
        note=note,
        observed_at=_timestamp(document.get("observedAt")),
        trace_id=trace,
    )


def process_feedback(event: FeedbackEvent) -> None:
    """ASC-47/48 replace this boundary; never acknowledge a mock as applied learning."""
    raise RuntimeError("Feedback extraction and durable application are not connected.")


def process_batch(
    documents: Iterable[Mapping[str, object]],
    processor: Callable[[FeedbackEvent], None],
) -> None:
    counts = {"received": 0, "ignored": 0, "dispatched": 0, "invalid": 0, "failed": 0}
    for document in documents:
        counts["received"] += 1
        try:
            event = eligible_feedback(document)
        except Exception:
            counts["invalid"] += 1
            continue
        if event is None:
            counts["ignored"] += 1
            continue
        try:
            processor(event)
            counts["dispatched"] += 1
        except Exception:
            counts["failed"] += 1
    if counts["invalid"] or counts["failed"]:
        logging.warning(json.dumps({"event": "feedback_batch_pending", **counts}))
        raise FeedbackBatchFailed from None


@app.function_name(name="feedback_change_feed")
@app.retry(strategy="fixed_delay", max_retry_count="-1", delay_interval="00:01:00")
@app.cosmos_db_trigger(
    arg_name="documents",
    connection="FeedbackCosmos",
    database_name="%ASC_COSMOS_DATABASE%",
    container_name="%ASC_FEEDBACK_CONTAINER%",
    lease_connection="FeedbackLeases",
    lease_database_name="%ASC_COSMOS_DATABASE%",
    lease_container_name="%ASC_LEASE_CONTAINER%",
    create_lease_container_if_not_exists=False,
    lease_container_prefix="feedback-v1",
    max_items_per_invocation=50,
    # azure-functions 1.24.0 incorrectly annotates this Boolean property as datetime.time.
    start_from_beginning=True,  # type: ignore[arg-type]
)
def feedback_change_feed(documents: func.DocumentList) -> None:
    validate_configuration()
    process_batch(documents, process_feedback)


def validate_configuration() -> None:
    client = os.environ.get("AZURE_CLIENT_ID", "")
    if not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", client):
        raise RuntimeError("Host requires its own managed identity client ID.")
    for prefix in ("AzureWebJobsStorage", "FeedbackCosmos", "FeedbackLeases"):
        if (
            os.environ.get(prefix)
            or os.environ.get(f"{prefix}__credential") != "managedidentity"
            or os.environ.get(f"{prefix}__clientId") != client
        ):
            raise RuntimeError("Host connections must use the configured managed identity.")
    if not re.fullmatch(r"[a-z0-9]{3,24}", os.environ.get("AzureWebJobsStorage__accountName", "")):
        raise RuntimeError("Host requires an identity-based storage account name.")
    endpoint = os.environ.get("FeedbackCosmos__accountEndpoint", "")
    if not re.fullmatch(
        r"https://[a-z0-9-]+\.documents\.azure\.com(?::443)?/?", endpoint
    ) or endpoint != os.environ.get("FeedbackLeases__accountEndpoint"):
        raise RuntimeError("Host requires matching, valid Cosmos and lease endpoints.")
    if os.environ.get("ASC_ENVIRONMENT") not in {"dev", "prod"}:
        raise RuntimeError("Host requires an explicit dev or prod environment.")
    expected = {
        "ASC_COSMOS_DATABASE": "sous-chef",
        "ASC_FEEDBACK_CONTAINER": "RecipeHistory",
        "ASC_LEASE_CONTAINER": "leases",
        "FUNCTIONS_EXTENSION_VERSION": "~4",
        "FUNCTIONS_WORKER_RUNTIME": "python",
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Host runtime, environment or container configuration does not match.")


@app.function_name(name="host_health")
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def host_health(req: func.HttpRequest) -> func.HttpResponse:
    validate_configuration()
    release = json.loads(Path(__file__).with_name("release.json").read_text(encoding="utf-8"))
    sha = release.get("commit", "")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("Host release metadata is invalid.")
    return func.HttpResponse(
        '{"status":"ok","scope":"host-only"}',
        mimetype="application/json",
        headers={"X-ASC-Commit": sha, "Cache-Control": "no-store"},
    )
