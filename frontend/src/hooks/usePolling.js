import { useEffect } from "react";

/**
 * How often each view re-reads the API, in milliseconds.
 *
 * Collected here because they were four magic numbers in three files, and the numbers are a
 * policy rather than a detail of each component: they set how much load a single idle browser
 * tab puts on the backend, and the backend's rate limiter has to be sized around them.
 *
 * `jobsActive` in particular is load-bearing beyond this file. At 1200ms a single tab makes
 * ~50 requests a minute to `/api/jobs`, which is above the default rate-limit budget of 30 —
 * which is why the read routes are deliberately exempt from throttling, pinned by
 * `tests/test_api_security.py::test_polled_read_routes_are_not_throttled`. Lowering it here
 * without reading that test is how a self-inflicted 429 storm gets shipped.
 */
export const POLL_INTERVALS_MS = {
  /** While a job is queued or processing, or the watch folder is on: progress must feel live. */
  jobsActive: 1200,
  /** Nothing is running, but jobs are still being tracked. Slow enough to be nearly free. */
  jobsIdle: 4000,
  /** Publish attempts move through states without user action, so history needs to keep up. */
  history: 3000,
  /** Disk usage changes slowly, and reading it stats the filesystem. */
  storage: 15000,
};

/**
 * Call `callback` immediately, then every `intervalMs`, and stop on unmount.
 *
 * Pass a falsy `intervalMs` to poll not at all — **including the initial call**. That is the
 * behaviour App relies on: with no tracked jobs and the watch folder off there is nothing to
 * ask about, so it should make no requests rather than one.
 *
 * `callback` must be stable (wrap it in `useCallback`), or the interval is torn down and
 * recreated on every render and the effective period becomes "every render" — which looks like
 * it works, because the data does arrive.
 */
export function usePolling(callback, intervalMs) {
  useEffect(() => {
    if (!intervalMs) return undefined;
    callback();
    const id = setInterval(callback, intervalMs);
    return () => clearInterval(id);
  }, [callback, intervalMs]);
}
