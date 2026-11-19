import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { CartoonButton } from "@/components/ui/cartoon-button";
import { KitchenIcon } from "./KitchenArt";
import {
  CHAT_TIMEOUT_MS,
  closeOperation,
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
  blocked = false,
  prepare,
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
  blocked?: boolean;
  prepare?: (signal: AbortSignal) => Promise<string>;
}) {
  const [draft, setDraft] = useState("");
  const currentDraft = useRef("");
  const updateDraft = useCallback((value: string) => {
    currentDraft.current = value;
    setDraft(value);
  }, []);
  const [phase, setPhase] = useState<Phase>("checking");
  const [operationId, setOperationId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [validation, setValidation] = useState("");
  const [storageNotice, setStorageNotice] = useState("");
  const attempt = useRef<ChatAttempt | null>(null);
  const conversationId = useRef(id);
  const busy = useRef(false);
  const lifetime = useRef<AbortController | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const working = phase === "sending" || phase === "checking";
  const unresolvedTurn = turns.find(
    (turn) => turn.status === "pending" || turn.status === "outcomeUnknown",
  );
  const unresolved = Boolean(unresolvedTurn);
  const persistedOperationId = unresolvedTurn?.operationId;
  const locked = blocked || working;
  const sendLocked =
    locked ||
    unresolved ||
    phase === "pending" ||
    phase === "unknown" ||
    phase === "uncertain" ||
    !historyReady;

  const remember = useCallback(
    (operation: string | null) => {
      try {
        rememberOperation(owner, conversationId.current, operation);
      } catch {
        setStorageNotice(
          "This browser cannot remember operation checks after reload. Saved requests remain on the server.",
        );
      }
    },
    [owner],
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
      const sentContent = operation.turn.messages[0]!.content;
      if (
        (status === "pending" || status === "outcomeUnknown") &&
        operation.turn.operationId
      ) {
        setOperationId(operation.turn.operationId);
        remember(operation.turn.operationId);
      }
      if (status === "completed") {
        remember(null);
        setOperationId(null);
        attempt.current = null;
        if (currentDraft.current.trim() === sentContent) updateDraft("");
        setNotice("");
        setPhase("idle");
      } else if (status === "failed") {
        remember(null);
        setOperationId(null);
        const sameRequest = currentDraft.current.trim() === sentContent;
        attempt.current = sameRequest
          ? {
              key: original?.key ?? crypto.randomUUID(),
              content: sentContent,
              retryOfTurnId: operation.turn.turnId,
            }
          : null;
        setNotice(
          "No completed answer was saved. Edit your request or retry it.",
        );
        setPhase(sameRequest ? "failed" : "idle");
      } else if (status === "outcomeUnknown") {
        setNotice(
          "The outcome is unknown. Check saved status, or explicitly close the interrupted request without retrying. No new generation will be sent automatically.",
        );
        setPhase("unknown");
      } else {
        setNotice("");
        setPhase("pending");
      }
    },
    [onTurn, remember, updateDraft],
  );

  const failed = useCallback(
    (error: unknown) => {
      if (error instanceof ApiError && error.status === 401) {
        attempt.current = null;
        updateDraft("");
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
                ? "No completed answer is confirmed. You can edit your text; check this request before sending again."
                : "No completed answer is confirmed. Check saved status or persisted history before taking another action.",
        );
      }
    },
    [onExpired, onRefresh, updateDraft],
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
          conversationId.current,
          operationId!,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        accept(operation);
        if (operation.turn.status === "pending") {
          if (Date.now() - started >= CHAT_TIMEOUT_MS)
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

  async function send(original: ChatAttempt, preserveDraft = false) {
    const controller = lifetime.current;
    if (!controller || busy.current) return;
    busy.current = true;
    attempt.current = original;
    if (!preserveDraft) updateDraft(original.content);
    setValidation("");
    setNotice("");
    setPhase("sending");
    try {
      if (prepare) conversationId.current = await prepare(controller.signal);
      const target = conversationId.current;
      const result = await sendMessage(
        session,
        target,
        original,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      if (result.kind === "completed") {
        onTurn(result.turn);
        remember(null);
        attempt.current = null;
        setOperationId(null);
        if (currentDraft.current.trim() === original.content) updateDraft("");
        setPhase("idle");
        input.current?.focus();
      } else {
        setOperationId(result.operationId);
        remember(result.operationId);
        const operation =
          result.operation ??
          (await inspectOperation(
            session,
            target,
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

  async function check(savedId?: string) {
    const controller = lifetime.current;
    if (!controller || busy.current) return;
    const target = savedId ?? persistedOperationId ?? operationId;
    if (!target) {
      if (attempt.current) await send(attempt.current, true);
      else onRefresh();
      return;
    }
    busy.current = true;
    setOperationId(target);
    remember(target);
    setPhase("checking");
    try {
      const operation = await inspectOperation(
        session,
        conversationId.current,
        target,
        controller.signal,
      );
      if (!controller.signal.aborted) accept(operation);
    } catch (error) {
      if (!controller.signal.aborted) failed(error);
    } finally {
      busy.current = false;
    }
  }

  async function close(savedId: string) {
    const controller = lifetime.current;
    if (!controller || busy.current) return;
    busy.current = true;
    setPhase("checking");
    try {
      const operation = await closeOperation(
        session,
        conversationId.current,
        savedId,
        controller.signal,
      );
      if (!controller.signal.aborted) {
        accept(operation, null);
        onRefresh();
      }
    } catch (error) {
      if (!controller.signal.aborted) failed(error);
    } finally {
      busy.current = false;
    }
  }

  function submit() {
    if (sendLocked) return;
    const content = draft.trim();
    if (!content || [...content].length > 4000) {
      setValidation("Write a request between 1 and 4,000 characters.");
      input.current?.focus();
      return;
    }
    const previous = attempt.current;
    void send({
      key: crypto.randomUUID(),
      content,
      ...(previous?.retryOfTurnId && previous.content === content
        ? { retryOfTurnId: previous.retryOfTurnId }
        : {}),
    });
  }

  return (
    <section className="chat-composer" aria-label="Recipe request">
      <div
        className="chat-recovery-scroll"
        role="region"
        aria-label="Request status"
        tabIndex={0}
      >
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
        {turns.some((turn) => turn.status !== "completed") && (
          <section
            className="request-recovery"
            aria-label="Saved request recovery"
          >
            {turns
              .filter((turn) => turn.status !== "completed")
              .map((turn) => (
                <div className="saved-request" key={turn.turnId}>
                  <p>
                    {safeTitle(turn.messages[0]?.content ?? "Saved request")}
                  </p>
                  {turn.status === "failed" ? (
                    <>
                      <p>
                        I couldn't complete that recipe request. No completed
                        answer was saved.
                      </p>
                      <CartoonButton
                        className="secondary"
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
                      </CartoonButton>
                    </>
                  ) : (
                    <>
                      <p>
                        {turn.status === "pending"
                          ? "This request is pending. Leaving or reloading does not cancel it."
                          : "The outcome is unknown. Closing this attempt marks it failed without generating another answer. Retry remains a separate action."}
                      </p>
                      {turn.operationId && (
                        <CartoonButton
                          className="secondary"
                          disabled={working}
                          onClick={() =>
                            void check(turn.operationId ?? undefined)
                          }
                        >
                          Check saved status
                        </CartoonButton>
                      )}
                      {turn.status === "outcomeUnknown" && turn.operationId && (
                        <CartoonButton
                          className="secondary"
                          disabled={working}
                          onClick={() => void close(turn.operationId!)}
                        >
                          Close interrupted request without retrying
                        </CartoonButton>
                      )}
                    </>
                  )}
                </div>
              ))}
            <CartoonButton
              className="secondary"
              disabled={working}
              onClick={onRefresh}
            >
              Check saved history
            </CartoonButton>
          </section>
        )}
      </div>
      <form
        className="field"
        aria-label="Send a recipe request"
        aria-busy={working || phase === "pending"}
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <label className="sr-only" htmlFor="recipe-request">
          What would you like to cook?
        </label>
        <textarea
          id="recipe-request"
          ref={input}
          value={draft}
          rows={3}
          placeholder="What would you like to cook?"
          readOnly={locked}
          aria-invalid={Boolean(validation)}
          aria-description="Enter to send. Shift+Enter for a new line."
          aria-describedby={validation ? "recipe-request-error" : undefined}
          onChange={(event) => {
            updateDraft(event.target.value);
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
        {validation && (
          <p id="recipe-request-error" className="notice" role="alert">
            {validation}
          </p>
        )}
        <div className="chat-actions">
          <CartoonButton
            className="chat-send"
            aria-label={phase === "sending" ? "Sending..." : "Send request"}
            type="submit"
            disabled={sendLocked}
          >
            <KitchenIcon name="arrow" />
          </CartoonButton>
          {(phase === "uncertain" || phase === "unknown") &&
            !persistedOperationId && (
              <CartoonButton
                className="secondary"
                type="button"
                onClick={() => void check()}
              >
                {operationId ? "Check saved status" : "Check this request"}
              </CartoonButton>
            )}
        </div>
      </form>
    </section>
  );
}
