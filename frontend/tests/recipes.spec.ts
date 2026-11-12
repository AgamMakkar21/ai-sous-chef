import { expect, test, type Page } from "@playwright/test";
import type { Turn } from "../src/conversations.ts";
import { isStructuredRecipe, nutrientLabels } from "../src/recipes.ts";
import { backend } from "./backend.ts";
import { recipeFixture } from "./recipe-fixture.ts";

const id = "11111111-1111-4111-8111-111111111111";
const timestamp = "2026-11-09T12:00:00.000000Z";
const requestText = "Dinner with rice";

function recipeTurn(): Turn {
  return {
    turnId: crypto.randomUUID(),
    conversationId: id,
    turnIndex: 0,
    status: "completed",
    messages: [
      {
        messageId: crypto.randomUUID(),
        role: "user",
        content: requestText,
        timestamp,
      },
      {
        messageId: crypto.randomUUID(),
        role: "assistant",
        content: "private-model-summary-not-for-rendering",
        timestamp,
      },
    ],
    generatedRecipeId: crypto.randomUUID(),
    recipe: recipeFixture(),
  };
}

async function recipeBackend(page: Page, initial: unknown[] = []) {
  await backend(page);
  const state = {
    turns: initial,
    result: recipeTurn() as unknown,
    mode: "send" as "send" | "poll",
    writes: [] as string[],
    bodies: [] as unknown[],
  };
  const operationId = crypto.randomUUID();
  await page.route("**/api/v1/conversations**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "POST") {
      state.writes.push(url.pathname);
      state.bodies.push(route.request().postDataJSON());
      if (state.mode === "send") {
        state.turns = [state.result];
        await route.fulfill({ json: { data: state.result } });
      } else {
        const pending = recipeTurn();
        pending.status = "pending";
        pending.messages = pending.messages.slice(0, 1);
        pending.recipe = null;
        pending.generatedRecipeId = null;
        await route.fulfill({
          status: 202,
          json: {
            data: {
              operationId,
              conversationId: id,
              turnId: pending.turnId,
              status: "pending",
              failureCode: null,
              turn: pending,
            },
          },
        });
      }
    } else if (url.pathname.includes("/operations/")) {
      const completed = state.result;
      if (
        typeof completed !== "object" ||
        completed === null ||
        !("turnId" in completed)
      )
        throw new Error("Invalid test result.");
      state.turns = [completed];
      await route.fulfill({
        json: {
          data: {
            operationId,
            conversationId: id,
            turnId: completed.turnId,
            status: "completed",
            failureCode: null,
            turn: completed,
          },
        },
      });
    } else if (url.pathname.endsWith("/turns")) {
      await route.fulfill({ json: { items: state.turns, nextCursor: null } });
    } else {
      await route.fulfill({
        json: {
          items: [
            {
              conversationId: id,
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
  return state;
}

async function open(page: Page) {
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
    page.getByRole("textbox", { name: "What would you like to cook?" }),
  ).toBeVisible();
}

const card = (page: Page) =>
  page.getByRole("region", { name: "Simple carrot rice", exact: true });

test("recipes with more than nine USDA sources retain their estimates and conversion evidence", async ({
  page,
}) => {
  const turn = recipeTurn();
  const recipe = turn.recipe!;
  recipe.nutrition.mappings[0].foodDescription = "Rice, white, long-grain, raw";
  recipe.nutrition.mappings[0].approximateMatch = true;
  recipe.nutrition.mappings[0].conversionBasis = "USDA portion: 1 cup = 185 g";
  for (let index = 0; index < 10; index++) {
    const lineId = `extra${index}`;
    const source = {
      ...recipe.nutrition.sources[0],
      sourceId: `usda:fdc:${7000000 + index}`,
    };
    recipe.ingredients.push({ ...recipe.ingredients[0], lineId });
    recipe.instructions[1].ingredientLineIds.push(lineId);
    recipe.nutrition.sources.push(source);
    recipe.sources.push(source);
    recipe.nutrition.mappings.push({
      ...recipe.nutrition.mappings[0],
      ingredientLineId: lineId,
      sourceId: source.sourceId,
    });
  }
  expect(isStructuredRecipe(recipe)).toBe(true);
  await recipeBackend(page, [turn]);
  await open(page);
  await expect(card(page)).toBeVisible();
  await expect(card(page)).toContainText(
    "Estimated match: Rice, white, long-grain, raw",
  );
  await expect(card(page)).toContainText("USDA portion: 1 cup = 185 g");
});

test("contract recipe fields, shopping shortfall, citations and lifecycle actions render in saved history", async ({
  page,
}, testInfo) => {
  await recipeBackend(page, [recipeTurn()]);
  await open(page);
  const recipe = card(page);
  await expect(recipe).toBeVisible();
  await expect(recipe.getByText("25 min", { exact: true })).toBeVisible();
  await expect(recipe.getByText("beginner", { exact: true })).toBeVisible();
  await expect(recipe.getByText("2", { exact: true })).toBeVisible();
  await expect(
    recipe.getByRole("heading", { name: "Why this fits" }),
  ).toBeVisible();
  await expect(
    recipe.getByRole("region", { name: "Already have" }),
  ).toContainText("rice");
  const shopping = recipe.getByRole("region", { name: "Need to buy" });
  await expect(shopping).toContainText("carrot");
  await expect(shopping).toContainText("100 g");
  await expect(shopping).toContainText("Recipe needs 150 g in total");
  await expect(
    recipe.getByRole("region", { name: "Check your pantry" }),
  ).toContainText("olive oil");
  const steps = recipe
    .getByRole("region", { name: "Cooking steps" })
    .getByRole("listitem");
  await expect(steps).toHaveText([
    "Cook the rice.",
    "Soften the carrot in olive oil, then mix with the rice.",
  ]);
  await expect(
    recipe.getByText("Per serving / 2 servings", { exact: true }),
  ).toBeVisible();
  for (const label of Object.values(nutrientLabels))
    await expect(
      recipe.getByRole("term").filter({ hasText: new RegExp(`^${label}$`) }),
    ).toBeVisible();
  await expect(recipe.getByText("0 mg", { exact: true })).toBeVisible();
  await expect(
    recipe.getByText("Informational estimate, not medical advice.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    recipe.getByRole("region", { name: "Grounded recipe sources" }),
  ).toContainText("Beginner preparation, 25 minutes.");
  await expect(
    recipe.getByRole("region", { name: "Creative adaptations" }),
  ).toContainText("Generated changes, not claims");
  await expect(
    recipe.getByRole("link", { name: "fixture-rice", exact: false }),
  ).toHaveAttribute("href", "https://recipes.example.invalid/synthetic-rice");
  await expect(
    recipe.getByRole("link", { name: "USDA", exact: false }),
  ).toHaveAttribute("rel", "noopener noreferrer");
  for (const label of ["Cook This", "Save to Cook"]) {
    const action = recipe.getByRole("button", { name: label, exact: true });
    await expect(action).toBeEnabled();
  }
  await expect(
    page.getByText("private-model-summary-not-for-rendering"),
  ).toHaveCount(0);
  await page.reload();
  await expect(card(page)).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("recipe.png"),
    fullPage: true,
  });
});

for (const mode of ["send", "poll"] as const) {
  test(`completed ${mode} response renders a card, then reloads it from history`, async ({
    page,
  }) => {
    const state = await recipeBackend(page);
    state.mode = mode;
    await open(page);
    await page
      .getByRole("textbox", { name: "What would you like to cook?" })
      .fill(requestText);
    await page
      .getByRole("button", { name: "Send request", exact: true })
      .click();
    await expect(card(page)).toBeVisible();
    await expect(card(page)).toHaveCount(1);
    await page.reload();
    await expect(card(page)).toBeVisible();
    expect(state.writes).toEqual([`/api/v1/conversations/${id}/turns`]);
  });
}

for (const [format, content] of [
  ["plain", "Chicken dinner"],
  [
    "measured",
    "Dinner with 200 g CHICKEN BREASTS (boneless, skinless) and rice",
  ],
  ["list", "Make dinner:\n- chicken-breast, diced\n- 1/2 cup rice"],
  [
    "json",
    'Make dinner: [{"name":"chicken breast","quantity":0.2,"unit":"kg"}]',
  ],
] as const) {
  test(`${format} ingredient input sends unchanged and displays the returned chicken recipe`, async ({
    page,
  }) => {
    const state = await recipeBackend(page);
    const recipe = recipeFixture();
    recipe.title = "Chicken breast and rice dinner";
    recipe.ingredients.push({
      lineId: "chicken",
      name: "chicken breast",
      quantity: 200,
      unit: "g",
      inventoryStatus: "unknown",
    });
    recipe.instructions.push({
      step: 3,
      text: "Cook the chicken breast to an internal temperature of 165 F, measured with a food thermometer.",
      ingredientLineIds: ["chicken"],
    });
    recipe.nutrition = {
      ...recipe.nutrition,
      status: "unavailable",
      nutrients: [],
      mappings: [],
      sources: [],
      coverage: [],
      unknownComponents: [],
    };
    recipe.sources = recipe.sources.filter((source) => source.kind !== "usda");
    const turn = { ...recipeTurn(), recipe };
    turn.messages[0]!.content = content;
    state.result = turn;
    await open(page);
    await page
      .getByRole("textbox", { name: "What would you like to cook?" })
      .fill(content);
    await page
      .getByRole("button", { name: "Send request", exact: true })
      .click();
    const chicken = page.getByRole("region", {
      name: recipe.title,
      exact: true,
    });
    await expect(chicken).toBeVisible();
    await expect(chicken).toContainText("165 F");
    expect(state.bodies).toMatchObject([{ content }]);
    await page.reload();
    await expect(chicken).toBeVisible();
    expect(state.writes).toHaveLength(1);
  });
}

test("partial nutrients identify unknown ingredients; unavailable values never become zero", async ({
  page,
}) => {
  const recipe = recipeFixture();
  recipe.nutrition.status = "partial";
  recipe.nutrition.nutrients = [{ name: "energy", amount: 125, unit: "kcal" }];
  recipe.nutrition.mappings = recipe.nutrition.mappings.filter(
    (item) => item.ingredientLineId === "rice",
  );
  recipe.nutrition.coverage = (
    Object.keys(nutrientLabels) as (keyof typeof nutrientLabels)[]
  ).map((name) => ({
    name,
    status: name === "energy" ? "partial" : "unavailable",
    wholeRecipeAmount: name === "energy" ? 250 : null,
    coveredIngredientLineIds: name === "energy" ? ["rice"] : [],
    unknownIngredientLineIds:
      name === "energy" ? ["carrot", "oil"] : ["rice", "carrot", "oil"],
  }));
  recipe.nutrition.unknownComponents = ["rice", "carrot", "oil"].map(
    (ingredientLineId) => ({
      ingredientLineId,
      reason: "missing_nutrients",
      unknownNutrients: (
        Object.keys(nutrientLabels) as (keyof typeof nutrientLabels)[]
      ).filter((name) => ingredientLineId !== "rice" || name !== "energy"),
    }),
  );
  expect(isStructuredRecipe(recipe)).toBe(true);
  const turn = { ...recipeTurn(), recipe };
  const state = await recipeBackend(page, [turn]);
  await open(page);
  await expect(
    card(page).getByText("Partial: 125 kcal", { exact: true }),
  ).toBeVisible();
  await expect(
    card(page).getByText("Unavailable", { exact: true }),
  ).toHaveCount(5);
  await expect(
    card(page).getByText("Calories unavailable for: carrot, olive oil.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    card(page)
      .getByRole("region", { name: "Nutrition estimates" })
      .getByText("0 g", { exact: true }),
  ).toHaveCount(0);
  recipe.nutrition = {
    ...recipe.nutrition,
    status: "unavailable",
    nutrients: [],
    sources: [],
    mappings: [],
    coverage: [],
    unknownComponents: [],
  };
  recipe.sources = recipe.sources.filter((source) => source.kind !== "usda");
  state.turns = [{ ...turn, recipe }];
  await page.reload();
  await expect(
    card(page).getByText("Unavailable", { exact: true }),
  ).toHaveCount(6);
  await expect(card(page)).toContainText("Missing values are not zero.");
  await expect(
    card(page).getByRole("link", { name: "USDA", exact: false }),
  ).toHaveCount(0);
});

test("unknown quantities, empty shopping lists and whole-recipe basis stay explicit", async ({
  page,
}) => {
  const recipe = recipeFixture();
  recipe.ingredients.forEach((line) => {
    line.inventoryStatus = "unknown";
    line.quantity = null;
    line.unit = null;
  });
  recipe.shoppingItems = [];
  recipe.nutrition = {
    ...recipe.nutrition,
    status: "unavailable",
    servingBasis: "whole_recipe",
    nutrients: [],
    mappings: [],
    sources: [],
  };
  recipe.sources = recipe.sources.filter((source) => source.kind !== "usda");
  await recipeBackend(page, [{ ...recipeTurn(), recipe }]);
  await open(page);
  await expect(card(page)).toContainText("None confirmed in your groceries.");
  await expect(card(page)).toContainText("Nothing to buy.");
  await expect(
    card(page).getByText("Quantity unavailable", { exact: true }),
  ).toHaveCount(3);
  await expect(card(page)).toContainText("Whole recipe / 2 servings");
});

test("text is escaped, unsafe links are inert and long content works at 320px with keyboard links", async ({
  page,
}) => {
  const recipe = recipeFixture();
  const attack = '<img src=x onerror="window.recipeAttack=true">';
  recipe.fitReasons = [attack];
  recipe.ingredients[0]!.name = "x".repeat(100);
  recipe.sources[0]!.reference = "javascript:window.recipeAttack=true";
  recipe.webGrounding = {
    claims: [
      { text: attack, sourceUrl: "https://guidance.example.invalid/handling" },
    ],
    citations: [
      {
        title: "Handling guidance",
        url: "https://guidance.example.invalid/handling",
      },
    ],
    retrievedAt: timestamp,
  };
  await recipeBackend(page, [{ ...recipeTurn(), recipe }]);
  await page.setViewportSize({ width: 320, height: 844 });
  await open(page);
  await expect(card(page).getByText(attack, { exact: true })).toHaveCount(1);
  await expect(card(page).locator("img, script, iframe")).toHaveCount(0);
  await expect(card(page).locator('a[href^="javascript:"]')).toHaveCount(0);
  await expect(card(page)).toContainText("fixture-rice (link unavailable)");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const link = card(page).getByRole("link", {
    name: "Handling guidance",
    exact: false,
  });
  await link.focus();
  await expect(link).toBeFocused();
  await page
    .context()
    .route("https://guidance.example.invalid/handling", (route) =>
      route.fulfill({ body: "Synthetic guidance" }),
    );
  const popupPromise = page.waitForEvent("popup");
  await link.press("Enter");
  const popup = await popupPromise;
  await popup.waitForLoadState();
  expect(popup.url()).toBe("https://guidance.example.invalid/handling");
  expect(await popup.evaluate(() => window.opener)).toBeNull();
  await popup.close();
});

for (const invalid of [
  "failed",
  "pending",
  "outcomeUnknown",
  "no-id",
  "no-recipe",
  "bad-shape",
  "bad-reference",
  "bad-nutrition",
] as const) {
  test(`rejects ${invalid} candidates without exposing content or saving`, async ({
    page,
  }) => {
    const turn = recipeTurn();
    if (
      invalid === "failed" ||
      invalid === "pending" ||
      invalid === "outcomeUnknown"
    ) {
      turn.status = invalid;
      turn.messages = turn.messages.slice(0, 1);
    } else if (invalid === "no-id") turn.generatedRecipeId = null;
    else if (invalid === "no-recipe") turn.recipe = null;
    else if (invalid === "bad-shape") turn.recipe!.servings = 0;
    else if (invalid === "bad-reference")
      turn.recipe!.shoppingItems[0]!.ingredientLineId = "not-in-recipe";
    else turn.recipe!.nutrition.nutrients[0]!.amount = -1;
    const state = await recipeBackend(page, [turn]);
    await open(page);
    await expect(
      page.getByText("Messages could not be loaded.", { exact: false }),
    ).toBeVisible();
    await expect(card(page)).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: /Cook This|Save to Cook/ }),
    ).toHaveCount(0);
    expect(state.writes).toHaveLength(0);
  });
}

test("malformed live candidate follows composer recovery rather than rendering success", async ({
  page,
}) => {
  const state = await recipeBackend(page);
  const turn = recipeTurn();
  turn.recipe!.instructions = [];
  state.result = turn;
  await open(page);
  await page
    .getByRole("textbox", { name: "What would you like to cook?" })
    .fill(requestText);
  await page.getByRole("button", { name: "Send request", exact: true }).click();
  await expect(
    page.getByText("I couldn't complete that recipe request", { exact: false }),
  ).toBeVisible();
  await expect(card(page)).toHaveCount(0);
  await expect(
    page.getByText("private-model-summary-not-for-rendering"),
  ).toHaveCount(0);
  await expect(
    page.getByRole("textbox", { name: "What would you like to cook?" }),
  ).toHaveValue(requestText);
});
