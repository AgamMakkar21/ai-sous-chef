import {
  isField,
  isPreferences,
  isRecord,
  type FieldErrors,
  type Preferences,
  type Session,
  type Snapshot,
} from "./profile.ts";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fields: FieldErrors;

  constructor(status: number, code = "", fields: FieldErrors = {}) {
    super("The request could not be completed.");
    this.status = status;
    this.code = code;
    this.fields = fields;
  }
}

function fieldErrors(problem: unknown): FieldErrors {
  const errors: FieldErrors = {};
  if (!isRecord(problem) || !Array.isArray(problem.errors)) return errors;
  for (const entry of problem.errors) {
    if (!isRecord(entry) || typeof entry.field !== "string") continue;
    const field = entry.field.split(".")[0];
    if (!field || !isField(field)) continue;
    errors[field] =
      entry.code === "unsupported_constraint"
        ? "This cannot currently be enforced as a strict constraint. Review this entry."
        : "Check the entries in this field and its stated limits.";
  }
  return errors;
}

export async function request(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const deadline = AbortSignal.timeout(15_000);
  const signal = init.signal
    ? AbortSignal.any([init.signal, deadline])
    : deadline;
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      signal,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      headers,
    });
  } catch (error) {
    if (init.signal?.aborted) throw error;
    throw new ApiError(0, "transport");
  }
  if (response.status === 401) throw new ApiError(401);
  if (!response.ok) {
    let problem: unknown;
    try {
      problem = await response.json();
    } catch {
      throw new ApiError(response.status);
    }
    const code =
      isRecord(problem) &&
      typeof problem.code === "string" &&
      ["csrf_failed", "duplicate_grocery", "grocery_limit_reached"].includes(
        problem.code,
      )
        ? problem.code
        : "";
    throw new ApiError(response.status, code, fieldErrors(problem));
  }
  return response;
}

export async function json(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ApiError(502, "invalid_response");
  }
}

export async function getSession(signal?: AbortSignal): Promise<Session> {
  const value = await json(await request("/auth/session", { signal }));
  if (
    !isRecord(value) ||
    typeof value.userId !== "string" ||
    !value.userId ||
    typeof value.csrfToken !== "string" ||
    !/^[A-Za-z0-9_-]{20,256}$/.test(value.csrfToken)
  )
    throw new ApiError(502, "invalid_response");
  return { userId: value.userId, csrfToken: value.csrfToken };
}

async function snapshot(response: Response, userId: string): Promise<Snapshot> {
  const value = await json(response);
  const data = isRecord(value) ? value.data : undefined;
  const etag = response.headers.get("ETag");
  if (
    !isRecord(data) ||
    typeof data.userId !== "string" ||
    typeof data.onboardingCompleted !== "boolean" ||
    !(
      data.explicitPreferences === null ||
      isPreferences(data.explicitPreferences)
    ) ||
    (data.onboardingCompleted && data.explicitPreferences === null) ||
    !etag ||
    etag.length > 256 ||
    !/^"[^"\r\n]+"$/.test(etag)
  )
    throw new ApiError(502, "invalid_response");
  if (data.userId !== userId) throw new ApiError(401, "session_changed");
  return {
    profile: {
      userId: data.userId,
      onboardingCompleted: data.onboardingCompleted,
      explicitPreferences: data.explicitPreferences,
    },
    etag,
  };
}

export async function getProfile(
  session: Session,
  signal?: AbortSignal,
): Promise<Snapshot> {
  return snapshot(await request("/api/v1/me", { signal }), session.userId);
}

export async function saveProfile(
  session: Session,
  current: Snapshot,
  preferences: Partial<Preferences>,
): Promise<Snapshot> {
  const complete = current.profile.onboardingCompleted;
  const saved = await snapshot(
    await request(
      complete ? "/api/v1/me/preferences" : "/api/v1/me/onboarding",
      {
        method: complete ? "PATCH" : "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": session.csrfToken,
          "If-Match": current.etag,
        },
        body: JSON.stringify(preferences),
      },
    ),
    session.userId,
  );
  if (!saved.profile.onboardingCompleted)
    throw new ApiError(502, "invalid_response");
  return saved;
}

export const groceryUnits = [
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
] as const;
export type GroceryUnit = (typeof groceryUnits)[number];
function isGroceryUnit(value: unknown): value is GroceryUnit | null {
  return value === null || groceryUnits.some((unit) => unit === value);
}
export type GroceryInput = {
  name: string;
  quantity: number | null;
  unit: GroceryUnit | null;
};
export type Grocery = GroceryInput & { groceryId: string };
export type Inventory = { items: Grocery[]; etag: string };
export type GroceryChange =
  | { method: "POST"; input: GroceryInput }
  | { method: "PATCH"; id: string; input: { quantity: number | null } }
  | { method: "DELETE"; id: string };

export async function getGroceries(signal?: AbortSignal): Promise<Inventory> {
  const response = await request("/api/v1/groceries", { signal });
  const value = await json(response);
  const etag = response.headers.get("ETag");
  if (
    !isRecord(value) ||
    !Array.isArray(value.items) ||
    value.items.length > 200 ||
    value.nextCursor !== null ||
    !etag ||
    etag.length > 256 ||
    !/^"[^"\r\n]+"$/.test(etag)
  )
    throw new ApiError(502, "invalid_response");
  const items: Grocery[] = [];
  for (const row of value.items) {
    if (
      !isRecord(row) ||
      typeof row.groceryId !== "string" ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
        row.groceryId,
      ) ||
      items.some((item) => item.groceryId === row.groceryId) ||
      typeof row.name !== "string" ||
      !row.name.trim() ||
      [...row.name].length > 100 ||
      /[\p{Cc}\p{Cs}]/u.test(row.name) ||
      !(
        row.quantity === null ||
        (typeof row.quantity === "number" &&
          Number.isFinite(row.quantity) &&
          row.quantity > 0 &&
          row.quantity <= 1_000_000 &&
          Math.abs(row.quantity * 1000 - Math.round(row.quantity * 1000)) <
            0.000001)
      ) ||
      !isGroceryUnit(row.unit)
    )
      throw new ApiError(502, "invalid_response");
    items.push({
      groceryId: row.groceryId,
      name: row.name,
      quantity: row.quantity,
      unit: row.unit,
    });
  }
  return { items, etag };
}

export async function changeGroceries(
  session: Session,
  inventory: Inventory,
  change: GroceryChange,
): Promise<void> {
  await request(
    `/api/v1/groceries${change.method === "POST" ? "" : `/${encodeURIComponent(change.id)}`}`,
    {
      method: change.method,
      headers: {
        "X-CSRF-Token": session.csrfToken,
        "If-Match": inventory.etag,
        ...(change.method !== "DELETE"
          ? { "Content-Type": "application/json" }
          : {}),
      },
      ...(change.method !== "DELETE"
        ? { body: JSON.stringify(change.input) }
        : {}),
    },
  );
}
