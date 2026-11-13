import { isRecord, type Skill } from "./profile.ts";

type Source = {
  sourceId: string;
  version: string;
  kind: "curated_recipe" | "usda";
  reference: string;
};
type Amount = { quantity: number | null; unit: string | null };
type QuantityEstimate = {
  weightGrams: number;
  sources: {
    reference: string;
    quote: string;
    servings: number;
    weightGrams: number;
  }[];
};
type Ingredient = Amount & {
  lineId: string;
  name: string;
  inventoryStatus: "available" | "missing" | "unknown";
};
export const nutrientLabels = {
  energy: "Calories",
  protein: "Protein",
  carbohydrate: "Carbohydrates",
  fat: "Fat",
  fiber: "Fiber",
  sodium: "Sodium",
} as const;
type NutrientName = keyof typeof nutrientLabels;
type Coverage = {
  name: NutrientName;
  status: "known" | "partial" | "unavailable";
  wholeRecipeAmount: number | null;
  coveredIngredientLineIds: string[];
  unknownIngredientLineIds: string[];
};
type IngredientCalories = {
  ingredientLineId: string;
  amount: number;
  source: "usda_mcp" | "web_iq" | "ai_knowledge";
  reference: string | null;
  basis: string;
};
export type StructuredRecipe = {
  schemaVersion: 2;
  title: string;
  totalMinutes: number;
  servings: number;
  skill: Skill;
  fitReasons: string[];
  ingredients: Ingredient[];
  shoppingItems: (Amount & { ingredientLineId: string })[];
  instructions: { step: number; text: string; ingredientLineIds: string[] }[];
  adaptations: string[];
  substitutions: {
    replaces: string;
    ingredientLineId: string;
    reason: string;
  }[];
  sourceIds: string[];
  sourceClaims: { sourceId: string; quote: string }[];
  sources: Source[];
  webGrounding: {
    claims: { text: string; sourceUrl: string }[];
    citations: { url: string; title: string }[];
    retrievedAt: string;
  } | null;
  nutrition: {
    status: "known" | "partial" | "unavailable";
    servingBasis: "per_serving" | "whole_recipe";
    servings: number;
    isEstimate: true;
    notice: "Informational estimate, not medical advice.";
    nutrients: { name: NutrientName; amount: number; unit: string }[];
    sources: Source[];
    mappings: (Amount & {
      ingredientLineId: string;
      sourceId: string;
      usdaFoodId: string;
      foodDescription?: string | null;
      conversionBasis?: string | null;
      approximateMatch?: boolean;
      weightGrams?: number | null;
      quantityEstimate?: QuantityEstimate | null;
    })[];
    coverage?: Coverage[];
    ingredientCalories?: IngredientCalories[];
    unknownComponents?: {
      ingredientLineId: string;
      reason: string;
      unknownNutrients: NutrientName[];
    }[];
  };
};

function text(value: unknown, max: number): value is string {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    [...value].length <= max
  );
}
function number(value: unknown, min: number, max: number): value is number {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= min &&
    value <= max
  );
}
function list<T>(
  value: unknown,
  max: number,
  guard: (item: unknown) => item is T,
  min = 0,
): value is T[] {
  return (
    Array.isArray(value) &&
    value.length >= min &&
    value.length <= max &&
    value.every(guard)
  );
}
const lineId = (value: unknown): value is string =>
  typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(value);
const referenceId = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/.test(value);
const unique = (values: string[]) => new Set(values).size === values.length;
const same = (left: string[], right: string[]) =>
  unique(left) &&
  unique(right) &&
  left.length === right.length &&
  left.every((id) => right.includes(id));
const shortText = (value: unknown): value is string => text(value, 240);
const units = [
  "g",
  "kg",
  "ml",
  "l",
  "tsp",
  "tbsp",
  "cup",
  "oz",
  "lb",
  "item",
  "piece",
  "bunch",
  "can",
  "package",
];
function amount(value: Record<string, unknown>) {
  return (
    (value.quantity === null || number(value.quantity, 0.001, 1_000_000)) &&
    (value.unit === null ||
      (typeof value.unit === "string" && units.includes(value.unit)))
  );
}
function source(value: unknown): value is Source {
  return (
    isRecord(value) &&
    referenceId(value.sourceId) &&
    shortText(value.version) &&
    (value.kind === "curated_recipe" || value.kind === "usda") &&
    text(value.reference, 500)
  );
}
function nutrientName(value: unknown): value is NutrientName {
  return typeof value === "string" && Object.hasOwn(nutrientLabels, value);
}
function quantityEstimate(value: unknown): value is QuantityEstimate {
  return (
    isRecord(value) &&
    number(value.weightGrams, Number.MIN_VALUE, 1_000_000) &&
    list(
      value.sources,
      3,
      (item): item is QuantityEstimate["sources"][number] =>
        isRecord(item) &&
        text(item.reference, 500) &&
        text(item.quote, 1500) &&
        Number.isInteger(item.servings) &&
        number(item.servings, 1, 100) &&
        number(item.weightGrams, Number.MIN_VALUE, 100_000),
      1,
    ) &&
    unique(value.sources.map((item) => item.reference))
  );
}
function coverage(value: unknown): value is Coverage {
  if (
    !isRecord(value) ||
    !nutrientName(value.name) ||
    !list(value.coveredIngredientLineIds, 30, lineId) ||
    !list(value.unknownIngredientLineIds, 30, lineId)
  )
    return false;
  const covered = value.coveredIngredientLineIds;
  const unknown = value.unknownIngredientLineIds;
  return (
    unique([...covered, ...unknown]) &&
    covered.length + unknown.length > 0 &&
    value.status ===
      (covered.length
        ? unknown.length
          ? "partial"
          : "known"
        : "unavailable") &&
    (covered.length
      ? number(value.wholeRecipeAmount, 0, 200_000_000)
      : value.wholeRecipeAmount === null)
  );
}

// This validates the public display contract, not allergies or cooking safety.
// Only the backend's completed, owned turn is allowed to supply a candidate.
export function isStructuredRecipe(value: unknown): value is StructuredRecipe {
  if (
    !isRecord(value) ||
    value.schemaVersion !== 2 ||
    !text(value.title, 160) ||
    !Number.isInteger(value.servings) ||
    !number(value.servings, 1, 20) ||
    !Number.isInteger(value.totalMinutes) ||
    !number(value.totalMinutes, 1, 1440) ||
    !["beginner", "intermediate", "advanced"].includes(String(value.skill)) ||
    !list(value.fitReasons, 10, shortText, 1) ||
    !list(value.adaptations, 10, shortText) ||
    !list(
      value.ingredients,
      30,
      (item): item is Ingredient =>
        isRecord(item) &&
        lineId(item.lineId) &&
        text(item.name, 100) &&
        amount(item) &&
        ["available", "missing", "unknown"].includes(
          String(item.inventoryStatus),
        ),
      1,
    ) ||
    !list(
      value.shoppingItems,
      30,
      (item): item is StructuredRecipe["shoppingItems"][number] =>
        isRecord(item) && lineId(item.ingredientLineId) && amount(item),
    ) ||
    !list(
      value.instructions,
      30,
      (item): item is StructuredRecipe["instructions"][number] =>
        isRecord(item) &&
        Number.isInteger(item.step) &&
        text(item.text, 500) &&
        list(item.ingredientLineIds, 30, lineId) &&
        unique(item.ingredientLineIds),
      1,
    ) ||
    !list(
      value.substitutions,
      30,
      (item): item is StructuredRecipe["substitutions"][number] =>
        isRecord(item) &&
        shortText(item.replaces) &&
        lineId(item.ingredientLineId) &&
        shortText(item.reason),
    ) ||
    !list(value.sourceIds, 3, referenceId, 1) ||
    !list(
      value.sourceClaims,
      10,
      (item): item is StructuredRecipe["sourceClaims"][number] =>
        isRecord(item) && referenceId(item.sourceId) && shortText(item.quote),
    ) ||
    !list(value.sources, 33, source, 1)
  )
    return false;

  const ingredients = value.ingredients;
  const sourceIds = value.sourceIds;
  const sources = value.sources;
  const ids = ingredients.map((item) => item.lineId);
  if (
    !unique(ids) ||
    !unique(value.sources.map((item) => item.sourceId)) ||
    !same(
      value.sourceIds,
      value.sources
        .filter((item) => item.kind === "curated_recipe")
        .map((item) => item.sourceId),
    ) ||
    !value.sourceClaims.every((item) => sourceIds.includes(item.sourceId)) ||
    !value.substitutions.every((item) => ids.includes(item.ingredientLineId)) ||
    !same(
      [
        ...new Set(
          value.instructions.flatMap((item) => item.ingredientLineIds),
        ),
      ],
      ids,
    ) ||
    !value.instructions.every((item, index) => item.step === index + 1) ||
    !same(
      value.shoppingItems.map((item) => item.ingredientLineId),
      ingredients
        .filter((item) => item.inventoryStatus === "missing")
        .map((item) => item.lineId),
    ) ||
    !value.shoppingItems.every((item) => {
      const line = ingredients.find(
        (line) => line.lineId === item.ingredientLineId,
      );
      return (
        line &&
        line.unit === item.unit &&
        (line.quantity === null ||
          (item.quantity !== null && item.quantity <= line.quantity))
      );
    })
  )
    return false;

  const n = value.nutrition;
  if (!isRecord(n)) return false;
  const calories = n.ingredientCalories ?? [];
  if (
    !list(
      calories,
      30,
      (item): item is IngredientCalories =>
        isRecord(item) &&
        typeof item.ingredientLineId === "string" &&
        ids.includes(item.ingredientLineId) &&
        number(item.amount, 0, 10_000_000) &&
        ["usda_mcp", "web_iq", "ai_knowledge"].includes(String(item.source)) &&
        text(item.basis, 500) &&
        (item.source === "ai_knowledge"
          ? item.reference === null
          : text(item.reference, 500) &&
            safeRecipeUrl(item.reference) !== null),
    ) ||
    !unique(calories.map((item) => item.ingredientLineId))
  )
    return false;
  if (
    !["known", "partial", "unavailable"].includes(String(n.status)) ||
    !["per_serving", "whole_recipe"].includes(String(n.servingBasis)) ||
    n.servings !== value.servings ||
    n.isEstimate !== true ||
    n.notice !== "Informational estimate, not medical advice." ||
    !list(n.sources, 30, source) ||
    n.sources.some((item) => item.kind !== "usda") ||
    !list(
      n.nutrients,
      6,
      (item): item is StructuredRecipe["nutrition"]["nutrients"][number] =>
        isRecord(item) &&
        nutrientName(item.name) &&
        number(item.amount, 0, 10_000_000) &&
        item.unit ===
          (item.name === "energy"
            ? "kcal"
            : item.name === "sodium"
              ? "mg"
              : "g"),
    ) ||
    !unique(n.nutrients.map((item) => item.name)) ||
    !list(
      n.mappings,
      30,
      (item): item is StructuredRecipe["nutrition"]["mappings"][number] =>
        isRecord(item) &&
        lineId(item.ingredientLineId) &&
        referenceId(item.sourceId) &&
        referenceId(item.usdaFoodId) &&
        (item.foodDescription == null || text(item.foodDescription, 500)) &&
        (item.conversionBasis == null || text(item.conversionBasis, 500)) &&
        (item.approximateMatch === undefined ||
          typeof item.approximateMatch === "boolean") &&
        amount(item) &&
        (item.quantityEstimate == null
          ? item.quantity !== null && item.unit !== null
          : quantityEstimate(item.quantityEstimate) &&
            item.weightGrams === item.quantityEstimate.weightGrams),
    ) ||
    !unique(n.mappings.map((item) => item.ingredientLineId)) ||
    !same(
      n.sources.map((item) => item.sourceId),
      [...new Set(n.mappings.map((item) => item.sourceId))],
    ) ||
    !n.mappings.every((item) =>
      ingredients.some(
        (line) =>
          line.lineId === item.ingredientLineId &&
          line.quantity === item.quantity &&
          line.unit === item.unit,
      ),
    ) ||
    !same(
      n.sources.map((item) => item.sourceId),
      value.sources
        .filter((item) => item.kind === "usda")
        .map((item) => item.sourceId),
    ) ||
    !n.sources.every((item) =>
      sources.some(
        (source) =>
          source.sourceId === item.sourceId &&
          source.version === item.version &&
          source.reference === item.reference &&
          source.kind === item.kind,
      ),
    ) ||
    (n.status === "unavailable"
      ? n.nutrients.length +
          n.sources.length +
          n.mappings.length +
          calories.length !==
        0
      : !n.nutrients.length || (!n.mappings.length && !calories.length)) ||
    (n.status === "known" &&
      !same(
        n.mappings.map((item) => item.ingredientLineId),
        ids,
      ))
  )
    return false;

  if (n.coverage !== undefined) {
    if (!list(n.coverage, 6, coverage)) return false;
    if (
      n.coverage.length &&
      (!same(
        n.coverage.map((item) => item.name),
        Object.keys(nutrientLabels),
      ) ||
        !n.coverage.every((item) =>
          same(
            [
              ...item.coveredIngredientLineIds,
              ...item.unknownIngredientLineIds,
            ],
            ids,
          ),
        ) ||
        !same(
          n.nutrients.map((item) => item.name),
          n.coverage
            .filter((item) => item.status !== "unavailable")
            .map((item) => item.name),
        ) ||
        n.status !==
          (n.coverage.every((item) => item.status === "known")
            ? "known"
            : n.nutrients.length
              ? "partial"
              : "unavailable"))
    )
      return false;
  }
  if (
    n.unknownComponents !== undefined &&
    !list(
      n.unknownComponents,
      30,
      (
        item,
      ): item is NonNullable<
        StructuredRecipe["nutrition"]["unknownComponents"]
      >[number] =>
        isRecord(item) &&
        typeof item.ingredientLineId === "string" &&
        ids.includes(item.ingredientLineId) &&
        [
          "missing_quantity",
          "missing_unit",
          "unsupported_unit",
          "food_unavailable",
          "lookup_limit",
          "missing_nutrients",
          "service_unavailable",
        ].includes(String(item.reason)) &&
        list(item.unknownNutrients, 6, nutrientName, 1) &&
        unique(item.unknownNutrients),
    )
  )
    return false;

  const details = n.coverage ?? [];
  const unknown = n.unknownComponents ?? [];
  const mappings = n.mappings;
  const nutritionSources = n.sources;
  if (
    !unique(unknown.map((item) => item.ingredientLineId)) ||
    (details.length
      ? !same(
          [
            ...new Set(
              details.flatMap((item) => item.coveredIngredientLineIds),
            ),
          ],
          [
            ...new Set([
              ...n.mappings.map((item) => item.ingredientLineId),
              ...calories.map((item) => item.ingredientLineId),
            ]),
          ],
        ) ||
        details.some(
          (item) =>
            item.name !== "energy" &&
            item.coveredIngredientLineIds.some(
              (id) =>
                !mappings.some((mapping) => mapping.ingredientLineId === id),
            ),
        ) ||
        !same(
          details.flatMap((item) =>
            item.unknownIngredientLineIds.map((id) => `${id}:${item.name}`),
          ),
          unknown.flatMap((item) =>
            item.unknownNutrients.map(
              (name) => `${item.ingredientLineId}:${name}`,
            ),
          ),
        )
      : unknown.length > 0 || calories.length > 0) ||
    (calories.length > 0 &&
      (!same(
        calories.map((item) => item.ingredientLineId),
        details.find((item) => item.name === "energy")
          ?.coveredIngredientLineIds ?? [],
      ) ||
        calories.some(
          (item) =>
            item.source === "usda_mcp" &&
            !mappings.some(
              (mapping) =>
                mapping.ingredientLineId === item.ingredientLineId &&
                nutritionSources.some(
                  (source) =>
                    source.sourceId === mapping.sourceId &&
                    source.reference === item.reference,
                ),
            ),
        )))
  )
    return false;

  const web = value.webGrounding;
  const validWeb =
    web === null ||
    (isRecord(web) &&
      text(web.retrievedAt, 80) &&
      Number.isFinite(Date.parse(web.retrievedAt)) &&
      list(
        web.claims,
        3,
        (item): item is { text: string; sourceUrl: string } =>
          isRecord(item) && text(item.text, 500) && text(item.sourceUrl, 500),
        1,
      ) &&
      list(
        web.citations,
        3,
        (item): item is { url: string; title: string } =>
          isRecord(item) && text(item.url, 500) && shortText(item.title),
        1,
      ) &&
      same(
        web.citations.map((item) => item.url),
        [...new Set(web.claims.map((item) => item.sourceUrl))],
      ));
  return (
    validWeb &&
    n.mappings.every(
      (mapping) =>
        mapping.quantityEstimate == null ||
        mapping.quantityEstimate.sources.every(
          (evidence) =>
            sources.some(
              (source) =>
                source.kind === "curated_recipe" &&
                source.reference === evidence.reference,
            ) ||
            (isRecord(web) &&
              Array.isArray(web.citations) &&
              web.citations.some(
                (citation) =>
                  isRecord(citation) && citation.url === evidence.reference,
              )),
        ),
    )
  );
}

export function safeRecipeUrl(reference: string): string | null {
  try {
    const url = new URL(reference);
    return url.protocol === "https:" &&
      !url.username &&
      !url.password &&
      (!url.port || url.port === "443")
      ? url.href
      : null;
  } catch (error) {
    if (error instanceof TypeError) return null;
    throw error;
  }
}
