# ASC-57: preference learning versus explicit-only context

The synthetic experiment validates evidence accounting and context inclusion.
**It does not establish improved model recommendations.** Fixed-response replay
has the same preference alignment in both arms: 2/6 (33.3%), a zero-point change.
Independent review of the inclusion policy remains pending on this feature PR.

## Reproduce the bounded experiment

From the repository root, use Python 3.12 and the existing locked development and
worker dependencies:

```powershell
python -m scripts.evaluate_learning
```

The command runs ten fixed sequences from
`evaluations/preference-learning-v1.json`. It writes
`artifacts/learning-evaluation/report.json` and exits nonzero for missing,
duplicate, failed or skipped cases. PR CI runs this command and retains the
report as `preference-learning-evaluation` for 30 days. The full backend suite
also runs the acceptance cases and negative controls.

No Azure configuration, credentials or live user records are loaded. The real
Cosmos SDK uses the existing in-memory synthetic transport. Extraction responses
are scripted, but pass through the production structured-response validator.
Each delivery creates a fresh `LearningService`, exercising durable receipts,
source-revision validation, projection reduction and request-time
`ContextService` inclusion rather than a separate learning algorithm.

The clock is fixed at November 10, 2026. Every delivery has an independently
specified expected count, confidence and inclusion decision. Final evidence
identities and directions are separately labeled in the fixture. The three
explicit-conflict cases change the current profile after learning, ensuring that
old, formerly permissible learned state cannot bypass a new allergy, dietary
restriction or explicit dislike.

For each final state, the baseline and learned-enabled request envelopes differ
only in `learnedPreferences`. Both run through production orchestration, recipe
validation and final safety checks using the same fixed recipe response,
synthetic grounding and synthetic USDA transport. This is intentionally a
**no-improvement control**, not a substitute for running the models.

## Observed offline baseline

Sample: **10 synthetic sequences, 25 feedback deliveries/checkpoints,
20 current ledger assertions and 10 paired recommendation requests**.
There are six hand-authored heldout taste labels. The ambiguous case has no
label; the three explicit conflicts test precedence instead of scoring a
forbidden preference as a recommendation target.

| Metric | Explicit-only | Learned-enabled |
| --- | --- | --- |
| Final optional learned entries | 0/10 | 3/10, exactly the three expected cases |
| Final evidence precision and recall | Not applicable: no learned evidence | 20/20 correct, 20/20 expected recovered |
| Count/confidence correctness | Not applicable: no learned ledger | 25/25 checkpoints |
| Context inclusion decisions | Empty by definition | 25/25 checkpoints |
| Current explicit restrictions respected | 3/3 | 3/3 |
| Heldout confidence Brier score | Not applicable: no confidence emitted | 0.129352 over 6 labels |
| Recipe ingredient/preference alignment | 2/6 (33.3%) | 2/6 (33.3%); delta 0 percentage points |
| Live model recommendation quality | Not measured | Not measured |

Evidence precision counts unique current source/revision/direction assertions
that match the fixture's independent gold list, divided by all emitted current
assertions. Recall divides those matches by all expected assertions. Neither
metric measures how accurately GPT-5.1 nano interprets natural language: its
responses are scripted in this run.

Brier score is the mean squared difference between raw ledger confidence and a
heldout binary synthetic taste label; lower is better. Labels are not passed to
the learner. This small, hand-selected sample is **not population calibration**
or a probability guarantee. Excluded/ambiguous labels remain explicit, and
missing required scores produce `null`, not an improved denominator or a zero.
No neutral 0.5 forecast is invented for the explicit-only arm.

| Sequence | Final evidence/support/opposition | Confidence | Included |
| --- | --- | --- | --- |
| Repeated citrus | 3/3/0 | 0.80 | Yes |
| One clear citrus like | 1/1/0 | 0.666667 | No |
| One ambiguous note | 0/0/0 | None | No |
| Contradictory feedback | 4/2/2 | 0.50 | No |
| Revised feedback | 2/1/1 | 0.50 | No |
| Duplicate deliveries | 2/2/0 | 0.75 | Yes |
| Out-of-order old revision | 2/2/0 | 0.75 | Yes |
| New milk allergy | 2/2/0 in stored projection | 0.75 | No |
| New vegetarian restriction | 2/2/0 in stored projection | 0.75 | No |
| New explicit lime dislike | 2/2/0 in stored projection | 0.75 | No |

The contradiction sequence first admits two supporters at 0.75, then withdraws
the insight when opposition lowers confidence to 0.60, despite retaining two
supporters. The revised sequence replaces the old revision rather than adding a
third occasion. Duplicate deliveries invoke extraction twice for two unique
sources, not four times. The stale out-of-order revision adds no evidence.

## ASC-23 minimum inclusion policy: review requested

This PR names and tests the **existing** production thresholds; it does not lower
them or change runtime behavior. The optional context slot uses
`MIN_LEARNED_COOKED_SUPPORT = 2` and `MIN_LEARNED_CONFIDENCE = 0.65` from
`domain/context.py`. Independent collaborator approval of this policy is still
required; this document does not claim that review has happened.

Only current, owned, independently cooked supporting occasions count toward the
two-support minimum. Evidence must be within 180 days and not in the future.
The request-time gate recomputes
`(3 + supportUnits) / (6 + supportUnits + oppositionUnits)`, with cooked evidence
weighted three, rather than trusting stored counts or model-proposed confidence.
The scripted extractor proposes 0.99; a single support still yields 0.666667 and
is excluded. Negative controls prove the experiment fails if either the
two-support minimum or the confidence floor is bypassed.

Explicit allergies, dietary restrictions and preferences always take precedence.
The existing stable ranking and maximum of 20 learned entries remain unchanged;
budget trimming can remove learned entries but never mandatory explicit inputs.
These fixtures support retaining this conservative gate, not claiming it is an
optimal threshold. Any threshold change requires new evidence and peer review.

## Provenance and limitations

Every report records the dataset hash, starting commit, dirty-state flag, source
tree and prompt hashes, SDK versions, production policy versions, model IDs
`gpt-5.4-mini`, `gpt-5.4` and `gpt-5.1-nano`, the extraction source hash and the
acceptance-test hash. Both arms share those pins. Live backing model versions are
`null`: naming a model does not make scripted replay a live model experiment.

The fixture SHA-256 is
`0be9e3b6f3890cfa0ec9a7c7052d4372a260e841217555bf242e2ac82808b495`.
The machine-readable report contains every checkpoint, matched input envelope
and per-arm ingredient outcome. It is the provenance source for each run;
the starting commit alone does not identify an uncommitted evaluated tree.

The fixed carrot/rice recipe never uses the learned target, so the two negative
taste labels align and the four positive labels do not. This confirms that
better learned-context coverage alone does not demonstrate better
recommendations. No claim is made about live extraction accuracy, causality,
statistical significance, generalization, long-term retention or model quality.
Paid dev model execution and independent human labeling remain separate work;
this experiment neither provisions resources nor changes prompts or providers.
