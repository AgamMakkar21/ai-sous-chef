# Reviewed dev delivery (ASC-59)

[Issue #63](https://github.com/AgamMakkar21/ai-sous-chef/issues/63) adds
`.github/workflows/deploy-dev.yml`. It publishes the Python source and exact,
versioned New Foundry prompt-agent configuration to **existing dev resources**.
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
   [consumer authentication](authentication.md), including the Key Vault reference.
   Cosmos firewall rules must allow the App Service's outbound access.
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
   `repo:AgamMakkar21/ai-sous-chef:environment:dev`. The environment's **dev-only
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

5. Review `deploy/agents.dev.json` against the **actual approved agent versions**.
   The initial pins are `orchestrator:1` and `recipe-nutrition:1`; they are not
   evidence that those names exist in Azure. Change them in a reviewed PR if
   the approved runtime names/versions differ. Both agents must already have
   separate, enabled Entra Agent identities. The pipeline fails if a pinned
   version is absent, disabled, legacy, or identity-shared; it never substitutes
   `latest`, creates an identity-less fallback, or invents prompts/tools.
   Source versions are immutable release inputs, not names of model deployments.

The workflow deliberately has no manual/PR trigger. Complete setup before the
first reviewed merge. Do not broaden permissions to fix a failed bootstrap.
Wait for role propagation and investigate the actual denial at the granted scope.

## Release behavior and evidence

An unprivileged job associates the pushed commit with a merged PR targeting `dev`
in this repository and requires a peer approval of that PR's final head. Direct
pushes, self-approvals and stale/dismissed approvals cannot unlock deployment.
PR CI remains unchanged and has no OIDC permission or Azure login.

The environment-bound deployment job uses GitHub OIDC and Azure CLI login. It
checks out the event SHA, refuses a superseded dev SHA, verifies dev resource
tags/settings/authentication, and reads each manifest's exact agent version
through the pinned **New Foundry SDK**, API `v1`. It copies the entire approved
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
credential or secret value is included. Existing authentication, Key Vault
references and app settings are retained when release pins are updated.

Smoke checks wait up to five minutes for:

- `/health`: exact liveness response **and the expected source commit header**.
- `/ready`: exact readiness response and the same commit. The app reads Cosmos
  container metadata and both pinned Foundry agent versions with its own identity;
  missing permissions, missing containers/versions, identity mismatches and
  timeouts fail closed. No user documents, writes, LLM calls or tool calls occur.
- Anonymous `/auth/session` and `/api/v1/deployment-smoke`: **401**, not a login
  redirect or success.

The Actions environment links the app URL; the run summary records its commit,
agent versions and smoke outcome. The release increases the readiness deadline
to the supported ten-second maximum to allow cold workload authentication.
A successful metadata smoke is not proof of a complete recipe UI, valid consumer
sign-in, tool invocation or food safety. After initial setup, a collaborator must
sign in and perform the available app slices; use the separate approved
[managed-agent invocation smoke](foundry-gateway.md) for execution evidence.
The root still truthfully reports frontend unavailable until the frontend lands.
No worker, Web IQ evaluation suite or production promotion system blocks this
initial delivery pipeline; ASC-09 adds worker delivery before full MVP acceptance.

## Failures and recovery

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
