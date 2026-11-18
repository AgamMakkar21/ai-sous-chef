# Authenticated kitchen UI (ASC-19, ASC-20 and ASC-39)

Issue [#20](https://github.com/AgamMakkar21/ai-sous-chef/issues/20) adds the
React/TypeScript/Vite browser shell, first-run onboarding and explicit-profile
editing. The persistent tabs are **Groceries**, **Sous Chef**, and **Recipe History**.
Issue [#19](https://github.com/AgamMakkar21/ai-sous-chef/issues/19) implements
the grocery panel using the existing synchronous inventory APIs.
ASC-39 / [#41](https://github.com/AgamMakkar21/ai-sous-chef/issues/41) adds conversation
navigation to Sous Chef. ASC-40 adds sending messages and ASC-41 / [#38](https://github.com/AgamMakkar21/ai-sous-chef/issues/38)
renders structured recipe replies. ASC-45 / [#47](https://github.com/AgamMakkar21/ai-sous-chef/issues/47)
adds recipe save/cook actions, history and feedback. No sample groceries, conversations or recipes are
presented as account data.

## Recipe History and feedback (ASC-45)

**Save to Cook** stores a planned recipe; **Cook This** explicitly confirms cooking.
Both use the existing lifecycle APIs with CSRF and idempotency keys. A previously
planned recipe uses the conditional `/cooked` transition, not a second snapshot.
Recipe History shows paginated **Needs your rating** and **Saved and cooked**
lists; planned recipes never acquire a fabricated cooking date or rating.
Open a recipe to read its original stored snapshot, nutrition, provenance,
save/cook dates and notes. Pantry labels describe the saved snapshot, not current
inventory. **I've cooked this** makes a planned recipe eligible for feedback.

Feedback uses required, keyboard-accessible **Liked**, **Fine**, and **Didn't Like**
radio labels plus an optional note bounded to 2,000 Unicode characters. Saving or
editing uses the detail API's ETag and a stable idempotency key. Failures retain
the draft and require loading the saved version before another explicit save;
an uncertain transport response is never described as a confirmed failure or
success. Navigation warns before discarding feedback, and writes block shell
navigation. Success confirms persistence only, never completed learning.

Successful changes invalidate history in the current page and other same-origin
browser tabs through `BroadcastChannel`; returning focus also refetches (including
browsers without that API). Only a fixed invalidation signal crosses tabs, not
recipe content, account IDs, tokens or feedback. Open drafts are not overwritten.
Data stays on the server/in memory, and reload re-reads the backend. No new
configuration, services or AI calls are required. The existing owned history,
detail, lifecycle and feedback endpoints must be deployed together.

Run `cd frontend && npm test -- history.spec.ts recipes.spec.ts conversations.spec.ts`
for synthetic desktop/phone acceptance coverage, and `npm run lint` /
`npm run typecheck` for source checks. Fixtures are not real user history;
live acceptance still requires hosted sign-in and the configured Cosmos backend.

ASC-64 / [#59](https://github.com/AgamMakkar21/ai-sous-chef/issues/59) adds
[keyboard, responsive and accessibility acceptance checks](accessibility-acceptance.md),
including the recorded walkthrough, browser evidence and testing limitations.

## Visual design

The [supplied design guide](../AI%20SOUS%20DESIGN.md) defines the compact pixel
kitchen: **YOUR PANTRY** in Jersey 10, warm orange actions, original inline
produce sprites, one sliding navigation pill, and a full-width grocery workspace.
`frontend/src/kitchen.css` applies the design tokens and responsive presentation
over the existing component styles, including chat, history, and the full profile.
Light-mode functional accents are darkened for readable text contrast; decorative
produce and headline colors retain the reference palette.

This is a visual adaptation, not the guide's standalone demo. Authentication,
one server-saved inventory, profile safety fields, recipe workflows, and recipe
nutrition are unchanged. There are no sample kitchens, reset controls, canned
replies, or browser-stored account data. The promotional welcome text, footer,
row decorations, inspiration card and standalone ingredient-calorie form have
been removed. Recipe nutrition and its backend APIs remain unchanged.

Dark is the default. The theme button persists only `sous-chef-theme` in browser
storage and still works for the current page when storage is unavailable.
`?scoutTheme=light` and `?scoutTheme=dark` override that preference for previews.
The supplied Jersey 10 headline subset and its SIL Open Font License are bundled
inside the existing single-file build; no font CDN is contacted. The license
remains in an inert template rather than a visible footer. Rebuild the subset
before changing the headline.

### Buttons, motion and focus

The full-width Add ingredient action uses the supplied hover shine.
Its higher-contrast sheen takes 500ms to cross the wider surface, matching the
supplied button example without delaying clicks. Keyboard and reduced-motion
interactions do not animate the sweep.

`frontend/src/components/ui/cartoon-button.tsx` adapts the supplied cartoon button
to native button props, refs and form behavior. Every action, including secondary
actions, retries, pagination, profile controls, icon buttons
and tab triggers, uses this control. Button-shaped sign-in/sign-out links share its
label/highlight treatment while remaining real links. Active tabs retain their
ARIA state inside one shared rail. A single orange pill slides between the three
destinations over 200ms, with clipped visual label copies keeping text readable
throughout the slide. These copies are hidden from assistive technology; the
original Radix tab controls retain their keyboard and navigation behavior.
Keyboard and reduced-motion selection changes remain instant, and only the
navigation indicator moves, not the functional page content. `demo.tsx` contains an unmounted
example. `@/components/ui` resolves through TypeScript and Vite aliases;
`components.json` records that conventional shadcn-compatible location so shared
controls are not mixed with page components.
Chat-history entries are the deliberate exception: they remain simple selectable
rows with their original active-state highlight, not cartoon pills or shine.

The app already uses TypeScript. Tailwind v4 utilities are connected through
`@tailwindcss/vite`, without Preflight or replacement of the chosen design tokens.
Run `npm ci` in `frontend` to restore them. The component uses the current theme's
accessible action colors by default; `color` accepts a Tailwind background class.
It needs no provider, icon library, stock imagery or additional runtime dependency.
This custom control does not install stock shadcn primitives or their `cn` helper.
For a fresh project's stock setup, use `npx shadcn@latest init`, then
`npx shadcn@latest add <component>`; review generated global styles rather than
overwriting this app's theme.

Pointer feedback uses 120ms transform transitions, a 250ms highlight sweep and
200ms theme-icon/success feedback. Hover movement requires a fine hover pointer.
Keyboard-triggered actions are instant; input modality is tracked only in a DOM
attribute, never stored or sent to the server. Reduced motion removes movement
and retains only a shorter success fade. Grocery rows, chat text and functional
data never animate on entry or while updating.

Focused textboxes, number fields, selects and textareas use their own yellow
border without an outer outline or layout shift. Light mode uses a darker gold
for contrast. Buttons, tabs, checkboxes and other keyboard controls retain
visible focus outlines.

`design.spec.ts` checks the font, theme behavior, reduced motion, and both themes
at 320, 390, 680, 768, 1024, and 1440 pixels. Existing grocery, profile, recipe,
conversation, and accessibility tests continue to exercise the real UI with
synthetic API responses.

### Public sign-in page

The hosted App Service `/auth/login` page uses the same pixel-kitchen palette,
Jersey 10 headline, and appearance preference. A centered card stacks equal-width
Microsoft, Google, and GitHub links with inline provider logos. A decorative
pixel frying pan flips an egg below the card once over four seconds; reduced
motion disables the animation. All sign-in links work without JavaScript.
Provider links and the theme toggle match the app's cartoon-pill borders, hard
shadows, pointer press/lift and shine. The independently served HTML mirrors these
small CSS rules rather than importing the protected React app; provider URLs,
script-free navigation and the dynamically hashed CSP remain unchanged.

The backend renders `backend/src/sous_chef/templates/login.html` with a packaged
copy of the existing font and its full license. These resources intentionally
live outside the generated `static/` directory: the login page cannot depend on
the protected app bundle, and Vite clears that directory when building. No
external fonts, logos, scripts, or services are loaded. Its Content Security
Policy authorizes only the exact inline style/script hashes and embedded font,
while retaining the default-deny, framing, base-URL, and form restrictions.

Authentication behavior is unchanged: the same three fixed provider routes lead
to Easy Auth, query parameters (including theme previews) remain rejected, and
local/non-App-Service modes keep their existing responses. Use the theme button
or saved `sous-chef-theme` preference to preview this page. `login.spec.ts` covers
keyboard access, both themes at the design breakpoints, contrast, reduced motion,
storage failure, and script-free links. Backend integration tests check the
actual response, exact CSP hashes, packaged font, and authentication boundaries.

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
Edited quantities autosave after 600ms without further typing; Enter saves
pending valid edits immediately. There is no separate Save quantity button.
Unchanged and invalid values are not written. Invalid input has an associated
error, and clearing a quantity saves it as unknown.
Pending changes block navigation and other pantry actions. Multiple edited rows
save sequentially with a fresh collection ETag after each confirmed write.
The active field remains focused and becomes read-only during a write.
A failure discards unsent edits, restores confirmed values and requires the
existing explicit reload/review; no queued update is replayed automatically.
Blank quantity means unknown, not zero or one; +/- is disabled until a quantity
is explicitly supplied. Decrementing to zero or below asks for removal, and
cancelling performs no write. Units are never converted or merged.

The unit menu includes US fluid ounces (`fl oz`), quarts (`qt`), gallons (`gal`)
and `dozen` alongside the existing options. `oz` remains a weight unit; `dozen`
means 12 items. These units are also accepted in recipe amounts and preserved on
reload. Nutrition conversion still requires a food-specific source portion.

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
store or a model call. New Chat opens a local draft; no server conversation is
created until its first valid request is sent. Empty drafts are discarded on
leaving or reloading, and saved lists exclude chats without accepted turns.
First-send creation has a stable idempotency key and uses a freshly verified
session and CSRF token. An uncertain result or CSRF failure never automatically
replays the write: **Check this request** explicitly repeats the same creation
key/body before sending. The backend receipt resolves a committed creation to the
original opaque ID, which is remembered before submitting the first request.

Select a saved chat and choose **Delete chat** to confirm deletion. The action
clears selected navigation and its remembered operation, refreshes the list and
keeps saved Recipe History entries. Failures remain explicit and can be retried;
active or unresolved work must finish or be closed before deletion. Chat deletion
uses the [soft-deletion and retention contract](conversations.md#chat-lifecycle).

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

Hosted use still requires the live Cosmos/authentication setup and shared
`ASC_PAGINATION_KEY` described in [conversation setup](conversations.md#setup).
Local readiness doubles do not fabricate conversations or history.

## Chat composition and recovery (ASC-40)

Select a chat to send a recipe request. Enter submits, Shift+Enter adds a line,
and input-method composition does not submit. The client trims text and enforces
the API's 1-4,000 Unicode-code-point limit. A synchronous guard prevents duplicate
submissions. Each deliberate new send has a UUID idempotency key and a freshly
verified same-account session/CSRF token.

The send request has a 75-second browser deadline to accommodate the server's
60-second total deadline. Other API requests retain their 15-second deadline.
The composer labels in-flight work accessibly and keeps its input readable.
Navigation and competing mutations are disabled while a foreground request is
being sent or checked. There is no remote cancellation control: closing,
refreshing or aborting a browser request does not prove execution stopped.

Only validated, owned complete-turn responses are displayed. Failed, pending and
unknown turns stay user-only; no partial assistant text, recipe draft, raw
exception, provider response or injected HTML is rendered. Up to 20 recently updated turns can appear beside the
bounded history page, deduplicated by turn ID. Sending does not scan lifetime
history, and successive sends do not replace earlier visible requests.

Failures show **I couldn't complete that recipe request** with explicit recovery:

- **Check this request** reconciles an ambiguous send using its original body
  and key. It is not a new generation attempt. CSRF renewal never auto-replays.
- **Check saved status** reads a known owned operation. Pending operations are
  polled with GET every two seconds, stopping on a terminal result, failed read,
  view departure or the local polling budget. Operation links must be exact
  relative paths for the selected conversation; arbitrary server URLs are not followed.
- **Retry saved request** is available only for a known failed turn. It sends a
  new key with `retryOfTurnId` and the original unchanged text, preserving the
  saved turn and user-message identities. Editing the input instead starts a new turn.
- An unknown outcome blocks new sends and retries for that chat. A local timeout
  never invents a failed/cancelled result or releases the server's concurrency guard.
  Concurrent-send conflicts preserve input and reload bounded saved history for review.

Session storage keeps at most 20 opaque conversation/operation-ID pairs under the
hashed owner scope, in addition to the existing navigation metadata. It stores no
message text, idempotency payload, profile or token. Reloading a remembered
operation only reads its status; it never sends a mutation. Without an operation
reference, history still shows persisted pending/failed/unknown state and offers
explicit history refresh or a known-failure retry, not a guessed operation or
an automatic generation.

Drafts and unresolved request bodies/keys stay in memory. Navigation warns before
discarding them, and ordinary validation, transport, timeout and service failures
retain the input. **Session expiry or account change intentionally clears private
drafts**, following the existing privacy policy; requests already admitted remain
in server history after sign-in. A reload before an operation URL is received can
lose the in-memory reconciliation body/key. The UI shows the persisted turn
truthfully but cannot reconstruct an unknown admission or promise remote recovery.

No new environment settings are required. Real recipe generation still needs
the live executor, reviewed agents and FoodData Central setup described in
[conversation setup](conversations.md#setup); this UI does not enable them or
substitute a demo recipe when a dependency fails.

## Structured recipe cards (ASC-41)

Completed turns with a structured recipe and matching generated-recipe UUID render
the card instead of the assistant's free-text summary, both after sending/polling
and in reloaded, paginated history. The client checks the versioned display
contract, bounds, ingredient/shopping/step/source references and nutrition states.
Malformed or incomplete payloads use the existing history/composer error recovery;
pending, failed and unknown turns cannot carry a renderable candidate. These
checks do not replace backend ownership, deterministic safety or current-profile
revalidation.

Cards show time, skill, servings, concise fit reasons, ordered steps and ingredient
quantities. One ingredient list preserves recipe order and uses
**quantity - ingredient name - availability**. **Already have** means confirmed
inventory, **Need to buy** means a purchase is required, and unknown availability
says **Check your pantry**. The quantity is the full recipe amount;
when `shoppingItems` specifies a different purchase shortfall, a concise
**Buy quantity** note preserves that distinction without separate pantry/shopping sections.
Missing quantities/units are explicitly unavailable, never inferred.

Nutrition preserves its supplied per-serving/whole-recipe basis and real zeroes.
All six supported nutrients remain visible: absent values say **Unavailable**,
partial amounts are labelled **Partial**, and detailed coverage names unknown
ingredients when supplied. Legacy partial evidence without detailed coverage is
conservatively labelled partial. A collapsed **Nutrition details** disclosure holds
one whole-recipe table: ingredient, calories, protein, carbohydrates, fat, fiber
and sodium. Each ingredient appears once, with its source linked from its name;
source labels appear once per source beneath the ingredient, including both USDA
and AI/web labels for mixed-source rows. Nutrient cells contain values, not repeated
source labels. Missing ingredient values remain
**Unavailable**, including legacy snapshots without per-ingredient amounts.
Estimated matches and quantities retain concise labels and quantity-source links.
The table scrolls horizontally within a keyboard-focusable region on narrow screens.
Calculation prose, conversion formulas and a redundant nutrition-source list are
not rendered; the original evidence, full-precision values and source versions
remain unchanged in the validated/stored payload. The informational-estimate
disclaimer remains visible inside the disclosure. Exact grounded recipe source quotes,
creative adaptations/substitutions and additional Web IQ context have distinct
sections; citations do not imply that creative changes are source-verified.

These are presentation-only changes shared by chat and Recipe History: generation,
nutrition calculations, retries, persistence and response validation are unchanged.
Recipes still appear only after a completed, validated response.

Model/source text is escaped React text, not HTML or Markdown. Citation links
accept only credential-free HTTPS on the default port, open with no opener or
referrer, and label new-tab behavior. Other references display **link unavailable**.
The card inherits shell theme tokens, wraps long text, uses semantic headings and
lists, and introduces no entrance animations to delay reading or keyboard use.

**Cook This** and **Save to Cook** are deliberately disabled native buttons with
an accessible explanation. Card rendering never sends a lifecycle mutation or
claims a save/cooked result. Wiring the existing ASC-42 API and verifying it in
ASC-45/ASC-61 remains separate work; any later action must use the owned
generated-recipe ID, not trust a browser-supplied recipe or old safety decision.
No new environment variables, packages or backend setup are required.

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
Composer cases cover keyboard/double-click submission, Unicode limits, slow and
successful replies, same-turn explicit retries, lost-response/deadline
reconciliation, pending polling/reload, unknown-outcome fences, malformed/foreign
responses, operation-link rejection, draft navigation and session expiry.
API responses use synthetic test-only route doubles. These checks do not claim
live OAuth, live Cosmos or deployed-browser verification.

`tests/recipes.spec.ts` reuses the backend specialist contract fixture and covers
all card fields, purchase shortfalls, unknown inventory/quantities, partial and
unavailable nutrition, actual zero values, grounded versus creative content,
live/polled/reloaded rendering, disabled actions, malformed/failed candidates,
escaped text, inert unsafe links, keyboard link activation and 320-pixel wrapping
in both desktop and phone Chromium projects.

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
