import { expect, test, type Locator } from "@playwright/test";
import { backend } from "./backend.ts";

async function holdNextTransition(
  element: Locator,
  trigger: () => Promise<void>,
) {
  await element.evaluate((node) => {
    const hold = (event: Event) => {
      if (event.target !== node) return;
      for (const animation of node.getAnimations()) animation.pause();
      node.removeEventListener("transitionrun", hold);
    };
    node.addEventListener("transitionrun", hold);
  });
  await trigger();
  await expect
    .poll(() =>
      element.evaluate((node) =>
        node
          .getAnimations()
          .some((animation) => animation.playState === "paused"),
      ),
    )
    .toBe(true);
}

test("theme previews, toggling and persistence never change account data", async ({
  page,
}) => {
  const { writes } = await backend(page);
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "YOUR PANTRY" }),
  ).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("button", { name: "Switch to light theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.goto("/?scoutTheme=dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("button", { name: "Switch to light theme" }).click();
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(writes).toEqual([]);
});

test("theme controls work without browser storage", async ({ page }) => {
  await backend(page);
  await page.addInitScript(() => {
    Object.defineProperty(window, "localStorage", {
      get() {
        throw new DOMException("Storage unavailable", "SecurityError");
      },
    });
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "YOUR PANTRY" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Switch to light theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

for (const theme of ["dark", "light"]) {
  test(`${theme}: one navigation pill slides between destinations and stays instant for keyboard and reduced motion`, async ({
    page,
  }) => {
    await backend(page);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await page.goto(`/?scoutTheme=${theme}`);
    const indicator = page.locator(".tab-indicator");
    const chef = page.getByRole("tab", { name: "Sous Chef", exact: true });
    const history = page.getByRole("tab", {
      name: "Recipe History",
      exact: true,
    });
    const groceries = page.getByRole("tab", { name: "Groceries", exact: true });
    await expect(groceries).toHaveAttribute("aria-selected", "true");
    await expect(indicator).toHaveCount(1);
    const start = await indicator.boundingBox();
    const target = await chef.boundingBox();
    await holdNextTransition(indicator, () => chef.click());
    const halfway = await indicator.evaluate((element) => {
      const animation = element.getAnimations()[0];
      if (!animation?.effect)
        throw new Error("The sliding pill did not animate.");
      const duration = Number(animation.effect.getTiming().duration);
      animation.currentTime = duration / 2;
      const left = element.getBoundingClientRect().left;
      animation.play();
      return { left, duration };
    });
    expect(halfway.duration).toBe(200);
    expect(halfway.left).toBeGreaterThan(start!.x);
    expect(halfway.left).toBeLessThan(target!.x);
    await history.click();
    await expect(history).toHaveAttribute("aria-selected", "true");
    async function expectAligned(tab: typeof chef) {
      await expect
        .poll(async () => {
          const pill = await indicator.boundingBox();
          const selected = await tab.boundingBox();
          return Math.abs(pill!.x - selected!.x);
        })
        .toBeLessThan(1);
    }
    await expectAligned(history);
    await groceries.focus();
    await page.keyboard.press("ArrowRight");
    await expect(chef).toHaveAttribute("aria-selected", "true");
    await expect(indicator).toHaveCSS("transition-duration", "0s");
    await expectAligned(chef);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await history.click();
    await expect(indicator).toHaveCSS("transition-duration", "0s");
    await expectAligned(history);
    await page
      .getByRole("button", { name: "Cooking profile", exact: true })
      .click();
    await expect(indicator).toBeHidden();
  });

  test(`${theme}: Add ingredient has a visible shine sweep across its full width`, async ({
    page,
  }, info) => {
    test.skip(
      info.project.name !== "desktop",
      "Hover shine is for fine pointers.",
    );
    await backend(page);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await page.goto(`/?scoutTheme=${theme}`);
    const add = page.getByRole("button", {
      name: "Add ingredient",
      exact: true,
    });
    await expect(add).toBeEnabled();
    const shine = add.locator(".cartoon-highlight");
    const resting = await shine.boundingBox();
    const button = await add.boundingBox();
    expect(resting!.x + resting!.width).toBeLessThan(button!.x);
    await holdNextTransition(shine, () => add.hover());
    const crossing = await shine.evaluate(async (element) => {
      const animation = element.getAnimations()[0];
      if (!animation?.effect) throw new Error("The shine sweep did not start.");
      animation.pause();
      const duration = Number(animation.effect.getTiming().duration);
      animation.currentTime = duration / 2;
      await new Promise(requestAnimationFrame);
      const rect = element.getBoundingClientRect();
      return {
        left: rect.left,
        right: rect.right,
        duration,
        background: getComputedStyle(element).backgroundImage,
      };
    });
    const center = button!.x + button!.width / 2;
    expect(crossing.left).toBeLessThan(center);
    expect(crossing.right).toBeGreaterThan(center);
    expect(crossing.duration).toBe(500);
    expect(crossing.background).toContain(
      theme === "dark" ? "rgba(255, 255, 255, 0.36)" : "rgba(0, 0, 0, 0.16)",
    );
    await page.emulateMedia({ reducedMotion: "reduce" });
    await expect(shine).toHaveCSS("transition-duration", "0s");
  });

  test(`${theme}: every app view uses the shared cartoon button treatment`, async ({
    page,
  }) => {
    await backend(page);
    await page.goto(`/?scoutTheme=${theme}`);
    await expect(
      page.getByRole("heading", { name: "YOUR PANTRY" }),
    ).toBeVisible();
    async function checkControls() {
      await expect(
        page.locator(
          "button:not(.cartoon-button):not(.conversation-choice), a.button:not(.cartoon-button)",
        ),
      ).toHaveCount(0);
      const styles = await page
        .locator("button:not(.conversation-choice), a.button")
        .evaluateAll((buttons) =>
          buttons.map((button) => {
            const style = getComputedStyle(button);
            const tab = button.getAttribute("role") === "tab";
            return {
              name: button.textContent,
              pill: tab
                ? style.boxShadow === "none"
                : style.borderRadius === "9999px",
              border: style.borderTopWidth === (tab ? "0px" : "2px"),
              target: parseFloat(style.minHeight) >= 48,
              highlight:
                tab ||
                button.matches(":disabled, [aria-disabled=true]") ||
                Boolean(button.querySelector(".cartoon-highlight")),
            };
          }),
        );
      for (const { name, ...style } of styles)
        expect(style, name ?? "Icon button").toEqual({
          pill: true,
          border: true,
          target: true,
          highlight: true,
        });
    }
    await checkControls();
    await page.getByRole("tab", { name: "Sous Chef", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "New Chat", exact: true }),
    ).toBeVisible();
    await checkControls();
    await page
      .getByRole("tab", { name: "Recipe History", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "Recipe History", exact: true }),
    ).toBeVisible();
    await checkControls();
    await page
      .getByRole("button", { name: "Cooking profile", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Save profile", exact: true }),
    ).toBeVisible();
    await checkControls();
  });

  test(`${theme}: field focus changes only its own border`, async ({
    page,
  }) => {
    await backend(page);
    await page.goto(`/?scoutTheme=${theme}`);
    const ingredient = page.getByLabel("Ingredient name", { exact: true });
    const before = await ingredient.boundingBox();
    for (const label of [
      "Ingredient name",
      "Quantity (optional)",
      "Unit (optional)",
    ]) {
      const field = page.getByLabel(label, { exact: true });
      await field.focus();
      await expect(field).toHaveCSS("outline-style", "none");
      await expect(field).toHaveCSS(
        "border-color",
        theme === "dark" ? "rgb(251, 191, 36)" : "rgb(138, 101, 0)",
      );
    }
    await ingredient.focus();
    expect(await ingredient.boundingBox()).toEqual(before);
    await page
      .getByRole("button", { name: "Cooking profile", exact: true })
      .click();
    const notes = page.locator(".field textarea").first();
    await notes.focus();
    await expect(notes).toHaveCSS("outline-style", "none");
    await expect(notes).toHaveCSS(
      "border-color",
      theme === "dark" ? "rgb(251, 191, 36)" : "rgb(138, 101, 0)",
    );
    await page.keyboard.press("Tab");
    await expect(notes).not.toHaveCSS(
      "border-color",
      theme === "dark" ? "rgb(251, 191, 36)" : "rgb(138, 101, 0)",
    );
  });
}

test("cartoon buttons keep keyboard actions instant and respect reduced motion", async ({
  page,
}, info) => {
  await backend(page);
  const mutations: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/") && request.method() !== "GET")
      mutations.push(request.url());
  });
  await page.goto("/");
  const add = page.getByRole("button", { name: "Add ingredient", exact: true });
  await add.hover();
  await page.mouse.down();
  if (info.project.name === "desktop") {
    await expect(add).not.toHaveCSS("transform", "none");
    await expect(add).toHaveCSS("transition-duration", "0.12s");
  }
  await page.mouse.up();
  await page.keyboard.press("Tab");
  await add.focus();
  await page.keyboard.down("Space");
  await expect(add).toHaveCSS("transform", "none");
  await expect(add).toHaveCSS("transition-duration", "0s");
  await page.keyboard.up("Space");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await add.hover();
  await page.mouse.down();
  await expect(add).toHaveCSS("transform", "none");
  await page.mouse.up();
  await page
    .getByRole("tab", { name: "Sous Chef", exact: true })
    .press("Enter");
  await expect(
    page.getByRole("tab", { name: "Sous Chef", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByRole("button", { name: "New Chat", exact: true }),
  ).toBeVisible();
  expect(mutations).toEqual([]);
});

test("pixel kitchen loads its real font locally and fits every design breakpoint", async ({
  page,
  baseURL,
}, info) => {
  await backend(page);
  await page.route("**/api/v1/groceries", (route) =>
    route.fulfill({
      headers: { ETag: '"revision-1"' },
      json: {
        items: [
          {
            groceryId: "11111111-1111-4111-8111-111111111111",
            name: "Cherry tomatoes",
            quantity: 250,
            unit: "g",
          },
          {
            groceryId: "22222222-2222-4222-8222-222222222222",
            name: "Avocado",
            quantity: 2,
            unit: "piece",
          },
        ],
        nextCursor: null,
      },
    }),
  );
  const external: string[] = [];
  page.on("request", (request) => {
    if (
      request.url().startsWith("http") &&
      new URL(request.url()).origin !== new URL(baseURL!).origin
    )
      external.push(request.url());
  });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "YOUR PANTRY" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Cherry tomatoes" }),
  ).toBeVisible();
  await expect(
    page.locator(
      ".welcome-copy, .hero-footnote, #grocery-help, .app-footer, .ingredient-lookup, .ingredient-art, .inspiration, .kitchen-aside",
    ),
  ).toHaveCount(0);
  const license = page.locator('template[data-font-license="Jersey 10"]');
  await expect(license).toBeHidden();
  await expect(license).toContainText("SIL OPEN FONT LICENSE Version 1.1");
  const loaded = await page.evaluate(async () => {
    const fonts = await document.fonts.load('112px "Jersey 10"', "YOUR PANTRY");
    return fonts.length > 0 && fonts.every((font) => font.status === "loaded");
  });
  expect(loaded).toBe(true);
  await expect(page.locator(".floating").first()).toHaveCSS(
    "animation-name",
    "none",
  );
  for (const theme of ["dark", "light"]) {
    await page.goto(`/?scoutTheme=${theme}`);
    await expect(
      page.getByRole("heading", { name: "YOUR PANTRY" }),
    ).toBeVisible();
    for (const width of [320, 390, 680, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
        `${theme} at ${width}px`,
      ).toBe(true);
      await expect(
        page.getByRole("button", { name: "Cooking profile", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("link", { name: "Sign out", exact: true }),
      ).toBeVisible();
      const path = info.outputPath(`kitchen-${theme}-${width}.png`);
      await page.screenshot({ path, fullPage: true });
      await info.attach(`kitchen-${theme}-${width}`, {
        path,
        contentType: "image/png",
      });
    }
  }
  expect(external).toEqual([]);
});

test("all kitchen tabs share display typography without clipping or changing controls", async ({
  page,
}, info) => {
  await backend(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  const mutations: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/") && request.method() !== "GET")
      mutations.push(request.url());
  });
  for (const theme of ["dark", "light"]) {
    await page.goto(`/?scoutTheme=${theme}`);
    const references = new Map<number, Record<string, string>>();
    for (const [tab, title] of [
      ["Groceries", "YOUR PANTRY"],
      ["Sous Chef", "Your conversations"],
      ["Recipe History", "Recipe History"],
    ]) {
      await page.getByRole("tab", { name: tab, exact: true }).click();
      const heading = page.getByRole("heading", {
        name: title,
        exact: true,
        level: 1,
      });
      await expect(heading).toBeVisible();
      for (const width of [1440, 1024, 768, 680, 390, 320]) {
        await page.setViewportSize({ width, height: 900 });
        await page.evaluate(async (text) => {
          await document.fonts.load('112px "Jersey 10"', text);
        }, title);
        const typography = await heading.evaluate((element) => {
          const style = getComputedStyle(element);
          return {
            fontFamily: style.fontFamily,
            fontWeight: style.fontWeight,
            fontSize: style.fontSize,
            lineHeight: style.lineHeight,
            letterSpacing: style.letterSpacing,
            backgroundImage: style.backgroundImage,
            textFillColor: style.webkitTextFillColor,
            filter: style.filter,
          };
        });
        if (tab === "Groceries") references.set(width, typography);
        else
          expect
            .soft(typography, `${tab}, ${theme}, ${width}px`)
            .toEqual(references.get(width));
        expect
          .soft(
            await page.evaluate(
              () => document.documentElement.scrollWidth <= innerWidth,
            ),
            `${tab}, ${theme}, ${width}px`,
          )
          .toBe(true);
        const box = (await heading.boundingBox())!;
        expect.soft(box.x).toBeGreaterThanOrEqual(0);
        expect.soft(box.x + box.width).toBeLessThanOrEqual(width);
        if (width === 320 || width === 1440) {
          const path = info.outputPath(`${tab}-${theme}-${width}.png`);
          await page.screenshot({ path, fullPage: true });
          await info.attach(`${tab}-${theme}-${width}`, {
            path,
            contentType: "image/png",
          });
        }
      }
      await expect(
        page.getByRole("button", { name: "Cooking profile", exact: true }),
      ).toBeVisible();
      if (tab === "Sous Chef")
        await expect(
          page.getByRole("button", { name: "New Chat", exact: true }),
        ).toBeVisible();
    }
  }
  await expect(
    page.getByRole("tab", { name: "Groceries", exact: true }),
  ).not.toHaveCSS("font-family", /Jersey/);
  expect(mutations).toEqual([]);
});
