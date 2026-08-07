import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import StorageSettings from "./StorageSettings.jsx";
import { api } from "../api.js";

/**
 * This panel is the only thing in the app that deletes finished work, and it does so in two ways: a
 * retention window that removes clips on a timer nobody watches, and a cleanup button that removes
 * them now. Both are one interaction away from each other, so what has to be right is the *value*
 * each control sends — `retention_days` arrives from a `<select>` as the string "30", and a string
 * where the API expects an integer is either rejected or, worse, coerced somewhere downstream into
 * a window nobody chose.
 *
 * The polling is the other half. The component refreshes disk usage every 15 seconds and must stop
 * when it unmounts: the settings section is opened and closed repeatedly, and an interval left
 * behind on each open accumulates into a request loop that never ends and never shows itself,
 * because the failure of a background poll is deliberately swallowed.
 */

const STATE = {
  backend: "local",
  retention_choices: [0, 7, 30, 90],
  usage: {
    used_bytes: 3 * 1024 * 1024 * 1024,
    total_bytes: 10 * 1024 * 1024 * 1024,
    free_bytes: 7 * 1024 * 1024 * 1024,
    used_percent: 30,
    low_space: false,
    free_gb: 7,
    areas: { clips: 2 * 1024 * 1024 * 1024, uploads: 1024 * 1024 * 1024, temp: 512 * 1024 },
  },
  settings: {
    retention_days: 30,
    auto_delete_temp: true,
    delete_local_after_publish: false,
  },
};

const state = (overrides = {}) => ({
  ...STATE,
  ...overrides,
  usage: { ...STATE.usage, ...(overrides.usage || {}) },
  settings: { ...STATE.settings, ...(overrides.settings || {}) },
});

const mockStorage = (value = state()) => vi.spyOn(api, "storage").mockResolvedValue(value);

/** Render and wait for the first payload to land, so tests do not assert on the placeholder. */
const setup = async (value = state()) => {
  const storage = mockStorage(value);
  const utils = render(<StorageSettings />);
  await screen.findByRole("combobox", { name: /keep clips for/i });
  return { ...utils, storage };
};

beforeEach(() => {
  // shouldAdvanceTime keeps user-event's own timers working while the poll interval is faked.
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("StorageSettings rendering", () => {
  it("shows a placeholder until the first payload arrives", async () => {
    // The whole panel depends on server state, and rendering the controls against `null` would
    // read every field off nothing.
    mockStorage();
    render(<StorageSettings />);
    expect(screen.getByText(/loading storage…/i)).toBeInTheDocument();
    // Then let the first payload land, so the resolving fetch is awaited here rather than after
    // the test has finished — an unawaited state update is a warning, and warnings fail this suite.
    expect(await screen.findByRole("combobox", { name: /keep clips for/i })).toBeInTheDocument();
  });

  it("renders the usage figures in human units, not bytes", async () => {
    await setup();
    expect(screen.getByText(/3\.0 GB \/ 10 GB · 7\.0 GB free/)).toBeInTheDocument();
  });

  it("breaks the usage down by area, so the user knows what to clean", async () => {
    await setup();
    expect(screen.getByText(/Clips 2\.0 GB · Sources 1\.0 GB · Temp 512 KB/)).toBeInTheDocument();
  });

  it("names the storage backend, because retention means different things per backend", async () => {
    await setup(state({ backend: "s3" }));
    expect(screen.getByText("backend: s3")).toBeInTheDocument();
  });

  it("draws the usage bar at the reported percentage", async () => {
    const { container } = await setup(state({ usage: { used_percent: 42 } }));
    const bar = container.querySelector("[style]");
    expect(bar).toHaveStyle({ width: "42%" });
  });

  it("clamps the bar at 100% when the reported percentage is over", async () => {
    // Some filesystems report over 100% used against the non-reserved size. An unclamped width
    // paints the bar outside its track, which looks like a rendering fault rather than a full disk.
    const { container } = await setup(state({ usage: { used_percent: 137 } }));
    expect(container.querySelector("[style]")).toHaveStyle({ width: "100%" });
  });

  it("warns about low space and says how much is left", async () => {
    await setup(state({ usage: { low_space: true, free_gb: 1.5 } }));
    expect(screen.getByText(/low disk space \(1\.5 GB free\)/i)).toBeInTheDocument();
  });

  it("does not warn when there is space", async () => {
    await setup();
    expect(screen.queryByText(/low disk space/i)).not.toBeInTheDocument();
  });

  it("offers the retention windows the server advertises", async () => {
    // The list is the server's, because it is the server that enforces it; a hard-coded list here
    // would offer a window the API rejects.
    await setup();
    const select = screen.getByRole("combobox", { name: /keep clips for/i });
    expect([...select.options].map((option) => option.textContent)).toEqual([
      "Keep forever",
      "7 days",
      "30 days",
      "90 days",
    ]);
  });

  it("falls back to the known windows when the server advertises none", async () => {
    await setup(state({ retention_choices: undefined }));
    const select = screen.getByRole("combobox", { name: /keep clips for/i });
    expect([...select.options].map((option) => option.value)).toEqual([
      "0",
      "7",
      "14",
      "30",
      "60",
      "90",
    ]);
  });

  it("labels an unrecognised window in days rather than showing nothing", async () => {
    await setup(state({ retention_choices: [45] }));
    expect(screen.getByRole("option", { name: "45 days" })).toBeInTheDocument();
  });

  it("says clips are never deleted when retention is off", async () => {
    // Zero is the one value whose consequence is the opposite of the others, so it gets its own
    // sentence instead of "older than 0 days are removed automatically".
    await setup(state({ settings: { retention_days: 0 } }));
    expect(screen.getByText(/never auto-deleted/i)).toBeInTheDocument();
  });

  it("spells out what a retention window removes, and what it never touches", async () => {
    await setup();
    expect(screen.getByText(/older than 30 days are removed automatically/i)).toBeInTheDocument();
    expect(screen.getByText(/source video is never auto-deleted/i)).toBeInTheDocument();
  });

  it("reflects the current toggle state from the server, not from a default", async () => {
    await setup();
    expect(screen.getByRole("checkbox", { name: /auto-delete temp files/i })).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: /delete local clip copy after publishing/i })
    ).not.toBeChecked();
  });
});

describe("StorageSettings updates", () => {
  it("sends the retention window as a number, not the select's string", async () => {
    const update = vi.spyOn(api, "updateStorageSettings").mockResolvedValue(state());
    await setup();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /keep clips for/i }), "7");
    await waitFor(() => expect(update).toHaveBeenCalledWith({ retention_days: 7 }));
  });

  it("sends only the field that changed", async () => {
    // The endpoint is a patch. Posting the whole settings object would re-assert values the user
    // did not touch, overwriting anything changed elsewhere since this panel last loaded.
    const update = vi.spyOn(api, "updateStorageSettings").mockResolvedValue(state());
    await setup();
    await userEvent.click(screen.getByRole("checkbox", { name: /auto-delete temp files/i }));
    await waitFor(() => expect(update).toHaveBeenCalledWith({ auto_delete_temp: false }));
  });

  it("sends the delete-after-publish flag as a boolean", async () => {
    const update = vi.spyOn(api, "updateStorageSettings").mockResolvedValue(state());
    await setup();
    await userEvent.click(
      screen.getByRole("checkbox", { name: /delete local clip copy after publishing/i })
    );
    await waitFor(() => expect(update).toHaveBeenCalledWith({ delete_local_after_publish: true }));
  });

  it("adopts the server's answer rather than assuming the change took", async () => {
    // The API may normalise the value; showing the request instead of the response would leave the
    // panel claiming a window the server did not accept.
    vi.spyOn(api, "updateStorageSettings").mockResolvedValue(
      state({ settings: { retention_days: 90 } })
    );
    await setup();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /keep clips for/i }), "7");
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: /keep clips for/i })).toHaveValue("90")
    );
  });

  it("reports a rejected update instead of silently keeping the old value", async () => {
    vi.spyOn(api, "updateStorageSettings").mockRejectedValue(new Error("retention is enforced"));
    await setup();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /keep clips for/i }), "7");
    expect(await screen.findByText("retention is enforced")).toBeInTheDocument();
  });

  it("re-enables the controls after a failure, so the user can try something else", async () => {
    vi.spyOn(api, "updateStorageSettings").mockRejectedValue(new Error("nope"));
    await setup();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /keep clips for/i }), "7");
    await screen.findByText("nope");
    expect(screen.getByRole("combobox", { name: /keep clips for/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /clean up now/i })).toBeEnabled();
  });

  it("locks the controls while an update is in flight", async () => {
    // Two overlapping patches to the same settings file resolve in whatever order they finish.
    let release;
    vi.spyOn(api, "updateStorageSettings").mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      })
    );
    await setup();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /keep clips for/i }), "7");
    expect(screen.getByRole("combobox", { name: /keep clips for/i })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /auto-delete temp files/i })).toBeDisabled();

    release(state());
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: /keep clips for/i })).toBeEnabled()
    );
  });
});

describe("StorageSettings cleanup", () => {
  it("asks for both temp scratch and expired clips", async () => {
    // Those are the two things the button's own label promises; sending one of them would leave
    // the user believing a disk-full problem had been addressed when half of it had not.
    const cleanup = vi.spyOn(api, "cleanupStorage").mockResolvedValue({ expired: { removed: 4 } });
    await setup();
    await userEvent.click(screen.getByRole("button", { name: /clean up now/i }));
    await waitFor(() => expect(cleanup).toHaveBeenCalledWith({ temp: true, expired: true }));
  });

  it("reports how many files were actually removed", async () => {
    vi.spyOn(api, "cleanupStorage").mockResolvedValue({ expired: { removed: 4 } });
    await setup();
    await userEvent.click(screen.getByRole("button", { name: /clean up now/i }));
    expect(await screen.findByText(/cleaned 4 expired file\(s\)/i)).toBeInTheDocument();
  });

  it("reports zero rather than 'undefined' when nothing was expired", async () => {
    // A cleanup that removed nothing is the normal case on a healthy install, and the response
    // omits the count entirely when there was no expiry pass to report.
    vi.spyOn(api, "cleanupStorage").mockResolvedValue({});
    await setup();
    await userEvent.click(screen.getByRole("button", { name: /clean up now/i }));
    expect(await screen.findByText(/cleaned 0 expired file\(s\)/i)).toBeInTheDocument();
  });

  it("refreshes the usage figures after cleaning", async () => {
    // The whole point of the button is the number above it changing; leaving the old figure makes
    // a successful cleanup look like it did nothing.
    vi.spyOn(api, "cleanupStorage").mockResolvedValue({ expired: { removed: 1 } });
    const { storage } = await setup();
    const before = storage.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: /clean up now/i }));
    await waitFor(() => expect(storage.mock.calls.length).toBeGreaterThan(before));
  });

  it("surfaces a failed cleanup", async () => {
    vi.spyOn(api, "cleanupStorage").mockRejectedValue(new Error("permission denied"));
    await setup();
    await userEvent.click(screen.getByRole("button", { name: /clean up now/i }));
    expect(await screen.findByText("permission denied")).toBeInTheDocument();
  });
});

describe("StorageSettings polling", () => {
  it("refreshes the usage figures on a timer", async () => {
    // Disk usage changes while a render is running, and this panel is often the thing being
    // watched during one.
    const { storage } = await setup();
    expect(storage).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(15000);
    await waitFor(() => expect(storage).toHaveBeenCalledTimes(2));
    vi.advanceTimersByTime(15000);
    await waitFor(() => expect(storage).toHaveBeenCalledTimes(3));
  });

  it("stops polling when it unmounts", async () => {
    // The settings section is collapsible, so this component mounts and unmounts repeatedly. An
    // interval that outlives it keeps requesting /api/storage forever — and because a failed poll
    // is deliberately swallowed, nothing would ever show that it was happening.
    const { storage, unmount } = await setup();
    unmount();
    const after = storage.mock.calls.length;
    vi.advanceTimersByTime(120000);
    expect(storage).toHaveBeenCalledTimes(after);
  });

  it("keeps the panel rendered when a poll fails", async () => {
    // A transient error must not blank out settings the user is in the middle of changing.
    const { storage } = await setup();
    storage.mockRejectedValueOnce(new Error("network down"));
    vi.advanceTimersByTime(15000);
    await waitFor(() => expect(storage).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("combobox", { name: /keep clips for/i })).toBeInTheDocument();
  });
});
