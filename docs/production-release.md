# Reviewed production promotion and rollback (ASC-60)

ASC-60 adds release plumbing, not production approval or a declaration that the
MVP is complete. A release must come from this repository's **dev -> main PR**.
The publisher promotes the exact source ZIPs that passed dev deployment and
smoke checks. It never provisions resources, changes RBAC, migrates data, publishes
new agent versions, or substitutes `latest`.

**Current owner gates:** on 2026-11-02, the repository was public, `main` had no
effective protection rules, and no `prod` GitHub environment existed. The new
workflow refuses production in that state. Workflow files alone do not enforce
merges or establish a protected environment. The current collaborator has write,
not administrator, access. No production deployment or live rollback rehearsal
was performed while implementing this feature.

## What is promoted

After App Service and Function deployment and both smoke checks pass,
`Deploy dev` retains an immutable GitHub artifact named
`asc-release-<source-commit>-<run-attempt>`. It contains:

| File | Release input |
| --- | --- |
| `app.zip` | Reviewed backend, locked requirements, compiled frontend, verified USDA source, exact source marker |
| `worker.zip` | Reviewed Function source and requirements, with the same source marker |
| `runtime.json` | Dev feature flag and exact agent/workload pins; no complete app settings or secrets |
| `bundle.json` | Source commit/tree, six-check CI evidence, dev run/attempt, target references, data contract and file SHA-256 digests |

Agent definitions are environment-specific. Production config pins existing
approved **production** New Foundry prompt-agent versions, Entra principal IDs,
and definition digests. The publisher checks the definitions, enabled state and
distinct workload/agent identities; it does not copy dev principal IDs or create
another version. Calculate a definition digest from the approved version's
`definition.as_dict()` serialized with `json.dumps(..., sort_keys=True,
separators=(",", ":"))`, encoded as UTF-8.

These are immutable **source archives**, not an installed Python image. Oryx
rebuilds runtime dependencies using the same archived exact-version requirements
and Python 3.12. The publisher verifies archive bytes, source markers, approved
config and deployed pins/commit. It does not claim bit-for-bit reproducible
runtime binaries: registry contents and the platform build image are external
inputs. Fully prebuilt/container delivery is not introduced here.

Artifacts are retained for 90 days. Keep required release records and agent
versions available; expiration/deletion blocks promotion or rollback instead of
silently rebuilding or selecting a different version.

## One-time owner setup

1. Restore the ruleset for `dev` and `main` described in [CI setup](ci.md).
   Require a peer PR approval, dismiss stale approvals, require approval of the
   latest reviewable push, block deletion/force-push, and require up-to-date
   branches with all six `CI / ...` checks from GitHub Actions. Do not configure
   routine bypass. `CI / build` now rejects feature/fork -> main PRs and stale or
   incomplete release evidence. Production independently checks the merged
   dev -> main PR and final-head peer approval, so an unprotected merge is not
   sufficient to deploy.
2. Obtain explicit owner approval for the production subscription, resource group,
   regions, plans, resource ownership and costs. Provision only after an inspected
   owner-operated what-if. Reuse `infra/environments/prod.bicepparam` for the
   foundation and `prod-functions.bicepparam` for the Function host, supplying an
   existing approved production Cosmos account and private-network references.
   The production group must differ from dev. Never point production at the dev
   Cosmos account, authentication registrations, runtime identities or agent
   identities. Foundation/service setup and schema migrations are not publisher
   actions.
3. Bootstrap a **separate** production GitHub publisher with
   `infra/dev-delivery.bicep` with explicit `environment=prod` and the approved
   production web, Function and Foundry names. The legacy filename remains for
   existing dev setup; its default stays `dev`. Preview first. Production creates
   a different identity, role-assignment names and OIDC subject:
   `repo:AgamMakkar21@72168349/ai-sous-chef@1409242851:environment:prod`.
   Grants remain Website Contributor on the two sites and Foundry User on the
   specific project, with no Cosmos, group/subscription Contributor or RBAC-write
   privilege. Runtime workload and agent identities stay separate.
4. Create `prod` under GitHub **Settings -> Environments**. Allow only the
   **branch** `main`; no tags, patterns, other branches or PR refs. Set the sole
   required reviewer to `AgamMakkar21`, enable **Prevent self-review**, and
   disallow administrator bypass. Configure `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`
   and `AZURE_SUBSCRIPTION_ID` environment variables for the separate publisher.
   The workflow initiator must not be the required reviewer. If the owner
   initiated a waiting run, cancel it and have the other collaborator dispatch
   a new main run; never bypass the gate.
5. In a reviewed feature PR into dev, copy `deploy/production.example.json` to
   **`deploy/production.json`** and replace every placeholder with approved
   nonsecret production references. `{}` agents means explicitly app-only, not
   a ready MVP. An enabled config supplies both roles:

   ```json
   {
     "orchestrator": {
       "name": "<production-agent-name>",
       "version": "<positive numeric version string>",
       "principal_id": "<agent Entra principal GUID>",
       "definition_sha256": "<approved definition SHA-256>"
     },
     "recipe_nutrition": {
       "name": "<different production-agent-name>",
       "version": "<positive numeric version string>",
       "principal_id": "<different agent Entra principal GUID>",
       "definition_sha256": "<approved definition SHA-256>"
     }
   }
   ```

   Keep OAuth/database secrets in protected platform configuration, never this
   file or release artifacts. The initial app-only manifest is intentional;
   ASC-63 must not mistake it for working agent invocation, feedback learning or
   a completed MVP.

### GitHub capability limitations

[GitHub documents](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
that required environment reviewers are available only for public repositories
on Free, Pro and Team plans. Private/internal use needs a supported plan;
branch/tag restrictions have their own plan availability. Recheck the actual
repository and plan before relying on them. If reviewers, self-review prevention
or branch policy cannot be configured, **production stays blocked**. A checkbox,
manual workflow trigger, approval comment or environment name is not a substitute
for an enforced environment gate. The API guard verifies reviewer/branch rules;
the owner must also inspect and disable administrator bypass in settings.

## Dev rollback rehearsal

### First bootstrap on the default branch

This repository's default branch is `main`.
[GitHub requires manual workflows on the default branch](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).
Requiring a rehearsal before initially installing that workflow would deadlock
the first release. After feature review into dev, make a **peer-reviewed,
plumbing-only dev -> main PR**, with no `deploy/production.json` and the exact
body acknowledgement `<!-- asc60-bootstrap -->`. All normal quality checks and
peer review still apply. The source guard still rejects features and forks.
The production workflow explicitly skips deployment before referencing an
environment or requesting credentials.

This bootstrap publishes workflow definitions, **not an app/Function/agent
release**. It cannot authorize production when a real config exists. Add the
owner-approved production config later through feature -> dev review, complete
the rehearsal below, and use the full promotion template for actual production.

Wait until this feature has been peer-reviewed into dev and **two different
reviewed dev commits have successful artifact-producing deployments**. Older
successful deployments without `asc-release-*` artifacts cannot be reconstructed
and presented as tested immutable releases.

Coordinate a dev maintenance window. Dispatch **Rehearse dev rollback** from
the **dev** branch with the current and previous successful deployment run IDs.
It uses the existing dev-only environment/publisher and the same `deploy-dev`
concurrency group, so it cannot overlap a normal dev deployment.

The rehearsal verifies both artifact provenances and current-head approval,
same target resources, identities and rollback-compatible data contracts. It
deploys the previous app/Function archives and exact agent pins, runs both smoke
checks, then restores and smokes the current release **even when the previous
smoke fails**. A failed rollback or restoration fails the run; failed restoration
needs immediate owner recovery. There is no successful evidence artifact unless
both phases pass. The retained `asc-rollback-rehearsal-<attempt>` record identifies
both commits and smoke outcomes.

No product documents are read, migrated, rewritten or deleted by the publisher;
its identity has no Cosmos access. Offline tests exercise rollback/restore with
simulated persistent data and reject unexpected data-plane operations. They
are **not a live no-data-loss certificate**. For live acceptance, collaborators
must record a synthetic owned profile/recipe-history canary before and after the
rehearsal using their normal authenticated app access. Keep only the comparison
outcome and trace/run references in evidence, never actual profile/recipe data.
Worker processing and background user writes require their own application-level
compatibility validation. Host-only health does not prove learning or data
integrity.

## Promotion PR and production approval

Open the PR with `?template=promotion.md`, base **main**, head **dev**. Fill the
`asc60-release` JSON block with the exact source, successful dev deployment,
successful current-source rollback rehearsal, `bundle.json` SHA-256,
`production.json` SHA-256, and an evaluation-evidence URL.

Evaluation evidence must be a successful repository workflow run for that dev
commit, or a human collaborator's issue comment naming the exact source and
the real evaluation results/limitations. This records reviewed evidence; it does
not implement or claim the comprehensive readiness evaluation. **ASC-63 remains
the actual release gate.** Incomplete journey/learning/safety evidence must block
the owner's readiness acceptance.

The owner records these exact lines in a repository issue comment only after
approving the environment/costs and ASC-63 readiness for that source/artifact:

```text
ASC-60: approve production environment and costs; config-sha256=<production.json SHA-256>
ASC-63: release readiness accepted; source=<dev SHA>; bundle-sha256=<bundle.json SHA-256>
```

Set repository-level variable `ASC_OWNER_APPROVAL_COMMENT_ID` to that comment's
numeric ID. It is repository-level so the unprivileged preflight can verify it
**before** requesting the prod environment. The script verifies the author's
actual owner login; pasting an owner's name into another author's comment fails.
Production also requires the separate GitHub environment approval.

New dev commits invalidate source/evidence and need fresh review. Reruns have
separate attempt-qualified artifacts; changing archive bytes invalidates the
recorded bundle digest. The main merge's tree must equal the tested dev tree.
CI records all six checks for the exact reviewed feature head; integrated dev
deployment and smoke evidence bind the resulting merge source.

After peer approval and merge, **Promote production** performs read-only
preflight, waits for enforced owner environment approval, rechecks all evidence,
deploys both exact archives and approved production agent pins, and smokes
authentication, dependencies and both source markers. Only success retains
`asc-production-release-<attempt>` with the exact production config/rollback
record. No production IaC or role assignment is run by Actions.

## Production rollback and schema compatibility

Dispatch **Promote production** from **main**, operation **rollback**, and supply
the previous successful production workflow run ID. This is explicit selection,
not `latest`. Main review/protection, current target/cost approval and the actual
prod environment approval remain required. The previous run must have its
successful, attempt-qualified production artifact; dev artifacts are not
production rollback records.

Rollback restores the previous app/Function archive bytes and approved
production agent versions, verifies identities/definition digests and smokes
both hosts. It cannot change target resources or restore a full settings dump:
authentication secrets, managed identity bindings, network settings and existing
data remain in place. Old agent versions are never deleted by these workflows.

`deploy/compatibility.json` declares the stored-data contract and rollback safety.
The initial contract is **1**, matching current `schemaVersion: 1` documents.
Automatic rollback accepts only the same data contract with explicit
`rollback_safe: true`. Review changes to document shapes, discriminators, required
fields, indexes, TTL, lease/checkpoint formats and agent/tool schemas before
retaining that assertion. Older code must read newer writes and retries must
remain idempotent. Contract changes or destructive migrations require a separate
reviewed expand/contract and recovery procedure; the publisher never downgrades
data or silently replays learning events.

Basic plans have no deployment slots here. Source ZIP/Oryx delivery is not atomic:
builds, settings changes, restarts or smoke failures can leave a mixed or
unavailable release. A failed run is not successful deployment and does not
automatically roll back. Select the last known successful production record and
run the explicit rollback; report failure honestly if it cannot restore health.
The workflow does not claim zero downtime, cross-container transactions,
in-flight request preservation or platform backup/restore.

## Validation and pending live acceptance

Run the normal Ruff, strict mypy and infrastructure suite with Bicep 0.48.1:

```text
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m unittest discover -s tests/infrastructure -v
```

The focused `test_release.py` covers branch/evidence bypass, artifact integrity,
production gates, source/tree binding, pinned identity/version checks, build
failure ordering, cross-contract/target rollback rejection and restoration after
a failed rollback smoke.

**Live acceptance is pending** until this feature is reviewed into dev, two
artifact-producing releases exist, the dev rehearsal and canary comparison pass,
and the owner configures the actual repository/environment gates. Keep issue #60
open through the team's reviewed dev -> main completion policy; feature -> dev
merge is not automatic closure.
