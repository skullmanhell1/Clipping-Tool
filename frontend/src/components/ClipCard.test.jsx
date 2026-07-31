import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api.js";
import ClipCard from "./ClipCard.jsx";

/**
 * I8: the clip card was untested and is the busiest component in the app — metadata editing,
 * per-field regeneration, publishing, and now the review verdict (U9) and per-clip re-render (U7).
 *
 * The tests below concentrate on the actions that are **destructive or expensive**: a re-render
 * costs a minute of CPU and overwrites a file that may already be published, and a verdict is a
 * decision someone will act on later. Metadata editing is comparatively safe — a wrong title is
 * visible and one keystroke to fix.
 */

const CLIP = {
  id: "c1",
  filename: "clip_c1.mp4",
  start: 1,
  end: 6,
  duration: 5,
  score: 82,
  title: "A title",
  description: "A description",
  hashtags: ["#one"],
  hook_text: "hook",
  cta: "cta",
  thumbnail_text: "thumb",
  video_url: "/clips/c1.mp4",
  effects_applied: ["captions"],
  review_state: "pending",
};

const setup = (clip = CLIP, props = {}) => {
  const onUpdated = vi.fn();
  const utils = render(
    <ClipCard
      jobId="job1"
      clip={clip}
      llmAvailable={false}
      publishing={{ platforms: [] }}
      publisherStatuses={{}}
      attempts={[]}
      onUpdated={onUpdated}
      onPublished={vi.fn()}
      settings={{ color: "vivid" }}
      {...props}
    />,
  );
  return { ...utils, onUpdated };
};

describe("ClipCard review verdict (U9)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "reviewClip").mockImplementation((_j, id, state) =>
      Promise.resolve({ ...CLIP, id, review_state: state }),
    );
  });

  it("records an approval", async () => {
    const { onUpdated } = setup();
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() => expect(api.reviewClip).toHaveBeenCalledWith("job1", "c1", "approved"));
    expect(onUpdated).toHaveBeenCalledWith(expect.objectContaining({ review_state: "approved" }));
  });

  it("records a rejection", async () => {
    setup();
    await userEvent.click(screen.getByRole("button", { name: /reject/i }));
    await waitFor(() => expect(api.reviewClip).toHaveBeenCalledWith("job1", "c1", "rejected"));
  });

  it("clicking the same verdict again clears it", async () => {
    setup({ ...CLIP, review_state: "approved" });
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() => expect(api.reviewClip).toHaveBeenCalledWith("job1", "c1", "pending"));
  });

  it("shows the current verdict as pressed", () => {
    setup({ ...CLIP, review_state: "rejected" });
    expect(screen.getByRole("button", { name: /reject/i })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /approve/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("surfaces a failure instead of pretending the verdict stuck", async () => {
    api.reviewClip.mockRejectedValueOnce(new Error("server said no"));
    setup();
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(await screen.findByText("server said no")).toBeInTheDocument();
  });
});

describe("ClipCard re-render (U7)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "rerenderClip").mockResolvedValue({ ...CLIP, duration: 5 });
  });

  it("re-renders with the settings currently selected in the panel", async () => {
    // That is what makes "change one setting and see it" a single click rather than a resubmit.
    setup();
    await userEvent.click(screen.getByRole("button", { name: /re-render this clip/i }));
    await waitFor(() =>
      expect(api.rerenderClip).toHaveBeenCalledWith("job1", "c1", { color: "vivid" }),
    );
  });

  it("blocks a second click while a re-render is running", async () => {
    // A re-render is minutes of CPU that overwrites the clip file; two at once race to write it.
    let release;
    api.rerenderClip.mockImplementation(
      () => new Promise((resolve) => {
        release = resolve;
      }),
    );
    setup();
    const button = screen.getByRole("button", { name: /re-render this clip/i });
    await userEvent.click(button);
    expect(button).toBeDisabled();
    release({ ...CLIP });
    await waitFor(() => expect(button).not.toBeDisabled());
    expect(api.rerenderClip).toHaveBeenCalledTimes(1);
  });

  it("reports the refreshed clip upward", async () => {
    const { onUpdated } = setup();
    await userEvent.click(screen.getByRole("button", { name: /re-render this clip/i }));
    await waitFor(() => expect(onUpdated).toHaveBeenCalled());
  });

  it("shows why a re-render failed", async () => {
    api.rerenderClip.mockRejectedValueOnce(
      new Error("The original source file is no longer available"),
    );
    setup();
    await userEvent.click(screen.getByRole("button", { name: /re-render this clip/i }));
    expect(await screen.findByText(/no longer available/i)).toBeInTheDocument();
  });
});

describe("ClipCard batch selection and player (U3, U9)", () => {
  it("offers a batch checkbox only when the parent handles selection", () => {
    setup(CLIP, { onToggleSelected: undefined });
    expect(screen.queryByLabelText(/select clip/i)).not.toBeInTheDocument();
  });

  it("reports a selection toggle to the parent", async () => {
    const onToggleSelected = vi.fn();
    setup(CLIP, { onToggleSelected });
    await userEvent.click(screen.getByLabelText(/select clip c1 for batch review/i));
    expect(onToggleSelected).toHaveBeenCalledWith("c1");
  });

  it("renders the review player rather than a bare video element", () => {
    setup();
    expect(screen.getByTestId("clip-video")).toBeInTheDocument();
    expect(screen.getByLabelText("Scrub")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /next frame/i })).toBeInTheDocument();
  });

  it("dims a rejected clip so a scan of the grid shows what is out", () => {
    setup({ ...CLIP, review_state: "rejected" });
    expect(screen.getByTestId("clip-c1").className).toMatch(/opacity-60/);
  });

  it("records the verdict on the card for the grid to read", () => {
    setup({ ...CLIP, review_state: "approved" });
    expect(screen.getByTestId("clip-c1")).toHaveAttribute("data-review-state", "approved");
  });
});
