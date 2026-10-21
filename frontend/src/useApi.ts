// =============================================================================
// useApi.ts: A CUSTOM REACT "HOOK" FOR LOADING DATA
// -----------------------------------------------------------------------------
// Every page needs the same three things: the data, an error message, and "still
// loading?". Instead of repeating that in each page, this hook does it once:
//     const { data, error, loading, reload } = useApi<LeaderRow[]>("/api/leaderboard");
// Optional auto-refresh: useApi(path, 5000) re-fetches every 5 seconds (the System tab
// uses this, so the queue counts update live). A "hook" is just a function whose name
// starts with "use" and that uses other hooks (useState, useEffect) inside it.
// =============================================================================
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

/** Fetch `path`, optionally re-fetching every `refreshMs`. */
export function useApi<T>(path: string, refreshMs?: number, admin = false) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // useCallback keeps the SAME function object between re-draws unless path/admin change.
  // That matters because `load` is a dependency of the effect below; a brand-new function
  // on every draw would restart the effect (and the timer) every time.
  const load = useCallback(async () => {
    try {
      setData(await api<T>(path, { admin }));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);          // runs whether it succeeded or failed
    }
  }, [path, admin]);

  // Load now, then (optionally) every refreshMs. The cleanup stops the timer when the
  // component disappears (e.g. you switch tabs), so we don't keep fetching in the background.
  useEffect(() => {
    setLoading(true);
    load();
    if (!refreshMs) return;
    const id = setInterval(load, refreshMs);
    return () => clearInterval(id);
  }, [load, refreshMs]);

  // `reload` lets a page refresh on demand (e.g. after approving a review item).
  return { data, error, loading, reload: load };
}
