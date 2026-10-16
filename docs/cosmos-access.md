# Partition-scoped Cosmos access

ASC-15 adds shared storage primitives, not grocery, profile, chat or recipe
endpoints. It uses the existing `sous-chef` database and `/userId` partitions in
`Users`, `ChatHistory` and `RecipeHistory`. It never creates infrastructure or
accesses the worker `leases` container. See the
[immutable storage contract and dev networking](../infra/cosmos.md).

## Configuration and identity

Install the locked runtime requirements and the backend package. Cosmos is a
runtime dependency pinned to `azure-cosmos==4.17.1`; no package versions changed
to implement this issue.

| Setting | Default | Bound |
| --- | --- | --- |
| `ASC_COSMOS_ENDPOINT` | None | Required HTTPS endpoint in live mode |
| `ASC_COSMOS_DATABASE` | None | Required existing database in live mode |
| `ASC_COSMOS_OPERATION_TIMEOUT_SECONDS` | 5 | Positive finite value, at most 5 seconds |
| `ASC_COSMOS_MAX_ATTEMPTS` | 3 | 1 through 5 total attempts, including the first |
| `ASC_COSMOS_PAGE_SIZE` | 20 | 1 through 50 |

Dev/prod use only the configured App Service managed identity through
`AZURE_CLIENT_ID`. Local live access uses only the developer's explicit
`az login`. There is no key, connection-string, credential-chain or local-data
fallback. `ASC_ADAPTER_MODE=local` does not construct a Cosmos client.
The existing settings loader still requires the other live-mode settings;
Cosmos access does not call Foundry.

The account must already grant account metadata read and access to the three
product containers. Private networking and DNS must work from the caller.
A configuration value is not proof of network access or successful live use.

## Caller contract

Use one short `open_cosmos` context for a request or bounded unit of work.
It captures one deadline before client setup. Initialization, operations,
retries, waits and response validation share that budget; a later operation
does not get another five seconds. Pass the request's existing absolute
`time.monotonic()` deadline if less time remains.

```python
from sous_chef.adapters.cosmos import open_cosmos
from sous_chef.domain.cosmos import ProductContainer, RecordPatch

async with open_cosmos(settings, deadline=request_deadline) as repository:
    profile = await repository.read(user, ProductContainer.USERS, user.user_id)
    if profile is None:
        # The business service decides whether to bootstrap or report absence.
        raise OwnedResourceNotFound
    updated = await repository.patch(
        user,
        ProductContainer.USERS,
        user.user_id,
        RecordPatch(etag=profile.etag, fields={"groceries": validated_groceries}),
    )
```

`user` must come from `CurrentUser` or another verified server-owned identity
context. It is not a body/header/query `userId`. Every operation derives its
partition from that context. Creates stamp ownership, ID, record kind, schema
version and UTC timestamps on the server. Profiles use the trusted user ID;
public history resources use UUIDv4. The internal conversation-creation receipt
uses a deterministic `conversation-create:<digest>` ID and is written only
atomically with its conversation, not through standalone `create`.
Callers may retain an explicit server-issued UUIDv4 before dispatch to reconcile
other uncertain creates.

The repository checks returned ownership before returning content, comparing
ETags or changing/deleting records. An absent point read returns `None`.
Ownerless/foreign records raise `OwnedResourceNotFound`; the shared API handler
renders the same safe 404 as a missing record. Missing containers, network
failures and denied permissions are failures, not empty data.

`NewRecord` and `RecordPatch` accept JSON fields, not complete business schemas.
Business services must still validate their feature-specific record schemas,
ownership relationships, explicit-over-learned precedence and deterministic
allergy checks. This layer does not grant a learning agent access to profiles.

## Conditional writes

There is no upsert, unconditional replace or arbitrary SQL interface.
Patches set only named top-level fields plus server-controlled `updatedAt`.
They reject metadata overrides and JSON-pointer paths. A learned-preference
patch cannot combine learned state with another profile section.

Updates and deletes require a nonempty ETag, read the owned record and send
`If-Match` to Cosmos. Another writer can still win between that read and write;
Cosmos then rejects the stale write. A stale ETag is never silently merged,
retried with a newer ETag or used to replay an absolute grocery quantity.
The caller must reload and decide whether a new edit is still valid.
At the future HTTP boundary, missing `If-Match` must map to 428 and stale
`If-Match` to 412; this issue does not add those product routes.

Every application record is limited to 256 KiB of serialized UTF-8 JSON,
using the pinned SDK's JSON escaping and spacing. SDK system metadata is
excluded. Patch validation checks the complete projected application record,
not just the patch body. Invalid input and oversized records fail before a write.
The nine-field patch limit leaves Cosmos's tenth operation for `updatedAt`.

## Histories

`HistoryQuery` supports conversations, conversation-scoped turns and saved
recipes, using the existing composite indexes and an ID tie-breaker.
It binds all values as parameters and always supplies the trusted partition.
It returns one page, never all history or all users. Cosmos may return fewer
items than requested. The pinned SDK follows empty service pages that have a
continuation token within the same deadline. Tokens come from the query's own
response metadata, not the client's shared last-response headers.

Construct one `HistoryQuery` for a traversal and reuse it with each
`PageRequest`. Its server timestamp excludes later appends. Preserve the query
and its timestamp across requests; updates to existing records remain live.
The default page is 20, the hard maximum is 50, and invalid limits are rejected.
Tokens are bounded internal SDK tokens, not browser cursors.
The [conversation APIs](conversations.md) now wrap these tokens in encrypted,
authenticated, user/query-bound public cursors. Their metadata and creation
receipt are committed atomically in the same user partition.

Future product endpoints must implement the already-agreed authenticated,
user/query-bound public cursor with a 24-hour expiry and the first-page upper
bound. Never accept or return a raw SDK token through a public API.

## Retry and failure contract

The repository owns ordinary throttling/transient retries. The pinned SDK's
`retry_total=0` alone leaves throttling defaults active, so the client also
configures its separate `ConnectionPolicy.RetryOptions` to zero.
Transport retries, write retries, endpoint discovery/failover and request
hedging are disabled. Internal SDK routing/metadata work remains subject to
the same outer deadline.

Reads retry 408/500/502/503/504 and transport failures within the attempt and
time bounds. Writes retry only a confirmed 429 rejection, never an ambiguous
transport failure, timeout or 5xx response. Stale revisions and permanent
errors are not retried.

Retries honor `x-ms-retry-after-ms`, then `Retry-After` seconds or an HTTP date.
Without guidance they use 100 ms exponential backoff capped at one second.
Malformed guidance fails explicitly. If the required wait exceeds the
remaining budget, the operation fails without an early retry.

| Failure code | API status | Meaning |
| --- | --- | --- |
| `revision_conflict` | 412 | Stale ETag; reload before deciding on another edit |
| `duplicate_resource` | 409 | The requested create already exists |
| `record_too_large` | 413 | Reduce the stored record size |
| `cosmos_throttled` | 429 | Attempts/time exhausted; safe retry guidance is retained |
| `cosmos_timeout` | 504 | The operation exceeded its deadline |
| `cosmos_authentication` / `cosmos_forbidden` | 503 | Backend identity/token or data-plane permission failure, not consumer sign-in failure |
| `cosmos_unavailable` | 503 | Network or transient service failure |
| `cosmos_invalid_result` | 502 | Malformed record, page or retry guidance |
| `cosmos_rejected` | 502 | Another permanent service rejection |

`CosmosError` contains only a safe code, opaque operation ID, retry guidance
and `outcome_unknown`. An uncertain write must be reconciled, not replayed.
The problem response says this explicitly and omits retry guidance for an
uncertain outcome. Caller cancellation propagates; a cancelled write may
also have reached Cosmos. Client and credential contexts close on success,
failure and cancellation. Logs use the existing allowlisted event policy;
they do not include records, user IDs, ETags, endpoints, tokens or SDK errors.

## Verification and approved dev acceptance

Offline checks use the actual pinned async SDK over a synthetic stateful HTTP
transport. They exercise partitions, conditional headers, overlapping writers,
pagination, exact retry counts, deadlines, cancellation and safe API failures:

```bash
python -m pytest tests/unit/test_cosmos_repository.py
```

Live acceptance is separate and must be explicitly approved after PR review.
From the configured dev workload on the approved private network, with its
managed identity and existing live settings:

```bash
ASC_APPROVE_COSMOS_SMOKE=1 python -m sous_chef.check_cosmos
```

This command rejects local/prod modes, non-dev account names, other databases
and missing approval before data access. It creates eight storage sentinels
under two unique synthetic identities, checks all three product containers,
pagination, colliding IDs, missing items and competing ETag writes, then
conditionally deletes the eight exact keys. It creates no resources or roles,
does not query real users and never accesses leases.

Cleanup runs in fresh bounded contexts, including after uncertain creates.
A failure never reports success. The command prints a synthetic `run_id`
before writing; retain it if cleanup fails or the process is interrupted.
An approved operator can rerun `cleanup_smoke(settings, UUID(run_id))` from
`sous_chef.check_cosmos`; it reconstructs only that run's eight exact keys.
No live acceptance result is implied by the offline suite.
