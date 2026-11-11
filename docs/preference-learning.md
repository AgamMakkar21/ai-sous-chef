# Durable preference application (ASC-48 / #42)

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
extraction retains its own 30-second limit.
The existing private SDK diagnostic guard covers storage as well as model I/O;
Cosmos HTTP logging/tracing are disabled. Notes, owner IDs and ledger contents
are not operational telemetry.

Run in the existing WSL Python 3.12 environment:

```powershell
python -m pytest tests\unit\test_learning.py tests\unit\test_context.py tests\unit\test_feedback.py tests\unit\test_feedback_worker.py tests\unit\test_preference_extraction.py tests\unit\test_cosmos_repository.py
python -m unittest discover -s tests\infrastructure -p test_functions.py -v
```

These are synthetic SDK/storage tests, not claims of live deployment or model
quality. Receipts prevent repeated extraction after application, not duplicate
model charges before a receipt commits (including concurrent extraction).
Durable attempt/token budgets, poison-event handling and operational retries
remain ASC-49; the current host continues to surface failures for delivery replay.
