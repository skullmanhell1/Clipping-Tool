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
      screen.getByLabelText("Google Doc link"),
      "https://docs.google.com/document/d/brief-12345/edit",
    );
    await user.click(screen.getByRole("button", { name: "Fetch and review preset" }));

    await waitFor(() => expect(parse).toHaveBeenCalledWith("https://docs.google.com/document/d/brief-12345/edit"));
    expect(await screen.findByText("Spring Launch")).toBeInTheDocument();
    expect(onPresetApplied).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Apply preset to job options" }));
    expect(onPresetApplied).toHaveBeenCalledWith(preset);
    expect(saveCampaign).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("No publish campaign was created or saved");
  });

  it("shows Drive configuration or access errors while keeping the paste fallback visible", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "parseCampaignPresetFromDrive").mockRejectedValue(
      new Error("Google Drive import is not configured on the server. Use the paste fallback."),
    );

    renderPanel({ driveConfigured: false });
    await user.type(screen.getByLabelText("Google Doc link"), "https://drive.google.com/file/d/brief-12345/view");
    await user.click(screen.getByRole("button", { name: "Fetch and review preset" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/not configured/i);
    await user.click(screen.getByRole("button", { name: /Use pasted brief instead/i }));
    expect(screen.getByLabelText("Pasted campaign brief fallback")).toBeInTheDocument();
  });
});
