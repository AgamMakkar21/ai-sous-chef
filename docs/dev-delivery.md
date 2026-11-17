# Reviewed dev delivery (ASC-59)

[Issue #63](https://github.com/AgamMakkar21/ai-sous-chef/issues/63) adds
`.github/workflows/deploy-dev.yml`. It publishes the Python source and exact,
versioned New Foundry prompt-agent configuration to **existing dev resources**
when agents are explicitly enabled in the reviewed release manifest.
Before acquiring Azure credentials or changing the running app, delivery now
requires all six PR quality jobs to have succeeded for the merged PR's exact
reviewed head. The gate follows the newest run for that head and waits up to
12 minutes if it is queued or active; an older successful run cannot hide a
pending or failed rerun. The unprivileged review job allows 15 minutes for this
wait and the review checks. The same guard is reused when sealing the release.
Missing, failed or stale evidence blocks deployment immediately; a run still
incomplete at the deadline also blocks. This protects deployment, not just artifact
retention. GitHub API errors remain explicit failures.

A human collaborator other than the PR author must formally approve the exact
final PR head before merging. A review comment or the merge action alone is not
an approval. For a documentation-only delivery retry, also review the pending
application/configuration changes inherited from dev; the new diff is not the
entire deployment payload.

Agent-enabled smoke checks also execute one synthetic Orchestrator -> Recipe +
Nutrition -> USDA request using the deployed agent pins and protected USDA key.
They require a structured recipe with complete, attributed nutrition for every
ingredient; a healthy `/ready` response, clarification or missing nutrient evidence
cannot pass. Nutrition can combine USDA MCP, Bing-grounded evidence and labeled AI estimates; it does
not require every ingredient or nutrient to be covered by USDA.
The synthetic request is not retried as a whole and reads/writes no consumer
documents. It runs under the delivery identity, not the App Service identity,
so signed-in browser, authentication and workload-specific acceptance remain
separate requirements. Do not treat this dependency check as an authenticated
end-to-end UI test.

The version-4 combined manifest pins one Recipe + Nutrition source with both
recipe MCP and native web search. That same version handles calorie fallback;
an extra `recipe_nutrition.web_version` is rejected in combined mode.
USDA is queried first. Missing calories, protein, carbohydrate, fat, fiber and
sodium are requested from Grounding with Bing for the specified recipe quantities.
Web values require natively cited, searched sources. Existing USDA values are
preserved. Tool-free AI knowledge fills any remaining nutrient gaps, explicitly
labeled approximate with null references and portion assumptions, not fabricated
USDA records, web citations or placeholder zeros. These attributed estimates satisfy
release smoke without requiring every ingredient to have USDA MCP calorie evidence.
If USDA, web and AI recovery cannot complete the nutrition, chat fails explicitly
instead of delivering a partially calculated recipe.
The independent recovery smoke checks all six nutrients for every ingredient.
The allergy check compares declared ingredient names with stored allergies,
including recognized aliases; it does not reject prose or unknown catalog names.

Run `43656633821` deployed the app successfully, then failed this check after a
USDA timeout and an invalid web fallback response. The fixed 30-second fallback
reservation could give USDA an expired deadline after slow agent generation.
Nutrition now divides the remaining usable time instead, and orchestration has
120 seconds, durable chat sending 130 seconds, and browser waiting/release smoke
150 seconds. Earlier caller deadlines and individual provider timeouts still
apply. This does not bypass complete-nutrition or allergy checks, and the original
log does not identify which native web-response validation predicate failed.

The first fallback release's deployment check incorrectly selected `environment=local`
to obtain delivery credentials, rejecting a valid 4,949-token recipe MCP response
against the local token limit. The check now preserves deployed `ASC_ENVIRONMENT`
and explicitly uses Azure CLI credentials. MCP-response token-count rejection has
been removed; character, schema, provenance, deadlines and USDA retries remain.
The app deployment had succeeded before that failed check; the worker/release
sealing stages had not run.

The November 13 chat failure reached both agents, then failed in USDA lookup
with `foundry_unavailable`; the old smoke check still passed because it only
checked health, metadata readiness and rejection of anonymous requests.
The recorded error does not identify USDA's exact HTTP response or prove that
a particular PR caused the outage. Read-only probes from the App Service SCM
diagnostic environment reproduced intermittent USDA HTTP timeouts without MCP:
four food-detail requests and one search exceeded a five-second socket timeout,
while four searches succeeded. Later probes of the same carrot/chicken search
and food-detail endpoints succeeded, including food ID 170393. This demonstrates
intermittent dependency latency, not permanently missing food records. SCM is a
separate diagnostic process, not the live application container, and these later
probes cannot establish the exact cause of the earlier `foundry_unavailable`.
The application's HTTP timeout is five seconds; its default MCP session budget
is ten seconds and covers multiple sequential searches/profiles. Both latency
and accumulated lookup work can therefore exhaust a request's budget.
Read-only USDA search/profile requests now retry transient connection failures,
timeouts and HTTP 500/502/503/504 responses at most twice, with short backoff.
The existing MCP session and chat deadlines still bound total time. Authentication,
quota, missing-food and malformed-data errors are not retried; quota cooldown
remains enforced. Persistent failures remain explicit, with no invented nutrition,
agent replay or weakened allergy validation. Recipe-only transient USDA outages
retain completed source evidence and use the explicitly attributed calorie
fallback; they do not fabricate USDA IDs or fill missing non-calorie nutrients.

GitHub can briefly return incomplete merge/review evidence immediately after a
push. The review gate checks up to six times, waiting five seconds between
checks (at most 25 seconds of waiting). Every check still requires the exact
merged commit and a current human peer approval of the final PR head; missing,
stale, self, bot or dismissed approvals never authorize deployment. GitHub API
errors remain explicit failures rather than being treated as missing evidence.
Worker health polling allows up to ten minutes so the platform's initial
230-second container warm-up and a subsequent startup retry can finish. It still
requires the exact deployed commit and host-only response; a timeout, stale
commit, redirect or malformed response never counts as success. Host health does
not verify learning or Cosmos permissions.
It does not provision the application foundation,
promote production, implement agent prompts/tools, or claim the full MVP is ready.
ASC-09 extends the same reviewed job to deploy the
[feedback Functions host](feedback-host.md) after web smoke checks. Complete its
one-time provisioning, worker-scoped publisher grant and `ASC_FUNCTION_APP_NAME`
environment variable before merging that extension; missing setup fails delivery.
After those safety checks, worker deployment applies and reads back the declared
Python 3.12, Always On and `/api/health` host settings when they have drifted.
It does not change identities, connections, hosting plans or permissions.
Read-only smoke/production/rollback checks still reject drift; deployment errors
name the mismatched host fields without printing configuration values.

For Cosmos SDK diagnostics, the pinned azure-core transport uses
`tracing_options={"enabled": False}`, not `tracing_enable=False`. The latter
survives the Cosmos pipeline as an unknown aiohttp argument and makes profile
loads return HTTP 500 before a Cosmos request is sent. The repository regression
exercises the real aiohttp transport against a local service double so permissive
mock transports cannot hide this failure. A successful static shell or anonymous
health check alone is not proof that a signed-in kitchen can load its profile.

## One-time owner setup

The source ZIP includes the app-owned USDA FoodData Central MCP server;
the normal Python lock includes its runtime dependencies. No upstream executable
or USDA database is downloaded.
After that reviewed artifact is deployed, follow
[shared USDA setup](recipe-specialist.md#shared-developer-and-azure-setup) to set
protected `ASC_USDA_API_KEY`, allow outbound HTTPS to `api.nal.usda.gov`,
and enable `ASC_USDA_ENABLED=true`. Each restart then checks real USDA data over
MCP before serving traffic. No laptop endpoint or manual sidecar process is used.
The deployment preserves these owner-managed settings; it does not provision
PostgreSQL, import USDA data, register API keys or copy developer credentials.

1. Retain the [required PR checks and peer-review rules](ci.md) on `dev`.
   Create a GitHub Actions environment named **dev**. Under deployment branches
   and tags, select **Selected branches and tags**, add the **branch** `dev`,
   and allow no other branches or tags. Do not use "protected branches" as a
   substitute: `main` is protected too. Environment reviewers may add an approval
   gate if desired; they do not replace code review.
2. Inspect the existing App Service and Foundry project IDs, tags and configuration
   using the [foundation outputs](../infra/README.md). Both must have
   `project=ai-sous-chef` and `environment=dev`. The app must have its own
   user-assigned identity, live Cosmos/Foundry settings, and working
   [consumer authentication](authentication.md), with its protected App Service credential.
   Cosmos firewall rules must allow the App Service's outbound access.
   Authentication can use the original External ID mode or the explicit
   `app_service` mode (Google/GitHub/single-tenant Microsoft); see the
   [multi-provider setup](authentication.md#multi-provider-owner-setup-existing-app-no-new-tenant).
   The latter requires all three registered providers, nonempty directly stored
   App Service secrets (not Key Vault references),
   canonical origin, token store, exact public routes and the fixed session policy.
   Microsoft uses the configured tenant's v2 issuer and an exact app audience;
   `ASC_AUTH_MICROSOFT_TENANT_ID` and `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` must match
   that tenant. The registration must separately use `AzureADMyOrg` (single-tenant).
   The delivery guard reads the ARM auth settings directly and rejects mismatches
   before publishing agents or code; it does not create provider registrations.
3. Have the identity/RBAC owner preview and deploy `infra/dev-delivery.bicep` in
   the approved dev group. It creates a **separate** GitHub delivery identity,
   one federated credential, **Website Contributor on this app only**, and
   **Foundry User on this project only** (same role as the existing Foundry access
   module). No subscription/group Contributor, Owner, Cosmos access, role-writing
   privilege, production grant, or cloud password is given to GitHub.
   The owner must check that both supplied targets are dev resources before
   applying the role assignments. Example in PowerShell:

   ```powershell
   az deployment group what-if --subscription <dev-subscription-id> --resource-group test-2 --template-file infra\dev-delivery.bicep --parameters appName=<dev-app> foundryAccountName=<dev-foundry-account> foundryProjectName=<dev-project> location=<identity-region>
   az deployment group create --subscription <dev-subscription-id> --resource-group test-2 --name ai-sous-chef-dev-delivery --template-file infra\dev-delivery.bicep --parameters appName=<dev-app> foundryAccountName=<dev-foundry-account> foundryProjectName=<dev-project> location=<identity-region>
   ```

   The issuer is `https://token.actions.githubusercontent.com`, audience
   `api://AzureADTokenExchange`, and subject
   `repo:AgamMakkar21@72168349/ai-sous-chef@1409242851:environment:dev`.
   This matches the ID-qualified subject observed in the GitHub Azure login
   step; the name-only subject does not match tokens issued for this repository.
   Issuer, subject and audience must match exactly. The environment's **dev-only
   branch policy is essential** because an environment-subject token does not
   encode a branch. Never add a PR-subject credential or reuse the app identity.
4. Set these nonsecret **environment variables** in GitHub's `dev` environment:

   | Variable | Value |
   | --- | --- |
   | `AZURE_CLIENT_ID` | Delivery identity client ID from the bootstrap output, **not** the App Service client ID |
   | `AZURE_TENANT_ID` | Bootstrap tenant ID |
   | `AZURE_SUBSCRIPTION_ID` | Approved dev subscription ID |
   | `ASC_RESOURCE_GROUP` | Approved existing dev group (`test-2`) |
   | `ASC_APP_NAME` | Existing dev web app name |
   | `ASC_FUNCTION_APP_NAME` | Separate dev feedback Function App name (ASC-09) |
   | `ASC_FOUNDRY_ACCOUNT_NAME` | Existing dev Foundry account name |
   | `ASC_FOUNDRY_PROJECT_NAME` | Existing dev project name |

5. Review the explicit stage in `deploy/agents.dev.json`. The current candidate
   enables combined-mode chat with the read-back-verified Orchestrator source 54,
   Recipe + Nutrition combined source 76, tool-free nutrition recovery source 62
   and AI recipe recovery source 75.
   This is release configuration,
   not evidence that the candidate has been deployed or passed live UI acceptance.
   Agent-enabled releases pin the **actual approved agent versions**.
   Both agents must already have separate, enabled Entra Agent identities.
   If neither agent exists yet, use the owner-approved
   [first-time bootstrap](orchestrator.md#first-time-creation-when-the-project-has-no-agents)
   before activating the manifest. The pipeline fails if a pinned
   version is absent, disabled, legacy, or identity-shared; it never substitutes
   `latest`, creates an identity-less fallback, or invents prompts/tools.
   Source versions are immutable release inputs, not names of model deployments.

   Configure the recipe-grounding settings on **App Service**, not only in the
   publisher's local `.env.dev`: `ASC_FOUNDRY_IQ_SEARCH_ENDPOINT`,
   `ASC_FOUNDRY_IQ_INDEX_NAME`, `ASC_FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME`,
   `ASC_FOUNDRY_IQ_KNOWLEDGE_BASE_NAME`, `ASC_FOUNDRY_IQ_CONNECTION_NAME`, and
   `ASC_FOUNDRY_PROJECT_RESOURCE_ID`, alongside the existing
   `ASC_FOUNDRY_PROJECT_ENDPOINT`. Use the verified Search resources and Foundry
   project; these are nonsecret resource identifiers, not credentials. The enabled
   release checks for missing or blank values before publishing agent versions,
   uploading code, or changing the feature flag. An app-only release does not
   require these settings. A published specialist alone does not configure the
   backend: missing settings prevent application startup and make `/health`
   return a platform 503.

The workflow deliberately has no manual/PR trigger. Complete setup before the
first reviewed merge. Do not broaden permissions to fix a failed bootstrap.
Wait for role propagation and investigate the actual denial at the granted scope.

## Release stages

The current dev agent design is **combined**: native web search and recipe MCP
are published on one Recipe + Nutrition source version, with distinct tool-free
nutrition and AI recipe recovery sources. After publishing and exactly verifying the reviewed
sources, use this manifest shape:

```json
{
  "schema_version": 7,
  "agents_enabled": true,
  "agents": {
    "orchestrator": {
      "name": "sous-chef-orchestrator-dev",
      "version": "54",
      "contract_sha256": "724d6e50aea7108083f0a82633be0de1c5489c4566f5bc8b956540db594d9950"
    },
    "recipe_nutrition": {
      "name": "sous-chef-recipe-nutrition-dev",
      "version": "76",
      "calorie_version": "62",
      "ai_recipe_version": "75"
    }
  }
}
```

The pins above are the actual read-back-verified sources selected for this candidate,
not assumed next-version numbers. Future releases must read back their selected
versions again. Deployment sets `ASC_FOUNDRY_RECIPE_NUTRITION_MODE=combined`,
verifies and clones all four exact sources under their existing identities, then pins
the resulting release versions. It does not publish separate retrieval/web phases.
Orchestrator source 54 carries policy 13, bounded MCP-to-AI recipe recovery and
direct delivery after deterministic gates, including the evidence-backed spice
baseline policy. Source 75 is the tool-free `ai_recipe` phase; it does not replace
combined source 76 or nutrition source 62. Combined source 76 still has both recipe
MCP and native web tools attached.
The version-7 manifest records its read-back-verified `asc_contract_sha256` as
`contract_sha256`. Manifest validation, already exercised by infrastructure PR CI,
recomputes that contract from the current instructions and function schema before
any Azure access. A policy-version, instruction or tool-schema change therefore fails PR
checks until the reviewed source is published, read back and both the numeric pin
and receipt are updated together. The portable contract normalizes environment-specific
model-deployment and RAI identifiers; deployment still validates the full remote
definition, its full fingerprint, policy metadata, active state and identity.
This receipt does not replace live verification or authorize publication/deployment.
Shared schema changes also require fresh affected specialist sources. The
measurement-unit merge added `fl oz`, `qt`, `gal` and `dozen` to the recipe schema,
so older combined/AI-recipe definitions failed exact phase verification even
though application CI passed. Sources 76 and 75 include those units; unchanged
nutrition source 62 was separately verified and retained. Publish, read back and
update all affected pins in the same PR as a shared-schema change; never bypass
the mismatch or override a published response schema at invocation time.
App Service must
receive the compatible backend and complete runtime pins together through delivery;
do not enable its flag manually against the older backend. USDA secrets/enabling
and the stable `ASC_PAGINATION_KEY` remain owner-managed.
The release smoke runs ordinary yogurt, chicken and household rice/beans requests,
then injects retrieval unavailability while retaining real pinned AI composition
and USDA/AI nutrition. It requires a complete honestly attributed recipe, not
just a successful model HTTP response. This synthetic dependency check does not
read/write consumer documents or prove signed-in browser/app-identity acceptance.
Applying a retained bundle removes only omitted specialist phase-version settings
and verifies their absence; unrelated authentication/workload settings are preserved.

This backend writes **data contract 2** because recipe snapshots now carry
`generationSource` and may have no curated evidence for explicitly labelled AI
recipes. It reads older snapshots, but a contract-1 backend cannot read the new
format. Rollback/rehearsal across contracts 1 and 2 stays blocked. Establish
compatible contract-2 retained releases before rollback rehearsal or production
promotion; do not lower the contract number, bypass that gate or claim an old
contract-1 bundle is a safe rollback after new snapshots have been written.
Source publication/read-back does not activate the application.
Post-execution web-query validation is not pre-search privacy isolation; review
the [combined-agent boundaries](recipe-specialist.md#combined-recipe-agent-with-native-web-search).

The earlier allergy-focused chat sources retain the existing enabled, distinct agent identities.
Read-back verified the full definitions and metadata, including definition
fingerprints `ce0c57fff2bc549e9550b67c7af71a004e3d2c82043d43725302a1a127419da5`
for Orchestrator source 12 (policy 4) and
`f4d8b96b7a41ed18b87db369b9997f92a255bacf5a3ed1df75ee5401b3b03de4`
for combined Recipe + Nutrition source 16 (policy 8). These instructions remove
the cooking/format gates, retain hard allergy checks, and request substitutions
rather than discarding an allergenic source recipe. These source publications do not
change the running app's pins or prove signed-in chat acceptance; activate them
only through the reviewed dev delivery workflow.

The grams-first nutrition repair in PR #137 requires combined policy 9; source 16
does not satisfy that contract. Combined source 21 was published under the same
existing specialist identity and read back with its complete definition and metadata.
Its verified definition fingerprint is
`3b8530c2ba0a3c6eb7c5ec0f6a9e653471534387d04b37ef765e3a62f60cc635`.
The model deployment, approved content-safety policy, MCP/native web tools and
strict output schema remain unchanged from source 16. Publication changed no app
settings or runtime pins. The reviewed source-pin merge, successful deployment
and signed-in calorie acceptance remain required before calling the fix live.

The schema-version-3 instructions below remain for legacy phased releases.

An explicit app-only release or rollback uses schema version 2:

```json
{
  "schema_version": 2,
  "agents_enabled": false,
  "agents": {}
}
```

This deploys the exact reviewed backend commit with `ASC_AGENTS_ENABLED=false`.
No Foundry agent SDK client is created, no source version is read or published,
and runtime managed-agent execution is blocked even if previous pins remain.
Authentication, dev target/OIDC checks, Cosmos readiness and exact-commit smoke
checks remain required. The target guard still validates the existing dev Foundry
resource/endpoint configuration; it does not depend on agent-service availability.
The switch does not delete remote agents, disable model deployments, or substitute
direct-model generation. Existing non-AI APIs remain available; unimplemented
chat/recipe/UI features are not claimed as delivered.

After the required tools and source definitions are ready, change this same file
in a peer-reviewed PR: set `schema_version` to `3`, set `agents_enabled` to `true` and populate both
`agents.orchestrator` and `agents.recipe_nutrition` with `name` and positive numeric
string `version` fields. The specialist additionally requires positive numeric
`retrieval_version` and `web_version` strings, distinct from its tool-free
generation `version`. All three remain under the same specialist `name` and
identity; they are not additional agents. For example, with deliberately illustrative pins:

```json
{
  "schema_version": 3,
  "agents_enabled": true,
  "agents": {
    "orchestrator": {"name": "orchestrator-agent", "version": "1"},
    "recipe_nutrition": {
      "name": "recipe-agent",
      "version": "5",
      "retrieval_version": "3",
      "web_version": "4"
    }
  }
}
```

Use verified source versions, not these illustrative numbers, `latest` or assumed
version `1`. That merge activates the existing agent release path and writes
`ASC_AGENTS_ENABLED=true` together with the returned version/identity pins.
No new workflow or manual runtime toggle is needed. Failures never automatically
fall back to app-only. To turn app invocation off again, a reviewed manifest change
sets the flag to false and empties `agents`; old pins are retained but inactive.
Do not manually change the runtime flag: smoke checks require it to match the
reviewed manifest. App-only delivery leaves the agent portion of ASC-59 pending.

## Release behavior and evidence

An unprivileged job associates the pushed commit with a merged PR targeting `dev`
in this repository and requires a peer approval of that PR's final head. Direct
pushes, self-approvals and stale/dismissed approvals cannot unlock deployment.
PR CI remains unchanged and has no OIDC permission or Azure login.

The environment-bound deployment job uses GitHub OIDC and Azure CLI login via
the commit-pinned, Node.js 24 Azure Login v3 action. It
checks out the event SHA, refuses a superseded dev SHA, verifies dev resource
tags/settings/authentication. In the agent-enabled stage it reads each manifest's
exact agent version through the pinned **New Foundry SDK**, API `v1`.
Before creating any release version, it verifies the Orchestrator's current policy,
instructions, strict tool schema and fingerprint, and every specialist source's phase,
policy metadata, fingerprint, tool restriction and output schema. It copies the entire approved
definition (including instructions, tools and safety configuration) into a new
version under the **same agent**, preserving source/phase metadata and adding
source-version and commit metadata.
It retains distinct Entra Agent identities; it never recreates agents or grants
them Cosmos access. The application pins the returned numeric versions and
principal IDs, never whatever happens to be latest.

GitHub's Azure CLI credential is used only by deployment. In App Service,
dependency checks and application calls still use only the configured workload
managed identity, with no developer/cloud-secret fallback.

The backend source ZIP comes from `git archive <event-sha> backend requirements.txt`,
not an untrusted PR artifact or a dirty working directory. The job builds the
[frontend](frontend.md) from that same reviewed checkout with Node 24 and `npm ci`
before Azure login. Packaging adds its self-contained static HTML and a release
commit marker, and rejects a missing frontend build.
Oryx installs the locked runtime dependencies using the existing
App Service startup configuration. No `.env`, publishing profile, user data,
credential or secret value is included. Existing authentication and provider
secrets in App Service settings are retained when release pins are updated.
Website Contributor can read those settings; restrict the delivery identity's
OIDC/environment access and never print settings values to logs.

The existing app must enable `SCM_DO_BUILD_DURING_DEPLOYMENT=true` and must not
mount `WEBSITE_RUN_FROM_PACKAGE`; a source-only ZIP does not contain installed
dependencies. Delivery uploads asynchronously with deployment-triggered restart
and runtime tracking disabled. Azure CLI still polls the **Kudu build**, and the
script requires its terminal success status (`4`) before continuing; upload
acceptance or an in-progress status is not success. Only then does it update
release settings and explicitly restart the app, including when pins are
unchanged. This prevents a settings-triggered restart from interrupting Oryx or
starting a container without its build manifest and virtual environment.
The existing five-minute runtime smoke remains mandatory after restart.
The restart command uses `--output none`, not JSON parsing: a successful Azure
CLI restart returns no JSON document. A nonzero restart exit still fails delivery,
and commands that require structured results still reject empty or malformed JSON.

Smoke checks wait up to five minutes for:

- `/health`: exact liveness response **and the expected source commit header**.
- `/ready`: exact readiness response and the same commit. The app reads Cosmos
  container metadata and, only when agents are enabled, both pinned Foundry agent
  versions with its own identity;
  missing permissions, missing containers/versions, identity mismatches and
  timeouts fail closed. No user documents, writes, LLM calls or tool calls occur.
- Anonymous `/auth/session` and `/api/v1/deployment-smoke`: **401**, not a login
  redirect or success.

The Actions environment links the app URL; the run summary records its commit,
agent enabled/disabled stage, any published versions and smoke outcome.
The release increases the readiness deadline
to the supported ten-second maximum to allow cold workload authentication.
A successful metadata smoke is not proof of a complete recipe UI, valid consumer
sign-in, tool invocation or food safety. After initial setup, a collaborator must
sign in and perform the available app slices; use the separate approved
[managed-agent invocation smoke](foundry-gateway.md) for execution evidence.
The root still truthfully reports frontend unavailable until the frontend lands.
No worker, Web IQ evaluation suite or production promotion system blocks this
initial delivery pipeline; ASC-09 adds worker delivery before full MVP acceptance.

## Failures and recovery

If a reviewed merge starts delivery while a newer PR quality run is still
running, the CI gate waits instead of immediately rejecting incomplete evidence.
It still requires that newest run to finish successfully with all six checks.
For a release that failed under the earlier immediate-rejection behavior,
confirm that the exact-head CI and peer approval are valid before rerunning
the failed workflow. Do not select an older green run or bypass the gate.

If **Verify reviewed dev merge** fails with "No merged dev PR with a current
peer approval matches this commit", deployment has not started. A merge by a
collaborator is not itself an approval: a human collaborator other than the PR
author must submit **Review changes -> Approve** against the final PR head.
Ordinary comments and approvals of earlier commits do not satisfy the gate.

For a PR already merged without approval, use a new recovery PR into `dev` if
post-merge approval is unavailable. The reviewer must inspect the previously
merged deployment changes as well as the recovery diff, submit an explicit
approval, wait for required checks, and then merge without adding more commits.
That new merge triggers delivery of the current dev source. Do not disable the
review gate, fabricate approval, or repeatedly rerun a commit with no approval.
Check the repository ruleset if an unapproved merge was permitted.

Deployment jobs are serialized without interrupting an in-progress release.
GitHub can replace queued pending runs with newer ones; superseded commits are
not guaranteed their own deployment. A stale queued run cannot overwrite a
newer dev release. Any publication, ZIP deployment or smoke failure fails the
workflow; the summary's URL alone is not successful acceptance.

For `az webapp deploy` failures, inspect **Deployment Center -> Logs** and
`LogFiles/StartupLogs` before retrying. The 2026-10-16 failure restarted the app
while Oryx was unfinished: startup could not find `oryx-manifest.toml`, `antenv`
or `__oryx_packages__`, and exited with `No module named uvicorn`. The corrected
build/settings/restart ordering addresses that race; increasing startup timeout
alone does not install missing dependencies. A failed/timed-out CLI request can
leave a server-side deployment marked "Building"; verify whether the builder is
still running before starting another deployment. Command errors identify the
failed operation but deliberately withhold credential-bearing CLI output.

Dev has no deployment slots or automatic rollback. ASC-60 adds retained source
archives and an explicitly dispatched [rollback rehearsal](production-release.md);
it does not automatically recover a failed dev deployment. Updating release settings
restarts the app, so a release can have downtime. A partial agent publication can
leave unused versions; it does not delete the previous versions. Failures after
updating settings can leave a mixed/failed dev release, never a claimed success.
A failed build does not apply new release pins or issue the final restart, but
Oryx may already have removed/replaced files in the deployment directory; this
is not an atomic release or a guarantee that the previous app remains healthy.
Correct or revert the change through a newly reviewed PR into `dev`; its merge
rebuilds the corresponding source and agent release. Do not delete old versions
needed by existing app pins.

Foundation IaC remains an owner-operated lifecycle, not part of this publisher.
Reapplying the foundation's complete app-settings array resets release pins:
follow that operation with a reviewed dev deployment. Do not run the foundation
template from PR validation or hand the publisher its provisioning/RBAC rights.
No production resource is a deployment target.

## Offline checks

With the repository's Python 3.12 dependencies installed, run the ordinary lint,
type and backend tests. Deployment orchestration tests also run in the existing
infrastructure check, with Bicep 0.48.1:

```powershell
$env:BICEP_CLI = "<path-to-bicep.exe>"
python -m unittest discover -s tests\infrastructure -v
python -m pytest tests\unit\test_live_readiness.py tests\integration\test_application.py
```

Offline fixtures verify review gating, packaging, pinned agent publication,
identity separation, dev-target rejection, commit/auth/dependency failures and
scoped federation/RBAC. They are not a live deployment receipt. Keep #63 open
through feature-to-dev integration until the team's reviewed dev-to-main
completion policy is satisfied.
