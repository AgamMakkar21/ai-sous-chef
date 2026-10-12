# PR quality checks

Issue [ASC-05 / #4](https://github.com/AgamMakkar21/ai-sous-chef/issues/4).
`.github/workflows/ci.yml` validates every PR targeting `dev` or `main`, including
drafts and new commits. There are no path filters that could leave required checks
pending. GitHub tests the PR merge result, not just the feature branch.

## Checks and local equivalents

Use Python 3.12 and the locked installation commands in the README. Each job
installs `requirements-dev.txt`, installs the application without resolving more
dependencies, and runs `python -m pip check`.

| Required check name | Commands |
| --- | --- |
| `CI / lint` | `python -m ruff format --check .` and `python -m ruff check .` |
| `CI / types` | `python -m mypy` |
| `CI / tests` | `python -m pytest` (unit and API/contract tests) |
| `CI / build` | Build sdist/wheel, install the wheel, and smoke-test its health, readiness, and OpenAPI contracts |
| `CI / infrastructure` | Pinned Bicep 0.48.1 compilation and offline ARM-template tests |

Run the package smoke check in a disposable virtual environment: it replaces the
editable installation with the wheel. In WSL/Linux:

```bash
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
deployment workflow. Deployment remains a separate issue.

## Owner: enable required checks after a successful run

Workflow files do not enforce merges by themselves. An administrator must wire
the checks into GitHub settings after GitHub has reported these exact names.

1. Open a feature PR into `dev` and confirm all five named checks pass. In a
   temporary commit, introduce an unused Python import and a failing fixture
   assertion; confirm `CI / lint` and `CI / tests` fail respectively. Remove
   these deliberate failures, rerun successfully, and request fresh peer review.
2. In **Settings -> Rules -> Rulesets**, inspect the existing
   `PR-approval-main-dev` ruleset rather than creating overlapping rules.
   Target both `refs/heads/dev` and `refs/heads/main`.
3. Require a PR, at least one approving review from another collaborator, dismiss
   stale approvals after new commits, and require approval of the most recent
   reviewable push. Block deletion and force pushes; do not grant routine bypass.
4. Add all five exact check names above as required status checks, select
   **GitHub Actions** as their source, and require branches to be up to date.
   Enable the ruleset only after the checks exist. If repository-plan restrictions
   prevent enforcement, record the actual limitation; documentation is not
   enforcement.
5. Verify a PR with failed checks or no peer approval cannot merge, and that a new
   commit invalidates its prior approval. Verify the same policy targets `main`
   when promoting `dev`. Do not self-approve or merge a test PR to bypass policy.

Feature/fix branches originate from `dev`; promote `dev` to `main` through a
separately reviewed PR. Reference #4 without an auto-closing keyword on the
feature-to-dev PR. Keep the issue open until the team's release/completion
criteria are met. Board transitions are Backlog -> Ready after prerequisites,
then In progress -> In review; a green run alone is not completion.
