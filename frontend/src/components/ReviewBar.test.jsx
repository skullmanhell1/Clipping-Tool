import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewBar from "./ReviewBar.jsx";

/**
 * U9: the batch review bar.
 *
 * The tests are weighted towards the *destructive* direction. A batch action applies a verdict to
 * many clips at once, so the failure that matters is acting on a selection the user did not make -
 * and that failure looks like success, because the call returns 200 and the counts change.
 */

const setup = (overrides = {}) => {
  const props = {
    counts: { approved: 1, rejected: 2, pending: 3 },
    total: 6,
    selectedCount: 0,
    busy: false,
    error: "",
    onSelectAll: vi.fn(),
    onSelectNone: vi.fn(),
    onSelectPending: vi.fn(),
    onApprove: vi.fn(),
    onReject: vi.fn(),
    onReset: vi.fn(),
    ...overrides,
  };
  const utils = render(<ReviewBar {...props} />);
  return { ...props, ...utils };
};

describe("ReviewBar", () => {
  it("shows how many clips are in each state", () => {
    setup();
    const tally = screen.getByTestId("review-tally");
    expect(tally).toHaveTextContent("1 approved");
    expect(tally).toHaveTextContent("2 rejected");
    expect(tally).toHaveTextContent("3 to review");
    expect(tally).toHaveTextContent("6 total");
  });

  it("disables every batch verdict while nothing is selected", () => {
    setup({ selectedCount: 0 });
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /reset/i })).toBeDisabled();
  });

  it("enables the verdicts once clips are selected, and reports the count", async () => {
    const props = setup({ selectedCount: 4 });
    expect(screen.getByText("4 selected")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(props.onApprove).toHaveBeenCalledTimes(1);
    expect(props.onReject).not.toHaveBeenCalled();
  });

  it("each verdict calls only its own handler", async () => {
    const props = setup({ selectedCount: 2 });
    await userEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(props.onReject).toHaveBeenCalledTimes(1);
    expect(props.onApprove).not.toHaveBeenCalled();
    expect(props.onReset).not.toHaveBeenCalled();
  });

  it("offers 'select pending', which is the second-pass action", async () => {
    // Selecting the undecided clips by hand is the work the batch action exists to remove.
    const props = setup();
    await userEvent.click(screen.getByRole("button", { name: /select pending/i }));
    expect(props.onSelectPending).toHaveBeenCalledTimes(1);
  });

  it("blocks every action while a batch is in flight", () => {
    // Otherwise a second click sends a second request for the same clips, and whichever lands
    // last silently wins.
    setup({ selectedCount: 3, busy: true });
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /select pending/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^all$/i })).toBeDisabled();
  });

  it("surfaces a failure instead of swallowing it", () => {
    setup({ error: "Batch review failed." });
    expect(screen.getByText("Batch review failed.")).toBeInTheDocument();
  });

  it("documents the keyboard shortcuts, since they are otherwise undiscoverable", () => {
    const { container } = setup();
    const keys = [...container.querySelectorAll("kbd")].map((node) => node.textContent);
    // The review keys specifically: moving, judging and selecting.
    expect(keys).toEqual(expect.arrayContaining(["j", "k", "a", "x", "s", "space"]));
  });
});
