// App's own decisions: which endpoint a submission goes to, the two guards in front of it, which
// jobs are shown, and how state survives a failure.
//
// Everything else in App is composition, and the child components have their own tests. What is
// only testable here is the orchestration — and it is where the consequences are largest, because
// picking the wrong submit endpoint or filtering the job list wrongly produces an app that looks
// like it is working.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App.jsx";
import { api } from "./api.js";

const job = (overrides = {}) => ({
  id: "j1",
  status: "processing",
  progress: 0,
  stage: "Analyzing",
  clips: [],
  source: "https://a.test/1",
  ...overrides,
});

/** Every endpoint App touches on mount, stubbed to a quiet, empty install. */
function stubApi() {
  vi.spyOn(api, "watchStatus").mockResolvedValue({ enabled: false, folder: "" });
  vi.spyOn(api, "info").mockResolvedValue({ version: "1.0.0", engines: [] });
  vi.spyOn(api, "updates").mockResolvedValue({ update_available: false });
  vi.spyOn(api, "publisherStatuses").mockResolvedValue({ platforms: {} });
  vi.spyOn(api, "campaigns").mockResolvedValue({ campaigns: [] });
  vi.spyOn(api, "history").mockResolvedValue({ clips: [], publish_attempts: [] });
  vi.spyOn(api, "profiles").mockResolvedValue({ profiles: [], default_id: null });
  vi.spyOn(api, "listJobs").mockResolvedValue({ jobs: [] });
  vi.spyOn(api, "storage").mockResolvedValue(null);
  vi.spyOn(api, "preview").mockResolvedValue({ title: "T", duration: 10 });
  vi.spyOn(api, "upload").mockResolvedValue({ jobs: [job({ id: "up1" })] });
  vi.spyOn(api, "submitUrl").mockResolvedValue(job({ id: "url1" }));
  vi.spyOn(api, "submitBatch").mockResolvedValue({
    jobs: [job({ id: "b1" }), job({ id: "b2" })],
  });
  vi.spyOn(api, "watchToggle").mockResolvedValue({ enabled: true, folder: "/inbox" });
}

beforeEach(stubApi);
afterEach(() => vi.restoreAllMocks());

const mounted = () => waitFor(() => expect(api.profiles).toHaveBeenCalled());
// Addressed by placeholder, not by role: App renders the settings and publishing panels too, so
// `getByRole("textbox")` is ambiguous here in a way it was not in InputBar's own tests.
const urlBox = () => screen.getByPlaceholderText(/Paste a video URL/i);
const getClips = () => screen.getByRole("button", { name: /Get Clips/i });
const videoFile = (name = "a.mp4") => new File(["x"], name, { type: "video/mp4" });

describe("choosing the submit endpoint", () => {
  it("uploads when files are selected", async () => {
    render(<App />);
    await mounted();
    await userEvent.upload(document.querySelector('input[type="file"]'), [videoFile()]);
    await userEvent.click(getClips());
    await waitFor(() => expect(api.upload).toHaveBeenCalled());
    expect(api.submitUrl).not.toHaveBeenCalled();
    expect(api.submitBatch).not.toHaveBeenCalled();
  });

  it("prefers the upload even when a URL is also present", async () => {
    // Files win: they are the more expensive thing the user did, and submitting the URL instead
    // would silently ignore the file they picked.
    render(<App />);
    await mounted();
    await userEvent.type(urlBox(), "https://a.test/1");
    await userEvent.upload(document.querySelector('input[type="file"]'), [videoFile()]);
    await userEvent.click(getClips());
    await waitFor(() => expect(api.upload).toHaveBeenCalled());
    expect(api.submitUrl).not.toHaveBeenCalled();
  });

  it("uses the single-URL endpoint for exactly one URL", async () => {
    render(<App />);
    await mounted();
    await userEvent.type(urlBox(), "https://a.test/1");
    await userEvent.click(getClips());
    await waitFor(() =>
      expect(api.submitUrl).toHaveBeenCalledWith("https://a.test/1", expect.any(Object)),
    );
    expect(api.submitBatch).not.toHaveBeenCalled();
  });

  it("uses the batch endpoint for more than one URL", async () => {
    render(<App />);
    await mounted();
    await userEvent.type(urlBox(), "https://a.test/1 https://a.test/2");
    await userEvent.click(getClips());
    await waitFor(() =>
      expect(api.submitBatch).toHaveBeenCalledWith(
        ["https://a.test/1", "https://a.test/2"],
        expect.any(Object),
      ),
    );
    expect(api.submitUrl).not.toHaveBeenCalled();
  });

  it("sends the settings payload, not the raw UI state", async () => {
    // The API takes snake_case option names; sending the panel's own shape would be silently
    // ignored field by field.
    render(<App />);
    await mounted();
    await userEvent.type(urlBox(), "https://a.test/1");
    await userEvent.click(getClips());
    await waitFor(() => expect(api.submitUrl).toHaveBeenCalled());
    const options = api.submitUrl.mock.calls[0][1];
    expect(options).toHaveProperty("aspect", "9:16");
    expect(options).toHaveProperty("publish_mode", "review");
    expect(options).toHaveProperty("translate", false);
  });
});

describe("the guards in front of submission", () => {
  it("refuses with nothing to work on, and says so", async () => {
    render(<App />);
    await mounted();
    await userEvent.click(getClips());
    expect(await screen.findByText(/Add a video URL or upload a file first/)).toBeInTheDocument();
    expect(api.submitUrl).not.toHaveBeenCalled();
  });

  it("refuses auto publishing without a campaign", async () => {
    // Auto mode needs a routing target per platform, and a campaign is the only thing that
    // carries one. Submitting would queue publishes with nowhere to go.
    api.publisherStatuses.mockResolvedValue({
      platforms: { tiktok: { configured: true, direct_publish: true, message: "" } },
    });
    render(<App />);
    await mounted();
    await userEvent.type(urlBox(), "https://a.test/1");
    await userEvent.click(screen.getByRole("button", { name: /Publishing settings/ }));
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "auto");
    await userEvent.click(await screen.findByRole("checkbox", { name: /tiktok/i }));
    await userEvent.click(getClips());
    expect(
      await screen.findByText(/Auto publishing requires a saved campaign/),
    ).toBeInTheDocument();
    expect(api.submitUrl).not.toHaveBeenCalled();
  });

  it("allows auto publishing with no platforms selected", async () => {
    // Nothing is being published, so there is no routing to resolve; blocking here would stop a
    // plain "auto" job that publishes nowhere.
    render(<App />);
    await mounted();
    await userEvent.type(urlBox(), "https://a.test/1");
    await userEvent.click(screen.getByRole("button", { name: /Publishing settings/ }));
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "auto");
    await userEvent.click(getClips());
    await waitFor(() => expect(api.submitUrl).toHaveBeenCalled());
  });

  it("clears a previous error when a new submission starts", async () => {
    render(<App />);
    await mounted();
    await userEvent.click(getClips());
    expect(await screen.findByText(/Add a video URL/)).toBeInTheDocument();
    await userEvent.type(urlBox(), "https://a.test/1");
    await userEvent.click(getClips());
    await waitFor(() => expect(screen.queryByText(/Add a video URL/)).toBeNull());
  });
});

describe("submission failure", () => {
  it("shows the server's message", async () => {
    api.submitUrl.mockRejectedValue(new Error("unsupported site"));
    render(<App />);
    await mounted();
    await userEvent.type(urlBox(), "https://a.test/1");
    await userEvent.click(getClips());
    expect(await screen.findByText("unsupported site")).toBeInTheDocument();
  });

  it("re-enables the button, so a transient failure is retryable", async () => {
    // The `finally` is what guarantees this; without it one failed submit disables the primary
    // action until reload.
    api.submitUrl.mockRejectedValue(new Error("boom"));
    render(<App />);
    await mounted();
    await userEvent.type(urlBox(), "https://a.test/1");
    await userEvent.click(getClips());
    await waitFor(() => expect(getClips()).toBeEnabled());
  });

  it("falls back to a generic message when the error has none", async () => {
    api.submitUrl.mockRejectedValue(new Error(""));
    render(<App />);
    await mounted();
    await userEvent.type(urlBox(), "https://a.test/1");
    await userEvent.click(getClips());
    expect(await screen.findByText("Failed to submit.")).toBeInTheDocument();
  });
});

describe("which jobs are shown", () => {
  it("shows a job this session created", async () => {
    // `listJobs` has to return it too: the optimistic insert is replaced by the next poll, so a
    // stub that kept answering "no jobs" would make the card vanish a second later.
    api.listJobs.mockResolvedValue({ jobs: [job({ id: "url1" })] });
    render(<App />);
    await mounted();
    await userEvent.type(urlBox(), "https://a.test/1");
    await userEvent.click(getClips());
    expect(await screen.findByRole("heading", { name: "Jobs" })).toBeInTheDocument();
  });

  it("hides jobs this session did not create", async () => {
    // The list is per-session, not a global queue: showing every job on the server would bury the
    // one the user just started under someone else's history.
    api.listJobs.mockResolvedValue({ jobs: [job({ id: "someone-else" })] });
    render(<App />);
    await mounted();
    await waitFor(() => expect(api.profiles).toHaveBeenCalled());
    expect(screen.queryByText("Jobs")).toBeNull();
  });

  it("shows watch-folder jobs it did not create, because nothing else would", async () => {
    // A watch-folder drop has no submitting session, so the source path is the only thing that
    // marks it as belonging here.
    api.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    api.listJobs.mockResolvedValue({
      jobs: [job({ id: "w1", source: "/data/watch/clip.mp4" })],
    });
    render(<App />);
    await waitFor(() => expect(screen.getByText(/watch-folder active/)).toBeInTheDocument());
  });
});

describe("views", () => {
  it("opens on Create", async () => {
    render(<App />);
    await mounted();
    expect(getClips()).toBeInTheDocument();
  });

  it("switches to History and leaves Create behind", async () => {
    render(<App />);
    await mounted();
    await userEvent.click(screen.getByRole("button", { name: "History" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: /Get Clips/i })).toBeNull());
    expect(await screen.findByRole("heading", { name: "History" })).toBeInTheDocument();
  });

  it("switches to Schedule and mounts the calendar", async () => {
    vi.spyOn(api, "schedule").mockResolvedValue({ attempts: [] });
    render(<App />);
    await mounted();
    await userEvent.click(screen.getByRole("button", { name: "Schedule" }));
    await waitFor(() => expect(screen.getByLabelText("Previous month")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Get Clips/i })).toBeNull();
  });

  it("shows the version on the Settings view", async () => {
    render(<App />);
    await mounted();
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    // Twice on this view: once in the header, once in the "Version & updates" card.
    await waitFor(() => expect(screen.getAllByText(/v1\.0\.0/).length).toBeGreaterThan(1));
  });
});

describe("the update banner", () => {
  it("stays hidden when up to date", async () => {
    render(<App />);
    await mounted();
    expect(screen.queryByText(/Update available/)).toBeNull();
  });

  it("names both versions and links to the notes when an update exists", async () => {
    api.updates.mockResolvedValue({
      update_available: true,
      latest: "2.0.0",
      current: "1.0.0",
      html_url: "https://github.test/releases/2.0.0",
    });
    render(<App />);
    expect(await screen.findByText(/v2\.0\.0 is out/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Release notes" })).toHaveAttribute(
      "href",
      "https://github.test/releases/2.0.0",
    );
  });

  it("survives an update check that fails", async () => {
    // Not being able to check for updates must not stop the app rendering.
    api.updates.mockRejectedValue(new Error("offline"));
    render(<App />);
    await mounted();
    expect(getClips()).toBeInTheDocument();
  });
});

describe("the watch folder", () => {
  it("sends the current options with the toggle, so drops use what is on screen", async () => {
    render(<App />);
    await mounted();
    await userEvent.click(screen.getByRole("checkbox", { name: /Watch-folder mode/i }));
    await waitFor(() => expect(api.watchToggle).toHaveBeenCalled());
    const [enabled, options] = api.watchToggle.mock.calls[0];
    expect(enabled).toBe(true);
    expect(options).toHaveProperty("aspect", "9:16");
  });

  it("reports a toggle failure", async () => {
    api.watchToggle.mockRejectedValue(new Error("folder not writable"));
    render(<App />);
    await mounted();
    await userEvent.click(screen.getByRole("checkbox", { name: /Watch-folder mode/i }));
    expect(await screen.findByText("folder not writable")).toBeInTheDocument();
  });
});

describe("startup resilience", () => {
  it("renders when every optional probe fails", async () => {
    // /api/info, /api/updates, /api/watch, the publishing trio and the profile list are all
    // optional. The app must still be able to submit a job with none of them.
    api.info.mockRejectedValue(new Error("x"));
    api.updates.mockRejectedValue(new Error("x"));
    api.watchStatus.mockRejectedValue(new Error("x"));
    api.publisherStatuses.mockRejectedValue(new Error("x"));
    api.campaigns.mockRejectedValue(new Error("x"));
    api.history.mockRejectedValue(new Error("x"));
    api.profiles.mockRejectedValue(new Error("x"));
    render(<App />);
    await waitFor(() => expect(api.info).toHaveBeenCalled());
    await userEvent.type(urlBox(), "https://a.test/1");
    await userEvent.click(getClips());
    await waitFor(() => expect(api.submitUrl).toHaveBeenCalled());
  });

  it("applies the default profile's settings to the form", async () => {
    api.profiles.mockResolvedValue({
      profiles: [{ id: "p1", name: "Square", settings: { aspect: "1:1" }, publishing: {} }],
      default_id: "p1",
    });
    render(<App />);
    await waitFor(() => expect(screen.getByLabelText(/Aspect Ratio/i)).toHaveValue("1:1"));
  });
});
