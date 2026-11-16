# Bounded fault and load acceptance (ASC-62)

Run the fixed **offline** matrix with the locked Python 3.12 development and worker
dependencies installed:

```powershell
$env:PYTHONPATH = 'backend\src'
python -m scripts.evaluate_resilience
```

Run from the checkout being tested. The existing USDA transport tests start a
sanitized subprocess, so the application's editable installation must also point
at that checkout, as described in [CI setup](ci.md). No credentials, dev URL,
cloud resources, model calls, telemetry ingestion or notification routing are
needed. This command has no live mode.

The report is `artifacts/resilience/report.json`; CI retains it as
`fault-load-acceptance` for 30 days. It records source/test hashes, SDK/policy
versions, exact required test outcomes and measured load samples. Missing, skipped,
duplicate, failed or interrupted cases cannot pass. Missing/incomplete load
measurements, failed cleanup or breached measured guards also fail the command.
Provider token usage and live performance remain explicitly unmeasured.

## Small acceptance matrix

The runner reuses existing production-path regression tests rather than building
another fault-injection service. Synthetic remote transports sit beneath the
pinned SDKs; no deployed bypass or fault endpoint is added.

| Scenario | Required behavior | Corresponding operational runbook |
| --- | --- | --- |
| Cosmos 429, recovered and exhausted | At most three SDK attempts; observed throttles include recovery | [Database](operations.md#database) |
| Agent 429, timeout and lost connection | Explicit API error; no duplicate execution; uncertain outcomes stay fenced | [Agents and tools](operations.md#agents-and-tools) |
| USDA transient search/profile faults and timeout | Bounded real MCP retries or terminated subprocess; no invented nutrition | [Agents and tools](operations.md#agents-and-tools) |
| Web timeout and invalid structured output | No persisted recipe/facts; fresh request works after dependency recovery | [Invalid outputs](operations.md#invalid-outputs) |
| Duplicate sends and feedback | One execution/evidence revision; pending or conflict response as appropriate | [Application](operations.md#application) |
| Concurrent inventory and profile changes | ETag conflict plus explicit retry preserves unrelated changes | [Database](operations.md#database) |
| Worker interruption/recreated recovery service | Durable backoff and receipts resume without repeated extraction/evidence | [Learning](operations.md#learning) |
| Deletion during extraction and late replay | Tombstones fence writes; late work cannot recreate deleted data | [Deletion](data-retention.md) |
| Failure/dedup/lag telemetry | Classified responsible operation and useful counters; private canaries absent | [Operations signals](operations.md#signals-and-limitations) |

Unknown agent outcomes deliberately require reconciliation rather than blind
replay. A healthy fresh conversation works without manual data repair; the runner
does not claim the uncertain original request was safely executed twice.

## Measured workload and guards

Twelve synthetic signed-JWT users each onboard and create one conversation before
measurement. Four requests at a time traverse real authentication, API, context,
Orchestrator/Recipe services, Foundry SDK, USDA MCP subprocess, final safety gate
and Cosmos SDK persistence. Foundry responses have a fixed 5 ms synthetic delay;
each request gets independent response-fixture state.

Latency covers the HTTP send through its complete response, excluding setup,
semaphore queueing and cleanup. The report retains individual samples and actual
status counts, error rate, p50/p95/p99, peak in-flight requests, model-call counts
and locally tokenized input. Percentiles use inclusive interpolation; twelve
samples are a smoke measurement, not a statistically established capacity limit.

Proposed **local regression guards**, not approved live service-level objectives:
concurrency cap 4 with observed overlap, no HTTP failures, p95 <= 15 seconds,
per-request latency <= the current 120-second orchestration deadline, model calls
within the current policy and an explicitly opted-in 4,096-token context cap.
No new default or cumulative model token limit is introduced. Locally counted
Responses input fields exclude instructions, schemas and output; synthetic usage
receipts are not real provider token or billing measurements.

Cleanup runs in `finally` through the real repository deletion workflow. Every
created partition must contain only its three permanent content-free deletion
guards/tombstones, which are intentional safeguards, not orphan user data.
Other matrix tests use disposable in-memory partitions and leave no remote data.

### Recorded offline capture

[`evaluations/fault-load-offline-v1.json`](../evaluations/fault-load-offline-v1.json)
records **31/31 cases passing** on the shared Windows host. The twelve requests
reached peak concurrency 4; all returned HTTP 200, with zero measured budget
breaches and verified cleanup. The report retains all measured latency percentiles.
Each used three synthetic model responses, 185 context tokens including framing
and 726 locally counted visible input tokens across its Responses calls.
These are synthetic-transport timings, not live model latency or Azure capacity.

The capture started from `c336ef4` with the candidate changes uncommitted; its
source/test hashes identify the measured implementation. Do not relabel this
capture as a later commit or combine its percentiles with another environment.
CI creates a fresh exact-source report independently.
Fresh chat runs additionally count the bounded AI USDA selection invocation;
the historical capture above is unchanged.

## Live acceptance remains pending

Offline telemetry assertions exercise the signal contracts used by the shared
operational KQL; they do **not** execute Azure alerts, prove notification delivery
or establish deployed capacity. Follow the [operational acceptance procedure](operations.md#notification-acceptance)
only after an owner approves the exact dev resource/commit, synthetic test users,
window, request/rate/concurrency caps, spend/RU/model-token limits, stop conditions
and an existing non-paging action-group destination.

Retain observed live percentiles/errors/budget breaches and opaque alert
fired/resolved evidence; verify dependency recovery and deletion of all synthetic
operational data, leaving only required tombstones. Restore the reviewed disabled
alert configuration. Do not page production, broaden access, increase throughput,
remove deletion guards or mark live criteria complete from green offline CI.
