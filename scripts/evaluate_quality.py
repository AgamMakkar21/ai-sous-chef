"""ASC-56 offline-first quality suite. Cloud use requires explicit, process-only approval."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from contextlib import ExitStack
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from azure.ai.projects import AIProjectClient
from sous_chef.adapters.credentials import build_credential
from sous_chef.domain.orchestrator import POLICY
from sous_chef.recipe_definition import RECIPE_POLICY_VERSION
from sous_chef.settings import ConfigurationError, Settings, load_settings

from scripts.quality_capture import ROOT, Observation, capture
from scripts.quality_evidence import digest, load_suite, report, review_template, rows, suite_digest
from scripts.quality_foundry import collect, deployment_versions, submit

APPROVAL_VARIABLE = "ASC_APPROVE_FOUNDRY_EVALUATION"


def write(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_versions() -> dict[str, Any]:
    files = sorted(
        [
            path
            for directory in (ROOT / "backend" / "src", ROOT / "scripts")
            for path in directory.rglob("*.py")
            if "_usda" not in path.parts
        ]
        + [
            ROOT / "tests" / "fixtures" / "recipe-specialist.json",
            ROOT / "tests" / "fixtures" / "usda_mcp_server.py",
        ]
    )
    sources = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }
    git = subprocess.run(
        ["git", "--no-pager", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    dirty = subprocess.run(
        ["git", "--no-pager", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        "git_commit": git.stdout.strip(),
        "git_worktree_dirty": bool(dirty.stdout.strip()),
        "source_tree_sha256": digest(sources),
        "prompt_sha256": {
            path.name: sources[path.relative_to(ROOT).as_posix()]
            for path in files
            if path.name in {"orchestrator_definition.py", "recipe_definition.py"}
        },
        "policy_versions": {"orchestrator": POLICY.version, "recipe": RECIPE_POLICY_VERSION},
        "sdk": {name: version(name) for name in ("azure-ai-projects", "openai", "azure-identity")},
        "tools": {
            "foundry_iq": "production validated MCP retrieval envelope",
            "usda": "production USDA MCP adapter and deterministic serving aggregation",
            "web_iq": "production minimized public-query policy and native citations",
            "implementation_sha256": digest(sources),
        },
    }


def live_settings() -> Settings:
    if os.environ.get(APPROVAL_VARIABLE) != "1":
        raise ConfigurationError("Live evaluation requires explicit process-only approval.")
    settings = load_settings()
    if (
        settings.environment != "dev"
        or settings.adapter_mode != "live"
        or not settings.agents_enabled
    ):
        raise ConfigurationError("Live evaluation is restricted to the configured dev workload.")
    return settings


def configured_agents(settings: Settings) -> dict[str, Any]:
    return {
        "orchestrator": {
            "name": settings.foundry_orchestrator_agent_name,
            "version": settings.foundry_orchestrator_agent_version,
        },
        "recipe": {
            "name": settings.foundry_recipe_nutrition_agent_name,
            "generation": settings.foundry_recipe_nutrition_agent_version,
            "retrieval": settings.foundry_recipe_nutrition_retrieval_agent_version,
            "web": settings.foundry_recipe_nutrition_web_agent_version,
        },
    }


def costs(mode: str, observations: list[Observation], state: dict[str, Any]) -> dict[str, Any]:
    if mode == "replay":
        return {
            "status": "no_cloud_calls",
            "total_usd": 0,
            "note": "Replay token counts are synthetic, not billable usage.",
        }
    return {
        "status": "unknown",
        "total_usd": None,
        "note": "Precomputed dataset evaluations do not guarantee complete target/judge billing. "
        "Reconcile usage and run IDs against the approved Azure rate card/invoice; "
        "unknown is not zero.",
        "target_input_tokens_observed": sum(
            usage.input_tokens for observation in observations for usage in observation.trace.usage
        ),
        "target_output_tokens_observed": sum(
            usage.output_tokens for observation in observations for usage in observation.trace.usage
        ),
        "unattributed_target_invocations": sum(
            max(0, observation.trace.model_attempts - len(observation.trace.usage))
            for observation in observations
        ),
        "judge_usage": state.get("judge_usage"),
        "service_estimated_cost": state.get("service_estimated_cost"),
    }


async def execute(args: argparse.Namespace) -> int:
    suite = load_suite()
    output: Path = args.output
    if args.report_only and args.live:
        raise ValueError("Report-only does not invoke cloud services.")
    args.resume = args.resume or args.report_only
    settings = live_settings() if args.live else None
    if args.live and not args.judge_deployment and not args.resume:
        raise ValueError("Live submission requires an approved existing judge deployment.")
    state: dict[str, Any]
    observations: list[Observation] = []
    outputs: list[dict[str, Any]] = []
    if args.resume:
        state = json.loads((output / "state.json").read_text(encoding="utf-8"))
        observations = [
            Observation.model_validate_json(json.dumps(item))
            for item in json.loads((output / "observations.json").read_text(encoding="utf-8"))
        ]
        if state["suite_sha256"] != suite_digest(suite) or (
            not args.report_only and state["mode"] != ("live" if args.live else "replay")
        ):
            raise ValueError("Resume must use the original suite and execution mode.")
        if state.get("observations_sha256") != digest(
            [item.model_dump(mode="json") for item in observations]
        ):
            raise ValueError("Saved observations do not match the submitted evidence.")
        if state["versions"]["source_tree_sha256"] != source_versions()["source_tree_sha256"]:
            raise ValueError("Resume must use the original scoring/capture implementation.")
        if settings is not None and (
            state.get("project_sha256") != digest(str(settings.foundry_project_endpoint))
            or state["versions"].get("agents") != configured_agents(settings)
        ):
            raise ValueError("Resume must use the original project and pinned agents.")
        if (output / "outputs.json").exists():
            outputs = json.loads((output / "outputs.json").read_text(encoding="utf-8"))
    else:
        output.mkdir(parents=True, exist_ok=False)
        state = {
            "mode": "live" if args.live else "replay",
            "started_at": datetime.now(UTC).isoformat(),
            "suite_sha256": suite_digest(suite),
            "versions": source_versions(),
            "status": "capturing" if args.live else "not_run",
            "approval": "explicit_process_only" if args.live else "offline_only",
        }
        if settings is not None:
            state["project_sha256"] = digest(str(settings.foundry_project_endpoint))
            state["versions"]["agents"] = configured_agents(settings)
        write(output / "suite.json", suite.model_dump(mode="json", by_alias=True))
        write(output / "state.json", state)
    try:
        with ExitStack() as resources:
            project = None
            client = None
            if args.live:
                assert settings is not None
                credential = build_credential(settings)
                close = getattr(credential, "close", None)
                if callable(close):
                    resources.callback(close)
                project = resources.enter_context(
                    AIProjectClient(
                        endpoint=str(settings.foundry_project_endpoint),
                        credential=credential,
                        api_version="v1",
                        retry_total=0,
                        connection_timeout=10,
                        read_timeout=60,
                    )
                )
                client = resources.enter_context(
                    project.get_openai_client(max_retries=0, timeout=60)
                )
                if not args.resume:
                    state["versions"]["models"] = deployment_versions(
                        project, settings, args.judge_deployment
                    )
                    state["versions"]["models_recorded_at"] = datetime.now(UTC).isoformat()
                    write(output / "state.json", state)
            if not args.resume:
                for case in suite.cases:
                    observations.append(await capture(case, suite.defaults, settings))
                    write(
                        output / "observations.json",
                        [item.model_dump(mode="json") for item in observations],
                    )
                state["observations_sha256"] = digest(
                    [item.model_dump(mode="json") for item in observations]
                )
                write(output / "review-template.json", review_template(suite, observations))
                write(output / "dataset.json", rows(suite, observations))
                write(output / "state.json", state)
            if args.live:
                assert settings is not None and project is not None and client is not None
                if not args.resume:
                    if state["versions"]["models"] != deployment_versions(
                        project, settings, args.judge_deployment
                    ):
                        raise ValueError("Backing model versions changed during capture.")
                    state["versions"]["models_verified_after_capture"] = True
                    submit(
                        project,
                        client,
                        suite,
                        rows(suite, observations),
                        args.judge_deployment,
                        state,
                        lambda value: write(output / "state.json", value),
                    )
                outputs = collect(
                    client,
                    state,
                    lambda value: write(output / "state.json", value),
                    expected_rows=len(suite.cases) + len(suite.anchors),
                    wait_seconds=args.wait_seconds,
                )
                write(output / "outputs.json", outputs)
                state.pop("runner_error", None)
                write(output / "state.json", state)
    except Exception:
        state["runner_error"] = "evaluation_execution_failed"
        write(output / "state.json", state)
    review = None
    review_error = None
    if args.review:
        try:
            review = json.loads(args.review.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            review_error = "invalid_human_review"
    summary = report(
        suite,
        observations,
        mode=state["mode"],
        cloud_status=state["status"],
        outputs=outputs,
        review=review,
    )
    summary["versions"] = state["versions"]
    summary["foundry"] = {
        key: state.get(key) for key in ("eval_id", "run_id", "report_url", "evaluators")
    }
    summary["cost"] = costs(state["mode"], observations, state)
    summary["runner_error"] = state.get("runner_error")
    summary["review_error"] = review_error
    if state.get("runner_error") or review_error:
        summary["quality_passed"] = False
    write(output / "report.json", summary)
    print(f"Quality evidence: {output / 'report.json'}")
    print(
        f"Deterministic checks: {summary['deterministic_passed']}/"
        f"{summary['required_cases']} cases; "
        f"live quality passed: {summary['quality_passed']}."
    )
    if state["mode"] == "replay":
        return (
            0
            if summary["deterministic_passed"] == summary["required_cases"]
            and not state.get("runner_error")
            and not review_error
            else 1
        )
    return 0 if summary["quality_passed"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/quality-evaluation"))
    parser.add_argument(
        "--live", action="store_true", help="Paid dev-only capture and Foundry judging."
    )
    parser.add_argument(
        "--resume", action="store_true", help="Collect a saved run; never resubmit."
    )
    parser.add_argument("--judge-deployment", default="")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Regrade saved evidence and human review without credentials or network calls.",
    )
    parser.add_argument("--review", type=Path, help="Completed, evidence-bound human review JSON.")
    parser.add_argument(
        "--wait-seconds", type=int, default=900, choices=range(1, 901), metavar="1..900"
    )
    args = parser.parse_args()
    try:
        return asyncio.run(execute(args))
    except (OSError, ValueError, ConfigurationError):
        print(
            "Evaluation not started: check output path, matching evidence "
            "and explicit dev approval."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
