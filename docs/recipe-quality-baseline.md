# ASC-56 offline engineering baseline

The replay passed all eight deterministic scenarios. **Live model quality,
judge scores and human calibration were not measured.** The user authorized
offline implementation/testing only; no Azure evaluation, deployment or agent
publication occurred. `quality_passed` remains `false`.

## Reproducible evidence

Run `python -m scripts.evaluate_quality` from a fresh output directory using the
[evaluation instructions](recipe-quality-evaluations.md). Full per-case
observations, grade inputs, review template and aggregate report persist locally
under `artifacts\quality-evaluation`; generated evidence is excluded from Git.
`--resume` and `--report-only` also reproduced the saved result without recapture.

| Provenance | Recorded value |
| --- | --- |
| Dataset | `asc56-recipe-quality-v1` |
| Dataset SHA-256 | `c14b6c47e080a1cd413e438f7ccae3eb4b82719d96deb77ca8d9b7679207a94a` |
| Starting commit | `3ee0735f31893302a326709e1f4c5e0a29a7789e` |
| Evaluated source-tree SHA-256 | `30336622c6829cf83f70f91f2e5c7e59a978c25d54b51285093c380498a47d72` |
| Working tree | Dirty: includes this feature's uncommitted capture/scoring implementation |
| SDK versions | `azure-ai-projects 2.8.0`, `openai 3.27.0`, `azure-identity 1.26.0` |
| Production policy versions | Orchestrator `2`; recipe `5` |
| Cloud spend | USD 0; no cloud calls; replay token counts are synthetic |
| Threshold approval | Pending collaborator review |

The source-tree digest includes production Python, evaluation scripts and both
replay fixtures. Prompt hashes and tool implementation/version details are in
`report.json`. The starting commit alone is not the evaluated implementation.
No live model or evaluator version is claimed for a fixed-response replay.

## Per-case result

| Required case | Observed outcome | Deterministic checks | Known nutrition |
| --- | --- | --- | --- |
| `normal-no-web` | Candidate recipe | Pass | Yes |
| `insufficient-groceries` | Candidate with shopping shortages | Pass | Yes |
| `unrealistic-time` | Clarification | Pass | No recipe |
| `missing-nutrition` | Candidate with explicit unavailable nutrition | Pass | No |
| `nutrition-timeout` | Controlled `orchestrator_specialist_failed` | Pass | No recipe |
| `conflicting-taste-nutrition` | Candidate recipe | Pass | Yes |
| `current-public-information` | Candidate with synthetic Web evidence | Pass | Yes |
| `creative-within-constraints` | Candidate recipe | Pass | Yes |

All eight required observations were captured. Known nutrition covers **5/8**
scenarios, not 8/8: transparent missing-data handling is not a nutrient fact.
The insufficient-pantry case needs 80 g more rice for its 100 g recipe when
only 20 g is available. The timeout reaches the injected nutrition boundary once
and terminates without retry; it is not an observed cloud outage.

No subjective grades were submitted: relevance has 0/7 required candidate scores,
groundedness 0/6, grocery utilization 0/6, feasibility 0/7 and controlled creativity
0/6. All five means are `null`; zero pass rates mean **no passing evidence**, not
an observed model score of zero. Calibration anchors and human labels remain
unmeasured. The seven cases with subjective requirements remain weak/unmeasured
in the report; the timeout has no preregistered subjective slots.

## Weaknesses and unsupported claims

Replay uses the same fixed rice/carrot draft for recipe cases. It verifies
production routing, schema/guard behavior and evidence accounting, **not** whether
the models satisfy conflicting taste/protein goals, make good shopping choices
or invent a useful creative variation. Those cases need live judges and
independent human review; no prompt optimization was performed to conceal them.

The output keeps the adaptation "Olive oil replaces butter; seasonings can be
adapted." separate from the source quote "Beginner preparation, 25 minutes."
Exact-quote checks validate that quoted evidence exists; they do not prove every
statement in adaptations, fit reasons or preparation prose. Human/rubric review
must identify unsupported nutrition, allergy, safety or source assertions there.

The Web case uses `fda.example.invalid` and explicitly labels its evidence as
synthetic, **not current FDA information or recall/safety clearance**. Known
nutrition comes from synthetic USDA records, not a claim about a real dinner.
The mandatory safety/authorization gate remains separate.

To establish a live-quality baseline, first obtain collaborator approval of the
prospective thresholds and authorization for paid dev execution, then submit,
export, independently label and calibrate a new run. Cloud cost remains unknown
unless reconciled with usage/billing evidence; do not substitute replay results
for that acceptance.
