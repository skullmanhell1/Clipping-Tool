import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api.js";
import ScheduleCalendar from "./ScheduleCalendar.jsx";

// A fixed clock, so "which cell is today" and the month grid are not a function of the day the
// suite happens to run. 2026-03-15 is a Sunday, which also exercises the Monday-first offset.
const NOW = new Date(2026, 2, 15, 12, 0, 0);

// Local-time epochs on purpose: the component groups by local calendar day, so building these in
// UTC would put them in a different cell depending on the runner's timezone.
const epoch = (y, m, d, h = 9, min = 0) => new Date(y, m, d, h, min).getTime() / 1000;

const attempt = (overrides = {}) => ({
  id: "a1",
  platform: "tiktok",
  state: "scheduled",
  scheduled_at: epoch(2026, 2, 10, 9, 30),
  url: "",
  error: "",
  ...overrides,
});

function mockApi(attempts = [attempt()]) {
  vi.spyOn(api, "schedule").mockResolvedValue({ attempts });
  vi.spyOn(api, "scheduleSuggestions").mockResolvedValue({
    suggestions: [{ at: epoch(2026, 2, 17, 18, 0) }, { at: epoch(2026, 2, 18, 19, 0) }],
    basis: "published platform heuristics, not this account's engagement",
  });
  vi.spyOn(api, "reschedulePublishAttempt").mockResolvedValue({});
  vi.spyOn(api, "cancelPublishAttempt").mockResolvedValue({});
}

function setup(attempts) {
  mockApi(attempts);
  const onError = vi.fn();
  const utils = render(<ScheduleCalendar onError={onError} />);
  return { onError, ...utils };
}

const loaded = () => waitFor(() => expect(api.schedule).toHaveBeenCalled());

beforeEach(() => {
  // `shouldAdvanceTime` so userEvent's internal waits still resolve; the clock is frozen only to
  // make "which cell is today" and the month grid independent of the run date.
  vi.useFakeTimers({ shouldAdvanceTime: true, now: NOW });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("the month grid", () => {
  it("opens on the current month", async () => {
    setup();
    await loaded();
    expect(screen.getByText("March 2026")).toBeInTheDocument();
  });

  it("starts weeks on Monday", async () => {
    // JS weeks start Sunday, so the offset arithmetic is the part that can be wrong; a
    // Sunday-first grid would put every event one cell out.
    setup();
    await loaded();
    const headers = screen.getAllByText(/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$/);
    expect(headers.map((h) => h.textContent)).toEqual([
      "Mon",
      "Tue",
      "Wed",
      "Thu",
      "Fri",
      "Sat",
      "Sun",
    ]);
  });

  it("renders six whole weeks, so the grid height never jumps between months", async () => {
    setup();
    await loaded();
    // 42 day cells, each carrying a day-of-month label.
    const cells = document.querySelectorAll(".min-h-\\[68px\\]");
    expect(cells).toHaveLength(42);
  });

  it("requests exactly the visible month's range", async () => {
    setup();
    await loaded();
    const [from, to] = api.schedule.mock.calls[0];
    expect(new Date(from * 1000).getMonth()).toBe(2);
    expect(new Date(from * 1000).getDate()).toBe(1);
    expect(new Date(to * 1000).getMonth()).toBe(2);
    expect(new Date(to * 1000).getDate()).toBe(31);
  });

  it("re-reads when the month changes", async () => {
    setup();
    await loaded();
    await userEvent.click(screen.getByLabelText("Next month"));
    await waitFor(() => expect(screen.getByText("April 2026")).toBeInTheDocument());
    expect(api.schedule).toHaveBeenCalledTimes(2);
  });

  it("steps backwards across a year boundary", async () => {
    setup();
    await loaded();
    for (let i = 0; i < 3; i += 1) {
      await userEvent.click(screen.getByLabelText("Previous month"));
    }
    await waitFor(() => expect(screen.getByText("December 2025")).toBeInTheDocument());
  });

  it("returns to the current month on Today", async () => {
    setup();
    await loaded();
    await userEvent.click(screen.getByLabelText("Next month"));
    await waitFor(() => expect(screen.getByText("April 2026")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Today" }));
    await waitFor(() => expect(screen.getByText("March 2026")).toBeInTheDocument());
  });

  it("says when it is loading", async () => {
    let release;
    vi.spyOn(api, "schedule").mockReturnValue(new Promise((r) => (release = r)));
    vi.spyOn(api, "scheduleSuggestions").mockResolvedValue({ suggestions: [] });
    render(<ScheduleCalendar onError={vi.fn()} />);
    expect(screen.getByText("Loading schedule…")).toBeInTheDocument();
    release({ attempts: [] });
    await waitFor(() => expect(screen.queryByText("Loading schedule…")).toBeNull());
  });

  it("reports a failed load rather than showing an empty month", async () => {
    // An empty calendar and a broken calendar look identical, and the difference matters: one
    // means "nothing is scheduled", the other means "you cannot see what is scheduled".
    vi.spyOn(api, "schedule").mockRejectedValue(new Error("gateway timeout"));
    const onError = vi.fn();
    render(<ScheduleCalendar onError={onError} />);
    await waitFor(() => expect(onError).toHaveBeenCalledWith("gateway timeout"));
  });
});

describe("attempts in the grid", () => {
  it("shows the local time and platform", async () => {
    setup();
    await loaded();
    expect(await screen.findByTitle("tiktok — scheduled")).toBeInTheDocument();
  });

  it("shows every state, not only pending ones", async () => {
    // A calendar that hid what had already gone out would show an empty week the operator had
    // in fact filled.
    setup([
      attempt({ id: "a1", state: "published", scheduled_at: epoch(2026, 2, 10, 9) }),
      attempt({ id: "a2", state: "failed", scheduled_at: epoch(2026, 2, 11, 9) }),
    ]);
    await loaded();
    expect(await screen.findByTitle("tiktok — published")).toBeInTheDocument();
    expect(screen.getByTitle("tiktok — failed")).toBeInTheDocument();
  });

  it("ignores attempts with no scheduled time", async () => {
    // An unscheduled attempt has no cell to go in; grouping it by epoch 0 would file it under
    // 1970.
    setup([attempt({ scheduled_at: null })]);
    await loaded();
    await waitFor(() => expect(screen.queryByTitle(/tiktok/)).toBeNull());
  });

  it("caps a busy day at three and counts the rest", async () => {
    setup(
      [9, 10, 11, 12, 13].map((hour, i) =>
        attempt({ id: `a${i}`, scheduled_at: epoch(2026, 2, 10, hour) }),
      ),
    );
    await loaded();
    expect(await screen.findByText("+2 more")).toBeInTheDocument();
  });

  it("orders a day's attempts by time", async () => {
    setup([
      attempt({ id: "late", platform: "x", scheduled_at: epoch(2026, 2, 10, 18) }),
      attempt({ id: "early", platform: "whop", scheduled_at: epoch(2026, 2, 10, 6) }),
    ]);
    await loaded();
    const buttons = await screen.findAllByTitle(/— scheduled/);
    expect(buttons[0]).toHaveAccessibleName(/whop/);
  });

  it("falls back to the draft style for an unrecognised state", async () => {
    // A state added on the backend must still render as a visible chip rather than an unstyled
    // one, so it can be clicked and inspected.
    setup([attempt({ state: "some_new_state" })]);
    await loaded();
    expect(await screen.findByTitle("tiktok — some_new_state")).toBeInTheDocument();
  });
});

describe("the detail panel", () => {
  const openFirst = async () => {
    const chip = await screen.findByTitle(/tiktok — /);
    await userEvent.click(chip);
  };

  it("opens on click and closes on Close", async () => {
    setup();
    await loaded();
    await openFirst();
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("button", { name: "Close" })).toBeNull();
  });

  it("pre-fills the move field with the attempt's local time", async () => {
    // `datetime-local` needs local wall-clock text; an ISO/UTC string renders as blank.
    setup();
    await loaded();
    await openFirst();
    expect(screen.getByLabelText("Move to")).toHaveValue("2026-03-10T09:30");
  });

  it("offers reschedule and cancel for a pending attempt", async () => {
    setup([attempt({ state: "queued" })]);
    await loaded();
    await openFirst();
    expect(screen.getByRole("button", { name: "Reschedule" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Cancel post" })).toBeEnabled();
  });

  it("refuses to move a settled attempt, and says why", async () => {
    // The API returns 409 for these; offering the control would be an invitation to fail.
    setup([attempt({ state: "published" })]);
    await loaded();
    await openFirst();
    expect(screen.queryByRole("button", { name: "Reschedule" })).toBeNull();
    expect(screen.getByText(/is published and can no longer be moved or cancelled/)).toBeVisible();
  });

  it("links to a published post when there is a url", async () => {
    setup([attempt({ state: "published", url: "https://tiktok.test/v/1" })]);
    await loaded();
    await openFirst();
    const link = screen.getByRole("link", { name: "View post" });
    expect(link).toHaveAttribute("href", "https://tiktok.test/v/1");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("shows the failure reason for a failed attempt", async () => {
    setup([attempt({ state: "failed", error: "upload rejected: video too short" })]);
    await loaded();
    await openFirst();
    expect(screen.getByText("upload rejected: video too short")).toBeInTheDocument();
  });

  it("reschedules to the chosen epoch and reloads", async () => {
    setup();
    await loaded();
    await openFirst();
    const field = screen.getByLabelText("Move to");
    await userEvent.clear(field);
    await userEvent.type(field, "2026-03-20T15:45");
    await userEvent.click(screen.getByRole("button", { name: "Reschedule" }));
    await waitFor(() => expect(api.reschedulePublishAttempt).toHaveBeenCalled());
    const [id, at] = api.reschedulePublishAttempt.mock.calls[0];
    expect(id).toBe("a1");
    expect(at).toBe(new Date(2026, 2, 20, 15, 45).getTime() / 1000);
    expect(api.schedule).toHaveBeenCalledTimes(2);
  });

  it("closes the panel after a successful reschedule", async () => {
    setup();
    await loaded();
    await openFirst();
    await userEvent.click(screen.getByRole("button", { name: "Reschedule" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Close" })).toBeNull());
  });

  it("does nothing when the move field is empty", async () => {
    // Clearing the field and pressing Reschedule must not send a request: `new Date("")` is
    // Invalid Date, and its epoch is NaN, which the API would reject as a 422 at best.
    setup([attempt({ state: "queued" })]);
    await loaded();
    await openFirst();
    await userEvent.clear(screen.getByLabelText("Move to"));
    await userEvent.click(screen.getByRole("button", { name: "Reschedule" }));
    expect(api.reschedulePublishAttempt).not.toHaveBeenCalled();
  });

  it("reports a reschedule failure and leaves the panel open to retry", async () => {
    const { onError } = setup();
    await loaded();
    vi.spyOn(api, "reschedulePublishAttempt").mockRejectedValue(new Error("already published"));
    await openFirst();
    await userEvent.click(screen.getByRole("button", { name: "Reschedule" }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith("already published"));
    expect(screen.getByRole("button", { name: "Reschedule" })).toBeEnabled();
  });

  it("cancels the attempt and reloads", async () => {
    setup();
    await loaded();
    await openFirst();
    await userEvent.click(screen.getByRole("button", { name: "Cancel post" }));
    await waitFor(() => expect(api.cancelPublishAttempt).toHaveBeenCalledWith("a1"));
    expect(api.schedule).toHaveBeenCalledTimes(2);
  });

  it("reports a cancel failure", async () => {
    const { onError } = setup();
    await loaded();
    vi.spyOn(api, "cancelPublishAttempt").mockRejectedValue(new Error("too late"));
    await openFirst();
    await userEvent.click(screen.getByRole("button", { name: "Cancel post" }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith("too late"));
  });
});

describe("best-time suggestions", () => {
  it("asks only when requested, not on every render", async () => {
    // Suggestions are a separate API call; firing it on mount would double the calendar's cost
    // for a feature most opens do not use.
    setup();
    await loaded();
    expect(api.scheduleSuggestions).not.toHaveBeenCalled();
  });

  it("fetches for the selected platform", async () => {
    setup();
    await loaded();
    await userEvent.selectOptions(screen.getByLabelText("Platform for suggestions"), "youtube");
    await userEvent.click(screen.getByRole("button", { name: "Suggest times" }));
    await waitFor(() => expect(api.scheduleSuggestions).toHaveBeenCalledWith("youtube", 7, 2));
  });

  it("states the basis, so the numbers are not mistaken for measured engagement", async () => {
    setup();
    await loaded();
    await userEvent.click(screen.getByRole("button", { name: "Suggest times" }));
    expect(
      await screen.findByText(/published platform heuristics, not this account's engagement/),
    ).toBeInTheDocument();
  });

  it("shows nothing when there are no suggestions", async () => {
    setup();
    await loaded();
    vi.spyOn(api, "scheduleSuggestions").mockResolvedValue({ suggestions: [], basis: "" });
    await userEvent.click(screen.getByRole("button", { name: "Suggest times" }));
    await waitFor(() => expect(screen.queryByText(/Suggested times/)).toBeNull());
  });

  it("reports a suggestions failure", async () => {
    const { onError } = setup();
    await loaded();
    vi.spyOn(api, "scheduleSuggestions").mockRejectedValue(new Error("no history yet"));
    await userEvent.click(screen.getByRole("button", { name: "Suggest times" }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith("no history yet"));
  });
});
