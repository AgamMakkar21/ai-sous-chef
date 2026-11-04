import asyncio
import json
import sys
from pathlib import Path
from time import monotonic
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sous_chef.adapters.usda_mcp import UsdaMcpConfig
from sous_chef.app import create_app
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.usda_dependency import SOURCE_PATH, installed_source
from test_usda_nutrition import config, ingredient, request


def upstream_tool() -> UsdaNutritionTool:
    if not SOURCE_PATH.is_file():
        pytest.skip("Install the pinned source during build to run the upstream contract check.")
    fixture = Path(__file__).parents[1] / "fixtures" / "usda_upstream_database.py"
    return UsdaNutritionTool(
        UsdaMcpConfig(Path(sys.executable), fixture, {"TEST_USDA_SOURCE": str(installed_source())})
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("chicken, raw", {"99910", "99911", "99913"}),
        ("RAW chicken breast", {"99910", "99913"}),
        ("chicken-breast (raw)", {"99910", "99913"}),
        ("chicken roasted", {"99912", "99914"}),
        ("chicken raw nonexistent", set()),
        ("chicken raw' OR '1'='1", set()),
    ],
)
def test_real_mcp_search_filters_every_term_before_limiting(name: str, expected: set[str]) -> None:
    result = asyncio.run(upstream_tool().search(name, deadline=monotonic() + 10))
    assert {food.ndb_no for food in result.foods} == expected
    assert not result.truncated


def test_real_mcp_ranks_raw_chicken_ahead_of_more_than_twenty_prepared_products() -> None:
    async def check() -> None:
        tool = upstream_tool()
        found = await tool.search("chicken", deadline=monotonic() + 10)
        assert len(found.foods) == 20 and found.truncated
        assert {food.ndb_no for food in found.foods[:3]} == {"99910", "99911", "99913"}
        assert found.foods[3].ndb_no == "99912"
        assert all(food.food_group == "Poultry Products" for food in found.foods[:4])
        assert (
            await tool.lookup(request(ingredient("chicken")), deadline=monotonic() + 10)
        ).status == "unavailable"
        selected = request(ingredient("chicken, raw")).model_copy(
            update={"selected_food_ids": {"rice": "99910"}}
        )
        evidence = await tool.lookup(selected, deadline=monotonic() + 10)
        assert evidence.mappings[0].usda_food_id == "99910"
        assert evidence.mappings[0].source_id == "usda:sr:99910"
        wrong_form = selected.model_copy(update={"selected_food_ids": {"rice": "99912"}})
        assert (await tool.lookup(wrong_form, deadline=monotonic() + 10)).status == "unavailable"

    asyncio.run(check())


def test_starting_app_runs_real_stdio_lookup_and_reaps_process(tmp_path: Path) -> None:
    log = tmp_path / "mcp.jsonl"
    tool = UsdaNutritionTool(config(log=log))
    settings = Settings(
        environment="local",
        adapter_mode="local",
        usda_enabled=True,
        _env_file=None,
    )
    with patch.object(UsdaNutritionTool, "from_settings", return_value=tool):
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert app.state.nutrition is tool
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    calls = [request["params"] for request in requests if request["method"] == "tools/call"]
    assert [call["name"] for call in calls] == ["search_foods", "get_nutrition_profile"]
    assert calls[0]["arguments"] == {"keyword": "Carrots, raw", "limit": 20}


@pytest.mark.parametrize("mode", ["no_food", "error", "malformed", "timeout"])
def test_enabled_app_does_not_start_with_broken_database_or_mcp(mode: str) -> None:
    tool = UsdaNutritionTool(config(mode, timeout=1))
    settings = Settings(
        environment="local",
        adapter_mode="local",
        usda_enabled=True,
        _env_file=None,
    )
    with patch.object(UsdaNutritionTool, "from_settings", return_value=tool):
        with pytest.raises(ConfigurationError, match="USDA startup check") as caught:
            with TestClient(create_app(settings)):
                pytest.fail("Broken live startup must not succeed.")
    assert "private" not in str(caught.value)


def test_default_offline_app_never_starts_live_mcp() -> None:
    settings = Settings(environment="local", adapter_mode="local", _env_file=None)
    with patch.object(UsdaNutritionTool, "from_settings") as nutrition:
        with TestClient(create_app(settings)) as client:
            assert client.get("/health").status_code == 200
        nutrition.assert_not_called()


def test_actual_pinned_upstream_and_mcp_sdk_start_with_synthetic_database() -> None:
    tool = upstream_tool()
    settings = Settings(
        environment="local",
        adapter_mode="local",
        usda_enabled=True,
        _env_file=None,
    )
    with patch.object(UsdaNutritionTool, "from_settings", return_value=tool):
        with TestClient(create_app(settings)) as client:
            assert client.get("/health").status_code == 200
