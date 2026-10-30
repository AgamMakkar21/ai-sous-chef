import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sous_chef.adapters.cosmos import CosmosRepository, open_cosmos
from sous_chef.app import create_app
from sous_chef.domain.authorization import AuthenticatedUser, OwnedResourceNotFound
from sous_chef.domain.chat import ChatExecution, ExecutionFailed, ValidatedReply
from sous_chef.domain.cosmos import (
    CosmosError,
    CosmosFailure,
    NewRecord,
    ProductContainer,
    RecordPatch,
    StoredRecord,
)
from sous_chef.domain.recipe_safety import RecipeSafetyError
from sous_chef.domain.recipes import StructuredRecipe
from sous_chef.services import chat
from sous_chef.services.recipe_safety import prepare_recipe_save
from sous_chef.settings import Settings
from test_conversation_api import BASE, CHAT, OWNER, create
from test_conversation_api import conversation_settings as conversation_settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_profile_api import onboard, signed_token
from test_recipe_safety import full_recipe


class FakeExecutor:
    """Deterministic injected test seam, never part of deployed application setup."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.calls: list[ChatExecution] = []
        self.mode = "success"
        self.started = Event()
        self.release = Event()
        self.block = False
        self.recipe: StructuredRecipe | None = None
        self.recipe_id = uuid4()
        self.control_kind: Literal["clarification", "unable"] = "clarification"
        self.reply_content = "Synthetic validated answer"

    async def execute(self, request: ChatExecution) -> ValidatedReply:
        self.calls.append(request)
        conversation = self.transport.documents[
            (CHAT.value, request.user.user_id, str(request.conversation_id))
        ]
        turn = self.transport.documents[(CHAT.value, request.user.user_id, str(request.turn_id))]
        operation = self.transport.documents[
            (CHAT.value, request.user.user_id, str(request.operation_id))
        ]
        assert conversation["activeOperationId"] == str(request.operation_id)
        assert turn["status"] == operation["status"] == "pending"
        assert turn["messages"][0]["content"] == request.content
        assert [message["role"] for message in turn["messages"]] == ["user"]
        assert request.explicit_preferences.allergies
        self.started.set()
        if self.block:
            assert await asyncio.to_thread(self.release.wait, 10)
        if self.mode == "fail":
            raise ExecutionFailed("private upstream failure")
        if self.mode == "timeout":
            raise TimeoutError("private upstream timeout")
        if self.mode == "crash":
            raise RuntimeError("private executor crash")
        if self.mode == "invalid":
            return ValidatedReply(content=" " * 3, entity_id="synthetic-agent")
        if self.mode == "reference_only":
            return ValidatedReply(
                content="Private unvalidated recipe draft", entity_id="synthetic-agent"
            ).model_copy(update={"generated_recipe_id": self.recipe_id})
        if self.mode == "lost_completion":
            self.transport.write_then_disconnect = True
        if self.mode == "unavailable_completion":
            self.transport.faults["POST"].extend([503, 503, 503])
        if self.mode == "transient_completion":
            self.transport.faults["POST"].append(503)
        return ValidatedReply(
            content=self.reply_content,
            kind="recipe" if self.recipe is not None else self.control_kind,
            entity_id="synthetic-agent",
            generated_recipe_id=self.recipe_id if self.recipe is not None else None,
            provider_response_id="private-provider-response",
            recipe=self.recipe,
        )


@pytest.fixture
def executor(harness: tuple[Transport, SyntheticCredential]) -> FakeExecutor:
    return FakeExecutor(harness[0])


@pytest.fixture
def chat_api(
    conversation_settings: Settings,
    auth_headers: dict[str, str],
    executor: FakeExecutor,
) -> Iterator[TestClient]:
    with TestClient(
        create_app(conversation_settings, chat_executor=executor), headers=auth_headers
    ) as client:
        onboard(client)
        yield client
    executor.release.set()


def send(
    api: TestClient,
    conversation: str,
    key: str = "send-key",
    content: str = "Private dinner request",
    retry: str | None = None,
) -> Any:
    body = {"content": content}
    if retry is not None:
        body["retryOfTurnId"] = retry
    return api.post(f"{BASE}/{conversation}/turns", json=body, headers={"Idempotency-Key": key})


def documents(transport: Transport, kind: str) -> list[dict[str, Any]]:
    return [record for record in transport.documents.values() if record["recordKind"] == kind]


def test_durable_exchange_replays_after_app_restart_without_executor(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    conversation_settings: Settings,
    auth_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, _ = harness
    conversation = create(chat_api)
    first = send(chat_api, conversation, content="  Private dinner request  ")
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    turn = first.json()["data"]
    assert turn["turnIndex"] == 0
    assert turn["status"] == "completed"
    assert [message["role"] for message in turn["messages"]] == ["user", "assistant"]
    assert turn["messages"][0]["entityId"] == OWNER.user_id
    assert turn["messages"][1]["entityId"] == "synthetic-agent"
    assert all(message["timestamp"].endswith("Z") for message in turn["messages"])
    assert len({message["messageId"] for message in turn["messages"]}) == 2
    assert "providerResponseId" not in first.text
    assert "requestHash" not in first.text
    assert len(executor.calls) == 1
    with TestClient(create_app(conversation_settings), headers=auth_headers) as restarted:
        replay = send(restarted, conversation)
        assert replay.status_code == 200
        assert replay.json() == first.json()
        inspected = restarted.get(first.headers["location"])
        assert inspected.status_code == 200
        assert inspected.json()["data"]["turn"] == turn
    assert len(documents(transport, "turn")) == len(documents(transport, "sendOperation")) == 1
    metadata = documents(transport, "conversation")[0]
    assert metadata["nextTurnIndex"] == 1
    assert metadata["activeOperationId"] is None
    history = chat_api.get(f"{BASE}/{conversation}/turns").json()["items"]
    assert history == [turn]
    for private in (
        "Private dinner request",
        "Synthetic validated answer",
        "private-provider-response",
        OWNER.user_id,
        "send-key",
    ):
        assert private not in caplog.text


def test_payload_conflict_and_key_scope(chat_api: TestClient, executor: FakeExecutor) -> None:
    first = create(chat_api)
    second = create(chat_api)
    assert send(chat_api, first).status_code == 200
    conflict = send(chat_api, first, content="Different input")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
    assert send(chat_api, second).status_code == 200
    assert len(executor.calls) == 2


@pytest.mark.parametrize("rejected", [False, True])
def test_full_recipe_gate_controls_public_text_and_atomic_snapshot(
    rejected: bool,
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor.recipe = full_recipe(
        instructions=("Cook the rice, then garnish with peanut.",)
        if rejected
        else ("Cook the rice.",)
    )
    conversation = create(chat_api)
    response = send(chat_api, conversation)
    assert response.status_code == (502 if rejected else 200), response.text
    turn = documents(harness[0], "turn")[0]
    assert turn["status"] == ("failed" if rejected else "completed")
    assert "Synthetic validated answer" not in json.dumps(turn)
    assert "garnish with peanut" not in json.dumps(turn)
    assert "garnish with peanut" not in response.text + caplog.text
    if rejected:
        assert len(turn["messages"]) == 1
        assert turn["generatedRecipeId"] is None
        assert "recipe" not in turn
    else:
        assert "Cook the rice." in turn["messages"][1]["content"]
        assert turn["recipe"]["ingredients"][0]["name"] == "rice"
        assert turn["generatedRecipeId"] == str(executor.recipe_id)
        assert turn["safetyProfileRevision"]
    assert not documents(harness[0], "recipe")


@pytest.mark.parametrize("kind", ["clarification", "unable"])
def test_text_only_reply_cannot_smuggle_an_unvalidated_recipe(
    kind: Literal["clarification", "unable"],
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    executor.control_kind = kind
    executor.reply_content = "Cook the rice, then garnish with peanut."
    response = send(chat_api, create(chat_api))
    assert response.status_code == 200
    assert executor.reply_content not in response.text
    turn = documents(harness[0], "turn")[0]
    assert executor.reply_content not in json.dumps(turn)
    assert "recipe" not in turn and turn["generatedRecipeId"] is None
    public = response.json()["data"]["messages"][1]["content"]
    assert public == (
        "Which dish would you like to make?"
        if kind == "clarification"
        else "I couldn't complete a recipe that passes the current ingredient and cooking-safety "
        "checks within the allowed attempts. No recipe was approved or saved."
    )


def test_profile_change_before_delivery_revalidates_the_actual_recipe(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    executor.recipe = full_recipe()
    executor.block = True
    conversation = create(chat_api)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(send, chat_api, conversation)
        try:
            assert executor.started.wait(10)
            current = chat_api.get("/api/v1/me")
            updated = chat_api.patch(
                "/api/v1/me/preferences",
                headers={"If-Match": current.headers["etag"]},
                json={"allergies": ["rice"]},
            )
            assert updated.status_code == 200
        finally:
            executor.release.set()
        response = future.result(timeout=10)
    assert response.status_code == 502
    assert response.json()["code"] == "invalid_assistant_response"
    turn = documents(harness[0], "turn")[0]
    assert len(turn["messages"]) == 1 and turn["generatedRecipeId"] is None
    assert "recipe" not in turn and not documents(harness[0], "recipe")


def test_save_preparation_loads_owned_generation_and_revalidates_latest_policy(
    chat_api: TestClient,
    executor: FakeExecutor,
    conversation_settings: Settings,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    executor.recipe = full_recipe()
    conversation = create(chat_api)
    sent = send(chat_api, conversation)
    assert sent.status_code == 200
    turn_id = UUID(sent.json()["data"]["turnId"])

    async def prepare(user: AuthenticatedUser = OWNER) -> Any:
        async with open_cosmos(conversation_settings) as repository:
            return await prepare_recipe_save(
                repository,
                user,
                turn_id=turn_id,
                generated_recipe_id=executor.recipe_id,
            )

    checked = asyncio.run(prepare())
    assert checked.recipe == executor.recipe
    assert "Cook the rice." in checked.content
    stranger = AuthenticatedUser.from_verified_identity("synthetic", "other-user")
    with pytest.raises(OwnedResourceNotFound):
        asyncio.run(prepare(stranger))
    current = chat_api.get("/api/v1/me")
    updated = chat_api.patch(
        "/api/v1/me/preferences",
        headers={"If-Match": current.headers["etag"]},
        json={"allergies": ["rice"]},
    )
    assert updated.status_code == 200
    with pytest.raises(RecipeSafetyError):
        asyncio.run(prepare())
    assert not documents(harness[0], "recipe")
    assert documents(harness[0], "turn")[0]["recipe"]["ingredients"][0]["name"] == "rice"


def test_completion_retry_rechecks_profile_without_another_generation(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor.recipe = full_recipe()
    original = CosmosRepository.commit_chat
    changed = False

    async def change_before_retry(
        repository: CosmosRepository,
        user: AuthenticatedUser,
        *,
        creates: tuple[NewRecord, ...] = (),
        patches: tuple[tuple[StoredRecord, RecordPatch], ...],
        deadline: float | None = None,
    ) -> tuple[StoredRecord, ...]:
        nonlocal changed
        if not changed and any(patch.fields.get("recipe") for _, patch in patches):
            changed = True
            profile = await repository.read(user, ProductContainer.USERS, user.user_id)
            assert profile is not None
            preferences = profile.body["explicitPreferences"]
            assert isinstance(preferences, dict)
            await repository.patch(
                user,
                ProductContainer.USERS,
                user.user_id,
                RecordPatch(
                    etag=profile.etag,
                    fields={"explicitPreferences": {**preferences, "allergies": ["rice"]}},
                ),
            )
            raise CosmosError(CosmosFailure.UNAVAILABLE, uuid4())
        return await original(repository, user, creates=creates, patches=patches, deadline=deadline)

    monkeypatch.setattr(CosmosRepository, "commit_chat", change_before_retry)
    response = send(chat_api, create(chat_api))
    assert response.status_code == 502 and changed
    assert len(executor.calls) == 1
    turn = documents(harness[0], "turn")[0]
    assert turn["status"] == "failed"
    assert len(turn["messages"]) == 1 and turn["generatedRecipeId"] is None
    assert "recipe" not in turn and not documents(harness[0], "recipe")


@pytest.mark.parametrize(
    "mode,code",
    [
        ("fail", "execution_failed"),
        ("invalid", "invalid_assistant_response"),
        ("reference_only", "invalid_assistant_response"),
    ],
)
def test_known_failure_retries_existing_turn_explicitly(
    mode: str,
    code: str,
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    conversation = create(chat_api)
    executor.mode = mode
    failed = send(chat_api, conversation)
    assert failed.status_code == 502
    assert failed.json()["code"] == code
    state = chat_api.get(failed.headers["location"]).json()["data"]
    original = state["turn"]
    assert state["status"] == original["status"] == "failed"
    assert len(original["messages"]) == 1
    assert send(chat_api, conversation).status_code == 502
    assert len(executor.calls) == 1
    executor.mode = "success"
    retry = send(chat_api, conversation, key="retry", retry=original["turnId"])
    assert retry.status_code == 200, retry.text
    assert retry.json()["data"]["turnId"] == original["turnId"]
    assert retry.json()["data"]["turnIndex"] == original["turnIndex"] == 0
    assert retry.json()["data"]["messages"][0] == original["messages"][0]
    assert len(documents(transport, "turn")) == 1
    assert len(documents(transport, "sendOperation")) == 2
    assert documents(transport, "conversation")[0]["nextTurnIndex"] == 1
    assert chat_api.get(failed.headers["location"]).json()["data"]["status"] == "failed"
    assert (
        send(chat_api, conversation, key="retry", retry=original["turnId"]).json() == retry.json()
    )
    assert send(chat_api, conversation, key="again", retry=original["turnId"]).status_code == 409
    assert len(executor.calls) == 2


def test_parallel_sends_are_serialized_and_pending_duplicates_do_not_execute(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    conversation = create(chat_api)
    executor.block = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(send, chat_api, conversation)
        try:
            assert executor.started.wait(10)
            duplicate = send(chat_api, conversation)
            assert duplicate.status_code == 202
            assert duplicate.headers["retry-after"] == "2"
            pending = chat_api.get(duplicate.headers["location"])
            assert pending.json()["data"]["status"] == "pending"
            different = send(chat_api, conversation, key="different")
            assert different.status_code == 409
            assert len(executor.calls) == 1
            assert len(documents(transport, "turn")) == 1
        finally:
            executor.release.set()
        assert first.result(timeout=10).status_code == 200
    executor.block = False
    following = send(chat_api, conversation, key="different")
    assert following.status_code == 200
    assert following.json()["data"]["turnIndex"] == 1
    turns = chat_api.get(f"{BASE}/{conversation}/turns").json()["items"]
    assert [turn["turnIndex"] for turn in turns] == [0, 1]


@pytest.mark.parametrize("same_key", [True, False])
def test_racing_first_admissions_roll_back_loser_without_orphan_records(
    same_key: bool,
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = create(chat_api)
    original = CosmosRepository.commit_chat
    barrier = Barrier(2)

    async def competing_batch(
        repository: CosmosRepository,
        user: AuthenticatedUser,
        *,
        creates: tuple[NewRecord, ...] = (),
        patches: tuple[tuple[StoredRecord, RecordPatch], ...],
        deadline: float | None = None,
    ) -> tuple[StoredRecord, ...]:
        if creates:
            await asyncio.to_thread(barrier.wait, 10)
        return await original(repository, user, creates=creates, patches=patches, deadline=deadline)

    monkeypatch.setattr(CosmosRepository, "commit_chat", competing_batch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda key: send(chat_api, conversation, key=key),
                ("one", "one" if same_key else "two"),
            )
        )
    statuses = sorted(response.status_code for response in results)
    assert statuses in ([200, 200], [200, 202]) if same_key else statuses == [200, 409]
    assert len(executor.calls) == 1
    transport, _ = harness
    assert len(documents(transport, "turn")) == 1
    assert len(documents(transport, "sendOperation")) == 1
    assert documents(transport, "conversation")[0]["nextTurnIndex"] == 1


def test_executor_deadline_is_enforced_and_stale_completion_cannot_overwrite_unknown(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat, "SEND_TIMEOUT_SECONDS", 6)
    conversation = create(chat_api)
    executor.block = True
    try:
        timed_out = send(chat_api, conversation)
        assert timed_out.status_code == 504
        assert documents(harness[0], "turn")[0]["status"] == "outcomeUnknown"
        assert len(executor.calls) == 1
    finally:
        executor.release.set()


def test_expired_operation_fences_a_late_executor_result(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    conversation = create(chat_api)
    executor.block = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(send, chat_api, conversation)
        try:
            assert executor.started.wait(10)
            operation = documents(harness[0], "sendOperation")[0]
            operation["expiresAt"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            inspected = chat_api.get(f"{BASE}/{conversation}/operations/{operation['id']}")
            assert inspected.json()["data"]["status"] == "outcomeUnknown"
        finally:
            executor.release.set()
        assert first.result(timeout=10).status_code == 504
    assert len(documents(harness[0], "turn")[0]["messages"]) == 1


def test_atomic_completion_reconciles_lost_response(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    executor.mode = "lost_completion"
    conversation = create(chat_api)
    result = send(chat_api, conversation)
    assert result.status_code == 200, result.text
    harness[0].write_then_disconnect = False
    assert send(chat_api, conversation).json() == result.json()
    assert len(executor.calls) == 1
    assert len(documents(harness[0], "turn")) == 1


def test_transient_completion_failure_persists_existing_reply_without_regeneration(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    conversation = create(chat_api)
    executor.mode = "transient_completion"
    result = send(chat_api, conversation)
    assert result.status_code == 200, result.text
    assert len(executor.calls) == 1
    assert len(documents(harness[0], "turn")) == 1
    assert len(documents(harness[0], "turn")[0]["messages"]) == 2
    assert send(chat_api, conversation).json() == result.json()
    assert len(executor.calls) == 1


def test_atomic_admission_lost_response_never_reinvokes(
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    transport, _ = harness
    conversation = create(chat_api)
    transport.write_then_disconnect = True
    result = send(chat_api, conversation)
    assert result.status_code == 503
    assert not executor.calls
    transport.write_then_disconnect = False
    replay = send(chat_api, conversation)
    assert replay.status_code == 202
    assert not executor.calls
    assert len(documents(transport, "turn")) == 1
    operation = documents(transport, "sendOperation")[0]
    operation["expiresAt"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    inspected = chat_api.get(replay.headers["location"])
    assert inspected.json()["data"]["status"] == "outcomeUnknown"
    assert send(chat_api, conversation).status_code == 504
    assert not executor.calls


@pytest.mark.parametrize("mode", ["timeout", "crash", "unavailable_completion"])
def test_uncertain_execution_is_visible_and_never_blindly_retried(
    mode: str,
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport, _ = harness
    conversation = create(chat_api)
    executor.mode = mode
    result = send(chat_api, conversation)
    assert result.status_code in {500, 503, 504}
    assert "private" not in result.text
    operation = documents(transport, "sendOperation")[0]
    operation["expiresAt"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    replay = send(chat_api, conversation)
    assert replay.status_code == 504
    state = chat_api.get(replay.headers["location"]).json()["data"]
    assert state["status"] == state["turn"]["status"] == "outcomeUnknown"
    assert state["failureCode"] == "execution_outcome_unknown"
    assert len(state["turn"]["messages"]) == 1
    assert send(chat_api, conversation, key="retry", retry=state["turnId"]).status_code == 409
    assert send(chat_api, conversation, key="another-send").status_code == 409
    assert len(executor.calls) == 1
    assert "private executor crash" not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"content": ""},
        {"content": "   "},
        {"content": 1},
        {"content": "x" * 4001},
        {"content": "text", "userId": "forged"},
        {"content": "text", "role": "assistant"},
        {"content": "text", "status": "completed"},
        {"content": "text", "retry_of_turn_id": str(uuid4())},
    ],
)
def test_invalid_input_rejected_before_persistence_or_execution(
    payload: dict[str, Any],
    chat_api: TestClient,
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    conversation = create(chat_api)
    response = chat_api.post(
        f"{BASE}/{conversation}/turns", json=payload, headers={"Idempotency-Key": "key"}
    )
    assert response.status_code == 422
    assert not executor.calls
    assert not documents(harness[0], "turn")
    assert not documents(harness[0], "sendOperation")


def test_exact_input_boundary_and_oversized_body(
    chat_api: TestClient, executor: FakeExecutor
) -> None:
    conversation = create(chat_api)
    assert send(chat_api, conversation, content="x" * 4000).status_code == 200
    oversized = chat_api.post(
        f"{BASE}/{conversation}/turns",
        content=json.dumps({"content": "x" * (128 * 1024)}),
        headers={"Idempotency-Key": "oversized", "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    assert len(executor.calls) == 1


def test_user_and_conversation_isolation(
    chat_api: TestClient,
    executor: FakeExecutor,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    conversation = create(chat_api)
    completed = send(chat_api, conversation)
    second = create(chat_api)
    operation_id = completed.headers["location"].rsplit("/", 1)[1]
    assert chat_api.get(f"{BASE}/{second}/operations/{operation_id}").status_code == 404
    missing = send(chat_api, str(uuid4()))
    assert missing.status_code == 404
    other_headers = {"Authorization": "Bearer " + signed_token(signing_key, "other-user")}
    denied = chat_api.post(
        f"{BASE}/{conversation}/turns",
        json={"content": "Forbidden"},
        headers={**other_headers, "Idempotency-Key": "key"},
    )
    assert denied.status_code == 404
    assert denied.json()["code"] == missing.json()["code"] == "not_found"
    assert chat_api.get(completed.headers["location"], headers=other_headers).status_code == 404
    assert len(executor.calls) == 1


def test_retry_binding_and_request_headers_are_enforced(
    chat_api: TestClient, executor: FakeExecutor
) -> None:
    conversation = create(chat_api)
    executor.mode = "fail"
    failed = send(chat_api, conversation)
    turn = chat_api.get(failed.headers["location"]).json()["data"]["turn"]
    executor.mode = "success"
    assert (
        send(
            chat_api, conversation, key="retry", content="Changed text", retry=turn["turnId"]
        ).status_code
        == 409
    )
    second = create(chat_api)
    assert send(chat_api, second, key="retry", retry=turn["turnId"]).status_code == 404
    assert send(chat_api, conversation, key="retry", retry=str(uuid4())).status_code == 404
    for headers in (
        {},
        {"Idempotency-Key": ""},
        {"Idempotency-Key": "x" * 129},
        {"Idempotency-Key": "contains space"},
        [("Idempotency-Key", "one"), ("Idempotency-Key", "two")],
    ):
        response = chat_api.post(
            f"{BASE}/{conversation}/turns", json={"content": "Question"}, headers=headers
        )
        assert response.status_code == 422
    assert (
        chat_api.post(
            f"{BASE}/{conversation}/turns?userId=forged",
            json={"content": "Question"},
            headers={"Idempotency-Key": "key"},
        ).status_code
        == 422
    )
    assert chat_api.get(failed.headers["location"] + "?userId=forged").status_code == 422
    assert len(executor.calls) == 1


def test_missing_onboarding_and_executor_fail_without_creating_turns(
    conversation_settings: Settings,
    auth_headers: dict[str, str],
    executor: FakeExecutor,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    with TestClient(
        create_app(conversation_settings, chat_executor=executor), headers=auth_headers
    ) as api:
        conversation = create(api)
        assert send(api, conversation).status_code == 403
        api.get("/api/v1/me")
        assert send(api, conversation).status_code == 403
        onboard(api)
    with TestClient(create_app(conversation_settings), headers=auth_headers) as api:
        response = send(api, conversation)
        assert response.status_code == 503
        assert response.json()["code"] == "configuration_unavailable"
    assert not executor.calls
    assert not documents(harness[0], "turn")
