import { expect, test, type Page } from "@playwright/test";
import type { RecipeSnapshot, RecipeSummary } from "../src/recipeHistoryApi.ts";
import { backend } from "./backend.ts";
import { recipeFixture } from "./recipe-fixture.ts";

const timestamp = "2026-11-09T12:00:00.000000Z";
const conversationId = "11111111-1111-4111-8111-111111111111";

function record(title: string, cooked = false): RecipeSnapshot {
  return {
    etag: '"revision-1"',
    data: {
      historyId: crypto.randomUUID(),
      generatedRecipeId: crypto.randomUUID(),
      recipe: { ...recipeFixture(), title },
      savedAt: timestamp,
      cookedAt: cooked ? timestamp : null,
      state: cooked ? "cooked" : "planned",
      awaitingFeedback: cooked,
      rating: null,
      note: null,
      feedbackRevision: 0,
    },
  };
}

function summary(snapshot: RecipeSnapshot): RecipeSummary {
  const {
    recipe,
    generatedRecipeId: _,
    note: __,
    feedbackRevision: ___,
    ...fields
  } = snapshot.data;
  return { ...fields, title: recipe.title };
}

function historyState(records: RecipeSnapshot[] = []) {
  const generatedRecipeId: string = crypto.randomUUID();
  return {
    records,
    listFailure: 0,
    detailFailure: 0,
    mutationFailure: 0,
    commitThenLose: false,
    revisionless: false,
    malformed: false,
    listReads: 0,
    writes: [] as {
      path: string;
      body: Record<string, unknown>;
      headers: Record<string, string>;
    }[],
    receipts: new Map<string, RecipeSnapshot>(),
    generatedRecipeId,
  };
}

async function historyBackend(page: Page, state = historyState()) {
  const auth = await backend(page);
  await page.route("**/api/v1/recipes**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const id = url.pathname.split("/")[4];
    if (request.method() === "GET" && !id) {
      state.listReads++;
      if (state.listFailure) {
        await route.fulfill({
          status: state.listFailure,
          json: { code: "unavailable" },
        });
        return;
      }
      const pending = url.searchParams.get("awaitingFeedback") === "true";
      const items = state.records.filter(
        (item) => !pending || item.data.awaitingFeedback,
      );
      const offset = Number(url.searchParams.get("cursor") ?? 0);
      await route.fulfill({
        json: state.malformed
          ? { items: [{}], nextCursor: null }
          : {
              items: items.slice(offset, offset + 2).map(summary),
              nextCursor: items.length > offset + 2 ? String(offset + 2) : null,
            },
      });
      return;
    }
    if (request.method() === "GET") {
      const found = state.records.find((item) => item.data.historyId === id);
      await route.fulfill(
        state.detailFailure || !found
          ? { status: state.detailFailure || 404, json: { code: "not_found" } }
          : {
              headers: state.revisionless ? {} : { ETag: found.etag },
              json: { data: found.data },
            },
      );
      return;
    }
    const body = request.postDataJSON() as Record<string, unknown>;
    const headers = request.headers();
    state.writes.push({ path: url.pathname, body, headers });
    if (state.mutationFailure) {
      await route.fulfill({
        status: state.mutationFailure,
        json: { code: "revision_conflict", detail: "private-provider-details" },
      });
      return;
    }
    let found = state.records.find((item) =>
      id
        ? item.data.historyId === id
        : item.data.generatedRecipeId === body.generatedRecipeId,
    );
    const receipt = state.receipts.get(headers["idempotency-key"]);
    if (receipt) {
      await route.fulfill({
        headers: { ETag: receipt.etag },
        json: { data: receipt.data },
      });
      return;
    }
    if (!id) {
      if (!found) {
        found = record("Simple carrot rice");
        found.data.generatedRecipeId = String(body.generatedRecipeId);
        state.records.unshift(found);
      }
    } else {
      if (!found) throw new Error("Missing synthetic recipe.");
      if (headers["if-match"] !== found.etag) {
        await route.fulfill({
          status: 412,
          json: { code: "revision_conflict" },
        });
        return;
      }
      if (url.pathname.endsWith("/cooked")) {
        found.data.cookedAt = timestamp;
        found.data.state = "cooked";
        found.data.awaitingFeedback = true;
      } else {
        found.data.rating = body.rating as RecipeSnapshot["data"]["rating"];
        found.data.note = body.note as string | null;
        found.data.feedbackRevision++;
        found.data.awaitingFeedback = false;
      }
      found.etag = `"revision-${Number(found.etag.match(/\d+/)?.[0] ?? 1) + 1}"`;
    }
    if (!found) throw new Error("Missing result.");
    state.receipts.set(headers["idempotency-key"], structuredClone(found));
    if (state.commitThenLose) {
      state.commitThenLose = false;
      await route.abort("failed");
    } else {
      await route.fulfill({
        status: id ? 200 : 201,
        headers: { ETag: found.etag },
        json: { data: found.data },
      });
    }
  });
  await page.route("**/api/v1/conversations**", async (route) => {
    if (new URL(route.request().url()).pathname.endsWith("/turns")) {
      await route.fulfill({
        json: {
          items: [
            {
              turnId: crypto.randomUUID(),
              conversationId,
              turnIndex: 0,
              status: "completed",
              generatedRecipeId: state.generatedRecipeId,
              recipe: recipeFixture(),
              messages: [
                {
                  messageId: crypto.randomUUID(),
                  role: "user",
                  content: "Dinner",
                  timestamp,
                },
                {
                  messageId: crypto.randomUUID(),
                  role: "assistant",
                  content: "Recipe",
                  timestamp,
                },
              ],
            },
          ],
          nextCursor: null,
        },
      });
    } else {
      await route.fulfill({
        json: {
          items: [
            {
              conversationId,
              title: "Recipe ideas",
              createdAt: timestamp,
              updatedAt: timestamp,
            },
          ],
          nextCursor: null,
        },
      });
    }
  });
  return { state, auth };
}

async function openChat(page: Page) {
  await page.goto("/#sous-chef");
  const toggle = page.getByRole("button", {
    name: "Conversations",
    exact: true,
  });
  if ((await toggle.getAttribute("aria-expanded")) === "false")
    await toggle.click();
  await page
    .getByRole("button", { name: "Recipe ideas", exact: false })
    .click();
  await expect(
    page.getByRole("button", { name: "Save to Cook", exact: true }),
  ).toBeVisible();
}

test("save, cook, rate and edit round-trip through history without application reload", async ({
  page,
}, testInfo) => {
  const { state, auth } = await historyBackend(page);
  await openChat(page);
  await page.evaluate(() => {
    document.documentElement.dataset.noReload = "yes";
  });
  await page.getByRole("button", { name: "Save to Cook", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Recipe saved to cook later" }),
  ).toContainText("has not been marked cooked");
  await page.getByRole("tab", { name: "Recipe History" }).click();
  const list = page.getByRole("region", {
    name: "Saved and cooked",
    exact: true,
  });
  await expect(list).toContainText("Planned");
  await list.getByRole("button", { name: "View Simple carrot rice" }).click();
  await expect(
    page.getByRole("region", { name: "Recipe detail" }),
  ).toContainText("original saved recipe");
  await expect(page.getByRole("radio")).toHaveCount(0);
  await page.getByRole("button", { name: "I've cooked this" }).click();
  await expect(
    page.getByRole("radio", { name: "Liked", exact: true }),
  ).toBeVisible();
  await page.getByRole("radio", { name: "Liked", exact: true }).focus();
  await page.keyboard.press("Space");
  await page
    .getByRole("textbox", { name: "Cooking notes (optional)" })
    .fill("Original note: less salt next time.");
  await page.screenshot({
    path: testInfo.outputPath("feedback.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Save rating", exact: true }).click();
  await expect(page.getByRole("status")).toContainText(
    "Personalization is processed separately",
  );
  await expect(
    page.getByRole("radio", { name: "Liked", exact: true }),
  ).toBeChecked();
  await page.getByRole("radio", { name: "Fine", exact: true }).check();
  await page
    .getByRole("textbox", { name: "Cooking notes (optional)" })
    .fill("Edited note.");
  await page.getByRole("button", { name: "Save feedback changes" }).click();
  await expect(
    page.getByRole("button", { name: "Save feedback changes" }),
  ).toBeDisabled();
  expect(state.records[0].data.feedbackRevision).toBe(2);
  expect(state.records[0].data.note).toBe("Edited note.");
  expect(
    state.writes.every(
      (write) =>
        write.headers["x-csrf-token"] === auth.state.token &&
        !!write.headers["idempotency-key"],
    ),
  ).toBe(true);
  expect(
    state.writes
      .filter((write) => write.path.endsWith("/feedback"))
      .every((write) => !!write.headers["if-match"]),
  ).toBe(true);
  await page
    .getByRole("button", { name: "Back to Recipe History", exact: false })
    .click();
  await expect(
    page.getByRole("region", { name: "Needs your rating", exact: true }),
  ).toContainText("Nothing waiting");
  await expect(list).toContainText("Fine");
  expect(
    await page.evaluate(() => document.documentElement.dataset.noReload),
  ).toBe("yes");
  await page.reload();
  await list.getByRole("button", { name: "View Simple carrot rice" }).click();
  await expect(
    page.getByRole("radio", { name: "Fine", exact: true }),
  ).toBeChecked();
  await expect(
    page.getByRole("textbox", { name: "Cooking notes (optional)" }),
  ).toHaveValue("Edited note.");
  await expect(
    page.getByRole("region", { name: "Cooking steps" }),
  ).toContainText("Cook the rice.");
});

test("Cook This confirms an existing planned snapshot rather than saving a duplicate", async ({
  page,
}) => {
  const planned = record("Simple carrot rice");
  const state = historyState([planned]);
  state.generatedRecipeId = planned.data.generatedRecipeId;
  await historyBackend(page, state);
  await openChat(page);
  await page.getByRole("button", { name: "Cook This", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Recipe marked cooked" }),
  ).toContainText("Recipe marked cooked");
  expect(state.records).toHaveLength(1);
  expect(state.writes.map((write) => write.path)).toEqual([
    "/api/v1/recipes",
    `/api/v1/recipes/${planned.data.historyId}/cooked`,
  ]);
  await page.getByRole("tab", { name: "Recipe History" }).click();
  await expect(
    page.getByRole("button", { name: "Rate Simple carrot rice" }),
  ).toBeVisible();
});

test("failed rating retains input and explicitly reloads before retry with a fresh revision", async ({
  page,
}) => {
  const item = record("Saved soup", true);
  const { state } = await historyBackend(page, historyState([item]));
  await page.goto("/#recipe-history");
  await page.getByRole("button", { name: "Rate Saved soup" }).click();
  await page.getByRole("radio", { name: "Didn't Like" }).check();
  await page
    .getByRole("textbox", { name: "Cooking notes (optional)" })
    .fill("Keep my input");
  state.mutationFailure = 412;
  await page.getByRole("button", { name: "Save rating", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("rating wasn't saved");
  await expect(
    page.getByRole("textbox", { name: "Cooking notes (optional)" }),
  ).toHaveValue("Keep my input");
  await expect(page.getByRole("radio", { name: "Didn't Like" })).toBeChecked();
  await expect(page.getByText("private-provider-details")).toHaveCount(0);
  state.mutationFailure = 0;
  item.etag = '"revision-9"';
  await page.getByRole("button", { name: "Load saved version" }).click();
  await expect(page.getByRole("status")).toContainText(
    "Your draft is unchanged",
  );
  expect(state.writes).toHaveLength(1);
  await page.getByRole("button", { name: "Save rating", exact: true }).click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Your rating and notes are saved" }),
  ).toBeVisible();
  expect(state.writes[1].headers["if-match"]).toBe('"revision-9"');
  await page
    .getByRole("button", { name: "Back to Recipe History", exact: false })
    .click();
  await expect(
    page.getByRole("region", { name: "Saved and cooked", exact: true }),
  ).toContainText("Didn't Like");
});

test("lost save response retries with the same idempotency key and creates one snapshot", async ({
  page,
}) => {
  const { state } = await historyBackend(page);
  await openChat(page);
  state.commitThenLose = true;
  await page.getByRole("button", { name: "Save to Cook", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("could not be confirmed");
  await page.getByRole("button", { name: "Save to Cook", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Recipe saved to cook later" }),
  ).toContainText("has not been marked cooked");
  expect(state.writes[0].headers["idempotency-key"]).toBe(
    state.writes[1].headers["idempotency-key"],
  );
  expect(state.records).toHaveLength(1);
});

test("uncertain feedback success is reconciled without an automatic second write", async ({
  page,
}) => {
  const item = record("Rice", true);
  const { state } = await historyBackend(page, historyState([item]));
  await page.goto("/#recipe-history");
  await page.getByRole("button", { name: "Rate Rice", exact: true }).click();
  await page.getByRole("radio", { name: "Fine", exact: true }).check();
  state.commitThenLose = true;
  await page.getByRole("button", { name: "Save rating", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("could not be confirmed");
  await page.getByRole("button", { name: "Load saved version" }).click();
  await expect(page.getByRole("status")).toContainText(
    "Saved version loaded: Fine",
  );
  expect(state.writes).toHaveLength(1);
  expect(item.data.feedbackRevision).toBe(1);
});

test("lists paginate independently and render honest planned, cooked and rated states", async ({
  page,
}, testInfo) => {
  const items = [
    record("Carrot rice", true),
    record("Saved soup"),
    record("Weeknight beans", true),
  ];
  items[2].data.rating = "liked";
  items[2].data.feedbackRevision = 1;
  items[2].data.awaitingFeedback = false;
  await historyBackend(page, historyState(items));
  await page.goto("/#recipe-history");
  const list = page.getByRole("region", {
    name: "Saved and cooked",
    exact: true,
  });
  await expect(list).toContainText("Saved soup");
  await expect(list).toContainText("Planned");
  await expect(list).toContainText("Needs your rating");
  await page.screenshot({
    path: testInfo.outputPath("history.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await list.getByRole("button", { name: "Next", exact: true }).click();
  await expect(list).toContainText("Weeknight beans");
  await expect(list).toContainText("Liked");
  await expect(list.getByRole("listitem")).toHaveCount(1);
  await list.getByRole("button", { name: "Previous", exact: true }).click();
  await expect(list).toContainText("Saved soup");
  await expect(
    page
      .getByRole("region", { name: "Needs your rating", exact: true })
      .getByRole("listitem"),
  ).toHaveCount(1);
});

test("loading, empty, failed and malformed lists are distinct", async ({
  page,
}) => {
  const { state } = await historyBackend(page);
  state.listFailure = 503;
  await page.goto("/#recipe-history");
  await expect(page.getByRole("alert")).toHaveCount(2);
  await expect(
    page.getByText("No recipes saved yet.", { exact: false }),
  ).toHaveCount(0);
  state.listFailure = 0;
  state.malformed = true;
  await page
    .getByRole("region", { name: "Saved and cooked", exact: true })
    .getByRole("button", { name: "Try again" })
    .click();
  await expect(page.getByRole("alert")).toHaveCount(2);
  state.malformed = false;
  await page
    .getByRole("region", { name: "Saved and cooked", exact: true })
    .getByRole("button", { name: "Try again" })
    .click();
  await expect(
    page.getByText("No recipes saved yet.", { exact: false }),
  ).toBeVisible();
  let release!: () => void;
  const wait = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/recipes?*", async (route) => {
    await wait;
    await route.fulfill({ json: { items: [], nextCursor: null } });
  });
  await page.reload();
  await expect(
    page.getByText("Loading recipe history...", { exact: true }),
  ).toBeVisible();
  release();
  await expect(
    page.getByText("No recipes saved yet.", { exact: false }),
  ).toBeVisible();
});

test("missing detail revisions fail visibly, expiry clears data, and changed accounts cannot write", async ({
  page,
}) => {
  const item = record("Private recipe", true);
  const { state, auth } = await historyBackend(page, historyState([item]));
  state.revisionless = true;
  await page.goto("/#recipe-history");
  await page.getByRole("button", { name: "Rate Private recipe" }).click();
  await expect(page.getByRole("alert")).toContainText("could not be confirmed");
  await expect(page.getByRole("radio")).toHaveCount(0);
  state.revisionless = false;
  await page.getByRole("button", { name: "Reload recipe" }).click();
  await page.getByRole("radio", { name: "Liked", exact: true }).check();
  auth.state.sessionUser = "different-user";
  await page.getByRole("button", { name: "Save rating", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Sign in", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Private recipe", { exact: true })).toHaveCount(
    0,
  );
  expect(state.writes).toHaveLength(0);
});

test("cross-tab invalidation refreshes mounted history after save", async ({
  page,
  context,
}) => {
  const state = historyState();
  await historyBackend(page, state);
  await page.goto("/#recipe-history");
  await expect(
    page.getByText("No recipes saved yet.", { exact: false }),
  ).toBeVisible();
  const other = await context.newPage();
  await historyBackend(other, state);
  await openChat(other);
  const reads = state.listReads;
  await other
    .getByRole("button", { name: "Save to Cook", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "View Simple carrot rice" }),
  ).toBeVisible();
  expect(state.listReads).toBeGreaterThan(reads);
  await other.getByRole("button", { name: "Cook This", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Rate Simple carrot rice" }),
  ).toBeVisible();
  await other.getByRole("tab", { name: "Recipe History" }).click();
  await other.getByRole("button", { name: "Rate Simple carrot rice" }).click();
  await other.getByRole("radio", { name: "Liked", exact: true }).check();
  await other.getByRole("button", { name: "Save rating", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Needs your rating", exact: true }),
  ).toContainText("Nothing waiting");
  await expect(
    page.getByRole("region", { name: "Saved and cooked", exact: true }),
  ).toContainText("Liked");
  await other.close();
});

test("feedback navigation warns about unsaved changes and enforces the note limit", async ({
  page,
}) => {
  const { state } = await historyBackend(
    page,
    historyState([record("Rice", true)]),
  );
  await page.goto("/#recipe-history");
  await page.getByRole("button", { name: "Rate Rice", exact: true }).click();
  await page.getByRole("radio", { name: "Fine", exact: true }).check();
  await page
    .getByRole("textbox", { name: "Cooking notes (optional)" })
    .fill("a".repeat(2001));
  await page.getByRole("button", { name: "Save rating", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("2,000 characters");
  expect(state.writes).toHaveLength(0);
  page.once("dialog", (dialog) => void dialog.dismiss());
  await page.getByRole("tab", { name: "Groceries", exact: true }).click();
  await expect(
    page.getByRole("radio", { name: "Fine", exact: true }),
  ).toBeChecked();
  await page
    .getByRole("textbox", { name: "Cooking notes (optional)" })
    .fill(" ");
  await page.getByRole("button", { name: "Save rating", exact: true }).click();
  await expect(page.getByRole("status")).toContainText(
    "Your rating and notes are saved",
  );
  expect(state.records[0].data.note).toBeNull();
});
