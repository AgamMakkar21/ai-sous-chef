"""Durable, owner-scoped learning attempts and explicit operator replay."""

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import UUID4, AwareDatetime, Field

from sous_chef.adapters.cosmos import CosmosRepository
from sous_chef.domain.authorization import AuthenticatedUser, UserDataDeleted
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    NewRecord,
    RecordKind,
    RecordPatch,
    StoredRecord,
)
from sous_chef.domain.extraction import ExtractionError, ExtractionFailure, ExtractionModel
from sous_chef.services.learning import HISTORY, _invalid, _parse
from sous_chef.services.recipe_lifecycle import _identity
from sous_chef.settings import ConfigurationError
from sous_chef.telemetry import ErrorCategory, Event
from sous_chef.tracing import learning_event

type Failure = (
    ExtractionFailure
    | CosmosFailure
    | Literal["operation_timeout", "internal_error", "attempt_abandoned", "invalid_event"]
)

# Invalid envelopes cannot supply an authenticated owner. This partition has no user data.
QUARANTINE_OWNER = hashlib.sha256(b"ai-sous-chef:learning-quarantine:v1").hexdigest()


class RecoveryPolicy(ExtractionModel):
    max_attempts: int = Field(default=3, ge=1, le=5)
    backoff_seconds: int = Field(default=60, ge=1, le=300)

    @classmethod
    def from_environment(cls) -> "RecoveryPolicy":
        try:
            return cls(
                max_attempts=int(os.environ.get("ASC_LEARNING_MAX_ATTEMPTS", "3")),
                backoff_seconds=int(os.environ.get("ASC_LEARNING_BACKOFF_SECONDS", "60")),
            )
        except ValueError:
            raise ConfigurationError("Invalid bounded learning recovery configuration.") from None


class LearningAttempt(ExtractionModel):
    role: Literal["learningRecovery"] = "learningRecovery"
    feedback_id: UUID4 = Field(repr=False)
    feedback_revision: int = Field(ge=1)
    generation: int = Field(default=0, ge=0)
    max_attempts: int = Field(ge=1, le=5)
    attempts: int = Field(ge=0, le=5)
    state: Literal["running", "retry", "completed", "failed"]
    available_at: AwareDatetime
    lease_id: UUID4 | None = Field(default=None, repr=False)
    error_category: Failure | None = None


class LearningReplay(ExtractionModel):
    role: Literal["learningReplay"] = "learningReplay"
    feedback_id: UUID4 = Field(repr=False)
    feedback_revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    request_id: UUID4 = Field(repr=False)


class LearningPending(RuntimeError):
    code = ErrorCategory.LEARNING_PENDING

    def __init__(self) -> None:
        super().__init__("Learning delivery remains pending.")


def attempt_id(user: AuthenticatedUser, feedback_id: UUID) -> UUID:
    return _identity(user, "learning-recovery:v1", str(feedback_id))


def replay_id(user: AuthenticatedUser, feedback_id: UUID, request_id: UUID) -> UUID:
    return _identity(user, "learning-replay:v1", f"{feedback_id}:{request_id}")


def classify_failure(error: Exception) -> tuple[bool, Failure]:
    if isinstance(error, ExtractionError):
        return error.code in {
            ExtractionFailure.TIMEOUT,
            ExtractionFailure.THROTTLED,
            ExtractionFailure.UNAVAILABLE,
        }, error.code
    if isinstance(error, CosmosError):
        return error.outcome_unknown or error.code in {
            CosmosFailure.TIMEOUT,
            CosmosFailure.THROTTLED,
            CosmosFailure.UNAVAILABLE,
            CosmosFailure.CONFLICT,
            CosmosFailure.EXISTS,
        }, error.code
    if isinstance(error, TimeoutError):
        return True, "operation_timeout"
    return False, "internal_error"


def _report(
    state: str, category: Failure | None, attempts: int = 0, generation: int | None = None
) -> None:
    learning_event(
        {
            "retry": Event.LEARNING_RETRY,
            "failed": Event.LEARNING_FAILED,
            "quarantined": Event.LEARNING_QUARANTINED,
            "completed": Event.LEARNING_COMPLETED,
        }[state],
        category=ErrorCategory(category) if category is not None else None,
        attempt=attempts,
        generation=generation,
    )


class LearningRecovery:
    def __init__(self, repository: CosmosRepository, policy: RecoveryPolicy | None = None) -> None:
        self.repository = repository
        self.policy = policy or RecoveryPolicy()

    async def inspect(
        self, user: AuthenticatedUser, feedback_id: UUID, revision: int
    ) -> tuple[StoredRecord, LearningAttempt] | None:
        record = await self.repository.read(user, HISTORY, str(attempt_id(user, feedback_id)))
        if record is None:
            return None
        attempt = _parse(LearningAttempt, record)
        if (
            record.body.get("role") != "learningRecovery"
            or attempt.feedback_id != feedback_id
            or attempt.feedback_revision != revision
            or attempt.attempts > attempt.max_attempts
        ):
            raise _invalid()
        return record, attempt

    async def _change(
        self, user: AuthenticatedUser, record: StoredRecord, **fields: object
    ) -> StoredRecord:
        # Validate through the same bounded persisted model before constructing a patch.
        current = _parse(LearningAttempt, record)
        updated = LearningAttempt.model_validate(
            {**current.model_dump(), **fields}, strict=True
        ).model_dump(mode="json", by_alias=True)
        changed = {key: value for key, value in updated.items() if value != record.body.get(key)}
        return await self.repository.patch(
            user, HISTORY, str(record.body["id"]), RecordPatch(etag=record.etag, fields=changed)
        )

    async def process(
        self,
        user: AuthenticatedUser,
        feedback_id: UUID,
        revision: int,
        action: Callable[[], Awaitable[None]],
        *,
        generation: int | None = None,
        now: datetime | None = None,
    ) -> None:
        started_at = now or datetime.now(UTC)
        found = await self.inspect(user, feedback_id, revision)
        if found is None:
            if generation is not None:
                raise _invalid()
            attempt = LearningAttempt(
                feedback_id=feedback_id,
                feedback_revision=revision,
                max_attempts=self.policy.max_attempts,
                attempts=1,
                state="running",
                available_at=started_at + timedelta(seconds=120),
                lease_id=uuid4(),
            )
            record = await self.repository.create(
                user,
                NewRecord(
                    kind=RecordKind.PROCESSING,
                    record_id=attempt_id(user, feedback_id),
                    fields=attempt.model_dump(mode="json", by_alias=True),
                ),
            )
        else:
            record, attempt = found
            if generation is not None and generation != attempt.generation:
                learning_event(
                    Event.LEARNING_STALE_REPLAY, generation=generation, attempt=attempt.attempts
                )
                return
            if attempt.state in {"completed", "failed"}:
                if attempt.state == "completed":
                    learning_event(
                        Event.LEARNING_DEDUPLICATED,
                        attempt=attempt.attempts,
                        generation=attempt.generation,
                        contributions=0,
                    )
                else:
                    _report("failed", attempt.error_category, attempt.attempts, attempt.generation)
                return
            if started_at < attempt.available_at:
                learning_event(
                    Event.LEARNING_DEFERRED, attempt=attempt.attempts, generation=attempt.generation
                )
                raise LearningPending
            if attempt.attempts >= attempt.max_attempts:
                await self._change(
                    user,
                    record,
                    state="failed",
                    lease_id=None,
                    error_category="attempt_abandoned",
                )
                _report("failed", "attempt_abandoned", attempt.attempts, attempt.generation)
                return
            record = await self._change(
                user,
                record,
                state="running",
                attempts=attempt.attempts + 1,
                available_at=started_at + timedelta(seconds=120),
                lease_id=uuid4(),
            )
            attempt = _parse(LearningAttempt, record)
        learning_event(
            Event.LEARNING_ATTEMPT, attempt=attempt.attempts, generation=attempt.generation
        )
        try:
            # Leave time in the worker's 60-second storage scope for durable bookkeeping.
            async with asyncio.timeout(45):
                await action()
        except UserDataDeleted:
            raise
        except Exception as error:
            transient, category = classify_failure(error)
            retry = transient and attempt.attempts < attempt.max_attempts
            await self._change(
                user,
                record,
                state="retry" if retry else "failed",
                lease_id=None,
                error_category=category,
                available_at=(now or datetime.now(UTC))
                + timedelta(
                    seconds=min(300, self.policy.backoff_seconds * 2 ** (attempt.attempts - 1))
                ),
            )
            _report("retry" if retry else "failed", category, attempt.attempts, attempt.generation)
            if retry:
                raise LearningPending from None
            return
        await self._change(user, record, state="completed", lease_id=None, error_category=None)
        _report("completed", None, attempt.attempts, attempt.generation)

    async def replay(
        self,
        user: AuthenticatedUser,
        feedback_id: UUID,
        revision: int,
        request_id: UUID,
        *,
        now: datetime | None = None,
    ) -> Literal["requested", "already_requested", "already_completed"]:
        now = now or datetime.now(UTC)
        identifier = replay_id(user, feedback_id, request_id)
        existing = await self.repository.read(user, HISTORY, str(identifier))
        if existing is not None:
            replay = _parse(LearningReplay, existing)
            if (
                existing.body.get("role") != "learningReplay"
                or replay.feedback_id != feedback_id
                or replay.feedback_revision != revision
                or replay.request_id != request_id
            ):
                raise _invalid()
            return "already_requested"
        found = await self.inspect(user, feedback_id, revision)
        if found is None:
            raise ValueError("A replay requires a failed learning record.")
        record, attempt = found
        if attempt.state == "completed":
            return "already_completed"
        if attempt.state != "failed":
            raise ValueError("A replay requires a failed learning record.")
        replay = LearningReplay(
            feedback_id=feedback_id,
            feedback_revision=revision,
            generation=attempt.generation + 1,
            request_id=request_id,
        )
        await self.repository.commit_learning_replay(
            user,
            request=NewRecord(
                kind=RecordKind.PROCESSING,
                record_id=identifier,
                fields=replay.model_dump(mode="json", by_alias=True),
            ),
            current=record,
            patch=RecordPatch(
                etag=record.etag,
                fields={
                    "state": "retry",
                    "attempts": 0,
                    "generation": replay.generation,
                    "leaseId": None,
                    "availableAt": now.isoformat(),
                    "errorCategory": None,
                },
            ),
        )
        return "requested"

    async def quarantine(self, document: Mapping[str, object]) -> None:
        # Hash only the envelope, never notes, prompts, model output or exception text.
        fingerprint = hashlib.sha256(
            json.dumps(
                {key: document.get(key) for key in ("id", "userId", "schemaVersion")},
                sort_keys=True,
                default=lambda _: None,
            ).encode()
        ).hexdigest()
        owner = document.get("userId")
        user = AuthenticatedUser(
            owner
            if isinstance(owner, str) and re.fullmatch(r"[0-9a-f]{64}", owner)
            else QUARANTINE_OWNER
        )
        identifier = _identity(user, "learning-quarantine:v1", fingerprint)
        existing = await self.repository.read(user, HISTORY, str(identifier))
        if existing is not None:
            if (
                existing.body.get("role") != "learningQuarantine"
                or existing.body.get("state") != "quarantined"
                or existing.body.get("sourceFingerprint") != fingerprint
            ):
                raise _invalid()
            learning_event(Event.LEARNING_QUARANTINED)
            return
        source_id = document.get("id")
        try:
            parsed_id = UUID(source_id) if isinstance(source_id, str) else None
        except ValueError:
            parsed_id = None
        await self.repository.create(
            user,
            NewRecord(
                kind=RecordKind.PROCESSING,
                record_id=identifier,
                fields={
                    "role": "learningQuarantine",
                    "state": "quarantined",
                    "errorCategory": "invalid_event",
                    "sourceFingerprint": fingerprint,
                    "sourceId": str(parsed_id) if parsed_id and parsed_id.version == 4 else None,
                },
            ),
        )
        _report("quarantined", "invalid_event")
