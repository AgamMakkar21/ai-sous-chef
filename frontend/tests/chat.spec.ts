import { expect, test, type Page } from "@playwright/test";
import type { Operation, Turn } from "../src/conversations.ts";
import { backend } from "./backend.ts";

const id = "11111111-1111-4111-8111-111111111111";
const otherId = "22222222-2222-4222-8222-222222222222";
const timestamp = "2026-10-29T12:00:00.000000Z";
const reply = "Which ingredients would you like to use?";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((complete) => {
    resolve = complete;
  });
  return { promise, resolve: () => resolve() };
}

function savedTurn(
  content: string,
  index = 0,
  status: Turn["status"] = "completed",
): Turn {
  return {
    turnId: crypto.randomUUID(),
    conversationId: id,
    turnIndex: index,
    status,
    messages: [
      { messageId: crypto.randomUUID(), role: "user", content, timestamp },
      ...(status === "completed"
        ? [
            {
              messageId: crypto.randomUUID(),
              role: "assistant" as const,
              content: reply,
              timestamp,
            },
          ]
        : []),
    ],
  };
}

async function chatBackend(page: Page) {
  const profile = await backend(page);
  const state = {
    turns: [] as Turn[],
    operations: new Map<string, Operation>(),
    receipts: new Map<string, { body: unknown; operationId: string }>(),
    writes: [] as {
      key: string;
      token: string | undefined;
      body: { content: string; retryOfTurnId?: string };
    }[],
    reads: [] as string[],
    generations: 0,
    mode: "success" as
      | "success"
      | "failed"
      | "pending"
      | "unknown"
      | "transport"
      | "partial"
      | "foreign"
      | "mismatched"
      | "csrf"
      | "expired"
      | "conflict",
    gate: null as ReturnType<typeof deferred> | null,
    location: null as string | null,
  };
  function view(operation: Operation): Operation {
    return {
      ...operation,
      turn: state.turns.find((turn) => turn.turnId === operation.turnId)!,
    };
  }
  function complete() {
    for (const operation of state.operations.values()) {
      if (operation.status !== "pending") continue;
      const turn = state.turns.find(
        (item) => item.turnId === operation.turnId,
      )!;
      turn.status = "completed";
      turn.messages.push({
        messageId: crypto.randomUUID(),
        role: "assistant",
        content: reply,
        timestamp,
      });
      operation.status = "completed";
      operation.failureCode = null;
    }
  }
  await page.route("**/api/v1/conversations**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const parts = url.pathname.split("/");
    if (request.method() === "GET") {
      state.reads.push(url.pathname + url.search);
      if (parts[5] === "operations") {
        expect(url.search).toBe("");
        const operation = state.operations.get(parts[6]!);
        if (!operation) throw new Error("Unexpected operation read.");
        await route.fulfill({ json: { data: view(operation) } });
      } else if (parts[5] === "turns") {
        expect(url.searchParams.get("limit")).toBe("20");
        const start = url.searchParams.has("cursor") ? 20 : 0;
        const turns = parts[4] === id ? state.turns : [];
        await route.fulfill({
          json: {
            items: turns.slice(start, start + 20),
            nextCursor: turns.length > start + 20 ? "next-page" : null,
          },
        });
      } else {
        await route.fulfill({
          json: {
            items: [id, otherId].map((conversationId, index) => ({
              conversationId,
              title: index === 0 ? "Dinner requests" : "Another chat",
              createdAt: timestamp,
              updatedAt: timestamp,
            })),
            nextCursor: null,
          },
        });
      }
      return;
    }
    expect(request.method()).toBe("POST");
    expect(url.pathname).toBe(`/api/v1/conversations/${id}/turns`);
    const key = request.headers()["idempotency-key"];
    if (!key) throw new Error("A send requires an idempotency key.");
    const body = request.postDataJSON() as {
      content: string;
      retryOfTurnId?: string;
    };
    state.writes.push({ key, body, token: request.headers()["x-csrf-token"] });
    const mode = state.mode;
    state.mode = "success";
    if (mode === "csrf" || mode === "expired" || mode === "conflict") {
      await route.fulfill({
        status: mode === "csrf" ? 403 : mode === "conflict" ? 409 : 401,
        contentType:
          mode === "expired" ? "text/html" : "application/problem+json",
        body:
          mode !== "expired"
            ? JSON.stringify({
                code: mode === "csrf" ? "csrf_failed" : "send_conflict",
                detail: "private-provider-details",
              })
            : "<html>private-provider-details</html>",
      });
      return;
    }
    const receipt = state.receipts.get(key);
    let operation = receipt
      ? state.operations.get(receipt.operationId)
      : undefined;
    if (receipt) expect(body).toEqual(receipt.body);
    else {
      expect(
        state.turns.some(
          (turn) =>
            turn.status === "pending" || turn.status === "outcomeUnknown",
        ),
      ).toBe(false);
      const status =
        mode === "failed"
          ? "failed"
          : mode === "pending"
            ? "pending"
            : mode === "unknown"
              ? "outcomeUnknown"
              : "completed";
      let turn = body.retryOfTurnId
        ? state.turns.find((item) => item.turnId === body.retryOfTurnId)
        : undefined;
      if (body.retryOfTurnId) {
        expect(turn?.status).toBe("failed");
        expect(turn?.messages[0]?.content).toBe(body.content);
        turn!.status = status;
        turn!.messages = [
          turn!.messages[0]!,
          ...(status === "completed"
            ? [
                {
                  messageId: crypto.randomUUID(),
                  role: "assistant" as const,
                  content: reply,
                  timestamp,
                },
              ]
            : []),
        ];
      } else {
        turn = savedTurn(body.content, state.turns.length, status);
        state.turns.push(turn);
      }
      operation = {
        operationId: crypto.randomUUID(),
        conversationId: id,
        turnId: turn!.turnId,
        status,
        failureCode:
          status === "failed"
            ? "execution_failed"
            : status === "outcomeUnknown"
              ? "execution_outcome_unknown"
              : null,
        turn: turn!,
      };
      state.operations.set(operation.operationId, operation);
      state.receipts.set(key, { body, operationId: operation.operationId });
      state.generations++;
    }
    if (!operation) throw new Error("Missing synthetic operation.");
    const gate = state.gate;
    state.gate = null;
    if (gate) await gate.promise;
    if (mode === "transport") {
      await route.abort("failed");
      return;
    }
    const headers = {
      Location:
        state.location ??
        `/api/v1/conversations/${id}/operations/${operation.operationId}`,
    };
    if (
      operation.status === "failed" ||
      operation.status === "outcomeUnknown"
    ) {
      await route.fulfill({
        status: operation.status === "failed" ? 502 : 504,
        headers,
        json: {
          code: operation.failureCode,
          detail: "private-provider-details",
        },
      });
    } else if (operation.status === "pending") {
      await route.fulfill({
        status: 202,
        headers: { ...headers, "Retry-After": "2" },
        json: { data: view(operation) },
      });
    } else if (
      mode === "partial" ||
      mode === "foreign" ||
      mode === "mismatched"
    ) {
      await route.fulfill({
        json: {
          data: {
            ...operation.turn,
            ...(mode === "partial" ? { status: "pending" } : {}),
            ...(mode === "foreign" ? { conversationId: otherId } : {}),
            messages: [
              {
                ...operation.turn.messages[0],
                ...(mode === "mismatched"
                  ? { content: "a different request" }
                  : {}),
              },
              {
                ...operation.turn.messages[1],
                content: "private-unvalidated-model-output",
              },
            ],
          },
        },
      });
    } else await route.fulfill({ headers, json: { data: operation.turn } });
  });
  return { ...profile, chat: state, complete };
}

async function choose(page: Page, title = "Dinner requests") {
  const toggle = page.getByRole("button", {
    name: "Conversations",
    exact: true,
  });
  if ((await toggle.getAttribute("aria-expanded")) === "false")
    await toggle.click();
  await page.getByRole("button", { name: title, exact: false }).click();
  await expect(
    page.getByRole("button", { name: "Send request", exact: true }),
  ).toBeVisible();
}

async function open(page: Page) {
  await page.goto("/#sous-chef");
  await choose(page);
  await expect(
    page.getByRole("button", { name: "Send request", exact: true }),
  ).toBeEnabled();
}

const requestInput = (page: Page) =>
  page.getByRole("textbox", { name: "What would you like to cook?" });
const sendButton = (page: Page) =>
  page.getByRole("button", { name: "Send request", exact: true });

test("keyboard composition, slow sending and synchronous double submission save exactly one exchange", async ({
  page,
}) => {
  const { chat, state } = await chatBackend(page);
  await open(page);
  state.token = "r".repeat(43);
  const gate = deferred();
  chat.gate = gate;
  await requestInput(page).fill("Quick dinner");
  await requestInput(page).press("Shift+Enter");
  await requestInput(page).press("k");
  await requestInput(page).press("Enter");
  await expect.poll(() => chat.writes.length).toBe(1);
  await expect(
    page.getByText("Sending your request.", { exact: false }),
  ).toBeVisible();
  await expect(requestInput(page)).toHaveValue("Quick dinner\nk");
  await expect(requestInput(page)).toHaveAttribute("readonly");
  await expect(
    page.getByRole("tab", { name: "Groceries", exact: true }),
  ).toBeDisabled();
  await requestInput(page).press("Enter");
  await page
    .getByRole("button", { name: "Sending...", exact: true })
    .evaluate((button: HTMLButtonElement) => {
      button.click();
      button.click();
    });
  expect(chat.writes).toHaveLength(1);
  gate.resolve();
  await expect(page.getByText(reply, { exact: true })).toBeVisible();
  await expect(requestInput(page)).toHaveValue("");
  expect(chat.writes[0]?.body).toEqual({ content: "Quick dinner\nk" });
  expect(chat.writes[0]?.token).toBe("r".repeat(43));
  expect(chat.turns).toHaveLength(1);
  expect(chat.generations).toBe(1);
  await requestInput(page).fill("A second idea");
  await sendButton(page).evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect.poll(() => chat.writes.length).toBe(2);
  await expect(requestInput(page)).toHaveValue("");
  expect(chat.turns).toHaveLength(2);
  expect(chat.writes[1]?.key).not.toBe(chat.writes[0]?.key);
  await expect(
    page.getByText("Quick dinner\nk", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("A second idea", { exact: true })).toBeVisible();
});

test("input validation preserves drafts and counts Unicode code points rather than UTF-16 units", async ({
  page,
}) => {
  const { chat } = await chatBackend(page);
  await open(page);
  await requestInput(page).fill(" \n ");
  await requestInput(page).press("Enter");
  await expect(
    page.getByText("Write a request between 1 and 4,000 characters."),
  ).toBeVisible();
  await requestInput(page).fill("x".repeat(4001));
  await sendButton(page).click();
  await expect(requestInput(page)).toHaveValue("x".repeat(4001));
  expect(chat.writes).toHaveLength(0);
  await requestInput(page).fill(` ${"🍅".repeat(4000)} `);
  await sendButton(page).click();
  await expect(requestInput(page)).toHaveValue("");
  expect(chat.writes[0]?.body.content).toBe("🍅".repeat(4000));
});

test("known agent failure retries the original text with a new key and the same saved turn", async ({
  page,
}) => {
  const { chat } = await chatBackend(page);
  await open(page);
  chat.mode = "failed";
  await requestInput(page).fill("Dinner with beans");
  await sendButton(page).click();
  await expect(page.getByRole("alert")).toContainText(
    "I couldn't complete that recipe request",
  );
  await expect(requestInput(page)).toHaveValue("Dinner with beans");
  await expect(sendButton(page)).toBeDisabled();
  const failedId = chat.turns[0]!.turnId;
  const messageId = chat.turns[0]!.messages[0]!.messageId;
  await page.getByRole("button", { name: "Retry saved request" }).click();
  await expect(page.getByText(reply, { exact: true })).toBeVisible();
  expect(chat.writes).toHaveLength(2);
  expect(chat.writes[1]?.key).not.toBe(chat.writes[0]?.key);
  expect(chat.writes[1]?.body).toEqual({
    content: "Dinner with beans",
    retryOfTurnId: failedId,
  });
  expect(chat.turns).toHaveLength(1);
  expect(chat.turns[0]?.turnIndex).toBe(0);
  expect(chat.turns[0]?.messages[0]?.messageId).toBe(messageId);
  await expect(page.locator("body")).not.toContainText(
    "private-provider-details",
  );
});

test("a committed but lost response is reconciled explicitly with the original key, body and fresh CSRF", async ({
  page,
}) => {
  const { chat, state } = await chatBackend(page);
  await open(page);
  chat.mode = "transport";
  await requestInput(page).fill("Something lemony");
  await sendButton(page).click();
  await expect(page.getByRole("alert")).toContainText(
    "I couldn't complete that recipe request",
  );
  await expect(requestInput(page)).toHaveValue("Something lemony");
  expect(chat.writes).toHaveLength(1);
  state.token = "r".repeat(43);
  await page.getByRole("button", { name: "Check this request" }).click();
  await expect(page.getByText(reply, { exact: true })).toBeVisible();
  expect(chat.writes).toHaveLength(2);
  expect(chat.writes[1]?.key).toBe(chat.writes[0]?.key);
  expect(chat.writes[1]?.body).toEqual(chat.writes[0]?.body);
  expect(chat.writes[1]?.token).toBe("r".repeat(43));
  expect(chat.generations).toBe(1);
  expect(chat.turns).toHaveLength(1);
});

test("pending operations survive reload and finish through read-only status polling, never another send", async ({
  page,
}) => {
  const { chat, complete } = await chatBackend(page);
  await open(page);
  chat.mode = "pending";
  await requestInput(page).fill("Slow dinner request");
  await sendButton(page).click();
  await expect(
    page.getByText("Status is checked automatically", { exact: false }),
  ).toBeVisible();
  await expect(sendButton(page)).toBeDisabled();
  const storage = await page.evaluate(() => JSON.stringify(sessionStorage));
  expect(storage).not.toContain("Slow dinner request");
  expect(storage).not.toContain("synthetic-user");
  page.once("dialog", (dialog) => dialog.accept());
  await page.reload();
  await expect(
    page.getByText("Status is checked automatically", { exact: false }),
  ).toBeVisible();
  expect(chat.writes).toHaveLength(1);
  complete();
  await expect(page.getByText(reply, { exact: true })).toBeVisible({
    timeout: 8000,
  });
  await expect(sendButton(page)).toBeEnabled();
  expect(chat.writes).toHaveLength(1);
  expect(chat.reads.some((read) => read.includes("/operations/"))).toBe(true);
});

for (const status of ["pending", "failed", "outcomeUnknown"] as const) {
  test(`persisted ${status} turns reload truthfully without a remembered operation or replay`, async ({
    page,
  }) => {
    const { chat } = await chatBackend(page);
    chat.turns = [savedTurn("Previously accepted request", 0, status)];
    await page.goto("/#sous-chef");
    await choose(page);
    await expect(
      page.getByRole("region", { name: "Saved request recovery" }),
    ).toContainText(
      status === "failed"
        ? "I couldn't complete that recipe request"
        : status === "pending"
          ? "This request is pending"
          : "The outcome is unknown",
    );
    if (status === "failed") {
      await page.getByRole("button", { name: "Retry saved request" }).click();
      await expect(page.getByText(reply, { exact: true })).toBeVisible();
      expect(chat.writes[0]?.body.retryOfTurnId).toBe(chat.turns[0]?.turnId);
      expect(chat.turns).toHaveLength(1);
    } else {
      await expect(sendButton(page)).toBeDisabled();
      await expect(
        page.getByRole("button", { name: "Retry saved request" }),
      ).toHaveCount(0);
      await page.getByRole("button", { name: "Check saved history" }).click();
      expect(chat.writes).toHaveLength(0);
    }
  });
}

test("unknown execution outcomes remain fenced across status checks and reload", async ({
  page,
}) => {
  const { chat } = await chatBackend(page);
  await open(page);
  chat.mode = "unknown";
  await requestInput(page).fill("An uncertain request");
  await sendButton(page).click();
  await expect(page.getByRole("alert")).toContainText(
    "The outcome is unknown.",
  );
  await expect(sendButton(page)).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Retry saved request" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Check saved status" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "The outcome is unknown.",
  );
  page.once("dialog", (dialog) => dialog.accept());
  await page.reload();
  await expect(page.getByRole("alert")).toContainText(
    "The outcome is unknown.",
  );
  expect(chat.writes).toHaveLength(1);
  await expect(page.locator("body")).not.toContainText(
    "private-provider-details",
  );
});

for (const mode of ["partial", "foreign", "mismatched"] as const) {
  test(`${mode} send responses never display unvalidated output or create a fresh retry`, async ({
    page,
  }) => {
    const { chat } = await chatBackend(page);
    await open(page);
    chat.mode = mode;
    await requestInput(page).fill("An original request");
    await sendButton(page).click();
    await expect(page.getByRole("alert")).toContainText(
      "I couldn't complete that recipe request",
    );
    await expect(requestInput(page)).toHaveValue("An original request");
    await expect(page.locator("body")).not.toContainText(
      "private-unvalidated-model-output",
    );
    await page.getByRole("button", { name: "Check this request" }).click();
    await expect(page.getByText(reply, { exact: true })).toBeVisible();
    expect(chat.writes[1]?.key).toBe(chat.writes[0]?.key);
    expect(chat.generations).toBe(1);
  });
}

test("CSRF rejection never auto-replays; explicit reconciliation refreshes the token and keeps the key", async ({
  page,
}) => {
  const { chat, state } = await chatBackend(page);
  await open(page);
  chat.mode = "csrf";
  await requestInput(page).fill("Keep this request");
  await sendButton(page).click();
  await expect(page.getByRole("alert")).toContainText(
    "Your security token changed.",
  );
  expect(chat.writes).toHaveLength(1);
  expect(chat.generations).toBe(0);
  state.token = "r".repeat(43);
  await page.getByRole("button", { name: "Check this request" }).click();
  await expect(page.getByText(reply, { exact: true })).toBeVisible();
  expect(chat.writes[1]?.key).toBe(chat.writes[0]?.key);
  expect(chat.writes[1]?.token).toBe("r".repeat(43));
});

for (const expiry of ["html401", "identity"] as const) {
  test(`${expiry} clears private drafts and transcript without replaying after expiry`, async ({
    page,
  }) => {
    const { chat, state } = await chatBackend(page);
    chat.turns = [savedTurn("Private saved request")];
    await open(page);
    await requestInput(page).fill("Private unsent request");
    if (expiry === "html401") chat.mode = "expired";
    else state.sessionUser = "different-synthetic-user";
    await sendButton(page).click();
    await expect(
      page.getByRole("link", { name: "Sign in", exact: true }),
    ).toBeVisible();
    await expect(page.locator("body")).not.toContainText(
      "Private saved request",
    );
    await expect(page.locator("body")).not.toContainText(
      "Private unsent request",
    );
    await expect(page.locator("body")).not.toContainText(
      "private-provider-details",
    );
    expect(chat.writes).toHaveLength(expiry === "html401" ? 1 : 0);
    expect(chat.generations).toBe(0);
    const storage = await page.evaluate(() =>
      JSON.stringify({ session: sessionStorage, local: localStorage }),
    );
    expect(storage).not.toContain("Private");
  });
}

test("a browser send timeout preserves the uncertain request and never treats abort as remote cancellation", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const timeout = AbortSignal.timeout.bind(AbortSignal);
    AbortSignal.timeout = (milliseconds) =>
      timeout(milliseconds > 60_000 ? 500 : milliseconds);
  });
  const { chat } = await chatBackend(page);
  await open(page);
  const gate = deferred();
  chat.gate = gate;
  await requestInput(page).fill("Deadline request");
  await sendButton(page).click();
  await expect(page.getByRole("alert")).toContainText(
    "No completed answer is confirmed.",
  );
  await expect(requestInput(page)).toHaveValue("Deadline request");
  expect(chat.writes).toHaveLength(1);
  gate.resolve();
  await page.getByRole("button", { name: "Check this request" }).click();
  await expect(page.getByText(reply, { exact: true })).toBeVisible();
  expect(chat.writes[1]?.key).toBe(chat.writes[0]?.key);
  expect(chat.generations).toBe(1);
});

test("new replies remain visible outside a full history page without scanning lifetime history", async ({
  page,
}) => {
  const { chat } = await chatBackend(page);
  chat.turns = Array.from({ length: 20 }, (_, index) =>
    savedTurn(`Older request ${index}`, index),
  );
  await open(page);
  const historyReads = chat.reads.filter((read) =>
    read.includes("/turns?"),
  ).length;
  await requestInput(page).fill("The newest request");
  await sendButton(page).click();
  await expect(
    page.getByRole("region", { name: "Recent requests" }),
  ).toContainText("The newest request");
  await expect(requestInput(page)).toHaveValue("");
  expect(chat.reads.filter((read) => read.includes("/turns?"))).toHaveLength(
    historyReads,
  );
  expect(chat.turns).toHaveLength(21);
  await expect(
    page
      .getByRole("list", { name: "Conversation transcript" })
      .getByRole("article"),
  ).toHaveCount(40);
});

test("navigation warns before discarding a draft and cancelling preserves input without browser persistence", async ({
  page,
}) => {
  await chatBackend(page);
  await open(page);
  await requestInput(page).fill("A draft to keep");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("tab", { name: "Groceries", exact: true }).click();
  await expect(requestInput(page)).toHaveValue("A draft to keep");
  page.once("dialog", (dialog) => dialog.dismiss());
  await choose(page, "Another chat");
  await expect(requestInput(page)).toHaveValue("A draft to keep");
  const storage = await page.evaluate(() =>
    JSON.stringify({ session: sessionStorage, local: localStorage }),
  );
  expect(storage).not.toContain("A draft to keep");
});

test("a concurrent-send conflict preserves the draft and exposes the pending turn through bounded history", async ({
  page,
}) => {
  const { chat } = await chatBackend(page);
  chat.turns = [
    ...Array.from({ length: 20 }, (_, index) =>
      savedTurn(`Older request ${index}`, index),
    ),
    savedTurn("Another tab's active request", 20, "pending"),
  ];
  await open(page);
  chat.mode = "conflict";
  await requestInput(page).fill("Keep my new idea");
  await sendButton(page).click();
  await expect(page.getByRole("alert")).toContainText(
    "Another request owns this chat",
  );
  await expect(requestInput(page)).toHaveValue("Keep my new idea");
  await page.getByRole("button", { name: "Later messages" }).click();
  await expect(
    page.getByRole("region", { name: "Saved request recovery" }),
  ).toContainText("This request is pending");
  await expect(sendButton(page)).toBeDisabled();
  expect(chat.writes).toHaveLength(1);
  expect(chat.generations).toBe(0);
  await expect(page.locator("body")).not.toContainText(
    "private-provider-details",
  );
});

test("foreign operation payloads are rejected and recovery reads the original owned operation without another send", async ({
  page,
}) => {
  const { chat } = await chatBackend(page);
  let malformed = true;
  await page.route(`**/conversations/${id}/operations/*`, async (route) => {
    if (!malformed) {
      await route.fallback();
      return;
    }
    const operation = [...chat.operations.values()][0]!;
    await route.fulfill({
      json: {
        data: {
          ...operation,
          turn: {
            ...operation.turn,
            conversationId: otherId,
            messages: [
              {
                ...operation.turn.messages[0],
                content: "private-foreign-operation-text",
              },
            ],
          },
        },
      },
    });
  });
  await open(page);
  chat.mode = "failed";
  await requestInput(page).fill("Original owned request");
  await sendButton(page).click();
  await expect(page.getByRole("alert")).toContainText(
    "No completed answer is confirmed.",
  );
  await expect(requestInput(page)).toHaveValue("Original owned request");
  await expect(page.locator("body")).not.toContainText(
    "private-foreign-operation-text",
  );
  malformed = false;
  await page.getByRole("button", { name: "Check saved status" }).click();
  await expect(
    page.getByRole("button", { name: "Retry saved request" }),
  ).toBeEnabled();
  expect(chat.writes).toHaveLength(1);
});

test("foreign Location headers are never followed; explicit reconciliation still uses the original request key", async ({
  page,
}) => {
  const { chat } = await chatBackend(page);
  let externalReads = 0;
  await page.route("https://untrusted.invalid/**", async (route) => {
    externalReads++;
    await route.abort();
  });
  await open(page);
  chat.mode = "failed";
  chat.location = `https://untrusted.invalid/api/v1/conversations/${id}/operations/${crypto.randomUUID()}`;
  await requestInput(page).fill("No untrusted redirects");
  await sendButton(page).click();
  await expect(page.getByRole("alert")).toContainText(
    "No completed answer is confirmed.",
  );
  expect(externalReads).toBe(0);
  expect(chat.reads.some((read) => read.includes("/operations/"))).toBe(false);
  chat.location = null;
  await page.getByRole("button", { name: "Check this request" }).click();
  await expect(
    page.getByRole("button", { name: "Retry saved request" }),
  ).toBeEnabled();
  expect(chat.writes[1]?.key).toBe(chat.writes[0]?.key);
  expect(chat.generations).toBe(1);
});

test("another account's saved operation reference is discarded before reading any operation", async ({
  page,
}) => {
  const { chat } = await chatBackend(page);
  await page.addInitScript(
    ({ conversationId, operationId }) => {
      sessionStorage.setItem(
        "asc.chat-operations",
        JSON.stringify({
          owner: "a".repeat(64),
          operations: [{ conversationId, operationId }],
        }),
      );
    },
    { conversationId: id, operationId: crypto.randomUUID() },
  );
  await open(page);
  expect(chat.reads.some((read) => read.includes("/operations/"))).toBe(false);
  expect(
    await page.evaluate(() => sessionStorage.getItem("asc.chat-operations")),
  ).toBeNull();
});
