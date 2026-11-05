# Deterministic hard-safety release gate

ASC-55 / [#57](https://github.com/AgamMakkar21/ai-sous-chef/issues/57) makes a
fixed synthetic safety/authorization suite fail closed in `CI / tests`.
A passing run means **zero observed hard-check violations on this required
suite**, not universal food safety, model quality or production isolation.

## Required set and deterministic oracles

[`tests/fixtures/hard-safety-v1.json`](../tests/fixtures/hard-safety-v1.json)
versions the required fixture IDs, expected outcomes and exact pytest node IDs.
The referenced tests and their existing synthetic fixtures supply the inputs
and executable assertions. They call production validators, context assembly,
orchestration, retrieval handling and the signed-JWT/two-user offline SDK harness.
No live model or subjective judge decides a hard outcome.

| Scope | Required outcomes |
| --- | --- |
| Peanut oil | Block peanut oil, groundnut/arachis aliases and peanut ingredients |
| Hidden allergens | Reject allergenic substitutes and allergens hidden in prose/metadata |
| Vegetarian meat/stock | Block animal meat, broth, stock, fats, gelatin and fish |
| Explicit versus learned | Keep stored restrictions authoritative; exclude conflicting/injected learning |
| Allergy override prompts | Reject unsafe candidates despite the user prompt and a model's acceptance |
| Malicious retrieval | Reject allergy-removal/identity/tool instructions; retain approved tool limits |
| Unsafe instructions | Reject unsafe temperatures, raw serving, storage and unsupported instructions |
| Unknown ingredients | Clarify uncertainty under restrictions; reject unknown full-recipe ingredients even without restrictions |
| Cross-user access | Enforce ownership, foreign-ID errors, pagination/cursors, linked references and authentication |
| Positive controls | Accept compliant ingredients, recipes, cooking rules, substitutions and owned API operations |

The fixed cross-user subset supplements, not replaces, the complete mandatory
[authorization matrix](../CONTRIBUTING.md#required-user-data-authorization-matrix).
The ordinary full test suite still runs in the same CI job. Existing mandatory
MVP allergy/authentication checks remain unchanged.

## Run and inspect evidence

From the repository root, using the existing locked Python 3.12 dev environment:

```bash
python -m scripts.evaluate_safety
python -m pytest tests/unit/test_safety_evaluation.py
```

The command writes `artifacts/safety-evaluation/report.json` and returns zero
only when every required node was collected exactly once and its setup, call
and teardown passed. Assertions, skipped cases, expected failures (including
unexpected passes), deselection, missing/duplicate cases, collection errors,
interruption and nonzero pytest results fail the gate. An expected safety
rejection is a **passing assertion**, not a failed evaluation.

Every completed runner invocation saves the suite version/manifest SHA-256,
required fixture IDs and expected results, every required node's phase outcomes,
and required/executed/verified coverage, including failed runs. Invalid or missing
manifests produce a failed diagnostic report rather than a passing empty suite.
`executed_tests` counts nodes with a call report; only `verified_tests` completed
all three phases successfully. The report labels checks `deterministic`, records
no model-scored checks, and contains only synthetic fixture metadata and bounded
outcomes: no JWTs, request/response bodies, stored records or failure tracebacks.

CI uploads that JSON as `hard-safety-evaluation` with `always()` after the tests
job, retaining it for 30 days even when the gate fails. GitHub associates the
artifact with the tested PR merge commit. The gate runs inside the existing
`CI / tests` check, so the six check names and release/deployment verification
contracts do not change. Actual merge enforcement still requires the repository
owner's [required-check settings](ci.md#owner-enable-required-checks-after-a-successful-run).
No cloud credentials, deployment or agent activation are involved.

## Negative controls and changes

Both controls must return **exit 1**, report failed hard checks, and keep compliant
controls passing:

```bash
python -m scripts.evaluate_safety --negative-control unsafe-candidate --output artifacts/safety-evaluation/unsafe-candidate.json
python -m scripts.evaluate_safety --negative-control ownership-bypass --output artifacts/safety-evaluation/ownership-bypass.json
```

The first temporarily lets deliberately unsafe structured candidates through
final validation. The second temporarily disables `require_owned`. These patches
exist only in this offline dev script, are restored on exit, and add no deployed
configuration or API bypass. Regression tests run each in an isolated subprocess
and verify that the real runner fails for the intended assertions, not a setup
error. Partial execution with otherwise successful pytest also fails.

When changing required inputs, assertions or node IDs, review and increment the
fixture manifest filename/version and its loader together. Do not remove a hard
case, mark it skipped/xfail, weaken its oracle or hide failure to make CI green.
Quality/creativity scoring belongs to ASC-56 / #51, not this gate. This suite does
not measure universal prompt-injection resistance, manufacturing cross-contact,
live provider behavior or deployed identity/storage enforcement.
