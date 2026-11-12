import { expect, test, type Page } from "@playwright/test";
import type { Conversation, Turn } from "../src/conversations.ts";
import type { Grocery } from "../src/api.ts";
import type { RecipeSnapshot } from "../src/recipeHistoryApi.ts";
import { backend } from "./backend.ts";
import { recipeFixture } from "./recipe-fixture.ts";

async function journeyBackend(page: Page) {
  const profile = await backend(page, null);
  const groceries: Grocery[] = [];
  const conversations: Conversation[] = [];
  const turns = new Map<string, Turn[]>();
  const timestamp = "2026-11-12T12:00:00.000000Z";
  const generatedRecipeId = crypto.randomUUID();
  const recipe = recipeFixture();
  let saved: RecipeSnapshot | null = null;
  let revision = 1;
  let signedIn = false;
  await page.route("**/auth/session", async (route) => {
    if (signedIn) await route.fallback();
    else
      await route.fulfill({ status: 401, json: { code: "unauthenticated" } });
  });
  await page.route("**/auth/login", async (route) => {
    signedIn = true;
    await route.fulfill({ status: 302, headers: { Location: "/" } });
  });
  await page.route("**/api/v1/groceries**", async (route) => {
    const request = route.request();
    if (request.method() === "GET") {
      await route.fulfill({
        headers: { ETag: `"groceries-${revision}"` },
        json: { items: groceries, nextCursor: null },
      });
      return;
    }
    expect(request.headers()["if-match"]).toBe(`"groceries-${revision}"`);
    const body = request.postDataJSON() as Grocery;
    if (request.method() === "POST") {
      groceries.push({ ...body, groceryId: crypto.randomUUID() });
    } else {
      expect(request.method()).toBe("PATCH");
      groceries[0]!.quantity = body.quantity;
    }
    revision++;
    await route.fulfill({
      status: request.method() === "POST" ? 201 : 200,
      headers: { ETag: `"groceries-${revision}"` },
      json: { data: groceries[0] },
    });
  });
  await page.route("**/api/v1/conversations**", async (route) => {
    const request = route.request();
    const parts = new URL(request.url()).pathname.split("/");
    if (request.method() === "GET") {
      await route.fulfill({
        json: {
          items:
            parts[5] === "turns" ? (turns.get(parts[4]!) ?? []) : conversations,
          nextCursor: null,
        },
      });
      return;
    }
    expect(request.headers()["idempotency-key"]).toBeTruthy();
    if (parts[5] !== "turns") {
      const conversation: Conversation = {
        conversationId: crypto.randomUUID(),
        title: "Acceptance dinner",
        createdAt: timestamp,
        updatedAt: timestamp,
      };
      conversations.push(conversation);
      turns.set(conversation.conversationId, []);
      await route.fulfill({ status: 201, json: { data: conversation } });
      return;
    }
    const body = request.postDataJSON() as { content: string };
    const turn: Turn = {
      turnId: crypto.randomUUID(),
      conversationId: parts[4]!,
      turnIndex: 0,
      status: "completed",
      messages: [
        {
          messageId: crypto.randomUUID(),
          role: "user",
          content: body.content,
          timestamp,
        },
        {
          messageId: crypto.randomUUID(),
          role: "assistant",
          content: "Here is a grounded pantry dinner.",
          timestamp,
        },
      ],
      generatedRecipeId,
      recipe,
    };
    turns.get(parts[4]!)!.push(turn);
    await route.fulfill({ json: { data: turn } });
  });
  await page.route("**/api/v1/recipes**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "GET") {
      if (url.pathname === "/api/v1/recipes") {
        await route.fulfill({
          json: {
            items:
              saved &&
              (!url.searchParams.has("awaitingFeedback") ||
                saved.data.awaitingFeedback)
                ? [{ ...saved.data, title: saved.data.recipe.title }]
                : [],
            nextCursor: null,
          },
        });
      } else {
        expect(saved).not.toBeNull();
        await route.fulfill({
          headers: { ETag: saved!.etag },
          json: { data: saved!.data },
        });
      }
      return;
    }
    expect(request.headers()["idempotency-key"]).toBeTruthy();
    const body = request.postDataJSON() as {
      generatedRecipeId?: string;
      rating?: "liked";
      note?: string;
    };
    if (!saved) {
      expect(body.generatedRecipeId).toBe(generatedRecipeId);
      saved = {
        data: {
          historyId: crypto.randomUUID(),
          generatedRecipeId,
          recipe: structuredClone(recipe),
          savedAt: timestamp,
          cookedAt: null,
          state: "planned",
          awaitingFeedback: false,
          rating: null,
          note: null,
          feedbackRevision: 0,
        },
        etag: '"saved-1"',
      };
    } else {
      expect(request.headers()["if-match"]).toBe(saved.etag);
      if (url.pathname.endsWith("/cooked")) {
        saved.data.cookedAt = timestamp;
        saved.data.state = "cooked";
        saved.data.awaitingFeedback = true;
      } else {
        expect(url.pathname.endsWith("/feedback")).toBe(true);
        saved.data.rating = body.rating!;
        saved.data.note = body.note!;
        saved.data.feedbackRevision++;
        saved.data.awaitingFeedback = false;
      }
      saved.etag = `"saved-${++revision}"`;
    }
    await route.fulfill({
      status: url.pathname === "/api/v1/recipes" ? 201 : 200,
      headers: { ETag: saved.etag },
      json: { data: saved.data },
    });
  });
  return { profile };
}

test("MVP sign-in, onboarding and three-tab journey persist across reload", async ({
  page,
}) => {
  const { profile } = await journeyBackend(page);
  await page.goto("/");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Let's make this kitchen yours." }),
  ).toBeVisible();
  await page.getByLabel("Food allergies", { exact: true }).fill("peanut");
  await page
    .getByLabel("Strict dietary restrictions", { exact: true })
    .fill("vegetarian");
  await page.getByLabel("Flavors and foods you like").fill("lemon");
  await page.getByLabel("Flavors and foods you dislike").fill("olives");
  await page.getByLabel("Preferred cuisines").fill("Italian");
  await page.getByLabel("Spice preference").selectOption("mild");
  await page.getByLabel("Cooking experience").selectOption("beginner");
  await page.getByLabel("Nutrition preferences").fill("More vegetables");
  await page.getByLabel("Usual cooking time (minutes)").fill("30");
  await page
    .getByRole("checkbox", { name: /^I have reviewed my allergies/ })
    .check();
  await page
    .getByRole("button", { name: "Save and open my kitchen", exact: true })
    .click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Your cooking profile is saved." }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Groceries", exact: true }).click();
  await expect(page.getByText("Your grocery list is empty.")).toBeVisible();
  await page.getByLabel("Ingredient name", { exact: true }).fill("Rice");
  await page.getByLabel("Quantity (optional)").fill("100");
  await page.getByLabel("Unit (optional)").selectOption("g");
  await page.getByLabel("Ingredient name", { exact: true }).press("Enter");
  await page
    .getByRole("button", { name: "Increase Rice", exact: true })
    .click();
  await expect(page.getByLabel("Quantity for Rice")).toHaveValue("101");
  await page.getByRole("tab", { name: "Sous Chef", exact: true }).click();
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await page
    .getByRole("textbox", { name: "What would you like to cook?" })
    .fill("Make a 30-minute pantry dinner.");
  await page.getByRole("button", { name: "Send request" }).click();
  await expect(
    page.getByRole("region", { name: "Simple carrot rice", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save to Cook", exact: true }).click();
  await page.getByRole("tab", { name: "Recipe History" }).click();
  await expect(page.getByText("Planned", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "View Simple carrot rice", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Save rating", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "I've cooked this", exact: true })
    .click();
  await page.getByRole("radio", { name: "Liked", exact: true }).check();
  await page
    .getByRole("textbox", { name: "Cooking notes (optional)" })
    .fill("Synthetic browser note: I loved the carrot.");
  await page.getByRole("button", { name: "Save rating", exact: true }).click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Personalization is processed separately" }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("tab", { name: "Groceries", exact: true }).click();
  await expect(page.getByLabel("Quantity for Rice")).toHaveValue("101");
  await page.getByRole("tab", { name: "Sous Chef", exact: true }).click();
  const disclosure = page.getByRole("button", {
    name: "Conversations",
    exact: true,
  });
  if ((await disclosure.getAttribute("aria-expanded")) === "false")
    await disclosure.click();
  await page
    .getByRole("navigation", { name: "Conversations", exact: true })
    .getByRole("button", { name: /^Acceptance dinner\b/ })
    .click();
  await expect(
    page.getByText("Make a 30-minute pantry dinner.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save to Cook", exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Recipe History" }).click();
  await page
    .getByRole("button", { name: "View Simple carrot rice", exact: true })
    .click();
  await expect(
    page.getByRole("radio", { name: "Liked", exact: true }),
  ).toBeChecked();
  await expect(
    page.getByRole("textbox", { name: "Cooking notes (optional)" }),
  ).toHaveValue("Synthetic browser note: I loved the carrot.");
  expect(profile.state.profile?.allergies).toEqual(["peanut"]);
});
