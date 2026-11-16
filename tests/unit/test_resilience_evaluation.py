from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from test_safety_evaluation import PHASES, execution_report

from scripts.evaluate_resilience import LOAD_NODE, REQUESTS, FaultGate


@pytest.mark.parametrize(
    "regression",
    [
        None,
        "missing-load",
        "incomplete-load",
        "latency",
        "context",
        "calls",
        "concurrency",
        "cleanup",
        "http-error",
    ],
)
def test_report_requires_measured_load_and_reports_budget_failures(regression: str | None) -> None:
    gate = FaultGate()
    session = MagicMock(spec=pytest.Session)
    session.items = [SimpleNamespace(nodeid=node) for node in sorted(gate.required)]
    gate.pytest_collection_finish(session)
    evidence: dict[str, Any] = {
        "samples": [
            {
                "latency_ms": 10.0,
                "status": 200,
                "model_calls": 3,
                "context_tokens": 200,
                "visible_input_tokens": 1000,
            }
            for _ in range(REQUESTS)
        ],
        "peak_concurrency": 4,
        "cleanup_complete": True,
    }
    if regression == "incomplete-load":
        evidence["samples"].pop()
    elif regression == "latency":
        for sample in evidence["samples"]:
            sample["latency_ms"] = 70_000.0
    elif regression == "context":
        evidence["samples"][0]["context_tokens"] = 10_000
    elif regression == "calls":
        evidence["samples"][0]["model_calls"] = 100
    elif regression == "concurrency":
        evidence["peak_concurrency"] = 5
    elif regression == "cleanup":
        evidence["cleanup_complete"] = False
    elif regression == "http-error":
        evidence["samples"][0]["status"] = 503
    for node in gate.required:
        for phase in PHASES:
            report = execution_report(node, phase, "passed")
            if node == LOAD_NODE and phase == "call" and regression != "missing-load":
                report.user_properties = [("asc62_load", evidence)]
            gate.pytest_runtest_logreport(report)
    result = gate.result(0)
    assert result["passed"] == (regression is None)
    assert result["verified_tests"] == len(gate.required)
    if regression in {"missing-load", "incomplete-load"}:
        assert result["load"] is None
    else:
        assert result["load"]["requests"] == REQUESTS
        assert result["load"]["latency_ms"]["p99"] >= 10
        assert result["load"]["provider_input_tokens"] is None
        if regression == "http-error":
            assert result["load"]["errors"] == 1
            assert result["load"]["status_counts"][503] == 1
        elif regression is not None:
            assert any(result["load"]["budget_breaches"].values())
