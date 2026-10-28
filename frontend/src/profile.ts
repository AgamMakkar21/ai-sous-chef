export const fields = [
  "allergies",
  "dietaryRestrictions",
  "likes",
  "dislikes",
  "preferredCuisines",
  "spicePreference",
  "cookingSkill",
  "typicalTimeBudgetMinutes",
  "nutritionGoals",
] as const;

export type Field = (typeof fields)[number];
export type FieldErrors = Partial<Record<Field, string>>;
export type Draft = Record<Field, string>;
export type Spice = "none" | "mild" | "medium" | "hot";
export type Skill = "beginner" | "intermediate" | "advanced";
export type Preferences = {
  allergies: string[];
  dietaryRestrictions: string[];
  likes: string[];
  dislikes: string[];
  preferredCuisines: string[];
  spicePreference: Spice;
  cookingSkill: Skill;
  typicalTimeBudgetMinutes: number;
  nutritionGoals: string[];
};
export type Profile = {
  userId: string;
  onboardingCompleted: boolean;
  explicitPreferences: Preferences | null;
};
export type Snapshot = { profile: Profile; etag: string };
export type Session = { userId: string; csrfToken: string };

export const labels: Record<Field, string> = {
  allergies: "Food allergies",
  dietaryRestrictions: "Strict dietary restrictions",
  likes: "Flavors and foods you like",
  dislikes: "Flavors and foods you dislike",
  preferredCuisines: "Preferred cuisines",
  spicePreference: "Spice preference",
  cookingSkill: "Cooking experience",
  typicalTimeBudgetMinutes: "Usual cooking time (minutes)",
  nutritionGoals: "Nutrition preferences",
};

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isField(value: string): value is Field {
  return fields.some((field) => field === value);
}

export function isSpice(value: unknown): value is Spice {
  return (
    value === "none" ||
    value === "mild" ||
    value === "medium" ||
    value === "hot"
  );
}

export function isSkill(value: unknown): value is Skill {
  return (
    value === "beginner" || value === "intermediate" || value === "advanced"
  );
}

function isList(value: unknown, limit: number): value is string[] {
  return (
    Array.isArray(value) &&
    value.length <= limit &&
    value.every(
      (entry: unknown) =>
        typeof entry === "string" &&
        entry.trim().length > 0 &&
        [...entry].length <= 100,
    )
  );
}

export function isPreferences(value: unknown): value is Preferences {
  return (
    isRecord(value) &&
    isList(value.allergies, 50) &&
    isList(value.dietaryRestrictions, 20) &&
    isList(value.likes, 50) &&
    isList(value.dislikes, 50) &&
    isList(value.preferredCuisines, 20) &&
    isList(value.nutritionGoals, 20) &&
    isSpice(value.spicePreference) &&
    isSkill(value.cookingSkill) &&
    typeof value.typicalTimeBudgetMinutes === "number" &&
    Number.isInteger(value.typicalTimeBudgetMinutes) &&
    value.typicalTimeBudgetMinutes >= 5 &&
    value.typicalTimeBudgetMinutes <= 240
  );
}

export function toDraft(preferences: Preferences | null): Draft {
  return {
    allergies: preferences?.allergies.join("\n") ?? "",
    dietaryRestrictions: preferences?.dietaryRestrictions.join("\n") ?? "",
    likes: preferences?.likes.join("\n") ?? "",
    dislikes: preferences?.dislikes.join("\n") ?? "",
    preferredCuisines: preferences?.preferredCuisines.join("\n") ?? "",
    spicePreference: preferences?.spicePreference ?? "",
    cookingSkill: preferences?.cookingSkill ?? "",
    typicalTimeBudgetMinutes: String(
      preferences?.typicalTimeBudgetMinutes ?? 30,
    ),
    nutritionGoals: preferences?.nutritionGoals.join("\n") ?? "",
  };
}

export function changedFields(draft: Draft, baseline: Draft): Field[] {
  return fields.filter((field) => draft[field] !== baseline[field]);
}

export function parseDraft(
  draft: Draft,
):
  | { preferences: Preferences; errors?: never }
  | { errors: FieldErrors; preferences?: never } {
  const errors: FieldErrors = {};
  function list(field: Field, limit: number): string[] {
    const values = draft[field]
      .split("\n")
      .map((value) => value.trim())
      .filter(Boolean);
    if (values.length > limit)
      errors[field] = `Use no more than ${limit} entries.`;
    else if (values.some((value) => [...value].length > 100))
      errors[field] = "Keep each entry to 100 characters or fewer.";
    return values;
  }
  const lists = {
    allergies: list("allergies", 50),
    dietaryRestrictions: list("dietaryRestrictions", 20),
    likes: list("likes", 50),
    dislikes: list("dislikes", 50),
    preferredCuisines: list("preferredCuisines", 20),
    nutritionGoals: list("nutritionGoals", 20),
  };
  const minutes = Number(draft.typicalTimeBudgetMinutes);
  if (!Number.isInteger(minutes) || minutes < 5 || minutes > 240)
    errors.typicalTimeBudgetMinutes = "Choose a whole number from 5 to 240.";
  if (!isSpice(draft.spicePreference))
    errors.spicePreference = "Choose a spice preference.";
  if (!isSkill(draft.cookingSkill))
    errors.cookingSkill = "Choose your cooking experience.";
  if (
    Object.keys(errors).length ||
    !isSpice(draft.spicePreference) ||
    !isSkill(draft.cookingSkill)
  )
    return { errors };
  return {
    preferences: {
      ...lists,
      spicePreference: draft.spicePreference,
      cookingSkill: draft.cookingSkill,
      typicalTimeBudgetMinutes: minutes,
    },
  };
}
