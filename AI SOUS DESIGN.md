# Sous Chef Design System

Use this document to reproduce the current Sous Chef interface in another chat. Preserve the visual identity and product controls; do not reinterpret it as a new design.

## Overview

**Direction: a warm pixel kitchen, built around a practical cooking workspace.**

Use a near-black canvas, warm orange actions, chunky pixel display type, and floating pixel-art produce. Keep forms, navigation, and ingredient lists clean and readable. The personality comes from the headline and illustrations, not from making every control look like an arcade game.

This is primarily a task-oriented application, not a full-screen marketing landing page. Users should reach their groceries without scrolling through a large decorative introduction.

### Locked decisions

- The hero says **YOUR KITCHEN**, with no second phrase or trailing period.
- Use **Jersey 10**, weight **400**, for the hero; its glyphs are already thick.
- Do not substitute Press Start 2P, a thin pixel font, or a bolded monospace approximation.
- Keep the yellow-to-orange-to-coral headline gradient and hard downward pixel shadow.
- Keep floating fruit and vegetables, the pixel cooking-pot mark, and the dark-first palette.
- Keep the kitchen switcher, groceries, Sous Chef, recipe history, cooking profile, theme toggle, reset, and sign-out affordance.
- Use plain HTML, CSS, and JavaScript for a standalone prototype unless the host project already requires a framework.

### Starter prompt for another chat

> Recreate Sous Chef using this design system as the visual authority. Build a dark-first cooking workspace with Jersey 10 display lettering, the exact headline "Your kitchen" rendered uppercase, a vertical yellow/orange/coral gradient, and a hard 5px downward shadow. Use Segoe UI for functional text and Consolas for small labels. Add original, gently floating pixel-art produce around a pixel cooking pot. Keep the workspace compact: four kitchen choices, three tool tabs, a grocery editor on the left, and calories plus recipe inspiration on the right. Use the tokens, component rules, breakpoints, and interaction requirements below. Do not add "A little inspired," substitute a thin pixel font, use emoji as artwork, or turn the product into a generic AI landing page.

## Colors

The following CSS is the authoritative dark palette. Derived colors remain formulas so their relationships survive theme changes.

```css
:root {
  color-scheme: dark;

  --cp-bg: #1a1a1a;
  --cp-text: #dedede;
  --cp-text-soft: #b0b0b0;
  --cp-text-muted: #919191;
  --cp-border-strong: #5f5f5f;

  --cp-warning: #fbbf24;
  --cp-danger: #f87171;
  --cp-success: #4ade80;
  --cp-orange: color-mix(in srgb, var(--cp-warning) 64%, var(--cp-danger));
  --cp-coral: var(--cp-danger);
  --cp-button-ink: var(--cp-bg);
  --cp-action-text: var(--cp-orange);
  --cp-status-text: var(--cp-success);
  --cp-produce-highlight: #dedede;
  --cp-produce-shadow: #919191;
  --cp-produce-detail: #5f5f5f;
  --cp-tomato-shadow: var(--cp-danger);
  --cp-leaf-shadow: var(--cp-produce-shadow);
  --cp-avocado-flesh: var(--cp-produce-highlight);
  --cp-stalk-highlight: var(--cp-produce-highlight);
  --cp-hero-start: var(--cp-warning);
  --cp-hero-middle: var(--cp-orange);
  --cp-hero-end: var(--cp-coral);

  --cp-bg-elevated: color-mix(in srgb, var(--cp-bg) 97%, var(--cp-text));
  --cp-surface: color-mix(in srgb, var(--cp-bg) 95%, var(--cp-text));
  --cp-surface-soft: color-mix(in srgb, var(--cp-bg) 92%, var(--cp-text));
  --cp-border: color-mix(in srgb, var(--cp-bg) 85%, var(--cp-text));
  --cp-grid: color-mix(in srgb, var(--cp-text) 9%, var(--cp-bg));
  --cp-pixel-shadow: color-mix(in srgb, var(--cp-coral) 32%, var(--cp-button-ink));

  --cp-clear: color-mix(in srgb, var(--cp-bg) 0%, transparent);
  --cp-sheen: rgba(255, 255, 255, 0.04);
  --cp-overlay: rgba(41, 41, 41, 0.88);
  --cp-card-shadow: 0 1px 2px var(--cp-sheen);
}
```

For quick visual reference, the dark derived colors round to orange `#faa340`, elevated background `#202020`, card surface `#242424`, soft surface `#2a2a2a`, and border `#373737`. These rounded swatches do not replace the formulas.

### Color roles

| Role | Treatment |
| --- | --- |
| Canvas | Near-black; never a bright yellow page background |
| Primary action | Solid warm orange; dark text in dark mode, white text in light mode |
| Active navigation | Orange text and a thin orange underline |
| Selected kitchen | Orange border, a 7% orange-tinted background, and a small selection dot |
| Main text | Warm light gray, not pure white everywhere |
| Supporting text | Soft gray; quieter metadata uses muted gray |
| Status | Green for readiness or saved status; coral/red for errors and destructive actions |
| Decorative gradient | Hero lettering; only a very faint warm tint on the inspiration card |

Use color variables in component styles. Do not scatter literal colors across components or introduce competing purple, blue, teal, or pink brand accents.

### Light mode: match the supplied reference

Light mode follows the supplied light reference: a warm off-white canvas, orange-to-red display lettering, a pale peach pixel shadow, orange buttons with white labels, and natural green produce. Preserve the existing workspace layout and the headline **YOUR KITCHEN**.

This is a matched light palette, not a pure-white copy of the dark palette. The canvas uses the reference hero's sampled `#f7f4f0`; button orange, tomato red, leaf green, and peach shadow are sampled from the image. Keep the headline's warm gold/orange/red progression rather than darkening the entire gradient to brown.

```css
html[data-theme="light"] {
  color-scheme: light;
  --cp-bg: #f7f4f0;
  --cp-bg-elevated: #fcfbf8;
  --cp-surface: #ffffff;
  --cp-surface-soft: #f5f5f5;
  --cp-border: #dedede;
  --cp-border-strong: #919191;
  --cp-text: #242424;
  --cp-text-muted: #5c5c5c;
  --cp-text-soft: #6f6f6f;
  --cp-orange: #db7332;
  --cp-warning: #e9a23b;
  --cp-danger: #ca3a31;
  --cp-success: #4ca154;
  --cp-button-ink: var(--cp-surface);
  --cp-pixel-shadow: #e7bcb8;
  --cp-produce-highlight: #efc6a7;
  --cp-produce-shadow: #efc6a7;
  --cp-produce-detail: #8bc89a;
  --cp-tomato-shadow: #dc8280;
  --cp-leaf-shadow: #8bc89a;
  --cp-avocado-flesh: var(--cp-text);
  --cp-stalk-highlight: var(--cp-text);
  --cp-sheen: rgba(255, 255, 255, 0.55);
  --cp-overlay: rgba(255, 255, 255, 0.8);
}
```

Default to dark unless the user has selected light. Persist that selection. Support `?scoutTheme=light` and `?scoutTheme=dark` for direct previews.

Use `--cp-action-text` for accent-colored labels, links, focus rings, and selected borders; use `--cp-orange` for primary button fills and decorative orange paint. The aliases stay shared while each theme supplies its own matched palette. Use the named produce paint tokens so light-mode tomato shading, green leaves, and dark avocado interiors match the reference without changing dark mode.

## Typography

| Role | Family | Size and treatment |
| --- | --- | --- |
| Hero | Jersey 10 | 400 weight, uppercase, zero letter spacing, 0.95 line height |
| Main interface | Segoe UI, Aptos, Calibri, sans-serif | Base 15px / 1.5 |
| Section heading | Main interface family | 20px, 600 weight, -0.5px tracking |
| Supporting heading | Main interface family | 18px, 550-600 weight |
| Inputs and buttons | Main interface family | 12px; buttons use 600 weight |
| Helper text | Main interface family | Usually 10-13px |
| Eyebrows and metadata | Consolas, Courier New, monospace | 8-10px; uppercase labels use approximately 1-2px tracking |

The small metadata sizes describe the current compact prototype; do not use them for instructions or essential long-form content.

Load the actual [Jersey 10 font](https://fonts.google.com/specimen/Jersey+10) before assessing the visual result. A fallback font is not an acceptable final rendering. For an offline, single-file deliverable, embed a WOFF2 subset and preserve its SIL Open Font License.

The current embedded subset only covers the current headline's characters. Rebuild the subset if the headline changes; do not assume it supports arbitrary text.

```css
.hero h1 {
  font-family: "Jersey 10", Consolas, "Courier New", monospace;
  font-size: clamp(64px, 8vw, 112px);
  font-weight: 400;
  font-synthesis: none;
  line-height: 0.95;
  letter-spacing: 0;
  text-transform: uppercase;
  background: linear-gradient(
    180deg,
    var(--cp-hero-start),
    var(--cp-hero-middle) 45%,
    var(--cp-hero-end)
  );
  color: var(--cp-action-text);
  background-clip: text;
  -webkit-text-fill-color: var(--cp-clear);
  filter: drop-shadow(0 5px 0 var(--cp-pixel-shadow));
}
```

Use normal readable sans-serif type for the rest of the product. Pixel typography is a display accent, not the default interface font.

## Layout

### Page hierarchy

```text
Slim local-demo notice + reset
Brand header + cooking profile + sign out + theme control
Four kitchen choices
Groceries / Sous Chef / Recipe History tabs
Compact hero: YOUR KITCHEN + supporting copy | floating pixel produce
Main workspace:
  Grocery form + ingredient list          | Ingredient calories
                                         | Recipe inspiration
Quiet footer
```

### Desktop geometry

- Center the page in a 1200px maximum-width shell with 32px horizontal padding.
- Increase the shell maximum to 1280px at viewport widths of 1500px and above.
- Use a 98px header, a four-column kitchen selector with 10px gaps, and underlined tabs.
- Keep the hero approximately 286px tall, increasing its minimum to 310px on wide screens.
- Give hero copy up to 58% of the width and position the produce scene in the right 46%.
- Use a workspace grid of `minmax(0, 1.75fr) minmax(280px, 1fr)` with a 22px gap.
- Use approximately 20-24px card padding and 8-12px gaps within controls.
- Favor a 4px spacing rhythm, while retaining the specific dimensions above for close reproduction.

### Responsive rules

| Viewport | Adaptation |
| --- | --- |
| 901-1499px | Default desktop composition |
| 681-900px | 24px page padding; workspace ratio 1.5:1; ingredient name spans its form row |
| 680px and below | 18px page padding; two-column kitchen selector; one-column workspace; 20px card padding |
| 1500px and above | Wider shell; produce scene capped at 480px |

At 900px and below, set the hero type to `clamp(56px, 8vw, 72px)`. At 680px and below, use `clamp(52px, 14vw, 80px)`.

On small screens, retain the tomato, avocado, and carrot; remove the decorative orbit, central pot, lemon, and broccoli from the hero scene. Keep the brand pot in the header. Reduce decoration before reducing readability.

Keep the profile and theme controls accessible when header labels collapse. Sign out remains available in the profile dialog. Never hide required product actions with no alternative.

## Elevation & Depth

Use tonal layers and thin borders, not large shadows, to separate the canvas, forms, and cards.

Cards use a 1px border and the restrained `--cp-card-shadow`. Avoid glassmorphism, blurred panels, luminous outlines, and heavy black offset shadows.

The intentional exception is the hero's hard, 5px downward pixel shadow. It gives the chunky lettering the stepped depth seen in the reference.

Behind the hero, use a very faint dotted field: 20px grid spacing and approximately 0.7px dots. A thin, dashed 220px circle may organize the desktop produce scene. Neither should compete with text.

## Shapes

| Element | Geometry |
| --- | --- |
| Main cards and dialogs | 16px corner radius |
| Buttons, kitchen choices, icon containers | 10px corner radius |
| Inputs and selects | 8px corner radius |
| Quantity stepper | 7px corner radius |
| Count badges and color swatches | 4px corner radius |
| Component boundaries | Usually 1px borders |
| Status marks | Tiny square green status marks; small round kitchen-selection dot |

Use filled or outlined rounded rectangles for controls, not oversized pills. Reserve hard square edges for pixel artwork and the display lettering.

## Components

| Component | Required treatment and behavior |
| --- | --- |
| Brand | Pixel cooking-pot mark beside `sous.chef`; orange dot; small uppercase monospace subtitle |
| Demo notice | Slim, quiet strip with green status mark, honest local-demo explanation, and reset action |
| Kitchen selector | Diner Desk, Market Board, Lunchbox, After Hours; numbered metadata and small striped swatches; separate local inventories |
| Tool tabs | Groceries, Sous Chef, Recipe History; small outline icons; active orange underline; restrained count badges |
| Hero | Eyebrow `DINNER STARTS HERE`; only `YOUR KITCHEN` as the headline; supporting copy below; produce scene to the right |
| Grocery form | Ingredient name, optional quantity, optional unit; one prominent orange Add ingredient button |
| Ingredient row | Pixel ingredient thumbnail, ingredient name, quantity/unit, delete control, -1/+1 stepper, editable quantity, and Save quantity action |
| Calorie reference | Quiet secondary button; results beneath the form; explicit per-100g units and offline-reference disclaimer |
| Inspiration card | Faint warm tint, small pixel lemon, concise copy, and an orange text action with an arrow |
| Sous Chef | Readable message area, suggestion buttons, recipe steps, and save-to-history action; identify canned replies as local demo content |
| Recipe history | Scannable saved-recipe rows with a View recipe action and a useful empty state |
| Profile dialog | 440px maximum width, 28px padding, name/servings/preference fields, clear save and cancel actions |
| Feedback | Small bottom-centered toast; inline form errors; no large modal for ordinary success |

### Interaction states

- Primary buttons use orange fill, theme-appropriate ink, 10px corners, and a 40px minimum visual height.
- Secondary buttons use a dark surface or transparent fill with a thin border.
- Hover brightens enabled buttons slightly; press moves them down 1px.
- Focus uses a 2px orange outline with a 4px offset; focused fields also get an orange border.
- Disabled controls use approximately 40% opacity and do not animate as clickable.
- Surface and border transitions take approximately 160ms.
- Use labels for inputs, accessible names for icon controls, keyboard-operable tabs, and dismissible dialogs.

### Pixel artwork and motion

Use original inline SVG sprites built on small integer grids, approximately 16-28 units. Keep their rectangular steps crisp with `shape-rendering="crispEdges"`; do not replace them with emoji, photos, rounded vector illustrations, or blurry raster images.

The desktop scene contains a tomato, lemon, avocado, carrot, broccoli, and a central cooking pot. Produce is roughly 43-90px across its relevant dimensions; the pot is 95px square. Use warm highlights and green leaves from the system palette.

Decorative artwork must not intercept clicks or be announced as content by screen readers.

Float gently over 6 seconds with `ease-in-out`, a maximum upward travel of 11px, a rotation change of approximately 5 degrees, and staggered negative delays. Keep it ambient, not bouncy.

Honor `prefers-reduced-motion: reduce` by disabling animation and transitions.

### Product truth and voice

Write like a helpful cooking companion: brief, practical, and low-pressure. Prefer "Add ingredient," "Quantity unknown," and "Let's cook something" to hype or technical jargon.

For a replica of the current demo, keep data in browser storage, allow fractional and unknown quantities, prevent negative quantities, keep each kitchen separate, and confirm resets before deleting local changes. If storage is unavailable, state that changes are session-only.

Do not claim live AI, live USDA access, real authentication, or medical precision unless those services are actually implemented.

## Do's and Don'ts

**Do**

- Keep the chunky Jersey 10 headline, warm dark palette, and pixel produce as the recognizable identity.
- Make the grocery workflow more visually prominent than decorative or marketing content.
- Carry the same tokens, spacing, controls, empty states, and focus treatment into new screens.
- Use a self-contained HTML file, inline assets, and no runtime network dependencies when reproducing the lightweight demo.

**Don't**

- Restore "A little inspired" or add another phrase to the hero.
- Use Press Start 2P, a thin pixel typeface, Inter, or a generic startup-dashboard visual style.
- Reintroduce the cream graph-paper page, bright yellow hero panel, thick dark borders, or heavy offset card shadows from the earlier layout reference.
- Add oversized pill controls, broad decorative gradients, excessive cards, or animation that interferes with tasks.

### Replication acceptance check

The result should show only **YOUR KITCHEN** in the chunky display font, with a warm vertical gradient and hard downward shadow. Functional text should remain sans-serif. Produce should float gently without overlapping essential controls.

Check 320px, 390px, 680px, 768px, 1024px, and 1440px widths. Verify the actual font loads, no content clips horizontally, controls still work, keyboard focus remains visible, and reduced-motion preferences are respected. For an offline replica, verify that loading the page makes no network requests.

Implementation reference: `index.html` in this folder. Documentation structure informed by [Impeccable's design-system guidance](https://github.com/microsoft/frontier-ghcp-ttt-rvas/blob/main/.agents/skills/impeccable/SKILL.md); the current interface and the user's latest choices remain the visual authority.
