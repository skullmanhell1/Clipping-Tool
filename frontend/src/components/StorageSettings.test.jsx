import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api.js";
import StorageSettings from "./StorageSettings.jsx";

const STATE = {
  backend: "local",
  usage: {
    used_bytes: 40 * 1024 ** 3,
    total_bytes: 100 * 1024 ** 3,
    free_bytes: 60 * 1024 ** 3,
    used_percent: 40,
    low_space: false,
    free_gb: 60,
    areas: { clips: 1024 ** 3, uploads: 2 * 1024 ** 3, temp: 512 * 1024 ** 2 },
  },
  settings: { retention_days: 30, auto_delete_temp: true, delete_local_after_publish: false },
  retention_choices: [0, 7, 14, 30, 60, 90],
};

const state = (overrides = {}) => ({
  ...STATE,
  ...overrides,
  usage: { ...STATE.usage, ...(overrides.usage || {}) },
  settings: { ...STATE.settings, ...(overrides.settings || {}) },
});

beforeEach(() => {
  vi.spyOn(api, "storage").mockResolvedValue(state());
  vi.spyOn(api, "updateStorageSettings").mockImplementation(async (change) =>
    state({ settings: { ...STATE.settings, ...change } }),
  );
  vi.spyOn(api, "cleanupStorage").mockResolvedValue({ expired: { removed: 3 } });
});

afterEach(() => vi.restoreAllMocks());

const ready = () => waitFor(() => expect(screen.getByText("Storage")).toBeInTheDocument());

describe("loading", () => {
  it("shows a placeholder until the first response arrives", async () => {
    render(<StorageSettings />);
    expect(screen.getByText("Loading storage…")).toBeInTheDocument();
    await ready();
    expect(screen.queryByText("Loading storage…")).toBeNull();
  });

  it("stays on the placeholder when the very first read fails", async () => {
    // `load` swallows errors so a transient failure does not blank an already-rendered panel —
    // but on first load there is nothing to keep, so the placeholder has to persist rather than
    // crash on `state.usage`.
    api.storage.mockRejectedValue(new Error("offline"));
    render(<StorageSettings />);
    await waitFor(() => expect(api.storage).toHaveBeenCalled());
    expect(screen.getByText("Loading storage…")).toBeInTheDocument();
  });
});

describe("disk usage", () => {
  it("reports used, total and free", async () => {
    render(<StorageSettings />);
    await ready();
    expect(screen.getByText(/40 GB \/ 100 GB · 60 GB free/)).toBeInTheDocument();
  });

  it("breaks usage down by area", async () => {
    render(<StorageSettings />);
    await ready();
    expect(screen.getByText(/Clips 1.0 GB · Sources 2.0 GB · Temp 512 MB/)).toBeInTheDocument();
  });

  it("names the backend, so a misconfigured deployment is visible", async () => {
    render(<StorageSettings />);
    await ready();
    expect(screen.getByText("backend: local")).toBeInTheDocument();
  });

  it("warns, in red, when space is low", async () => {
    api.storage.mockResolvedValue(
      state({ usage: { low_space: true, free_gb: 2, used_percent: 98 } }),
    );
    render(<StorageSettings />);
    await ready();
    expect(screen.getByText(/Low disk space \(2 GB free\)/)).toBeInTheDocument();
    expect(document.querySelector(".bg-rose-500")).toBeTruthy();
  });

  it("shows no warning and a green bar when there is room", async () => {
    render(<StorageSettings />);
    await ready();
    expect(screen.queryByText(/Low disk space/)).toBeNull();
    expect(document.querySelector(".bg-emerald-500")).toBeTruthy();
  });

  it("caps the bar at 100% so an over-report cannot overflow the track", async () => {
    api.storage.mockResolvedValue(state({ usage: { used_percent: 140 } }));
    render(<StorageSettings />);
    await ready();
    expect(document.querySelector('[style*="width"]').style.width).toBe("100%");
  });
});

describe("retention", () => {
  it("offers the choices the backend sent, not a hardcoded list", async () => {
    // The list is server-driven; hardcoding it here would let the UI offer a window the backend
    // rejects.
    api.storage.mockResolvedValue(state({ retention_choices: [0, 30] }));
    render(<StorageSettings />);
    await ready();
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Keep forever",
      "30 days",
    ]);
  });

  it("falls back to a default list when the backend sends none", async () => {
    api.storage.mockResolvedValue(state({ retention_choices: null }));
    render(<StorageSettings />);
    await ready();
    expect(screen.getAllByRole("option")).toHaveLength(6);
  });

  it("labels an unknown window in days rather than showing nothing", async () => {
    api.storage.mockResolvedValue(state({ retention_choices: [45] }));
    render(<StorageSettings />);
    await ready();
    expect(screen.getByRole("option", { name: "45 days" })).toBeInTheDocument();
  });

  it("sends the window as a number, because the select gives a string", async () => {
    render(<StorageSettings />);
    await ready();
    await userEvent.selectOptions(screen.getByRole("combobox"), "60");
    expect(api.updateStorageSettings).toHaveBeenCalledWith({ retention_days: 60 });
  });

  it('explains that "keep forever" disables auto-deletion', async () => {
    api.storage.mockResolvedValue(state({ settings: { retention_days: 0 } }));
    render(<StorageSettings />);
    await ready();
    expect(screen.getByText("Clips are never auto-deleted.")).toBeInTheDocument();
  });

  it("says source video is never auto-deleted, which is the surprising half", async () => {
    render(<StorageSettings />);
    await ready();
    expect(screen.getByText(/Source video is never auto-deleted/)).toBeInTheDocument();
  });
});

describe("the toggles", () => {
  it("reflects the server state rather than local guesses", async () => {
    render(<StorageSettings />);
    await ready();
    expect(screen.getByLabelText(/Auto-delete temp files/)).toBeChecked();
    expect(screen.getByLabelText(/Delete local clip copy/)).not.toBeChecked();
  });

  it("patches only the field that changed", async () => {
    render(<StorageSettings />);
    await ready();
    await userEvent.click(screen.getByLabelText(/Delete local clip copy/));
    expect(api.updateStorageSettings).toHaveBeenCalledWith({ delete_local_after_publish: true });
  });

  it("adopts the state the server returns, not the click", async () => {
    // The server is authoritative: if it refuses a change, the checkbox must go back.
    api.updateStorageSettings.mockResolvedValue(state({ settings: { auto_delete_temp: true } }));
    render(<StorageSettings />);
    await ready();
    await userEvent.click(screen.getByLabelText(/Auto-delete temp files/));
    await waitFor(() => expect(screen.getByLabelText(/Auto-delete temp files/)).toBeChecked());
  });

  it("surfaces an update failure instead of silently reverting", async () => {
    api.updateStorageSettings.mockRejectedValue(new Error("disk is read-only"));
    render(<StorageSettings />);
    await ready();
    await userEvent.click(screen.getByLabelText(/Delete local clip copy/));
    expect(await screen.findByText("disk is read-only")).toBeInTheDocument();
  });
});

describe("manual cleanup", () => {
  it("removes temp and expired files, and reports the count", async () => {
    render(<StorageSettings />);
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "Clean up now" }));
    expect(api.cleanupStorage).toHaveBeenCalledWith({ temp: true, expired: true });
    expect(await screen.findByText(/Cleaned 3 expired file\(s\)/)).toBeInTheDocument();
  });

  it("re-reads usage afterwards, so the bar is not stale", async () => {
    render(<StorageSettings />);
    await ready();
    const before = api.storage.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Clean up now" }));
    await waitFor(() => expect(api.storage.mock.calls.length).toBeGreaterThan(before));
  });

  it("reports zero rather than blank when nothing was expired", async () => {
    api.cleanupStorage.mockResolvedValue({});
    render(<StorageSettings />);
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "Clean up now" }));
    expect(await screen.findByText(/Cleaned 0 expired file\(s\)/)).toBeInTheDocument();
  });

  it("surfaces a cleanup failure", async () => {
    api.cleanupStorage.mockRejectedValue(new Error("permission denied"));
    render(<StorageSettings />);
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "Clean up now" }));
    expect(await screen.findByText("permission denied")).toBeInTheDocument();
  });

  it("clears a previous message when a new action starts", async () => {
    api.cleanupStorage.mockRejectedValueOnce(new Error("permission denied"));
    render(<StorageSettings />);
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "Clean up now" }));
    expect(await screen.findByText("permission denied")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clean up now" }));
    await waitFor(() => expect(screen.queryByText("permission denied")).toBeNull());
  });
});

describe("polling", () => {
  it("re-reads storage on the storage interval", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<StorageSettings />);
    await waitFor(() => expect(api.storage).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(15000);
    expect(api.storage).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("stops polling once unmounted", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { unmount } = render(<StorageSettings />);
    await waitFor(() => expect(api.storage).toHaveBeenCalledTimes(1));
    unmount();
    await vi.advanceTimersByTimeAsync(60000);
    expect(api.storage).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
