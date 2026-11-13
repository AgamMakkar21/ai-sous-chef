import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from time import monotonic
from typing import Any
from uuid import uuid4

import pytest
from openai.types.responses import Response
from sous_chef.adapters.cosmos import CosmosRepository, open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.context import LearnedCandidate
from sous_chef.domain.cosmos import (
    MAX_RECORD_BYTES,
    CosmosError,
    CosmosFailure,
    NewRecord,
    ProductContainer,
    RecordKind,
    RecordPatch,
    StoredRecord,
)
from sous_chef.domain.extraction import ExtractionRequest, ExtractionResult, ExtractionSource
from sous_chef.domain.learning import (
    LearningContribution,
    LearningSource,
    advance_window,
    reduce_preferences,
)
from sous_chef.domain.recipe_lifecycle import RecipeHistory
from sous_chef.services.context import _eligible
from sous_chef.services.learning import (
    LearningPreferenceReader,
    LearningService,
    _receipt_id,
    _window_id,
)
from sous_chef.services.recipe_lifecycle import _identity
from test_cosmos_repository import SyntheticCredential, Transport, settings
from test_cosmos_repository import harness as harness
from test_preference_extraction import NOW, config, profile, proposal, response_body, source
from test_recipe_safety import full_recipe

from worker import function_app as worker
from worker.extraction import ExtractionConfig, _result

OWNER = AuthenticatedUser("a" * 64)
HISTORY = ProductContainer.RECIPE_HISTORY
USERS = ProductContainer.USERS


class Extractor:
    def __init__(self) -> None:
        self.calls: list[ExtractionRequest] = []
        self.no_insight = False
        self.oppose = False

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        self.calls.append(request)
        await asyncio.sleep(0)
        candidate = proposal(request.current)
        if self.oppose:
            candidate["evidence"][0]["direction"] = "oppose"
        draft = (
            {"outcome": "no_insight", "candidates": []}
            if self.no_insight
            else {"outcome": "candidates", "candidates": [candidate]}
        )
        return _result(
            Response.model_validate(response_body(draft)), request, (request.current,), config()
        )


async def seed_profile(repository: CosmosRepository) -> StoredRecord:
    return await repository.create(
        OWNER,
        NewRecord(
            kind=RecordKind.PROFILE,
            fields={
                "onboardingCompleted": True,
                "explicitPreferences": profile().explicit_preferences.model_dump(
                    mode="json", by_alias=True
                ),
                "groceries": [{"privateSyntheticInventory": True}],
                "learnedPreferences": [],
            },
        ),
    )


async def seed_source(repository: CosmosRepository, item: ExtractionSource) -> None:
    feedback = item.feedback
    history = RecipeHistory(
        history_id=feedback.history_id,
        generated_recipe_id=feedback.generated_recipe_id,
        occasion_id=feedback.occasion_id,
        supersedes_recipe_id=None,
        source_conversation_id=feedback.source_conversation_id,
        source_turn_id=feedback.source_turn_id,
        recipe=full_recipe(("lime", "rice")),
        saved_at=item.cooked_at - timedelta(minutes=1),
        cooked_at=item.cooked_at,
        rating=feedback.rating,
        note=feedback.note,
        feedback_revision=feedback.feedback_revision,
        processing_status="pending",
    )
    await repository.create(
        OWNER,
        NewRecord(
            kind=RecordKind.RECIPE,
            record_id=feedback.history_id,
            fields=history.model_dump(
                mode="json", by_alias=True, exclude={"state", "awaiting_feedback"}
            ),
        ),
    )
    await seed_event(repository, item)


async def seed_event(repository: CosmosRepository, item: ExtractionSource) -> None:
    await repository.create(
        OWNER,
        NewRecord(
            kind=RecordKind.FEEDBACK_REVISION,
            record_id=item.feedback.feedback_id,
            fields=item.feedback.model_dump(mode="json", by_alias=True),
        ),
    )


def stored_profile(transport: Transport) -> dict[str, Any]:
    return transport.documents[("Users", OWNER.user_id, OWNER.user_id)]


def test_replay_and_restart_count_each_feedback_once_and_preserve_explicit_inventory(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    async def scenario() -> None:
        first, second = source(days=1), source()
        extractor = Extractor()
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            original = await seed_profile(repository)
            for item in (first, second):
                await seed_source(repository, item)
                await LearningService(repository, extractor).process(
                    OWNER, item.feedback.feedback_id, now=NOW
                )
                await LearningService(repository, extractor).process(
                    OWNER, item.feedback.feedback_id, now=NOW
                )
            result = stored_profile(harness[0])
            assert result["explicitPreferences"] == original.body["explicitPreferences"]
            assert result["groceries"] == original.body["groceries"]
            assert len(result["learnedPreferences"]) == 1
            candidate = result["learnedPreferences"][0]
            assert candidate["evidenceCount"] == candidate["supportingCookedCount"] == 2
            assert candidate["confidence"] == 0.75
            assert candidate["supportUnits"] == 6 and candidate["oppositionUnits"] == 0
            assert len(extractor.calls) == 2
            assert len(json.dumps(result).encode()) <= MAX_RECORD_BYTES
            assert "processedEventIds" not in result
            internal = [
                document
                for document in harness[0].documents.values()
                if document.get("role") == "feedbackApplication"
            ]
            assert len(internal) == 2 and all(
                item["recordKind"] == "processing" for item in internal
            )
            candidates = tuple(
                LearnedCandidate.model_validate(item) for item in result["learnedPreferences"]
            )
            resolved = await LearningPreferenceReader(repository).resolve(
                OWNER, candidates, now=NOW, deadline=monotonic() + 5
            )
            eligible = _eligible(OWNER, resolved, candidates, profile().explicit_preferences, NOW)
            assert len(eligible) == 1 and eligible[0].confidence == 0.75
        batch_calls = [
            call
            for call in harness[0].calls
            if isinstance(call.body, list)
            and any(
                item.get("resourceBody", {}).get("role") == "feedbackApplication"
                for item in call.body
            )
        ]
        assert len(batch_calls) == 2 and all(len(call.body) == 4 for call in batch_calls)
        assert all("/RecipeHistory/" in call.path for call in batch_calls)

    asyncio.run(scenario())


@pytest.mark.parametrize("crash_at", ["before_projection", "after_projection", "before_status"])
def test_durable_receipt_recovers_crashes_without_extraction_or_duplicate_evidence(
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    crash_at: str,
) -> None:
    async def scenario() -> None:
        item = source()
        extractor = Extractor()
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await seed_profile(repository)
            await seed_source(repository, item)
            original = repository.patch

            async def interrupted(
                user: AuthenticatedUser,
                container: ProductContainer,
                item_id: str,
                request: RecordPatch,
                **kwargs: Any,
            ) -> StoredRecord:
                target = (
                    container == USERS
                    if crash_at != "before_status"
                    else (request.fields == {"processingStatus": "completed"})
                )
                if target:
                    if crash_at == "after_projection":
                        await original(user, container, item_id, request, **kwargs)
                    raise RuntimeError("Synthetic crash")
                return await original(user, container, item_id, request, **kwargs)

            monkeypatch.setattr(repository, "patch", interrupted)
            with pytest.raises(RuntimeError, match="Synthetic crash"):
                await LearningService(repository, extractor).process(
                    OWNER, item.feedback.feedback_id, now=NOW
                )
            assert await repository.read(
                OWNER, HISTORY, str(_receipt_id(OWNER, item.feedback.feedback_id))
            )
            monkeypatch.setattr(repository, "patch", original)
            await LearningService(repository, extractor).process(
                OWNER, item.feedback.feedback_id, now=NOW
            )
            result = stored_profile(harness[0])["learnedPreferences"]
            assert len(result) == 1 and result[0]["evidenceCount"] == 1
            assert len(extractor.calls) == 1
            current = await repository.read(OWNER, HISTORY, str(item.feedback.history_id))
            assert current is not None and current.body["processingStatus"] == "completed"

    asyncio.run(scenario())


def test_unacknowledged_atomic_commit_is_reconciled_without_replaying_extraction(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    async def scenario() -> None:
        extractor = Extractor()
        item = source()
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await seed_profile(repository)
            await seed_source(repository, item)
            harness[0].write_then_disconnect = True
            with pytest.raises(CosmosError) as failure:
                await LearningService(repository, extractor).process(
                    OWNER, item.feedback.feedback_id, now=NOW
                )
            assert failure.value.outcome_unknown
            harness[0].write_then_disconnect = False
            await LearningService(repository, extractor).process(
                OWNER, item.feedback.feedback_id, now=NOW
            )
            assert stored_profile(harness[0])["learnedPreferences"][0]["evidenceCount"] == 1
            assert len(extractor.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("new_signal", ["oppose", "no_insight"])
@pytest.mark.parametrize("initial_processed", [False, True])
def test_edit_replaces_evidence_and_late_original_cannot_restore_it(
    harness: tuple[Transport, SyntheticCredential], new_signal: str, initial_processed: bool
) -> None:
    async def scenario() -> None:
        extractor = Extractor()
        item = source()
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await seed_profile(repository)
            await seed_source(repository, item)
            service = LearningService(repository, extractor)
            if initial_processed:
                await service.process(OWNER, item.feedback.feedback_id, now=NOW)
            feedback = item.feedback.model_copy(
                update={
                    "feedback_revision": 2,
                    "feedback_id": _identity(
                        OWNER, "recipe-feedback", f"{item.feedback.history_id}:2"
                    ),
                    "supersedes_feedback_id": item.feedback.feedback_id,
                    "note": "Private synthetic note: no more lime.",
                }
            )
            edited = item.model_copy(update={"feedback": feedback})
            await seed_event(repository, edited)
            current = await repository.read(OWNER, HISTORY, str(feedback.history_id))
            assert current is not None
            await repository.patch(
                OWNER,
                HISTORY,
                str(feedback.history_id),
                RecordPatch(
                    etag=current.etag,
                    fields={
                        "feedbackRevision": 2,
                        "note": feedback.note,
                        "processingStatus": "pending",
                    },
                ),
            )
            extractor.oppose = new_signal == "oppose"
            extractor.no_insight = new_signal == "no_insight"
            await service.process(OWNER, feedback.feedback_id, now=NOW)
            await service.process(OWNER, item.feedback.feedback_id, now=NOW)
            learned = stored_profile(harness[0])["learnedPreferences"]
            if new_signal == "no_insight":
                assert learned == []
            else:
                assert learned[0]["evidenceCount"] == 1
                assert learned[0]["supportUnits"] == 0 and learned[0]["oppositionUnits"] == 3
                assert learned[0]["confidence"] == 1 / 3
            assert len(extractor.calls) == 1 + int(initial_processed)

    asyncio.run(scenario())


def test_deleted_or_superseded_evidence_is_excluded_before_profile_repair(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    async def scenario() -> None:
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await seed_profile(repository)
            items = (source(days=1), source())
            service = LearningService(repository, Extractor())
            for item in items:
                await seed_source(repository, item)
                await service.process(OWNER, item.feedback.feedback_id, now=NOW)
            candidates = tuple(
                LearnedCandidate.model_validate(item)
                for item in stored_profile(harness[0])["learnedPreferences"]
            )
            current = await repository.read(OWNER, HISTORY, str(items[0].feedback.history_id))
            assert current is not None
            await repository.delete(OWNER, HISTORY, str(items[0].feedback.history_id), current.etag)
            resolved = await LearningPreferenceReader(repository).resolve(
                OWNER, candidates, now=NOW, deadline=monotonic() + 5
            )
            assert not _eligible(OWNER, resolved, candidates, profile().explicit_preferences, NOW)
            await service.reconcile(OWNER, now=NOW)
            assert stored_profile(harness[0])["learnedPreferences"][0]["evidenceCount"] == 1
            await service.reconcile(OWNER, now=NOW + timedelta(days=181))
            assert stored_profile(harness[0])["learnedPreferences"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize("same_event", [False, True])
def test_concurrent_feedback_replays_conflict_without_duplicate_or_lost_contributions(
    harness: tuple[Transport, SyntheticCredential],
    same_event: bool,
) -> None:
    async def scenario() -> None:
        items = (source(days=1), source())
        extractor = Extractor()
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await seed_profile(repository)
            for item in items:
                await seed_source(repository, item)
            service = LearningService(repository, extractor)
            results = await asyncio.gather(
                *(
                    service.process(OWNER, item.feedback.feedback_id, now=NOW)
                    for item in ((items[0], items[0]) if same_event else items)
                ),
                return_exceptions=True,
            )
            assert any(isinstance(result, CosmosError) for result in results)
            for item in (items[0],) if same_event else items:
                await service.process(OWNER, item.feedback.feedback_id, now=NOW)
            candidate = stored_profile(harness[0])["learnedPreferences"][0]
            assert candidate["evidenceCount"] == (1 if same_event else 2)
            assert candidate["confidence"] == (2 / 3 if same_event else 0.75)

    asyncio.run(scenario())


def test_change_feed_processor_connects_owned_storage_extraction_and_durable_application(
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = source()
    extractor = Extractor()

    async def seed() -> None:
        async with open_cosmos(settings()) as repository:
            await seed_profile(repository)
            await seed_source(repository, item)

    asyncio.run(seed())
    scopes = []

    @asynccontextmanager
    async def worker_repository(_settings: Any, **kwargs: Any) -> AsyncIterator[CosmosRepository]:
        scopes.append(kwargs["containers"])
        logging.getLogger("azure.cosmos.synthetic").debug(
            "Private synthetic storage diagnostic: %s / %s", item.feedback.note, OWNER.user_id
        )
        async with open_cosmos(settings(), **kwargs) as repository:
            yield repository

    monkeypatch.setenv("ASC_ENVIRONMENT", "dev")
    monkeypatch.setenv("FeedbackCosmos__accountEndpoint", "https://cosmos.example.invalid")
    monkeypatch.setenv("ASC_COSMOS_DATABASE", "sous-chef")
    monkeypatch.setattr(worker, "open_cosmos", worker_repository)
    monkeypatch.setattr(ExtractionConfig, "from_environment", lambda: config())
    monkeypatch.setattr(worker, "PreferenceExtractor", lambda _config: extractor)
    document = harness[0].documents[
        ("RecipeHistory", OWNER.user_id, str(item.feedback.feedback_id))
    ]
    caplog.set_level(logging.DEBUG)
    factory = logging.getLogRecordFactory()
    try:
        logging.setLogRecordFactory(logging.LogRecord)
        worker.process_batch([document, document], worker.process_feedback)
    finally:
        logging.setLogRecordFactory(factory)
    assert len(extractor.calls) == 1
    assert scopes == [(USERS, HISTORY), (USERS, HISTORY)]
    assert stored_profile(harness[0])["learnedPreferences"][0]["evidenceCount"] == 1
    assert item.feedback.note is not None and item.feedback.note not in caplog.text
    assert OWNER.user_id not in caplog.text
    assert "Preference extraction SDK diagnostic suppressed." in caplog.text


def test_reversed_delivery_reduces_to_the_same_profile(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    items = (source(days=2), source(days=1), source())

    async def scenario(reverse: bool) -> list[dict[str, Any]]:
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await seed_profile(repository)
            for item in items:
                await seed_source(repository, item)
            extractor = Extractor()
            service = LearningService(repository, extractor)
            for item in reversed(items) if reverse else items:
                extractor.oppose = item == items[2]
                await service.process(OWNER, item.feedback.feedback_id, now=NOW)
            result: list[dict[str, Any]] = stored_profile(harness[0])["learnedPreferences"]
            return result

    forward = asyncio.run(scenario(False))
    harness[0].documents.clear()
    backward = asyncio.run(scenario(True))
    assert forward == backward
    assert forward[0]["evidenceCount"] == 3 and forward[0]["confidence"] == 0.6


def test_profile_conflict_does_not_overwrite_explicit_or_grocery_edits(
    harness: tuple[Transport, SyntheticCredential], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        item = source()
        extractor = Extractor()
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await seed_profile(repository)
            await seed_source(repository, item)
            original = repository.patch
            edited = False

            async def concurrent(
                user: AuthenticatedUser,
                container: ProductContainer,
                item_id: str,
                request: RecordPatch,
                **kwargs: Any,
            ) -> StoredRecord:
                nonlocal edited
                if container == USERS and not edited:
                    edited = True
                    await original(
                        user,
                        USERS,
                        item_id,
                        RecordPatch(
                            etag=request.etag,
                            fields={
                                "explicitPreferences": profile(
                                    dislikes=["lime"]
                                ).explicit_preferences.model_dump(mode="json", by_alias=True),
                                "groceries": [{"privateConcurrentInventory": True}],
                            },
                        ),
                    )
                return await original(user, container, item_id, request, **kwargs)

            monkeypatch.setattr(repository, "patch", concurrent)
            service = LearningService(repository, extractor)
            with pytest.raises(CosmosError) as failure:
                await service.process(OWNER, item.feedback.feedback_id, now=NOW)
            assert failure.value.code == CosmosFailure.CONFLICT
            await service.process(OWNER, item.feedback.feedback_id, now=NOW)
            result = stored_profile(harness[0])
            assert result["learnedPreferences"] == []
            assert result["explicitPreferences"]["dislikes"] == ["lime"]
            assert result["groceries"] == [{"privateConcurrentInventory": True}]
            assert len(extractor.calls) == 1

    asyncio.run(scenario())


def test_window_and_candidate_bounds_have_deterministic_eviction_and_no_duplicate_occasions() -> (
    None
):
    items = tuple(
        LearningSource(
            history_id=uuid4(),
            occasion_id=uuid4(),
            feedback_id=uuid4(),
            feedback_revision=1,
            observed_at=NOW - timedelta(minutes=index),
        )
        for index in range(110)
    )
    windows = []
    for ordering in (items, tuple(reversed(items))):
        window = None
        for item in ordering:
            window = advance_window(window, item, now=NOW)
        assert window is not None
        assert len(window.sources) == 100
        assert window.sources == items[:100]
        windows.append(window)
    assert windows[0] == windows[1]
    same_occasion = items[0].model_copy(
        update={
            "history_id": uuid4(),
            "feedback_id": uuid4(),
            "observed_at": NOW + timedelta(seconds=1),
        }
    )
    window = advance_window(windows[0], same_occasion, now=NOW + timedelta(seconds=1))
    assert len(window.sources) == 100
    assert len({item.occasion_id for item in window.sources}) == 100
    assert window.sources[0] == same_occasion

    current = source()
    request = ExtractionRequest(current=current, profile=profile(), now=NOW)
    extracted = asyncio.run(Extractor().extract(request)).candidates[0].preference
    contributions = tuple(
        LearningContribution(
            insight_key=f"key-{index:03}",
            insight=f"Insight {index}",
            field="likes",
            value="lime",
            evidence=extracted.evidence[0],
        )
        for index in range(60)
    )
    forward = reduce_preferences(contributions, profile().explicit_preferences, now=NOW)
    reverse = reduce_preferences(
        tuple(reversed(contributions)), profile().explicit_preferences, now=NOW
    )
    assert forward == reverse and len(forward) == 50
    assert forward[-1].candidate.insight_key == "key-010"


def test_source_edit_during_extraction_rolls_back_receipt_and_window(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    async def scenario() -> None:
        item = source()
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await seed_profile(repository)
            await seed_source(repository, item)

            class EditingExtractor(Extractor):
                async def extract(self, request: ExtractionRequest) -> ExtractionResult:
                    current = await repository.read(OWNER, HISTORY, str(item.feedback.history_id))
                    assert current is not None
                    await repository.patch(
                        OWNER,
                        HISTORY,
                        str(item.feedback.history_id),
                        RecordPatch(etag=current.etag, fields={"feedbackRevision": 2}),
                    )
                    return await super().extract(request)

            with pytest.raises(CosmosError) as failure:
                await LearningService(repository, EditingExtractor()).process(
                    OWNER, item.feedback.feedback_id, now=NOW
                )
            assert failure.value.code == CosmosFailure.CONFLICT
            assert not [
                record
                for record in harness[0].documents.values()
                if record["recordKind"] == "processing"
            ]
            assert stored_profile(harness[0])["learnedPreferences"] == []

    asyncio.run(scenario())


def test_older_ledger_snapshot_cannot_replace_a_newer_profile_projection(
    harness: tuple[Transport, SyntheticCredential], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await seed_profile(repository)
            items = (source(days=1), source())
            service = LearningService(repository, Extractor())
            await seed_source(repository, items[0])
            await service.process(OWNER, items[0].feedback.feedback_id, now=NOW)
            old = await repository.read(OWNER, HISTORY, str(_window_id(OWNER)))
            assert old is not None
            await seed_source(repository, items[1])
            await service.process(OWNER, items[1].feedback.feedback_id, now=NOW)
            before = deepcopy(stored_profile(harness[0]))
            assert before["learningGeneration"] == 2
            original = repository.read

            async def stale(
                user: AuthenticatedUser,
                container: ProductContainer,
                item_id: str,
                **kwargs: Any,
            ) -> StoredRecord | None:
                if container == HISTORY and item_id == str(_window_id(OWNER)):
                    return old
                return await original(user, container, item_id, **kwargs)

            monkeypatch.setattr(repository, "read", stale)
            with pytest.raises(CosmosError) as failure:
                await service.reconcile(OWNER, now=NOW)
            assert failure.value.code == CosmosFailure.CONFLICT
            assert stored_profile(harness[0]) == before

    asyncio.run(scenario())


def test_profile_byte_limit_fails_explicitly_without_losing_durable_receipt_or_explicit_state(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    async def scenario() -> None:
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            profile_record = await seed_profile(repository)
            used = len(
                json.dumps(
                    {key: value for key, value in profile_record.body.items() if key != "_etag"}
                ).encode()
            )
            padded = await repository.patch(
                OWNER,
                USERS,
                OWNER.user_id,
                RecordPatch(
                    etag=profile_record.etag,
                    fields={"privateSyntheticPadding": "x" * (MAX_RECORD_BYTES - used - 128)},
                ),
            )
            item = source()
            await seed_source(repository, item)
            extractor = Extractor()
            service = LearningService(repository, extractor)
            for _ in range(2):
                with pytest.raises(CosmosError) as failure:
                    await service.process(OWNER, item.feedback.feedback_id, now=NOW)
                assert failure.value.code == CosmosFailure.TOO_LARGE
            assert len(extractor.calls) == 1
            result = stored_profile(harness[0])
            assert result["learnedPreferences"] == []
            assert result["explicitPreferences"] == padded.body["explicitPreferences"]
            assert result["groceries"] == padded.body["groceries"]
            assert await repository.read(
                OWNER, HISTORY, str(_receipt_id(OWNER, item.feedback.feedback_id))
            )

    asyncio.run(scenario())
