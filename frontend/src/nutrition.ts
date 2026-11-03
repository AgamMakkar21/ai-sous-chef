import { ApiError, json, request } from "./api";
import { isRecord, type Session } from "./profile";

export type IngredientNutrition = {
  calories: number | null;
  source: { id: string; foodId: string; version: string } | null;
  status: "known" | "partial" | "unavailable";
};

export type FoodCandidate = {
  ndb_no: string;
  long_desc: string;
  shrt_desc: string;
  food_group: string;
};
export type FoodSearch = { foods: FoodCandidate[]; truncated: boolean };

export async function searchNutrition(
  name: string,
  session: Session,
  signal: AbortSignal,
): Promise<FoodSearch> {
  const value = await json(
    await request("/api/v1/nutrition/search", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": session.csrfToken,
      },
      body: JSON.stringify({ name }),
      signal,
    }),
  );
  if (
    !isRecord(value) ||
    !Array.isArray(value.foods) ||
    value.foods.length > 20 ||
    typeof value.truncated !== "boolean" ||
    value.truncated !== (value.foods.length === 20)
  )
    throw new ApiError(502, "invalid_response");
  const foods: FoodCandidate[] = [];
  for (const food of value.foods) {
    if (
      !isRecord(food) ||
      typeof food.ndb_no !== "string" ||
      !/^[0-9]{5}$/.test(food.ndb_no) ||
      foods.some((row) => row.ndb_no === food.ndb_no) ||
      typeof food.long_desc !== "string" ||
      !food.long_desc.trim() ||
      food.long_desc.length > 500 ||
      typeof food.shrt_desc !== "string" ||
      !food.shrt_desc.trim() ||
      food.shrt_desc.length > 500 ||
      typeof food.food_group !== "string" ||
      !food.food_group.trim() ||
      food.food_group.length > 500
    )
      throw new ApiError(502, "invalid_response");
    foods.push({
      ndb_no: food.ndb_no,
      long_desc: food.long_desc,
      shrt_desc: food.shrt_desc,
      food_group: food.food_group,
    });
  }
  return { foods, truncated: value.truncated };
}

const units: Record<string, string> = {
  energy: "kcal",
  protein: "g",
  carbohydrate: "g",
  fat: "g",
  fiber: "g",
  sodium: "mg",
};

export async function lookupNutrition(
  name: string,
  session: Session,
  signal: AbortSignal,
  foodId?: string,
): Promise<IngredientNutrition> {
  const value = await json(
    await request("/api/v1/nutrition/lookup", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": session.csrfToken,
      },
      body: JSON.stringify({ name, ...(foodId ? { foodId } : {}) }),
      signal,
    }),
  );
  const data = isRecord(value) ? value.data : undefined;
  if (
    !isRecord(data) ||
    !["known", "partial", "unavailable"].includes(String(data.status)) ||
    data.servingBasis !== "per_serving" ||
    data.servings !== 1 ||
    !Array.isArray(data.nutrients) ||
    data.nutrients.length > 6 ||
    !Array.isArray(data.mappings) ||
    !Array.isArray(data.sources)
  )
    throw new ApiError(502, "invalid_response");
  if (data.status === "unavailable") {
    if (data.nutrients.length || data.mappings.length || data.sources.length)
      throw new ApiError(502, "invalid_response");
    return { status: "unavailable", calories: null, source: null };
  }
  const names = new Set<string>();
  let calories: number | null = null;
  for (const nutrient of data.nutrients) {
    if (
      !isRecord(nutrient) ||
      typeof nutrient.name !== "string" ||
      !Object.hasOwn(units, nutrient.name) ||
      nutrient.unit !== units[nutrient.name] ||
      typeof nutrient.amount !== "number" ||
      !Number.isFinite(nutrient.amount) ||
      nutrient.amount < 0 ||
      nutrient.amount > 10_000_000 ||
      names.has(nutrient.name)
    )
      throw new ApiError(502, "invalid_response");
    names.add(nutrient.name);
    if (nutrient.name === "energy") calories = nutrient.amount;
  }
  const mapping: unknown = data.mappings[0];
  const source: unknown = data.sources[0];
  if (
    !names.size ||
    (data.status === "known" && names.size !== 6) ||
    data.mappings.length !== 1 ||
    data.sources.length !== 1 ||
    !isRecord(mapping) ||
    mapping.ingredientLineId !== "ingredient" ||
    mapping.quantity !== 100 ||
    mapping.unit !== "g" ||
    typeof mapping.usdaFoodId !== "string" ||
    !/^[0-9]{5}$/.test(mapping.usdaFoodId) ||
    (foodId !== undefined && mapping.usdaFoodId !== foodId) ||
    !isRecord(source) ||
    source.kind !== "usda" ||
    source.sourceId !== `usda:sr:${mapping.usdaFoodId}` ||
    source.sourceId !== mapping.sourceId ||
    typeof source.version !== "string" ||
    !source.version.trim() ||
    source.version.length > 240 ||
    source.reference !== "https://www.ars.usda.gov/Services/docs.htm?docid=8964"
  )
    throw new ApiError(502, "invalid_response");
  return {
    status: data.status === "known" ? "known" : "partial",
    calories,
    source: {
      id: source.sourceId,
      foodId: mapping.usdaFoodId,
      version: source.version,
    },
  };
}
