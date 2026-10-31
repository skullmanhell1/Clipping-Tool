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

describe("ClipCard measured review signals", () => {
  it("shows the score breakdown and the measured reasons behind the ranking", () => {
    setup({
      ...CLIP,
      engagement_score: 76,
      retention_plan: {
        mode: "balanced",
        hook_score: 0.82,
        quality_scores: { flow: 0.74, impact: 0.61, clarity: 0.88, relevance: 0.4 },
        reasons: ["front_loaded_hook", "clear_payoff"],
      },
    });

    expect(screen.getByText("Why this clip")).toBeInTheDocument();
    expect(screen.getByText("76/100 measured engagement")).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Hook signal" })).toHaveAttribute(
      "aria-valuenow",
      "82",
    );
    expect(screen.getByRole("meter", { name: "Clarity signal" })).toHaveAttribute(
      "aria-valuenow",
      "88",
    );
    expect(screen.getByText("Strong opening")).toBeInTheDocument();
    expect(screen.getByText("Clear payoff")).toBeInTheDocument();
    expect(screen.getByText(/not a prediction of views/i)).toBeInTheDocument();
    expect(screen.getByTestId("top-pick-badge")).toHaveTextContent("Top pick");
  });

  it("marks a weak axis for attention instead of calling it a top pick", () => {
    setup({
      ...CLIP,
      engagement_score: 55,
      retention_plan: {
        mode: "balanced",
        hook_score: 0.3,
        quality_scores: { flow: 0.7, impact: 0.7, clarity: 0.4, relevance: 0.6 },
      },
    });

    expect(screen.getByTestId("attention-badge")).toHaveAttribute(
      "title",
      "weak opening · unclear thought",
    );
    expect(screen.queryByTestId("top-pick-badge")).not.toBeInTheDocument();
  });

  it("falls back to engagement features when no retention plan was run", () => {
    setup({
      ...CLIP,
      engagement_features: {
        hook_score: 0.9,
        quality_scores: { flow: 0.55, impact: 0.45, clarity: 0.65, relevance: 0.5 },
      },
    });

    expect(screen.getByRole("meter", { name: "Hook signal" })).toHaveAttribute(
      "aria-valuenow",
      "90",
    );
    expect(screen.getByRole("meter", { name: "Flow signal" })).toHaveAttribute(
      "aria-valuenow",
      "55",
    );
  });

  it("does not invent a signal panel when no measurements exist", () => {
    setup();
    expect(screen.queryByText("Why this clip")).not.toBeInTheDocument();
  });
});

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

describe("ClipCard platform variants", () => {
  it("mounts the delivery panel when the server advertises platforms", () => {
    setup(CLIP, {
      outputProfiles: [
        { name: "tiktok", label: "TikTok", recommended_aspect: "9:16", width: 1080, height: 1920 },
      ],
    });
    expect(screen.getByTestId("platform-variants")).toBeInTheDocument();
    expect(screen.getByText("TikTok")).toBeInTheDocument();
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
    // The trailing empty cut list is U4's: a plain re-render must send no cuts, or pressing
    // this button would silently re-apply whatever the transcript editor last had selected.
    await waitFor(() =>
      expect(api.rerenderClip).toHaveBeenCalledWith("job1", "c1", { color: "vivid" }, []),
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

describe("ClipCard transcript trimming (U4)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "rerenderClip").mockResolvedValue({ ...CLIP, duration: 4 });
    vi.spyOn(api, "clipTranscript").mockResolvedValue({
      duration: 5,
      trimmed: false,
      max_cuts: 200,
      words: [
        { start: 0.0, end: 0.4, text: "keep", probability: 0.99 },
        { start: 0.6, end: 1.0, text: "um", probability: 0.4 },
      ],
    });
  });

  it("does not fetch the transcript until the editor is opened", () => {
    // One request per clip, and a grid of twenty clips is the normal case.
    setup();
    expect(api.clipTranscript).not.toHaveBeenCalled();
  });

  it("opens the editor on demand", async () => {
    setup();
    await userEvent.click(screen.getByRole("button", { name: /trim by transcript/i }));
    expect(await screen.findByTestId("transcript-editor-c1")).toBeInTheDocument();
    expect(api.clipTranscript).toHaveBeenCalledWith("job1", "c1");
  });

  it("re-renders with the struck word's time range and the current settings", async () => {
    const { onUpdated } = setup();
    await userEvent.click(screen.getByRole("button", { name: /trim by transcript/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Cut “um”/ }));
    await userEvent.click(screen.getByRole("button", { name: /apply cuts/i }));
    await waitFor(() =>
      expect(api.rerenderClip).toHaveBeenCalledWith(
        "job1",
        "c1",
        { color: "vivid" },
        [{ start: 0.6, end: 1.0 }],
      ),
    );
    await waitFor(() => expect(onUpdated).toHaveBeenCalled());
  });

  it("closes the editor after a trim, so the stale selection cannot be re-applied", async () => {
    setup();
    await userEvent.click(screen.getByRole("button", { name: /trim by transcript/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Cut “um”/ }));
    await userEvent.click(screen.getByRole("button", { name: /apply cuts/i }));
    await waitFor(() =>
      expect(screen.queryByTestId("transcript-editor-c1")).not.toBeInTheDocument(),
    );
  });

  it("keeps the editor open when the re-render fails, so the edit is not lost", async () => {
    api.rerenderClip.mockRejectedValueOnce(new Error("Re-render exploded"));
    setup();
    await userEvent.click(screen.getByRole("button", { name: /trim by transcript/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Cut “um”/ }));
    await userEvent.click(screen.getByRole("button", { name: /apply cuts/i }));
    expect(await screen.findByText(/re-render exploded/i)).toBeInTheDocument();
    expect(screen.getByTestId("transcript-editor-c1")).toBeInTheDocument();
  });
});

describe("BOXABL campaign publish preflight", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "publishClip").mockResolvedValue({ attempts: [] });
  });

  it("requires every manual checklist item before an approved clip can be uploaded", async () => {
    const user = userEvent.setup();
    setup(
      { ...CLIP, review_state: "approved" },
      {
        campaignProfile: "boxabl_campaign",
        publishing: { platforms: ["youtube"], mode: "review" },
        publisherStatuses: { youtube: { configured: true, message: "Ready" } },
      },
    );

    await user.click(screen.getByRole("button", { name: /upload for review/i }));
    expect(await screen.findByText(/complete every boxabl campaign review checkbox/i)).toBeInTheDocument();
    expect(api.publishClip).not.toHaveBeenCalled();
  });

  it("sends completed BOXABL checks to the API in review mode", async () => {
    const user = userEvent.setup();
    setup(
      { ...CLIP, review_state: "approved" },
      {
        campaignProfile: "boxabl_campaign",
        publishing: { platforms: ["youtube"], mode: "review" },
        publisherStatuses: { youtube: { configured: true, message: "Ready" } },
      },
    );
    const labels = [
      /source link is on the approved/i,
      /complete casita is visible/i,
      /exact boxabl branding/i,
      /strong first-two-second hook/i,
      /captions are readable/i,
      /no kanye west/i,
    ];
    for (const label of labels) {
      await user.click(screen.getByRole("checkbox", { name: label }));
    }
    await user.click(screen.getByRole("button", { name: /upload for review/i }));

    await waitFor(() => expect(api.publishClip).toHaveBeenCalledWith(
      "job1",
      "c1",
      expect.objectContaining({ mode: "review", affiliate_url: "" }),
    ));
    const body = api.publishClip.mock.calls[0][2];
    expect(Object.values(body.campaign_checks)).toHaveLength(6);
    expect(Object.values(body.campaign_checks).every(Boolean)).toBe(true);
  });

  it("clears campaign checks after a re-render so changed framing is reviewed again", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "rerenderClip").mockResolvedValue({ ...CLIP, duration: 6 });
    setup(
      { ...CLIP, review_state: "approved" },
      {
        campaignProfile: "boxabl_campaign",
        publishing: { platforms: ["youtube"], mode: "review" },
        publisherStatuses: { youtube: { configured: true, message: "Ready" } },
      },
    );
    const labels = [
      /source link is on the approved/i,
      /complete casita is visible/i,
      /exact boxabl branding/i,
      /strong first-two-second hook/i,
      /captions are readable/i,
      /no kanye west/i,
    ];
    for (const label of labels) {
      await user.click(screen.getByRole("checkbox", { name: label }));
    }

    await user.click(screen.getByRole("button", { name: /re-render this clip/i }));
    await waitFor(() => expect(api.rerenderClip).toHaveBeenCalled());
    expect(screen.getByRole("checkbox", { name: labels[0] })).not.toBeChecked();
  });

  it("shows which delivery file a publish attempt used", () => {
    setup(CLIP, {
      attempts: [
        {
          id: "attempt-1",
          job_id: "job1",
          clip_id: "c1",
          platform: "youtube",
          state: "queued",
          request_json: {
            delivery: { kind: "variant", filename: "clip_c1_youtube.mp4" },
          },
        },
      ],
    });

    expect(screen.getByText("platform variant")).toBeInTheDocument();
    expect(screen.getByText("platform variant")).toHaveAttribute(
      "title",
      "clip_c1_youtube.mp4",
    );
  });

  it("blocks Instagram API publish so the native account tag is not skipped", async () => {
    setup(
      { ...CLIP, review_state: "approved" },
      {
        campaignProfile: "boxabl_campaign",
        publishing: { platforms: ["instagram"], mode: "review" },
        publisherStatuses: { instagram: { configured: true, message: "Ready" } },
      },
    );
    await userEvent.click(screen.getByRole("button", { name: /upload for review/i }));
    expect(await screen.findByText(/native @boxabl tag/i)).toBeInTheDocument();
    expect(api.publishClip).not.toHaveBeenCalled();
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


describe("saved campaign review checklist", () => {
  const preset = {
    id: "podcast",
    name: "Podcast Launch",
    compliance: {
      required_checks: [{ id: "brand_check", label: "Brand check" }],
    },
    posting: { manual_only: true },
  };

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "publishClip").mockResolvedValue({ attempts: [] });
  });

  it("renders the checks defined by the active saved preset", () => {
    setup(
      { ...CLIP, review_state: "approved" },
      {
        campaignPreset: preset,
        publishing: { platforms: ["youtube"], mode: "review" },
        publisherStatuses: { youtube: { configured: true, message: "Ready" } },
      },
    );

    expect(screen.getByText(/Podcast Launch review/i)).toBeInTheDocument();
    expect(screen.getByText("Brand check")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /brand check/i })).not.toBeChecked();
  });

  it("shows the backend's exact profile blockers instead of a generic failure", async () => {
    const user = userEvent.setup();
    const failure = new Error("Campaign profile compliance failed");
    failure.detail = {
      message: "Campaign profile compliance failed",
      report: {
        profiles: [
          {
            profile_name: "BOXABL Casita",
            results: [
              {
                rule: "length",
                status: "fail",
                message: "clip duration is outside the campaign range",
              },
            ],
          },
        ],
      },
    };
    api.publishClip.mockRejectedValueOnce(failure);

    setup(
      { ...CLIP, review_state: "approved" },
      {
        campaignPreset: preset,
        publishing: { platforms: ["youtube"], mode: "review" },
        publisherStatuses: { youtube: { configured: true, message: "Ready" } },
      },
    );
    await user.click(screen.getByRole("checkbox", { name: /brand check/i }));
    await user.click(screen.getByRole("button", { name: /upload for review/i }));

    expect(await screen.findByText(/clip duration is outside the campaign range/i)).toBeInTheDocument();
  });
});
