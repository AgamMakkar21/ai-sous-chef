# Feedback Functions host and change-feed filtering (ASC-09, ASC-46)

[Issue #3](https://github.com/AgamMakkar21/ai-sous-chef/issues/3) adds a separate
Python Function App. [ASC-46 / #43](https://github.com/AgamMakkar21/ai-sous-chef/issues/43)
adds its identity-based `feedback_change_feed` trigger and deterministic admission
filter. `GET /api/health` still returns a fixed `{"status":"ok","scope":"host-only"}` and
an `X-ASC-Commit` header from packaged release metadata. It accepts no business
inputs, returns no user data, and performs no Cosmos reads/writes itself.
ASC-47 supplies the [extraction adapter](preference-extraction.md); ASC-48
connects owned context, durable application and recovery through the
[learning ledger](preference-learning.md). Host health is not evidence that the
trigger, configured model access or learning works.

## Hosting and versions

`infra/functions.bicep` is an additive resource-group deployment, defaulting to
**dev**. ASC-60 also permits explicit `environment=prod` using the separate
`infra/environments/prod-functions.bicepparam`; production needs owner-approved
resources, costs and identities and is not provisioned by Actions.
It references the existing Cosmos account and foundation monitoring resources;
it does not redeploy the web app, Foundry, database or containers. Deploy in
incremental mode. Resource names and role-assignment GUIDs are deterministic for
the group/environment. Review any parameter/region changes before applying.

The worker has its own Linux **B1 plan**, Always On, user-assigned managed identity
and Standard LRS StorageV2 host account. This costs an additional continuously
billed B1 instance plus storage/telemetry, even with no events; use the approved
region's Azure pricing calculator before provisioning. A dedicated plan avoids
the Azure Files connection-string/content-share requirement of classic
Consumption/Premium hosting. It also avoids coupling the worker to web capacity.
No Azure Files share, publishing password, storage key, SAS or connection-string
secret is required by this configuration. Do not copy the web app's provider
secrets into the worker.

Python is pinned to **3.12**, the Functions runtime to the supported **v4**
major (`FUNCTIONS_EXTENSION_VERSION=~4`), and the Python library and all its
dependencies are exactly pinned in `worker/requirements.txt`. Azure manages host
patch releases for Linux code apps; `~4` is not a claim of a frozen host patch.
Extension bundle **4.39.1** is constrained to `[4.39.1, 4.39.2)` in `host.json`;
it includes Cosmos extension **4.16.1**, supporting managed-identity connections.
Upgrade the bundle and pins through review, not by widening to an unbounded range.

The previous 4.41.0 pin existed as an in-progress GitHub release but was absent
from the Functions CDN index. The host could not resolve the bundle and failed
before Python indexing with `ValidateBundleRequirements` /
`System.ArgumentNullException: Value cannot be null. (Parameter 'input')`.
Increasing health timeouts or redeploying the same archive cannot fix this.
The deployment now checks the archive's exact pin against the public Functions
CDN index before uploading/restarting the worker, and fails explicitly if the
pin is unpublished or availability cannot be verified. Bundle 4.39.1 is published
and retains the same Cosmos extension 4.16.1 and managed-identity bindings.

## Identity, storage and data permissions

The workload identity is named `ai-sous-chef-dev-feedback-identity`, different
from both `ai-sous-chef-dev-app-identity` and the GitHub delivery identity.
No Entra Agent identity is created or reused in this infrastructure-only slice.

| Principal | Scope | Grant and purpose |
| --- | --- | --- |
| Worker identity | Dedicated host storage account | Storage Blob Data Owner (`b7e6dc6d-f1e8-4753-8033-0f276bb0955b`): host locks, host-key blobs and containers |
| Worker identity | Existing Cosmos account | Custom `readMetadata` only, for SDK account discovery |
| Worker identity | `sous-chef/RecipeHistory` container | Cosmos DB Built-in Data Contributor (`...0002`), for change-feed reads and atomic learning receipts/window/source guards |
| Worker identity | `sous-chef/Users` container | Cosmos DB Built-in Data Contributor (`...0002`), for owned context and conditional learned-only profile projection |
| Worker identity | Precreated `sous-chef/leases` container | Cosmos DB Built-in Data Contributor (`...0002`), to create/update/delete lease items |
| Worker identity | Existing Application Insights resource | Monitoring Metrics Publisher (`3913510d-42f4-4e42-8a64-420c390055eb`), Entra-authenticated telemetry ingestion |
| GitHub delivery identity | This Function App only | Website Contributor, added by `infra/dev-delivery.bicep` |

Storage Blob Data Owner is a **storage data-plane role**, not Azure resource
Owner. No queue/table roles or Storage Account Contributor are needed for this
HTTP-health/Cosmos-trigger host. Blob triggers, Durable Functions or other storage features would
require separately reviewed grants. The worker has no ChatHistory,
subscription/group management or RBAC-writing grant. Owner-configured
project-scoped Foundry invocation access and extraction pins are required
separately. Container RBAC cannot enforce learned-only JSON-property writes;
the application boundary and ETag checks do so.

Host storage uses `AzureWebJobsStorage__accountName`, `__credential=managedidentity`
and `__clientId=<worker-client-id>` with standard public-Azure DNS.
`FeedbackCosmos` and `FeedbackLeases` each use `__accountEndpoint`,
`__credential=managedidentity` and `__clientId=<worker-client-id>`. Both refer to
the existing account, with database/container names in `ASC_COSMOS_DATABASE`,
`ASC_FEEDBACK_CONTAINER` and `ASC_LEASE_CONTAINER`. Never add a setting named
exactly `AzureWebJobsStorage`, `FeedbackCosmos` or `FeedbackLeases`: a connection
string can take precedence over the identity-property group.

The Cosmos trigger references those prefixes, uses the precreated
`leases` container (`/id` partition key), and sets
`create_lease_container_if_not_exists=False`. Identity-based Cosmos bindings
cannot create the lease container. User isolation, explicit-over-learned
precedence and deterministic allergy checks remain responsibilities of future
application code, not guarantees supplied by container-scoped RBAC.

## Feedback admission and durable processing

The trigger watches `sous-chef/RecipeHistory`, with the separate lease connection,
prefix `feedback-v1`, a 50-item invocation hint and `start_from_beginning=True`.
The hint is not an absolute batch-size limit: Cosmos can preserve transactional
batches. Existing leases determine subsequent resume positions; do not delete
leases or change the prefix during an ordinary release.

Only immutable, schema-v1 `recordKind=feedbackRevision` outbox records from the
[feedback API](recipe-lifecycle.md#versioned-feedback-asc-44) enter the processor.
The filter checks a positive integer semantic revision, pending eligibility,
exact rating, bounded normalized optional note, explicit-action provenance,
aware timestamps, canonical UUIDv4 references and the opaque owner namespace.
It verifies the event's deterministic owner/history/revision-bound ID, matching
source/history IDs, and previous-event linkage. Unknown schema versions and
malformed feedback fail closed. Notes stay private data, never instructions.

Recipe snapshots, operation receipts, processing records, generated chat,
groceries, profiles and behavioral records cause zero processor calls, even if
they contain a rating or `pending` marker. Nonpending feedback and modified
outbox records (`updatedAt != createdAt`) are ignored. Status bookkeeping must
remain in recipe/processing records, not mutate an immutable feedback event.

Each admitted `FeedbackEvent` carries its owner, source/occasion lineage, rating,
note, semantic revision, superseded event, observation time and trace reference.
Its `event_key` is SHA-256 of
`feedback:v1\0<userId>\0<historyId>\0<feedbackRevision>`, where `\0` denotes
one NUL byte. ETags and delivery timestamps do not affect it. Different owners
and genuine edits have different keys. There is deliberately no in-memory
deduplication: duplicate deliveries can reach the same boundary with the same
key. ASC-48 checks a durable owner/version/policy receipt, reloads current
owned/cooked source state and replaces superseded evidence. The default
`process_feedback` now invokes the configured extractor and learning service;
missing configuration/access fails rather than manufacturing a no-insight result.
See [transaction boundaries and recovery](preference-learning.md).

Every document is isolated: an invalid event or processor exception does not
prevent later documents in the same batch from being attempted. After the batch,
any failure raises a fixed safe exception. The function-level fixed-delay retry
policy retries indefinitely at one-minute intervals instead of exhausting a
finite retry budget and acknowledging unprocessed input. Successful siblings
can therefore be redelivered; their event keys remain stable. This is not a
durable exactly-once guarantee.

A poison event or unavailable dependency can stall progress on its lease.
Inspect the aggregate `feedback_batch_pending` counts (`received`, `ignored`,
`dispatched`, `invalid`, `failed`) and host invocation failures. Application code
logs no document, owner, event key, note, recipe, raw exception or model content.
Dead-lettering, durable retries and operator recovery remain ASC-49 work. Do not
manually mark feedback completed, advance a blocked lease, or disable retries
to make a smoke test appear successful.

Admission still performs no model/storage calls itself. The worker ZIP now
includes shared backend storage/services plus pinned New Foundry dependencies;
it starts only the Functions host, never the web application. API-to-worker
regressions pass synthetic persisted outbox records through admission and durable
application to guard contract drift.

Host storage's public network endpoint is enabled but shared keys and anonymous
blob access are disabled; all access requires Entra authorization. Cosmos keeps
its existing network policy. **If public access is prohibited, leave it disabled;
IP rules do not provide access in that mode.** Use the
[existing-VNet private networking setup](private-cosmos-network.md) for both the
worker and web app. Never enable public access or all-IP access to work around
policy. This private-Cosmos path does not change host storage or app ingress.

## Owner provisioning and delivery

Use the already approved subscription, resource group, region and foundation
resource names. The provisioning operator needs resource deployment/write
permissions, permission to assign the listed scoped Azure roles, and Cosmos
SQL role-definition/assignment write permissions. GitHub does **not** receive
these provisioning permissions.

```powershell
az deployment group what-if --subscription <approved-subscription> --resource-group <dev-group> --template-file infra\functions.bicep --parameters environment=dev location=<approved-region> owner=<resource-owner> cosmosAccountName=<existing-cosmos> applicationInsightsName=<existing-insights> logAnalyticsName=<existing-workspace>
az deployment group create --subscription <approved-subscription> --resource-group <dev-group> --mode Incremental --name ai-sous-chef-dev-feedback --template-file infra\functions.bicep --parameters environment=dev location=<approved-region> owner=<resource-owner> cosmosAccountName=<existing-cosmos> applicationInsightsName=<existing-insights> logAnalyticsName=<existing-workspace>
```

Inspect the what-if first: only worker resources and its scoped grants should
change. Missing permissions must fail provisioning, not be worked around with
Owner grants or keys. Allow time for role propagation, then:

1. Compare `functionIdentityPrincipalId`/`functionIdentityClientId` outputs with
   the web identity: both must differ. Preserve the existing product RBAC grants.
2. Complete the [private Cosmos path](private-cosmos-network.md) when public
   access is disabled. Pass `integrationSubnetResourceId=<worker-subnet-id>`
   on subsequent `functions.bicep` deployments to retain the declared integration.
   For an explicitly approved public selected-IP environment only, arrange its
   existing firewall allowlist; do not change a private-only policy.
3. Preview/reapply the existing delivery bootstrap with **all its original
   parameters** plus `functionAppName=<functionAppName-output>`. This adds only a
   conditional Website Contributor grant on the worker; omitting it preserves
   the original web-only bootstrap but cannot authorize worker delivery.
4. Set nonsecret GitHub **dev environment** variable `ASC_FUNCTION_APP_NAME`.
   Keep the existing dev-only environment policy, OIDC federation and peer-review
   gate. No new GitHub secret is needed.
5. Merge a peer-reviewed feature PR into `dev`. The existing workflow deploys and
   checks the web app first, then builds/deploys `worker/` from the **same current
   reviewed commit**, and retries worker health for up to five minutes.

   After validating the dev target, separate identity and keyless application settings,
   delivery reconciles only the three reviewed host settings: Python 3.12, Always On
   and `/api/health`. It reads them back before uploading the worker ZIP and fails if
   the update fails or drift remains. Other settings, hosting plans and permissions
   are not changed. Host checks during smoke, production preflight and rollback remain
   read-only and reject drift rather than repairing it.

The source ZIP has `host.json` at its root and a generated `release.json`;
uncommitted files are excluded by `git archive`. Remote Oryx build requires
`SCM_DO_BUILD_DURING_DEPLOYMENT=true`, `ENABLE_ORYX_BUILD=true` and no
`WEBSITE_RUN_FROM_PACKAGE` mount. Publishing basic authentication stays disabled;
delivery uses the existing Entra/OIDC token. Missing identity/connection configuration, stale
commits, shared identities, failed builds and failed startup checks fail the job.
A web rollout can succeed before worker delivery fails; there is no claimed
cross-app atomic deployment or automatic rollback.
ASC-60 retains exact archives and adds an explicitly approved
[production rollback and dev rehearsal](production-release.md), not automatic
recovery or feedback-data migration.

## Health, failures and acceptance evidence

App Service probes `/api/health`; an invalid runtime/identity/connection setting
or absent release metadata makes it fail rather than report success. The probe
demonstrates Python function execution and expected configuration, **not** Cosmos
connectivity, actual RBAC propagation or feedback processing. Check host startup
and storage authorization logs as well. After approved provisioning/deployment:

```powershell
az webapp show --subscription <approved-subscription> --resource-group <dev-group> --name <worker-name> --query "{state:state,identity:identity,outbound:outboundIpAddresses}"
curl.exe --fail --include https://<worker-hostname>/api/health
```

Require HTTP 200, the exact host-only JSON, the reviewed `X-ASC-Commit`, a running
host, no storage authorization/startup errors, and telemetry arriving in the
existing workspace. Record the deployment/run and what-if links in the PR.
After a reviewed trigger deployment, separately verify its Cosmos/lease access.

Functions host/startup/results logs and metrics go to the existing Log Analytics
workspace via diagnostic settings. Application Insights uses Entra authentication
(`APPLICATIONINSIGHTS_AUTHENTICATION_STRING`) with the worker identity; local
Insights authentication remains disabled. Foundation Application Insights tables
retain their existing 30-day policy; `FunctionAppLogs` inherits the workspace's
30-day retention. Operators need the existing approved monitoring-reader access.

Use Azure's Function App **Diagnose and solve problems**, **Health check** metric,
deployment logs and these workspace queries for startup failures (including
failures that occur before the Python worker can export telemetry):

```kusto
FunctionAppLogs
| where TimeGenerated > ago(1h)
| where _ResourceId endswith "/<worker-name>"
| where Level in ("Error", "Warning")
| project TimeGenerated, Level, Category, Message
```

```kusto
AppRequests
| where TimeGenerated > ago(1h) and AppRoleName == "<worker-name>"
| summarize requests=count(), failures=countif(Success == false) by bin(TimeGenerated, 5m)
```

No request body, user ID, recipe, prompt, credential or connection setting is
logged by worker application code. HTTP extended information, query-string
tracing, dependency collection and live metrics are disabled. Trigger code
preserves this boundary. Automatic alerts/on-call routing are not configured
here; operators inspect the health metric and deployment gate failures.

Offline checks run with the pinned Bicep compiler and worker requirements:
`python -m unittest discover -s tests/infrastructure -p test_functions.py -v`.
They verify compiled infrastructure, scoped grants, health behavior, packaging
and delivery failure paths without cloud access. Passing these checks is **not**
live dev host-start or trigger acceptance.

### Trigger checks and approved dev smoke

Run the focused synthetic checks before delivery:

```bash
python -m pytest tests/unit/test_feedback_worker.py tests/unit/test_feedback.py
python -m unittest discover -s tests/infrastructure -p test_functions.py -v
```

The infrastructure checks compile the existing source-reader/lease-writer grants
and inspect real pinned-SDK function metadata, including identity prefixes,
precreated leases, replay position and retry policy. They do not authenticate to Azure.

After peer approval and the normal dev deployment, an authorized operator must:

1. Verify worker health and `X-ASC-Commit` against the reviewed release. Confirm
   `feedback_change_feed` is indexed, using Python 3.12, extension bundle 4.39.1,
   the worker's distinct identity, the existing connection prefixes and private
   Cosmos network path. Do not use function keys or dump application settings.
2. Observe lease creation/renewal in the precreated `sous-chef/leases` container
   under `feedback-v1`, with no 401/403 or lease-container-create failures.
3. Use a dedicated synthetic dev account to cook/save a recipe and submit a
   rating through the normal API. Confirm its immutable event is version 1 with
   the expected owner/source. Observe `feedback_batch_pending` with `failed > 0`
   and `invalid = 0`: the default processor is deliberately disconnected.
   The persisted rating must remain readable and learning must remain pending.
4. Repeat the same feedback/key, then edit its note using a new key and current
   ETag. Confirm one event for replay and a distinct version-2 event for the edit.
   Ordinary grocery/profile/chat writes must not create feedback revisions.
   Use local mocks, not private-note logging, to verify exact dispatch identity
   and zero processor calls for status-only/unrelated records.
5. Record the deployment/run, reviewed commit, indexing/lease result and observed
   aggregate counts in the PR. Leave live acceptance unverified until this is
   actually observed; health alone cannot satisfy it.

Do not run this smoke against production, create resources, broaden roles or
write test feedback without approval. Local fixture and compiled-template results
are not proof of deployed Cosmos identity/lease access.

References: [identity-based host/binding connections](https://learn.microsoft.com/azure/azure-functions/manage-connections?pivots=functions-auth-identity&tabs=host),
[extension bundle 4.39.1](https://github.com/Azure/azure-functions-extension-bundles/releases/tag/4.39.1),
[Cosmos trigger identity guidance](https://learn.microsoft.com/azure/azure-functions/functions-bindings-cosmosdb-v2-trigger).
