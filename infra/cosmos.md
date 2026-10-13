# Dev Cosmos foundation

[ASC-07 / #6](https://github.com/AgamMakkar21/ai-sous-chef/issues/6) uses
`cosmos.bicep` and `modules/cosmos.bicep`, alongside (not replacing) the monitoring
deployment. This entry point supports **dev only**. No Foundry portal setup,
App Service, Function, trigger, agent identity, or application CRUD API is created.
Do not run the resource-group creation template for the existing `test-2` group.

## Approved design and deployment gate

The dev design uses one-region **serverless** Cosmos DB for NoSQL, session
consistency, continuous seven-day backup, and Entra authentication only.
There is no reserved RU/s, autoscale throughput, free-tier claim, or hard cost cap.
Request units, indexed storage, and backup features incur usage-based charges;
review [regional pricing](https://azure.microsoft.com/pricing/details/cosmos-db/)
before applying. A small smoke dataset does not benchmark production workloads.
Capacity and partition hotspots still need monitoring as usage grows.

The subscription/resource group, **Cosmos** region, costs, IP ranges, and identities
must be approved before deployment. Monitoring's East US 2 approval does not
approve Cosmos. The region is a required environment input with no fallback.
The deployment identity needs account/database/container/deployment write access,
including native `sqlRoleDefinitions/write` and `sqlRoleAssignments/write` for
approved grants. Azure Cosmos DB Operator alone does not grant those role writes.
`Microsoft.DocumentDB` must already be registered; ask the owner if it is not.
Authentication, policy, region/capacity, provider, and authorization errors block
deployment. Do not register providers or grant identities implicitly.

By default, the public endpoint is disabled when the approved-IP list is empty. Otherwise
only approved IPv4 addresses/CIDRs can reach it; no trusted-Azure bypass, portal
IP exception, or virtual-network exception is enabled. Never supply `0.0.0.0`
(Azure-wide access), `0.0.0.0/0`, or broad unreviewed ranges. Approve the backend's
actual outbound IPs and temporary operator egress separately. This dev policy
does not create private endpoints or allow browser data access.

For this demo, the owner explicitly approved **all-IP public networking**.
Set `ASC_COSMOS_ALLOW_ALL_PUBLIC_NETWORK=true` only for that approved dev
deployment: it enables the public endpoint with no IP rules. This overrides the
IP list, not authentication; keys remain disabled and callers still need approved
Entra data-plane roles. It increases network exposure and is not a production
recommendation. Restore the default restricted-IP policy when the demo ends.
An inherited Azure policy can override this requested setting. A successful ARM
deployment does not prove public access is enabled: always inspect the live
account and do not work around organizational network controls.

`productWriterPrincipalIds` is empty by default. Approved principals receive the
built-in Cosmos Data Contributor role separately on each product container, plus
an account-level **metadata-only** custom role for SDK initialization. They get
no lease **data** access. Runtime access should use the App Service's intended
managed identity; local smoke checks explicitly use the developer's Azure CLI
identity. Never include consumer/browser identities or interactive agents.
Worker identity/grants belong to the worker ticket.

Removing a principal from an incremental template **does not revoke an existing
assignment**. Inventory native Cosmos assignments and explicitly delete the
departed principal's assignments through an owner-approved access change. Remove
temporary operator IPs and grants after smoke checks. An empty IP list disables
the endpoint even if old grants remain. Backend authorization must still derive
`userId` from authenticated claims: container-level RBAC is not per-user isolation
and cannot enforce learned-state-only JSON-property writes.

## Immutable storage contract

| Container | Partition key | Stored record kinds |
| --- | --- | --- |
| `Users` | `/userId`, Hash v2 | One bounded `profile`; point read with `id=userId` |
| `ChatHistory` | `/userId`, Hash v2 | `conversation`, `turn`, `sendOperation` |
| `RecipeHistory` | `/userId`, Hash v2 | `recipe`, `behavior`, `feedbackRevision`, `processing` |
| `leases` | `/id`, Hash v2 | Future change-feed processor checkpoint/coordination documents only |

Database: `sous-chef`. The discriminator is `recordKind`, containing the unprefixed
kind from the existing schema outline (for example `turn`, not `ChatHistory.turn`).
Revision/provenance records use `sourceId` for the owned source record ID alongside
`feedbackRevision`/`behaviorRevision` and `policyVersion` where applicable.
All product documents carry `id`, server-derived `userId`, `schemaVersion`, and
`createdAt`. Existing bounded-record contracts remain authoritative. Never append
lifetime messages, recipes, or receipts to profile arrays.

Changing a partition key needs an explicit migration, not a template update.
Unbounded history means many bounded records, not infinite capacity in one
logical partition (20 GB). Monitor per-user growth/throttling; review a
hierarchical-partition migration before approaching capacity.
Transactions apply only within one container and user partition.

No container sets `defaultTtl`: chat metadata/turns/deduplication remain until
user-requested deletion, independently of profile and saved-recipe retention.
Do not add per-item `ttl` as a shortcut. Primary-data deletion is ASC-50; backups
use the separate seven-day recovery tier and require live recovery verification
before release. Leases have no automatic expiry, so checkpoint state is retained.

## Query indexes and RU evidence

Product containers use consistent indexing, explicitly included scalar paths,
and exclude `/*`, leaving chat bodies, profile arrays, recipe snapshots, and
private evidence unindexed. Leases retain default-style indexing for the future
processor. There are no vector, full-text, or analytical indexes.
Cosmos indexes the system `id` property automatically; do not explicitly include
`/id/?`, which the service rejects. `/id` is valid in the composite orderings.

| Operation | Index/query contract |
| --- | --- |
| Profile | Point read by `id` and `/userId`; no profile array query |
| Conversations | `recordKind=conversation`; composite `(recordKind ASC, createdAt DESC, id DESC)` |
| Turns | `recordKind=turn`, `conversationId`; composite `(recordKind ASC, conversationId ASC, turnIndex ASC, id ASC)` |
| Recipe list | `recordKind=recipe`; composite `(recordKind ASC, createdAt DESC, id DESC)` |
| Recipe filters | Scalar indexes on `accepted`, `cookedAt`, `rating`; cooked/unrated is awaiting feedback |
| Generated/occasion lookup | Scalar indexes on `generatedRecipeId`, `occasionId` |
| Feedback/processing | Scalar indexes on `sourceId`, `feedbackRevision`, `behaviorRevision`, `policyVersion`, `status` |

Equality-filtered discriminator/conversation fields also appear in `ORDER BY` to
match the composite index; their constant values do not change product ordering.
Queries must pass the authenticated `partition_key` explicitly and disable
cross-partition execution, with bound parameters and bounded page sizes.
Applications own authorization, signed pagination cursors, and ETag concurrency.

`scripts/smoke_cosmos.py` measures total `x-ms-request-charge` across every page of
each query, and reports RU and page counts as JSON without product data. It covers
two users, two conversations, stable ordering, filters, lookups, internal record
exclusion, profile isolation, and cleanup using exact synthetic IDs. Fixtures are
storage/query sentinels, not full application API payloads. Record actual results
with deployment evidence; **no live RU results are claimed by offline tests**.
Index growth adds write/storage costs; optimize from measured larger workloads,
not fabricated RU estimates.

No product path needs a cross-partition query. Operator inventory, migration,
or whole-container reconciliation may require a separately approved administrative
scan: it fans out across physical partitions and incurs aggregate RU/storage
cost, requires bounded paging, and must not expose user content in logs.
Normal user deletion can enumerate each product container within that user's
partition; it does not require a cross-user scan.

## Compile, preview, deploy, and smoke

Use the existing WSL Azure CLI/Bicep workflow. The following are **PowerShell**
commands for an environment where `az`, `bicep`, and `uv` are installed; in WSL
use Bash environment assignments and its native path syntax.
Set only owner-approved inputs; JSON arrays are not credentials:

```powershell
$env:ASC_AZURE_SUBSCRIPTION = '<approved-subscription-id>'
$env:ASC_AZURE_RESOURCE_GROUP = 'test-2'
$env:ASC_COSMOS_LOCATION = '<approved-cosmos-region>'
$env:ASC_COSMOS_APPROVED_IP_RANGES = '["<approved-egress-ip>"]'
$env:ASC_COSMOS_ALLOW_ALL_PUBLIC_NETWORK = 'false'
$env:ASC_COSMOS_WRITER_PRINCIPAL_IDS = '["<approved-entra-object-id>"]'

bicep build infra\cosmos.bicep --stdout > $null
$env:BICEP_CLI = (Get-Command bicep).Source
python -m unittest discover -s tests\infrastructure -v
az deployment group validate --subscription $env:ASC_AZURE_SUBSCRIPTION `
  --resource-group $env:ASC_AZURE_RESOURCE_GROUP `
  --parameters infra\environments\dev-cosmos.bicepparam
az deployment group what-if --subscription $env:ASC_AZURE_SUBSCRIPTION `
  --resource-group $env:ASC_AZURE_RESOURCE_GROUP --name ai-sous-chef-dev-cosmos `
  --parameters infra\environments\dev-cosmos.bicepparam
```

For the owner-approved all-IP demo, use
`$env:ASC_COSMOS_ALLOW_ALL_PUBLIC_NETWORK = 'true'` and an empty approved-IP array.

Inspect the preview: only the dev Cosmos account, database, four containers,
metadata role, and explicitly approved grants may change. Stop on unexpected
changes or errors. Only after approval of the preview:

```powershell
az deployment group create --subscription $env:ASC_AZURE_SUBSCRIPTION `
  --resource-group $env:ASC_AZURE_RESOURCE_GROUP --name ai-sous-chef-dev-cosmos `
  --mode Incremental --parameters infra\environments\dev-cosmos.bicepparam `
  --query '{state:properties.provisioningState,outputs:properties.outputs}'

uv run --script scripts\smoke_cosmos.py `
  --endpoint '<cosmosEndpoint-output>' --database sous-chef `
  --subscription $env:ASC_AZURE_SUBSCRIPTION `
  --allow-dev-writes
```

`uv` installs the script's pinned Cosmos/identity dependencies in an isolated
environment, not the backend runtime. The Cosmos SDK is also in the locked dev
dependencies for CI type checks. Azure CLI sign-in must already have access to
the approved subscription; that explicit subscription selects its tenant.
Do not supply a tenant override with it: Azure CLI rejects that combination.
Identity/RBAC/firewall propagation can take time; report errors and retry
deliberately, never fall back to keys or open networking.
For a hosted runner, select the intended user-assigned identity explicitly
instead of passing `--subscription`:

```powershell
uv run --script scripts\smoke_cosmos.py `
  --endpoint '<cosmosEndpoint-output>' --database sous-chef `
  --managed-identity-client-id '<app-identity-client-id>' --allow-dev-writes
```

The two authentication modes are mutually exclusive; hosted execution never
falls back to developer credentials. The runner must have the managed-identity
endpoint environment and approved network connectivity. An interactive App
Service SSH shell does not necessarily inherit the application's identity
environment. Never print identity headers or copy them into files/commands.

The smoke command never creates a database/container, touches leases, scans
partitions, or queries existing user data. It cleans up on success/failure;
cleanup failure exits nonzero with the synthetic run ID for operator remediation.

Inspect live account settings and all four container properties via Azure CLI
management reads, including lease `/id`, indexes, disabled local auth, firewall,
backup tier, and absence of TTL/throughput. Repeat the identical preview and
incremental application, checking the stable resource IDs and a retained synthetic
sentinel before/after; the smoke run's cleanup alone cannot prove deployment
preserves existing data. Delete that exact sentinel after verification.
Do not use Complete mode, deployment stacks with delete-on-detach, rename the
account/database/containers, or run resource deletion as a redeployment shortcut.
Changing the resource group changes account naming and is not a data migration.

Use the nonsecret outputs for `ASC_COSMOS_ENDPOINT` and `ASC_COSMOS_DATABASE`;
there are no account keys or connection-string outputs. Archive the reviewed
preview, deployment state, retained-sentinel check, and smoke RU report in review
evidence. A reviewed feature PR into `dev` is not automatic issue completion;
follow the team's reviewed `dev` -> `main` policy.

## Dev deployment status (2026-10-13)

The owner-approved deployment `ai-sous-chef-dev-cosmos` created account
`ai-sous-chef-dev-u46htzcnodzaw` in `test-2`, East US 2, with database `sous-chef`
and all four containers. The live account confirms serverless capacity, session
consistency, seven-day continuous backup, and disabled key authentication.
Management-plane reads verified all four immutable partition keys, the deployed
scalar/composite indexes, and absence of automatic TTL.
The operator's approved grants cover product data and SDK metadata, not lease data.

Initial live query/RU and retained-data checks were blocked:
Inherited management-group policy `CosmosDB_PublicNetwork_Modify`, assigned by
`MCAPSGovDeployPolicies`, forces `publicNetworkAccess=Disabled` despite the approved
demo opt-in. The public-endpoint smoke check receives an explicit firewall 403.
The owner subsequently configured private networking in the portal rather than
requesting a policy exception. No policy change or Foundry portal change was made.

Immediately after pulling dev commit `b2d86ca`, hosting alone did not unblock
access: VNet integration, a private endpoint, and runtime data grants were absent.

### Completed private-path verification

App Service `ai-sous-chef-dev-web-b5r36p` now integrates with the `default` subnet
of `ai-sous-chef-vnet` in Central US. Approved endpoint
`ai-sous-chef-dev-cosmos-pe` targets `Sql` in the separate `private-endpoints`
subnet. Its DNS zone group uses `privatelink.documents.azure.com`, with a completed
VNet link. The application container resolves the normal Cosmos hostname to
`10.0.1.4`; public network access remains disabled.

The owner-requested runtime access was applied **through Bicep**, preserving the
operator's existing grants and adding three product-container grants plus the
metadata-only grant for the app's existing user-assigned identity. Do not grant
the app Azure IAM Contributor as a substitute for Cosmos data-plane access.
The private networking is portal-created and not owned by this Cosmos template;
it was preserved by incremental deployment and needs separate networking IaC.

Live smoke execution from the application container used the explicit app
managed identity, not an operator token, keys, or developer-credential fallback.
All 16 query checks (eight query shapes for each of two synthetic users) passed,
including conversation isolation, record-kind filtering, ordering/tie-breaks,
profile point reads, and exact-ID cleanup. A separate lease point-read check was
denied with 403, confirming the app has no lease-data access.

| Query shape | Pages per user | Measured RU per user |
| --- | --- | --- |
| Conversation list | 3 | 8.56 |
| Conversation turns | 3 | 8.56 |
| Recipe list | 3 | 8.56 |
| Cooked, awaiting feedback | 1 | 3.02 |
| Liked recipes | 1 | 2.92 |
| Generated-recipe lookup | 1 | 3.02 |
| Feedback revision | 1 | 3.12 |
| Processing receipt | 1 | 3.22 |

These are measured totals with `max_item_count=1` and a tiny synthetic dataset,
not production RU estimates or latency benchmarks.

A repeat preview introduced no creates/deletes, and the identical incremental
deployment succeeded. All four container `_rid` values and the three retained
synthetic product records' `_rid`/`_etag` values survived unchanged. The retained
records were then deleted. Temporary diagnostic code/packages were isolated from
the deployed app and removed after verification. The application's CRUD adapters
and readiness implementation remain separate work; database access verification
does not claim those features are implemented.
