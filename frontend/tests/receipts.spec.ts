import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { backend } from "./backend.ts";
import type { Grocery } from "../src/api.ts";

const file = {
  name: "receipt.png",
  mimeType: "image/png",
  buffer: Buffer.from("synthetic receipt image"),
};
const pasta: Grocery = {
  groceryId: "11111111-1111-4111-8111-111111111111",
  name: "Pasta",
  quantity: 500,
  unit: "g",
  receipt: {
    importId: "22222222-2222-4222-8222-222222222222",
    quantitySource: "estimated",
    estimateReason: "Assumed one 500 g packet.",
  },
};

async function receipts(page: Page, enabled = true) {
  await backend(page);
  const state = {
    items: [] as Grocery[],
    revision: 1,
    requests: [] as Record<string, string>[],
    fail: false,
    loseResponse: false,
  };
  await page.route("**/api/v1/groceries", (route) =>
    route.fulfill({
      headers: { ETag: `"revision-${state.revision}"` },
      json: {
        items: state.items,
        nextCursor: null,
        receiptImportEnabled: enabled,
      },
    }),
  );
  await page.route("**/api/v1/groceries/receipts", async (route) => {
    state.requests.push(route.request().headers());
    if (state.fail) {
      state.fail = false;
      await route.fulfill({
        status: 503,
        json: { code: "receipt_ai_unavailable" },
      });
      return;
    }
    const already = state.items.length > 0;
    if (!already) {
      state.items.push(pasta);
      state.revision++;
    }
    if (state.loseResponse) {
      state.loseResponse = false;
      await route.abort("failed");
      return;
    }
    await route.fulfill({
      json: {
        importId: pasta.receipt!.importId,
        added: already ? 0 : 1,
        merged: 0,
        estimated: already ? 0 : 1,
        skipped: already ? [] : [{ name: "Candle", reason: "Nonfood." }],
        needsReview: already
          ? []
          : [{ name: "MYSTERY", reason: "Unclear food identity." }],
        alreadyImported: already,
      },
    });
  });
  await page.goto("/");
  return state;
}

test("receipt upload is hidden when the backend feature is disabled", async ({
  page,
}) => {
  await receipts(page, false);
  await expect(
    page.getByRole("button", { name: "Add ingredient" }),
  ).toBeVisible();
  await expect(page.getByLabel("Receipt file")).toHaveCount(0);
  await expect(page.getByText("Upload receipt", { exact: true })).toHaveCount(
    0,
  );
});

test("receipt upload starts compact and expands accessibly with the keyboard", async ({
  page,
}) => {
  await receipts(page);
  const disclosure = page.getByText("Upload receipt", { exact: true });
  await expect(page.getByLabel("Receipt file")).toBeHidden();
  const collapsed = await page.locator(".receipt-upload").boundingBox();
  expect(collapsed!.height).toBeLessThanOrEqual(64);
  await disclosure.focus();
  await disclosure.press("Enter");
  await expect(page.getByLabel("Receipt file")).toBeVisible();
  const result = await new AxeBuilder({ page })
    .include(".receipt-upload")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(result.violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    ),
  ).toBe(false);
  await disclosure.press("Enter");
  await expect(page.getByLabel("Receipt file")).toBeHidden();
});

test("upload adds groceries, shows estimates and explains skipped and unclear lines", async ({
  page,
}) => {
  const state = await receipts(page);
  await page.getByText("Upload receipt", { exact: true }).click();
  await page.getByLabel("Receipt file").setInputFiles(file);
  await page.getByRole("button", { name: "Upload and add groceries" }).click();
  await expect(page.getByLabel("Receipt import results")).toContainText(
    "Added 1 foods",
  );
  await expect(
    page.getByRole("heading", { name: "Pasta", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Estimated: Assumed one 500 g packet."),
  ).toBeVisible();
  await page.getByText("Upload receipt", { exact: true }).click();
  await expect(page.getByLabel("Receipt import results")).toBeVisible();
  await expect(page.getByText("MYSTERY: Unclear food identity.")).toBeVisible();
  await page.getByText("Skipped nonfood items").click();
  await expect(page.getByText("Candle", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("list", { name: "Saved groceries" }),
  ).not.toContainText("Candle");
  expect(state.requests[0]["content-type"]).toBe("image/png");
  expect(state.requests[0]["if-match"]).toBe('"revision-1"');
  expect(state.requests[0]["x-csrf-token"]).toBe("t".repeat(43));
  expect(state.requests[0]["idempotency-key"]).toBeTruthy();
  await page.reload();
  await expect(
    page.getByText("Estimated: Assumed one 500 g packet."),
  ).toBeVisible();
});

test("AI failure leaves manual entry available and retry retains its import key", async ({
  page,
}) => {
  const state = await receipts(page);
  state.fail = true;
  await page.getByText("Upload receipt", { exact: true }).click();
  await page.getByLabel("Receipt file").setInputFiles(file);
  await page.getByRole("button", { name: "Upload and add groceries" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Receipt reading is unavailable",
  );
  await expect(
    page.getByRole("button", { name: "Add ingredient" }),
  ).toBeEnabled();
  expect(state.requests).toHaveLength(1);
  await page.getByRole("button", { name: "Upload and add groceries" }).click();
  await expect(page.getByLabel("Receipt import results")).toBeVisible();
  expect(state.requests[0]["idempotency-key"]).toBe(
    state.requests[1]["idempotency-key"],
  );
});

test("a lost response reloads stock and same-file retry does not add it twice", async ({
  page,
}) => {
  const state = await receipts(page);
  state.loseResponse = true;
  await page.getByText("Upload receipt", { exact: true }).click();
  await page.getByLabel("Receipt file").setInputFiles(file);
  await page.getByRole("button", { name: "Upload and add groceries" }).click();
  await expect(page.getByRole("alert")).toContainText("could not be confirmed");
  await expect(
    page.getByRole("heading", { name: "Pasta", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Upload and add groceries" }).click();
  await expect(page.getByLabel("Receipt import results")).toContainText(
    "already imported",
  );
  expect(state.items).toHaveLength(1);
  expect(state.requests[0]["idempotency-key"]).toBe(
    state.requests[1]["idempotency-key"],
  );
  expect(state.requests[1]["if-match"]).toBe('"revision-2"');
});
