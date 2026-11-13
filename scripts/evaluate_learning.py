"""Bounded offline preference experiment; never loads cloud settings or live user data."""

import argparse
import json
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sous_chef.domain.context import MIN_LEARNED_CONFIDENCE, MIN_LEARNED_COOKED_SUPPORT
from sous_chef.orchestrator_definition import ORCHESTRATOR_MODEL
from sous_chef.recipe_definition import RECIPE_MODEL

from scripts.evaluate_quality import source_versions
from scripts.quality_capture import ROOT
from scripts.quality_evidence import digest

FIXTURE = ROOT / "evaluations" / "preference-learning-v1.json"
TEST = "tests/integration/test_learning_evaluation.py::test_longitudinal_case"
REQUIRED = frozenset(
    (
        "repeated-citrus",
        "single-citrus",
        "ambiguous-note",
        "contradictory-feedback",
        "revised-feedback",
        "duplicate-events",
        "out-of-order",
        "explicit-allergy",
        "explicit-restriction",
        "explicit-dislike",
    )
)


class FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Step(FixtureModel):
    history: str
    revision: int = Field(default=1, ge=1)
    note: str
    signal: Literal["support", "oppose", "none"]
    day: int = Field(ge=1, le=29)
    support: int = Field(ge=0)
    opposition: int = Field(ge=0)
    confidence: float | None = Field(ge=0, le=1)
    included: bool


class Case(FixtureModel):
    id: str
    value: str = "lime"
    heldout_likes: bool | None
    final_explicit: dict[str, list[str]] = Field(default_factory=dict)
    extractions: int = Field(ge=1)
    expected_evidence: tuple[str, ...]
    steps: tuple[Step, ...] = Field(min_length=1, max_length=6)


class Suite(FixtureModel):
    version: Literal["asc57-preference-learning-v1"]
    data_classification: Literal["synthetic-only"]
    now: AwareDatetime
    cases: tuple[Case, ...]


def load_suite() -> Suite:
    suite = Suite.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    if len(suite.cases) != len(REQUIRED) or {case.id for case in suite.cases} != REQUIRED:
        raise ValueError("The fixed preference experiment requires every case exactly once.")
    return suite


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def report(suite: Suite, observations: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [row["case_id"] for row in observations]
    complete = len(ids) == len(REQUIRED) and set(ids) == REQUIRED
    rows = observations if complete else []
    checkpoints = sum(len(case.steps) for case in suite.cases)
    evidence = sum(len(case.expected_evidence) for case in suite.cases)
    correct = sum(row["evidence"]["correct"] for row in rows)
    emitted = sum(row["evidence"]["emitted"] for row in rows)
    labels = sum(case.heldout_likes is not None for case in suite.cases)
    scored = [row for row in rows if row["brier"] is not None]
    aligned: dict[str, Any] = {}
    for arm in ("explicit_only", "learned_enabled"):
        measured = [row["arms"][arm]["aligned"] for row in rows]
        measured = [value for value in measured if value is not None]
        aligned[arm] = {
            "aligned": sum(measured),
            "scored": len(measured),
            "required": labels,
            "rate": ratio(sum(measured), labels) if len(measured) == labels else None,
        }
    return {
        "suite": suite.version,
        "fixture_sha256": digest(suite.model_dump(mode="json")),
        "measurement": "Synthetic fixed-response replay; NOT a live model-quality measurement.",
        "deterministic_passed": complete and all(row["passed"] for row in rows),
        "live_quality_measured": False,
        "policy_review": "pending independent PR review",
        "policy": {
            "minimum_independent_cooked_support": MIN_LEARNED_COOKED_SUPPORT,
            "minimum_confidence": MIN_LEARNED_CONFIDENCE,
            "evidence_window_days": 180,
        },
        "sample": {
            "sequences": len(suite.cases),
            "deliveries": checkpoints,
            "observed_sequences": len(rows),
            "matched_recommendation_pairs": len(rows),
            "heldout_taste_labels": labels,
        },
        "evidence_precision": {
            "correct": correct,
            "emitted": emitted,
            "expected": evidence,
            "precision": ratio(correct, emitted),
            "recall": ratio(correct, evidence) if complete else None,
            "scope": "verified current ledger assertions, not model extraction accuracy",
        },
        "count_correctness": {
            "correct": sum(row["correct_checkpoints"] for row in rows),
            "required": checkpoints,
        },
        "explicit_precedence": {
            "passed": sum(row["precedence"] is True for row in rows),
            "required": sum(bool(case.final_explicit) for case in suite.cases),
        },
        "context_inclusion": {
            "explicit_only": 0 if complete else None,
            "learned_enabled": sum(row["included"] for row in rows) if complete else None,
            "expected": sum(case.steps[-1].included for case in suite.cases),
            "correct_checkpoints": sum(row["correct_inclusion"] for row in rows),
            "required_checkpoints": checkpoints,
        },
        "confidence_calibration": {
            "explicit_only_brier": None,
            "learned_enabled_brier": sum(row["brier"] for row in scored) / labels
            if len(scored) == labels
            else None,
            "scored": len(scored),
            "required": labels,
            "scope": "raw ledger confidence vs hand-authored heldout synthetic labels; "
            "not a calibrated population probability; explicit-only emits no confidence",
        },
        "recommendation_alignment": aligned,
        "recommendation_alignment_delta": aligned["learned_enabled"]["rate"]
        - aligned["explicit_only"]["rate"]
        if all(aligned[arm]["rate"] is not None for arm in aligned)
        else None,
        "observations": observations,
    }


class Capture:
    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call":
            for name, value in report.user_properties:
                if name == "learning_observation" and isinstance(value, str):
                    observation = json.loads(value)
                    observation["passed"] = report.passed
                    self.observations.append(observation)


def execute(output: Path) -> bool:
    suite = load_suite()
    versions = source_versions()
    versions["extraction_prompt_sha256"] = digest(
        (ROOT / "worker" / "extraction.py").read_text(encoding="utf-8")
    )
    versions["evaluation_test_sha256"] = digest(
        (ROOT / "tests" / "integration" / "test_learning_evaluation.py").read_text(encoding="utf-8")
    )
    versions["synthetic_helpers_sha256"] = {
        name: digest((ROOT / "tests" / "unit" / name).read_text(encoding="utf-8"))
        for name in (
            "test_cosmos_repository.py",
            "test_learning.py",
            "test_preference_extraction.py",
            "test_recipe_safety.py",
        )
    }
    versions["model_ids"] = [ORCHESTRATOR_MODEL, RECIPE_MODEL, "gpt-5.1-nano"]
    versions["live_model_versions"] = None
    capture = Capture()
    status = pytest.main(
        [
            "-q",
            "--tb=no",
            "-o",
            "addopts=",
            "-o",
            "pythonpath=backend/src tests/unit tests/integration",
            *(f"{TEST}[{case.id}]" for case in suite.cases),
        ],
        plugins=[capture],
    )
    result = report(suite, capture.observations)
    result["deterministic_passed"] &= status == pytest.ExitCode.OK
    result["versions"] = versions
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return bool(result["deterministic_passed"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts" / "learning-evaluation" / "report.json"
    )
    arguments = parser.parse_args()
    return 0 if execute(arguments.output) else 1


if __name__ == "__main__":
    raise SystemExit(main())
