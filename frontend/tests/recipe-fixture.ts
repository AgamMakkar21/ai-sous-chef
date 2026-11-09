import { readFileSync } from "node:fs";
import { isRecord } from "../src/profile.ts";
import { isStructuredRecipe, type StructuredRecipe } from "../src/recipes.ts";

export function recipeFixture(): StructuredRecipe {
  const fixture: unknown = JSON.parse(
    readFileSync(
      new URL("../../tests/fixtures/recipe-specialist.json", import.meta.url),
      "utf8",
    ),
  );
  if (
    !isRecord(fixture) ||
    !isRecord(fixture.draft) ||
    !isRecord(fixture.nutrition) ||
    !isRecord(fixture.grounding) ||
    !Array.isArray(fixture.grounding.recipes) ||
    !Array.isArray(fixture.draft.ingredients)
  )
    throw new Error("Invalid specialist fixture.");
  const recipe: unknown = {
    ...fixture.draft,
    ingredients: fixture.draft.ingredients.map(
      (line: unknown, index: number) => {
        if (!isRecord(line)) throw new Error("Invalid fixture ingredient.");
        return {
          ...line,
          ingredientId: null,
          groceryIds: [],
          inventoryStatus: ["available", "missing", "unknown"][index],
        };
      },
    ),
    shoppingItems: [{ ingredientLineId: "carrot", quantity: 100, unit: "g" }],
    nutrition: {
      ...fixture.nutrition,
      isEstimate: true,
      notice: "Informational estimate, not medical advice.",
      nutrients: [
        { name: "energy", amount: 250, unit: "kcal" },
        { name: "protein", amount: 5, unit: "g" },
        { name: "carbohydrate", amount: 40, unit: "g" },
        { name: "fat", amount: 8, unit: "g" },
        { name: "fiber", amount: 3, unit: "g" },
        { name: "sodium", amount: 0, unit: "mg" },
      ],
    },
    sources: [
      ...fixture.grounding.recipes.map((item: unknown) => {
        if (!isRecord(item)) throw new Error("Invalid fixture source.");
        return item.source;
      }),
      ...(Array.isArray(fixture.nutrition.sources)
        ? fixture.nutrition.sources
        : []),
    ],
    webGrounding: null,
  };
  if (!isStructuredRecipe(recipe))
    throw new Error("Invalid structured fixture.");
  return recipe;
}
