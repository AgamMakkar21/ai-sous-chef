import { expect, test, type Page } from "@playwright/test";
import { backend } from "./backend.ts";

function evidence(energy: number | null = 41) {
  return {
    status: "partial",
    servingBasis: "per_serving",
    servings: 1,
    nutrients:
      energy === null
        ? [{ name: "protein", amount: 0.93, unit: "g" }]
        : [{ name: "energy", amount: energy, unit: "kcal" }],
    mappings: [
      {
        ingredientLineId: "ingredient",
        quantity: 100,
        unit: "g",
        sourceId: "usda:fdc:1234567",
        usdaFoodId: "1234567",
      },
    ],
    sources: [
      {
        sourceId: "usda:fdc:1234567",
        version: "SR Legacy; published 4/1/2019",
        kind: "usda",
        reference: "https://fdc.nal.usda.gov/food-details/1234567/nutrients",
      },
    ],
  };
}

test("ambiguous search lets the user choose an attributed food without guessing", async ({
  page,
}) => {
  await backend(page);
  const food = {
    fdc_id: "1234567",
    long_desc: "Carrots, raw",
    shrt_desc: "CARROTS,RAW",
    food_group: "Vegetables",
    data_type: "SR Legacy",
  };
  await page.route("**/api/v1/nutrition/search", (route) =>
    route.fulfill({ json: { foods: [food], truncated: false } }),
  );
  const bodies: unknown[] = [];
  await page.route("**/api/v1/nutrition/lookup", async (route) => {
    const body = route.request().postDataJSON() as { foodId?: string };
    bodies.push(body);
    await route.fulfill({
      json: {
        data: body.foodId
          ? evidence()
          : {
              ...evidence(),
              status: "unavailable",
              nutrients: [],
              mappings: [],
              sources: [],
            },
      },
    });
  });
  await page.goto("/");
  await page.getByLabel("USDA ingredient name").fill("carrots");
  await page
    .getByRole("button", { name: "Look up calories", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "USDA food matches" }),
  ).toBeVisible();
  await expect(page.locator(".ingredient-nutrition-result")).not.toContainText(
    "41 kcal",
  );
  await page
    .getByRole("button", { name: "Carrots, raw · FDC 1234567" })
    .click();
  await expect(page.locator(".ingredient-nutrition-result")).toContainText(
    "41 kcal per 100 g",
  );
  expect(bodies).toEqual([
    { name: "carrots" },
    { name: "carrots", foodId: "1234567" },
  ]);
});

for (const count of [2, 20]) {
  test(`an incomplete search with ${count} candidates asks for refinement without choosing a result`, async ({
    page,
  }) => {
    await backend(page);
    await page.route("**/api/v1/nutrition/search", (route) =>
      route.fulfill({
        json: {
          foods: Array.from({ length: count }, (_, index) => ({
            fdc_id: String(2000000 + index),
            long_desc: `Chicken fixture ${index}`,
            shrt_desc: `CHICKEN ${index}`,
            food_group: "Fixture",
            data_type: "SR Legacy",
          })),
          truncated: true,
        },
      }),
    );
    await page.route("**/api/v1/nutrition/lookup", (route) =>
      route.fulfill({
        json: {
          data: {
            ...evidence(),
            status: "unavailable",
            nutrients: [],
            mappings: [],
            sources: [],
          },
        },
      }),
    );
    await page.goto("/");
    await lookup(page);
    await expect(
      page.getByText("Showing a limited set of matches (at most 20).", {
        exact: false,
      }),
    ).toBeVisible();
    await expect(
      page
        .getByRole("region", { name: "USDA food matches" })
        .getByRole("button"),
    ).toHaveCount(count);
    await expect(
      page.locator(".ingredient-nutrition-result"),
    ).not.toContainText("kcal");
  });
}

async function lookup(page: Page) {
  await page
    .getByLabel("USDA ingredient name", { exact: true })
    .fill("Carrots, raw");
  await page
    .getByRole("button", { name: "Look up calories", exact: true })
    .click();
}

test("ingredient lookup shows calories, mass basis and USDA attribution without changing groceries", async ({
  page,
}) => {
  await backend(page);
  const requests: { body: unknown; token: string | undefined }[] = [];
  await page.route("**/api/v1/nutrition/lookup", async (route) => {
    requests.push({
      body: route.request().postDataJSON(),
      token: route.request().headers()["x-csrf-token"],
    });
    await route.fulfill({ json: { data: evidence() } });
  });
  await page.goto("/");
  await lookup(page);
  const result = page.locator(".ingredient-nutrition-result");
  await expect(result).toContainText("41 kcal per 100 g");
  await expect(result).toContainText("FDC 1234567");
  await expect(result).toContainText("usda:fdc:1234567");
  await expect(
    result.getByRole("link", { name: "USDA FoodData Central" }),
  ).toHaveAttribute(
    "href",
    "https://fdc.nal.usda.gov/food-details/1234567/nutrients",
  );
  expect(requests).toEqual([
    { body: { name: "Carrots, raw" }, token: "t".repeat(43) },
  ]);
  await page.getByLabel("USDA ingredient name").fill("Rice, raw");
  await expect(result).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Add ingredient" }),
  ).toBeVisible();
});

for (const state of ["unavailable", "no-energy", "zero"] as const) {
  test(`nutrition ${state} is not replaced with invented calories`, async ({
    page,
  }) => {
    await backend(page);
    await page.route("**/api/v1/nutrition/lookup", (route) =>
      route.fulfill({
        json: {
          data:
            state === "unavailable"
              ? {
                  ...evidence(),
                  status: "unavailable",
                  nutrients: [],
                  mappings: [],
                  sources: [],
                }
              : evidence(state === "zero" ? 0 : null),
        },
      }),
    );
    await page.goto("/");
    await lookup(page);
    const result = page.locator(".ingredient-nutrition-result");
    if (state === "zero")
      await expect(result).toContainText("0 kcal per 100 g");
    else {
      await expect(result).not.toContainText("0 kcal");
      await expect(result).toContainText(
        state === "unavailable"
          ? "No unambiguous USDA match"
          : "Calories are unavailable",
      );
    }
  });
}

for (const failure of [
  "unavailable",
  "timeout",
  "throttled",
  "malformed",
  "wrong-unit",
  "wrong-source",
  "signed-out",
] as const) {
  test(`nutrition ${failure} clears previous calories and is explicit`, async ({
    page,
  }) => {
    await backend(page);
    let failed = false;
    await page.route("**/api/v1/nutrition/lookup", async (route) => {
      if (!failed) {
        await route.fulfill({ json: { data: evidence() } });
        return;
      }
      if (failure === "malformed") {
        await route.fulfill({ json: { unexpected: true } });
      } else if (failure === "wrong-unit" || failure === "wrong-source") {
        const data = evidence();
        if (failure === "wrong-unit") data.nutrients[0]!.unit = "kJ";
        else data.sources[0]!.sourceId = "usda:fdc:00000";
        await route.fulfill({ json: { data } });
      } else {
        await route.fulfill({
          status:
            failure === "signed-out"
              ? 401
              : failure === "timeout"
                ? 504
                : failure === "throttled"
                  ? 429
                  : 503,
          json: {
            code: "nutrition_unavailable",
            detail: "private-provider-details",
          },
        });
      }
    });
    await page.goto("/");
    await lookup(page);
    await expect(page.locator(".ingredient-nutrition-result")).toContainText(
      "41 kcal",
    );
    failed = true;
    await page
      .getByRole("button", { name: "Look up calories", exact: true })
      .click();
    await expect(page.locator(".ingredient-nutrition-result")).toHaveCount(0);
    await expect(page.getByRole("alert")).toBeVisible();
    if (failure === "signed-out")
      await expect(
        page.getByRole("link", { name: "Sign in", exact: true }),
      ).toBeVisible();
    else
      await expect(page.getByRole("alert")).toContainText(
        failure === "timeout"
          ? "timed out"
          : failure === "throttled"
            ? "USDA API quota reached"
            : "Nutrition could not be retrieved",
      );
    await expect(page.locator("body")).not.toContainText(
      "private-provider-details",
    );
  });
}

test("lookup disables duplicate submission and rejects wildcard names before a request", async ({
  page,
}) => {
  await backend(page);
  let calls = 0;
  await page.route("**/api/v1/nutrition/lookup", async (route) => {
    calls++;
    await new Promise((resolve) => setTimeout(resolve, 400));
    await route.fulfill({ json: { data: evidence() } });
  });
  await page.goto("/");
  await page.getByLabel("USDA ingredient name").fill("%");
  await page
    .getByRole("button", { name: "Look up calories", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("without wildcards");
  expect(calls).toBe(0);
  await lookup(page);
  await expect(
    page.getByRole("button", { name: "Looking up..." }),
  ).toBeDisabled();
  await expect(page.getByLabel("USDA ingredient name")).toBeDisabled();
  await expect(page.locator(".ingredient-nutrition-result")).toContainText(
    "41 kcal",
  );
  expect(calls).toBe(1);
});

test("same-origin lookup sends Referer for Easy Auth but external links do not", async ({
  page,
}) => {
  await backend(page);
  let internalReferer: string | undefined;
  await page.route("**/api/v1/nutrition/lookup", async (route) => {
    internalReferer = route.request().headers()["referer"];
    await route.fulfill({ json: { data: evidence() } });
  });
  let externalReferer: string | undefined;
  await page.route("https://fdc.nal.usda.gov/**", async (route) => {
    externalReferer = route.request().headers()["referer"];
    await route.fulfill({
      contentType: "text/html",
      body: "<h1>External source fixture</h1>",
    });
  });
  await page.goto("/");
  await lookup(page);
  await expect(page.locator(".ingredient-nutrition-result")).toContainText(
    "41 kcal",
  );
  expect(internalReferer).toBe(new URL(page.url()).origin + "/");
  await page.getByRole("link", { name: "USDA FoodData Central" }).click();
  await expect(
    page.getByRole("heading", { name: "External source fixture" }),
  ).toBeVisible();
  expect(externalReferer).toBeUndefined();
});
