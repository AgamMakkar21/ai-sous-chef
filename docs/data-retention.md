# Retention and single-user deletion

ASC-50 deletes one validated user's primary product data and prevents upgraded
writers from recreating it. It does not erase external identities, provider
storage, telemetry, backups, or content already delivered to a client.
No live deletion or retention-policy approval is implied by the offline tests.

## Retention decisions

The [retention ADR](adr/0001-mvp-contracts.md#6-bounds-targets-and-retention)
remains authoritative. The default has **no automatic chat expiry**.

| Data | Retention and deletion |
| --- | --- |
| Conversation metadata, complete turns, creation/send receipts | Until user deletion by default; optional approved chat policy below |
| Profile and groceries | Current bounded state until user deletion |
| Saved/cooked recipes, feedback, behavior and provenance | Until user deletion, independently of source chat expiry |
| Learning windows, application receipts, retries, failures, replay outbox and owner-scoped quarantine | Until user deletion; no automatic receipt TTL that could allow duplicate learning |
| Learned candidates | At most 50; context rechecks the 180-day evidence window |
| Privacy tombstone and two history fences | Permanent, content-free but pseudonymous prevention metadata; never automatically expire or reopen |
| Ordinary operational telemetry | 30 days, without product content |
| Protected diagnostic content | Off by default; explicit approval and protected access, at most seven days |
| Product backups | Target maximum seven-day recovery horizon; verify deployed settings and restoration behavior |
| Shared worker leases | Processor coordination, not user content; do not delete/reset leases for a user request |

### Optional approved chat policy

`ASC_CHAT_RETENTION_DAYS` is unset by default. A reviewed value from 1 through
3650 enables creation-age retention for **ChatHistory only**. Never enable it
merely to complete deployment or acceptance.

The infrastructure enables per-item TTL with `ChatHistory.defaultTtl=-1`;
that value does not expire ordinary items. Users and RecipeHistory have no
container TTL. Deploy the retention index and wait for its transformation to
complete before enabling the setting. The backend rejects a configured policy
when the chat container lacks per-item TTL capability.

Roll out the compatible backend/worker code first, with the setting unset
against the existing no-TTL containers, and drain old writers. During an
approved maintenance window, apply the TTL capability/index update and wait
for index transformation to finish before resuming chat traffic. Enable a
retention value only after its separate approval. Reverting to pre-ASC-50 code
after enabling container TTL capability is not a safe rollback.

New and updated chat records receive server-owned `retentionExpiresAt` and
per-item `ttl`. Updates recompute remaining seconds from the original absolute
deadline, not a sliding inactivity window. Previously stamped deadlines are
not extended by disabling or lengthening configuration. The existing send
operation's `expiresAt` remains its execution deadline, not its retention.

Reads, histories, source resolution and model context exclude expired chat,
including legacy records older than the currently configured policy. Cosmos
physical TTL deletion is asynchronous. Untouched legacy records do not acquire
item TTL merely because configuration changes: inventory and approve a separate
backfill or purge before claiming physical retention compliance. Do not remove
the legacy read cutoff before that cleanup; unstamped legacy rows could become
visible again. Saved/cooked snapshots and profiles retain their independent
policy. Expired source chat cannot be newly saved or revised through source lookup.

## Authorized operator procedure

Complete primary-store deletion within seven calendar days of validation.
Do not paste owner keys, private content or provider IDs into GitHub issues,
ordinary logs or command transcripts shared outside the authorized team.

1. Validate the authenticated request and its scope through the approved support
   process. Derive the opaque 64-hex owner key from the verified issuer/subject
   using the application identity mapping; never trust a caller-supplied key.
   Record approval, operator, owner mapping, request UUIDv4 and deadlines in a
   protected deletion journal kept separately from product backups.
2. Verify the approved account, environment and private network. Use an explicit
   `az login` principal with existing, approved Cosmos metadata and data-plane
   read/write/delete permissions on all three product containers in `sous-chef`.
   The command does not create roles, use account keys or fall back to another
   credential. A denied identity fails; confirmation flags are not authorization.
3. Before the first purge, upgrade **every** web, worker, replay and administrative
   writer to the fencing-aware version and drain older processes. An older
   binary or direct data-plane writer can bypass these application safeguards.
   Keep the same prerequisite during rollback: do not run old writers against
   tombstoned users or remove the fences to restore service.
4. Revoke external sign-in/session access as approved. Securely collect only the
   identifiers needed for provider erasure before the primary purge removes
   their mapping; do not export raw chats or notes. Start the purge promptly so
   the tombstone blocks product access even with a still-valid access token.
5. Run the installed backend command on the approved network. Replace the
   placeholders, repeat the verified owner exactly, and retain the UUID privately:

   ```text
   python -m sous_chef.delete_user_data --delete --cosmos-endpoint https://<approved-account>.documents.azure.com --owner <verified-owner> --confirm-owner <verified-owner> --request-id <uuid-v4>
   ```

6. Require exit code zero and `status=primary_stores_completed`. Output contains
   only the original request ID, status and per-container counts removed
   **during this invocation**. Copy that result into the protected journal.
   Nonzero exit, interruption or an uncertain response means incomplete:
   correct access/network problems and rerun with the same owner/request.
   Counts from a resumed run are not a lifetime deletion total.
7. Cancel or drain the user's in-flight model work and invalidate application
   and client caches. An already authorized response can hold content in memory;
   the storage fence cannot recall content already sent to a client/provider.
   Complete the external steps below and record evidence separately.

The workflow tombstones `Users/id=userId`, permanently closes
`privacy:guard:v1` in each history partition, then conditionally deletes at
most 50 other documents per container per pass. The command opens a fresh
bounded context for each pass until empty. It includes unknown/legacy record
kinds and all owner-scoped histories, evidence, receipts and pending work.
It does not scan other users, touch shared leases, or delete account resources.

The three retained markers hold only owner partition, kind/version, state,
timestamps, request ID and completion metadata. They are **not anonymous** and
remain access-controlled. An authenticated deleted owner receives the same
safe 404 as a missing resource; profile creation cannot replace the tombstone.

History mutations include a server-evaluated active-fence predicate in the same
Cosmos transaction as the business changes. A paused write cannot commit once
its partition fence closes. Profile updates retain conditional ETags against
the tombstone replacement. Late feedback, extraction, replay and owner-scoped
quarantine acknowledge deletion without rebuilding state. This is resumable
cross-container coordination, **not a cross-container transaction**.

## External erasure and recovery

- **Identity:** revoke sessions and remove the external account through the
  approved Microsoft Entra External ID process; the primary command does neither.
- **Foundry/provider storage:** after in-flight work drains, use each provider's
  authorized deletion procedure for persisted responses, conversations, files
  or indexes actually used. Do not delete shared recipe/reference indexes.
  If reliable owner-to-provider IDs are unavailable, record the unresolved
  mapping and escalate through provider retention/support; do not claim erasure.
- **Telemetry and platform diagnostics:** inspect any explicitly enabled
  protected content capture, including server-side capture outside this code.
  Apply its authorized deletion/retention procedure. Ordinary content-free
  operational events are not evidence that separate platform capture was off.
- **Unattributable quarantine:** invalid-owner envelopes retain bounded
  fingerprints and optional source IDs, not user content, under the shared quarantine
  owner. Do not guess ownership or purge another partition; investigate proven
  associations through the protected incident procedure.
- **Backups and restore:** record the actual deployed recovery horizon and
  expiration deadline. Backups are not immediately erased by primary deletion.
  Before any restored environment accepts traffic or starts workers, reapply
  every validated deletion from the independent journal using fencing-aware
  code. Recheck that only the three markers remain and provider/cache cleanup
  still holds. Never reopen access to restored deleted profiles.

## Evidence and rollout limits

SDK-backed synthetic tests cover two-user isolation, bounded/resumed and
uncertain deletion, repeat safety, paused history/profile writers, in-flight
learning, replay/quarantine, all 19 user-data routes with still-valid tokens,
operator input/RBAC failures, audit-output privacy, and TTL-enabled recipe/context
behavior. They do not prove live cloud permissions, provider erasure, deployed
backup retention, restore readiness, or production performance.

Owner checks and transactional fence patches add storage requests and charge.
Measure deployed latency/RU and perform an approved synthetic deletion/restore
rehearsal before production release; never use real-user deletion as a smoke test.
