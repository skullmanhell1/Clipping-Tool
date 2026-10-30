import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import CampaignWorkspacePanel from "./CampaignWorkspacePanel.jsx";
import { api } from "../api.js";

const profile = {
  id: "profile-1",
  name: "Whop launch",
  aspect_resolution: { aspect_ratios: ["9:16"] },
  platforms: ["youtube_shorts"],
  branding: { logo: "logo.png", font: "Inter", colors: { primary: "#7c3aed" } },
  video_links: ["https://youtu.be/abc123"],
  captions_language: { required: true, style: "karaoke" },
  copy_rules: { hashtags: ["#launch"], required_copy: ["Shop now"], cta: "Learn more" },
  banned_words_topics: { words: ["guaranteed"], topics: [] },
  watermark_end_cards: { watermark: "logo.png" },
  source_rules: { allowed: [], prohibited: [], video_links: ["https://youtu.be/abc123"] },
  ambiguities: [],
  uncertainties: [],
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CampaignWorkspacePanel", () => {
  it("imports a pasted brief once, shows extracted links, and applies all rules", async () => {
    const user = userEvent.setup();
    const onPresetApplied = vi.fn();
    const onSourcesExtracted = vi.fn();
    vi.spyOn(api, "campaignProfiles").mockResolvedValue({ profiles: [], selected_id: "" });
    vi.spyOn(api, "parseCampaignProfile").mockResolvedValue({ profile, status: "offline_paste" });

    render(<CampaignWorkspacePanel onPresetApplied={onPresetApplied} onSourcesExtracted={onSourcesExtracted} />);
    await user.type(screen.getByLabelText("Campaign brief or source links"), "Campaign: Whop launch");
    await user.click(screen.getByRole("button", { name: "Read brief" }));

    expect(await screen.findByText("https://youtu.be/abc123", { selector: "li" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply all rules to clipping" }));

    await waitFor(() => expect(onPresetApplied).toHaveBeenCalledWith(expect.objectContaining({
      aspect_ratios: ["9:16"],
      branding: expect.objectContaining({ logo: "logo.png" }),
      video_links: ["https://youtu.be/abc123"],
    })));
    expect(onSourcesExtracted).toHaveBeenCalledWith(["https://youtu.be/abc123"]);
    expect(screen.getByRole("status")).toHaveTextContent(/applied to clipping settings/i);
  });
});
