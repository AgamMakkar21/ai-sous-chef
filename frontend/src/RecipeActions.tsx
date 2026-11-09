import { useRef, useState } from "react";
import { ApiError } from "./api";
import type { Session } from "./profile";
import { changeRecipe, type RecipeSnapshot } from "./recipeHistoryApi";

export default function RecipeActions({
  generatedRecipeId,
  session,
  onExpired,
  onBusy,
  disabled,
}: {
  generatedRecipeId: string;
  session: Session;
  onExpired: () => void;
  onBusy: (value: boolean) => void;
  disabled: boolean;
}) {
  const [saved, setSaved] = useState<RecipeSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pendingCook, setPendingCook] = useState<boolean | null>(null);
  const attempt = useRef<{
    cook: boolean;
    saveKey: string;
    cookKey: string;
    current: RecipeSnapshot | null;
  } | null>(null);
  const lock = useRef(false);

  async function save(cook: boolean) {
    if (lock.current || disabled) return;
    lock.current = true;
    setBusy(true);
    onBusy(true);
    setError("");
    const action = attempt.current ?? {
      cook,
      saveKey: crypto.randomUUID(),
      cookKey: crypto.randomUUID(),
      current: saved,
    };
    attempt.current = action;
    setPendingCook(action.cook);
    try {
      action.current ??= await changeRecipe(
        session,
        { kind: "save", generatedRecipeId },
        action.saveKey,
      );
      if (action.cook && action.current.data.state === "planned")
        action.current = await changeRecipe(
          session,
          { kind: "cooked", current: action.current },
          action.cookKey,
        );
      setSaved(action.current);
      attempt.current = null;
      setPendingCook(null);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onExpired();
      else
        setError(
          "The update could not be confirmed. Retry the same action, or review the saved recipe in Recipe History. Nothing will be replayed automatically.",
        );
    } finally {
      lock.current = false;
      setBusy(false);
      onBusy(false);
    }
  }

  return (
    <div>
      <div className="recipe-actions">
        <button
          type="button"
          className="button primary"
          disabled={
            busy ||
            disabled ||
            saved?.data.state === "cooked" ||
            pendingCook === false
          }
          onClick={() => void save(true)}
        >
          {saved?.data.state === "cooked" ? "Marked cooked" : "Cook This"}
        </button>
        <button
          type="button"
          className="button secondary"
          disabled={busy || disabled || !!saved || pendingCook === true}
          onClick={() => void save(false)}
        >
          {saved ? "Saved to Recipe History" : "Save to Cook"}
        </button>
      </div>
      <p>
        Save to Cook keeps this planned. Cook This confirms you cooked it and
        makes it ready to rate.
      </p>
      {busy && <p role="status">Saving your recipe...</p>}
      {saved && (
        <p role="status">
          {saved.data.state === "cooked"
            ? "Recipe marked cooked. You can rate it in Recipe History."
            : "Recipe saved to cook later. It has not been marked cooked."}
        </p>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
