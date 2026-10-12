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
        expect.objectContaining({ aspect: "9:16" }),
      );
    });
  });
});
