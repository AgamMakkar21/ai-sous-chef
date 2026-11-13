# Operations baseline (ASC-54)

`infra/operations.bicep` adds one Azure Monitor workbook and eleven log alerts to
an **existing** environment-specific workspace/Insights pair. It does not change
the application, agents, data, identities, log retention, or action-group receivers.
The foundation and automatic dev deployment do not invoke this template.
Both committed environment files leave alerts **disabled**. The proposed defaults
are not approved service-level objectives or permission to page anyone.

## Signals and limitations

All queries use the same `monitoring/operations.kql` definitions. Production
sources restrict `_ResourceId` to the selected Insights component; synthetic
sources contain only in-memory datatables. There are no notes, recipes, prompts,
user IDs, exception messages or tool payloads in the workbook/notification fields.
Application spans carry a stable `asc.operation` because the Azure exporter
renames HTTP spans to method/route and moves status codes into `ResultCode`.
The queries also handle the exporter's stringified boolean evaluation outcomes.

| Alert | Proposed threshold in a 15-minute window | Runbook |
| --- | --- | --- |
| Application failures | 5 HTTP 5xx spans; expected 4xx excluded | [Application](#application) |
| Agent failures | 5 failed/cancelled agent spans | [Agents and tools](#agents-and-tools) |
| Tool failures, including USDA | 5 failed/cancelled tool spans | [Agents and tools](#agents-and-tools) |
| Learning failures | 3 failed/invalid/quarantined batch documents | [Learning](#learning) |
| Cosmos failures | 3 failed database spans | [Database](#database) |
| Processing lag | maximum observed lag >= 300,000 ms | [Learning](#learning) |
| Cosmos throttles | 3 observed SDK throttles, including recovered ones | [Database](#database) |
| Model latency | observed p95 >= 15,000 ms | [Models](#models) |
| Model tokens | >= 100,000 reported input + output tokens | [Models](#models) |
| Invalid structured outputs | 1 classified schema/structure rejection | [Invalid outputs](#invalid-outputs) |
| Evaluation regressions | 1 reported failed versioned evaluation | [Evaluation](#evaluation) |

Evaluation runs every five minutes; one failing period triggers, severity is 2,
and stateful alerts automatically resolve after healthy evaluations. Override
`thresholds` by signal ID, `windowSize`, `evaluationFrequency`, `failingPeriods`,
`severity`, `enabledSignals`, and `routing` separately for each environment after owner review.
Thresholds must be positive; do not activate unreviewed zero/negative overrides.

Health separates application, agents, tools, database, learning and model work.
Counts are **observed spans**, not unique requests: nested agent/tool spans can
describe the same failure. Percentiles are from observed spans, not metric
histogram averages. Sampling can undercount calls and tokens. Use sample ratio 1
for an approved bounded acceptance run. The workbook exposes last-seen timestamps;
an empty panel or absence of failures is **unknown**, not proof of health.

Lag measures persisted feedback age **when a delivery runs**, not current backlog
or a stopped worker. Learning counters distinguish commit, dedup, replay,
quarantine and batch failure. Platform-host failures before application telemetry
starts need the existing Function host diagnostics; this baseline cannot detect
an unstarted host from absent application events.

Tokens and USD remain null when required usage/pricing is absent. `modelPrices`
accepts exact reported model IDs with `inputUsdPerMillion` and
`outputUsdPerMillion` in USD, both nonnegative and reviewed against the applicable
deployment/date. Empty defaults mean **unknown**, not free. Extraction currently
reports tokens but no model attribute, so its cost remains unknown. These are
sampled token estimates, excluding cached-token discounts and infrastructure;
use Azure Cost Management for billing. No price or live quality gain is invented.

## Validate and deploy

Run the normal safety gate, backend checks and infrastructure suite. Then execute
the **actual shared KQL** against Azure's query engine using synthetic datatables:

```bash
python -m scripts.monitoring check-synthetic \
  --workspace-id '<approved-workspace-customer-guid>' \
  --output artifacts/monitoring/synthetic.json
```

This uses the signed-in operator's Azure CLI credential, requires query permission
on that workspace, and never ingests data or reads application rows. Missing,
duplicate, unexpected or failing checks cannot pass. It checks success/failure,
expected HTTP 4xx exclusion, lag, recovered throttles, latency, unknown tokens,
pricing, learning outcomes, schema rejection and evaluation failure. It does not
test notification routing. Offline CI checks compiled resources and bounded
evaluation publication; it does not claim to execute KQL or contact Azure.

Render any signal ID or `Health`, `Models`, `Learning`, `Lag`, `Evaluation` with:

```bash
python -m scripts.monitoring query application-failures \
  --insights-id '<approved-application-insights-resource-id>'
bicep build infra/operations.bicep --stdout >/dev/null
az deployment group what-if --subscription "$ASC_AZURE_SUBSCRIPTION" \
  --resource-group "$ASC_AZURE_RESOURCE_GROUP" \
  --template-file infra/operations.bicep \
  --parameters infra/environments/dev-operations.bicepparam \
  --result-format ResourceIdOnly
```

On Windows use PowerShell environment syntax and backslash file paths. Stop if the
preview changes anything except this workbook and the eleven named alert rules.
The owner reviews subscription, group, workspace/Insights binding, location,
thresholds, log-query costs and existing action groups. No new RBAC is granted.
Use scoped Monitoring Contributor or equivalent deployment access and existing
query permission, not a subscription-wide privilege escalation.

Only after independent review and an acceptable preview, run the same explicit
target/parameters with `az deployment group create --mode Incremental --output none`.
Use `prod-operations.bicepparam` only with separately approved production inputs.
Reapplying the same inputs updates stable resource names.

Activation requires a reviewed parameter file containing:

```bicep
param routing = {
  mode: 'approved'
  actionGroupResourceIds: ['<existing-owner-approved-action-group-resource-id>']
  reviewReference: '<independent-review-url>'
}
param enabledSignals = ['evaluation-regression']
```

The discriminated parameter requires both fields; `disabled` has no destination.
Only explicitly selected signal IDs can activate; the default selection is empty.
Retain approved inputs on later deployments. To stop notifications, redeploy this
template with `routing={mode:'disabled'}` and read back `properties.enabled=false`
on every rule. Merely omitting the template from an incremental deployment does
not disable existing alerts. Do not edit the unrelated foundation or delete data.

## Notification acceptance

**Pending until an owner approves a non-paging destination and a test window.**
Query execution and green CI are not notification-delivery evidence.

1. Deploy reviewed rules disabled first. Read back their queries, scopes, action
   groups, thresholds, `autoMitigate=true` and workbook scope.
2. With the owner present, temporarily route only `evaluation-regression` to the
   approved test group, set its threshold to 1, keep all other rules disabled,
   and confirm the group's **common alert schema** and resolved notifications.
3. Use a copy of the real synthetic safety report, with `passed=false`, and run
   the explicit publisher below. This emits only suite, boolean and opaque run
   ID, not the report body. Verify the event appears in the target workspace.
4. Record the opaque Azure alert ID, fired timestamp and recipient confirmation.
   Stop publishing failures; wait for the lookback window and Azure's healthy
   evaluation requirement. A passing event does not cancel a failure still in
   the window. Verify the same alert resolves and the destination receives it.
5. Restore the reviewed disabled configuration and read it back. Record evidence
   in the PR without personal contact details. If either notification is absent,
   leave this criterion open and investigate routing; do not page production.

## Application

Compare HTTP 5xx, last-seen time and dependency health. Follow an opaque trace ID
with the [existing trace query](../README.md#synchronous-opentelemetry-asc-52);
project only operation names, duration, result and allowlisted error categories.
Check deployment provenance, readiness, configuration and dependency failures.
Roll back through the reviewed release process if the new version is responsible.
Resolve only after a synthetic request succeeds and the alert returns healthy.

## Agents and tools

Separate agent control failures from `foundry.responses`, Foundry IQ and USDA
search/profile failures. Check reviewed model/agent pins, distinct identities,
timeouts, provider throttling, Search binding and protected USDA configuration.
Never print secrets or raw provider/tool responses. Retry only through existing
bounded application behavior. Do not disable safety checks or add a fallback
model/provider to clear the alert. Verify a synthetic request and normal latency.

## Learning

Compare batch failure/quarantine counters, extraction spans, observed lag,
deduplication and ledger-commit counters. Check worker configuration, extraction
identity/model pins and Cosmos access. A quiet worker may be stopped; verify host
health independently. Follow [bounded retry/replay](preference-learning.md), fix
the classified cause, and use only the operator-approved durable replay path.
Never rewrite feedback, remove dedup markers or replay arbitrary user partitions.
Verify one synthetic commit and one duplicate with no second evidence addition.

## Database

Compare failed operations, RU observations and recovered throttle counts. Check
429s, retry budgets, private networking, container-scoped access and provisioning.
Do not grant broader roles, disable isolation or loop writes. Any throughput
change needs owner cost approval. Verify scoped synthetic reads/writes through
existing acceptance procedures, then confirm throttles/failures fall below policy.

## Models

Separate `foundry.responses` from `learning.extract` in the health panel. Check
usage coverage before interpreting token totals or price estimates. Compare
model/prompt versions and bounded context/call budgets against the last reviewed
baseline. Investigate p95 with individual operation timing; do not widen budgets,
change model pins or relax explicit restrictions without review. Resolve after a
matched synthetic workload meets the reviewed latency/token threshold.

## Invalid outputs

Inspect only the classified rejection and responsible operation. Compare deployed
schema/prompt/policy versions, then reproduce with synthetic input. Keep fail-closed
parsing and deterministic final allergy validation. Do not log rejected content or
coerce invalid model output into a successful recipe. Verify the fixed safety gate
and targeted schema tests before independently reviewed deployment.

## Evaluation

Evaluation alerts are **explicit report publication**, not automatic CI ingestion.
No new workflow gets Azure credentials. After reviewing the exact versioned report,
an authorized operator may publish only its boolean summary:

```bash
python -m scripts.monitoring publish-evaluation --suite safety \
  --report artifacts/safety-evaluation/report.json
```

`learning` accepts `artifacts/learning-evaluation/report.json`. Unknown reports or
missing/nonboolean outcomes fail closed. Arbitrary report fields are never copied.
Publishing requires `ASC_TELEMETRY_ENABLED=true` and the existing Insights export
configuration. On a deployed host use its managed identity; a local operator uses
the existing local/Azure CLI credential policy. This is an explicit telemetry
write and needs approval; query acceptance does not invoke it.

Compare suite/fixture/policy and source versions in the retained report. A failed
hard-safety gate blocks promotion; do not lower thresholds or remove test cases.
Reproduce the fixed synthetic suite and review the fix independently. A later
pass does not erase the earlier event. Wait for healthy alert resolution and
preserve the report/alert evidence. Synthetic preference replay is not a live
model-quality measurement.
