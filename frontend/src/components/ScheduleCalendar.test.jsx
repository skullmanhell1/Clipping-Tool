import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ScheduleCalendar from "./ScheduleCalendar.jsx";
import { api } from "../api.js";

/**
 * PB7: the month calendar of scheduled posts.
 *
 * Everything this component does is a conversion between three representations of one instant: the
 * epoch seconds the API speaks, the local calendar day a post appears under, and the local
 * wall-clock text a `datetime-local` input holds. Each boundary is a place an off-by-one hides in
 * silence — a post shown on Tuesday that goes out on Wednesday, or a reschedule that moves a post by
 * the size of the UTC offset because the input's text was read as UTC.
 *
 * None of that is visible from the screen: the calendar looks right either way, and the only
 * evidence is the post appearing at the wrong time, once, after the fact. So the tests fix the clock
 * and construct every instant from *local* date components, which makes the expected epoch and the
 * expected wall-clock text correct in any timezone the suite happens to run in.
 *
 * The other property worth pinning is which attempts can be moved. The API allows a reschedule only
 * for `queued` and `scheduled`, and answers 409 otherwise; offering the control for a published post
 * would invite a user to try to un-publish something that is already public.
 */

/** An instant on a day of May 2024, in local time — the same basis the component reads. */
const at = (day, hour, minute = 0) => new Date(2024, 4, day, hour, minute).getTime() / 1000;

const timeLabel = (epoch) =>
  new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const monthLabel = (date) => date.toLocaleDateString([], { month: "long", year: "numeric" });

const attempt = (overrides = {}) => ({
  id: "a1",
  platform: "tiktok",
  state: "scheduled",
  scheduled_at: at(8, 9, 30),
  url: "",
  error: "",
  ...overrides,
});

const mockSchedule = (attempts) =>
  vi.spyOn(api, "schedule").mockResolvedValue({ attempts, count: attempts.length });

/**
 * The 42 day cells of the grid. They carry no role of their own, so they are found by the height
 * class that only they have — the index of a cell is the assertion in the placement tests, and
 * nothing else on the page identifies a cell by its date.
 */
const dayCells = (container) =>
  [...container.querySelectorAll("div")].filter((node) => node.className.includes("min-h-[68px]"));

const setup = async (attempts = [attempt()]) => {
  const schedule = mockSchedule(attempts);
  const onError = vi.fn();
  const utils = render(<ScheduleCalendar onError={onError} />);
  // Waiting for the loading line to go, rather than for the call, so that the state updates the
  // response triggers happen inside this await instead of after the test has moved on.
  await waitFor(() => expect(screen.queryByText(/loading schedule…/i)).not.toBeInTheDocument());
  return { ...utils, schedule, onError };
};

beforeEach(() => {
  // A fixed clock: 15 May 2024, local noon. May 2024 starts on a Wednesday, so a Monday-first grid
  // has to begin on 29 April — which is what makes the padding assertions meaningful.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(2024, 4, 15, 12, 0, 0));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("ScheduleCalendar month window", () => {
  it("asks for exactly the visible month, in epoch seconds", async () => {
    // The request window is derived from the displayed month rather than from "now", so a user
    // paging back a month must not be served the current month's attempts.
    const { schedule } = await setup();
    expect(schedule).toHaveBeenCalledWith(
      new Date(2024, 4, 1).getTime() / 1000,
      new Date(2024, 4, 31, 23, 59, 59).getTime() / 1000
    );
  });

  it("opens on the current month", async () => {
    await setup();
    expect(screen.getByRole("heading", { name: monthLabel(new Date(2024, 4, 1)) })).toBeVisible();
  });

  it("pages forward a month and refetches that window", async () => {
    const { schedule } = await setup();
    await userEvent.click(screen.getByRole("button", { name: /next month/i }));
    expect(screen.getByRole("heading", { name: monthLabel(new Date(2024, 5, 1)) })).toBeVisible();
    await waitFor(() =>
      expect(schedule).toHaveBeenCalledWith(
        new Date(2024, 5, 1).getTime() / 1000,
        new Date(2024, 5, 30, 23, 59, 59).getTime() / 1000
      )
    );
  });

  it("pages back across a year boundary without losing the year", async () => {
    // `new Date(year, month - 1, 1)` is the arithmetic that has to roll the year over; a naive
    // month decrement would show January again with a different label.
    const { schedule } = await setup();
    vi.setSystemTime(new Date(2024, 0, 10, 12));
    await userEvent.click(screen.getByRole("button", { name: /^today$/i }));
    await userEvent.click(screen.getByRole("button", { name: /previous month/i }));
    expect(screen.getByRole("heading", { name: monthLabel(new Date(2023, 11, 1)) })).toBeVisible();
    await waitFor(() =>
      expect(schedule).toHaveBeenCalledWith(
        new Date(2023, 11, 1).getTime() / 1000,
        new Date(2023, 11, 31, 23, 59, 59).getTime() / 1000
      )
    );
  });

  it("returns to the current month from wherever the user has paged to", async () => {
    await setup();
    await userEvent.click(screen.getByRole("button", { name: /next month/i }));
    await userEvent.click(screen.getByRole("button", { name: /^today$/i }));
    expect(screen.getByRole("heading", { name: monthLabel(new Date(2024, 4, 1)) })).toBeVisible();
  });

  it("reports a failed load rather than showing an empty month", async () => {
    // An empty calendar and a failed request look identical, and one of them means "nothing is
    // scheduled" — which is the answer a user would act on.
    vi.spyOn(api, "schedule").mockRejectedValue(new Error("schedule unavailable"));
    const onError = vi.fn();
    render(<ScheduleCalendar onError={onError} />);
    await waitFor(() => expect(onError).toHaveBeenCalledWith("schedule unavailable"));
  });

  it("shows that it is loading", async () => {
    let release;
    vi.spyOn(api, "schedule").mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      })
    );
    render(<ScheduleCalendar onError={vi.fn()} />);
    expect(screen.getByText(/loading schedule…/i)).toBeInTheDocument();
    release({ attempts: [] });
    await waitFor(() => expect(screen.queryByText(/loading schedule…/i)).not.toBeInTheDocument());
  });
});

describe("ScheduleCalendar grid", () => {
  it("labels the week Monday first", async () => {
    // The header and the grid have to agree about which column is which day, and only the header
    // says so out loud.
    const { container } = await setup();
    const header = container.querySelector(".grid-cols-7");
    expect([...header.children].map((cell) => cell.textContent)).toEqual([
      "Mon",
      "Tue",
      "Wed",
      "Thu",
      "Fri",
      "Sat",
      "Sun",
    ]);
  });

  it("pads to six whole weeks, starting on the Monday before the 1st", async () => {
    // The grid is a fixed 42 cells. If it began on Sunday instead, every attempt would render one
    // column off — a post shown on the wrong day of the week, with the right time on it.
    const { container } = await setup();
    const cells = dayCells(container);
    expect(cells).toHaveLength(42);
    expect(cells[0]).toHaveTextContent("29"); // Monday 29 April
    expect(cells[2]).toHaveTextContent("1"); // Wednesday 1 May
  });

  it("rings today", async () => {
    // 15 May 2024 is the 16th cell: two days of April padding, then the 1st through the 15th.
    const { container } = await setup();
    expect(dayCells(container)[16].className).toMatch(/ring-brand-accent/);
    expect(dayCells(container)[15].className).not.toMatch(/ring-brand-accent/);
  });

  it("places an attempt on the local day its epoch falls in", async () => {
    // This is the conversion that hides an off-by-one: 09:30 local on the 8th belongs under the
    // 8th, not under the 7th because the epoch is earlier in UTC.
    const { container } = await setup();
    const cell = dayCells(container)[9];
    expect(cell).toHaveTextContent(`${timeLabel(at(8, 9, 30))} tiktok`);
  });

  it("shows the first three of a busy day and counts the rest", async () => {
    // A cell is 68px tall; ten attempts would push the row past the fold and hide the days below it.
    const { container } = await setup([
      attempt({ id: "b1", scheduled_at: at(20, 8) }),
      attempt({ id: "b2", scheduled_at: at(20, 9) }),
      attempt({ id: "b3", scheduled_at: at(20, 10) }),
      attempt({ id: "b4", scheduled_at: at(20, 11) }),
      attempt({ id: "b5", scheduled_at: at(20, 12) }),
    ]);
    const cell = dayCells(container)[21];
    expect(cell.querySelectorAll("button")).toHaveLength(3);
    expect(cell).toHaveTextContent("+2 more");
  });

  it("orders a day's attempts by time", async () => {
    // Out of order they read as a different plan than the one that will run.
    const { container } = await setup([
      attempt({ id: "late", scheduled_at: at(8, 18) }),
      attempt({ id: "early", scheduled_at: at(8, 6) }),
    ]);
    const times = [...dayCells(container)[9].querySelectorAll("button")].map(
      (button) => button.textContent
    );
    expect(times).toEqual([`${timeLabel(at(8, 6))} tiktok`, `${timeLabel(at(8, 18))} tiktok`]);
  });

  it("ignores an attempt that has no scheduled time", async () => {
    // A publish attempt that was never scheduled has `scheduled_at: null`, and `null * 1000` is
    // 1970 — it would render in January of whatever month the user paged to.
    const { container } = await setup([
      attempt({ id: "n1", platform: "whop", scheduled_at: null }),
    ]);
    expect(screen.queryByRole("button", { name: /whop/i })).not.toBeInTheDocument();
    expect(dayCells(container).every((cell) => cell.querySelectorAll("button").length === 0)).toBe(
      true
    );
  });

  it("shows attempts in every state, including ones that already went out", async () => {
    // "What did I post on Tuesday" is the same question as "what am I posting on Thursday", and a
    // calendar that hid published posts would show an empty week the operator had in fact filled.
    await setup([
      attempt({ id: "s1", scheduled_at: at(6, 9) }),
      attempt({ id: "s2", state: "published", platform: "instagram", scheduled_at: at(7, 9) }),
      attempt({ id: "s3", state: "failed", platform: "x", scheduled_at: at(9, 9) }),
      attempt({ id: "s4", state: "review_required", platform: "whop", scheduled_at: at(10, 9) }),
    ]);
    expect(screen.getByRole("button", { name: /instagram/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /x$/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /whop/ })).toBeInTheDocument();
  });
});

describe("ScheduleCalendar rescheduling", () => {
  const open = async () => {
    const utils = await setup();
    await userEvent.click(screen.getByRole("button", { name: /tiktok/ }));
    return utils;
  };

  it("opens the attempt it was asked for", async () => {
    await open();
    expect(screen.getByRole("heading", { name: "tiktok — scheduled" })).toBeVisible();
  });

  it("pre-fills the field with local wall-clock time, not an ISO string", async () => {
    // `datetime-local` holds local wall-clock text. Feeding it `toISOString()` would display the
    // UTC time — which looks like a plausible time, and is wrong by the offset.
    await open();
    expect(screen.getByLabelText(/move to/i)).toHaveValue("2024-05-08T09:30");
  });

  it("sends the epoch seconds of the local time that was typed", async () => {
    // The mirror image of the pre-fill: the text is read as local, so 18:45 means 18:45 here.
    // A `Date.UTC` parse would move every rescheduled post by the size of the offset.
    const reschedule = vi.spyOn(api, "reschedulePublishAttempt").mockResolvedValue({});
    await open();
    // A date control cannot be dragged or reliably typed into in jsdom, so the value is set the way
    // the browser would deliver it.
    fireEvent.change(screen.getByLabelText(/move to/i), { target: { value: "2024-05-09T18:45" } });
    await userEvent.click(screen.getByRole("button", { name: "Reschedule" }));
    await waitFor(() =>
      expect(reschedule).toHaveBeenCalledWith("a1", new Date(2024, 4, 9, 18, 45).getTime() / 1000)
    );
  });

  it("reloads the month after a reschedule, and closes the panel", async () => {
    // The attempt has moved to another day, so the grid it came from is stale.
    vi.spyOn(api, "reschedulePublishAttempt").mockResolvedValue({});
    const { schedule } = await open();
    const before = schedule.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Reschedule" }));
    await waitFor(() => expect(schedule.mock.calls.length).toBeGreaterThan(before));
    expect(screen.queryByLabelText(/move to/i)).not.toBeInTheDocument();
  });

  it("reports a refused reschedule and keeps the panel open", async () => {
    // The API answers 409 when the worker has already picked the attempt up. Closing the panel
    // would leave the user believing the move had happened.
    vi.spyOn(api, "reschedulePublishAttempt").mockRejectedValue(new Error("already uploading"));
    const { onError } = await open();
    await userEvent.click(screen.getByRole("button", { name: "Reschedule" }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith("already uploading"));
    expect(screen.getByLabelText(/move to/i)).toBeInTheDocument();
  });

  it("cancels a pending post", async () => {
    const cancel = vi.spyOn(api, "cancelPublishAttempt").mockResolvedValue({});
    const { schedule } = await open();
    const before = schedule.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: /cancel post/i }));
    await waitFor(() => expect(cancel).toHaveBeenCalledWith("a1"));
    await waitFor(() => expect(schedule.mock.calls.length).toBeGreaterThan(before));
  });

  it("reports a refused cancellation", async () => {
    vi.spyOn(api, "cancelPublishAttempt").mockRejectedValue(new Error("too late"));
    const { onError } = await open();
    await userEvent.click(screen.getByRole("button", { name: /cancel post/i }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith("too late"));
  });

  it("blocks both actions while one is in flight", async () => {
    // Rescheduling and cancelling the same attempt at once is a race whose winner decides whether
    // the post exists.
    let release;
    vi.spyOn(api, "cancelPublishAttempt").mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      })
    );
    await open();
    await userEvent.click(screen.getByRole("button", { name: /cancel post/i }));
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /cancel post/i })).toBeDisabled();
    release({});
    await waitFor(() => expect(screen.queryByLabelText(/move to/i)).not.toBeInTheDocument());
  });

  it("closes the panel on request", async () => {
    await open();
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByLabelText(/move to/i)).not.toBeInTheDocument();
  });

  it("offers the same controls for a queued attempt", async () => {
    // Queued and scheduled are the two states the API will move; a queued attempt has not been
    // picked up yet, so it is still the user's to change.
    await setup([attempt({ state: "queued" })]);
    await userEvent.click(screen.getByRole("button", { name: /tiktok/ }));
    expect(screen.getByRole("button", { name: "Reschedule" })).toBeEnabled();
  });
});

describe("ScheduleCalendar attempts that cannot be moved", () => {
  it("offers no controls for a published post, and links to it instead", async () => {
    // There is nothing to move: the post is public. The link is the only useful action left.
    await setup([
      attempt({ state: "published", url: "https://tiktok.test/p/1", scheduled_at: at(8, 9, 30) }),
    ]);
    await userEvent.click(screen.getByRole("button", { name: /tiktok/ }));
    expect(screen.queryByLabelText(/move to/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /cancel post/i })).not.toBeInTheDocument();
    expect(screen.getByText(/can no longer be moved or cancelled/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view post/i })).toHaveAttribute(
      "href",
      "https://tiktok.test/p/1"
    );
  });

  it("says why, naming the state that closed the door", async () => {
    await setup([attempt({ state: "uploading" })]);
    await userEvent.click(screen.getByRole("button", { name: /tiktok/ }));
    expect(screen.getByText(/this attempt is uploading/i)).toBeInTheDocument();
  });

  it("omits the link when a published post reported no URL", async () => {
    await setup([attempt({ state: "published", url: "" })]);
    await userEvent.click(screen.getByRole("button", { name: /tiktok/ }));
    expect(screen.queryByRole("link", { name: /view post/i })).not.toBeInTheDocument();
  });

  it("shows the error a failed attempt carries", async () => {
    // Without it the calendar says a post failed and offers nothing to act on.
    await setup([attempt({ state: "failed", error: "token expired" })]);
    await userEvent.click(screen.getByRole("button", { name: /tiktok/ }));
    expect(screen.getByText("token expired")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reschedule" })).not.toBeInTheDocument();
  });
});

describe("ScheduleCalendar suggestions", () => {
  it("does not fetch suggestions until they are asked for", async () => {
    // They are a heuristic, not part of the schedule; fetching them on mount would spend a request
    // on every page load for a panel most sessions never open.
    const suggest = vi.spyOn(api, "scheduleSuggestions");
    await setup();
    expect(suggest).not.toHaveBeenCalled();
  });

  it("fetches a week of suggestions for the selected platform", async () => {
    const suggest = vi
      .spyOn(api, "scheduleSuggestions")
      .mockResolvedValue({ suggestions: [], basis: "" });
    await setup();
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /platform for suggestions/i }),
      "instagram"
    );
    await userEvent.click(screen.getByRole("button", { name: /suggest times/i }));
    await waitFor(() => expect(suggest).toHaveBeenCalledWith("instagram", 7, 2));
  });

  it("renders where the suggestions came from", async () => {
    // They are published heuristics rather than this account's measured engagement, and presenting
    // a guess as an analysis is the actual harm available here.
    vi.spyOn(api, "scheduleSuggestions").mockResolvedValue({
      suggestions: [{ at: at(16, 18) }],
      basis: "Published platform guidance, not your audience data",
    });
    await setup();
    await userEvent.click(screen.getByRole("button", { name: /suggest times/i }));
    expect(await screen.findByText(/not your audience data/i)).toBeInTheDocument();
  });

  it("shows at most eight suggestions", async () => {
    vi.spyOn(api, "scheduleSuggestions").mockResolvedValue({
      suggestions: Array.from({ length: 10 }, (_, index) => ({ at: at(16, 8 + index) })),
      basis: "",
    });
    await setup();
    await userEvent.click(screen.getByRole("button", { name: /suggest times/i }));
    const block = (await screen.findByText(/suggested times for tiktok/i)).closest("div");
    expect(block.querySelectorAll("span")).toHaveLength(8);
  });

  it("reports a failed suggestion fetch", async () => {
    vi.spyOn(api, "scheduleSuggestions").mockRejectedValue(new Error("no history yet"));
    const { onError } = await setup();
    await userEvent.click(screen.getByRole("button", { name: /suggest times/i }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith("no history yet"));
  });

  it("shows nothing until there is something to show", async () => {
    await setup();
    expect(screen.queryByText(/suggested times/i)).not.toBeInTheDocument();
  });
});
