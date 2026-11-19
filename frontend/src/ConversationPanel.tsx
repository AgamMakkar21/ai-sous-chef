import {
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type ReactNode,
  type Ref,
} from "react";
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
import { KitchenIcon, PixelArt } from "./KitchenArt";
import useScrollPages from "./useScrollPages";

function ScrollPane({
  children,
  label,
  className,
  onMore,
  follow,
}: {
  children: ReactNode;
  label: string;
  className: string;
  onMore: () => void;
  follow?: readonly Turn[];
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const intent = useRef(false);
  useEffect(() => {
    if (!follow?.length || !scroll.current) return;
    intent.current = false;
    scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [follow]);
  function check(element: HTMLDivElement) {
    if (!intent.current) return;
    if (element.scrollHeight - element.scrollTop - element.clientHeight < 120) {
      intent.current = false;
      onMore();
    }
  }
  return (
    <div
      ref={scroll}
      className={className}
      role="region"
      aria-label={label}
      tabIndex={0}
      onScroll={(event) => check(event.currentTarget)}
      onWheel={(event) => {
        if (event.deltaY > 0) {
          intent.current = true;
          check(event.currentTarget);
        }
      }}
      onTouchMove={() => {
        intent.current = true;
      }}
      onPointerDown={() => {
        intent.current = true;
      }}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (["End", "PageDown", "ArrowDown", " "].includes(event.key)) {
          intent.current = true;
          check(event.currentTarget);
        }
      }}
    >
      {children}
    </div>
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
  onCompleted,
  blocked,
  persistedId,
  ref,
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
  onCompleted: () => void;
  blocked: boolean;
  persistedId: string | null;
  ref: Ref<() => void>;
}) {
  const load = useCallback(
    (cursor: string | null, signal: AbortSignal) =>
      prepare && !persistedId
        ? Promise.resolve({ items: [], nextCursor: null })
        : listTurns(session, persistedId ?? id, cursor, signal),
    [id, session, prepare, persistedId],
  );
  const history = useScrollPages(load, onExpired);
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
      if (turn.recipe || (turn.turnIndex === 0 && turn.messages[0]))
        onTitle(
          turn.conversationId,
          safeTitle(turn.recipe?.title ?? turn.messages[0]!.content),
        );
      if (turn.status === "completed") onCompleted();
    },
    [onTitle, onStarted, onCompleted],
  );
  const loaded = history.state.kind === "ready" ? history.state.page.items : [];
  const byId = new Map(updates.map((turn) => [turn.turnId, turn]));
  const visible = [
    ...new Map([
      ...loaded.map(
        (turn) => [turn.turnId, byId.get(turn.turnId) ?? turn] as const,
      ),
      ...updates.map((turn) => [turn.turnId, turn] as const),
    ]).values(),
  ].sort((left, right) => left.turnIndex - right.turnIndex);
  const refreshHistory = history.refresh;
  const refresh = useCallback(() => {
    setUpdates([]);
    refreshHistory();
  }, [refreshHistory]);
  useImperativeHandle(ref, () => refresh, [refresh]);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (focusRequest > 0) heading.current?.focus();
  }, [focusRequest]);
  useEffect(() => {
    if (history.state.kind === "ready") {
      const turns = history.state.page.items;
      const recipe = turns.find((turn) => turn.recipe)?.recipe;
      const first = turns[0]?.messages[0];
      if (recipe || first)
        onTitle(persistedId ?? id, safeTitle(recipe?.title ?? first!.content));
    }
  }, [history.state, id, persistedId, onTitle]);

  return (
    <section
      className="conversation-transcript"
      aria-label={`Conversation: ${title}`}
      data-empty={visible.length === 0 && history.state.kind === "ready"}
    >
      <div className="conversation-heading">
        <h2 id="transcript-title" ref={heading} tabIndex={-1} title={title}>
          {title}
        </h2>
      </div>
      <ScrollPane
        className="conversation-scroll"
        label="Messages"
        follow={updates}
        onMore={() => {
          if (!blocked && !chatBusy && !recipeBusy) void history.more();
        }}
      >
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
              onClick={() => history.refresh()}
            >
              Try history again
            </CartoonButton>
          </div>
        )}
        {history.state.kind === "ready" &&
          (visible.length === 0 ? (
            <div className="conversation-empty" aria-hidden="true">
              <PixelArt kind="pot" />
            </div>
          ) : (
            <ol
              className="conversation-turns"
              aria-label="Conversation transcript"
            >
              {visible.map((turn) => (
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
        {history.loadingMore && <p role="status">Loading more messages...</p>}
        {history.moreError !== null && (
          <div className="notice" role="alert">
            <p>More messages could not be loaded.</p>
            <CartoonButton
              className="secondary"
              disabled={blocked || chatBusy || recipeBusy}
              onClick={() =>
                history.moreError === 400
                  ? history.refresh()
                  : void history.more(true)
              }
            >
              Try history again
            </CartoonButton>
          </div>
        )}
      </ScrollPane>
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
  const [viewKey, setViewKey] = useState<string | null>(null);
  const transcriptRefresh = useRef<(() => void) | null>(null);
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
  const chats = useScrollPages(load, onExpired);

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
      const draft = restored.selectedId
        ? null
        : (restored.creationKey ?? crypto.randomUUID());
      setDraftId(draft);
      setViewKey(restored.selectedId ?? draft);
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

  function closeMobileSidebar() {
    if (window.matchMedia("(max-width: 720px)").matches) setSidebarOpen(false);
  }

  function select(id: string) {
    if (!current.current || deletingRef.current || sending) return;
    if (
      (draftId || id !== current.current.selectedId) &&
      chatDirty.current &&
      !window.confirm(
        "Leave this recipe request? Unsaved input will be discarded; accepted requests are not cancelled.",
      )
    ) {
      closeMobileSidebar();
      return;
    }
    remember({ ...current.current, selectedId: id, creationKey: null });
    setDraftId(null);
    setViewKey(id);
    setFocusRequest((value) => value + 1);
    setNotice("");
    closeMobileSidebar();
  }

  function create() {
    const previous = current.current;
    if (!previous || deletingRef.current || sending) return;
    if (draftId && !previous.selectedId && !previous.creationKey) {
      closeMobileSidebar();
      setFocusRequest((value) => value + 1);
      return;
    }
    if (
      chatDirty.current &&
      !window.confirm(
        "Leave this recipe request? Unsaved input will be discarded; accepted requests are not cancelled.",
      )
    )
      return;
    setNotice("");
    const draft = crypto.randomUUID();
    setDraftId(draft);
    setViewKey(draft);
    remember({ ...previous, selectedId: null, creationKey: null });
    setFocusRequest((value) => value + 1);
    closeMobileSidebar();
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
  const completed = useCallback(() => refreshChats(), [refreshChats]);
  const started = useCallback(
    (turn: Turn) => {
      if (!current.current) return;
      remember({
        ...current.current,
        selectedId: turn.conversationId,
        creationKey: null,
      });
      setDraftId(null);
      refreshChats();
    },
    [refreshChats, remember],
  );

  const newChatButton = useRef<HTMLButtonElement>(null);
  const focusAfterDelete = useRef(false);
  useEffect(() => {
    if (!deleting && focusAfterDelete.current) {
      focusAfterDelete.current = false;
      newChatButton.current?.focus();
    }
  }, [deleting]);
  async function remove(id: string) {
    const previous = current.current;
    const controller = lifetime.current;
    if (!previous || !controller || deletingRef.current || sending) return;
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
      await deleteConversation(session, id, controller.signal);
      if (controller.signal.aborted || !current.current) return;
      if (current.current.selectedId === id) {
        remember({ ...current.current, selectedId: null, creationKey: null });
        const draft = crypto.randomUUID();
        setDraftId(draft);
        setViewKey(draft);
        trackDirty(false);
        closeMobileSidebar();
      }
      try {
        rememberOperation(previous.owner, id, null);
      } catch {
        setStorageNotice(
          "Deleted chat navigation could not be cleared from browser storage.",
        );
      }
      chats.refresh();
      focusAfterDelete.current = true;
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

  const items =
    chats.state.kind === "ready"
      ? [
          ...new Map(
            chats.state.page.items.map((item) => [item.conversationId, item]),
          ).values(),
        ]
      : [];
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
  const choices = items.map((item) => ({
    id: item.conversationId,
    title: titleFor(item),
  }));
  if (selected && !summary) choices.unshift({ id: selected, title });

  return (
    <section
      className="conversation-panel"
      aria-label="Sous Chef conversations"
    >
      <div className="conversation-toolbar">
        <CartoonButton
          className="secondary sidebar-toggle"
          aria-label="Conversations"
          title="Toggle chats"
          aria-expanded={sidebarOpen}
          aria-controls="conversation-sidebar"
          onClick={() => setSidebarOpen((value) => !value)}
        >
          <KitchenIcon name="sidebar" />
        </CartoonButton>
        <div>
          <h1 className="kitchen-title">Your conversations</h1>
        </div>
        <CartoonButton
          ref={newChatButton}
          label="New Chat"
          disabled={!navigation || deleting || sending}
          onClick={create}
        />
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
      <div className="conversation-layout card" data-sidebar-open={sidebarOpen}>
        <aside id="conversation-sidebar" hidden={!sidebarOpen}>
          <div className="conversation-heading">
            <h2>Chats</h2>
            <CartoonButton
              className="secondary chat-tool"
              aria-label="Refresh chats and messages"
              title="Refresh chats and messages"
              disabled={deleting || sending}
              onClick={() => {
                chats.refresh();
                transcriptRefresh.current?.();
              }}
            >
              <KitchenIcon name="refresh" />
            </CartoonButton>
          </div>
          <ScrollPane
            className="chat-list-scroll"
            label="Chat list"
            onMore={() => {
              if (!deleting && !sending) void chats.more();
            }}
          >
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
                  onClick={() => chats.refresh()}
                >
                  Try chats again
                </CartoonButton>
              </div>
            )}
            {chats.state.kind === "ready" && items.length === 0 && (
              <p className="privacy-note">Your chats will appear here.</p>
            )}
            <nav aria-label="Conversations">
              <ul className="conversation-list">
                {choices.map((item) => (
                  <li key={item.id} data-selected={item.id === selected}>
                    <button
                      type="button"
                      className="conversation-choice"
                      aria-current={item.id === selected ? "true" : undefined}
                      disabled={!navigation || deleting || sending}
                      title={item.title}
                      onClick={() => select(item.id)}
                    >
                      <span>{item.title}</span>
                    </button>
                    <button
                      type="button"
                      className="conversation-delete"
                      aria-label="Delete chat"
                      aria-description={item.title}
                      title={`Delete ${item.title}`}
                      disabled={deleting || sending}
                      onClick={() => void remove(item.id)}
                    >
                      <KitchenIcon name="trash" />
                    </button>
                  </li>
                ))}
              </ul>
            </nav>
            {chats.loadingMore && <p role="status">Loading more chats...</p>}
            {chats.moreError !== null && (
              <div className="notice" role="alert">
                <p>More chats could not be loaded.</p>
                <CartoonButton
                  className="secondary"
                  disabled={deleting || sending}
                  onClick={() =>
                    chats.moreError === 400
                      ? chats.refresh()
                      : void chats.more(true)
                  }
                >
                  Try chats again
                </CartoonButton>
              </div>
            )}
          </ScrollPane>
        </aside>
        {selected || draftId ? (
          <Transcript
            key={viewKey}
            ref={transcriptRefresh}
            persistedId={navigation!.selectedId}
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
            onCompleted={completed}
            blocked={deleting}
          />
        ) : (
          <section
            className="conversation-empty"
            aria-label="No conversation selected"
          >
            <p role="status">Opening your chat...</p>
          </section>
        )}
      </div>
    </section>
  );
}
