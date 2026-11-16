"""Bounded offline ASC-62 acceptance. No cloud load, telemetry export or notifications."""

import argparse
import hashlib
import platform
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import quantiles
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sous_chef.domain.orchestrator import POLICY

from scripts.evaluate_quality import source_versions, write
from scripts.evaluate_safety import ROOT, Fixture, HardGate

REQUESTS = 12
CONCURRENCY = 4
CONTEXT_TOKENS = 4096
P95_MS = 15_000
LOAD_NODE = (
    "tests/integration/test_resilience_acceptance.py"
    "::test_bounded_chat_load_measures_budgets_and_purges_synthetic_users"
)


class LoadSample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    latency_ms: float = Field(ge=0)
    status: int = Field(ge=100, le=599)
    model_calls: int = Field(ge=0)
    context_tokens: int = Field(ge=0)
    visible_input_tokens: int = Field(ge=0)


class LoadEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    samples: list[LoadSample] = Field(min_length=REQUESTS, max_length=REQUESTS)
    peak_concurrency: int = Field(ge=1, le=REQUESTS)
    cleanup_complete: bool

    def summary(self) -> dict[str, Any]:
        percentiles = quantiles(
            [sample.latency_ms for sample in self.samples], n=100, method="inclusive"
        )
        breaches = {
            "p95_latency": int(percentiles[94] > P95_MS),
            "request_deadline": sum(
                sample.latency_ms > POLICY.deadline_seconds * 1000 for sample in self.samples
            ),
            "model_calls": sum(
                not 0 < sample.model_calls <= POLICY.max_model_calls for sample in self.samples
            ),
            "context_tokens": sum(
                not 0 < sample.context_tokens <= CONTEXT_TOKENS for sample in self.samples
            ),
            "concurrency": int(not 2 <= self.peak_concurrency <= CONCURRENCY),
            "cleanup": int(not self.cleanup_complete),
        }
        errors = sum(sample.status != 200 for sample in self.samples)
        return {
            "passed": errors == 0 and not any(breaches.values()),
            "requests": len(self.samples),
            "configured_concurrency": CONCURRENCY,
            "observed_peak_concurrency": self.peak_concurrency,
            "latency_ms": {
                "p50": percentiles[49],
                "p95": percentiles[94],
                "p99": percentiles[98],
                "max": max(sample.latency_ms for sample in self.samples),
            },
            "status_counts": dict(
                sorted(Counter(sample.status for sample in self.samples).items())
            ),
            "errors": errors,
            "error_rate": errors / len(self.samples),
            "limits": {
                "basis": "Proposed offline regression guards, not approved live SLOs",
                "p95_ms": P95_MS,
                "request_deadline_ms": POLICY.deadline_seconds * 1000,
                "context_tokens_including_framing": CONTEXT_TOKENS,
                "policy": asdict(POLICY),
            },
            "budget_breaches": breaches,
            "visible_input_tokens": sum(sample.visible_input_tokens for sample in self.samples),
            "token_scope": "Locally tokenized Responses input fields only; not provider usage "
            "or full-request billing. Instructions, schemas and output are excluded.",
            "provider_input_tokens": None,
            "provider_output_tokens": None,
            "cleanup_complete": self.cleanup_complete,
            "samples": [sample.model_dump() for sample in self.samples],
        }


def fixture(
    identifier: str, module: str, test: str, expected: str, parameters: tuple[str, ...] = ()
) -> Fixture:
    node = f"tests/{module}.py::{test}"
    return Fixture(
        id=identifier,
        scope=identifier,
        expected_outcome=expected,
        tests=tuple(f"{node}[{value}]" for value in parameters) if parameters else (node,),
    )


@dataclass(frozen=True)
class FaultSuite:
    fixtures: tuple[Fixture, ...]


SUITE = FaultSuite(
    (
        fixture(
            "duplicate-sends",
            "unit/test_chat_api",
            "test_parallel_sends_are_serialized_and_pending_duplicates_do_not_execute",
            "One execution; duplicate pending response and explicit competing-send conflict.",
        ),
        fixture(
            "deletion-during-processing",
            "unit/test_data_retention",
            "test_inflight_extraction_late_feedback_replay_and_poison_cannot_restore_state",
            "Deletion fences in-flight work and late replay; only permanent guards remain.",
        ),
        fixture(
            "duplicate-feedback",
            "unit/test_feedback",
            "test_concurrent_requests_cannot_create_two_versions_from_one_etag",
            "Duplicate submissions produce one evidence revision; stale competing edits conflict.",
            ("False", "True"),
        ),
        fixture(
            "profile-concurrency",
            "unit/test_grocery_api",
            "test_concurrent_inventory_and_other_profile_sections_survive_explicit_conflict_retry",
            "Explicit conflict retry preserves inventory and unrelated explicit/learned changes.",
            ("explicitPreferences", "learnedPreferences"),
        ),
        fixture(
            "worker-interruption",
            "unit/test_learning",
            "test_durable_receipt_recovers_crashes_without_extraction_or_duplicate_evidence",
            "Durable receipts recover three interrupted phases without a second extraction.",
            ("before_projection", "after_projection", "before_status"),
        ),
        fixture(
            "worker-restart",
            "unit/test_learning_recovery",
            "test_transient_failure_recovers_after_durable_backoff_across_restart",
            "Restart honors durable backoff, preserves ratings and applies evidence once.",
            ("error0", "error1", "error2"),
        ),
        fixture(
            "usda-timeout",
            "unit/test_usda_nutrition",
            "test_timeout_and_cancellation_stop_the_child",
            "Timed-out USDA subprocess is cancelled and closed without fabricated nutrition.",
        ),
        fixture(
            "feedback-telemetry",
            "integration/test_learning_telemetry",
            "test_feedback_links_to_one_applied_insight_and_duplicate_is_deduplicated",
            "Correlated lag, extraction and dedup signals contain no private content.",
        ),
        fixture(
            "usda-recovery",
            "integration/test_recipe_chat",
            "test_chat_recovers_from_transient_usda_failure_without_replaying_agents",
            "Real MCP/USDA retries recover without replaying agents or duplicating stored turns.",
            ("transient_search", "transient_profile"),
        ),
        fixture(
            "agent-failures",
            "integration/test_recipe_chat",
            "test_failed_or_uncertain_agent_execution_preserves_recovery_without_replaying",
            "Safe API errors preserve failed/unknown state; known failures permit explicit retry.",
            (
                "-function",
                "-mcp",
                "timeout-function",
                "timeout-mcp",
                "connection-function",
                "connection-mcp",
            ),
        ),
        fixture(
            "failure-telemetry",
            "integration/test_synchronous_telemetry",
            "test_failures_name_the_responsible_operation_without_recording_payloads",
            "Timeout, structured-output and safety failures emit classified, content-free signals.",
            (
                "timeout-foundry_timeout-504",
                "invalid_json-foundry_invalid_result-502",
                "safety-recipe_ingredient_rejected-200",
            ),
        ),
        fixture(
            "cosmos-throttling",
            "integration/test_synchronous_telemetry",
            "test_cosmos_accounts_for_recovered_and_exhausted_throttling",
            "Pinned SDK stops at three attempts and reports recovered and exhausted throttles.",
            ("1", "3"),
        ),
        fixture(
            "web-deadline",
            "integration/test_web_grounding",
            "test_web_has_a_short_deadline_and_does_not_retry",
            "Web deadline cancels the dependency without retry or fabricated facts.",
        ),
        fixture(
            "web-api-recovery",
            "integration/test_resilience_acceptance",
            "test_web_failure_is_explicit_and_a_fresh_send_recovers",
            "Web timeout/invalid output cannot persist a recipe; a recovered dependency works.",
            ("timeout", "invalid_json"),
        ),
        fixture(
            "bounded-load",
            "integration/test_resilience_acceptance",
            LOAD_NODE.partition("::")[2],
            "Twelve signed-user requests overlap; measured concurrency, budgets and purge pass.",
        ),
    )
)


class FaultGate(HardGate):
    def __init__(self) -> None:
        super().__init__(SUITE)
        self.measurements: list[object] = []

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        super().pytest_runtest_logreport(report)
        if report.nodeid == LOAD_NODE and report.when == "call":
            self.measurements.extend(
                value for name, value in report.user_properties if name == "asc62_load"
            )

    def result(self, exit_code: int) -> dict[str, Any]:
        result = super().result(exit_code)
        result["load"] = None
        if len(self.measurements) == 1:
            try:
                result["load"] = LoadEvidence.model_validate(self.measurements[0]).summary()
            except ValueError:
                pass
        result["passed"] = bool(
            result["passed"] and result["load"] is not None and result["load"]["passed"]
        )
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/resilience/report.json")
    args = parser.parse_args()
    gate = FaultGate()
    exit_code = 3
    interrupted = False
    try:
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
                        for item in SUITE.fixtures
                        for node in item.tests
                    ],
                ],
                plugins=[gate],
            )
        )
    except (Exception, KeyboardInterrupt, SystemExit):
        interrupted = True
    report = {
        "schema_version": 1,
        "suite_id": "asc62-fault-load-v1",
        "measured_at": datetime.now(UTC).isoformat(),
        "synthetic_only": True,
        "scope": "In-process API and real SDKs over synthetic transports; not live performance.",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "versions": source_versions(),
        "test_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in sorted({node.partition("::")[0] for node in gate.required})
        },
        "actual_cloud_spend_usd": 0,
        "live_performance_verified": False,
        "live_alert_delivery_verified": False,
        "runner_interrupted": interrupted,
        **gate.result(exit_code),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write(args.output, report)
    print("Fixed offline fault/load acceptance: " + ("PASS" if report["passed"] else "FAIL"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
