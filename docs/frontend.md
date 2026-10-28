# Authenticated kitchen shell (ASC-19)

Issue [#20](https://github.com/AgamMakkar21/ai-sous-chef/issues/20) adds the
React/TypeScript/Vite browser shell, first-run onboarding and explicit-profile
editing. The persistent tabs are **Groceries**, **Sous Chef**, and **Recipe History**.
Their panels describe upcoming work: grocery CRUD controls, chat and recipe-history
contents are not implemented by this slice. No sample groceries or recipes are
presented as account data.

## Build and run

Use Node.js **24 LTS** and npm in the same operating system as `node_modules`.
Keep Windows and WSL dependency directories separate. From the repository root
in the [recommended WSL checkout](../README.md#local-backend):

```bash
cd frontend
npm ci
npm run build
cd ..
```

The build produces one self-contained
`backend/src/sous_chef/static/index.html`; it bundles React, the Radix tabs
primitive, styles and theme initialization without runtime CDN requests.
Run the backend using the README's commands. The backend serves the bundle at
its own authenticated root, with `Cache-Control: no-store` and
`Referrer-Policy: no-referrer`. `ASC_STATIC_DIR` can override the serving directory.

**Start hosted sign-in at `/auth/login`, not at an anonymous `/`.** App Service
protects the root and static assets before Python runs. The UI never makes them
public. The existing provider selector supports Google, GitHub and single-tenant
Microsoft when configured by the owner.

Local External ID mode accepts explicitly signed bearer tokens for API testing;
it does not emulate hosted cookie sign-in. Local `/auth/login` therefore returns
hosted-login guidance, not a bypass. Readiness doubles do not provide Cosmos
profiles. Vite's development/preview servers are development tools, not an
alternative authenticated production origin. Use the reviewed hosted deployment
for an actual cookie-authenticated account flow.

## Profile and privacy behavior

The UI loads `/auth/session`, then `/api/v1/me`. The backend's
`onboardingCompleted` flag controls onboarding; browser storage cannot unlock it.
Returning completed users go straight to the shell. Profile editing covers all
nine [explicit preference fields](profiles.md), including limits, enums and
integer time budgets. Empty safety lists require explicit review instead of
silently assuming no allergies. Nutrition preferences are informational, not
medical instructions.

Onboarding sends a complete `PUT`; subsequent edits send only changed fields
with `PATCH`. Both include the current `If-Match` revision and in-memory CSRF
token. Backend success updates the shell. A reload reads the persisted profile
again. Profile CRUD does not invoke AI.

Validation errors retain the visible draft and identify fields without exposing
provider messages. A stale revision or uncertain save blocks retry until the
user loads the saved profile, compares it with the unchanged draft, and explicitly
acknowledges the comparison. Retry uses that reviewed revision and only edited
fields. CSRF renewal never automatically replays a mutation.

Any 401, including an HTML platform response, clears displayed private details
and unsaved drafts before offering `/auth/login`. A changed session/profile user
does the same. A malformed response or missing ETag fails visibly rather than
inventing a profile or claiming a confirmed save.

Profiles, drafts and CSRF tokens stay in memory, not local/session storage,
analytics or URLs. Only fixed tab hashes persist navigation. User text is rendered
through escaped React text/controls, never injected HTML. Sign-out and navigation
warn before discarding an edited draft.

## Checks and packaging

```bash
cd frontend
npm ci
npx playwright install --with-deps chromium
npm run lint
npm run typecheck
npm test
```

Playwright builds the actual production bundle and owns a temporary preview
server. Desktop and phone contexts cover onboarding, returning-user edits,
persistence across reload, validation, concurrency, uncertain writes, CSRF,
identity/session expiry, keyboard tabs, escaping, themes and narrow layouts.
API responses use synthetic test-only route doubles. These checks do not claim
live OAuth, live Cosmos or deployed-browser verification.

The installed-wheel build first compiles the frontend and includes the ignored
bundle through Hatch build artifacts. The wheel smoke check rejects a missing
frontend. Dev delivery builds from the reviewed checkout **before Azure login**,
then adds only the compiled HTML to the exact-commit backend source ZIP. Packaging
rejects a missing frontend build. No credentials, source maps, `node_modules` or
test fixtures enter the browser bundle.

`CI / frontend` is added without removing the five existing Python/infrastructure
checks. An administrator must add the new status to the existing required-check
ruleset after its first successful GitHub run; this branch does not change live
repository settings.
