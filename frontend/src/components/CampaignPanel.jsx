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
    () => jobs.filter((job) => Array.isArray(job.clips) && job.clips.length > 1),
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
    setDraft((current) => ({ ...current, settings: { ...(current.settings || {}), [key]: value } }));

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
    if (!draft.id) {
      setError("Save the preset before running its stored compliance report.");
      return;
    }
    setBusy("compliance");
    clearStatus();
    try {
      const result = await api.campaignPresetCompliance({
        profile_id: draft.id,
        visibility: "public",
        clip: {
          caption: complianceText,
          description: complianceText,
          hashtags: draft.copy_rules?.hashtags || [],
          platform: (Array.isArray(draft.posting?.platforms) && draft.posting.platforms.length
            ? draft.posting.platforms
            : draft.platforms || [])[0] || "",
          aspect_ratio: (draft.aspect_resolution?.aspect_ratios || [])[0] || "",
          captions: draft.captions_language?.required !== false,
          source_url: (draft.source_library || []).find((item) => item.status === "approved" && item.url)?.url || "",
        },
      });
      setCompliance(result);
      setMessage(result.status === "pass" ? "Compliance report passed." : "Review the compliance report before publishing.");
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
    if (presetPlatforms.length && !presetPlatforms.includes(platform)) return;
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
    if (presetPlatforms.length && schedule.platforms.some((platform) => !presetPlatforms.includes(platform))) {
      setError("The active campaign preset does not allow one or more scheduled platforms.");
      return;
    }
    setBusy("schedule");
    clearStatus();
    try {
      const result = await api.scheduleCampaignDay(schedule.campaignId, {
        job_id: schedule.jobId,
        clip_ids: schedule.clipIds,
        platforms: schedule.platforms,
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
  const presetRatios = draft.aspect_resolution?.aspect_ratios || [];
  const requiredChecks = draft.compliance?.required_checks || [];
  const manualOnly = !!(draft.posting?.manual_only || draft.posting?.review_only);
  const strategySettings = draft.settings || {};
  const hasImportInput = Boolean(requirementFiles.length || (usePaste ? pasteText.trim() : sources.trim()));
  const blueprintSignals = [
    ["Selection", strategySettings.selection_prompt ? "Directed by your brief" : "AI selects from the source"],
    ["Retention", selectLabel(STRATEGY_OPTIONS.retention_mode, strategySettings.retention_mode || "off", "Standard ranking")],
    ["Framing", strategySettings.speaker_reframe ? "Speaker-aware" : "Campaign default"],
    ["Captions", strategySettings.caption_preset || draft.captions_language?.style || "Karaoke"],
  ];

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
                <span className="text-[11px] text-slate-500">{requirementFiles.length ? "Local files take priority for this analysis; nothing is uploaded until the campaign is saved." : driveConfigured ? "Server-side Drive OAuth is configured." : "Drive OAuth is not configured; public links and paste remain available."}</span>
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
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Content type">
                <select value={strategySettings.content_type || ""} onChange={(event) => setStrategyField("content_type", event.target.value)} className={inputClass}>
                  {STRATEGY_OPTIONS.content_type.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </Field>
              <Field label="Clip goal">
                <select value={strategySettings.clip_goal || ""} onChange={(event) => setStrategyField("clip_goal", event.target.value)} className={inputClass}>
                  {STRATEGY_OPTIONS.clip_goal.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </Field>
              <Field label="Hook approach">
                <select value={strategySettings.hook_style || ""} onChange={(event) => setStrategyField("hook_style", event.target.value)} className={inputClass}>
                  {STRATEGY_OPTIONS.hook_style.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </Field>
              <Field label="Target clips" hint="A target, not a promise. The source may yield fewer usable moments.">
                <input type="number" min="1" max="50" value={strategySettings.num_clips || ""} onChange={(event) => setStrategyField("num_clips", event.target.value)} placeholder="Auto" className={inputClass} />
              </Field>
            </div>
            <div className="mt-3 grid gap-3 lg:grid-cols-[0.8fr_1.2fr]">
              <Field label="Audience / viewer promise">
                <input value={strategySettings.target_audience || ""} onChange={(event) => setStrategyField("target_audience", event.target.value)} placeholder="Who should stop scrolling?" className={inputClass} />
              </Field>
              <Field label="Selection direction" hint="This is sent to the clip-selection engine as a natural-language cue.">
                <textarea rows="2" value={strategySettings.selection_prompt || ""} onChange={(event) => setStrategyField("selection_prompt", event.target.value)} placeholder="Find moments with a clear payoff, a surprising insight, or a useful before/after transformation." className={`${inputClass} resize-y`} />
              </Field>
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Retention ranking">
                <select value={strategySettings.retention_mode || "off"} onChange={(event) => setStrategyField("retention_mode", event.target.value)} className={inputClass}>
                  {STRATEGY_OPTIONS.retention_mode.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </Field>
              <label className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-xs text-slate-300"><input type="checkbox" checked={!!strategySettings.storytelling_enabled} onChange={(event) => setStrategyField("storytelling_enabled", event.target.checked)} className="accent-cyan-400" /> Story-first assembly</label>
              <label className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-xs text-slate-300"><input type="checkbox" checked={!!strategySettings.speaker_reframe} onChange={(event) => setStrategyField("speaker_reframe", event.target.checked)} className="accent-cyan-400" /> Active-speaker framing</label>
              <label className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-xs text-slate-300"><input type="checkbox" checked={!!strategySettings.broll} onChange={(event) => setStrategyField("broll", event.target.checked)} className="accent-cyan-400" /> B-roll cues</label>
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Campaign name"><input value={draft.name} onChange={(event) => setDraftField("name", event.target.value)} className={inputClass} /></Field>
            <Field label="Aspect ratios" hint="Example: 9:16, 1:1"><input value={presetRatios.join(", ")} onChange={(event) => setDraftField("aspect_resolution", { ...draft.aspect_resolution, aspect_ratios: splitLines(event.target.value) })} className={inputClass} /></Field>
            <Field label="Allowed platforms" hint="Example: youtube_shorts, tiktok"><input value={presetPlatforms.join(", ")} onChange={(event) => { const platforms = splitLines(event.target.value); setDraft((current) => ({ ...current, platforms, posting: { ...(current.posting || {}), platforms } })); }} className={inputClass} /></Field>
            <Field label="Clip length"><input value={draft.length?.raw || draft.settings?.clip_length || ""} onChange={(event) => { const { length, clipLength } = lengthSettings(event.target.value); setDraft((current) => ({ ...current, length, settings: { ...(current.settings || {}), clip_length: clipLength } })); }} placeholder="30-60 seconds" className={inputClass} /></Field>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Caption style"><input value={strategySettings.caption_preset || draft.captions_language?.style || ""} onChange={(event) => { setNested("captions_language", "style", event.target.value); setStrategyField("caption_preset", event.target.value); }} className={inputClass} /></Field>
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
              <textarea value={complianceText} onChange={(event) => setComplianceText(event.target.value)} rows="5" className={`${inputClass} mt-3 w-full resize-y`} placeholder="Paste a caption or describe the clip for a review report" />
              <button type="button" onClick={runCompliance} disabled={!draft.id || !!busy} className="mt-3 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">{busy === "compliance" ? "Checking…" : "Run compliance check"}</button>
              {compliance ? <div className={`mt-3 rounded-lg border p-3 text-xs ${compliance.status === "pass" ? "border-emerald-400/20 bg-emerald-400/[0.05] text-emerald-100" : "border-amber-400/20 bg-amber-400/[0.05] text-amber-100"}`}><p className="font-semibold">Status: {compliance.status}</p><ul className="mt-2 space-y-1">{(compliance.profiles?.[0]?.results || compliance.results || []).map((item, index) => <li key={`${item.rule || "rule"}-${index}`}>{item.rule || "rule"}: {item.status} · {item.message}</li>)}</ul></div> : null}
            </div>
            <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-4">
              <div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-sm font-semibold text-white">Posting schedule</h3><p className="mt-1 text-xs text-slate-500">The public-only scheduler queues exactly two clips at distinct peak slots.</p></div><span className="rounded-full border border-emerald-400/20 px-2 py-1 text-[10px] text-emerald-200">{manualOnly ? "Manual publish policy" : "Review required"}</span></div>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <Field label="Saved publishing campaign"><select value={schedule.campaignId} onChange={(event) => setSchedule((current) => ({ ...current, campaignId: event.target.value }))} className={inputClass}><option value="">Choose campaign…</option>{campaigns.map((campaign) => <option key={campaign.id} value={campaign.id}>{campaign.name}</option>)}</select></Field>
                <Field label="Rendered job"><select value={schedule.jobId} onChange={(event) => setSchedule((current) => ({ ...current, jobId: event.target.value, clipIds: [] }))} className={inputClass}><option value="">Choose job…</option>{completedJobs.map((job) => <option key={job.id} value={job.id}>{job.title || job.id}</option>)}</select></Field>
                <Field label="Date"><input type="date" value={schedule.day} onChange={(event) => setSchedule((current) => ({ ...current, day: event.target.value }))} className={inputClass} /></Field>
                <Field label="Timezone"><input value={schedule.timezone} onChange={(event) => setSchedule((current) => ({ ...current, timezone: event.target.value }))} className={inputClass} /></Field>
              </div>
              {selectedJob ? <div className="mt-3 rounded-xl border border-slate-800 bg-slate-950/40 p-3"><p className="text-xs font-semibold text-slate-300">Select exactly two clips</p><div className="mt-2 grid gap-2 sm:grid-cols-2">{selectedJob.clips.map((clip) => <label key={clip.id} className="flex items-center gap-2 text-xs text-slate-400"><input type="checkbox" checked={schedule.clipIds.includes(clip.id)} onChange={() => toggleClip(clip.id)} className="accent-emerald-400" />{clip.title || clip.id}</label>)}</div></div> : null}
               <div className="mt-3 grid gap-3 sm:grid-cols-3"><Field label="Peak slot 1"><input value={schedule.peakHours[0]} onChange={(event) => setSchedule((current) => ({ ...current, peakHours: [event.target.value, current.peakHours[1]] }))} className={inputClass} /></Field><Field label="Peak slot 2"><input value={schedule.peakHours[1]} onChange={(event) => setSchedule((current) => ({ ...current, peakHours: [current.peakHours[0], event.target.value] }))} className={inputClass} /></Field><div><span className="text-xs text-slate-400">Connected platforms</span><div className="mt-1 flex flex-wrap gap-1">{Object.entries(publisherStatuses).map(([platform, status]) => { const allowed = !presetPlatforms.length || presetPlatforms.includes(platform); return <label key={platform} className={`rounded-full border px-2 py-1 text-[10px] ${schedule.platforms.includes(platform) ? "border-emerald-300 text-emerald-100" : "border-slate-700 text-slate-500"} ${!status.configured || !status.direct_publish || !allowed ? "opacity-40" : ""}`}><input type="checkbox" checked={schedule.platforms.includes(platform)} onChange={() => togglePlatform(platform)} disabled={!status.configured || !status.direct_publish || !allowed} className="mr-1" />{platform}</label>; })}</div></div></div>
               <button type="button" onClick={scheduleDay} disabled={!!busy || manualOnly} className="mt-3 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">{busy === "schedule" ? "Scheduling…" : manualOnly ? "Manual publish required" : "Schedule two public clips"}</button>
            </div>
          </div>
          {requiredChecks.length ? <div className="rounded-xl border border-amber-400/20 bg-amber-400/[0.04] p-4"><h3 className="text-sm font-semibold text-amber-100">Pre-submit checklist</h3><ul className="mt-2 grid gap-2 sm:grid-cols-2">{requiredChecks.map((check) => <li key={check.id || check.label} className="flex items-start gap-2 text-xs text-amber-100"><input type="checkbox" className="mt-0.5 accent-amber-300" /><span>{check.label || check}</span></li>)}</ul></div> : null}
          <button type="button" onClick={() => setStep("review")} className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300">Back to preset review</button>
        </div>
      )}

      {message ? <p role="status" className="mt-4 text-xs text-emerald-300">{message}</p> : null}
      {error ? <p role="alert" className="mt-4 rounded-lg border border-rose-400/20 bg-rose-400/[0.06] px-3 py-2 text-xs text-rose-100">{error}</p> : null}
    </section>
  );
}
