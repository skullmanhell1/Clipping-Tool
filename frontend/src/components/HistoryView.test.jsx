// Tests for the publish-attempt actions in the history table (PB2).
//
// `/api/publish-attempts/{id}/approve` and `/retry` shipped with **zero references anywhere
// in `frontend/src/`**. Three of the five publishers can return `review_required` — Instagram
// and X when the account lacks direct-publish approval, Whop when the upload could not be
// attached to a target — so an attempt in that state stopped permanently, with the only way
// out being a hand-written HTTP request.
//
// The distinction these tests protect is that approve and retry are *different decisions*:
// approve escalates a held submission into a live post, retry re-runs it exactly as it was.
// Conflating them would publish something a user deliberately held back, which is the kind of
// mistake you cannot take back.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import HistoryView from "./HistoryView.jsx";
import { api } from "../api.js";

function attempt(overrides = {}) {
  return {
    id: "att-1",
    platform: "instagram",
    campaign_id: "",
    account_id: "acct-9",
    state: "review_required",
    error: "",
    message: "held for review",
    url: "",
    created_at: 1_700_000_000,
    ...overrides,
  };
}

function mockHistory(attempts) {
  return vi.spyOn(api, "history").mockResolvedValue({
    clips: [],
    publish_attempts: attempts,
  });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("publish attempt actions", () => {
  it("offers approve and retry for an attempt held in review", async () => {
    mockHistory([attempt()]);
    render(<HistoryView />);

    expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("offers retry but not approve for a failed attempt", async () => {
    // A failure is transient trouble, not a withheld decision: there is nothing to approve,
    // and offering it would imply the attempt was waiting on a human when it was not.
    mockHistory([attempt({ state: "failed", error: "token expired" })]);
    render(<HistoryView />);

    expect(await screen.findByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("offers neither for an attempt that already published", async () => {
    // Re-posting a published clip creates a duplicate on someone's real account.
    mockHistory([attempt({ state: "published", url: "https://example.test/p/1" })]);
    render(<HistoryView />);

    await screen.findByText("published");
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("offers neither while an attempt is still in flight", async () => {
    for (const state of ["queued", "uploading", "scheduled"]) {
      mockHistory([attempt({ state })]);
      const { unmount } = render(<HistoryView />);
      await screen.findByText(state);
      expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
      unmount();
      vi.restoreAllMocks();
    }
  });

  it("approving calls the approve endpoint, and only that one", async () => {
    mockHistory([attempt()]);
    const approve = vi.spyOn(api, "approvePublishAttempt").mockResolvedValue({ state: "queued" });
    const retry = vi.spyOn(api, "retryPublishAttempt").mockResolvedValue({});

    render(<HistoryView />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));

    await waitFor(() => expect(approve).toHaveBeenCalledWith("att-1"));
    expect(retry).not.toHaveBeenCalled();
  });

  it("retrying calls the retry endpoint, and never escalates to approve", async () => {
    mockHistory([attempt({ state: "failed" })]);
    const approve = vi.spyOn(api, "approvePublishAttempt").mockResolvedValue({});
    const retry = vi.spyOn(api, "retryPublishAttempt").mockResolvedValue({ state: "queued" });

    render(<HistoryView />);
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() => expect(retry).toHaveBeenCalledWith("att-1"));
    expect(approve).not.toHaveBeenCalled();
  });

  it("reloads from the server after acting", async () => {
    // The worker may already have advanced the attempt past `queued` by the time the call
    // resolves, so the table is refreshed rather than patched from the response.
    const history = mockHistory([attempt()]);
    vi.spyOn(api, "approvePublishAttempt").mockResolvedValue({ state: "queued" });

    render(<HistoryView />);
    await screen.findByRole("button", { name: "Approve" });
    const before = history.mock.calls.length;

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(history.mock.calls.length).toBeGreaterThan(before));
  });

  it("surfaces a failure instead of silently doing nothing", async () => {
    mockHistory([attempt()]);
    vi.spyOn(api, "approvePublishAttempt").mockRejectedValue(new Error("403 not approved"));

    render(<HistoryView />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));

    expect(await screen.findByText("403 not approved")).toBeInTheDocument();
    // And the control comes back, so the user can try the other action.
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("disables both controls while a call is in flight", async () => {
    // Double-clicking approve would queue the same attempt twice.
    mockHistory([attempt()]);
    let release;
    vi.spyOn(api, "approvePublishAttempt").mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );

    render(<HistoryView />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));

    expect(screen.getByRole("button", { name: "Approving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Retry" })).toBeDisabled();

    release({ state: "queued" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
  });
});
