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

type Props = {
  session: Session;
  onExpired: () => void;
  onBusy: (busy: boolean) => void;
  onSession: (session: Session) => void;
};

function quantity(value: string): number | null {
  return value.trim() === "" ? null : Number(value);
}

function QuantityInput({ id, value }: { id: string; value?: number | null }) {
  return (
    <input
      id={id}
      name="quantity"
      type="number"
      min="0.001"
      max="1000000"
      step="0.001"
      defaultValue={value ?? ""}
      placeholder="Unknown"
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
  const [removing, setRemoving] = useState<Grocery | null>(null);
  const lock = useRef(false);
  const addForm = useRef<HTMLFormElement>(null);
  const expired = useRef(onExpired);
  useEffect(() => {
    expired.current = onExpired;
  }, [onExpired]);

  useEffect(() => {
    const controller = new AbortController();
    void getGroceries(controller.signal)
      .then((saved) => {
        if (!controller.signal.aborted) {
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
    return () => controller.abort();
  }, []);

  function working(value: boolean) {
    lock.current = value;
    setBusy(value);
    onBusy(value);
  }

  function failure(error: unknown) {
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
      setInventory(await getGroceries());
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
    if (lock.current || failed || !inventory) return;
    working(true);
    setNotice("");
    setRemoving(null);
    try {
      await changeGroceries(session, inventory, change);
      // Re-read the collection: the response row alone cannot confirm other rows.
      setInventory(await getGroceries());
      if (change.method === "POST") addForm.current?.reset();
      setNotice("Groceries saved.");
    } catch (error) {
      failure(error);
    } finally {
      working(false);
    }
  }

  function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const name = String(data.get("name") ?? "").trim();
    if (!name || [...name].length > 100 || /[\p{Cc}\p{Cs}]/u.test(name)) {
      setNotice(
        "Enter an ingredient name of 1 to 100 characters without control characters.",
      );
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
    if (next <= 0) setRemoving(row);
    else
      void save({
        method: "PATCH",
        id: row.groceryId,
        input: { quantity: next },
      });
  }

  return (
    <section aria-label="Grocery inventory">
      <div className="card">
        <h2>Your groceries</h2>
        <p id="grocery-help">
          Add what you have. Quantity and unit are optional; leave quantity
          blank when unknown. Plus and minus change a known quantity by 1 in its
          unit. Nothing here calls AI.
        </p>
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
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => void reload()}
            >
              Retry
            </button>
          </div>
        )}
        {!failed && notice && <p role="status">{notice}</p>}
        {busy && <p role="status">Updating your groceries...</p>}
        {inventory && (
          <>
            <form ref={addForm} onSubmit={add} aria-label="Add grocery">
              <fieldset
                className="form-fields"
                disabled={busy || failed || inventory.items.length >= 200}
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
                  <button className="button primary" type="submit">
                    Add ingredient
                  </button>
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
                    <h3>{row.name}</h3>
                    <p className="grocery-amount">
                      {row.quantity === null
                        ? "Quantity unknown"
                        : row.quantity}
                      {row.unit ? ` ${row.unit}` : ""}
                    </p>
                    <div className="grocery-controls">
                      <button
                        className="button secondary"
                        disabled={busy || failed || row.quantity === null}
                        aria-label={`Decrease ${row.name}`}
                        onClick={() => adjust(row, -1)}
                      >
                        -1
                      </button>
                      <button
                        className="button secondary"
                        disabled={
                          busy ||
                          failed ||
                          row.quantity === null ||
                          row.quantity > 999999
                        }
                        aria-label={`Increase ${row.name}`}
                        onClick={() => adjust(row, 1)}
                      >
                        +1
                      </button>
                      <button
                        className="button secondary"
                        disabled={busy || failed}
                        aria-label={`Delete ${row.name}`}
                        onClick={() => setRemoving(row)}
                      >
                        Delete
                      </button>
                    </div>
                    <form
                      key={`${inventory.etag}-${failed}`}
                      aria-label={`Edit ${row.name} quantity`}
                      onSubmit={(event) => {
                        event.preventDefault();
                        void save({
                          method: "PATCH",
                          id: row.groceryId,
                          input: {
                            quantity: quantity(
                              String(
                                new FormData(event.currentTarget).get(
                                  "quantity",
                                ) ?? "",
                              ),
                            ),
                          },
                        });
                      }}
                    >
                      <fieldset
                        className="form-fields grocery-edit"
                        disabled={busy || failed}
                      >
                        <div className="field">
                          <label htmlFor={`quantity-${row.groceryId}`}>
                            Quantity for {row.name}
                          </label>
                          <QuantityInput
                            id={`quantity-${row.groceryId}`}
                            value={row.quantity}
                          />
                        </div>
                        <button className="button secondary" type="submit">
                          Save quantity
                        </button>
                      </fieldset>
                    </form>
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
              <button
                autoFocus
                className="button secondary"
                onClick={() => setRemoving(null)}
              >
                Cancel
              </button>
              <button
                className="button primary"
                onClick={() =>
                  void save({ method: "DELETE", id: removing.groceryId })
                }
              >
                Confirm removal
              </button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
