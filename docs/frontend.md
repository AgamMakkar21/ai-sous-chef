# Authenticated kitchen UI (ASC-19, ASC-20 and ASC-39)

Issue [#20](https://github.com/AgamMakkar21/ai-sous-chef/issues/20) adds the
React/TypeScript/Vite browser shell, first-run onboarding and explicit-profile
editing. The persistent tabs are **Groceries**, **Sous Chef**, and **Recipe History**.
Issue [#19](https://github.com/AgamMakkar21/ai-sous-chef/issues/19) implements
the grocery panel using the existing synchronous inventory APIs.
ASC-39 / [#41](https://github.com/AgamMakkar21/ai-sous-chef/issues/41) adds conversation
navigation to Sous Chef. Sending messages and recipe-history
screens remain separate work. No sample groceries, conversations or recipes are
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
analytics or URLs. Fixed tab hashes persist top-level navigation. User text is rendered
through escaped React text/controls, never injected HTML. Sign-out and navigation
warn before discarding an edited draft.

## Grocery inventory

The Groceries tab reads the full bounded collection with its ETag. Quick entry
accepts an ingredient name and optional quantity/unit. Each saved row offers
keyboard-accessible quantity editing, +/- controls and confirmed deletion.
Blank quantity means unknown, not zero or one; +/- is disabled until a quantity
is explicitly supplied. Decrementing to zero or below asks for removal, and
cancelling performs no write. Units are never converted or merged.

Writes use `If-Match`, the in-memory CSRF token and the authenticated backend
owner; the UI cannot select another user or access Cosmos directly. All controls
and shell navigation are disabled during a write/reconciliation, with a
synchronous single-flight guard against duplicate submissions. Quantities remain
at their last confirmed values until the write and a fresh collection read
succeed. This intentionally avoids optimistic rollback races.

Loading, a confirmed empty list, pending updates and failures have separate
accessible states. A failed write displays **Unable to update your groceries**,
restores quantity inputs to confirmed values, retains quick-entry text and blocks
mutations. **Retry** renews the session and reloads persisted inventory, never
replays a write. The user reviews the refreshed quantities before explicitly
submitting another change, including after a stale revision, CSRF failure or an
ambiguous/committed-but-lost response. Last-confirmed values are labelled as
potentially stale while recovery is required. Failed reads and malformed or
revisionless responses never become an empty-list success. Session expiry or
identity change clears the grocery view through the existing sign-in flow.

No additional environment settings, services or AI dependencies are required;
reuse [grocery API setup](groceries.md). Grocery data and drafts stay in memory.
The layout wraps long names/labels and stacks quick entry on narrow screens.

## Conversation navigation (ASC-39)

The Sous Chef tab uses the existing owned conversation APIs, not a second history
store or a model call. New Chat creates an empty conversation only on explicit
action. Each creation has a fresh idempotency key and uses a freshly verified
session and CSRF token. An uncertain result or CSRF failure never automatically
replays the write: **Check chat creation** explicitly repeats the same key/body.
The backend receipt resolves a committed creation to the original opaque ID.

Session storage retains only the selected opaque conversation UUID, any unresolved
creation key and a SHA-256 owner scope. It contains no messages, titles, user IDs,
profiles or tokens. Reloading or switching tabs restores selection, not cached
content. A different signed-in user discards the old navigation record; every
read rechecks the session and lets the backend enforce ownership. A 401 or changed
identity clears the displayed conversation with the rest of the private UI.
Unavailable/corrupt browser storage produces a visible notice rather than a false
promise of reload persistence.

Conversation lists and transcripts request 20 records per page. Earlier/next
controls fetch only the requested page, preserve server ordering and discard the
previous page's content; there is no lifetime-history prefetch. Refresh restarts
the server snapshot. Expired history cursors can restart from page one. A selected
conversation outside the current list page remains highlighted separately, without
scanning every list page to locate it.

Existing metadata titles are rendered as escaped text. A generic title can use
the selected chat's first loaded user message, shortened to 80 Unicode code points
with control/format characters removed. This display-only title never inherits
another chat's transcript, changes stored metadata or invokes a model. Empty new
chats use **New chat**. Transcript messages are plain React text, not injected
HTML or executable Markdown. Pending, failed and unknown outcomes stay distinct
from completed assistant replies.

The sidebar collapses on phones and supports native keyboard buttons, selection
highlighting and focus on the selected transcript. Loading, empty, unavailable
and failed reads have separate visible states. No raw service error is displayed.
Sending/retrying messages and structured recipe rendering belong to ASC-40 and
later work; there is no active message composer in this slice.

Hosted use still requires the live Cosmos/authentication setup and shared
`ASC_PAGINATION_KEY` described in [conversation setup](conversations.md#setup).
Local readiness doubles do not fabricate conversations or history.

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
Conversation cases also cover independent creation, reload/tab restoration,
bounded pages, context-derived titles, malicious markup, interrupted creation,
CSRF, cross-account isolation and late responses after switching chats.
API responses use synthetic test-only route doubles. These checks do not claim
live OAuth, live Cosmos or deployed-browser verification.

`tests/groceries.spec.ts` additionally covers keyboard CRUD/reload persistence,
zero-removal cancellation/confirmation, unknown and fractional quantities, limits,
single-flight clicks, concurrent conflicts, failed writes, retry reconciliation,
lost responses, expired sessions and 320-pixel layouts on desktop/phone Chromium.
The dependency lock uses available PostCSS 8.5.31 (within Vite's declared range)
and oxlint 1.89.0; the previous PostCSS 8.5.32/oxlint 1.90.0 entries returned
package-not-found during clean installation.

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
