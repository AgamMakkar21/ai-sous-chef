# Reviewed dev delivery (ASC-59)

[Issue #63](https://github.com/AgamMakkar21/ai-sous-chef/issues/63) adds
`.github/workflows/deploy-dev.yml`. It publishes the Python source and exact,
versioned New Foundry prompt-agent configuration to **existing dev resources**
when agents are explicitly enabled in the reviewed release manifest.
It does not provision the application foundation, deploy the Function worker,
promote production, implement agent prompts/tools, or claim the full MVP is ready.

## One-time owner setup

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
   | `ASC_FOUNDRY_ACCOUNT_NAME` | Existing dev Foundry account name |
   | `ASC_FOUNDRY_PROJECT_NAME` | Existing dev project name |

5. Review the explicit stage in `deploy/agents.dev.json`. The current app-only
   stage has `agents_enabled: false` and an empty `agents` mapping; source agents
   and their unfinished tool integrations do not block the backend/auth rollout.
   For an agent-enabled release, pin the **actual approved agent versions**.
   Both agents must already have
   separate, enabled Entra Agent identities. The pipeline fails if a pinned
   version is absent, disabled, legacy, or identity-shared; it never substitutes
   `latest`, creates an identity-less fallback, or invents prompts/tools.
   Source versions are immutable release inputs, not names of model deployments.

The workflow deliberately has no manual/PR trigger. Complete setup before the
first reviewed merge. Do not broaden permissions to fix a failed bootstrap.
Wait for role propagation and investigate the actual denial at the granted scope.

## Release stages

The checked-in schema-version-2 manifest intentionally starts with:

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
in a peer-reviewed PR: set `agents_enabled` to `true` and populate both
`agents.orchestrator` and `agents.recipe_nutrition` with `name` and positive numeric
string `version` fields. Use verified source versions, not `latest` or assumed
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

The environment-bound deployment job uses GitHub OIDC and Azure CLI login. It
checks out the event SHA, refuses a superseded dev SHA, verifies dev resource
tags/settings/authentication. In the agent-enabled stage it reads each manifest's
exact agent version through the pinned **New Foundry SDK**, API `v1`.
It copies the entire approved
definition (including instructions, tools and safety configuration) into a new
version under the **same agent**, with source-version and commit metadata.
It retains distinct Entra Agent identities; it never recreates agents or grants
them Cosmos access. The application pins the returned numeric versions and
principal IDs, never whatever happens to be latest.

GitHub's Azure CLI credential is used only by deployment. In App Service,
dependency checks and application calls still use only the configured workload
managed identity, with no developer/cloud-secret fallback.

The source ZIP comes from `git archive <event-sha> backend requirements.txt`,
not an untrusted PR artifact or a dirty working directory. It includes a release
commit marker. Oryx installs the locked runtime dependencies using the existing
App Service startup configuration. No `.env`, publishing profile, user data,
credential or secret value is included. Existing authentication and provider
secrets in App Service settings are retained when release pins are updated.
Website Contributor can read those settings; restrict the delivery identity's
OIDC/environment access and never print settings values to logs.

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

Dev has no deployment slots or automatic rollback. Updating release settings
restarts the app, so a release can have downtime. A partial agent publication can
leave unused versions; it does not delete the previous versions. Failures after
updating settings can leave a mixed/failed dev release, never a claimed success.
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
