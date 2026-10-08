# MVP API and schema outline

Status: Proposed; read with [ADR 0001](../adr/0001-mvp-contracts.md).
This specifies the intended implementation contract, not an implemented OpenAPI document.
Chat storage uses Microsoft's
[complete-exchange turn model](https://learn.microsoft.com/en-us/azure/cosmos-db/gen-ai/agentic-memories#recommended-data-model-one-document-per-turn)
to keep each user input and its validated response together.

## 1. Shared conventions

| Concern | Contract |
| --- | --- |
| API prefix | Same-origin `/api/v1`; JSON field names are `camelCase` |
| Authentication | App Service / Entra External ID; backend verifies trusted identity, issuer, and authenticated subject |
| User identity | Stable opaque server-derived ID bound to verified issuer/subject; never email or client-supplied identity |
| Ownership | Every user-data lookup/mutation is scoped to that authenticated user and the `/userId` Cosmos partition |
| Resource IDs | Server-issued UUIDv4; opaque to clients and immutable across renames |
| Time | Server-generated UTC RFC 3339 strings with `Z`; use the same stored precision consistently |
| Schema version | Integer `schemaVersion: 1` on stored record kinds; schema migrations are explicit |
| Revisions | Opaque ETag for concurrency; integer `feedbackRevision` and `behaviorRevision` for semantic learning events; never conflate them |
| Input validation | Reject unknown mutation properties and nonfinite/out-of-range values; no client-controlled ownership, timestamps, roles, learned state, or processing state |
| Transport safety | HTTPS, same-origin requests, secure authentication cookies; CSRF token and Origin validation on browser mutations |
| Sensitive content | Never put content in URLs or ordinary telemetry; render user/model text as escaped text, not trusted HTML |

Use separate input, persistence, and public-response schemas. Public profiles
do not expose authentication claims, raw Cosmos metadata, private processing
receipts, or provider payloads. `_etag` is represented as an opaque HTTP `ETag`.

All state-changing operations require authentication. With incomplete onboarding,
allow profile bootstrap/onboarding but reject recipe generation with a typed
`onboarding_required` error. Reading another user's ID returns the same 404 shape
as an absent ID. Never make an unpartitioned lookup to discover its owner.

## 2. Responses, errors, and concurrency

Single-resource reads/writes return `{ "data": { ... } }`; collections return
`{ "items": [ ... ], "nextCursor": null }`. Creation normally returns 201,
reads/updates 200, and successful deletion 204 with no body. Completed synchronous
chat sends return 200 with their turn, as specified below. Health is the unauthenticated
exception: `GET /health` returns only `{ "status": "ok" }`, without dependency
details, identities, versions, or secrets.

Errors use `application/problem+json` with RFC 9457 fields and stable extensions:

```json
{
  "type": "urn:ai-sous-chef:problem:revision-conflict",
  "title": "This item changed",
  "status": 412,
  "detail": "Reload the latest version before trying again.",
  "code": "revision_conflict",
  "traceId": "opaque-correlation-id",
  "errors": []
}
```

`errors` may contain safe `{ "field": "quantity", "code": "must_be_positive" }`
entries; never echo rejected sensitive values or a raw Pydantic exception.

| Status | Meaning |
| --- | --- |
| 400 | Malformed JSON or invalid cursor |
| 401 | Missing/invalid authentication |
| 403 | Forbidden operation or incomplete required onboarding |
| 404 | Absent or inaccessible owned resource |
| 409 | Exact duplicate, conflicting lifecycle, send already active, or idempotency-key payload mismatch |
| 412 | Stale `If-Match` revision |
| 413 | Body or serialized-record size ceiling exceeded |
| 422 | Invalid field values, unsupported constraints, or context that cannot fit safely |
| 428 | Required `If-Match` header missing |
| 429 | Admission/rate limit reached; safe retry guidance |
| 502 / 503 / 504 | Invalid upstream response / unavailable dependency / exceeded deadline |
| 500 | Unexpected internal error with a trace ID, never a stack trace |

Mutable resources return `ETag`. Updates/deletes require `If-Match`; grocery
additions require the current collection ETag because they modify the bounded
`Users` document. Profile/grocery views share the underlying profile revision.
On conflict the UI preserves the draft, reloads current state, and asks the user
to retry. Do not silently apply a stale absolute quantity or overwrite unrelated
profile/worker edits. Backend patches modify only the intended fields.

Use `Idempotency-Key` for conversation creation, chat sends, recipe save,
mark-cooked, rejection/revision signals, and feedback submission. Scope the key
to user and operation; persist a canonical request hash and outcome/reference.
A replay of the same request returns its existing result, before rechecking an
outdated ETag. Reusing a key with different input returns 409.

Retain deduplication identity with its conversation/history until deletion, not
in an unbounded `Users` array or an expiring cache alone. Duplicate grocery
creation is guarded by duplicate detection and conditional writes; grocery
mutations are not blindly auto-retried after ambiguous completion.

## 3. Pagination and ordering

Use opaque, authenticated, user/query-bound cursors, never numeric offsets or
client-supplied raw Cosmos query text. Default page size 20, maximum 50; invalid
limits fail validation rather than silently widening the bound. Cursor payloads
must not expose sensitive content. A cursor expires after 24 hours; the client
then restarts listing, without implying product records have expired.

- Conversations: descending `(createdAt, id)`; show `updatedAt` but do not use a
  mutable key for pagination ordering.
- Turns: ascending `(turnIndex, id)` in the selected conversation; each item
  contains its user message and, only after completion, its assistant response.
- Recipe history: descending `(createdAt, id)` with optional lifecycle/rating
  filters. `awaitingFeedback=true` means cooked and unrated.
- Bounded grocery state: return the full bounded collection, not a lifetime query.

Capture a first-page upper bound for append-only lists and carry it in the
cursor. New conversations/recipes/turns appear on refresh, not mid-traversal.
Updates remain live: a newly rated item can leave the awaiting list, so that
filtered view is not a frozen cross-request snapshot. Stable keys prevent
duplicate/skip behavior from identical timestamps or title/feedback edits.
Completion updates an existing turn; the client upserts it by ID rather than
appending a second copy. Page size counts turns, not individual messages.

## 4. Minimum endpoints

All paths below are relative to `/api/v1`. The server derives ownership;
none accepts a `userId` route/body field.

| Method and path | Purpose and contract |
| --- | --- |
| `GET /me` | Bootstrap/retrieve bounded profile; idempotent server-side initialization for a new authenticated user |
| `PUT /me/onboarding` | Validate complete explicit settings and mark onboarding complete |
| `PATCH /me/preferences` | Update specified explicit fields only; no AI or learned writes |
| `GET /groceries` | Current bounded rows and collection ETag |
| `POST /groceries` | Add name, optional quantity/unit; duplicate check and collection ETag |
| `PATCH /groceries/{groceryId}` | Set an explicit positive quantity and/or selected unit/name, conditionally |
| `DELETE /groceries/{groceryId}` | Remove a confirmed row conditionally |
| `POST /conversations` | Create an empty owned conversation |
| `GET /conversations` | Paginated summaries |
| `GET /conversations/{conversationId}/turns` | Paginated ordered complete-exchange records, including explicit pending/failure state |
| `POST /conversations/{conversationId}/turns` | Accept user text under one idempotency key; create a pending turn and persist validated output in that same turn |
| `GET /conversations/{conversationId}/operations/{operationId}` | Inspect known pending/completed/failed/unknown send outcome without another model invocation |
| `POST /recipes` | Save an owned `generatedRecipeId` into an immutable snapshot; no client recipe body |
| `POST /recipes/{historyId}/cooked` | Confirm cooking; set cooked timestamp once |
| `POST /generated-recipes/{generatedRecipeId}/rejections` | Record an explicit rejection and optional reason, not abandonment |
| `POST /generated-recipes/{generatedRecipeId}/revisions` | Request a bounded, safety-checked revision in the source conversation; preserves lineage |
| `GET /recipes` | Paginated saved/cooked history; optional `awaitingFeedback=true` |
| `GET /recipes/{historyId}` | Snapshot, lifecycle, current feedback and safe processing status |
| `PUT /recipes/{historyId}/feedback` | Cooked-only `liked`/`fine`/`disliked` and optional note; persist the full note as primary specific-preference evidence and atomically create a genuine feedback revision |

Read-only `GET /me` initialization must be duplicate-safe and create no AI work.
Onboarding and preference updates never accept `onboardingCompleted`, learned
state, or timestamps directly; the server controls those fields.

Sending text normally returns 200 with the completed turn only after the validated
assistant response is persisted. If a duplicate key finds an existing pending send, return 202
with its operation URL and safe retry interval, without starting another generation.
Reject a different concurrent send for that conversation/user with 409.
Allocate a monotonic `turnIndex` under the conversation concurrency guard.
Persist the user-only pending turn, conversation counter/active-operation
reference, and send deduplication record in one transactional batch within the
same `ChatHistory` container and user partition, before starting generation.
On completion, conditionally update the turn with its assistant message/snapshot
and finalize the operation in the same partition. Never store the assistant
response as an unrelated message item or expose a half-committed successful pair.

The 60-second deadline includes dependencies, retries, validation, and persistence.
Retain explicit `pending`, `completed`, `failed`, or `outcomeUnknown` state.
If the provider may still have completed a response, reconcile its provider
response ID before offering a new attempt. If the ID or outcome cannot be
recovered, retain `outcomeUnknown` rather than automatically replaying.
Replaying an idempotency key never means "call again."
Known failures can be retried with a new key only through an explicit retry action;
bind that action to the existing owned failed turn, allocate a new operation,
and retain the same `turnIndex` and user message. Reset its state to pending
under the same conversation guard without incrementing the turn counter.
Operation history is separate
bounded records, not a growing attempt array inside the turn. A completed turn
cannot be replayed as a retry; a recipe revision creates a new turn in the same
recipe occasion/lineage.

## 5. Stored and public record outline

All stored records include `id`, `userId`, `schemaVersion`, and `createdAt`.
Mutable records include `updatedAt` and concurrency metadata.
The three product containers use `/userId`; internal leases are separate.
Histories are unbounded collections of bounded records, not unbounded records.
This proposal favors user-scoped access for the small MVP, not unlimited storage
in one logical partition. The ADR documents logical-partition capacity and a
reviewed hierarchical-partition migration path if growth requires one.
Transactions are limited to one container and logical partition; sharing
`userId` does not make `RecipeHistory` and `Users` updates atomic.

| Record kind | Required domain fields |
| --- | --- |
| `Users.profile` | `onboardingCompleted`, `groceries[]`, `explicitPreferences`, `learnedPreferences[]`, current revisions |
| Grocery row | `groceryId`, `name`, `ingredientId?`, `quantity: number or null`, `unit: string or null`, `updatedAt` |
| Explicit preferences | `allergies[]`, `dietaryRestrictions[]`, `likes[]`, `dislikes[]`, `preferredCuisines[]`, `spicePreference`, `cookingSkill`, `typicalTimeBudgetMinutes`, `nutritionGoals[]` |
| Learned candidate | `insightKey`, `insight`, `confidence`, `evidenceCount`, `supportingCookedCount`, `supportUnits`, `oppositionUnits`, `source: "recipe_feedback"`, `lastUpdated`, `latestEvidenceAt`, `policyVersion` |
| `ChatHistory.conversation` | `conversationId`, `title`, `nextTurnIndex`, last activity and active-operation reference; no turn array |
| `ChatHistory.turn` | `conversationId`, `turnIndex`, status, bounded `messages[]`, current operation reference, optional validated recipe snapshot and bounded tool/source references |
| Turn message | Server-issued `messageId`, server-controlled `role: user/assistant`, `entityId`, `content`, `timestamp`; no browser-controlled role/entity |
| `ChatHistory.sendOperation` | `conversationId`, `turnId`, idempotency identity/hash, attempt state, `providerResponseId?`, turn/result references, safe failure code |
| Generated recipe snapshot | `generatedRecipeId`, `occasionId`, `supersedesRecipeId?`, title, servings, minutes, skill, concise fit reasons, ingredients, instructions, shopping items, nutrition, sources, safety profile revision |
| `RecipeHistory.recipe` | `historyId`, `occasionId`, `generatedRecipeId`, immutable full snapshot, `accepted`, `savedAt`, `cookedAt: timestamp or null`, current rating/note, feedback revision, processing status |
| `RecipeHistory.behavior` | `occasionId`, owned generated/history reference, `kind: saved/rejected/revisionRequested`, bounded reason, behavior revision, provenance |
| `RecipeHistory.feedbackRevision` | History/occasion reference, feedback revision, current rating/note at that revision, superseded revision reference, processing provenance |
| `RecipeHistory.processing` | Owned source/revision, policy version, status/attempts, validated evidence references, result/receipt and safe failure classification |

Provider response and optional provider conversation IDs are internal references
to New Foundry Responses/Conversations, not classic Threads/Runs. Map them
server-side to the owned application conversation/operation; never accept them
from the browser as authority to retrieve or resume provider state. They do not
replace application UUIDs or make Foundry the durable history authority.

An extraction candidate links each support/opposition assertion to an owned
history/occasion ID, current feedback or behavior revision, signal type, and
normalized insight key/direction. For note-derived assertions, include an exact
supporting excerpt from that revision's note and an actionable preference
description. Validate that excerpts belong to the referenced note; the model
cannot invent quotes or source IDs. Excerpts alone do not establish meaning:
evaluate negation, mixed sentiment, cooking mishaps, and contradictory phrasing.
Model-supplied scores/counts remain proposals, never authoritative evidence.

Keep these bounded source assertions in `RecipeHistory.processing`, not a growing
array in `Users`. Stored notes/excerpts are private product data, not operational
telemetry. On a note edit, supersede the previous interpretation and recompute
the affected insight from current evidence even when the overall rating is unchanged.

Large evidence lists and processing receipts never accumulate inside `Users`.
Use record-kind filters consistently so internal records do not appear as
conversations, cooked recipes, or learning evidence.

A turn's `id` is its stable `turnId`. Its `messages` array contains exactly one
user message while pending/failed and exactly that user message followed by one
validated assistant message when completed. `outcomeUnknown` contains no claimed
successful assistant response. Persist a structured generated recipe once in
the completed turn; a saved history snapshot is its deliberate immutable copy.
Every turn stays within the 256-KiB record ceiling; an oversized response fails
explicitly before being displayed or saved as successful.

Tool/agent subcalls are not additional public chat messages. Store at most eight
necessary tool references containing tool name, outcome, and source IDs; do not
persist raw tool arguments/results, internal system prompts, or hidden reasoning
in the turn. Operational diagnostics follow the separate protected-trace policy.
This MVP has no per-turn embeddings or semantic-cache records.

Provide indexes for owned record-kind/conversation filters, `turnIndex`
ordering, stable list ordering, and generated-recipe lookup. Use partition-scoped
queries and point reads, not client-side filtering after a cross-user scan.
Partition-key scoping is not a substitute for authorization checks.
Monitor partition size, RU consumption, and hot-key throttling.

`email` is not required by the MVP and is not copied into the profile by default.
Identity lookup must not depend on a mutable email address.

Ingredient identity has three separate meanings:

- `groceryId`: immutable identity of the user's inventory row.
- `ingredientId`: optional deterministic identifier from a versioned curated
  ingredient/alias catalog, for example `ingredient:lime`; never minted by an
  LLM and never equated with the grocery row ID.
- USDA source identifier: nutrition-source identity with dataset/version
  provenance; not a replacement for either application identifier.

Unmatched free-text groceries are allowed without a canonical ingredient ID.
Their unknown mapping cannot become proof that a generated ingredient is safe
or that a nutrition quantity is known. The final-recipe safety gate must resolve
every ingredient and ingredient-bearing instruction before presenting it.

Supported units are `g`, `kg`, `ml`, `l`, `tsp`, `tbsp`, `cup`, `oz`, `lb`, `item`,
`piece`, `bunch`, `can`, `package`, or `null`. Selecting a unit does not authorize
automatic conversion. Quantity is finite, at most 1,000,000, with at most three
fractional digits; server arithmetic validates decimal precision.
The +/- step is 1 in the selected unit. For a smaller amount, decrement requests
removal confirmation instead of storing zero or a negative quantity.
Unknown quantity and/or unit must remain explicit, not fabricated for nutrition.

Recipe ingredients include a line ID, canonical ingredient ID, display name,
amount/unit and available/missing/unknown inventory status. Instructions reference
ingredient line IDs. Recipe sources include source ID, version, and bounded
references, not an unbounded retrieved document.

Nutrition includes serving basis, nutrient amount/unit, USDA food/source/version
references, mapped ingredient amounts, and `status: known/partial/unavailable`.
Known portions of a partial result are labeled partial, not a complete total.
Missing data is not zero, and no value is generated from model memory.
Display the informational-estimate/not-medical-advice notice.

## 6. State and revision invariants

| Transition | Effect |
| --- | --- |
| Generate suggestion | Owned structured output in chat; no cooking evidence |
| Save | One snapshot per owned generated recipe; `accepted=true`, `cookedAt=null`, no rating; one intentional weak signal |
| Mark cooked | Set server `cookedAt` exactly once; now eligible for awaiting-rating query |
| Submit first feedback | Require cooked state; persist rating/note, revision 1, and `pending` learning marker atomically |
| Submit identical feedback again | No new revision or evidence, even with a different transport key |
| Edit rating/note | Increment feedback revision; retain provenance; supersede old contribution |
| Process feedback/behavior | Nano extraction prioritizes specific note meaning, validates current revision/evidence, and produces a bounded replay-safe learned-state update |

Each cooked occasion contributes at most 3 directional units per insight, derived
primarily from its specific note and secondarily from its overall rating. A clear
note can support an insight despite `fine` or `disliked` overall. Do not count the
note and rating as two independent meals or two additive votes. Without specific
note evidence, a neutral rating contributes no direction. Ambiguous notes cannot
be resolved by inventing an ingredient preference from a whole-meal rating.
Notes never mutate explicit preferences, establish cooked status, or manufacture
an independent occasion. Two separately confirmed-cooked feedback occasions
are still required, even when both supporting signals come from their notes.

Processing status is `notEligible`, `pending`, `processing`, `completed`, or
`failed`; retries do not change the user's persisted rating. A legitimate
no-insight extraction completes with an explicit no-insight outcome.
An extraction failure is never disguised as that successful outcome.

`feedbackRevision=0` means no rating; revisions start at 1 and increase only
for semantic changes. `behaviorRevision` follows the same rule for intentional
signals. ETag changes for unrelated processing metadata do not create learning
evidence. `policyVersion` identifies the reviewed scoring rule independently
from schema/event revisions.

Generated revisions retain one server-owned `occasionId` for the same proposed
meal. Save retries or revision chains cannot satisfy the two-distinct-cooked-
occasions gate. Only independently recorded cooking occasions count separately.
The MVP does not add a bulk "cook again" history feature; this can be reviewed later.

## 7. Collection, input, and context limits

String lengths below count Unicode code points after trimming. Byte limits use
UTF-8 serialized JSON. Enforce both before persistence; do not silently truncate
user-entered values or security constraints.

| Input/state | Maximum |
| --- | --- |
| Any HTTP JSON body | 128 KiB |
| Any product Cosmos record, including `Users` | 256 KiB serialized; reject before approaching the Cosmos service limit |
| Groceries | 200 rows; name 100 characters |
| Allergies / dietary restrictions | 50 / 20 entries; 100 characters per entry |
| Likes / dislikes / preferred cuisines / nutrition goals | 50 / 50 / 20 / 20 entries; 100 characters per entry |
| Learned candidates | 50 entries; insight 240 characters; key 100 characters |
| User chat message / assistant narrative | 4,000 / 4,000 characters; structured recipe fields have their own bounds |
| Feedback note / rejection or revision reason | 2,000 characters |
| Note evidence per extraction candidate | At most one source assertion per occasion, within the 100-occasion window; quoted text cannot exceed the referenced note's 2,000-character limit or the processing record's byte ceiling |
| Conversation title / recipe title | 100 / 160 characters |
| Generated recipe | 30 ingredients, 30 instructions, 12 source references, 10 fit reasons |
| Instruction / fit reason | 500 / 240 characters |
| Recipe servings / total minutes | 1-20 integer servings / 1-1,440 integer minutes |
| Typical time budget | 5-240 integer minutes |
| Persisted turn messages | One user plus at most one validated assistant message; no conversation-length array |
| Context chat | At most 10 recent completed exchanges from the current conversation, plus the current request once |
| Context prior recipes | At most 5 relevant owned snapshots/evidence summaries |
| Context learned preferences | At most 20 currently eligible insights |
| Recipe retrieval | At most 3 snippets per lookup, 1,500 characters per snippet |
| Aggregate tool response inserted per model call | At most 4,000 tokens, also counted in total input |
| Extraction output | At most 10 candidate insights per execution |
| Learning evidence lookup | At most 100 recent independent occasions within 180 days |

`spicePreference` is `none/mild/medium/hot`; `cookingSkill` is
`beginner/intermediate/advanced`. Required onboarding safety arrays can be empty
only when the user explicitly confirms no known entries; absence is not consent.
Version the supported strict restriction rules and catalog coverage.
Unsupported safety inputs return a clear limitation, never silent acceptance.

Use the selected deployment's tokenizer/accounting rules, not character counts,
to enforce the ADR token ceilings. Reserve output capacity before each invocation.
Context order is: trusted instructions and complete hard constraints, current
request, explicit soft preferences/current groceries, recent conversation,
eligible learned preferences, relevant history, and bounded retrieved/tool data.

Query only completed owned turns for historical model context, newest first
within the limit, then restore chronological order. Keep pending/failed turns
visible in UI history but out of successful past-exchange context. Include the
current user's pending input once, not again through a history query.
Drop the oldest complete exchanges first, never an answer without its question,
then optional historical examples and the lowest-ranked learned insights;
bound retrieved content before use. Groceries may be selected
deterministically by request match then stable ID, with an explicit omitted-row
count. Never drop or summarize away allergies/strict restrictions.
If mandatory context cannot fit, return `context_limit_exceeded` and clarification,
not a request with weakened safety. Refresh profile/groceries before each send
and reread safety state before delivery.
Supply this selected context through App Service; provider-managed conversation
history must not silently append excluded turns, cross-user content, or older
safety state. Do not blindly reuse a provider conversation or chain
`previous_response_id` around the application's selection and retention limits.
Test that the implementation enforces these controls through the selected New
Foundry API; this is wiring validation, not a capability-discovery prerequisite.
Ordinary conversation text is not automatically consolidated into a durable
preference: only the approved recipe-behavior learning pipeline may do that.

Extraction context includes the full current validated note, overall rating,
relevant recipe facts, and complete hard constraints before optional prior
examples. Prior feedback remains bounded by the learning window and execution
limits; storing 100 eligible occasions does not require sending all their raw
notes to the model at once. Do not trim away the current note to fit optional
history. If mandatory extraction context cannot fit, retain the saved feedback
and record an explicit retryable or failed processing outcome, not a silent
rating-only or successful no-insight fallback.

## 8. Contract verification requirements

The implementation must demonstrate these invariants; this outline does not
claim that the tests already exist:

- SDK imports, dependency locks, and adapter behavior use the ADR's New Foundry
  baseline, versioned agents, and Responses/Conversations rather than classic APIs.
- Separate App Service/Function managed identities and distinct interactive-agent
  Entra principal IDs; actual permitted/denied tool access and caller attribution,
  reviewed shared-identity/secret exceptions, and no automatic credential fallback.
- Agent/version, provider response ID, application operation ID, and actual principal
  attribution correlate through traces; Function extraction links to its source
  feedback revision without exposing tokens or raw user content.
- All API roles/ownership fields rejected on mutation; foreign IDs return safe 404.
- Empty/unknown/zero/negative/maximal quantities; exact duplicates and different units.
- Stale grocery and concurrent profile/learning writes without lost updates.
- Equal timestamps across pages and coherent awaiting-rating membership changes.
- Pending-to-completed turn updates, bounded two-message storage, pair-preserving
  pagination/context truncation, and no duplicated current input.
- Save versus cooked versus feedback, immutable snapshots, and duplicate submissions.
- Same idempotency key through timeout/retry without duplicate model invocations.
- Failed-turn retry reuses its turn identity; operation completion and assistant
  persistence are atomic within `ChatHistory`; cross-container learning is not
  incorrectly treated as a transaction.
- Two-cooked-meal and 0.65 boundaries, one-third weights, repeated/contradictory/
  corrected/expired evidence, and exact-at-threshold comparison before rounding.
- Note-led evidence despite neutral/contradictory overall ratings, several
  distinct insights from one note without duplicate votes per insight, and no
  inference of food dislike from a cooking mishap.
- Exact note/source-revision provenance, note-only edits superseding old evidence,
  full current-note preservation, explicit extraction-context failures, and no
  note-driven modification of explicit safety/profile fields.
- Max-size context with all hard constraints preserved; explicit failure if it cannot fit.
- Provider-side history cannot reintroduce excluded turns; no cross-conversation
  or cross-user memory leakage; raw remembered content never becomes instructions.
- USDA/Foundry outages, non-AI CRUD independence, and no fabricated nutrition.
- User-requested deletion covers derived memory and provider content where used,
  dependent evidence invalidation, and recovery deletion replay.
- Complete primary journeys on mobile and laptop viewport/browser combinations,
  including touch/keyboard use, mobile on-screen keyboard behavior, and preserving
  unsaved recipe notes across responsive layout changes.
- Reproducible dependency installation, startup and offline checks inside WSL 2
  and Linux CI without relying on native Windows-only runtime behavior.

## 9. Machine-readable contract fixtures

[schemas.json](schemas.json) contains JSON Schema Draft 2020-12 definitions for
the specified input fields, explicit and learned state, versioned stored-record
projections, recipe lifecycle, and safe errors. Select a named `$defs` entry;
the root intentionally rejects payloads so that selecting no contract cannot
silently pass validation.

[api-fixtures.json](api-fixtures.json) encodes all 19 operations above, shared
headers/statuses, pagination and byte ceilings, positive/negative input examples,
and learning-threshold examples. The health path is outside `/api/v1`.
The revision operation uses the source conversation's turn/send semantics.
Fixtures are synthetic and contain no credentials or real user data.

These are focused contract fixtures, **not a generated OpenAPI specification,
complete persistence DTOs, or an implementation**. Stored projections leave
internal concurrency, processing, and provenance metadata open. The nested
recipe ingredient/instruction/nutrition/source objects retain the semantic
requirements in section 5 rather than inventing an alternative nested API.
Input schemas reject unrecognized fields and are separate from stored projections;
never expose a persistence record as a public response.

Follow-on typed schema/API implementation must name the nested ingredient,
instruction, shopping-item, nutrition, and source-reference wire fields; finalize
the failed-turn retry payload, pending-operation response fields, and remaining
stable error codes; and provide complete public-response and internal-record
DTOs. Those unresolved wire choices are not frozen by these projections.

Validate trimmed strings by Unicode code point, reject nonfinite numbers, and
use decimal arithmetic for the three-place quantity rule. Enforce JSON byte
ceilings separately. Schema acceptance does not establish ingredient safety,
user ownership, current evidence, cooked confirmation, immutable snapshots,
authorization, ETags, cursor authenticity, or transaction correctness.
`runtimeAssertions` lists the principal integration-test obligations.

Run the focused documentation/fixture checks from the repository root.
The [documentation-only dependency pins](../../scripts/requirements-docs.txt)
include the PDF renderer version used for the pixel comparison; they do not
select application dependencies. In a WSL 2/Linux Python environment:

```sh
python -m venv /tmp/ai-sous-chef-docs
. /tmp/ai-sous-chef-docs/bin/activate
python -m pip install -r scripts/requirements-docs.txt
python scripts/validate_contracts.py
```

For normal development, run this in WSL 2 or Linux. The check uses no Azure
credentials, application dependencies, model calls, or live capability probes.
It renders Markdown with tables, checks local links/anchors and fenced JSON,
validates fixtures and boundary examples, and verifies the unchanged PDF hash
and its PNG preview. It does not claim browser/application or deployment testing.