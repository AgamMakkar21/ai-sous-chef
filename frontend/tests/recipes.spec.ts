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

for (const unit of ["fl oz", "qt", "gal", "dozen"]) {
  test(`recipe unit ${unit} renders after conversation reload`, async ({
    page,
  }) => {
    const turn = recipeTurn();
    const recipe = turn.recipe!;
    for (const line of recipe.ingredients) line.unit = unit;
    for (const item of recipe.shoppingItems) item.unit = unit;
    for (const mapping of recipe.nutrition.mappings) mapping.unit = unit;
    await recipeBackend(page, [turn]);
    await open(page);
    const amount = `${recipe.ingredients[0]!.quantity} ${unit}`;
    await expect(card(page)).toContainText(amount);
    await page.reload();
    await expect(card(page)).toContainText(amount);
  });
}

test("time and skill notes stay visible on a completed recipe after reload", async ({
  page,
}) => {
  const turn = recipeTurn();
  const recipe = turn.recipe!;
  recipe.totalMinutes = 45;
  recipe.skill = "advanced";
  recipe.fitNotes = [
    "This recipe takes about 45 minutes and may not fit your 30-minute time budget.",
    "This recipe is rated advanced and may be above your beginner cooking skill level.",
  ];
  expect(isStructuredRecipe(recipe)).toBe(true);
  for (const invalid of [
    null,
    "not a list",
    [""],
    ["a", "b", "c"],
    ["x".repeat(241)],
  ]) {
    expect(isStructuredRecipe({ ...recipe, fitNotes: invalid })).toBe(false);
  }
  await recipeBackend(page, [turn]);
  await open(page);
  const notes = card(page).getByRole("region", {
    name: "Recipe notes",
    exact: true,
  });
  for (const note of recipe.fitNotes) await expect(notes).toContainText(note);
  await expect(card(page)).toContainText("45 min");
  await expect(card(page)).toContainText("advanced");
  await page.reload();
  for (const note of recipe.fitNotes) await expect(notes).toContainText(note);
  await expect(
    page.getByText("I couldn't complete a recipe", { exact: false }),
  ).toHaveCount(0);
});

test("AI recipe attribution survives history reload without claiming curated evidence", async ({
  page,
}) => {
  const turn = recipeTurn();
  const recipe = turn.recipe!;
  const curated = recipe.sources.find(
    (source) => source.kind === "curated_recipe",
  )!;
  recipe.generationSource = "ai_knowledge";
  recipe.sourceIds = [];
  recipe.sourceClaims = [];
  recipe.sources = recipe.sources.filter(
    (source) => source.kind !== "curated_recipe",
  );
  recipe.webGrounding = null;
  expect(isStructuredRecipe(recipe)).toBe(true);
  const falseAttribution = structuredClone(recipe);
  falseAttribution.sourceIds = [curated.sourceId];
  falseAttribution.sources.push(curated);
  expect(isStructuredRecipe(falseAttribution)).toBe(false);
  await recipeBackend(page, [turn]);
  await open(page);
  await expect(card(page)).toContainText("AI-knowledge recipe");
  await expect(card(page)).toContainText("not externally verified");
  await page.reload();
  await expect(card(page)).toContainText("AI-knowledge recipe");
  await card(page).getByText("Nutrition details", { exact: true }).click();
  await expect(
    card(page).getByRole("link", {
      name: "rice (opens in a new tab)",
      exact: true,
    }),
  ).toHaveAttribute("href", recipe.nutrition.sources[0].reference);
});

for (const backup of ["web_iq", "ai_knowledge"] as const) {
  test(`${backup} fills missing nutrients alongside USDA ingredient breakdowns`, async ({
    page,
  }) => {
    const turn = recipeTurn();
    const recipe = turn.recipe!;
    const n = recipe.nutrition;
    const ids = recipe.ingredients.map((item) => item.lineId);
    n.mappings = n.mappings.slice(0, 1);
    n.nutrients.find((item) => item.name === "energy")!.amount = 150;
    n.ingredientCalories = ids.map((id, index) => ({
      ingredientLineId: id,
      amount: 100,
      source: index === 0 ? "usda_mcp" : backup,
      reference:
        index === 0
          ? n.sources[0].reference
          : backup === "web_iq"
            ? "https://nutrition.example.invalid/portion"
            : null,
      basis:
        "Calculation evidence: 50 kcal per 100 g multiplied by 2 = 100 kcal.",
    }));
    n.coverage = n.nutrients.map((item) => ({
      name: item.name,
      status: "known",
      wholeRecipeAmount: item.amount * recipe.servings,
      coveredIngredientLineIds: ids,
      unknownIngredientLineIds: [],
    }));
    n.nutrientEstimates = ids.slice(1).map((id) => ({
      ingredientLineId: id,
      nutrients: n.nutrients
        .filter((item) => item.name !== "energy")
        .map((item) => ({ ...item, amount: item.amount / 3 })),
      source: backup,
      reference:
        backup === "web_iq"
          ? "https://nutrition.example.invalid/portion"
          : null,
      basis:
        "Calculation evidence: multiply nutrient density by portion weight.",
    }));
    n.mappings[0].nutrients = n.nutrients.map((item) => ({
      ...item,
      amount:
        item.name === "energy"
          ? 100
          : item.amount * recipe.servings - (item.amount * 2) / 3,
    }));
    if (backup === "ai_knowledge") {
      const protein = n.nutrientEstimates[0].nutrients.shift()!;
      n.nutrientEstimates.push({
        ...n.nutrientEstimates[0],
        nutrients: [protein],
        source: "web_iq",
        reference: "https://nutrition.example.invalid/protein",
      });
      n.mappings[0].nutrients = n.mappings[0].nutrients.filter(
        (item) => item.name !== "energy",
      );
      n.ingredientCalories[0].source = "ai_knowledge";
      n.ingredientCalories[0].reference = null;
    }
    n.unknownComponents = [];
    expect(isStructuredRecipe(recipe)).toBe(true);
    const corrupted = structuredClone(recipe);
    corrupted.nutrition.nutrientEstimates![0].nutrients.pop();
    expect(isStructuredRecipe(corrupted)).toBe(false);
    const wrongTotal = structuredClone(recipe);
    wrongTotal.nutrition.nutrients.find(
      (item) => item.name === "protein",
    )!.amount += 1;
    expect(isStructuredRecipe(wrongTotal)).toBe(false);
    const wrongIngredient = structuredClone(recipe);
    wrongIngredient.nutrition.mappings[0].nutrients![1].amount += 1;
    expect(isStructuredRecipe(wrongIngredient)).toBe(false);
    const inventedCitation = structuredClone(recipe);
    inventedCitation.nutrition.nutrientEstimates![0].reference =
      backup === "ai_knowledge" ? "https://invented.example.invalid" : null;
    expect(isStructuredRecipe(inventedCitation)).toBe(false);
    await recipeBackend(page, [turn]);
    await open(page);
    const breakdown = card(page).getByRole("table", {
      name: "Nutrition by ingredient (whole recipe)",
      includeHidden: true,
    });
    await expect(breakdown).toBeHidden();
    await expect(card(page).getByText(/Incomplete estimate/)).toHaveCount(0);
    await expect(card(page).getByText(/Partial:/)).toHaveCount(0);
    await card(page).getByText("Nutrition details", { exact: true }).click();
    await expect(breakdown).toBeVisible();
    await expect(breakdown.getByRole("columnheader")).toHaveText([
      "Ingredient",
      ...Object.values(nutrientLabels),
    ]);
    await expect(breakdown.getByRole("row")).toHaveCount(ids.length + 1);
    const rice = breakdown.getByRole("row").filter({
      has: page.getByRole("rowheader", { name: /^rice/ }),
    });
    await expect(rice.getByRole("cell").nth(1)).toHaveText("6.67 g");
    await expect(rice.getByRole("cell").nth(5)).toHaveText("0 mg");
    if (backup === "ai_knowledge")
      await expect(rice.getByRole("cell").nth(0)).toHaveText(
        "100 kcalAI estimate",
      );
    await expect(
      rice.getByRole("link", {
        name: "rice (opens in a new tab)",
        exact: true,
      }),
    ).toHaveAttribute("href", n.sources[0].reference);
    const carrot = breakdown.getByRole("row").filter({
      has: page.getByRole("rowheader", { name: /^carrot/ }),
    });
    await expect(carrot.getByRole("cell").nth(0)).toContainText("100 kcal");
    await expect(carrot.getByRole("cell").nth(0)).toContainText(
      backup === "ai_knowledge" ? "AI estimate" : "Web estimate",
    );
    if (backup === "ai_knowledge") {
      await expect(carrot.getByRole("cell").nth(1)).toContainText(
        "Web estimate",
      );
      await expect(carrot.getByRole("cell").nth(2)).toContainText(
        "AI estimate",
      );
    }
    await expect(card(page)).not.toContainText("Calculation evidence");
    await expect(
      card(page).getByRole("heading", { name: "Nutrition sources" }),
    ).toHaveCount(0);
    await page.reload();
    await expect(breakdown).toBeHidden();
    await card(page).getByText("Nutrition details", { exact: true }).click();
    await expect(carrot.getByRole("cell").nth(0)).toContainText("100 kcal");
    await expect(card(page)).not.toContainText("Calculation evidence");
  });
}

test("thirty ingredients retain disjoint web, AI and residual repair nutrient records", () => {
  const recipe = recipeTurn().recipe!;
  const original = recipe.ingredients;
  recipe.ingredients = Array.from({ length: 30 }, (_, index) => ({
    ...original[index % original.length],
    lineId: index < original.length ? original[index].lineId : `food${index}`,
  }));
  const ids = recipe.ingredients.map((item) => item.lineId);
  recipe.instructions = [
    {
      step: 1,
      text: "Combine and cook the declared ingredients.",
      ingredientLineIds: ids,
    },
  ];
  recipe.shoppingItems = recipe.ingredients
    .filter((item) => item.inventoryStatus === "missing")
    .map((item) => ({
      ingredientLineId: item.lineId,
      quantity: item.quantity,
      unit: item.unit,
    }));
  const n = recipe.nutrition;
  n.mappings = [];
  n.sources = [];
  recipe.generationSource = "ai_knowledge";
  recipe.sourceIds = [];
  recipe.sourceClaims = [];
  recipe.sources = [];
  n.ingredientCalories = ids.map((ingredientLineId) => ({
    ingredientLineId,
    amount: 10,
    source: "ai_knowledge",
    reference: null,
    basis: "Synthetic AI portion estimate.",
  }));
  n.nutrients = n.nutrients.map((item) => ({
    ...item,
    amount: (item.name === "energy" ? 300 : 90) / recipe.servings,
  }));
  n.coverage = n.nutrients.map((item) => ({
    name: item.name,
    status: "known",
    wholeRecipeAmount: item.amount * recipe.servings,
    coveredIngredientLineIds: ids,
    unknownIngredientLineIds: [],
  }));
  n.unknownComponents = [];
  n.nutrientEstimates = ids.flatMap((ingredientLineId) => [
    {
      ingredientLineId,
      source: "web_iq" as const,
      reference: "https://nutrition.example.invalid/protein",
      basis: "Synthetic cited portion protein.",
      nutrients: [{ name: "protein" as const, amount: 3, unit: "g" }],
    },
    {
      ingredientLineId,
      source: "ai_knowledge" as const,
      reference: null,
      basis: "Synthetic AI portion carbohydrate.",
      nutrients: [{ name: "carbohydrate" as const, amount: 3, unit: "g" }],
    },
    {
      ingredientLineId,
      source: "ai_knowledge" as const,
      reference: null,
      basis: "Synthetic AI estimates for remaining portion nutrients.",
      nutrients: n.nutrients
        .filter(
          (item) =>
            item.name !== "energy" &&
            item.name !== "protein" &&
            item.name !== "carbohydrate",
        )
        .map((item) => ({ ...item, amount: 3 })),
    },
  ]);
  expect(isStructuredRecipe(recipe)).toBe(true);
  n.nutrientEstimates[1].nutrients.push({
    name: "protein",
    amount: 3,
    unit: "g",
  });
  expect(isStructuredRecipe(recipe)).toBe(false);
});

test("mixed calorie sources are labeled per ingredient and survive history reload", async ({
  page,
}) => {
  const turn = recipeTurn();
  const recipe = turn.recipe!;
  const n = recipe.nutrition;
  const ids = recipe.ingredients.map((item) => item.lineId);
  n.status = "partial";
  n.mappings = n.mappings.slice(0, 1);
  n.nutrients.find((item) => item.name === "energy")!.amount = 90;
  n.ingredientCalories = [
    {
      ingredientLineId: ids[0],
      amount: 100,
      source: "usda_mcp",
      reference: n.sources[0].reference,
      basis: "Synthetic USDA portion.",
    },
    {
      ingredientLineId: ids[1],
      amount: 50,
      source: "web_iq",
      reference: "https://nutrition.example.invalid/carrot",
      basis: "Synthetic web calorie density scaled to this portion.",
    },
    {
      ingredientLineId: ids[2],
      amount: 30,
      source: "ai_knowledge",
      reference: null,
      basis: "Approximate portion from AI knowledge.",
    },
  ];
  n.coverage = Object.entries(nutrientLabels).map(([name]) => ({
    name: name as keyof typeof nutrientLabels,
    status: name === "energy" ? "known" : "partial",
    wholeRecipeAmount: name === "energy" ? 180 : 10,
    coveredIngredientLineIds: name === "energy" ? ids : [ids[0]],
    unknownIngredientLineIds: name === "energy" ? [] : ids.slice(1),
  }));
  n.unknownComponents = ids.slice(1).map((id) => ({
    ingredientLineId: id,
    reason: "service_unavailable",
    unknownNutrients: ["protein", "carbohydrate", "fat", "fiber", "sodium"],
  }));
  expect(isStructuredRecipe(recipe)).toBe(true);
  const corrupted = structuredClone(recipe);
  corrupted.nutrition.ingredientCalories![2].reference =
    "https://invented.example.invalid";
  expect(isStructuredRecipe(corrupted)).toBe(false);
  await recipeBackend(page, [turn]);
  await open(page);
  await expect(card(page).getByText("90 kcal", { exact: true })).toBeVisible();
  await expect(card(page).getByText(/not USDA-verified/)).toHaveCount(0);
  await card(page).getByText("Nutrition details", { exact: true }).click();
  const table = card(page).getByRole("table");
  for (const source of ["USDA", "AI estimate"])
    await expect(
      table.getByText(source, { exact: true }).first(),
    ).toBeVisible();
  await expect(
    table.getByRole("link", {
      name: "Web estimate (opens in a new tab)",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    table.getByRole("link", {
      name: "rice (opens in a new tab)",
      exact: true,
    }),
  ).toHaveAttribute("href", n.sources[0].reference);
  await expect(
    table.getByRole("link", {
      name: "carrot (opens in a new tab)",
      exact: true,
    }),
  ).toHaveAttribute("href", "https://nutrition.example.invalid/carrot");
  const oil = table.getByRole("row").filter({
    has: page.getByRole("rowheader", { name: "olive oil", exact: true }),
  });
  await expect(oil.getByRole("cell").nth(0)).toHaveText("30 kcalAI estimate");
  await expect(oil.getByRole("cell").nth(1)).toHaveText("Unavailable");
  await page.reload();
  await card(page).getByText("Nutrition details", { exact: true }).click();
  await expect(oil.getByRole("cell").nth(0)).toHaveText("30 kcalAI estimate");
});

test("assumed quantity is disclosed with its source without changing the original ingredient", async ({
  page,
}) => {
  const turn = recipeTurn();
  const recipe = turn.recipe!;
  recipe.ingredients[0].quantity = null;
  recipe.ingredients[0].unit = null;
  const mapping = recipe.nutrition.mappings[0];
  mapping.quantity = null;
  mapping.unit = null;
  mapping.weightGrams = 250;
  mapping.conversionBasis =
    "Assumed 250 g for this recipe; original amount unchanged.";
  mapping.quantityEstimate = {
    weightGrams: 250,
    sources: [
      {
        reference: recipe.sources[0].reference,
        quote: "Serves 4; 500 g rice.",
        servings: 4,
        weightGrams: 500,
      },
    ],
  };
  expect(isStructuredRecipe(recipe)).toBe(true);
  const bad = structuredClone(recipe);
  bad.nutrition.mappings[0].quantityEstimate!.sources[0].reference =
    "https://invented.example.invalid/";
  expect(isStructuredRecipe(bad)).toBe(false);
  const unattributed = structuredClone(recipe);
  delete unattributed.nutrition.mappings[0].quantityEstimate;
  expect(isStructuredRecipe(unattributed)).toBe(false);
  await recipeBackend(page, [turn]);
  await open(page);
  await expect(
    card(page).getByText("Estimated quantities", { exact: true }),
  ).toBeVisible();
  const details = card(page).getByText("Nutrition details", { exact: true });
  await details.focus();
  await details.press("Enter");
  await expect(card(page)).toContainText("Estimated quantity");
  await expect(card(page)).not.toContainText("Assumed 250 g for this recipe");
  await expect(
    card(page).getByRole("region", { name: "Ingredients", exact: true }),
  ).toContainText("Quantity unavailable");
  await expect(
    card(page).getByRole("link", { name: "Quantity estimate source" }),
  ).toHaveAttribute("href", recipe.sources[0].reference);
  await expect(card(page)).not.toContainText("500 g for 4 source servings");
  await page.reload();
  await expect(
    card(page).getByText("Estimated quantities", { exact: true }),
  ).toBeVisible();
  await expect(
    card(page).getByRole("link", {
      name: "Quantity estimate source",
      includeHidden: true,
    }),
  ).toBeHidden();
});

test("recipes with more than nine USDA sources retain match labels and links without conversion math", async ({
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
  await card(page).getByText("Nutrition details", { exact: true }).click();
  await expect(card(page)).toContainText(
    "Estimated match: Rice, white, long-grain, raw",
  );
  await expect(card(page)).not.toContainText("USDA portion: 1 cup = 185 g");
  await expect(card(page).getByRole("table").getByRole("row")).toHaveCount(
    recipe.ingredients.length + 1,
  );
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
  const ingredients = recipe.getByRole("region", {
    name: "Ingredients",
    exact: true,
  });
  await expect(ingredients.getByRole("listitem")).toHaveText([
    "100 g - rice - Already have",
    "150 g - carrot - Need to buyBuy 100 g.",
    "10 g - olive oil - Check your pantry",
  ]);
  await expect(ingredients.getByRole("heading", { level: 5 })).toHaveCount(0);
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
  const notice = recipe.getByText(
    "Informational estimate, not medical advice.",
    {
      exact: true,
    },
  );
  await expect(notice).toBeHidden();
  await recipe.getByText("Nutrition details", { exact: true }).click();
  await expect(notice).toBeVisible();
  const nutritionScroll = recipe.getByRole("region", {
    name: "Ingredient nutrition",
    exact: true,
  });
  await nutritionScroll.focus();
  await expect(nutritionScroll).toBeFocused();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  if (testInfo.project.name === "phone") {
    expect(
      await nutritionScroll.evaluate(
        (element) => element.scrollWidth > element.clientWidth,
      ),
    ).toBe(true);
    await nutritionScroll.press("ArrowRight");
    await expect
      .poll(() => nutritionScroll.evaluate((element) => element.scrollLeft))
      .toBeGreaterThan(0);
  }
  await expect(recipe.getByRole("table").getByRole("row")).toHaveCount(4);
  await expect(
    recipe
      .getByRole("table")
      .getByRole("cell", { name: "Unavailable", exact: true }),
  ).toHaveCount(18);
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
    recipe.getByRole("link", {
      name: "rice (opens in a new tab)",
      exact: true,
    }),
  ).toHaveAttribute("rel", "noopener noreferrer");
  for (const label of ["Cook This", "Save to Cook"]) {
    const action = recipe.getByRole("button", { name: label, exact: true });
    await expect(action).toBeEnabled();
  }
  await expect(
    page.getByText("private-model-summary-not-for-rendering"),
  ).toHaveCount(0);
  await page.screenshot({
    path: testInfo.outputPath("nutrition-table.png"),
    fullPage: true,
  });
  await page.reload();
  await expect(card(page)).toBeVisible();
  await expect(notice).toBeHidden();
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
    card(page)
      .getByRole("definition")
      .filter({ hasText: /^Unavailable$/ }),
  ).toHaveCount(5);
  await expect(
    card(page).getByText("Partial estimate", { exact: true }),
  ).toBeVisible();
  await card(page).getByText("Nutrition details", { exact: true }).click();
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
    card(page)
      .getByRole("definition")
      .filter({ hasText: /^Unavailable$/ }),
  ).toHaveCount(6);
  await expect(
    card(page).getByText("Nutrition unavailable", { exact: true }),
  ).toBeVisible();
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
  const ingredients = card(page).getByRole("region", {
    name: "Ingredients",
    exact: true,
  });
  await expect(ingredients.getByRole("listitem")).toHaveText([
    "Quantity unavailable - rice - Check your pantry",
    "Quantity unavailable - carrot - Check your pantry",
    "Quantity unavailable - olive oil - Check your pantry",
  ]);
  await expect(ingredients).not.toContainText("Already have");
  await expect(ingredients).not.toContainText("Need to buy");
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
