import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sous_chef.adapters.cosmos import CosmosRepository, open_cosmos
from sous_chef.app import create_app
from sous_chef.domain.cosmos import ProductContainer, RecordKind, StoredRecord
from sous_chef.settings import Settings
from test_chat_api import FakeExecutor, documents, send
from test_chat_api import chat_api as chat_api
from test_chat_api import executor as executor
from test_conversation_api import CHAT, OWNER, create
from test_conversation_api import conversation_settings as conversation_settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_recipe_safety import full_recipe

RECIPES = "/api/v1/recipes"


def generate(api: TestClient, executor: FakeExecutor) -> dict[str, Any]:
    executor.recipe = full_recipe()
    executor.recipe_id = uuid4()
    response = send(api, create(api))
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["data"]
    return result


def accept(api: TestClient, recipe_id: str, *, key: str = "accept", cooked: bool = False) -> Any:
    return api.post(
        RECIPES,
        json={"generatedRecipeId": recipe_id, "cooked": cooked},
        headers={"Idempotency-Key": key},
    )


def cook(api: TestClient, history_id: str, etag: str, key: str = "cook") -> Any:
    return api.post(
        f"{RECIPES}/{history_id}/cooked",
        json={},
        headers={"Idempotency-Key": key, "If-Match": etag},
    )


def signal(
    api: TestClient,
    recipe_id: str,
    kind: str,
    reason: str = "Use less rice.",
    key: str = "signal",
) -> Any:
    return api.post(
        f"/api/v1/generated-recipes/{recipe_id}/{kind}",
        json={"reason": reason},
        headers={"Idempotency-Key": key},
    )


@pytest.mark.parametrize("confirmed_cooked", [False, True])
def test_acceptance_is_an_immutable_owned_snapshot_not_inferred_cooking(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    confirmed_cooked: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = generate(chat_api, executor)
    transport = harness[0]
    assert not documents(transport, "recipe") and not documents(transport, "behavior")
    before_profile = deepcopy(documents(transport, "profile"))
    response = accept(chat_api, source["generatedRecipeId"], cooked=confirmed_cooked)
    assert response.status_code == 201, response.text
    result = response.json()["data"]
    assert result["recipe"] == source["recipe"]
    assert result["sourceTurnId"] == source["turnId"]
    assert result["sourceConversationId"] == source["conversationId"]
    assert result["occasionId"] == source["occasionId"]
    assert result["accepted"] is True
    assert result["state"] == ("cooked" if confirmed_cooked else "planned")
    assert result["awaitingFeedback"] is confirmed_cooked
    assert (result["cookedAt"] is not None) is confirmed_cooked
    assert result["savedAt"].endswith("Z")
    assert result["rating"] is None and result["note"] is None
    assert result["feedbackRevision"] == 0 and result["processingStatus"] == "notEligible"
    assert response.headers["etag"] and response.headers["cache-control"] == "no-store"
    assert response.headers["location"] == RECIPES + "/" + result["historyId"]
    snapshot = documents(transport, "recipe")[0]
    assert snapshot["userId"] == OWNER.user_id
    assert snapshot["safetyProfileRevision"] and snapshot["safetyPolicyVersion"]
    event = documents(transport, "behavior")[0]
    assert event["kind"] == "saved" and event["provenance"] == "explicit_user_action"
    assert event["historyId"] == event["sourceId"] == result["historyId"]
    assert event["behaviorRevision"] == 1
    assert documents(transport, "profile") == before_profile
    source_record = documents(transport, "turn")[0]
    source_record["recipe"]["title"] = "Changed source after acceptance"
    assert documents(transport, "recipe")[0]["recipe"] == result["recipe"]
    for private in (OWNER.user_id, "Private dinner request", "Use less rice.", "Simple dinner"):
        assert private not in caplog.text


def test_save_and_cooking_replay_survive_restart_before_stale_etag_checks(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    source = generate(chat_api, executor)
    saved = accept(chat_api, source["generatedRecipeId"])
    history_id = saved.json()["data"]["historyId"]
    first = cook(chat_api, history_id, saved.headers["etag"])
    assert first.status_code == 200, first.text
    result = first.json()["data"]
    assert result["cookedAt"] >= result["savedAt"]
    assert result["awaitingFeedback"] and result["state"] == "cooked"
    assert result["recipe"] == source["recipe"]
    calls = len(executor.calls)
    with TestClient(create_app(conversation_settings), headers=auth_headers) as restarted:
        repeated_save = accept(restarted, source["generatedRecipeId"])
        repeated_cook = cook(restarted, history_id, saved.headers["etag"])
        assert repeated_save.json()["data"] == result
        assert repeated_cook.json() == first.json()
        assert repeated_cook.headers["etag"] == first.headers["etag"]
        assert cook(restarted, history_id, saved.headers["etag"], key="new-cook").status_code == 412
        current = cook(restarted, history_id, first.headers["etag"], key="new-cook")
        assert current.status_code == 200 and current.json() == first.json()
    assert len(executor.calls) == calls
    assert len(documents(harness[0], "recipe")) == len(documents(harness[0], "behavior")) == 1


def test_save_new_keys_do_not_duplicate_or_bypass_conditional_cooking(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    source = generate(chat_api, executor)
    first = accept(chat_api, source["generatedRecipeId"])
    repeated = accept(chat_api, source["generatedRecipeId"], key="new-save")
    assert repeated.json() == first.json()
    conflict = accept(chat_api, source["generatedRecipeId"], cooked=True)
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    conflict = accept(chat_api, source["generatedRecipeId"], cooked=True, key="new-confirmation")
    assert conflict.status_code == 409 and conflict.json()["code"] == "recipe_lifecycle_conflict"
    assert len(documents(harness[0], "recipe")) == len(documents(harness[0], "behavior")) == 1
    assert documents(harness[0], "recipe")[0]["cookedAt"] is None


@pytest.mark.parametrize("kind", ["rejections", "revisions"])
def test_explicit_signals_deduplicate_without_creating_likes_or_cooked_history(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    kind: str,
) -> None:
    source = generate(chat_api, executor)
    executor.recipe_id = uuid4()
    before_profile = deepcopy(documents(harness[0], "profile"))
    first = signal(chat_api, source["generatedRecipeId"], kind)
    assert first.status_code == (201 if kind == "rejections" else 200), first.text
    second = signal(chat_api, source["generatedRecipeId"], kind)
    third = signal(chat_api, source["generatedRecipeId"], kind, key="same-intent-new-key")
    assert second.json() == third.json() == first.json()
    assert (
        signal(chat_api, source["generatedRecipeId"], kind, reason="Other reason.").status_code
        == 409
    )
    events = documents(harness[0], "behavior")
    assert len(events) == 1 and events[0]["reason"] == "Use less rice."
    assert events[0]["kind"] == ("rejected" if kind == "rejections" else "revisionRequested")
    assert events[0]["provenance"] == "explicit_user_action"
    assert not documents(harness[0], "recipe")
    assert documents(harness[0], "profile") == before_profile
    if kind == "revisions":
        result = first.json()["data"]
        assert result["turnId"] != source["turnId"]
        assert result["conversationId"] == source["conversationId"]
        assert result["occasionId"] == source["occasionId"]
        assert result["supersedesRecipeId"] == source["generatedRecipeId"]
        assert len(executor.calls) == 2
        request = executor.calls[-1]
        assert request.context.revision is not None
        assert (
            request.context.revision.recipe.model_dump(mode="json", by_alias=True)
            == source["recipe"]
        )
        assert request.context.current_request == "Use less rice."
        saved = accept(chat_api, result["generatedRecipeId"])
        assert saved.status_code == 201
        assert saved.json()["data"]["occasionId"] == source["occasionId"]
    else:
        assert len(executor.calls) == 1


@pytest.mark.parametrize("stage", ["accept", "cook", "revise"])
def test_current_allergies_and_edited_ingredients_are_revalidated(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    stage: str,
) -> None:
    source = generate(chat_api, executor)
    if stage == "cook":
        saved = accept(chat_api, source["generatedRecipeId"])
    profile = chat_api.get("/api/v1/me")
    changed = chat_api.patch(
        "/api/v1/me/preferences",
        json={"allergies": ["rice"]},
        headers={"If-Match": profile.headers["etag"]},
    )
    assert changed.status_code == 200
    if stage == "accept":
        response = accept(chat_api, source["generatedRecipeId"])
    elif stage == "cook":
        response = cook(chat_api, saved.json()["data"]["historyId"], saved.headers["etag"])
    else:
        executor.recipe_id = uuid4()
        response = signal(chat_api, source["generatedRecipeId"], "revisions")
        assert executor.calls[-1].context.explicit_preferences.allergies == ("rice",)
    assert response.status_code == (502 if stage == "revise" else 422), response.text
    if stage != "cook":
        assert not documents(harness[0], "recipe")
    else:
        assert documents(harness[0], "recipe")[0]["cookedAt"] is None
    assert "rice" not in response.text


def test_altered_persisted_recipe_does_not_inherit_its_old_safety_approval(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    source = generate(chat_api, executor)
    turn = documents(harness[0], "turn")[0]
    turn["recipe"]["instructions"][0]["text"] = "Cook the rice, then garnish with peanut."
    rejected = accept(chat_api, source["generatedRecipeId"])
    assert rejected.status_code == 422, rejected.text
    assert not documents(harness[0], "recipe") and not documents(harness[0], "behavior")


@pytest.mark.parametrize("kind", ["rejections", "revisions"])
def test_same_signal_key_cannot_reference_a_different_owned_recipe(
    chat_api: TestClient,
    executor: FakeExecutor,
    kind: str,
) -> None:
    first, second = generate(chat_api, executor), generate(chat_api, executor)
    executor.recipe_id = uuid4()
    assert signal(chat_api, first["generatedRecipeId"], kind).status_code in {200, 201}
    response = signal(chat_api, second["generatedRecipeId"], kind)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "idempotency_conflict"


@pytest.mark.parametrize("same_key", [False, True])
def test_simultaneous_acceptance_creates_one_snapshot_and_one_saved_signal(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    same_key: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = generate(chat_api, executor)
    barrier = Barrier(2)
    commit = CosmosRepository.commit_recipe

    async def concurrent_commit(*args: Any, **kwargs: Any) -> tuple[StoredRecord, ...]:
        if any(record.kind == RecordKind.RECIPE for record in kwargs["creates"]):
            await asyncio.to_thread(barrier.wait, 10)
        return await commit(*args, **kwargs)

    monkeypatch.setattr(CosmosRepository, "commit_recipe", concurrent_commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [
            pool.submit(
                accept, chat_api, source["generatedRecipeId"], key="race" if same_key else str(i)
            )
            for i in range(2)
        ]
        results = [future.result(timeout=15) for future in pending]
    assert [result.status_code for result in results] == [201, 201]
    assert results[0].json() == results[1].json()
    assert len(documents(harness[0], "recipe")) == len(documents(harness[0], "behavior")) == 1


@pytest.mark.parametrize("same_key", [False, True])
def test_simultaneous_cooking_reconciles_one_confirmation_without_bypassing_etag(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    same_key: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = generate(chat_api, executor)
    saved = accept(chat_api, source["generatedRecipeId"])
    barrier = Barrier(2)
    commit = CosmosRepository.commit_recipe

    async def concurrent_commit(*args: Any, **kwargs: Any) -> tuple[StoredRecord, ...]:
        if kwargs.get("patches"):
            await asyncio.to_thread(barrier.wait, 10)
        return await commit(*args, **kwargs)

    monkeypatch.setattr(CosmosRepository, "commit_recipe", concurrent_commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [
            pool.submit(
                cook,
                chat_api,
                saved.json()["data"]["historyId"],
                saved.headers["etag"],
                "race" if same_key else str(index),
            )
            for index in range(2)
        ]
        results = [future.result(timeout=15) for future in pending]
    assert sorted(result.status_code for result in results) == (
        [200, 200] if same_key else [200, 412]
    )
    successful = [result.json()["data"] for result in results if result.status_code == 200]
    assert all(result == successful[0] for result in successful)
    assert successful[0]["cookedAt"] is not None
    assert successful[0]["recipe"] == source["recipe"]
    assert documents(harness[0], "recipe")[0]["cookedAt"] == successful[0]["cookedAt"]
    assert len(documents(harness[0], "recipe")) == len(documents(harness[0], "behavior")) == 1


def test_acceptance_rechecks_profile_after_deduplication_reads(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = generate(chat_api, executor)
    read = CosmosRepository.read
    validated_profile_read = False

    async def change_profile(*args: Any, **kwargs: Any) -> StoredRecord | None:
        nonlocal validated_profile_read
        result = await read(*args, **kwargs)
        if args[2] == ProductContainer.USERS:
            validated_profile_read = True
        elif args[2] == ProductContainer.RECIPE_HISTORY and validated_profile_read:
            profile = documents(harness[0], "profile")[0]
            profile["explicitPreferences"]["allergies"] = ["rice"]
        return result

    monkeypatch.setattr(CosmosRepository, "read", change_profile)
    response = accept(chat_api, source["generatedRecipeId"])
    assert response.status_code == 422, response.text
    assert not documents(harness[0], "recipe") and not documents(harness[0], "behavior")


@pytest.mark.parametrize("operation", ["save", "rejections", "revisions"])
def test_duplicate_generated_source_fails_without_lifecycle_or_agent_effects(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    operation: str,
) -> None:
    source = generate(chat_api, executor)
    duplicate = deepcopy(documents(harness[0], "turn")[0])
    duplicate["id"] = str(uuid4())
    harness[0].documents[(CHAT.value, OWNER.user_id, duplicate["id"])] = duplicate
    before = deepcopy(harness[0].documents)
    response = (
        accept(chat_api, source["generatedRecipeId"])
        if operation == "save"
        else signal(chat_api, source["generatedRecipeId"], operation)
    )
    assert response.status_code == 502, response.text
    assert response.json()["code"] == "cosmos_invalid_result"
    assert harness[0].documents == before and len(executor.calls) == 1


@pytest.mark.parametrize("operation", ["save", "cook", "reject", "revise"])
def test_lost_committed_response_reconciles_without_replaying_effects_or_generation(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    operation: str,
) -> None:
    source = generate(chat_api, executor)
    saved = accept(chat_api, source["generatedRecipeId"]) if operation == "cook" else None

    async def prime_guard() -> None:
        async with open_cosmos(conversation_settings) as repository:
            await repository._privacy_marker(
                OWNER, ProductContainer.RECIPE_HISTORY, deadline=repository._deadline(None)
            )

    asyncio.run(prime_guard())
    executor.recipe_id = uuid4()
    harness[0].write_then_disconnect = True
    try:
        if operation == "save":
            response = accept(chat_api, source["generatedRecipeId"])
        elif operation == "cook":
            assert saved is not None
            response = cook(chat_api, saved.json()["data"]["historyId"], saved.headers["etag"])
        else:
            response = signal(
                chat_api,
                source["generatedRecipeId"],
                "rejections" if operation == "reject" else "revisions",
            )
    finally:
        harness[0].write_then_disconnect = False
    if operation == "revise":
        # A lost chat-admission response remains recoverable through the same revision key.
        response = signal(chat_api, source["generatedRecipeId"], "revisions")
        assert response.status_code == 202
        assert len(executor.calls) == 1
    else:
        assert response.status_code in {200, 201}, response.text
        assert len(documents(harness[0], "behavior")) == 1


@pytest.mark.parametrize("mode", ["fail", "timeout"])
def test_failed_or_uncertain_revision_replay_never_starts_another_generation(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    mode: str,
) -> None:
    source = generate(chat_api, executor)
    executor.mode = mode
    first = signal(chat_api, source["generatedRecipeId"], "revisions")
    assert first.status_code == (502 if mode == "fail" else 504)
    replay = signal(chat_api, source["generatedRecipeId"], "revisions")
    assert replay.status_code == first.status_code
    assert replay.headers["location"] == first.headers["location"]
    inspected = chat_api.get(first.headers["location"])
    assert inspected.status_code == 200
    assert inspected.json()["data"]["status"] == ("failed" if mode == "fail" else "outcomeUnknown")
    assert len(executor.calls) == 2
    assert len(documents(harness[0], "behavior")) == 1
    assert not documents(harness[0], "recipe")


def test_explicit_failed_revision_retry_keeps_mandatory_source_and_lineage(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    source = generate(chat_api, executor)
    executor.mode = "fail"
    failed = signal(chat_api, source["generatedRecipeId"], "revisions")
    assert failed.status_code == 502
    turn = chat_api.get(failed.headers["location"]).json()["data"]["turn"]
    executor.mode = "success"
    executor.recipe_id = uuid4()
    retried = send(
        chat_api,
        source["conversationId"],
        key="explicit-revision-retry",
        content="Use less rice.",
        retry=turn["turnId"],
    )
    assert retried.status_code == 200, retried.text
    revision = executor.calls[-1].context.revision
    assert revision is not None
    assert revision.recipe.model_dump(mode="json", by_alias=True) == source["recipe"]
    assert retried.json()["data"]["occasionId"] == source["occasionId"]
    assert retried.json()["data"]["supersedesRecipeId"] == source["generatedRecipeId"]
    replay = send(
        chat_api,
        source["conversationId"],
        key="explicit-revision-retry",
        content="Use less rice.",
        retry=turn["turnId"],
    )
    assert replay.json() == retried.json() and len(executor.calls) == 3
    assert len(documents(harness[0], "behavior")) == 1


def test_oversized_revision_source_cannot_be_trimmed_or_start_generation(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    source = generate(chat_api, executor)
    turn = documents(harness[0], "turn")[0]
    step = turn["recipe"]["instructions"][0]
    turn["recipe"]["instructions"] = [
        {**step, "text": "\U0001f600" * 500, "step": index + 1} for index in range(30)
    ]
    response = signal(chat_api, source["generatedRecipeId"], "revisions")
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "context_limit_exceeded"
    assert len(executor.calls) == len(documents(harness[0], "turn")) == 1
    assert len(documents(harness[0], "behavior")) == 1
    assert documents(harness[0], "behavior")[0]["kind"] == "revisionRequested"


@pytest.mark.parametrize(
    ("path", "body", "headers", "status"),
    [
        (RECIPES, {"generatedRecipeId": str(uuid4()), "recipe": {}}, {"Idempotency-Key": "x"}, 422),
        (
            RECIPES,
            {"generatedRecipeId": str(uuid4()), "cookedAt": "2020-01-01"},
            {"Idempotency-Key": "x"},
            422,
        ),
        (
            RECIPES,
            {"generatedRecipeId": str(uuid4()), "cooked": "true"},
            {"Idempotency-Key": "x"},
            422,
        ),
        (RECIPES, {"generatedRecipeId": str(uuid4())}, {}, 422),
        (RECIPES + f"/{uuid4()}/cooked", {}, {"Idempotency-Key": "x"}, 428),
        (RECIPES + f"/{uuid4()}/cooked", {}, {"Idempotency-Key": "x", "If-Match": "*"}, 422),
        (
            f"/api/v1/generated-recipes/{uuid4()}/revisions",
            {"reason": " "},
            {"Idempotency-Key": "x"},
            422,
        ),
        (
            f"/api/v1/generated-recipes/{uuid4()}/rejections",
            {"reason": "x" * 2001},
            {"Idempotency-Key": "x"},
            422,
        ),
    ],
)
def test_invalid_lifecycle_input_has_no_storage_or_agent_side_effects(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    path: str,
    body: dict[str, Any],
    headers: dict[str, str],
    status: int,
) -> None:
    calls = len(harness[0].calls)
    response = chat_api.post(path, json=body, headers=headers)
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/problem+json"
    assert len(harness[0].calls) == calls and not executor.calls
    assert "generatedRecipeId" not in response.text and "2020-01-01" not in response.text
