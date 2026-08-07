import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, resolveLanguage } from "./api.js";
import HistoryView from "./components/HistoryView.jsx";
import InputBar from "./components/InputBar.jsx";
import JobCard from "./components/JobCard.jsx";
import PreviewCard from "./components/PreviewCard.jsx";
import ProfilesBar from "./components/ProfilesBar.jsx";
import PublishingPanel from "./components/PublishingPanel.jsx";
import ScheduleCalendar from "./components/ScheduleCalendar.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";
import StorageSettings from "./components/StorageSettings.jsx";

const numOrNull = (value) =>
  value === "" || value === null || value === undefined ? null : Number(value);

const scheduleToEpoch = (value) => {
  if (!value) return null;
  const milliseconds = new Date(value).getTime();
  return Number.isNaN(milliseconds) ? null : milliseconds / 1000;
};

const numberOrZero = (value) => Number(value) || 0;

/**
 * Every setting, declared once.
 *
 * This replaces three separate statements of the same list: `DEFAULT_SETTINGS` (~52 keys),
 * `toOptions` (~60 hand-written `key: settings.key` lines) and the engine block. Adding a
 * setting meant editing two of them, and forgetting `toOptions` produced a setting that
 * appeared in the UI, saved into a profile, and **never reached the backend** — the failure the
 * old comment warned about, with nothing to catch it.
 *
 * Each entry is `{ default }` plus, where the wire form differs from the stored form, one of:
 *
 *   `toWire`   a value transform. Only six settings need one.
 *   `expand`   returns several wire fields from one setting. `language` is the only case:
 *              the UI stores `"es-translate"` and the API takes `language` + `translate`.
 *   `from`     `"publishing"`, for the four wire fields that come from the publishing state
 *              rather than from settings. They are declared here, in wire order, so the
 *              request shape is readable in one place — but they are excluded from
 *              `DEFAULT_SETTINGS`, because they are not settings.
 *
 * **Declaration order is the wire order**, which is why the publishing four sit in the middle
 * where they have always been rather than being appended.
 *
 * **Keys are the snake_case API spellings** and that is load-bearing twice over: they are
 * forwarded verbatim as `/api/upload` form fields and matched against `OptionsModel`, and saved
 * profiles round-trip the whole settings object opaquely — so renaming a key here silently
 * invalidates every stored profile. `tests/App.settings.test.jsx` pins the spellings.
 */
export const SETTINGS_SCHEMA = {
  // `resolveLanguage` splits the UI's single choice into the two fields the API takes.
  language: { default: "auto", expand: (value) => resolveLanguage(value) },
  clip_length: { default: "auto" },
  aspect: { default: "9:16" },
  num_clips: { default: "auto" },
  strategy: { default: "ai" },
  captions: { default: true },
  subtitle_sidecar: { default: false },
  topic: { default: "" },
  vocabulary: { default: "" },
  vibe: { default: "" },
  platform: { default: "generic" },
  hashtag_count: { default: 5, toWire: numberOrZero },
  // Empty means "no bound", which the API spells as null rather than as 0.
  range_start: { default: "", toWire: numOrNull },
  range_end: { default: "", toWire: numOrNull },
  metadata: { default: true },

  // From the publishing state, not from settings. `publish_to` is deliberately empty unless
  // the mode is `auto`: in `review` mode the clips are held for approval, and sending the
  // platform list would publish them immediately.
  publish_to: {
    from: "publishing",
    toWire: (publishing) => (publishing.mode === "auto" ? publishing.platforms : []),
  },
  campaign_id: { from: "publishing", toWire: (publishing) => publishing.campaign_id },
  publish_mode: { from: "publishing", toWire: (publishing) => publishing.mode },
  schedule_at: {
    from: "publishing",
    toWire: (publishing) => scheduleToEpoch(publishing.schedule),
  },

  // Phase 4 — visual effects (all individually toggleable)
  caption_template: { default: "karaoke" },
  caption_position: { default: "bottom" },
  reframe: { default: false },
  zoom: { default: false },
  transitions: { default: false },
  hook_title: { default: false },
  fades: { default: false },
  progress_bar: { default: false },
  color: { default: "" },
  music: { default: "" },
  music_volume: { default: 0.12, toWire: numberOrZero },
  emoji: { default: "off" },
  emoji_mode: { default: "keyword" },
  emoji_animate: { default: true },
  filler_removal: { default: false },

  // Tier 1 — animated captions / b-roll / visual selection (all default OFF / karaoke)
  caption_preset: { default: "karaoke" },
  // U6: the brand kit. Part of `settings` on purpose - saved profiles store the whole settings
  // blob, so a kit is saved, applied and set as default by machinery that already exists.
  brand_font: { default: "" },
  brand_primary_color: { default: "" },
  brand_highlight_color: { default: "" },
  brand_cta: { default: "" },
  brand_logo: { default: "" },
  brand_logo_position: { default: "top_right" },
  brand_logo_scale: { default: 0.16 },
  brand_logo_opacity: { default: 0.85 },
  caption_animation: { default: "" },
  caption_keyword_highlight: { default: false },
  caption_keyword_ai: { default: false },
  caption_emoji: { default: false },
  broll: { default: false },
  broll_intensity: { default: "standard" },
  asset_sourcing_mode: { default: "off" },
  broll_provider: { default: "" },
  selection_prompt: { default: "" },
  visual_selection: { default: false },
  permissibility_mode: { default: false },

  // Speaker diarisation & multi-speaker reframe (all default OFF / follow_active / standard)
  diarization: { default: false },
  speaker_reframe: { default: false },
  reframe_layout: { default: "follow_active" },
  reframe_intensity: { default: "standard" },

  // Advanced AV engines (Req 20.4). A sibling engine spec adds its `<engine_id>_enabled` flag
  // and option defaults *here only* — they are forwarded generically and profiles persist them
  // automatically, because they round-trip through the opaque settings blob.
  //
  // Kinetic typography (kinetic-typography spec, Req 17.5) and stem inpainting
  // (audio-stem-inpainting spec). Both mirror `ProcessingOptions` exactly and both flags are
  // off, so a stock install still renders exactly as v0.8.0.
  kinetic_typography_enabled: { default: false },
  kinetic_style: { default: "karaoke_fill" },
  kinetic_reveal: { default: "cumulative" },
  kinetic_font: { default: "" },
  kinetic_max_lines: { default: 2 },
  kinetic_max_line_width: { default: 22 },
  kinetic_safe_area_x_pct: { default: 6.0 },
  kinetic_safe_area_y_pct: { default: 10.0 },
  kinetic_motion_ms: { default: 120 },
  kinetic_confidence_floor: { default: 0.0 },
  stem_inpainting_enabled: { default: false },
  stem_mix_preset: { default: "custom" },
  stem_gain_vocals: { default: 1.0 },
  stem_gain_music: { default: 1.0 },
  stem_gain_other: { default: 1.0 },
  stem_repair_mode: { default: "crossfade" },
  stem_repair_window_ms: { default: 12 },
  stem_declick: { default: false },
  stem_backend: { default: "auto" },
  stem_model: { default: "htdemucs" },
  stem_retain_stems: { default: false },
};

/** Initial settings, derived from the one schema. */
export const DEFAULT_SETTINGS = Object.fromEntries(
  Object.entries(SETTINGS_SCHEMA)
    .filter(([, spec]) => spec.from !== "publishing")
    .map(([key, spec]) => [key, spec.default])
);

/**
 * How many consecutive poll failures before the app says the backend is unreachable.
 *
 * Three, not one. A single failure is routine — a container restarting, a laptop waking, a proxy
 * recycling a connection — and a banner that appears on every one of those trains the user to
 * ignore it. Three consecutive failures at the fast interval is under four seconds, which is
 * still well inside the window where the answer is useful.
 */
export const BACKEND_UNREACHABLE_AFTER = 3;

/**
 * How many failed attempts to open the event stream before giving up on it for the session.
 *
 * Two. An environment that cannot carry SSE — a proxy that buffers responses, a browser without
 * streaming `fetch` — fails immediately and fails every time, so one retry is enough to tell it
 * apart from a container that happened to be restarting. Falling back permanently rather than
 * retrying forever matters because the fallback *works*: retrying a stream that will never work
 * would leave the user watching a frozen progress bar behind a banner, when polling would have
 * shown them the truth.
 */
export const STREAM_FALLBACK_AFTER = 2;

/** Delay before reconnecting a stream that dropped. */
export const STREAM_RETRY_MS = 1000;

/**
 * How often to refresh publish attempts while the event stream is carrying job progress.
 *
 * The stream deliberately carries jobs only. Publish attempts live in a different store
 * (`history.db`) and reading them is a SQL query, where reading jobs is a dict lookup — putting
 * them in the stream would mean querying that database twice a second for every open tab, which
 * is exactly the kind of cost the job store is structured to avoid.
 *
 * Five seconds rather than the 1.2s they used to arrive at, because nothing needs them faster:
 * an attempt changes state when a publisher answers, on the order of seconds, and the actions a
 * user takes on one already refresh it immediately.
 */
export const PUBLISH_ATTEMPT_POLL_MS = 5000;

export const DEFAULT_PUBLISHING = {
  platforms: [],
  campaign_id: "",
  mode: "review",
  schedule: "",
  account_id: "",
  target_type: "",
  target_id: "",
};

/**
 * Build the request body from the current settings and publishing state.
 *
 * Derived from `SETTINGS_SCHEMA` rather than restating it, so a setting cannot exist in the UI
 * and be missing from the request. A missing key falls back to its declared default, which is
 * what lets an older saved profile — written before a setting existed — still submit.
 */
export function toOptions(settings, publishing) {
  const wire = {};
  for (const [key, spec] of Object.entries(SETTINGS_SCHEMA)) {
    if (spec.from === "publishing") {
      wire[key] = spec.toWire(publishing);
      continue;
    }
    // `undefined` means "absent", not "false" — so a saved profile predating this setting gets
    // the default, while a user's explicit `false`, `0` or `""` survives.
    const value = settings[key] === undefined ? spec.default : settings[key];
    if (spec.expand) {
      Object.assign(wire, spec.expand(value));
    } else {
      wire[key] = spec.toWire ? spec.toWire(value) : value;
    }
  }
  return wire;
}

// `App` takes no props — `main.jsx` mounts it as the root and every value it renders comes from its
// own state or from the API — so there is no boundary here to declare. The shapes it hands down are
// declared on the components that receive them, and `SETTINGS_SCHEMA` above remains the single
// authoritative statement of the settings object.
export default function App() {
  const [activeView, setActiveView] = useState("create");
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [publishing, setPublishing] = useState(DEFAULT_PUBLISHING);
  const [publisherStatuses, setPublisherStatuses] = useState({});
  const [campaigns, setCampaigns] = useState([]);
  const [publishAttempts, setPublishAttempts] = useState([]);
  const [input, setInput] = useState({ urls: [], files: [] });
  const [preview, setPreview] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [jobs, setJobs] = useState([]);
  const [trackedIds, setTrackedIds] = useState(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [watch, setWatch] = useState({ enabled: false, folder: "" });
  const [llmAvailable, setLlmAvailable] = useState(false);
  const [version, setVersion] = useState("");
  const [effects, setEffects] = useState(null);
  const [engines, setEngines] = useState([]);
  // /api/info's `capabilities` block: probe results keyed by `<kind>:<name>`
  // plus engine-specific option domains keyed by Engine_Id (e.g.
  // capabilities.kinetic_typography.styles / .reveal_modes).
  const [capabilities, setCapabilities] = useState(null);
  const [updateInfo, setUpdateInfo] = useState(null);
  const [profiles, setProfiles] = useState([]);
  const [defaultProfileId, setDefaultProfileId] = useState(null);
  const [activeProfileId, setActiveProfileId] = useState("");
  // Consecutive poll failures. See `poll` and BACKEND_UNREACHABLE_AFTER.
  const [pollFailures, setPollFailures] = useState(0);
  const pollRef = useRef(null);
  // Whether job progress arrives over the event stream. Starts true and only ever goes false —
  // see STREAM_FALLBACK_AFTER. Kept in state rather than a ref because the polling effect is
  // gated on it and has to re-run when it flips.
  const [useStream, setUseStream] = useState(true);
  const streamAttemptsRef = useRef(0);
  const defaultAppliedRef = useRef(false);

  const loadPublishingData = useCallback(async () => {
    const [statusResult, campaignResult, historyResult] = await Promise.allSettled([
      api.publisherStatuses(),
      api.campaigns(),
      api.history(),
    ]);
    if (statusResult.status === "fulfilled") {
      setPublisherStatuses(statusResult.value.platforms || {});
    }
    if (campaignResult.status === "fulfilled") {
      setCampaigns(campaignResult.value.campaigns || []);
    }
    if (historyResult.status === "fulfilled") {
      setPublishAttempts(historyResult.value.publish_attempts || []);
    }
  }, []);

  const applyProfile = useCallback(
    (id) => {
      setActiveProfileId(id);
      if (!id) return;
      const profile = profiles.find((p) => p.id === id);
      if (!profile) return;
      setSettings({ ...DEFAULT_SETTINGS, ...(profile.settings || {}) });
      setPublishing({ ...DEFAULT_PUBLISHING, ...(profile.publishing || {}) });
    },
    [profiles]
  );

  const loadProfiles = useCallback(async () => {
    try {
      const data = await api.profiles();
      setProfiles(data.profiles || []);
      setDefaultProfileId(data.default_id || null);
      return data;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    api
      .watchStatus()
      .then(setWatch)
      .catch(() => {});
    api
      .info()
      .then((info) => {
        setLlmAvailable(!!info.llm_available);
        setVersion(info.version || "");
        setEffects(info.effects || null);
        setEngines(Array.isArray(info.engines) ? info.engines : []);
        setCapabilities(info.capabilities || null);
      })
      .catch(() => {});
    api
      .updates()
      .then(setUpdateInfo)
      .catch(() => {});
    loadPublishingData();
    // Load profiles and pre-fill from the default profile once on startup.
    loadProfiles().then((data) => {
      if (data && data.default_id && !defaultAppliedRef.current) {
        defaultAppliedRef.current = true;
        const profile = (data.profiles || []).find((p) => p.id === data.default_id);
        if (profile) {
          setActiveProfileId(profile.id);
          setSettings({ ...DEFAULT_SETTINGS, ...(profile.settings || {}) });
          setPublishing({ ...DEFAULT_PUBLISHING, ...(profile.publishing || {}) });
        }
      }
    });
  }, [loadPublishingData, loadProfiles]);

  const handleSaveProfile = useCallback(
    async (name, id) => {
      const saved = await api.saveProfile({ name, id, settings, publishing });
      await loadProfiles();
      setActiveProfileId(saved.id);
    },
    [settings, publishing, loadProfiles]
  );

  const handleSetDefaultProfile = useCallback(
    async (id) => {
      await api.setDefaultProfile(id);
      await loadProfiles();
    },
    [loadProfiles]
  );

  const handleDeleteProfile = useCallback(
    async (id) => {
      await api.deleteProfile(id);
      if (id === activeProfileId) setActiveProfileId("");
      await loadProfiles();
    },
    [activeProfileId, loadProfiles]
  );

  const handleClipUpdated = useCallback((jobId, updatedClip) => {
    setJobs((previous) =>
      previous.map((job) =>
        job.id === jobId
          ? {
              ...job,
              clips: job.clips.map((clip) => (clip.id === updatedClip.id ? updatedClip : clip)),
            }
          : job
      )
    );
  }, []);

  const poll = useCallback(async () => {
    try {
      const [{ jobs: allJobs }, history] = await Promise.all([api.listJobs(), api.history()]);
      setJobs(allJobs);
      setPublishAttempts(history.publish_attempts || []);
      // Recovered. Reset rather than decrement: the banner is about the backend being
      // unreachable *now*, and one good answer means it is not.
      setPollFailures(0);
    } catch {
      // The last known state is deliberately kept through a transient failure — a job list that
      // blanks out because one poll missed is worse than a slightly stale one.
      //
      // But *only* transient. This `catch` used to be empty, which meant a backend that had
      // stopped answering entirely was indistinguishable from one with nothing to report: the
      // job cards simply froze at whatever they last showed, and the user's reasonable reading of
      // that is "my render is stuck", not "the server is gone". Counting consecutive failures is
      // what separates the two, and the count is what the banner is gated on.
      setPollFailures((previous) => previous + 1);
    }
  }, []);

  // I11: derived outside the effect so the effect can depend on a *boolean* rather than on the
  // jobs array. Depending on `jobs` directly would tear down and rebuild the interval on every
  // poll, since each response is a new array; depending on `jobs.length` — the previous
  // suppression — silently missed the case that matters, because a job going from processing to
  // completed does not change the count, so the fast 1.2s poll continued indefinitely after
  // everything had finished. This fixes that as well as the warning.
  const hasActiveJobs = useMemo(
    () => jobs.some((job) => ["queued", "processing"].includes(job.status)),
    [jobs]
  );

  /**
   * Apply an incremental frame from the event stream.
   *
   * Merge by id rather than replace, because the frame contains only the jobs that changed.
   * Re-sorted newest-first on `created_at` so the order matches what `GET /api/jobs` returns —
   * the rest of the app reads `jobs` as an ordered list, and a merge that appended would put a
   * newly-started job at the bottom while a full refetch put it at the top.
   */
  const mergeJobs = useCallback((changed) => {
    setJobs((previous) => {
      const byId = new Map(previous.map((job) => [job.id, job]));
      changed.forEach((job) => byId.set(job.id, job));
      return [...byId.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    });
  }, []);

  // Job progress over SSE (Phase 5.5).
  //
  // Note what this effect does *not* depend on: `hasActiveJobs`. The 1200/4000ms split below
  // exists only because a poll has to choose a rate, and the rate has to change when work starts
  // or stops. A stream has no rate — the server sends a frame when something moves — so the
  // "is anything running" question stops being an input, and the connection is no longer torn
  // down and rebuilt every time a job crosses the queued/processing boundary.
  useEffect(() => {
    if (!useStream) return undefined;
    // Same arming condition as the poll: an idle tab that has submitted nothing opens nothing.
    if (trackedIds.size === 0 && !watch.enabled) return undefined;

    const controller = new AbortController();
    let cancelled = false;
    let retryTimer = null;

    const connect = async () => {
      try {
        await api.jobEvents({
          signal: controller.signal,
          onSnapshot: (all) => {
            // Authoritative, so replace. This is what makes a reconnect self-healing: whatever
            // was missed while disconnected is superseded rather than reconciled.
            setJobs(all);
            setPollFailures(0);
            streamAttemptsRef.current = 0;
          },
          onJobs: (changed) => {
            mergeJobs(changed);
            setPollFailures(0);
          },
        });
        // A clean close is not an error — a server restart looks like this — so reconnect.
        if (!cancelled) retryTimer = setTimeout(connect, STREAM_RETRY_MS);
      } catch (error) {
        // Our own teardown, not a failure. Reporting it would show the unreachable-backend
        // banner every time the user navigated away.
        if (cancelled || error?.name === "AbortError") return;
        streamAttemptsRef.current += 1;
        setPollFailures((previous) => previous + 1);
        if (streamAttemptsRef.current >= STREAM_FALLBACK_AFTER) {
          setUseStream(false);
          return;
        }
        retryTimer = setTimeout(connect, STREAM_RETRY_MS);
      }
    };
    connect();

    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
      controller.abort();
    };
  }, [useStream, trackedIds, watch.enabled, mergeJobs]);

  // Publish attempts, while the stream is carrying jobs. The fallback `poll` fetches these in the
  // same pass, so this runs only when the stream has taken its place. See PUBLISH_ATTEMPT_POLL_MS.
  useEffect(() => {
    if (!useStream) return undefined;
    if (trackedIds.size === 0 && !watch.enabled) return undefined;
    const load = () =>
      api
        .history()
        .then((history) => setPublishAttempts(history.publish_attempts || []))
        // Swallowed on purpose: the unreachable-backend banner is driven by the job stream, which
        // is the connection that matters. A failure here would double-count the same outage.
        .catch(() => {});
    load();
    const id = setInterval(load, PUBLISH_ATTEMPT_POLL_MS);
    return () => clearInterval(id);
  }, [useStream, trackedIds, watch.enabled]);

  // The polling fallback. Documented as a fallback rather than removed: it is what runs when the
  // stream cannot be established (see STREAM_FALLBACK_AFTER), and it is the only path that works
  // behind an intermediary that buffers responses.
  useEffect(() => {
    if (useStream) return undefined;
    const active = watch.enabled || hasActiveJobs;
    if (pollRef.current) clearInterval(pollRef.current);
    if (trackedIds.size > 0 || watch.enabled) {
      poll();
      pollRef.current = setInterval(poll, active ? 1200 : 4000);
    }
    return () => pollRef.current && clearInterval(pollRef.current);
  }, [useStream, trackedIds, watch.enabled, poll, hasActiveJobs]);

  const handlePreview = useCallback(async (url) => {
    setPreview(null);
    setPreviewLoading(true);
    try {
      setPreview(await api.preview(url));
    } catch {
      setPreview(null);
    } finally {
      setPreviewLoading(false);
    }
  }, []);

  const track = (newJobs) =>
    setTrackedIds((previous) => {
      const next = new Set(previous);
      newJobs.forEach((job) => next.add(job.id));
      return next;
    });

  const handleGetClips = async () => {
    setError("");
    const options = toOptions(settings, publishing);
    const { urls, files } = input;

    if (files.length === 0 && urls.length === 0) {
      setError("Add a video URL or upload a file first.");
      return;
    }
    if (publishing.mode === "auto" && publishing.platforms.length > 0 && !publishing.campaign_id) {
      setError("Auto publishing requires a saved campaign so each platform has a routing target.");
      return;
    }

    setSubmitting(true);
    try {
      let created = [];
      if (files.length > 0) {
        const result = await api.upload(files, options);
        created = result.jobs || [];
      } else if (urls.length === 1) {
        created = [await api.submitUrl(urls[0], options)];
      } else {
        const result = await api.submitBatch(urls, options);
        created = result.jobs || [];
      }
      track(created);
      setJobs((previous) => [...created, ...previous]);
    } catch (submissionError) {
      setError(submissionError.message || "Failed to submit.");
    } finally {
      setSubmitting(false);
    }
  };

  const handleToggleWatch = async (enabled) => {
    try {
      setWatch(await api.watchToggle(enabled, toOptions(settings, publishing)));
    } catch (toggleError) {
      setError(toggleError.message || "Watch toggle failed.");
    }
  };

  const handleCampaignSaved = (campaign) => {
    setCampaigns((previous) => [campaign, ...previous.filter((item) => item.id !== campaign.id)]);
  };

  const handlePublished = (attempts) => {
    setPublishAttempts((previous) => [
      ...(attempts || []),
      ...previous.filter((item) => !(attempts || []).some((next) => next.id === item.id)),
    ]);
  };

  const visibleJobs = jobs.filter(
    (job) => trackedIds.has(job.id) || job.source?.includes("/watch/")
  );

  return (
    <div className="min-h-full bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-6xl px-6 py-10">
        <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="bg-gradient-to-r from-brand-accent to-brand bg-clip-text text-3xl font-bold text-transparent">
              AI Video Clipper
            </h1>
            <p className="mt-1 text-slate-400">
              Create, package, schedule, and publish short-form clips.
              {version && <span className="ml-2 text-xs text-slate-600">v{version}</span>}
            </p>
          </div>
          <nav className="flex rounded-xl border border-slate-800 bg-slate-900 p-1">
            {[
              ["create", "Create"],
              ["schedule", "Schedule"],
              ["history", "History"],
              ["settings", "Settings"],
            ].map(([id, label]) => (
              <button
                key={id}
                type="button"
                onClick={() => setActiveView(id)}
                className={`rounded-lg px-4 py-2 text-sm font-medium transition ${
                  activeView === id ? "bg-brand text-white" : "text-slate-400 hover:text-white"
                }`}
              >
                {label}
              </button>
            ))}
          </nav>
        </header>

        {/* A backend that has stopped answering, said out loud.
Above the view switch rather than inside the create view, because it applies to every view:
the schedule, the history and the storage panel all poll the same server, and each of them
degrades to "nothing to show" when it is gone. Placed beside the update notice because they
are the same kind of thing — a statement about the installation rather than about a job. */}
        {pollFailures >= BACKEND_UNREACHABLE_AFTER && (
          <div
            role="alert"
            className="mb-6 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-rose-700/60 bg-rose-950/40 p-3 text-sm text-rose-200"
          >
            <span>
              ⚠ Cannot reach the backend — {pollFailures} consecutive attempts failed. The figures
              below are the last known state and are no longer being updated. Any job still shown as
              running may have finished, failed, or never started.
            </span>
            <button
              type="button"
              onClick={() => poll()}
              className="rounded-lg border border-rose-600 px-3 py-1 font-medium hover:bg-rose-900/40"
            >
              Retry now
            </button>
          </div>
        )}

        {updateInfo?.update_available && (
          <div className="mb-6 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-700/60 bg-amber-950/40 p-3 text-sm text-amber-200">
            <span>
              🎉 Update available — v{updateInfo.latest} is out (you're on v{updateInfo.current}).
              Update with{" "}
              <code className="rounded bg-slate-900 px-1">
                git pull &amp;&amp; docker compose up --build
              </code>
              .
            </span>
            <a
              href={updateInfo.html_url}
              target="_blank"
              rel="noreferrer"
              className="rounded-lg border border-amber-600 px-3 py-1 font-medium hover:bg-amber-900/40"
            >
              Release notes
            </a>
          </div>
        )}

        {/* PB7: the schedule was previously a single datetime input with no way to
            see, move or cancel what had been scheduled. */}
        {activeView === "schedule" && <ScheduleCalendar onError={setError} />}

        {activeView === "history" && <HistoryView />}

        {activeView === "settings" && (
          <div className="space-y-6">
            <StorageSettings />
            <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5 text-sm text-slate-400">
              <div className="mb-1 font-semibold text-slate-300">Version &amp; updates</div>
              <p>
                Running <span className="text-slate-200">v{version || "?"}</span>
                {updateInfo?.latest ? ` · latest v${updateInfo.latest}` : ""}
                {updateInfo && !updateInfo.update_available && updateInfo.latest
                  ? " · up to date ✓"
                  : ""}
              </p>
              <p className="mt-1 text-xs text-slate-500">
                Update the app with{" "}
                <code className="rounded bg-slate-950 px-1">
                  git pull &amp;&amp; docker compose up --build
                </code>
                .
              </p>
            </div>
          </div>
        )}

        {activeView === "create" && (
          <>
            <div className="space-y-4">
              <ProfilesBar
                profiles={profiles}
                defaultId={defaultProfileId}
                activeId={activeProfileId}
                onApply={applyProfile}
                onSave={handleSaveProfile}
                onSetDefault={handleSetDefaultProfile}
                onDelete={handleDeleteProfile}
              />
              <InputBar onChange={setInput} onPreview={handlePreview} />
              {input.files.length === 0 && (
                <PreviewCard preview={preview} loading={previewLoading} />
              )}
              <SettingsPanel
                settings={settings}
                onChange={setSettings}
                watch={watch}
                onToggleWatch={handleToggleWatch}
                effects={effects}
                engines={engines}
                capabilities={capabilities}
              />
              <PublishingPanel
                value={publishing}
                onChange={setPublishing}
                statuses={publisherStatuses}
                campaigns={campaigns}
                onCampaignSaved={handleCampaignSaved}
              />

              <button
                onClick={handleGetClips}
                disabled={submitting}
                className="w-full rounded-xl bg-emerald-500 py-4 text-lg font-bold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {submitting ? "Submitting…" : "Get Clips"}
              </button>

              {error && (
                <div className="rounded-lg border border-rose-800 bg-rose-950/40 p-3 text-sm text-rose-300">
                  {error}
                </div>
              )}
            </div>

            {visibleJobs.length > 0 && (
              <section className="mt-10 space-y-4">
                <h2 className="text-lg font-semibold text-slate-200">
                  {watch.enabled ? "Jobs (watch-folder active)" : "Jobs"}
                </h2>
                {visibleJobs.map((job) => (
                  <JobCard
                    key={job.id}
                    job={job}
                    llmAvailable={llmAvailable}
                    publishing={publishing}
                    publisherStatuses={publisherStatuses}
                    publishAttempts={publishAttempts}
                    onClipUpdated={handleClipUpdated}
                    onPublished={handlePublished}
                    // U7: a re-render applies whatever is selected in the panel right now, which
                    // is what makes "change one setting and see it" a single click.
                    settings={toOptions(settings, publishing)}
                  />
                ))}
              </section>
            )}
          </>
        )}

        <footer className="mt-12 border-t border-slate-800 pt-6 text-xs text-slate-500">
          You are responsible for holding the rights to any source footage you process. See the
          README for content and copyright guidance.
        </footer>
      </div>
    </div>
  );
}
