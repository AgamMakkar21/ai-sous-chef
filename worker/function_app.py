"""Feedback-only admission and durable preference application."""

import asyncio
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
from sous_chef.adapters.cosmos import open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.cosmos import ProductContainer
from sous_chef.domain.extraction import ExtractionRequest, ExtractionResult
from sous_chef.services.learning import LearningService
from sous_chef.services.learning_recovery import (
    LearningPending,
    LearningRecovery,
    LearningReplay,
    RecoveryPolicy,
    classify_failure,
    replay_id,
)
from sous_chef.settings import Settings

from worker.extraction import ExtractionConfig, PreferenceExtractor, private_sdk_diagnostics

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


@dataclass(frozen=True, repr=False)
class ReplayEvent:
    user_id: str
    replay: LearningReplay


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


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "environment": os.environ["ASC_ENVIRONMENT"],
            "adapter_mode": "live",
            "agents_enabled": False,
            "cosmos_endpoint": os.environ["FeedbackCosmos__accountEndpoint"],
            "cosmos_database": os.environ["ASC_COSMOS_DATABASE"],
        }
    )


class _EnvironmentExtractor:
    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        return await PreferenceExtractor(ExtractionConfig.from_environment()).extract(request)


def process_feedback(event: FeedbackEvent) -> None:
    with private_sdk_diagnostics():
        asyncio.run(
            _process_learning(event.user_id, UUID(event.feedback_id), event.feedback_revision)
        )


async def _process_learning(
    owner: str, feedback_id: UUID, revision: int, *, generation: int | None = None
) -> None:
    async with open_cosmos(
        _settings(),
        containers=(ProductContainer.USERS, ProductContainer.RECIPE_HISTORY),
        lifetime_seconds=60,
    ) as repository:
        user = AuthenticatedUser(owner)

        async def apply() -> None:
            await LearningService(repository, _EnvironmentExtractor()).process(user, feedback_id)

        await LearningRecovery(repository, RecoveryPolicy.from_environment()).process(
            user, feedback_id, revision, apply, generation=generation
        )


def eligible_replay(document: Mapping[str, object]) -> ReplayEvent | None:
    if document.get("recordKind") != "processing" or document.get("role") != "learningReplay":
        return None
    owner = document.get("userId")
    if (
        type(document.get("schemaVersion")) is not int
        or document["schemaVersion"] != 1
        or not isinstance(owner, str)
        or not re.fullmatch(r"[0-9a-f]{64}", owner)
        or _timestamp(document.get("createdAt")) != _timestamp(document.get("updatedAt"))
        or document.get("ttl") is not None
    ):
        raise InvalidFeedback
    replay = LearningReplay.model_validate_json(
        json.dumps(
            {
                field.alias or name: document.get(field.alias or name)
                for name, field in LearningReplay.model_fields.items()
            }
        )
    )
    if document.get("id") != str(
        replay_id(AuthenticatedUser(owner), replay.feedback_id, replay.request_id)
    ):
        raise InvalidFeedback
    return ReplayEvent(owner, replay)


def process_replay(event: ReplayEvent) -> None:
    with private_sdk_diagnostics():
        asyncio.run(
            _process_learning(
                event.user_id,
                event.replay.feedback_id,
                event.replay.feedback_revision,
                generation=event.replay.generation,
            )
        )


def quarantine_feedback(document: Mapping[str, object]) -> None:
    with private_sdk_diagnostics():
        asyncio.run(_quarantine_feedback(document))


async def _quarantine_feedback(document: Mapping[str, object]) -> None:
    async with open_cosmos(
        _settings(), containers=(ProductContainer.RECIPE_HISTORY,), lifetime_seconds=15
    ) as repository:
        await LearningRecovery(repository).quarantine(document)


def process_batch(
    documents: Iterable[Mapping[str, object]],
    processor: Callable[[FeedbackEvent], None],
    *,
    quarantine: Callable[[Mapping[str, object]], None] | None = None,
    replay_processor: Callable[[ReplayEvent], None] | None = None,
) -> None:
    counts = {
        "received": 0,
        "ignored": 0,
        "dispatched": 0,
        "invalid": 0,
        "failed": 0,
        "quarantined": 0,
    }
    pending = False

    def persist_quarantine(document: Mapping[str, object]) -> bool:
        if quarantine is not None:
            try:
                quarantine(document)
            except Exception:
                return False
            counts["quarantined"] += 1
            return True
        return False

    for document in documents:
        counts["received"] += 1
        try:
            replay = eligible_replay(document)
            event = eligible_feedback(document)
        except Exception:
            counts["invalid"] += 1
            pending = not persist_quarantine(document) or pending
            continue
        if event is None and replay is None:
            counts["ignored"] += 1
            continue
        try:
            if replay is not None:
                if replay_processor is None:
                    raise LearningPending
                replay_processor(replay)
            elif event is not None:
                processor(event)
            counts["dispatched"] += 1
        except Exception as error:
            if (
                not isinstance(error, LearningPending)
                and not classify_failure(error)[0]
                and persist_quarantine(document)
            ):
                continue
            counts["failed"] += 1
            pending = True
    if pending:
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
    process_batch(
        documents,
        process_feedback,
        quarantine=quarantine_feedback,
        replay_processor=process_replay,
    )


def validate_configuration() -> None:
    RecoveryPolicy.from_environment()
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
