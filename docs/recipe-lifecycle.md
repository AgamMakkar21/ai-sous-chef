# Recipe lifecycle APIs (ASC-42 / #49)

Generated suggestions do not create cooking history or preference evidence.
These four authenticated mutation APIs record deliberate acceptance, cooking,
rejection and revision. History reads, feedback, learning and UI are separate work.

## Requests and responses

Every request requires `Idempotency-Key`: 1-128 printable, non-space ASCII
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
History `Location` identifies the resource reserved for ASC-43's detail API;
this issue does not implement that GET route.

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

Run the lifecycle tests and required two-user authorization matrix, plus the
existing chat/context/Cosmos suites, Ruff and strict mypy. Tests exercise the
production services and pinned Cosmos SDK over the offline transport, including
duplicate acceptance, cooking timestamps, stale ETags, altered ingredients,
current allergies, revision lineage and uncertain-outcome recovery.
Live deployed authentication, Cosmos concurrency and managed-agent revision
acceptance still require owner-approved dev verification; offline tests do not
establish those results.
