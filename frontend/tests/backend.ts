import type { Page } from "@playwright/test";
import type { Preferences } from "../src/profile.ts";

export const preferences: Preferences = {
  allergies: ["peanuts"],
  dietaryRestrictions: ["vegetarian"],
  likes: ["lemon"],
  dislikes: ["olives"],
  preferredCuisines: ["Italian"],
  spicePreference: "mild",
  cookingSkill: "beginner",
  typicalTimeBudgetMinutes: 30,
  nutritionGoals: ["More vegetables"],
};

type Failure = {
  status: number;
  html?: boolean;
  commit?: boolean;
  invalid?: boolean;
  field?: boolean;
};
type Write = {
  body: unknown;
  method: string;
  headers: Record<string, string>;
  url: string;
};

export async function backend(
  page: Page,
  initial: Preferences | null = preferences,
) {
  await page.route("**/api/v1/groceries", (route) =>
    route.fulfill({
      headers: { ETag: '"revision-1"' },
      json: { items: [], nextCursor: null },
    }),
  );
  const writes: Write[] = [];
  const state: {
    profile: Preferences | null;
    completed: boolean;
    revision: number;
    sessions: number;
    token: string;
    sessionUser: string;
    profileUser: string;
    failure: Failure | null;
    getFailure: Failure | null;
  } = {
    profile: initial ? { ...initial } : null,
    completed: Boolean(initial),
    revision: 1,
    sessions: 0,
    token: "t".repeat(43),
    sessionUser: "synthetic-user",
    profileUser: "synthetic-user",
    failure: null,
    getFailure: null,
  };
  await page.route("**/auth/session", async (route) => {
    state.sessions++;
    await route.fulfill({
      json: { userId: state.sessionUser, csrfToken: state.token },
    });
  });
  await page.route("**/api/v1/conversations?*", (route) =>
    route.fulfill({ json: { items: [], nextCursor: null } }),
  );
  await page.route("**/api/v1/me**", async (route) => {
    const request = route.request();
    const write = request.method() !== "GET";
    const failure = write ? state.failure : state.getFailure;
    if (write) {
      state.failure = null;
      const body: unknown = request.postDataJSON();
      writes.push({
        body,
        headers: request.headers(),
        method: request.method(),
        url: request.url(),
      });
      if (!failure || failure.commit) {
        if (typeof body !== "object" || body === null || Array.isArray(body))
          throw new Error("Profile writes must contain an object.");
        state.profile = { ...(state.profile ?? preferences), ...body };
        state.completed = true;
        state.revision++;
      }
    }
    if (failure?.status === 0) {
      await route.abort("failed");
    } else if (failure?.invalid) {
      await route.fulfill({ status: 200, json: { unexpected: true } });
    } else if (failure) {
      await route.fulfill({
        status: failure.status,
        contentType: failure.html ? "text/html" : "application/problem+json",
        body: failure.html
          ? "<html><body>Sign in at the identity provider.</body></html>"
          : JSON.stringify({
              code: failure.status === 403 ? "csrf_failed" : "invalid_input",
              detail: "private-provider-details",
              errors: failure.field
                ? [
                    {
                      field: "dietaryRestrictions",
                      code: "unsupported_constraint",
                    },
                  ]
                : [],
            }),
      });
    } else {
      await route.fulfill({
        headers: {
          ETag: `"revision-${state.revision}"`,
          "Cache-Control": "no-store",
        },
        json: {
          data: {
            userId: state.profileUser,
            onboardingCompleted: state.completed,
            explicitPreferences: state.profile,
          },
        },
      });
    }
  });
  return { state, writes };
}
