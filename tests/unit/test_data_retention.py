import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from azure.core.exceptions import ServiceResponseError
from azure.core.pipeline.transport import AsyncHttpResponse, HttpRequest
from fastapi.testclient import TestClient
from sous_chef import delete_user_data as command
from sous_chef.adapters.cosmos import PRIVACY_GUARD_ID, open_cosmos
from sous_chef.app import create_app
from sous_chef.domain.authorization import UserDataDeleted
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    HistoryQuery,
    NewRecord,
    PageRequest,
    ProductContainer,
    RecordKind,
    RecordPatch,
)
from sous_chef.services.learning import LearningService
from sous_chef.services.learning_recovery import LearningRecovery
from sous_chef.settings import ConfigurationError, Settings
from test_chat_api import FakeExecutor, send
from test_conversation_api import OWNER as CHAT_OWNER
from test_conversation_api import conversation_settings as conversation_settings
from test_conversation_api import create
from test_cosmos_repository import (
    ALICE,
    BOB,
    SyntheticCredential,
    Transport,
    profile,
    recipe,
    settings,
)
from test_cosmos_repository import harness as harness
from test_feedback_worker import record as feedback_record
from test_learning import OWNER, Extractor, seed_profile, seed_source
from test_preference_extraction import NOW, source
from test_profile_api import onboard
from test_recipe_safety import full_recipe

from worker import function_app as worker

USERS = ProductContainer.USERS
CHAT = ProductContainer.CHAT_HISTORY
HISTORY = ProductContainer.RECIPE_HISTORY


def test_purge_is_partition_scoped_bounded_resumable_and_repeat_safe(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        request = uuid4()
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            for user in (ALICE, BOB):
                await repository.create(user, profile())
                await repository.create(
                    user, NewRecord(kind=RecordKind.TURN, fields={"note": "PRIVATE"})
                )
                await repository.create(
                    user, NewRecord(kind=RecordKind.SEND_OPERATION, fields={"note": "PRIVATE"})
                )
                await repository.create_conversation(
                    user,
                    NewRecord(kind=RecordKind.CONVERSATION, fields={"title": "PRIVATE"}),
                    receipt_id="conversation-create:" + "a" * 64,
                    request_hash="b" * 64,
                )
                for kind in (
                    RecordKind.RECIPE,
                    RecordKind.RECIPE_OPERATION,
                    RecordKind.BEHAVIOR,
                    RecordKind.FEEDBACK_REVISION,
                    RecordKind.PROCESSING,
                ):
                    await repository.create(user, NewRecord(kind=kind, fields={"note": "PRIVATE"}))
            for _ in range(50):
                await repository.create(
                    ALICE, NewRecord(kind=RecordKind.PROCESSING, fields={"role": "learningReplay"})
                )
            transport.documents[("Users", ALICE.user_id, "legacy-settings")] = {
                "id": "legacy-settings",
                "userId": ALICE.user_id,
                "_etag": '"legacy"',
                "recordKind": "retired-kind",
                "private": "PRIVATE legacy payload",
            }
            untouched = {
                key: deepcopy(value)
                for key, value in transport.documents.items()
                if key[1] == BOB.user_id
            }
            first = await repository.purge_user(ALICE, request)
            assert not first.complete
            assert first.removed == {"Users": 2, "ChatHistory": 4, "RecipeHistory": 50}
            with pytest.raises(UserDataDeleted):
                await repository.read(ALICE, USERS, ALICE.user_id)
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            resumed = await repository.purge_user(ALICE, uuid4())
            assert resumed.complete and resumed.request_id == request
            assert resumed.removed["RecipeHistory"] == 5
            repeated = await repository.purge_user(ALICE, request)
            assert repeated.complete and not any(repeated.removed.values())
            with pytest.raises(CosmosError) as error:
                await repository.create(ALICE, profile())
            assert error.value.code == CosmosFailure.EXISTS
            with pytest.raises(UserDataDeleted):
                await repository.create(ALICE, recipe())
        assert untouched == {
            key: value for key, value in transport.documents.items() if key[1] == BOB.user_id
        }
        remaining = [value for key, value in transport.documents.items() if key[1] == ALICE.user_id]
        assert len(remaining) == 3
        assert all(value["state"] == "deleted" and value["ttl"] == -1 for value in remaining)
        assert "PRIVATE" not in json.dumps(remaining)
        assert all(
            not ({"groceries", "explicitPreferences", "learnedPreferences", "note"} & value.keys())
            for value in remaining
        )
        tombstone = transport.documents[("Users", ALICE.user_id, ALICE.user_id)]
        assert tombstone["purgeCompletedAt"] is not None
        assert all(
            json.loads(call.headers["x-ms-documentdb-partitionkey"])[0]
            in {ALICE.user_id, BOB.user_id}
            for call in transport.item_calls()
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["tombstone", "purge"])
def test_uncertain_deletion_stays_closed_and_resumes_without_replaying_writes(
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        request_id = uuid4()
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await repository.create(ALICE, profile())
            await repository.create(ALICE, recipe())
            original = transport.send
            failed = False

            async def disconnect(request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
                nonlocal failed
                body = json.loads(request.body) if isinstance(request.body, str | bytes) else None
                response = await original(request, **kwargs)
                is_purge = isinstance(body, list) and all(
                    item["operationType"] == "Delete" for item in body
                )
                if (
                    not failed
                    and response.status_code < 300
                    and (
                        (phase == "tombstone" and request.method == "PUT")
                        or (phase == "purge" and is_purge)
                    )
                ):
                    failed = True
                    raise ServiceResponseError("PRIVATE uncertain deletion")
                return response

            monkeypatch.setattr(transport, "send", disconnect)
            with pytest.raises(CosmosError) as error:
                await repository.purge_user(ALICE, request_id)
            assert failed and error.value.outcome_unknown
            with pytest.raises(UserDataDeleted):
                await repository.read(ALICE, USERS, ALICE.user_id)
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            result = await repository.purge_user(ALICE, request_id)
            assert result.complete and result.request_id == request_id
        assert len(transport.documents) == 3
        assert all(row["state"] == "deleted" for row in transport.documents.values())

    asyncio.run(scenario())


def test_inflight_profile_projection_cannot_replace_the_tombstone(
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()
        original = transport.send

        async def pause(request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
            body = json.loads(request.body) if isinstance(request.body, str | bytes) else None
            if (
                request.method == "PATCH"
                and isinstance(body, dict)
                and any(item["path"] == "/learnedPreferences" for item in body["operations"])
            ):
                started.set()
                await release.wait()
            return await original(request, **kwargs)

        monkeypatch.setattr(transport, "send", pause)
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            current = await repository.create(ALICE, profile())
            pending = asyncio.create_task(
                repository.patch(
                    ALICE,
                    USERS,
                    ALICE.user_id,
                    RecordPatch(
                        etag=current.etag,
                        fields={"learnedPreferences": ["PRIVATE late projection"]},
                    ),
                )
            )
            await asyncio.wait_for(started.wait(), 3)
            assert (await repository.purge_user(ALICE, uuid4())).complete
            release.set()
            with pytest.raises(CosmosError) as error:
                await pending
            assert error.value.code == CosmosFailure.CONFLICT
        tombstone = transport.documents[("Users", ALICE.user_id, ALICE.user_id)]
        assert tombstone["recordKind"] == "deletionTombstone"
        assert "learnedPreferences" not in tombstone

    asyncio.run(scenario())


def test_confirmed_throttle_retry_preserves_the_original_transaction_preconditions(
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            current = await repository.create(ALICE, recipe())
            key = ("RecipeHistory", ALICE.user_id, str(current.body["id"]))
            original = transport.send

            async def concurrent_edit(request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
                response = await original(request, **kwargs)
                if response.status_code == 429:
                    transport.documents[key]["_etag"] = '"concurrent-revision"'
                    transport.documents[key]["note"] = "PRIVATE peer edit"
                return response

            monkeypatch.setattr(transport, "send", concurrent_edit)
            transport.retry_after = "1"
            transport.faults["POST"].append(429)
            with pytest.raises(CosmosError) as error:
                await repository.patch(
                    ALICE,
                    HISTORY,
                    str(current.body["id"]),
                    RecordPatch(etag=current.etag, fields={"note": "stale overwrite"}),
                )
            assert error.value.code == CosmosFailure.CONFLICT
            assert transport.documents[key]["note"] == "PRIVATE peer edit"

    asyncio.run(scenario())


def test_a_write_already_past_preflight_cannot_commit_after_the_purge(
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()
        identifier = uuid4()
        original = transport.send

        async def pause(request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
            body = json.loads(request.body) if isinstance(request.body, str | bytes) else None
            if isinstance(body, list) and any(
                item.get("resourceBody", {}).get("id") == str(identifier) for item in body
            ):
                started.set()
                await release.wait()
            return await original(request, **kwargs)

        monkeypatch.setattr(transport, "send", pause)
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await repository.create(ALICE, profile())
            pending = asyncio.create_task(
                repository.create(
                    ALICE,
                    NewRecord(
                        kind=RecordKind.PROCESSING,
                        record_id=identifier,
                        fields={"role": "learningRecovery", "note": "PRIVATE late write"},
                    ),
                )
            )
            await asyncio.wait_for(started.wait(), 3)
            assert (HISTORY.value, ALICE.user_id, PRIVACY_GUARD_ID) in transport.documents
            result = await repository.purge_user(ALICE, uuid4())
            assert result.complete
            release.set()
            with pytest.raises(CosmosError) as failure:
                await pending
            assert failure.value.code == CosmosFailure.CONFLICT
            assert (HISTORY.value, ALICE.user_id, str(identifier)) not in transport.documents

    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["id", "ttl", "schemaVersion"])
def test_invalid_privacy_fence_fails_closed_before_writes_or_completed_purge(
    harness: tuple[Transport, SyntheticCredential],
    corruption: str,
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await repository.create(ALICE, profile())
            existing = await repository.create(ALICE, recipe())
            guard = transport.documents[("RecipeHistory", ALICE.user_id, PRIVACY_GUARD_ID)]
            guard[corruption] = (
                "wrong-item" if corruption == "id" else 60 if corruption == "ttl" else 2
            )
            with pytest.raises(CosmosError) as error:
                await repository.create(ALICE, recipe())
            assert error.value.code == CosmosFailure.INVALID_RESULT
            with pytest.raises(CosmosError) as error:
                await repository.purge_user(ALICE, uuid4())
            assert error.value.code == CosmosFailure.INVALID_RESULT
            assert ("RecipeHistory", ALICE.user_id, str(existing.body["id"])) in transport.documents
            with pytest.raises(UserDataDeleted):
                await repository.read(ALICE, USERS, ALICE.user_id)

    asyncio.run(scenario())


def test_inflight_extraction_late_feedback_replay_and_poison_cannot_restore_state(
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _ = harness
    event: dict[str, Any] = {}
    extractor = Extractor()
    monkeypatch.setattr(worker, "_settings", settings)
    monkeypatch.setattr(worker, "_EnvironmentExtractor", lambda: extractor)

    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()
        original = extractor.extract

        async def delayed(request: Any) -> Any:
            started.set()
            await release.wait()
            return await original(request)

        monkeypatch.setattr(extractor, "extract", delayed)
        item = source()
        async with open_cosmos(settings(), lifetime_seconds=60) as repository:
            await seed_profile(repository)
            await seed_source(repository, item)
            event.update(
                deepcopy(
                    transport.documents[
                        (HISTORY.value, OWNER.user_id, str(item.feedback.feedback_id))
                    ]
                )
            )
            assert worker.eligible_feedback(event) is not None

            async def apply() -> None:
                await LearningService(repository, extractor).process(
                    OWNER, item.feedback.feedback_id, now=NOW
                )

            pending = asyncio.create_task(
                LearningRecovery(repository).process(
                    OWNER, item.feedback.feedback_id, 1, apply, now=NOW
                )
            )
            await asyncio.wait_for(started.wait(), 3)
            assert (await repository.purge_user(OWNER, uuid4())).complete
            release.set()
            with pytest.raises(UserDataDeleted):
                await pending
            with pytest.raises(UserDataDeleted):
                await LearningRecovery(repository).replay(
                    OWNER, item.feedback.feedback_id, 1, uuid4()
                )
        await worker._process_learning(OWNER.user_id, item.feedback.feedback_id, 1, generation=1)

    asyncio.run(scenario())
    healthy: list[str] = []

    def dispatch(item: worker.FeedbackEvent) -> None:
        if item.user_id == BOB.user_id:
            healthy.append(item.user_id)
        else:
            worker.process_feedback(item)

    worker.process_batch(
        [{**event, "schemaVersion": 2}, event, feedback_record(owner=BOB.user_id)],
        dispatch,
        quarantine=worker.quarantine_feedback,
        replay_processor=worker.process_replay,
    )
    assert healthy == [BOB.user_id]
    assert len(extractor.calls) == 1
    assert len(transport.documents) == 3
    assert {row["recordKind"] for row in transport.documents.values()} == {
        "deletionTombstone",
        "deletionGuard",
    }


def test_approved_chat_ttl_excludes_expired_context_but_keeps_saved_snapshots(
    conversation_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    configured = conversation_settings.model_copy(update={"chat_retention_days": 1})
    executor = FakeExecutor(transport)
    executor.recipe = full_recipe()
    with TestClient(create_app(configured, chat_executor=executor), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        conversation_body = transport.documents[("ChatHistory", CHAT_OWNER.user_id, conversation)]
        deadline = conversation_body["retentionExpiresAt"]
        first = send(client, conversation, content="PRIVATE expired request")
        assert first.status_code == 200
        saved = client.post(
            "/api/v1/recipes",
            json={"generatedRecipeId": first.json()["data"]["generatedRecipeId"]},
            headers={"Idempotency-Key": "retain-snapshot"},
        )
        assert saved.status_code == 201
        turn = transport.documents[
            ("ChatHistory", CHAT_OWNER.user_id, first.json()["data"]["turnId"])
        ]
        assert 0 < turn["ttl"] <= 86400
        turn["retentionExpiresAt"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        executor.recipe = None
        second = send(client, conversation, key="after-expiry")
        assert second.status_code == 200
        assert not executor.calls[-1].context.history
        assert (
            transport.documents[("ChatHistory", CHAT_OWNER.user_id, conversation)][
                "retentionExpiresAt"
            ]
            == deadline
        )
        denied = client.post(
            "/api/v1/generated-recipes/" + first.json()["data"]["generatedRecipeId"] + "/revisions",
            json={"reason": "Revise an expired source"},
            headers={"Idempotency-Key": "expired-revision"},
        )
        assert denied.status_code == 404 and len(executor.calls) == 2
        retained = client.get("/api/v1/recipes/" + saved.json()["data"]["historyId"])
        assert retained.status_code == 200
        assert (
            "ttl"
            not in transport.documents[
                ("RecipeHistory", CHAT_OWNER.user_id, saved.json()["data"]["historyId"])
            ]
        )
        assert "ttl" not in transport.documents[("Users", CHAT_OWNER.user_id, CHAT_OWNER.user_id)]


def test_chat_retention_handles_legacy_rows_and_requires_item_ttl_capability(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness

    async def scenario() -> None:
        async with open_cosmos(settings()) as repository:
            record = await repository.create(
                ALICE, NewRecord(kind=RecordKind.CONVERSATION, fields={"title": "PRIVATE old chat"})
            )
        row = transport.documents[("ChatHistory", ALICE.user_id, str(record.body["id"]))]
        assert "ttl" not in row
        row["createdAt"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        async with open_cosmos(settings(chat_retention_days=1)) as repository:
            assert await repository.read(ALICE, CHAT, str(record.body["id"])) is None
            assert not (
                await repository.query(ALICE, HistoryQuery(kind="conversation"), PageRequest())
            ).items
        transport.container_overrides = {"defaultTtl": None}
        with pytest.raises(ConfigurationError):
            async with open_cosmos(settings(chat_retention_days=1)):
                pytest.fail("Retention needs per-item TTL capability.")
        async with open_cosmos(settings()) as repository:
            assert await repository.read(ALICE, CHAT, str(record.body["id"])) is not None

    asyncio.run(scenario())


def test_chat_retention_preserves_deadlines_on_maximum_field_updates(
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    async def scenario() -> None:
        async with open_cosmos(settings(chat_retention_days=1)) as repository:
            current = await repository.create(
                ALICE, NewRecord(kind=RecordKind.CONVERSATION, fields={"title": "Original title"})
            )
            changes = RecordPatch(
                etag=current.etag, fields={f"field{index}": index for index in range(9)}
            )
            updated = await repository.patch(
                ALICE,
                CHAT,
                str(current.body["id"]),
                changes,
            )
            assert all(updated.body[key] == value for key, value in changes.fields.items())
            assert updated.body["title"] == "Original title"
            assert updated.body["retentionExpiresAt"] == current.body["retentionExpiresAt"]
            with pytest.raises(CosmosError) as error:
                await repository.patch(
                    ALICE,
                    CHAT,
                    str(current.body["id"]),
                    changes,
                )
            assert error.value.code == CosmosFailure.CONFLICT

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid", ["confirmation", "owner", "endpoint", "request", "rbac", "syntax"]
)
def test_operator_command_rejects_bad_scope_or_denied_permissions_without_content(
    harness: tuple[Transport, SyntheticCredential],
    capsys: pytest.CaptureFixture[str],
    invalid: str,
) -> None:
    transport, _ = harness
    args = [
        "--delete",
        "--cosmos-endpoint",
        "https://synthetic.documents.azure.com",
        "--owner",
        ALICE.user_id,
        "--confirm-owner",
        ALICE.user_id,
        "--request-id",
        str(uuid4()),
    ]
    option, value = {
        "confirmation": ("--confirm-owner", BOB.user_id),
        "owner": ("--owner", "invalid"),
        "endpoint": ("--cosmos-endpoint", "http://untrusted.invalid"),
        "request": ("--request-id", "invalid"),
        "rbac": ("--request-id", args[-1]),
        "syntax": ("--request-id", args[-1]),
    }[invalid]
    args[args.index(option) + 1] = value
    if invalid == "rbac":
        transport.setup_fault = 403
    elif invalid == "syntax":
        args.append("secret-private-unrecognized-argument")
    assert command.main(args) == 1
    captured = capsys.readouterr()
    output = captured.out
    assert json.loads(output)["status"] == "incomplete"
    assert ALICE.user_id not in output and "secret-private" not in output
    assert not captured.err
    assert not transport.documents
    if invalid != "rbac":
        assert not transport.calls


def test_operator_command_finishes_and_emits_only_content_free_audit_metadata(
    harness: tuple[Transport, SyntheticCredential],
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def seed() -> None:
        async with open_cosmos(settings()) as repository:
            await repository.create(ALICE, profile())
            await repository.create(ALICE, recipe())

    asyncio.run(seed())
    request = str(uuid4())
    assert (
        command.main(
            [
                "--delete",
                "--cosmos-endpoint",
                "https://synthetic.documents.azure.com",
                "--owner",
                ALICE.user_id,
                "--confirm-owner",
                ALICE.user_id,
                "--request-id",
                request,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "primary_stores_completed" and result["requestId"] == request
    assert result["removedThisRun"] == {"Users": 1, "ChatHistory": 0, "RecipeHistory": 1}
    assert ALICE.user_id not in output and "synthetic" not in output
