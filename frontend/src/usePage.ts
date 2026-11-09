import { useCallback, useEffect, useState } from "react";
import { ApiError } from "./api";
import type { Page } from "./conversations";

type Loader<T> = (
  cursor: string | null,
  signal: AbortSignal,
) => Promise<Page<T>>;
type LoadState<T> =
  | { kind: "loading" }
  | { kind: "failed"; status: number }
  | { kind: "ready"; page: Page<T> };

export default function usePage<T>(load: Loader<T>, onExpired: () => void) {
  const [state, setState] = useState<LoadState<T>>({ kind: "loading" });
  const [navigation, setNavigation] = useState<{
    cursors: (string | null)[];
    index: number;
    revision: number;
  }>({ cursors: [null], index: 0, revision: 0 });
  const cursor = navigation.cursors[navigation.index] ?? null;

  useEffect(() => {
    const controller = new AbortController();
    async function read() {
      try {
        const page = await load(cursor, controller.signal);
        if (controller.signal.aborted) return;
        if (
          page.nextCursor !== null &&
          navigation.cursors
            .slice(0, navigation.index + 1)
            .includes(page.nextCursor)
        )
          throw new ApiError(502, "invalid_response");
        setState({ kind: "ready", page });
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) onExpired();
        else
          setState({
            kind: "failed",
            status: error instanceof ApiError ? error.status : 0,
          });
      }
    }
    void read();
    return () => controller.abort();
  }, [load, cursor, navigation, onExpired]);

  function move(forward: boolean) {
    if (state.kind !== "ready") return;
    const next = state.page.nextCursor;
    if (forward && next !== null) {
      setState({ kind: "loading" });
      setNavigation((current) => ({
        ...current,
        cursors: [...current.cursors.slice(0, current.index + 1), next],
        index: current.index + 1,
      }));
    } else if (!forward && navigation.index > 0) {
      setState({ kind: "loading" });
      setNavigation((current) => ({ ...current, index: current.index - 1 }));
    }
  }

  const refresh = useCallback((restart = false) => {
    setState({ kind: "loading" });
    setNavigation((current) => ({
      cursors: restart ? [null] : current.cursors,
      index: restart ? 0 : current.index,
      revision: current.revision + 1,
    }));
  }, []);

  return { state, index: navigation.index, move, refresh };
}
