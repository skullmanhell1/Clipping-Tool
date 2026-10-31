import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api.js";
import PlatformVariants from "./PlatformVariants.jsx";

const CLIP = {
  id: "c1",
  filename: "clip_c1.mp4",
  variants: [],
};

const PROFILES = [
  {
    name: "tiktok",
    label: "TikTok",
    recommended_aspect: "9:16",
    width: 1080,
    height: 1920,
  },
  {
    name: "youtube",
    label: "YouTube landscape",
    recommended_aspect: "16:9",
    width: 1920,
    height: 1080,
  },
];

const setup = (clip = CLIP, props = {}) => {
  const onUpdated = vi.fn();
  render(
    <PlatformVariants
      jobId="job1"
      clip={clip}
      settings={{ color: "vivid" }}
      outputProfiles={PROFILES}
      onUpdated={onUpdated}
      {...props}
    />,
  );
  return { onUpdated };
};

describe("PlatformVariants", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows destination geometry from the server catalogue", () => {
    setup();
    expect(screen.getByTestId("platform-variants")).toBeInTheDocument();
    expect(screen.getByText("TikTok")).toBeInTheDocument();
    expect(screen.getByText("9:16 · 1080×1920")).toBeInTheDocument();
    expect(screen.getByText("YouTube landscape")).toBeInTheDocument();
    expect(screen.getByText("16:9 · 1920×1080")).toBeInTheDocument();
  });

  it("renders one destination and reports the refreshed clip", async () => {
    const updated = {
      ...CLIP,
      variants: [{ platform: "youtube", filename: "clip_c1_youtube.mp4" }],
    };
    vi.spyOn(api, "renderVariant").mockResolvedValue(updated);
    const { onUpdated } = setup();

    await userEvent.click(screen.getAllByRole("button", { name: "Render" })[1]);

    await waitFor(() =>
      expect(api.renderVariant).toHaveBeenCalledWith("job1", "c1", "youtube", { color: "vivid" }, []),
    );
    await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(updated));
  });

  it("offers both downloads for a rendered variant", () => {
    setup({
      ...CLIP,
      variants: [
        {
          platform: "youtube",
          filename: "clip_c1_youtube.mp4",
          video_url: "clips/job1/clip_c1_youtube.mp4",
        },
      ],
    });

    expect(screen.getByRole("link", { name: "Video" })).toHaveAttribute(
      "href",
      "/api/clips/job1/clip_c1_youtube.mp4/video",
    );
    expect(screen.getByRole("link", { name: "Package" })).toHaveAttribute(
      "href",
      "/api/clips/job1/clip_c1_youtube.mp4/download",
    );
    expect(screen.getByText("1 ready")).toBeInTheDocument();
  });

  it("surfaces a render failure instead of showing a ready variant", async () => {
    vi.spyOn(api, "renderVariant").mockRejectedValueOnce(new Error("source file is gone"));
    setup();

    await userEvent.click(screen.getAllByRole("button", { name: "Render" })[0]);

    expect(await screen.findByRole("alert")).toHaveTextContent("source file is gone");
    expect(screen.getByText("0 ready")).toBeInTheDocument();
  });

  it("does not appear when the server advertises no destinations", () => {
    render(
      <PlatformVariants
        jobId="job1"
        clip={CLIP}
        settings={{}}
        outputProfiles={[]}
      />,
    );
    expect(screen.queryByTestId("platform-variants")).not.toBeInTheDocument();
  });
});
