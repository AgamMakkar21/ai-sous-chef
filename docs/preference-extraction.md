# Evidence-linked preference extraction (ASC-47 / #48)

`worker/extraction.py` supplies one bounded, private New Foundry extraction
attempt. It returns validated proposals or an explicit successful `no_insight`;
it never writes profiles, evidence or processing status. Durable application
is implemented by [ASC-48 / #42](preference-learning.md); operational attempt
budgets and retry handling belong to ASC-49 / #46.
The change-feed processor invokes this adapter with owned context and retains
durable application receipts. Host health and saved feedback alone do not mean
personalization has updated.

## Configuration and identity

The adapter uses pinned `azure-ai-projects==2.8.0`, its project-integrated
Responses client and the Function's distinct managed identity. It has no API-key,
developer-credential, classic-agent or Chat Completions fallback.

| Environment setting | Required value |
| --- | --- |
| `ASC_FOUNDRY_PROJECT_ENDPOINT` | HTTPS New Foundry project URL ending in `/api/projects/<project>` |
| `ASC_WORKER_EXTRACTION_DEPLOYMENT` | Explicit owner-approved existing GPT-5.1 nano deployment name; never `latest` |
| `ASC_WORKER_EXTRACTION_MODEL` | Expected returned model ID: `gpt-5.1-nano` or its exact dated ID |
| `AZURE_CLIENT_ID` | The separate Function workload identity's client UUID |

`ExtractionConfig.from_environment()` fails closed if a setting is absent or
invalid. A different returned model fails extraction rather than silently
switching models. The adapter does not read the web app's legacy
`ASC_FOUNDRY_EXTRACTION_DEPLOYMENT`.

**Configuration discrepancy:** the existing foundation template and ADR still
describe GPT-5.4 nano. Issue #48 explicitly specifies the working GPT-5.1 nano
deployment, so this adapter requires separate explicit pins for that deployment.
This change neither alters the foundation model deployment nor provisions a new
one. It performs no availability probe or alternative-model evaluation.

The host access template grants scoped source/profile/lease/monitoring access.
The owner must configure approved pins, grant only the required project-scoped
invocation permission and retain private networking. Do not copy web credentials,
broaden roles or deploy infrastructure to make a local test pass.

## Input and ownership boundary

Call `await PreferenceExtractor(config).extract(request)` with the private typed
contracts in `sous_chef.domain.extraction`. Construct the request from server-side
owned records, never browser-supplied recipe/profile/cooking claims:

- Current immutable feedback revision, owner, cooked timestamp, bounded recipe
  title and ingredient names.
- Complete current explicit preferences and at most 20 eligible learned context
  preferences from that same owner.
- Up to 99 other independent source occasions plus the current one, with a
  server-selected current time and opaque operation UUID.

The request validates deterministic owner/history/revision-bound feedback IDs,
source linkage, previous revision linkage, cooking chronology, owner agreement,
distinct histories/occasions and no future evidence. Current feedback must be
within 180 days. These checks do not replace authenticated, partition-scoped
storage reads: ASC-48 loads current owned/cooked sources and checks revisions
atomically before applying evidence. Supplying an old revision as current is not prevented
by its internally consistent immutable ID alone.

The existing change-feed filter remains the admission gate. Grocery/profile
changes, generated recipes, casual chat and processing-status records do not
become extraction input. The adapter does not accept a generic event dictionary
or expose an HTTP endpoint.

## Instructions, limits and outcomes

Policy-v1 instructions treat the JSON user message as untrusted data. The full
current note and specific meaning take precedence over the whole-meal rating:
liking lime sauce may survive an overall dislike; burning rice does not prove a
rice dislike. Existing learned text is context, not fresh evidence. Explicit
preferences and deterministic allergy checks remain authoritative.

The adapter selects at most five newest prior occasions within 180 days. It
counts instructions, data, schema and a framing reserve with the existing
`o200k_base` baseline. To fit the 6,000-input-token ceiling, it drops optional
learned context first, then oldest prior examples. It never truncates the current
note or explicit constraints; mandatory overflow fails before invocation.

Each call has a 30-second deadline and 1,000-output-token ceiling. SDK retries
are disabled. Provider history, response storage, automatic truncation and
tools are disabled. Usage must be present, internally consistent and within the
ceilings. Local token estimation is not a measurement of live provider usage.
The later durable worker must reserve and account for cumulative attempts/tokens
across the revision's retry policy; this adapter does not implement that ledger.

The provider receives a strict JSON schema. Local validation rejects malformed
or duplicate-key JSON, nonfinite/out-of-range confidence, unknown fields,
unsupported preference fields and inconsistent outcomes. At most ten candidates
are accepted. Completed responses from the configured model are required;
refusals, tools, partial responses and unaccounted usage are failures.

Every assertion must reference an actually supplied feedback ID, history,
revision and occasion, with an exact nonblank excerpt from that revision's note.
Dropped/expired examples cannot be cited. Each candidate must involve the
current feedback and count each occasion once. Repeated normalized proposals,
opposite likes/dislikes for the same value and conflicts with the current
explicit profile fail closed.

Application code builds stable normalized field/value keys and derives evidence
metadata from actual sources. Each supporting/opposing cooked occasion contributes
three units; the overall rating is not a separate vote. Confidence is
`(3 + supportUnits) / (6 + supportUnits + oppositionUnits)`. Two supporters score
0.75; two supporters and one opponent score 0.60. Model confidence remains a
separate nonauthoritative proposal.

One supporting occasion scores 2/3 but still cannot satisfy the existing
two-supporting-cooked-occasions context gate. These are provisional counts over
the supplied examples, not a durable aggregate. Exact quotation proves
provenance, not semantic correctness; local synthetic replies do not establish
live extraction quality.

A genuine `no_insight` returns an empty candidate tuple and usage/provenance.
Configuration, context overflow, authentication, throttling, timeout,
unavailability, rejection and invalid-result failures have distinct safe codes.
No failure is converted into successful no-insight. Cancellation propagates and
closes clients without starting a replacement request.

## Privacy, packaging and verification

Notes, excerpts, model output and owner IDs are private processing data, not
operational telemetry. The adapter emits none of them, suppresses SDK diagnostic
messages/exception text during its attempt and rejects content-capture opt-in.
Automatic OpenTelemetry export is disabled. The connected worker extends this
guard over storage I/O. Outside that private scope, it preserves the existing
logging factory; it does not weaken the web privacy policy.
Application receipts keep contributions private and outside the profile.

The worker lock is generated from `worker/requirements.in`, constrained by the
root lock. The deployable ZIP includes root Function files, `worker/extraction.py`
and shared `sous_chef` backend modules for storage and learning. It remains a
separate Functions host and does not start/import the web application.

```powershell
python -m piptools compile --no-header --no-annotate --no-emit-index-url --no-emit-trusted-host --strip-extras --output-file worker\requirements.txt worker\requirements.in
python -m pytest tests\unit\test_preference_extraction.py tests\unit\test_context.py tests\unit\test_feedback_worker.py
python -m unittest discover -s tests\infrastructure -p test_functions.py -v
```

Tests use the real pinned SDK with synthetic HTTP, including project-client and
managed-identity doubles. They cover provenance/scoring, explicit precedence,
request controls, context bounds, malformed replies, safe service errors,
cancellation and DEBUG-log privacy with the web policy installed. No live model
call, paid quality evaluation, deployment or live permission change is part of
these checks. The separate learning tests exercise durable application.
