import AxeBuilder from "@axe-core/playwright";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

const templates = new URL(
  "../../backend/src/sous_chef/templates/",
  import.meta.url,
);
const license = readFileSync(
  new URL("assets/Jersey-10-OFL.txt", templates),
  "utf8",
)
  .replace(/\r\n/g, "\n")
  .replace(/&/g, "&amp;")
  .replace(/</g, "&lt;")
  .replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;")
  .replace(/'/g, "&#x27;");
const loginDocument = readFileSync(new URL("login.html", templates), "utf8")
  .replace(/\r\n/g, "\n")
  .replace(
    "__JERSEY_FONT__",
    readFileSync(new URL("assets/jersey-10-kitchen.woff2", templates)).toString(
      "base64",
    ),
  )
  .replace("__FONT_LICENSE__", license);
const hashes = (tag: string) =>
  [...loginDocument.matchAll(new RegExp(`<${tag}>(.*?)</${tag}>`, "gs"))]
    .map(
      (match) =>
        `'sha256-${createHash("sha256").update(match[1]!).digest("base64")}'`,
    )
    .join(" ");
const policy =
  `default-src 'none'; style-src ${hashes("style")}; script-src ${hashes("script")}; ` +
  "font-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'";

test.beforeEach(async ({ page }) => {
  // Serve the packaged public page, not a second frontend implementation.
  await page.route("**/auth/login", (route) =>
    route.fulfill({
      contentType: "text/html; charset=utf-8",
      headers: {
        "Content-Security-Policy": policy,
        "Cache-Control": "no-store",
      },
      body: loginDocument,
    }),
  );
});

test("login is keyboard accessible and keeps the existing provider destinations", async ({
  page,
}) => {
  await page.goto("/auth/login");
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("link", { name: "Skip to sign in" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("main")).toBeFocused();
  for (const [provider, label] of [
    ["aad", "Microsoft"],
    ["google", "Google"],
    ["github", "GitHub"],
  ]) {
    await page.keyboard.press("Tab");
    const link = page.getByRole("link", {
      name: `Sign in with ${label}`,
      exact: true,
    });
    await expect(link).toBeFocused();
    await expect(link).toHaveAttribute("href", `/auth/login/${provider}`);
    await expect(link).toHaveCSS("outline-width", "2px");
    await expect(link.locator("svg")).toHaveAttribute("aria-hidden", "true");
    const box = (await link.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(44);
    expect(box.width).toBeGreaterThanOrEqual(44);
  }
  await page.route("**/auth/login/github", (route) =>
    route.fulfill({
      contentType: "text/plain",
      body: "Synthetic provider handoff",
    }),
  );
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/auth\/login\/github$/);
});

test("login themes persist appearance only and tolerate unavailable storage", async ({
  page,
}) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/auth/login");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("button", { name: "Switch to light theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(
    await page.evaluate(() => ({
      local: { ...localStorage },
      session: { ...sessionStorage },
    })),
  ).toEqual({ local: { "sous-chef-theme": "light" }, session: {} });

  await page.addInitScript(() => {
    Object.defineProperty(window, "localStorage", {
      get() {
        throw new DOMException("Storage unavailable", "SecurityError");
      },
    });
  });
  await page.reload();
  await expect(
    page.getByRole("link", { name: "Sign in with Microsoft" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Switch to light theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

test("public sign-in controls share the app's cartoon buttons without changing links", async ({
  page,
}, info) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/auth/login");
  for (const theme of ["dark", "light"]) {
    if (theme === "light")
      await page.getByRole("button", { name: "Switch to light theme" }).click();
    const controls = page.locator(".provider-link, .theme-toggle");
    await expect(controls).toHaveCount(4);
    for (const control of await controls.all()) {
      await expect(control).toHaveCSS("border-radius", "9999px");
      await expect(control).toHaveCSS("border-top-width", "2px");
      await expect(control).toHaveCSS("min-height", "48px");
    }
    const link = page.getByRole("link", {
      name: "Sign in with Microsoft",
      exact: true,
    });
    await expect(link).toHaveAttribute("href", "/auth/login/aad");
    await link.hover();
    if (info.project.name === "desktop") {
      await expect(link).toHaveCSS("transition-duration", "0.12s");
      expect(
        await link.evaluate(
          (element) => getComputedStyle(element, "::after").transitionDuration,
        ),
      ).toBe("0.25s");
    }
    await link.focus();
    await page.keyboard.press("Shift");
    await expect(link).toHaveCSS("transition-duration", "0s");
  }
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.getByRole("link", { name: "Sign in with Google" }).hover();
  await expect(page.locator(".provider-link").first()).toHaveCSS(
    "transition-duration",
    "0s",
  );
});

test("login is self-contained, responsive and accessible in both themes", async ({
  page,
}, info) => {
  const unexpected: string[] = [];
  page.on("request", (request) => {
    if (
      new URL(request.url()).protocol !== "data:" &&
      !request.url().endsWith("/auth/login")
    )
      unexpected.push(request.url());
  });
  page.on("pageerror", (error) => unexpected.push(error.message));
  await page.addInitScript(() => {
    (window as unknown as { cspViolations: string[] }).cspViolations = [];
    window.addEventListener("securitypolicyviolation", (event) => {
      (window as unknown as { cspViolations: string[] }).cspViolations.push(
        event.violatedDirective,
      );
    });
  });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/auth/login");
  expect(
    await page.evaluate(async () => {
      const fonts = await document.fonts.load(
        '88px "Jersey 10"',
        "YOUR KITCHEN",
      );
      return (
        fonts.length > 0 && fonts.every((font) => font.status === "loaded")
      );
    }),
  ).toBe(true);
  await expect(page.locator(".pan")).toHaveCSS("animation-name", "none");
  await expect(page.locator(".egg")).toHaveCSS("animation-name", "none");
  await expect(page.locator(".cooking-scene")).toHaveAttribute(
    "aria-hidden",
    "true",
  );

  for (const theme of ["dark", "light"]) {
    if (theme === "light")
      await page.getByRole("button", { name: "Switch to light theme" }).click();
    for (const width of [320, 390, 680, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `${theme} at ${width}px`,
      ).toBe(true);
      const card = (await page.locator(".login-card").boundingBox())!;
      expect(Math.abs(card.x + card.width / 2 - width / 2)).toBeLessThan(1);
      const boxes = await page.locator(".provider-link").evaluateAll((links) =>
        links.map((link) => {
          const box = link.getBoundingClientRect();
          return { x: box.x, width: box.width, height: box.height };
        }),
      );
      expect(boxes).toHaveLength(3);
      expect(new Set(boxes.map((box) => box.x)).size).toBe(1);
      expect(new Set(boxes.map((box) => box.width)).size).toBe(1);
      for (const box of boxes) expect(box.height).toBeGreaterThanOrEqual(44);
      const art = (await page.locator(".cooking-scene").boundingBox())!;
      expect(art.y).toBeGreaterThanOrEqual(card.y + card.height);
      const path = info.outputPath(`login-${theme}-${width}.png`);
      await page.screenshot({ path, fullPage: true });
      await info.attach(`login-${theme}-${width}`, {
        path,
        contentType: "image/png",
      });
    }
    await page.setViewportSize({ width: 320, height: 844 });
    const result = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(result.violations).toEqual([]);
  }
  expect(unexpected).toEqual([]);
  expect(
    await page.evaluate(
      () => (window as unknown as { cspViolations: string[] }).cspViolations,
    ),
  ).toEqual([]);
});

test("login animation ends within five seconds and sign-in works without scripts", async ({
  page,
  browser,
  baseURL,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/auth/login");
  await expect(page.locator(".egg")).toHaveCSS("animation-name", "egg-flip");
  await expect(page.locator(".pan")).toHaveCSS("animation-name", "pan-toss");
  await expect
    .poll(() => page.evaluate(() => document.getAnimations().length), {
      timeout: 5000,
    })
    .toBe(0);
  const context = await browser.newContext({ javaScriptEnabled: false });
  try {
    await context.route("**/auth/login", (route) =>
      route.fulfill({
        contentType: "text/html",
        headers: { "Content-Security-Policy": policy },
        body: loginDocument,
      }),
    );
    const withoutScripts = await context.newPage();
    await withoutScripts.goto(`${baseURL}/auth/login`);
    await expect(withoutScripts.getByRole("button")).toHaveCount(0);
    await expect(
      withoutScripts.getByRole("link", { name: /^Sign in with / }),
    ).toHaveCount(3);
    await context.route("**/auth/login/google", (route) =>
      route.fulfill({
        contentType: "text/plain",
        body: "Synthetic provider handoff",
      }),
    );
    await withoutScripts
      .getByRole("link", { name: "Sign in with Google" })
      .click();
    await expect(withoutScripts).toHaveURL(/\/auth\/login\/google$/);
  } finally {
    await context.close();
  }
});
