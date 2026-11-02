import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError } from "./api";
import { lookupNutrition, type IngredientNutrition } from "./nutrition";
import type { Session } from "./profile";

type Props = { session: Session; onExpired: () => void };
type Result = IngredientNutrition & { name: string };

export default function IngredientLookup({ session, onExpired }: Props) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);

  async function lookup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
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
      );
      if (!controller.signal.aborted)
        setResult({ ...nutrition, name: ingredient });
    } catch (failure) {
      if (controller.signal.aborted) return;
      if (failure instanceof ApiError && failure.status === 401) {
        onExpired();
        return;
      }
      setError(
        failure instanceof ApiError && failure.status === 422
          ? "Check the ingredient name and try again."
          : failure instanceof ApiError && failure.status === 504
            ? "The nutrition lookup timed out. Try again."
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
        Look up USDA calories per 100 g. Use the food's exact description and
        form, such as Carrots, raw. This reads nutrition through MCP; it does
        not change your groceries or call AI.
      </p>
      <form
        onSubmit={(event) => void lookup(event)}
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
      {result && (
        <div className="ingredient-nutrition-result" role="status">
          <h3>{result.name}</h3>
          {result.calories === null ? (
            <p>
              {result.status === "unavailable"
                ? "No unambiguous USDA match was found. Try the exact description and food form."
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
              <a href="https://www.ars.usda.gov/Services/docs.htm?docid=8964">
                USDA Standard Reference
              </a>{" "}
              &middot; NDB {result.source.foodId}
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
