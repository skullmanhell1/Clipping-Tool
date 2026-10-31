import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import CampaignAutomationPanel from "./CampaignAutomationPanel.jsx";
import { api } from "../api.js";

const preset = {
  name: "Spring Launch",
  aspect_ratios: ["9:16"],
  hashtags: ["#launch"],
  daily_clip_target: 2,
  require_public_visibility: true,
  pattern_interrupter_interval: 5,
  branding: { logo: "", font: "", colors: {} },
  caption_style: "",
  cta: "",
  allowed_platforms: [],
  hook_settings: { enabled: false, duration_seconds: 3, style: "" },
  cliffhanger_settings: { enabled: false, duration_seconds: 2, style: "" },
  brief_rules: {
    clip_duration_seconds: { min: 20, max: 60 },
    face_visible_within_seconds: 5,
    face_subject: "Mark",
    payout: { cpm_usd: 1.25, maximum_per_clip_usd: 300, minimum_payout_usd: 2 },
    subtitles_required: true,
    required_mentions: {
      common: ["@solvedpodcast", "@markmanson"],
      by_platform: { youtube_shorts: ["@solvedpodcast", "@iammarkmanson"] },
    },
  },
};

const renderPanel = (props = {}) =>
  render(
    <CampaignAutomationPanel
      campaigns={[]}
      publisherStatuses={{}}
      jobs={[]}
      {...props}
    />,
  );

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CampaignAutomationPanel Google Doc import", () => {
  it("loads a Drive preset for review, then applies only after explicit confirmation", async () => {
    const user = userEvent.setup();
    const onPresetApplied = vi.fn();
    const saveCampaign = vi.spyOn(api, "saveCampaign");
    const parse = vi.spyOn(api, "parseCampaignPresetFromDrive").mockResolvedValue({
      preset,
      requires_review: true,
      provenance: { source_type: "google_docs", file_id: "brief-12345" },
      sync: { status: "synced", parser: "deterministic" },
    });

    renderPanel({ driveConfigured: true, onPresetApplied });
    await user.type(
      screen.getByLabelText("Google Doc or Drive link"),
      "https://docs.google.com/document/d/brief-12345/edit",
    );
    await user.click(screen.getByRole("button", { name: "Load and review brief" }));

    await waitFor(() => expect(parse).toHaveBeenCalledWith("https://docs.google.com/document/d/brief-12345/edit"));
    expect(await screen.findByText("Spring Launch")).toBeInTheDocument();
    expect(screen.getByLabelText("Parsed brief rules")).toHaveTextContent("20–60 seconds");
    expect(screen.getByLabelText("Parsed brief rules")).toHaveTextContent("Mark's face appears in the first 5 seconds");
    expect(screen.getByLabelText("Parsed brief rules")).toHaveTextContent("$1.25 per 1,000 views");
    expect(screen.getByLabelText("Parsed brief rules")).toHaveTextContent("@iammarkmanson");
    expect(onPresetApplied).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Apply / replace campaign preset" }));
    expect(onPresetApplied).toHaveBeenCalledWith(preset);
    expect(saveCampaign).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("No publish campaign was created or saved");
  });

  it("can load a different link and replaces the prior parsed brief", async () => {
    const user = userEvent.setup();
    const replacement = { ...preset, name: "New Campaign", brief_rules: { subtitles_required: true } };
    const parse = vi.spyOn(api, "parseCampaignPresetFromDrive")
      .mockResolvedValueOnce({ preset, provenance: { source_type: "google_docs" }, sync: { status: "synced" } })
      .mockResolvedValueOnce({ preset: replacement, provenance: { source_type: "google_docs" }, sync: { status: "synced" } });

    renderPanel({ driveConfigured: true });
    const link = screen.getByLabelText("Google Doc or Drive link");
    await user.type(link, "https://docs.google.com/document/d/brief-one/edit");
    await user.click(screen.getByRole("button", { name: "Load and review brief" }));
    expect(await screen.findByText("Spring Launch")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load a different brief" }));
    await user.type(screen.getByLabelText("Google Doc or Drive link"), "https://docs.google.com/document/d/brief-two/edit");
    await user.click(screen.getByRole("button", { name: "Load and review brief" }));

    expect(await screen.findByText("New Campaign")).toBeInTheDocument();
    expect(screen.queryByText("Spring Launch")).not.toBeInTheDocument();
    expect(parse).toHaveBeenNthCalledWith(2, "https://docs.google.com/document/d/brief-two/edit");
  });

  it("switches to paste mode after a Drive access failure and clears an unreviewed preset", async () => {
    const user = userEvent.setup();
    const driveError = new Error("The Google link is private or access was denied.");
    driveError.detail = { fall_back_to_paste: true };
    vi.spyOn(api, "parseCampaignPresetFromDrive").mockRejectedValue(driveError);

    renderPanel({ driveConfigured: false });
    await user.type(screen.getByLabelText("Google Doc or Drive link"), "https://drive.google.com/file/d/brief-12345/view");
    await user.click(screen.getByRole("button", { name: "Load and review brief" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/private/i);
    expect(screen.getByLabelText("Pasted campaign brief fallback")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/paste the brief text/i);
    expect(screen.queryByText("Spring Launch")).not.toBeInTheDocument();
  });
});


  it("saves a reviewed brief as a reusable campaign preset and refreshes the library", async () => {
    const user = userEvent.setup();
    const onPresetApplied = vi.fn();
    const onPresetSaved = vi.fn();
    vi.spyOn(api, "parseCampaignPresetFromDrive").mockResolvedValue({
      preset,
      provenance: { source_type: "google_docs" },
      sync: { status: "synced" },
    });
    const importPreset = vi.spyOn(api, "importCampaignPreset").mockResolvedValue({
      preset: { ...preset, id: "", settings: { clip_length: "20-60s" } },
    });
    const savePreset = vi.spyOn(api, "saveCampaignPreset").mockResolvedValue({
      preset: { ...preset, id: "saved-1", name: "Spring Launch saved" },
    });

    renderPanel({ driveConfigured: true, onPresetApplied, onPresetSaved });
    await user.type(
      screen.getByLabelText("Google Doc or Drive link"),
      "https://docs.google.com/document/d/brief-12345/edit",
    );
    await user.click(screen.getByRole("button", { name: "Load and review brief" }));
    await screen.findByText("Spring Launch");

    const name = screen.getByLabelText("Campaign preset name");
    await user.clear(name);
    await user.type(name, "Spring Launch saved");
    await user.click(screen.getByRole("button", { name: "Save as campaign preset" }));

    expect(importPreset).toHaveBeenCalledWith({
      urls: ["https://docs.google.com/document/d/brief-12345/edit"],
    });
    expect(savePreset).toHaveBeenCalledWith(
      expect.objectContaining({ name: "Spring Launch saved" }),
      true,
    );
    expect(onPresetApplied).toHaveBeenCalledWith(
      expect.objectContaining({ id: "saved-1", name: "Spring Launch saved" }),
    );
    expect(onPresetSaved).toHaveBeenCalled();
  });
