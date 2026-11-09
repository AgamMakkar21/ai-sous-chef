import { ApiError, getSession, json, request } from "./api.ts";
import { isRecord, type Session } from "./profile.ts";
import { isStructuredRecipe, type StructuredRecipe } from "./recipes.ts";

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
  generatedRecipeId?: string | null;
  recipe?: StructuredRecipe | null;
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
  const hasRecipe = value.recipe !== undefined && value.recipe !== null;
  const hasRecipeId =
    value.generatedRecipeId !== undefined && value.generatedRecipeId !== null;
  if (
    hasRecipe !== hasRecipeId ||
    (hasRecipe &&
      (value.status !== "completed" ||
        !isConversationId(value.generatedRecipeId) ||
        !isStructuredRecipe(value.recipe)))
  )
    return false;
  const messages = value.messages;
  return (
    messages.length === (value.status === "completed" ? 2 : 1) &&
    messages[0]?.role === "user" &&
    (value.status !== "completed" || messages[1]?.role === "assistant")
  );
}

export type ChatAttempt = {
  key: string;
  content: string;
  retryOfTurnId?: string;
};
export type Operation = {
  operationId: string;
  conversationId: string;
  turnId: string;
  status: Turn["status"];
  failureCode:
    | "execution_failed"
    | "invalid_assistant_response"
    | "execution_outcome_unknown"
    | null;
  turn: Turn;
};
type SendResult =
  | { kind: "completed"; turn: Turn }
  | { kind: "operation"; operationId: string; operation?: Operation };

function isOperation(
  value: unknown,
  conversationId: string,
): value is Operation {
  if (
    !isRecord(value) ||
    !isConversationId(value.operationId) ||
    value.conversationId !== conversationId ||
    !isTurn(value.turn, conversationId) ||
    value.turnId !== value.turn.turnId
  )
    return false;
  return (
    ((value.status === "pending" || value.status === "completed") &&
      value.failureCode === null &&
      (value.status !== "completed" || value.turn.status === "completed")) ||
    (value.status === "failed" &&
      (value.failureCode === "execution_failed" ||
        value.failureCode === "invalid_assistant_response")) ||
    (value.status === "outcomeUnknown" &&
      value.failureCode === "execution_outcome_unknown")
  );
}

function matchesAttempt(turn: Turn, attempt: ChatAttempt) {
  return (
    turn.messages[0]?.content === attempt.content &&
    (!attempt.retryOfTurnId || turn.turnId === attempt.retryOfTurnId)
  );
}

export async function inspectOperation(
  session: Session,
  conversationId: string,
  operationId: string,
  signal: AbortSignal,
): Promise<Operation> {
  await verifySession(session, signal);
  const value = await json(
    await request(
      `/api/v1/conversations/${conversationId}/operations/${operationId}`,
      { signal },
    ),
  );
  if (
    !isRecord(value) ||
    !isOperation(value.data, conversationId) ||
    value.data.operationId !== operationId
  )
    throw new ApiError(502, "invalid_response");
  return value.data;
}

export async function sendMessage(
  session: Session,
  conversationId: string,
  attempt: ChatAttempt,
  signal: AbortSignal,
): Promise<SendResult> {
  const current = await verifySession(session, signal);
  let response: Response;
  try {
    response = await request(
      `/api/v1/conversations/${conversationId}/turns`,
      {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": current.csrfToken,
          "Idempotency-Key": attempt.key,
        },
        body: JSON.stringify({
          content: attempt.content,
          ...(attempt.retryOfTurnId
            ? { retryOfTurnId: attempt.retryOfTurnId }
            : {}),
        }),
      },
      75_000,
    );
  } catch (error) {
    const prefix = `/api/v1/conversations/${conversationId}/operations/`;
    const location = error instanceof ApiError ? error.location : null;
    const operationId = location?.startsWith(prefix)
      ? location.slice(prefix.length)
      : null;
    if (
      error instanceof ApiError &&
      (error.status === 502 || error.status === 504) &&
      isConversationId(operationId)
    )
      return { kind: "operation", operationId };
    throw error;
  }
  const value = await json(response);
  if (
    response.status === 200 &&
    isRecord(value) &&
    isTurn(value.data, conversationId) &&
    value.data.status === "completed" &&
    matchesAttempt(value.data, attempt)
  )
    return { kind: "completed", turn: value.data };
  if (
    response.status === 202 &&
    isRecord(value) &&
    isOperation(value.data, conversationId) &&
    value.data.status === "pending" &&
    matchesAttempt(value.data.turn, attempt)
  )
    return {
      kind: "operation",
      operationId: value.data.operationId,
      operation: value.data,
    };
  throw new ApiError(502, "invalid_response");
}

const operationKey = "asc.chat-operations";
type OperationReference = { conversationId: string; operationId: string };

function operationReferences(owner: string): OperationReference[] {
  const saved = sessionStorage.getItem(operationKey);
  if (saved === null) return [];
  const value: unknown = JSON.parse(saved);
  if (
    !isRecord(value) ||
    typeof value.owner !== "string" ||
    !Array.isArray(value.operations) ||
    value.operations.length > 20 ||
    !value.operations.every(
      (entry) =>
        isRecord(entry) &&
        isConversationId(entry.conversationId) &&
        isConversationId(entry.operationId),
    )
  )
    throw new SyntaxError("Invalid saved operation references.");
  if (value.owner !== owner) {
    sessionStorage.removeItem(operationKey);
    return [];
  }
  return value.operations as OperationReference[];
}

export function savedOperation(owner: string, conversationId: string) {
  return operationReferences(owner).find(
    (entry) => entry.conversationId === conversationId,
  )?.operationId;
}

export function rememberOperation(
  owner: string,
  conversationId: string,
  operationId: string | null,
) {
  const operations = operationReferences(owner).filter(
    (entry) => entry.conversationId !== conversationId,
  );
  if (operationId) operations.push({ conversationId, operationId });
  sessionStorage.setItem(
    operationKey,
    JSON.stringify({ owner, operations: operations.slice(-20) }),
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
