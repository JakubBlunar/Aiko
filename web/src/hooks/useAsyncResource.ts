import { type Dispatch, type SetStateAction, useCallback, useEffect, useState } from "react";

export interface AsyncResource<T> {
  /** The most recently loaded value (or ``initialData`` before the first
   * load resolves). */
  data: T;
  /** Direct setter for optimistic in-place updates (e.g. dropping a row
   * after a delete without a full refetch). */
  setData: Dispatch<SetStateAction<T>>;
  /** True while a load is in flight. */
  loading: boolean;
  /** Last error message (``String(err)``), or ``null``. */
  error: string | null;
  /** Manual error setter so action handlers (delete / resolve) can
   * surface their own failures through the same banner. */
  setError: (error: string | null) => void;
  /** Re-run ``loader``. Stable per ``loader`` identity. */
  refresh: () => Promise<void>;
}

/**
 * The fetch skeleton every Memory-tab panel (and several settings tabs)
 * had copy-pasted: ``loading`` / ``error`` state, a ``refresh`` that wraps
 * the loader in ``setLoading(true)`` / try / catch(setError) / finally, and
 * a mount-time + dependency-change effect that calls it.
 *
 * The ``loader`` MUST be stable across renders (wrap it in ``useCallback``)
 * — its identity is the effect's dependency, so an inline loader would
 * refetch every render. Put the panel's filter state in the loader's
 * ``useCallback`` deps and the resource refetches whenever a filter flips,
 * exactly like the hand-rolled version did.
 */
export function useAsyncResource<T>(
  loader: () => Promise<T>,
  initialData: T,
): AsyncResource<T> {
  const [data, setData] = useState<T>(initialData);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await loader());
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [loader]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { data, setData, loading, error, setError, refresh };
}
