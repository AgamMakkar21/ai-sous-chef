import argparse
import asyncio
import copy
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from pydantic import HttpUrl, ValidationError
from sous_chef.domain.recipes import RecipeSourceClaim
from sous_chef.settings import ConfigurationError, Settings

from scripts import evaluate_quality, quality_capture
from scripts.quality_capture import METRICS, Observation, ToolEvent, capture
from scripts.quality_evidence import (
    Suite,
    digest,
    factual_checks,
    load_suite,
    report,
    review_template,
    rows,
    suite_digest,
)


@pytest.fixture(scope="module")
def suite() -> Suite:
    return load_suite()


@pytest.fixture(scope="module")
def observations(suite: Suite) -> list[Observation]:
    async def run() -> list[Observation]:
        return [await capture(case, suite.defaults, None) for case in suite.cases]

    return asyncio.run(run())


def judge_outputs(suite: Suite, observations: list[Observation]) -> list[dict[str, Any]]:
    """Controlled judge responses test aggregation, not live quality."""
    data = {row["case_id"]: row for row in rows(suite, observations)}
    return [
        {
            "id": f"output-{id}",
            "datasource_item": data[id],
            "status": "pass" if score >= 4 else "fail",
            "results": [
                {"name": metric, "score": score, "passed": score >= 4, "reason": "Synthetic grade"}
                for metric in METRICS
            ],
        }
        for id, score in (
            [(case.id, 4) for case in suite.cases]
            + [(anchor.id, anchor.minimum) for anchor in suite.anchors]
        )
    ]


def matching_review(suite: Suite, observations: list[Observation]) -> dict[str, Any]:
    review = review_template(suite, observations)
    review["reviewer"] = "synthetic-reviewer"
    review["thresholds_approved"] = True
    expected = {
        item["datasource_item"]["case_id"]: {
            grade["name"]: grade["score"] for grade in item["results"]
        }
        for item in judge_outputs(suite, observations)
    }
    for id, scores in review["scores"].items():
        for metric in scores:
            scores[metric] = expected[id][metric]
    return review


def scored_report(
    suite: Suite, observations: list[Observation], **overrides: Any
) -> dict[str, Any]:
    arguments = {
        "mode": "live",
        "cloud_status": "completed",
        "outputs": judge_outputs(suite, observations),
        "review": matching_review(suite, observations),
    } | overrides
    return report(suite, observations, **arguments)


def test_replay_exercises_production_orchestration_and_never_claims_live_quality(
    suite: Suite,
    observations: list[Observation],
) -> None:
    result = report(suite, observations, mode="replay")
    assert result["deterministic_passed"] == result["required_cases"] == 8
    assert not result["quality_passed"]
    assert result["cloud_status"] == "not_run"
    assert result["nutrition_fact_coverage"]["known"] == 5
    assert result["nutrition_fact_coverage"]["required_cases"] == 8
    assert all(
        value["scored"] == 0 and value["mean"] is None for value in result["quality"].values()
    )
    pantry = observations[1].result
    assert pantry is not None and pantry.recipe is not None
    rice = next(item for item in pantry.recipe.shopping_items if item.ingredient_line_id == "rice")
    assert rice.quantity == 80
    assert "NOT a live" in result["measurement"]


@pytest.mark.parametrize("mode", ["phased", "combined"])
@pytest.mark.parametrize("case_id", ["normal-no-web", "current-public-information"])
def test_capture_preserves_configured_grounding_mode_and_single_combined_usage(
    suite: Suite,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["phased", "combined"],
    case_id: str,
) -> None:
    dependencies = quality_capture.dependencies
    monkeypatch.setattr(
        quality_capture, "dependencies", lambda case, settings: dependencies(case, None)
    )
    case = next(case for case in suite.cases if case.id == case_id)
    observation = asyncio.run(
        capture(case, suite.defaults, Settings.model_construct(foundry_recipe_nutrition_mode=mode))
    )
    assert all(item.status == "pass" for item in factual_checks(suite, case, observation).values())
    expected_models = 4 if mode == "phased" and case.web == "required" else 3
    assert observation.trace.model_attempts == len(observation.trace.usage) == expected_models
    assert len(observation.trace.web) == int(case.web == "required")


def test_timeout_and_unavailable_nutrition_are_expected_handling_not_available_facts(
    suite: Suite,
    observations: list[Observation],
) -> None:
    missing, timeout = observations[3:5]
    assert missing.result is not None and missing.result.recipe is not None
    assert missing.result.recipe.nutrition.status == "unavailable"
    assert not missing.result.recipe.nutrition.nutrients
    assert timeout.result is None
    assert timeout.error_code == "orchestrator_specialist_failed"
    assert [event.name for event in timeout.trace.tools] == ["foundry_iq", "usda"]
    assert timeout.trace.tools[-1].status == "error"
    assert len(timeout.trace.specialist_arguments) == 1
    assert suite.cases[4].judge_metrics == ()


def test_complete_calibrated_results_can_pass(
    suite: Suite,
    observations: list[Observation],
) -> None:
    result = scored_report(suite, observations)
    assert result["quality_passed"]
    assert result["calibration"]["anchors_passed"]
    assert all(value["pass_rate"] == 1 for value in result["quality"].values())
    assert not scored_report(suite, observations, mode="replay")["quality_passed"]


@pytest.mark.parametrize("failure", ["missing-case", "duplicate-case", "foreign-case"])
def test_observation_coverage_fails_closed(
    suite: Suite,
    observations: list[Observation],
    failure: str,
) -> None:
    selected = copy.deepcopy(observations)
    if failure == "missing-case":
        selected.pop()
    elif failure == "duplicate-case":
        selected.append(selected[0])
    else:
        selected[-1].case_id = "foreign-case"
    result = scored_report(suite, selected)
    assert not result["quality_passed"]
    assert result["deterministic_passed"] == 0
    assert result["required_cases"] == 8


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "duplicate",
        "extra",
        "skipped",
        "duplicate-grade",
        "wrong-input",
        "malformed-grade",
    ],
)
def test_cloud_coverage_cannot_raise_scores_by_dropping_or_repeating_results(
    suite: Suite,
    observations: list[Observation],
    failure: str,
) -> None:
    outputs = judge_outputs(suite, observations)
    if failure == "missing":
        outputs.pop(0)
    elif failure == "duplicate":
        outputs.append(outputs[0])
    elif failure == "extra":
        outputs.append({"datasource_item": {"case_id": "unexpected"}})
    elif failure == "skipped":
        outputs[0]["status"] = "skipped"
    elif failure == "wrong-input":
        outputs[0]["datasource_item"]["response"] = "A different candidate."
    elif failure == "malformed-grade":
        outputs[0]["results"][0]["name"] = []
    else:
        outputs[0]["results"].append(outputs[0]["results"][0])
    result = scored_report(suite, observations, outputs=outputs)
    assert not result["quality_passed"]
    assert result["quality"]["relevance"]["required"] == 7
    assert result["quality"]["relevance"]["mean"] is None or failure == "extra"
    assert result["output_problems"]


@pytest.mark.parametrize("score", [None, True, float("nan"), float("inf"), 0, 6, 4.2])
def test_invalid_or_unavailable_grades_do_not_count_as_success(
    suite: Suite,
    observations: list[Observation],
    score: Any,
) -> None:
    outputs = judge_outputs(suite, observations)
    outputs[0]["results"][0]["score"] = score
    outputs[0]["results"][0]["passed"] = True
    result = scored_report(suite, observations, outputs=outputs)
    assert not result["quality_passed"]
    assert result["quality"]["relevance"]["scored"] == 6
    assert result["quality"]["relevance"]["pass_rate"] == 6 / 7
    assert result["quality"]["relevance"]["mean"] is None


def test_local_preregistered_threshold_overrules_service_pass_flag(
    suite: Suite,
    observations: list[Observation],
) -> None:
    outputs = judge_outputs(suite, observations)
    outputs[0]["results"][0].update(score=3, passed=True)
    result = scored_report(suite, observations, outputs=outputs)
    assert not result["quality_passed"]
    assert result["cases"][0]["grades"]["relevance"]["passed"] is False
    assert not scored_report(suite, observations, cloud_status="failed")["quality_passed"]


@pytest.mark.parametrize(
    "change",
    ["absent", "unapproved", "wrong-evidence", "disagreement", "missing-score", "malformed"],
)
def test_human_calibration_requires_matching_evidence_and_all_scores(
    suite: Suite,
    observations: list[Observation],
    change: str,
) -> None:
    review = matching_review(suite, observations)
    if change == "unapproved":
        review["thresholds_approved"] = False
    elif change == "wrong-evidence":
        review["observations_sha256"] = "wrong"
    elif change == "disagreement":
        review["scores"]["normal-no-web"]["feasibility"] = 1
    elif change == "missing-score":
        del review["scores"]["normal-no-web"]["feasibility"]
    elif change == "malformed":
        review["scores"] = "not-a-score-map"
    result = scored_report(suite, observations, review=None if change == "absent" else review)
    assert not result["quality_passed"]
    assert not result["calibration"]["human_review_complete"]


def test_judges_must_distinguish_adaptation_from_unsupported_claims_on_anchors(
    suite: Suite,
    observations: list[Observation],
) -> None:
    outputs = judge_outputs(suite, observations)
    outputs[-1]["results"][0]["score"] = 5
    review = matching_review(suite, observations)
    review["scores"][suite.anchors[-1].id]["relevance"] = 5
    result = scored_report(suite, observations, outputs=outputs, review=review)
    assert result["calibration"]["human_review_complete"]
    assert not result["calibration"]["anchors_passed"]
    assert not result["quality_passed"]


@pytest.mark.parametrize("corruption", ["time", "nutrition", "source", "inventory", "web"])
def test_subjective_scores_cannot_compensate_for_factual_errors(
    suite: Suite,
    observations: list[Observation],
    corruption: str,
) -> None:
    selected = copy.deepcopy(observations)
    first = selected[0]
    assert first.result is not None and first.result.recipe is not None
    recipe = first.result.recipe
    if corruption == "time":
        recipe = recipe.model_copy(update={"total_minutes": 31})
    elif corruption == "nutrition":
        nutrition = recipe.nutrition.model_copy(
            update={
                "nutrients": (
                    recipe.nutrition.nutrients[0].model_copy(update={"amount": 999}),
                    *recipe.nutrition.nutrients[1:],
                )
            }
        )
        recipe = recipe.model_copy(update={"nutrition": nutrition})
    elif corruption == "source":
        recipe = recipe.model_copy(
            update={
                "source_claims": (
                    RecipeSourceClaim(
                        source_id=recipe.source_ids[0], quote="A fabricated source claim."
                    ),
                )
            }
        )
    elif corruption == "inventory":
        recipe = recipe.model_copy(
            update={
                "ingredients": (
                    recipe.ingredients[0].model_copy(update={"inventory_status": "missing"}),
                    *recipe.ingredients[1:],
                )
            }
        )
    else:
        first.trace.tools.append(ToolEvent(name="web_iq", status="completed"))
    first.result = first.result.model_copy(update={"recipe": recipe})
    result = scored_report(suite, selected)
    assert not result["quality_passed"]
    assert not result["cases"][0]["deterministic_passed"]


def test_web_evidence_must_be_attached_not_just_called(
    suite: Suite,
    observations: list[Observation],
) -> None:
    selected = copy.deepcopy(observations[6])
    assert selected.result is not None and selected.result.recipe is not None
    selected.result = selected.result.model_copy(
        update={"recipe": selected.result.recipe.model_copy(update={"web_grounding": None})}
    )
    assert factual_checks(suite, suite.cases[6], selected)["source_facts"].status == "fail"


@pytest.mark.parametrize("change", ["case", "metrics", "threshold", "anchor"])
def test_fixed_suite_rejects_removed_requirements_or_relaxed_thresholds(
    suite: Suite,
    change: str,
) -> None:
    data = suite.model_dump(mode="json", by_alias=True)
    if change == "case":
        data["cases"].pop()
    elif change == "metrics":
        data["cases"][0]["judge_metrics"] = []
    elif change == "threshold":
        data["thresholds"]["judge_minimum"] = 3
    else:
        data["anchors"].pop()
    with pytest.raises(ValidationError):
        Suite.model_validate_json(json.dumps(data))


def test_export_has_exact_case_ids_and_separates_quotes_from_adaptations(
    suite: Suite,
    observations: list[Observation],
) -> None:
    data = rows(suite, observations)
    assert len(data) == 12 and len({row["case_id"] for row in data}) == 12
    source_claims = json.loads(data[0]["grounded_response"])
    response = json.loads(data[0]["response"])
    assert source_claims == response["recipe"]["sourceClaims"]
    assert response["recipe"]["adaptations"]
    assert response["recipe"]["adaptations"] != source_claims
    assert "nutrition" in json.loads(data[0]["context"])


def test_costs_remain_unknown_when_live_billing_is_not_reported(
    observations: list[Observation],
) -> None:
    assert evaluate_quality.costs("replay", observations, {})["total_usd"] == 0
    cost = evaluate_quality.costs("live", observations, {})
    assert cost["total_usd"] is None and cost["status"] == "unknown"
    assert cost["target_input_tokens_observed"] > 0
    assert cost["judge_usage"] is None


def test_cloud_approval_is_checked_before_settings_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(evaluate_quality.APPROVAL_VARIABLE, raising=False)

    def forbidden() -> Any:
        pytest.fail("No settings or credential access is allowed without approval.")

    monkeypatch.setattr(evaluate_quality, "load_settings", forbidden)
    with pytest.raises(ConfigurationError, match="approval"):
        evaluate_quality.live_settings()


def test_offline_command_persists_evidence_and_resume_never_recaptures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observations: list[Observation],
) -> None:
    by_id = {observation.case_id: observation for observation in observations}

    async def replay(case: Any, defaults: Any, settings: Any) -> Observation:
        assert settings is None
        return by_id[case.id]

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("The offline command must never access cloud credentials or clients.")

    monkeypatch.setattr(evaluate_quality, "capture", replay)
    monkeypatch.setattr(evaluate_quality, "build_credential", forbidden)
    monkeypatch.setattr(evaluate_quality, "AIProjectClient", forbidden)
    args = argparse.Namespace(
        output=tmp_path / "run",
        live=False,
        resume=False,
        judge_deployment="",
        review=None,
        wait_seconds=1,
        report_only=False,
    )
    assert asyncio.run(evaluate_quality.execute(args)) == 0
    for name in (
        "report.json",
        "state.json",
        "observations.json",
        "review-template.json",
        "dataset.json",
        "suite.json",
    ):
        assert (args.output / name).is_file()
    summary = json.loads((args.output / "report.json").read_text())
    assert not summary["quality_passed"]
    monkeypatch.setattr(evaluate_quality, "capture", forbidden)
    args.resume = True
    assert asyncio.run(evaluate_quality.execute(args)) == 0
    args.report_only = True
    assert asyncio.run(evaluate_quality.execute(args)) == 0
    data = json.loads((args.output / "observations.json").read_text())
    data.pop()
    (args.output / "observations.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="submitted evidence"):
        asyncio.run(evaluate_quality.execute(args))


def test_saved_live_evidence_resumes_after_transient_error_and_reviews_without_cloud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suite: Suite,
    observations: list[Observation],
) -> None:
    settings = Settings.model_construct(
        foundry_project_endpoint=HttpUrl("https://project.example.invalid/api/projects/test")
    )
    serialized = [item.model_dump(mode="json") for item in observations]
    state = {
        "mode": "live",
        "status": "completed",
        "suite_sha256": suite_digest(suite),
        "versions": evaluate_quality.source_versions()
        | {"agents": evaluate_quality.configured_agents(settings)},
        "observations_sha256": digest(serialized),
        "project_sha256": digest(str(settings.foundry_project_endpoint)),
        "eval_id": "eval-synthetic",
        "run_id": "run-synthetic",
        "runner_error": "evaluation_execution_failed",
    }
    outputs = judge_outputs(suite, observations)
    review_path = tmp_path / "review.json"
    for name, value in (
        ("state.json", state),
        ("observations.json", serialized),
        ("outputs.json", outputs),
        ("review.json", matching_review(suite, observations)),
    ):
        evaluate_quality.write(tmp_path / name, value)
    args = argparse.Namespace(
        output=tmp_path,
        live=False,
        resume=True,
        report_only=True,
        judge_deployment="",
        review=review_path,
        wait_seconds=1,
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Saved evidence must not be recaptured or resubmitted.")

    monkeypatch.setattr(evaluate_quality, "capture", forbidden)
    monkeypatch.setattr(evaluate_quality, "submit", forbidden)
    monkeypatch.setattr(evaluate_quality, "build_credential", forbidden)
    monkeypatch.setattr(evaluate_quality, "AIProjectClient", forbidden)
    assert asyncio.run(evaluate_quality.execute(args)) == 2
    monkeypatch.setattr(evaluate_quality, "live_settings", lambda: settings)
    monkeypatch.setattr(evaluate_quality, "build_credential", lambda settings: object())
    monkeypatch.setattr(
        evaluate_quality,
        "AIProjectClient",
        lambda **kwargs: nullcontext(
            SimpleNamespace(get_openai_client=lambda **kwargs: nullcontext(object()))
        ),
    )
    monkeypatch.setattr(evaluate_quality, "collect", lambda *args, **kwargs: outputs)
    args.live, args.report_only = True, False
    assert asyncio.run(evaluate_quality.execute(args)) == 0
    assert "runner_error" not in json.loads((tmp_path / "state.json").read_text())

    args.live, args.report_only = False, True
    monkeypatch.setattr(evaluate_quality, "build_credential", forbidden)
    monkeypatch.setattr(evaluate_quality, "AIProjectClient", forbidden)
    review_path.write_text("{", encoding="utf-8")
    assert asyncio.run(evaluate_quality.execute(args)) == 2
    result = json.loads((tmp_path / "report.json").read_text())
    assert result["review_error"] == "invalid_human_review"
    assert not result["quality_passed"]
    evaluate_quality.write(review_path, matching_review(suite, observations))
    assert asyncio.run(evaluate_quality.execute(args)) == 0
