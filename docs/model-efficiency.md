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
The baseline used the original projection at `7b83f9ac89d06828aa35e73655e3107e11b8f074`;
the candidate includes this PR's changes. Both were measured before committing.
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
serialization and tokenization, not model/network latency. Each arm used five
warm-ups and 101 measured iterations per workload on the same Python/SDK/OS.
Runs were sequential on a shared host; these observations are not an SLO.

| Context | Before p50 / p95 / p99, ms | After p50 / p95 / p99, ms |
| --- | ---: | ---: |
| 3 groceries | 0.387 / 0.839 / 1.220 | 0.261 / 0.588 / 0.910 |
| 40 groceries | 3.745 / 5.968 / 8.875 | 2.483 / 3.950 / 4.815 |
| 100 groceries | 8.059 / 9.596 / 20.861 | 5.644 / 6.930 / 7.582 |
| 10 exchanges | 0.659 / 1.372 / 1.704 | 0.726 / 1.241 / 1.634 |

The history workload's p50 increased in this run. Do not claim a universal local
speedup or a measured improvement in live model latency.

The existing eight-scenario [quality replay](recipe-quality-evaluations.md)
retained every deterministic factual check and the same outcomes: **8/8 passed
in both arms**. Both used 21 observed model calls: three per ordinary candidate,
one for clarification and two for the injected nutrition timeout. Visible
`request.prompt` text across these calls fell from 7,274 to 6,134 tokens (15.7%).
The replay's fixed synthetic usage receipts are deliberately not used to measure
token savings.

At the public standard uncached input reference prices checked for this run
([GPT-5.4-mini: $0.75/million](https://developers.openai.com/api/docs/models/gpt-5.4-mini),
[GPT-5.4: $2.50/million](https://developers.openai.com/api/docs/models/gpt-5.4)),
the replay's **visible-input-only** estimate falls from $0.01104325 to
$0.00952325 (13.8%). This excludes published instructions, schemas, tool results,
reasoning, output, caching and tool charges. It is not an approved Azure rate
card or a forecast of full-request Azure savings.

Actual cloud spend was $0. Provider token counts, full-request Azure cost and
live model latency remain unknown, not zero. Replay cannot establish model
quality or human calibration; `live_quality_passed` remains false. The existing
judge minimum, pass rate, pantry-utilization and calibration thresholds are
unchanged. A paid live run still requires the existing explicit approval.

## Configuration and unchanged limits

`ASC_CONTEXT_MAX_TOKENS` can lower the context limit from its default 11,000
tokens, including framing. Valid configuration is 256-11,000; it cannot widen
the reviewed ceiling. Apply changes through reviewed application configuration.
No deployment or configuration write is required for the default optimization.

Trimming still removes oldest complete history exchanges first, then learned
entries, then groceries with an accurate omission count. If complete explicit
preferences, current input and the full revision cannot fit, chat returns
`422 context_limit_exceeded` before invoking a model. It never truncates these
mandatory fields or substitutes a model summary. Ownership, explicit-over-learned
precedence and final deterministic allergy checks are unchanged.

The report snapshots the existing policy: 16,000 input tokens per call, 32,000
total input, 6,000 total output, four normal model calls, at most one separately
bounded calorie fallback, two specialist attempts, one revision, eight tool
calls, one public web call and a 60-second overall deadline. Retrieval defaults
remain three results and 4,000 context tokens, with their existing configuration
bounds. Extraction remains at 6,000 input tokens, 1,000 output tokens and five
prior notes. None of these ceilings increased.

## Reproduce

Use the same Python 3.12 environment in separate baseline and candidate
checkouts. Copy `scripts\evaluate_efficiency.py` into the baseline checkout,
which predates the harness. Set `PYTHONPATH` to each checkout's `backend\src`.
Run the first command on the baseline and the second on the candidate:

```powershell
python -m scripts.evaluate_efficiency --output C:\temp\asc58-before.json
python -m scripts.evaluate_efficiency --baseline C:\temp\asc58-before.json --output artifacts\efficiency\comparison.json
```

The harness uses only fixed synthetic inputs, production services and the
existing synthetic USDA MCP transport. It never loads live settings, creates
credentials, publishes sources or invokes a cloud model. Comparison rejects
changed workloads, budgets, models, published instructions, measurement
environments, required context, quality checks or model-call counts.
