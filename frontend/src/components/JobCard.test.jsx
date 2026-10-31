import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api.js";
import JobCard from "./JobCard.jsx";

/**
 * I8: the job card was untested, and it now owns the review workflow — batch selection (U9) and
 * the keyboard shortcuts (U11).
 *
 * The shortcut tests carry most of the weight here, because a keyboard handler bound on the
 * *window* is exactly the kind of code that works when you try it by hand and then misfires in a
 * situation nobody demonstrated. The one that matters is typing: `a` must insert an `a` when
 * someone is editing a caption, not approve the clip they happen to be looking at. That failure is
 * silent — the clip is approved, the letter is typed, and nothing looks wrong until a rejected
 * clip publishes.
 */

const clip = (id, overrides = {}) => ({
  id,
  filename: `clip_${id}.mp4`,
  start: 0,
  end: 5,
  duration: 5,
  score: 70,
  title: `Clip ${id}`,
  description: "",
  hashtags: [],
  hook_text: "",
  cta: "",
  thumbnail_text: "",
  video_url: `/clips/${id}.mp4`,
  effects_applied: [],
  review_state: "pending",
  ...overrides,
});

const measuredClip = (id, overrides = {}) =>
  clip(id, {
    engagement_score: 78,
    engagement_features: {
      quality_scores: { hook: 0.82, flow: 0.74, impact: 0.7, clarity: 0.86, relevance: 0.68 },
    },
    ...overrides,
  });

const job = (clips) => ({
  id: "job1",
  status: "completed",
  progress: 1,
  stage: "Completed",
  title: "A source video",
  clips,
  stage_index: 6,
  stage_total: 6,
  created_at: 1,
  updated_at: 2,
});

const setup = (clips = [clip("a"), clip("b"), clip("c")], props = {}) => {
  const onClipUpdated = vi.fn();
  const utils = render(
    <JobCard
      job={job(clips)}
      llmAvailable={false}
      publishing={{ platforms: [] }}
      publisherStatuses={{}}
      publishAttempts={[]}
      onClipUpdated={onClipUpdated}
      onPublished={vi.fn()}
      settings={{}}
      {...props}
    />,
  );
  return { ...utils, onClipUpdated };
};

describe("JobCard review workflow", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "reviewClip").mockImplementation((_jobId, clipId, state) =>
      Promise.resolve(clip(clipId, { review_state: state })),
    );
    vi.spyOn(api, "reviewClips").mockImplementation((_jobId, ids, state) =>
      Promise.resolve({ updated: ids.map((id) => clip(id, { review_state: state })), count: ids.length }),
    );
  });

  it("shows a tally of the review states", () => {
    setup([
      clip("a", { review_state: "approved" }),
      clip("b", { review_state: "rejected" }),
      clip("c"),
    ]);
    const tally = screen.getByTestId("review-tally");
    expect(tally).toHaveTextContent("1 approved");
    expect(tally).toHaveTextContent("1 rejected");
    expect(tally).toHaveTextContent("1 to review");
  });

  it("selects only the clips still awaiting a verdict", async () => {
    setup([clip("a", { review_state: "approved" }), clip("b"), clip("c")]);
    await userEvent.click(screen.getByRole("button", { name: /select pending/i }));
    expect(screen.getByText("2 selected")).toBeInTheDocument();
  });

  it("applies the status filter to the visible clips", async () => {
    setup([
      clip("a", { review_state: "approved" }),
      clip("b"),
      clip("c", { review_state: "rejected" }),
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Rejected" }));
    expect(screen.getByTestId("clip-c")).toBeInTheDocument();
    expect(screen.queryByTestId("clip-a")).not.toBeInTheDocument();
    expect(screen.queryByTestId("clip-b")).not.toBeInTheDocument();
  });

  it("filters to top picks and clips needing attention", async () => {
    setup([
      measuredClip("a", { engagement_score: 82 }),
      measuredClip("b", { engagement_score: 30 }),
      clip("c"),
    ]);

    await userEvent.click(screen.getByRole("button", { name: "Top picks" }));
    expect(screen.getByTestId("clip-a")).toBeInTheDocument();
    expect(screen.queryByTestId("clip-b")).not.toBeInTheDocument();
    expect(screen.queryByTestId("clip-c")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Needs attention" }));
    expect(screen.getByTestId("clip-b")).toBeInTheDocument();
    expect(screen.queryByTestId("clip-a")).not.toBeInTheDocument();
  });

  it("sorts by measured signal on request", async () => {
    const { container } = setup([
      measuredClip("a", { engagement_score: 30 }),
      measuredClip("b", { engagement_score: 82 }),
    ]);
    await userEvent.selectOptions(screen.getByLabelText("Sort clips"), "score");
    const order = [...container.querySelectorAll("[data-testid]")]
      .map((node) => node.getAttribute("data-testid"))
      .filter((id) => /^clip-[ab]$/.test(id));
    expect(order).toEqual(["clip-b", "clip-a"]);
  });

  it("scopes select-shown to the filtered list", async () => {
    setup([clip("a", { review_state: "approved" }), clip("b"), clip("c")]);
    await userEvent.click(screen.getByRole("button", { name: "Pending" }));
    await userEvent.click(screen.getByRole("button", { name: /select shown/i }));
    await userEvent.click(screen.getByRole("button", { name: /approve selected/i }));
    await waitFor(() => expect(api.reviewClips).toHaveBeenCalledTimes(1));
    const [, ids, state] = api.reviewClips.mock.calls[0];
    expect(ids.sort()).toEqual(["b", "c"]);
    expect(state).toBe("approved");
  });

  it("drops a selection that a filter hides", async () => {
    setup([clip("a", { review_state: "approved" }), clip("b")]);
    await userEvent.click(screen.getByRole("button", { name: "Approved" }));
    await userEvent.click(screen.getByRole("button", { name: /select shown/i }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Pending" }));
    await waitFor(() => expect(screen.getByText("nothing selected")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /approve selected/i })).toBeDisabled();
    expect(api.reviewClips).not.toHaveBeenCalled();
  });

  it("applies a batch verdict to exactly the selection", async () => {
    setup();
    await userEvent.click(screen.getByLabelText(/select clip a for batch review/i));
    await userEvent.click(screen.getByLabelText(/select clip c for batch review/i));
    await userEvent.click(
      screen.getByRole("button", { name: /approve selected/i }),
    );
    await waitFor(() => expect(api.reviewClips).toHaveBeenCalledTimes(1));
    const [, ids, state] = api.reviewClips.mock.calls[0];
    expect(ids.sort()).toEqual(["a", "c"]);
    expect(state).toBe("approved");
  });

  it("clears the selection only after the batch succeeds", async () => {
    // Keeping it on failure means the user can retry without picking twenty clips again.
    api.reviewClips.mockRejectedValueOnce(new Error("nope"));
    setup();
    await userEvent.click(screen.getByLabelText(/select clip a for batch review/i));
    await userEvent.click(screen.getByRole("button", { name: /approve selected/i }));
    await waitFor(() => expect(screen.getByText("nope")).toBeInTheDocument());
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("reports every updated clip upward so the list re-renders", async () => {
    const { onClipUpdated } = setup();
    await userEvent.click(screen.getByRole("button", { name: /select shown/i }));
    await userEvent.click(screen.getByRole("button", { name: /reject selected/i }));
    await waitFor(() => expect(onClipUpdated).toHaveBeenCalledTimes(3));
  });
});

describe("JobCard keyboard shortcuts (U11)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "reviewClip").mockImplementation((_jobId, clipId, state) =>
      Promise.resolve(clip(clipId, { review_state: state })),
    );
  });

  it("approves the focused clip with 'a'", async () => {
    setup();
    await userEvent.keyboard("a");
    await waitFor(() => expect(api.reviewClip).toHaveBeenCalledWith("job1", "a", "approved"));
  });

  it("rejects the focused clip with 'x'", async () => {
    setup();
    await userEvent.keyboard("x");
    await waitFor(() => expect(api.reviewClip).toHaveBeenCalledWith("job1", "a", "rejected"));
  });

  it("moves the focus with j and k", async () => {
    setup();
    await userEvent.keyboard("j");
    await userEvent.keyboard("a");
    await waitFor(() => expect(api.reviewClip).toHaveBeenCalledWith("job1", "b", "approved"));

    await userEvent.keyboard("k");
    await userEvent.keyboard("x");
    await waitFor(() => expect(api.reviewClip).toHaveBeenCalledWith("job1", "a", "rejected"));
  });

  it("does not move the focus past either end of the list", async () => {
    setup([clip("a"), clip("b")]);
    await userEvent.keyboard("kkkk");
    await userEvent.keyboard("a");
    await waitFor(() => expect(api.reviewClip).toHaveBeenCalledWith("job1", "a", "approved"));

    await userEvent.keyboard("jjjjjj");
    await userEvent.keyboard("x");
    await waitFor(() => expect(api.reviewClip).toHaveBeenCalledWith("job1", "b", "rejected"));
  });

  it("re-pressing the same verdict clears it", async () => {
    // A mis-click should be one keystroke to undo rather than a state you cannot leave.
    setup([clip("a", { review_state: "approved" })]);
    await userEvent.keyboard("a");
    await waitFor(() => expect(api.reviewClip).toHaveBeenCalledWith("job1", "a", "pending"));
  });

  it("selects the focused clip with 's'", async () => {
    setup();
    await userEvent.keyboard("s");
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("stays out of the way while a text field has focus", async () => {
    // The failure this prevents is silent: the letter is typed AND the clip is approved.
    setup();
    const title = screen.getAllByDisplayValue("Clip a")[0];
    await userEvent.click(title);
    await userEvent.keyboard("a");
    expect(api.reviewClip).not.toHaveBeenCalled();
    expect(title).toHaveValue("Clip aa");
  });

  it("stays out of the way while a textarea has focus", async () => {
    setup();
    const description = document.querySelector("textarea");
    await userEvent.click(description);
    await userEvent.keyboard("x");
    expect(api.reviewClip).not.toHaveBeenCalled();
  });

  it("ignores shortcuts pressed with a modifier held", async () => {
    // Ctrl+A is select-all, not approve.
    setup();
    await userEvent.keyboard("{Control>}a{/Control}");
    expect(api.reviewClip).not.toHaveBeenCalled();
  });

  it("marks the focused clip so the user can see what a keystroke will hit", async () => {
    setup();
    expect(screen.getByTestId("clip-a").className).toMatch(/ring-brand-accent/);
    await userEvent.keyboard("j");
    await waitFor(() => expect(screen.getByTestId("clip-b").className).toMatch(/ring-brand-accent/));
    expect(screen.getByTestId("clip-a").className).not.toMatch(/ring-brand-accent/);
  });

  it("binds nothing when the job has no clips", async () => {
    setup([]);
    await userEvent.keyboard("a");
    expect(api.reviewClip).not.toHaveBeenCalled();
  });
});

describe("JobCard prompted clip discovery", () => {
  it("starts a fresh selection pass with the creator prompt", async () => {
    const onJobsAdded = vi.fn();
    vi.spyOn(api, "generateMore").mockResolvedValue({
      job: { id: "job-more", status: "queued", clips: [] },
    });
    setup([clip("a")], { onJobsAdded });

    await userEvent.type(
      screen.getByLabelText("More clips prompt"),
      "Find the funniest disagreement",
    );
    await userEvent.click(screen.getByRole("button", { name: "Find more" }));

    await waitFor(() =>
      expect(api.generateMore).toHaveBeenCalledWith("job1", {
        selection_prompt: "Find the funniest disagreement",
        num_clips: "3",
      }),
    );
    expect(onJobsAdded).toHaveBeenCalledWith([
      { id: "job-more", status: "queued", clips: [] },
    ]);
  });
});
