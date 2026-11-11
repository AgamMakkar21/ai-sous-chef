# Browser accessibility acceptance (ASC-64)

The bounded three-tab flow passes keyboard, responsive and automated accessibility
checks in Chromium. This record covers synthetic browser acceptance, not live
account acceptance or a claim of full Web Content Accessibility Guidelines
(WCAG) conformance.

## Reproduce

Use Node.js 24 LTS. From the repository root:

```text
cd frontend
npm ci
npx playwright install chromium
npm run lint
npm run typecheck
npm test -- accessibility.spec.ts
npm test
```

Playwright builds the production bundle and serves an isolated preview on port 4173. Tests intercept APIs with synthetic fixtures; they require no account,
Cosmos instance, model call or deployment. Existing component tests remain in the
full suite.

## Recorded results

| Check                            | Result                                                                                                                                                                 |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Frontend lint and formatting     | Passed                                                                                                                                                                 |
| TypeScript and production build  | Passed                                                                                                                                                                 |
| Full desktop/phone browser suite | 248 passed, including 14 new acceptance tests                                                                                                                          |
| Tagged axe scans                 | 56 scans; no reported violations or incomplete checks                                                                                                                  |
| Horizontal reflow                | No root overflow in any scanned state                                                                                                                                  |
| Visual inspection                | Copilot inspected representative long-chat and feedback screenshots at desktop and 320px widths, across both themes; text wraps and actions remain in their containers |

The screen audits run at 1365 x 900 and 320 x 844 in light and dark themes.
Loading/failure scans also run at the normal phone size, 390 x 844.
Keyboard journeys run at 1365 x 900 and 390 x 844. Phone results use Chromium
mobile emulation, not a physical device.

Each screen scan uses axe's WCAG 2 A/AA, 2.1 A/AA and 2.2 AA tags. The gate fails
on any critical or serious violation, without excluded elements or disabled
rules. Reports retain all violations and incomplete checks, not just gated
findings.

## Recorded keyboard walkthrough

The acceptance test uses Tab/Shift+Tab, arrows, Space, Enter and the browser
keyboard text API. It does not click or programmatically focus controls in the
end-to-end journey.

| Step                                                  | Verified outcome                                                                                              |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Complete onboarding with Tab, arrows, Space and Enter | Profile persists and focus moves into the kitchen                                                             |
| Add groceries, then Tab and Shift+Tab                 | Ingredient persists; reverse navigation works and the field has a visible 3px focus outline                   |
| Enter the tablist with Tab and switch with arrows     | All three tabs are reachable using the roving-tab keyboard pattern                                            |
| Create a chat, enter a request and press Enter        | Completed recipe and save controls become available                                                           |
| Save to Cook, open history and confirm cooking        | Saved recipe transitions to cooked; detail heading receives focus                                             |
| Use the rating radios with Space and arrows           | Liked, Fine and Didn't Like remain explicit text choices                                                      |
| Submit an overlong note, correct it and save          | Invalid field receives focus and an associated error; no rejected write occurs; valid rating and note persist |
| Return to Recipe History                              | Focus moves to the history heading                                                                            |

Separate keyboard regressions verify profile cancel/save return to the opener,
removal cancellation returns to Delete or Decrease, confirmed deletion focuses
ingredient entry, and retry recovers a failed profile read. Explicit chat creation
and selection focus the transcript; restoring a chat through the tablist does
not steal tab focus. Removal after a validation error clears the obsolete field
error association.

These are agent-driven automated walkthroughs. No human-operated keyboard or
screen-reader session was performed.

## Screens, fixes and evidence

Each theme covers onboarding, onboarding validation, empty groceries, grocery
validation, nutrition validation, empty history, empty conversations, long
conversation/recipe content, recipe feedback, invalid feedback with a selected
rating, failed history lists, profile editing and expired authentication.
Separate cases scan loading and failed profile states.

The pass fixes inactive tab-panel references during onboarding/profile states,
lost focus after navigation and removal, unassociated local field errors, and
low-contrast selected labels, timestamps, pagers and recipe-detail source links.
Existing theme tokens, responsive layout, mutation guards and recovery behavior
remain intact. Selected tabs also have an underline, rather than color alone.

Each run writes per-state JSON reports with axe/browser metadata, timestamp,
theme, viewport and measured page width. Long-chat and feedback cases also save
full-page screenshots in both themes and viewport projects.
CI retains `frontend/test-results/` as the **frontend-browser-acceptance** artifact
for 30 days, even after failures. Failure traces and screenshots use the existing
Playwright retention policy. Artifacts contain synthetic fixtures, not account
data.

Reviewers should supplement these checks with their screen reader and physical
phone. Browser zoom, other browser engines and assistive-technology behavior are
not certified here. Hosted sign-in, cloud persistence and live recipe generation
remain separate acceptance checks.
