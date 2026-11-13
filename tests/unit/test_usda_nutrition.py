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
from sous_chef.domain.recipes import (
    NUTRITION_NOTICE,
    DraftIngredient,
    NutritionEvidence,
    NutritionRequest,
    RecipeToolError,
)
from sous_chef.services.nutrition import UsdaNutritionTool
from sous_chef.services.recipe_specialist import RecipeSpecialistService
from sous_chef.settings import ConfigurationError, Settings
from sous_chef.telemetry import configure_safe_telemetry

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
            selected_food_ids={"rice": candidates.foods[0].fdc_id},
        )
        evidence = await tool.lookup(selected, deadline=monotonic() + 10)
        assert evidence.status == "known"
        assert evidence.mappings[0].usda_food_id == "99901"
        assert evidence.nutrients[0].amount == 100
        unrelated = selected.model_copy(update={"selected_food_ids": {"rice": "1234567"}})
        assert (await tool.lookup(unrelated, deadline=monotonic() + 10)).status == "unavailable"

    asyncio.run(check())


def test_unambiguous_reordered_words_resolve_without_choosing_a_different_food_form() -> None:
    tool = UsdaNutritionTool(config("reordered"))
    known = asyncio.run(tool.lookup(request(ingredient("raw rice")), deadline=monotonic() + 10))
    assert known.status == "known"
    cooked = asyncio.run(tool.lookup(request(ingredient("rice cooked")), deadline=monotonic() + 10))
    assert cooked.status == "unavailable"


def test_selected_candidate_line_ids_and_original_fdc_ids_are_validated() -> None:
    for selection in ({"foreign": "99901"}, {"rice": "0"}, {"rice": "00001"}, {"rice": "%"}):
        with pytest.raises(ValidationError):
            NutritionRequest(servings=1, ingredients=(ingredient(),), selected_food_ids=selection)


def test_search_marks_saturated_candidates_but_does_not_automatically_select() -> None:
    tool = UsdaNutritionTool(config("saturated"))
    found = asyncio.run(tool.search("rice", deadline=monotonic() + 10))
    assert len(found.foods) == 20 and found.truncated


def test_empty_multiword_search_never_drops_preparation_or_other_terms(tmp_path: Path) -> None:
    log = tmp_path / "search.jsonl"
    tool = UsdaNutritionTool(config(log=log))
    found = asyncio.run(tool.search("chicken, raw", deadline=monotonic() + 10))
    assert not found.foods and not found.truncated
    calls = [
        request["params"]["arguments"]
        for request in map(json.loads, log.read_text().splitlines())
        if request["method"] == "tools/call"
    ]
    assert calls == [{"keyword": "chicken, raw", "limit": 20}]


def test_known_food_has_typed_source_values_and_original_ndb_number() -> None:
    async def check() -> None:
        async with open_usda(config(), deadline=monotonic() + 10) as session:
            profile = await session.food("rice")
            assert profile.food.fdc_id == "99901"
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
        ("oz", 1, 28.35),
        ("lb", 1, 453.59),
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
    assert evidence.nutrients[0].amount == energy
    assert evidence.mappings[0].usda_food_id == "99901"
    assert evidence.mappings[0].quantity == quantity
    assert evidence.mappings[0].unit == unit
    assert evidence.sources[0].kind == "usda"
    assert evidence.mappings[0].source_id == evidence.sources[0].source_id
    assert evidence.mappings[0].weight_grams == pytest.approx(
        {"g": 1, "kg": 1000, "oz": 28.349523125, "lb": 453.59237}[unit] * quantity
    )


@pytest.mark.parametrize(
    ("unit", "quantity"),
    [("ml", 10), ("l", 1), ("tbsp", 1)],
)
def test_food_name_alone_does_not_invent_a_volume_density(unit: str, quantity: int) -> None:
    payload = request(ingredient("olive oil", quantity, unit))
    payload = payload.model_copy(update={"allow_estimates": True})
    evidence = asyncio.run(
        UsdaNutritionTool(config("varied")).lookup(payload, deadline=monotonic() + 10)
    )
    assert evidence.status == "unavailable"
    assert evidence.unknown_components[0].reason == "unsupported_unit"
    assert not evidence.mappings


def test_approximation_does_not_invent_density_or_count_weights_for_unknown_portions() -> None:
    for unit in ("ml", "piece", "package"):
        payload = request(ingredient(unit=unit)).model_copy(update={"allow_estimates": True})
        evidence = asyncio.run(
            UsdaNutritionTool(config()).lookup(payload, deadline=monotonic() + 10)
        )
        assert evidence.status == "unavailable"
        assert evidence.unknown_components[0].reason == "unsupported_unit"


@pytest.mark.parametrize(
    ("servings", "expected"),
    [
        (1, (350, 5.5, 43.5, 12.3, 5.5, 100)),
        (2, (175, 2.75, 21.75, 6.15, 2.75, 50)),
        (3, (116.67, 1.83, 14.5, 4.1, 1.83, 33.33)),
    ],
)
def test_distinct_source_values_produce_exact_whole_recipe_and_serving_estimates(
    servings: int, expected: tuple[float, ...]
) -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config("varied")).lookup(
            request(
                ingredient(),
                ingredient("carrot", 150, line_id="carrot"),
                ingredient("olive oil", 10, line_id="oil"),
                servings=servings,
            ),
            deadline=monotonic() + 10,
        )
    )
    assert evidence.status == "known"
    assert tuple(item.amount for item in evidence.nutrients) == expected
    assert tuple(item.whole_recipe_amount for item in evidence.coverage) == (
        350,
        5.5,
        43.5,
        12.3,
        5.5,
        100,
    )
    assert all(
        item.status == "known"
        and item.covered_ingredient_line_ids == ("rice", "carrot", "oil")
        and not item.unknown_ingredient_line_ids
        for item in evidence.coverage
    )
    assert not evidence.unknown_components
    assert evidence.is_estimate and evidence.notice == NUTRITION_NOTICE
    assert [item.usda_food_id for item in evidence.mappings] == ["99901", "99902", "99903"]
    assert [item.weight_grams for item in evidence.mappings] == [100, 150, 10]


@pytest.mark.parametrize(
    ("quantities", "whole", "per_serving"),
    [
        ((0.001, 0.001, 0.001), 0.01, 0),
        ((0.005,), 0.01, 0.01),
    ],
)
def test_round_half_up_only_after_aggregation_and_unrounded_serving_division(
    quantities: tuple[float, ...], whole: float, per_serving: float
) -> None:
    lines = tuple(
        DraftIngredient.model_validate(
            {"line_id": f"rice{index}", "name": "rice", "quantity": quantity, "unit": "g"}
        )
        for index, quantity in enumerate(quantities)
    )
    evidence = asyncio.run(
        UsdaNutritionTool(config()).lookup(request(*lines), deadline=monotonic() + 10)
    )
    assert evidence.coverage[0].whole_recipe_amount == whole
    assert evidence.nutrients[0].amount == per_serving


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
    ("line", "reason"),
    [
        (ingredient(quantity=None), "missing_quantity"),
        (ingredient(unit=None), "missing_unit"),
        (ingredient(unit="cup"), "unsupported_unit"),
        (ingredient(unit="ml"), "unsupported_unit"),
        (ingredient(unit="item"), "unsupported_unit"),
        (ingredient(unit="piece"), "unsupported_unit"),
        (ingredient("unknown food"), "food_unavailable"),
    ],
)
def test_unknown_quantities_density_or_portion_weights_are_not_invented(
    line: DraftIngredient,
    reason: str,
) -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config()).lookup(
            request(line),
            deadline=monotonic() + 10,
        )
    )
    assert evidence.status == "unavailable" and not evidence.nutrients
    assert len(evidence.unknown_components) == 1
    assert evidence.unknown_components[0].reason == reason
    assert all(
        item.whole_recipe_amount is None
        and not item.covered_ingredient_line_ids
        and item.unknown_ingredient_line_ids == (line.line_id,)
        for item in evidence.coverage
    )


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
    assert evidence.coverage[0].status == "partial"
    assert evidence.coverage[0].unknown_ingredient_line_ids == ("unknown",)
    assert evidence.coverage[4].status == "unavailable"
    assert evidence.coverage[4].whole_recipe_amount is None


def test_missing_nutrients_retain_only_labeled_subtotals_and_exact_unknown_components() -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config("mixed_nutrients")).lookup(
            request(ingredient(), ingredient("carrot", line_id="carrot")),
            deadline=monotonic() + 10,
        )
    )
    nutrients = {item.name: item.amount for item in evidence.nutrients}
    coverage = {item.name: item for item in evidence.coverage}
    assert evidence.status == "partial"
    assert nutrients["energy"] == 120 and coverage["energy"].status == "known"
    assert nutrients["protein"] == 0.5
    assert coverage["protein"].whole_recipe_amount == 1
    assert coverage["protein"].status == "partial"
    assert coverage["protein"].covered_ingredient_line_ids == ("carrot",)
    assert coverage["protein"].unknown_ingredient_line_ids == ("rice",)
    assert nutrients["fiber"] == 0.5
    assert coverage["fiber"].covered_ingredient_line_ids == ("rice",)
    assert coverage["fiber"].unknown_ingredient_line_ids == ("carrot",)
    assert [
        (item.ingredient_line_id, item.reason, item.unknown_nutrients)
        for item in evidence.unknown_components
    ] == [
        ("rice", "missing_nutrients", ("protein",)),
        ("carrot", "missing_nutrients", ("fiber",)),
    ]


@pytest.mark.parametrize(
    ("mode", "amount", "status"),
    [("zero_nutrient", 0, "known"), ("missing_nutrient", None, "unavailable")],
)
def test_explicit_source_zero_is_distinct_from_missing_data(
    mode: str, amount: float | None, status: str
) -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config(mode)).lookup(request(), deadline=monotonic() + 10)
    )
    fiber = evidence.coverage[4]
    assert fiber.status == status
    assert fiber.whole_recipe_amount == amount
    assert (
        next((item.amount for item in evidence.nutrients if item.name == "fiber"), None) == amount
    )


def test_kilojoules_are_not_silently_reported_as_calories() -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config("kj_only")).lookup(request(), deadline=monotonic() + 10)
    )
    assert evidence.status == "partial"
    assert "energy" not in {item.name for item in evidence.nutrients}
    assert evidence.coverage[0].whole_recipe_amount is None
    assert evidence.unknown_components[0].unknown_nutrients == ("energy",)


@pytest.mark.parametrize("change", ["coverage", "unknown", "estimate", "notice"])
def test_estimate_contract_rejects_false_completeness_or_medical_certainty(change: str) -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config("missing_nutrient")).lookup(request(), deadline=monotonic() + 10)
    )
    data = json.loads(evidence.model_dump_json(by_alias=True))
    if change == "coverage":
        data["coverage"][0]["unknownIngredientLineIds"] = ["foreign"]
        data["coverage"][0]["status"] = "partial"
    elif change == "unknown":
        data["unknownComponents"] = []
    elif change == "estimate":
        data["isEstimate"] = False
    else:
        data["notice"] = "Medically verified exact nutrition."
    with pytest.raises(ValidationError):
        NutritionEvidence.model_validate_json(json.dumps(data))


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


def test_exact_message_and_session_byte_limits() -> None:
    async def check() -> None:
        async with open_usda(config("at_limit"), deadline=monotonic() + 10) as session:
            # Each request budget excludes the one-time initialization exchange.
            for _ in range(8):
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


def test_all_thirty_recipe_lines_are_attempted(tmp_path: Path) -> None:
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
    assert sum(message["method"] == "tools/call" for message in messages) == 30
    assert [item.reason for item in result.unknown_components] == ["food_unavailable"] * 30


def test_large_recipes_get_a_bounded_larger_lookup_budget_and_keep_the_caller_deadline() -> None:
    lines = tuple(ingredient(f"unknown{index}", line_id=f"line{index}") for index in range(25))
    tool = UsdaNutritionTool(config("slow_foods", timeout=0.5))
    result = asyncio.run(tool.lookup(request(*lines), deadline=monotonic() + 3))
    assert len(result.unknown_components) == 25
    with pytest.raises(RecipeToolError) as caught:
        asyncio.run(tool.lookup(request(*lines), deadline=monotonic() + 0.3))
    assert caught.value.code == FoundryFailure.TIMEOUT


def test_live_specialist_factory_selects_real_nutrition_not_fixtures() -> None:
    from test_orchestrator_gateway import coordination_settings

    settings = Settings.model_validate(
        coordination_settings().model_dump()
        | {
            "usda_api_key": "synthetic-private-key",
        }
    )
    specialist = RecipeSpecialistService.from_settings(settings)
    assert isinstance(specialist._nutrition, UsdaNutritionTool)
    assert specialist._recipes is None
    assert UsdaMcpConfig.from_settings(settings).server.name == "usda_mcp_server.py"


def test_legacy_padded_ndb_ids_are_not_reinterpreted_as_fdc_ids() -> None:
    with pytest.raises(RecipeToolError) as caught:
        asyncio.run(
            UsdaNutritionTool(config("leading_zero")).lookup(
                request(),
                deadline=monotonic() + 10,
            )
        )
    assert caught.value.code == FoundryFailure.INVALID_RESULT


def test_first_fuzzy_search_hit_is_never_assumed_to_be_the_requested_food() -> None:
    evidence = asyncio.run(
        UsdaNutritionTool(config("mismatch")).lookup(
            request(ingredient("carrot")),
            deadline=monotonic() + 10,
        )
    )
    assert evidence.status == "unavailable"
    assert not evidence.nutrients and not evidence.mappings
