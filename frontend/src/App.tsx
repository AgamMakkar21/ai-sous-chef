import { useEffect, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { ApiError, getProfile, getSession } from "./api";
import ProfileForm from "./ProfileForm";
import ConversationPanel from "./ConversationPanel";
import type { Session, Snapshot } from "./profile";

type State =
  | { kind: "loading" }
  | { kind: "failed" }
  | { kind: "signed-out" }
  | { kind: "ready"; session: Session; snapshot: Snapshot };

const tabs = [
  {
    id: "groceries",
    name: "Groceries",
    title: "Good cooking starts with what you have.",
    text: "Your cooking profile is ready. Grocery lists and inventory controls are coming in the next update.",
    symbol: "01",
  },
  {
    id: "sous-chef",
    name: "Sous Chef",
    title: "A little inspiration for your everyday.",
    text: "Create independent conversations and revisit their message history.",
    symbol: "02",
  },
  {
    id: "recipe-history",
    name: "Recipe History",
    title: "Make room for your next favorite.",
    text: "Saved recipes, cooking history, and feedback will live here when the history screen is available.",
    symbol: "03",
  },
];

function initialTab(): string {
  const hash = window.location.hash.slice(1);
  return tabs.some((tab) => tab.id === hash) ? hash : "groceries";
}

export default function App() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState(initialTab);
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const session = await getSession(controller.signal);
        const snapshot = await getProfile(session, controller.signal);
        if (!controller.signal.aborted)
          setState({ kind: "ready", session, snapshot });
      } catch (error) {
        if (!controller.signal.aborted)
          setState(
            error instanceof ApiError && error.status === 401
              ? { kind: "signed-out" }
              : { kind: "failed" },
          );
      }
    }
    void load();
    return () => controller.abort();
  }, [attempt]);

  useEffect(() => {
    const update = () => setSelected(initialTab());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);

  function expire() {
    setState({ kind: "signed-out" });
    setEditing(false);
    setDirty(false);
    setSaved(false);
  }

  function discard(): boolean {
    return !dirty || window.confirm("Discard your unsaved profile changes?");
  }

  function openProfile() {
    if (editing || busy) return;
    setSaved(false);
    setEditing(true);
    setState({ kind: "loading" });
    setAttempt((value) => value + 1);
  }

  function closeEditor() {
    if (!busy && discard()) {
      setEditing(false);
      setDirty(false);
    }
  }

  function selectTab(value: string) {
    if (
      busy ||
      !tabs.some((tab) => tab.id === value) ||
      (editing && !discard())
    )
      return;
    setSelected(value);
    window.history.replaceState(null, "", `#${value}`);
    setEditing(false);
    setDirty(false);
    setSaved(false);
  }

  const ready = state.kind === "ready" ? state : null;
  const completed = Boolean(ready?.snapshot.profile.onboardingCompleted);
  const showProfile = ready && (!completed || editing);

  return (
    <div className="app-shell">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        Skip to main content
      </a>
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 32 32" fill="none">
              <path
                d="M7 16h18v3a9 9 0 0 1-18 0v-3ZM5 16h22M12 5v5m5-7v7m5-5v5"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <div>
            <span className="brand-name">AI Sous Chef</span>
            <span className="brand-caption">
              A kitchen that feels like you.
            </span>
          </div>
        </div>
        {ready && (
          <div className="header-actions">
            {completed && (
              <button
                className="button secondary"
                disabled={busy}
                onClick={openProfile}
              >
                Cooking profile
              </button>
            )}
            <a
              className="sign-out"
              href="/auth/logout"
              aria-disabled={busy}
              onClick={(event) => {
                if (busy || !discard()) event.preventDefault();
              }}
            >
              Sign out
            </a>
          </div>
        )}
      </header>
      <Tabs.Root
        value={selected}
        onValueChange={selectTab}
        activationMode="automatic"
      >
        <Tabs.List className="tab-list" aria-label="Your kitchen">
          {tabs.map((tab) => (
            <Tabs.Trigger
              className="tab"
              value={tab.id}
              key={tab.id}
              disabled={!completed || busy}
            >
              {tab.name}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        <main id="main-content" tabIndex={-1}>
          {state.kind === "loading" && (
            <section className="state-card card" role="status">
              <span className="eyebrow">GETTING YOUR KITCHEN READY</span>
              <h1>Loading your profile...</h1>
              <p>Checking your session and saved preferences.</p>
            </section>
          )}
          {state.kind === "failed" && (
            <section className="state-card card" role="alert">
              <h1>Your kitchen could not be loaded.</h1>
              <p>
                We could not confirm your session or saved profile. Nothing has
                been replaced with defaults.
              </p>
              <button
                className="button primary"
                onClick={() => {
                  setState({ kind: "loading" });
                  setAttempt((value) => value + 1);
                }}
              >
                Try again
              </button>
            </section>
          )}
          {state.kind === "signed-out" && (
            <section className="state-card card" role="alert">
              <p className="eyebrow">SIGN IN TO CONTINUE</p>
              <h1>Let's get you back to your kitchen.</h1>
              <p>
                Your session is no longer available. For your privacy, displayed
                profile details and unsaved changes have been cleared. No save
                will be replayed automatically.
              </p>
              <a className="button primary" href="/auth/login">
                Sign in
              </a>
            </section>
          )}
          {ready && showProfile && (
            <ProfileForm
              key={ready.snapshot.etag}
              initial={ready.snapshot}
              session={ready.session}
              onExpired={expire}
              onCancel={closeEditor}
              onDirty={setDirty}
              onBusy={setBusy}
              onSession={(session) =>
                setState((current) =>
                  current.kind === "ready" &&
                  current.session.userId === session.userId
                    ? { ...current, session }
                    : current,
                )
              }
              onSaved={(snapshot) => {
                setState((current) =>
                  current.kind === "ready" &&
                  current.session.userId === snapshot.profile.userId
                    ? { ...current, snapshot }
                    : current,
                );
                setEditing(false);
                setDirty(false);
                setSaved(true);
              }}
            />
          )}
          {ready && completed && !editing && (
            <>
              {saved && (
                <div className="success-notice" role="status">
                  Your cooking profile is saved.
                </div>
              )}
              {tabs.map((tab) => (
                <Tabs.Content
                  className="tab-content"
                  value={tab.id}
                  key={tab.id}
                >
                  {tab.id === "sous-chef" ? (
                    <ConversationPanel
                      key={ready.session.userId}
                      session={ready.session}
                      onExpired={expire}
                    />
                  ) : (
                    <>
                      <section className="welcome-panel">
                        <div>
                          <p className="eyebrow">YOUR KITCHEN, YOUR WAY</p>
                          <h1>{tab.title}</h1>
                          <p className="welcome-copy">
                            A little less wondering what to cook. A little more
                            making it your own.
                          </p>
                        </div>
                        <div className="kitchen-art" aria-hidden="true">
                          <span className="art-ring">
                            <svg viewBox="0 0 120 120" fill="none">
                              <path
                                d="M24 55h72v10a36 36 0 0 1-72 0V55ZM16 55h88M43 22v16m18-24v24m18-16v16"
                                stroke="currentColor"
                                strokeWidth="4"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                              />
                            </svg>
                          </span>
                          <span className="art-label">
                            MADE FOR YOUR EVERYDAY
                          </span>
                        </div>
                      </section>
                      <section className="card coming-next">
                        <span className="step">{tab.symbol}</span>
                        <div>
                          <p className="eyebrow">COMING NEXT</p>
                          <h2>{tab.name}</h2>
                          <p>{tab.text}</p>
                          <button className="text-button" onClick={openProfile}>
                            Review your cooking profile{" "}
                            <span aria-hidden="true">&rarr;</span>
                          </button>
                        </div>
                      </section>
                    </>
                  )}
                </Tabs.Content>
              ))}
            </>
          )}
        </main>
      </Tabs.Root>
      <footer className="app-footer">
        <span>Thoughtful cooking. Your preferences, respected.</span>
        <span>Profile updates never make AI calls.</span>
      </footer>
    </div>
  );
}
