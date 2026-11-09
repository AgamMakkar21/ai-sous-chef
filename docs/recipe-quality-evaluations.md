# Recipe quality evaluations

ASC-56 / [#51](https://github.com/AgamMakkar21/ai-sous-chef/issues/51) adds an
offline-first, synthetic-only harness and opt-in New Foundry evaluation runs.
It measures the existing integrations, not product/model availability.
Quality scores never replace the mandatory
[deterministic safety and authorization gate](hard-safety-evaluations.md).
This post-MVP suite does not add a cloud-dependent PR or MVP deployment gate.

## Dataset, facts and rubrics

[`evaluations/recipe-quality-v1.json`](../evaluations/recipe-quality-v1.json)
contains eight required scenarios: ordinary cooking without Web IQ, insufficient
groceries, impossible time, unavailable nutrition, nutrition timeout, conflicting
taste/nutrition, a justified current-public-information request, and constrained
creativity. Inputs contain no real users, profiles, history or learned preferences.
The loader rejects missing/duplicate cases, removed scoring requirements, missing
anchors and relaxed v1 thresholds.

Both replay and live capture call production `OrchestratorService`,
`RecipeSpecialistService`, final recipe safety validation and inventory logic.
Evaluation-only wrappers record tool attempts/results, measured token usage,
source versions, exact evidence quotes and nutrition receipts. They neither add
application endpoints nor alter deployed adapters or policy. The missing-data
and timeout cases deliberately inject a fault at the nutrition boundary; the
timeout is not a measurement of provider latency. A failure before that boundary
cannot pass as the expected injected timeout.

Replay defaults to the combined grounding route now pinned in dev. Live capture
preserves `ASC_FOUNDRY_RECIPE_NUTRITION_MODE`; both phased and combined routes are
tested offline. Combined retrieval and Web IQ share one model invocation and one
usage receipt, while retaining separate tool evidence. The configured mode is
recorded and must match on resume.

| Dimension | Evidence |
| --- | --- |
| Relevance | Foundry `builtin.relevance` on the full synthetic request and response |
| Grounding | Foundry `builtin.groundedness` on source claims, plus deterministic source/version/exact-quote checks |
| Grocery utilization | Pantry/shortage reconciliation and pantry coverage, plus a custom meal-usefulness rubric |
| Feasibility | Custom practical preparation/quantities/equipment rubric, including impossible-time clarification |
| Time/skill adherence | Deterministic limits plus practical feasibility review; a reported time alone does not prove feasibility |
| Nutrition provenance | Exact tool receipt/request equality, serving math, six-nutrient coverage and explicit unknowns |
| Necessary tool selection | Observed tools, mandatory recipe/nutrition evidence, justified Web IQ and bounded calls/retries |
| Controlled creativity | Custom rubric distinguishes useful constrained adaptation from unsupported source, safety or nutrient claims |

Nutrition arithmetic uses the production USDA MCP adapter and aggregation in
replay, backed by the existing synthetic USDA server. It checks whole-recipe to
per-serving values; the existing USDA tests independently check fixed per-100-g
source values, quantities, units and rounding. Missing quantities, partial
coverage and unavailable nutrients are not zero or proven facts.

## Offline replay

Use the existing locked Python 3.12 dev environment from the repository root:

```powershell
python -m scripts.evaluate_quality
python -m pytest tests\unit\test_quality_evaluation.py tests\integration\test_quality_foundry.py
```

The default creates `artifacts\quality-evaluation` and refuses to overwrite an
existing directory. Use a fresh `--output` directory for another run. Replay
never loads cloud settings or creates cloud credentials. Its fixed gateway
responses test capture, production guards, math, failure handling and reporting;
**they do not measure the deployed models' quality**.

Every run exports `suite.json`, `observations.json`, `dataset.json`,
`review-template.json`, `state.json` and `report.json`. Live collection also saves
`outputs.json`, including per-criterion reasons but excluding provider reasoning
samples and raw exception text. The report separates deterministic outcomes,
subjective scores, source claims, adaptations, unavailable facts and weak cases.
Replay exit 0 means only that all required deterministic replay cases passed.
It always reports `quality_passed: false`.

The [offline baseline](recipe-quality-baseline.md) documents the measured result
and its limitations. The report's commit identifies the starting revision; its
source-tree hash and dirty flag identify the actual evaluated working tree.
Prompt/policy hashes, SDK versions, replay fixture hashes and observed source
versions are retained. Live reports additionally record immutable agent pins,
backing model/deployment versions, exact evaluator versions and Foundry run IDs.

## Approved live baseline

Run only in the configured **dev workload**, with its existing environment,
distinct agent identities, USDA setup and managed identity. This command uses
the existing single-credential factory: dev uses managed identity, without a
developer-credential fallback. It does not create resources, grant access, publish
agents, switch providers or deploy anything.

The operator needs the existing Foundry evaluation permissions and an approved
structured-output-capable judge deployment. Do not copy secrets into arguments,
dataset files, reports or source control. Use only a curated corpus approved for
evaluation, not private recipes or production/user traces.

After obtaining approval for the paid run, set approval only in that process:

```powershell
$env:ASC_APPROVE_FOUNDRY_EVALUATION = '1'
try {
    python -m scripts.evaluate_quality --live --judge-deployment '<approved-existing-deployment>' --output artifacts\quality-evaluation\live-v1
} finally {
    Remove-Item Env:\ASC_APPROVE_FOUNDRY_EVALUATION
}
```

Eight requests retain production per-request call/token/deadline limits:
at most 32 target model invocations. The Foundry run contains eight observed rows
plus four calibration anchors and five graders, or 60 grading slots; service-side
judge token use/retries are controlled by Foundry, not a local dollar cap.
Approval must account for both target and judge costs.

The harness registers three content-hashed custom prompt evaluators, resolves
the two built-ins to exact versions, creates an evaluation through
`AIProjectClient.get_openai_client().evals.create`, and submits inline JSONL
through `evals.runs.create`. It follows all result pages and locally reapplies
the fixed thresholds instead of trusting a service pass flag.

The polling wait budget is at most 900 seconds; bounded network requests and
output export add time. A pending, failed, incomplete or
uncalibrated live run returns exit 2, not success. With process approval restored,
collect the **same run** using:

```powershell
python -m scripts.evaluate_quality --live --resume --output artifacts\quality-evaluation\live-v1
```

Resume checks the original project, agent pins, dataset, observation hash and
implementation hash. It never recaptures target responses or resubmits work.
Submission intent is saved before remote creation and each returned ID is saved
immediately. A create timeout with no saved run ID is ambiguous: reconcile it in
Foundry using the saved evaluation ID/suite metadata; do not retry create just
to obtain a green result. Start a deliberately approved new run only after
accounting for the first attempt.

Target/provider errors preserve safe categories and partial evidence. Unknown
tokens and costs stay unknown. Reports include observed target tokens, uncounted
invocations, judge usage and any service cost estimate; live total USD remains
`null` until reconciled with the approved rate card/invoice. A service target-only
or partial estimate must not be presented as complete target-plus-judge billing.
Replay records zero cloud spend explicitly because no cloud call occurs.

## Prospective thresholds and human calibration

Version 1 fixes the thresholds **before a live candidate is measured**:
each applicable judge score must be at least 4/5, every required case and metric
must pass, pantry coverage must reach 50% where applicable, and human versus judge
scores may differ by at most one point. No CLI option relaxes them.
These are **proposed thresholds pending peer review**, not a claim that review
or model/human calibration has already happened.

Required denominators come from the manifest, not returned rows. Missing,
duplicate, unexpected, skipped, errored, nonnumeric or mismatched-input results
fail closed. Missing grades remain in the denominator, and an incomplete metric
has no reported mean. Impossible-time cases preregister only relevance and
feasibility; the controlled timeout has no subjective quality slots. Neither can
inflate another metric's denominator. Transparent unavailable-nutrition handling
may pass its behavior check, but contributes no known nutrient fact to the
separate availability count.

The four anchors contrast practical/creative grounded adaptations with invented
claims and constraint violations. Their v1 ranges are developer-authored
references, **not completed human labels**. A collaborator must review the rubric,
thresholds, anchor ranges and every applicable candidate/anchor score:

1. Copy `review-template.json` to `review.json` inside the run directory.
2. Record a reviewer handle, approve the thresholds, and fill every 1-5 score after independently reading the request, response and evidence.
3. Keep the suite/observation hashes unchanged; investigate disagreements rather than modifying thresholds to rescue a candidate.
4. Rebuild the report without credentials or network calls:

```powershell
python -m scripts.evaluate_quality --report-only --output artifacts\quality-evaluation\live-v1 --review artifacts\quality-evaluation\live-v1\review.json
```

Quality passes only with completed cloud output, all factual checks, all required
grades, correct anchor ranges, matching human review and acceptable agreement.
Report-only never converts a replay into a live baseline. Threshold/rubric changes
require a reviewed new dataset version and a new baseline, not editing an existing
candidate's policy until it passes.

SDK contracts follow Microsoft's
[dataset evaluations](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/cloud-evaluation-datasets),
[custom evaluators](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/custom-evaluators)
and [result export](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/cloud-evaluation-results)
documentation. Tests exercise the installed New Foundry/OpenAI SDKs over synthetic
HTTP transports, including evaluator registration, inline submission, pagination,
bounded polling and ambiguous completion.
