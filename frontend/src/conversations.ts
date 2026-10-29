import { ApiError, getSession, json, request } from "./api.ts";
import { isRecord, type Session } from "./profile.ts";

export type Conversation = {
  conversationId: string;
  title: string;
  createdAt: string;
  updatedAt: string;
};
type Message = {
  messageId: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
};
export type Turn = {
  turnId: string;
  conversationId: string;
  turnIndex: number;
  status: "pending" | "completed" | "failed" | "outcomeUnknown";
  messages: Message[];
};
export type Page<T> = { items: T[]; nextCursor: string | null };
export type Navigation = {
  owner: string;
  selectedId: string | null;
  creationKey: string | null;
};
export const navigationKey = "asc.conversation-navigation";

export function isConversationId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      value,
    )
  );
}

function isTimestamp(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$/.test(value) &&
    Number.isFinite(Date.parse(value))
  );
}

function isConversation(value: unknown): value is Conversation {
  return (
    isRecord(value) &&
    isConversationId(value.conversationId) &&
    typeof value.title === "string" &&
    value.title.trim().length > 0 &&
    [...value.title].length <= 100 &&
    isTimestamp(value.createdAt) &&
    isTimestamp(value.updatedAt)
  );
}

function isMessage(value: unknown): value is Message {
  return (
    isRecord(value) &&
    isConversationId(value.messageId) &&
    (value.role === "user" || value.role === "assistant") &&
    typeof value.content === "string" &&
    value.content.trim().length > 0 &&
    [...value.content].length <= 4000 &&
    isTimestamp(value.timestamp)
  );
}

function isTurn(value: unknown, conversationId: string): value is Turn {
  if (
    !isRecord(value) ||
    !isConversationId(value.turnId) ||
    value.conversationId !== conversationId ||
    typeof value.turnIndex !== "number" ||
    !Number.isSafeInteger(value.turnIndex) ||
    value.turnIndex < 0 ||
    (value.status !== "pending" &&
      value.status !== "completed" &&
      value.status !== "failed" &&
      value.status !== "outcomeUnknown") ||
    !Array.isArray(value.messages) ||
    !value.messages.every(isMessage)
  )
    return false;
  const messages = value.messages;
  return (
    messages.length === (value.status === "completed" ? 2 : 1) &&
    messages[0]?.role === "user" &&
    (value.status !== "completed" || messages[1]?.role === "assistant")
  );
}

export function safeTitle(value: string): string {
  return (
    [
      ...value
        .replace(/[\p{Cc}\p{Cf}]/gu, " ")
        .replace(/\s+/gu, " ")
        .trim(),
    ]
      .slice(0, 80)
      .join("") || "New chat"
  );
}

export async function navigationOwner(userId: string): Promise<string> {
  const hash = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(userId),
  );
  return Array.from(new Uint8Array(hash), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export function readNavigation(owner: string): Navigation {
  const empty = { owner, selectedId: null, creationKey: null };
  const saved = sessionStorage.getItem(navigationKey);
  if (saved === null) return empty;
  const value: unknown = JSON.parse(saved);
  if (
    !isRecord(value) ||
    typeof value.owner !== "string" ||
    !(value.selectedId === null || isConversationId(value.selectedId)) ||
    !(value.creationKey === null || isConversationId(value.creationKey))
  )
    throw new SyntaxError("Invalid saved conversation navigation.");
  if (value.owner !== owner) {
    sessionStorage.removeItem(navigationKey);
    return empty;
  }
  return {
    owner,
    selectedId: value.selectedId,
    creationKey: value.creationKey,
  };
}

async function verifySession(session: Session, signal: AbortSignal) {
  const current = await getSession(signal);
  if (current.userId !== session.userId)
    throw new ApiError(401, "session_changed");
  return current;
}

async function page<T>(
  path: string,
  cursor: string | null,
  guard: (value: unknown) => value is T,
  signal: AbortSignal,
): Promise<Page<T>> {
  const query = new URLSearchParams({ limit: "20" });
  if (cursor !== null) query.set("cursor", cursor);
  const value = await json(await request(`${path}?${query}`, { signal }));
  if (
    !isRecord(value) ||
    !Array.isArray(value.items) ||
    value.items.length > 20 ||
    !value.items.every(guard) ||
    !(
      value.nextCursor === null ||
      (typeof value.nextCursor === "string" &&
        value.nextCursor.length > 0 &&
        value.nextCursor.length <= 24 * 1024)
    )
  )
    throw new ApiError(502, "invalid_response");
  return { items: value.items, nextCursor: value.nextCursor };
}

export async function listConversations(
  session: Session,
  cursor: string | null,
  signal: AbortSignal,
): Promise<Page<Conversation>> {
  await verifySession(session, signal);
  const result = await page(
    "/api/v1/conversations",
    cursor,
    isConversation,
    signal,
  );
  if (
    new Set(result.items.map((item) => item.conversationId)).size !==
    result.items.length
  )
    throw new ApiError(502, "invalid_response");
  return result;
}

export async function listTurns(
  session: Session,
  conversationId: string,
  cursor: string | null,
  signal: AbortSignal,
): Promise<Page<Turn>> {
  await verifySession(session, signal);
  const result = await page(
    `/api/v1/conversations/${conversationId}/turns`,
    cursor,
    (value): value is Turn => isTurn(value, conversationId),
    signal,
  );
  if (
    new Set(result.items.map((item) => item.turnId)).size !==
      result.items.length ||
    result.items.some(
      (item, index) =>
        index > 0 &&
        item.turnIndex <= (result.items[index - 1]?.turnIndex ?? -1),
    )
  )
    throw new ApiError(502, "invalid_response");
  return result;
}

export async function createConversation(
  session: Session,
  key: string,
  signal: AbortSignal,
): Promise<Conversation> {
  const current = await verifySession(session, signal);
  const value = await json(
    await request("/api/v1/conversations", {
      method: "POST",
      signal,
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": current.csrfToken,
        "Idempotency-Key": key,
      },
      body: "{}",
    }),
  );
  if (!isRecord(value) || !isConversation(value.data))
    throw new ApiError(502, "invalid_response");
  return value.data;
}
