import { expect, test, type Page } from "@playwright/test";
import type { Grocery } from "../src/api.ts";

const id = "11111111-1111-4111-8111-111111111111";
const milk: Grocery = { groceryId: id, name: "Milk", quantity: 2, unit: "l" };
type Failure = { status: number; commit?: boolean; code?: string };

async function backend(page: Page, initial: Grocery[] = []) {
  const state = {
    items: initial.map((item) => ({ ...item })),
    revision: 1,
    writes: [] as {
      method: string;
      body: Record<string, unknown> | null;
      headers: Record<string, string>;
    }[],
    failure: null as Failure | null,
    getFailure: false,
    malformed: false,
    delayed: false,
    user: "synthetic-user",
  };
  await page.route("**/auth/session", (route) =>
    route.fulfill({ json: { userId: state.user, csrfToken: "t".repeat(43) } }),
  );
  await page.route("**/api/v1/me", (route) =>
    route.fulfill({
      headers: { ETag: '"profile-1"' },
      json: {
        data: {
          userId: "synthetic-user",
          onboardingCompleted: true,
          explicitPreferences: {
            allergies: [],
            dietaryRestrictions: [],
            likes: [],
            dislikes: [],
            preferredCuisines: [],
            spicePreference: "mild",
            cookingSkill: "beginner",
            typicalTimeBudgetMinutes: 30,
            nutritionGoals: [],
          },
        },
      },
    }),
  );
  await page.route("**/api/v1/groceries**", async (route) => {
    const request = route.request();
    const method = request.method();
    const etag = () => `"groceries-${state.revision}"`;
    if (method === "GET") {
      if (state.delayed)
        await new Promise((resolve) => setTimeout(resolve, 400));
      if (state.getFailure) {
        await route.fulfill({ status: 503, json: { code: "unavailable" } });
      } else {
        await route.fulfill({
          headers: state.malformed ? {} : { ETag: etag() },
          json: state.malformed
            ? { items: [] }
            : { items: state.items, nextCursor: null },
        });
      }
      return;
    }
    const body =
      method === "DELETE"
        ? null
        : (request.postDataJSON() as Record<string, unknown>);
    state.writes.push({ method, body, headers: request.headers() });
    const failure = state.failure;
    state.failure = null;
    if (state.delayed) await new Promise((resolve) => setTimeout(resolve, 400));
    if (request.headers()["if-match"] !== etag()) {
      await route.fulfill({ status: 412, json: { code: "revision_conflict" } });
      return;
    }
    if (!failure || failure.commit) {
      if (method === "POST") {
        state.items.push({
          groceryId: `22222222-2222-4222-8222-${String(state.revision).padStart(12, "0")}`,
          name: String(body?.name),
          quantity: body?.quantity as number | null,
          unit: body?.unit as Grocery["unit"],
        });
      } else if (method === "PATCH") {
        const row = state.items.find((item) =>
          request.url().endsWith(item.groceryId),
        );
        if (row) row.quantity = body?.quantity as number | null;
      } else
        state.items = state.items.filter(
          (item) => !request.url().endsWith(item.groceryId),
        );
      state.revision++;
    }
    if (failure?.status === 0) await route.abort("failed");
    else if (failure)
      await route.fulfill({
        status: failure.status,
        contentType: "application/problem+json",
        json: {
          code: failure.code ?? "unavailable",
          detail: "private-provider-details",
        },
      });
    else
      await route.fulfill({
        status: method === "DELETE" ? 204 : method === "POST" ? 201 : 200,
        headers: { ETag: etag() },
        ...(method !== "DELETE" ? { json: { data: state.items.at(-1) } } : {}),
      });
  });
  return state;
}

function row(page: Page, name = "Milk") {
  return page
    .getByRole("listitem")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
}

test("keyboard add, change and delete persist after reload", async ({
  page,
}) => {
  const state = await backend(page);
  await page.goto("/");
  await expect(
    page.getByText("Your grocery list is empty.", { exact: false }),
  ).toBeVisible();
  await page.getByLabel("Ingredient name", { exact: true }).fill("Milk");
  await page.getByLabel("Quantity (optional)").fill("2");
  await page.getByLabel("Unit (optional)").selectOption("l");
  await page.getByLabel("Ingredient name", { exact: true }).press("Enter");
  await expect(row(page).locator(".grocery-amount")).toHaveText("2 l");
  await page
    .getByRole("button", { name: "Increase Milk", exact: true })
    .press("Enter");
  await expect(row(page).locator(".grocery-amount")).toHaveText("3 l");
  await page.getByLabel("Quantity for Milk", { exact: true }).fill("1.125");
  await page.getByLabel("Quantity for Milk", { exact: true }).press("Enter");
  await expect(row(page).locator(".grocery-amount")).toHaveText("1.125 l");
  await page.reload();
  await expect(row(page).locator(".grocery-amount")).toHaveText("1.125 l");
  await page
    .getByRole("button", { name: "Delete Milk", exact: true })
    .press("Enter");
  await page.getByRole("button", { name: "Confirm removal" }).press("Enter");
  await expect(page.getByRole("list", { name: "Saved groceries" })).toHaveCount(
    0,
  );
  await page.reload();
  await expect(
    page.getByText("Your grocery list is empty.", { exact: false }),
  ).toBeVisible();
  expect(state.writes.map((write) => write.method)).toEqual([
    "POST",
    "PATCH",
    "PATCH",
    "DELETE",
  ]);
  expect(state.writes[0]?.body).toEqual({
    name: "Milk",
    quantity: 2,
    unit: "l",
  });
  for (const [index, write] of state.writes.entries()) {
    expect(write.headers["if-match"]).toBe(`"groceries-${index + 1}"`);
    expect(write.headers["x-csrf-token"]).toBe("t".repeat(43));
  }
});

test("zero and fractional decrement require confirmation; cancellation writes nothing", async ({
  page,
}) => {
  const state = await backend(page, [{ ...milk, quantity: 0.5 }]);
  await page.goto("/");
  await page
    .getByRole("button", { name: "Decrease Milk", exact: true })
    .click();
  await expect(
    page.getByRole("group", { name: "Confirm grocery removal" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Cancel", exact: true })
    .press("Enter");
  expect(state.writes).toHaveLength(0);
  await expect(row(page).locator(".grocery-amount")).toHaveText("0.5 l");
  await page
    .getByRole("button", { name: "Decrease Milk", exact: true })
    .click();
  await page.getByRole("button", { name: "Confirm removal" }).press("Enter");
  await expect(
    page.getByText("Your grocery list is empty.", { exact: false }),
  ).toBeVisible();
  expect(state.writes[0]?.method).toBe("DELETE");
});

test("unknown quantities remain unknown until explicit entry and units stay distinct", async ({
  page,
}) => {
  await backend(page, [{ ...milk, quantity: null, unit: null }]);
  await page.goto("/");
  await expect(row(page).locator(".grocery-amount")).toHaveText(
    "Quantity unknown",
  );
  await expect(
    page.getByRole("button", { name: "Increase Milk", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Decrease Milk", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Quantity for Milk", { exact: true }).fill("0.125");
  await page.getByLabel("Quantity for Milk", { exact: true }).press("Enter");
  await expect(row(page).locator(".grocery-amount")).toHaveText("0.125");
  await page.getByLabel("Ingredient name", { exact: true }).fill("Milk");
  await page.getByLabel("Unit (optional)").selectOption("cup");
  await page.getByRole("button", { name: "Add ingredient" }).click();
  await expect(page.locator(".grocery-amount")).toHaveText([
    "0.125",
    "Quantity unknown cup",
  ]);
});

test("exact zero confirms deletion and a failed delete keeps the confirmed row", async ({
  page,
}) => {
  const state = await backend(page, [{ ...milk, quantity: 1 }]);
  await page.goto("/");
  await page
    .getByRole("button", { name: "Decrease Milk", exact: true })
    .click();
  await expect(
    page.getByRole("group", { name: "Confirm grocery removal" }),
  ).toBeVisible();
  expect(state.writes).toHaveLength(0);
  state.failure = { status: 503 };
  await page.getByRole("button", { name: "Confirm removal" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Unable to update your groceries",
  );
  await expect(row(page).locator(".grocery-amount")).toHaveText("1 l");
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Delete Milk", exact: true }),
  ).toBeEnabled();
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0]?.method).toBe("DELETE");
});

test("read failure after a committed write requires reconciliation before another write", async ({
  page,
}) => {
  const state = await backend(page, [milk]);
  await page.goto("/");
  await expect(row(page)).toBeVisible();
  state.getFailure = true;
  await page
    .getByRole("button", { name: "Increase Milk", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Unable to update your groceries",
  );
  await expect(row(page).locator(".grocery-amount")).toHaveText("2 l");
  expect(state.items[0]?.quantity).toBe(3);
  state.getFailure = false;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(row(page).locator(".grocery-amount")).toHaveText("3 l");
  expect(state.writes).toHaveLength(1);
});

test("full inventory prevents additions but permits existing-item changes", async ({
  page,
}) => {
  const initial = Array.from({ length: 200 }, (_, index): Grocery => ({
    groceryId: `33333333-3333-4333-8333-${String(index).padStart(12, "0")}`,
    name: `Ingredient ${index}`,
    quantity: 1,
    unit: null,
  }));
  const state = await backend(page, initial);
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Add ingredient" }),
  ).toBeDisabled();
  await expect(
    page.getByText("Your list has reached 200 ingredients.", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Increase Ingredient 0", exact: true })
    .click();
  await expect(row(page, "Ingredient 0").locator(".grocery-amount")).toHaveText(
    "2",
  );
  expect(state.writes).toHaveLength(1);
});

for (const status of [0, 503, 412, 403, 422, 409]) {
  test(`failed write ${status} restores confirmed quantities and retry never replays`, async ({
    page,
  }) => {
    const state = await backend(page, [milk]);
    await page.goto("/");
    await expect(row(page)).toBeVisible();
    state.failure = {
      status,
      code:
        status === 409
          ? "duplicate_grocery"
          : status === 403
            ? "csrf_failed"
            : "",
    };
    await page.getByLabel("Quantity for Milk", { exact: true }).fill("8");
    await page.getByLabel("Quantity for Milk", { exact: true }).press("Enter");
    await expect(page.getByRole("alert")).toContainText(
      "Unable to update your groceries",
    );
    await expect(row(page).locator(".grocery-amount")).toHaveText("2 l");
    await expect(
      page.getByLabel("Quantity for Milk", { exact: true }),
    ).toHaveValue("2");
    await expect(
      page.getByRole("button", { name: "Increase Milk", exact: true }),
    ).toBeDisabled();
    expect(state.writes).toHaveLength(1);
    await page.getByRole("button", { name: "Retry", exact: true }).click();
    await expect(
      page.getByText("Latest saved groceries loaded.", { exact: false }),
    ).toBeVisible();
    expect(state.writes).toHaveLength(1);
    await page
      .getByRole("button", { name: "Increase Milk", exact: true })
      .click();
    await expect(row(page).locator(".grocery-amount")).toHaveText("3 l");
    await expect(page.getByText("private-provider-details")).toHaveCount(0);
  });
}

test("committed but lost response is reconciled, not duplicated", async ({
  page,
}) => {
  const state = await backend(page);
  await page.goto("/");
  await page.getByLabel("Ingredient name", { exact: true }).fill("Beans");
  state.failure = { status: 0, commit: true };
  await page.getByRole("button", { name: "Add ingredient" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Unable to update your groceries",
  );
  await expect(
    page.getByRole("heading", { name: "Beans", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Beans", exact: true }),
  ).toBeVisible();
  expect(state.writes).toHaveLength(1);
  expect(state.items).toHaveLength(1);
});

test("rapid input is single-flight; stale concurrent update must be reviewed", async ({
  page,
}) => {
  const state = await backend(page, [milk]);
  state.delayed = true;
  await page.goto("/");
  const increase = page.getByRole("button", {
    name: "Increase Milk",
    exact: true,
  });
  await expect(increase).toBeEnabled();
  await increase.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
    button.click();
  });
  await expect(
    page.getByText("Updating your groceries...", { exact: true }),
  ).toBeVisible();
  await expect(row(page).locator(".grocery-amount")).toHaveText("2 l");
  await expect(
    page.getByRole("tab", { name: "Sous Chef", exact: true }),
  ).toBeDisabled();
  await expect(row(page).locator(".grocery-amount")).toHaveText("3 l");
  await expect(increase).toBeEnabled();
  expect(state.writes).toHaveLength(1);
  state.items[0]!.quantity = 9;
  state.revision++;
  await increase.click();
  await expect(page.getByRole("alert")).toContainText("changed elsewhere");
  await expect(row(page).locator(".grocery-amount")).toHaveText("3 l");
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(row(page).locator(".grocery-amount")).toHaveText("9 l");
  await expect(increase).toBeEnabled();
  await increase.click();
  await expect(row(page).locator(".grocery-amount")).toHaveText("10 l");
  expect(state.items[0]?.quantity).toBe(10);
  await page.reload();
  await expect(row(page).locator(".grocery-amount")).toHaveText("10 l");
});

test("loading, failed read and malformed responses never invent empty state", async ({
  page,
}) => {
  const state = await backend(page, [milk]);
  state.delayed = true;
  state.getFailure = true;
  await page.goto("/");
  await expect(
    page.getByText("Loading your groceries...", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("alert")).toContainText(
    "Unable to update your groceries",
  );
  await expect(
    page.getByText("Your grocery list is empty.", { exact: false }),
  ).toHaveCount(0);
  state.getFailure = false;
  state.malformed = true;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Retry", exact: true }),
  ).toBeEnabled();
  await expect(page.getByRole("alert")).toBeVisible();
  state.malformed = false;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(row(page)).toBeVisible();
});

test("expired session and changed identity clear inventory without replay", async ({
  page,
}) => {
  const state = await backend(page, [milk]);
  await page.goto("/");
  await expect(row(page)).toBeVisible();
  state.failure = { status: 503 };
  await page
    .getByRole("button", { name: "Increase Milk", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  state.user = "another-user";
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Sign in", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Milk", { exact: true })).toHaveCount(0);
  expect(state.writes).toHaveLength(1);
});

test("401 on a grocery save expires the shell", async ({ page }) => {
  const state = await backend(page, [milk]);
  await page.goto("/");
  await expect(row(page)).toBeVisible();
  state.failure = { status: 401 };
  await page
    .getByRole("button", { name: "Increase Milk", exact: true })
    .click();
  await expect(
    page.getByRole("link", { name: "Sign in", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Milk", { exact: true })).toHaveCount(0);
});

test("bounds, empty state and narrow layout remain usable without overflow", async ({
  page,
}) => {
  const state = await backend(page, [
    { ...milk, name: "A".repeat(100), quantity: 1000000 },
  ]);
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Increase", exact: false }),
  ).toBeDisabled();
  await page.getByLabel("Ingredient name", { exact: true }).fill("Rice");
  for (const invalid of ["0", "-1", "1000001", "0.0001"]) {
    await page.getByLabel("Quantity (optional)").fill(invalid);
    await page.getByLabel("Quantity (optional)").press("Enter");
    expect(state.writes).toHaveLength(0);
  }
  await page.getByLabel("Quantity (optional)").fill("");
  await page.getByRole("button", { name: "Add ingredient" }).click();
  await expect(row(page, "Rice").locator(".grocery-amount")).toHaveText(
    "Quantity unknown",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 320, height: 800 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  for (const button of await page
    .getByRole("region", { name: "Grocery inventory" })
    .getByRole("button")
    .all()) {
    const box = await button.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
});
