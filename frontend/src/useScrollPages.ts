import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Page } from "./conversations";

type State<T> =
  | { kind: "loading" }
  | { kind: "failed"; status: number }
  | { kind: "ready"; page: Page<T> };

export default function useScrollPages<T>(
  load: (cursor: string | null, signal: AbortSignal) => Promise<Page<T>>,
  onExpired: () => void,
) {
  const [state, setState] = useState<State<T>>({ kind: "loading" });
  const [revision, setRevision] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<number | null>(null);
  const current = useRef<Page<T> | null>(null);
  const lifetime = useRef<AbortController | null>(null);
  const busy = useRef(false);
  const visited = useRef(new Set<string | null>());

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    current.current = null;
    visited.current = new Set([null]);
    busy.current = true;
    void load(null, controller.signal).then(
      (page) => {
        if (controller.signal.aborted) return;
        current.current = page;
        busy.current = false;
        setState({ kind: "ready", page });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        busy.current = false;
        if (error instanceof ApiError && error.status === 401) onExpired();
        else
          setState({
            kind: "failed",
            status: error instanceof ApiError ? error.status : 0,
          });
      },
    );
    return () => controller.abort();
  }, [load, onExpired, revision]);

  const refresh = useCallback(() => {
    lifetime.current?.abort();
    setState({ kind: "loading" });
    setLoadingMore(false);
    setMoreError(null);
    setRevision((value) => value + 1);
  }, []);

  async function more(retry = false) {
    const controller = lifetime.current;
    const previous = current.current;
    const cursor = previous?.nextCursor;
    if (
      !controller ||
      controller.signal.aborted ||
      !previous ||
      !cursor ||
      busy.current ||
      (moreError !== null && !retry)
    )
      return;
    busy.current = true;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await load(cursor, controller.signal);
      if (controller.signal.aborted) return;
      if (
        page.nextCursor !== null &&
        (page.nextCursor === cursor || visited.current.has(page.nextCursor))
      )
        throw new ApiError(502, "invalid_response");
      visited.current.add(cursor);
      const merged = {
        items: [...previous.items, ...page.items],
        nextCursor: page.nextCursor,
      };
      current.current = merged;
      setState({ kind: "ready", page: merged });
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) onExpired();
      else setMoreError(error instanceof ApiError ? error.status : 0);
    } finally {
      if (!controller.signal.aborted) {
        busy.current = false;
        setLoadingMore(false);
      }
    }
  }

  return { state, refresh, more, loadingMore, moreError };
}
