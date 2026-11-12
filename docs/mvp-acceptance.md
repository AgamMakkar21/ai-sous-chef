# MVP acceptance and dev handoff (ASC-61 / #64)

The reviewed dev app and feedback host are published, but **live MVP acceptance
is not complete**. The worker lacks its required extraction settings, and no
signed-in collaborator journey was performed. Automated fixtures are not proof
that live agents, tools or personalization work.

## Observed release

Read-only checks on **2026-11-12** found:

| Component | Observed value |
| --- | --- |
| Dev app | <https://ai-sous-chef-dev-web-b5r36p.azurewebsites.net> |
| App and worker commit | `68b1df3825a4bd942a4d80a60e9f1ddf9f4ffadf` |
| Reviewed deployment | [Actions run 43300824195](https://github.com/AgamMakkar21/ai-sous-chef/actions/runs/43300824195), succeeded |
| Web checks | `/health` and `/ready`: HTTP 200 with that `X-ASC-Commit`; anonymous `/api/v1/me`: HTTP 401 |
| Feedback host | `ai-sous-chef-dev-feedback-b5r36p`; `/api/health`: HTTP 200 with that commit and `scope=host-only` |
| Authentication | `app_service`; a legitimate provider sign-in is required |
| Orchestrator | `sous-chef-orchestrator-dev`, version **24** |
| Recipe + Nutrition | `sous-chef-recipe-nutrition-dev`, version **29**, `combined` mode |
| Extraction deployment/model | **Not configured**; no live extraction version can be reported |

These are observed runtime pins, not the source-agent versions in
`deploy/agents.dev.json`. A subsequent reviewed deployment can supersede them.
This acceptance PR is not part of the observed deployment above.

The Function has `AZURE_CLIENT_ID`, but all three required settings below were
absent. Its health endpoint deliberately does not validate them:

| Missing setting | Owner-approved value required |
| --- | --- |
| `ASC_FOUNDRY_PROJECT_ENDPOINT` | Existing New Foundry project HTTPS endpoint |
| `ASC_WORKER_EXTRACTION_DEPLOYMENT` | Explicit existing GPT-5.1 nano deployment name |
| `ASC_WORKER_EXTRACTION_MODEL` | `gpt-5.1-nano` or the exact expected dated model ID |

Follow [extraction configuration](preference-extraction.md#configuration-and-identity)
and [worker permissions](feedback-host.md#identity-storage-and-data-permissions).
The worker's separate managed identity also needs the required project-scoped
invocation access; that permission was **not verified** here. Do not copy the
legacy web extraction pin, substitute GPT-5.4 nano, share identities, broaden
roles or enable public networking to make the check pass. No cloud configuration,
permissions, leases or application data were changed during these checks.

## Automated acceptance

`tests/integration/test_mvp_acceptance.py` runs the real authenticated application,
Cosmos/Foundry SDKs, grounding/safety services, feedback batch processor, extraction
adapter and default learned-preference reader over synthetic remote transports:

- Onboard, change inventory, create two independent chats, generate grounded
  recipes, save, explicitly cook and persist ratings/notes before worker dispatch.
- Restart the app and recover the same groceries, conversations, immutable recipe
  snapshots and feedback.
- Deliver both feedback revisions twice and poll durable recovery state every
  50 ms with a 15-second deadline; two extraction calls produce two supporters,
  confidence 0.75 and no duplicate evidence.
- Capture the later Orchestrator request consuming the learned insight; a new
  explicit dislike suppresses that insight without deleting its durable evidence.
- Exercise justified native web grounding versus an ordinary pantry request,
  foreign-user reads, deterministic allergen refusal and a failed save that
  creates no saved recipe.

`frontend/tests/mvp.spec.ts` drives the production UI bundle at desktop and phone
sizes: synthetic sign-in, onboarding, empty inventory, grocery add/change, new
chat, recipe save/cook, rating/note, reload and reopening all three tabs. Its
sign-in wrapper is stubbed; it does not test a real provider login. Learning and
tool execution are proved by the API composition test, not invented by the UI
mock. Existing grocery, conversation, history and accessibility suites cover
loading/error states, keyboard navigation and responsive behavior.

Use the [local setup](../README.md#local-backend), Python 3.12 and Node 24.
Restore locked dependencies in an isolated environment before the first run:

```powershell
python -m pip install -r requirements-dev.txt
python -m pip install --no-build-isolation --no-deps -e .
python -m scripts.evaluate_safety
python -m pytest tests\integration\test_mvp_acceptance.py tests\integration\test_recipe_chat.py tests\integration\test_web_grounding.py tests\unit\test_authorization_matrix.py tests\unit\test_learning_recovery.py tests\unit\test_feedback.py tests\unit\test_recipe_lifecycle.py
python -m ruff check .
python -m ruff format --check .
python -m mypy
Set-Location frontend
npm ci
npx playwright install chromium
npm run lint
npm run typecheck
npm test -- mvp.spec.ts
```

`npm test` runs the full desktop/phone suite. The browser runner builds and serves
the production bundle; reserve port 4173 rather than reusing an unrelated server.
CI uses the existing version-matched Playwright container. No new test framework,
public endpoint, runtime bypass or production promotion is required.

## Remaining live acceptance gate

After an owner configures the approved extraction pins/access, use a dedicated
dev test account and the reviewed deployment workflow. Do not overwrite another
person's profile or inventory, forge Easy Auth headers, log cookies/notes, or
delete leases to force processing.

1. Recheck the reviewed deployment, exact app/worker commit and runtime agent
   versions. Sign in through the configured provider and verify `/api/v1/me`
   loads the owned profile; anonymous 401 and health alone are insufficient.
2. Complete onboarding with explicit constraints, change groceries, create a
   chat and request an ordinary grounded pantry dinner. Privately verify real
   curated/USDA provenance and no unnecessary web search.
3. Save, explicitly cook and rate two independent recipe occasions with a
   consistent synthetic ingredient preference. Reload all three tabs and verify
   the original snapshots, inventory and feedback persist.
4. An authorized operator must inspect each feedback revision from an approved
   network using `python -m worker.replay --inspect` and its documented owner,
   feedback ID, revision and Cosmos endpoint arguments. Poll at most every
   10 seconds for five minutes. Require durable `completed`, not merely a saved
   rating or healthy host; stop and report pending/retry/failed rather than
   declaring success. Do not replay a successful event or exceed its retry budget.
5. Verify the durable application receipts/evidence privately, then make a later
   recipe request. Confirm learned-context consumption and explicit-constraint
   precedence without enabling content capture. Model wording alone is not
   evidence that context was loaded.
6. Make a justified public current-information request and verify real native
   Web IQ provenance. Record only safe statuses, counts, versions and trace IDs
   in the handoff; keep user data and model payloads out of public evidence.

Provider sign-in, live grounding/Web IQ, worker invocation/learning and actual
next-request personalization remain **unverified**, not waived. Leave #64 open
until those gates and peer review are complete. Production rollout, comprehensive
quality evaluations and observability dashboards remain outside this issue.
