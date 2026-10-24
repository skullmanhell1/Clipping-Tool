import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

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
  onError: vi.fn(),
  onPublished: vi.fn(),
};

afterEach(() => vi.restoreAllMocks());

describe("MultiPlatformScheduler", () => {
  it("shows a useful empty state before any clip is completed", () => {
    render(<MultiPlatformScheduler {...props} jobs={[]} />);

    expect(screen.getByText(/complete a clip job first/i)).toBeInTheDocument();
  });

  it("queues one selected clip for multiple direct platforms", async () => {
    vi.spyOn(api, "publishClip").mockResolvedValue({ attempts: [] });
    render(<MultiPlatformScheduler {...props} />);

    const platforms = screen.getAllByRole("checkbox");
    await userEvent.click(platforms[0]);
    await userEvent.click(platforms[1]);
    await userEvent.selectOptions(screen.getByLabelText(/campaign \/ routing/i), "campaign-1");
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
