import asyncio
import json
import sys
from pathlib import Path
from time import monotonic
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sous_chef.adapters import usda_mcp
from sous_chef.adapters.usda_mcp import UsdaMcpConfig
from sous_chef.app import create_app
from sous_chef.domain.foundry import FoundryFailure
from sous_chef.domain.recipes import RecipeToolError
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.settings import ConfigurationError, Settings
from test_usda_nutrition import config, ingredient, request


def api_tool(mode: str = "") -> UsdaNutritionTool:
    fixture = Path(__file__).parents[1] / "fixtures" / "usda_api_server.py"
    return UsdaNutritionTool(UsdaMcpConfig(Path(sys.executable), fixture, {"TEST_USDA_MODE": mode}))


@pytest.mark.parametrize(
    ("name", "known"),
    [
        ("raw apple with skin", True),
        ("raw carrot", True),
        ("ground cinnamon", True),
        ("apple", False),
        ("raw apple without skin", False),
        ("cooked apple with skin", False),
        ("cinnamon", False),
        ("cinnamon oil", False),
        ("plain whole milk Greek yogurt", False),
    ],
)
def test_food_wording_equivalents_preserve_preparation_and_composition(
    name: str, known: bool
) -> None:
    evidence = asyncio.run(api_tool().lookup(request(ingredient(name)), deadline=monotonic() + 10))
    assert (evidence.status == "known") is known
    if not known:
        assert not evidence.nutrients and not evidence.mappings


@pytest.mark.parametrize("mode", ["ambiguous_wording", "truncated_wording"])
def test_equivalent_food_words_still_require_one_match_from_a_complete_search(mode: str) -> None:
    evidence = asyncio.run(
        api_tool(mode).lookup(request(ingredient("raw apple with skin")), deadline=monotonic() + 10)
    )
    assert evidence.status == "unavailable"
    assert not evidence.nutrients and not evidence.mappings and not evidence.sources
    assert evidence.unknown_components[0].reason == "food_unavailable"


def test_starting_app_reuses_one_mcp_process_and_reaps_it_on_shutdown(tmp_path: Path) -> None:
    log = tmp_path / "mcp.jsonl"
    tool = UsdaNutritionTool(config(log=log))
    settings = Settings(
        environment="local", adapter_mode="local", usda_enabled=True, _env_file=None
    )
    with patch.object(UsdaNutritionTool, "from_settings", return_value=tool):
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert app.state.nutrition is tool
            assert tool._client is not None and tool._client._session is not None
            process = tool._client._session._process
            for _ in range(2):
                assert client.portal is not None
                client.portal.call(tool.check)
    messages = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(message["method"] == "initialize" for message in messages) == 1
    assert sum(message["method"] == "tools/call" for message in messages) == 6
    assert process.returncode is not None


def test_cold_initialization_does_not_consume_the_live_api_check_budget(tmp_path: Path) -> None:
    log = tmp_path / "cold-mcp.jsonl"
    tool = UsdaNutritionTool(config("slow_initialize", log=log, timeout=0.5))
    settings = Settings(
        environment="local", adapter_mode="local", usda_enabled=True, _env_file=None
    )
    with patch.object(UsdaNutritionTool, "from_settings", return_value=tool):
        with TestClient(create_app(settings)) as client:
            assert client.get("/health").status_code == 200
            assert tool.config.timeout_seconds == 0.5
            assert tool._client is not None and tool._client._session is not None
            process = tool._client._session._process
    messages = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(message["method"] == "initialize" for message in messages) == 1
    calls = [message for message in messages if message["method"] == "tools/call"]
    assert [message["params"]["name"] for message in calls] == [
        "search_foods",
        "get_nutrition_profile",
    ]
    assert process.returncode is not None


def test_stalled_initialization_is_bounded_and_reaps_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(usda_mcp, "STARTUP_TIMEOUT_SECONDS", 0.2)

    async def check() -> None:
        tool = UsdaNutritionTool(config("slow_initialize", timeout=0.5))
        sessions: list[usda_mcp.UsdaMcpSession] = []
        spawn = usda_mcp._spawn

        async def capture(config: UsdaMcpConfig) -> usda_mcp.UsdaMcpSession:
            session = await spawn(config)
            sessions.append(session)
            return session

        started = monotonic()
        with patch.object(usda_mcp, "_spawn", side_effect=capture):
            with pytest.raises(ConfigurationError, match="foundry_timeout"):
                async with tool.lifespan():
                    pytest.fail("A timed-out initialization must not pass startup.")
        assert monotonic() - started < 2
        assert len(sessions) == 1
        assert sessions[0]._process.returncode is not None
        assert tool._client is None

    asyncio.run(check())


def test_cancelling_initialization_reaps_the_child() -> None:
    async def check() -> None:
        tool = UsdaNutritionTool(config("slow_initialize"))
        spawned = asyncio.Event()
        sessions: list[usda_mcp.UsdaMcpSession] = []
        spawn = usda_mcp._spawn

        async def capture(config: UsdaMcpConfig) -> usda_mcp.UsdaMcpSession:
            session = await spawn(config)
            sessions.append(session)
            spawned.set()
            return session

        async def startup() -> None:
            async with tool.lifespan():
                pytest.fail("Cancelled initialization must not pass startup.")

        with patch.object(usda_mcp, "_spawn", side_effect=capture):
            task = asyncio.create_task(startup())
            await asyncio.wait_for(spawned.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(sessions) == 1
        assert sessions[0]._process.returncode is not None
        assert tool._client is None

    asyncio.run(check())


def test_startup_allowance_does_not_extend_runtime_or_replay_timed_out_calls(
    tmp_path: Path,
) -> None:
    async def check() -> None:
        log = tmp_path / "runtime-timeout.jsonl"
        tool = UsdaNutritionTool(config("hang_rice", log=log, timeout=0.2))
        async with tool.lifespan():
            assert tool._client is not None and tool._client._session is not None
            process = tool._client._session._process
            started = monotonic()
            with pytest.raises(RecipeToolError) as caught:
                await tool.search("rice", deadline=monotonic() + 10)
            assert caught.value.code == FoundryFailure.TIMEOUT
            assert monotonic() - started < 2
            assert process.returncode is not None
            assert tool._client._session is None
        messages = [json.loads(line) for line in log.read_text().splitlines()]
        assert sum(message["method"] == "initialize" for message in messages) == 1
        calls = [message["params"] for message in messages if message["method"] == "tools/call"]
        assert sum(call["arguments"].get("keyword") == "rice" for call in calls) == 1

    asyncio.run(check())


@pytest.mark.parametrize("mode", ["schema", "no_food", "malformed"])
def test_enabled_app_does_not_start_with_broken_api_or_mcp(mode: str) -> None:
    tool = UsdaNutritionTool(config(mode, timeout=1))
    settings = Settings(
        environment="local", adapter_mode="local", usda_enabled=True, _env_file=None
    )
    with patch.object(UsdaNutritionTool, "from_settings", return_value=tool):
        with pytest.raises(ConfigurationError, match="USDA startup check") as caught:
            with TestClient(create_app(settings)):
                pytest.fail("Broken live startup must not succeed.")
    assert "private" not in str(caught.value)
    assert tool._client is None


@pytest.mark.parametrize("mode", ["error", "timeout", "throttled"])
def test_transient_usda_outage_after_initialization_does_not_prevent_chat_startup(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    tool = UsdaNutritionTool(config(mode, timeout=0.2))
    settings = Settings(
        environment="local", adapter_mode="local", usda_enabled=True, _env_file=None
    )
    with patch.object(UsdaNutritionTool, "from_settings", return_value=tool):
        with TestClient(create_app(settings)) as client:
            assert client.get("/health").status_code == 200
            assert tool._client is not None
    assert tool._client is None
    assert "usda_failed" in caplog.text
    assert "private database credential" not in caplog.text


def test_real_api_backed_mcp_preserves_raw_search_and_original_fdc_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HOST", "DATABASE", "USER", "PASSWORD"):
        monkeypatch.setenv("USDA_POSTGRES_" + name, "deleted-database")

    async def check() -> None:
        tool = api_tool()
        async with tool.lifespan():
            raw = await tool.search("chicken, raw", deadline=monotonic() + 10)
            assert {food.fdc_id for food in raw.foods} == {"2345678", "2345679", "2345681"}
            generic = await tool.search("chicken", deadline=monotonic() + 10)
            assert len(generic.foods) == 20 and generic.truncated
            assert {food.fdc_id for food in generic.foods[:3]} == {
                "2345678",
                "2345679",
                "2345681",
            }
            selected = request(ingredient("chicken, raw")).model_copy(
                update={"selected_food_ids": {"rice": "2345678"}}
            )
            result = await tool.lookup(selected, deadline=monotonic() + 10)
            assert result.mappings[0].usda_food_id == "2345678"
            assert result.sources[0].source_id == "usda:fdc:2345678"
            assert result.sources[0].version == "Foundation; published 2024-04-18"
            assert (
                result.sources[0].reference
                == "https://fdc.nal.usda.gov/food-details/2345678/nutrients"
            )
            wrong_form = selected.model_copy(update={"selected_food_ids": {"rice": "2345680"}})
            assert (
                await tool.lookup(wrong_form, deadline=monotonic() + 10)
            ).status == "unavailable"

    asyncio.run(check())


def test_real_mcp_exposes_rate_limit_as_typed_failure_without_secret_text() -> None:
    with pytest.raises(RecipeToolError) as caught:
        asyncio.run(api_tool("throttled").search("chicken", deadline=monotonic() + 10))
    assert caught.value.code == FoundryFailure.THROTTLED
    assert "private" not in str(caught.value)


def test_default_offline_app_never_starts_live_mcp() -> None:
    settings = Settings(environment="local", adapter_mode="local", _env_file=None)
    with patch.object(UsdaNutritionTool, "from_settings") as nutrition:
        with TestClient(create_app(settings)) as client:
            assert client.get("/health").status_code == 200
        nutrition.assert_not_called()


def test_queued_deadline_does_not_close_active_process_and_cancelled_exchange_is_not_reused(
    tmp_path: Path,
) -> None:
    async def check() -> None:
        log = tmp_path / "reuse.jsonl"
        tool = UsdaNutritionTool(config("hang_rice", log=log))
        async with tool.lifespan():
            assert tool._client is not None and tool._client._session is not None
            process = tool._client._session._process
            blocked = asyncio.create_task(tool.search("rice", deadline=monotonic() + 10))
            await asyncio.sleep(0.1)
            with pytest.raises(RecipeToolError) as caught:
                await tool.search("Carrots, raw", deadline=monotonic() + 0.05)
            assert caught.value.code == FoundryFailure.TIMEOUT
            assert process.returncode is None
            blocked.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked
            assert process.returncode is not None
            found = await tool.search("Carrots, raw", deadline=monotonic() + 10)
            assert found.foods[0].fdc_id == "99904"
        messages = [json.loads(line) for line in log.read_text().splitlines()]
        assert sum(message["method"] == "initialize" for message in messages) == 2
        calls = [message["params"] for message in messages if message["method"] == "tools/call"]
        assert sum(call["arguments"].get("keyword") == "rice" for call in calls) == 1

    asyncio.run(check())


def test_rate_limit_retains_the_process_and_its_cooldown() -> None:
    async def check() -> None:
        tool = api_tool("throttle_chicken")
        async with tool.lifespan():
            assert tool._client is not None and tool._client._session is not None
            process = tool._client._session._process
            for _ in range(2):
                with pytest.raises(RecipeToolError) as caught:
                    await tool.search("chicken", deadline=monotonic() + 10)
                assert caught.value.code == FoundryFailure.THROTTLED
                assert tool._client._session is not None
                assert tool._client._session._process is process

    asyncio.run(check())
