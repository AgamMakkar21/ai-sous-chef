import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { CartoonButton } from "@/components/ui/cartoon-button";
import ChatComposer from "./ChatComposer";
import {
  createConversation,
  deleteConversation,
  listConversations,
  listTurns,
  navigationKey,
  navigationOwner,
  readNavigation,
  rememberOperation,
  safeTitle,
  type Conversation,
  type Navigation,
  type Turn,
} from "./conversations";
import type { Session } from "./profile";
import RecipeCard from "./RecipeCard";
import RecipeActions from "./RecipeActions";
import usePage from "./usePage";

function Pager({
  index,
  ready,
  next,
  noun,
  move,
}: {
  index: number;
  ready: boolean;
  next: boolean;
  noun: "chats" | "messages";
  move: (forward: boolean) => void;
}) {
  return (
    <nav className="conversation-pager" aria-label={`${noun} pages`}>
      <CartoonButton
        className="secondary"
        disabled={!ready || index === 0}
        onClick={() => move(false)}
      >
        Earlier {noun}
      </CartoonButton>
      <span>Page {index + 1}</span>
      <CartoonButton
        className="secondary"
        disabled={!ready || !next}
        onClick={() => move(true)}
      >
        {noun === "chats" ? "More chats" : "Later messages"}
      </CartoonButton>
    </nav>
  );
}

const turnStatus: Record<Turn["status"], string> = {
  completed: "Completed",
  pending: "Awaiting a reply",
  failed: "Reply failed. No completed answer was saved.",
  outcomeUnknown: "Reply outcome unknown. No completed answer is confirmed.",
};

function Transcript({
  id,
  title,
  session,
  onExpired,
  onTitle,
  owner,
  onBusy,
  onDirty,
  focusRequest,
  prepare,
  onStarted,
  blocked,
}: {
  id: string;
  title: string;
  session: Session;
  onExpired: () => void;
  onTitle: (id: string, title: string) => void;
  owner: string;
  onBusy: (busy: boolean) => void;
  onDirty: (dirty: boolean) => void;
  focusRequest: number;
  prepare?: (signal: AbortSignal) => Promise<string>;
  onStarted?: (turn: Turn) => void;
  blocked: boolean;
}) {
  const load = useCallback(
    (cursor: string | null, signal: AbortSignal) =>
      prepare
        ? Promise.resolve({ items: [], nextCursor: null })
        : listTurns(session, id, cursor, signal),
    [id, session, prepare],
  );
  const history = usePage(load, onExpired);
  const [recipeBusy, setRecipeBusy] = useState(false);
  const [chatBusy, setChatBusy] = useState(false);
  useEffect(() => {
    onBusy(recipeBusy || chatBusy);
    return () => onBusy(false);
  }, [recipeBusy, chatBusy, onBusy]);
  const [updates, setUpdates] = useState<Turn[]>([]);
  const recordTurn = useCallback(
    (turn: Turn) => {
      onStarted?.(turn);
      setUpdates((previous) =>
        [...previous.filter((item) => item.turnId !== turn.turnId), turn].slice(
          -20,
        ),
      );
      if (turn.turnIndex === 0 && turn.messages[0])
        onTitle(turn.conversationId, safeTitle(turn.messages[0].content));
    },
    [onTitle, onStarted],
  );
  const loaded = history.state.kind === "ready" ? history.state.page.items : [];
  const byId = new Map(updates.map((turn) => [turn.turnId, turn]));
  const currentPage = loaded.map((turn) => byId.get(turn.turnId) ?? turn);
  const separate = updates
    .filter((turn) => !loaded.some((item) => item.turnId === turn.turnId))
    .sort((left, right) => left.turnIndex - right.turnIndex);
  const visible = [...currentPage, ...separate];
  const refreshHistory = history.refresh;
  const refresh = useCallback(() => {
    setUpdates([]);
    refreshHistory(true);
  }, [refreshHistory]);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (focusRequest > 0) heading.current?.focus();
  }, [focusRequest]);
  useEffect(() => {
    if (history.index === 0 && history.state.kind === "ready") {
      const first = history.state.page.items[0]?.messages[0];
      if (first) onTitle(id, safeTitle(first.content));
    }
  }, [history.state, history.index, id, onTitle]);

  return (
    <section
      className="conversation-transcript"
      aria-labelledby="transcript-title"
    >
      <div className="conversation-heading">
        <h2 id="transcript-title" ref={heading} tabIndex={-1}>
          {title}
        </h2>
        <CartoonButton
          className="secondary"
          disabled={blocked || recipeBusy || chatBusy}
          onClick={refresh}
        >
          Refresh history
        </CartoonButton>
      </div>
      {history.state.kind === "loading" && (
        <p role="status">Loading messages...</p>
      )}
      {history.state.kind === "failed" && (
        <div className="notice" role="alert">
          <p>
            {history.state.status === 404
              ? "This conversation is not available. Choose another chat."
              : history.state.status === 400
                ? "This history page has expired or changed. Restart from the first page."
                : "Messages could not be loaded. No history has been replaced with an empty chat."}
          </p>
          <CartoonButton
            className="secondary"
            onClick={() =>
              history.refresh(
                history.state.kind === "failed" && history.state.status === 400,
              )
            }
          >
            Try history again
          </CartoonButton>
        </div>
      )}
      {history.state.kind === "ready" &&
        (history.state.page.items.length === 0 ? (
          <p className="conversation-empty">No messages in this chat yet.</p>
        ) : (
          <ol
            className="conversation-turns"
            aria-label="Conversation transcript"
          >
            {currentPage.map((turn) => (
              <li key={turn.turnId}>
                {turn.messages.map((message) => (
                  <article
                    className={`conversation-message ${message.role}`}
                    key={message.messageId}
                  >
                    <div className="message-heading">
                      <strong>
                        {message.role === "user" ? "You" : "Sous Chef"}
                      </strong>
                      <time dateTime={message.timestamp}>
                        {new Date(message.timestamp).toLocaleString()}
                      </time>
                    </div>
                    {message.role === "assistant" && turn.recipe ? (
                      <RecipeCard
                        recipe={turn.recipe}
                        actions={
                          turn.generatedRecipeId && (
                            <RecipeActions
                              generatedRecipeId={turn.generatedRecipeId}
                              session={session}
                              onExpired={onExpired}
                              onBusy={setRecipeBusy}
                              disabled={blocked || recipeBusy || chatBusy}
                            />
                          )
                        }
                      />
                    ) : (
                      <p>{message.content}</p>
                    )}
                  </article>
                ))}
                {turn.status !== "completed" && (
                  <p className="turn-status">{turnStatus[turn.status]}</p>
                )}
              </li>
            ))}
          </ol>
        ))}
      <Pager
        index={history.index}
        ready={!blocked && history.state.kind === "ready"}
        next={
          history.state.kind === "ready" &&
          history.state.page.nextCursor !== null
        }
        noun="messages"
        move={history.move}
      />
      {separate.length > 0 && (
        <section className="current-request" aria-label="Recent requests">
          <h3>Recent requests</h3>
          <p className="privacy-note">
            Up to 20 recently updated requests outside the history page above.
            Older requests remain in saved, paginated history.
          </p>
          {separate.map((turn) => (
            <div key={turn.turnId}>
              {turn.messages.map((message) => (
                <article
                  className={`conversation-message ${message.role}`}
                  key={message.messageId}
                >
                  <strong>
                    {message.role === "user" ? "You" : "Sous Chef"}
                  </strong>
                  {message.role === "assistant" && turn.recipe ? (
                    <RecipeCard
                      recipe={turn.recipe}
                      actions={
                        turn.generatedRecipeId && (
                          <RecipeActions
                            generatedRecipeId={turn.generatedRecipeId}
                            session={session}
                            onExpired={onExpired}
                            onBusy={setRecipeBusy}
                            disabled={blocked || recipeBusy || chatBusy}
                          />
                        )
                      }
                    />
                  ) : (
                    <p>{message.content}</p>
                  )}
                </article>
              ))}
              {turn.status !== "completed" && (
                <p className="turn-status">{turnStatus[turn.status]}</p>
              )}
            </div>
          ))}
        </section>
      )}
      <ChatComposer
        id={id}
        owner={owner}
        session={session}
        turns={visible}
        historyReady={history.state.kind === "ready"}
        onTurn={recordTurn}
        onRefresh={refresh}
        onExpired={onExpired}
        onBusy={setChatBusy}
        blocked={blocked || recipeBusy}
        onDirty={onDirty}
        prepare={prepare}
      />
    </section>
  );
}

export default function ConversationPanel({
  session,
  onExpired,
  onBusy,
  onDirty,
}: {
  session: Session;
  onExpired: () => void;
  onBusy: (busy: boolean) => void;
  onDirty: (dirty: boolean) => void;
}) {
  const [navigation, setNavigation] = useState<Navigation | null>(null);
  const current = useRef<Navigation | null>(null);
  const [initialization, setInitialization] = useState(0);
  const [initializationFailed, setInitializationFailed] = useState(false);
  const [storageNotice, setStorageNotice] = useState("");
  const [notice, setNotice] = useState("");
  const [draftId, setDraftId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const deletingRef = useRef(false);
  const [sending, setSending] = useState(false);
  const chatDirty = useRef(false);
  const trackDirty = useCallback(
    (value: boolean) => {
      chatDirty.current = value;
      onDirty(value);
    },
    [onDirty],
  );
  const trackBusy = useCallback((value: boolean) => setSending(value), []);
  useEffect(() => {
    onBusy(deleting || sending);
    return () => onBusy(false);
  }, [deleting, sending, onBusy]);
  const lifetime = useRef<AbortController | null>(null);
  const [titles, setTitles] = useState<Record<string, string>>({});
  const [focusRequest, setFocusRequest] = useState(0);
  const [sidebarOpen, setSidebarOpen] = useState(
    () => !window.matchMedia("(max-width: 720px)").matches,
  );
  const load = useCallback(
    (cursor: string | null, signal: AbortSignal) =>
      listConversations(session, cursor, signal),
    [session],
  );
  const chats = usePage(load, onExpired);

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    async function restore() {
      const owner = await navigationOwner(session.userId);
      if (controller.signal.aborted) return;
      let restored: Navigation = { owner, selectedId: null, creationKey: null };
      try {
        restored = readNavigation(owner);
      } catch (error) {
        if (error instanceof DOMException || error instanceof SyntaxError)
          setStorageNotice(
            "Saved chat navigation could not be read. Choose a chat again; browser storage may be unavailable.",
          );
        else throw error;
      }
      current.current = restored;
      setNavigation(restored);
      setDraftId(restored.selectedId ? null : restored.creationKey);
    }
    void restore().catch(() => {
      if (!controller.signal.aborted) setInitializationFailed(true);
    });
    return () => controller.abort();
  }, [session.userId, initialization]);

  const remember = useCallback((next: Navigation) => {
    current.current = next;
    setNavigation(next);
    try {
      sessionStorage.setItem(navigationKey, JSON.stringify(next));
    } catch (error) {
      if (error instanceof DOMException)
        setStorageNotice(
          "This browser cannot remember chat navigation after reload. Your messages remain saved on the server.",
        );
      else throw error;
    }
  }, []);

  const recordTitle = useCallback((id: string, title: string) => {
    setTitles((previous) =>
      previous[id] === title ? previous : { ...previous, [id]: title },
    );
  }, []);

  function select(id: string) {
    if (!current.current || deletingRef.current || sending) return;
    if (
      (draftId || id !== current.current.selectedId) &&
      chatDirty.current &&
      !window.confirm(
        "Leave this recipe request? Unsaved input will be discarded; accepted requests are not cancelled.",
      )
    )
      return;
    remember({ ...current.current, selectedId: id, creationKey: null });
    setDraftId(null);
    setFocusRequest((value) => value + 1);
    setNotice("");
    if (window.matchMedia("(max-width: 720px)").matches) setSidebarOpen(false);
  }

  function create() {
    const previous = current.current;
    if (!previous || deletingRef.current || sending) return;
    if (
      chatDirty.current &&
      !window.confirm(
        "Leave this recipe request? Unsaved input will be discarded; accepted requests are not cancelled.",
      )
    )
      return;
    setNotice("");
    setDraftId(crypto.randomUUID());
    remember({ ...previous, selectedId: null, creationKey: null });
    setFocusRequest((value) => value + 1);
    if (window.matchMedia("(max-width: 720px)").matches) setSidebarOpen(false);
  }

  const prepare = useCallback(
    async (signal: AbortSignal) => {
      if (!draftId || !current.current)
        throw new Error("No chat draft is open.");
      remember({ ...current.current, creationKey: draftId });
      const conversation = await createConversation(session, draftId, signal);
      if (signal.aborted || !current.current)
        throw new DOMException("Aborted", "AbortError");
      remember({
        ...current.current,
        selectedId: conversation.conversationId,
        creationKey: null,
      });
      return conversation.conversationId;
    },
    [draftId, session, remember],
  );

  const refreshChats = chats.refresh;
  const started = useCallback(
    (turn: Turn) => {
      if (!current.current) return;
      remember({
        ...current.current,
        selectedId: turn.conversationId,
        creationKey: null,
      });
      setDraftId(null);
      refreshChats(true);
    },
    [refreshChats, remember],
  );

  async function remove() {
    const previous = current.current;
    const controller = lifetime.current;
    if (!previous?.selectedId || !controller || deletingRef.current || sending)
      return;
    if (
      !window.confirm(
        "Delete this chat and its messages? This cannot be undone. Saved recipes will be kept.",
      )
    )
      return;
    deletingRef.current = true;
    setDeleting(true);
    setNotice("");
    try {
      await deleteConversation(session, previous.selectedId, controller.signal);
      if (controller.signal.aborted || !current.current) return;
      remember({ ...current.current, selectedId: null, creationKey: null });
      trackDirty(false);
      try {
        rememberOperation(previous.owner, previous.selectedId, null);
      } catch {
        setStorageNotice(
          "Deleted chat navigation could not be cleared from browser storage.",
        );
      }
      chats.refresh(true);
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onExpired();
      else
        setNotice(
          error instanceof ApiError && error.status === 409
            ? "This chat has an active request or changed. Check its saved status before deleting it."
            : "Chat deletion could not be confirmed. Try deleting it again; nothing was replayed automatically.",
        );
    } finally {
      deletingRef.current = false;
      if (!controller.signal.aborted) setDeleting(false);
    }
  }

  const items = chats.state.kind === "ready" ? chats.state.page.items : [];
  const selected = draftId ? null : (navigation?.selectedId ?? null);
  function titleFor(conversation: Conversation) {
    return conversation.title === "New conversation"
      ? (titles[conversation.conversationId] ?? "New chat")
      : safeTitle(conversation.title);
  }
  const summary = items.find((item) => item.conversationId === selected);
  const title = summary
    ? titleFor(summary)
    : (selected && titles[selected]) || "Conversation";

  return (
    <section
      className="conversation-panel"
      aria-label="Sous Chef conversations"
    >
      <div className="conversation-toolbar">
        <div>
          <p className="eyebrow">YOUR SOUS CHEF</p>
          <h1>Your conversations</h1>
          <p>Separate chats. Your kitchen context, kept in its place.</p>
        </div>
        <CartoonButton
          label="New Chat"
          disabled={!navigation || deleting || sending}
          onClick={create}
        />
        {selected && !draftId && (
          <CartoonButton
            className="secondary"
            disabled={deleting || sending}
            onClick={() => void remove()}
          >
            {deleting ? "Deleting chat..." : "Delete chat"}
          </CartoonButton>
        )}
      </div>
      {storageNotice && (
        <p className="notice" role="status">
          {storageNotice}
        </p>
      )}
      {notice && (
        <p className="notice" role="alert">
          {notice}
        </p>
      )}
      {initializationFailed && (
        <div className="notice" role="alert">
          <p>Chat navigation could not be initialized.</p>
          <CartoonButton
            className="secondary"
            onClick={() => {
              setInitializationFailed(false);
              setInitialization((value) => value + 1);
            }}
          >
            Retry navigation
          </CartoonButton>
        </div>
      )}
      <CartoonButton
        className="secondary sidebar-toggle"
        aria-expanded={sidebarOpen}
        aria-controls="conversation-sidebar"
        onClick={() => setSidebarOpen((value) => !value)}
      >
        Conversations
      </CartoonButton>
      <div className="conversation-layout card" data-sidebar-open={sidebarOpen}>
        <aside id="conversation-sidebar" hidden={!sidebarOpen}>
          <div className="conversation-heading">
            <h2>Chats</h2>
            <CartoonButton
              className="secondary"
              disabled={deleting || sending}
              onClick={() => chats.refresh(true)}
            >
              Refresh chats
            </CartoonButton>
          </div>
          {chats.state.kind === "loading" && (
            <p role="status">Loading chats...</p>
          )}
          {chats.state.kind === "failed" && (
            <div className="notice" role="alert">
              <p>
                Chats could not be loaded. Your saved conversations have not
                been removed.
              </p>
              <CartoonButton
                className="secondary"
                onClick={() =>
                  chats.refresh(
                    chats.state.kind === "failed" && chats.state.status === 400,
                  )
                }
              >
                Try chats again
              </CartoonButton>
            </div>
          )}
          {selected && !summary && (
            <button
              type="button"
              className="conversation-choice"
              aria-current="true"
              disabled={deleting || sending}
              onClick={() => select(selected)}
            >
              <span>{title}</span>
              <small>Selected conversation</small>
            </button>
          )}
          {chats.state.kind === "ready" && items.length === 0 && (
            <p>No conversations yet. Start with New Chat.</p>
          )}
          <nav aria-label="Conversations">
            <ul className="conversation-list">
              {items.map((item) => (
                <li key={item.conversationId}>
                  <button
                    type="button"
                    className="conversation-choice"
                    aria-current={
                      item.conversationId === selected ? "true" : undefined
                    }
                    disabled={!navigation || deleting || sending}
                    onClick={() => select(item.conversationId)}
                  >
                    <span>{titleFor(item)}</span>
                    <time dateTime={item.createdAt}>
                      {new Date(item.createdAt).toLocaleString()}
                    </time>
                  </button>
                </li>
              ))}
            </ul>
          </nav>
          <Pager
            index={chats.index}
            ready={chats.state.kind === "ready"}
            next={
              chats.state.kind === "ready" &&
              chats.state.page.nextCursor !== null
            }
            noun="chats"
            move={chats.move}
          />
        </aside>
        {selected || draftId ? (
          <Transcript
            key={selected ?? draftId}
            id={(selected ?? draftId)!}
            title={draftId ? "New chat" : title}
            session={session}
            onExpired={onExpired}
            onTitle={recordTitle}
            owner={navigation!.owner}
            onBusy={trackBusy}
            onDirty={trackDirty}
            focusRequest={focusRequest}
            prepare={draftId ? prepare : undefined}
            onStarted={draftId ? started : undefined}
            blocked={deleting}
          />
        ) : (
          <section
            className="conversation-empty"
            aria-label="No conversation selected"
          >
            <h2>A fresh space for your next idea.</h2>
            <p>
              Choose a conversation or create a new chat. Each chat keeps its
              own message history.
            </p>
            <p className="privacy-note">
              Send a recipe request after selecting a chat.
            </p>
          </section>
        )}
      </div>
    </section>
  );
}
