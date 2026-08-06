import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POLL_INTERVALS_MS, usePolling } from "./usePolling.js";

describe("usePolling", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("calls back immediately, before the first interval elapses", () => {
    // Otherwise every view would render empty for a full period on mount, which for storage
    // would be fifteen seconds of "Loading storage…".
    const load = vi.fn();
    renderHook(() => usePolling(load, 1000));
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("calls back once per interval", () => {
    const load = vi.fn();
    renderHook(() => usePolling(load, 1000));
    vi.advanceTimersByTime(3000);
    expect(load).toHaveBeenCalledTimes(4); // the immediate call, plus three ticks
  });

  it("makes no call at all when the interval is falsy", () => {
    // App relies on this: with no tracked jobs and the watch folder off there is nothing to ask
    // about, so it should be silent rather than issue one request.
    const load = vi.fn();
    renderHook(() => usePolling(load, null));
    vi.advanceTimersByTime(10000);
    expect(load).not.toHaveBeenCalled();
  });

  it("stops on unmount", () => {
    // The leak this prevents is not hypothetical: a timer that outlives its component keeps
    // calling setState on it, which React reports as an update on an unmounted component and
    // which keeps hitting the API for as long as the tab is open.
    const load = vi.fn();
    const { unmount } = renderHook(() => usePolling(load, 1000));
    vi.advanceTimersByTime(1000);
    expect(load).toHaveBeenCalledTimes(2);
    unmount();
    vi.advanceTimersByTime(10000);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("stops when the interval becomes falsy", () => {
    const load = vi.fn();
    const { rerender } = renderHook(({ ms }) => usePolling(load, ms), {
      initialProps: { ms: 1000 },
    });
    vi.advanceTimersByTime(1000);
    const before = load.mock.calls.length;
    rerender({ ms: null });
    vi.advanceTimersByTime(10000);
    expect(load).toHaveBeenCalledTimes(before);
  });

  it("adopts a new period without dropping a beat", () => {
    // App switches between jobsActive and jobsIdle while jobs finish, so this transition
    // happens in normal use rather than only at mount.
    const load = vi.fn();
    const { rerender } = renderHook(({ ms }) => usePolling(load, ms), {
      initialProps: { ms: 4000 },
    });
    load.mockClear();
    rerender({ ms: 1000 });
    // Re-running the effect calls back immediately again, then settles into the new period.
    expect(load).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(2000);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("runs one timer, not one per render", () => {
    const load = vi.fn();
    const { rerender } = renderHook(() => usePolling(load, 1000));
    rerender();
    rerender();
    load.mockClear();
    vi.advanceTimersByTime(1000);
    // Three renders with a stable callback and interval must still tick once.
    expect(load).toHaveBeenCalledTimes(1);
  });
});

describe("POLL_INTERVALS_MS", () => {
  it("polls faster while work is in flight than when idle", () => {
    expect(POLL_INTERVALS_MS.jobsActive).toBeLessThan(POLL_INTERVALS_MS.jobsIdle);
  });

  it("keeps jobsActive at 1200ms, which the backend's rate-limit exemption is sized around", () => {
    // ~50 requests/minute per tab, above the default budget of 30. The read routes are exempt
    // from throttling *because* of this number, pinned by
    // tests/test_api_security.py::test_polled_read_routes_are_not_throttled. Changing it here
    // without reading that test is how a self-inflicted 429 storm ships.
    expect(POLL_INTERVALS_MS.jobsActive).toBe(1200);
  });

  it("orders the periods by how much a stale value costs, not by how cheap the call is", () => {
    // The order is deliberate and is not simply "cheap things often":
    //
    //   jobsActive 1200  progress bars have to move
    //   history    3000  publish attempts change state with no user action, so a stale row is
    //                    actively misleading -- faster than *idle* jobs, which change slowly
    //   jobsIdle   4000  nothing is running; this only catches an externally-added job
    //   storage   15000  every poll stats the filesystem, and disk usage drifts slowly
    expect(POLL_INTERVALS_MS.jobsActive).toBeLessThan(POLL_INTERVALS_MS.history);
    expect(POLL_INTERVALS_MS.history).toBeLessThan(POLL_INTERVALS_MS.jobsIdle);
    expect(POLL_INTERVALS_MS.jobsIdle).toBeLessThan(POLL_INTERVALS_MS.storage);
  });
});
