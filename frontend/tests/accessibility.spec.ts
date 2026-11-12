import AxeBuilder from "@axe-core/playwright";
import { writeFile } from "node:fs/promises";
import {
  expect,
  test,
  type Locator,
  type Page,
  type TestInfo,
} from "@playwright/test";
import type { Grocery } from "../src/api.ts";
import type { Turn } from "../src/conversations.ts";
import type { RecipeSnapshot, RecipeSummary } from "../src/recipeHistoryApi.ts";
import { backend, preferences } from "./backend.ts";
import { recipeFixture } from "./recipe-fixture.ts";

const conversationId = "11111111-1111-4111-8111-111111111111";
const generatedRecipeId = "22222222-2222-4222-8222-222222222222";
const historyId = "33333333-3333-4333-8333-333333333333";
const timestamp = "2026-11-10T12:00:00.000000Z";

async function kitchen(page: Page, onboarding = false, long = false) {
  const auth = await backend(page, onboarding ? null : preferences);
  const recipe = recipeFixture();
  if (long) {
    recipe.title = "Recipe " + "r".repeat(150);
    recipe.ingredients[0]!.name = "i".repeat(100);
    recipe.instructions[0]!.text = "s".repeat(500);
    recipe.fitReasons[0] = "f".repeat(240);
  }
  const state = {
    created: false,
    turns: [] as Turn[],
    groceries: [] as Grocery[],
    saved: null as RecipeSnapshot | null,
    writes: [] as string[],
    listFailure: false,
  };
  await page.route("**/api/v1/groceries**", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      state.writes.push("grocery");
      state.groceries.push({
        groceryId: crypto.randomUUID(),
        ...(request.postDataJSON() as Omit<Grocery, "groceryId">),
      });
    } else if (request.method() === "DELETE") {
      state.writes.push("remove");
      state.groceries = [];
      await route.fulfill({ status: 204 });
      return;
    } else expect(request.method()).toBe("GET");
    await route.fulfill({
      status: request.method() === "POST" ? 201 : 200,
      headers: { ETag: `"groceries-${state.writes.length + 1}"` },
      json:
        request.method() === "POST"
          ? { data: state.groceries.at(-1) }
          : { items: state.groceries, nextCursor: null },
    });
  });
  await page.route("**/api/v1/conversations**", async (route) => {
    const request = route.request();
    const turns = new URL(request.url()).pathname.endsWith("/turns");
    const conversation = {
      conversationId,
      title: long ? "c".repeat(100) : "New conversation",
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    if (request.method() === "GET") {
      await route.fulfill({
        json: {
          items: turns ? state.turns : state.created ? [conversation] : [],
          nextCursor: null,
        },
      });
    } else if (turns) {
      state.writes.push("chat");
      const { content } = request.postDataJSON() as { content: string };
      const turn: Turn = {
        turnId: crypto.randomUUID(),
        conversationId,
        turnIndex: 0,
        status: "completed",
        generatedRecipeId,
        recipe,
        messages: [
          { messageId: crypto.randomUUID(), role: "user", content, timestamp },
          {
            messageId: crypto.randomUUID(),
            role: "assistant",
            content: "Recipe",
            timestamp,
          },
        ],
      };
      state.turns.push(turn);
      await route.fulfill({ json: { data: turn } });
    } else {
      state.created = true;
      await route.fulfill({ status: 201, json: { data: conversation } });
    }
  });
  await page.route("**/api/v1/recipes**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "GET" && !url.pathname.split("/")[4]) {
      if (state.listFailure) {
        await route.fulfill({ status: 503, json: { code: "unavailable" } });
        return;
      }
      const saved = state.saved?.data;
      const summary: RecipeSummary[] =
        saved &&
        (url.searchParams.get("awaitingFeedback") !== "true" ||
          saved.awaitingFeedback)
          ? [
              {
                historyId,
                title: saved.recipe.title,
                savedAt: timestamp,
                cookedAt: saved.cookedAt,
                state: saved.state,
                awaitingFeedback: saved.awaitingFeedback,
                rating: saved.rating,
              },
            ]
          : [];
      await route.fulfill({ json: { items: summary, nextCursor: null } });
      return;
    }
    if (request.method() === "POST" && url.pathname === "/api/v1/recipes") {
      state.writes.push("save");
      state.saved = {
        etag: '"recipe-1"',
        data: {
          historyId,
          generatedRecipeId,
          recipe,
          savedAt: timestamp,
          cookedAt: null,
          state: "planned",
          awaitingFeedback: false,
          rating: null,
          note: null,
          feedbackRevision: 0,
        },
      };
    } else if (request.method() !== "GET") {
      expect(state.saved).not.toBeNull();
      expect(request.headers()["if-match"]).toBe(state.saved!.etag);
      if (url.pathname.endsWith("/cooked")) {
        state.writes.push("cook");
        Object.assign(state.saved!.data, {
          cookedAt: timestamp,
          state: "cooked",
          awaitingFeedback: true,
        });
      } else {
        state.writes.push("feedback");
        Object.assign(state.saved!.data, request.postDataJSON(), {
          awaitingFeedback: false,
          feedbackRevision: state.saved!.data.feedbackRevision + 1,
        });
      }
      state.saved!.etag = `"recipe-${state.writes.length}"`;
    }
    if (!state.saved) throw new Error("Missing synthetic recipe.");
    await route.fulfill({
      headers: { ETag: state.saved.etag },
      json: { data: state.saved.data },
    });
  });
  return { auth, state, recipe };
}

async function accessible(page: Page, info: TestInfo, state: string) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const width = await page.locator("html").evaluate((element) => ({
    content: element.scrollWidth,
    viewport: element.clientWidth,
  }));
  const report = info.outputPath(`axe-${state}.json`);
  await writeFile(
    report,
    JSON.stringify(
      {
        url: page.url(),
        viewport: page.viewportSize(),
        theme: await page.locator("html").getAttribute("data-theme"),
        engine: results.testEngine,
        environment: results.testEnvironment,
        timestamp: results.timestamp,
        width,
        violations: results.violations,
        incomplete: results.incomplete,
      },
      null,
      2,
    ),
  );
  await info.attach(`axe-${state}`, {
    path: report,
    contentType: "application/json",
  });
  if (state === "long-recipe" || state === "recipe-feedback") {
    const screenshot = info.outputPath(`screen-${state}.png`);
    await page.screenshot({ path: screenshot, fullPage: true });
    await info.attach(`screen-${state}`, {
      path: screenshot,
      contentType: "image/png",
    });
  }
  expect
    .soft(
      results.violations
        .filter(
          (item) => item.impact === "critical" || item.impact === "serious",
        )
        .map((item) => ({
          rule: item.id,
          impact: item.impact,
          elements: item.nodes.map((node) => ({
            target: node.target,
            reason: node.failureSummary,
          })),
        })),
      state,
    )
    .toEqual([]);
  expect
    .soft(width.content, `${state}: horizontal overflow`)
    .toBeLessThanOrEqual(width.viewport);
}

async function keyboardTo(page: Page, target: Locator) {
  await expect(target).toBeVisible();
  await expect(target).toBeEnabled();
  for (let index = 0; index < 100; index++) {
    if (await target.evaluate((element) => element === document.activeElement))
      return;
    await page.keyboard.press("Tab");
  }
  await expect(target).toBeFocused();
}

async function activate(page: Page, target: Locator) {
  if ((await target.getAttribute("role")) === "tab") {
    await keyboardTo(page, page.getByRole("tab", { selected: true }));
    for (let index = 0; index < 3; index++) {
      if (
        await target.evaluate((element) => element === document.activeElement)
      )
        break;
      await page.keyboard.press("ArrowRight");
    }
    await expect(target).toBeFocused();
  } else await keyboardTo(page, target);
  await page.keyboard.press("Enter");
}

async function type(page: Page, target: Locator, text: string) {
  await keyboardTo(page, target);
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.insertText(text);
}

async function onboarding(page: Page) {
  await keyboardTo(page, page.getByLabel("Spice preference", { exact: true }));
  await page.keyboard.press("Home");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Tab");
  await keyboardTo(
    page,
    page.getByLabel("Cooking experience", { exact: true }),
  );
  await page.keyboard.press("Home");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Tab");
  await type(
    page,
    page.getByLabel("Usual cooking time (minutes)", { exact: true }),
    "30",
  );
  await keyboardTo(
    page,
    page.getByLabel("I have reviewed my allergies", { exact: false }),
  );
  await page.keyboard.press("Space");
  await activate(
    page,
    page.getByRole("button", { name: "Save and open my kitchen" }),
  );
  await expect(
    page.getByRole("heading", {
      name: "Good cooking starts with what you have.",
    }),
  ).toBeVisible();
}

for (const theme of ["light", "dark"]) {
  test(`${theme}: required screens, long content and failed states have no serious accessibility violations`, async ({
    page,
  }, info) => {
    test.setTimeout(90_000);
    if (info.project.name === "phone")
      await page.setViewportSize({ width: 320, height: 844 });
    const { auth, state, recipe } = await kitchen(page, true, true);
    await page.goto(`/?scoutTheme=${theme}`);
    await expect(
      page.getByRole("heading", { name: "Let's make this kitchen yours." }),
    ).toBeVisible();
    await accessible(page, info, "onboarding");
    await page
      .getByRole("button", { name: "Save and open my kitchen" })
      .click();
    await expect(page.getByRole("alert")).toBeVisible();
    await accessible(page, info, "onboarding-errors");
    await onboarding(page);
    await expect(
      page.getByText("Your grocery list is empty.", { exact: false }),
    ).toBeVisible();
    await accessible(page, info, "empty-groceries");
    const ingredient = page.getByLabel("Ingredient name", { exact: true });
    await ingredient.fill("g".repeat(101));
    await page
      .getByRole("button", { name: "Add ingredient", exact: true })
      .click();
    await expect(ingredient).toBeFocused();
    await expect(ingredient).toHaveAttribute("aria-invalid", "true");
    await expect(ingredient).toHaveAccessibleDescription(/1 to 100 characters/);
    await accessible(page, info, "grocery-error");
    await ingredient.fill("");
    const nutrition = page.getByLabel("USDA ingredient name", { exact: true });
    await nutrition.fill("%");
    await page
      .getByRole("button", { name: "Look up calories", exact: true })
      .click();
    await expect(nutrition).toBeFocused();
    await expect(nutrition).toHaveAttribute("aria-invalid", "true");
    await expect(nutrition).toHaveAccessibleDescription(/without wildcards/);
    await accessible(page, info, "nutrition-error");
    await page.getByRole("tab", { name: "Recipe History" }).click();
    await expect(
      page.getByText("No recipes saved yet.", { exact: false }),
    ).toBeVisible();
    await accessible(page, info, "empty-history");
    await page.getByRole("tab", { name: "Sous Chef" }).click();
    await expect(
      page.getByRole("button", { name: "New Chat", exact: true }),
    ).toBeEnabled();
    const sidebar = page.getByRole("button", {
      name: "Conversations",
      exact: true,
    });
    if ((await sidebar.getAttribute("aria-expanded")) === "false")
      await sidebar.click();
    await accessible(page, info, "empty-conversations");
    await page.getByRole("button", { name: "New Chat", exact: true }).click();
    const input = page.getByRole("textbox", {
      name: "What would you like to cook?",
    });
    await expect(input).toBeEditable();
    await input.fill("Dinner");
    await input.press("Enter");
    await expect(
      page.getByRole("button", { name: "Save to Cook", exact: true }),
    ).toBeVisible();
    if ((await sidebar.getAttribute("aria-expanded")) === "false")
      await sidebar.click();
    await accessible(page, info, "long-recipe");
    await page
      .getByRole("button", { name: "Save to Cook", exact: true })
      .click();
    await expect(
      page
        .getByRole("status")
        .filter({ hasText: "Recipe saved to cook later." }),
    ).toBeVisible();
    await page.getByRole("tab", { name: "Recipe History" }).click();
    await page
      .getByRole("button", { name: `View ${recipe.title}`, exact: true })
      .click();
    await page
      .getByRole("button", { name: "I've cooked this", exact: true })
      .click();
    await expect(
      page.getByRole("radio", { name: "Liked", exact: true }),
    ).toBeVisible();
    await accessible(page, info, "recipe-feedback");
    await page.getByRole("radio", { name: "Fine", exact: true }).check();
    const notes = page.getByRole("textbox", {
      name: "Cooking notes (optional)",
    });
    await notes.fill("n".repeat(2001));
    await page
      .getByRole("button", { name: "Save rating", exact: true })
      .click();
    await expect(notes).toBeFocused();
    await expect(notes).toHaveAttribute("aria-invalid", "true");
    await accessible(page, info, "feedback-validation");
    await notes.fill("");
    await page
      .getByRole("button", { name: "Save rating", exact: true })
      .click();
    await expect(
      page
        .getByRole("status")
        .filter({ hasText: "Your rating and notes are saved." }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Back to Recipe History", exact: false })
      .click();
    state.listFailure = true;
    await page.getByRole("tab", { name: "Groceries", exact: true }).click();
    await page
      .getByRole("tab", { name: "Recipe History", exact: true })
      .click();
    await expect(page.getByRole("alert")).toHaveCount(2);
    await accessible(page, info, "failed-history");
    await page
      .getByRole("button", { name: "Cooking profile", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "A little more you. A better fit." }),
    ).toBeVisible();
    await accessible(page, info, "profile-editor");
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    auth.state.getFailure = { status: 401, html: true };
    await page
      .getByRole("button", { name: "Cooking profile", exact: true })
      .click();
    await expect(
      page.getByRole("link", { name: "Sign in", exact: true }),
    ).toBeVisible();
    await accessible(page, info, "expired-session");
  });
}

test("keyboard alone completes onboarding, groceries, recipe save/cooking and feedback", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const { auth, state, recipe } = await kitchen(page, true);
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Let's make this kitchen yours." }),
  ).toBeVisible();
  await onboarding(page);
  await expect(page.locator("#main-content")).toBeFocused();
  const ingredient = page.getByRole("textbox", {
    name: "Ingredient name",
    exact: true,
  });
  await type(page, ingredient, "Carrots");
  await expect(ingredient).toHaveCSS("outline-style", "solid");
  await expect(ingredient).toHaveCSS("outline-width", "3px");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(ingredient).toBeFocused();
  await activate(
    page,
    page.getByRole("button", { name: "Add ingredient", exact: true }),
  );
  await expect(
    page.getByRole("heading", { name: "Carrots", exact: true }),
  ).toBeVisible();
  await keyboardTo(
    page,
    page.getByRole("tab", { name: "Groceries", exact: true }),
  );
  await page.keyboard.press("ArrowRight");
  await expect(
    page.getByRole("tab", { name: "Sous Chef", exact: true }),
  ).toBeFocused();
  await activate(
    page,
    page.getByRole("button", { name: "New Chat", exact: true }),
  );
  await type(
    page,
    page.getByRole("textbox", { name: "What would you like to cook?" }),
    "Dinner",
  );
  await page.keyboard.press("Enter");
  await activate(
    page,
    page.getByRole("button", { name: "Save to Cook", exact: true }),
  );
  await expect(
    page.getByRole("status").filter({ hasText: "Recipe saved to cook later." }),
  ).toBeVisible();
  await activate(
    page,
    page.getByRole("tab", { name: "Recipe History", exact: true }),
  );
  await activate(
    page,
    page.getByRole("button", { name: `View ${recipe.title}`, exact: true }),
  );
  await activate(
    page,
    page.getByRole("button", { name: "I've cooked this", exact: true }),
  );
  await expect(
    page.getByRole("heading", { name: recipe.title, exact: true, level: 1 }),
  ).toBeFocused();
  const liked = page.getByRole("radio", { name: "Liked", exact: true });
  await keyboardTo(page, liked);
  await page.keyboard.press("Space");
  await expect(liked).toBeChecked();
  await expect(
    page.getByRole("radio", { name: "Fine", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("radio", { name: "Didn't Like", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("ArrowRight");
  await expect(
    page.getByRole("radio", { name: "Fine", exact: true }),
  ).toBeChecked();
  await page.keyboard.press("ArrowLeft");
  await expect(liked).toBeChecked();
  const notes = page.getByRole("textbox", { name: "Cooking notes (optional)" });
  await type(page, notes, "n".repeat(2001));
  await activate(
    page,
    page.getByRole("button", { name: "Save rating", exact: true }),
  );
  await expect(notes).toBeFocused();
  await expect(notes).toHaveAttribute("aria-invalid", "true");
  await expect(notes).toHaveAccessibleDescription(
    /Keep your note to 2,000 characters/,
  );
  expect(state.writes).not.toContain("feedback");
  await type(
    page,
    page.getByRole("textbox", { name: "Cooking notes (optional)" }),
    "Loved the lime.",
  );
  await activate(
    page,
    page.getByRole("button", { name: "Save rating", exact: true }),
  );
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Your rating and notes are saved." }),
  ).toBeVisible();
  await activate(
    page,
    page.getByRole("button", { name: "Back to Recipe History", exact: false }),
  );
  await expect(
    page.getByRole("heading", { name: "Recipe History", exact: true }),
  ).toBeFocused();
  expect(state.saved?.data.rating).toBe("liked");
  expect(state.saved?.data.note).toBe("Loved the lime.");
  expect(state.writes).toEqual(["grocery", "chat", "save", "cook", "feedback"]);
  expect(auth.writes).toHaveLength(1);
});

test("profile cancel and save restore focus to the opener", async ({
  page,
}) => {
  await kitchen(page);
  await page.goto("/");
  const profile = page.getByRole("button", {
    name: "Cooking profile",
    exact: true,
  });
  await activate(page, profile);
  await activate(
    page,
    page.getByRole("button", { name: "Cancel", exact: true }),
  );
  await expect(profile).toBeFocused();
  await activate(page, profile);
  await type(
    page,
    page.getByLabel("Flavors and foods you like", { exact: true }),
    "lime",
  );
  await activate(
    page,
    page.getByRole("button", { name: "Save profile", exact: true }),
  );
  await expect(profile).toBeFocused();
});

test("grocery removal restores focus after cancelling or deleting the focused row", async ({
  page,
}) => {
  const { state } = await kitchen(page);
  state.groceries = [
    { groceryId: historyId, name: "Carrots", quantity: 1, unit: "g" },
  ];
  await page.goto("/");
  const ingredient = page.getByLabel("Ingredient name", { exact: true });
  await type(page, ingredient, "i".repeat(101));
  await activate(
    page,
    page.getByRole("button", { name: "Add ingredient", exact: true }),
  );
  await expect(ingredient).toHaveAttribute("aria-invalid", "true");
  const remove = page.getByRole("button", {
    name: "Delete Carrots",
    exact: true,
  });
  await activate(page, remove);
  const cancel = page.getByRole("button", { name: "Cancel", exact: true });
  await expect(cancel).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(remove).toBeFocused();
  const decrease = page.getByRole("button", {
    name: "Decrease Carrots",
    exact: true,
  });
  await activate(page, decrease);
  await expect(cancel).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(decrease).toBeFocused();
  await activate(page, remove);
  await activate(
    page,
    page.getByRole("button", { name: "Confirm removal", exact: true }),
  );
  await expect(ingredient).toBeFocused();
  await expect(ingredient).toHaveAttribute("aria-invalid", "false");
  await expect(ingredient).toHaveAttribute("aria-describedby", "grocery-help");
  expect(state.groceries).toEqual([]);
});

test("explicit chat selection focuses its transcript without stealing focus on tab restoration", async ({
  page,
}) => {
  await kitchen(page);
  await page.goto("/");
  await activate(
    page,
    page.getByRole("tab", { name: "Sous Chef", exact: true }),
  );
  await activate(
    page,
    page.getByRole("button", { name: "New Chat", exact: true }),
  );
  await expect(
    page.getByRole("heading", { name: "New chat", exact: true }),
  ).toBeFocused();
  const sidebar = page.getByRole("button", {
    name: "Conversations",
    exact: true,
  });
  if ((await sidebar.getAttribute("aria-expanded")) === "false")
    await activate(page, sidebar);
  const selectedChat = page
    .getByRole("navigation", { name: "Conversations", exact: true })
    .getByRole("button", { name: "New chat", exact: false });
  await activate(page, selectedChat);
  await expect(
    page.getByRole("heading", { name: "New chat", exact: true }),
  ).toBeFocused();
  await activate(
    page,
    page.getByRole("tab", { name: "Groceries", exact: true }),
  );
  await page.keyboard.press("ArrowRight");
  await expect(
    page.getByRole("heading", { name: "New chat", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("tab", { name: "Sous Chef", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(
    page.getByRole("tab", { name: "Recipe History", exact: true }),
  ).toBeFocused();
});

test("loading and failed profile states expose valid, announced recovery controls", async ({
  page,
}, info) => {
  const { auth } = await kitchen(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/me", async (route) => {
    await gate;
    await route.fallback();
  });
  try {
    await page.goto("/");
    await expect(page.getByRole("status")).toContainText(
      "Loading your profile",
    );
    await accessible(page, info, "loading-profile");
    auth.state.getFailure = { status: 503 };
  } finally {
    release();
  }
  await expect(page.getByRole("alert")).toContainText(
    "Your kitchen could not be loaded.",
  );
  await accessible(page, info, "failed-profile");
  auth.state.getFailure = null;
  await activate(
    page,
    page.getByRole("button", { name: "Try again", exact: true }),
  );
  await expect(
    page.getByLabel("Ingredient name", { exact: true }),
  ).toBeEnabled();
});
