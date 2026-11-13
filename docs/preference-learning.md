# Durable preference learning (ASC-48 / #42, ASC-49 / #46)

`LearningService` connects admitted feedback to the existing GPT-5.1 nano
extractor. All point reads and writes use the change-feed record's validated
owner partition. Before extraction, it reloads the immutable feedback and current
owned recipe, checks its revision, note, rating, lineage and real cooked timestamp,
and loads explicit preferences. Obsolete or deleted sources never add evidence.
Invalid records and dependency failures remain failures, not successful no-insight.

## Ledger and transaction boundary

The approved containers remain unchanged: `Users` contains one profile, while
typed `RecipeHistory.processing` records hold the learning ledger:

| Role | Identity and contents |
| --- | --- |
| `feedbackApplication` | Deterministic owner/feedback-version/policy-v1 UUID; current source/occasion/revision, outcome and at most ten typed contributions |
| `learningWindow` | One deterministic owner/policy-v1 UUID; generation and at most 100 current source pointers |
| `learningRecovery` | One owner/feedback-version attempt budget, conditional claim, retry time, generation and terminal outcome |
| `learningReplay` | Immutable owner/feedback-version/operator-request outbox; created atomically with a failed-cycle reset |
| `learningQuarantine` | Deduplicated invalid-envelope fingerprint, optional canonical source UUID and fixed failure category; no payload copy |

IDs use the existing owner-scoped SHA-256-to-UUIDv4 identity helper.
The receipt namespace is `learning-application:v1`, keyed by the deterministic
feedback UUID. The window namespace is `learning-window`, keyed by `v1`.
Feedback UUIDs already bind owner, history and semantic revision, so transport
metadata and repeated deliveries cannot create another contribution.

One three-operation transactional batch creates the immutable receipt,
creates/conditionally updates the window, and conditionally patches the source's
unchanged processing status. That last operation is an ETag guard: a feedback edit
during extraction rejects the entire batch. The window's ETag serializes different
feedback applications for the same user. A conflicting or uncertain write is
surfaced; delivery replay checks the durable receipt rather than assuming failure
means nothing committed.

**There is no cross-container transaction.** After ledger commit, the worker
rebuilds `Users.learnedPreferences` from current evidence. It reads the profile
before the ledger snapshot, checks the window ETag again, and patches only
`learnedPreferences` and its `learningGeneration` with the original profile ETag.
The generation is a monotonic projection watermark, not a processed-event list;
older ledger snapshots cannot replace a newer projection. Profiles without the
watermark start at zero. Concurrent explicit/grocery
edits or another projection cannot be silently overwritten. A replay repairs a
missing/stale projection without extracting or adding evidence again. Only then
is the recipe's processing status marked completed. That marker is recoverable
bookkeeping, never proof that learning was applied. Immutable feedback outbox
records are not modified.

## Deterministic reduction

The window retains the most recent 100 independent occasions in 180 days,
ordered by observation time descending, then history ID ascending. Edits replace
the pointer for that history; the newest feedback for an occasion supersedes
another history from that same occasion. An already-applied receipt cannot be
reinserted by replay after window eviction.

Prior feedback guides extraction, but only the current source's own verified
assertion becomes a durable contribution. Prior assertions/model counts are
never copied as additional votes. Each source is interpreted through its own
versioned delivery. A valid no-insight revision replaces the old interpretation
with no directional contribution.

Reduction validates sources again, excludes deleted/superseded/expired evidence,
and counts an occasion once per insight. Contradictory directions for the same
occasion contribute neither direction. Cooked support/opposition weighs three;
confidence is `(3 + supportUnits) / (6 + supportUnits + oppositionUnits)`.
Counts and confidence are rebuilt, never incremented. Metadata ties are stable,
and `lastUpdated` is the latest contributing evidence time, not delivery time.

Explicit conflicts are excluded without editing explicit facts. At most 50
candidates survive, evicting lowest confidence, oldest evidence, then ascending
insight key. Existing 256-KiB storage ceilings also apply. No event-ID array,
evidence list or lifetime receipt collection is embedded in the profile.

`LearningPreferenceReader` is the default recipe-context reader. It rechecks
owned current sources at request time, including deletion and edits that have not
yet been processed. The existing two-cooked-supporter / 0.65-confidence gate and
explicit precedence still apply. `LearningService.reconcile(user)` rebuilds the
projection after deletion/recovery without invoking extraction; deletion
orchestration itself remains ASC-50.

## Setup and verification

Apply the reviewed Function Cosmos access template to grant its distinct managed
identity Data Contributor separately on `RecipeHistory` and `Users`, alongside
existing lease and metadata permissions. It receives no `ChatHistory` access.
Cosmos RBAC cannot enforce field-level writes; the service enforces learned-only
profile mutations. Incremental deployment does not revoke the previous redundant
source-reader assignment; remove that obsolete grant only through an approved
access change.

Configure the existing project/deployment/model pins and required project-scoped
invocation permission from [the extraction guide](preference-extraction.md).
These settings and permissions must exist before processing succeeds; no fallback
or new model provisioning is performed. Networking and managed-identity rules
from [the host guide](feedback-host.md) remain in force.

The worker ZIP includes shared backend modules, but starts only the Functions
host, not the web application. Its locked dependencies include Cosmos,
Pydantic settings and safe telemetry support. Worker storage initialization
opens only `Users` and `RecipeHistory`. Its unit of work lasts at most 60 seconds;
each storage operation retains its existing at-most-five-second budget and
extraction retains its own 30-second limit. A learning attempt has a 45-second
deadline inside the storage scope, leaving time for recovery bookkeeping.
The existing private SDK diagnostic guard covers storage as well as model I/O;
Cosmos HTTP logging/tracing are disabled. Notes, owner IDs and ledger contents
are not operational telemetry.

Run in the existing WSL Python 3.12 environment:

```powershell
python -m pytest tests\unit\test_learning.py tests\unit\test_learning_recovery.py tests\unit\test_context.py tests\unit\test_feedback.py tests\unit\test_feedback_worker.py tests\unit\test_preference_extraction.py tests\unit\test_cosmos_repository.py
python -m unittest discover -s tests\infrastructure -p test_functions.py -v
```

These are synthetic SDK/storage tests, not claims of live deployment or model
quality. Receipts prevent repeated extraction after application, not repeated
model charges before a receipt commits. Recovery limits claimed attempts per
cycle, not a lifetime token quota. See the [recovery runbook](#recovery-runbook).

## Recovery runbook

Ratings and their immutable outbox revisions commit before asynchronous learning.
Learning failure never rolls them back. `learningRecovery.state`, not a recipe's
pending bookkeeping marker, is the authoritative operational failure status.

The worker claims an attempt **before** invoking learning. An ETag-protected,
two-minute lease excludes overlapping dispatch; the 60-second storage scope
ends before the lease expires. Crashes and uncertain claim writes consume their
reserved attempt conservatively. A fresh worker resumes the persisted count,
not the Functions runtime's in-memory retry counter.

| Worker setting | Default | Bounds |
| --- | --- | --- |
| `ASC_LEARNING_MAX_ATTEMPTS` | 3, including the first claim | 1-5 |
| `ASC_LEARNING_BACKOFF_SECONDS` | 60 | 1-300 seconds |

The budget is captured when the event's recovery record is created. Changing the
setting does not expand an existing event's budget. Retry delays double from the
configured base and cap at 300 seconds, starting after the failed attempt ends;
the next eligible time is persisted.
The host polls pending deliveries once per minute, so actual waits can be longer
than a configured sub-minute delay. There is no sleep or model call before due time.

Model timeouts, throttles and unavailable responses are transient. Cosmos
timeouts, throttles, unavailable responses, uncertain outcomes and ETag/duplicate
conflicts are transient. Invalid model output, rejected requests, bad extraction
configuration/authentication, invalid stored data and unexpected application
errors are permanent. A permanent failure, exhausted budget or exhausted crashed
claim becomes `failed`; it does not keep retrying extraction.

Malformed input is acknowledged only after an explicit `learningQuarantine`
record commits. Valid owner envelopes stay in that owner's partition. Invalid
owners use the reserved SHA-256 namespace of
`ai-sous-chef:learning-quarantine:v1`, without reading or writing a customer's profile.
The record stores an envelope fingerprint and optional canonical source UUID,
never a note, prompt, raw exception, invalid owner value or model response.
Quarantine writes are idempotent and all recovery/status roles are ignored by
admission. Only an immutable, validated `learningReplay` role is dispatchable.

The Functions policy intentionally retains delivery retries while work is
pending, a claim is live, or a terminal/quarantine write cannot be confirmed.
It is **not** an unbounded model-attempt policy. Returning after a durable terminal
outcome permits batch progress; healthy siblings are attempted even when another
item fails. An unavailable storage service can still hold a lease: advancing it
without a durable outcome would lose input.

The Cosmos trigger does not retry application exceptions by default. Its explicit
function-level policy reruns failed executions; existing leases control restart
positions. Do not rely on an assumed built-in dead-letter queue or a finite,
in-memory host retry counter as the durable budget. See
[Cosmos trigger failure/redelivery semantics](https://learn.microsoft.com/en-us/azure/cosmos-db/troubleshoot-changefeed-functions)
and [Functions retry policies](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-error-pages).

### Inspect and replay one failed event

Use the reviewed checkout with its Python 3.12 and worker requirements installed.
Run from the repository root, with `backend\src` on `PYTHONPATH`.
The command uses **only your explicit `az login` identity**, not a function key,
connection string, managed-identity impersonation or public recovery endpoint.
Your approved principal needs Cosmos data-plane read/write/transaction access
scoped to `sous-chef/RecipeHistory`, plus account metadata access. Inspection alone
can use the existing scoped reader role. Both paths also require `Users` metadata
and point-read access for the deletion tombstone check, never ChatHistory access.
RBAC and the existing network policy
authorize the command; missing access fails closed. No grants are created here.

```powershell
az login
$env:PYTHONPATH = "backend\src"
python -m worker.replay --inspect --cosmos-endpoint https://<approved-account>.documents.azure.com --owner <opaque-owner> --feedback-id <immutable-feedback-uuid> --revision <semantic-revision>
python -m worker.replay --replay --cosmos-endpoint https://<approved-account>.documents.azure.com --owner <opaque-owner> --feedback-id <immutable-feedback-uuid> --revision <semantic-revision> --request-id <new-uuid-v4>
```

Inspect the fixed category and attempt count, then fix configuration, access,
provider or code failures before replay. A replay resets only a **failed** cycle,
with its existing budget, and atomically creates the immutable replay outbox.
The worker performs extraction with its own configured managed identity.
Repeating the exact request UUID returns `already_requested`, including after a
lost transaction response; it does not reset the budget again. Use the same UUID
when retrying an uncertain command. Active work cannot be reset. Older replay
generations are ignored. Completed work returns `already_completed`.

The command prints status, counts, generation, due time and a fixed category, not
event bodies or owner identifiers. Its exit code is zero for a confirmed result,
one for rejected arguments/access/state or an unconfirmed storage outcome.
After `requested`, use `--inspect` until the new cycle completes or fails.
Existing `feedbackApplication` receipts repair a partial projection/status
without extracting again or duplicating evidence. Obsolete/deleted evidence
remains ineligible; replay does not restore deleted preferences or override
explicit settings.

For quarantines, use approved Cosmos tooling to inspect only bounded metadata:

```sql
SELECT TOP 50 c.id, c.userId, c.sourceId, c.sourceFingerprint, c.createdAt
FROM c
WHERE c.recordKind = 'processing' AND c.role = 'learningQuarantine'
  AND c.state = 'quarantined'
```

Keep these identifiers in authorized data tooling, not operational logs or
tickets. Malformed envelopes are not valid replay inputs: correct the producer
and save a new valid feedback revision through the authorized application flow.
Do not edit an immutable outbox, manually mark learning complete, delete leases
or change the lease prefix to recover one item. The
[ASC-50 operator procedure](data-retention.md) defines receipt/quarantine retention
and single-user deletion. Tombstoned owners cannot be replayed; workers acknowledge
their late feedback/replay/quarantine without recreating data.

`learning_recovery` warnings contain only state, fixed category and claimed count.
`feedback_batch_pending` contains only aggregate received/ignored/dispatched/
invalid/failed/quarantined counts. Investigate repeated pending batches as storage,
configuration, active-claim or due-time issues; a terminal model failure alone
does not keep a batch pending. Inspect storage records for event-level diagnosis.
