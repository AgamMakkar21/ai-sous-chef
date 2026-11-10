import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from scripts import evaluate_safety as evaluation
from scripts.evaluate_safety import MANIFEST, ROOT, HardGate, Suite

Phase = Literal["setup", "call", "teardown"]
Outcome = Literal["passed", "failed", "skipped"]
PHASES: tuple[Phase, ...] = ("setup", "call", "teardown")


def execution_report(node: str, phase: Phase, outcome: Outcome, **extra: Any) -> pytest.TestReport:
    return pytest.TestReport(
        nodeid=node,
        location=(node.partition("::")[0], 0, node.partition("::")[2]),
        keywords={},
        outcome=outcome,
        longrepr=None,
        when=phase,
        **extra,
    )


@pytest.fixture
def completed_gate() -> HardGate:
    gate = HardGate(Suite.model_validate_json(MANIFEST.read_bytes()))
    session = MagicMock(spec=pytest.Session)
    session.items = [SimpleNamespace(nodeid=node) for node in sorted(gate.required)]
    gate.pytest_collection_finish(session)
    for node in gate.required:
        for phase in PHASES:
            gate.pytest_runtest_logreport(execution_report(node, phase, "passed"))
    assert gate.result(0)["passed"]
    return gate


@pytest.mark.parametrize(
    "regression",
    [
        "failed-call",
        "skipped-setup",
        "skipped-call",
        "xfail",
        "xpass",
        "setup-error",
        "teardown-error",
        "missing-call",
        "missing-teardown",
        "duplicate-report",
        "missing-test",
        "duplicate-test",
        "unexpected-test",
        "nonzero-exit",
    ],
)
def test_required_coverage_fails_closed_and_keeps_expectations(
    completed_gate: HardGate, regression: str
) -> None:
    gate = completed_gate
    node = sorted(gate.required)[0]
    gate.reports[node] = {}
    events: dict[Phase, Outcome] = {
        "setup": "skipped" if regression == "skipped-setup" else "passed",
        "call": "failed" if regression == "failed-call" else "passed",
        "teardown": "passed",
    }
    if regression == "skipped-call":
        events["call"] = "skipped"
    if regression == "setup-error":
        events["setup"] = "failed"
    elif regression == "teardown-error":
        events["teardown"] = "failed"
    for phase, outcome in events.items():
        if regression == "missing-" + phase:
            continue
        extra = (
            {"wasxfail": "synthetic expected failure"} if regression in {"xfail", "xpass"} else {}
        )
        gate.pytest_runtest_logreport(execution_report(node, phase, outcome, **extra))
    if regression == "duplicate-report":
        gate.pytest_runtest_logreport(execution_report(node, "call", "passed"))
    elif regression == "missing-test":
        gate.collected.remove(node)
        gate.reports.pop(node)
    elif regression == "duplicate-test":
        gate.collected.append(node)
    elif regression == "unexpected-test":
        gate.collected.append("unrequired-synthetic-test")
    result = gate.result(3 if regression == "nonzero-exit" else 0)
    assert not result["passed"]
    assert result["required_tests"] == len(gate.required)
    assert {fixture["id"] for fixture in result["fixtures"]} == {
        fixture.id for fixture in gate.suite.fixtures
    }
    assert all(fixture["expected_outcome"] for fixture in result["fixtures"])
    assert {
        item["node_id"] for fixture in result["fixtures"] for item in fixture["results"]
    } == gate.required
    if regression != "nonzero-exit":
        assert result["verified_tests"] < result["required_tests"] or result["unexpected_tests"]
    if regression == "failed-call":
        assert result["failed_hard_checks"] == 1


@pytest.mark.parametrize("regression", ["duplicate-id", "duplicate-test", "missing-scope"])
def test_invalid_required_sets_are_rejected(regression: str) -> None:
    manifest = json.loads(MANIFEST.read_bytes())
    if regression == "duplicate-id":
        manifest["fixtures"][1]["id"] = manifest["fixtures"][0]["id"]
    elif regression == "duplicate-test":
        manifest["fixtures"][1]["tests"].append(manifest["fixtures"][0]["tests"][0])
    else:
        manifest["fixtures"] = manifest["fixtures"][1:]
    with pytest.raises(ValidationError):
        Suite.model_validate_json(json.dumps(manifest))


@pytest.mark.parametrize("manifest", [None, "{invalid synthetic manifest"])
def test_unavailable_manifest_still_writes_a_failed_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest: str | None
) -> None:
    source = tmp_path / "manifest.json"
    output = tmp_path / "report.json"
    if manifest is not None:
        source.write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(evaluation, "MANIFEST", source)
    monkeypatch.setattr(sys, "argv", ["evaluate_safety", "--output", str(output)])
    assert evaluation.main() == 1
    result = json.loads(output.read_text(encoding="utf-8"))
    assert not result["passed"]
    assert result["runner_error"] == "required_manifest_unavailable_or_invalid"
    assert "invalid synthetic manifest" not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "error", [RuntimeError("private exception text"), KeyboardInterrupt(), SystemExit(0)]
)
def test_interrupted_runner_retains_required_coverage_without_exception_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    output = tmp_path / "report.json"

    def interrupt(*args: object, **kwargs: object) -> int:
        raise error

    monkeypatch.setattr(pytest, "main", interrupt)
    monkeypatch.setattr(sys, "argv", ["evaluate_safety", "--output", str(output)])
    assert evaluation.main() == 1
    result = json.loads(output.read_text(encoding="utf-8"))
    assert not result["passed"]
    assert result["runner_error"] == "pytest_interrupted_or_failed"
    assert result["executed_tests"] == result["verified_tests"] == 0
    assert result["fixtures"] and all(fixture["expected_outcome"] for fixture in result["fixtures"])
    assert "private exception text" not in output.read_text(encoding="utf-8")


def run_gate(
    tmp_path: Path, *, control: str | None = None, pytest_addopts: str = ""
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    output = tmp_path / "report.json"
    command = [sys.executable, "-m", "scripts.evaluate_safety", "--output", str(output)]
    if control:
        command.extend(["--negative-control", control])
    process = subprocess.run(
        command,
        cwd=ROOT,
        env={
            **os.environ,
            "PYTEST_ADDOPTS": pytest_addopts,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")]),
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    return process, json.loads(output.read_text(encoding="utf-8"))


@pytest.mark.parametrize("control", [None, "unsafe-candidate", "ownership-bypass"])
def test_real_gate_detects_disabled_safety_and_ownership(
    tmp_path: Path, control: str | None
) -> None:
    process, result = run_gate(tmp_path, control=control)
    assert process.returncode == (1 if control else 0), process.stdout
    assert result["passed"] is (control is None)
    assert result["negative_control"] == control
    assert result["check_type"] == "deterministic"
    assert result["model_scored_checks"] == []
    assert result["synthetic_only"] is True
    assert result["required_tests"] == result["executed_tests"]
    assert not result["unexpected_tests"]
    fixtures = {fixture["id"]: fixture for fixture in result["fixtures"]}
    assert fixtures["compliant-controls"]["passed"], process.stdout
    if control:
        assert result["failed_hard_checks"] > 0
        target = "recipe-integrity" if control == "unsafe-candidate" else "wrong-owner-records"
        assert not fixtures[target]["passed"]
        assert all(item["outcome"] == "failed" for item in fixtures[target]["results"])
    else:
        assert result["verified_tests"] == result["required_tests"]
        assert all(fixture["passed"] for fixture in fixtures.values())
        assert result["failed_hard_checks"] == 0
    serialized = json.dumps(result)
    for forbidden in ("Traceback", "private-other-user", "Authorization", "Bearer ", "eyJ"):
        assert forbidden not in serialized


def test_partial_execution_cannot_pass_even_when_pytest_succeeds(tmp_path: Path) -> None:
    process, result = run_gate(
        tmp_path, pytest_addopts="-k test_known_compliant_recipe_passes_with_an_explanation"
    )
    assert process.returncode == 1, process.stdout
    assert result["pytest_exit_code"] == 0
    assert not result["passed"]
    assert result["executed_tests"] == 1
    assert result["required_tests"] > result["executed_tests"]
    assert result["outcomes"]["missing_or_duplicate"] == result["required_tests"] - 1
    assert all(fixture["expected_outcome"] for fixture in result["fixtures"])


def test_real_skips_xfails_and_xpasses_fail_even_when_pytest_succeeds(tmp_path: Path) -> None:
    nodes = Suite.model_validate_json(MANIFEST.read_bytes()).fixtures[0].tests
    (tmp_path / "synthetic_gate_marks.py").write_text(
        "import pytest\n"
        "def pytest_collection_modifyitems(items):\n"
        "    marks = {\n"
        f"        {nodes[0]!r}: pytest.mark.skip(reason='synthetic skip'),\n"
        f"        {nodes[1]!r}: pytest.mark.xfail(run=False, reason='synthetic xfail'),\n"
        f"        {nodes[2]!r}: pytest.mark.xfail(strict=False, reason='synthetic xpass'),\n"
        "    }\n"
        "    for item in items:\n"
        "        if item.nodeid in marks:\n"
        "            item.add_marker(marks[item.nodeid])\n",
        encoding="utf-8",
    )
    process, result = run_gate(tmp_path, pytest_addopts="-p synthetic_gate_marks")
    assert process.returncode == 1, process.stdout
    assert result["pytest_exit_code"] == 0
    assert not result["passed"]
    assert result["outcomes"]["skipped"] == 1
    assert result["outcomes"]["xfail_or_xpass"] == 2
    assert result["verified_tests"] == result["required_tests"] - 3


def test_ci_runs_gate_before_full_tests_and_uploads_bounded_failure_evidence() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    command = workflow.split("          - check: tests\n", 1)[1].split("          - check:", 1)[0]
    assert "python -m scripts.evaluate_safety\n              python -m pytest" in command
    assert "continue-on-error" not in workflow and "||" not in command
    artifact = workflow.split("- name: Retain fixed-suite safety evidence even after failure", 1)[1]
    artifact = artifact.split("\n  frontend:", 1)[0]
    assert "if: always() && matrix.check == 'tests'" in artifact
    assert "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9" in artifact
    assert "path: artifacts/safety-evaluation/report.json" in artifact
    assert "if-no-files-found: error" in artifact
