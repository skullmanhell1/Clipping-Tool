import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App.jsx";
import { api } from "./api.js";

function mockStartup(jobs = []) {
  vi.spyOn(api, "info").mockResolvedValue({
    version: "test",
    llm_available: false,
    effects: {},
    engines: [],
    capabilities: {},
  });
  vi.spyOn(api, "watchStatus").mockResolvedValue({ enabled: false, folder: "" });
  vi.spyOn(api, "updates").mockResolvedValue({});
  vi.spyOn(api, "publisherStatuses").mockResolvedValue({ platforms: {} });
  vi.spyOn(api, "campaigns").mockResolvedValue({ campaigns: [] });
  vi.spyOn(api, "campaignPresets").mockResolvedValue({ presets: [], selected_id: null });
  vi.spyOn(api, "history").mockResolvedValue({ publish_attempts: [] });
  vi.spyOn(api, "profiles").mockResolvedValue({ profiles: [], default_id: null });
  vi.spyOn(api, "listJobs").mockResolvedValue({ jobs });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("creator studio workflow", () => {
  it("restores recent projects when the studio opens", async () => {
    mockStartup([
      {
        id: "restored-job",
        status: "completed",
        title: "The saved interview",
        source: "interview.mp4",
        input_type: "file",
        progress: 1,
        clips: [],
      },
    ]);

    render(<App />);

    expect(await screen.findByText("The saved interview")).toBeInTheDocument();
    expect(screen.getByText("Recent work")).toBeInTheDocument();
    expect(api.listJobs).toHaveBeenCalled();
  });

  it("applies a platform format preset accessibly", async () => {
    const user = userEvent.setup();
    mockStartup();
    render(<App />);

    const preset = screen.getByRole("button", { name: /TikTok Vertical · 9:16/ });
    await user.click(preset);

    expect(preset).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Aspect Ratio")).toHaveValue("9:16");
  });

  it("puts campaign tools on their own clear navigation page", async () => {
    const user = userEvent.setup();
    mockStartup();
    render(<App />);

    expect(screen.queryByRole("heading", { name: "Campaign brief" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Campaigns" }));

    expect(await screen.findByRole("heading", { name: "Campaign brief" })).toBeInTheDocument();
    expect(screen.getByText("Save settings to reuse")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Import & compliance" })).toBeInTheDocument();
  });

  it("keeps optional video tools closed until they are requested", async () => {
    const user = userEvent.setup();
    mockStartup();
    render(<App />);

    const moreTools = screen.getByRole("button", { name: /More video tools/ });
    expect(moreTools).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Enable face tracking")).not.toBeInTheDocument();

    await user.click(moreTools);
    expect(moreTools).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("Enable face tracking")).toBeInTheDocument();
    expect(screen.queryByLabelText("Face-tracking reframe")).not.toBeInTheDocument();
  });

  it("applies the brief duration and a supported metadata platform", async () => {
    const user = userEvent.setup();
    mockStartup();
    vi.spyOn(api, "parseCampaignPresetFromDrive").mockResolvedValue({
      preset: {
        name: "Solved",
        allowed_platforms: ["youtube_shorts", "instagram"],
        aspect_ratios: ["9:16"],
        brief_rules: { clip_duration_seconds: { min: 20, max: 60 } },
      },
      provenance: { source_type: "google_docs" },
      sync: { status: "synced" },
    });

    render(<App />);
    await user.click(screen.getByRole("button", { name: "Campaigns" }));
    await user.type(
      screen.getByLabelText("Google Doc or Drive link"),
      "https://docs.google.com/document/d/solved-brief/edit",
    );
    await user.click(screen.getByRole("button", { name: "Load and review brief" }));
    await user.click(await screen.findByRole("button", { name: "Apply / replace campaign preset" }));
    await user.click(screen.getByRole("button", { name: "Make clips" }));

    expect(screen.getByLabelText("Clip Length")).toHaveValue("20-60s");
    await user.click(screen.getByRole("button", { name: /More clip settings/ }));
    expect(screen.getByLabelText("Platform")).toHaveValue("youtube");
  });

  it("replacing a brief clears settings that the new brief does not specify", async () => {
    const user = userEvent.setup();
    mockStartup();
    vi.spyOn(api, "parseCampaignPresetFromDrive")
      .mockResolvedValueOnce({
        preset: {
          name: "Old campaign",
          allowed_platforms: ["tiktok"],
          aspect_ratios: ["1:1"],
          branding: { logo: "old-logo.png", font: "Old Font", colors: { primary: "#123456", accent: "#abcdef" } },
          cta: "Old call to action",
          caption_style: "boxed",
          brief_rules: { clip_duration_seconds: { min: 60, max: 90 } },
        },
        provenance: { source_type: "google_docs" },
        sync: { status: "synced" },
      })
      .mockResolvedValueOnce({
        preset: {
          name: "New campaign",
          allowed_platforms: [],
          aspect_ratios: [],
          branding: { logo: "", font: "", colors: {} },
          cta: "",
          caption_style: "",
          brief_rules: {},
        },
        provenance: { source_type: "google_docs" },
        sync: { status: "synced" },
      });

    render(<App />);
    await user.click(screen.getByRole("button", { name: "Campaigns" }));
    await user.type(
      screen.getByLabelText("Google Doc or Drive link"),
      "https://docs.google.com/document/d/old-brief/edit",
    );
    await user.click(screen.getByRole("button", { name: "Load and review brief" }));
    await user.click(await screen.findByRole("button", { name: "Apply / replace campaign preset" }));

    await user.click(screen.getByRole("button", { name: "Load a different brief" }));
    await user.type(
      screen.getByLabelText("Google Doc or Drive link"),
      "https://docs.google.com/document/d/new-brief/edit",
    );
    await user.click(screen.getByRole("button", { name: "Load and review brief" }));
    await user.click(await screen.findByRole("button", { name: "Apply / replace campaign preset" }));
    await user.click(screen.getByRole("button", { name: "Make clips" }));

    expect(screen.getByLabelText("Clip Length")).toHaveValue("auto");
    expect(screen.getByLabelText("Aspect Ratio")).toHaveValue("9:16");
    await user.click(screen.getByRole("button", { name: /More clip settings/ }));
    expect(screen.getByLabelText("Platform")).toHaveValue("generic");
  });

  it("submits uploaded videos and pasted links together", async () => {
    const user = userEvent.setup();
    mockStartup();
    const upload = vi.spyOn(api, "upload").mockResolvedValue({
      jobs: [{ id: "upload-job", status: "queued", title: "Uploaded video", source: "upload.mp4", input_type: "file", progress: 0, clips: [] }],
    });
    const submitUrl = vi.spyOn(api, "submitUrl").mockResolvedValue({
      id: "url-job", status: "queued", title: "Linked video", source: "https://video.example/1", input_type: "url", progress: 0, clips: [],
    });

    render(<App />);
    await user.type(screen.getByLabelText("Video link"), "https://video.example/1");
    await user.upload(
      screen.getByLabelText("Choose video files"),
      new File(["video"], "upload.mp4", { type: "video/mp4" }),
    );
    await user.click(screen.getByRole("button", { name: "Generate clips" }));

    await waitFor(() => {
      expect(upload).toHaveBeenCalledTimes(1);
      expect(submitUrl).toHaveBeenCalledWith(
        "https://video.example/1",
        expect.objectContaining({ aspect: "9:16", new_project: false }),
      );
    });
  });
});
