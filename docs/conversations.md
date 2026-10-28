# Conversation persistence APIs

ASC-21 / #18 adds empty conversation creation, metadata listing and bounded turn
retrieval. ASC-22 / #25 adds idempotent sends, durable operation state and explicit
failed-turn retries. Cosmos `ChatHistory` remains the durable history authority.
There is no production agent pipeline, transcript copy, automatic title generation
or deployment in these changes.

## Setup

Use the existing [consumer authentication](authentication.md) and
[live Cosmos configuration](cosmos-access.md). Local API calls require a signed
consumer token and explicit developer Azure CLI credentials; dev/prod use the
configured App Service managed identity. Local-double mode does not fabricate
history: conversation operations return 503 instead.

Set `ASC_PAGINATION_KEY` to an environment-specific Fernet key. Generate it once
inside the activated Python environment:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store the result outside source control in the ignored local environment file
or protected App Service configuration. All processes/instances in the same
environment must share the key; do not generate it at application startup.
Cryptography is already installed by the locked `PyJWT[crypto]` dependency.
Missing configuration returns a safe 503 `configuration_unavailable` before
database access. An invalid configured key fails startup without echoing it.

Replacing this key invalidates outstanding cursors, requiring clients to restart
listing. It does not affect conversation IDs, stored history or creation
deduplication. Do not log keys, cursors, idempotency keys, titles or messages.
Do not change Azure settings or deploy as part of local implementation.

## HTTP contract

All routes require the existing verified `CurrentUser` boundary. Cookie-authenticated
browser mutations also require the existing same-origin/CSRF protections.
Successful responses use `Cache-Control: no-store`.

| Route | Request | Response |
| --- | --- | --- |
| `POST /api/v1/conversations` | JSON object with optional `title`; required `Idempotency-Key` header | 201 `{ "data": { "conversationId", "title", "createdAt", "updatedAt" } }`, `ETag`, and `Location` pointing to its turns |
| `GET /api/v1/conversations` | Optional `limit` and `cursor` query parameters | 200 `{ "items": [summaries], "nextCursor": string or null }` |
| `GET /api/v1/conversations/{conversationId}/turns` | Server-issued UUIDv4; optional `limit` and `cursor` | 200 `{ "items": [turns], "nextCursor": string or null }` |

An omitted title becomes `New conversation`. Titles are trimmed and must contain
1-100 Unicode code points; non-string values and unknown mutation properties
are rejected. The JSON body limit is 128 KiB, including whitespace, enforced
while reading the request, even without a trustworthy `Content-Length`.
The creation key is 1-128 printable non-space ASCII characters; use a newly
generated UUID for each deliberate New Chat action. Keep that same key when
reconciling an interrupted creation.

No route accepts a `userId`, provider conversation ID, role, timestamp, turn
counter or transcript from a client. Unknown query fields and repeated query
parameters are rejected. Untrusted identity-hint headers cannot replace verified
authentication. Invalid UUIDs fail validation; missing and inaccessible valid
IDs return the same safe 404. No cross-user lookup discovers a resource's owner.

## Durable, atomic creation

The backend generates a random UUIDv4 for the new conversation. Metadata starts
with `nextTurnIndex=0`, `activeOperationId=null`, a bounded title and server UTC
creation/activity timestamps; it contains no messages or turn array.

One Cosmos transactional batch creates that metadata and an internal
`conversationCreation` receipt in the same `ChatHistory` user partition. The
receipt ID is `conversation-create:` plus a SHA-256 digest scoped to the
authenticated user and creation key. This internal, non-public ID is not a
conversation UUID. Its body stores a canonical request hash and the new
conversation reference, not a growing array or raw key.

A repeated equivalent request returns the same conversation, including after
an uncertain response or a competing first creation. Reusing the same key with
a different trimmed title returns 409 `idempotency_conflict`. A different user
can independently reuse that key. A competing batch cannot leave an orphan
conversation or receipt. Missing/corrupt referenced records fail explicitly;
they never trigger a new creation under an existing receipt.

Creation does not automatically replay writes after a transport error or timeout.
The response marks an uncertain outcome using the shared Cosmos failure contract.
The client reconciles by repeating the same request/key, which point-reads the
durable receipt before considering a write. Receipts have no TTL and must be
deleted together with their conversation by the later user-deletion workflow.
Listing excludes receipts, turns and send-operation records at query time.

## Pagination and public turns

The default limit is the configured `ASC_COSMOS_PAGE_SIZE` (20 by default);
the allowed range is 1-50. A cursor retains its original limit; clients may omit
`limit` thereafter, or repeat the same value, but cannot change it mid-traversal.

Fernet encrypts and authenticates the internal SDK continuation and binds it to
the verified user, list type, selected conversation, page limit and first-page
creation-time upper bound. Raw SDK tokens and internal user IDs are not exposed.
Cursors expire 24 hours after the first page; later pages do not extend that
expiry. Tampered, expired or incorrectly scoped cursors return a safe
400 `invalid_cursor` before database access. Cursor input/output is bounded at
24 KiB; oversized service continuations fail explicitly rather than returning
an unusable cursor. Invalid limits and oversized query fields fail validation.

Conversation summaries sort by descending `(createdAt, id)`, not mutable title
or activity time. Turns sort by ascending `(turnIndex, id)` within the selected
owned conversation. The first-page upper bound excludes later appends until
refresh; title/activity edits do not move records between pages. Every query
uses the server-derived `/userId` partition, a record-kind filter and a bounded
SDK page. Listing summaries never enumerates the transcript.

Each public turn has `turnId`, `conversationId`, `turnIndex`, `status`,
`messages`, `createdAt` and `updatedAt`. It contains one user message while
`pending`, `failed` or `outcomeUnknown`; only `completed` contains the user and
one validated assistant message in that order. Each message exposes only
`messageId`, `role`, `entityId`, `content` and `timestamp`; content is bounded
at 4,000 characters. Invalid stored exchange/state combinations return a safe
502 rather than a false empty/successful history.

Internal prompts, provider response IDs, operation records and raw Cosmos
metadata never enter these responses. Structured recipe views remain a later chat-generation deliverable, not arbitrary
private payload passthrough here. The read endpoints do not invoke agents.

## Durable chat sends (ASC-22)

| Route | Request | Response |
| --- | --- | --- |
| `POST /api/v1/conversations/{conversationId}/turns` | Required `Idempotency-Key`; `{ "content": "Suggest dinner" }` | 200 `{ "data": turn }` only after atomic completion; pending duplicate returns 202 `{ "data": operation }` |
| `GET /api/v1/conversations/{conversationId}/operations/{operationId}` | Owned UUIDv4 references; no query parameters | 200 `{ "data": operation }`; never invokes the executor |

Both require the existing authentication/CSRF boundary and return `no-store`.
Send responses include `Location` pointing to the operation, including known
execution failures. Pending responses include `Retry-After: 2`.
An operation view contains `operationId`, `conversationId`, `turnId`, `status`,
safe `failureCode` (or null), and the current public `turn`. Internal hashes,
provider response references and Cosmos metadata are excluded.

`content` is a strict, trimmed string of 1-4,000 Unicode code points; the shared
128-KiB transport limit still applies. Unknown properties/queries and duplicate
idempotency headers are rejected. Clients cannot supply ownership, roles,
message IDs, timestamps, state, turn counters or provider references. Missing
and inaccessible conversations return the same 404 before execution.
Completed onboarding is required before admitting new execution.

The key has the same 1-128 printable non-space ASCII constraints as conversation
creation. Its operation UUID is derived deterministically from the authenticated
user, conversation, send operation name and key; the raw key is not stored.
A canonical payload hash includes the trimmed content and retry reference.
Reusing a key with changed input returns 409 `idempotency_conflict`.
Deduplication records do not expire and are retained with the conversation.

### Admission, ordering and completion

Only one send may be active in an owned conversation, across all processes.
An ETag-conditional transaction creates a user-only `pending` turn, a
`sendOperation`, and increments `nextTurnIndex` while setting `activeOperationId`.
All three writes share the `ChatHistory` user partition. A losing transaction
leaves no orphan turn/operation and consumes no turn index. The same key returns
the existing operation; a different competing key returns 409 `send_conflict`.
Different conversations remain independent.

Only the process that successfully admits the batch invokes the executor.
Completion atomically appends the validated assistant message to that same
bounded turn, finalizes the operation and releases the conversation guard.
Each message has a server-issued UUID, server-controlled role/entity and UTC
timestamp. Optional `generatedRecipeId` and `providerResponseId` references are
stored internally from validated executor output, never accepted from clients.
A lost completion response is reconciled by point-reading the operation; a
completed key can be replayed after an application restart even without an
executor configured.
Transient finalization failures allow at most three completion attempts within
the original deadline. Each re-reads the durable operation and uses conditional
ETags, retaining the already validated reply rather than invoking the executor
again. This is reconciliation of a guarded write, not a generation retry.

The overall send deadline is 60 seconds, including database setup, admission,
execution, validation and final persistence. Execution stops at most 55 seconds
after send starts, reserving five seconds for completion. Database calls retain
their smaller configured operation deadlines. No successful response is
returned before the completion batch is persisted.

### Failure and explicit retry

A confirmed executor failure returns 502 `execution_failed`. An invalid public
reply returns 502 `invalid_assistant_response`. Both atomically mark the turn
and operation `failed`, retain the user message, and release the guard.
Repeating the original key returns that failure, **not another invocation**.

Retry a known failed turn with a new key and its original content:

```json
{
  "content": "Suggest dinner",
  "retryOfTurnId": "existing-owned-failed-turn-uuid"
}
```

Admission conditionally resets that same turn to `pending` and creates a new
operation without changing its `turnId`, `turnIndex`, user message or creation
time. Completed/pending/unknown turns and changed content cannot be retried.
Operation attempts are separate bounded documents, never a growing array.
Older failed operations retain their original status; their public `turn`
field reflects the latest state of that same turn, possibly completed by a retry.

A timeout returns 504 `execution_outcome_unknown`, retains a user-only turn and
holds the conversation guard. An interrupted worker or uncertain database write
can leave a `pending` operation. Polling its operation or replaying its key after
the persisted 60-second expiry conditionally marks it `outcomeUnknown`, without
invoking anything. Late completions cannot overwrite that terminal state.
Unknown outcomes cannot be explicitly retried or bypassed with a new send.
This intentionally fails closed: the real pipeline must reconcile a provider
response before allowing further execution; there is no recovery worker or
automatic model replay in this issue.

Database failures use the shared safe Cosmos problem envelope. If admission
may have committed, repeat the same payload/key to inspect its state. If
completion cannot be persisted, do not claim success or mark a potentially
completed execution as a known retryable failure.

### Executor setup

`create_app(settings, chat_executor=...)` injects the `ChatExecutor` interface.
It receives server-derived user/conversation/turn/operation IDs, user text,
validated explicit preferences and a monotonic execution deadline. It must
return a `ValidatedReply` containing only final public output after the real
pipeline's deterministic safety checks. Explicit-over-learned precedence,
distinct agent identities and provider reconciliation remain that pipeline's
responsibility (ASC-38 / #39); this boundary does not change them.

The deterministic fake exists **only in `tests/unit/test_chat_api.py`**.
There is no environment switch, default fake, live Foundry invocation, or
success-shaped fallback. The normal application has no executor yet and
returns 503 `configuration_unavailable` before admitting a new turn; reads,
operation inspection and existing-key replay remain available.

## Offline coverage and live boundary

`tests/unit/test_conversation_api.py` exercises the actual HTTP authentication,
API, service, pinned Cosmos SDK and synthetic HTTP transport together. It covers
independent conversations, complete/incomplete turns, deterministic timestamp
ties, bounded metadata-only listing, continuation bounds/expiry, two-user
isolation, atomic/concurrent/uncertain creation and privacy-safe failures. `tests/unit/test_chat_api.py` adds durable send/retry,
first-admission races, pending duplicates, atomic completion, interrupted
execution, actual deadlines, stale-result fencing and pre-invocation validation.
The shared Cosmos tests continue to cover deadlines, throttling and cleanup.

```bash
python -m pytest tests/unit/test_chat_api.py tests/unit/test_conversation_api.py tests/unit/test_cosmos_repository.py
```

These checks do not prove Azure connectivity or live deployment. Approved
synthetic live acceptance remains separate; do not deploy, invoke agents or
write real user data to validate this issue locally.
