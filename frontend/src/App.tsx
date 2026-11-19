import { useCallback, useEffect, useRef, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { ApiError, getProfile, getSession } from "./api";
import ProfileForm from "./ProfileForm";
import Groceries from "./Groceries";
import ConversationPanel from "./ConversationPanel";
import RecipeHistory from "./RecipeHistory";
import { KitchenHero, KitchenIcon, PixelArt } from "./KitchenArt";
import fontLicense from "./assets/Jersey-10-OFL.txt?raw";
import type { Session, Snapshot } from "./profile";
import { CartoonButton, CartoonLink } from "@/components/ui/cartoon-button";

type State =
  | { kind: "loading" }
  | { kind: "failed" }
  | { kind: "signed-out" }
  | { kind: "ready"; session: Session; snapshot: Snapshot };

const tabs = [
  {
    id: "groceries",
    name: "Groceries",
  },
  {
    id: "sous-chef",
    name: "Sous Chef",
  },
  {
    id: "recipe-history",
    name: "Recipe History",
  },
] as const;

function TabLabel({ tab }: { tab: (typeof tabs)[number] }) {
  return (
    <>
      <KitchenIcon name={tab.id} />
      <span>{tab.name}</span>
    </>
  );
}

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
  const [theme, setTheme] = useState(() =>
    document.documentElement.dataset.theme === "light" ? "light" : "dark",
  );
  const leaving = useRef(false);
  const profileButton = useRef<HTMLButtonElement>(null);
  const main = useRef<HTMLElement>(null);
  const returnFocus = useRef<"profile" | "main" | null>(null);

  useEffect(() => {
    const root = document.documentElement;
    const pointer = () => {
      if (root.dataset.input !== "pointer") root.dataset.input = "pointer";
    };
    const keyboard = () => {
      if (root.dataset.input !== "keyboard") root.dataset.input = "keyboard";
    };
    window.addEventListener("pointerdown", pointer, true);
    window.addEventListener("pointermove", pointer, true);
    window.addEventListener("keydown", keyboard, true);
    return () => {
      window.removeEventListener("pointerdown", pointer, true);
      window.removeEventListener("pointermove", pointer, true);
      window.removeEventListener("keydown", keyboard, true);
      delete root.dataset.input;
    };
  }, []);

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
    const update = () => {
      const next = initialTab();
      if (next === selected) return;
      if (
        busy ||
        (!editing && dirty && !window.confirm("Discard your unsaved changes?"))
      ) {
        window.history.replaceState(null, "", `#${selected}`);
        return;
      }
      setSelected(next);
    };
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, [busy, dirty, editing, selected]);

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (!leaving.current) event.preventDefault();
    };
    if (
      busy ||
      (!editing &&
        (selected === "sous-chef" || selected === "recipe-history") &&
        dirty)
    )
      window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, busy, editing, selected]);

  const expire = useCallback(() => {
    setState({ kind: "signed-out" });
    setEditing(false);
    setDirty(false);
    setSaved(false);
    setBusy(false);
  }, []);

  function discard(): boolean {
    return (
      !dirty ||
      window.confirm(
        editing
          ? "Discard your unsaved profile changes?"
          : selected === "recipe-history"
            ? "Discard your unsaved recipe feedback?"
            : "Leave this recipe request? Unsaved input will be discarded; accepted requests are not cancelled.",
      )
    );
  }

  function openProfile() {
    if (editing || busy || !discard()) return;
    setDirty(false);
    setSaved(false);
    setEditing(true);
    setState({ kind: "loading" });
    setAttempt((value) => value + 1);
  }

  function closeEditor() {
    if (!busy && discard()) {
      returnFocus.current = "profile";
      setEditing(false);
      setDirty(false);
    }
  }

  function selectTab(value: string) {
    if (busy || !tabs.some((tab) => tab.id === value) || !discard()) return;
    setSelected(value);
    window.history.replaceState(null, "", `#${value}`);
    setEditing(false);
    setDirty(false);
    setSaved(false);
  }

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    setTheme(next);
    try {
      localStorage.setItem("sous-chef-theme", next);
    } catch {
      // Theme changes still work when browser storage is unavailable.
    }
    const url = new URL(window.location.href);
    if (url.searchParams.has("scoutTheme")) {
      url.searchParams.set("scoutTheme", next);
      window.history.replaceState(null, "", url);
    }
  }

  const ready = state.kind === "ready" ? state : null;
  const completed = Boolean(ready?.snapshot.profile.onboardingCompleted);
  const showProfile = ready && (!completed || editing);

  useEffect(() => {
    if (!completed || editing || busy || !returnFocus.current) return;
    const target =
      returnFocus.current === "profile" ? profileButton.current : main.current;
    target?.focus();
    returnFocus.current = null;
  }, [completed, editing, busy]);

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
            <PixelArt kind="pot" />
          </span>
          <div>
            <span className="brand-name" aria-label="AI Sous Chef">
              sous<span>.</span>chef
            </span>
            <span className="brand-caption">
              YOUR EVERYDAY COOKING COMPANION
            </span>
          </div>
        </div>
        <div className="header-actions">
          {ready && (
            <>
              {completed && (
                <CartoonButton
                  ref={profileButton}
                  className="secondary"
                  disabled={busy}
                  onClick={openProfile}
                  aria-label="Cooking profile"
                >
                  <KitchenIcon name="profile" />
                  <span className="profile-label">Cooking profile</span>
                </CartoonButton>
              )}
              <CartoonLink
                className="secondary sign-out"
                href="/auth/logout"
                aria-disabled={busy}
                onClick={(event) => {
                  if (busy || !discard()) event.preventDefault();
                  else leaving.current = true;
                }}
              >
                Sign out
              </CartoonLink>
            </>
          )}
          <CartoonButton
            className="secondary theme-toggle"
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            onClick={toggleTheme}
          >
            <KitchenIcon name={theme === "dark" ? "sun" : "moon"} />
          </CartoonButton>
        </div>
      </header>
      <Tabs.Root
        className="kitchen-tabs"
        value={ready && completed && !editing ? selected : ""}
        onValueChange={selectTab}
        activationMode="automatic"
      >
        <Tabs.List
          className="tab-list"
          aria-label="Your kitchen"
          data-active={selected}
          data-inactive={!ready || !completed || editing}
        >
          <span className="tab-indicator" aria-hidden="true">
            <span className="tab-active-labels">
              {tabs.map((tab) => (
                <span
                  className="cartoon-label tab-indicator-label"
                  key={tab.id}
                >
                  <TabLabel tab={tab} />
                </span>
              ))}
            </span>
          </span>
          {tabs.map((tab) => (
            <Tabs.Trigger
              asChild
              value={tab.id}
              key={tab.id}
              disabled={!completed || busy}
            >
              <CartoonButton className="tab" hasHighlight={false}>
                <TabLabel tab={tab} />
              </CartoonButton>
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        <main id="main-content" ref={main} tabIndex={-1}>
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
              <CartoonButton
                className="primary"
                onClick={() => {
                  setState({ kind: "loading" });
                  setAttempt((value) => value + 1);
                }}
              >
                Try again
              </CartoonButton>
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
              <CartoonLink className="primary" href="/auth/login">
                Sign in
              </CartoonLink>
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
                returnFocus.current = completed ? "profile" : "main";
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
                      onBusy={setBusy}
                      onDirty={setDirty}
                    />
                  ) : tab.id === "recipe-history" ? (
                    <RecipeHistory
                      key={ready.session.userId}
                      session={ready.session}
                      onExpired={expire}
                      onBusy={setBusy}
                      onDirty={setDirty}
                    />
                  ) : (
                    <>
                      <KitchenHero />
                      <Groceries
                        session={ready.session}
                        onExpired={expire}
                        onBusy={setBusy}
                        onSession={(session) =>
                          setState((current) =>
                            current.kind === "ready" &&
                            current.session.userId === session.userId
                              ? { ...current, session }
                              : current,
                          )
                        }
                      />
                    </>
                  )}
                </Tabs.Content>
              ))}
            </>
          )}
        </main>
      </Tabs.Root>
      <template data-font-license="Jersey 10">{fontLicense}</template>
    </div>
  );
}
