import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from time import monotonic

import pytest
from pydantic import SecretStr, ValidationError
from sous_chef.adapters.usda_mcp import UsdaMcpConfig, open_usda
from sous_chef.domain.foundry import FoundryFailure
from sous_chef.domain.recipes import DraftIngredient, NutritionRequest, RecipeToolError
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.services.recipe_specialist import RecipeSpecialistService
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import configure_safe_telemetry
from sous_chef.usda_mcp_server import __file__ as LAUNCHER

SERVER = Path(__file__).parents[1] / "fixtures" / "usda_mcp_server.py"


def config(mode: str = "", *, log: Path | None = None, timeout: float = 10) -> UsdaMcpConfig:
    environment = {"TEST_USDA_MODE": mode}
    if log is not None:
        environment["TEST_USDA_LOG"] = str(log)
    return UsdaMcpConfig(Path(sys.executable), SERVER, environment, timeout)


def ingredient(
    name: str = "rice",
    quantity: int | None = 100,
    unit: str | None = "g",
    line_id: str = "rice",
) -> DraftIngredient:
    return DraftIngredient.model_validate(
        {"line_id": line_id, "name": name, "quantity": quantity, "unit": unit}
    )


def request(*lines: DraftIngredient, servings: int = 2) -> NutritionRequest:
    return NutritionRequest(servings=servings, ingredients=lines or (ingredient(),))


def test_agents_can_search_then_select_a_real_candidate_with_original_source_id() -> None:
    async def check() -> None:
        tool = UsdaNutritionTool(config("candidates"))
        candidates = await tool.search("chicken", deadline=monotonic() + 10)
        assert len(candidates.foods) == 2 and not candidates.truncated
        ambiguous = await tool.lookup(request(ingredient("chicken")), deadline=monotonic() + 10)
        assert ambiguous.status == "unavailable"
        selected = NutritionRequest(
            servings=2,
            ingredients=(ingredient("chicken"),),
            selected_food_ids={"rice": candidates.foods[0].ndb_no},
        )
        evidence = await tool.lookup(selected, deadline=monotonic() + 10)
        assert evidence.status == "known"
        assert evidence.mappings[0].usda_food_id == "99901"
        assert evidence.nutrients[0].amount == 100
        unrelated = selected.model_copy(update={"selected_food_ids": {"rice": "00001"}})
        assert (await tool.lookup(unrelated, deadline=monotonic() + 10)).status == "unavailable"

    asyncio.run(check())


def test_unambiguous_reordered_words_resolve_without_choosing_a_different_food_form() -> None:
    tool = UsdaNutritionTool(config("reordered"))
    known = asyncio.run(tool.lookup(request(ingredient("raw rice")), deadline=monotonic() + 10))
    assert known.status == "known"
    cooked = asyncio.run(tool.lookup(request(ingredient("rice cooked")), deadline=monotonic() + 10))
    assert cooked.status == "unavailable"


def test_selected_candidate_line_ids_and_original_five_digit_food_ids_are_validated() -> None:
    for selection in ({"foreign": "99901"}, {"rice": "123"}, {"rice": "%"}):
        with pytest.raises(ValidationError):
            NutritionRequest(servings=1, ingredients=(ingredient(),), selected_food_ids=selection)


def test_search_marks_saturated_candidates_but_does_not_automatically_select() -> None:
    tool = UsdaNutritionTool(config("saturated"))
    found = asyncio.run(tool.search("rice", deadline=monotonic() + 10))
    assert len(found.foods) == 20 and found.truncated


def test_known_food_has_typed_source_values_and_original_ndb_number() -> None:
    async def check() -> None:
        async with open_usda(config(), deadline=monotonic() + 10) as session:
            profile = await session.food("rice")
            assert profile.food.ndb_no == "99901"
            assert profile.basis == "per_100_g"
            assert profile.nutrients[0].nutr_val == Decimal(200)
            assert profile.nutrients[0].units == "kcal"
        assert session._process.returncode is not None

    asyncio.run(check())


@pytest.mark.parametrize(
    ("unit", "quantity", "energy"),
    [
        ("g", 100, 100),
        ("kg", 1, 1000),
        ("oz", 1, 28.349523125),
        ("lb", 1, 453.59237),
    ],
)
def test_mass_conversion_and_servings_are_deterministic(
    unit: str,
    quantity: int,
    energy: float,
) -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config()).lookup(
            request(ingredient(quantity=quantity, unit=unit)),
            deadline=monotonic() + 10,
        )
    )
    assert evidence.status == "known"
    assert evidence.serving_basis == "per_serving" and evidence.servings == 2
    assert evidence.nutrients[0].amount == pytest.approx(energy)
    assert evidence.mappings[0].usda_food_id == "99901"
    assert evidence.mappings[0].quantity == quantity
    assert evidence.mappings[0].unit == unit
    assert evidence.sources[0].kind == "usda"
    assert evidence.mappings[0].source_id == evidence.sources[0].source_id


@pytest.mark.parametrize("mode", ["no_food", "ambiguous", "missing_profile", "saturated"])
def test_unknown_or_ambiguous_foods_are_unavailable_not_zero(mode: str) -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config(mode)).lookup(
            request(),
            deadline=monotonic() + 10,
        )
    )
    assert evidence.status == "unavailable"
    assert not evidence.nutrients and not evidence.mappings and not evidence.sources


@pytest.mark.parametrize(
    "line",
    [
        ingredient(quantity=None),
        ingredient(unit=None),
        ingredient(unit="cup"),
        ingredient(unit="ml"),
        ingredient(unit="item"),
        ingredient("unknown food"),
    ],
)
def test_unknown_quantities_density_or_portion_weights_are_not_invented(
    line: DraftIngredient,
) -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config()).lookup(
            request(line),
            deadline=monotonic() + 10,
        )
    )
    assert evidence.status == "unavailable" and not evidence.nutrients


def test_partial_results_never_claim_complete_recipe_or_assume_missing_nutrients_are_zero() -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config("missing_nutrient")).lookup(
            request(ingredient(), ingredient("unknown", line_id="unknown")),
            deadline=monotonic() + 10,
        )
    )
    assert evidence.status == "partial"
    assert len(evidence.mappings) == 1
    assert "fiber" not in {nutrient.name for nutrient in evidence.nutrients}


def test_duplicate_ingredients_share_food_lookup_but_not_quantity_mappings(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    evidence = asyncio.run(
        UsdaNutritionTool(config(log=log)).lookup(
            request(ingredient(), ingredient(quantity=200, line_id="second")),
            deadline=monotonic() + 10,
        )
    )
    assert evidence.status == "known" and evidence.nutrients[0].amount == 300
    assert len(evidence.sources) == 1 and len(evidence.mappings) == 2
    messages = [json.loads(line) for line in log.read_text().splitlines()]
    calls = [message["params"] for message in messages if message["method"] == "tools/call"]
    assert [call["name"] for call in calls] == ["search_foods", "get_nutrition_profile"]


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("schema", FoundryFailure.INVALID_RESULT),
        ("malformed", FoundryFailure.INVALID_RESULT),
        ("oversized", FoundryFailure.INVALID_RESULT),
        ("wrong_food", FoundryFailure.INVALID_RESULT),
        ("wrong_unit", FoundryFailure.INVALID_RESULT),
        ("nan", FoundryFailure.INVALID_RESULT),
        ("negative", FoundryFailure.INVALID_RESULT),
        ("duplicate", FoundryFailure.INVALID_RESULT),
        ("is_error", FoundryFailure.UNAVAILABLE),
        ("error", FoundryFailure.UNAVAILABLE),
    ],
)
def test_invalid_or_failed_source_never_becomes_nutrition(
    mode: str,
    code: FoundryFailure,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_safe_telemetry()
    with pytest.raises(RecipeToolError) as caught:
        asyncio.run(UsdaNutritionTool(config(mode)).lookup(request(), deadline=monotonic() + 10))
    assert caught.value.code == code
    assert "private" not in str(caught.value) and "private" not in caplog.text
    assert "usda_failed" in caplog.text


def test_no_arbitrary_sql_or_non_allowlisted_tool_can_execute(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"

    async def check() -> None:
        async with open_usda(config(log=log), deadline=monotonic() + 10) as session:
            with pytest.raises(RecipeToolError) as caught:
                await session._call("execute_query", {"query": "DELETE FROM food_des"})
            assert caught.value.code == FoundryFailure.REJECTED

    asyncio.run(check())
    assert "tools/call" not in log.read_text()


def test_timeout_and_cancellation_stop_the_child() -> None:
    async def check() -> None:
        async with open_usda(config("timeout"), deadline=monotonic() + 10) as session:
            task = asyncio.create_task(session.food("rice"))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert session._process.returncode is not None
        with pytest.raises(RecipeToolError) as caught:
            async with open_usda(
                config("timeout", timeout=0.5), deadline=monotonic() + 10
            ) as timed:
                await timed.food("rice")
        assert caught.value.code == FoundryFailure.TIMEOUT
        assert timed._process.returncode is not None

    asyncio.run(check())


def test_expired_deadline_does_not_launch_a_process(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    with pytest.raises(RecipeToolError) as caught:
        asyncio.run(
            UsdaNutritionTool(config(log=log)).lookup(
                request(),
                deadline=monotonic() - 1,
            )
        )
    assert caught.value.code == FoundryFailure.TIMEOUT
    assert not log.exists()


def test_settings_are_explicit_and_credentials_are_redacted() -> None:
    settings = Settings(
        environment="local",
        adapter_mode="local",
        _env_file=None,
        usda_mcp_python=Path(sys.executable),
        usda_mcp_server=SERVER,
        usda_mcp_env={"USDA_PASSWORD": SecretStr("private")},
    )
    assert UsdaMcpConfig.from_settings(settings).environment == {"USDA_PASSWORD": "private"}
    assert "private" not in repr(settings)
    assert "private" not in repr(UsdaMcpConfig.from_settings(settings))
    with pytest.raises(ConfigurationError):
        UsdaNutritionTool.from_settings(
            Settings(
                environment="local",
                adapter_mode="local",
                _env_file=None,
            )
        )
    with pytest.raises(ConfigurationError):
        Settings(environment="local", adapter_mode="local", usda_mcp_server=Path("relative.py"))
    with pytest.raises(ValidationError):
        Settings(environment="local", adapter_mode="local", usda_mcp_timeout_seconds=float("inf"))


def test_secure_entrypoint_overrides_sample_db_config_and_does_not_inherit_app_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASC_PAGINATION_KEY", "private app credential")
    environment = {
        "USDA_MCP_SOURCE": str(SERVER.with_name("usda_dependency.py")),
        "USDA_POSTGRES_HOST": "synthetic.invalid",
        "USDA_POSTGRES_DATABASE": "usda",
        "USDA_POSTGRES_USER": "readonly",
        "USDA_POSTGRES_PASSWORD": "private server credential",
    }
    tool = UsdaNutritionTool(UsdaMcpConfig(Path(sys.executable), Path(LAUNCHER), environment))
    result = asyncio.run(tool.lookup(request(), deadline=monotonic() + 10))
    assert result.status == "known"
    del environment["USDA_POSTGRES_PASSWORD"]
    with pytest.raises(RecipeToolError) as caught:
        asyncio.run(tool.lookup(request(), deadline=monotonic() + 10))
    assert caught.value.code == FoundryFailure.UNAVAILABLE


def test_exact_message_and_session_byte_limits() -> None:
    async def check() -> None:
        async with open_usda(config("at_limit"), deadline=monotonic() + 10) as session:
            # Two setup messages + seven two-message lookups = exactly 2 MiB.
            for _ in range(7):
                await session.food("rice")
            assert session._bytes == 2_097_152
            with pytest.raises(RecipeToolError) as caught:
                await session.food("rice")
            assert caught.value.code == FoundryFailure.INVALID_RESULT
        with pytest.raises(RecipeToolError) as caught:
            async with open_usda(config("over_limit"), deadline=monotonic() + 10):
                pytest.fail("A 128 KiB + 1 byte message must be rejected.")
        assert caught.value.code == FoundryFailure.INVALID_RESULT

    asyncio.run(check())


@pytest.mark.parametrize("count", [256, 257])
def test_nutrient_row_bound_is_exact(count: int) -> None:
    async def check() -> None:
        async with open_usda(config(f"rows_{count}"), deadline=monotonic() + 10) as session:
            profile = await session.food("rice")
            assert len(profile.nutrients) == count

    if count == 256:
        asyncio.run(check())
    else:
        with pytest.raises(RecipeToolError) as caught:
            asyncio.run(check())
        assert caught.value.code == FoundryFailure.INVALID_RESULT


def test_only_nine_food_searches_are_attempted_per_request(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    lines = tuple(ingredient(f"unknown{index}", line_id=f"line{index}") for index in range(30))
    result = asyncio.run(
        UsdaNutritionTool(config(log=log)).lookup(
            request(*lines),
            deadline=monotonic() + 10,
        )
    )
    assert result.status == "unavailable"
    messages = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(message["method"] == "tools/call" for message in messages) == 9


def test_live_specialist_factory_selects_real_nutrition_not_fixtures() -> None:
    from test_orchestrator_gateway import coordination_settings

    settings = Settings.model_validate(
        coordination_settings().model_dump()
        | {
            "usda_mcp_python": Path(sys.executable),
            "usda_mcp_server": SERVER,
        }
    )
    specialist = RecipeSpecialistService.from_settings(settings)
    assert isinstance(specialist._nutrition, UsdaNutritionTool)
    assert specialist._recipes is None


def test_ndb_leading_zeros_survive_recipe_mapping() -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config("leading_zero")).lookup(
            request(),
            deadline=monotonic() + 10,
        )
    )
    assert evidence.mappings[0].usda_food_id == "00001"
    assert evidence.sources[0].source_id == "usda:sr:00001"


def test_first_fuzzy_search_hit_is_never_assumed_to_be_the_requested_food() -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config("mismatch")).lookup(
            request(ingredient("carrot")),
            deadline=monotonic() + 10,
        )
    )
    assert evidence.status == "unavailable"
    assert not evidence.nutrients and not evidence.mappings
