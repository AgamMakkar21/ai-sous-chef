import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from time import monotonic
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sous_chef.adapters.cosmos import CosmosRepository, open_cosmos
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.cosmos import CosmosError, CosmosFailure, RecordPatch, StoredRecord
from sous_chef.domain.extraction import ExtractionError, ExtractionFailure
from sous_chef.services.context import ContextService
from sous_chef.services.learning import LearningService
from sous_chef.services.learning_recovery import (
    QUARANTINE_OWNER,
    LearningPending,
    LearningRecovery,
    RecoveryPolicy,
    attempt_id,
)
from test_cosmos_repository import SyntheticCredential, Transport, settings
from test_cosmos_repository import harness as harness
from test_feedback_worker import record
from test_learning import (
    HISTORY,
    OWNER,
    USERS,
    Extractor,
    seed_profile,
    seed_source,
    stored_profile,
)
from test_preference_extraction import NOW, config, source

from worker import function_app as worker
from worker import replay as command
from worker.extraction import ExtractionConfig


@pytest.mark.parametrize(
    "error",
    [
        ExtractionError(ExtractionFailure.TIMEOUT),
        ExtractionError(ExtractionFailure.THROTTLED),
        CosmosError(CosmosFailure.UNAVAILABLE, uuid4(), outcome_unknown=True),
    ],
)
def test_transient_failure_recovers_after_durable_backoff_across_restart(
    harness: tuple[Transport, SyntheticCredential], error: Exception
) -> None:
    async def scenario() -> None:
        item = source()
        extractor = Extractor()
        calls = 0
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await seed_profile(repository)
            await seed_source(repository, item)

            async def apply() -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise error
                await LearningService(repository, extractor).process(
                    OWNER, item.feedback.feedback_id, now=NOW
                )

            with pytest.raises(LearningPending):
                await LearningRecovery(repository).process(
                    OWNER, item.feedback.feedback_id, 1, apply, now=NOW
                )
            current = await repository.read(OWNER, HISTORY, str(item.feedback.history_id))
            assert current is not None
            assert current.body["rating"] == item.feedback.rating
            assert current.body["note"] == item.feedback.note
            with pytest.raises(LearningPending):
                await LearningRecovery(repository).process(
                    OWNER, item.feedback.feedback_id, 1, apply, now=NOW + timedelta(seconds=59)
                )
            assert calls == 1
            for _ in range(2):
                await LearningRecovery(repository).process(
                    OWNER, item.feedback.feedback_id, 1, apply, now=NOW + timedelta(seconds=60)
                )
            found = await LearningRecovery(repository).inspect(OWNER, item.feedback.feedback_id, 1)
            assert found is not None and found[1].state == "completed"
            assert found[1].attempts == calls == 2
            assert len(extractor.calls) == 1
            assert stored_profile(harness[0])["learnedPreferences"][0]["evidenceCount"] == 1

    asyncio.run(scenario())


def test_backoff_starts_after_slow_failure_not_at_attempt_start(
    harness: tuple[Transport, SyntheticCredential], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Mock()
    clock.now.return_value = NOW
    monkeypatch.setattr("sous_chef.services.learning_recovery.datetime", clock)

    async def scenario() -> None:
        identifier = uuid4()
        calls = 0

        async def apply() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                clock.now.return_value = NOW + timedelta(seconds=45)
                raise ExtractionError(ExtractionFailure.TIMEOUT)

        async with open_cosmos(settings()) as repository:
            recovery = LearningRecovery(repository, RecoveryPolicy(backoff_seconds=100))
            with pytest.raises(LearningPending):
                await recovery.process(OWNER, identifier, 1, apply)
            found = await recovery.inspect(OWNER, identifier, 1)
            assert found is not None
            assert found[1].available_at == NOW + timedelta(seconds=145)
            clock.now.return_value = NOW + timedelta(seconds=144)
            with pytest.raises(LearningPending):
                await recovery.process(OWNER, identifier, 1, apply)
            assert calls == 1
            clock.now.return_value = NOW + timedelta(seconds=145)
            await recovery.process(OWNER, identifier, 1, apply)
            assert calls == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("permanent", [False, True])
def test_attempt_budget_is_terminal_and_failure_logs_contain_no_private_text(
    harness: tuple[Transport, SyntheticCredential],
    caplog: pytest.LogCaptureFixture,
    permanent: bool,
) -> None:
    async def scenario() -> None:
        item = source()
        calls = 0

        async def fail() -> None:
            nonlocal calls
            calls += 1
            if permanent:
                raise RuntimeError("PRIVATE note, prompt and provider response")
            raise ExtractionError(ExtractionFailure.UNAVAILABLE)

        async with open_cosmos(settings()) as repository:
            recovery = LearningRecovery(repository, RecoveryPolicy(max_attempts=2))
            for minute in (0, 1, 10, 100):
                try:
                    await recovery.process(
                        OWNER,
                        item.feedback.feedback_id,
                        1,
                        fail,
                        now=NOW + timedelta(minutes=minute),
                    )
                except LearningPending:
                    assert not permanent and minute == 0
            found = await recovery.inspect(OWNER, item.feedback.feedback_id, 1)
            assert found is not None and found[1].state == "failed"
            assert found[1].attempts == calls == (1 if permanent else 2)

    caplog.set_level(logging.WARNING)
    asyncio.run(scenario())
    assert "PRIVATE" not in caplog.text and OWNER.user_id not in caplog.text


def test_abandoned_claims_exhaust_budget_without_restarting_forever(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    async def scenario() -> None:
        identifier = uuid4()
        calls = 0

        async def crash() -> None:
            nonlocal calls
            calls += 1
            raise asyncio.CancelledError

        async with open_cosmos(settings()) as repository:
            for minute in (0, 2):
                with pytest.raises(asyncio.CancelledError):
                    await LearningRecovery(repository, RecoveryPolicy(max_attempts=2)).process(
                        OWNER, identifier, 1, crash, now=NOW + timedelta(minutes=minute)
                    )
            await LearningRecovery(repository).process(
                OWNER, identifier, 1, crash, now=NOW + timedelta(minutes=4)
            )
            found = await LearningRecovery(repository).inspect(OWNER, identifier, 1)
            assert found is not None
            assert found[1].state == "failed" and found[1].attempts == calls == 2
            assert found[1].error_category == "attempt_abandoned"

    asyncio.run(scenario())


def test_concurrent_deliveries_only_dispatch_one_claim(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    async def scenario() -> None:
        identifier = uuid4()
        calls = 0

        async def apply() -> None:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)

        async with open_cosmos(settings()) as repository:
            results = await asyncio.gather(
                *(
                    LearningRecovery(repository).process(OWNER, identifier, 1, apply, now=NOW)
                    for _ in range(2)
                ),
                return_exceptions=True,
            )
            assert calls == 1
            assert any(isinstance(result, (LearningPending, CosmosError)) for result in results)

    asyncio.run(scenario())


def test_uncertain_claim_is_not_immediately_reissued(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    async def scenario() -> None:
        identifier = uuid4()
        calls = 0

        async def apply() -> None:
            nonlocal calls
            calls += 1

        async with open_cosmos(settings()) as repository:
            harness[0].write_then_disconnect = True
            with pytest.raises(CosmosError) as failure:
                await LearningRecovery(repository).process(OWNER, identifier, 1, apply, now=NOW)
            assert failure.value.outcome_unknown and calls == 0
            harness[0].write_then_disconnect = False
            with pytest.raises(LearningPending):
                await LearningRecovery(repository).process(
                    OWNER, identifier, 1, apply, now=NOW + timedelta(seconds=60)
                )
            await LearningRecovery(repository).process(
                OWNER, identifier, 1, apply, now=NOW + timedelta(seconds=120)
            )
            stored = harness[0].documents[
                ("RecipeHistory", OWNER.user_id, str(attempt_id(OWNER, identifier)))
            ]
            assert stored["attempts"] == 2 and calls == 1

    asyncio.run(scenario())


def test_replay_repairs_committed_receipt_and_is_consumed_by_next_request(
    harness: tuple[Transport, SyntheticCredential], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        first, second = source(days=1), source()
        extractor = Extractor()
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            profile = await seed_profile(repository)
            await repository.patch(
                OWNER,
                USERS,
                OWNER.user_id,
                RecordPatch(etag=profile.etag, fields={"groceries": []}),
            )
            for item in (first, second):
                await seed_source(repository, item)
            await LearningService(repository, extractor).process(
                OWNER, first.feedback.feedback_id, now=NOW
            )
            original = repository.patch

            async def interrupted(
                user: AuthenticatedUser,
                container: Any,
                item_id: str,
                request: RecordPatch,
                **kwargs: Any,
            ) -> StoredRecord:
                if container == USERS:
                    raise RuntimeError("PRIVATE projection failure after committed evidence")
                return await original(user, container, item_id, request, **kwargs)

            async def apply() -> None:
                await LearningService(repository, extractor).process(
                    OWNER, second.feedback.feedback_id, now=NOW
                )

            monkeypatch.setattr(repository, "patch", interrupted)
            recovery = LearningRecovery(repository)
            await recovery.process(OWNER, second.feedback.feedback_id, 1, apply, now=NOW)
            assert len(extractor.calls) == 2
            failed = await recovery.inspect(OWNER, second.feedback.feedback_id, 1)
            assert failed is not None and failed[1].state == "failed"
            monkeypatch.setattr(repository, "patch", original)
            request_id = uuid4()
            assert (
                await recovery.replay(OWNER, second.feedback.feedback_id, 1, request_id, now=NOW)
                == "requested"
            )
            assert (
                await recovery.replay(OWNER, second.feedback.feedback_id, 1, request_id, now=NOW)
                == "already_requested"
            )
            replay = next(
                row for row in harness[0].documents.values() if row.get("role") == "learningReplay"
            )
            assert worker.eligible_replay(replay) is not None
            for _ in range(2):
                await LearningRecovery(repository).process(
                    OWNER, second.feedback.feedback_id, 1, apply, generation=1, now=NOW
                )
            assert len(extractor.calls) == 2
            updated = await repository.read(OWNER, USERS, OWNER.user_id)
            assert updated is not None
            context = await ContextService().build(
                repository,
                OWNER,
                uuid4(),
                updated,
                "Dinner",
                deadline=monotonic() + 5,
            )
            assert len(context.learned_preferences) == 1
            assert context.learned_preferences[0].confidence == 0.75
            assert stored_profile(harness[0])["learnedPreferences"][0]["evidenceCount"] == 2
            assert (
                context.explicit_preferences.model_dump(mode="json", by_alias=True)
                == profile.body["explicitPreferences"]
            )

    # The fixture evidence is dated at NOW; make the real context builder use that clock.
    from sous_chef.services import context as context_module

    class Clock:
        @staticmethod
        def now(tz: Any) -> Any:
            return NOW

    monkeypatch.setattr(context_module, "datetime", Clock)
    asyncio.run(scenario())


@pytest.mark.parametrize("bad_owner", [False, True])
def test_poison_is_durably_quarantined_without_blocking_a_valid_sibling_or_looping(
    harness: tuple[Transport, SyntheticCredential], bad_owner: bool
) -> None:
    poison = record() | {"note": "PRIVATE unnormalized note "}
    if bad_owner:
        poison["userId"] = "PRIVATE malformed owner"
    handler = Mock()

    def quarantine(document: Any) -> None:
        async def persist() -> None:
            async with open_cosmos(settings()) as repository:
                await LearningRecovery(repository).quarantine(document)

        asyncio.run(persist())

    for _ in range(2):
        worker.process_batch([poison, record(2)], handler, quarantine=quarantine)
    rows = [row for row in harness[0].documents.values() if row.get("role") == "learningQuarantine"]
    assert len(rows) == 1 and rows[0]["state"] == "quarantined"
    assert rows[0]["userId"] == (QUARANTINE_OWNER if bad_owner else OWNER.user_id)
    assert "PRIVATE" not in json.dumps(rows)
    assert handler.call_count == 2
    handler.reset_mock()
    worker.process_batch(rows, handler, quarantine=quarantine)
    handler.assert_not_called()


def test_failed_quarantine_keeps_batch_pending_but_still_processes_valid_sibling() -> None:
    handler = Mock()
    with pytest.raises(worker.FeedbackBatchFailed):
        worker.process_batch(
            [record() | {"note": 17}, record(2)],
            handler,
            quarantine=Mock(side_effect=RuntimeError("PRIVATE storage outage")),
        )
    assert handler.call_count == 1


def test_operator_command_uses_cli_credentials_and_returns_only_safe_status(
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identifier = uuid4()
    credential_modes = []

    @asynccontextmanager
    async def repository(configured: Any, **kwargs: Any) -> AsyncIterator[CosmosRepository]:
        credential_modes.append(configured.environment)
        async with open_cosmos(settings(), **kwargs) as result:
            yield result

    monkeypatch.setattr(command, "open_cosmos", repository)
    args = [
        "--inspect",
        "--cosmos-endpoint",
        "https://synthetic.documents.azure.com",
        "--owner",
        OWNER.user_id,
        "--feedback-id",
        str(identifier),
        "--revision",
        "1",
    ]
    assert command.main(args) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "not_found"}
    assert credential_modes == ["local"]
    assert command.main([*args[:-1], "PRIVATE bad revision"]) == 1
    assert "PRIVATE" not in capsys.readouterr().out
    harness[0].faults["GET"].append(403)
    assert command.main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"
    assert not harness[0].documents


@pytest.mark.parametrize("uncertain", [False, True])
def test_operator_replay_outbox_reaches_worker_once_without_status_feedback_loops(
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    uncertain: bool,
) -> None:
    item = source()
    extractor = Extractor()
    broken = True

    class Model:
        async def extract(self, request: Any) -> Any:
            if broken:
                raise ExtractionError(ExtractionFailure.REJECTED)
            return await extractor.extract(request)

    @asynccontextmanager
    async def repository(_settings: Any, **kwargs: Any) -> AsyncIterator[CosmosRepository]:
        async with open_cosmos(settings(), **kwargs) as result:
            yield result

    async def seed() -> None:
        async with open_cosmos(settings()) as result:
            await seed_profile(result)
            await seed_source(result, item)

    asyncio.run(seed())
    monkeypatch.setenv("ASC_ENVIRONMENT", "dev")
    monkeypatch.setenv("FeedbackCosmos__accountEndpoint", "https://synthetic.documents.azure.com")
    monkeypatch.setenv("ASC_COSMOS_DATABASE", "sous-chef")
    monkeypatch.setattr(worker, "open_cosmos", repository)
    monkeypatch.setattr(command, "open_cosmos", repository)
    monkeypatch.setattr(ExtractionConfig, "from_environment", lambda: config())
    monkeypatch.setattr(worker, "PreferenceExtractor", lambda _: Model())
    feedback = harness[0].documents[
        ("RecipeHistory", OWNER.user_id, str(item.feedback.feedback_id))
    ]
    worker.process_batch([feedback], worker.process_feedback)
    saved = harness[0].documents[("RecipeHistory", OWNER.user_id, str(item.feedback.history_id))]
    assert saved["rating"] == item.feedback.rating and saved["note"] == item.feedback.note
    args = [
        "--replay",
        "--cosmos-endpoint",
        "https://synthetic.documents.azure.com",
        "--owner",
        OWNER.user_id,
        "--feedback-id",
        str(item.feedback.feedback_id),
        "--revision",
        "1",
        "--request-id",
        str(uuid4()),
    ]
    harness[0].write_then_disconnect = uncertain
    assert command.main(args) == (1 if uncertain else 0)
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == ("rejected" if uncertain else "requested")
    harness[0].write_then_disconnect = False
    assert command.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_requested"
    broken = False
    replay = next(
        row for row in harness[0].documents.values() if row.get("role") == "learningReplay"
    )
    worker.process_batch(
        [replay, replay, feedback],
        worker.process_feedback,
        replay_processor=worker.process_replay,
    )
    assert len(extractor.calls) == 1
    assert stored_profile(harness[0])["learnedPreferences"][0]["evidenceCount"] == 1
    processing = [
        row for row in harness[0].documents.values() if row.get("role") != "learningReplay"
    ]
    handler = Mock()
    worker.process_batch(processing, handler)
    assert handler.call_count == 1  # Only the immutable original feedback, not status writes.
