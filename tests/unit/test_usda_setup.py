import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx
import pytest
from pydantic import SecretStr
from sous_chef.adapters.usda_mcp import UsdaMcpConfig
from sous_chef.domain.nutrition_inputs import (
    ingredient_weight,
    recipe_food_match,
    recipe_food_query,
)
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.usda_mcp_server import FdcError, FoodDataCentral

FOOD = {
    "fdcId": 1234567,
    "description": "Chicken, breast, raw",
    "dataType": "Foundation",
    "foodCategory": {"description": "Poultry Products"},
    "publicationDate": "2024-04-18",
}


def test_recipe_normalization_preserves_meaningful_food_forms_and_composition() -> None:
    from sous_chef.domain.nutrition import UsdaFood

    def candidate(description: str) -> UsdaFood:
        return UsdaFood(
            fdc_id="1234567",
            long_desc=description,
            shrt_desc=description,
            data_type="SR Legacy",
            food_group="Poultry Products",
        )

    assert recipe_food_query("shrimp, raw, peeled and deveined") == "shrimp raw"
    assert recipe_food_query("jasmine rice dry") == "jasmine rice dry"
    assert (
        recipe_food_query("boneless skinless chicken breast, sliced") == "meat only chicken breast"
    )
    assert recipe_food_query("red bell peppers, sliced") == "red bell pepper"
    assert recipe_food_match(candidate("Chicken, breast, meat only, raw"), "chicken raw")
    assert not recipe_food_match(candidate("Chicken, breast, roasted"), "chicken raw")
    assert not recipe_food_match(candidate("Chicken, breast, roasted"), "chicken")
    assert not recipe_food_match(candidate("Yogurt, plain, nonfat"), "yogurt whole milk")
    assert not recipe_food_match(candidate("Soy sauce, reduced sodium"), "soy sauce regular")
    assert not recipe_food_match(candidate("Babyfood, chicken, raw"), "chicken raw")
    assert not recipe_food_match(candidate("Chicken, skin only, raw"), "chicken raw")
    assert not recipe_food_match(candidate("Chicken, ground, raw"), "chicken")
    assert recipe_food_match(candidate("Chicken, ground, raw"), "ground chicken")
    assert recipe_food_query("chicken, boneless pieces") == "chicken"
    assert recipe_food_query("chicken thighs, boneless chunks") == "chicken thigh"
    assert recipe_food_query("chicken broth") == "chicken broth"
    assert recipe_food_query("chicken, cooked pieces") == "chicken cooked"
    assert not recipe_food_match(
        candidate("Chicken spread"), recipe_food_query("chicken, boneless pieces")
    )
    assert recipe_food_query("water") == "water"
    assert not recipe_food_match(candidate("Water convolvulus, raw"), recipe_food_query("water"))
    assert recipe_food_query("tomatoes, chopped") == "tomato"
    assert recipe_food_query("neutral oil") == "neutral oil"
    assert recipe_food_query("regional grain blend, uncooked") == "regional grain blend raw"
    assert not recipe_food_match(
        candidate("Regional grain blend with milk"), "regional grain blend"
    )
    assert not recipe_food_match(
        candidate("Regional grain blend, with milk"), "regional grain blend"
    )
    assert recipe_food_match(candidate("Rice, white, long-grain, regular, raw, enriched"), "rice")


@pytest.mark.parametrize(
    ("name", "query"),
    [
        ("mushrooms, chopped", "mushroom"),
        ("radishes, sliced", "radish"),
        ("blueberries, fresh", "blueberry"),
        ("potatoes, cubed", "potato"),
        ("asparagus, trimmed", "asparagus"),
    ],
)
def test_regular_noun_inflections_do_not_need_a_food_name_dictionary(name: str, query: str) -> None:
    assert recipe_food_query(name) == query


def test_source_portions_retain_amount_and_unit_without_inventing_missing_weights() -> None:
    async def check() -> None:
        response = {
            **FOOD,
            "foodNutrients": [{"nutrient": {"id": 1008, "unitName": "KCAL"}, "amount": 100}],
            "foodPortions": [
                {"amount": 3, "gramWeight": 9, "modifier": "cloves"},
                {
                    "amount": 1,
                    "gramWeight": 150,
                    "measureUnit": {"name": "cup"},
                    "modifier": "chopped",
                },
                {"amount": 1, "modifier": "package"},
                {"amount": 0, "gramWeight": 0, "modifier": "not measured"},
            ],
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
        ) as client:
            profile = await FoodDataCentral(client, SecretStr("synthetic-key")).profile("1234567")
        assert len(profile.portions) == 2
        assert profile.portions[0].amount == 3 and profile.portions[0].gram_weight == 9
        assert profile.portions[1].measure_unit == "cup"
        assert profile.portions[1].modifier == "chopped"

    asyncio.run(check())


@pytest.mark.parametrize(
    ("name", "label", "unit", "expected"),
    [
        ("regional food", "medium", "piece", 50),
        ("regional food", "whole", "item", 50),
        ("regional food", "small package", "piece", None),
        ("regional food", "medium slice", "piece", None),
        ("regional food slice", "slice", "piece", 50),
        ("regional food wedge", "fruit", "piece", None),
        ("regional food pod", "pod", "piece", 50),
        ("regional food", "tablespoon", "tbsp", 50),
        ("\u98df\u6750", "piece", "piece", 50),
    ],
)
def test_source_portions_work_for_unlisted_foods_without_guessing_item_parts(
    name: str, label: str, unit: str, expected: int | None
) -> None:
    async def check() -> None:
        response = {
            **FOOD,
            "description": name,
            "foodNutrients": [{"nutrient": {"id": 1008, "unitName": "KCAL"}, "amount": 100}],
            "foodPortions": [{"amount": 2, "gramWeight": 50, "modifier": label}],
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
        ) as client:
            profile = await FoodDataCentral(client, SecretStr("synthetic-key")).profile("1234567")
        result = ingredient_weight(name, Decimal(2), unit, profile, estimates=True)
        if expected is None:
            assert result is None
        else:
            assert result is not None and result[0] == expected
            assert result[1].startswith("USDA portion:")

    asyncio.run(check())


def test_default_config_needs_only_protected_api_key_not_database_or_download() -> None:
    config = UsdaMcpConfig.from_settings(
        Settings(
            environment="local",
            adapter_mode="local",
            _env_file=None,
            usda_api_key=SecretStr("synthetic-private-key"),
            usda_mcp_env={"USDA_POSTGRES_PASSWORD": SecretStr("obsolete-credential")},
        )
    )
    assert config.python == Path(sys.executable)
    assert config.server.name == "usda_mcp_server.py"
    assert config.environment["USDA_API_KEY"] == "synthetic-private-key"
    assert "PYTHONPATH" in config.environment
    assert "USDA_POSTGRES_PASSWORD" not in config.environment
    assert "private" not in repr(config)
    with pytest.raises(ConfigurationError, match="ASC_USDA_API_KEY"):
        UsdaMcpConfig.from_settings(
            Settings(
                environment="local",
                adapter_mode="local",
                _env_file=None,
                usda_mcp_env={"USDA_POSTGRES_PASSWORD": SecretStr("obsolete-credential")},
            )
        )


@pytest.mark.parametrize("environment", ["dev", "prod"])
@pytest.mark.parametrize("override", ["usda_mcp_server", "usda_mcp_python"])
def test_hosted_api_cannot_be_replaced_by_a_legacy_database_launcher(
    environment: Literal["dev", "prod"],
    override: Literal["usda_mcp_server", "usda_mcp_python"],
) -> None:
    settings = Settings(
        environment=environment,
        adapter_mode="live",
        agents_enabled=False,
        cosmos_endpoint="https://synthetic.documents.azure.com",
        cosmos_database="synthetic",
        usda_api_key=SecretStr("synthetic-private-key"),
        usda_mcp_server=Path(sys.executable) if override == "usda_mcp_server" else None,
        usda_mcp_python=Path(sys.executable) if override == "usda_mcp_python" else None,
        _env_file=None,
    )
    with pytest.raises(ConfigurationError, match="Remove ASC_USDA_MCP_SERVER") as caught:
        UsdaMcpConfig.from_settings(settings)
    assert "private" not in str(caught.value)


def test_search_preserves_terms_filters_forms_and_ranks_before_requested_result_limit() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.nal.usda.gov"
        assert request.headers["X-Api-Key"] == "synthetic-private-key"
        assert "api_key" not in request.url.params
        return httpx.Response(
            200,
            json={
                "totalHits": 35,
                "foods": [
                    *[
                        dict(
                            FOOD,
                            fdcId=2000000 + index,
                            description=f"Babyfood, chicken dinner {index}",
                            foodCategory="Baby Foods",
                        )
                        for index in range(15)
                    ],
                    dict(FOOD, fdcId=3000000, description="Chicken, breast, roasted"),
                    dict(FOOD, fdcId=3000001, description="Babyfood, chicken and strawberry"),
                    FOOD,
                ],
            },
        )

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            api = FoodDataCentral(client, SecretStr("synthetic-private-key"))
            raw = await api.search("chicken, raw")
            assert [food.fdc_id for food in raw.foods] == ["1234567"]
            assert raw.truncated  # Provider reports additional unexamined matches.
            generic = await api.search("chicken", limit=2)
            assert len(generic.foods) == 2 and generic.foods[0].fdc_id == "1234567"

    asyncio.run(check())
    assert json.loads(requests[0].content)["query"] == "+chicken +raw"


@pytest.mark.parametrize("keyword", ["rice", "salt"])
def test_broad_search_requests_fit_the_existing_response_budget(keyword: str) -> None:
    page_sizes: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        page_sizes.append(body["pageSize"])
        return httpx.Response(
            200,
            json={
                "totalHits": 300,
                "foods": [
                    dict(
                        FOOD,
                        fdcId=1234567 + index,
                        description=f"{keyword}, synthetic food {index}",
                        foodNutrients=[
                            {
                                "nutrientId": 1008,
                                "value": 100,
                                "derivationDescription": "synthetic source metadata " * 600,
                            }
                        ],
                    )
                    for index in range(body["pageSize"])
                ],
            },
        )

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await FoodDataCentral(client, SecretStr("synthetic-key")).search(keyword)
            assert len(result.foods) == 20
            assert result.truncated

    asyncio.run(check())
    assert page_sizes == [20]


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_hosted_api_configuration_needs_no_database_settings(
    environment: Literal["dev", "prod"],
) -> None:
    config = UsdaMcpConfig.from_settings(
        Settings(
            environment=environment,
            adapter_mode="live",
            agents_enabled=False,
            cosmos_endpoint="https://synthetic.documents.azure.com",
            cosmos_database="synthetic",
            usda_enabled=True,
            usda_api_key=SecretStr("synthetic-private-key"),
            _env_file=None,
        )
    )
    assert set(config.environment) == {"USDA_API_KEY", "PYTHONPATH"}
    assert config.environment["USDA_API_KEY"] == "synthetic-private-key"
    assert config.python == Path(sys.executable)
    assert config.server.name == "usda_mcp_server.py"


@pytest.mark.parametrize("count", [20, 21])
def test_complete_search_is_only_truncated_when_candidates_were_omitted(count: int) -> None:
    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "totalHits": count,
                        "foods": [dict(FOOD, fdcId=1234567 + i) for i in range(min(count, 20))],
                    },
                )
            )
        ) as client:
            result = await FoodDataCentral(client, SecretStr("synthetic-key")).search("chicken")
            assert len(result.foods) == 20
            assert result.truncated == (count > 20)

    asyncio.run(check())


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "authentication"),
        (403, "authentication"),
        (404, "no_results"),
        (429, "throttled"),
        (302, "rejected"),
        (400, "rejected"),
    ],
)
def test_provider_failures_are_safe_and_never_retried(status: int, code: str) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, text="private-key-and-provider-details", headers={"Retry-After": "60"}
        )

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            api = FoodDataCentral(client, SecretStr("synthetic-private-key"))
            with pytest.raises(FdcError) as caught:
                await api.search("chicken")
            assert caught.value.code == code and "private" not in str(caught.value)
            if status == 429:
                with pytest.raises(FdcError, match="throttled"):
                    await api.search("carrots")

    asyncio.run(check())
    assert calls == 1


@pytest.mark.parametrize("failure", [500, 502, 503, 504, "timeout", "connection"])
@pytest.mark.parametrize("recovers", [False, True])
def test_transient_read_failures_are_retried_with_a_fixed_bound(
    failure: int | str, recovers: bool, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if recovers and len(calls) == 3:
            return httpx.Response(200, json={"foods": [FOOD], "totalHits": 1})
        if failure == "timeout":
            raise httpx.ReadTimeout("private-key", request=request)
        if failure == "connection":
            raise httpx.ConnectError("private-provider-details", request=request)
        assert isinstance(failure, int)
        return httpx.Response(failure, text="private-provider-details")

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            api = FoodDataCentral(client, SecretStr("synthetic-private-key"))
            if recovers:
                assert (await api.search("chicken")).foods[0].fdc_id == "1234567"
            else:
                with pytest.raises(FdcError) as caught:
                    await api.search("chicken")
                assert caught.value.code == ("timeout" if failure == "timeout" else "unavailable")

    asyncio.run(check())
    assert len(calls) == 3
    assert all(call.content == calls[0].content for call in calls)
    assert "private" not in caplog.text


def test_cancellation_stops_transient_retries() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            api = FoodDataCentral(client, SecretStr("synthetic-key"))
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.01):
                    await api.search("chicken")

    asyncio.run(check())
    assert calls == 1


@pytest.mark.parametrize(
    ("retry_after", "seconds"),
    [
        ("60", 60),
        ("0", 1),
        ("99999", 3600),
        ("", 3600),
        ("not-a-duration", 3600),
        ("9" * 5000, 3600),
    ],
)
def test_quota_cooldown_is_bounded_and_invalid_headers_are_safe(
    retry_after: str, seconds: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sous_chef.usda_mcp_server.monotonic", lambda: 100.0)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(429, headers={"Retry-After": retry_after})
            )
        ) as client:
            api = FoodDataCentral(client, SecretStr("synthetic-private-key"))
            with pytest.raises(FdcError, match="throttled"):
                await api.search("chicken")
            assert api._retry_at == 100.0 + seconds

    asyncio.run(check())


@pytest.mark.parametrize(
    "kind",
    [
        "malformed",
        "oversized",
        "duplicate",
        "branded",
        "negative",
        "wrong_unit",
        "wrong_id",
        "timeout",
    ],
)
def test_invalid_or_timed_out_api_details_cannot_become_nutrition(kind: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if kind == "timeout":
            raise httpx.ReadTimeout("private-key", request=request)
        if kind == "malformed":
            return httpx.Response(200, text="private invalid JSON")
        if kind == "oversized":
            return httpx.Response(200, content=b"x" * 2_097_153)
        nutrient = {"nutrient": {"id": 1008, "unitName": "KCAL"}, "amount": 100}
        food = dict(FOOD, foodNutrients=[nutrient])
        if kind == "duplicate":
            food["foodNutrients"] = [nutrient, nutrient]
        elif kind == "branded":
            food["dataType"] = "Branded"
        elif kind == "negative":
            nutrient["amount"] = -1
        elif kind == "wrong_unit":
            nutrient["nutrient"] = {"id": 1008, "unitName": "KJ"}
        elif kind == "wrong_id":
            food["fdcId"] = 7654321
        return httpx.Response(200, json=food)

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            api = FoodDataCentral(client, SecretStr("synthetic-private-key"))
            with pytest.raises(FdcError) as caught:
                await api.profile("1234567")
            assert caught.value.code == ("timeout" if kind == "timeout" else "invalid_result")
            assert "private" not in str(caught.value)

    asyncio.run(check())


def test_source_zero_missing_values_and_energy_preference_are_distinct() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **FOOD,
                "foodNutrients": [
                    {"nutrient": {"id": id, "unitName": unit}, "amount": amount}
                    for id, unit, amount in [
                        (2047, "KCAL", 120),
                        (2048, "KCAL", 110),
                        (1008, "KCAL", 100),
                        (1079, "G", 0),
                        (1003, "G", None),
                    ]
                ],
            },
        )

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await FoodDataCentral(client, SecretStr("synthetic-private-key")).profile(
                "1234567"
            )
            assert {row.nutrdesc: row.nutr_val for row in result.nutrients} == {
                "Energy": Decimal(100),
                "Fiber, total dietary": Decimal(0),
            }
            assert result.food.fdc_id == "1234567" and result.food.publication_date == "2024-04-18"

    asyncio.run(check())
