# PR quality checks

Issue [ASC-05 / #4](https://github.com/AgamMakkar21/ai-sous-chef/issues/4).
`.github/workflows/ci.yml` validates every PR targeting `dev` or `main`, including
drafts and new commits. There are no path filters that could leave required checks
pending. GitHub tests the PR merge result, not just the feature branch.

## Checks and local equivalents

Use Python 3.12 and the locked installation commands in the README. Each job
installs `requirements-dev.txt` and the separate `worker/requirements.txt`,
installs the application without resolving more dependencies, and runs
`python -m pip check`. Worker dependencies support host acceptance/type checks;
they are not added to the web application's deployment package.

| Required check name | Commands |
| --- | --- |
| `CI / lint` | `python -m ruff format --check .` and `python -m ruff check .` |
| `CI / types` | `python -m mypy` |
| `CI / tests` | `python -m pytest` (unit and API/contract tests) |
| `CI / build` | Build sdist/wheel, install the wheel, and smoke-test its health, readiness, and OpenAPI contracts |
| `CI / infrastructure` | Pinned Bicep 0.48.1 compilation and offline ARM-template tests |
| `CI / frontend` | Node 24, `npm ci`, Oxlint/Prettier, strict TypeScript, and Playwright production-bundle desktop/phone checks |

The frontend job has no Python or cloud dependency. The Python build job also
runs `npm ci` and `npm run build` in `frontend/` before packaging, so its installed
wheel contains the browser application. See [frontend setup](frontend.md) for
browser installation and local commands.

Run the package smoke check in a disposable virtual environment: it replaces the
editable installation with the wheel. In WSL/Linux:

```bash
(cd frontend && npm ci && npm run build)
python -m build --no-isolation
python -m pip install --no-deps --force-reinstall dist/*.whl
python -I scripts/smoke_wheel.py
```

`-I` excludes checkout imports; the script additionally rejects an import outside
the Python environment so a broken wheel cannot pass by using source files.
Restore the editable install afterward with
`python -m pip install --no-build-isolation --no-deps -e .`.
For infrastructure checks, install Bicep 0.48.1 and run
`python -m unittest discover -s tests/infrastructure -v`.

Tests use synthetic fixtures and explicitly selected local doubles. They do not
download the recipe corpus, use user data, authenticate to Azure, or deploy
resources. Dependency installation and downloading the Bicep compiler require
network access; test execution does not require cloud access.

## Execution and security

Jobs have ten-minute timeouts, run independently, and all report even if one
fails. New runs cancel superseded runs on the same PR. Python dependency downloads
are cached by the manifests/lockfiles; environments and test results are not
cached. Action revisions are pinned to commit SHAs.

The workflow uses `pull_request`, never `pull_request_target`, a read-only
`contents` permission, and checkout without persisted credentials. It references
no secrets, environments, deployment identities, or self-hosted runners. Do not
add cloud credentials to validation jobs or run untrusted PR code in a privileged
deployment workflow. [Reviewed dev deployment](dev-delivery.md) runs separately
on `dev` pushes, verifies a current peer-reviewed merge, and acquires OIDC
credentials only in its environment-bound deployment job.

## Owner: enable required checks after a successful run

Workflow files do not enforce merges by themselves. An administrator must wire
the checks into GitHub settings after GitHub has reported these exact names.

1. Open a feature PR into `dev` and confirm all six named checks pass. In a
   temporary commit, introduce an unused Python import and a failing fixture
   assertion; confirm `CI / lint` and `CI / tests` fail respectively. Remove
   these deliberate failures, rerun successfully, and request fresh peer review.
2. In **Settings -> Rules -> Rulesets**, inspect the existing
   `PR-approval-main-dev` ruleset rather than creating overlapping rules.
   Target both `refs/heads/dev` and `refs/heads/main`.
3. Require a PR, at least one approving review from another collaborator, dismiss
   stale approvals after new commits, and require approval of the most recent
   reviewable push. Block deletion and force pushes; do not grant routine bypass.
4. Add all six exact check names above as required status checks, select
   **GitHub Actions** as their source, and require branches to be up to date.
   Enable the ruleset only after the checks exist. If repository-plan restrictions
   prevent enforcement, record the actual limitation; documentation is not
   enforcement.
   The existing recorded rules below contain the original five checks;
   `CI / frontend` needs an owner update after its first successful run.
5. Verify a PR with failed checks or no peer approval cannot merge, and that a new
   commit invalidates its prior approval. Verify the same policy targets `main`
   when promoting `dev`. Do not self-approve or merge a test PR to bypass policy.

Feature/fix branches originate from `dev`; promote `dev` to `main` through a
separately reviewed PR. Reference #4 without an auto-closing keyword on the
feature-to-dev PR. Keep the issue open until the team's release/completion
criteria are met. Board transitions are Backlog -> Ready after prerequisites,
then In progress -> In review; a green run alone is not completion.

## Recorded workflow acceptance

The [initial clean PR run](https://github.com/AgamMakkar21/ai-sous-chef/actions/runs/38296256982)
passed all five checks. A
[temporary failure-probe run](https://github.com/AgamMakkar21/ai-sous-chef/actions/runs/38296357949)
failed `CI / lint` for an unused import (F401) and `CI / tests` for the deliberate
fixture assertion; the other three checks passed. The probe was then removed.
This verifies workflow rejection, not merge enforcement; the administrator
steps above describe the enforcement setup.

After removing the probe and updating to Node 24 Actions, the
[final clean run](https://github.com/AgamMakkar21/ai-sous-chef/actions/runs/38296521071)
passed all five checks. On 2026-10-12, the owner authorized activating
[`PR-approval-main-dev`](https://github.com/AgamMakkar21/ai-sous-chef/rules/24673928).
GitHub's effective-rules API confirmed both branches require all five checks
from GitHub Actions, up-to-date branches, one peer approval, dismissal of stale
approvals, and approval of the most recent push. Deletion/force pushes are blocked
and the bypass list is empty. No peer approval was fabricated: the live
approve-then-push exercise still requires the other collaborator.
