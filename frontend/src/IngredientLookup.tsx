import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, getSession } from "./api";
import {
  lookupNutrition,
  searchNutrition,
  type FoodSearch,
  type FoodCandidate,
  type IngredientNutrition,
} from "./nutrition";
import type { Session } from "./profile";

type Props = {
  session: Session;
  onExpired: () => void;
  onSession: (session: Session) => void;
};
type Result = IngredientNutrition & { name: string };

export default function IngredientLookup({
  session,
  onExpired,
  onSession,
}: Props) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [candidates, setCandidates] = useState<FoodSearch | null>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);

  async function lookup(candidate?: FoodCandidate) {
    if (active.current) return;
    setResult(null);
    setError("");
    const ingredient = name.trim();
    if (
      !ingredient ||
      [...ingredient].length > 100 ||
      /[%_\p{Cc}\p{Cs}]/u.test(ingredient)
    ) {
      setError(
        "Enter an ingredient name without wildcards or control characters.",
      );
      return;
    }
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    try {
      const nutrition = await lookupNutrition(
        ingredient,
        session,
        controller.signal,
        candidate?.fdc_id,
      );
      if (nutrition.status === "unavailable" && !candidate) {
        const matches = await searchNutrition(
          ingredient,
          session,
          controller.signal,
        );
        if (!controller.signal.aborted) setCandidates(matches);
      } else if (!controller.signal.aborted) {
        setCandidates(null);
      }
      if (!controller.signal.aborted)
        setResult({ ...nutrition, name: candidate?.long_desc ?? ingredient });
    } catch (failure) {
      if (controller.signal.aborted) return;
      if (failure instanceof ApiError && failure.status === 401) {
        onExpired();
        return;
      }
      if (failure instanceof ApiError && failure.code === "csrf_failed") {
        try {
          const renewed = await getSession(controller.signal);
          if (controller.signal.aborted) return;
          if (renewed.userId !== session.userId) {
            onExpired();
            return;
          }
          onSession(renewed);
          setError(
            "Your security token changed. It has been renewed; press Look up calories again. No lookup was replayed.",
          );
        } catch (renewalError) {
          if (controller.signal.aborted) return;
          if (renewalError instanceof ApiError && renewalError.status === 401)
            onExpired();
          else
            setError(
              "The security session could not be renewed. Refresh this page before trying again.",
            );
        }
        return;
      }
      setError(
        failure instanceof ApiError && failure.status === 422
          ? "Check the ingredient name and try again."
          : failure instanceof ApiError && failure.status === 504
            ? "The nutrition lookup timed out. Try again."
            : failure instanceof ApiError && failure.status === 429
              ? "USDA API quota reached. Try again later."
              : "Nutrition could not be retrieved. Try again; no calorie estimate was invented.",
      );
    } finally {
      if (!controller.signal.aborted) {
        active.current = null;
        setBusy(false);
      }
    }
  }

  return (
    <section
      className="card ingredient-lookup"
      aria-labelledby="nutrition-heading"
    >
      <h2 id="nutrition-heading">Ingredient calories</h2>
      <p id="nutrition-help">
        Look up USDA calories per 100 g. Search an ingredient, such as chicken,
        then choose its cut and food form from USDA matches. This reads
        nutrition through MCP; it does not change your groceries or call AI.
      </p>
      <form
        onSubmit={(event: FormEvent<HTMLFormElement>) => {
          event.preventDefault();
          setCandidates(null);
          void lookup();
        }}
        aria-label="Look up ingredient calories"
      >
        <div className="ingredient-lookup-form">
          <div className="field">
            <label htmlFor="nutrition-name">USDA ingredient name</label>
            <input
              id="nutrition-name"
              value={name}
              onChange={(event) => {
                setName(event.target.value);
                setResult(null);
                setError("");
                setCandidates(null);
              }}
              required
              maxLength={100}
              placeholder="Carrots, raw"
              autoComplete="off"
              aria-describedby="nutrition-help"
              disabled={busy}
            />
          </div>
          <button className="button primary" type="submit" disabled={busy}>
            {busy ? "Looking up..." : "Look up calories"}
          </button>
        </div>
      </form>
      {busy && <p role="status">Reading USDA nutrition through MCP...</p>}
      {error && <p role="alert">{error}</p>}
      {candidates && (
        <section aria-label="USDA food matches">
          <h3>Choose the USDA food</h3>
          <p>
            Match the cut, skin and preparation. No candidate is selected
            automatically.
          </p>
          {candidates.truncated && (
            <p role="status">
              Showing a limited set of matches (at most 20). Refine your search
              to find a more specific food.
            </p>
          )}
          {!candidates.foods.length && (
            <p>No USDA matches found. Try a shorter ingredient or cut name.</p>
          )}
          <ul className="grocery-list">
            {candidates.foods.map((food) => (
              <li key={food.fdc_id} className="grocery-row">
                <button
                  className="button secondary"
                  disabled={busy}
                  onClick={() => void lookup(food)}
                >
                  {food.long_desc} &middot; FDC {food.fdc_id}
                </button>
                <p>
                  {food.food_group} &middot; {food.data_type}
                </p>
              </li>
            ))}
          </ul>
        </section>
      )}
      {result && (
        <div className="ingredient-nutrition-result" role="status">
          <h3>{result.name}</h3>
          {result.calories === null ? (
            <p>
              {result.status === "unavailable"
                ? "No unambiguous USDA match was found. Choose a specific food below or refine your search."
                : "Calories are unavailable for this ingredient. Missing values are not zero."}
            </p>
          ) : (
            <p className="ingredient-calories">
              <strong>
                {result.calories.toLocaleString(undefined, {
                  maximumFractionDigits: 3,
                })}{" "}
                kcal
              </strong>{" "}
              per 100 g
            </p>
          )}
          {result.source && (
            <p>
              <a href={result.source.reference}>USDA FoodData Central</a>{" "}
              &middot; FDC {result.source.foodId}
              <br />
              Source: {result.source.id} &middot; {result.source.version}
            </p>
          )}
          {result.status === "partial" && result.calories !== null && (
            <p>
              Calories are source-backed; other nutrient data is incomplete.
            </p>
          )}
        </div>
      )}
      <p className="privacy-note">
        Informational estimate, not medical advice.
      </p>
    </section>
  );
}
