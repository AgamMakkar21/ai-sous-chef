"""Fixed denominators and factual oracles, independent of subjective Foundry grades."""

import hashlib
import json
import math
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, StrictInt, ValidationError, model_validator
from sous_chef.domain.context import RequestContext
from sous_chef.domain.orchestrator import POLICY
from sous_chef.domain.recipes import DraftIngredient
from sous_chef.services.recipe_specialist import _inventory

from scripts.quality_capture import METRICS, ROOT, Case, EvidenceModel, Observation

DATASET = ROOT / "evaluations" / "recipe-quality-v1.json"
REQUIRED_CASES = {
    "normal-no-web",
    "insufficient-groceries",
    "unrealistic-time",
    "missing-nutrition",
    "nutrition-timeout",
    "conflicting-taste-nutrition",
    "current-public-information",
    "creative-within-constraints",
}
REQUIRED_ANCHORS = {
    "anchor-practical",
    "anchor-unsupported",
    "anchor-creative",
    "anchor-novelty-not-evidence",
}


class Thresholds(EvidenceModel):
    judge_minimum: Literal[4]
    required_pass_rate: float = Field(ge=1, le=1)
    minimum_pantry_utilization: float = Field(ge=0.5, le=0.5)
    maximum_human_judge_delta: Literal[1]


class Anchor(EvidenceModel):
    id: str
    query: str
    response: str
    context: str
    grounded_response: str
    minimum: int = Field(ge=1, le=5)
    maximum: int = Field(ge=1, le=5)


class Suite(EvidenceModel):
    version: Literal["asc56-recipe-quality-v1"]
    data_classification: Literal["synthetic-only"]
    thresholds: Thresholds
    defaults: RequestContext
    cases: tuple[Case, ...]
    rubrics: dict[str, str]
    scale: dict[str, str]
    anchors: tuple[Anchor, ...]

    @model_validator(mode="after")
    def fixed_coverage(self) -> "Suite":
        if (
            len(self.cases) != len(REQUIRED_CASES)
            or {case.id for case in self.cases} != REQUIRED_CASES
            or len(self.anchors) != len(REQUIRED_ANCHORS)
            or {anchor.id for anchor in self.anchors} != REQUIRED_ANCHORS
            or set(self.rubrics) != set(METRICS[2:])
            or set(self.scale) != {"1", "2", "3", "4", "5"}
            or self.defaults.history
            or self.defaults.learned_preferences
            or self.defaults.revision is not None
            or any(
                case.judge_metrics
                != (
                    ("relevance", "feasibility")
                    if case.id == "unrealistic-time"
                    else ()
                    if case.id == "nutrition-timeout"
                    else METRICS
                )
                for case in self.cases
            )
        ):
            raise ValueError("The fixed synthetic suite, rubric and anchor set must be complete.")
        return self


def load_suite(path: Path = DATASET) -> Suite:
    return Suite.model_validate_json(path.read_bytes())


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def suite_digest(suite: Suite) -> str:
    return digest(suite.model_dump(mode="json", by_alias=True))


class Check(EvidenceModel):
    status: Literal["pass", "fail", "unavailable", "not_applicable"]
    reason: str


def check(ok: bool, reason: str) -> Check:
    return Check(status="pass" if ok else "fail", reason=reason)


def factual_checks(suite: Suite, case: Case, observation: Observation) -> dict[str, Check]:
    trace = observation.trace
    result = observation.result
    recipe = result.recipe if result is not None else None
    status = result.status if result is not None else "error"
    tools = [event.name for event in trace.tools]
    completed = [event.name for event in trace.tools if event.status == "completed"]
    expected_timeout = (
        case.fault == "nutrition_timeout"
        and observation.error_code == "orchestrator_specialist_failed"
        and tools.count("usda") == 1
        and trace.tools[-1].status == "error"
        and trace.tools[-1].fault_injected
        and not trace.nutrition
        and result is None
    )
    expected_outcome = status in case.expected and (
        expected_timeout if status == "error" else observation.error_code is None
    )
    checks = {
        "expected_outcome": check(
            expected_outcome, "Match the preregistered response/fault outcome."
        ),
        "bounded_execution": check(
            trace.model_attempts <= POLICY.max_model_calls
            and len(trace.specialist_arguments) <= POLICY.max_specialist_calls
            and len(tools) + len(trace.specialist_arguments) <= POLICY.max_tool_calls
            and tools.count("web_iq") <= POLICY.max_web_calls,
            "Count observed invocations, including failed attempts.",
        ),
        "necessary_tools": check(
            (not tools if case.id == "unrealistic-time" else True)
            and (
                "web_iq" not in tools if case.web == "forbidden" else completed.count("web_iq") == 1
            )
            and ("foundry_iq" in completed and "usda" in completed if recipe is not None else True),
            "Ordinary cooking must not use Web IQ; current public requests must use it.",
        ),
        "no_fault_retry": check(
            not expected_timeout
            or (len(trace.specialist_arguments) == 1 and tools.count("usda") == 1),
            "An injected timeout must terminate without a specialist/tool retry.",
        ),
        "usage_attribution": check(
            expected_timeout or trace.model_attempts == len(trace.usage),
            "Missing invocation usage is unknown, never a free or zero-token call.",
        ),
    }
    if recipe is None:
        checks["structured_recipe"] = check(
            status != "candidate" and expected_outcome,
            "A clarification/controlled failure must not fabricate a recipe or nutrition.",
        )
        return checks

    checks["structured_recipe"] = check(
        status == "candidate", "Validate the production strict structured-recipe contract."
    )
    context = case.context(suite.defaults)
    skill = ("beginner", "intermediate", "advanced")
    checks["time_skill"] = check(
        recipe.total_minutes <= case.minutes
        and skill.index(recipe.skill) <= skill.index(context.explicit_preferences.cooking_skill),
        "Reported time and skill must respect explicit limits; practical timing is also judged.",
    )
    ingredients = tuple(
        DraftIngredient.model_validate(
            line.model_dump(exclude={"ingredient_id", "inventory_status", "grocery_ids"})
        )
        for line in recipe.ingredients
    )
    expected_inventory, expected_shopping = _inventory(ingredients, context)
    checks["inventory_facts"] = check(
        recipe.ingredients == expected_inventory
        and recipe.shopping_items == expected_shopping
        and (not case.require_shopping or bool(recipe.shopping_items)),
        "Reconcile every inventory and shortage assertion against the synthetic pantry.",
    )
    if case.measure_pantry_utilization:
        pantry = {row.name.casefold().strip() for row in context.groceries}
        used = {line.name.casefold().strip() for line in recipe.ingredients}
        checks["pantry_coverage"] = check(
            bool(pantry)
            and len(pantry & used) / len(pantry) >= suite.thresholds.minimum_pantry_utilization,
            "Use at least half the distinct supplied pantry foods; meal usefulness is also judged.",
        )
    sources = {
        item.source.source_id: item for grounding in trace.grounding for item in grounding.recipes
    }
    checks["source_facts"] = check(
        bool(recipe.source_claims)
        and all(
            source.source_id in sources and source == sources[source.source_id].source
            for source in recipe.sources
            if source.kind == "curated_recipe"
        )
        and all(
            claim.source_id in sources and claim.quote in sources[claim.source_id].snippet
            for claim in recipe.source_claims
        )
        and (
            recipe.web_grounding is not None and recipe.web_grounding in trace.web
            if case.web == "required"
            else recipe.web_grounding is None
        ),
        "Source identifiers, versions and exact quotes must have observed tool evidence.",
    )
    received = trace.nutrition[-1] if trace.nutrition else None
    requested = trace.nutrition_requests[-1] if trace.nutrition_requests else None
    checks["nutrition_provenance"] = check(
        received is not None
        and recipe.nutrition.model_dump() == received.model_dump()
        and requested is not None
        and requested.ingredients == ingredients
        and requested.servings == recipe.servings,
        "Nutrition must be the tool's evidence for these ingredients, quantities and servings.",
    )
    if case.fault == "missing_nutrition":
        checks["missing_nutrition_handling"] = check(
            recipe.nutrition.status == "unavailable"
            and not recipe.nutrition.nutrients
            and not recipe.nutrition.mappings
            and not recipe.nutrition.sources,
            "Unavailable nutrition is explicit, never invented or treated as zero.",
        )
    else:
        checks["nutrition_arithmetic"] = check(
            len(recipe.nutrition.coverage) == 6
            and recipe.nutrition.serving_basis == "per_serving"
            and all(
                item.whole_recipe_amount is not None
                and abs(
                    Decimal(str(nutrient.amount))
                    - (Decimal(str(item.whole_recipe_amount)) / recipe.servings).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
                <= Decimal("0.01")
                for nutrient in recipe.nutrition.nutrients
                for item in recipe.nutrition.coverage
                if item.name == nutrient.name
            )
            and recipe.nutrition.status == "known",
            "All six nutrient facts must be available and reconcile whole-recipe/serving amounts.",
        )
    return checks


def rows(suite: Suite, observations: list[Observation]) -> list[dict[str, str]]:
    by_id = {observation.case_id: observation for observation in observations}
    result: list[dict[str, str]] = []
    for case in suite.cases:
        observation = by_id.get(case.id)
        response = observation.result if observation is not None else None
        recipe = response.recipe if response is not None else None
        trace = observation.trace if observation is not None else None
        result.append(
            {
                "case_id": case.id,
                "query": case.context(suite.defaults).model_dump_json(by_alias=True),
                "response": response.model_dump_json(by_alias=True)
                if response is not None
                else json.dumps({"status": "error", "recipe": None}),
                "context": json.dumps(
                    {
                        "curated": [
                            g.model_dump(mode="json", by_alias=True) for g in trace.grounding
                        ]
                        if trace is not None
                        else [],
                        "nutrition": [
                            n.model_dump(mode="json", by_alias=True) for n in trace.nutrition
                        ]
                        if trace is not None
                        else [],
                        "web": [w.model_dump(mode="json", by_alias=True) for w in trace.web]
                        if trace is not None
                        else [],
                    }
                ),
                "grounded_response": json.dumps(
                    [claim.model_dump(mode="json", by_alias=True) for claim in recipe.source_claims]
                    if recipe is not None
                    else []
                ),
            }
        )
    for anchor in suite.anchors:
        result.append(
            anchor.model_dump(exclude={"minimum", "maximum", "id"}) | {"case_id": anchor.id}
        )
    return result


def review_template(suite: Suite, observations: list[Observation]) -> dict[str, Any]:
    return {
        "reviewer": None,
        "thresholds_approved": False,
        "suite_sha256": suite_digest(suite),
        "observations_sha256": digest([item.model_dump(mode="json") for item in observations]),
        "scores": {
            **{case.id: dict.fromkeys(case.judge_metrics) for case in suite.cases},
            **{anchor.id: dict.fromkeys(METRICS) for anchor in suite.anchors},
        },
    }


def parse_grades(
    suite: Suite, outputs: list[dict[str, Any]], expected_rows: list[dict[str, str]]
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[str]]:
    required = REQUIRED_CASES | REQUIRED_ANCHORS
    submitted = {row["case_id"]: row for row in expected_rows}
    grades: dict[str, dict[str, dict[str, Any]]] = {}
    problems: list[str] = []
    for output in outputs:
        if not isinstance(output, dict):
            problems.append("invalid_output")
            continue
        source = output.get("datasource_item")
        case_id = source.get("case_id") if isinstance(source, dict) else None
        if not isinstance(case_id, str):
            problems.append("invalid_output")
            continue
        if case_id not in required or case_id in grades:
            problems.append("unexpected_or_duplicate_output")
            if case_id in grades:
                grades[case_id] = {}
            continue
        grades[case_id] = {}
        if not isinstance(source, dict) or any(
            source.get(key) != value for key, value in submitted[case_id].items()
        ):
            problems.append("mismatched_input_evidence")
            continue
        if output.get("status") not in ("pass", "fail", "completed"):
            problems.append("incomplete_output")
            continue
        seen: set[str] = set()
        results = output.get("results")
        if not isinstance(results, list):
            problems.append("invalid_output")
            continue
        for grade in results:
            if not isinstance(grade, dict):
                problems.append("invalid_grade")
                continue
            name, score = grade.get("name"), grade.get("score")
            if not isinstance(name, str) or name not in METRICS or name in seen:
                problems.append("unexpected_or_duplicate_grade")
                if name in grades[case_id]:
                    del grades[case_id][name]
                continue
            seen.add(name)
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
                or not 1 <= score <= 5
                or score != int(score)
                or grade.get("status") not in (None, "completed", "pass", "fail")
                or grade.get("error")
            ):
                problems.append("unavailable_grade")
                continue
            grades[case_id][name] = {
                "score": score,
                "passed": score >= suite.thresholds.judge_minimum,
                "reason": grade.get("reason"),
            }
    if set(grades) != required:
        problems.append("missing_outputs")
    return grades, problems


class HumanReview(EvidenceModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reviewer: str | None
    thresholds_approved: bool
    suite_sha256: str
    observations_sha256: str
    scores: dict[str, dict[str, StrictInt | None]]


def report(
    suite: Suite,
    observations: list[Observation],
    *,
    mode: str,
    cloud_status: str = "not_run",
    outputs: list[dict[str, Any]] | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    grades, problems = parse_grades(suite, outputs or [], rows(suite, observations))
    try:
        review = HumanReview.model_validate(review).model_dump() if review is not None else None
    except ValidationError:
        review = None
    identifiers = [item.case_id for item in observations]
    valid_observations = (
        len(identifiers) == len(REQUIRED_CASES) and set(identifiers) == REQUIRED_CASES
    )
    by_id = {item.case_id: item for item in observations}
    case_reports: list[dict[str, Any]] = []
    for case in suite.cases:
        observation = by_id.get(case.id)
        checks = (
            factual_checks(suite, case, observation)
            if observation is not None
            else {"coverage": Check(status="fail", reason="Required observation missing.")}
        )
        recipe = observation.result.recipe if observation and observation.result else None
        case_reports.append(
            {
                "id": case.id,
                "expected": list(case.expected),
                "outcome": observation.result.status
                if observation and observation.result
                else observation.error_code
                if observation
                else "missing",
                "deterministic_passed": all(value.status == "pass" for value in checks.values()),
                "checks": {name: value.model_dump() for name, value in checks.items()},
                "grades": {name: grades.get(case.id, {}).get(name) for name in case.judge_metrics},
                "adaptations": list(recipe.adaptations) if recipe else [],
                "source_claims": [
                    claim.model_dump(mode="json", by_alias=True) for claim in recipe.source_claims
                ]
                if recipe
                else [],
                "nutrition_available": recipe is not None and recipe.nutrition.status == "known",
                "unsupported_claim_detection": (
                    "exact source claims checked; broader prose needs review"
                ),
            }
        )
    aggregates: dict[str, Any] = {}
    for metric in METRICS:
        expected = [case.id for case in suite.cases if metric in case.judge_metrics]
        scored = [grades[id][metric] for id in expected if metric in grades.get(id, {})]
        passed = sum(item["passed"] for item in scored)
        aggregates[metric] = {
            "required": len(expected),
            "scored": len(scored),
            "passed": passed,
            "pass_rate": passed / len(expected) if expected else None,
            "mean": sum(item["score"] for item in scored) / len(expected)
            if len(scored) == len(expected) and expected
            else None,
        }
    template = review_template(suite, observations)
    human_ok = bool(
        review
        and isinstance(review.get("reviewer"), str)
        and review["reviewer"].strip()
        and review.get("thresholds_approved") is True
        and review.get("suite_sha256") == template["suite_sha256"]
        and review.get("observations_sha256") == template["observations_sha256"]
        and set(review["scores"]) == set(template["scores"])
        and all(
            set(review["scores"][id]) == set(scores) for id, scores in template["scores"].items()
        )
    )
    disagreements: list[str] = []
    for id, expected_scores in template["scores"].items():
        for metric in expected_scores:
            human = (review or {}).get("scores", {}).get(id, {}).get(metric)
            judge = grades.get(id, {}).get(metric, {}).get("score")
            if (
                isinstance(human, bool)
                or not isinstance(human, int)
                or not 1 <= human <= 5
                or judge is None
                or abs(human - judge) > suite.thresholds.maximum_human_judge_delta
            ):
                human_ok = False
                disagreements.append(f"{id}:{metric}")
    anchors_ok = (
        all(
            anchor.minimum
            <= grades.get(anchor.id, {}).get(metric, {}).get("score", -1)
            <= anchor.maximum
            and anchor.minimum
            <= (review or {}).get("scores", {}).get(anchor.id, {}).get(metric, -1)
            <= anchor.maximum
            for anchor in suite.anchors
            for metric in METRICS
        )
        if human_ok
        else False
    )
    deterministic_passed = sum(item["deterministic_passed"] for item in case_reports)
    quality_passed = (
        mode == "live"
        and cloud_status == "completed"
        and valid_observations
        and not problems
        and deterministic_passed == len(suite.cases)
        and all(
            item["pass_rate"] == suite.thresholds.required_pass_rate for item in aggregates.values()
        )
        and human_ok
        and anchors_ok
    )
    return {
        "suite": suite.version,
        "suite_sha256": suite_digest(suite),
        "mode": mode,
        "measurement": "offline harness replay; NOT a live model-quality baseline"
        if mode == "replay"
        else "synthetic live observations and Foundry judges",
        "thresholds": suite.thresholds.model_dump(),
        "threshold_review": "approved in matching human review" if human_ok else "pending",
        "cloud_status": cloud_status,
        "quality_passed": quality_passed,
        "required_cases": len(suite.cases),
        "captured_cases": len(set(identifiers) & REQUIRED_CASES),
        "valid_observation_coverage": valid_observations,
        "deterministic_passed": deterministic_passed if valid_observations else 0,
        "nutrition_fact_coverage": {
            "known": sum(item["nutrition_available"] for item in case_reports),
            "required_cases": len(suite.cases),
            "note": "Transparent missing-data handling is not an available nutrient fact.",
        },
        "quality": aggregates,
        "calibration": {
            "human_review_complete": human_ok,
            "anchors_passed": anchors_ok,
            "unresolved": disagreements,
        },
        "output_problems": sorted(set(problems)),
        "cases": case_reports,
        "weak_cases": [
            item["id"]
            for item in case_reports
            if not item["deterministic_passed"]
            or any(grade is None or not grade["passed"] for grade in item["grades"].values())
        ],
        "hard_safety_gate": "Separate mandatory command: python -m scripts.evaluate_safety",
    }
