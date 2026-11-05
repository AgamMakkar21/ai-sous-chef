"""Offline, fixed-suite hard gate. Never imports or configures a live model judge."""

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Self
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sous_chef.domain.authorization import AuthenticatedUser
from sous_chef.domain.food_safety import SafetyProfile
from sous_chef.domain.ingredient_catalog import CATALOG_VERSION
from sous_chef.domain.recipe_safety import POLICY_VERSION
from sous_chef.domain.recipes import StructuredRecipe

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "fixtures" / "hard-safety-v1.json"
SCOPES = {
    "peanut-oil",
    "hidden-allergens",
    "vegetarian-meat-stock",
    "explicit-versus-learned",
    "allergy-override-prompts",
    "malicious-retrieval",
    "unsafe-instructions",
    "unknown-ingredients",
    "cross-user-access",
    "positive-controls",
}
NegativeControl = Literal["unsafe-candidate", "ownership-bypass"]


class Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    scope: str
    expected_outcome: str = Field(min_length=1, max_length=300)
    tests: tuple[str, ...] = Field(min_length=1)


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    suite_id: Literal["asc55-hard-safety"]
    version: Literal[1]
    fixtures: tuple[Fixture, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def fixed_required_set(self) -> Self:
        identifiers = [fixture.id for fixture in self.fixtures]
        nodes = [node for fixture in self.fixtures for node in fixture.tests]
        if len(set(identifiers)) != len(identifiers) or len(set(nodes)) != len(nodes):
            raise ValueError("Required fixtures and tests must be unique.")
        if {fixture.scope for fixture in self.fixtures} != SCOPES:
            raise ValueError("Every required scope and the positive controls must be present.")
        for node in nodes:
            path, separator, test = node.partition("::")
            if (
                not separator
                or not test.startswith("test_")
                or not path.startswith(("tests/unit/test_", "tests/integration/test_"))
                or not path.endswith(".py")
                or ".." in Path(path).parts
                or "\n" in node
            ):
                raise ValueError("Required tests must be explicit local pytest node IDs.")
        return self


class HardGate:
    def __init__(self, suite: Suite) -> None:
        self.suite = suite
        self.required = {node for fixture in suite.fixtures for node in fixture.tests}
        self.collected: list[str] = []
        self.reports: dict[str, dict[str, list[str]]] = {}

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.nodeid in self.required:
            outcome = "xfail_or_xpass" if hasattr(report, "wasxfail") else report.outcome
            phases = self.reports.setdefault(report.nodeid, {})
            phases.setdefault(report.when, []).append(outcome)

    def outcome(self, node: str) -> str:
        if self.collected.count(node) != 1:
            return "missing_or_duplicate"
        phases = self.reports.get(node, {})
        if any("skipped" in results for results in phases.values()):
            return "skipped"
        if any("xfail_or_xpass" in results for results in phases.values()):
            return "xfail_or_xpass"
        if any(results != ["passed"] for phase, results in phases.items() if phase != "call"):
            return "error"
        if phases.get("call") == ["failed"]:
            return "failed"
        if phases == {"setup": ["passed"], "call": ["passed"], "teardown": ["passed"]}:
            return "passed"
        return "incomplete"

    def result(self, exit_code: int) -> dict[str, Any]:
        counts = Counter(self.outcome(node) for node in self.required)
        unexpected = len(set(self.collected) - self.required)
        passed = (
            exit_code == 0
            and counts["passed"] == len(self.required)
            and len(self.collected) == len(self.required)
            and unexpected == 0
        )
        return {
            "passed": passed,
            "pytest_exit_code": exit_code,
            "required_tests": len(self.required),
            "executed_tests": sum("call" in self.reports.get(node, {}) for node in self.required),
            "verified_tests": counts["passed"],
            "failed_hard_checks": counts["failed"],
            "outcomes": dict(sorted(counts.items())),
            "unexpected_tests": unexpected,
            "fixtures": [
                {
                    **fixture.model_dump(mode="json"),
                    "passed": all(self.outcome(node) == "passed" for node in fixture.tests),
                    "results": [
                        {
                            "node_id": node,
                            "expected_test_outcome": "passed",
                            "outcome": self.outcome(node),
                            "phases": self.reports.get(node, {}),
                        }
                        for node in fixture.tests
                    ],
                }
                for fixture in self.suite.fixtures
            ],
        }


def unchecked_recipe(recipe: StructuredRecipe, profile: SafetyProfile) -> StructuredRecipe:
    return recipe.model_copy(deep=True)


def unchecked_owner[T: Mapping[str, object]](
    user: AuthenticatedUser, resource: T | None
) -> T | None:
    return resource


@contextmanager
def negative_control(kind: NegativeControl | None) -> Iterator[None]:
    if kind == "unsafe-candidate":
        with patch("sous_chef.domain.recipe_safety.validate_final_recipe", unchecked_recipe):
            yield
    elif kind == "ownership-bypass":
        with patch.object(AuthenticatedUser, "require_owned", unchecked_owner):
            yield
    else:
        yield


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts/safety-evaluation/report.json"
    )
    parser.add_argument("--negative-control", choices=["unsafe-candidate", "ownership-bypass"])
    args = parser.parse_args()
    report: dict[str, Any] = {
        "schema_version": 1,
        "check_type": "deterministic",
        "model_scored_checks": [],
        "synthetic_only": True,
        "scope": "Fixed required synthetic suite only; not a universal safety guarantee.",
        "catalog_version": CATALOG_VERSION,
        "recipe_policy_version": POLICY_VERSION,
        "negative_control": args.negative_control,
        "passed": False,
    }
    try:
        manifest = MANIFEST.read_bytes()
        suite = Suite.model_validate_json(manifest)
        report.update(
            suite_id=suite.suite_id,
            suite_version=suite.version,
            manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        )
        gate = HardGate(suite)
        exit_code = 3
        try:
            with negative_control(args.negative_control):
                exit_code = int(
                    pytest.main(
                        [
                            "-c",
                            str(ROOT / "pyproject.toml"),
                            "--rootdir",
                            str(ROOT),
                            "--confcutdir",
                            str(ROOT),
                            "-q",
                            "--tb=no",
                            "--show-capture=no",
                            "-rN",
                            *[
                                str(ROOT / node.partition("::")[0]) + "::" + node.partition("::")[2]
                                for fixture in suite.fixtures
                                for node in fixture.tests
                            ],
                        ],
                        plugins=[gate],
                    )
                )
        except (Exception, KeyboardInterrupt, SystemExit):
            report["runner_error"] = "pytest_interrupted_or_failed"
        report.update(gate.result(exit_code))
    except (OSError, ValueError):
        report["runner_error"] = "required_manifest_unavailable_or_invalid"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Fixed synthetic hard gate: " + ("PASS" if report["passed"] else "FAIL"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
