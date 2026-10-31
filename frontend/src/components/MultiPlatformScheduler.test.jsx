import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api.js";
import MultiPlatformScheduler from "./MultiPlatformScheduler.jsx";

const props = {
  jobs: [
    {
      id: "job-1",
      status: "completed",
      title: "Episode one",
      clips: [{ id: "clip-1", title: "The good bit", duration: 30 }],
    },
  ],
  campaigns: [
    {
      id: "campaign-1",
      name: "Main campaign",
      routes: {
        tiktok: { account_id: "tt-account" },
        youtube: { account_id: "yt-account" },
        instagram: { account_id: "ig-account" },
      },
    },
  ],
  publisherStatuses: {
    tiktok: { configured: true, direct_publish: true },
    youtube: { configured: true, direct_publish: true },
    instagram: { configured: true, direct_publish: true },
  },
  outputProfiles: [
    { name: "tiktok", label: "TikTok", width: 1080, height: 1920, max_duration_s: 300 },
    {
      name: "youtube_shorts",
      label: "YouTube Shorts",
      width: 1080,
      height: 1920,
      max_duration_s: 180,
    },
    { name: "instagram", label: "Instagram Reels", width: 1080, height: 1920, max_duration_s: 90 },
  ],
  settings: { color: "vivid" },
  onClipUpdated: vi.fn(),
  onError: vi.fn(),
  onPublished: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(api, "deliveryReadiness").mockResolvedValue({
    destinations: [],
    summary: { ready: 0, fallback: 0, blocked: 0 },
  });
});

afterEach(() => vi.restoreAllMocks());

describe("MultiPlatformScheduler", () => {
  it("shows a useful empty state before any clip is completed", () => {
    render(<MultiPlatformScheduler {...props} jobs={[]} />);

    expect(screen.getByText(/complete a clip job first/i)).toBeInTheDocument();
  });

  it("marks a selected destination as using its rendered variant", async () => {
    render(
      <MultiPlatformScheduler
        {...props}
        jobs={[
          {
            ...props.jobs[0],
            clips: [
              {
                ...props.jobs[0].clips[0],
                variants: [{ platform: "tiktok", filename: "clip_tiktok.mp4" }],
              },
            ],
          },
        ]}
      />,
    );

    await userEvent.click(screen.getAllByRole("checkbox")[0]);

    expect(screen.getByText(/TikTok: 1080×1920.*variant ready/)).toBeInTheDocument();
  });

  it("shows a fallback and renders the missing variant without leaving the scheduler", async () => {
    const fallback = {
      destinations: [
        {
          requested_platform: "tiktok",
          label: "TikTok",
          status: "fallback",
          filename: "clip-1.mp4",
          width: null,
          height: null,
          blockers: [],
          warnings: ["No TikTok variant is rendered; the canonical clip will be sent."],
          action: "render_variant",
        },
      ],
      summary: { ready: 0, fallback: 1, blocked: 0 },
    };
    api.deliveryReadiness.mockResolvedValue(fallback);
    const updated = {
      ...props.jobs[0].clips[0],
      variants: [{ platform: "tiktok", filename: "clip-1_tiktok.mp4" }],
    };
    vi.spyOn(api, "renderVariant").mockResolvedValue(updated);
    render(<MultiPlatformScheduler {...props} />);

    await userEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(await screen.findByText("Canonical fallback")).toBeInTheDocument();
    expect(
      screen.getByText(/No TikTok variant is rendered/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /render tiktok variant/i }));

    await waitFor(() =>
      expect(api.renderVariant).toHaveBeenCalledWith(
        "job-1",
        "clip-1",
        "tiktok",
        { color: "vivid" },
        [],
      ),
    );
    expect(props.onClipUpdated).toHaveBeenCalledWith("job-1", updated);
  });

  it("does not queue while a selected destination is blocked", async () => {
    api.deliveryReadiness.mockResolvedValue({
      destinations: [
        {
          requested_platform: "tiktok",
          label: "TikTok",
          status: "blocked",
          blockers: ["TikTok is not configured"],
          warnings: [],
          action: "configure_publisher",
        },
      ],
      summary: { ready: 0, fallback: 0, blocked: 1 },
    });
    render(<MultiPlatformScheduler {...props} />);

    await userEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(await screen.findByText("Blocked")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resolve delivery blockers" })).toBeDisabled();
  });

  it("queues one selected clip for multiple direct platforms", async () => {
    vi.spyOn(api, "publishClip").mockResolvedValue({ attempts: [] });
    render(<MultiPlatformScheduler {...props} />);

    const platforms = screen.getAllByRole("checkbox");
    await userEvent.click(platforms[0]);
    await userEvent.click(platforms[1]);
    await userEvent.selectOptions(screen.getByLabelText(/campaign \/ routing/i), "campaign-1");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Publish selected" })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Publish selected" }));

    await waitFor(() => expect(api.publishClip).toHaveBeenCalledTimes(1));
    expect(api.publishClip).toHaveBeenCalledWith(
      "job-1",
      "clip-1",
      expect.objectContaining({
        platforms: ["tiktok", "youtube_shorts"],
        campaign_id: "campaign-1",
        mode: "auto",
        schedule_at: null,
        routes: expect.objectContaining({
          tiktok: { account_id: "tt-account" },
          youtube: { account_id: "yt-account" },
          youtube_shorts: { account_id: "yt-account" },
        }),
      }),
    );
    expect(await screen.findByText(/2 platform posts queued now/i)).toBeInTheDocument();
  });
});
