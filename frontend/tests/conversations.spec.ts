import { expect, test, type Page as BrowserPage } from "@playwright/test";
import type { Conversation, Page, Turn } from "../src/conversations.ts";
import { backend } from "./backend.ts";
import { recipeFixture } from "./recipe-fixture.ts";

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
    sends: [] as string[],
    loseSendResponse: false,
    recipe: null as ReturnType<typeof recipeFixture> | null,
    deletes: [] as { id: string; token: string | undefined }[],
    receipts: new Map<string, Conversation>(),
    failure: null as {
      target: "list" | "history" | "create" | "delete";
      status: number;
      commit?: boolean;
      html?: boolean;
      invalid?: "foreign" | "status";
    } | null,
  };
  await page.route("**/api/v1/conversations**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const id = url.pathname.split("/")[4];
    if (request.method() === "POST" && url.pathname.endsWith("/turns")) {
      if (!id) throw new Error("Missing conversation ID.");
      const saved = turn(id, request.postDataJSON().content as string);
      if (state.recipe) {
        saved.recipe = state.recipe;
        saved.generatedRecipeId = crypto.randomUUID();
        const chat = state.chats.find((item) => item.conversationId === id);
        if (chat?.title === "New conversation") chat.title = state.recipe.title;
      }
      state.sends.push(id);
      state.turns.set(`${id}:`, { items: [saved], nextCursor: null });
      if (state.loseSendResponse) {
        state.loseSendResponse = false;
        await route.abort("failed");
        return;
      }
      await route.fulfill({ json: { data: saved } });
      return;
    }
    const creating = request.method() === "POST";
    const deleting = request.method() === "DELETE";
    const target = deleting
      ? "delete"
      : creating
        ? "create"
        : id
          ? "history"
          : "list";
    const failure = state.failure?.target === target ? state.failure : null;
    if (failure) state.failure = null;
    if (deleting) {
      if (!id) throw new Error("Missing conversation ID.");
      state.deletes.push({ id, token: request.headers()["x-csrf-token"] });
      if (!failure) {
        state.chats = state.chats.filter((chat) => chat.conversationId !== id);
        state.turns.delete(`${id}:`);
        await route.fulfill({ status: 204 });
        return;
      }
    }
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
    } else if (!deleting) {
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

async function removeChat(page: BrowserPage, title: string) {
  await openChats(page);
  const row = page.locator(".conversation-list li").filter({
    has: page.getByRole("button", { name: title, exact: true }),
  });
  await row.hover();
  await row.getByRole("button", { name: "Delete chat", exact: true }).click();
}

test("empty drafts are not saved and the first send creates one chat with current CSRF", async ({
  page,
}) => {
  const { chat, state } = await conversations(page);
  chat.chats = [];
  await page.goto("/#sous-chef");
  await expect(
    page.getByRole("button", { name: "New Chat", exact: true }),
  ).toBeEnabled();
  state.token = "r".repeat(43);
  await expect(page.getByRole("textbox")).toBeEnabled();
  await expect(
    page.locator(
      "#recipe-request-help, .chat-composer .privacy-note, .conversation-toolbar p, .conversation-empty h2",
    ),
  ).toHaveCount(0);
  expect(chat.writes).toHaveLength(0);
  await page.getByRole("textbox").fill("An unsent idea");
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect(page.getByRole("textbox")).toHaveValue("An unsent idea");
  expect(chat.writes).toHaveLength(0);
  expect(chat.reads.every((read) => !read.includes("/turns"))).toBe(true);
  page.once("dialog", (dialog) => dialog.accept());
  await page.reload();
  await expect(
    page.getByRole("region", { name: "Conversation: New chat", exact: true }),
  ).toBeVisible();
  await page.getByRole("textbox").fill("   ");
  await page.getByRole("button", { name: "Send request", exact: true }).click();
  await expect(
    page.getByText("Write a request between 1 and 4,000 characters."),
  ).toBeVisible();
  expect(chat.writes).toHaveLength(0);
  await page.getByRole("textbox").fill("A quick dinner");
  await page.getByRole("button", { name: "Send request", exact: true }).click();
  await expect(
    page.getByText("Reply: A quick dinner", { exact: true }),
  ).toBeVisible();
  expect(chat.writes.map((write) => write.body)).toEqual([{}]);
  expect(chat.writes[0]?.token).toBe("r".repeat(43));
  expect(chat.sends).toHaveLength(1);
  await page.reload();
  await expect(
    page.getByText("Reply: A quick dinner", { exact: true }),
  ).toBeVisible();
  await openChats(page);
  await expect(
    page.locator(".conversation-list .conversation-choice"),
  ).toHaveCount(1);
});

test("trash controls appear only for the hovered or keyboard-focused row on desktop and stay available on touch", async ({
  page,
}) => {
  await conversations(page);
  await page.goto("/#sous-chef");
  await openChats(page);
  const first = page.locator(".conversation-list li").filter({
    has: page.getByRole("button", { name: "Quick lunches", exact: true }),
  });
  const trash = first.getByRole("button", { name: "Delete chat", exact: true });
  const other = page
    .locator(".conversation-list li")
    .filter({
      has: page.getByRole("button", { name: "Weekend dinners", exact: true }),
    })
    .getByRole("button", { name: "Delete chat", exact: true });
  await expect(trash).toBeAttached();
  if (
    await page.evaluate(
      () => matchMedia("(hover: hover) and (pointer: fine)").matches,
    )
  ) {
    await expect(trash).toHaveCSS("opacity", "0");
    await first.hover();
    await expect(trash).toHaveCSS("opacity", "1");
    await expect(other).toHaveCSS("opacity", "0");
    await page.mouse.move(0, 0);
    await expect(trash).toHaveCSS("opacity", "0");
    await first
      .getByRole("button", { name: "Quick lunches", exact: true })
      .focus();
    await expect(trash).toHaveCSS("opacity", "1");
    await page.keyboard.press("Tab");
    await expect(trash).toBeFocused();
  } else {
    await expect(trash).toHaveCSS("opacity", "1");
    await expect(other).toHaveCSS("opacity", "1");
  }
});

test("completed recipes replace new-chat labels and keep their titles after reload", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  chat.chats = [];
  chat.recipe = { ...recipeFixture(), title: "Quick lemon rice dinner" };
  await page.goto("/#sous-chef");
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await page.getByRole("textbox").fill("What can I cook with rice and lemons?");
  await page.getByRole("button", { name: "Send request", exact: true }).click();
  await expect(page.locator("#transcript-title")).toHaveText(chat.recipe.title);
  await openChats(page);
  const titles = page.locator(".conversation-list .conversation-choice");
  await expect(titles).toHaveCount(1);
  await expect(titles).toContainText(chat.recipe.title);
  await page.reload();
  await openChats(page);
  await expect(titles).toContainText(chat.recipe.title);
  await expect(titles).not.toContainText("New chat");
});

test("an existing default-named chat refreshes its saved title after a reply", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  chat.chats[0]!.title = "New conversation";
  chat.recipe = { ...recipeFixture(), title: "High protein rice lunch" };
  await page.goto("/#sous-chef");
  await openChats(page);
  await page
    .locator(".conversation-list")
    .getByRole("button", { name: "New chat", exact: false })
    .click();
  await page.getByRole("textbox").fill("Make that a high protein lunch");
  await page.getByRole("button", { name: "Send request", exact: true }).click();
  await openChats(page);
  await expect(
    page.locator(".conversation-list [aria-current='true']"),
  ).toContainText(chat.recipe.title);
  await expect(
    page.getByRole("button", { name: "Weekend dinners", exact: false }),
  ).toBeVisible();
});

test("selecting, switching tabs and reloading preserve the right transcript and highlight", async ({
  page,
}, testInfo) => {
  await conversations(page);
  await page.goto("/#sous-chef");
  await choose(page, "Quick lunches");
  const entry = page.locator(".conversation-list .conversation-choice").first();
  await expect(entry).not.toHaveClass(/cartoon-button/);
  await expect(entry).toHaveCSS("border-radius", "8px");
  await expect(entry).toHaveCSS("box-shadow", "none");
  await expect(entry.locator(".cartoon-highlight")).toHaveCount(0);
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

test("scrolling loads bounded chat and message pages without replacing prior rows", async ({
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
    page.getByRole("button", { name: "Quick lunches", exact: true }),
  ).toBeVisible();
  expect(chat.reads).toHaveLength(1);
  await page
    .getByRole("region", { name: "Chat list", exact: true })
    .press("End");
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
    .getByRole("region", { name: "Messages", exact: true })
    .press("End");
  await expect(
    page.getByText("A later exchange", { exact: true }),
  ).toBeVisible();
  await expect(
    messages.getByText("A fresh third topic", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: /More chats|Earlier messages|Later messages/,
    }),
  ).toHaveCount(0);
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

test("additional pages preserve loaded chats and require explicit retry after a failed read", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  chat.listPages.set("", {
    items: [summary(firstId, "Quick lunches")],
    nextCursor: "next",
  });
  chat.listPages.set("next", {
    items: [summary(secondId, "Weekend dinners")],
    nextCursor: "loop",
  });
  chat.listPages.set("loop", {
    items: [summary(thirdId, "Rejected repeated cursor")],
    nextCursor: "loop",
  });
  await page.goto("/#sous-chef");
  await openChats(page);
  const list = page.getByRole("region", { name: "Chat list", exact: true });
  await expect(
    page.getByRole("button", { name: "Quick lunches", exact: true }),
  ).toBeVisible();
  chat.failure = { target: "list", status: 503 };
  await list.press("End");
  await expect(page.getByRole("alert")).toContainText(
    "More chats could not be loaded.",
  );
  expect(chat.reads).toHaveLength(2);
  await list.press("End");
  await expect(
    page.getByRole("button", { name: "Quick lunches", exact: true }),
  ).toBeVisible();
  expect(chat.reads).toHaveLength(2);
  await page
    .getByRole("button", { name: "Try chats again", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Weekend dinners", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".conversation-choice")).toHaveCount(2);
  await list.press("End");
  await expect(page.getByRole("alert")).toContainText(
    "More chats could not be loaded.",
  );
  await expect(page.locator(".conversation-choice")).toHaveCount(2);
  expect(chat.reads).toHaveLength(4);
});

test("deleting another chat keeps the current conversation and unsent request", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  await page.goto("/#sous-chef");
  await choose(page, "Quick lunches");
  await page.getByRole("textbox").fill("Keep this draft");
  await openChats(page);
  page.once("dialog", (dialog) => dialog.accept());
  await removeChat(page, "Weekend dinners");
  await expect(
    page.getByRole("button", { name: "Weekend dinners", exact: true }),
  ).toHaveCount(0);
  const toggle = page.getByRole("button", {
    name: "Conversations",
    exact: true,
  });
  if (
    (page.viewportSize()?.width ?? 0) <= 720 &&
    (await toggle.getAttribute("aria-expanded")) === "true"
  )
    await toggle.click();
  await expect(page.getByRole("textbox")).toHaveValue("Keep this draft");
  await expect(
    page.getByText("Lemony lunch ideas", { exact: true }),
  ).toBeVisible();
  expect(chat.deletes.map((item) => item.id)).toEqual([secondId]);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect(page.getByRole("textbox")).toHaveValue("");
  expect(chat.writes).toHaveLength(0);
});

test("the chat list and messages scroll inside the viewport independently", async ({
  page,
}, info) => {
  const { chat } = await conversations(page);
  chat.chats = [
    summary(firstId, "Quick lunches"),
    ...Array.from({ length: 19 }, (_, i) =>
      summary(crypto.randomUUID(), `Saved chat ${i}`),
    ),
  ];
  chat.turns.set(`${firstId}:`, {
    items: Array.from({ length: 20 }, (_, i) =>
      turn(firstId, `Request ${i}: ${"Vegetable supper. ".repeat(20)}`, i),
    ),
    nextCursor: null,
  });
  await page.goto("/#sous-chef");
  await choose(page, "Quick lunches");
  const messages = page.getByRole("region", { name: "Messages", exact: true });
  const list = page.locator(".chat-list-scroll");
  await expect(page.locator(".conversation-message")).toHaveCount(40);
  const mainBounds = await messages.boundingBox();
  expect(mainBounds).not.toBeNull();
  await page.mouse.move(
    mainBounds!.x + mainBounds!.width / 2,
    mainBounds!.y + mainBounds!.height / 2,
  );
  await page.mouse.wheel(0, 300);
  await expect
    .poll(() => messages.evaluate((e) => e.scrollTop))
    .toBeGreaterThan(0);
  const mainScroll = await messages.evaluate((e) => e.scrollTop);
  expect(await list.evaluate((e) => e.scrollTop)).toBe(0);
  if (info.project.name === "phone") {
    await page.screenshot({
      path: info.outputPath("chat-messages.png"),
      fullPage: true,
    });
    await openChats(page);
    await expect(messages).toBeHidden();
  }
  const sidebarBounds = await list.boundingBox();
  expect(sidebarBounds).not.toBeNull();
  await page.mouse.move(
    sidebarBounds!.x + sidebarBounds!.width / 2,
    sidebarBounds!.y + sidebarBounds!.height / 2,
  );
  await page.mouse.wheel(0, 300);
  await expect.poll(() => list.evaluate((e) => e.scrollTop)).toBeGreaterThan(0);
  if (info.project.name === "desktop")
    expect(await messages.evaluate((e) => e.scrollTop)).toBe(mainScroll);
  expect(await page.evaluate(() => document.scrollingElement?.scrollTop)).toBe(
    0,
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollHeight <= innerHeight + 1,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("chat-workspace.png"),
    fullPage: true,
  });
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
  await openChats(page);
  await page
    .getByRole("button", { name: "Refresh chats and messages" })
    .click();
  if ((page.viewportSize()?.width ?? 0) <= 720)
    await page
      .getByRole("button", { name: "Conversations", exact: true })
      .click();
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

test("uncertain first-send creation retries the original key only on explicit action", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  chat.failure = { target: "create", status: 0, commit: true };
  await page.goto("/#sous-chef");
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await page.getByRole("textbox").fill("A quick dinner");
  await page.getByRole("button", { name: "Send request", exact: true }).click();
  await expect(
    page.getByText("No completed answer is confirmed.", { exact: false }),
  ).toBeVisible();
  expect(chat.writes).toHaveLength(1);
  expect(chat.sends).toHaveLength(0);
  await page.getByRole("button", { name: "Check this request" }).click();
  await expect(
    page.getByText("Reply: A quick dinner", { exact: true }),
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
  await page.getByRole("textbox").fill("A quick dinner");
  await page.getByRole("button", { name: "Send request", exact: true }).click();
  await expect(
    page.getByText("Your security token changed.", { exact: false }),
  ).toBeVisible();
  expect(chat.writes).toHaveLength(1);
  state.token = "r".repeat(43);
  await page.getByRole("button", { name: "Check this request" }).click();
  await expect(
    page.getByText("Reply: A quick dinner", { exact: true }),
  ).toBeVisible();
  expect(chat.writes[1]?.token).toBe("r".repeat(43));
  expect(chat.writes[1]?.key).toBe(chat.writes[0]?.key);
});

test("New Chat can leave an attempted first send without replaying it", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  chat.loseSendResponse = true;
  await page.goto("/#sous-chef");
  await page.getByRole("textbox").fill("Original request");
  await page.getByRole("button", { name: "Send request", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("textbox").fill("Edited request");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect(page.getByRole("textbox")).toHaveValue("Edited request");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect(page.getByRole("textbox")).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "Send request", exact: true }),
  ).toBeEnabled();
  expect(chat.writes).toHaveLength(1);
  expect(chat.sends).toHaveLength(1);
});

test("first-send reconciliation keeps edits when a draft becomes a saved chat", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  chat.loseSendResponse = true;
  await page.goto("/#sous-chef");
  await page.getByRole("textbox").fill("Original request");
  await page.getByRole("button", { name: "Send request", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("textbox").fill("Edited request");
  await page
    .getByRole("button", { name: "Check this request", exact: true })
    .click();
  await expect(
    page.getByText("Reply: Original request", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveValue("Edited request");
  expect(chat.writes).toHaveLength(2);
  expect(chat.writes[1]?.key).toBe(chat.writes[0]?.key);
  expect(chat.receipts.size).toBe(1);
});

test("one refresh updates the chat list and messages while preserving unsent text", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  await page.goto("/#sous-chef");
  await choose(page, "Quick lunches");
  await page.getByRole("textbox").fill("Keep this draft");
  chat.chats[0]!.title = "Updated chat title";
  chat.turns.set(`${firstId}:`, {
    items: [turn(firstId, "Updated saved exchange")],
    nextCursor: null,
  });
  await openChats(page);
  await page
    .getByRole("button", { name: "Refresh chats and messages", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Updated chat title", exact: true }),
  ).toBeVisible();
  const toggle = page.getByRole("button", {
    name: "Conversations",
    exact: true,
  });
  if ((page.viewportSize()?.width ?? 0) <= 720) await toggle.click();
  await expect(
    page.getByText("Updated saved exchange", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveValue("Keep this draft");
  await expect(
    page.getByRole("button", { name: "Refresh history", exact: true }),
  ).toHaveCount(0);
});

test("a lost first-send response restores the real chat without creating or sending again", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  chat.loseSendResponse = true;
  await page.goto("/#sous-chef");
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await page.getByRole("textbox").fill("A quick dinner");
  await page.getByRole("button", { name: "Send request", exact: true }).click();
  await expect(
    page.getByText("No completed answer is confirmed.", { exact: false }),
  ).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.reload();
  await expect(
    page.getByText("Reply: A quick dinner", { exact: true }),
  ).toBeVisible();
  expect(chat.writes).toHaveLength(1);
  expect(chat.sends).toHaveLength(1);
});

test("delete confirms intent, uses current CSRF and clears the selected chat across reloads", async ({
  page,
}) => {
  const { chat, state } = await conversations(page);
  await page.goto("/#sous-chef");
  await choose(page, "Quick lunches");
  await expect(
    page.getByText("Lemony lunch ideas", { exact: true }),
  ).toBeVisible();
  page.once("dialog", (dialog) => dialog.dismiss());
  await removeChat(page, "Quick lunches");
  expect(chat.deletes).toHaveLength(0);
  state.token = "r".repeat(43);
  page.once("dialog", (dialog) => dialog.accept());
  await removeChat(page, "Quick lunches");
  await expect(
    page.getByRole("region", { name: "Conversation: New chat", exact: true }),
  ).toBeVisible();
  expect(chat.deletes).toEqual([{ id: firstId, token: "r".repeat(43) }]);
  await page.reload();
  await openChats(page);
  await expect(
    page.getByRole("button", { name: "Quick lunches", exact: false }),
  ).toHaveCount(0);
  await choose(page, "Weekend dinners");
  await expect(
    page.getByText("Weekend vegetable supper", { exact: true }),
  ).toBeVisible();
});

test("failed deletion keeps the selected chat and allows an explicit retry", async ({
  page,
}) => {
  const { chat } = await conversations(page);
  await page.goto("/#sous-chef");
  await choose(page, "Quick lunches");
  chat.failure = { target: "delete", status: 503 };
  page.on("dialog", (dialog) => dialog.accept());
  await removeChat(page, "Quick lunches");
  await expect(
    page.getByText("Chat deletion could not be confirmed.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("Lemony lunch ideas", { exact: true }),
  ).toBeAttached();
  expect(chat.deletes).toHaveLength(1);
  await removeChat(page, "Quick lunches");
  await expect(
    page.getByRole("region", { name: "Conversation: New chat", exact: true }),
  ).toBeVisible();
  expect(chat.deletes).toHaveLength(2);
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
    await openChats(page);
    await page
      .getByRole("button", { name: "Refresh chats and messages" })
      .click();
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
    page.getByRole("region", { name: "Conversation: New chat", exact: true }),
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
