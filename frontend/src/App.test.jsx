import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App, { DEFAULT_PUBLISHING, DEFAULT_SETTINGS, SETTINGS_SCHEMA, toOptions } from "./App.jsx";

/**
 * `App.jsx` owns two things worth testing and they fail in completely different ways.
 *
 * The first is `toOptions`, the single point where about sixty settings become the request body.
 * Everything about that translation is silent when it goes wrong: the upload endpoint takes the
 * options as form fields and matches them against `OptionsModel`, so a field it does not recognise
 * is dropped without an error, a `422` or anything else a user could see. The clip renders — just
 * not the way it was configured. That is the exact defect the `SETTINGS_SCHEMA` refactor exists to
 * make impossible, and it is only impossible while something checks that the derivation still
 * covers every declared setting, that no key has drifted into camelCase, and that the six value
 * transforms and the four publishing-derived fields still do what their declarations say.
 *
 * The second is how job progress arrives. Since Phase 5.5 that is an SSE stream, with the old
 * poll kept as the fallback for an environment that cannot carry one. Both paths are tested,
 * because both ship and each fails differently.
 *
 * For the stream, the failure modes are lifecycle ones: a connection that is not closed on unmount
 * leaks a request that never ends (worse than a leaked interval, which at least ends between
 * ticks), a connection rebuilt on every progress frame would be far more expensive than the poll
 * it replaced, and an incremental frame applied by replacing rather than merging would delete
 * every job it did not mention. None of those are visible in the UI.
 *
 * For the fallback, the two intervals — 1.2s while work is in flight, 4s when it is not — are a
 * load decision, not a cosmetic one: the fast poll issues two requests a second per open tab, so
 * leaking it after a job finishes, or after the component unmounts, is a bug that only shows up as
 * server load nobody can attribute. Those assertions are unchanged from before the stream existed;
 * they now run behind a forced fallback, because the path they describe is still shipped and is
 * what a user behind a buffering proxy actually gets.
 *
 * `./api.js` is mocked wholesale so no test touches `fetch`, but `resolveLanguage` is kept real:
 * it is the transform `SETTINGS_SCHEMA` uses to expand the language setting, so a fake one would
 * mean the expansion tests assert against the fake instead of against the mapping the API is
 * actually given.
 */

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    // Called on mount.
    watchStatus: vi.fn(),
    info: vi.fn(),
    updates: vi.fn(),
    publisherStatuses: vi.fn(),
    campaigns: vi.fn(),
    history: vi.fn(),
    profiles: vi.fn(),
    // The SSE job stream (Phase 5.5) and the poll it replaced.
    jobEvents: vi.fn(),
    listJobs: vi.fn(),
    submitUrl: vi.fn(),
    submitBatch: vi.fn(),
    upload: vi.fn(),
    preview: vi.fn(),
    watchToggle: vi.fn(),
    saveProfile: vi.fn(),
    setDefaultProfile: vi.fn(),
    deleteProfile: vi.fn(),
    saveCampaign: vi.fn(),
    // URL builders are called during render, so they must return a string rather than undefined.
    clipUrl: vi.fn(() => ""),
    downloadUrl: vi.fn(() => ""),
    videoDownloadUrl: vi.fn(() => ""),
  },
}));

vi.mock("./api.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, api: mockApi };
});

// ---------------------------------------------------------------------------
// toOptions
// ---------------------------------------------------------------------------

/** The wire keys `toOptions` is expected to produce: every schema key, plus `translate`. */
const EXPECTED_WIRE_KEYS = [...Object.keys(SETTINGS_SCHEMA), "translate"];

const wire = (settings = {}, publishing = {}) =>
  toOptions({ ...DEFAULT_SETTINGS, ...settings }, { ...DEFAULT_PUBLISHING, ...publishing });

describe("toOptions coverage", () => {
  it("sends every setting the UI can edit", () => {
    // This is the regression the schema refactor exists to prevent. Before it, `toOptions` was
    // sixty hand-written `key: settings.key` lines, and a setting added to the UI but forgotten
    // here appeared on screen, saved into a profile and never reached the backend — with no error
    // anywhere, because the upload endpoint silently ignores fields it does not know.
    const options = wire();
    const missing = Object.keys(DEFAULT_SETTINGS).filter((key) => !(key in options));
    expect(missing).toEqual([]);
  });

  it("sends nothing beyond the declared schema and the language expansion", () => {
    // The complement of the check above: a stray key is not harmless either, because it is a
    // field `OptionsModel` will reject or drop, and either way it means the request shape has
    // drifted from what is declared.
    expect(Object.keys(wire()).sort()).toEqual([...EXPECTED_WIRE_KEYS].sort());
  });

  it("spells every wire field in snake_case, the way the backend does", () => {
    // Schema keys are forwarded verbatim as `/api/upload` form fields and matched against
    // `OptionsModel`. A camelCase key — the natural thing to write in a React file — would be
    // dropped by that match, so the setting would exist in the UI and never arrive. There is no
    // failure mode to observe: the clip just renders as though the option had not been set.
    for (const key of Object.keys(wire())) {
      expect(key).toMatch(/^[a-z][a-z0-9_]*$/);
    }
  });

  it("fills in every field for a saved profile written before the newer settings existed", () => {
    // Profiles round-trip the whole settings object opaquely, so a profile saved at v0.6 has no
    // key at all for anything added since. Reading those as `undefined` would send them as the
    // string "undefined" through FormData, or omit them and change the render — so the absent
    // keys have to fall back to their declared defaults, which is what makes an old profile still
    // submittable rather than quietly producing a differently configured clip.
    const sparse = { aspect: "1:1", captions: true, topic: "growth" };
    const options = toOptions(sparse, DEFAULT_PUBLISHING);
    expect(Object.keys(options).sort()).toEqual([...EXPECTED_WIRE_KEYS].sort());
    expect(Object.entries(options).filter(([, value]) => value === undefined)).toEqual([]);
    // The three the profile did carry are its own values, not the defaults.
    expect(options.aspect).toBe("1:1");
    expect(options.topic).toBe("growth");
    // And a setting the profile predates arrives as the declared default.
    expect(options.kinetic_style).toBe(SETTINGS_SCHEMA.kinetic_style.default);
    expect(options.stem_backend).toBe(SETTINGS_SCHEMA.stem_backend.default);
  });

  it("treats undefined as absent but keeps an explicit false, zero or empty string", () => {
    // The distinction is the whole reason the fallback tests `=== undefined` rather than being
    // written as `settings[key] || spec.default`. A user who unticks captions, sets the logo scale
    // to 0 or clears the brand font has said something, and a truthiness fallback would replace
    // each of those with the default — silently re-enabling a feature that was switched off.
    const options = toOptions(
      { captions: false, brand_logo_scale: 0, brand_font: "", music_volume: 0 },
      DEFAULT_PUBLISHING
    );
    expect(options.captions).toBe(false);
    expect(options.brand_logo_scale).toBe(0);
    expect(options.brand_font).toBe("");
    expect(options.music_volume).toBe(0);
    // Absent, so the default stands.
    expect(options.metadata).toBe(true);
  });
});

describe("toOptions value transforms", () => {
  it("sends the hashtag count as a number", () => {
    // The number input hands back a string, and the backend expects an int.
    expect(wire({ hashtag_count: "12" }).hashtag_count).toBe(12);
  });

  it("sends an empty or unparseable hashtag count as zero", () => {
    // `Number("") || 0` — an empty field must not arrive as NaN, which serialises as the string
    // "NaN" and fails validation on a field the user never deliberately touched.
    expect(wire({ hashtag_count: "" }).hashtag_count).toBe(0);
    expect(wire({ hashtag_count: "many" }).hashtag_count).toBe(0);
  });

  it("sends the music volume as a number, and an empty one as zero", () => {
    expect(wire({ music_volume: "0.35" }).music_volume).toBe(0.35);
    expect(wire({ music_volume: "" }).music_volume).toBe(0);
  });

  it("sends an unset range bound as null rather than zero", () => {
    // Empty means "no bound". Zero is a different instruction — a start of 0 is legitimate — so
    // coercing the empty field to 0 would look identical here and change nothing, while an empty
    // *end* coerced to 0 would select no video at all.
    const options = wire({ range_start: "", range_end: "" });
    expect(options.range_start).toBeNull();
    expect(options.range_end).toBeNull();
  });

  it("sends a range bound the user typed as a number", () => {
    const options = wire({ range_start: "30", range_end: "90" });
    expect(options.range_start).toBe(30);
    expect(options.range_end).toBe(90);
  });

  it("keeps a range start of zero as zero rather than folding it into null", () => {
    // The one value where "no bound" and "this bound" are easy to confuse.
    expect(wire({ range_start: 0 }).range_start).toBe(0);
  });

  it("expands the language choice into both fields the API takes", () => {
    // The UI has one dropdown; the API has `language` and `translate`. "auto" is not a language
    // code, so it has to become a null language rather than being sent as-is.
    expect(wire({ language: "auto" })).toMatchObject({ language: null, translate: false });
    expect(wire({ language: "es" })).toMatchObject({ language: "es", translate: false });
  });

  it("sends translate as its own flag, not as a language", () => {
    // "Translate to English" is a mode, not a language. Collapsing it into `language: "en"` would
    // transcribe in English instead of translating into it — a plausible-looking result that is
    // the wrong operation.
    expect(wire({ language: "translate" })).toMatchObject({ language: null, translate: true });
  });
});

describe("toOptions publishing fields", () => {
  it("holds the platform list back unless the mode is auto", () => {
    // In review mode the clips are held for approval. Sending the platform list anyway would
    // publish them the moment they finish rendering, and a post is public the instant it lands —
    // there is no undo for this one.
    const options = wire({}, { mode: "review", platforms: ["tiktok", "instagram"] });
    expect(options.publish_to).toEqual([]);
    expect(options.publish_mode).toBe("review");
  });

  it("sends the platform list in auto mode", () => {
    const options = wire({}, { mode: "auto", platforms: ["tiktok", "instagram"] });
    expect(options.publish_to).toEqual(["tiktok", "instagram"]);
    expect(options.publish_mode).toBe("auto");
  });

  it("sends the campaign id, which is what gives each platform a route", () => {
    expect(wire({}, { campaign_id: "c1" }).campaign_id).toBe("c1");
  });

  it("converts the schedule from the browser-local input string to epoch seconds", () => {
    // A `datetime-local` value carries no zone, so it means local time and the conversion has to
    // happen here. Sending the string itself, or seconds computed as though it were UTC, would
    // publish at the wrong hour — by however much this machine is offset, which is why the
    // expectation is computed rather than written as a literal.
    const local = "2024-05-08T09:30";
    expect(wire({}, { schedule: local }).schedule_at).toBe(new Date(local).getTime() / 1000);
  });

  it("sends no schedule at all when the field is empty or unparseable", () => {
    // An unscheduled run and a run scheduled for NaN are very different requests; the second one
    // is a publish attempt with no time on it.
    expect(wire({}, { schedule: "" }).schedule_at).toBeNull();
    expect(wire({}, { schedule: "whenever" }).schedule_at).toBeNull();
  });
});

describe("SETTINGS_SCHEMA structure", () => {
  it("keeps the publishing-derived fields out of the settings defaults", () => {
    // They are declared in the schema so the request shape reads in one place, but they come from
    // the publishing state. Leaking them into `DEFAULT_SETTINGS` would put them in every saved
    // profile, and a restored profile would then carry a stale `schedule_at`.
    for (const key of ["publish_to", "campaign_id", "publish_mode", "schedule_at"]) {
      expect(SETTINGS_SCHEMA[key].from).toBe("publishing");
      expect(DEFAULT_SETTINGS).not.toHaveProperty(key);
    }
  });

  it("declares a default for every setting that is not publishing-derived", () => {
    // `DEFAULT_SETTINGS` is derived by reading `spec.default`, so an entry without one produces a
    // key whose value is `undefined` — which then reads as "absent" in `toOptions` and takes a
    // default that does not exist. The whole thing fails as a silently missing option.
    const undeclared = Object.entries(SETTINGS_SCHEMA)
      .filter(([, spec]) => spec.from !== "publishing" && !("default" in spec))
      .map(([key]) => key);
    expect(undeclared).toEqual([]);
  });

  it("carries no undefined value in the defaults it derives", () => {
    const undefinedKeys = Object.entries(DEFAULT_SETTINGS)
      .filter(([, value]) => value === undefined)
      .map(([key]) => key);
    expect(undefinedKeys).toEqual([]);
  });

  it("derives the settings defaults from exactly the non-publishing schema entries", () => {
    const expected = Object.entries(SETTINGS_SCHEMA)
      .filter(([, spec]) => spec.from !== "publishing")
      .map(([key]) => key);
    expect(Object.keys(DEFAULT_SETTINGS)).toEqual(expected);
  });
});

// ---------------------------------------------------------------------------
// The rendered app: polling and profiles
// ---------------------------------------------------------------------------

const INFO = {
  llm_available: false,
  version: "0.9.0",
  effects: null,
  engines: [],
  capabilities: null,
};

/** A job the poll can return. `clips` is always present, as the API always sends it. */
const job = (overrides = {}) => ({
  id: "j1",
  status: "processing",
  source: "/watch/inbox/talk.mp4",
  input_type: "file",
  progress: 0.4,
  stage: "rendering",
  clips: [],
  ...overrides,
});

beforeEach(() => {
  // `shouldAdvanceTime` keeps the microtask-driven parts of the app working normally while the
  // interval is under the test's control.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  mockApi.watchStatus.mockResolvedValue({ enabled: false, folder: "" });
  mockApi.info.mockResolvedValue(INFO);
  mockApi.updates.mockResolvedValue({ update_available: false, current: "0.9.0", latest: "0.9.0" });
  mockApi.publisherStatuses.mockResolvedValue({ platforms: {} });
  mockApi.campaigns.mockResolvedValue({ campaigns: [{ id: "c1", name: "Launch", routes: {} }] });
  mockApi.history.mockResolvedValue({ publish_attempts: [] });
  mockApi.profiles.mockResolvedValue({ profiles: [], default_id: null });
  mockApi.listJobs.mockResolvedValue({ jobs: [] });
  // A stream that opens and then says nothing, which is what an idle backend looks like. Tests
  // that care override this — `fakeStream` to drive frames, or a rejection to force the fallback.
  mockApi.jobEvents.mockImplementation(() => new Promise(() => {}));
  mockApi.submitUrl.mockResolvedValue(job({ id: "submitted-1", status: "queued", source: "u" }));
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

/**
 * Render and wait for the six startup calls to have been applied.
 *
 * All of them resolve in microtasks, and each one sets state. Letting any of them land after the
 * test body has finished produces an `act()` warning attributed to whichever test happens to be
 * running next, so the settling is done here rather than being left to chance.
 */
const mount = async () => {
  const utils = render(<App />);
  await waitFor(() => expect(mockApi.profiles).toHaveBeenCalled());
  await settle();
  return utils;
};

/** Flush the promise chains — `Promise.allSettled` and the chained profile load need several. */
const settle = async () => {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  });
};

/** Advance the fake clock, letting each poll's promises resolve as they are triggered. */
const advance = async (milliseconds) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(milliseconds);
  });
};

const aspectSelect = () => screen.getByRole("combobox", { name: "Aspect Ratio" });
const profileSelect = () =>
  screen.getByRole("option", { name: /select a profile/i }).closest("select");

/** Submit one URL and wait for the request, so the options it was given can be inspected. */
const submitOneUrl = async () => {
  fireEvent.change(screen.getByPlaceholderText(/paste a video url/i), {
    target: { value: "https://example.com/talk" },
  });
  fireEvent.click(screen.getByRole("button", { name: /get clips/i }));
  await waitFor(() => expect(mockApi.submitUrl).toHaveBeenCalled());
  await settle();
  return mockApi.submitUrl.mock.calls.at(-1)[1];
};

/**
 * A controllable stand-in for `api.jobEvents`.
 *
 * Returns a promise that stays pending, like a real open stream, plus handles to push frames into
 * the component and to end it. `EventSource` is not used by the implementation and does not exist
 * in jsdom anyway, so there is nothing to polyfill — the seam is this one function, which is what
 * makes it mockable at all.
 */
const fakeStream = () => {
  const state = { snapshot: null, jobs: null, settle: null, aborts: 0, opens: 0 };
  mockApi.jobEvents.mockImplementation(({ onSnapshot, onJobs, signal }) => {
    state.opens += 1;
    state.snapshot = onSnapshot;
    state.jobs = onJobs;
    signal?.addEventListener("abort", () => {
      state.aborts += 1;
      // A real reader rejects with an AbortError when the signal fires; the component relies on
      // that name to tell its own teardown apart from a genuine failure.
      const error = new Error("aborted");
      error.name = "AbortError";
      state.settle?.reject(error);
    });
    return new Promise((resolve, reject) => {
      state.settle = { resolve, reject };
    });
  });
  return {
    get opens() {
      return state.opens;
    },
    get aborts() {
      return state.aborts;
    },
    emitSnapshot: async (jobs) => {
      await act(async () => state.snapshot(jobs));
      await settle();
    },
    emitJobs: async (jobs) => {
      await act(async () => state.jobs(jobs));
      await settle();
    },
    /** End the stream the way a server restart would: cleanly, not as an error. */
    close: async () => {
      await act(async () => {
        state.settle.resolve();
        await Promise.resolve();
      });
    },
  };
};

describe("App job event stream", () => {
  let stream;

  beforeEach(() => {
    stream = fakeStream();
  });

  it("opens no stream until something is being tracked", async () => {
    // An idle tab that has submitted nothing has nothing to follow. This is the same arming
    // condition the poll had, and it is why an open tab costs the server nothing.
    await mount();
    await advance(10000);
    expect(mockApi.jobEvents).not.toHaveBeenCalled();
  });

  it("opens the stream as soon as the watch folder is reported active", async () => {
    // Watch-folder mode produces jobs this tab never submitted, so it is the one case where
    // following progress has to start without the user doing anything.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    expect(mockApi.jobEvents).toHaveBeenCalled();
  });

  it("does not poll the job list while the stream is carrying progress", async () => {
    // The point of the endpoint. If both ran, this would be strictly more work than before.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    await advance(10000);
    expect(mockApi.listJobs).not.toHaveBeenCalled();
  });

  it("replaces the job list from a snapshot", async () => {
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    await stream.emitSnapshot([
      job({ id: "a", source: "/watch/a.mp4", title: "Alpha", created_at: 2 }),
    ]);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });

  it("merges an incremental frame by id instead of replacing the list", async () => {
    // The failure this prevents is silent and total: applying a frame that contains one job by
    // replacing the array would delete every other job from the UI while they carried on
    // rendering on the server.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    await stream.emitSnapshot([
      job({ id: "a", source: "/watch/a.mp4", title: "Alpha", created_at: 2 }),
      job({ id: "b", source: "/watch/b.mp4", title: "Beta", created_at: 1 }),
    ]);
    await stream.emitJobs([
      job({ id: "b", source: "/watch/b.mp4", title: "Beta renamed", created_at: 1 }),
    ]);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("Beta renamed")).toBeInTheDocument();
  });

  it("keeps the newest job first after a merge, as the job list route does", async () => {
    // `jobs` is read as an ordered list, so a merge that appended would put a newly-started job
    // at the bottom of the page while a full refetch put it at the top.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    await stream.emitSnapshot([
      job({ id: "old", source: "/watch/old.mp4", title: "Older", created_at: 1 }),
    ]);
    await stream.emitJobs([
      job({ id: "new", source: "/watch/new.mp4", title: "Newer", created_at: 9 }),
    ]);
    const rendered = screen.getByText("Newer").compareDocumentPosition(screen.getByText("Older"));
    // Node.DOCUMENT_POSITION_FOLLOWING — "Older" comes after "Newer".
    expect(rendered & 4).toBeTruthy();
  });

  it("does not reopen the stream when a job changes state", async () => {
    // The concrete gain over polling. The 1200/4000ms split existed because a poll must choose a
    // rate and the rate had to change when work started or stopped, so the timer was torn down
    // and rebuilt on every queued->processing->completed transition. A stream has no rate, so
    // `hasActiveJobs` is deliberately not one of its dependencies. Reconnecting per transition
    // would be far more expensive than the poll it replaced.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    const opens = stream.opens;
    await stream.emitSnapshot([
      job({ id: "a", source: "/watch/a.mp4", status: "queued", created_at: 1 }),
    ]);
    await stream.emitJobs([
      job({ id: "a", source: "/watch/a.mp4", status: "processing", created_at: 1 }),
    ]);
    await stream.emitJobs([
      job({ id: "a", source: "/watch/a.mp4", status: "completed", progress: 1, created_at: 1 }),
    ]);
    expect(stream.opens).toBe(opens);
  });

  it("closes the stream when the app unmounts", async () => {
    // A leaked stream is worse than a leaked interval: the request never ends at all, so it holds
    // a connection on the server for a component that no longer exists.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    const { unmount } = await mount();
    expect(stream.aborts).toBe(0);
    unmount();
    expect(stream.aborts).toBe(1);
  });

  it("reconnects after the server closes the stream cleanly", async () => {
    // A server restart or a proxy recycling the connection ends the stream without an error. That
    // is not a failure and must not count towards the fallback, but it does need reconnecting or
    // progress silently stops arriving for the rest of the session.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    const opens = stream.opens;
    await stream.close();
    await advance(1100);
    expect(stream.opens).toBe(opens + 1);
  });

  it("refreshes publish attempts on its own slower interval while streaming", async () => {
    // The stream carries jobs only, deliberately: publish attempts live in a different database
    // and reading them is a SQL query where reading jobs is a dict lookup. They still have to
    // refresh, just not twice a second.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    const before = mockApi.history.mock.calls.length;
    await advance(5000);
    expect(mockApi.history.mock.calls.length).toBe(before + 1);
    await advance(5000);
    expect(mockApi.history.mock.calls.length).toBe(before + 2);
  });
});

describe("App stream fallback", () => {
  it("falls back to polling after two failed attempts to open the stream", async () => {
    // An environment that cannot carry SSE fails immediately and every time, so retrying forever
    // would leave the user watching a frozen progress bar when polling would have worked.
    mockApi.jobEvents.mockRejectedValue(new Error("no streaming here"));
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    expect(mockApi.listJobs).not.toHaveBeenCalled();
    await advance(1100);
    await waitFor(() => expect(mockApi.listJobs).toHaveBeenCalled());
    expect(mockApi.jobEvents.mock.calls.length).toBe(2);
  });

  it("stops trying the stream once it has fallen back", async () => {
    // Otherwise every re-arm would pay for two more failed connections.
    mockApi.jobEvents.mockRejectedValue(new Error("no streaming here"));
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    await advance(1100);
    await settle();
    const attempts = mockApi.jobEvents.mock.calls.length;
    await advance(20000);
    expect(mockApi.jobEvents.mock.calls.length).toBe(attempts);
  });

  it("reports an unreachable backend when neither the stream nor the poll answers", async () => {
    // The two failed stream attempts and the failing polls count towards the same total, which is
    // what the banner is gated on — a user does not care which transport could not reach the
    // server, only that nothing can.
    mockApi.jobEvents.mockRejectedValue(new Error("no streaming here"));
    mockApi.listJobs.mockRejectedValue(new Error("backend down"));
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    await advance(1100);
    await waitFor(() => expect(mockApi.listJobs).toHaveBeenCalled());
    await advance(2500);
    expect(await screen.findByRole("alert")).toHaveTextContent(/cannot reach the backend/i);
  });
});

describe("App polling fallback", () => {
  // These are the assertions that described the only transport there used to be. They are kept
  // verbatim in substance, run behind a forced fallback: the poll still ships, and its two
  // intervals are still a load decision worth pinning.
  beforeEach(() => {
    mockApi.jobEvents.mockRejectedValue(new Error("no streaming here"));
  });

  /** Burn through the stream attempts so `useStream` flips and the interval is installed. */
  const fallBack = async () => {
    await advance(1100);
    await settle();
  };

  it("does not poll at all until something is being tracked", async () => {
    // With nothing submitted and no watch folder there is nothing to ask about, and an unconditional
    // interval would have every idle tab querying the job list forever.
    await mount();
    await advance(10000);
    expect(mockApi.listJobs).not.toHaveBeenCalled();
  });

  it("starts polling as soon as the watch folder is reported active", async () => {
    // Watch-folder mode produces jobs this tab never submitted, so it is the one case where
    // polling has to start without the user doing anything.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    await mount();
    await fallBack();
    expect(mockApi.listJobs).toHaveBeenCalled();
  });

  it("starts polling once a submitted job is being tracked", async () => {
    // The other arming condition, and the ordinary one: until this tab submits something there is
    // nothing whose progress it needs to follow. Polling that failed to start here would leave the
    // job card frozen at "queued" until the page was reloaded, with the job running fine on the
    // server — which reads as a broken submission.
    mockApi.listJobs.mockResolvedValue({
      jobs: [job({ id: "submitted-1", status: "queued", source: "u", input_type: "url" })],
    });
    await mount();
    expect(mockApi.listJobs).not.toHaveBeenCalled();
    await submitOneUrl();
    await fallBack();
    await waitFor(() => expect(mockApi.listJobs).toHaveBeenCalled());
    const before = mockApi.listJobs.mock.calls.length;
    // And it is an interval, not a single poll at submission time.
    await advance(1200);
    expect(mockApi.listJobs.mock.calls.length).toBe(before + 1);
  });

  it("polls every 1.2 seconds while a job is queued or processing", async () => {
    // The fast interval is what makes the progress bar move. It is only defensible while there is
    // something in flight, which is why it is asserted together with the back-off below.
    mockApi.listJobs.mockResolvedValue({
      jobs: [job({ id: "submitted-1", status: "processing" })],
    });
    await mount();
    await submitOneUrl();
    await fallBack();
    await waitFor(() => expect(mockApi.listJobs).toHaveBeenCalled());
    const before = mockApi.listJobs.mock.calls.length;
    await advance(1200);
    expect(mockApi.listJobs.mock.calls.length).toBe(before + 1);
    await advance(1200);
    expect(mockApi.listJobs.mock.calls.length).toBe(before + 2);
  });

  it("backs off to 4 seconds once the last job has finished", async () => {
    // The case a `jobs.length` dependency used to miss: a job going from processing to completed
    // does not change the count, so the 1.2s poll carried on indefinitely after everything had
    // finished — two requests a second, per open tab, for as long as the tab stayed open. Advancing
    // past the fast interval first is the assertion that matters: a request at 1.2s would mean the
    // fast timer is still installed.
    mockApi.listJobs.mockResolvedValue({
      jobs: [job({ id: "submitted-1", status: "completed", progress: 1 })],
    });
    await mount();
    await submitOneUrl();
    await fallBack();
    // The submitted job arrives `queued`, so the fast interval is installed and then replaced once
    // the first poll reports it finished. Counting from after that transition is the point.
    await waitFor(() => expect(mockApi.listJobs).toHaveBeenCalled());
    await settle();
    const before = mockApi.listJobs.mock.calls.length;
    await advance(1200);
    expect(mockApi.listJobs.mock.calls.length).toBe(before);
    await advance(2800);
    expect(mockApi.listJobs.mock.calls.length).toBe(before + 1);
  });

  it("keeps the fast interval in watch-folder mode even with nothing active", async () => {
    // Deliberate, and the reason the back-off test above uses a tracked job instead: watch mode is
    // waiting for a job that does not exist yet, so "nothing is active" is precisely the state in
    // which a new file could appear. Backing off there would delay every dropped file by 4s.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    mockApi.listJobs.mockResolvedValue({ jobs: [job({ status: "completed", progress: 1 })] });
    await mount();
    await fallBack();
    const before = mockApi.listJobs.mock.calls.length;
    await advance(1200);
    expect(mockApi.listJobs.mock.calls.length).toBe(before + 1);
  });

  it("stops polling when the app unmounts", async () => {
    // An interval that outlives the component keeps issuing requests against a tree that no longer
    // exists — invisible in the UI, visible only as load on the server and as state updates on an
    // unmounted component.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    mockApi.listJobs.mockResolvedValue({ jobs: [job({ status: "processing" })] });
    const { unmount } = await mount();
    await fallBack();
    const before = mockApi.listJobs.mock.calls.length;
    unmount();
    await advance(10000);
    expect(mockApi.listJobs.mock.calls.length).toBe(before);
  });
});

describe("App profiles", () => {
  const PROFILE = {
    id: "p1",
    name: "Shorts",
    settings: { aspect: "1:1" },
    publishing: { mode: "auto", platforms: ["tiktok"], campaign_id: "c1" },
  };

  it("pre-fills the settings from the default profile on load", async () => {
    // The point of a default profile is that the page opens configured. If the load only set the
    // active id, the dropdown would name a profile whose settings were not in effect.
    mockApi.profiles.mockResolvedValue({ profiles: [PROFILE], default_id: "p1" });
    await mount();
    expect(profileSelect()).toHaveValue("p1");
    expect(aspectSelect()).toHaveValue("1:1");
  });

  it("merges a default profile over the declared defaults rather than replacing them", async () => {
    // A stored profile is an arbitrarily old snapshot, and this one carries a single key. Applying
    // it without the `{ ...DEFAULT_SETTINGS }` base would leave every other setting `undefined`:
    // the controls would fall back to uncontrolled inputs and the submitted request would be
    // missing most of its fields.
    mockApi.profiles.mockResolvedValue({ profiles: [PROFILE], default_id: "p1" });
    await mount();
    const options = await submitOneUrl();
    expect(options.aspect).toBe("1:1");
    expect(Object.keys(DEFAULT_SETTINGS).filter((key) => !(key in options))).toEqual([]);
    expect(Object.entries(options).filter(([, value]) => value === undefined)).toEqual([]);
    expect(options.caption_preset).toBe(DEFAULT_SETTINGS.caption_preset);
  });

  it("applies the default profile's publishing state as well as its settings", async () => {
    // The profile says auto-publish to TikTok, and half-applying it would render clips that are
    // held when the user's saved configuration says to send them.
    mockApi.profiles.mockResolvedValue({ profiles: [PROFILE], default_id: "p1" });
    await mount();
    const options = await submitOneUrl();
    expect(options.publish_mode).toBe("auto");
    expect(options.publish_to).toEqual(["tiktok"]);
    expect(options.campaign_id).toBe("c1");
  });

  it("applies the default profile once, not on every render", async () => {
    // The guard is a ref, and without it every re-render that re-ran the load would overwrite
    // whatever the user had changed since the page opened — while they were looking at it. The
    // watch-folder poll below is the cheapest way to force repeated re-renders.
    mockApi.watchStatus.mockResolvedValue({ enabled: true, folder: "/inbox" });
    mockApi.profiles.mockResolvedValue({ profiles: [PROFILE], default_id: "p1" });
    await mount();
    fireEvent.change(aspectSelect(), { target: { value: "16:9" } });
    expect(aspectSelect()).toHaveValue("16:9");
    await advance(4000);
    await advance(4000);
    expect(aspectSelect()).toHaveValue("16:9");
    expect(mockApi.profiles).toHaveBeenCalledTimes(1);
  });

  it("applies a profile chosen from the dropdown over the full defaults", async () => {
    // Same merge, reached the other way. This is the path a user takes to switch configurations
    // mid-session, and a profile that predates a setting must not blank it out.
    mockApi.profiles.mockResolvedValue({ profiles: [PROFILE], default_id: null });
    await mount();
    expect(aspectSelect()).toHaveValue(DEFAULT_SETTINGS.aspect);
    fireEvent.change(profileSelect(), { target: { value: "p1" } });
    expect(aspectSelect()).toHaveValue("1:1");
    const options = await submitOneUrl();
    expect(Object.keys(DEFAULT_SETTINGS).filter((key) => !(key in options))).toEqual([]);
    expect(options.publish_to).toEqual(["tiktok"]);
  });

  it("clears the selection without disturbing the settings when the profile is deselected", async () => {
    // Choosing the placeholder option means "stop tracking a profile", not "reset everything" —
    // resetting would discard edits the user made after applying it.
    mockApi.profiles.mockResolvedValue({ profiles: [PROFILE], default_id: "p1" });
    await mount();
    fireEvent.change(profileSelect(), { target: { value: "" } });
    expect(profileSelect()).toHaveValue("");
    expect(aspectSelect()).toHaveValue("1:1");
  });
});
