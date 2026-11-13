"""Render operational KQL, check synthetic rows, or explicitly publish evaluation summaries."""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from azure.identity import AzureCliCredential
from sous_chef.settings import Settings
from sous_chef.telemetry import configure_safe_telemetry
from sous_chef.tracing import Operation, Telemetry, operation

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "monitoring"
PANELS = ("Health", "Models", "Learning", "Lag", "Evaluation")
SYNTHETIC_PRICES = {"synthetic-model": {"inputUsdPerMillion": 1, "outputUsdPerMillion": 2}}


def query(name: str, *, insights_id: str = "", synthetic: bool = False) -> str:
    signals = json.loads((MONITORING / "signals.json").read_text())
    selectors = {signal["id"]: signal["query"] for signal in signals}
    selectors.update({panel: panel for panel in PANELS})
    selectors["synthetic"] = (MONITORING / "acceptance.kql").read_text()
    if name not in selectors:
        raise ValueError("Unknown monitoring query.")
    if not synthetic and not insights_id:
        raise ValueError("An explicit Application Insights resource ID is required.")
    source = (MONITORING / ("synthetic.kql" if synthetic else "sources.kql")).read_text()
    shared = (MONITORING / "operations.kql").read_text()
    return f"{source}\n{shared}\n{selectors[name]}".replace(
        "__INSIGHTS_RESOURCE_ID__", insights_id
    ).replace("__MODEL_PRICES__", json.dumps(SYNTHETIC_PRICES if synthetic else {}))


def check_synthetic(workspace_id: UUID) -> dict[str, Any]:
    # This query creates in-memory datatables; it never ingests or queries customer rows.
    rendered = query("synthetic", synthetic=True)
    with AzureCliCredential() as credential:
        token = credential.get_token("https://api.loganalytics.io/.default")
        request = Request(
            f"https://api.loganalytics.azure.com/v1/workspaces/{workspace_id}/query",
            data=json.dumps({"query": rendered}).encode(),
            headers={"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=60) as response:
            result = json.load(response)
    if "error" in result or len(result.get("tables", [])) != 1:
        raise ValueError("Synthetic KQL execution did not return one complete result table.")
    table = result["tables"][0]
    rows = [
        dict(zip((column["name"] for column in table["columns"]), row, strict=True))
        for row in table["rows"]
    ]
    expected = {
        *(
            f"failures-{area}"
            for area in (
                "application",
                "agents",
                "tools",
                "database",
                "learning",
                "models",
                "evaluation",
            )
        ),
        "lag",
        "throttles",
        "latency",
        "unknown-token-total",
        "invalid-output",
        "evaluation",
        "learning-events",
        "learning-failures",
        "priced-model",
        "unknown-price",
    }
    passed = (
        len(rows) == len(expected)
        and {row["Check"] for row in rows} == expected
        and all(row["Passed"] is True for row in rows)
    )
    return {
        "synthetic_only": True,
        "passed": passed,
        "executed_at": datetime.now(UTC).isoformat(),
        "query_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        "checks": rows,
    }


def evaluation_result(suite: str, report: dict[str, Any]) -> bool:
    if suite == "safety" and report.get("suite_id") == "asc55-hard-safety":
        value = report.get("passed")
    elif suite == "learning" and report.get("suite") == "asc57-preference-learning-v1":
        value = report.get("deterministic_passed")
    else:
        raise ValueError("Unrecognized versioned evaluation report.")
    if type(value) is not bool:
        raise ValueError("Evaluation result must be an explicit boolean.")
    return value


def record_evaluation(telemetry: Telemetry, suite: str, passed: bool, run_id: UUID) -> None:
    if suite not in ("safety", "learning") or type(passed) is not bool:
        raise ValueError("Only bounded evaluation summaries may be published.")
    with telemetry.activate(), operation(Operation.EVALUATION) as span:
        span.set_attribute("asc.evaluation.suite", suite)
        span.set_attribute("asc.evaluation.passed", passed)
        span.set_attribute("asc.evaluation.run_id", str(run_id))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("query")
    render.add_argument("name")
    render.add_argument("--insights-id", default="")
    render.add_argument("--synthetic", action="store_true")
    check = commands.add_parser("check-synthetic")
    check.add_argument("--workspace-id", type=UUID, required=True)
    check.add_argument("--output", type=Path, required=True)
    publish = commands.add_parser("publish-evaluation")
    publish.add_argument("--suite", choices=["safety", "learning"], required=True)
    publish.add_argument("--report", type=Path, required=True)
    publish.add_argument("--run-id", type=UUID, default=None)
    args = parser.parse_args()
    if args.command == "query":
        print(query(args.name, insights_id=args.insights_id, synthetic=args.synthetic))
    elif args.command == "check-synthetic":
        result = check_synthetic(args.workspace_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(f"Synthetic KQL acceptance: {'PASS' if result['passed'] else 'FAIL'}")
        return 0 if result["passed"] else 1
    else:
        passed = evaluation_result(args.suite, json.loads(args.report.read_text()))
        configure_safe_telemetry()
        settings = Settings.model_validate(
            {
                "environment": os.environ["ASC_ENVIRONMENT"],
                "adapter_mode": os.environ["ASC_ADAPTER_MODE"],
                "agents_enabled": False,
            }
        )
        if not settings.telemetry_enabled:
            raise ValueError("Publishing requires explicit ASC_TELEMETRY_ENABLED=true.")
        telemetry = Telemetry.from_settings(settings, service_name="ai-sous-chef-evaluations")
        try:
            record_evaluation(telemetry, args.suite, passed, args.run_id or uuid4())
        finally:
            telemetry.shutdown()
        print("Evaluation summary export attempted; verify ingestion in the approved workspace.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
