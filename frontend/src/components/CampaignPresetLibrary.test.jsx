import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import CampaignPresetLibrary from "./CampaignPresetLibrary.jsx";
import { api } from "../api.js";

const presets = [
  {
    id: "podcast",
    name: "Podcast Launch",
    length: { raw: "20-60 seconds" },
    aspect_resolution: { aspect_ratios: ["9:16"] },
    platforms: ["youtube_shorts"],
    source_library: [],
    posting: { manual_only: false },
  },
  {
    id: "boxabl",
    name: "BOXABL Casita",
    length: { raw: "30-60 seconds" },
    aspect_resolution: { aspect_ratios: ["9:16"] },
    platforms: ["youtube_shorts", "x"],
    source_library: [{ status: "approved" }],
    posting: { manual_only: true },
  },
];

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CampaignPresetLibrary", () => {
  it("lists multiple saved campaigns and applies the chosen one", async () => {
    const user = userEvent.setup();
    const onPresetApplied = vi.fn();
    vi.spyOn(api, "campaignPresets").mockResolvedValue({ presets, selected_id: "podcast" });
    const select = vi.spyOn(api, "selectCampaignPreset").mockResolvedValue({ preset: presets[1] });

    render(<CampaignPresetLibrary onPresetApplied={onPresetApplied} />);

    expect(await screen.findByText("BOXABL Casita")).toBeInTheDocument();
    expect(screen.getByText("Podcast Launch")).toBeInTheDocument();
    expect(screen.getByText("1 approved source")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Apply" }));

    expect(select).toHaveBeenCalledWith("boxabl");
    expect(onPresetApplied).toHaveBeenCalledWith(presets[1]);
    expect(await screen.findByText(/Applied “BOXABL Casita”/i)).toBeInTheDocument();
  });

  it("duplicates a preset into the library without applying it", async () => {
    const user = userEvent.setup();
    const onPresetApplied = vi.fn();
    const copy = { ...presets[0], id: "podcast-copy", name: "Podcast Launch copy" };
    vi.spyOn(api, "campaignPresets").mockResolvedValue({ presets: [presets[0]], selected_id: "" });
    vi.spyOn(api, "duplicateCampaignPreset").mockResolvedValue({ preset: copy });

    render(<CampaignPresetLibrary onPresetApplied={onPresetApplied} />);
    await screen.findByText("Podcast Launch");
    await user.click(screen.getByRole("button", { name: "Duplicate" }));

    await waitFor(() => expect(screen.getByText("Podcast Launch copy")).toBeInTheDocument());
    expect(onPresetApplied).not.toHaveBeenCalled();
  });
});
