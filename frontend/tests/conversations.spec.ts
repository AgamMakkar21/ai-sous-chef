import { expect, test, type Page as BrowserPage } from "@playwright/test";
import type { Conversation, Page, Turn } from "../src/conversations.ts";
import { backend } from "./backend.ts";

const firstId = "11111111-1111-4111-8111-111111111111";
const secondId = "22222222-2222-4222-8222-222222222222";
const thirdId = "33333333-3333-4333-8333-333333333333";
const timestamp = "2026-10-29T12:00:00.000000Z";

function deferred() {
  let resolve: () => void = () => {
    throw new Error("The test gate has not been initialized.");
  };
  const promise = new Promise<void>((complete) => {
    resolve = complete;
  });
  return { promise, resolve: () => resolve() };
}

function summary(conversationId: string, title: string): Conversation {
  return { conversationId, title, createdAt: timestamp, updatedAt: timestamp };
}

function turn(conversationId: string, content: string, index = 0): Turn {
  return {
    conversationId,
    turnId: `44444444-4444-4444-8444-${String(index).padStart(12, "0")}`,
    turnIndex: index,
    status: "completed",
    messages: [
      { messageId: crypto.randomUUID(), role: "user", content, timestamp },
      {
        messageId: crypto.randomUUID(),
        role: "assistant",
        content: `Reply: ${content}`,
        timestamp,
      },
    ],
  };
}

async function conversations(page: BrowserPage) {
  const profile = await backend(page);
  const state = {
    chats: [
      summary(firstId, "Quick lunches"),
      summary(secondId, "Weekend dinners"),
    ],
    listPages: new Map<string, Page<Conversation>>(),
    turns: new Map<string, Page<Turn>>([
      [
        `${firstId}:`,
        { items: [turn(firstId, "Lemony lunch ideas")], nextCursor: null },
      ],
      [
        `${secondId}:`,
        {
          items: [turn(secondId, "Weekend vegetable supper")],
          nextCursor: null,
        },
      ],
    ]),
    reads: [] as string[],
    writes: [] as { key: string; token: string | undefined; body: unknown }[],
    receipts: new Map<string, Conversation>(),
    failure: null as {
      target: "list" | "history" | "create";
      status: number;
      commit?: boolean;
      html?: boolean;
      invalid?: "foreign" | "status";
    } | null,
  };
  await page.route("**/api/v1/conversations**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const creating = request.method() === "POST";
    const id = url.pathname.split("/")[4];
    const target = creating ? "create" : id ? "history" : "list";
    const failure = state.failure?.target === target ? state.failure : null;
    if (failure) state.failure = null;
    if (creating) {
      expect(url.pathname).toBe("/api/v1/conversations");
      const key = request.headers()["idempotency-key"];
      if (!key) throw new Error("Creation requires an idempotency key.");
      state.writes.push({
        key,
        token: request.headers()["x-csrf-token"],
        body: request.postDataJSON(),
      });
      let created = state.receipts.get(key);
      if ((!failure || failure.commit) && !created) {
        created = summary(crypto.randomUUID(), "New conversation");
        state.receipts.set(key, created);
        state.chats.unshift(created);
        state.turns.set(`${created.conversationId}:`, {
          items: [],
          nextCursor: null,
        });
      }
      if (!failure) {
        await route.fulfill({ status: 201, json: { data: created } });
        return;
      }
    } else {
      state.reads.push(`${url.pathname}${url.search}`);
      expect(url.searchParams.get("limit")).toBe("20");
      expect([...url.searchParams.keys()].sort()).toEqual(
        url.searchParams.has("cursor") ? ["cursor", "limit"] : ["limit"],
      );
    }
    if (failure?.status === 0) {
      await route.abort("failed");
    } else if (failure?.invalid) {
      const malformed = turn(
        failure.invalid === "foreign" ? secondId : firstId,
        "foreign-private-message",
      );
      await route.fulfill({
        json: {
          items: [
            failure.invalid === "status"
              ? {
                  ...malformed,
                  status: ["pending"],
                  messages: malformed.messages.slice(0, 1),
                }
              : malformed,
          ],
          nextCursor: null,
        },
      });
    } else if (failure) {
      await route.fulfill({
        status: failure.status,
        contentType: failure.html ? "text/html" : "application/problem+json",
        body: failure.html
          ? "<html>private-provider-details</html>"
          : JSON.stringify({
              code:
                failure.status === 403 ? "csrf_failed" : "storage_unavailable",
              detail: "private-provider-details",
            }),
      });
    } else {
      const cursor = url.searchParams.get("cursor") ?? "";
      const result = id
        ? state.turns.get(`${id}:${cursor}`)
        : (state.listPages.get(cursor) ?? {
            items: state.chats,
            nextCursor: null,
          });
      if (!result) throw new Error("Unexpected synthetic history page.");
      await route.fulfill({ json: result });
    }
  });
  return { ...profile, chat: state };
}

async function openChats(page: BrowserPage) {
  const toggle = page.getByRole("button", {
    name: "Conversations",
    exact: true,
  });
  if ((await toggle.getAttribute("aria-expanded")) === "false")
    await toggle.click();
}

async function choose(page: BrowserPage, title: string) {
  await openChats(page);
  await page.getByRole("button", { name: title, exact: false }).click();
}

test("New Chat creates independent stable IDs with current CSRF and no messages sent", async ({
  page,
}) => {
  const { chat, state } = await conversations(page);
  chat.chats = [];
  await page.goto("/#sous-chef");
  await expect(
    page.getByRole("button", { name: "New Chat", exact: true }),
  ).toBeEnabled();
  state.token = "r".repeat(43);
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect(
    page.getByText("No messages in this chat yet.", { exact: true }),
  ).toBeVisible();
  const first = [...chat.receipts.values()][0];
  if (!first) throw new Error("Missing first created conversation.");
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect.poll(() => chat.receipts.size).toBe(2);
  await expect(
    page.getByText("No messages in this chat yet.", { exact: true }),
  ).toBeVisible();
  const second = [...chat.receipts.values()][1];
  if (!second) throw new Error("Missing second created conversation.");
  expect(second.conversationId).not.toBe(first.conversationId);
  expect(chat.writes.map((write) => write.body)).toEqual([{}, {}]);
  expect(chat.writes.map((write) => write.token)).toEqual([
    "r".repeat(43),
    "r".repeat(43),
  ]);
  expect(chat.writes[0]?.key).not.toBe(chat.writes[1]?.key);
  await page.reload();
  await expect(
    page.getByText("No messages in this chat yet.", { exact: true }),
  ).toBeVisible();
  expect(chat.reads.at(-1)).toContain(`/${second.conversationId}/turns?`);
  expect(chat.writes).toHaveLength(2);
  await openChats(page);
  await page.locator(".conversation-list .conversation-choice").last().click();
  await expect
    .poll(() => chat.reads.at(-1))
    .toContain(`/${first.conversationId}/turns?`);
});

test("selecting, switching tabs and reloading preserve the right transcript and highlight", async ({
  page,
}, testInfo) => {
  await conversations(page);
  await page.goto("/#sous-chef");
  await choose(page, "Quick lunches");
  await expect(
    page.getByText("Lemony lunch ideas", { exact: true }),
  ).toBeVisible();
  await expect(page.locator("#transcript-title")).toBeFocused();
  await choose(page, "Weekend dinners");
  await expect(
    page.getByText("Weekend vegetable supper", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Lemony lunch ideas", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.locator(".conversation-list [aria-current='true']"),
  ).toContainText("Weekend dinners");
  await page.getByRole("tab", { name: "Recipe History" }).click();
  await page.getByRole("tab", { name: "Sous Chef", exact: true }).click();
  await expect(
    page.getByText("Weekend vegetable supper", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByText("Weekend vegetable supper", { exact: true }),
  ).toBeVisible();
  await expect(
    page.locator(".conversation-list [aria-current='true']"),
  ).toContainText("Weekend dinners");
  await page.screenshot({
    path: testInfo.outputPath("conversation.png"),
    fullPage: true,
  });
});

test("chat and transcript pages are bounded and loaded only on explicit navigation", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  chat.listPages.set("", {
    items: [summary(firstId, "Quick lunches")],
    nextCursor: "chat-page-2",
  });
  chat.listPages.set("chat-page-2", {
    items: [summary(thirdId, "New conversation")],
    nextCursor: null,
  });
  chat.turns.set(`${thirdId}:`, {
    items: [turn(thirdId, "A fresh third topic")],
    nextCursor: "turn-page-2",
  });
  chat.turns.set(`${thirdId}:turn-page-2`, {
    items: [turn(thirdId, "A later exchange", 2)],
    nextCursor: null,
  });
  await page.goto("/#sous-chef");
  await openChats(page);
  await expect(
    page.getByRole("button", { name: "More chats", exact: true }),
  ).toBeEnabled();
  expect(chat.reads).toHaveLength(1);
  await page.getByRole("button", { name: "More chats", exact: true }).click();
  await page
    .locator(".conversation-list")
    .getByRole("button", { name: "New chat", exact: false })
    .click();
  const messages = page.locator(".conversation-message");
  await expect(
    messages.getByText("A fresh third topic", { exact: true }),
  ).toBeVisible();
  await expect(page.locator("#transcript-title")).toHaveText(
    "A fresh third topic",
  );
  expect(chat.reads.some((read) => read.includes("turn-page-2"))).toBe(false);
  await page
    .getByRole("button", { name: "Later messages", exact: true })
    .click();
  await expect(
    page.getByText("A later exchange", { exact: true }),
  ).toBeVisible();
  await expect(
    messages.getByText("A fresh third topic", { exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Earlier messages", exact: true })
    .click();
  await expect(
    messages.getByText("A fresh third topic", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    messages.getByText("A fresh third topic", { exact: true }),
  ).toBeVisible();
  await openChats(page);
  await expect(
    page.locator("#conversation-sidebar [aria-current='true']"),
  ).toContainText("A fresh third topic");
  expect(
    chat.reads.filter((read) => read.includes("cursor=chat-page-2")),
  ).toHaveLength(1);
});

test("message markup stays text, navigation stores no content and mobile keyboard controls work", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  const markup =
    "<img src=x onerror=window.injected=true><script>window.injected=true</script>";
  chat.turns.set(`${firstId}:`, {
    items: [turn(firstId, markup)],
    nextCursor: null,
  });
  await page.goto("/?scoutTheme=dark#sous-chef");
  await openChats(page);
  const choice = page.getByRole("button", {
    name: "Quick lunches",
    exact: false,
  });
  await choice.focus();
  await choice.press("Enter");
  await expect(page.getByText(markup, { exact: true })).toBeVisible();
  await expect(
    page.locator(
      ".conversation-transcript img, .conversation-transcript script",
    ),
  ).toHaveCount(0);
  expect(await page.evaluate(() => Object.hasOwn(window, "injected"))).toBe(
    false,
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  const storage = await page.evaluate(() => ({
    local: localStorage.length,
    values: Object.values(sessionStorage).join(""),
  }));
  expect(storage.local).toBe(0);
  expect(storage.values).not.toContain("synthetic-user");
  expect(storage.values).not.toContain("Quick lunches");
  expect(storage.values).not.toContain(markup);
  expect(storage.values).not.toContain("t".repeat(43));
  const toggle = page.getByRole("button", {
    name: "Conversations",
    exact: true,
  });
  await toggle.focus();
  const before = await toggle.getAttribute("aria-expanded");
  await toggle.press("Enter");
  await expect(toggle).toHaveAttribute(
    "aria-expanded",
    before === "true" ? "false" : "true",
  );
});

test("list, history and malformed/foreign history failures stay explicit and private", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  chat.failure = { target: "list", status: 503 };
  await page.goto("/#sous-chef");
  await openChats(page);
  await expect(
    page.getByText("Chats could not be loaded.", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Try chats again" }).click();
  chat.failure = { target: "history", status: 503 };
  await choose(page, "Quick lunches");
  await expect(
    page.getByText("Messages could not be loaded.", { exact: false }),
  ).toBeVisible();
  expect(await page.locator("body").innerText()).not.toContain(
    "private-provider-details",
  );
  await page.getByRole("button", { name: "Try history again" }).click();
  await expect(
    page.getByText("Lemony lunch ideas", { exact: true }),
  ).toBeVisible();
  chat.failure = { target: "history", status: 200, invalid: "foreign" };
  await page.getByRole("button", { name: "Refresh history" }).click();
  await expect(
    page.getByText("Messages could not be loaded.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("foreign-private-message", { exact: true }),
  ).toHaveCount(0);
  chat.failure = { target: "history", status: 200, invalid: "status" };
  await page.getByRole("button", { name: "Try history again" }).click();
  await expect(
    page.getByText("Messages could not be loaded.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("foreign-private-message", { exact: true }),
  ).toHaveCount(0);
  chat.failure = { target: "history", status: 404 };
  await page.getByRole("button", { name: "Try history again" }).click();
  await expect(
    page.getByText("This conversation is not available.", { exact: false }),
  ).toBeVisible();
});

test("uncertain creation survives reload and reconciles the original key only on explicit action", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  chat.failure = { target: "create", status: 0, commit: true };
  await page.goto("/#sous-chef");
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect(
    page.getByText("Chat creation is not confirmed.", { exact: false }),
  ).toBeVisible();
  expect(chat.writes).toHaveLength(1);
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Check chat creation" }),
  ).toBeEnabled();
  expect(chat.writes).toHaveLength(1);
  await page.getByRole("button", { name: "Check chat creation" }).click();
  await expect(
    page.getByText("No messages in this chat yet.", { exact: true }),
  ).toBeVisible();
  expect(chat.writes).toHaveLength(2);
  expect(chat.writes[1]?.key).toBe(chat.writes[0]?.key);
  expect(chat.receipts.size).toBe(1);
});

test("CSRF failures do not replay creation and a reviewed retry uses a fresh token with the same key", async ({
  page,
}) => {
  const { chat, state } = await conversations(page);
  chat.failure = { target: "create", status: 403 };
  await page.goto("/#sous-chef");
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect(
    page.getByText("Your security token changed.", { exact: false }),
  ).toBeVisible();
  expect(chat.writes).toHaveLength(1);
  state.token = "r".repeat(43);
  await page.getByRole("button", { name: "Check chat creation" }).click();
  await expect(
    page.getByText("No messages in this chat yet.", { exact: true }),
  ).toBeVisible();
  expect(chat.writes[1]?.token).toBe("r".repeat(43));
  expect(chat.writes[1]?.key).toBe(chat.writes[0]?.key);
});

for (const failure of ["html401", "identity"] as const) {
  test(`${failure} clears transcript and conversation titles before sign-in`, async ({
    page,
  }) => {
    const { chat, state } = await conversations(page);
    await page.goto("/#sous-chef");
    await choose(page, "Quick lunches");
    await expect(
      page.getByText("Lemony lunch ideas", { exact: true }),
    ).toBeVisible();
    if (failure === "html401")
      chat.failure = { target: "history", status: 401, html: true };
    else state.sessionUser = "different-synthetic-user";
    await page.getByRole("button", { name: "Refresh history" }).click();
    await expect(
      page.getByRole("link", { name: "Sign in", exact: true }),
    ).toBeVisible();
    await expect(page.locator("body")).not.toContainText("Lemony lunch ideas");
    await expect(page.locator("body")).not.toContainText("Quick lunches");
    await expect(page.locator("body")).not.toContainText(
      "private-provider-details",
    );
  });
}

test("a different signed-in user never restores the old account's selected conversation", async ({
  page,
}) => {
  const { chat, state } = await conversations(page);
  await page.goto("/#sous-chef");
  await choose(page, "Quick lunches");
  await expect(
    page.getByText("Lemony lunch ideas", { exact: true }),
  ).toBeVisible();
  const before = chat.reads.length;
  state.sessionUser = state.profileUser = "different-synthetic-user";
  chat.chats = [];
  await page.reload();
  await expect(
    page.getByRole("button", { name: "New Chat", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByRole("region", { name: "No conversation selected" }),
  ).toBeVisible();
  expect(chat.reads.slice(before).some((read) => read.includes(firstId))).toBe(
    false,
  );
});

test("late history responses cannot replace a newly selected conversation", async ({
  page,
}) => {
  await conversations(page);
  const arrived = deferred();
  const release = deferred();
  const fulfilled = deferred();
  await page.route(`**/conversations/${firstId}/turns?*`, async (route) => {
    arrived.resolve();
    await release.promise;
    await route.fulfill({
      json: {
        items: [turn(firstId, "late-old-chat-message")],
        nextCursor: null,
      },
    });
    fulfilled.resolve();
  });
  await page.goto("/#sous-chef");
  await choose(page, "Quick lunches");
  await arrived.promise;
  await expect(
    page.getByText("Loading messages...", { exact: true }),
  ).toBeVisible();
  await choose(page, "Weekend dinners");
  await expect(
    page.getByText("Weekend vegetable supper", { exact: true }),
  ).toBeVisible();
  release.resolve();
  await fulfilled.promise;
  await expect(
    page.getByText("late-old-chat-message", { exact: true }),
  ).toHaveCount(0);
  await expect(page.locator("#transcript-title")).toHaveText("Weekend dinners");
});
