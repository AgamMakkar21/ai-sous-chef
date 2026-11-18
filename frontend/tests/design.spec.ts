import { expect, test } from "@playwright/test";
import { backend } from "./backend.ts";

test("theme previews, toggling and persistence never change account data", async ({
  page,
}) => {
  const { writes } = await backend(page);
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "YOUR KITCHEN" }),
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
    page.getByRole("heading", { name: "YOUR KITCHEN" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Switch to light theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

test("inspiration opens the existing chef without sending a recipe request", async ({
  page,
}) => {
  await backend(page);
  const mutations: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/") && request.method() !== "GET")
      mutations.push(request.url());
  });
  await page.goto("/");
  await page
    .getByRole("button", { name: "Let's cook something" })
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
    page.getByRole("heading", { name: "YOUR KITCHEN" }),
  ).toBeVisible();
  const loaded = await page.evaluate(async () => {
    const fonts = await document.fonts.load(
      '112px "Jersey 10"',
      "YOUR KITCHEN",
    );
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
      page.getByRole("heading", { name: "YOUR KITCHEN" }),
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
