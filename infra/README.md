# Azure foundation

Issue [ASC-06 / #9](https://github.com/AgamMakkar21/ai-sous-chef/issues/9).
These Bicep files define infrastructure; editing or compiling them does not
deploy anything. Only an explicit Azure deployment command provisions resources.

## Scope and environments

`main.bicep` targets an existing resource group and deploys:

- One environment-specific, pay-as-you-go Log Analytics workspace.
- One workspace-linked Application Insights component.
- Retention settings on Application Insights tables, not additional services.

No Cosmos DB, Functions, PostgreSQL, storage account,
agent identities, application instrumentation, dashboards, or alerts are deployed.
Foundry is added by `modules/foundry.bicep` and hosting by `modules/app-service.bicep`
(see the Foundry and App Service sections below).
Hosting includes consumer Entra External ID authentication. Before compiling either
environment's parameter file or deploying, complete the tenant-admin steps and set
`ASC_AUTH_ISSUER`, `ASC_AUTH_JWKS_URI`, `ASC_AUTH_CLIENT_ID`, `ASC_AUTH_ORIGIN` and
`ASC_AUTH_SECRET_URI` as described in [consumer sign-in setup](../docs/authentication.md).
The secret URI is a Key Vault reference, never the client-secret value. Main-template
redeployments retain these settings and authentication enforcement.
Cosmos has a separate, opt-in [dev deployment](cosmos.md) for ASC-07; running
`main.bicep` does not provision Cosmos.
`modules/monitoring.bicep` is the foundation module. Later service issues add their
own modules and consume the nonsecret resource-ID outputs.

| Environment | Configuration | Azure resources |
| --- | --- | --- |
| Local | Existing `.env.local.example` and offline adapters | None; do not run a cloud deployment for local |
| Dev | `environments/dev.bicepparam` | Approved existing `test-2` resource group, East US 2, owner `AgamMakkar21` |
| Prod | `environments/prod.bicepparam` | Not approved or deployed; requires explicit region, owner, subscription, and target group |

Names include `ai-sous-chef-dev` or `ai-sous-chef-prod`. Reusing a parameter file
updates the same named resources; changing the environment addresses different
resources even within a shared group. Incremental deployments do not delete
unrelated resources. Changing region for existing resources can fail and needs a
reviewed migration; do not treat it as a move operation.

The dev deployment does not create, rename, retag, or relocate `test-2`.
`resource-group.bicep` is a separate subscription-scoped template for creating a
NEW, environment-named resource group when an owner approves one. Do not run it
for the existing dev target. Its deployment requires subscription-scoped access.

## Prerequisites and ownership

Use Azure CLI and Bicep CLI (validated baseline: Azure CLI 2.91.0, Bicep 0.48.1).
Normal development commands run in Ubuntu on WSL 2. Authenticate with your own
Entra account using `az login`; deployment automation must use workload identity
federation, never a source-controlled credential.

The owner must approve the subscription, target group, region, and costs before
provisioning. Pass the subscription and group explicitly on every command; do
not rely on the CLI's default subscription. The dev owner approved East US 2 for
monitoring only. This is not approval of a region for later AI/model services.

The deployment identity needs resource and deployment read/write permissions
(for example Contributor on the existing target group). The subscription must
have `Microsoft.OperationalInsights` and `Microsoft.Insights` registered. If
registration is required, ask the subscription owner to do it; do not silently
register providers or grant roles. Authentication, authorization, policy, or
provider errors block provisioning; compilation is not deployment success.

For a resource group in another directory, sign in to that subscription's tenant
with the identity granted access, and accept any guest invitation first. Owner
RBAC does not override Conditional Access. If the tenant requires a managed
device, use an organization-approved compliant device or deployment pipeline;
ask the tenant administrator for enrollment/access guidance rather than trying
to bypass the policy.

`AgamMakkar21` owns the dev monitoring resources and cost review. The subscription
owner controls access assignments; this template creates no RBAC grants.
Existing inherited Azure roles still apply.

## Validate, preview, and deploy

From the repository root in WSL, set the approved target:

```bash
export ASC_AZURE_SUBSCRIPTION='<approved-subscription-id>'
export ASC_AZURE_RESOURCE_GROUP='test-2'

bicep build infra/main.bicep --stdout >/dev/null
bicep build infra/resource-group.bicep --stdout >/dev/null
python -m unittest discover -s tests/infrastructure -v

az group show --subscription "$ASC_AZURE_SUBSCRIPTION" \
  --name "$ASC_AZURE_RESOURCE_GROUP" --query '{name:name,location:location}'

az deployment group validate --subscription "$ASC_AZURE_SUBSCRIPTION" \
  --resource-group "$ASC_AZURE_RESOURCE_GROUP" \
  --template-file infra/main.bicep --parameters infra/environments/dev.bicepparam

az deployment group what-if --subscription "$ASC_AZURE_SUBSCRIPTION" \
  --resource-group "$ASC_AZURE_RESOURCE_GROUP" \
  --name ai-sous-chef-dev-foundation \
  --template-file infra/main.bicep --parameters infra/environments/dev.bicepparam
```

Inspect the preview before continuing: only the two named monitoring resources
and their table-retention configuration may change. Stop on unexpected changes,
unresolved preview errors, or modifications to existing unrelated resources.
Provider-generated properties can produce preview noise; investigate it rather
than automatically approving every modification.

Only after owner approval and an acceptable preview:

```bash
az deployment group create --subscription "$ASC_AZURE_SUBSCRIPTION" \
  --resource-group "$ASC_AZURE_RESOURCE_GROUP" \
  --name ai-sous-chef-dev-foundation --mode Incremental \
  --template-file infra/main.bicep --parameters infra/environments/dev.bicepparam \
  --query '{state:properties.provisioningState,outputs:properties.outputs}'
```

Verify the deployed resource properties and repeat the preview/application to
check idempotency. A successful deployment alone does not prove that an
application is sending telemetry. The backend now emits privacy-safe operational
logs to its process logging handlers; this foundation does not configure ingestion
of those logs into Application Insights. See the
[MVP logging and launch policy](../README.md#privacy-safe-mvp-logging).
Do not output connection strings, instrumentation keys, tokens, or workspace
shared keys. Outputs intentionally contain only resource IDs.

For production, set `ASC_AZURE_LOCATION` and `ASC_RESOURCE_OWNER`, choose an
independently approved subscription/group, and use `prod.bicepparam`. Production
is not provisioned by the dev commands.

On Windows, use the same Azure CLI arguments with backslash paths and PowerShell
environment variables. Set `BICEP_CLI` to the full compiler path if it is not on
PATH. For the tests, the Python standard library is sufficient; no Azure account
or backend dependencies are required. Infrastructure checks run separately from
the default backend pytest suite because they require a Bicep compiler.

## Foundry project and model deployments

Issue [ASC-10 / #10](https://github.com/AgamMakkar21/ai-sous-chef/issues/10).
`modules/foundry.bicep` adds, in the same resource group:

- One New Foundry resource (`AIServices`, key auth disabled, system-assigned identity).
- One Foundry project with its own system-assigned identity.
- Three `GlobalStandard` deployments: `orchestrator` (gpt-5.4-mini), `recipe-nutrition`
  (gpt-5.4) and `extraction` (gpt-5.4-nano). Models and versions are the
  `modelDeployments` parameter in `main.bicep`; the app only knows the deployment names.

The deployment output `foundryProjectEndpoint` is the value for
`ASC_FOUNDRY_PROJECT_ENDPOINT`. Set the three `ASC_FOUNDRY_*_DEPLOYMENT` names
in the environment (App Service settings in dev/prod). No key is stored anywhere;
the app signs in with managed identity, or `az login` on a developer machine.
This template grants no roles. The caller needs the Azure AI User role on the
project to invoke models, which the subscription owner assigns.

Deployment is incremental, so it does not change unrelated resources. Run the
same validate and what-if commands as above with a new `--name`
(`ai-sous-chef-dev-foundry-rollout`). Do not reuse `ai-sous-chef-dev-foundry`: it
is the nested module's name, and ARM rejects it with `DeploymentActive`. Expect only the Foundry resource, project and
three deployments in the preview.

After deploying, check the wiring with one small call per deployment:

```bash
ASC_ENVIRONMENT=dev python -m sous_chef.check_foundry
```

It prints `OK` per role, or a failure naming the role and error type only.

## App Service hosting

Issue [ASC-08 / #8](https://github.com/AgamMakkar21/ai-sous-chef/issues/8).
`modules/app-service.bicep` adds one Linux App Service plan, one web app, and a
dedicated user-assigned managed identity (`<prefix>-app-identity`). It is the single
Python web/API deployment; there are no microservices.

- Runtime `PYTHON|3.12`, HTTPS only, TLS 1.2 minimum, FTPS disabled, HTTP/2, Always On.
- Startup command: `python -m uvicorn sous_chef.app:create_app --factory --app-dir backend/src --host 0.0.0.0 --port 8000 --no-access-log`.
- Health check path `/health` (process liveness only). `/ready` returns a generic
  503 problem response with no configuration until a readiness probe exists.
- FTP and SCM basic-auth publishing credentials are disabled. Deploy with Entra.
- App settings are nonsecret only: environment, adapter mode, Cosmos endpoint and
  database, Foundry endpoint and deployment names, `AZURE_CLIENT_ID` of the app
  identity, and the Application Insights connection string. Prompt/message content
  capture is off. Consumer auth adds issuer/JWKS/client/origin settings and a
  Key Vault **reference**, never an embedded provider-secret value. The reference
  uses the dedicated app identity; an approved vault reader grant is a prerequisite.
  No Cosmos or Foundry key exists anywhere.
- This template grants no roles. The app identity needs Cosmos data-plane and
  Azure AI User roles from issues #11/#16, so `/ready` stays 503 until then.

**Region.** The plan location is the separate `appServiceLocation` parameter. Dev
uses Central US because the subscription has no App Service quota in East US 2
(checked for B1, B2, B3, P0v3, P1v3). Foundry, Cosmos and monitoring stay in East US 2.
Cross-region latency and transfer cost are not measured. Prod reads
`ASC_APP_SERVICE_LOCATION`, `ASC_COSMOS_ENDPOINT` and `ASC_COSMOS_DATABASE`.

**Scaling and cost assumptions.** SKU `B1` (default; B2, B3, P0v3, P1v3 allowed),
one instance, no autoscale. B1 is a dev-sized plan billed for as long as it exists;
check the Azure pricing page for the current price (not verified here). Raise the SKU
or add instances only after load data justifies it.

Deploy with a new `--name` (for example `ai-sous-chef-dev-app-rollout`), not the
nested module name `<prefix>-app-service`. Then publish the code with an Entra token:

```bash
git archive --format=zip -o app.zip HEAD backend requirements.txt
az webapp deploy -g <group> -n <app-name> --src-path app.zip --type zip
curl https://<app-host>/health   # {"status":"ok"}
```

`SCM_DO_BUILD_DURING_DEPLOYMENT=true` makes the platform install `requirements.txt`.
The first start after a deploy can return 503 for a few minutes while it builds.

## Agent identities (ASC-12)

The Orchestrator and Recipe + Nutrition agents each get their own Entra Agent identity. Foundry
creates it automatically for every new-model agent; there is no secret to store. Legacy agents have
no identity and must be recreated.

Keep these distinct in configuration (all nonsecret):

| Setting | Meaning |
| --- | --- |
| `ASC_FOUNDRY_ORCHESTRATOR_AGENT_NAME`, `ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_NAME` | Foundry runtime agent names |
| `ASC_FOUNDRY_ORCHESTRATOR_AGENT_PRINCIPAL_ID`, `ASC_FOUNDRY_RECIPE_NUTRITION_AGENT_PRINCIPAL_ID` | Expected Entra Agent principal IDs (`instance_identity.principal_id`) |
| `ASC_APP_WORKLOAD_PRINCIPAL_ID` | App Service managed identity principal ID |

`resolve_agent_identities` reads each deployed agent's identity and fails if two agents share one,
an agent uses the App Service identity, or a pinned ID does not match. Tool allow-lists live in
`sous_chef/domain/agent_identity.py`. No agent identity gets Cosmos access; the backend reads
Cosmos and passes context to the agents.

## Retention, access, and costs

Ordinary operational telemetry has 30-day retention, including explicit
analytics/total retention for Application Insights tables so their special
defaults do not extend storage. The workspace enables immediate purge at the
30-day boundary. This is a retention configuration, not a promise of instant
erasure after a policy change; Azure can delay deletion after shortening retention.
Do not introduce per-table overrides or longer-term exports without review.

Both resources disable local/key authentication. Future application identities
must receive only the required Azure Monitor ingestion permission. Log queries
require Entra authentication and approved workspace-scoped access; operators
should receive Log Analytics Reader only where needed. Resource-context access
alone is disabled. Public endpoints are enabled for the small dev foundation;
they still require authentication. Private networking is not included.

Never send raw chats, prompts, recipes, notes, email, allergies, tool payloads,
or secrets to ordinary telemetry. Sensitive AI tracing stays disabled by default;
this foundation does not enable diagnostic opt-in storage. The backend enforces
allowlisted logging and disables raw AI capture; hosted deployment must also
disable automatic tracing agents and raw URL access-log collection. Protected
capture, custom exporters and dashboards remain later work.

The resource group has no standalone charge. Log Analytics uses `PerGB2018`
pay-as-you-go ingestion, with associated retention/query charges where applicable;
Application Insights billing is workspace-based. No compute or database capacity
is purchased by this issue. Empty resources do not imply permanently free use:
cost grows with telemetry volume. Check the
[Azure Monitor pricing calculator](https://azure.microsoft.com/pricing/details/monitor/)
for the approved subscription/region before enabling ingestion. No fixed monthly
estimate or hard spending cap is claimed. Budgets/alerts and dashboards are later
work; the owner must review usage and costs.
