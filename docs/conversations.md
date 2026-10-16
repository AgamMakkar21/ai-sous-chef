# Conversation persistence APIs

ASC-21 / #18 adds empty conversation creation, metadata listing and bounded turn
retrieval. Cosmos `ChatHistory` remains the durable history authority. There is
no model invocation, send endpoint, profile write, transcript copy, automatic
title generation or deployment in this change.

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
metadata never enter these responses. Structured recipe views and persisted
send/retry state are later chat-generation deliverables, not arbitrary private
payload passthrough here. These endpoints do not create turns or invoke agents.

## Offline coverage and live boundary

`tests/unit/test_conversation_api.py` exercises the actual HTTP authentication,
API, service, pinned Cosmos SDK and synthetic HTTP transport together. It covers
independent conversations, complete/incomplete turns, deterministic timestamp
ties, bounded metadata-only listing, continuation bounds/expiry, two-user
isolation, atomic/concurrent/uncertain creation and privacy-safe failures.
The shared Cosmos tests continue to cover deadlines, throttling and cleanup.

```bash
python -m pytest tests/unit/test_conversation_api.py tests/unit/test_cosmos_repository.py
```

These checks do not prove Azure connectivity or live deployment. Approved
synthetic live acceptance remains separate; do not deploy, invoke agents or
write real user data to validate this issue locally.
