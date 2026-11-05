import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import {
  inspectOperation,
  rememberOperation,
  safeTitle,
  savedOperation,
  sendMessage,
  type ChatAttempt,
  type Operation,
  type Turn,
} from "./conversations";
import type { Session } from "./profile";

type Phase =
  | "idle"
  | "sending"
  | "checking"
  | "pending"
  | "failed"
  | "uncertain"
  | "unknown";

export default function ChatComposer({
  id,
  owner,
  session,
  turns,
  historyReady,
  onTurn,
  onRefresh,
  onExpired,
  onBusy,
  onDirty,
}: {
  id: string;
  owner: string;
  session: Session;
  turns: Turn[];
  historyReady: boolean;
  onTurn: (turn: Turn) => void;
  onRefresh: () => void;
  onExpired: () => void;
  onBusy: (busy: boolean) => void;
  onDirty: (dirty: boolean) => void;
}) {
  const [draft, setDraft] = useState("");
  const [phase, setPhase] = useState<Phase>("checking");
  const [operationId, setOperationId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [validation, setValidation] = useState("");
  const [storageNotice, setStorageNotice] = useState("");
  const attempt = useRef<ChatAttempt | null>(null);
  const busy = useRef(false);
  const lifetime = useRef<AbortController | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const working = phase === "sending" || phase === "checking";
  const unresolved = turns.some(
    (turn) => turn.status === "pending" || turn.status === "outcomeUnknown",
  );
  const locked =
    working ||
    phase === "pending" ||
    phase === "unknown" ||
    phase === "uncertain";

  const remember = useCallback(
    (operation: string | null) => {
      try {
        rememberOperation(owner, id, operation);
      } catch {
        setStorageNotice(
          "This browser cannot remember operation checks after reload. Saved requests remain on the server.",
        );
      }
    },
    [owner, id],
  );

  const accept = useCallback(
    (operation: Operation, original = attempt.current) => {
      if (
        original &&
        (operation.turn.messages[0]?.content !== original.content ||
          (original.retryOfTurnId &&
            operation.turn.turnId !== original.retryOfTurnId))
      )
        throw new ApiError(502, "invalid_response");
      onTurn(operation.turn);
      const status = operation.turn.status;
      if (status === "completed") {
        remember(null);
        setOperationId(null);
        attempt.current = null;
        setDraft("");
        setNotice("");
        setPhase("idle");
      } else if (status === "failed") {
        remember(null);
        setOperationId(null);
        setNotice(
          "No completed answer was saved. Explicitly retry the saved request below.",
        );
        setPhase("failed");
      } else if (status === "outcomeUnknown") {
        setNotice(
          "The outcome is unknown. This chat is blocked to avoid duplicate generation. A service owner must resolve it before another request can be sent.",
        );
        setPhase("unknown");
      } else {
        setNotice("");
        setPhase("pending");
      }
    },
    [onTurn, remember],
  );

  const failed = useCallback(
    (error: unknown) => {
      if (error instanceof ApiError && error.status === 401) {
        attempt.current = null;
        setDraft("");
        onExpired();
      } else if (error instanceof ApiError && error.code === "send_conflict") {
        attempt.current = null;
        setPhase("idle");
        setNotice(
          "Another request owns this chat, or that turn is no longer retryable. Refresh saved history and review it before sending again.",
        );
        onRefresh();
      } else {
        setPhase("uncertain");
        setNotice(
          error instanceof ApiError && error.code === "csrf_failed"
            ? "Your security token changed. Check this request explicitly with a fresh token; nothing was automatically replayed."
            : error instanceof ApiError && error.code === "idempotency_conflict"
              ? "This request conflicts with its saved receipt. Check saved history; do not send it with a new key."
              : attempt.current
                ? "No completed answer is confirmed. Check this same request before taking another action. Your input has been kept."
                : "No completed answer is confirmed. Check saved status or persisted history before taking another action.",
        );
      }
    },
    [onExpired, onRefresh],
  );

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    async function restore() {
      let saved: string | undefined;
      try {
        saved = savedOperation(owner, id);
      } catch {
        setStorageNotice(
          "Saved operation checks could not be read. Inspect persisted history before sending.",
        );
      }
      if (!saved) {
        setPhase("idle");
        return;
      }
      setOperationId(saved);
      try {
        const operation = await inspectOperation(
          session,
          id,
          saved,
          controller.signal,
        );
        if (!controller.signal.aborted) accept(operation, null);
      } catch (error) {
        if (!controller.signal.aborted) failed(error);
      }
    }
    void restore();
    return () => controller.abort();
  }, [id, owner, session, accept, failed]);

  useEffect(() => {
    onBusy(working);
    return () => onBusy(false);
  }, [working, onBusy]);

  useEffect(() => {
    onDirty(draft.length > 0);
    return () => onDirty(false);
  }, [draft, onDirty]);

  useEffect(() => {
    if (phase !== "pending" || !operationId) return;
    const controller = new AbortController();
    const started = Date.now();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const operation = await inspectOperation(
          session,
          id,
          operationId!,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        accept(operation);
        if (operation.turn.status === "pending") {
          if (Date.now() - started >= 75_000)
            failed(new ApiError(0, "transport"));
          else timer = setTimeout(() => void poll(), 2_000);
        }
      } catch (error) {
        if (!controller.signal.aborted) failed(error);
      }
    }
    timer = setTimeout(() => void poll(), 2_000);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [phase, operationId, id, session, accept, failed]);

  async function send(original: ChatAttempt) {
    const controller = lifetime.current;
    if (!controller || busy.current) return;
    busy.current = true;
    attempt.current = original;
    setDraft(original.content);
    setValidation("");
    setNotice("");
    setPhase("sending");
    try {
      const result = await sendMessage(
        session,
        id,
        original,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      if (result.kind === "completed") {
        onTurn(result.turn);
        remember(null);
        attempt.current = null;
        setOperationId(null);
        setDraft("");
        setPhase("idle");
        input.current?.focus();
      } else {
        setOperationId(result.operationId);
        remember(result.operationId);
        const operation =
          result.operation ??
          (await inspectOperation(
            session,
            id,
            result.operationId,
            controller.signal,
          ));
        if (!controller.signal.aborted) accept(operation);
      }
    } catch (error) {
      if (!controller.signal.aborted) failed(error);
    } finally {
      busy.current = false;
    }
  }

  async function check() {
    const controller = lifetime.current;
    if (!controller || busy.current) return;
    if (!operationId) {
      if (attempt.current) await send(attempt.current);
      else onRefresh();
      return;
    }
    busy.current = true;
    setPhase("checking");
    try {
      const operation = await inspectOperation(
        session,
        id,
        operationId,
        controller.signal,
      );
      if (!controller.signal.aborted) accept(operation);
    } catch (error) {
      if (!controller.signal.aborted) failed(error);
    } finally {
      busy.current = false;
    }
  }

  function submit() {
    if (locked || unresolved || !historyReady || phase === "failed") return;
    const content = draft.trim();
    if (!content || [...content].length > 4000) {
      setValidation("Write a request between 1 and 4,000 characters.");
      input.current?.focus();
      return;
    }
    void send({ key: crypto.randomUUID(), content });
  }

  return (
    <section className="chat-composer" aria-label="Recipe request">
      {storageNotice && (
        <p className="notice" role="status">
          {storageNotice}
        </p>
      )}
      {notice && (
        <div className="notice" role="alert">
          <p>I couldn't complete that recipe request</p>
          <p>{notice}</p>
        </div>
      )}
      <p className="turn-status" role="status" aria-live="polite">
        {phase === "sending"
          ? "Sending your request. Waiting for a saved reply..."
          : phase === "checking"
            ? "Checking the saved request..."
            : phase === "pending"
              ? "Awaiting a reply. Status is checked automatically; no new generation is sent."
              : unresolved
                ? "This chat has an unresolved request. Check its saved state before sending another."
                : ""}
      </p>
      <form
        aria-label="Send a recipe request"
        aria-busy={working || phase === "pending"}
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <label htmlFor="recipe-request">What would you like to cook?</label>
        <textarea
          id="recipe-request"
          ref={input}
          value={draft}
          rows={3}
          readOnly={locked}
          aria-invalid={Boolean(validation)}
          aria-describedby={`recipe-request-help${validation ? " recipe-request-error" : ""}`}
          onChange={(event) => {
            setDraft(event.target.value);
            setValidation("");
            if (phase === "failed") {
              attempt.current = null;
              setPhase("idle");
              setNotice("");
            }
          }}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <p id="recipe-request-help" className="privacy-note">
          Up to 4,000 characters. Enter sends; Shift+Enter adds a line. Editing
          a failed request starts a new turn; retry below keeps its original
          text.
        </p>
        {validation && (
          <p id="recipe-request-error" className="notice" role="alert">
            {validation}
          </p>
        )}
        <div className="chat-actions">
          <button
            className="button primary"
            type="submit"
            disabled={
              locked || unresolved || !historyReady || phase === "failed"
            }
          >
            {phase === "sending" ? "Sending..." : "Send request"}
          </button>
          {(phase === "uncertain" || phase === "unknown") && (
            <button
              className="button secondary"
              type="button"
              onClick={() => void check()}
            >
              {operationId ? "Check saved status" : "Check this request"}
            </button>
          )}
        </div>
      </form>
      {turns.some((turn) => turn.status !== "completed") && (
        <section
          className="request-recovery"
          aria-label="Saved request recovery"
        >
          {turns
            .filter((turn) => turn.status !== "completed")
            .map((turn) => (
              <div className="saved-request" key={turn.turnId}>
                <p>{safeTitle(turn.messages[0]?.content ?? "Saved request")}</p>
                {turn.status === "failed" ? (
                  <>
                    <p>
                      I couldn't complete that recipe request. No completed
                      answer was saved.
                    </p>
                    <button
                      className="button secondary"
                      disabled={
                        working ||
                        locked ||
                        unresolved ||
                        !historyReady ||
                        (draft.trim().length > 0 &&
                          draft.trim() !== turn.messages[0]?.content)
                      }
                      onClick={() =>
                        void send({
                          key: crypto.randomUUID(),
                          content: turn.messages[0]!.content,
                          retryOfTurnId: turn.turnId,
                        })
                      }
                    >
                      Retry saved request
                    </button>
                  </>
                ) : (
                  <p>
                    {turn.status === "pending"
                      ? "This request is pending. Leaving or reloading does not cancel it."
                      : "The outcome is unknown. Do not retry; this chat remains blocked until the service owner resolves it."}
                  </p>
                )}
              </div>
            ))}
          <button
            className="text-button"
            disabled={working}
            onClick={onRefresh}
          >
            Check saved history
          </button>
        </section>
      )}
      <p className="privacy-note">
        There is no remote cancellation control. Refreshing or closing the page
        does not prove a request stopped. Draft text stays in memory and is
        cleared on leaving this view or session expiry; accepted requests remain
        on the server.
      </p>
    </section>
  );
}
