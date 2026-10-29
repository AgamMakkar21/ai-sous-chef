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
      isRecord(problem) && problem.code === "csrf_failed" ? "csrf_failed" : "";
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
