"""Operational summaries preserve privacy and unknown measurements."""

import json
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID

import pytest
from azure.monitor.opentelemetry.exporter.export.trace._exporter import _convert_span_to_envelope
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sous_chef.tracing import Operation, Telemetry, operation
from test_synchronous_telemetry import capture as capture

from scripts.monitoring import check_synthetic, evaluation_result, query, record_evaluation


@pytest.mark.parametrize(
    ("suite", "report"),
    [
        ("safety", {"suite_id": "asc55-hard-safety", "passed": False}),
        ("learning", {"suite": "asc57-preference-learning-v1", "deterministic_passed": False}),
    ],
)
def test_evaluation_failure_is_not_replaced_by_a_truthy_value(
    suite: str, report: dict[str, object]
) -> None:
    assert evaluation_result(suite, report) is False
    key = "passed" if suite == "safety" else "deterministic_passed"
    for unknown in (None, "false", 0):
        report[key] = unknown
        with pytest.raises(ValueError):
            evaluation_result(suite, report)
    with pytest.raises(ValueError):
        evaluation_result(suite, {})


def test_evaluation_exports_only_the_bounded_summary(
    capture: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    report = {
        "suite_id": "asc55-hard-safety",
        "passed": False,
        "prompt": "PRIVATE_CANARY@example.invalid",
        "observations": [{"raw_note": "PRIVATE_CANARY@example.invalid"}],
    }
    record_evaluation(
        capture[0],
        "safety",
        evaluation_result("safety", report),
        UUID("11111111-1111-4111-8111-111111111111"),
    )
    (span,) = capture[1].get_finished_spans()
    assert span.name == "evaluation.run"
    assert span.attributes is not None
    assert span.attributes["asc.evaluation.passed"] is False
    assert span.attributes["asc.evaluation.suite"] == "safety"
    assert "PRIVATE_CANARY" not in repr(span.attributes)
    with pytest.raises(ValueError):
        record_evaluation(capture[0], "PRIVATE_CANARY", False, UUID(int=0))


def test_live_queries_require_scope_and_synthetic_queries_use_no_live_tables() -> None:
    with pytest.raises(ValueError):
        query("Health")
    synthetic = query("synthetic", synthetic=True)
    assert "AppRequests" not in synthetic
    assert "AppDependencies" not in synthetic
    assert "AppMetrics" not in synthetic
    assert "__MODEL_PRICES__" not in synthetic
    assert "datatable(" in synthetic
    live = query("model-tokens", insights_id="/synthetic/insights")
    assert "where _ResourceId =~ '/synthetic/insights'" in live
    assert "synthetic-model" not in live


def test_query_fields_match_the_pinned_azure_exporter_wire_format(
    capture: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    with capture[0].activate(), operation(Operation.HTTP, root=True) as span:
        span.set_attribute("http.request.method", "GET")
        span.set_attribute("http.route", "/synthetic")
        span.set_attribute("http.response.status_code", 503)
    record_evaluation(capture[0], "safety", False, UUID(int=0))
    http, evaluation = [
        cast(Any, _convert_span_to_envelope(span).data).base_data
        for span in capture[1].get_finished_spans()
    ]
    assert http.name == "GET /synthetic"
    assert http.response_code == "503"
    assert http.properties["asc.operation"] == "http.request"
    assert "http.response.status_code" not in http.properties
    assert evaluation.success is True
    assert evaluation.properties["asc.evaluation.passed"] == "False"


@pytest.mark.parametrize("rows", [[], [["unexpected", 0, 0, True]]])
def test_missing_or_foreign_synthetic_checks_cannot_pass(rows: list[list[object]]) -> None:
    result = {
        "tables": [
            {
                "columns": [{"name": name} for name in ("Check", "Actual", "Expected", "Passed")],
                "rows": rows,
            }
        ]
    }
    with (
        patch("scripts.monitoring.AzureCliCredential"),
        patch("scripts.monitoring.urlopen") as request,
    ):
        request.return_value.__enter__.return_value.read.return_value = json.dumps(result)
        assert check_synthetic(UUID(int=0))["passed"] is False
