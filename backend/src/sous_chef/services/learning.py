"""Durable feedback receipts and a recoverable, conditional Users projection."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError

from sous_chef.adapters.cosmos import CosmosRepository
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.context import (
    ContextPreference,
    LearnedCandidate,
    PreferenceEvidence,
    ResolvedPreference,
)
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    NewRecord,
    ProductContainer,
    RecordKind,
    RecordPatch,
    StoredRecord,
)
from sous_chef.domain.extraction import (
    MAX_EXTRACTION_PRIOR,
    ExtractionModel,
    ExtractionProfile,
    ExtractionRequest,
    ExtractionResult,
    ExtractionSource,
)
from sous_chef.domain.feedback import FeedbackRevision
from sous_chef.domain.learning import (
    LearningContribution,
    LearningReceipt,
    LearningSource,
    LearningWindow,
    advance_window,
    independent_evidence,
    reduce_preferences,
)
from sous_chef.domain.profiles import require_onboarding
from sous_chef.services.profiles import _view as profile_view
from sous_chef.services.recipe_lifecycle import _history, _identity
from sous_chef.telemetry import Event
from sous_chef.tracing import Operation, learning_event, observed

HISTORY = ProductContainer.RECIPE_HISTORY
USERS = ProductContainer.USERS


class Extractor(Protocol):
    async def extract(self, request: ExtractionRequest) -> ExtractionResult: ...


def _invalid() -> CosmosError:
    return CosmosError(CosmosFailure.INVALID_RESULT, uuid4())


def _parse[Model: ExtractionModel](model: type[Model], record: StoredRecord) -> Model:
    fields = {
        field.alias or name: record.body[field.alias or name]
        for name, field in model.model_fields.items()
        if (field.alias or name) in record.body
    }
    try:
        return model.model_validate_json(json.dumps(fields))
    except ValidationError:
        raise _invalid() from None


def _window_id(user: AuthenticatedUser) -> UUID:
    return _identity(user, "learning-window", "v1")


def _receipt_id(user: AuthenticatedUser, feedback_id: UUID) -> UUID:
    return _identity(user, "learning-application:v1", str(feedback_id))


def _pointer(source: ExtractionSource) -> LearningSource:
    feedback = source.feedback
    return LearningSource(
        history_id=feedback.history_id,
        occasion_id=feedback.occasion_id,
        feedback_id=feedback.feedback_id,
        feedback_revision=feedback.feedback_revision,
        observed_at=feedback.observed_at,
    )


class LearningService:
    def __init__(self, repository: CosmosRepository, extractor: Extractor | None = None) -> None:
        self.repository = repository
        self.extractor = extractor

    async def _window(
        self, user: AuthenticatedUser
    ) -> tuple[StoredRecord | None, LearningWindow | None]:
        record = await self.repository.read(user, HISTORY, str(_window_id(user)))
        if record is None:
            return None, None
        if record.body["recordKind"] != "processing" or record.body.get("role") != "learningWindow":
            raise _invalid()
        window = _parse(LearningWindow, record)
        if len({source.history_id for source in window.sources}) != len(window.sources) or len(
            {source.occasion_id for source in window.sources}
        ) != len(window.sources):
            raise _invalid()
        return record, window

    async def _source(
        self, user: AuthenticatedUser, feedback_id: UUID
    ) -> tuple[StoredRecord, ExtractionSource] | None:
        event = await self.repository.read(user, HISTORY, str(feedback_id))
        if event is None:
            return None
        if event.body["recordKind"] != "feedbackRevision":
            raise _invalid()
        try:
            feedback = FeedbackRevision.model_validate_json(
                json.dumps(
                    {
                        field.alias or name: event.body[field.alias or name]
                        for name, field in FeedbackRevision.model_fields.items()
                        if (field.alias or name) in event.body
                    }
                )
            )
        except ValidationError:
            raise _invalid() from None
        if feedback.feedback_id != feedback_id:
            raise _invalid()
        record = await self.repository.read(user, HISTORY, str(feedback.history_id))
        if record is None:
            return None
        view = _history(record)
        if view.feedback_revision != feedback.feedback_revision or view.cooked_at is None:
            return None
        if (
            view.rating != feedback.rating
            or view.note != feedback.note
            or view.occasion_id != feedback.occasion_id
            or view.generated_recipe_id != feedback.generated_recipe_id
            or view.source_conversation_id != feedback.source_conversation_id
            or view.source_turn_id != feedback.source_turn_id
        ):
            raise _invalid()
        try:
            source = ExtractionSource(
                user_id=user.user_id,
                feedback=feedback,
                cooked_at=view.cooked_at,
                title=view.recipe.title,
                ingredients=tuple(item.name for item in view.recipe.ingredients),
            )
        except ValidationError:
            raise _invalid() from None
        return record, source

    async def _current(
        self, user: AuthenticatedUser, window: LearningWindow | None, *, now: datetime
    ) -> tuple[tuple[ExtractionSource, LearningReceipt], ...]:
        current = []
        for pointer in window.sources if window else ():
            if not now - timedelta(days=180) <= pointer.observed_at <= now:
                continue
            record = await self.repository.read(
                user, HISTORY, str(_receipt_id(user, pointer.feedback_id))
            )
            if record is None:
                raise _invalid()
            receipt = _parse(LearningReceipt, record)
            if (
                record.body["recordKind"] != "processing"
                or record.body.get("role") != "feedbackApplication"
                or receipt.source != pointer
            ):
                raise _invalid()
            source = await self._source(user, pointer.feedback_id)
            if source is not None:
                if _pointer(source[1]) != pointer:
                    raise _invalid()
                for item in receipt.contributions:
                    evidence = item.evidence
                    if (
                        evidence.user_id != user.user_id
                        or evidence.source_id != pointer.history_id
                        or evidence.source_revision != pointer.feedback_revision
                        or evidence.observed_at != pointer.observed_at
                        or evidence.occasion_id != source[1].feedback.occasion_id
                        or not evidence.cooked
                    ):
                        raise _invalid()
                current.append((source[1], receipt))
        return tuple(current)

    async def process(
        self, user: AuthenticatedUser, feedback_id: UUID, *, now: datetime | None = None
    ) -> None:
        now = now or datetime.now(UTC)
        receipt_id = _receipt_id(user, feedback_id)
        existing = await self.repository.read(user, HISTORY, str(receipt_id))
        if existing is None:
            source = await self._source(user, feedback_id)
            if source is not None:
                record, current = source
                if current.feedback.observed_at > now:
                    raise _invalid()
                window_record, window = await self._window(user)
                profile = await self.repository.read(user, USERS, user.user_id)
                if profile is None:
                    return
                explicit = require_onboarding(profile_view(profile))
                prior = await self._current(user, window, now=now)
                resolved = reduce_preferences(
                    tuple(item for _, receipt in prior for item in receipt.contributions),
                    explicit,
                    now=now,
                )
                contributions: list[LearningContribution] = []
                if now - timedelta(days=180) <= current.feedback.observed_at <= now:
                    if self.extractor is None:
                        raise RuntimeError("Feedback application requires a configured extractor.")
                    result = await self.extractor.extract(
                        ExtractionRequest(
                            current=current,
                            profile=ExtractionProfile(
                                user_id=user.user_id,
                                explicit_preferences=explicit,
                                learned_preferences=tuple(
                                    ContextPreference(
                                        insight_key=item.candidate.insight_key,
                                        field=item.field,
                                        value=item.value,
                                        confidence=item.candidate.confidence,
                                        supporting_cooked_count=(
                                            item.candidate.supporting_cooked_count
                                        ),
                                    )
                                    for item in resolved[:20]
                                ),
                            ),
                            prior=tuple(
                                item
                                for item, _ in prior
                                if item.feedback.history_id != current.feedback.history_id
                                and item.feedback.occasion_id != current.feedback.occasion_id
                            )[:MAX_EXTRACTION_PRIOR],
                            now=now,
                        )
                    )
                    for extracted in result.candidates:
                        assertion = next(
                            (
                                item
                                for item in extracted.assertions
                                if item.feedback_id == feedback_id
                            ),
                            None,
                        )
                        feedback = current.feedback
                        if (
                            assertion is None
                            or assertion.source_id != feedback.history_id
                            or assertion.source_revision != feedback.feedback_revision
                            or assertion.occasion_id != feedback.occasion_id
                            or feedback.note is None
                            or assertion.excerpt not in feedback.note
                        ):
                            raise _invalid()
                        preference = extracted.preference
                        contributions.append(
                            LearningContribution(
                                insight_key=preference.candidate.insight_key,
                                insight=preference.candidate.insight,
                                field=preference.field,
                                value=preference.value,
                                evidence=PreferenceEvidence(
                                    user_id=user.user_id,
                                    occasion_id=feedback.occasion_id,
                                    source_id=feedback.history_id,
                                    source_revision=feedback.feedback_revision,
                                    observed_at=feedback.observed_at,
                                    cooked=True,
                                    direction=assertion.direction,
                                ),
                            )
                        )
                receipt = LearningReceipt(
                    source=_pointer(current),
                    outcome="candidates" if contributions else "no_insight",
                    contributions=tuple(contributions),
                )
                next_window = advance_window(window, receipt.source, now=now)
                fields = next_window.model_dump(mode="json", by_alias=True)
                transition: NewRecord | tuple[StoredRecord, RecordPatch]
                if window_record is None:
                    transition = NewRecord(
                        kind=RecordKind.PROCESSING,
                        record_id=_window_id(user),
                        fields=fields,
                    )
                else:
                    transition = (
                        window_record,
                        RecordPatch(
                            etag=window_record.etag,
                            fields={name: fields[name] for name in ("generation", "sources")},
                        ),
                    )
                await self.repository.commit_learning(
                    user,
                    receipt=NewRecord(
                        kind=RecordKind.PROCESSING,
                        record_id=receipt_id,
                        fields=receipt.model_dump(mode="json", by_alias=True),
                    ),
                    window=transition,
                    source=record,
                )
                learning_event(Event.LEARNING_LEDGER_COMMITTED, contributions=len(contributions))
        elif (
            existing.body["recordKind"] != "processing"
            or existing.body.get("role") != "feedbackApplication"
            or _parse(LearningReceipt, existing).source.feedback_id != feedback_id
        ):
            raise _invalid()
        else:
            learning_event(Event.LEARNING_DEDUPLICATED, contributions=0)
        await self.reconcile(user, now=now)
        source = await self._source(user, feedback_id)
        if source is not None and source[0].body.get("processingStatus") != "completed":
            await self.repository.patch(
                user,
                HISTORY,
                str(source[1].feedback.history_id),
                RecordPatch(etag=source[0].etag, fields={"processingStatus": "completed"}),
            )

    @observed(Operation.PROJECTION)
    async def reconcile(
        self, user: AuthenticatedUser, *, now: datetime | None = None
    ) -> tuple[ResolvedPreference, ...]:
        now = now or datetime.now(UTC)
        # Read the profile BEFORE the ledger snapshot so a concurrent projection
        # cannot be overwritten by a stale one with a freshly acquired profile ETag.
        profile = await self.repository.read(user, USERS, user.user_id)
        if profile is None:
            return ()
        record, window = await self._window(user)
        generation = profile.body.get("learningGeneration", 0)
        if type(generation) is not int or generation < 0:
            raise _invalid()
        next_generation = window.generation if window else 0
        if generation > next_generation:
            raise CosmosError(CosmosFailure.CONFLICT, uuid4())
        explicit = require_onboarding(profile_view(profile))
        current = await self._current(user, window, now=now)
        resolved = reduce_preferences(
            tuple(item for _, receipt in current for item in receipt.contributions),
            explicit,
            now=now,
        )
        latest, _ = await self._window(user)
        if (record.etag if record else None) != (latest.etag if latest else None):
            raise CosmosError(CosmosFailure.CONFLICT, uuid4())
        fields: dict[str, JsonValue] = {
            "learnedPreferences": [
                item.candidate.model_dump(mode="json", by_alias=True) for item in resolved
            ],
            "learningGeneration": next_generation,
        }
        if (
            profile.body.get("learnedPreferences") != fields["learnedPreferences"]
            or generation != next_generation
        ):
            await self.repository.patch(
                user, USERS, user.user_id, RecordPatch(etag=profile.etag, fields=fields)
            )
            learning_event(Event.LEARNING_USER_UPDATED, insights=len(resolved))
        return resolved


class LearningPreferenceReader:
    def __init__(self, repository: CosmosRepository) -> None:
        self.repository = repository

    async def resolve(
        self,
        user: AuthenticatedUser,
        candidates: tuple[LearnedCandidate, ...],
        *,
        now: datetime,
        deadline: float,
    ) -> tuple[ResolvedPreference, ...]:
        async with asyncio.timeout_at(deadline):
            return await self._resolve(user, candidates, now=now)

    async def _resolve(
        self,
        user: AuthenticatedUser,
        candidates: tuple[LearnedCandidate, ...],
        *,
        now: datetime,
    ) -> tuple[ResolvedPreference, ...]:
        service = LearningService(self.repository)
        _, window = await service._window(user)
        current = await service._current(user, window, now=now)
        contributions = tuple(item for _, receipt in current for item in receipt.contributions)
        result = []
        for candidate in candidates:
            matching = [item for item in contributions if item.insight_key == candidate.insight_key]
            if not matching:
                # Empty evidence explicitly invalidates a stale profile candidate.
                result.append(
                    ResolvedPreference(
                        candidate=candidate, field="likes", value="invalidated", evidence=()
                    )
                )
                continue
            matching.sort(key=lambda item: (item.field, item.value, item.insight))
            result.append(
                ResolvedPreference(
                    candidate=candidate,
                    field=matching[0].field,
                    value=matching[0].value,
                    evidence=independent_evidence(matching),
                )
            )
        return tuple(result)
