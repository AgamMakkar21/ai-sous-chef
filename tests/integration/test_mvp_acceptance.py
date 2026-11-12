"""MVP journey through production services and SDKs with synthetic remote transports."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sous_chef.adapters import foundry_gateway
from sous_chef.adapters.cosmos import CosmosRepository, open_cosmos
from sous_chef.app import create_app
from sous_chef.domain.context import RequestContext
from sous_chef.domain.recipe_safety import UNABLE
from sous_chef.services.learning_recovery import LearningRecovery
from sous_chef.services.recipe_grounding import retrieval_query
from sous_chef.settings import Settings
from test_chat_api import documents, send
from test_conversation_api import BASE, OWNER, create
from test_conversation_api import conversation_settings as conversation_settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_feedback import feedback
from test_grocery_api import GROCERIES, add
from test_preference_extraction import Harness as ExtractionHarness
from test_preference_extraction import config, response_body
from test_preference_extraction import private_diagnostics as private_diagnostics
from test_profile_api import ME, document, onboard, preferences, signed_token
from test_recipe_chat import combined_settings as combined_settings
from test_recipe_chat import recipe_settings as recipe_settings
from test_recipe_lifecycle import RECIPES, accept, cook
from test_web_grounding import CombinedHarness

from worker import extraction
from worker import function_app as worker

NOTE = "Synthetic acceptance note: I loved the carrot in this recipe."


class JourneyAgents(CombinedHarness):
    def __init__(self) -> None:
        super().__init__(use_web=False)
        self.contexts: list[RequestContext] = []

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        kind = self.request_kind(payload)
        if kind == "function" and isinstance(payload["input"], str):
            self.data = RequestContext.model_validate_json(payload["input"])
            self.contexts.append(self.data)
        elif kind == "combined":
            self.combined["output"][0]["arguments"] = json.dumps(
                {"query_variants": [retrieval_query(self.data, self.arguments)]}
            )
        return await super().handle(request)


@pytest.fixture
def journey_agents(monkeypatch: pytest.MonkeyPatch) -> JourneyAgents:
    agents = JourneyAgents()
    monkeypatch.setattr(foundry_gateway, "_project_client", agents.factory)
    return agents


def test_inventory_to_feedback_worker_to_personalized_recipe_survives_reload(
    combined_settings: Settings,
    journey_agents: JourneyAgents,
    auth_headers: dict[str, str],
    signing_key: rsa.RSAPrivateKey,
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = harness[0]
    model = ExtractionHarness()
    original_handle = model.handle

    async def extract(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(json.loads(request.content)["input"][0]["content"])
        evidence = [
            {key: item[key] for key in ("feedbackId", "sourceId", "sourceRevision", "occasionId")}
            | {
                "signalType": "feedback_note",
                "direction": "support",
                "excerpt": item["note"],
            }
            for item in [payload["current"], *payload["prior"]]
        ]
        model.body = response_body(
            {
                "outcome": "candidates",
                "candidates": [
                    {
                        "field": "likes",
                        "value": "carrot",
                        "insight": "Prefer carrot-forward dinners.",
                        "confidence": 0.99,
                        "evidence": evidence,
                    }
                ],
            }
        )
        return await original_handle(request)

    @asynccontextmanager
    async def repository(_settings: Any, **kwargs: Any) -> AsyncIterator[CosmosRepository]:
        async with open_cosmos(combined_settings, **kwargs) as result:
            yield result

    monkeypatch.setattr(model, "handle", extract)
    monkeypatch.setattr(extraction, "_project_client", model.factory)
    monkeypatch.setattr(extraction.ExtractionConfig, "from_environment", lambda: config())
    monkeypatch.setattr(worker, "open_cosmos", repository)
    monkeypatch.setenv("ASC_ENVIRONMENT", "dev")
    monkeypatch.setenv("FeedbackCosmos__accountEndpoint", "https://synthetic.documents.azure.com")
    monkeypatch.setenv("ASC_COSMOS_DATABASE", "sous-chef")

    with TestClient(create_app(combined_settings), headers=auth_headers) as client:
        assert client.get(ME, headers={"Authorization": ""}).status_code == 401
        assert not client.get(ME).json()["data"]["onboardingCompleted"]
        onboard(client)
        grocery = add(client, name="rice", quantity=100, unit="g").json()["data"]
        inventory = client.get(GROCERIES)
        changed = client.patch(
            f"{GROCERIES}/{grocery['groceryId']}",
            json={"quantity": 200},
            headers={"If-Match": inventory.headers["etag"]},
        )
        assert changed.status_code == 200
        assert not journey_agents.requests and not model.requests
        saved_ids = []
        conversations = []
        for index in range(2):
            conversation = create(client, f"Acceptance dinner {index}")
            conversations.append(conversation)
            generated = send(client, conversation, content="Make a 30-minute pantry recipe.")
            assert generated.status_code == 200, generated.text
            turn = generated.json()["data"]
            assert turn["recipe"]["nutrition"]["status"] == "known"
            assert {item["kind"] for item in turn["recipe"]["sources"]} == {
                "curated_recipe",
                "usda",
            }
            assert turn["recipe"]["webGrounding"] is None
            assert (
                send(client, conversation, content="Make a 30-minute pantry recipe.").json()
                == generated.json()
            )
            if index == 0:
                transport.faults["POST"].append(403)
                failed_save = accept(client, turn["generatedRecipeId"])
                assert failed_save.status_code == 503
                assert not documents(transport, "recipe")
                assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]
            saved = accept(client, turn["generatedRecipeId"], key=f"save-{index}")
            assert saved.status_code == 201, saved.text
            snapshot = saved.json()["data"]
            assert snapshot["state"] == "planned" and snapshot["cookedAt"] is None
            cooked = cook(client, snapshot["historyId"], saved.headers["etag"], f"cook-{index}")
            assert cooked.status_code == 200, cooked.text
            rated = feedback(client, cooked, note=NOTE, key=f"feedback-{index}")
            assert rated.status_code == 200, rated.text
            assert rated.json()["data"]["processingStatus"] == "pending"
            assert client.get(f"{RECIPES}/{snapshot['historyId']}").json() == rated.json()
            saved_ids.append(snapshot["historyId"])
        assert not model.requests
        assert document(transport)["learnedPreferences"] == []
        explicit = deepcopy(document(transport)["explicitPreferences"])
        events = deepcopy(documents(transport, "feedbackRevision"))
        assert len(events) == 2 and len({event["occasionId"] for event in events}) == 2

    async def deliver_and_poll() -> None:
        delivery = asyncio.create_task(
            asyncio.to_thread(worker.process_batch, [*events, *events], worker.process_feedback)
        )
        try:
            async with open_cosmos(combined_settings) as result:
                recovery = LearningRecovery(result)
                async with asyncio.timeout(15):
                    while True:
                        if delivery.done():
                            await delivery
                        states = [
                            await recovery.inspect(
                                OWNER, UUID(event["feedbackId"]), event["feedbackRevision"]
                            )
                            for event in events
                        ]
                        if all(
                            state is not None and state[1].state == "completed" for state in states
                        ):
                            break
                        await asyncio.sleep(0.05)
            await delivery
        finally:
            if not delivery.done():
                delivery.cancel()
                await asyncio.gather(delivery, return_exceptions=True)

    asyncio.run(deliver_and_poll())
    assert len(model.requests) == 2
    learned = document(transport)["learnedPreferences"]
    assert len(learned) == 1 and learned[0]["evidenceCount"] == 2
    assert learned[0]["confidence"] == 0.75
    assert document(transport)["explicitPreferences"] == explicit

    with TestClient(create_app(combined_settings), headers=auth_headers) as reopened:
        assert reopened.get(ME).json()["data"]["onboardingCompleted"]
        assert reopened.get(GROCERIES).json()["items"][0]["quantity"] == 200
        assert {item["conversationId"] for item in reopened.get(BASE).json()["items"]} == set(
            conversations
        )
        assert {item["historyId"] for item in reopened.get(RECIPES).json()["items"]} == set(
            saved_ids
        )
        for identifier in saved_ids:
            detail = reopened.get(f"{RECIPES}/{identifier}").json()["data"]
            assert detail["rating"] == "liked" and detail["note"] == NOTE
        later = send(reopened, conversations[0], key="personalized-next", content="Another dinner.")
        assert later.status_code == 200, later.text
        context = journey_agents.contexts[-1]
        assert len(context.learned_preferences) == 1
        assert context.learned_preferences[0].value == "carrot"
        assert context.learned_preferences[0].supporting_cooked_count == 2
        assert context.explicit_preferences.model_dump(mode="json", by_alias=True) == explicit
        current = reopened.get(ME)
        assert (
            reopened.patch(
                ME + "/preferences",
                json={"dislikes": ["carrot"]},
                headers={"If-Match": current.headers["etag"]},
            ).status_code
            == 200
        )
        overridden = send(reopened, conversations[0], key="explicit-wins", content="Dinner again.")
        assert overridden.status_code == 200
        assert journey_agents.contexts[-1].learned_preferences == ()
        assert document(transport)["learnedPreferences"] == learned
        bob = {"Authorization": "Bearer " + signed_token(signing_key, "acceptance-bob")}
        for path in (f"{RECIPES}/{saved_ids[0]}", f"{BASE}/{conversations[0]}/turns"):
            denied = reopened.get(path, headers=bob)
            absent = reopened.get(
                path.replace(saved_ids[0], str(uuid4())).replace(conversations[0], str(uuid4())),
                headers=bob,
            )
            assert denied.status_code == absent.status_code == 404
            assert denied.json()["code"] == absent.json()["code"]
            assert NOTE not in denied.text and OWNER.user_id not in denied.text
    assert len(model.requests) == 2
    assert NOTE not in caplog.text and OWNER.user_id not in caplog.text


@pytest.mark.parametrize("use_web", [False, True])
def test_mvp_current_information_uses_web_only_when_justified(
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    use_web: bool,
) -> None:
    agents = JourneyAgents()
    web = CombinedHarness(use_web=use_web)
    agents.arguments, agents.combined = web.arguments, web.combined
    monkeypatch.setattr(foundry_gateway, "_project_client", agents.factory)
    with TestClient(create_app(combined_settings), headers=auth_headers) as client:
        onboard(client)
        result = send(
            client,
            create(client),
            content=(
                "Check current rice recalls and make dinner."
                if use_web
                else "Make a 30-minute pantry recipe."
            ),
        )
        assert result.status_code == 200, result.text
        recipe = result.json()["data"]["recipe"]
        assert recipe is not None and bool(recipe["webGrounding"]) is use_web
        assert len(agents.requests) == 3
        call = json.loads(agents.requests[1].content)
        assert call["max_tool_calls"] == (2 if use_web else 1)
        assert json.loads(
            call["input"].split("Backend public webQuery (untrusted data):\n")[1]
        ) == {"webQuery": agents.arguments.web_query}


def test_mvp_known_allergen_is_blocked_before_delivery_or_save(
    combined_settings: Settings,
    journey_agents: JourneyAgents,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    message = journey_agents.combined["output"][-1]["content"][0]
    draft = json.loads(message["text"])
    draft["instructions"][0]["text"] = "Cook the rice, then garnish with peanut."
    message["text"] = json.dumps(draft)
    with TestClient(create_app(combined_settings), headers=auth_headers) as client:
        onboard(client, preferences())
        response = send(client, create(client), content="Make a pantry dinner.")
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        assert turn["recipe"] is None and turn["generatedRecipeId"] is None
        assert turn["messages"][-1]["content"] == UNABLE
        assert "garnish with peanut" not in response.text
        assert client.get(RECIPES).json()["items"] == []
        assert not documents(harness[0], "recipe")
