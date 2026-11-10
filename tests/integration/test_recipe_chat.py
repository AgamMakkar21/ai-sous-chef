"""Authenticated chat through real services/SDKs and synthetic Foundry/Cosmos/MCP transports."""

import json
import sys
from collections.abc import Iterator
from copy import deepcopy
from typing import Any
from unittest.mock import patch

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sous_chef.adapters import foundry_gateway
from sous_chef.app import create_app
from sous_chef.domain.context import RequestContext, ResolvedPreference
from sous_chef.domain.recipe_safety import UNABLE
from sous_chef.services.recipe_chat import RecipeChatExecutor
from sous_chef.services.recipe_grounding import retrieval_query
from sous_chef.settings import ConfigurationError, Settings
from test_chat_api import documents, send
from test_context import EvidenceReader, candidate, evidence
from test_conversation_api import BASE, OWNER, create
from test_conversation_api import conversation_settings as conversation_settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_foundry_gateway import Harness
from test_grocery_api import add
from test_grounded_specialist import configuration, native_mcp_result
from test_orchestrator_gateway import body, function_output
from test_profile_api import document, onboard, preferences
from test_usda_nutrition import SERVER
from test_web_grounding import CombinedHarness, WebHarness


class RecipeChatHarness(WebHarness):
    def __init__(self) -> None:
        super().__init__(use_web=False)
        self.fail_at = ""
        self.response_status = 200
        self.response_fault = ""
        self.reject_recipe = False
        self.contexts: list[RequestContext] = []
        self.change_profile: dict[str, Any] | None = None

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        kind = self.request_kind(payload)
        if kind == "function" and isinstance(payload["input"], str):
            self.data = RequestContext.model_validate_json(payload["input"])
            self.contexts.append(self.data)
        if kind == "mcp":
            self.recipe["output"][0]["arguments"] = json.dumps(
                {"query_variants": [retrieval_query(self.data, self.arguments)]}
            )
            if self.reject_recipe:
                draft = json.loads(self.recipe["output"][1]["content"][0]["text"])
                draft["instructions"][0]["text"] = "Cook the rice, then garnish with peanut."
                self.recipe["output"][1]["content"][0]["text"] = json.dumps(draft)
        if kind == "function" and isinstance(payload["input"], list) and self.change_profile:
            self.change_profile["explicitPreferences"]["allergies"].append("rice")
        self.status = self.response_status if kind == self.fail_at else 200
        self.fault = self.response_fault if kind == self.fail_at else ""
        return await super().handle(request)


@pytest.fixture
def recipe_transport(monkeypatch: pytest.MonkeyPatch) -> RecipeChatHarness:
    transport = RecipeChatHarness()
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    return transport


@pytest.fixture
def recipe_settings(conversation_settings: Settings) -> Settings:
    values = conversation_settings.model_dump()
    values.update(
        {
            key: value
            for key, value in configuration().model_dump().items()
            if key.startswith("foundry_") or key == "app_workload_principal_id"
        }
    )
    return Settings.model_validate(
        values
        | {
            "usda_enabled": True,
            "usda_mcp_python": sys.executable,
            "usda_mcp_server": SERVER,
            "usda_mcp_env": {"TEST_USDA_MODE": SecretStr("")},
        }
    )


@pytest.fixture
def combined_settings(recipe_settings: Settings) -> Settings:
    return Settings.model_validate(
        recipe_settings.model_dump()
        | {
            "foundry_recipe_nutrition_mode": "combined",
            "foundry_recipe_nutrition_agent_version": "4",
            "foundry_recipe_nutrition_retrieval_agent_version": None,
            "foundry_recipe_nutrition_web_agent_version": None,
        }
    )


@pytest.fixture
def recipe_api(
    recipe_settings: Settings,
    recipe_transport: RecipeChatHarness,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
) -> Iterator[TestClient]:
    with TestClient(create_app(recipe_settings), headers=auth_headers) as client:
        onboard(client)
        add(client, name="rice", quantity=100, unit="g")
        yield client


@pytest.mark.parametrize("use_web", [False, True])
@pytest.mark.parametrize("native_documents", [False, True])
@pytest.mark.parametrize(
    "poultry_request",
    [
        None,
        "Chicken dinner",
        "Dinner with 200 g CHICKEN BREASTS (boneless, skinless), 1/2 cup rice and carrots.",
        "Make dinner with:\n- chicken-breast, diced\n- rice\n- carrots",
        'Make dinner: [{"name":"chicken breast","quantity":0.2,"unit":"kg"},'
        '{"name":"rice"},{"name":"carrots"}]',
    ],
)
def test_combined_app_composition_returns_and_persists_a_grounded_recipe(
    use_web: bool,
    native_documents: bool,
    poultry_request: str | None,
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = CombinedHarness(use_web=use_web)
    if native_documents:
        transport.combined["output"][0]["output"] = json.dumps(native_mcp_result())
    if poultry_request is not None:
        message = transport.combined["output"][-1]["content"][0]
        draft = json.loads(message["text"])
        draft["title"] = "Chicken and rice skillet"
        draft["ingredients"].append(
            {"lineId": "chicken", "name": "chicken breast", "quantity": 200, "unit": "g"}
        )
        draft["instructions"].append(
            {
                "step": 3,
                "text": "Pan-fry the chicken breast in a skillet until fully cooked, "
                "turning occasionally.",
                "ingredientLineIds": ["chicken"],
            }
        )
        draft["instructions"].append(
            {
                "step": 4,
                "text": "Serve the chicken breast with rice and carrot.",
                "ingredientLineIds": ["chicken", "rice", "carrot"],
            }
        )
        message["text"] = json.dumps(draft)
        if use_web:
            claim = draft["webClaims"][0]["text"]
            message["annotations"][0]["start_index"] = message["text"].index(claim)
            message["annotations"][0]["end_index"] = message["text"].index(claim) + len(claim)
    original_handle = transport.handle

    async def handle(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        kind = transport.request_kind(payload)
        if kind == "function" and isinstance(payload["input"], str):
            transport.data = RequestContext.model_validate_json(payload["input"])
        elif kind == "combined":
            transport.combined["output"][0]["arguments"] = json.dumps(
                {"query_variants": [retrieval_query(transport.data, transport.arguments)]}
            )
        return await original_handle(request)

    monkeypatch.setattr(transport, "handle", handle)
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    content = (
        "Check current rice recalls and make a dinner."
        if use_web
        else "Make a 30-minute pantry recipe."
    )
    if poultry_request is not None:
        content = ("Check current rice recalls. " if use_web else "") + poultry_request
    with TestClient(create_app(combined_settings), headers=auth_headers) as client:
        onboard(
            client,
            preferences() | {"dietaryRestrictions": []}
            if poultry_request is not None
            else preferences(),
        )
        add(client, name="rice", quantity=100, unit="g")
        conversation = create(client)
        response = send(client, conversation, content=content)
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        assert turn["status"] == "completed" and turn["generatedRecipeId"]
        assert turn["recipe"]["nutrition"]["status"] == "known"
        assert transport.data.current_request == content
        if poultry_request is not None:
            assert turn["messages"][0]["content"] == content
            assert turn["recipe"]["title"] == "Chicken and rice skillet"
            assert "Pan-fry the chicken breast" in turn["messages"][1]["content"]
            assert "thermometer" not in turn["messages"][1]["content"]
            assert "chicken" in {
                mapping["ingredientLineId"] for mapping in turn["recipe"]["nutrition"]["mappings"]
            }
        assert {source["kind"] for source in turn["recipe"]["sources"]} == {
            "curated_recipe",
            "usda",
        }
        assert bool(turn["recipe"]["webGrounding"]) == use_web
        assert len(transport.requests) == 3
        combined = json.loads(transport.requests[1].content)
        assert combined["agent_reference"]["version"] == "4"
        assert combined["max_tool_calls"] == (2 if use_web else 1)
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]
        assert send(client, conversation, content=content).json() == response.json()
        assert len(transport.requests) == 3
    with TestClient(create_app(combined_settings), headers=auth_headers) as restarted:
        assert restarted.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]
    assert len(transport.requests) == 3


@pytest.mark.parametrize("allergen_location", ["metadata", "ingredient"])
def test_combined_revision_preserves_context_and_enriches_only_its_current_ingredients(
    allergen_location: str,
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = CombinedHarness(use_web=False)
    original_handle = transport.handle
    draft = json.loads(transport.combined["output"][-1]["content"][0]["text"])
    attempts = 0
    correction = "Remove the conflicting garnish and retain the original measured ingredients."

    async def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        payload = json.loads(request.content)
        kind = transport.request_kind(payload)
        if kind == "function" and isinstance(payload["input"], str):
            transport.data = RequestContext.model_validate_json(payload["input"])
        elif kind == "function":
            feedback = json.loads(payload["input"][-1]["output"])
            assert feedback == {
                "status": "safety_rejected",
                "revisions_remaining": 1,
                "rejection_code": "recipe_ingredient_rejected",
                "candidate": None,
            }
            assert "Private rejected peanut skillet" not in request.content.decode()
            transport.arguments = transport.arguments.model_copy(
                update={"requirements": correction}
            )
            transport.body = body(
                function_output(arguments=transport.arguments.model_dump_json())
                | {"call_id": "revision"}
            )
            return await Harness.handle(transport, request)
        else:
            attempts += 1
            current = deepcopy(draft)
            if attempts == 1:
                current["title"] = "Private rejected peanut skillet"
                if allergen_location == "ingredient":
                    current["ingredients"][0]["name"] = "peanut oil"
            else:
                current["ingredients"][0]["quantity"] = 200
            transport.combined["output"][-1]["content"][0]["text"] = json.dumps(current)
            transport.combined["output"][0]["arguments"] = json.dumps(
                {"query_variants": [retrieval_query(transport.data, transport.arguments)]}
            )
        return await original_handle(request)

    monkeypatch.setattr(transport, "handle", handle)
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    app = create_app(combined_settings)
    with TestClient(app, headers=auth_headers) as client:
        onboard(client)
        add(client, name="rice", quantity=100, unit="g")
        conversation = create(client)
        nutrition = app.state.nutrition
        with patch.object(nutrition, "lookup", wraps=nutrition.lookup) as lookup:
            response = send(client, conversation, content="Make a 30-minute pantry recipe.")
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        assert turn["recipe"]["ingredients"][0]["quantity"] == 200
        energy = next(
            nutrient
            for nutrient in turn["recipe"]["nutrition"]["nutrients"]
            if nutrient["name"] == "energy"
        )
        assert energy["amount"] == 360
        assert [call.args[0].ingredients[0].quantity for call in lookup.call_args_list] == (
            [100, 200] if allergen_location == "metadata" else [200]
        )
        assert "Private rejected peanut skillet" not in response.text
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]
        assert (
            send(client, conversation, content="Make a 30-minute pantry recipe.").json()
            == response.json()
        )
    assert attempts == 2 and len(transport.requests) == 4
    requests = [json.loads(request.content) for request in transport.requests]
    assert [transport.request_kind(request) for request in requests] == [
        "function",
        "combined",
        "function",
        "combined",
    ]
    assert requests[0]["agent_reference"] == requests[2]["agent_reference"]
    assert requests[1]["agent_reference"] == requests[3]["agent_reference"]
    for request in (requests[1], requests[3]):
        assert request["input"].startswith(transport.data.prompt)
        assert request["store"] is False
        assert request["max_tool_calls"] == 1
        assert '"time_budget_minutes":30' in request["input"]
    assert correction in requests[3]["input"]


def test_chat_uses_the_startup_checked_nutrition_client(
    recipe_settings: Settings, auth_headers: dict[str, str]
) -> None:
    with patch.object(
        RecipeChatExecutor, "from_settings", wraps=RecipeChatExecutor.from_settings
    ) as factory:
        app = create_app(recipe_settings)
        with TestClient(app, headers=auth_headers):
            nutrition = app.state.nutrition
            assert factory.call_count == 1
            assert factory.call_args.kwargs["nutrition"] is nutrition
            assert nutrition._client is not None
        assert nutrition._client is None


@pytest.mark.parametrize("use_web", [False, True])
def test_pantry_recipe_returns_and_replays_the_same_grounded_structured_exchange(
    use_web: bool,
    recipe_api: TestClient,
    recipe_transport: RecipeChatHarness,
    recipe_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    caplog: pytest.LogCaptureFixture,
) -> None:
    recipe_transport.arguments = recipe_transport.arguments.model_copy(
        update={"use_web": use_web, "web_query": "current rice recalls" if use_web else None}
    )
    content = (
        "Check current rice recalls and make a dinner."
        if use_web
        else "Make a 30-minute pantry recipe."
    )
    conversation = create(recipe_api)
    response = send(recipe_api, conversation, content=content)
    assert response.status_code == 200, response.text
    turn = response.json()["data"]
    recipe = turn["recipe"]
    assert turn["status"] == "completed" and turn["generatedRecipeId"]
    assert turn["messages"][1]["entityId"] == "orchestrator-agent"
    assert recipe["totalMinutes"] <= 30
    assert recipe["ingredients"][0]["inventoryStatus"] == "available"
    assert {item["ingredientLineId"] for item in recipe["shoppingItems"]} == {"carrot", "oil"}
    assert recipe["nutrition"]["status"] == "known" and recipe["nutrition"]["isEstimate"]
    assert recipe["nutrition"]["nutrients"][0]["amount"] == 260
    assert {source["kind"] for source in recipe["sources"]} == {"curated_recipe", "usda"}
    assert bool(recipe["webGrounding"]) == use_web
    assert len(recipe_transport.requests) == 3 + int(use_web)
    context = recipe_transport.contexts[0]
    assert context.current_request == content
    assert context.explicit_preferences.allergies == ("peanut", "peaches")
    assert context.groceries[0].name == "rice" and not context.learned_preferences
    assert recipe_api.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]
    assert recipe_api.get(response.headers["location"]).json()["data"]["turn"] == turn
    disabled = recipe_settings.model_copy(update={"agents_enabled": False})
    with TestClient(create_app(disabled), headers=auth_headers) as restarted:
        replay = send(restarted, conversation, content=content)
        assert replay.status_code == 200 and replay.json() == response.json()
    assert len(recipe_transport.requests) == 3 + int(use_web)
    assert len(documents(harness[0], "turn")) == len(documents(harness[0], "sendOperation")) == 1
    assert documents(harness[0], "turn")[0]["generatedRecipeId"] == turn["generatedRecipeId"]
    for private in (content, OWNER.user_id, "hf-food-recipes-1", "resp_synthetic"):
        assert private not in caplog.text
    assert "providerResponseId" not in response.text


def test_final_allergen_and_mid_request_profile_change_never_deliver_a_recipe(
    recipe_api: TestClient,
    recipe_transport: RecipeChatHarness,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    recipe_transport.reject_recipe = True
    refused = send(recipe_api, create(recipe_api))
    assert refused.status_code == 200, refused.text
    assert refused.json()["data"]["recipe"] is None
    assert refused.json()["data"]["messages"][1]["content"] == UNABLE
    assert "garnish with peanut" not in refused.text
    recipe_transport.reject_recipe = False
    draft = json.loads(recipe_transport.recipe["output"][1]["content"][0]["text"])
    draft["instructions"][0]["text"] = "Cook the rice."
    recipe_transport.recipe["output"][1]["content"][0]["text"] = json.dumps(draft)
    recipe_transport.change_profile = document(harness[0])
    changed = send(recipe_api, create(recipe_api))
    assert changed.status_code == 502, changed.text
    failed = next(turn for turn in documents(harness[0], "turn") if turn["status"] == "failed")
    assert len(failed["messages"]) == 1 and not failed["generatedRecipeId"]
    assert "recipe" not in failed


@pytest.mark.parametrize("fail_at", ["function", "mcp"])
@pytest.mark.parametrize("fault", ["", "timeout", "connection"])
def test_failed_or_uncertain_agent_execution_preserves_recovery_without_replaying(
    fail_at: str,
    fault: str,
    recipe_api: TestClient,
    recipe_transport: RecipeChatHarness,
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    unknown = bool(fault)
    recipe_transport.fail_at = fail_at
    recipe_transport.response_status = 200 if unknown else 429
    recipe_transport.response_fault = fault
    conversation = create(recipe_api)
    response = send(recipe_api, conversation)
    assert response.status_code == (504 if unknown else 502), response.text
    operation = recipe_api.get(response.headers["location"]).json()["data"]
    assert operation["status"] == ("outcomeUnknown" if unknown else "failed")
    calls = len(recipe_transport.requests)
    assert send(recipe_api, conversation).status_code == response.status_code
    assert len(recipe_transport.requests) == calls
    recipe_transport.fail_at = ""
    if unknown:
        assert send(recipe_api, conversation, key="another-send").status_code == 409
        assert len(recipe_transport.requests) == calls
    else:
        retried = send(recipe_api, conversation, key="explicit-retry", retry=operation["turnId"])
        assert retried.status_code == 200, retried.text
        assert retried.json()["data"]["turnId"] == operation["turnId"]
        assert len(documents(harness[0], "turn")) == 1
        assert len(documents(harness[0], "sendOperation")) == 2


def test_eligible_learned_context_reaches_real_composition_without_overriding_explicit_safety(
    recipe_api: TestClient,
    recipe_transport: RecipeChatHarness,
    recipe_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    resolved = tuple(
        ResolvedPreference(
            candidate=candidate(key),
            field="likes",
            value=value,
            evidence=(evidence(), evidence()),
        )
        for key, value in (("rice", "rice"), ("blocked", "peanut"))
    )
    profile = document(harness[0])
    profile["learnedPreferences"] = [
        item.candidate.model_dump(mode="json", by_alias=True) for item in resolved
    ]
    before = deepcopy(profile)
    reader = EvidenceReader(resolved)
    with TestClient(
        create_app(recipe_settings, learned_preference_reader=reader), headers=auth_headers
    ) as client:
        response = send(client, create(client))
    assert response.status_code == 200, response.text
    assert reader.calls == 1
    context = recipe_transport.contexts[-1]
    assert [item.insight_key for item in context.learned_preferences] == ["rice"]
    assert "peanut" in context.explicit_preferences.allergies
    assert profile == before


@pytest.mark.parametrize("disabled", ["agents_enabled", "usda_enabled"])
def test_release_flags_keep_unconfigured_chat_unavailable_without_admission(
    disabled: str,
    recipe_api: TestClient,
    recipe_transport: RecipeChatHarness,
    recipe_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    settings = recipe_settings.model_copy(update={disabled: False})
    with TestClient(create_app(settings), headers=auth_headers) as client:
        response = send(client, create(client))
    assert response.status_code == 503
    assert not recipe_transport.requests and not documents(harness[0], "turn")


@pytest.mark.parametrize(
    "missing",
    [
        "foundry_orchestrator_agent_version",
        "app_workload_principal_id",
        "foundry_iq_connection_name",
    ],
)
def test_enabled_recipe_chat_rejects_incomplete_runtime_configuration(
    missing: str, recipe_settings: Settings
) -> None:
    with pytest.raises(ConfigurationError):
        create_app(recipe_settings.model_copy(update={missing: None}))
