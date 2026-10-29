import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import {
  createConversation,
  listConversations,
  listTurns,
  navigationKey,
  navigationOwner,
  readNavigation,
  safeTitle,
  type Conversation,
  type Navigation,
  type Page,
  type Turn,
} from "./conversations";
import type { Session } from "./profile";

type Loader<T> = (
  cursor: string | null,
  signal: AbortSignal,
) => Promise<Page<T>>;
type LoadState<T> =
  | { kind: "loading" }
  | { kind: "failed"; status: number }
  | { kind: "ready"; page: Page<T> };

function usePage<T>(load: Loader<T>, onExpired: () => void) {
  const [state, setState] = useState<LoadState<T>>({ kind: "loading" });
  const [navigation, setNavigation] = useState<{
    cursors: (string | null)[];
    index: number;
    revision: number;
  }>({ cursors: [null], index: 0, revision: 0 });
  const cursor = navigation.cursors[navigation.index] ?? null;

  useEffect(() => {
    const controller = new AbortController();
    async function read() {
      try {
        const page = await load(cursor, controller.signal);
        if (controller.signal.aborted) return;
        if (
          page.nextCursor !== null &&
          navigation.cursors
            .slice(0, navigation.index + 1)
            .includes(page.nextCursor)
        )
          throw new ApiError(502, "invalid_response");
        setState({ kind: "ready", page });
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) onExpired();
        else
          setState({
            kind: "failed",
            status: error instanceof ApiError ? error.status : 0,
          });
      }
    }
    void read();
    return () => controller.abort();
  }, [load, cursor, navigation, onExpired]);

  function move(forward: boolean) {
    if (state.kind !== "ready") return;
    const next = state.page.nextCursor;
    if (forward && next !== null) {
      setState({ kind: "loading" });
      setNavigation((current) => ({
        ...current,
        cursors: [...current.cursors.slice(0, current.index + 1), next],
        index: current.index + 1,
      }));
    } else if (!forward && navigation.index > 0) {
      setState({ kind: "loading" });
      setNavigation((current) => ({ ...current, index: current.index - 1 }));
    }
  }

  function refresh(restart = false) {
    setState({ kind: "loading" });
    setNavigation((current) => ({
      cursors: restart ? [null] : current.cursors,
      index: restart ? 0 : current.index,
      revision: current.revision + 1,
    }));
  }

  return { state, index: navigation.index, move, refresh };
}

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
      <button
        className="button secondary"
        disabled={!ready || index === 0}
        onClick={() => move(false)}
      >
        Earlier {noun}
      </button>
      <span>Page {index + 1}</span>
      <button
        className="button secondary"
        disabled={!ready || !next}
        onClick={() => move(true)}
      >
        {noun === "chats" ? "More chats" : "Later messages"}
      </button>
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
}: {
  id: string;
  title: string;
  session: Session;
  onExpired: () => void;
  onTitle: (id: string, title: string) => void;
}) {
  const load = useCallback(
    (cursor: string | null, signal: AbortSignal) =>
      listTurns(session, id, cursor, signal),
    [id, session],
  );
  const history = usePage(load, onExpired);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), []);
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
        <button className="text-button" onClick={() => history.refresh(true)}>
          Refresh history
        </button>
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
          <button
            className="button secondary"
            onClick={() =>
              history.refresh(
                history.state.kind === "failed" && history.state.status === 400,
              )
            }
          >
            Try history again
          </button>
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
            {history.state.page.items.map((turn) => (
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
                    <p>{message.content}</p>
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
        ready={history.state.kind === "ready"}
        next={
          history.state.kind === "ready" &&
          history.state.page.nextCursor !== null
        }
        noun="messages"
        move={history.move}
      />
      <p className="privacy-note">
        History and navigation only. Sending messages will be available in a
        later update.
      </p>
    </section>
  );
}

export default function ConversationPanel({
  session,
  onExpired,
}: {
  session: Session;
  onExpired: () => void;
}) {
  const [navigation, setNavigation] = useState<Navigation | null>(null);
  const current = useRef<Navigation | null>(null);
  const [initialization, setInitialization] = useState(0);
  const [initializationFailed, setInitializationFailed] = useState(false);
  const [storageNotice, setStorageNotice] = useState("");
  const [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const creatingRef = useRef(false);
  const lifetime = useRef<AbortController | null>(null);
  const [titles, setTitles] = useState<Record<string, string>>({});
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
    }
    void restore().catch(() => {
      if (!controller.signal.aborted) setInitializationFailed(true);
    });
    return () => controller.abort();
  }, [session.userId, initialization]);

  function remember(next: Navigation) {
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
  }

  const recordTitle = useCallback((id: string, title: string) => {
    setTitles((previous) =>
      previous[id] === title ? previous : { ...previous, [id]: title },
    );
  }, []);

  function select(id: string) {
    if (!current.current || creatingRef.current) return;
    remember({ ...current.current, selectedId: id });
    setNotice("");
    if (window.matchMedia("(max-width: 720px)").matches) setSidebarOpen(false);
  }

  async function create() {
    const previous = current.current;
    const controller = lifetime.current;
    if (!previous || !controller || creatingRef.current) return;
    creatingRef.current = true;
    setCreating(true);
    setNotice("");
    const key = previous.creationKey ?? crypto.randomUUID();
    remember({ ...previous, creationKey: key });
    try {
      const conversation = await createConversation(
        session,
        key,
        controller.signal,
      );
      if (controller.signal.aborted || !current.current) return;
      remember({
        ...current.current,
        selectedId: conversation.conversationId,
        creationKey: null,
      });
      recordTitle(
        conversation.conversationId,
        conversation.title === "New conversation"
          ? "New chat"
          : safeTitle(conversation.title),
      );
      chats.refresh(true);
      if (window.matchMedia("(max-width: 720px)").matches)
        setSidebarOpen(false);
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onExpired();
      else
        setNotice(
          error instanceof ApiError && error.code === "csrf_failed"
            ? "Your security token changed. Check chat creation again to use a fresh token. Nothing was replayed."
            : "Chat creation is not confirmed. Check it again with the same request; no additional chat will be created for this request.",
        );
    } finally {
      creatingRef.current = false;
      if (!controller.signal.aborted) setCreating(false);
    }
  }

  const items = chats.state.kind === "ready" ? chats.state.page.items : [];
  const selected = navigation?.selectedId ?? null;
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
        <button
          className="button primary"
          disabled={!navigation || creating}
          onClick={() => void create()}
        >
          {creating
            ? "Creating chat..."
            : navigation?.creationKey
              ? "Check chat creation"
              : "New Chat"}
        </button>
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
      {navigation?.creationKey && !creating && !notice && (
        <p className="notice" role="status">
          A previous chat creation has not been confirmed. Select Check chat
          creation to reconcile the same request.
        </p>
      )}
      {initializationFailed && (
        <div className="notice" role="alert">
          <p>Chat navigation could not be initialized.</p>
          <button
            className="button secondary"
            onClick={() => {
              setInitializationFailed(false);
              setInitialization((value) => value + 1);
            }}
          >
            Retry navigation
          </button>
        </div>
      )}
      <button
        className="button secondary sidebar-toggle"
        aria-expanded={sidebarOpen}
        aria-controls="conversation-sidebar"
        onClick={() => setSidebarOpen((value) => !value)}
      >
        Conversations
      </button>
      <div className="conversation-layout card" data-sidebar-open={sidebarOpen}>
        <aside id="conversation-sidebar" hidden={!sidebarOpen}>
          <div className="conversation-heading">
            <h2>Chats</h2>
            <button
              className="text-button"
              disabled={creating}
              onClick={() => chats.refresh(true)}
            >
              Refresh chats
            </button>
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
              <button
                className="button secondary"
                onClick={() =>
                  chats.refresh(
                    chats.state.kind === "failed" && chats.state.status === 400,
                  )
                }
              >
                Try chats again
              </button>
            </div>
          )}
          {selected && !summary && (
            <button
              className="conversation-choice"
              aria-current="true"
              disabled={creating}
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
                    className="conversation-choice"
                    aria-current={
                      item.conversationId === selected ? "true" : undefined
                    }
                    disabled={!navigation || creating}
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
        {selected ? (
          <Transcript
            key={selected}
            id={selected}
            title={title}
            session={session}
            onExpired={onExpired}
            onTitle={recordTitle}
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
              Sending messages is coming in a later update.
            </p>
          </section>
        )}
      </div>
    </section>
  );
}
