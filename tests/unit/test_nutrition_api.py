import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sous_chef.domain.recipes import NUTRITION_NOTICE
from sous_chef.services.nutrition import UsdaNutritionTool
from test_usda_nutrition import config

LOOKUP = "/api/v1/nutrition/lookup"


def set_tool(client: TestClient, mode: str = "", *, timeout: float = 10) -> None:
    assert isinstance(client.app, FastAPI)
    client.app.state.nutrition = UsdaNutritionTool(config(mode, timeout=timeout))


def test_search_and_explicit_selection_return_only_candidate_backed_nutrition(
    client: TestClient,
) -> None:
    set_tool(client, "candidates")
    search = client.post("/api/v1/nutrition/search", json={"name": "chicken"})
    assert search.status_code == 200 and search.headers["cache-control"] == "no-store"
    assert len(search.json()["foods"]) == 2
    assert search.json()["truncated"] is False
    selected = client.post(LOOKUP, json={"name": "chicken", "foodId": "99901"})
    assert selected.status_code == 200, selected.text
    assert selected.json()["data"]["mappings"][0]["usdaFoodId"] == "99901"
    foreign = client.post(LOOKUP, json={"name": "chicken", "foodId": "00001"})
    assert foreign.status_code == 200
    assert foreign.json()["data"]["status"] == "unavailable"
    assert client.post(LOOKUP, json={"name": "chicken", "foodId": "1"}).status_code == 422
    assert (
        client.post(
            "/api/v1/nutrition/search",
            json={"name": "chicken"},
            headers={"Authorization": ""},
        ).status_code
        == 401
    )


def test_authenticated_lookup_returns_real_stdio_calories_per_100_g(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    set_tool(client)
    response = client.post(LOOKUP, json={"name": "rice"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["servings"] == 1 and data["servingBasis"] == "per_serving"
    assert {"name": "energy", "amount": 200, "unit": "kcal"} in data["nutrients"]
    assert data["mappings"] == [
        {
            "ingredientLineId": "ingredient",
            "quantity": 100,
            "unit": "g",
            "sourceId": "usda:sr:99901",
            "usdaFoodId": "99901",
            "weightGrams": 100,
        }
    ]
    assert response.headers["cache-control"] == "no-store"
    assert data["sources"][0]["kind"] == "usda"
    assert data["isEstimate"] is True and data["notice"] == NUTRITION_NOTICE
    assert data["coverage"][0] == {
        "name": "energy",
        "status": "known",
        "wholeRecipeAmount": 200,
        "coveredIngredientLineIds": ["ingredient"],
        "unknownIngredientLineIds": [],
    }
    assert "rice" not in caplog.text


def test_disabled_lookup_does_not_invent_nutrition(client: TestClient) -> None:
    response = client.post(LOOKUP, json={"name": "rice"})
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "configuration_unavailable"
    assert "data" not in response.json()


@pytest.mark.parametrize(
    ("mode", "status", "code"),
    [
        ("error", 503, "nutrition_unavailable"),
        ("malformed", 502, "nutrition_invalid_result"),
        ("timeout", 504, "nutrition_timeout"),
    ],
)
def test_lookup_reports_safe_tool_errors(
    client: TestClient, mode: str, status: int, code: str
) -> None:
    set_tool(client, mode, timeout=1)
    response = client.post(LOOKUP, json={"name": "rice"})
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["traceId"]
    assert response.headers["content-type"] == "application/problem+json"
    assert "private" not in response.text and "data" not in response.json()


def test_unmatched_food_is_unavailable_not_zero(client: TestClient) -> None:
    set_tool(client, "no_food")
    response = client.post(LOOKUP, json={"name": "unknown ingredient"})
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "unavailable"
    assert response.json()["data"]["nutrients"] == []
    assert response.json()["data"]["isEstimate"] is True
    assert response.json()["data"]["notice"] == NUTRITION_NOTICE
    assert all(item["wholeRecipeAmount"] is None for item in response.json()["data"]["coverage"])


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "x" * 101},
        {"name": "%"},
        {"name": "wild_card"},
        {"name": "private\x00name"},
        {"name": 123},
        {"name": "rice", "userId": "someone-else"},
        {"name": "rice", "quantity": 200},
    ],
)
def test_invalid_lookup_inputs_do_not_reach_mcp(
    client: TestClient, body: dict[str, object]
) -> None:
    response = client.post(LOOKUP, json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_input"
    assert "private" not in response.text


def test_lookup_requires_authentication_and_rejects_query_content(client: TestClient) -> None:
    response = client.post(LOOKUP + "?name=private", json={"name": "rice"})
    assert response.status_code == 422
    assert "private" not in response.text
    response = client.post(LOOKUP, json={"name": "rice"}, headers={"Authorization": ""})
    assert response.status_code == 401
    response = client.post(LOOKUP, content="{", headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["code"] == "malformed_json"
    assert "Traceback" not in json.dumps(response.json())
