import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

/** Fetch `path`, optionally re-fetching every `refreshMs`. */
export function useApi<T>(path: string, refreshMs?: number, admin = false) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      setData(await api<T>(path, { admin }));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [path, admin]);

  useEffect(() => {
    setLoading(true);
    load();
    if (!refreshMs) return;
    const id = setInterval(load, refreshMs);
    return () => clearInterval(id);
  }, [load, refreshMs]);

  return { data, error, loading, reload: load };
}
