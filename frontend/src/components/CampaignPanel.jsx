import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

const splitLines = (value) =>
  String(value || "")
    .split(/\r?\n|,/) 
    .map((item) => item.trim())
    .filter(Boolean);

function lengthSettings(rawValue) {
  const raw = String(rawValue || "");
  const compact = raw.toLowerCase().replace(/[–—]/g, "-").replace(/\s+/g, "");
  const numbers = (compact.match(/\d+(?:\.\d+)?/g) || []).map(Number);
  const length = { raw };
  if (/^</.test(compact) && numbers.length) {
    length.max_seconds = numbers[0];
  } else if (numbers.length >= 2) {
    length.min_seconds = numbers[0];
    length.max_seconds = numbers[1];
  } else if (numbers.length === 1 && /\+|more|over/.test(compact)) {
    length.min_seconds = numbers[0];
  }
  let clipLength = raw.trim() ? raw : "auto";
  if (/^<30(?:s|sec|secs|second|seconds)?$/.test(compact)) clipLength = "<30s";
  else if (/^30-60(?:s|sec|secs|second|seconds)?$/.test(compact)) clipLength = "30-60s";
  else if (/^60-90(?:s|sec|secs|second|seconds)?$/.test(compact)) clipLength = "60-90s";
  else if (/^90s?-3(?:min|m)$/.test(compact) || /^90-180(?:s|sec|secs|second|seconds)?$/.test(compact)) clipLength = "90s-3min";
  return { length, clipLength };
}

const today = () => new Date().toISOString().slice(0, 10);

const EMPTY_PRESET = {
  schema_version: 2,
  id: "",
  name: "",
  length: {},
  aspect_resolution: { aspect_ratios: [], resolutions: [] },
  platforms: [],
  captions_language: { required: true, languages: [], style: "" },
  copy_rules: { hashtags: [], mentions: [], required_copy: [], cta: "" },
  watermark_end_cards: { watermark: "", end_card: "" },
  banned_words_topics: { words: [], topics: [] },
  source_rules: { allowed: [], prohibited: [] },
  source_library: [],
  settings: {},
  posting: {
    platforms: [],
    manual_only: false,
    public_visibility_required: true,
    daily_clip_target: 2,
    timezone: "",
    peak_hours: ["12:00", "19:00"],
  },
  compliance: { required_checks: [] },
  source_links: [],
  warnings: [],
  dismissed_warnings: [],
  ambiguities: [],
  uncertainties: [],
};

const inputClass =
  "rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-violet-300";

const REQUIREMENT_FILE_EXTENSIONS = new Set(["txt", "md", "markdown", "csv", "json", "yaml", "yml"]);
const MAX_REQUIREMENT_FILE_BYTES = 2 * 1024 * 1024;
const MAX_REQUIREMENT_FILES = 10;
const STRATEGY_OPTIONS = {
  content_type: [
    ["", "Not specified"],
    ["interview", "Interview / podcast"],
    ["livestream", "Livestream / gaming"],
    ["webinar", "Webinar / presentation"],
    ["course", "Course / education"],
    ["demo", "Product demo"],
    ["commentary", "Commentary / reaction"],
  ],
  clip_goal: [
    ["", "Not specified"],
    ["growth", "Reach and growth"],
    ["education", "Teach a clear idea"],
    ["leads", "Drive leads / sales"],
    ["brand", "Build brand trust"],
    ["community", "Start conversation"],
  ],
  hook_style: [
    ["", "Not specified"],
    ["payoff_first", "Payoff first"],
    ["bold_claim", "Bold claim"],
    ["question", "Open question"],
    ["story", "Story setup"],
    ["contrarian", "Contrarian angle"],
  ],
  retention_mode: [
    ["off", "Standard ranking"],
    ["balanced", "Balanced retention"],
    ["aggressive", "Aggressive retention"],
  ],
};
const PRODUCTION_OPTIONS = {
  strategy: [
    ["ai", "AI highlights"],
    ["silence", "Silence-based"],
    ["fixed", "Fixed length"],
  ],
  vibe: [
    ["", "Auto"],
    ["energetic", "Energetic"],
    ["educational", "Educational"],
    ["funny", "Funny"],
    ["inspirational", "Inspirational"],
    ["dramatic", "Dramatic"],
    ["chill", "Chill"],
  ],
  caption_preset: [
    ["karaoke", "Karaoke"],
    ["boxed", "Boxed"],
    ["minimal", "Minimal"],
    ["pop", "Pop"],
    ["typewriter", "Typewriter"],
    ["hormozi", "Hormozi"],
  ],
  caption_position: [
    ["bottom", "Bottom"],
    ["center", "Center"],
    ["top", "Top"],
  ],
  broll_intensity: [
    ["off", "Off"],
    ["subtle", "Subtle"],
    ["standard", "Standard"],
    ["heavy", "Heavy"],
  ],
  asset_sourcing_mode: [
    ["off", "Off"],
    ["local_only", "Local only"],
    ["local_then_external", "Local, then external"],
  ],
  reframe_layout: [
    ["follow_active", "Follow active speaker"],
    ["split_screen", "Split screen"],
  ],
  reframe_intensity: [
    ["subtle", "Subtle"],
    ["standard", "Standard"],
    ["heavy", "Heavy"],
  ],
};
const CREATOR_RECIPES = [
  {
    id: "podcast_growth",
    label: "Podcast growth",
    description: "Strong openings, speaker-aware framing, punchy captions.",
    values: {
      content_type: "interview",
      clip_goal: "growth",
      hook_style: "payoff_first",
      strategy: "ai",
      vibe: "energetic",
      retention_mode: "balanced",
      storytelling_enabled: true,
      speaker_reframe: true,
      reframe_layout: "follow_active",
      reframe_intensity: "standard",
      caption_preset: "hormozi",
      caption_position: "bottom",
      caption_keyword_highlight: true,
      caption_emoji: false,
      visual_selection: true,
    },
  },
  {
    id: "education_authority",
    label: "Education authority",
    description: "Complete explanations, readable captions, low distraction.",
    values: {
      content_type: "course",
      clip_goal: "education",
      hook_style: "question",
      strategy: "ai",
      vibe: "educational",
      retention_mode: "balanced",
      storytelling_enabled: true,
      speaker_reframe: true,
      reframe_layout: "follow_active",
      reframe_intensity: "subtle",
      caption_preset: "karaoke",
      caption_position: "bottom",
      caption_keyword_highlight: true,
      caption_emoji: false,
      visual_selection: true,
      filler_removal: true,
    },
  },
  {
    id: "product_launch",
    label: "Product launch",
    description: "Benefit-led hooks, brand-safe visuals, clear CTA.",
    values: {
      content_type: "demo",
      clip_goal: "leads",
      hook_style: "bold_claim",
      strategy: "ai",
      vibe: "energetic",
      retention_mode: "aggressive",
      storytelling_enabled: true,
      speaker_reframe: false,
      caption_preset: "pop",
      caption_position: "bottom",
      caption_keyword_highlight: true,
      caption_emoji: false,
      visual_selection: true,
      broll: true,
      broll_intensity: "subtle",
      asset_sourcing_mode: "local_only",
      hook_title: true,
    },
  },
  {
    id: "clean_story",
    label: "Clean story",
    description: "Minimal treatment for thoughtful, high-trust clips.",
    values: {
      content_type: "commentary",
      clip_goal: "brand",
      hook_style: "story",
      strategy: "ai",
      vibe: "inspirational",
      retention_mode: "off",
      storytelling_enabled: true,
      speaker_reframe: false,
      caption_preset: "minimal",
      caption_position: "bottom",
      caption_keyword_highlight: false,
      caption_emoji: false,
      visual_selection: true,
      broll: false,
      asset_sourcing_mode: "off",
    },
  },
];

function requirementFileExtension(name) {
  return String(name || "").split(".").pop()?.toLowerCase() || "";
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** index;
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function isStructuredPresetJson(value) {
  return Boolean(
    value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      (value.schema_version || value.source_library || value.aspect_resolution || value.posting || value.settings),
  );
}

function selectLabel(options, value, fallback = "Not specified") {
  return options.find(([optionValue]) => optionValue === value)?.[1] || fallback;
}

function normalizePreset(value) {
  const preset = value?.preset || value?.profile || value || {};
  const normalizedSettings = { ...(preset.settings || {}) };
  if (Object.prototype.hasOwnProperty.call(normalizedSettings, "clip_length")) {
    normalizedSettings.clip_length = lengthSettings(normalizedSettings.clip_length).clipLength;
  }
  const sourceLibrary = Array.isArray(preset.source_library)
    ? preset.source_library.map((item, index) => {
        const source = item && typeof item === "object" ? item : { url: String(item || "") };
        const status = ["approved", "suggested", "banned"].includes(source.status)
          ? source.status
          : source.approved
            ? "approved"
            : "suggested";
        return {
          ...source,
          id: source.id || source.url || `source-${index}`,
          status,
          approved: status === "approved",
        };
      })
    : [];
  return {
    ...EMPTY_PRESET,
    ...preset,
    name: typeof preset.name === "string" ? preset.name : "",
    platforms: Array.isArray(preset.platforms) ? preset.platforms : [],
    aspect_resolution: {
      ...EMPTY_PRESET.aspect_resolution,
      ...(preset.aspect_resolution || {}),
      aspect_ratios: Array.isArray(preset.aspect_resolution?.aspect_ratios)
        ? preset.aspect_resolution.aspect_ratios
        : [],
      resolutions: Array.isArray(preset.aspect_resolution?.resolutions)
        ? preset.aspect_resolution.resolutions
        : [],
    },
    captions_language: {
      ...EMPTY_PRESET.captions_language,
      ...(preset.captions_language || {}),
    },
    copy_rules: { ...EMPTY_PRESET.copy_rules, ...(preset.copy_rules || {}) },
    watermark_end_cards: {
      ...EMPTY_PRESET.watermark_end_cards,
      ...(preset.watermark_end_cards || {}),
    },
    banned_words_topics: {
      ...EMPTY_PRESET.banned_words_topics,
      ...(preset.banned_words_topics || {}),
    },
    source_rules: { ...EMPTY_PRESET.source_rules, ...(preset.source_rules || {}) },
    source_library: sourceLibrary,
    settings: normalizedSettings,
    posting: { ...EMPTY_PRESET.posting, ...(preset.posting || {}) },
    compliance: { ...(preset.compliance || {}) },
    source_links: Array.isArray(preset.source_links) ? preset.source_links : [],
    provenance: Array.isArray(preset.provenance)
      ? preset.provenance.filter((item) => item && typeof item === "object")
      : [],
    warnings: Array.isArray(preset.warnings)
      ? preset.warnings
          .map((item, index) => (typeof item === "string" ? { id: `warning:${index}`, severity: "warning", message: item } : item))
          .filter(Boolean)
      : [],
    dismissed_warnings: Array.isArray(preset.dismissed_warnings)
      ? preset.dismissed_warnings
      : [],
  };
}

function Field({ label, children, hint }) {
  return (
    <label className="flex flex-col gap-1.5 text-xs text-slate-400">
      <span>{label}</span>
      {children}
      {hint ? <span className="text-[11px] leading-4 text-slate-500">{hint}</span> : null}
    </label>
  );
}

function sourceStatusClass(status) {
  if (status === "approved") return "border-emerald-400/30 bg-emerald-400/[0.06] text-emerald-100";
  if (status === "banned") return "border-rose-400/30 bg-rose-400/[0.06] text-rose-100";
  return "border-amber-400/30 bg-amber-400/[0.06] text-amber-100";
}

function readinessStatusClass(status) {
  if (status === "ready") return "border-emerald-400/20 bg-emerald-400/[0.05] text-emerald-100";
  if (status === "blocked") return "border-rose-400/20 bg-rose-400/[0.05] text-rose-100";
  return "border-amber-400/20 bg-amber-400/[0.05] text-amber-100";
}

function ReadinessPanel({ checks, readyCount }) {
  const blockedCount = checks.filter((check) => check.status === "blocked").length;
  const warningCount = checks.filter((check) => check.status === "warning").length;
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/45 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-400">Campaign readiness</p>
          <p className="mt-1 text-xs leading-5 text-slate-500">A transparent preflight for the same rules Save &amp; apply will enforce.</p>
        </div>
        <div className="text-right">
          <p className="text-sm font-semibold text-white">{readyCount}/{checks.length} ready</p>
          <p className="mt-1 text-[10px] text-slate-500">{blockedCount} blocked · {warningCount} review</p>
        </div>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {checks.map((check) => (
          <div key={check.id} className={`rounded-lg border p-3 ${readinessStatusClass(check.status)}`}>
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-semibold">{check.label}</span>
              <span className="text-[10px] font-semibold uppercase tracking-wide">{check.status === "ready" ? "Ready" : check.status === "blocked" ? "Blocked" : "Review"}</span>
            </div>
            <p className="mt-1 text-[11px] leading-4 opacity-75">{check.detail}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function CampaignPanel({
  campaigns = [],
  publisherStatuses = {},
  jobs = [],
  driveConfigured = false,
  onPresetApplied,
  onScheduled,
}) {
  const [presets, setPresets] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState(EMPTY_PRESET);
  const [step, setStep] = useState("import");
  const [sources, setSources] = useState("");
  const [pasteText, setPasteText] = useState("");
  const [usePaste, setUsePaste] = useState(false);
  const [requirementFiles, setRequirementFiles] = useState([]);
  const [requirementDropActive, setRequirementDropActive] = useState(false);
  const [actionName, setActionName] = useState("");
  const [manualSource, setManualSource] = useState("");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [compliance, setCompliance] = useState(null);
  const [complianceText, setComplianceText] = useState("");
  const [scheduleChecks, setScheduleChecks] = useState({});
  const [schedule, setSchedule] = useState({
    campaignId: "",
    jobId: "",
    clipIds: [],
    day: today(),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    peakHours: ["12:00", "19:00"],
    platforms: [],
  });

  const completedJobs = useMemo(
    () => jobs.filter((job) => job.status === "completed" && Array.isArray(job.clips) && job.clips.length > 1),
    [jobs],
  );
  const selectedJob = completedJobs.find((job) => job.id === schedule.jobId);
  const warnings = useMemo(() => {
    const result = [...(draft.warnings || [])];
    if (!draft.name.trim()) {
      result.push({ id: "required:name", severity: "required", message: "Campaign name is required." });
    }
    if (!(draft.aspect_resolution?.aspect_ratios || []).length) {
      result.push({ id: "required:aspect_ratio", severity: "required", message: "Choose at least one aspect ratio." });
    }
    const platforms = Array.isArray(draft.posting?.platforms) && draft.posting.platforms.length
      ? draft.posting.platforms
      : draft.platforms || [];
    if (!platforms.length) {
      result.push({ id: "required:platforms", severity: "required", message: "Choose at least one allowed platform." });
    }
    return result.filter((item, index, all) => all.findIndex((other) => other.id === item.id) === index);
  }, [draft]);
  const activeWarnings = warnings.filter((item) => !(draft.dismissed_warnings || []).includes(item.id));
  const requiredWarnings = activeWarnings.filter((item) => item.severity === "required");

  useEffect(() => {
    api
      .campaignPresets()
      .then((data) => {
        const items = (data.presets || data.profiles || []).map(normalizePreset);
        setPresets(items);
        setSelectedId(data.selected_id || "");
        const selected = items.find((item) => item.id === data.selected_id);
        if (selected) {
          setDraft(selected);
          setStep("compliance");
          onPresetApplied?.(selected);
        }
      })
      .catch(() => {});
  }, [onPresetApplied]);

  const clearStatus = () => {
    setMessage("");
    setError("");
  };
  const setDraftField = (key, value) => setDraft((current) => ({ ...current, [key]: value }));
  const setNested = (section, key, value) =>
    setDraft((current) => ({ ...current, [section]: { ...(current[section] || {}), [key]: value } }));
  const setStrategyField = (key, value) =>
    setDraft((current) => {
      const enforced = current.edit_policy?.enforced_settings || {};
      const locked = current.settings?.locked_fields || [];
      if (Object.prototype.hasOwnProperty.call(enforced, key) || locked.includes(key)) return current;
      return { ...current, settings: { ...(current.settings || {}), [key]: value } };
    });

  const applyCreatorRecipe = (recipe) => {
    setDraft((current) => {
      const enforced = current.edit_policy?.enforced_settings || {};
      const locked = new Set(current.settings?.locked_fields || []);
      const nextSettings = { ...(current.settings || {}) };
      Object.entries(recipe.values).forEach(([key, value]) => {
        if (!Object.prototype.hasOwnProperty.call(enforced, key) && !locked.has(key)) nextSettings[key] = value;
      });
      const nextCaptions = Object.prototype.hasOwnProperty.call(enforced, "caption_preset") || locked.has("caption_preset")
        ? current.captions_language
        : { ...(current.captions_language || {}), style: recipe.values.caption_preset || current.captions_language?.style || "" };
      return { ...current, settings: nextSettings, captions_language: nextCaptions };
    });
    setMessage(`${recipe.label} recipe loaded. Review the fields before applying the campaign.`);
  };

  const addRequirementFiles = async (incomingFiles) => {
    clearStatus();
    const files = Array.from(incomingFiles || []).filter(Boolean);
    if (!files.length) return;
    if (files.length + requirementFiles.length > MAX_REQUIREMENT_FILES) {
      setError(`Add up to ${MAX_REQUIREMENT_FILES} requirement files at a time.`);
      return;
    }
    setBusy("read-files");
    try {
      const parsed = [];
      for (const file of files) {
        const extension = requirementFileExtension(file.name);
        if (!REQUIREMENT_FILE_EXTENSIONS.has(extension)) {
          throw new Error(`${file.name}: unsupported requirement file type. Use TXT, Markdown, CSV, JSON, or YAML.`);
        }
        if (file.size > MAX_REQUIREMENT_FILE_BYTES) {
          throw new Error(`${file.name}: files must be 2 MB or smaller.`);
        }
        const content = await file.text();
        if (!content.trim()) {
          throw new Error(`${file.name}: the requirement file is empty.`);
        }
        let json = null;
        if (extension === "json") {
          try {
            json = JSON.parse(content);
          } catch {
            throw new Error(`${file.name}: the JSON file is invalid.`);
          }
        }
        parsed.push({
          id: `${file.name}:${file.size}:${file.lastModified}`,
          name: file.name,
          size: file.size,
          extension,
          content,
          json,
        });
      }
      setRequirementFiles((current) => {
        const existing = new Set(current.map((item) => item.id));
        return [...current, ...parsed.filter((item) => !existing.has(item.id))];
      });
      setMessage(`${parsed.length} requirement file${parsed.length === 1 ? "" : "s"} ready to analyze.`);
    } catch (fileError) {
      setError(fileError.message || "Could not read the requirement file.");
    } finally {
      setBusy("");
    }
  };

  const removeRequirementFile = (id) => {
    setRequirementFiles((current) => current.filter((item) => item.id !== id));
  };

  const clearRequirementFiles = () => setRequirementFiles([]);

  const handleRequirementDragEnter = (event) => {
    event.preventDefault();
    setRequirementDropActive(true);
  };

  const handleRequirementDragLeave = (event) => {
    event.preventDefault();
    setRequirementDropActive(false);
  };

  const handleRequirementDrop = (event) => {
    event.preventDefault();
    setRequirementDropActive(false);
    addRequirementFiles(event.dataTransfer?.files);
  };

  const importBrief = async () => {
    clearStatus();
    const payload = usePaste ? { text: pasteText } : { urls: splitLines(sources) };
    const hasTextInput = requirementFiles.length > 0 || (usePaste && pasteText.trim());
    if (!hasTextInput && !payload.urls?.length) return;
    setBusy("import");
    try {
      let result;
      const structuredJson = requirementFiles.length === 1
        ? requirementFiles[0].json?.preset || requirementFiles[0].json
        : null;
      const structuredPreset = isStructuredPresetJson(structuredJson) ? structuredJson : null;
      if (structuredPreset) {
        result = await api.importCampaignPresetJson(structuredPreset, false, false);
      } else if (hasTextInput) {
        const importedText = [
          ...requirementFiles.map((file) => file.content),
          usePaste ? pasteText : "",
        ].filter((value) => String(value || "").trim()).join("\n\n");
        result = await api.importCampaignPreset({
          text: importedText,
        });
      } else {
        result = await api.importCampaignPreset(payload);
      }
      const importedProvenance = requirementFiles.map((file) => ({
        source_type: "local_requirement_file",
        source_name: file.name,
        content_hash: "browser_local",
      }));
      const normalized = normalizePreset(result);
      const next = importedProvenance.length
        ? { ...normalized, provenance: [...(normalized.provenance || []), ...importedProvenance] }
        : normalized;
      setDraft(next);
      setRequirementFiles([]);
      setStep("review");
      setMessage(
        structuredPreset
          ? "Preset JSON loaded for review. Save & apply it only after checking the policy."
          : result.sync_status === "offline_paste"
            ? "Brief parsed locally. Review every field before applying."
            : "Sources imported. Review the extracted campaign before applying it.",
      );
    } catch (importError) {
      setError(importError.message || "Could not import the campaign brief.");
    } finally {
      setBusy("");
    }
  };

  const applyPreset = async () => {
    clearStatus();
    if (requiredWarnings.length) {
      setStep("review");
      setError("Resolve or explicitly dismiss the required campaign warnings before applying.");
      return;
    }
    setBusy("save");
    try {
      const result = await api.saveCampaignPreset(draft, true);
      const next = normalizePreset(result);
      setDraft(next);
      setSelectedId(next.id);
      setPresets((current) => [next, ...current.filter((item) => item.id !== next.id)]);
      onPresetApplied?.(next);
      setStep("compliance");
      setMessage("Campaign preset saved and applied to clipping, editing, and publishing settings.");
    } catch (saveError) {
      setStep("review");
      setError(saveError.message || "Could not save the campaign preset.");
    } finally {
      setBusy("");
    }
  };

  const selectPreset = async (id) => {
    if (!id) return;
    clearStatus();
    setBusy("select");
    try {
      const result = await api.selectCampaignPreset(id);
      const next = normalizePreset(result);
      setDraft(next);
      setSelectedId(next.id);
      onPresetApplied?.(next);
      setStep("compliance");
      setMessage(`Applied ${next.name || "campaign preset"} to the workspace.`);
    } catch (selectError) {
      const localPreset = presets.find((item) => item.id === id);
      if (localPreset) {
        setDraft(localPreset);
        setSelectedId(id);
        setStep("review");
      }
      setError(selectError.message || "Could not switch campaign presets.");
    } finally {
      setBusy("");
    }
  };

  const renamePreset = async () => {
    if (!selectedId || !actionName.trim()) return;
    setBusy("rename");
    clearStatus();
    try {
      const result = await api.renameCampaignPreset(selectedId, actionName.trim());
      const next = normalizePreset(result);
      setDraft(next);
      setPresets((current) => current.map((item) => (item.id === next.id ? next : item)));
      setActionName("");
      setMessage("Campaign preset renamed.");
    } catch (renameError) {
      setError(renameError.message || "Could not rename the campaign preset.");
    } finally {
      setBusy("");
    }
  };

  const duplicatePreset = async () => {
    if (!selectedId) return;
    setBusy("duplicate");
    clearStatus();
    try {
      const result = await api.duplicateCampaignPreset(selectedId, actionName.trim());
      const next = normalizePreset(result);
      setDraft(next);
      setSelectedId(next.id);
      setPresets((current) => [next, ...current]);
      setActionName("");
      setMessage("Campaign preset duplicated. Edit it before applying if needed.");
    } catch (duplicateError) {
      setError(duplicateError.message || "Could not duplicate the campaign preset.");
    } finally {
      setBusy("");
    }
  };

  const deletePreset = async () => {
    if (!selectedId || !window.confirm("Delete this saved campaign preset?")) return;
    setBusy("delete");
    clearStatus();
    try {
      await api.deleteCampaignPreset(selectedId);
      setPresets((current) => current.filter((item) => item.id !== selectedId));
      setSelectedId("");
      setDraft(EMPTY_PRESET);
      onPresetApplied?.(EMPTY_PRESET);
      setStep("import");
      setMessage("Campaign preset deleted.");
    } catch (deleteError) {
      setError(deleteError.message || "Could not delete the campaign preset.");
    } finally {
      setBusy("");
    }
  };

  const resyncPreset = async () => {
    if (!selectedId) return;
    setBusy("sync");
    clearStatus();
    try {
      const result = await api.syncCampaignPreset(selectedId);
      const next = normalizePreset(result);
      setDraft(next);
      setPresets((current) => current.map((item) => (item.id === next.id ? next : item)));
      setStep("review");
      setMessage(
        result.changed
          ? "The source changed. Review the updated fields before applying."
          : "Campaign source is already up to date.",
      );
    } catch (syncError) {
      setError(syncError.message || "Could not re-sync the campaign source.");
    } finally {
      setBusy("");
    }
  };

  const exportPreset = async () => {
    if (!selectedId) return;
    setBusy("export");
    clearStatus();
    try {
      const exported = await api.exportCampaignPreset(selectedId);
      const blob = new Blob([JSON.stringify(exported, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${(exported.name || "campaign-preset").replace(/[^a-z0-9_-]+/gi, "-")}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      setMessage("Campaign preset JSON exported.");
    } catch (exportError) {
      setError(exportError.message || "Could not export the campaign preset.");
    } finally {
      setBusy("");
    }
  };

  const importJson = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setBusy("json");
    clearStatus();
    try {
      const parsed = JSON.parse(await file.text());
      const result = await api.importCampaignPresetJson(parsed.preset || parsed, false, false);
      const next = normalizePreset(result);
      setDraft(next);
      setStep("review");
      setMessage("Campaign preset JSON loaded for review. Save & apply it only after checking the policy.");
    } catch (jsonError) {
      setError(jsonError.message || "The selected file is not valid campaign preset JSON.");
    } finally {
      setBusy("");
    }
  };

  const updateSourceStatus = (index, status) => {
    setDraft((current) => ({
      ...current,
      source_library: (current.source_library || []).map((item, itemIndex) =>
        itemIndex === index ? { ...item, status, approved: status === "approved" } : item,
      ),
    }));
  };

  const addManualSource = () => {
    const url = manualSource.trim();
    if (!url) return;
    setDraft((current) => ({
      ...current,
      source_library: [
        ...(current.source_library || []),
        {
          id: `manual-${Date.now()}`,
          url,
          title: "Manual source",
          type: "url",
          status: "suggested",
          approved: false,
          origin: "manual",
          reason: "Added manually; approve it before clipping.",
        },
      ],
      source_links: [...new Set([...(current.source_links || []), url])],
    }));
    setManualSource("");
  };

  const removeSource = (index) => {
    setDraft((current) => ({
      ...current,
      source_library: (current.source_library || []).filter((_, itemIndex) => itemIndex !== index),
    }));
  };

  const toggleWarning = (warningId) => {
    setDraft((current) => {
      const dismissed = new Set(current.dismissed_warnings || []);
      if (dismissed.has(warningId)) dismissed.delete(warningId);
      else dismissed.add(warningId);
      return { ...current, dismissed_warnings: [...dismissed] };
    });
  };

  const runCompliance = async () => {
    if (!draft.name.trim()) {
      setError("Add a campaign name before running compliance.");
      return;
    }
    setBusy("compliance");
    clearStatus();
    try {
      const platforms = presetPlatforms.length ? presetPlatforms : [""];
      const reports = await Promise.all(platforms.map((platform) => api.campaignPresetCompliance({
        profile: draft,
        visibility: "public",
        clip: {
          caption: complianceText,
          description: complianceText,
          hashtags: draft.copy_rules?.hashtags || [],
          platform,
          aspect_ratio: (draft.aspect_resolution?.aspect_ratios || [])[0] || "",
          captions: draft.captions_language?.required !== false,
          source_url: (draft.source_library || []).find((item) => item.status === "approved" && item.url)?.url || "",
        },
      })));
      const result = reports.length === 1
        ? reports[0]
        : {
            status: reports.some((item) => item.status === "fail")
              ? "fail"
              : reports.some((item) => item.status === "unknown") ? "unknown" : "pass",
            platform_reports: reports.map((item, index) => ({ platform: platforms[index] || "default", ...item })),
            profiles: reports.flatMap((item, index) => (item.profiles || []).map((profile) => ({
              ...profile,
              platform: platforms[index] || "default",
            }))),
          };
      setCompliance(result);
      setMessage(result.status === "pass" ? "Compliance passed for every configured platform." : "Review the compliance report before publishing.");
    } catch (complianceError) {
      setError(complianceError.message || "Could not run campaign compliance.");
    } finally {
      setBusy("");
    }
  };

  const toggleClip = (clipId) => {
    setSchedule((current) => ({
      ...current,
      clipIds: current.clipIds.includes(clipId)
        ? current.clipIds.filter((id) => id !== clipId)
        : current.clipIds.length < 2
          ? [...current.clipIds, clipId]
          : current.clipIds,
    }));
  };

  const togglePlatform = (platform) => {
    if (!platformAllowed(platform)) return;
    setSchedule((current) => ({
      ...current,
      platforms: current.platforms.includes(platform)
        ? current.platforms.filter((item) => item !== platform)
        : [...current.platforms, platform],
    }));
  };

  const scheduleDay = async () => {
    if (!schedule.campaignId || !schedule.jobId || schedule.clipIds.length !== 2 || !schedule.platforms.length) {
      setError("Choose a campaign, a rendered job, exactly two clips, and at least one connected platform.");
      return;
    }
    if (schedule.platforms.some((platform) => !platformAllowed(platform))) {
      setError("The active campaign preset does not allow one or more scheduled platforms.");
      return;
    }
    if (manualOnly) {
      setError("This campaign requires human review; use each clip's review card instead of the automation scheduler.");
      return;
    }
    const unapproved = (selectedJob?.clips || [])
      .filter((clip) => schedule.clipIds.includes(clip.id) && clip.review_state !== "approved");
    if (unapproved.length) {
      setError("Approve both selected clips before scheduling them.");
      return;
    }
    if (requiredChecks.some((check) => scheduleChecks[check.id] !== true)) {
      setError("Complete every active campaign checklist item before scheduling.");
      return;
    }
    if (schedule.peakHours[0] === schedule.peakHours[1]) {
      setError("Choose two distinct peak-time slots.");
      return;
    }
    if (!compliance || compliance.status !== "pass") {
      setError("Run a passing compliance check before scheduling the campaign day.");
      return;
    }
    setBusy("schedule");
    clearStatus();
    try {
      const result = await api.scheduleCampaignDay(schedule.campaignId, {
        job_id: schedule.jobId,
        clip_ids: schedule.clipIds,
        platforms: schedule.platforms,
        campaign_checks: scheduleChecks,
        day: schedule.day,
        timezone: schedule.timezone,
        peak_hours: schedule.peakHours,
        visibility: "public",
      });
      onScheduled?.(result);
      setMessage(`Queued two public clips for ${result.day}.`);
    } catch (scheduleError) {
      setError(scheduleError.message || "Could not schedule the campaign day.");
    } finally {
      setBusy("");
    }
  };

  const renderSource = (item, index) => (
    <li key={item.id || item.url || index} className={`rounded-xl border p-3 ${sourceStatusClass(item.status)}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold">{item.title || item.type || "Source"}</span>
            <span className="rounded-full border border-current/20 px-2 py-0.5 text-[10px] uppercase tracking-wide">{item.status || "suggested"}</span>
          </div>
          {item.url ? (
            <a href={item.url} target="_blank" rel="noreferrer" className="mt-1 block truncate text-[11px] underline decoration-current/30 hover:decoration-current">
              {item.url}
            </a>
          ) : (
            <p className="mt-1 text-[11px] opacity-75">Discovery reference only; approve an individual video URL.</p>
          )}
          <p className="mt-1 text-[11px] opacity-75">{item.reason || "Review this source before clipping."}</p>
        </div>
        <div className="flex shrink-0 gap-1">
          {item.url ? <button type="button" onClick={() => navigator.clipboard?.writeText(item.url)} className="rounded border border-current/20 px-2 py-1 text-[10px]">Copy</button> : null}
          {item.url ? <button type="button" onClick={() => updateSourceStatus(index, "approved")} className="rounded border border-emerald-300/30 px-2 py-1 text-[10px]">Accept</button> : null}
          {item.url ? <button type="button" onClick={() => updateSourceStatus(index, "banned")} className="rounded border border-rose-300/30 px-2 py-1 text-[10px]">Reject</button> : null}
          <button type="button" onClick={() => removeSource(index)} className="rounded border border-current/20 px-2 py-1 text-[10px]">Remove</button>
        </div>
      </div>
    </li>
  );

  const presetPlatforms = Array.isArray(draft.posting?.platforms) && draft.posting.platforms.length
    ? draft.posting.platforms
    : Array.isArray(draft.platforms)
      ? draft.platforms
      : [];
  const platformAllowed = (platform) =>
    !presetPlatforms.length
    || presetPlatforms.includes(platform)
    || (platform === "youtube" && presetPlatforms.includes("youtube_shorts"))
    || (platform === "youtube_shorts" && presetPlatforms.includes("youtube"));
  const presetRatios = draft.aspect_resolution?.aspect_ratios || [];
  const requiredChecks = (Array.isArray(draft.compliance?.required_checks) ? draft.compliance.required_checks : [])
    .map((item, index) => {
      if (typeof item === "string") return { id: item, label: item };
      const id = item?.id || item?.key || item?.label || `campaign-check-${index}`;
      return { ...item, id, label: item?.label || item?.message || id };
    });
  if (draft.source_library?.length && !requiredChecks.some((check) => check.id === "source_approved")) {
    requiredChecks.push({ id: "source_approved", label: "I reviewed and approved the campaign source" });
  }
  const requiredCheckSignature = [
    ...requiredChecks.map((check) => check.id),
    ...(draft.source_library || []).map((item) => `${item.id || item.url || "source"}:${item.status || "suggested"}`),
  ].join("|");
  useEffect(() => {
    setScheduleChecks({});
    setCompliance(null);
  }, [draft.id, requiredCheckSignature]);
  const manualOnly = !!(draft.posting?.manual_only || draft.posting?.review_only);
  const strategySettings = draft.settings || {};
  const enforcedStrategySettings = draft.edit_policy?.enforced_settings || {};
  const lockedStrategyFields = new Set(draft.settings?.locked_fields || []);
  const recipeValue = (key, fallback) =>
    Object.prototype.hasOwnProperty.call(enforcedStrategySettings, key)
      ? enforcedStrategySettings[key]
      : Object.prototype.hasOwnProperty.call(strategySettings, key)
        ? strategySettings[key]
        : fallback;
  const recipeLocked = (key) => lockedStrategyFields.has(key) || Object.prototype.hasOwnProperty.call(enforcedStrategySettings, key);
  const settingFlag = (key, fallback = false) =>
    !!recipeValue(key, fallback);
  const hasImportInput = Boolean(requirementFiles.length || (usePaste ? pasteText.trim() : sources.trim()));
  const blueprintSignals = [
    ["Selection", recipeValue("selection_prompt", "") ? "Directed by your brief" : "AI selects from the source"],
    ["Retention", selectLabel(STRATEGY_OPTIONS.retention_mode, recipeValue("retention_mode", "off"), "Standard ranking")],
    ["Framing", recipeValue("speaker_reframe", false) ? "Speaker-aware" : "Campaign default"],
    ["Captions", recipeValue("caption_preset", draft.captions_language?.style || "Karaoke")],
  ];
  const sourceItems = draft.source_library || [];
  const approvedSourceCount = sourceItems.filter((item) => item.status === "approved").length;
  const suggestedSourceCount = sourceItems.filter((item) => item.status === "suggested").length;
  const readinessChecks = [
    {
      id: "identity",
      label: "Campaign identity",
      status: draft.name.trim() ? "ready" : "blocked",
      detail: draft.name.trim() ? draft.name.trim() : "Add a campaign name before applying.",
    },
    {
      id: "format",
      label: "Output format",
      status: presetRatios.length ? "ready" : "blocked",
      detail: presetRatios.length ? `${presetRatios.join(", ")} format${presetRatios.length === 1 ? "" : "s"} configured.` : "Choose at least one aspect ratio.",
    },
    {
      id: "distribution",
      label: "Distribution",
      status: presetPlatforms.length ? "ready" : "blocked",
      detail: presetPlatforms.length ? `${presetPlatforms.length} allowed platform${presetPlatforms.length === 1 ? "" : "s"}.` : "Choose at least one allowed platform.",
    },
    {
      id: "strategy",
      label: "Clip strategy",
      status: recipeValue("selection_prompt", "") || recipeValue("content_type", "") || recipeValue("clip_goal", "") ? "ready" : "warning",
      detail: recipeValue("selection_prompt", "") ? "Selection direction is explicit." : recipeValue("content_type", "") || recipeValue("clip_goal", "") ? "Structured intent is configured; add a prompt for more control." : "Optional: choose a recipe or leave the engine on its default strategy.",
    },
    {
      id: "sources",
      label: "Source approvals",
      status: suggestedSourceCount ? "warning" : "ready",
      detail: suggestedSourceCount ? `${suggestedSourceCount} suggested source${suggestedSourceCount === 1 ? " remains" : "s remain"} unapproved; ${approvedSourceCount} approved.` : sourceItems.length ? `${approvedSourceCount} source${approvedSourceCount === 1 ? " is" : "s are"} approved.` : "No source allow-list is configured yet.",
    },
    {
      id: "policy",
      label: "Policy checks",
      status: requiredWarnings.length ? "blocked" : "ready",
      detail: requiredWarnings.length ? `${requiredWarnings.length} required item${requiredWarnings.length === 1 ? "" : "s"} must be resolved or dismissed.` : "Required fields are resolved or explicitly dismissed.",
    },
  ];
  const readinessReadyCount = readinessChecks.filter((check) => check.status === "ready").length;
  const complianceResults = compliance?.platform_reports
    ? compliance.platform_reports.flatMap((report) => (report.profiles || []).flatMap((profile) => (profile.results || []).map((item) => ({
        ...item,
        platform: report.platform,
      }))))
    : (compliance?.profiles?.[0]?.results || compliance?.results || []);

  return (
    <section className="rounded-2xl border border-violet-400/20 bg-violet-400/[0.035] p-5 shadow-xl shadow-violet-950/10">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-3xl">
          <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-violet-200">Campaign workspace</p>
          <h2 className="mt-1 text-xl font-semibold text-white">One campaign preset for the whole workflow</h2>
          <p className="mt-1 text-sm leading-6 text-slate-400">Import a brief, review every rule and source, then apply the saved preset to clipping, editing, compliance, and posting.</p>
        </div>
        <span className="rounded-full border border-emerald-400/20 bg-emerald-400/[0.08] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-emerald-200">Public-only policy</span>
      </div>

      <div className="mt-5 grid gap-2 sm:grid-cols-4" role="tablist" aria-label="Campaign steps">
        {[
          ["import", "1", "Import"],
          ["review", "2", "Review"],
          ["apply", "3", "Save & apply"],
          ["compliance", "4", "Compliance & posting"],
        ].map(([id, number, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={step === id}
            onClick={() => setStep(id)}
            className={`rounded-xl border px-3 py-2 text-left transition ${step === id ? "border-violet-300/50 bg-violet-400/10" : "border-slate-800 bg-slate-950/40 hover:border-slate-600"}`}
          >
            <span className="text-[10px] font-semibold uppercase tracking-wide text-violet-200">Step {number}</span>
            <span className="mt-1 block text-sm font-semibold text-white">{label}</span>
          </button>
        ))}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2 rounded-xl border border-slate-800 bg-slate-950/40 p-3">
        <label className="flex min-w-[220px] flex-1 flex-col gap-1 text-xs text-slate-400">
          <span>Saved campaign presets</span>
          <select value={selectedId} onChange={(event) => selectPreset(event.target.value)} disabled={busy === "select"} className={inputClass}>
            <option value="">Select a preset…</option>
            {presets.map((preset) => <option key={preset.id} value={preset.id}>{preset.name || preset.id}{preset.changed ? " · changed" : ""}</option>)}
          </select>
        </label>
        <input value={actionName} onChange={(event) => setActionName(event.target.value)} placeholder="Rename or duplicate as…" className={`${inputClass} mt-5 min-w-[180px] flex-1`} />
        <button type="button" onClick={renamePreset} disabled={!selectedId || !actionName.trim() || !!busy} className="mt-5 rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 disabled:opacity-40">Rename</button>
        <button type="button" onClick={duplicatePreset} disabled={!selectedId || !!busy} className="mt-5 rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 disabled:opacity-40">Duplicate</button>
        <button type="button" onClick={deletePreset} disabled={!selectedId || !!busy} className="mt-5 rounded-lg border border-rose-400/30 px-3 py-2 text-xs text-rose-200 disabled:opacity-40">Delete</button>
        <button type="button" onClick={resyncPreset} disabled={!selectedId || !!busy} className="mt-5 rounded-lg border border-indigo-300/30 px-3 py-2 text-xs text-indigo-100 disabled:opacity-40">Re-sync</button>
        <button type="button" onClick={exportPreset} disabled={!selectedId || !!busy} className="mt-5 rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 disabled:opacity-40">Export JSON</button>
        <label className="mt-5 cursor-pointer rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:border-violet-300/40">
          Import JSON
          <input type="file" accept="application/json,.json" className="sr-only" onChange={importJson} />
        </label>
      </div>

      {step === "import" && (
        <div className="mt-4 grid gap-4 lg:grid-cols-[1.05fr_0.95fr]">
          <div className="space-y-3">
            <div className="rounded-xl border border-indigo-300/20 bg-indigo-400/[0.035] p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-white">Import and analyze a brief</h3>
                  <p className="mt-1 text-xs leading-5 text-slate-400">Paste multiple public Docs/Drive links, folders, or readable files. Private Drive reads happen on the server with read-only access.</p>
                </div>
                <button type="button" onClick={() => setUsePaste((current) => !current)} className="text-xs font-semibold text-indigo-200 hover:underline">{usePaste ? "Use source links" : "Use pasted brief fallback"}</button>
              </div>
              {usePaste ? (
                <textarea value={pasteText} onChange={(event) => setPasteText(event.target.value)} rows="10" className={`${inputClass} mt-3 w-full resize-y`} placeholder={'Campaign: Creator Growth\nAspect ratios: 9:16\nPlatforms: YouTube Shorts, TikTok\nHashtags: #brand #campaign'} />
              ) : (
                <textarea value={sources} onChange={(event) => setSources(event.target.value)} rows="7" className={`${inputClass} mt-3 w-full resize-y`} placeholder="One Google Doc, Drive file, or folder URL per line" />
              )}
              <div
                role="group"
                aria-label="Requirement file dropzone"
                onDragEnter={handleRequirementDragEnter}
                onDragOver={(event) => event.preventDefault()}
                onDragLeave={handleRequirementDragLeave}
                onDrop={handleRequirementDrop}
                className={`mt-3 rounded-xl border border-dashed p-4 transition ${requirementDropActive ? "border-cyan-300 bg-cyan-400/10" : "border-slate-700 bg-slate-950/40 hover:border-slate-500"}`}
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex items-start gap-3">
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-cyan-400/10 text-lg text-cyan-200" aria-hidden="true">↥</span>
                    <div>
                      <p className="text-sm font-semibold text-slate-200">{requirementDropActive ? "Drop requirements to add them" : "Drop the requirement file here"}</p>
                      <p className="mt-1 text-[11px] leading-5 text-slate-500">Read locally in your browser — TXT, Markdown, CSV, JSON, or YAML, up to 2 MB each. For PDF/DOCX, use the authorized Drive connector or export text first.</p>
                    </div>
                  </div>
                  <label className="cursor-pointer rounded-lg border border-slate-700 px-3 py-2 text-xs font-semibold text-slate-200 hover:border-cyan-300/50">
                    Browse files
                    <input
                      type="file"
                      multiple
                      accept=".txt,.md,.markdown,.csv,.json,.yaml,.yml,text/plain,text/markdown,text/csv,application/json"
                      className="sr-only"
                      onChange={(event) => {
                        addRequirementFiles(event.target.files);
                        event.target.value = "";
                      }}
                    />
                  </label>
                </div>
                {requirementFiles.length ? (
                  <div className="mt-3 overflow-hidden rounded-lg border border-cyan-300/20 bg-cyan-400/[0.035]">
                    <div className="flex items-center justify-between border-b border-cyan-300/10 px-3 py-2 text-[11px]">
                      <span className="font-semibold text-cyan-100">{requirementFiles.length} file{requirementFiles.length === 1 ? "" : "s"} ready · {formatBytes(requirementFiles.reduce((total, file) => total + file.size, 0))}</span>
                      <button type="button" onClick={clearRequirementFiles} className="rounded px-1.5 py-1 text-slate-500 hover:bg-rose-400/10 hover:text-rose-200">Clear all</button>
                    </div>
                    <ul className="divide-y divide-cyan-300/10">
                      {requirementFiles.map((file) => (
                        <li key={file.id} className="flex items-center gap-2 px-3 py-2">
                          <span className="min-w-0 flex-1 truncate text-xs text-slate-200">{file.name}</span>
                          <span className="text-[10px] uppercase tracking-wide text-slate-500">{file.extension} · {formatBytes(file.size)}</span>
                          <button type="button" onClick={() => removeRequirementFile(file.id)} className="rounded px-1.5 py-1 text-[10px] text-slate-500 hover:bg-rose-400/10 hover:text-rose-200">Remove</button>
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button type="button" onClick={importBrief} disabled={busy === "import" || busy === "read-files" || !hasImportInput} className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-500 disabled:opacity-40">{busy === "read-files" ? "Reading files…" : busy === "import" ? "Analyzing…" : requirementFiles.length ? "Analyze requirements" : "Import and analyze"}</button>
                <span className="text-[11px] text-slate-500">{requirementFiles.length ? "Raw files stay in the browser; extracted text is sent only to the review parser and is not saved until you apply the campaign." : driveConfigured ? "Server-side Drive OAuth is configured." : "Drive OAuth is not configured; public links and paste remain available."}</span>
              </div>
            </div>
            {!presets.length && !draft.id ? (
              <div className="rounded-xl border border-dashed border-violet-300/30 bg-violet-400/[0.04] p-5 text-sm text-violet-100">Import a brief to create your first campaign</div>
            ) : null}
          </div>
           <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-4 text-xs text-slate-400">
             <p className="font-semibold text-slate-200">A creator-studio workflow, without the black box</p>
             <ul className="mt-3 space-y-2 leading-5">
               <li>• Bring in a brief, transcript notes, or multiple requirement files in one pass.</li>
               <li>• Shape the clip strategy: hook, retention, story, speaker framing, captions, and b-roll.</li>
               <li>• Explicit rules stay editable; ambiguous values become warnings.</li>
               <li>• Channels and discovered videos are suggested, not approved.</li>
               <li>• No preset is applied or published automatically during import.</li>
             </ul>
           </div>
        </div>
      )}

      {step === "review" && (
        <div className="mt-4 space-y-4">
          <div className="rounded-xl border border-cyan-300/20 bg-cyan-400/[0.035] p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-cyan-200">Creator intelligence blueprint</p>
                <h3 className="mt-1 text-sm font-semibold text-white">Tell the clip engine what “great” means</h3>
                <p className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">Inspired by transcript-first editors, auto-curation, active-speaker framing, and multi-platform output. These choices stay editable and are saved with the campaign; no engagement score is invented.</p>
              </div>
              <span className="rounded-full border border-cyan-300/20 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-cyan-100">Deterministic + reviewable</span>
            </div>
            <div className="mt-4 rounded-xl border border-slate-800 bg-slate-950/35 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Quick-start recipes</p><span className="text-[10px] text-slate-600">Loads editable defaults — never auto-applies</span></div>
              <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                {CREATOR_RECIPES.map((recipe) => (
                  <button key={recipe.id} type="button" onClick={() => applyCreatorRecipe(recipe)} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-left transition hover:border-cyan-300/40 hover:bg-cyan-400/[0.05]">
                    <span className="block text-xs font-semibold text-slate-200">{recipe.label}</span>
                    <span className="mt-1 block text-[10px] leading-4 text-slate-500">{recipe.description}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Content type">
                <select value={recipeValue("content_type", "")} onChange={(event) => setStrategyField("content_type", event.target.value)} disabled={recipeLocked("content_type")} className={`${inputClass} disabled:opacity-40`}>
                  {STRATEGY_OPTIONS.content_type.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </Field>
              <Field label="Clip goal">
                <select value={recipeValue("clip_goal", "")} onChange={(event) => setStrategyField("clip_goal", event.target.value)} disabled={recipeLocked("clip_goal")} className={`${inputClass} disabled:opacity-40`}>
                  {STRATEGY_OPTIONS.clip_goal.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </Field>
              <Field label="Hook approach">
                <select value={recipeValue("hook_style", "")} onChange={(event) => setStrategyField("hook_style", event.target.value)} disabled={recipeLocked("hook_style")} className={`${inputClass} disabled:opacity-40`}>
                  {STRATEGY_OPTIONS.hook_style.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </Field>
              <Field label="Target clips" hint="A target, not a promise. The source may yield fewer usable moments.">
                <input type="number" min="1" max="50" value={recipeValue("num_clips", "")} onChange={(event) => setStrategyField("num_clips", event.target.value)} disabled={recipeLocked("num_clips")} placeholder="Auto" className={`${inputClass} disabled:opacity-40`} />
              </Field>
            </div>
            <div className="mt-3 grid gap-3 lg:grid-cols-[0.8fr_1.2fr]">
              <Field label="Audience / viewer promise">
                <input value={recipeValue("target_audience", "")} onChange={(event) => setStrategyField("target_audience", event.target.value)} disabled={recipeLocked("target_audience")} placeholder="Who should stop scrolling?" className={`${inputClass} disabled:opacity-40`} />
              </Field>
              <Field label="Selection direction" hint="This is sent to the clip-selection engine as a natural-language cue.">
                <textarea rows="2" value={recipeValue("selection_prompt", "")} onChange={(event) => setStrategyField("selection_prompt", event.target.value)} disabled={recipeLocked("selection_prompt")} placeholder="Find moments with a clear payoff, a surprising insight, or a useful before/after transformation." className={`${inputClass} resize-y disabled:opacity-40`} />
              </Field>
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Retention ranking">
                <select value={recipeValue("retention_mode", "off")} onChange={(event) => setStrategyField("retention_mode", event.target.value)} disabled={recipeLocked("retention_mode")} className={`${inputClass} disabled:opacity-40`}>
                  {STRATEGY_OPTIONS.retention_mode.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </Field>
              <label className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-xs text-slate-300"><input type="checkbox" checked={settingFlag("storytelling_enabled")} disabled={recipeLocked("storytelling_enabled")} onChange={(event) => setStrategyField("storytelling_enabled", event.target.checked)} className="accent-cyan-400" /> Story-first assembly</label>
              <label className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-xs text-slate-300"><input type="checkbox" checked={settingFlag("speaker_reframe")} disabled={recipeLocked("speaker_reframe")} onChange={(event) => setStrategyField("speaker_reframe", event.target.checked)} className="accent-cyan-400" /> Active-speaker framing</label>
              <label className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-xs text-slate-300"><input type="checkbox" checked={settingFlag("broll")} disabled={recipeLocked("broll")} onChange={(event) => setStrategyField("broll", event.target.checked)} className="accent-cyan-400" /> B-roll cues</label>
            </div>
            <div className="mt-4 border-t border-cyan-300/10 pt-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div><p className="text-[10px] font-semibold uppercase tracking-wide text-cyan-200">Production recipe</p><p className="mt-1 text-[11px] text-slate-500">These controls map directly to the existing clip engine and stay locked to the saved campaign.</p></div>
                <span className="text-[10px] text-slate-600">No hidden provider calls</span>
              </div>
              <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <Field label="Clip engine">
                  <select value={recipeValue("strategy", "ai")} onChange={(event) => setStrategyField("strategy", event.target.value)} disabled={recipeLocked("strategy")} className={`${inputClass} disabled:opacity-40`}>{PRODUCTION_OPTIONS.strategy.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
                </Field>
                <Field label="Vibe / tone">
                  <select value={recipeValue("vibe", "")} onChange={(event) => setStrategyField("vibe", event.target.value)} disabled={recipeLocked("vibe")} className={`${inputClass} disabled:opacity-40`}>{PRODUCTION_OPTIONS.vibe.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
                </Field>
                <Field label="Caption preset">
                  <select value={recipeValue("caption_preset", "karaoke")} onChange={(event) => { if (recipeLocked("caption_preset")) return; setStrategyField("caption_preset", event.target.value); setNested("captions_language", "style", event.target.value); }} disabled={recipeLocked("caption_preset")} className={`${inputClass} disabled:opacity-40`}>{PRODUCTION_OPTIONS.caption_preset.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
                </Field>
                <Field label="Caption placement">
                  <select value={recipeValue("caption_position", "bottom")} onChange={(event) => setStrategyField("caption_position", event.target.value)} disabled={recipeLocked("caption_position")} className={`${inputClass} disabled:opacity-40`}>{PRODUCTION_OPTIONS.caption_position.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
                </Field>
              </div>
              <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <Field label="B-roll intensity">
                  <select value={recipeValue("broll_intensity", "standard")} onChange={(event) => setStrategyField("broll_intensity", event.target.value)} disabled={recipeLocked("broll_intensity")} className={`${inputClass} disabled:opacity-40`}>{PRODUCTION_OPTIONS.broll_intensity.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
                </Field>
                <Field label="Asset sourcing" hint="Permissibility mode can force this back to local-only.">
                  <select value={recipeValue("asset_sourcing_mode", "off")} onChange={(event) => setStrategyField("asset_sourcing_mode", event.target.value)} disabled={recipeLocked("asset_sourcing_mode")} className={`${inputClass} disabled:opacity-40`}>{PRODUCTION_OPTIONS.asset_sourcing_mode.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
                </Field>
                <Field label="Speaker layout">
                  <select value={recipeValue("reframe_layout", "follow_active")} onChange={(event) => setStrategyField("reframe_layout", event.target.value)} disabled={!recipeValue("speaker_reframe", false) || recipeLocked("reframe_layout")} className={`${inputClass} disabled:opacity-40`}>{PRODUCTION_OPTIONS.reframe_layout.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
                </Field>
                <Field label="Speaker motion">
                  <select value={recipeValue("reframe_intensity", "standard")} onChange={(event) => setStrategyField("reframe_intensity", event.target.value)} disabled={!recipeValue("speaker_reframe", false) || recipeLocked("reframe_intensity")} className={`${inputClass} disabled:opacity-40`}>{PRODUCTION_OPTIONS.reframe_intensity.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
                </Field>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                {[
                  ["hook_title", "Hook title overlay", true],
                  ["subtitle_sidecar", "Subtitle sidecar", false],
                  ["filler_removal", "Remove filler / long pauses", false],
                  ["visual_selection", "Visual scene selection", true],
                  ["caption_keyword_highlight", "Highlight keywords", true],
                  ["caption_emoji", "Caption emoji", true],
                  ["permissibility_mode", "Local-only assets", false],
                ].map(([key, label, fallback]) => (
                  <label key={key} className="flex items-center gap-2 rounded-full border border-slate-800 bg-slate-950/40 px-3 py-2 text-[11px] text-slate-300"><input type="checkbox" checked={settingFlag(key, fallback)} disabled={recipeLocked(key)} onChange={(event) => setStrategyField(key, event.target.checked)} className="accent-cyan-400" /> {label}</label>
                ))}
              </div>
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Campaign name"><input value={draft.name} onChange={(event) => setDraftField("name", event.target.value)} className={inputClass} /></Field>
            <Field label="Aspect ratios" hint="Example: 9:16, 1:1"><input value={presetRatios.join(", ")} onChange={(event) => setDraftField("aspect_resolution", { ...draft.aspect_resolution, aspect_ratios: splitLines(event.target.value) })} className={inputClass} /></Field>
            <Field label="Allowed platforms" hint="Example: youtube_shorts, tiktok"><input value={presetPlatforms.join(", ")} onChange={(event) => { const platforms = splitLines(event.target.value); setDraft((current) => ({ ...current, platforms, posting: { ...(current.posting || {}), platforms } })); }} className={inputClass} /></Field>
            <Field label="Clip length"><input value={draft.length?.raw || draft.settings?.clip_length || ""} onChange={(event) => { const { length, clipLength } = lengthSettings(event.target.value); setDraft((current) => ({ ...current, length, settings: { ...(current.settings || {}), clip_length: clipLength } })); }} placeholder="30-60 seconds" className={inputClass} /></Field>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Caption style"><input value={recipeValue("caption_preset", draft.captions_language?.style || "")} onChange={(event) => { if (recipeLocked("caption_preset")) return; setNested("captions_language", "style", event.target.value); setStrategyField("caption_preset", event.target.value); }} disabled={recipeLocked("caption_preset")} className={`${inputClass} disabled:opacity-40`} /></Field>
            <Field label="Hashtags"><input value={(draft.copy_rules?.hashtags || []).join(" ")} onChange={(event) => setNested("copy_rules", "hashtags", splitLines(event.target.value).map((tag) => tag.startsWith("#") ? tag : `#${tag}`))} className={inputClass} /></Field>
            <Field label="Required spelling / copy"><input value={(draft.copy_rules?.required_copy || []).join(", ")} onChange={(event) => setNested("copy_rules", "required_copy", splitLines(event.target.value))} className={inputClass} /></Field>
            <Field label="CTA"><input value={draft.copy_rules?.cta || ""} onChange={(event) => setNested("copy_rules", "cta", event.target.value)} className={inputClass} /></Field>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Forbidden words"><textarea rows="3" value={(draft.banned_words_topics?.words || []).join(", ")} onChange={(event) => setNested("banned_words_topics", "words", splitLines(event.target.value))} className={`${inputClass} resize-y`} /></Field>
            <Field label="Forbidden topics"><textarea rows="3" value={(draft.banned_words_topics?.topics || []).join(", ")} onChange={(event) => setNested("banned_words_topics", "topics", splitLines(event.target.value))} className={`${inputClass} resize-y`} /></Field>
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Logo / watermark"><input value={draft.watermark_end_cards?.watermark || ""} onChange={(event) => setNested("watermark_end_cards", "watermark", event.target.value)} className={inputClass} /></Field>
            <Field label="Brand font"><input value={draft.settings?.brand_font || ""} onChange={(event) => setDraftField("settings", { ...draft.settings, brand_font: event.target.value })} className={inputClass} /></Field>
            <Field label="Brand colors"><input value={JSON.stringify(draft.settings?.brand_colors || {})} onChange={(event) => setDraftField("settings", { ...draft.settings, brand_colors: event.target.value })} placeholder='{"primary":"#7c3aed"}' className={inputClass} /></Field>
          </div>

          <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
             <div className="flex flex-wrap items-start justify-between gap-3">
               <div><h3 className="text-sm font-semibold text-white">Source library</h3><p className="mt-1 text-xs text-slate-500">Only approved sources can be submitted while this preset is active.</p>{draft.last_synced_at ? <p className="mt-1 text-[11px] text-slate-600">Last synced {new Date(draft.last_synced_at * 1000).toLocaleString()}{draft.changed ? " · changed since sync" : ""}</p> : null}</div>
               <div className="flex gap-2"><input value={manualSource} onChange={(event) => setManualSource(event.target.value)} placeholder="Add source URL" className={`${inputClass} min-w-[220px]`} /><button type="button" onClick={addManualSource} className="rounded-lg border border-violet-300/30 px-3 py-2 text-xs text-violet-100">Add suggested</button></div>
             </div>
             {draft.provenance?.some((item) => item.source_type === "local_requirement_file") ? <div className="mt-3 rounded-lg border border-cyan-300/15 bg-cyan-400/[0.035] p-3"><p className="text-[11px] font-semibold uppercase tracking-wide text-cyan-200">Requirement intake provenance</p><p className="mt-1 text-[11px] text-slate-500">These local files shaped the draft; they are not automatically treated as footage or approved sources.</p><div className="mt-2 flex flex-wrap gap-1.5">{draft.provenance.filter((item) => item.source_type === "local_requirement_file").map((item) => <span key={`${item.source_name}-${item.content_hash}`} className="rounded-full border border-cyan-300/15 px-2 py-1 text-[10px] text-cyan-100">{item.source_name}</span>)}</div></div> : null}
             {draft.source_links?.length ? <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/50 p-3"><p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Original brief sources</p><div className="mt-2 grid gap-1">{draft.source_links.map((url) => <a key={url} href={url} target="_blank" rel="noreferrer" className="truncate text-xs text-indigo-200 hover:underline">{url}</a>)}</div></div> : null}
           {draft.source_library?.length ? <ul className="mt-3 grid gap-2">{draft.source_library.map(renderSource)}</ul> : <p className="mt-3 text-xs text-slate-500">No explicit sources were found. Add and approve sources manually, or re-import a brief containing links.</p>}
          </div>

          <ReadinessPanel checks={readinessChecks} readyCount={readinessReadyCount} />

          <div className="rounded-xl border border-amber-400/20 bg-amber-400/[0.04] p-4">
            <div className="flex items-center justify-between gap-2"><div><h3 className="text-sm font-semibold text-amber-100">Warnings and required review</h3><p className="mt-1 text-xs text-amber-200/70">Uncertainty is kept visible instead of being guessed.</p></div><span className="text-xs text-amber-200">{activeWarnings.length} open</span></div>
            {warnings.length ? <ul className="mt-3 space-y-2">{warnings.map((warning) => <li key={warning.id} className="flex items-start gap-2 text-xs text-amber-100"><input type="checkbox" checked={(draft.dismissed_warnings || []).includes(warning.id)} onChange={() => toggleWarning(warning.id)} className="mt-0.5 accent-amber-300" /><span>{warning.message}{warning.severity === "required" ? " (required or dismiss explicitly)" : ""}</span></li>)}</ul> : <p className="mt-3 text-xs text-emerald-200">No unresolved warnings.</p>}
          </div>
          <div className="flex flex-wrap justify-between gap-2"><button type="button" onClick={() => setStep("import")} className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300">Back to import</button><button type="button" onClick={() => setStep("apply")} className="rounded-lg bg-violet-600 px-4 py-2 text-xs font-semibold text-white disabled:opacity-40" disabled={requiredWarnings.length > 0}>Review apply step</button></div>
        </div>
      )}

      {step === "apply" && (
        <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_0.8fr]">
          <div className="rounded-xl border border-violet-300/20 bg-violet-400/[0.04] p-4">
            <h3 className="text-sm font-semibold text-white">Save and apply the full preset</h3>
            <p className="mt-1 text-xs leading-5 text-slate-400">This updates the clipping settings, source allow-list, captions/brand rules, tool policy, compliance checks, and posting defaults together.</p>
             <div className="mt-4 grid gap-3 sm:grid-cols-3">
               <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3"><div className="text-[10px] uppercase tracking-wide text-slate-500">Preset</div><div className="mt-1 text-sm font-semibold text-white">{draft.name || "Unnamed campaign"}</div></div>
               <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3"><div className="text-[10px] uppercase tracking-wide text-slate-500">Sources</div><div className="mt-1 text-sm font-semibold text-white">{(draft.source_library || []).filter((item) => item.status === "approved").length} approved</div></div>
               <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3"><div className="text-[10px] uppercase tracking-wide text-slate-500">Posting</div><div className="mt-1 text-sm font-semibold text-white">{manualOnly ? "Manual review" : "Review before publish"}</div></div>
             </div>
             <div className="mt-4 rounded-xl border border-cyan-300/15 bg-cyan-400/[0.035] p-3">
               <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-[10px] font-semibold uppercase tracking-wide text-cyan-200">Production blueprint</p><span className="text-[10px] text-slate-500">No automatic publishing</span></div>
               <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{blueprintSignals.map(([label, value]) => <div key={label} className="rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2"><div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div><div className="mt-1 text-xs font-semibold text-slate-200">{value}</div></div>)}</div>
             </div>
             <div className="mt-4"><ReadinessPanel checks={readinessChecks} readyCount={readinessReadyCount} /></div>
             {requiredWarnings.length ? <p className="mt-4 rounded-lg border border-rose-400/20 bg-rose-400/[0.06] p-3 text-xs text-rose-100">{requiredWarnings.map((warning) => warning.message).join(" ")}</p> : <p className="mt-4 rounded-lg border border-emerald-400/20 bg-emerald-400/[0.05] p-3 text-xs text-emerald-100">Required campaign fields are resolved or explicitly dismissed.</p>}
             <div className="mt-4 flex flex-wrap gap-2"><button type="button" onClick={() => setStep("review")} className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300">Edit review</button><button type="button" onClick={applyPreset} disabled={!!busy || !!requiredWarnings.length} className="rounded-lg bg-emerald-600 px-4 py-2 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-40">{busy === "save" ? "Saving…" : "Save & apply"}</button></div>
          </div>
          <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-4 text-xs leading-5 text-slate-400">
            <p className="font-semibold text-slate-200">Policy locks</p>
             <p className="mt-2">{manualOnly ? "Auto-publishing is locked by this preset; clips require human review." : "Publishing remains in review mode until you explicitly choose a connected route."}</p>
             <p className="mt-2">{draft.meme_policy?.allowed === false || draft.edit_policy?.meme_allowed === false ? "Meme mode is disabled by this campaign." : "Meme mode stays available when the campaign permits it."}</p>
             <p className="mt-2">{draft.quality_policy?.allowed === false ? "Quality delivery is disabled by this campaign." : "Quality delivery remains available when enabled below."}</p>
             <p className="mt-2">{draft.edit_policy?.reframe === false ? "Speaker framing is locked by this campaign." : "Speaker framing follows the campaign edit policy."}</p>
             <p className="mt-2">{draft.source_library?.some((item) => item.status === "suggested") ? "Suggested sources remain blocked until accepted." : "No suggested sources are currently waiting for approval."}</p>
          </div>
        </div>
      )}

      {step === "compliance" && (
        <div className="mt-4 space-y-4">
          <div className="grid gap-4 lg:grid-cols-[0.9fr_1.1fr]">
            <div className="rounded-xl border border-emerald-400/20 bg-emerald-400/[0.035] p-4">
              <h3 className="text-sm font-semibold text-white">Compliance report</h3>
              <p className="mt-1 text-xs leading-5 text-slate-400">Check required copy, hashtags, platforms, aspect ratio, captions, and source provenance before publishing.</p>
              <textarea value={complianceText} onChange={(event) => { setComplianceText(event.target.value); setCompliance(null); }} rows="5" className={`${inputClass} mt-3 w-full resize-y`} placeholder="Paste a caption or describe the clip for a review report" />
              <button type="button" onClick={runCompliance} disabled={!draft.name.trim() || !!busy} className="mt-3 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">{busy === "compliance" ? "Checking…" : "Run compliance check"}</button>
              {compliance ? <div className={`mt-3 rounded-lg border p-3 text-xs ${compliance.status === "pass" ? "border-emerald-400/20 bg-emerald-400/[0.05] text-emerald-100" : "border-amber-400/20 bg-amber-400/[0.05] text-amber-100"}`}><p className="font-semibold">Status: {compliance.status}</p><ul className="mt-2 space-y-1">{complianceResults.map((item, index) => <li key={`${item.platform || "platform"}-${item.rule || "rule"}-${index}`}>{item.platform ? `${item.platform}: ` : ""}{item.rule || "rule"}: {item.status} · {item.message}</li>)}</ul></div> : null}
            </div>
            <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-4">
              <div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-sm font-semibold text-white">Posting schedule</h3><p className="mt-1 text-xs text-slate-500">The public-only scheduler queues exactly two clips at distinct peak slots.</p></div><span className="rounded-full border border-emerald-400/20 px-2 py-1 text-[10px] text-emerald-200">{manualOnly ? "Manual publish policy" : "Review required"}</span></div>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <Field label="Saved publishing campaign"><select value={schedule.campaignId} onChange={(event) => setSchedule((current) => ({ ...current, campaignId: event.target.value }))} className={inputClass}><option value="">Choose campaign…</option>{campaigns.map((campaign) => <option key={campaign.id} value={campaign.id}>{campaign.name}</option>)}</select></Field>
                <Field label="Rendered job"><select value={schedule.jobId} onChange={(event) => setSchedule((current) => ({ ...current, jobId: event.target.value, clipIds: [] }))} className={inputClass}><option value="">Choose job…</option>{completedJobs.map((job) => <option key={job.id} value={job.id}>{job.title || job.id}</option>)}</select></Field>
                <Field label="Date"><input type="date" value={schedule.day} onChange={(event) => setSchedule((current) => ({ ...current, day: event.target.value }))} className={inputClass} /></Field>
                <Field label="Timezone"><input value={schedule.timezone} onChange={(event) => setSchedule((current) => ({ ...current, timezone: event.target.value }))} className={inputClass} /></Field>
              </div>
               {selectedJob ? <div className="mt-3 rounded-xl border border-slate-800 bg-slate-950/40 p-3"><p className="text-xs font-semibold text-slate-300">Select exactly two approved clips</p><div className="mt-2 grid gap-2 sm:grid-cols-2">{selectedJob.clips.map((clip) => <label key={clip.id} className={`flex items-center gap-2 text-xs ${clip.review_state === "approved" ? "text-slate-300" : "text-slate-600"}`}><input type="checkbox" checked={schedule.clipIds.includes(clip.id)} onChange={() => toggleClip(clip.id)} disabled={clip.review_state !== "approved"} className="accent-emerald-400" />{clip.title || clip.id}{clip.review_state !== "approved" ? " · approve first" : ""}</label>)}</div></div> : null}
                <div className="mt-3 grid gap-3 sm:grid-cols-3"><Field label="Peak slot 1"><input value={schedule.peakHours[0]} onChange={(event) => setSchedule((current) => ({ ...current, peakHours: [event.target.value, current.peakHours[1]] }))} className={inputClass} /></Field><Field label="Peak slot 2"><input value={schedule.peakHours[1]} onChange={(event) => setSchedule((current) => ({ ...current, peakHours: [current.peakHours[0], event.target.value] }))} className={inputClass} /></Field><div><span className="text-xs text-slate-400">Connected platforms</span><div className="mt-1 flex flex-wrap gap-1">{Object.entries(publisherStatuses).map(([platform, status]) => { const allowed = platformAllowed(platform); return <label key={platform} className={`rounded-full border px-2 py-1 text-[10px] ${schedule.platforms.includes(platform) ? "border-emerald-300 text-emerald-100" : "border-slate-700 text-slate-500"} ${!status.configured || !status.direct_publish || !allowed ? "opacity-40" : ""}`}><input type="checkbox" checked={schedule.platforms.includes(platform)} onChange={() => togglePlatform(platform)} disabled={!status.configured || !status.direct_publish || !allowed} className="mr-1" />{platform}</label>; })}</div></div></div>
                {requiredChecks.length ? <div className="mt-3 rounded-xl border border-amber-400/20 bg-amber-400/[0.04] p-3"><p className="text-xs font-semibold text-amber-100">Complete pre-submit checklist</p><div className="mt-2 grid gap-2 sm:grid-cols-2">{requiredChecks.map((check) => <label key={check.id} className="flex items-start gap-2 text-xs text-amber-100"><input type="checkbox" checked={scheduleChecks[check.id] === true} onChange={(event) => setScheduleChecks((current) => ({ ...current, [check.id]: event.target.checked }))} className="mt-0.5 accent-amber-300" /><span>{check.label}</span></label>)}</div></div> : null}
                <button type="button" onClick={scheduleDay} disabled={!!busy || manualOnly || !compliance || compliance.status !== "pass"} className="mt-3 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">{busy === "schedule" ? "Scheduling…" : manualOnly ? "Manual publish required" : !compliance || compliance.status !== "pass" ? "Pass compliance first" : "Schedule two public clips"}</button>
            </div>
          </div>
           <button type="button" onClick={() => setStep("review")} className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300">Back to preset review</button>
        </div>
      )}

      {message ? <p role="status" className="mt-4 text-xs text-emerald-300">{message}</p> : null}
      {error ? <p role="alert" className="mt-4 rounded-lg border border-rose-400/20 bg-rose-400/[0.06] px-3 py-2 text-xs text-rose-100">{error}</p> : null}
    </section>
  );
}
