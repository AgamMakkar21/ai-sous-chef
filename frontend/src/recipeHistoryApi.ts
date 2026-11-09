import { ApiError, getSession, json, request } from "./api.ts";
import { isConversationId, type Page } from "./conversations.ts";
import { isRecord, type Session } from "./profile.ts";
import { isStructuredRecipe, type StructuredRecipe } from "./recipes.ts";

export const ratingLabels = {
  liked: "Liked",
  fine: "Fine",
  disliked: "Didn't Like",
} as const;
export type Rating = keyof typeof ratingLabels;
export type RecipeSummary = {
  historyId: string;
  title: string;
  savedAt: string;
  cookedAt: string | null;
  state: "planned" | "cooked";
  awaitingFeedback: boolean;
  rating: Rating | null;
};
export type RecipeSnapshot = {
  data: Omit<RecipeSummary, "title"> & {
    generatedRecipeId: string;
    recipe: StructuredRecipe;
    note: string | null;
    feedbackRevision: number;
  };
  etag: string;
};

const timestamp = (value: unknown): value is string =>
  typeof value === "string" && Number.isFinite(Date.parse(value));
const isRating = (value: unknown): value is Rating =>
  typeof value === "string" && Object.hasOwn(ratingLabels, value);

function isSummary(value: unknown): value is RecipeSummary {
  return (
    isRecord(value) &&
    isConversationId(value.historyId) &&
    typeof value.title === "string" &&
    !!value.title.trim() &&
    [...value.title].length <= 160 &&
    timestamp(value.savedAt) &&
    (value.cookedAt === null || timestamp(value.cookedAt)) &&
    (value.rating === null || isRating(value.rating)) &&
    value.state === (value.cookedAt === null ? "planned" : "cooked") &&
    (value.cookedAt !== null || value.rating === null) &&
    value.awaitingFeedback ===
      (value.cookedAt !== null && value.rating === null)
  );
}

async function verify(session: Session, signal?: AbortSignal) {
  const current = await getSession(signal);
  if (current.userId !== session.userId)
    throw new ApiError(401, "session_changed");
  return current;
}

export async function listRecipes(
  session: Session,
  pending: boolean,
  cursor: string | null,
  signal: AbortSignal,
): Promise<Page<RecipeSummary>> {
  await verify(session, signal);
  const query = new URLSearchParams({ limit: "20" });
  if (pending) query.set("awaitingFeedback", "true");
  if (cursor) query.set("cursor", cursor);
  const value = await json(
    await request(`/api/v1/recipes?${query}`, { signal }),
  );
  if (
    !isRecord(value) ||
    !Array.isArray(value.items) ||
    value.items.length > 20 ||
    !value.items.every(isSummary) ||
    new Set(value.items.map((item) => item.historyId)).size !==
      value.items.length ||
    (pending && value.items.some((item) => !item.awaitingFeedback)) ||
    !(
      value.nextCursor === null ||
      (typeof value.nextCursor === "string" &&
        value.nextCursor.length > 0 &&
        value.nextCursor.length <= 24 * 1024 &&
        value.nextCursor !== cursor)
    )
  )
    throw new ApiError(502, "invalid_response");
  return { items: value.items, nextCursor: value.nextCursor };
}

async function snapshot(response: Response): Promise<RecipeSnapshot> {
  const value = await json(response);
  const data = isRecord(value) ? value.data : null;
  const etag = response.headers.get("ETag");
  if (
    !isRecord(data) ||
    !isStructuredRecipe(data.recipe) ||
    !isSummary({ ...data, title: data.recipe.title }) ||
    !isConversationId(data.generatedRecipeId) ||
    !(
      data.note === null ||
      (typeof data.note === "string" && [...data.note].length <= 2000)
    ) ||
    typeof data.feedbackRevision !== "number" ||
    !Number.isSafeInteger(data.feedbackRevision) ||
    data.feedbackRevision < 0 ||
    (data.rating === null) !== (data.feedbackRevision === 0) ||
    !etag ||
    etag.length > 256 ||
    !/^"[^"\r\n]+"$/.test(etag)
  )
    throw new ApiError(502, "invalid_response");
  // Reconstruct from validated fields rather than trusting an unchecked cast.
  const summary = { ...data, title: data.recipe.title };
  if (!isSummary(summary)) throw new ApiError(502, "invalid_response");
  return {
    data: {
      ...summary,
      generatedRecipeId: data.generatedRecipeId,
      recipe: data.recipe,
      note: data.note,
      feedbackRevision: data.feedbackRevision,
    },
    etag,
  };
}

export async function getRecipe(
  session: Session,
  id: string,
  signal?: AbortSignal,
): Promise<RecipeSnapshot> {
  await verify(session, signal);
  const result = await snapshot(
    await request(`/api/v1/recipes/${encodeURIComponent(id)}`, { signal }),
  );
  if (result.data.historyId !== id) throw new ApiError(502, "invalid_response");
  return result;
}

export async function changeRecipe(
  session: Session,
  change:
    | { kind: "save"; generatedRecipeId: string }
    | { kind: "cooked"; current: RecipeSnapshot }
    | {
        kind: "feedback";
        current: RecipeSnapshot;
        rating: Rating;
        note: string | null;
      },
  key: string,
): Promise<RecipeSnapshot> {
  const currentSession = await verify(session);
  const path =
    change.kind === "save"
      ? "/api/v1/recipes"
      : `/api/v1/recipes/${encodeURIComponent(change.current.data.historyId)}/${change.kind}`;
  const result = await snapshot(
    await request(path, {
      method: change.kind === "feedback" ? "PUT" : "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": currentSession.csrfToken,
        "Idempotency-Key": key,
        ...(change.kind === "save" ? {} : { "If-Match": change.current.etag }),
      },
      body: JSON.stringify(
        change.kind === "save"
          ? { generatedRecipeId: change.generatedRecipeId, cooked: false }
          : change.kind === "feedback"
            ? { rating: change.rating, note: change.note }
            : {},
      ),
    }),
  );
  if (
    (change.kind === "save"
      ? result.data.generatedRecipeId !== change.generatedRecipeId
      : result.data.historyId !== change.current.data.historyId) ||
    (change.kind === "cooked" && result.data.state !== "cooked") ||
    (change.kind === "feedback" &&
      (result.data.rating !== change.rating ||
        result.data.note !== change.note))
  )
    throw new ApiError(502, "invalid_response");
  notifyRecipeHistory();
  return result;
}

const changed = "asc-recipe-history-changed";

export function notifyRecipeHistory() {
  window.dispatchEvent(new Event(changed));
  if (typeof BroadcastChannel !== "undefined") {
    const channel = new BroadcastChannel(changed);
    channel.postMessage(changed);
    channel.close();
  }
}

export function watchRecipeHistory(refresh: () => void) {
  const channel =
    typeof BroadcastChannel !== "undefined"
      ? new BroadcastChannel(changed)
      : null;
  if (channel) channel.onmessage = refresh;
  window.addEventListener(changed, refresh);
  window.addEventListener("focus", refresh);
  return () => {
    channel?.close();
    window.removeEventListener(changed, refresh);
    window.removeEventListener("focus", refresh);
  };
}
