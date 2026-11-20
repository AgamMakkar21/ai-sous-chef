import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { Operation, Turn } from "../src/conversations.ts";
import { foodFacts } from "../src/foodFacts.ts";
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
    closes: [] as string[],
    generations: 0,
    mode: "success" as
      | "success"
      | "failed"
      | "pending"
      | "lostPending"
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
        expect(parts[4]).toBe(id);
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
    if (url.pathname === "/api/v1/conversations") {
      await route.fulfill({
        status: 201,
        json: {
          data: {
            conversationId: id,
            title: "New conversation",
            createdAt: timestamp,
            updatedAt: timestamp,
          },
        },
      });
      return;
    }
    if (parts[5] === "operations" && parts[7] === "close") {
      expect(request.headers()["x-csrf-token"]).toBeTruthy();
      expect(request.headers()["idempotency-key"]).toBe(parts[6]);
      const operation = state.operations.get(parts[6]!);
      if (!operation || operation.status !== "outcomeUnknown")
        throw new Error("Only an interrupted synthetic operation can close.");
      state.closes.push(url.pathname);
      operation.status = "failed";
      operation.failureCode = "execution_closed";
      operation.turn.status = "failed";
      await route.fulfill({ json: { data: view(operation) } });
      return;
    }
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
          : mode === "pending" || mode === "lostPending"
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
      turn!.operationId = operation.operationId;
      state.operations.set(operation.operationId, operation);
      state.receipts.set(key, { body, operationId: operation.operationId });
      state.generations++;
    }
    if (!operation) throw new Error("Missing synthetic operation.");
    const gate = state.gate;
    state.gate = null;
    if (gate) await gate.promise;
    if (mode === "transport" || mode === "lostPending") {
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
    page.getByText("Waiting for your recipe.", { exact: true }),
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
    page
      .getByRole("region", { name: "Messages", exact: true })
      .getByText("Quick dinner\nk", { exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("region", { name: "Messages", exact: true })
      .getByText("A second idea", { exact: true }),
  ).toBeVisible();
});

for (const phase of ["sending", "pending"] as const) {
  test(`a slow ${phase} request can complete after 90 seconds without resubmitting`, async ({
    page,
  }) => {
    await page.clock.install();
    await page.addInitScript(() => {
      AbortSignal.timeout = (milliseconds) => {
        const controller = new AbortController();
        setTimeout(
          () =>
            controller.abort(
              new DOMException("Request timed out.", "TimeoutError"),
            ),
          milliseconds,
        );
        return controller.signal;
      };
    });
    const { chat, complete } = await chatBackend(page);
    await open(page);
    const gate = deferred();
    if (phase === "sending") chat.gate = gate;
    else chat.mode = "pending";
    await requestInput(page).fill("A slow recipe request");
    await sendButton(page).click();
    const notice = page.getByText("Waiting for your recipe.", { exact: true });
    await expect(notice).toBeVisible();
    await page.clock.fastForward(90_000);
    await expect(notice).toBeVisible();
    if (phase === "sending") gate.resolve();
    else {
      complete();
      await page.clock.fastForward(2_000);
    }
    await expect(page.getByText(reply, { exact: true })).toBeVisible();
    expect(chat.writes).toHaveLength(1);
    expect(chat.generations).toBe(1);
  });
}

test("waiting facts hold for two seconds, cycle every fact, pause fully readable and reset after completion", async ({
  page,
}, info) => {
  test.setTimeout(90_000);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.clock.install({ time: new Date("2026-11-19T20:00:00Z") });
  await page.clock.pauseAt(new Date("2026-11-19T20:00:01Z"));
  const { chat } = await chatBackend(page);
  await open(page);
  const gate = deferred();
  chat.gate = gate;
  await requestInput(page).fill("A cozy soup");
  await sendButton(page).click();
  const waiting = page.locator(".recipe-waiting");
  const entries = waiting.locator(".word-loader-item");
  const intro = waiting.locator(".waiting-fact-intro");
  const fact = (index: number) => entries.nth(index);
  const readable = (index: number) =>
    fact(index)
      .locator(".char")
      .evaluateAll((chars) =>
        chars.every((char) => getComputedStyle(char).opacity === "1"),
      );
  await expect(waiting).toBeVisible();
  await expect(entries).toHaveCount(26);
  await expect(intro).toHaveText("Did you know...");
  for (const entry of await entries.all()) {
    const text = `Did you know ${await entry.getAttribute("data-fact")}`;
    expect(text).toMatch(/^Did you know [^.!?]+\?$/);
    expect(text.split(/\s+/).length).toBeLessThanOrEqual(14);
  }
  const introBox = await intro.boundingBox();
  await page.clock.runFor(100);
  const entrance = await fact(0)
    .locator(".char")
    .evaluateAll((chars) =>
      chars.map((char) => Number(getComputedStyle(char).opacity)),
    );
  expect(entrance[0]).toBeGreaterThan(entrance.at(-1)!);
  await page.clock.runFor(500);
  await expect(fact(0)).toBeVisible();
  expect(await readable(0)).toBe(true);
  expect(await intro.boundingBox()).toEqual(introBox);
  const steamTravel = await waiting
    .locator(".pot-steam")
    .first()
    .evaluate((element) => {
      const animation = element.getAnimations()[0]!;
      animation.pause();
      animation.currentTime = 500;
      const start = getComputedStyle(element).transform;
      animation.currentTime = 1_500;
      const end = getComputedStyle(element).transform;
      animation.play();
      return { start, end };
    });
  expect(steamTravel.start).not.toBe(steamTravel.end);
  await page.clock.runFor(1_800);
  await expect(fact(0)).toBeVisible();
  expect(await readable(0)).toBe(true);
  await page.clock.runFor(1_200);
  await expect(fact(1)).toBeVisible();
  for (let index = 2; index <= foodFacts.length; index += 1) {
    await page.clock.runFor(3_000);
    await expect(fact(index % foodFacts.length)).toBeVisible();
    expect(await readable(index % foodFacts.length)).toBe(true);
  }
  const pause = page.getByRole("button", { name: "Pause food facts" });
  const pauseSize = await pause.boundingBox();
  expect(pauseSize!.height).toBeGreaterThanOrEqual(24);
  expect(pauseSize!.height).toBeLessThanOrEqual(28);
  expect(pauseSize!.width).toBeLessThanOrEqual(64);
  await pause.click();
  await expect(waiting).toHaveAttribute("data-paused", "true");
  await page.clock.runFor(10_000);
  await expect(fact(0)).toBeVisible();
  expect(await readable(0)).toBe(true);
  await expect(waiting.locator(".pot-steam").first()).toHaveCSS(
    "animation-play-state",
    "paused",
  );
  for (const entry of await entries.all()) {
    const overflow = await entry.evaluate(
      (element) => element.scrollWidth > element.clientWidth + 1,
    );
    expect(overflow).toBe(false);
  }
  await page.screenshot({
    path: info.outputPath("waiting-food-facts.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Resume food facts" }).click();
  await page.clock.runFor(3_000);
  await expect(fact(1)).toBeVisible();
  gate.resolve();
  await expect(page.getByText(reply, { exact: true })).toBeVisible();
  await expect(waiting).toHaveCount(0);
  const next = deferred();
  chat.gate = next;
  await requestInput(page).fill("A second soup");
  await sendButton(page).click();
  await page.clock.runFor(600);
  await expect(fact(0)).toBeVisible();
  next.resolve();
  await expect(waiting).toHaveCount(0);
  expect(chat.generations).toBe(2);
});

test("reduced motion removes steam travel and fact staggering while keeping waiting accessible", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const { chat } = await chatBackend(page);
  await open(page);
  const gate = deferred();
  chat.gate = gate;
  await requestInput(page).fill("A quiet soup");
  await sendButton(page).click();
  const waiting = page.locator(".recipe-waiting");
  await expect(waiting).toBeVisible();
  await page.getByRole("button", { name: "Pause food facts" }).click();
  await expect(waiting.locator(".pot-steam").first()).toHaveCSS(
    "animation-name",
    "none",
  );
  const transforms = await waiting
    .locator(".word-loader-item")
    .first()
    .locator(".char")
    .evaluateAll((chars) =>
      chars.map((char) => getComputedStyle(char).transform),
    );
  expect(
    transforms.every((value) => value === "matrix(1, 0, 0, 1, 0, 0)"),
  ).toBe(true);
  await expect(
    page.getByRole("status").filter({ hasText: "Waiting for your recipe." }),
  ).toHaveCount(1);
  expect(
    (await new AxeBuilder({ page }).include(".chat-composer").analyze())
      .violations,
  ).toEqual([]);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await expect(waiting.locator(".word-loader-item").first()).toBeVisible();
  await expect(waiting.locator(".pot-steam").first()).toHaveCSS(
    "animation-play-state",
    "paused",
  );
  gate.resolve();
  await expect(waiting).toHaveCount(0);
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
  await expect(sendButton(page)).toBeEnabled();
  const failedId = chat.turns[0]!.turnId;
  const messageId = chat.turns[0]!.messages[0]!.messageId;
  await sendButton(page).click();
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

test("editing after an uncertain send preserves the revision while checking the original request", async ({
  page,
}, info) => {
  const { chat } = await chatBackend(page);
  await open(page);
  chat.mode = "transport";
  await requestInput(page).fill("Original dinner");
  await sendButton(page).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(requestInput(page)).toBeEditable();
  await requestInput(page).fill("Revised dinner");
  await expect(sendButton(page)).toBeDisabled();
  expect(chat.writes).toHaveLength(1);
  const check = page.getByRole("button", {
    name: "Check this request",
    exact: true,
  });
  await check.hover();
  const button = await check.boundingBox();
  const composer = await page
    .getByRole("region", { name: "Recipe request", exact: true })
    .boundingBox();
  expect(button).not.toBeNull();
  expect(composer).not.toBeNull();
  expect(button!.y).toBeGreaterThanOrEqual(composer!.y + 2);
  expect(button!.y + button!.height + 4).toBeLessThanOrEqual(
    composer!.y + composer!.height,
  );
  expect(composer!.y + composer!.height).toBeLessThanOrEqual(
    page.viewportSize()!.height,
  );
  await page.screenshot({
    path: info.outputPath("editable-error.png"),
    fullPage: true,
  });
  await check.click();
  await expect(page.getByText(reply, { exact: true })).toBeVisible();
  await expect(requestInput(page)).toHaveValue("Revised dinner");
  expect(chat.writes[1]?.key).toBe(chat.writes[0]?.key);
  expect(chat.writes[1]?.body).toEqual(chat.writes[0]?.body);
  expect(chat.generations).toBe(1);
  await expect(sendButton(page)).toBeEnabled();
  await sendButton(page).click();
  await expect(requestInput(page)).toHaveValue("");
  expect(chat.writes[2]?.body.content).toBe("Revised dinner");
  expect(chat.generations).toBe(2);
});

test("a confirmed failed request can be edited and sent as a new request", async ({
  page,
}) => {
  const { chat } = await chatBackend(page);
  await open(page);
  chat.mode = "failed";
  await requestInput(page).fill("Original dinner");
  await sendButton(page).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(requestInput(page)).toBeEditable();
  await requestInput(page).fill("Revised dinner");
  await sendButton(page).click();
  await expect(requestInput(page)).toHaveValue("");
  expect(chat.writes[1]?.body).toEqual({ content: "Revised dinner" });
  expect(chat.turns).toHaveLength(2);
  expect(chat.turns[0]?.status).toBe("failed");
});

test("a pending reply does not erase the next draft when it completes", async ({
  page,
}) => {
  const { chat, complete } = await chatBackend(page);
  await open(page);
  chat.mode = "pending";
  await requestInput(page).fill("First dinner");
  await sendButton(page).click();
  await expect(
    page.getByText("Waiting for your recipe.", { exact: true }),
  ).toBeVisible();
  await requestInput(page).fill("Next dinner");
  await expect(sendButton(page)).toBeDisabled();
  complete();
  await expect(page.getByText(reply, { exact: true })).toBeVisible({
    timeout: 8000,
  });
  await expect(requestInput(page)).toHaveValue("Next dinner");
  await expect(sendButton(page)).toBeEnabled();
  expect(chat.writes).toHaveLength(1);
});

test("the first chat can refresh and check a lost pending reply using its saved conversation ID", async ({
  page,
}) => {
  const { chat, complete } = await chatBackend(page);
  await page.goto("/#sous-chef");
  chat.mode = "lostPending";
  await requestInput(page).fill("First dinner");
  await sendButton(page).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await requestInput(page).fill("Next dinner");
  const toggle = page.getByRole("button", {
    name: "Conversations",
    exact: true,
  });
  if ((await toggle.getAttribute("aria-expanded")) === "false")
    await toggle.click();
  await page
    .getByRole("button", { name: "Refresh chats and messages", exact: true })
    .click();
  if ((page.viewportSize()?.width ?? 0) <= 720) await toggle.click();
  const check = page.getByRole("button", {
    name: "Check saved status",
    exact: true,
  });
  await expect(check).toBeVisible();
  expect(chat.writes).toHaveLength(1);
  complete();
  await check.click();
  await expect(page.getByText(reply, { exact: true })).toBeVisible();
  await expect(requestInput(page)).toHaveValue("Next dinner");
  await expect(sendButton(page)).toBeEnabled();
  expect(chat.writes).toHaveLength(1);
  expect(chat.generations).toBe(1);
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
    page.getByText("Waiting for your recipe.", { exact: true }),
  ).toBeVisible();
  await expect(sendButton(page)).toBeDisabled();
  const storage = await page.evaluate(() => JSON.stringify(sessionStorage));
  expect(storage).not.toContain("Slow dinner request");
  expect(storage).not.toContain("synthetic-user");
  page.once("dialog", (dialog) => dialog.accept());
  await page.reload();
  await expect(
    page.getByText("Waiting for your recipe.", { exact: true }),
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

test("a lost operation reference is recovered from history and closes without another generation", async ({
  page,
}) => {
  const { chat } = await chatBackend(page);
  const turn = savedTurn("Previously interrupted request", 0, "outcomeUnknown");
  const operationId = crypto.randomUUID();
  turn.operationId = operationId;
  chat.turns = [turn];
  chat.operations.set(operationId, {
    operationId,
    conversationId: id,
    turnId: turn.turnId,
    status: "outcomeUnknown",
    failureCode: "execution_outcome_unknown",
    turn,
  });
  await page.goto("/#sous-chef");
  await choose(page);
  await expect(sendButton(page)).toBeDisabled();
  const recovery = page.getByRole("region", { name: "Saved request recovery" });
  await recovery.getByRole("button", { name: "Check saved status" }).click();
  expect(chat.writes).toHaveLength(0);
  expect(chat.generations).toBe(0);
  await recovery
    .getByRole("button", {
      name: "Close interrupted request without retrying",
    })
    .click();
  await expect(
    recovery.getByRole("button", { name: "Retry saved request" }),
  ).toBeVisible();
  expect(chat.closes).toHaveLength(1);
  expect(chat.generations).toBe(0);
  await recovery.getByRole("button", { name: "Retry saved request" }).click();
  await expect(page.getByText(reply, { exact: true })).toBeVisible();
  expect(chat.writes).toHaveLength(1);
  expect(chat.writes[0]?.body.retryOfTurnId).toBe(turn.turnId);
  expect(chat.generations).toBe(1);
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
  chat.turns = Array.from({ length: 40 }, (_, index) =>
    savedTurn(`Older request ${index}`, index),
  );
  await open(page);
  const historyReads = chat.reads.filter((read) =>
    read.includes("/turns?"),
  ).length;
  await page
    .getByRole("region", { name: "Messages", exact: true })
    .dispatchEvent("wheel", { deltaY: 10 });
  await requestInput(page).fill("The newest request");
  await sendButton(page).click();
  await expect(
    page.getByRole("list", { name: "Conversation transcript" }),
  ).toContainText("The newest request");
  await expect(requestInput(page)).toHaveValue("");
  expect(chat.reads.filter((read) => read.includes("/turns?"))).toHaveLength(
    historyReads,
  );
  expect(chat.turns).toHaveLength(41);
  await expect(
    page
      .getByRole("list", { name: "Conversation transcript" })
      .getByRole("article"),
  ).toHaveCount(42);
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
  await page
    .getByRole("region", { name: "Messages", exact: true })
    .press("End");
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
