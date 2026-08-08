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

// Advanced AV engines (Req 20.4): a sibling engine spec adds its
// `<engine_id>_enabled` flag and option defaults *here only* — `toOptions`
// forwards every key generically, and profiles persist them automatically
// because they round-trip through the opaque settings blob.
//
// Keys use the snake_case API spellings, because `engineOptions` forwards them
// verbatim to the `/api/upload` Form fields and `OptionsModel` — a camelCase key
// here would silently never reach the backend.
const DEFAULT_ENGINE_SETTINGS = {
  // Kinetic typography engine (kinetic-typography spec, Req 17.5). Defaults
  // mirror `ProcessingOptions` / `Kinetic_Options` exactly; the flag is off, so
  // a stock install still renders exactly as v0.8.0.
  kinetic_typography_enabled: false,
  kinetic_style: "karaoke_fill",
  kinetic_reveal: "cumulative",
  kinetic_font: "",
  kinetic_max_lines: 2,
  kinetic_max_line_width: 22,
  kinetic_safe_area_x_pct: 6.0,
  kinetic_safe_area_y_pct: 10.0,
  kinetic_motion_ms: 120,
  kinetic_confidence_floor: 0.0,

  // Stem inpainting engine (audio-stem-inpainting spec). Defaults mirror
  // `ProcessingOptions` / `Stem_Options` exactly; the flag is off, so a stock install
  // still renders exactly as v0.8.0. Listing every field here is what makes them reach
  // the backend and round-trip through saved profiles without a dedicated panel.
  stem_inpainting_enabled: false,
  stem_mix_preset: "custom",
  stem_gain_vocals: 1.0,
  stem_gain_music: 1.0,
  stem_gain_other: 1.0,
  stem_repair_mode: "crossfade",
  stem_repair_window_ms: 12,
  stem_declick: false,
  stem_backend: "auto",
  stem_model: "htdemucs",
  stem_retain_stems: false,
};

const engineOptions = (settings) =>
  Object.fromEntries(
    Object.keys(DEFAULT_ENGINE_SETTINGS).map((key) => [
      key,
      settings[key] === undefined ? DEFAULT_ENGINE_SETTINGS[key] : settings[key],
    ])
  );

const DEFAULT_SETTINGS = {
  language: "auto",
  clip_length: "auto",
  aspect: "9:16",
  num_clips: "auto",
  strategy: "ai",
  captions: true,
  subtitle_sidecar: false,
  topic: "",
  vocabulary: "",
  vibe: "",
  platform: "generic",
  hashtag_count: 5,
  range_start: "",
  range_end: "",
  metadata: true,
  // Phase 4 — visual effects (all individually toggleable). Defaults mirror
  // ProcessingOptions: U1 turned these on because shipping them off made the tool look
  // worse than it is capable of, and a panel that opens with them off undoes that for
  // every user who never opens it.
  caption_template: "karaoke",
  caption_position: "bottom",
  reframe: true,
  zoom: true,
  transitions: true,
  hook_title: true,
  fades: true,
  progress_bar: true,
  color: "",
  music: "",
  music_volume: 0.12,
  emoji: "standard",
  emoji_mode: "keyword",
  emoji_animate: true,
  filler_removal: false,
  // Tier 1 — animated captions / b-roll / visual selection (b-roll and AI keywording
  // stay off: one needs assets, the other costs an LLM call)
  caption_preset: "karaoke",
  // U6: the brand kit. Part of `settings` on purpose - saved profiles store the whole settings
  // blob, so a kit is saved, applied and set as default by machinery that already exists.
  brand_font: "",
  brand_primary_color: "",
  brand_highlight_color: "",
  brand_cta: "",
  brand_logo: "",
  brand_logo_position: "top_right",
  brand_logo_scale: 0.16,
  brand_logo_opacity: 0.85,
  caption_animation: "",
  caption_keyword_highlight: true,
  caption_keyword_ai: false,
  caption_emoji: true,
  broll: false,
  broll_intensity: "standard",
  asset_sourcing_mode: "off",
  broll_provider: "",
  selection_prompt: "",
  visual_selection: true,
  permissibility_mode: false,
  // Speaker diarisation & multi-speaker reframe (all default OFF / follow_active / standard)
  diarization: false,
  speaker_reframe: false,
  reframe_layout: "follow_active",
  reframe_intensity: "standard",
  face_detector: "haar",
  // Advanced AV engines — every flag/option default, forwarded generically
  ...DEFAULT_ENGINE_SETTINGS,
};

const DEFAULT_PUBLISHING = {
  platforms: [],
  campaign_id: "",
  mode: "review",
  schedule: "",
  account_id: "",
  target_type: "",
  target_id: "",
};

const numOrNull = (value) =>
  value === "" || value === null || value === undefined ? null : Number(value);

const scheduleToEpoch = (value) => {
  if (!value) return null;
  const milliseconds = new Date(value).getTime();
  return Number.isNaN(milliseconds) ? null : milliseconds / 1000;
};

function toOptions(settings, publishing) {
  const { language, translate } = resolveLanguage(settings.language);
  return {
    language,
    translate,
    clip_length: settings.clip_length,
    aspect: settings.aspect,
    num_clips: settings.num_clips,
    strategy: settings.strategy,
    captions: settings.captions,
    subtitle_sidecar: settings.subtitle_sidecar,
    topic: settings.topic,
    vocabulary: settings.vocabulary,
    vibe: settings.vibe,
    platform: settings.platform,
    hashtag_count: Number(settings.hashtag_count) || 0,
    range_start: numOrNull(settings.range_start),
    range_end: numOrNull(settings.range_end),
    metadata: settings.metadata,
    publish_to: publishing.mode === "auto" ? publishing.platforms : [],
    campaign_id: publishing.campaign_id,
    publish_mode: publishing.mode,
    schedule_at: scheduleToEpoch(publishing.schedule),
    // Phase 4 — visual effects
    caption_template: settings.caption_template,
    caption_position: settings.caption_position,
    reframe: settings.reframe,
    zoom: settings.zoom,
    transitions: settings.transitions,
    hook_title: settings.hook_title,
    fades: settings.fades,
    progress_bar: settings.progress_bar,
    color: settings.color,
    music: settings.music,
    music_volume: Number(settings.music_volume) || 0,
    emoji: settings.emoji,
    emoji_mode: settings.emoji_mode,
    emoji_animate: settings.emoji_animate,
    filler_removal: settings.filler_removal,
    // Tier 1 — animated captions / b-roll / visual selection
    caption_preset: settings.caption_preset,
    brand_font: settings.brand_font,
    brand_primary_color: settings.brand_primary_color,
    brand_highlight_color: settings.brand_highlight_color,
    brand_cta: settings.brand_cta,
    brand_logo: settings.brand_logo,
    brand_logo_position: settings.brand_logo_position,
    brand_logo_scale: settings.brand_logo_scale,
    brand_logo_opacity: settings.brand_logo_opacity,
    caption_animation: settings.caption_animation,
    caption_keyword_highlight: settings.caption_keyword_highlight,
    caption_keyword_ai: settings.caption_keyword_ai,
    caption_emoji: settings.caption_emoji,
    broll: settings.broll,
    broll_intensity: settings.broll_intensity,
    asset_sourcing_mode: settings.asset_sourcing_mode,
    broll_provider: settings.broll_provider,
    selection_prompt: settings.selection_prompt,
    visual_selection: settings.visual_selection,
    permissibility_mode: settings.permissibility_mode,
    // Speaker diarisation & multi-speaker reframe
    diarization: settings.diarization,
    speaker_reframe: settings.speaker_reframe,
    reframe_layout: settings.reframe_layout,
    reframe_intensity: settings.reframe_intensity,
    face_detector: settings.face_detector,
    // Advanced AV engines — forwarded generically from DEFAULT_ENGINE_SETTINGS
    ...engineOptions(settings),
  };
}

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
  const pollRef = useRef(null);
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

  const applyProfile = useCallback((id) => {
    setActiveProfileId(id);
    if (!id) return;
    const profile = profiles.find((p) => p.id === id);
    if (!profile) return;
    setSettings({ ...DEFAULT_SETTINGS, ...(profile.settings || {}) });
    setPublishing({ ...DEFAULT_PUBLISHING, ...(profile.publishing || {}) });
  }, [profiles]);

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
    api.watchStatus().then(setWatch).catch(() => {});
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
    api.updates().then(setUpdateInfo).catch(() => {});
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

  const handleSaveProfile = useCallback(async (name, id) => {
    const saved = await api.saveProfile({ name, id, settings, publishing });
    await loadProfiles();
    setActiveProfileId(saved.id);
  }, [settings, publishing, loadProfiles]);

  const handleSetDefaultProfile = useCallback(async (id) => {
    await api.setDefaultProfile(id);
    await loadProfiles();
  }, [loadProfiles]);

  const handleDeleteProfile = useCallback(async (id) => {
    await api.deleteProfile(id);
    if (id === activeProfileId) setActiveProfileId("");
    await loadProfiles();
  }, [activeProfileId, loadProfiles]);

  const handleClipUpdated = useCallback((jobId, updatedClip) => {
    setJobs((previous) =>
      previous.map((job) =>
        job.id === jobId
          ? {
              ...job,
              clips: job.clips.map((clip) =>
                clip.id === updatedClip.id ? updatedClip : clip
              ),
            }
          : job
      )
    );
  }, []);

  const poll = useCallback(async () => {
    try {
      const [{ jobs: allJobs }, history] = await Promise.all([
        api.listJobs(),
        api.history(),
      ]);
      setJobs(allJobs);
      setPublishAttempts(history.publish_attempts || []);
    } catch {
      // Keep the last known state through transient API failures.
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

  useEffect(() => {
    const active = watch.enabled || hasActiveJobs;
    if (pollRef.current) clearInterval(pollRef.current);
    if (trackedIds.size > 0 || watch.enabled) {
      poll();
      pollRef.current = setInterval(poll, active ? 1200 : 4000);
    }
    return () => pollRef.current && clearInterval(pollRef.current);
  }, [trackedIds, watch.enabled, poll, hasActiveJobs]);

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
              {version && (
                <span className="ml-2 text-xs text-slate-600">v{version}</span>
              )}
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
                  activeView === id
                    ? "bg-brand text-white"
                    : "text-slate-400 hover:text-white"
                }`}
              >
                {label}
              </button>
            ))}
          </nav>
        </header>

        {updateInfo?.update_available && (
          <div className="mb-6 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-700/60 bg-amber-950/40 p-3 text-sm text-amber-200">
            <span>
              🎉 Update available — v{updateInfo.latest} is out (you're on v
              {updateInfo.current}). Update with{" "}
              <code className="rounded bg-slate-900 px-1">git pull &amp;&amp; docker compose up --build</code>.
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
                <code className="rounded bg-slate-950 px-1">git pull &amp;&amp; docker compose up --build</code>.
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
          You are responsible for holding the rights to any source footage you process.
          See the README for content and copyright guidance.
        </footer>
      </div>
    </div>
  );
}
