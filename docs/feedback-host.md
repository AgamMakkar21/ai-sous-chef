# Feedback Functions host (ASC-09)

[Issue #3](https://github.com/AgamMakkar21/ai-sous-chef/issues/3) adds a separate
Python Function App, not feedback processing. `worker/` registers only
`GET /api/health`: a fixed `{"status":"ok","scope":"host-only"}` response and
an `X-ASC-Commit` header from packaged release metadata. It accepts no business
inputs, returns no user data, and performs no Cosmos reads/writes. There is no
Cosmos trigger, extraction, Foundry invocation, feedback receipt or learned-state
mutation. Host health must not be used as evidence that learning works.

## Hosting and versions

`infra/functions.bicep` is an additive **dev-only**, resource-group deployment.
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
Extension bundle **4.41.0** is constrained to `[4.41.0, 4.41.1)` in `host.json`;
it includes Cosmos extension **4.16.1**, supporting managed-identity connections.
Upgrade the bundle and pins through review, not by widening to an unbounded range.

## Identity, storage and data permissions

The workload identity is named `ai-sous-chef-dev-feedback-identity`, different
from both `ai-sous-chef-dev-app-identity` and the GitHub delivery identity.
No Entra Agent identity is created or reused in this infrastructure-only slice.

| Principal | Scope | Grant and purpose |
| --- | --- | --- |
| Worker identity | Dedicated host storage account | Storage Blob Data Owner (`b7e6dc6d-f1e8-4753-8033-0f276bb0955b`): host locks, host-key blobs and containers |
| Worker identity | Existing Cosmos account | Custom `readMetadata` only, for SDK account discovery |
| Worker identity | `sous-chef/RecipeHistory` container | Cosmos DB Built-in Data Reader (`...0001`), including change-feed reads |
| Worker identity | Precreated `sous-chef/leases` container | Cosmos DB Built-in Data Contributor (`...0002`), to create/update/delete lease items |
| Worker identity | Existing Application Insights resource | Monitoring Metrics Publisher (`3913510d-42f4-4e42-8a64-420c390055eb`), Entra-authenticated telemetry ingestion |
| GitHub delivery identity | This Function App only | Website Contributor, added by `infra/dev-delivery.bicep` |

Storage Blob Data Owner is a **storage data-plane role**, not Azure resource
Owner. No queue/table roles or Storage Account Contributor are needed for this
HTTP-only host. Blob triggers, Durable Functions or other storage features would
require separately reviewed grants. The worker has no product-container write,
Users/ChatHistory, Foundry, subscription/group management or RBAC-writing grant.
Later extraction/receipt work must add its specific required access; this slice
does not pregrant it.

Host storage uses `AzureWebJobsStorage__accountName`, `__credential=managedidentity`
and `__clientId=<worker-client-id>` with standard public-Azure DNS.
`FeedbackCosmos` and `FeedbackLeases` each use `__accountEndpoint`,
`__credential=managedidentity` and `__clientId=<worker-client-id>`. Both refer to
the existing account, with database/container names in `ASC_COSMOS_DATABASE`,
`ASC_FEEDBACK_CONTAINER` and `ASC_LEASE_CONTAINER`. Never add a setting named
exactly `AzureWebJobsStorage`, `FeedbackCosmos` or `FeedbackLeases`: a connection
string can take precedence over the identity-property group.

The future Cosmos trigger must reference those prefixes, use the precreated
`leases` container (`/id` partition key), and set
`create_lease_container_if_not_exists=False`. Identity-based Cosmos bindings
cannot create the lease container. User isolation, explicit-over-learned
precedence and deterministic allergy checks remain responsibilities of future
application code, not guarantees supplied by container-scoped RBAC.

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

The source ZIP has `host.json` at its root and a generated `release.json`;
uncommitted files are excluded by `git archive`. Remote Oryx build requires
`SCM_DO_BUILD_DURING_DEPLOYMENT=true`, `ENABLE_ORYX_BUILD=true` and no
`WEBSITE_RUN_FROM_PACKAGE` mount. Publishing basic authentication stays disabled;
delivery uses the existing Entra/OIDC token. Missing worker configuration, stale
commits, shared identities, failed builds and failed startup checks fail the job.
A web rollout can succeed before worker delivery fails; there is no claimed
cross-app atomic deployment or automatic rollback.

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
Before deploying the future trigger, separately verify its Cosmos/lease access.

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
tracing, dependency collection and live metrics are disabled. Future trigger code
must preserve this boundary. Automatic alerts/on-call routing are not configured
here; operators inspect the health metric and deployment gate failures.

Offline checks run with the pinned Bicep compiler and worker requirements:
`python -m unittest discover -s tests/infrastructure -p test_functions.py -v`.
They verify compiled infrastructure, scoped grants, health behavior, packaging
and delivery failure paths without cloud access. Passing these checks is **not**
live dev host-start acceptance. Azure what-if, provisioning and observed startup
remain pending until an authorized owner runs them in the approved environment.

References: [identity-based host/binding connections](https://learn.microsoft.com/azure/azure-functions/manage-connections?pivots=functions-auth-identity&tabs=host),
[extension bundle 4.41.0](https://github.com/Azure/azure-functions-extension-bundles/releases/tag/4.41.0),
[Cosmos trigger identity guidance](https://learn.microsoft.com/azure/azure-functions/functions-bindings-cosmosdb-v2-trigger).
