import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { ApiError } from "./api";
import { CartoonButton } from "@/components/ui/cartoon-button";
import type { Session } from "./profile";
import RecipeCard from "./RecipeCard";
import {
  changeRecipe,
  getRecipe,
  listRecipes,
  ratingLabels,
  watchRecipeHistory,
  type Rating,
  type RecipeSnapshot,
  type RecipeSummary,
} from "./recipeHistoryApi";
import usePage from "./usePage";

type Props = {
  session: Session;
  onExpired: () => void;
  onBusy: (value: boolean) => void;
  onDirty: (value: boolean) => void;
};

function date(value: string) {
  return new Date(value).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function stateLabel(item: RecipeSummary | RecipeSnapshot["data"]) {
  return item.rating
    ? ratingLabels[item.rating]
    : item.state === "planned"
      ? "Planned"
      : "Needs your rating";
}

function FeedbackForm({
  current,
  session,
  onExpired,
  onBusy,
  onDirty,
  onSaved,
}: Props & {
  current: RecipeSnapshot;
  onSaved: (value: RecipeSnapshot) => void;
}) {
  const id = useId();
  const [rating, setRating] = useState<Rating | "">(current.data.rating ?? "");
  const [note, setNote] = useState(current.data.note ?? "");
  const [base, setBase] = useState(current);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [invalidNote, setInvalidNote] = useState(false);
  const [notice, setNotice] = useState("");
  const [needsReview, setNeedsReview] = useState(false);
  const lock = useRef(false);
  const noteInput = useRef<HTMLTextAreaElement>(null);
  const attempt = useRef<{ key: string; payload: string; etag: string } | null>(
    null,
  );
  const dirty =
    rating !== (current.data.rating ?? "") ||
    note !== (current.data.note ?? "");

  useEffect(() => {
    onDirty(dirty);
    return () => onDirty(false);
  }, [dirty, onDirty]);

  async function reload() {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    onBusy(true);
    try {
      const latest = await getRecipe(session, current.data.historyId);
      setBase(latest);
      attempt.current = null;
      setNeedsReview(false);
      setError("");
      setNotice(
        `Saved version loaded: ${latest.data.rating ? ratingLabels[latest.data.rating] : "Not rated"}; note: ${latest.data.note ?? "None"}. Your draft is unchanged. Review it before saving again.`,
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onExpired();
      else
        setError(
          "The saved rating could not be loaded. Your draft is unchanged.",
        );
    } finally {
      lock.current = false;
      setBusy(false);
      onBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (lock.current || !rating || needsReview) return;
    if (!note.trim()) {
      setInvalidNote(true);
      setError("Add cooking notes before saving your rating.");
      noteInput.current?.focus();
      return;
    }
    if ([...note.trim()].length > 2000) {
      setInvalidNote(true);
      setError("Keep your note to 2,000 characters or fewer.");
      noteInput.current?.focus();
      return;
    }
    lock.current = true;
    setBusy(true);
    onBusy(true);
    setError("");
    setInvalidNote(false);
    setNotice("");
    const input = { rating, note: note.trim() };
    const payload = JSON.stringify(input);
    if (
      attempt.current?.payload !== payload ||
      attempt.current.etag !== base.etag
    )
      attempt.current = { key: crypto.randomUUID(), payload, etag: base.etag };
    try {
      const saved = await changeRecipe(
        session,
        { kind: "feedback", current: base, ...input },
        attempt.current.key,
      );
      attempt.current = null;
      setRating(saved.data.rating ?? "");
      setNote(saved.data.note ?? "");
      setBase(saved);
      onSaved(saved);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onExpired();
      else {
        setNeedsReview(true);
        setError(
          error instanceof ApiError && error.status >= 400 && error.status < 500
            ? "Your rating wasn't saved. Your input is kept. Load the saved version and review it before trying again."
            : "Your rating save could not be confirmed. Your input is kept. Load the saved version before trying again; it may already have saved.",
        );
      }
    } finally {
      lock.current = false;
      setBusy(false);
      onBusy(false);
    }
  }

  return (
    <form
      className="rating-form"
      aria-label="Recipe feedback"
      noValidate
      onSubmit={(event) => void submit(event)}
    >
      <h2>Your feedback</h2>
      <fieldset disabled={busy || needsReview}>
        <legend>Recipe rating (required)</legend>
        <div className="rating-options">
          {Object.entries(ratingLabels).map(([value, label]) => (
            <label key={value}>
              <input
                type="radio"
                name={`${id}-rating`}
                value={value}
                checked={rating === value}
                onChange={() => setRating(value as Rating)}
                required
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
        <label className="field" htmlFor={`${id}-note`}>
          <span>Cooking notes (required)</span>
          <textarea
            id={`${id}-note`}
            ref={noteInput}
            value={note}
            onChange={(event) => {
              setNote(event.target.value);
              if (invalidNote) {
                setInvalidNote(false);
                setError("");
              }
            }}
            aria-invalid={invalidNote}
            aria-describedby={`${id}-note-help${invalidNote ? ` ${id}-error` : ""}`}
            rows={3}
            maxLength={4000}
            required
            placeholder="What would you repeat or change?"
          />
        </label>
        <p id={`${id}-note-help`} className="privacy-note">
          Up to 2,000 characters.
        </p>
        <CartoonButton
          label={
            busy
              ? "Saving rating..."
              : current.data.rating
                ? "Save feedback changes"
                : "Save rating"
          }
          type="submit"
          disabled={!rating || !dirty}
        />
      </fieldset>
      {error && (
        <p id={`${id}-error`} role="alert">
          {error}
        </p>
      )}
      {needsReview && (
        <CartoonButton
          type="button"
          className="secondary"
          disabled={busy}
          onClick={() => void reload()}
        >
          Load saved version
        </CartoonButton>
      )}
      {notice && <p role="status">{notice}</p>}
    </form>
  );
}

function RecipeDetail({
  id,
  onBack,
  ...props
}: Props & { id: string; onBack: () => void }) {
  const [current, setCurrent] = useState<RecipeSnapshot | null>(null);
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const lock = useRef(false);
  const cookKey = useRef<string | null>(null);
  const { session, onExpired } = props;
  const heading = useRef<HTMLHeadingElement>(null);
  const historyId = current?.data.historyId;
  const recipeState = current?.data.state;
  useEffect(() => {
    if (historyId) heading.current?.focus();
  }, [historyId, recipeState]);
  useEffect(() => {
    const controller = new AbortController();
    void getRecipe(session, id, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setCurrent(value);
          setFailed(false);
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) onExpired();
        else setFailed(true);
      });
    return () => controller.abort();
  }, [id, session, onExpired, revision]);

  async function cook() {
    if (!current || lock.current) return;
    lock.current = true;
    setBusy(true);
    props.onBusy(true);
    cookKey.current ??= crypto.randomUUID();
    try {
      setCurrent(
        await changeRecipe(
          props.session,
          { kind: "cooked", current },
          cookKey.current,
        ),
      );
      cookKey.current = null;
      setNotice("Recipe marked cooked. It is now ready for your rating.");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) props.onExpired();
      else setFailed(true);
    } finally {
      lock.current = false;
      setBusy(false);
      props.onBusy(false);
    }
  }

  return (
    <section className="history-detail" aria-label="Recipe detail">
      <CartoonButton className="secondary" disabled={busy} onClick={onBack}>
        &larr; Back to Recipe History
      </CartoonButton>
      {failed ? (
        <div className="notice" role="alert">
          <h2>Recipe could not be confirmed</h2>
          <p>
            Reload the stored recipe before making changes. No update will be
            replayed.
          </p>
          <CartoonButton
            className="secondary"
            onClick={() => {
              setCurrent(null);
              setFailed(false);
              setRevision((value) => value + 1);
              cookKey.current = null;
            }}
          >
            Reload recipe
          </CartoonButton>
        </div>
      ) : !current ? (
        <p role="status">Loading saved recipe...</p>
      ) : (
        <>
          <div className="history-detail-heading">
            <h1 ref={heading} tabIndex={-1}>
              {current.data.recipe.title}
            </h1>
            <div className="history-meta">
              <span className="history-state" data-state={current.data.state}>
                {stateLabel(current.data)}
              </span>
              <span>Saved {date(current.data.savedAt)}</span>
              {current.data.cookedAt && (
                <span>Cooked {date(current.data.cookedAt)}</span>
              )}
            </div>
          </div>
          {notice && (
            <p className="success-notice" role="status">
              {notice}
            </p>
          )}
          <div className="history-detail-layout">
            {current.data.state === "planned" ? (
              <div className="history-cook">
                <h2>Cooked this recipe?</h2>
                <p>Confirm after cooking to add your rating and notes.</p>
                <CartoonButton
                  className="primary"
                  disabled={busy}
                  onClick={() => void cook()}
                >
                  {busy ? "Saving..." : "I've cooked this"}
                </CartoonButton>
              </div>
            ) : (
              <FeedbackForm
                {...props}
                current={current}
                onSaved={(saved) => {
                  setCurrent(saved);
                  props.onDirty(false);
                  setNotice("Your rating and notes are saved.");
                }}
              />
            )}
            <div className="history-recipe">
              <h2>Saved recipe</h2>
              <p className="history-snapshot-note">
                Your original saved recipe. Pantry reflects when saved.
              </p>
              <RecipeCard recipe={current.data.recipe} />
            </div>
          </div>
        </>
      )}
    </section>
  );
}

function HistoryList({
  pending,
  session,
  onExpired,
  onOpen,
}: Pick<Props, "session" | "onExpired"> & {
  pending: boolean;
  onOpen: (id: string) => void;
}) {
  const load = useCallback(
    (cursor: string | null, signal: AbortSignal) =>
      listRecipes(session, pending, cursor, signal),
    [session, pending],
  );
  const page = usePage(load, onExpired);
  const refresh = page.refresh;
  useEffect(() => watchRecipeHistory(() => refresh(true)), [refresh]);
  const heading = pending ? "Needs your rating" : "Saved and cooked";
  return (
    <section
      className="history-section"
      aria-label={heading}
      data-pending={pending}
    >
      <h2>{heading}</h2>
      {page.state.kind === "loading" && (
        <p role="status">
          Loading {pending ? "recipes to rate" : "recipe history"}...
        </p>
      )}
      {page.state.kind === "failed" && (
        <div className="notice" role="alert">
          <p>
            {heading} could not be loaded. Your saved recipes have not been
            replaced.
          </p>
          <CartoonButton className="secondary" onClick={() => refresh(true)}>
            Try again
          </CartoonButton>
        </div>
      )}
      {page.state.kind === "ready" &&
        (page.state.page.items.length ? (
          <ul className={pending ? "pending-recipes" : "history-rows"}>
            {page.state.page.items.map((item) => (
              <li key={item.historyId}>
                <div>
                  <h3>{item.title}</h3>
                  <div className="history-meta">
                    {!pending && (
                      <span className="history-state" data-state={item.state}>
                        {stateLabel(item)}
                      </span>
                    )}
                    <span>
                      {item.cookedAt
                        ? `Cooked ${date(item.cookedAt)}`
                        : `Saved ${date(item.savedAt)}`}
                    </span>
                  </div>
                </div>
                <CartoonButton
                  className={`${pending ? "primary" : "secondary"}`}
                  aria-label={`${pending ? "Rate" : "View"} ${item.title}`}
                  onClick={() => onOpen(item.historyId)}
                >
                  {pending ? "Rate recipe" : "View recipe"}
                </CartoonButton>
              </li>
            ))}
          </ul>
        ) : (
          <p className="history-empty">
            {pending
              ? "Nothing waiting for a rating. Cook a saved recipe to leave feedback."
              : "No recipes saved yet. Save a recipe from Sous Chef to find it here."}
          </p>
        ))}
      {page.state.kind === "ready" &&
        (page.index > 0 || page.state.page.nextCursor) && (
          <nav className="conversation-pager" aria-label={`${heading} pages`}>
            <CartoonButton
              className="secondary"
              disabled={page.index === 0}
              onClick={() => page.move(false)}
            >
              Previous
            </CartoonButton>
            <span>Page {page.index + 1}</span>
            <CartoonButton
              className="secondary"
              disabled={!page.state.page.nextCursor}
              onClick={() => page.move(true)}
            >
              Next
            </CartoonButton>
          </nav>
        )}
    </section>
  );
}

export default function RecipeHistory(props: Props) {
  const { onDirty: reportDirty, onBusy: reportBusy } = props;
  const [selected, setSelected] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const returning = useRef(false);
  useEffect(() => {
    if (!selected && returning.current) {
      heading.current?.focus();
      returning.current = false;
    }
  }, [selected]);
  const onDirty = useCallback(
    (value: boolean) => {
      setDirty(value);
      reportDirty(value);
    },
    [reportDirty],
  );
  const onBusy = useCallback(
    (value: boolean) => {
      setBusy(value);
      reportBusy(value);
    },
    [reportBusy],
  );
  return selected ? (
    <RecipeDetail
      {...props}
      key={selected}
      id={selected}
      onDirty={onDirty}
      onBusy={onBusy}
      onBack={() => {
        if (
          !busy &&
          (!dirty || window.confirm("Discard your unsaved recipe feedback?"))
        ) {
          onDirty(false);
          returning.current = true;
          setSelected(null);
        }
      }}
    />
  ) : (
    <div className="recipe-history">
      <header className="history-heading">
        <h1 className="kitchen-title" ref={heading} tabIndex={-1}>
          Recipe History
        </h1>
      </header>
      <div className="history-collections">
        <HistoryList {...props} pending onOpen={setSelected} />
        <HistoryList {...props} pending={false} onOpen={setSelected} />
      </div>
    </div>
  );
}
