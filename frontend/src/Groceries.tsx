import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  changeGroceries,
  getGroceries,
  getSession,
  groceryUnits,
  type Grocery,
  type GroceryChange,
  type GroceryUnit,
  type Inventory,
} from "./api";
import type { Session } from "./profile";
import { KitchenIcon } from "./KitchenArt";
import { CartoonButton } from "@/components/ui/cartoon-button";

type Props = {
  session: Session;
  onExpired: () => void;
  onBusy: (busy: boolean) => void;
  onSession: (session: Session) => void;
};

function quantity(value: string): number | null {
  return value.trim() === "" ? null : Number(value);
}

function QuantityInput({
  id,
  value,
  onEdit,
  readOnly,
  error,
}: {
  id: string;
  value?: number | null;
  onEdit?: (input: HTMLInputElement) => void;
  readOnly?: boolean;
  error?: string;
}) {
  const [draft, setDraft] = useState({
    saved: value,
    text: String(value ?? ""),
  });
  if (draft.saved !== value)
    setDraft({ saved: value, text: String(value ?? "") });
  // Whole-unit arrows and typed fractions need separate precision validation.
  return (
    <input
      id={id}
      name="quantity"
      type="number"
      min="0"
      aria-valuemin={0.001}
      max="1000000"
      step="any"
      {...(onEdit
        ? {
            value: draft.text,
            onChange: (event: FormEvent<HTMLInputElement>) =>
              setDraft({ saved: value, text: event.currentTarget.value }),
          }
        : { defaultValue: value ?? "" })}
      readOnly={readOnly}
      aria-invalid={error ? true : undefined}
      aria-describedby={error ? `${id}-error` : undefined}
      placeholder="Unknown"
      onInput={(event) => {
        const input = event.currentTarget;
        const amount = input.valueAsNumber;
        input.setCustomValidity(
          input.value !== "" &&
            (amount <= 0 || Math.round(amount * 1000) / 1000 !== amount)
            ? "Enter a positive quantity with up to 3 decimal places."
            : "",
        );
        onEdit?.(input);
      }}
    />
  );
}

export default function Groceries({
  session,
  onExpired,
  onBusy,
  onSession,
}: Props) {
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [notice, setNotice] = useState("");
  const [invalidName, setInvalidName] = useState(false);
  const [removing, setRemoving] = useState<Grocery | null>(null);
  const [quantityPending, setQuantityPending] = useState(false);
  const [quantityErrors, setQuantityErrors] = useState<Record<string, string>>(
    {},
  );
  const confirmedInventory = useRef<Inventory | null>(null);
  const quantityChanges = useRef(new Map<string, number | null>());
  const quantityTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lock = useRef(false);
  const addForm = useRef<HTMLFormElement>(null);
  const removalTrigger = useRef<HTMLElement | null>(null);
  const focusEntry = useRef(false);
  const expired = useRef(onExpired);
  useEffect(() => {
    expired.current = onExpired;
  }, [onExpired]);

  useEffect(() => {
    const controller = new AbortController();
    const pendingQuantityChanges = quantityChanges.current;
    void getGroceries(controller.signal)
      .then((saved) => {
        if (!controller.signal.aborted) {
          confirmedInventory.current = saved;
          setInventory(saved);
          setLoading(false);
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401)
          expired.current();
        else {
          setFailed(true);
          setLoading(false);
        }
      });
    return () => {
      controller.abort();
      if (quantityTimer.current !== null) clearTimeout(quantityTimer.current);
      pendingQuantityChanges.clear();
    };
  }, []);

  useEffect(() => {
    if (busy || !focusEntry.current) return;
    focusEntry.current = false;
    if (!failed)
      addForm.current
        ?.querySelector<HTMLInputElement>("#grocery-name")
        ?.focus();
  }, [busy, failed]);

  function requestRemoval(row: Grocery) {
    removalTrigger.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setRemoving(row);
  }

  function working(value: boolean) {
    lock.current = value;
    setBusy(value);
    onBusy(value || quantityChanges.current.size > 0);
  }

  function failure(error: unknown) {
    if (quantityTimer.current !== null) clearTimeout(quantityTimer.current);
    quantityChanges.current.clear();
    setQuantityPending(false);
    setQuantityErrors({});
    if (error instanceof ApiError && error.status === 401) {
      onExpired();
      return;
    }
    setFailed(true);
    setNotice(
      error instanceof ApiError && error.status === 412
        ? "Your groceries changed elsewhere. Retry to load the latest quantities before making another change."
        : error instanceof ApiError && error.code === "duplicate_grocery"
          ? "This ingredient and unit already exist. Retry to review your saved list; quantities were not merged."
          : error instanceof ApiError && error.code === "grocery_limit_reached"
            ? "Your list is limited to 200 ingredients. Retry to review it and remove an item before adding another."
            : error instanceof ApiError && error.status === 422
              ? "Check the name, quantity and unit. Retry to review the saved list before trying again."
              : "The update could not be confirmed. Retry to reload saved groceries before deciding whether to submit again.",
    );
  }

  async function reload() {
    if (lock.current) return;
    working(true);
    setRemoving(null);
    try {
      const current = await getSession();
      if (current.userId !== session.userId) {
        onExpired();
        return;
      }
      onSession(current);
      const saved = await getGroceries();
      confirmedInventory.current = saved;
      setInventory(saved);
      setFailed(false);
      setNotice(
        "Latest saved groceries loaded. Review the quantities before submitting another change. No update was replayed.",
      );
    } catch (error) {
      failure(error);
    } finally {
      setLoading(false);
      working(false);
    }
  }

  async function save(change: GroceryChange) {
    const current = confirmedInventory.current;
    if (lock.current || failed || !current) return false;
    working(true);
    setNotice("");
    setInvalidName(false);
    focusEntry.current = change.method === "DELETE";
    setRemoving(null);
    try {
      await changeGroceries(session, current, change);
      // Re-read the collection: the response row alone cannot confirm other rows.
      const saved = await getGroceries();
      confirmedInventory.current = saved;
      setInventory(saved);
      if (change.method === "POST") addForm.current?.reset();
      setNotice("Groceries saved.");
      return true;
    } catch (error) {
      failure(error);
      return false;
    } finally {
      working(false);
    }
  }

  async function flushQuantities() {
    if (lock.current) return;
    if (quantityTimer.current !== null) clearTimeout(quantityTimer.current);
    quantityTimer.current = null;
    for (const [id, value] of quantityChanges.current) {
      quantityChanges.current.delete(id);
      if (!(await save({ method: "PATCH", id, input: { quantity: value } }))) {
        quantityChanges.current.clear();
        break;
      }
    }
    setQuantityPending(false);
    onBusy(false);
  }

  function editQuantity(row: Grocery, input: HTMLInputElement) {
    if (quantityTimer.current !== null) clearTimeout(quantityTimer.current);
    const valid = input.validity.valid;
    setQuantityErrors((previous) => {
      const next = { ...previous };
      if (valid) delete next[row.groceryId];
      else next[row.groceryId] = input.validationMessage;
      return next;
    });
    quantityChanges.current.delete(row.groceryId);
    const next = quantity(input.value);
    if (valid && next !== row.quantity)
      quantityChanges.current.set(row.groceryId, next);
    const pending = quantityChanges.current.size > 0;
    setQuantityPending(pending);
    setRemoving(null);
    onBusy(lock.current || pending);
    if (pending)
      quantityTimer.current = setTimeout(() => void flushQuantities(), 600);
  }

  function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const name = String(data.get("name") ?? "").trim();
    if (!name || [...name].length > 100 || /[\p{Cc}\p{Cs}]/u.test(name)) {
      setInvalidName(true);
      setNotice(
        "Enter an ingredient name of 1 to 100 characters without control characters.",
      );
      addForm.current
        ?.querySelector<HTMLInputElement>("#grocery-name")
        ?.focus();
      return;
    }
    const unit = String(data.get("unit") ?? "");
    void save({
      method: "POST",
      input: {
        name,
        quantity: quantity(String(data.get("quantity") ?? "")),
        unit: groceryUnits.find((entry) => entry === unit) ?? null,
      },
    });
  }

  function adjust(row: Grocery, step: number) {
    if (row.quantity === null) return;
    const next = Math.round((row.quantity + step) * 1000) / 1000;
    if (next <= 0) requestRemoval(row);
    else
      void save({
        method: "PATCH",
        id: row.groceryId,
        input: { quantity: next },
      });
  }

  return (
    <section aria-label="Grocery inventory">
      <div className="card grocery-card">
        <div className="card-heading">
          <div>
            <p className="eyebrow">01 / THE PREP STATION</p>
            <h2>Your groceries</h2>
          </div>
          <span className="heading-icon">
            <KitchenIcon name="groceries" />
          </span>
        </div>
        {loading && <p role="status">Loading your groceries...</p>}
        {failed && (
          <div className="notice" role="alert">
            <h2>Unable to update your groceries</h2>
            <p>{notice || "Your saved groceries could not be loaded."}</p>
            {inventory && (
              <p>
                Showing last confirmed quantities, which may have changed
                elsewhere.
              </p>
            )}
            <CartoonButton
              className="secondary"
              disabled={busy}
              onClick={() => void reload()}
            >
              Retry
            </CartoonButton>
          </div>
        )}
        {!failed && notice && (
          <p id="grocery-notice" role={invalidName ? "alert" : "status"}>
            {notice}
          </p>
        )}
        {busy && <p role="status">Updating your groceries...</p>}
        {quantityPending && !busy && <p role="status">Saving quantities...</p>}
        {inventory && (
          <>
            <form ref={addForm} onSubmit={add} aria-label="Add grocery">
              <fieldset
                className="form-fields"
                disabled={
                  busy ||
                  failed ||
                  quantityPending ||
                  inventory.items.length >= 200
                }
              >
                <legend className="sr-only">Quick item entry</legend>
                <div className="grocery-entry">
                  <div className="field">
                    <label htmlFor="grocery-name">Ingredient name</label>
                    <input
                      id="grocery-name"
                      name="name"
                      required
                      maxLength={200}
                      autoComplete="off"
                      placeholder="e.g. cherry tomatoes"
                      aria-invalid={invalidName}
                      aria-describedby={
                        invalidName ? "grocery-notice" : undefined
                      }
                      onChange={() => {
                        if (invalidName) {
                          setInvalidName(false);
                          setNotice("");
                        }
                      }}
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="grocery-quantity">
                      Quantity (optional)
                    </label>
                    <QuantityInput id="grocery-quantity" />
                  </div>
                  <div className="field">
                    <label htmlFor="grocery-unit">Unit (optional)</label>
                    <select id="grocery-unit" name="unit" defaultValue="">
                      <option value="">No unit</option>
                      {groceryUnits.map((unit: GroceryUnit) => (
                        <option key={unit}>{unit}</option>
                      ))}
                    </select>
                  </div>
                  <CartoonButton label="Add ingredient" type="submit" />
                </div>
              </fieldset>
            </form>
            {inventory.items.length >= 200 && (
              <p>
                Your list has reached 200 ingredients. Remove an item to add
                another.
              </p>
            )}
            {inventory.items.length === 0 ? (
              <p className="grocery-empty">
                Your grocery list is empty. Add your first ingredient above.
              </p>
            ) : (
              <ul
                className="grocery-list"
                aria-label="Saved groceries"
                aria-busy={busy}
              >
                {inventory.items.map((row) => (
                  <li key={row.groceryId} className="grocery-row">
                    <div className="grocery-row-heading">
                      <div className="grocery-row-copy">
                        <h3>{row.name}</h3>
                        <p className="grocery-amount">
                          {row.quantity === null
                            ? "Quantity unknown"
                            : row.quantity}
                          {row.unit ? ` ${row.unit}` : ""}
                        </p>
                      </div>
                      <CartoonButton
                        className="secondary grocery-delete"
                        disabled={busy || failed || quantityPending}
                        aria-label={`Delete ${row.name}`}
                        onClick={() => requestRemoval(row)}
                      >
                        <KitchenIcon name="trash" />
                      </CartoonButton>
                    </div>
                    <div className="grocery-quantity-editor">
                      <div className="grocery-controls">
                        <CartoonButton
                          className="secondary"
                          disabled={
                            busy ||
                            failed ||
                            quantityPending ||
                            row.quantity === null
                          }
                          aria-label={`Decrease ${row.name}`}
                          onClick={() => adjust(row, -1)}
                        >
                          -1
                        </CartoonButton>
                        <CartoonButton
                          className="secondary"
                          disabled={
                            busy ||
                            failed ||
                            quantityPending ||
                            row.quantity === null ||
                            row.quantity > 999999
                          }
                          aria-label={`Increase ${row.name}`}
                          onClick={() => adjust(row, 1)}
                        >
                          +1
                        </CartoonButton>
                      </div>
                      <form
                        key={String(failed)}
                        aria-label={`Edit ${row.name} quantity`}
                        onSubmit={(event) => {
                          event.preventDefault();
                          void flushQuantities();
                        }}
                      >
                        <fieldset
                          className="form-fields grocery-edit"
                          disabled={failed}
                          aria-busy={busy}
                        >
                          <div className="field">
                            <label htmlFor={`quantity-${row.groceryId}`}>
                              Quantity for {row.name}
                            </label>
                            <QuantityInput
                              id={`quantity-${row.groceryId}`}
                              value={row.quantity}
                              onEdit={(input) => editQuantity(row, input)}
                              readOnly={busy}
                              error={quantityErrors[row.groceryId]}
                            />
                            {quantityErrors[row.groceryId] && (
                              <p
                                id={`quantity-${row.groceryId}-error`}
                                role="alert"
                              >
                                {quantityErrors[row.groceryId]}
                              </p>
                            )}
                          </div>
                        </fieldset>
                      </form>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
        {removing && (
          <div
            className="notice"
            role="group"
            aria-label="Confirm grocery removal"
          >
            <h2>Remove {removing.name}?</h2>
            <p>
              Zero or negative quantities are not kept. Confirm removal from
              your saved groceries.
            </p>
            <div className="action-buttons">
              <CartoonButton
                autoFocus
                className="secondary"
                onClick={() => {
                  setRemoving(null);
                  removalTrigger.current?.focus();
                }}
              >
                Cancel
              </CartoonButton>
              <CartoonButton
                label="Confirm removal"
                onClick={() =>
                  void save({ method: "DELETE", id: removing.groceryId })
                }
              />
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
