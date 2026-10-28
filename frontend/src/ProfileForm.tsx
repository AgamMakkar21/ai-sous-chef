import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, getProfile, getSession, saveProfile } from "./api";
import {
  changedFields,
  fields,
  labels,
  parseDraft,
  toDraft,
  type Draft,
  type Field,
  type FieldErrors,
  type Preferences,
  type Session,
  type Snapshot,
} from "./profile";

type Props = {
  initial: Snapshot;
  session: Session;
  onSaved: (snapshot: Snapshot) => void;
  onSession: (session: Session) => void;
  onExpired: () => void;
  onCancel: () => void;
  onDirty: (dirty: boolean) => void;
  onBusy: (busy: boolean) => void;
};

const guidance: Partial<Record<Field, string>> = {
  allergies:
    "One food or allergen per line. Leave blank only if you have no food allergies.",
  dietaryRestrictions:
    "One strict restriction per line, such as vegetarian, vegan, or pescatarian. Unsupported restrictions will be flagged, not silently accepted.",
  likes: "One food or flavor per line. Optional.",
  dislikes: "One food or flavor per line. Optional.",
  preferredCuisines: "One cuisine per line. Optional.",
  nutritionGoals:
    "One preference per line. Informational recipe personalization only, not medical advice or a treatment plan.",
};

export default function ProfileForm({
  initial,
  session,
  onSaved,
  onSession,
  onExpired,
  onCancel,
  onDirty,
  onBusy,
}: Props) {
  const [baseline] = useState(() =>
    toDraft(initial.profile.explicitPreferences),
  );
  const [draft, setDraft] = useState<Draft>(baseline);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [confirmed, setConfirmed] = useState(false);
  const [confirmationError, setConfirmationError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [blocked, setBlocked] = useState(false);
  const [latest, setLatest] = useState<Snapshot | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const focusTarget = useRef<Field | "safety-confirmation" | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const onboarding = !initial.profile.onboardingCompleted;
  const changed = changedFields(draft, baseline);
  const safetyChanged =
    onboarding ||
    changed.includes("allergies") ||
    changed.includes("dietaryRestrictions");

  useEffect(() => {
    document.getElementById("profile-title")?.focus();
  }, []);
  useEffect(() => onDirty(changed.length > 0), [changed.length, onDirty]);
  useEffect(() => {
    onBusy(busy);
    return () => onBusy(false);
  }, [busy, onBusy]);
  // Server validation arrives while controls are disabled; focus after they are enabled.
  useEffect(() => {
    const target = focusTarget.current;
    if (!busy && target) {
      form.current
        ?.querySelector<HTMLElement>(
          target === "safety-confirmation"
            ? "#safety-confirmation"
            : `[name="${target}"]`,
        )
        ?.focus();
      focusTarget.current = null;
    }
  }, [busy, errors]);

  function edit(field: Field, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
    setErrors((current) => ({ ...current, [field]: undefined }));
    if (field === "allergies" || field === "dietaryRestrictions")
      setConfirmed(false);
  }

  function expire(error: unknown): boolean {
    if (error instanceof ApiError && error.status === 401) {
      onExpired();
      return true;
    }
    return false;
  }

  async function loadLatest() {
    setBusy(true);
    setNotice("");
    try {
      const freshSession = await getSession();
      if (freshSession.userId !== session.userId) {
        onExpired();
        return;
      }
      onSession(freshSession);
      setLatest(await getProfile(freshSession));
      setReviewed(false);
      setNotice(
        "Latest saved profile loaded. Your draft is unchanged. Compare it below before saving again.",
      );
    } catch (error) {
      if (!expire(error))
        setNotice(
          "The saved profile could not be loaded. Your draft is still here. Try loading it again.",
        );
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || (blocked && (!latest || !reviewed))) return;
    const parsed = parseDraft(draft);
    const validation = parsed.errors ?? {};
    const missingConfirmation = safetyChanged && !confirmed;
    setErrors(validation);
    setConfirmationError(missingConfirmation);
    if (!parsed.preferences || missingConfirmation) {
      setNotice("Please review the highlighted fields.");
      const first = fields.find((field) => validation[field]);
      focusTarget.current = first ?? "safety-confirmation";
      return;
    }
    const current = latest ?? initial;
    const payload: Partial<Preferences> = {};
    if (current.profile.onboardingCompleted) {
      for (const field of changed)
        Object.assign(payload, { [field]: parsed.preferences[field] });
      if (!Object.keys(payload).length) {
        setNotice("No profile changes to save.");
        return;
      }
    } else {
      Object.assign(payload, parsed.preferences);
    }
    setBusy(true);
    setNotice("");
    try {
      onSaved(await saveProfile(session, current, payload));
    } catch (error) {
      if (expire(error)) return;
      if (error instanceof ApiError && error.status === 422) {
        setErrors(error.fields);
        setNotice(
          "Your changes were not saved. Review the highlighted fields and try again.",
        );
        const first = fields.find((field) => error.fields[field]);
        focusTarget.current = first ?? "allergies";
      } else if (error instanceof ApiError && error.status === 413) {
        setNotice(
          "Your changes were not saved. Shorten the entries before trying again.",
        );
      } else if (error instanceof ApiError && error.code === "csrf_failed") {
        try {
          const renewed = await getSession();
          if (renewed.userId !== session.userId) onExpired();
          else {
            onSession(renewed);
            setNotice(
              "Your security token changed. It has been renewed; review your draft and press Save again. Nothing was replayed.",
            );
          }
        } catch (renewalError) {
          if (!expire(renewalError)) {
            setBlocked(true);
            setNotice(
              "The security session could not be renewed. Load your saved profile before trying again.",
            );
          }
        }
      } else {
        setBlocked(true);
        setLatest(null);
        setReviewed(false);
        setNotice(
          error instanceof ApiError && error.status === 412
            ? "Your profile changed elsewhere. Your draft is preserved. Load the latest profile and compare before retrying."
            : "The save could not be confirmed. Your draft is preserved. Load the saved profile and compare before retrying.",
        );
      }
    } finally {
      setBusy(false);
    }
  }

  function textField(field: Field) {
    return (
      <div className="field" key={field}>
        <label htmlFor={`profile-${field}`}>{labels[field]}</label>
        <p className="field-help" id={`${field}-help`}>
          {guidance[field]}
        </p>
        <textarea
          id={`profile-${field}`}
          name={field}
          rows={3}
          value={draft[field]}
          onChange={(event) => edit(field, event.target.value)}
          aria-invalid={Boolean(errors[field])}
          aria-describedby={`${field}-help${errors[field] ? ` ${field}-error` : ""}`}
        />
        {errors[field] && (
          <p className="field-error" id={`${field}-error`}>
            {errors[field]}
          </p>
        )}
      </div>
    );
  }

  function selectField(
    field: "spicePreference" | "cookingSkill",
    options: string[],
  ) {
    return (
      <div className="field">
        <label htmlFor={`profile-${field}`}>{labels[field]}</label>
        <select
          id={`profile-${field}`}
          name={field}
          value={draft[field]}
          onChange={(event) => edit(field, event.target.value)}
          aria-invalid={Boolean(errors[field])}
          aria-describedby={errors[field] ? `${field}-error` : undefined}
        >
          <option value="">Choose one</option>
          {options.map((option) => (
            <option key={option} value={option}>
              {option[0]?.toUpperCase()}
              {option.slice(1)}
            </option>
          ))}
        </select>
        {errors[field] && (
          <p className="field-error" id={`${field}-error`}>
            {errors[field]}
          </p>
        )}
      </div>
    );
  }

  return (
    <section className="profile-section" aria-labelledby="profile-title">
      <div className="section-heading">
        <p className="eyebrow">
          {onboarding ? "WELCOME TO YOUR KITCHEN" : "YOUR COOKING PROFILE"}
        </p>
        <h1 id="profile-title" tabIndex={-1}>
          {onboarding
            ? "Let's make this kitchen yours."
            : "A little more you. A better fit."}
        </h1>
        <p>
          {onboarding
            ? "Start with what matters to you. You can update these preferences anytime."
            : "Edit your explicit preferences. Nothing here changes automatically or starts an AI request."}
        </p>
      </div>
      <form ref={form} onSubmit={submit} noValidate>
        {notice && (
          <div className="notice" role="alert">
            {notice}
          </div>
        )}
        <fieldset className="form-fields" disabled={busy}>
          <section className="card">
            <div className="card-heading">
              <span className="step">01</span>
              <div>
                <h2>Food safety comes first</h2>
                <p>Tell us explicitly what your recipes must respect.</p>
              </div>
            </div>
            <div className="field-grid">
              {textField("allergies")}
              {textField("dietaryRestrictions")}
            </div>
            {safetyChanged && (
              <div className="confirmation">
                <label htmlFor="safety-confirmation">
                  <input
                    id="safety-confirmation"
                    type="checkbox"
                    checked={confirmed}
                    onChange={(event) => {
                      setConfirmed(event.target.checked);
                      setConfirmationError(false);
                    }}
                    aria-invalid={confirmationError}
                    aria-describedby={
                      confirmationError ? "confirmation-error" : undefined
                    }
                  />
                  <span>
                    I have reviewed my allergies and strict restrictions. Empty
                    fields mean I have none to declare.
                  </span>
                </label>
                {confirmationError && (
                  <p className="field-error" id="confirmation-error">
                    Confirm your food-safety preferences before continuing.
                  </p>
                )}
              </div>
            )}
          </section>
          <section className="card">
            <div className="card-heading">
              <span className="step">02</span>
              <div>
                <h2>Find your kind of delicious</h2>
                <p>Soft preferences, not extra safety restrictions.</p>
              </div>
            </div>
            <div className="field-grid">
              {textField("likes")}
              {textField("dislikes")}
              {textField("preferredCuisines")}
              {selectField("spicePreference", [
                "none",
                "mild",
                "medium",
                "hot",
              ])}
            </div>
          </section>
          <section className="card">
            <div className="card-heading">
              <span className="step">03</span>
              <div>
                <h2>Fit your everyday routine</h2>
                <p>Make room for your time, experience, and preferences.</p>
              </div>
            </div>
            <div className="field-grid">
              {selectField("cookingSkill", [
                "beginner",
                "intermediate",
                "advanced",
              ])}
              <div className="field">
                <label htmlFor="profile-typicalTimeBudgetMinutes">
                  {labels.typicalTimeBudgetMinutes}
                </label>
                <p className="field-help" id="time-help">
                  A whole number from 5 to 240 minutes.
                </p>
                <input
                  id="profile-typicalTimeBudgetMinutes"
                  name="typicalTimeBudgetMinutes"
                  type="number"
                  min={5}
                  max={240}
                  step={1}
                  value={draft.typicalTimeBudgetMinutes}
                  onChange={(event) =>
                    edit("typicalTimeBudgetMinutes", event.target.value)
                  }
                  aria-invalid={Boolean(errors.typicalTimeBudgetMinutes)}
                  aria-describedby={`time-help${errors.typicalTimeBudgetMinutes ? " time-error" : ""}`}
                />
                {errors.typicalTimeBudgetMinutes && (
                  <p className="field-error" id="time-error">
                    {errors.typicalTimeBudgetMinutes}
                  </p>
                )}
              </div>
              <div className="wide">{textField("nutritionGoals")}</div>
            </div>
          </section>
          {blocked && (
            <section
              className="card reconciliation"
              aria-label="Review before retrying"
            >
              <h2>Check what is saved before retrying</h2>
              <p>
                A failed response does not always mean a write failed. We will
                not repeat the save automatically.
              </p>
              <button
                type="button"
                className="button secondary"
                onClick={() => void loadLatest()}
              >
                Load latest saved profile
              </button>
              {latest && (
                <>
                  <details open>
                    <summary>Latest saved profile</summary>
                    {latest.profile.explicitPreferences ? (
                      <dl>
                        {fields.map((field) => {
                          const value =
                            latest.profile.explicitPreferences?.[field];
                          return (
                            <div key={field}>
                              <dt>{labels[field]}</dt>
                              <dd>
                                {Array.isArray(value)
                                  ? value.join(", ") || "None declared"
                                  : value}
                              </dd>
                            </div>
                          );
                        })}
                      </dl>
                    ) : (
                      <p>Onboarding has not yet been completed.</p>
                    )}
                  </details>
                  <label className="review-check">
                    <input
                      type="checkbox"
                      checked={reviewed}
                      onChange={(event) => setReviewed(event.target.checked)}
                    />
                    <span>
                      I reviewed the saved profile. Apply only my edited fields,
                      without replacing other preferences.
                    </span>
                  </label>
                </>
              )}
            </section>
          )}
          <div className="form-actions">
            <p className="privacy-note">
              Stored in your account. No AI calls or browser analytics.
            </p>
            <div className="action-buttons">
              {!onboarding && (
                <button
                  className="button secondary"
                  type="button"
                  onClick={onCancel}
                >
                  Cancel
                </button>
              )}
              <button
                className="button primary"
                type="submit"
                disabled={
                  busy ||
                  (blocked && (!latest || !reviewed)) ||
                  (!onboarding && !changed.length)
                }
              >
                {busy
                  ? "Working..."
                  : onboarding
                    ? "Save and open my kitchen"
                    : "Save profile"}
              </button>
            </div>
          </div>
        </fieldset>
      </form>
    </section>
  );
}
