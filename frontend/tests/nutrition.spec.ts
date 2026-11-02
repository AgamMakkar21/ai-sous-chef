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
        sourceId: "usda:sr:11124",
        usdaFoodId: "11124",
      },
    ],
    sources: [
      {
        sourceId: "usda:sr:11124",
        version: "SR (release unspecified)",
        kind: "usda",
        reference: "https://www.ars.usda.gov/Services/docs.htm?docid=8964",
      },
    ],
  };
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
  await expect(result).toContainText("NDB 11124");
  await expect(result).toContainText("usda:sr:11124");
  await expect(
    result.getByRole("link", { name: "USDA Standard Reference" }),
  ).toHaveAttribute(
    "href",
    "https://www.ars.usda.gov/Services/docs.htm?docid=8964",
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
        else data.sources[0]!.sourceId = "usda:sr:00000";
        await route.fulfill({ json: { data } });
      } else {
        await route.fulfill({
          status:
            failure === "signed-out" ? 401 : failure === "timeout" ? 504 : 503,
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
  await page.route("https://www.ars.usda.gov/**", async (route) => {
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
  await page.getByRole("link", { name: "USDA Standard Reference" }).click();
  await expect(
    page.getByRole("heading", { name: "External source fixture" }),
  ).toBeVisible();
  expect(externalReferer).toBeUndefined();
});
