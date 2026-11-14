# Model efficiency: ASC-58

The optimized model-input projection removes 20.2%-33.9% of context tokens in
four complete synthetic workloads. It omits only grocery `updatedAt` timestamps
and conversation `turnId` identifiers. The full backend context still contains
both fields. Grocery identifiers, quantities, units, ingredient identifiers,
complete conversation text, explicit constraints, learned evidence and the full
recipe-revision snapshot remain unchanged.

The prescribed models remain GPT-5.4-mini for orchestration, GPT-5.4 for Recipe +
Nutrition and GPT-5.1 nano for extraction. No agent definitions, version pins,
identities, model selection, tools or provider retry behavior changed.

## Before/after evidence

The versioned [machine-readable report](../evaluations/model-efficiency-baseline-v1.json)
records the baseline and candidate source-tree digests, starting Git commit,
dirty-worktree flags, published-instruction hashes, policy/SDK/Python versions,
tokenizer, workload hashes, per-call measurements and comparison checks.
The baseline used the original projection at `35315b357a77701d185a7c4ae04295a434043eb7`;
the candidate includes this PR's changes merged with that same dev revision.
The baseline worktree was clean; the candidate was measured before committing
the merge resolution.
Deployed agent versions were not read or invoked.

All four inputs fit the default budget without trimming in both arms. Counts
use the production `o200k_base` tokenizer and exclude the separately budgeted
64 framing tokens. They are local input-text counts, not provider usage.

| Complete context | Before tokens | After tokens | Reduction |
| --- | ---: | ---: | ---: |
| 3 groceries | 297 | 237 | 20.2% |
| 40 groceries | 2,644 | 1,844 | 30.3% |
| 100 groceries | 6,448 | 4,448 | 31.0% |
| 3 groceries and 10 exchanges | 799 | 528 | 33.9% |

Latency below measures **local `bound_context` preparation**, including JSON
serialization, not model/network latency. The default transport limit does not
tokenize; visible tokens are counted separately. Each arm used five
warm-ups and 101 measured iterations per workload on the same Python/SDK/OS.
Runs were sequential on a shared host; these observations are not an SLO.

| Context | Before p50 / p95 / p99, ms | After p50 / p95 / p99, ms |
| --- | ---: | ---: |
| 3 groceries | 0.0244 / 0.0593 / 0.1456 | 0.0148 / 0.0167 / 0.0308 |
| 40 groceries | 0.2365 / 0.2846 / 0.6136 | 0.1132 / 0.1507 / 0.2639 |
| 100 groceries | 0.6556 / 2.1088 / 2.4855 | 0.2722 / 0.3400 / 1.0158 |
| 10 exchanges | 0.0359 / 0.1540 / 0.8750 | 0.0277 / 0.0300 / 0.1135 |

These observations do not establish a universal local speedup or a measured
improvement in live model latency.

The existing eight-scenario [quality replay](recipe-quality-evaluations.md)
retained every deterministic factual check and the same outcomes: **8/8 passed
in both arms**. Both used 21 observed model calls: three per ordinary candidate,
one for clarification and two for the injected nutrition timeout. Visible
`request.prompt` text across these calls fell from 7,309 to 6,169 tokens (15.6%).
The replay's fixed synthetic usage receipts are deliberately not used to measure
token savings.

At the public standard uncached input reference prices checked for this run
([GPT-5.4-mini: $0.75/million](https://developers.openai.com/api/docs/models/gpt-5.4-mini),
[GPT-5.4: $2.50/million](https://developers.openai.com/api/docs/models/gpt-5.4)),
the replay's **visible-input-only** estimate falls from $0.01113075 to
$0.00961075 (13.7%). This excludes published instructions, schemas, tool results,
reasoning, output, caching and tool charges. It is not an approved Azure rate
card or a forecast of full-request Azure savings.

Actual cloud spend was $0. Provider token counts, full-request Azure cost and
live model latency remain unknown, not zero. Replay cannot establish model
quality or human calibration; `live_quality_passed` remains false. The existing
judge minimum, pass rate, pantry-utilization and calibration thresholds are
unchanged. A paid live run still requires the existing explicit approval.

## Configuration and unchanged limits

The default remains the upstream 131,072-character transport ceiling, with no
context token ceiling. This preserves token-heavy input rather than reinstating
the former 11,000-token restriction. `ASC_CONTEXT_MAX_TOKENS` is optional and
unset by default; setting 256-11,000 adds a conservative token ceiling, including
framing, without widening the transport limit. Apply opt-in changes through
reviewed application configuration. No deployment or configuration write is
required for the default optimization.

Trimming still removes oldest complete history exchanges first, then learned
entries, then groceries with an accurate omission count. If complete explicit
preferences, current input and the full revision cannot fit, chat returns
`422 context_limit_exceeded` before invoking a model. It never truncates these
mandatory fields or substitutes a model summary. Ownership, explicit-over-learned
precedence and final deterministic allergy checks are unchanged.

The report snapshots upstream policy v6: four normal model calls, at most one
separately bounded calorie fallback, two specialist attempts, one revision,
eight tool calls, one public web call, a 10-second web deadline, a 30-second
calorie deadline and a 60-second overall deadline. Upstream no longer rejects
valid recipe results using the former cumulative input/output token budgets;
this optimization does not restore those gates. Retrieval defaults remain three
results and 4,000 context tokens, with their existing configuration bounds.
Extraction remains at 6,000 input tokens, 1,000 output tokens and five prior notes.
These policies are identical in the baseline and candidate.

## Reproduce

Use the same Python 3.12 environment in separate baseline and candidate
checkouts. Run the candidate harness against each checkout's production and
replay modules; the baseline need not contain a copy of the new harness:

```powershell
$baseline = 'C:\work\ai-sous-chef-baseline'
$candidate = 'C:\work\ai-sous-chef-candidate'
$harness = Join-Path $candidate 'scripts\evaluate_efficiency.py'
Set-Location $baseline
$env:PYTHONPATH = Join-Path $baseline 'backend\src'
python -c "import runpy; runpy.run_path(r'$harness', run_name='__main__')" --output C:\temp\asc58-before.json
Set-Location $candidate
$env:PYTHONPATH = Join-Path $candidate 'backend\src'
python -m scripts.evaluate_efficiency --baseline C:\temp\asc58-before.json --output artifacts\efficiency\comparison.json
```

The harness uses only fixed synthetic inputs, production services and the
existing synthetic USDA MCP transport. It never loads live settings, creates
credentials, publishes sources or invokes a cloud model. Comparison rejects
changed workloads, budgets, models, published instructions, measurement
environments, required context, quality checks or model-call counts.
