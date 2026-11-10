# Recipe lifecycle, history and feedback APIs (ASC-42 / #49, ASC-43 / #45, ASC-44 / #44)

Generated suggestions do not create cooking history or preference evidence.
These four authenticated mutation APIs record deliberate acceptance, cooking,
rejection and revision. Two read APIs return history lists and stored detail.
Feedback writes persist synchronously as described below; learning and UI remain
separate work.

## Requests and responses

Every mutation requires `Idempotency-Key`: 1-128 printable, non-space ASCII
characters. Keep the same key and body when reconciling an interrupted request.
All responses use `Cache-Control: no-store`. Existing authentication, same-origin
cookie/CSRF checks and the 128-KiB body limit apply. Unknown body/query fields,
duplicate headers, client ownership, snapshots and timestamps are rejected.

| Route | JSON body | Success |
| --- | --- | --- |
| `POST /api/v1/recipes` | `{"generatedRecipeId":"<uuid>","cooked":false}` | 201 recipe envelope, `ETag` and history `Location` |
| `POST /api/v1/recipes/{historyId}/cooked` | `{}`; also requires `If-Match` | 200 recipe envelope and `ETag` |
| `POST /api/v1/generated-recipes/{generatedRecipeId}/rejections` | `{}` or `{"reason":"Not what I wanted."}` | 201 explicit behavior envelope |
| `POST /api/v1/generated-recipes/{generatedRecipeId}/revisions` | `{"reason":"Use less rice."}` | Existing chat turn/operation response and operation `Location` |
| `PUT /api/v1/recipes/{historyId}/feedback` | `{"rating":"liked","note":"Use less salt."}`; also requires `If-Match` | 200 recipe envelope and `ETag`; learning remains pending |

`cooked` defaults to `false` and must be a JSON boolean. Save to Cook creates a
planned snapshot with `cookedAt=null`. Use `cooked=true` only when the user
explicitly confirms they cooked the recipe, not when they merely select it.
Cooking an already planned snapshot requires the conditional `/cooked` action;
resubmitting save with `cooked=true` cannot bypass its ETag check.

Reasons are trimmed and limited to 2,000 characters. A rejection reason is
optional; a revision reason is required and cannot be blank. Ingredient changes
must use revision generation, not a client-supplied edited snapshot.

The recipe envelope contains `historyId`, `generatedRecipeId`, `occasionId`,
`supersedesRecipeId`, source conversation/turn IDs, the full `recipe`, `accepted`,
`savedAt`, `cookedAt`, `state`, `awaitingFeedback`, rating/note, `feedbackRevision`
and `processingStatus`. Before feedback, rating/note are null, revision is zero
and processing is `notEligible`. `state` is `planned` or `cooked`;
`awaitingFeedback` means cooked and unrated, not saved.
History `Location` identifies the implemented detail API below.

## History reads (ASC-43)

| Route | Success |
| --- | --- |
| `GET /api/v1/recipes` | 200 `{ "items": [...], "nextCursor": null }` |
| `GET /api/v1/recipes/{historyId}` | 200 full recipe envelope and current `ETag` |

Both require authentication and return `Cache-Control: no-store`; neither
requires an idempotency key, completed onboarding, CSRF token or `If-Match`.
No read writes data, invokes agents, or checks an old snapshot against today's
profile. Detail returns the stored recipe, including its original nutrition,
dates, current rating/note and safe processing status. Reading history does not
reapprove that recipe for cooking under changed restrictions; `/cooked` still
checks current safety. It does not depend on the source conversation remaining.

Lists return only `historyId`, `title`, `savedAt`, `cookedAt`, `state`,
`awaitingFeedback` and `rating`. By default they include all saved snapshots,
including planned items. `awaitingFeedback=true` selects cooked, unrated items;
`rated=true` selects cooked items rated `liked`, `fine` or `disliked`. Planned
items appear in neither filtered view. False flags mean no filter; both flags
true is invalid. Unknown, duplicate or invalid parameters return safe 422
errors. Behavior, operation, feedback-revision and processing records are not
recipe list entries.

`limit` defaults to 20 and is bounded to 1-50. Pass the opaque `nextCursor` as
`cursor`, repeat the original true filter on every continuation, and either
omit `limit` or repeat its original value. Omit `cursor` to start or refresh;
an empty, modified, expired, wrong-user, wrong-view or wrong-limit cursor
returns safe 400 `invalid_cursor` before storage access. Cursors preserve
the original first-page cutoff and 24-hour expiry, with descending
`(createdAt, id)` ordering. New saves appear on refresh; cooking/feedback changes
remain live, so filtered membership is not a frozen cross-request snapshot.
Equal timestamps do not create duplicate page entries.

Missing and foreign IDs share the no-store 404 response. Owned internal record
IDs are not recipe detail resources. Malformed stored snapshots and unexpected
provider results fail explicitly without returning private content or ETags.
Every query/read passes the authenticated `/userId` partition and checks returned
ownership. Existing scalar and composite indexes support these queries; no
profile arrays, index changes or cross-partition lookup are introduced.

## Versioned feedback (ASC-44)

Feedback requires an owned, explicitly cooked history snapshot; planned recipes
return 409 `feedback_requires_cooking`. Ratings are exactly `liked`, `fine` or
`disliked` (UI labels: Liked, Fine, Didn't Like). Notes are optional: omitted or
`null` clears the previous note because this is a full PUT, not a partial edit.
A supplied note is trimmed, must be nonblank and has a 2,000-character limit.
Unknown properties, invalid ratings, non-string notes and oversized notes return
422 without storage access. Ownership is derived from the verified principal;
missing/foreign/internal record IDs return the same no-store 404.

The initial submission atomically commits three operations in the caller's
`RecipeHistory` partition: patch the recipe's rating/note, `feedbackRevision=1`
and `processingStatus=pending`; create an immutable `feedbackRevision` event;
create a private `recipeOperation` receipt with `action=feedback`, canonical
request hash and history result reference. A genuine rating **or note** edit
increments the semantic revision once and creates a new event. The immutable
recipe, cooking timestamp, profile and explicit preferences are not modified.

The event uses stored `schemaVersion=1` and `recordKind=feedbackRevision`, with:

| Field | Meaning |
| --- | --- |
| `id`, `feedbackId` | Same deterministic user/history/revision-bound UUIDv4 |
| `sourceId`, `historyId` | Owned recipe history snapshot reference |
| `occasionId`, `generatedRecipeId` | Original cooking occasion and recipe lineage |
| `sourceConversationId`, `sourceTurnId` | Source provenance; not authority to cross user partitions |
| `feedbackRevision`, `supersedesFeedbackId` | Positive semantic version and previous event ID, or null for revision 1 |
| `rating`, `note` | Exact normalized feedback at this revision; full note retained as private evidence |
| `provenance`, `observedAt` | `explicit_user_action` and server UTC observation time |
| `processingStatus`, `traceId` | Initial `pending` marker and opaque request correlation ID |

Events are outbox inputs, not learning results. A future worker must filter
`recordKind=feedbackRevision`, deduplicate by owned source/revision, verify the
current recipe revision before applying evidence, supersede old contributions,
and keep mutable processing receipts separate. Never count the current recipe,
receipt, rating and note as independent meals. Separate immutable events avoid
depending on the latest-version change feed retaining every intermediate edit.
No event expiry is introduced; retain events and retry receipts until history
deletion. Notes, request hashes and user IDs must not enter operational telemetry.

A matching `Idempotency-Key` replay returns the **current** persisted recipe
before checking the original ETag, even after restart or later edits; it never
reapplies the old payload. Reusing the key with a different normalized payload or
history reference returns 409. Identical current feedback under a new key creates
only a receipt, no revision/event, and preserves processing status, including
`failed` or `completed`. It still conditionally patches the same feedback fields
to atomically guard its receipt against concurrent edits; its ETag may change
without a new semantic revision. Missing `If-Match` returns 428, malformed/wildcard
revisions 422 and stale new-key writes 412. A conflict preserves the caller's
draft; reload before an explicit retry. Competing first submissions cannot
produce two events for the same revision.

Success means **feedback durably saved**, not personalization updated. There is
no Foundry, extraction, queue-send or learning call in this request path. Learning
outages do not block persistence; database write failures do return failure, and
the transaction cannot leave an event without its rating or vice versa. A lost
batch response is reconciled from its receipt, not blindly replayed. If a durable
outcome cannot be confirmed, return the existing safe dependency error and
reconcile with the same key/body.

### Azure setup for feedback persistence

No new Azure resource, app setting, model deployment, secret or role grant is
required **when the existing web/Cosmos foundation is already configured**.
Deploy the peer-reviewed web application change through the existing `dev`
delivery workflow. Verify the web managed identity has its existing Cosmos
metadata-read grant and container-scoped Data Contributor on
`sous-chef/RecipeHistory`, with `/userId` partitioning, existing scalar/composite
indexes and no product TTL. Keep the existing private-network access path;
do not enable keys or public/all-IP access as a workaround. See
[Cosmos setup](cosmos-access.md) if that foundation is not provisioned yet.

The [Functions host](feedback-host.md#feedback-admission-and-deferred-processing)
now filters immutable feedback revisions through an identity-based change-feed
trigger. Its processor remains deliberately disconnected until ASC-47/48 deliver
extraction and durable application. Deploying this API, provisioning the host,
or checking host health does not complete asynchronous-learning MVP acceptance.
The later worker's narrowly scoped product-write/Foundry grants and deployment
remain separate work. Do not manually mark pending feedback completed or broaden
worker permissions for this API.

Focused offline checks:
`python -m pytest tests/unit/test_feedback.py tests/unit/test_authorization_matrix.py tests/unit/test_recipe_lifecycle.py tests/unit/test_recipe_queries.py`.
These use the real pinned SDK with a synthetic transport, not live Cosmos/RBAC
acceptance. After deployment, manually confirm a cooked recipe's rating/note
round-trip, same-key retry, note edit and safe stale-ETag failure using an
authenticated session. Confirm learning remains honestly pending until a worker
actually processes that revision.

## Persistence, safety and replay

The backend resolves a completed generated recipe using an indexed,
partition-scoped lookup limited to two results. Missing/foreign IDs return the
same no-store 404. Duplicate generated references or malformed stored data fail
explicitly. It verifies source ownership and conversation linkage, then rereads
the persisted recipe and current profile through the deterministic safety gate.
An old generation approval never substitutes for acceptance-time validation.
Cooking a planned snapshot also rechecks current safety constraints.

Acceptance copies a self-contained snapshot into `RecipeHistory`, with stored
profile/policy validation provenance. It never trusts a browser recipe body.
Subsequent source changes do not rewrite that historical copy.
The source containers and profile are not part of the history transaction:
safety checks use the latest profile read at that boundary, not a cross-container
serializable lock.

One user-partition transaction creates the snapshot, its explicit `saved`
behavior and a private `recipeOperation` receipt. The snapshot ID is a
user/generated-recipe-bound UUIDv4; both same-key and different-key retries
resolve to one snapshot and one saved signal. Receipts store an operation-scoped
request digest and result reference, never a raw transport key.

Cooking atomically patches only `cookedAt` and creates its receipt. It sets server
UTC time once, preserves recipe content, and cannot move back to planned.
A known-key replay returns the current persisted result before checking a stale
ETag. A new-key stale update returns 412; missing `If-Match` returns 428.
Reusing a key with different input returns 409. A planned snapshot cannot be
promoted by another save request: use `/cooked` with its existing ID and ETag.

Definitive competing create collisions are reread within a bounded loop.
A lost batch response is reconciled through its receipt; an unresolved
uncertain write is not blindly replayed. Retain all receipts with their history
until the later deletion workflow removes them. No per-item TTL is introduced.

## Explicit signals and revisions

Behavior records retain `sourceId` (saved history or source turn), generated and
conversation/turn IDs, reason, occasion, server timestamp,
`behaviorRevision=1` and `provenance=explicit_user_action`. They are immutable
intent events, not inferred likes/dislikes or feedback ratings. Identical
kind/source/trimmed-reason intents deduplicate even with a new transport key;
different reasons are separate explicit events. No route updates learned state.

A revision records the user's request before attempting generation. That event
does not claim admission, completion, acceptance or cooking. Because chat and
history use separate containers, an interrupted request can leave the explicit
intent recorded before its send is admitted. Replay resumes the same send
identity rather than creating another intent or model invocation.

The revision uses the existing chat admission, current-profile context, managed
agents, bounded budgets, final safety gate and atomic turn completion. Its source
recipe is mandatory structured untrusted context, counted within the existing
11,000-token input ceiling; it cannot be trimmed away to make an oversized request
appear valid. The result is a new turn in the source conversation with the same
`occasionId` and `supersedesRecipeId` pointing to its parent. Older generated turns
without lineage use their original generated ID as the occasion.

Pending, failed and outcome-unknown responses follow the existing
[chat contract](conversations.md#durable-chat-sends-asc-22). Inspect the supplied
operation URL; repeating a revision is never permission to invoke again.
Known failed turns use the existing explicit failed-turn retry action.
Saving multiple revisions cannot manufacture independent cooking occasions for
the later learning worker.

## Setup and verification

Reuse [authentication](authentication.md), [Cosmos](cosmos-access.md) and
[recipe-agent setup](recipe-specialist.md). No new secret, service, dependency,
index or environment setting is required. Existing `generatedRecipeId` scalar
indexing supports source lookup. Only revisions invoke agents; save, cooking
and rejection remain available when agent execution is disabled.

Run the lifecycle/history tests and required two-user authorization matrix, plus the
existing chat/context/Cosmos suites, Ruff and strict mypy. Tests exercise the
production services and pinned Cosmos SDK over the offline transport, including
duplicate acceptance, cooking timestamps, stale ETags, altered ingredients,
current allergies, revision lineage and uncertain-outcome recovery.
Live deployed authentication, Cosmos concurrency and managed-agent revision
acceptance still require owner-approved dev verification; offline tests do not
establish those results.
