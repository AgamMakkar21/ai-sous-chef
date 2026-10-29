import { expect, test, type Page } from "@playwright/test";
import { backend, preferences } from "./backend.ts";

async function openProfile(page: Page) {
  await page
    .getByRole("button", { name: "Cooking profile", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "A little more you. A better fit." }),
  ).toBeVisible();
}

test("first-run onboarding saves all nine fields and persists after reload", async ({
  page,
}) => {
  const { writes } = await backend(page, null);
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Let's make this kitchen yours." }),
  ).toBeVisible();
  await expect(
    page.getByRole("tab", { name: "Groceries", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Food allergies", { exact: true }).fill(" peanuts \n");
  await page
    .getByLabel("Strict dietary restrictions", { exact: true })
    .fill("vegetarian");
  await page
    .getByLabel("Flavors and foods you like", { exact: true })
    .fill("lemon");
  await page
    .getByLabel("Flavors and foods you dislike", { exact: true })
    .fill("olives");
  await page.getByLabel("Preferred cuisines", { exact: true }).fill("Italian");
  await page
    .getByLabel("Spice preference", { exact: true })
    .selectOption("mild");
  await page
    .getByLabel("Cooking experience", { exact: true })
    .selectOption("beginner");
  await page
    .getByLabel("Nutrition preferences", { exact: true })
    .fill("More vegetables");
  await page
    .getByLabel("I have reviewed my allergies", { exact: false })
    .check();
  await page.getByRole("button", { name: "Save and open my kitchen" }).click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Your cooking profile is saved." }),
  ).toHaveText("Your cooking profile is saved.");
  expect(writes).toHaveLength(1);
  expect(writes[0]?.method).toBe("PUT");
  expect(writes[0]?.body).toEqual(preferences);
  expect(writes[0]?.headers["if-match"]).toBe('"revision-1"');
  expect(writes[0]?.headers["x-csrf-token"]).toBe("t".repeat(43));
  await page.reload();
  await expect(
    page.getByRole("heading", {
      name: "Good cooking starts with what you have.",
    }),
  ).toBeVisible();
  await openProfile(page);
  await expect(page.getByLabel("Food allergies", { exact: true })).toHaveValue(
    "peanuts",
  );
  await expect(
    page.getByLabel("Nutrition preferences", { exact: true }),
  ).toHaveValue("More vegetables");
});

test("backend completion overrides browser storage and asks for explicit safety confirmation", async ({
  page,
}) => {
  const { state, writes } = await backend(page);
  state.completed = false;
  await page.addInitScript(() =>
    localStorage.setItem("onboardingCompleted", "true"),
  );
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Let's make this kitchen yours." }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save and open my kitchen" }).click();
  await expect(
    page.getByText("Confirm your food-safety preferences before continuing."),
  ).toBeVisible();
  expect(writes).toHaveLength(0);
});

test("returning users edit only intended fields and persistence survives reload", async ({
  page,
}) => {
  const { state, writes } = await backend(page);
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Cooking profile", exact: true }),
  ).toBeVisible();
  state.profile = { ...preferences, cookingSkill: "advanced" };
  state.revision = 2;
  await openProfile(page);
  await expect(
    page.getByLabel("Cooking experience", { exact: true }),
  ).toHaveValue("advanced");
  await page
    .getByLabel("Flavors and foods you like", { exact: true })
    .fill("basil");
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Your cooking profile is saved." }),
  ).toHaveText("Your cooking profile is saved.");
  expect(writes[0]?.method).toBe("PATCH");
  expect(writes[0]?.headers["if-match"]).toBe('"revision-2"');
  expect(writes[0]?.body).toEqual({ likes: ["basil"] });
  await page.reload();
  await openProfile(page);
  await expect(
    page.getByLabel("Flavors and foods you like", { exact: true }),
  ).toHaveValue("basil");
  await expect(page.getByLabel("Food allergies", { exact: true })).toHaveValue(
    "peanuts",
  );
});

test("field errors keep the draft, use friendly messages, and focus the invalid field", async ({
  page,
}) => {
  const { state, writes } = await backend(page);
  await page.goto("/");
  await openProfile(page);
  await page
    .getByLabel("Strict dietary restrictions", { exact: true })
    .fill("keto");
  await page
    .getByLabel("I have reviewed my allergies", { exact: false })
    .check();
  state.failure = { status: 422, field: true };
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  await expect(
    page.getByText(
      "This cannot currently be enforced as a strict constraint. Review this entry.",
    ),
  ).toBeVisible();
  await expect(
    page.getByLabel("Strict dietary restrictions", { exact: true }),
  ).toHaveValue("keto");
  await expect(
    page.getByLabel("Strict dietary restrictions", { exact: true }),
  ).toBeFocused();
  await expect(page.locator("body")).not.toContainText(
    "private-provider-details",
  );
  expect(writes).toHaveLength(1);
});

test("client validation rejects invalid time and oversized entries without a write", async ({
  page,
}) => {
  const { writes } = await backend(page);
  await page.goto("/");
  await openProfile(page);
  await page
    .getByLabel("Usual cooking time (minutes)", { exact: true })
    .fill("4");
  await page
    .getByLabel("Flavors and foods you like", { exact: true })
    .fill("x".repeat(101));
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  await expect(
    page.getByText("Choose a whole number from 5 to 240."),
  ).toBeVisible();
  await expect(
    page.getByText("Keep each entry to 100 characters or fewer."),
  ).toBeVisible();
  await expect(
    page.getByLabel("Flavors and foods you like", { exact: true }),
  ).toHaveValue("x".repeat(101));
  expect(writes).toHaveLength(0);
});

test("a stale revision requires comparison and explicit retry without overwriting unrelated fields", async ({
  page,
}) => {
  const { state, writes } = await backend(page);
  await page.goto("/");
  await openProfile(page);
  await page
    .getByLabel("Flavors and foods you like", { exact: true })
    .fill("basil");
  state.profile = { ...preferences, typicalTimeBudgetMinutes: 45 };
  state.revision = 2;
  state.failure = { status: 412 };
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  await expect(
    page.getByText("Your profile changed elsewhere.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save profile", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Load latest saved profile" }).click();
  await expect(
    page.getByRole("definition").filter({ hasText: "45" }),
  ).toBeVisible();
  expect(writes).toHaveLength(1);
  await expect(
    page.getByLabel("Flavors and foods you like", { exact: true }),
  ).toHaveValue("basil");
  await page
    .getByLabel("I reviewed the saved profile.", { exact: false })
    .check();
  let releaseGroceries = () => {};
  const groceries = new Promise<void>((resolve) => {
    releaseGroceries = resolve;
  });
  await page.route("**/api/v1/groceries", async (route) => {
    await groceries;
    await route.fulfill({
      headers: { ETag: '"revision-2"' },
      json: { items: [], nextCursor: null },
    });
  });
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  try {
    await expect(page.getByRole("status")).toHaveCount(2);
    await expect(
      page
        .getByRole("status")
        .filter({ hasText: "Your cooking profile is saved." }),
    ).toHaveText("Your cooking profile is saved.");
    await expect(
      page.getByRole("status").filter({ hasText: "Loading your groceries..." }),
    ).toBeVisible();
  } finally {
    releaseGroceries();
  }
  await expect(
    page.getByText("Your grocery list is empty.", { exact: false }),
  ).toBeVisible();
  expect(writes).toHaveLength(2);
  expect(writes[1]?.headers["if-match"]).toBe('"revision-2"');
  expect(writes[1]?.body).toEqual({ likes: ["basil"] });
  expect(state.profile?.typicalTimeBudgetMinutes).toBe(45);
});

for (const mode of ["transport", "server", "malformed"] as const) {
  test(`an uncertain ${mode} response cannot silently claim success or replay a write`, async ({
    page,
  }) => {
    const { state, writes } = await backend(page);
    await page.goto("/");
    await openProfile(page);
    await page
      .getByLabel("Flavors and foods you like", { exact: true })
      .fill("basil");
    state.failure =
      mode === "transport"
        ? { status: 0 }
        : mode === "server"
          ? { status: 503, commit: true }
          : { status: 200, commit: true, invalid: true };
    await page
      .getByRole("button", { name: "Save profile", exact: true })
      .click();
    await expect(
      page.getByText("The save could not be confirmed.", { exact: false }),
    ).toBeVisible();
    await expect(
      page.getByLabel("Flavors and foods you like", { exact: true }),
    ).toHaveValue("basil");
    await expect(
      page.getByRole("button", { name: "Save profile", exact: true }),
    ).toBeDisabled();
    await page
      .getByRole("button", { name: "Load latest saved profile" })
      .click();
    await expect(
      page.getByRole("checkbox", {
        name: "I reviewed the saved profile.",
        exact: false,
      }),
    ).toBeVisible();
    expect(writes).toHaveLength(1);
    await expect(page.getByText("Your cooking profile is saved.")).toHaveCount(
      0,
    );
  });
}

test("CSRF renewal requires a new user-initiated save", async ({ page }) => {
  const { state, writes } = await backend(page);
  await page.goto("/");
  await openProfile(page);
  const sessions = state.sessions;
  await page
    .getByLabel("Flavors and foods you like", { exact: true })
    .fill("basil");
  state.token = "r".repeat(43);
  state.failure = { status: 403 };
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  await expect(
    page.getByText("Your security token changed.", { exact: false }),
  ).toBeVisible();
  expect(state.sessions).toBe(sessions + 1);
  expect(writes).toHaveLength(1);
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Your cooking profile is saved." }),
  ).toHaveText("Your cooking profile is saved.");
  expect(writes[1]?.headers["x-csrf-token"]).toBe("r".repeat(43));
});

test("an HTML 401 clears both saved details and drafts before offering sign-in", async ({
  page,
}) => {
  const { state, writes } = await backend(page);
  await page.goto("/");
  await openProfile(page);
  await page
    .getByLabel("Flavors and foods you like", { exact: true })
    .fill("private-draft-marker");
  state.failure = { status: 401, html: true };
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Sign in", exact: true }),
  ).toHaveAttribute("href", "/auth/login");
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("peanuts");
  await expect(page.locator("body")).not.toContainText("private-draft-marker");
  expect(writes).toHaveLength(1);
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
});

test("identity changes during reconciliation clear the old user's draft", async ({
  page,
}) => {
  const { state } = await backend(page);
  await page.goto("/");
  await openProfile(page);
  await page
    .getByLabel("Flavors and foods you like", { exact: true })
    .fill("private-draft-marker");
  state.failure = { status: 412 };
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  state.sessionUser = "different-synthetic-user";
  await page.getByRole("button", { name: "Load latest saved profile" }).click();
  await expect(
    page.getByRole("link", { name: "Sign in", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveCount(0);
});

test("a mismatched profile identity is never displayed", async ({ page }) => {
  const { state } = await backend(page);
  state.profileUser = "different-synthetic-user";
  await page.goto("/");
  await expect(
    page.getByRole("link", { name: "Sign in", exact: true }),
  ).toBeVisible();
  await expect(page.locator("body")).not.toContainText("peanuts");
});

test("an invalid successful profile response fails visibly and can be retried", async ({
  page,
}) => {
  const { state } = await backend(page);
  state.getFailure = { status: 200, invalid: true };
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Your kitchen could not be loaded." }),
  ).toBeVisible();
  state.getFailure = null;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "Good cooking starts with what you have.",
    }),
  ).toBeVisible();
});

test("tabs support keyboard navigation and a reloadable hash without private URLs or storage", async ({
  page,
  baseURL,
}) => {
  await backend(page);
  if (!baseURL)
    throw new Error("The browser test requires a configured base URL.");
  const origin = new URL(baseURL).origin;
  const external: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).origin !== origin) external.push(request.url());
  });
  await page.goto("/");
  const groceries = page.getByRole("tab", { name: "Groceries", exact: true });
  await groceries.focus();
  await groceries.press("ArrowRight");
  const chef = page.getByRole("tab", { name: "Sous Chef", exact: true });
  await expect(chef).toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/#sous-chef$/);
  await chef.press("End");
  await expect(
    page.getByRole("tab", { name: "Recipe History" }),
  ).toHaveAttribute("aria-selected", "true");
  await page.reload();
  await expect(
    page.getByRole("tab", { name: "Recipe History" }),
  ).toHaveAttribute("aria-selected", "true");
  await page.getByRole("link", { name: "Skip to main content" }).focus();
  await page.getByRole("link", { name: "Skip to main content" }).press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();
  await expect(page).toHaveURL(/#recipe-history$/);
  await page.getByRole("tab", { name: "Recipe History" }).press("Home");
  await expect(groceries).toHaveAttribute("aria-selected", "true");
  expect(new URL(page.url()).search).toBe("");
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
  expect(external).toEqual([]);
});

test("canceling navigation preserves a dirty draft", async ({ page }) => {
  await backend(page);
  await page.goto("/");
  await openProfile(page);
  await page
    .getByLabel("Flavors and foods you like", { exact: true })
    .fill("basil");
  page.once("dialog", (dialog) => void dialog.dismiss());
  await page.getByRole("tab", { name: "Sous Chef", exact: true }).click();
  await expect(
    page.getByLabel("Flavors and foods you like", { exact: true }),
  ).toHaveValue("basil");
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "Good cooking starts with what you have.",
    }),
  ).toBeVisible();
});

test("preferences remain escaped and the form fits a narrow viewport in both themes", async ({
  page,
}) => {
  await backend(page, {
    ...preferences,
    likes: ["<script>window.injected=true</script>"],
  });
  await page.goto("/?scoutTheme=dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await openProfile(page);
  await expect(
    page.getByLabel("Flavors and foods you like", { exact: true }),
  ).toHaveValue("<script>window.injected=true</script>");
  expect(await page.evaluate(() => Object.hasOwn(window, "injected"))).toBe(
    false,
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.goto("/?scoutTheme=light");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await openProfile(page);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
