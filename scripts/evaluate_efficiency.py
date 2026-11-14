"""Offline ASC-58 measurements; never invokes a model or estimates Azure billing."""

import argparse
import asyncio
import json
import platform
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from statistics import median, quantiles
from time import perf_counter
from typing import Any
from unittest.mock import patch
from uuid import UUID

from sous_chef.domain.context import (
    CONTEXT_FRAMING_TOKENS,
    MAX_CONTEXT_CHARACTERS,
    MAX_CONTEXT_EXCHANGES,
    MAX_CONTEXT_LEARNED,
    ContextExchange,
    RequestContext,
)
from sous_chef.domain.extraction import (
    MAX_EXTRACTION_INPUT_TOKENS,
    MAX_EXTRACTION_OUTPUT_TOKENS,
    MAX_EXTRACTION_PRIOR,
)
from sous_chef.domain.orchestrator import POLICY, CoordinationRequest, CoordinationStep, TokenUsage
from sous_chef.domain.recipes import (
    CombinedRecipeGenerationRequest,
    RecipeDraft,
    RecipeGrounding,
    WebGrounding,
)
from sous_chef.services.context import _encoding, bound_context, context_tokens

from scripts.evaluate_quality import source_versions, write
from scripts.quality_capture import ROOT, ReplayGateway, capture
from scripts.quality_evidence import digest, factual_checks, load_suite, suite_digest

VERSION = "asc58-model-efficiency-v1"
INPUT_USD_PER_MILLION = {"gpt-5.4-mini": 0.75, "gpt-5.4": 2.5}
REFERENCE_PRICES = {
    "basis": "OpenAI public standard uncached input; not an approved Azure rate card",
    "usd_per_million_input_tokens": INPUT_USD_PER_MILLION,
    "sources": [
        "https://developers.openai.com/api/docs/models/gpt-5.4-mini",
        "https://developers.openai.com/api/docs/models/gpt-5.4",
    ],
}


def workloads(defaults: RequestContext) -> dict[str, RequestContext]:
    result = {"pantry-3": defaults}
    for count in (40, 100):
        groceries = tuple(
            defaults.groceries[index % 3].model_copy(
                update={"grocery_id": UUID(int=index + 1, version=4), "name": f"rice {index}"}
            )
            for index in range(count)
        )
        result[f"pantry-{count}"] = defaults.model_copy(update={"groceries": groceries})
    result["history-10"] = defaults.model_copy(
        update={
            "history": tuple(
                ContextExchange(
                    turn_id=UUID(int=index + 1, version=4),
                    user=f"Make a mild rice dinner, variation {index}.",
                    assistant=(
                        "Use rice, carrots and olive oil within the stated time and allergies."
                    ),
                )
                for index in range(10)
            )
        }
    )
    return result


def semantic_digest(prompt: str) -> str:
    payload = json.loads(prompt)
    for grocery in payload["groceries"]:
        grocery.pop("updatedAt", None)
    for exchange in payload["history"]:
        exchange.pop("turnId", None)
    return digest(payload)


def measure_context(context: RequestContext, iterations: int) -> dict[str, Any]:
    for _ in range(5):
        bound_context(context)
    durations = []
    for _ in range(iterations):
        started = perf_counter()
        bounded = bound_context(context)
        durations.append((perf_counter() - started) * 1000)
    if bounded != context:
        raise ValueError("Matched workloads must fit completely; do not compare truncated inputs.")
    percentiles = quantiles(durations, n=100, method="inclusive")
    return {
        "visible_input_tokens": context_tokens(bounded) - CONTEXT_FRAMING_TOKENS,
        "backend_context_sha256": digest(context.model_dump(mode="json", by_alias=True)),
        "semantic_input_sha256": semantic_digest(bounded.prompt),
        "local_context_preparation_ms": {
            "samples": iterations,
            "p50": median(durations),
            "p95": percentiles[94],
            "p99": percentiles[98],
        },
    }


async def measure(iterations: int) -> dict[str, Any]:
    if not 5 <= iterations <= 1001:
        raise ValueError("Use 5 to 1001 iterations.")
    suite = load_suite()
    contexts = workloads(suite.defaults)
    measured_contexts = {
        name: measure_context(context, iterations) for name, context in contexts.items()
    }
    calls: list[dict[str, Any]] = []

    class MeasuredReplay(ReplayGateway):
        def record(self, model: str, prompt: str) -> None:
            calls.append(
                {
                    "case_id": self.case.id,
                    "model": model,
                    "visible_input_tokens": len(_encoding().encode(prompt, disallowed_special=())),
                }
            )

        async def coordinate(self, request: CoordinationRequest) -> CoordinationStep:
            self.record("gpt-5.4-mini", request.request.prompt)
            return await super().coordinate(request)

        async def generate_combined_recipe(
            self, request: CombinedRecipeGenerationRequest
        ) -> tuple[RecipeDraft, RecipeGrounding, WebGrounding | None, TokenUsage]:
            self.record("gpt-5.4", request.prompt)
            return await super().generate_combined_recipe(request)

    replay: list[dict[str, Any]] = []
    with patch("scripts.quality_capture.ReplayGateway", MeasuredReplay):
        for case in suite.cases:
            observation = await capture(case, suite.defaults, None)
            observed = [call for call in calls if call["case_id"] == case.id]
            checks = factual_checks(suite, case, observation)
            replay.append(
                {
                    "case_id": case.id,
                    "model_calls": observation.trace.model_attempts,
                    "recorded_model_calls": len(observed),
                    "checks": {name: check.status for name, check in checks.items()},
                    "outcome_sha256": digest(
                        {
                            "error_code": observation.error_code,
                            "result": observation.result.model_dump(mode="json", exclude={"recipe"})
                            if observation.result
                            else None,
                        }
                    ),
                }
            )
    return {
        "version": VERSION,
        "measured_at": datetime.now(UTC).isoformat(),
        "versions": source_versions()
        | {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "tiktoken": version("tiktoken"),
        },
        "tokenizer": "o200k_base",
        "workload_sha256": digest(
            {name: context.model_dump(mode="json") for name, context in contexts.items()}
        ),
        "quality_suite_sha256": suite_digest(suite),
        "fixed_models": {
            "orchestrator": "gpt-5.4-mini",
            "recipe_nutrition": "gpt-5.4",
            "extraction": "gpt-5.1-nano",
        },
        "budgets": {
            "orchestrator": asdict(POLICY),
            "context_transport_characters": MAX_CONTEXT_CHARACTERS,
            "context_tokens_including_framing": None,
            "history_exchanges": MAX_CONTEXT_EXCHANGES,
            "learned_entries": MAX_CONTEXT_LEARNED,
            "retrieval_max_results_default": 3,
            "retrieval_max_context_tokens_default": 4000,
            "extraction_input_tokens": MAX_EXTRACTION_INPUT_TOKENS,
            "extraction_output_tokens": MAX_EXTRACTION_OUTPUT_TOKENS,
            "extraction_prior_notes": MAX_EXTRACTION_PRIOR,
        },
        "contexts": measured_contexts,
        "replay": replay,
        "calls": calls,
        "reference_prices": REFERENCE_PRICES,
        "cost": {
            "estimated_visible_input_reference_usd": sum(
                call["visible_input_tokens"] * INPUT_USD_PER_MILLION[call["model"]] / 1_000_000
                for call in calls
            ),
            "scope": "Only request.prompt text; excludes instructions, schemas, tool results, "
            "reasoning, output, caching and tool charges. Local tokens are not provider usage.",
            "actual_cloud_spend_usd": 0,
            "azure_full_request_estimated_usd": None,
            "provider_input_tokens": None,
            "provider_output_tokens": None,
            "live_model_latency_ms": None,
        },
        "deterministic_passed": all(
            row["model_calls"] == row["recorded_model_calls"]
            and all(status == "pass" for status in row["checks"].values())
            for row in replay
        ),
        "live_quality_passed": False,
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "version",
        "tokenizer",
        "workload_sha256",
        "quality_suite_sha256",
        "fixed_models",
        "budgets",
        "reference_prices",
    ):
        if before[key] != after[key]:
            raise ValueError(f"Unmatched comparison: {key}.")
    if before["versions"]["prompt_sha256"] != after["versions"]["prompt_sha256"]:
        raise ValueError("Published agent instructions changed.")
    for key in ("python", "platform", "tiktoken", "sdk"):
        if before["versions"][key] != after["versions"][key]:
            raise ValueError(f"Measurement environment changed: {key}.")
    if not before["deterministic_passed"] or not after["deterministic_passed"]:
        raise ValueError("The fixed deterministic quality checks must pass in both arms.")
    if before["replay"] != after["replay"]:
        raise ValueError("Replay outcomes, quality checks or model-call counts changed.")
    if before["contexts"].keys() != after["contexts"].keys():
        raise ValueError("Context workloads changed.")
    savings = {}
    for name, previous in before["contexts"].items():
        current = after["contexts"][name]
        for key in ("backend_context_sha256", "semantic_input_sha256"):
            if previous[key] != current[key]:
                raise ValueError(f"Required context changed: {name}.")
        if (
            previous["local_context_preparation_ms"]["samples"]
            != current["local_context_preparation_ms"]["samples"]
        ):
            raise ValueError(f"Measurement sample count changed: {name}.")
        saved = previous["visible_input_tokens"] - current["visible_input_tokens"]
        if saved < 0:
            raise ValueError(f"Visible input tokens increased: {name}.")
        savings[name] = {
            "tokens_saved": saved,
            "percent_saved": 100 * saved / previous["visible_input_tokens"],
        }
    return {
        "matched": True,
        "context_savings": savings,
        "estimated_visible_input_reference_usd_saved": before["cost"][
            "estimated_visible_input_reference_usd"
        ]
        - after["cost"]["estimated_visible_input_reference_usd"],
        "latency_scope": "Local context preparation only; noisy observations, not a live SLO.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=101)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts" / "efficiency" / "report.json"
    )
    args = parser.parse_args()
    current = asyncio.run(measure(args.iterations))
    result = current
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        result = {"before": baseline, "after": current, "comparison": compare(baseline, current)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write(args.output, result)
    print(
        json.dumps(
            {"report": str(args.output), "deterministic_passed": current["deterministic_passed"]}
        )
    )
    if not current["deterministic_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
