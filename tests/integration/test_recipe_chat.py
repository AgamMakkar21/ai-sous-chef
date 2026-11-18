"""Authenticated chat through real services/SDKs and synthetic Foundry/Cosmos/MCP transports."""

import asyncio
import json
import sys
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx2
import pytest
import test_web_grounding as web_fixtures
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sous_chef.adapters import foundry_gateway
from sous_chef.app import create_app
from sous_chef.domain.context import RequestContext, ResolvedPreference
from sous_chef.domain.orchestrator import POLICY
from sous_chef.domain.recipe_safety import UNABLE
from sous_chef.services import recipe_specialist
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.services.recipe_chat import RecipeChatExecutor
from sous_chef.services.recipe_grounding import retrieval_query
from sous_chef.settings import ConfigurationError, Settings
from test_chat_api import documents, send
from test_context import EvidenceReader, candidate, evidence
from test_conversation_api import BASE, OWNER, create
from test_conversation_api import conversation_settings as conversation_settings
from test_cosmos_repository import SyntheticCredential, Transport
from test_cosmos_repository import harness as harness
from test_foundry_gateway import Harness, response_body
from test_grocery_api import add
from test_grounded_specialist import configuration, native_mcp_result
from test_orchestrator_gateway import body, function_output
from test_profile_api import document, onboard, preferences
from test_recipe_specialist import FIXTURE as RECIPE_FIXTURE
from test_usda_nutrition import SERVER
from test_web_grounding import CombinedHarness, WebHarness, prompt_context

from scripts import deploy_dev as delivery


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
            self.data = prompt_context(payload["input"])
            self.contexts.append(self.data)
        if kind == "mcp":
            self.recipe["output"][0]["arguments"] = json.dumps(
                {"query_variants": [retrieval_query(self.data, self.arguments)]}
            )
            if self.reject_recipe:
                draft = json.loads(self.recipe["output"][1]["content"][0]["text"])
                draft["ingredients"][0]["name"] = "peanut oil"
                self.recipe["output"][1]["content"][0]["text"] = json.dumps(draft)
        if kind == "mcp" and self.change_profile:
            self.change_profile["explicitPreferences"]["allergies"].append("rice")
        self.status = self.response_status if kind == self.fail_at else 200
        self.fault = self.response_fault if kind == self.fail_at else ""
        return await super().handle(request)


class RecipeRecoveryHarness(CombinedHarness):
    ai_version = "99"

    def __init__(self, failure: str = "empty") -> None:
        super().__init__(use_web=False)
        self.failure = failure
        self.ai_failure = ""
        self.ai_calls = 0
        self.correct_allergy = False
        self.ai_recipe = deepcopy(RECIPE_FIXTURE["draft"])
        self.ai_recipe.update(
            title="Yogurt bowl",
            totalMinutes=5,
            sourceIds=[],
            sourceClaims=[],
            substitutions=[],
            adaptations=[],
            fitReasons=["The requested no-cook bowl fits a beginner's time budget."],
            ingredients=[
                {
                    "lineId": "yogurt",
                    "name": "plain Greek yogurt, 2% fat",
                    "quantity": 200,
                    "unit": "g",
                },
                {"lineId": "berries", "name": "berries", "quantity": 80, "unit": "g"},
                {"lineId": "oats", "name": "oats", "quantity": 30, "unit": "g"},
            ],
            instructions=[
                {
                    "step": 1,
                    "text": "Add the yogurt, berries and oats to a bowl and mix.",
                    "ingredientLineIds": ["yogurt", "berries", "oats"],
                }
            ],
        )

    @staticmethod
    def request_kind(payload: dict[str, Any]) -> str:
        if payload["agent_reference"]["version"] == RecipeRecoveryHarness.ai_version:
            return "ai_recipe"
        return CombinedHarness.request_kind(payload)

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        kind = self.request_kind(payload)
        self.status, self.fault = 200, ""
        self.hang = False
        if kind == "ai_recipe":
            self.ai_calls += 1
            draft = deepcopy(self.ai_recipe)
            if self.correct_allergy and self.ai_calls == 1:
                draft["ingredients"][0]["name"] = "peanut butter"
            if self.ai_failure == "citation":
                draft["sourceIds"] = ["invented-source"]
            message = response_body()["output"][0]
            message["content"][0]["text"] = json.dumps(draft)
            self.body = body(message)
            if self.ai_failure == "tool":
                self.body["output"].insert(0, deepcopy(self.combined["output"][0]))
            if self.ai_failure == "timeout":
                self.fault = "timeout"
            return await Harness.handle(self, request)
        if kind == "function" and isinstance(payload["input"], list) and self.correct_allergy:
            feedback = json.loads(payload["input"][-1]["output"])
            assert feedback["status"] == "safety_rejected"
            self.body = body(
                function_output(arguments=self.arguments.model_dump_json())
                | {"call_id": "allergy-correction"}
            )
            return await Harness.handle(self, request)
        if kind == "combined":
            if self.failure == "hang":
                self.hang = True
                return await Harness.handle(self, request)
            if self.failure in {"unavailable", "timeout"}:
                self.status = 503 if self.failure == "unavailable" else 200
                self.fault = "timeout" if self.failure == "timeout" else ""
                self.body = {"error": {"message": "Synthetic recipe source unavailable"}}
                return await Harness.handle(self, request)
            if self.failure == "empty":
                self.combined["output"][0]["output"] = "\n"
            elif self.failure == "invalid":
                content = self.combined["output"][-1]["content"][0]
                draft = json.loads(content["text"])
                draft["totalMinutes"] = -1
                content["text"] = json.dumps(draft)
        return await super().handle(request)


@pytest.mark.parametrize("failure", ["empty", "unavailable", "timeout", "invalid", "hang"])
def test_mcp_failure_saves_a_labelled_ai_recipe_with_complete_nutrition_without_replay(
    failure: str,
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecipeRecoveryHarness(failure)
    if failure == "hang":
        monkeypatch.setattr(
            recipe_specialist,
            "POLICY",
            replace(POLICY, recipe_grounding_deadline_seconds=0.05),
        )
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    settings = combined_settings.model_copy(
        update={"foundry_recipe_nutrition_ai_recipe_agent_version": transport.ai_version}
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation, content="Make a yogurt bowl with 2% Greek yogurt.")
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        recipe = turn["recipe"]
        assert turn["status"] == "completed" and turn["generatedRecipeId"]
        assert recipe["generationSource"] == "ai_knowledge"
        assert recipe["sourceIds"] == recipe["sourceClaims"] == recipe["sources"] == []
        assert recipe["webGrounding"] is None
        assert recipe["nutrition"]["status"] == "known"
        assert len(recipe["nutrition"]["nutrients"]) == 6
        assert not recipe["nutrition"]["unknownComponents"]
        assert {item["source"] for item in recipe["nutrition"]["ingredientCalories"]} == {
            "ai_knowledge"
        }
        assert "AI-knowledge recipe" in turn["messages"][1]["content"]
        assert transport.ai_calls == 1
        assert transport.cancelled == (failure == "hang")
        count = len(transport.requests)
        assert (
            send(client, conversation, content="Make a yogurt bowl with 2% Greek yogurt.").json()
            == response.json()
        )
        assert len(transport.requests) == count
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]
    ai_request = next(
        json.loads(request.content)
        for request in transport.requests
        if transport.request_kind(json.loads(request.content)) == "ai_recipe"
    )
    assert not set(ai_request) & {"tools", "tool_choice", "text", "instructions"}


@pytest.mark.parametrize("source", ["curated_recipe", "ai_knowledge"])
@pytest.mark.parametrize(
    ("minutes", "skill", "note_count"),
    [(5, "beginner", 0), (31, "beginner", 1), (5, "advanced", 1), (31, "advanced", 2)],
)
def test_fit_mismatches_deliver_and_save_complete_recipes_with_visible_notes(
    source: str,
    minutes: int,
    skill: str,
    note_count: int,
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecipeRecoveryHarness("empty" if source == "ai_knowledge" else "none")
    transport.ai_recipe.update(totalMinutes=minutes, skill=skill)
    content = transport.combined["output"][-1]["content"][0]
    draft = json.loads(content["text"])
    draft.update(totalMinutes=minutes, skill=skill)
    content["text"] = json.dumps(draft)
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    settings = combined_settings.model_copy(
        update={"foundry_recipe_nutrition_ai_recipe_agent_version": transport.ai_version}
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation, content="Make dinner in 30 minutes.")
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        recipe = turn["recipe"]
        assert turn["status"] == "completed" and turn["generatedRecipeId"]
        assert recipe["generationSource"] == source
        assert recipe["totalMinutes"] == minutes and recipe["skill"] == skill
        assert len(recipe["fitNotes"]) == note_count
        assert recipe["ingredients"] and recipe["instructions"] and recipe["nutrition"]["nutrients"]
        assert all(note in turn["messages"][1]["content"] for note in recipe["fitNotes"])
        assert UNABLE not in turn["messages"][1]["content"]
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"][0]["recipe"] == recipe
        saved = client.post(
            "/api/v1/recipes",
            json={"generatedRecipeId": turn["generatedRecipeId"], "cooked": False},
            headers={"Idempotency-Key": "save-with-fit-notes"},
        )
        assert saved.status_code == 201, saved.text
        assert saved.json()["data"]["recipe"] == recipe
        history_id = saved.json()["data"]["historyId"]
        assert client.get(f"/api/v1/recipes/{history_id}").json()["data"]["recipe"] == recipe
        calls = len(transport.requests)
        assert (
            send(client, conversation, content="Make dinner in 30 minutes.").json()
            == response.json()
        )
        assert len(transport.requests) == calls
    kinds = [transport.request_kind(json.loads(request.content)) for request in transport.requests]
    assert kinds.count("function") == kinds.count("combined") == 1
    assert transport.ai_calls == (1 if source == "ai_knowledge" else 0)


def test_overnight_oats_do_not_require_matching_global_tastes_or_pantry(
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecipeRecoveryHarness()
    transport.arguments = transport.arguments.model_copy(
        update={"requirements": "Make overnight oats for breakfast."}
    )
    transport.ai_recipe.update(
        title="Overnight oats",
        totalMinutes=485,
        fitReasons=["A make-ahead oat breakfast."],
        instructions=[
            {
                "step": 1,
                "text": "Mix the yogurt, berries and oats, then refrigerate overnight.",
                "ingredientLineIds": ["yogurt", "berries", "oats"],
            }
        ],
    )
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    settings = combined_settings.model_copy(
        update={"foundry_recipe_nutrition_ai_recipe_agent_version": transport.ai_version}
    )
    explicit = preferences() | {"spicePreference": "hot", "likes": ["chili", "lime"]}
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client, explicit)
        add(client, name="rice", quantity=100, unit="g")
        before = deepcopy(document(harness[0]))
        conversation = create(client)
        response = send(client, conversation, content="Make overnight oats for breakfast.")
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        recipe = turn["recipe"]
        assert turn["status"] == "completed" and turn["generatedRecipeId"]
        assert recipe["title"] == "Overnight oats" and recipe["totalMinutes"] == 485
        assert len(recipe["fitNotes"]) == 1
        assert [line["name"] for line in recipe["ingredients"]] == [
            line["name"] for line in transport.ai_recipe["ingredients"]
        ]
        assert all(line["inventoryStatus"] == "missing" for line in recipe["ingredients"])
        assert {item["ingredientLineId"] for item in recipe["shoppingItems"]} == {
            "yogurt",
            "berries",
            "oats",
        }
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"][0]["recipe"] == recipe
        after = document(harness[0])
        assert after["explicitPreferences"] == before["explicitPreferences"] == explicit
        assert after["groceries"] == before["groceries"]
    assert transport.ai_calls == 1


@pytest.mark.parametrize("current_information", [False, True])
def test_ai_recipe_opt_in_preserves_mcp_priority_and_required_current_information(
    current_information: bool,
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecipeRecoveryHarness("unavailable" if current_information else "none")
    if current_information:
        transport.arguments = transport.arguments.model_copy(
            update={"use_web": True, "web_query": "rice"}
        )
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    settings = combined_settings.model_copy(
        update={"foundry_recipe_nutrition_ai_recipe_agent_version": transport.ai_version}
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation, content="Make a rice dinner.")
        if current_information:
            assert response.status_code == 504, response.text
            assert response.json()["code"] == "execution_outcome_unknown"
            turn = client.get(f"{BASE}/{conversation}/turns").json()["items"][0]
            assert turn["status"] == "outcomeUnknown" and turn["recipe"] is None
        else:
            assert response.status_code == 200, response.text
            assert response.json()["data"]["recipe"]["generationSource"] == "curated_recipe"
        assert transport.ai_calls == 0


@pytest.mark.parametrize("failure", ["citation", "tool", "timeout"])
def test_invalid_or_uncertain_ai_recipe_is_not_saved_or_retried(
    failure: str,
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecipeRecoveryHarness()
    transport.ai_failure = failure
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    settings = combined_settings.model_copy(
        update={"foundry_recipe_nutrition_ai_recipe_agent_version": transport.ai_version}
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation)
        assert response.status_code == (504 if failure == "timeout" else 502)
        saved = client.get(f"{BASE}/{conversation}/turns").json()["items"][0]
        assert saved["recipe"] is None and len(saved["messages"]) == 1
        assert transport.ai_calls == 1
        send(client, conversation)
        assert transport.ai_calls == 1


def test_ai_recipe_recovery_preserves_allergy_correction_and_the_complete_long_recipe_card(
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecipeRecoveryHarness()
    transport.correct_allergy = True
    transport.ai_recipe.update(totalMinutes=31, skill="advanced")
    transport.ai_recipe["instructions"] = [
        {
            "step": index + 1,
            "text": ("Mix the yogurt, berries and oats gently in the bowl. " * 9).strip(),
            "ingredientLineIds": ["yogurt", "berries", "oats"],
        }
        for index in range(9)
    ]
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    settings = combined_settings.model_copy(
        update={"foundry_recipe_nutrition_ai_recipe_agent_version": transport.ai_version}
    )
    app = create_app(settings)
    with TestClient(app, headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        with patch.object(
            app.state.nutrition, "lookup", wraps=app.state.nutrition.lookup
        ) as lookup:
            response = send(client, conversation, content="Make a yogurt bowl.")
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        assert transport.ai_calls == 2 and lookup.call_count == 1
        assert turn["recipe"]["nutrition"]["status"] == "known"
        assert turn["recipe"]["instructions"] == transport.ai_recipe["instructions"]
        assert len(turn["recipe"]["fitNotes"]) == 2
        assert all(note in turn["messages"][1]["content"] for note in turn["recipe"]["fitNotes"])
        assert "peanut butter" not in response.text
        assert len(turn["messages"][1]["content"]) <= 4000
        assert "AI-knowledge recipe" in turn["messages"][1]["content"]
        assert "recipe card" in turn["messages"][1]["content"]
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]


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


@pytest.mark.parametrize("mode", ["transient_search", "transient_profile"])
def test_chat_recovers_from_transient_usda_failure_without_replaying_agents(
    mode: str,
    recipe_settings: Settings,
    recipe_transport: RecipeChatHarness,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    settings = recipe_settings.model_copy(
        update={
            "usda_mcp_server": Path(__file__).parents[1] / "fixtures" / "usda_api_server.py",
            "usda_mcp_env": {"TEST_USDA_MODE": SecretStr(mode)},
        }
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation)
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        assert turn["status"] == "completed"
        assert {
            mapping["ingredientLineId"] for mapping in turn["recipe"]["nutrition"]["mappings"]
        } >= {"rice", "carrot"}
        calls = len(recipe_transport.requests)
        assert calls == 4  # Routing, generation, USDA selection and oil recovery.
        assert send(client, conversation).json() == response.json()
        assert len(recipe_transport.requests) == calls
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]


@pytest.mark.parametrize("mode", ["invalid_search", "invalid_profile"])
def test_chat_recovers_rejected_usda_data_before_saving_complete_answer(
    mode: str,
    recipe_settings: Settings,
    recipe_transport: RecipeChatHarness,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
) -> None:
    settings = recipe_settings.model_copy(
        update={
            "usda_mcp_server": Path(__file__).parents[1] / "fixtures" / "usda_api_server.py",
            "usda_mcp_env": {"TEST_USDA_MODE": SecretStr(mode)},
        }
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation)
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        assert turn["status"] == "completed"
        recipe = turn["recipe"]
        nutrition = recipe["nutrition"]
        assert nutrition["status"] == "known" and not nutrition["unknownComponents"]
        assert {row["name"] for row in nutrition["nutrients"]} == {
            "energy",
            "protein",
            "carbohydrate",
            "fat",
            "fiber",
            "sodium",
        }
        assert {row["ingredientLineId"] for row in nutrition["ingredientCalories"]} == {
            row["lineId"] for row in recipe["ingredients"]
        }
        assert all(row["ingredientLineId"] != "rice" for row in nutrition["mappings"])
        calls = len(recipe_transport.requests)
        assert send(client, conversation).json() == response.json()
        assert len(recipe_transport.requests) == calls
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]


def test_chat_saves_no_cook_yogurt_bowl_when_optional_quantity_search_is_skipped(
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = CombinedHarness(use_web=False)
    transport.arguments = transport.arguments.model_copy(
        update={
            "requirements": "A simple no-cook yogurt bowl.",
            "quantity_ingredients": ("yogurt",),
        }
    )
    content = transport.combined["output"][-1]["content"][0]
    draft = json.loads(content["text"])
    draft.update(
        title="Yogurt bowl",
        totalMinutes=5,
        ingredients=[
            {"lineId": "yogurt", "name": "plain Greek yogurt", "quantity": 200, "unit": "g"},
            {"lineId": "berries", "name": "blueberries", "quantity": 80, "unit": "g"},
        ],
        instructions=[
            {
                "step": 1,
                "text": "Spoon the yogurt into a bowl and top with blueberries.",
                "ingredientLineIds": ["yogurt", "berries"],
            }
        ],
        substitutions=[],
        fitReasons=["A quick no-cook bowl."],
        adaptations=[],
        sourceClaims=[],
    )
    content["text"] = json.dumps(draft)
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    with TestClient(create_app(combined_settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation, content="yogurt bowl")
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        assert turn["status"] == "completed" and turn["recipe"]["title"] == "Yogurt bowl"
        recipe = turn["recipe"]
        assert recipe["webGrounding"] is None
        assert recipe["nutrition"]["status"] == "known"
        assert not recipe["nutrition"]["unknownComponents"]
        assert {item["ingredientLineId"] for item in recipe["nutrition"]["ingredientCalories"]} == {
            "yogurt",
            "berries",
        }
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]
        calls = len(transport.requests)
        assert send(client, conversation, content="yogurt bowl").json() == response.json()
        assert len(transport.requests) == calls


@pytest.mark.parametrize("fails", [False, True])
def test_release_chat_check_requires_live_recipe_and_attributed_calories(
    fails: bool,
    recipe_settings: Settings,
    recipe_transport: RecipeChatHarness,
) -> None:
    deployed = {
        f"ASC_{name.upper()}": str(value)
        for name, value in recipe_settings.model_dump().items()
        if value is not None
    } | {"ASC_USDA_API_KEY": "synthetic-key", "ASC_ENVIRONMENT": "dev"}
    nutrition = UsdaNutritionTool.from_settings(recipe_settings)
    if fails:
        recipe_transport.fail_at = "mcp"
        recipe_transport.response_status = 503
    result = json.loads(recipe_transport.recipe["output"][0]["output"])
    result[0]["untrusted_metadata"] = "a " * 5000
    recipe_transport.recipe["output"][0]["output"] = json.dumps(result)
    with (
        patch.object(UsdaNutritionTool, "from_settings", return_value=nutrition),
        patch.object(delivery, "_release_project_client", wraps=recipe_transport.factory) as client,
    ):
        if fails:
            with pytest.raises(delivery.DeliveryError, match="Live chat dependency"):
                asyncio.run(delivery.chat_smoke(deployed))
        else:
            asyncio.run(delivery.chat_smoke(deployed))
            assert [context.current_request for context in recipe_transport.contexts] == [
                "Give me a recipe for a yogurt bowl.",
                "Give me a recipe for a chicken dinner.",
                "Make rice and beans using 1 cup dry rice and 1 can black beans.",
            ]
            recovery = json.loads(recipe_transport.requests[-1].content)
            assert recovery["agent_reference"]["version"] == (
                recipe_settings.foundry_recipe_nutrition_calorie_agent_version
            )
            assert json.loads(recovery["input"])["useWeb"] is False
        assert client.call_args.args[0].environment == "dev"


@pytest.mark.parametrize("invalid_ai", [False, True])
def test_release_smoke_exercises_mcp_first_and_the_real_tool_free_recipe_recovery_route(
    invalid_ai: bool,
    combined_settings: Settings,
) -> None:
    transport = RecipeRecoveryHarness("none")
    if invalid_ai:
        transport.ai_failure = "citation"
    settings = combined_settings.model_copy(
        update={"foundry_recipe_nutrition_ai_recipe_agent_version": transport.ai_version}
    )
    deployed = {
        f"ASC_{name.upper()}": str(value)
        for name, value in settings.model_dump().items()
        if value is not None
    } | {"ASC_USDA_API_KEY": "synthetic-key", "ASC_ENVIRONMENT": "dev"}
    nutrition = UsdaNutritionTool.from_settings(settings)
    with (
        patch.object(UsdaNutritionTool, "from_settings", return_value=nutrition),
        patch.object(delivery, "_release_project_client", transport.factory),
    ):
        if invalid_ai:
            with pytest.raises(delivery.DeliveryError, match="Live chat dependency"):
                asyncio.run(delivery.chat_smoke(deployed))
        else:
            asyncio.run(delivery.chat_smoke(deployed))
    kinds = [transport.request_kind(json.loads(request.content)) for request in transport.requests]
    assert kinds.count("combined") == 3
    assert kinds.count("ai_recipe") == transport.ai_calls == 1


@pytest.mark.parametrize(
    ("omission", "ingredient_count"),
    [("ingredient", 3), ("nutrient", 3), ("nutrient", 30), ("persistent", 3)],
)
def test_chat_repairs_only_residual_ai_nutrients_before_durable_completion(
    omission: str,
    ingredient_count: int,
    recipe_settings: Settings,
    recipe_transport: RecipeChatHarness,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = recipe_settings.model_copy(
        update={"usda_mcp_env": {"TEST_USDA_MODE": SecretStr("error")}}
    )
    if ingredient_count == 30:
        content = recipe_transport.recipe["output"][1]["content"][0]
        draft = json.loads(content["text"])
        draft["ingredients"] = [
            {"lineId": f"food{i}", "name": f"ingredient {i}", "quantity": 10, "unit": "g"}
            for i in range(ingredient_count)
        ]
        draft["instructions"] = [
            {
                "step": 1,
                "text": "Cook the declared ingredients.",
                "ingredientLineIds": [row["lineId"] for row in draft["ingredients"]],
            }
        ]
        draft.update(substitutions=[], sourceClaims=[])
        content["text"] = json.dumps(draft)
    original = web_fixtures.nutrient_response
    requests: list[dict[str, Any]] = []

    def respond(payload: dict[str, Any], *, combined: bool = False) -> dict[str, Any]:
        data = json.loads(payload["input"])
        requests.append(data)
        response = original(payload, combined=combined)
        content = response["output"][-1]["content"][0]
        output = json.loads(content["text"])
        assert data["useWeb"] is False
        if len(requests) == 1 and omission != "ingredient":
            for row in output["nutrientEstimates"]:
                row["nutrients"] = [
                    item for item in row["nutrients"] if item["name"] == "carbohydrate"
                ]
        elif len(requests) == 1:
            output["nutrientEstimates"] = output["nutrientEstimates"][:-1]
        elif omission == "persistent":
            output["nutrientEstimates"] = []
        content["text"] = json.dumps(output)
        return response

    monkeypatch.setattr(web_fixtures, "nutrient_response", respond)
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation)
        assert response.status_code == (502 if omission == "persistent" else 200), response.text
        assert [row["useWeb"] for row in requests] == [False, False]
        first_ai = {row["ingredientLineId"]: row for row in requests[0]["ingredients"]}
        residual = {row["ingredientLineId"]: row for row in requests[1]["ingredients"]}
        assert all(len(row["missingNutrients"]) == 6 for row in first_ai.values())
        if omission == "ingredient":
            assert list(residual) == [list(first_ai)[-1]]
            assert residual == {list(first_ai)[-1]: list(first_ai.values())[-1]}
        else:
            assert set(residual) == set(first_ai)
            assert all(
                set(row["missingNutrients"]) == {"energy", "protein", "fat", "fiber", "sodium"}
                for row in residual.values()
            )
        turns = client.get(f"{BASE}/{conversation}/turns").json()["items"]
        if omission == "persistent":
            assert turns[0]["status"] == "failed" and turns[0]["recipe"] is None
        else:
            turn = response.json()["data"]
            nutrition = turn["recipe"]["nutrition"]
            assert nutrition["status"] == "known" and not nutrition["unknownComponents"]
            assert all(
                len(row["coveredIngredientLineIds"]) == ingredient_count
                and not row["unknownIngredientLineIds"]
                for row in nutrition["coverage"]
            )
            assert len(nutrition["ingredientCalories"]) == ingredient_count
            assert len(nutrition["nutrientEstimates"]) == (
                ingredient_count if omission == "ingredient" else 2 * ingredient_count
            )
            assert turns == [turn]
        calls = len(recipe_transport.requests)
        replay = send(client, conversation)
        if omission == "persistent":
            assert replay.status_code == 502 and replay.json()["code"] == "execution_failed"
        else:
            assert replay.json() == response.json()
        assert len(recipe_transport.requests) == calls


def test_chat_saves_ai_nutrition_without_search_and_replays_without_new_calls(
    recipe_settings: Settings,
    recipe_transport: RecipeChatHarness,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = recipe_settings.model_copy(
        update={"usda_mcp_env": {"TEST_USDA_MODE": SecretStr("error")}}
    )
    original = web_fixtures.nutrient_response
    requests = []

    def respond(payload: dict[str, Any], *, combined: bool = False) -> dict[str, Any]:
        data = json.loads(payload["input"])
        requests.append(data)
        assert data["useWeb"] is False
        return original(payload, combined=combined)

    monkeypatch.setattr(web_fixtures, "nutrient_response", respond)
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation)
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        nutrition = turn["recipe"]["nutrition"]
        assert nutrition["status"] == "known" and not nutrition["unknownComponents"]
        for row in (*nutrition["ingredientCalories"], *nutrition["nutrientEstimates"]):
            assert row["source"] == "ai_knowledge" and row["reference"] is None
        assert all(row["amount"] > 0 for row in nutrition["ingredientCalories"])
        assert [row["useWeb"] for row in requests] == [False]
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]
        calls = len(recipe_transport.requests)
        assert send(client, conversation).json() == response.json()
        assert len(recipe_transport.requests) == calls


@pytest.mark.parametrize(
    "mode", ["missing_nutrient", "fail_carrot", "error", "invalid_later_profile"]
)
def test_release_chat_check_accepts_complete_ai_fallback_nutrition(
    mode: str,
    recipe_settings: Settings,
    recipe_transport: RecipeChatHarness,
) -> None:
    settings = recipe_settings.model_copy(
        update={"usda_mcp_env": {"TEST_USDA_MODE": SecretStr(mode)}}
    )
    nutrition = UsdaNutritionTool.from_settings(settings)
    deployed = {
        f"ASC_{name.upper()}": str(value)
        for name, value in settings.model_dump().items()
        if value is not None
    } | {"ASC_USDA_API_KEY": "synthetic-key", "ASC_ENVIRONMENT": "dev"}
    with (
        patch.object(UsdaNutritionTool, "from_settings", return_value=nutrition),
        patch.object(delivery, "_release_project_client", recipe_transport.factory),
    ):
        asyncio.run(delivery.chat_smoke(deployed))
    calorie_calls = [
        json.loads(payload["input"])
        for request in recipe_transport.requests
        if isinstance((payload := json.loads(request.content))["input"], str)
        and payload["input"].startswith(
            ('{"purpose":"calorie_fallback"', '{"purpose":"nutrition_fallback"')
        )
    ]
    assert all(call["useWeb"] is False for call in calorie_calls)
    if mode in {"fail_carrot", "error", "invalid_later_profile"}:
        if mode in {"fail_carrot", "invalid_later_profile"}:
            assert {item["ingredientLineId"] for item in calorie_calls[0]["ingredients"]} == {
                "carrot",
                "oil",
            }


@pytest.mark.parametrize("mode", ["error", "throttled", "hang_rice", "fail_carrot"])
@pytest.mark.parametrize("combined", [False, True])
def test_chat_persists_complete_ai_nutrition_during_usda_outage(
    mode: str,
    combined: bool,
    recipe_settings: Settings,
    recipe_transport: WebHarness,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if combined:
        recipe_transport = CombinedHarness(use_web=False)
        monkeypatch.setattr(foundry_gateway, "_project_client", recipe_transport.factory)
        recipe_settings = recipe_settings.model_copy(
            update={
                "foundry_recipe_nutrition_mode": "combined",
                "foundry_recipe_nutrition_agent_version": "4",
                "foundry_recipe_nutrition_retrieval_agent_version": None,
                "foundry_recipe_nutrition_web_agent_version": None,
            }
        )
    settings = recipe_settings.model_copy(
        update={
            "usda_mcp_env": {"TEST_USDA_MODE": SecretStr(mode)},
            "usda_mcp_timeout_seconds": 0.3,
        }
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation)
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        nutrition = turn["recipe"]["nutrition"]
        calories = nutrition["ingredientCalories"]
        assert {item["ingredientLineId"] for item in calories} == {"rice", "carrot", "oil"}
        assert {item["source"] for item in calories} == (
            {"usda_mcp", "ai_knowledge"} if mode == "fail_carrot" else {"ai_knowledge"}
        )
        assert all(
            (item["reference"] is None) == (item["source"] == "ai_knowledge") for item in calories
        )
        assert {item["ingredientLineId"] for item in nutrition["mappings"]} == (
            {"rice"} if mode == "fail_carrot" else set()
        )
        energy = next(item for item in nutrition["nutrients"] if item["name"] == "energy")
        assert energy["amount"] == (200 if mode == "fail_carrot" else 150)
        assert nutrition["status"] == "known" and not nutrition["unknownComponents"]
        assert len(nutrition["nutrients"]) == 6
        assert {item["ingredientLineId"] for item in nutrition["nutrientEstimates"]} == (
            {"carrot", "oil"} if mode == "fail_carrot" else {"rice", "carrot", "oil"}
        )
        assert "AI estimate" in turn["messages"][-1]["content"]
        assert "not USDA-verified" not in turn["messages"][-1]["content"]
        assert len(recipe_transport.requests) == 3 + int(mode == "fail_carrot")
        if combined:
            assert {
                json.loads(request.content)["agent_reference"]["version"]
                for request in recipe_transport.requests
                if json.loads(request.content)["agent_reference"]["name"] == "recipe-agent"
            } == {"4", "6"}
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]
        assert send(client, conversation).json() == response.json()
        assert len(recipe_transport.requests) == 3 + int(mode == "fail_carrot")


@pytest.mark.parametrize(
    "fault",
    [
        "invalid_json",
        "incomplete",
        "invented_source",
        "above_old_output_limit",
        "partial",
        "empty",
        "high_usage",
    ],
)
def test_incomplete_or_falsely_cited_ai_nutrition_cannot_complete_chat(
    fault: str,
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="sous_chef.operational")
    original = web_fixtures.nutrient_response

    def calorie_result(payload: dict[str, Any], *, combined: bool = False) -> dict[str, Any]:
        response = original(payload, combined=combined)
        content = response["output"][-1]["content"][0]
        output = json.loads(content["text"])
        if fault == "invalid_json":
            content["text"] = "private invalid calorie content"
            return response
        if fault == "incomplete":
            response["status"] = "incomplete"
        elif fault == "invented_source":
            output["nutrientEstimates"][0]["reference"] = "https://invented.example.invalid"
        elif fault == "above_old_output_limit":
            response["usage"]["input_tokens"] = 160001
            response["usage"]["output_tokens"] = 20000
        elif fault in {"partial", "empty"}:
            output["nutrientEstimates"] = output["nutrientEstimates"][
                : 1 if fault == "partial" else 0
            ]
        elif fault == "high_usage":
            response["usage"]["output_tokens"] = 6500
        content["text"] = json.dumps(output)
        return response

    monkeypatch.setattr(web_fixtures, "nutrient_response", calorie_result)
    transport = CombinedHarness(use_web=False)
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    settings = combined_settings.model_copy(
        update={"usda_mcp_env": {"TEST_USDA_MODE": SecretStr("fail_carrot")}}
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation)
        if fault in {"high_usage", "above_old_output_limit", "partial"}:
            assert response.status_code == 200, response.text
            turn = response.json()["data"]
            assert turn["status"] == "completed"
            assert turn["recipe"]["nutrition"]["status"] == "known"
        else:
            assert response.status_code == 502, response.text
            turn = client.get(f"{BASE}/{conversation}/turns").json()["items"][0]
            assert turn["status"] == "failed" and turn.get("recipe") is None
        if fault not in {"high_usage", "above_old_output_limit"}:
            assert "calorie_fallback_unavailable" in caplog.text
        assert "private invalid calorie content" not in caplog.text
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]
        assert send(client, conversation).status_code == response.status_code
        assert len(transport.requests) == (
            5 if fault in {"invalid_json", "invented_source", "partial", "empty"} else 4
        )
        for request in transport.requests[3:]:
            fallback = json.loads(request.content)
            assert "max_output_tokens" not in fallback
            assert json.loads(fallback["input"])["useWeb"] is False


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
        if poultry_request == "Chicken dinner":
            draft["sourceIds"] = ["0"]
            draft["sourceClaims"] = [{"sourceId": "0", "quote": "Unverified generated quotation"}]
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
            transport.data = prompt_context(payload["input"])
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
            if poultry_request == "Chicken dinner":
                assert turn["recipe"]["sourceIds"] == ["hf-food-recipes-1"]
                assert turn["recipe"]["sourceClaims"] == []
                assert "Unverified generated quotation" not in response.text
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


def test_combined_chat_keeps_known_components_when_broad_search_details_are_missing(
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = CombinedHarness(use_web=False)
    original_handle = transport.handle

    async def handle(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        kind = transport.request_kind(payload)
        if kind == "function" and isinstance(payload["input"], str):
            transport.data = prompt_context(payload["input"])
        elif kind == "combined":
            transport.combined["output"][0]["arguments"] = json.dumps(
                {"query_variants": [retrieval_query(transport.data, transport.arguments)]}
            )
        return await original_handle(request)

    monkeypatch.setattr(transport, "handle", handle)
    monkeypatch.setattr(foundry_gateway, "_project_client", transport.factory)
    settings = Settings.model_validate(
        combined_settings.model_dump()
        | {
            "usda_mcp_server": SERVER.with_name("usda_api_server.py"),
            "usda_mcp_env": {"TEST_USDA_MODE": SecretStr("broad_search")},
        }
    )
    with TestClient(create_app(settings), headers=auth_headers) as client:
        onboard(client)
        conversation = create(client)
        response = send(client, conversation, content="Rice dinner")
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        assert turn["status"] == "completed" and turn["generatedRecipeId"]
        nutrition = turn["recipe"]["nutrition"]
        assert nutrition["status"] == "known"
        assert nutrition["nutrients"]
        assert {item["ingredientLineId"] for item in nutrition["mappings"]} == {"carrot"}
        assert not nutrition["unknownComponents"]
        assert {item["ingredientLineId"] for item in nutrition["nutrientEstimates"]} == {
            "rice",
            "oil",
        }
        assert {source["kind"] for source in turn["recipe"]["sources"]} == {
            "curated_recipe",
            "usda",
        }
        assert len(transport.requests) == 4
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]


@pytest.mark.parametrize("quantity_hints", [False, True])
@pytest.mark.parametrize("allergen_location", ["alias", "ingredient"])
def test_combined_revision_preserves_context_and_enriches_only_its_current_ingredients(
    allergen_location: str,
    quantity_hints: bool,
    combined_settings: Settings,
    auth_headers: dict[str, str],
    harness: tuple[Transport, SyntheticCredential],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = CombinedHarness(use_web=False)
    if quantity_hints:
        transport.arguments = transport.arguments.model_copy(
            update={"quantity_ingredients": ("rice",)}
        )
    original_handle = transport.handle
    draft = json.loads(transport.combined["output"][-1]["content"][0]["text"])
    attempts = 0
    correction = "Remove the conflicting garnish and retain the original measured ingredients."

    async def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        payload = json.loads(request.content)
        kind = transport.request_kind(payload)
        if kind == "function" and isinstance(payload["input"], str):
            transport.data = prompt_context(payload["input"])
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
        elif kind == "combined":
            attempts += 1
            current = deepcopy(draft)
            if attempts == 1:
                current["title"] = "Private rejected peanut skillet"
                current["ingredients"][0]["name"] = (
                    "peanut oil" if allergen_location == "ingredient" else "groundnut oil"
                )
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
            content = (
                "Make a 30-minute pantry recipe with two handfuls of rice."
                if quantity_hints
                else "Make a 30-minute pantry recipe."
            )
            response = send(client, conversation, content=content)
        assert response.status_code == 200, response.text
        turn = response.json()["data"]
        assert turn["recipe"]["ingredients"][0]["quantity"] == 200
        energy = next(
            nutrient
            for nutrient in turn["recipe"]["nutrition"]["nutrients"]
            if nutrient["name"] == "energy"
        )
        assert energy["amount"] == 360
        assert [call.args[0].ingredients[0].quantity for call in lookup.call_args_list] == [200]
        assert "Private rejected peanut skillet" not in response.text
        assert client.get(f"{BASE}/{conversation}/turns").json()["items"] == [turn]
        assert send(client, conversation, content=content).json() == response.json()
    assert attempts == 2 and len(transport.requests) == 5
    requests = [json.loads(request.content) for request in transport.requests]
    generations = [request for request in requests if transport.request_kind(request) == "combined"]
    assert [request["max_tool_calls"] for request in generations] == (
        [2, 1] if quantity_hints else [1, 1]
    )
    assert [transport.request_kind(request) for request in requests] == [
        "function",
        "combined",
        "function",
        "combined",
        "usda_selection",
    ]
    assert requests[0]["agent_reference"] == requests[2]["agent_reference"]
    assert requests[1]["agent_reference"] == requests[3]["agent_reference"]
    for request in (requests[1], requests[3]):
        assert request["input"].startswith(transport.data.prompt)
        assert request["store"] is False
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
    draft["ingredients"][0]["name"] = "rice"
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
