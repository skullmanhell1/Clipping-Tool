import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, resolveLanguage } from "./api.js";
import HistoryView from "./components/HistoryView.jsx";
import InputBar from "./components/InputBar.jsx";
import BoxablCampaignPanel from "./components/BoxablCampaignPanel.jsx";
import CampaignAutomationPanel from "./components/CampaignAutomationPanel.jsx";
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
  brand_logo_background: "auto",
  brand_intro_asset: "",
  brand_outro_asset: "",
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
  retention_mode: "off",
  audience_reactions: "",
  permissibility_mode: false,
  campaign_preset: {},
  storytelling_enabled: false,
  public_visibility: "public",
  // Speaker diarisation & multi-speaker reframe (all default OFF / follow_active / standard)
  diarization: false,
  speaker_reframe: false,
  reframe_layout: "follow_active",
  reframe_intensity: "standard",
  face_detector: "haar",
  profile: "",
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
  affiliate_url: "",
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
    campaign_preset: settings.campaign_preset || {},
    storytelling_enabled: settings.storytelling_enabled,
    public_visibility: "public",
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
    brand_logo_background: settings.brand_logo_background,
    brand_intro_asset: settings.brand_intro_asset,
    brand_outro_asset: settings.brand_outro_asset,
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
    retention_mode: settings.retention_mode,
    audience_reactions: settings.audience_reactions,
    permissibility_mode: settings.permissibility_mode,
    // Speaker diarisation & multi-speaker reframe
    diarization: settings.diarization,
    speaker_reframe: settings.speaker_reframe,
    reframe_layout: settings.reframe_layout,
    reframe_intensity: settings.reframe_intensity,
    face_detector: settings.face_detector,
    profile: settings.profile,
    // Advanced AV engines — forwarded generically from DEFAULT_ENGINE_SETTINGS
    ...engineOptions(settings),
  };
}

const FORMAT_PRESETS = [
  {
    id: "shorts",
    label: "YouTube Shorts",
    detail: "Vertical · 9:16",
    glyph: "S",
    color: "text-red-300 bg-red-400/10",
    values: { aspect: "9:16", platform: "youtube" },
  },
  {
    id: "tiktok",
    label: "TikTok",
    detail: "Vertical · 9:16",
    glyph: "♪",
    color: "text-cyan-200 bg-cyan-400/10",
    values: { aspect: "9:16", platform: "tiktok" },
  },
  {
    id: "reels",
    label: "Instagram Reels",
    detail: "Vertical · 9:16",
    glyph: "◎",
    color: "text-pink-200 bg-pink-400/10",
    values: { aspect: "9:16", platform: "instagram" },
  },
  {
    id: "wide",
    label: "YouTube / landscape",
    detail: "Widescreen · 16:9",
    glyph: "▰",
    color: "text-violet-200 bg-violet-400/10",
    values: { aspect: "16:9", platform: "youtube" },
  },
];

function WorkflowHeading({ number, title, description }) {
  return (
    <div className="mb-4 flex items-start gap-3">
      <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-xl border border-violet-400/20 bg-violet-400/10 text-xs font-bold text-violet-200">
        {number}
      </span>
      <div>
        <h3 className="text-base font-semibold text-white">{title}</h3>
        <p className="mt-1 text-sm text-slate-400">{description}</p>
      </div>
    </div>
  );
}

function StatCard({ label, value, detail, tone }) {
  return (
    <div className="rounded-2xl border border-white/[0.07] bg-slate-900/65 p-4 shadow-lg shadow-black/10">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">
          {label}
        </span>
        <span className={`h-1.5 w-1.5 rounded-full ${tone}`} aria-hidden="true" />
      </div>
      <div className="mt-3 text-2xl font-semibold tracking-tight text-white">{value}</div>
      <p className="mt-1 text-xs text-slate-500">{detail}</p>
    </div>
  );
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
  const [jobsLoaded, setJobsLoaded] = useState(false);
  const [apiStatus, setApiStatus] = useState("checking");
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
  const [driveConfigured, setDriveConfigured] = useState(false);
  const [builtinProfiles, setBuiltinProfiles] = useState([]);
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
    const profileSettings = { ...DEFAULT_SETTINGS, ...(profile.settings || {}) };
    setSettings(profileSettings);
    setPublishing({
      ...DEFAULT_PUBLISHING,
      ...(profile.publishing || {}),
      ...(profileSettings.profile === "boxabl_campaign" ? { mode: "review" } : {}),
    });
  }, [profiles]);

  const applyBoxablCampaign = useCallback(() => {
    const profile = builtinProfiles.find((item) => item.name === "boxabl_campaign");
    if (!profile) return;
    setActiveProfileId("");
    setSettings({ ...DEFAULT_SETTINGS, ...(profile.settings || {}), profile: profile.name });
    setPublishing((current) => ({
      ...DEFAULT_PUBLISHING,
      ...current,
      platforms: [],
      campaign_id: "",
      mode: "review",
    }));
  }, [builtinProfiles]);

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
        setApiStatus("connected");
        setLlmAvailable(!!info.llm_available);
        setVersion(info.version || "");
        setEffects(info.effects || null);
        setEngines(Array.isArray(info.engines) ? info.engines : []);
        setCapabilities(info.capabilities || null);
        setDriveConfigured(!!info.google_drive?.configured);
      })
      .catch(() => setApiStatus("offline"));
    api.builtinProfiles().then((data) => setBuiltinProfiles(data.profiles || [])).catch(() => {});
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

  // Jobs are stored by the backend; restore the latest projects so a refresh or a
  // browser restart does not make active renders or completed clips appear lost.
  useEffect(() => {
    let mounted = true;
    api
      .listJobs()
      .then(({ jobs: savedJobs = [] }) => {
        if (!mounted) return;
        const recent = Array.isArray(savedJobs) ? savedJobs.slice(0, 12) : [];
        setJobs(recent);
        setTrackedIds(new Set(recent.map((job) => job.id).filter(Boolean)));
      })
      .catch(() => {})
      .finally(() => {
        if (mounted) setJobsLoaded(true);
      });
    return () => {
      mounted = false;
    };
  }, []);

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
      newJobs.forEach((job) => {
        if (job?.id) next.add(job.id);
      });
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
    let submittedAny = false;
    let firstSubmission = true;
    const addJobs = (newJobs, replaceProject = false) => {
      const validJobs = (newJobs || []).filter((job) => job?.id);
      if (!validJobs.length) return;
      submittedAny = true;
      if (replaceProject) {
        setTrackedIds(new Set(validJobs.map((job) => job.id)));
        setPublishAttempts([]);
      } else {
        track(validJobs);
      }
      setJobs((previous) => {
        const newIds = new Set(validJobs.map((job) => job.id));
        return replaceProject
          ? validJobs
          : [...validJobs, ...previous.filter((job) => !newIds.has(job.id))];
      });
    };

    try {
      // Uploads and links are separate API operations. Submit both kinds instead of
      // silently ignoring whichever source type happened to come second in the form.
      if (files.length > 0) {
        const result = await api.upload(files, { ...options, new_project: firstSubmission });
        addJobs(result.jobs || [], firstSubmission);
        firstSubmission = false;
      }
      if (urls.length === 1) {
        addJobs(
          [await api.submitUrl(urls[0], { ...options, new_project: firstSubmission })],
          firstSubmission,
        );
        firstSubmission = false;
      } else if (urls.length > 1) {
        const result = await api.submitBatch(urls, { ...options, new_project: firstSubmission });
        addJobs(result.jobs || [], firstSubmission);
        firstSubmission = false;
      }
    } catch (submissionError) {
      const message = submissionError.message || "Failed to submit.";
      setError(
        submittedAny
          ? `${message} The sources already submitted are in Recent work.`
          : message
      );
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

  const handlePresetApplied = (preset) => {
    setSettings((previous) => ({
      ...previous,
      campaign_preset: preset,
      storytelling_enabled: true,
      aspect: preset.aspect_ratios?.[0] || previous.aspect,
      brand_logo: preset.branding?.logo || previous.brand_logo,
      brand_font: preset.branding?.font || previous.brand_font,
      brand_primary_color: preset.branding?.colors?.primary || previous.brand_primary_color,
      brand_highlight_color: preset.branding?.colors?.accent || previous.brand_highlight_color,
      brand_cta: preset.cta || previous.brand_cta,
      caption_preset: preset.caption_style || previous.caption_preset,
    }));
    setPublishing((previous) => ({
      ...previous,
      campaign_id: previous.campaign_id || "",
      mode: "review",
    }));
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
  const sourceCount = input.urls.length + input.files.length;
  const activeJobCount = visibleJobs.filter((job) => ["queued", "processing"].includes(job.status)).length;
  const readyClipCount = visibleJobs.reduce(
    (count, job) => count + (job.status === "completed" ? (job.clips || []).length : 0),
    0
  );
  const approvedClipCount = visibleJobs.reduce(
    (count, job) => count + (job.clips || []).filter((clip) => clip.review_state === "approved").length,
    0
  );
  const activePreset = FORMAT_PRESETS.find((preset) =>
    Object.entries(preset.values).every(([key, value]) => settings[key] === value)
  );

  const applyFormatPreset = (preset) => {
    setSettings((previous) => ({ ...previous, ...preset.values }));
    setActiveProfileId("");
  };

  return (
    <div className="relative min-h-screen overflow-x-hidden bg-[#090b10] text-slate-100 selection:bg-violet-400/30">
      <div className="pointer-events-none fixed inset-0 z-0 overflow-hidden" aria-hidden="true">
        <div className="absolute -left-40 -top-48 h-[34rem] w-[34rem] rounded-full bg-violet-600/[0.09] blur-[120px]" />
        <div className="absolute -right-40 top-[30rem] h-[28rem] w-[28rem] rounded-full bg-cyan-500/[0.06] blur-[120px]" />
      </div>
      <div className="relative z-10 mx-auto max-w-7xl px-4 pb-10 pt-4 sm:px-6 lg:px-8">
        <header className="sticky top-3 z-30 mb-8 flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-white/[0.08] bg-[#0d1018]/90 p-3 shadow-2xl shadow-black/20 backdrop-blur-xl">
          <div className="flex min-w-0 items-center gap-3 px-1">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 to-indigo-600 shadow-lg shadow-violet-950/50">
              <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5 text-white" aria-hidden="true">
                <rect x="3" y="5" width="18" height="14" rx="3" stroke="currentColor" strokeWidth="1.8" />
                <path d="m10 9 5 3-5 3V9Z" fill="currentColor" />
              </svg>
            </span>
            <div className="min-w-0">
              <h1 className="truncate text-sm font-semibold tracking-tight text-white sm:text-base">AI Video Clipper</h1>
              <p className="text-[10px] font-medium uppercase tracking-[0.15em] text-slate-500">Creator studio{version ? ` · v${version}` : ""}</p>
            </div>
          </div>

          <nav aria-label="Primary" className="order-3 flex w-full items-center gap-1 overflow-x-auto rounded-xl border border-white/[0.06] bg-black/20 p-1 sm:order-none sm:w-auto">
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
                aria-pressed={activeView === id}
                className={`shrink-0 rounded-lg px-3 py-2 text-xs font-semibold transition sm:px-4 sm:text-sm ${
                  activeView === id
                    ? "bg-white/[0.1] text-white shadow-sm"
                    : "text-slate-500 hover:bg-white/[0.04] hover:text-slate-200"
                }`}
              >
                {label}
              </button>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2 rounded-full border border-white/[0.07] bg-black/20 px-3 py-2 text-xs text-slate-400 sm:ml-0">
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                apiStatus === "connected" ? "bg-emerald-400" : apiStatus === "offline" ? "bg-rose-400" : "animate-pulse bg-amber-400"
              }`}
              aria-hidden="true"
            />
            <span>{apiStatus === "connected" ? "System ready" : apiStatus === "offline" ? "Server unavailable" : "Connecting"}</span>
          </div>
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

        <main className="space-y-10">
          {/* PB7: the schedule is a movable, cancellable publishing calendar. */}
          {activeView === "schedule" && (
            <section className="space-y-6">
              <div>
                <p className="text-xs font-semibold uppercase tracking-[0.18em] text-violet-300">Publishing workspace</p>
                <h2 className="mt-2 text-3xl font-semibold tracking-tight text-white">Plan your next posts</h2>
                <p className="mt-2 max-w-2xl text-sm text-slate-400">See what is queued, find a good time, and adjust your publishing calendar in one place.</p>
              </div>
              <ScheduleCalendar onError={setError} />
            </section>
          )}

          {activeView === "history" && (
            <section className="space-y-6">
              <div>
                <p className="text-xs font-semibold uppercase tracking-[0.18em] text-violet-300">Publishing workspace</p>
                <h2 className="mt-2 text-3xl font-semibold tracking-tight text-white">Publishing history</h2>
                <p className="mt-2 max-w-2xl text-sm text-slate-400">Track what went live, retry failed posts, and follow up on items waiting for review.</p>
              </div>
              <HistoryView />
            </section>
          )}

          {activeView === "settings" && (
            <section className="space-y-6">
              <div>
                <p className="text-xs font-semibold uppercase tracking-[0.18em] text-violet-300">Workspace</p>
                <h2 className="mt-2 text-3xl font-semibold tracking-tight text-white">Storage &amp; system settings</h2>
                <p className="mt-2 max-w-2xl text-sm text-slate-400">Manage saved media, retention, and the version running in your studio.</p>
              </div>
              <StorageSettings />
              <div className="rounded-2xl border border-white/[0.07] bg-slate-900/65 p-5 text-sm text-slate-400">
                <div className="mb-1 font-semibold text-slate-200">Version &amp; updates</div>
                <p>
                  Running <span className="text-slate-100">v{version || "?"}</span>
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
            </section>
          )}

          {activeView === "create" && (
            <>
              <section className="grid gap-8 lg:grid-cols-[minmax(0,1.35fr)_minmax(350px,0.8fr)] lg:items-end">
                <div className="max-w-3xl">
                  <div className="inline-flex items-center gap-2 rounded-full border border-violet-300/15 bg-violet-400/[0.06] px-3 py-1.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-violet-200">
                    <span className="h-1.5 w-1.5 rounded-full bg-violet-300" aria-hidden="true" />
                    Your creator studio
                  </div>
                  <h2 className="mt-5 text-4xl font-semibold leading-[1.08] tracking-tight text-white sm:text-5xl">
                    One video.
                    <span className="block bg-gradient-to-r from-violet-300 via-fuchsia-200 to-cyan-200 bg-clip-text text-transparent">Your best moments, ready to share.</span>
                  </h2>
                  <p className="mt-4 max-w-2xl text-sm leading-6 text-slate-400 sm:text-base">
                    Find the moments worth sharing, turn them into polished vertical videos, then review and publish — all from one workspace.
                  </p>
                  <div className="mt-5 flex flex-wrap gap-2 text-xs text-slate-400">
                    {["AI-picked highlights", "Dynamic captions", "Review before publishing"].map((feature) => (
                      <span key={feature} className="rounded-full border border-white/[0.07] bg-white/[0.025] px-3 py-1.5">{feature}</span>
                    ))}
                  </div>
                </div>

                <div className="grid grid-cols-3 gap-2 sm:gap-3" aria-label="Recent project summary">
                  <StatCard
                    label="In progress"
                    value={jobsLoaded ? activeJobCount : "—"}
                    detail={jobsLoaded ? "Active projects" : "Restoring work…"}
                    tone="bg-amber-300"
                  />
                  <StatCard
                    label="Clips ready"
                    value={jobsLoaded ? readyClipCount : "—"}
                    detail="From recent projects"
                    tone="bg-emerald-300"
                  />
                  <StatCard
                    label="Approved"
                    value={jobsLoaded ? approvedClipCount : "—"}
                    detail="Marked for publishing"
                    tone="bg-cyan-300"
                  />
                </div>
              </section>

              <section className="space-y-5">
                <BoxablCampaignPanel
                  active={settings.profile === "boxabl_campaign"}
                  driveConfigured={driveConfigured}
                  profileAvailable={builtinProfiles.some((item) => item.name === "boxabl_campaign")}
                  onApply={applyBoxablCampaign}
                />
                <CampaignAutomationPanel
                  campaigns={campaigns}
                  publisherStatuses={publisherStatuses}
                  jobs={jobs}
                  driveConfigured={driveConfigured}
                  onPresetApplied={handlePresetApplied}
                  onScheduled={loadPublishingData}
                />
                <ProfilesBar
                  profiles={profiles}
                  defaultId={defaultProfileId}
                  activeId={activeProfileId}
                  onApply={applyProfile}
                  onSave={handleSaveProfile}
                  onSetDefault={handleSetDefaultProfile}
                  onDelete={handleDeleteProfile}
                />

                <section>
                  <WorkflowHeading
                    number="01"
                    title="Choose your source"
                    description="Paste a link, upload a file, or combine several videos in one batch."
                  />
                  <InputBar onChange={setInput} onPreview={handlePreview} />
                  {input.urls.length === 1 && (
                    <div className="mt-4">
                      <PreviewCard preview={preview} loading={previewLoading} />
                    </div>
                  )}
                </section>

                <section>
                  <WorkflowHeading
                    number="02"
                    title="Set the format and style"
                    description="Start with a platform-ready preset, then fine-tune any of the advanced options."
                  />
                  <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-4 sm:gap-3" role="group" aria-label="Quick format presets">
                    {FORMAT_PRESETS.map((preset) => {
                      const selected = activePreset?.id === preset.id;
                      return (
                        <button
                          key={preset.id}
                          type="button"
                          onClick={() => applyFormatPreset(preset)}
                          aria-pressed={selected}
                          className={`group flex min-h-[92px] items-start gap-3 rounded-2xl border p-3 text-left transition sm:p-4 ${
                            selected
                              ? "border-violet-300/50 bg-violet-400/[0.08] shadow-lg shadow-violet-950/20"
                              : "border-white/[0.07] bg-slate-900/55 hover:border-white/20 hover:bg-slate-900"
                          }`}
                        >
                          <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-sm font-bold ${preset.color}`} aria-hidden="true">
                            {preset.glyph}
                          </span>
                          <span className="min-w-0">
                            <span className="block text-xs font-semibold leading-4 text-slate-100 sm:text-sm">{preset.label}</span>
                            <span className="mt-1 block text-[10px] text-slate-500 sm:text-xs">{preset.detail}</span>
                          </span>
                        </button>
                      );
                    })}
                  </div>
                  <SettingsPanel
                    settings={settings}
                    onChange={setSettings}
                    watch={watch}
                    onToggleWatch={handleToggleWatch}
                    effects={effects}
                    engines={engines}
                    capabilities={capabilities}
                  />
                </section>

                <section>
                  <WorkflowHeading
                    number="03"
                    title="Choose how to publish"
                    description="Keep clips in review mode or route them to a saved publishing campaign."
                  />
                  <PublishingPanel
                    value={publishing}
                    onChange={setPublishing}
                    campaignProfile={settings.profile}
                    statuses={publisherStatuses}
                    campaigns={campaigns}
                    onCampaignSaved={handleCampaignSaved}
                  />
                </section>

                <div className="sticky bottom-3 z-20 rounded-2xl border border-white/[0.1] bg-[#12151e]/95 p-3 shadow-2xl shadow-black/40 backdrop-blur-xl sm:p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0 px-1">
                      <p className="text-sm font-semibold text-white" aria-live="polite">
                        {sourceCount === 0
                          ? "Your next project starts with a video"
                          : `${sourceCount} source${sourceCount === 1 ? "" : "s"} ready to clip`}
                      </p>
                      <p className="mt-1 text-xs text-slate-500">
                        {sourceCount === 0
                          ? "Add a link or upload a video above to get started."
                          : `${settings.aspect} format · Captions ${settings.captions ? "on" : "off"} · ${publishing.mode === "review" ? "Review before publishing" : "Auto publish"}`}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={handleGetClips}
                      disabled={submitting || sourceCount === 0}
                      aria-busy={submitting}
                      className="flex shrink-0 items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-violet-500 to-indigo-500 px-6 py-3.5 text-sm font-semibold text-white shadow-lg shadow-violet-950/40 transition hover:from-violet-400 hover:to-indigo-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-300 focus-visible:ring-offset-2 focus-visible:ring-offset-[#12151e] disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {submitting ? (
                        <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity=".25" strokeWidth="3" />
                          <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
                        </svg>
                      ) : (
                        <span aria-hidden="true">✦</span>
                      )}
                      {submitting ? "Submitting videos…" : sourceCount === 0 ? "Add a source to begin" : "Generate clips"}
                      {!submitting && sourceCount > 0 ? <span aria-hidden="true">→</span> : null}
                    </button>
                  </div>
                  {error && (
                    <div role="alert" className="mt-3 rounded-xl border border-rose-400/20 bg-rose-400/[0.08] px-3 py-2 text-sm text-rose-200">
                      {error}
                    </div>
                  )}
                </div>
              </section>

              <section className="space-y-4" aria-labelledby="recent-work-heading">
                <div className="flex flex-wrap items-end justify-between gap-3">
                  <div>
                    <p className="text-xs font-semibold uppercase tracking-[0.17em] text-slate-500">Your projects</p>
                    <h2 id="recent-work-heading" className="mt-1 text-xl font-semibold text-white">Recent work</h2>
                  </div>
                  <p className="text-xs text-slate-500">
                    {watch.enabled ? "Watch-folder is active" : jobsLoaded ? `${visibleJobs.length} recent project${visibleJobs.length === 1 ? "" : "s"}` : "Restoring saved projects…"}
                  </p>
                </div>
                {visibleJobs.length > 0 ? (
                  <div className="space-y-4">
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
                        // U7: a re-render applies the settings selected above.
                        settings={toOptions(settings, publishing)}
                      />
                    ))}
                  </div>
                ) : (
                  <div className="flex flex-col items-start gap-3 rounded-2xl border border-dashed border-white/[0.09] bg-white/[0.02] p-6 sm:flex-row sm:items-center">
                    <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-white/[0.04] text-lg text-slate-400" aria-hidden="true">▣</span>
                    <div>
                      <p className="text-sm font-medium text-slate-200">
                        {jobsLoaded ? "Your projects will appear here" : "Restoring your workspace…"}
                      </p>
                      <p className="mt-1 text-xs text-slate-500">
                        {jobsLoaded ? "Generated clips and active renders stay available when you return." : "Checking for active renders and recently completed clips."}
                      </p>
                    </div>
                  </div>
                )}
              </section>
            </>
          )}
        </main>

        <footer className="mt-12 border-t border-slate-800 pt-6 text-xs text-slate-500">
          You are responsible for holding the rights to any source footage you process.
          See the README for content and copyright guidance.
        </footer>
      </div>
    </div>
  );
}
