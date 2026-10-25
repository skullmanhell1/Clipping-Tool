import { useMemo, useState } from "react";
import { api } from "../api.js";

const today = () => new Date().toISOString().slice(0, 10);

export default function CampaignAutomationPanel({
  campaigns = [],
  publisherStatuses = {},
  jobs = [],
  driveConfigured = false,
  onPresetApplied,
  onScheduled,
}) {
  const [driveUrl, setDriveUrl] = useState("");
  const [pasteText, setPasteText] = useState("");
  const [usePasteFallback, setUsePasteFallback] = useState(false);
  const [preset, setPreset] = useState(null);
  const [provenance, setProvenance] = useState(null);
  const [campaignId, setCampaignId] = useState("");
  const [jobId, setJobId] = useState("");
  const [clipIds, setClipIds] = useState([]);
  const [day, setDay] = useState(today);
  const [timezone, setTimezone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [peakHours, setPeakHours] = useState(["12:00", "19:00"]);
  const [platforms, setPlatforms] = useState([]);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const completedJobs = useMemo(
    () => jobs.filter((job) => Array.isArray(job.clips) && job.clips.length > 1),
    [jobs],
  );
  const selectedJob = completedJobs.find((job) => job.id === jobId);

  const parseBrief = async () => {
    const source = usePasteFallback ? pasteText : driveUrl;
    if (!source.trim()) return;
    setBusy("parse");
    setPreset(null);
    setProvenance(null);
    setError("");
    setMessage("");
    try {
      const result = usePasteFallback
        ? await api.parseCampaignPreset(pasteText)
        : await api.parseCampaignPresetFromDrive(driveUrl);
      setPreset(result.preset);
      setProvenance(result);
      setMessage("New brief loaded for review. Applying it replaces the previous campaign preset.");
    } catch (parseError) {
      setError(parseError.message || "Could not load the campaign brief.");
      if (!usePasteFallback && parseError.detail?.fall_back_to_paste) {
        setUsePasteFallback(true);
        setMessage("The link could not be read. Paste the brief text here to load it without Drive access.");
      }
    } finally {
      setBusy("");
    }
  };

  const clearBrief = () => {
    setPreset(null);
    setProvenance(null);
    setDriveUrl("");
    setPasteText("");
    setError("");
    setMessage("");
  };

  const applyPreset = () => {
    if (!preset) return;
    onPresetApplied?.(preset);
    setMessage("Preset applied to job options. No publish campaign was created or saved.");
  };

  const toggleClip = (id) => {
    setClipIds((current) =>
      current.includes(id)
        ? current.filter((item) => item !== id)
        : current.length < 2
          ? [...current, id]
          : current,
    );
  };

  const togglePlatform = (id) => {
    setPlatforms((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  };

  const schedule = async () => {
    setBusy("schedule");
    setError("");
    setMessage("");
    try {
      const result = await api.scheduleCampaignDay(campaignId, {
        job_id: jobId,
        clip_ids: clipIds,
        platforms,
        day,
        timezone,
        peak_hours: peakHours,
        visibility: "public",
      });
      onScheduled?.(result);
      setMessage(`Queued exactly two public clips for ${result.day}.`);
    } catch (scheduleError) {
      setError(scheduleError.message || "Could not schedule the campaign day.");
    } finally {
      setBusy("");
    }
  };

  const sourceLabel = provenance?.provenance?.source_type === "paste" ? "Pasted brief" : "Google Drive";
  const syncStatus = provenance?.sync?.status || provenance?.sync_status;
  const briefRules = preset?.brief_rules || {};
  const requiredMentionRule = briefRules.required_mentions || {};
  const mentionRows = [
    ...(requiredMentionRule.common?.length
      ? [["Shared tags", requiredMentionRule.common.join(" ")]]
      : []),
    ...Object.entries(requiredMentionRule.by_platform || {}).map(([platform, tags]) => [
      ({ youtube_shorts: "YouTube Shorts", instagram: "Instagram Reels", tiktok: "TikTok" })[platform] || platform,
      Array.isArray(tags) ? tags.join(" ") : String(tags || ""),
    ]),
  ];
  const durationRule = briefRules.clip_duration_seconds;
  const sourceRule = briefRules.source_policy || {};
  const audienceRule = briefRules.audience_requirement || {};
  const payoutRule = briefRules.payout || {};
  const ruleItems = [];
  if (durationRule?.min !== undefined || durationRule?.max !== undefined) {
    ruleItems.push(`Clip length: ${durationRule.min ?? "?"}–${durationRule.max ?? "?"} seconds`);
  }
  if (briefRules.subtitles_required) ruleItems.push("Subtitles: on for the whole clip (check accuracy)");
  if (mentionRows.length) {
    mentionRows.forEach(([platform, tags]) => ruleItems.push(`${platform}: ${tags}`));
  }
  if (sourceRule.allow_only_approved_sources || sourceRule.approved_sources?.length) {
    ruleItems.push(`Source: only use ${sourceRule.approved_sources?.join(", ") || "the approved source"}`);
  }
  if (briefRules.face_visible_within_seconds !== undefined) {
    const faceSubject = briefRules.face_subject || "the featured person";
    ruleItems.push(`Human check: ${faceSubject}'s face appears in the first ${briefRules.face_visible_within_seconds} seconds`);
  }
  if (audienceRule.minimum_percent !== undefined) {
    ruleItems.push(`After posting: ${audienceRule.minimum_percent}% of views from ${(audienceRule.regions || []).join(", ")}`);
  }
  if (briefRules.minimum_post_days !== undefined) {
    ruleItems.push(`After posting: keep each post up for ${briefRules.minimum_post_days} days`);
  }
  if (briefRules.minimum_engagement_percent !== undefined) {
    ruleItems.push(`Human check: at least ${briefRules.minimum_engagement_percent}% engagement`);
  }
  const payoutFacts = [];
  if (payoutRule.cpm_usd !== undefined) payoutFacts.push(`$${payoutRule.cpm_usd} per 1,000 views`);
  if (payoutRule.maximum_per_clip_usd !== undefined) payoutFacts.push(`up to $${payoutRule.maximum_per_clip_usd} per clip`);
  if (payoutRule.minimum_payout_usd !== undefined) payoutFacts.push(`$${payoutRule.minimum_payout_usd} minimum payout`);
  if (payoutFacts.length) ruleItems.push(`Payout facts: ${payoutFacts.join("; ")}`);
  if (briefRules.selection_guidance) ruleItems.push(`Choose clips: ${briefRules.selection_guidance}`);
  Object.entries(briefRules.platform_topic_guidance || {}).forEach(([platform, topics]) => {
    const label = ({ youtube_shorts: "YouTube Shorts", instagram: "Instagram Reels", tiktok: "TikTok" })[platform] || platform;
    const text = Array.isArray(topics) ? topics.join("; ") : String(topics || "");
    if (text) ruleItems.push(`${label} topic fit: ${text}`);
  });
  if (Array.isArray(briefRules.clip_formats)) {
    briefRules.clip_formats.forEach((format) => {
      if (format?.name && format?.guidance) ruleItems.push(`${format.name}: ${format.guidance}`);
    });
  }
  if (briefRules.suggested_caption_font) ruleItems.push(`Suggested caption font: ${briefRules.suggested_caption_font}`);
  if (briefRules.likes_must_be_visible) ruleItems.push("Human check: likes stay visible");
  if (briefRules.caption_guidance) ruleItems.push(`Caption: ${briefRules.caption_guidance}`);
  if (briefRules.prohibited_content?.length) {
    ruleItems.push(`Do not use: ${briefRules.prohibited_content.join(" ")}`);
  }
  if (!ruleItems.length && preset) ruleItems.push("No extra rules were picked up. Check the brief before applying.");

  return (
    <section className="rounded-2xl border border-violet-400/20 bg-violet-400/[0.04] p-4 shadow-lg shadow-violet-950/10">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-violet-300">Campaign automation</p>
          <h2 className="mt-1 text-lg font-semibold text-white">Campaign brief</h2>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">
            Load a Google Doc/Drive brief or paste its text. Check the rules, then apply the new brief to your job.
            The separate public scheduler stays below when you are ready to post.
          </p>
        </div>
        <span className="rounded-full border border-emerald-400/20 bg-emerald-400/[0.08] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-emerald-200">
          Public-only policy
        </span>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <label className="text-xs font-semibold text-slate-300" htmlFor="campaign-brief-url">
            Google Doc or Drive link
          </label>
          <input
            id="campaign-brief-url"
            type="url"
            value={driveUrl}
            onChange={(event) => {
              setDriveUrl(event.target.value);
              setPreset(null);
              setProvenance(null);
            }}
            className="w-full rounded-xl border border-slate-700 bg-slate-950/80 p-3 text-sm text-slate-100 outline-none focus:border-violet-300"
            placeholder="https://docs.google.com/document/d/... or drive.google.com/file/d/..."
            disabled={usePasteFallback}
          />
          <p className="text-[11px] leading-5 text-slate-500">
            {driveConfigured
              ? "The server tries Drive OAuth first, then a public link export. Private files need Viewer access for the server account; credentials never enter the browser."
              : "Public Google links can be read without OAuth. Private files need server-side Drive OAuth, or paste the brief text below."}
          </p>
          <button
            type="button"
            onClick={() => {
              setUsePasteFallback((current) => !current);
              setPreset(null);
              setProvenance(null);
              setError("");
              setMessage("");
            }}
            className="text-xs font-semibold text-violet-200 hover:text-white hover:underline"
          >
            {usePasteFallback ? "Use a Google Doc link instead" : "Use pasted brief instead (offline fallback)"}
          </button>
          {usePasteFallback ? (
            <textarea
              id="campaign-brief-paste"
              aria-label="Pasted campaign brief fallback"
              value={pasteText}
              onChange={(event) => {
                setPasteText(event.target.value);
                setPreset(null);
                setProvenance(null);
              }}
              className="h-36 w-full resize-y rounded-xl border border-slate-700 bg-slate-950/80 p-3 text-sm text-slate-100 outline-none focus:border-violet-300"
              placeholder={'Paste the full brief here\nExample: use official clips only; keep clips 20–60 seconds; add the required account tags.'}
            />
          ) : null}
          <button
            type="button"
            onClick={parseBrief}
            disabled={busy === "parse" || !(usePasteFallback ? pasteText : driveUrl).trim()}
            className="rounded-lg bg-violet-600 px-3 py-2 text-xs font-semibold text-white hover:bg-violet-500 disabled:opacity-40"
          >
            {busy === "parse" ? "Loading…" : usePasteFallback ? "Parse pasted brief" : "Load and review brief"}
          </button>
          {preset ? (
            <div className="rounded-xl border border-slate-700 bg-slate-950/60 p-3 text-xs text-slate-300">
              <div className="flex flex-wrap gap-2">
                <span>{preset.name || "Unnamed campaign"}</span>
                <span className="text-slate-500">{(preset.aspect_ratios || []).join(", ") || "Any format"}</span>
                <span className="text-slate-500">{(preset.hashtags || []).join(" ") || "No required hashtags"}</span>
              </div>
              <p className="mt-2 text-slate-500">
                {preset.daily_clip_target || 2} clips/day · {preset.require_public_visibility ? "public required" : "visibility flexible"} · {preset.pattern_interrupter_interval || 0}s visual resets
              </p>
              <p className="mt-2 text-[11px] text-slate-500">
                {sourceLabel}{syncStatus ? ` · ${syncStatus}` : ""}
                {provenance?.provenance?.file_id ? ` · file ${provenance.provenance.file_id}` : ""}
              </p>
              <div aria-label="Parsed brief rules" className="mt-3 rounded-lg border border-violet-300/15 bg-violet-300/[0.04] p-3">
                <p className="text-xs font-semibold text-violet-100">Rules found — check before applying</p>
                <ul className="mt-2 list-disc space-y-1 pl-4 text-[11px] leading-5 text-slate-300">
                  {ruleItems.map((item) => <li key={item}>{item}</li>)}
                </ul>
                {sourceRule.style_references?.length ? (
                  <p className="mt-2 text-[10px] text-slate-500">Style reference only: {sourceRule.style_references.join(", ")}</p>
                ) : null}
              </div>
              <button
                type="button"
                onClick={applyPreset}
                className="mt-3 rounded-lg border border-emerald-300/30 bg-emerald-400/10 px-3 py-2 text-xs font-semibold text-emerald-100 hover:bg-emerald-400/20"
              >
                Apply / replace campaign preset
              </button>
              <button type="button" onClick={clearBrief} className="ml-3 mt-3 text-xs font-semibold text-slate-400 hover:text-white hover:underline">
                Load a different brief
              </button>
              <p className="mt-2 text-[11px] text-slate-500">This replaces old brief-derived settings only. It does not create or save a publish campaign.</p>
            </div>
          ) : null}
        </div>

        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-xs text-slate-400">
              Campaign
              <select value={campaignId} onChange={(event) => setCampaignId(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm text-slate-100">
                <option value="">Choose campaign…</option>
                {campaigns.map((campaign) => <option key={campaign.id} value={campaign.id}>{campaign.name}</option>)}
              </select>
            </label>
            <label className="text-xs text-slate-400">
              Rendered job
              <select value={jobId} onChange={(event) => { setJobId(event.target.value); setClipIds([]); }} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm text-slate-100">
                <option value="">Choose job…</option>
                {completedJobs.map((job) => <option key={job.id} value={job.id}>{job.title || job.id}</option>)}
              </select>
            </label>
          </div>
          {selectedJob ? (
            <div className="rounded-xl border border-slate-700 bg-slate-950/50 p-3">
              <p className="mb-2 text-xs font-semibold text-slate-300">Select exactly two clips</p>
              <div className="grid gap-1 sm:grid-cols-2">
                {selectedJob.clips.map((clip) => (
                  <label key={clip.id} className="flex items-center gap-2 text-xs text-slate-400">
                    <input type="checkbox" checked={clipIds.includes(clip.id)} onChange={() => toggleClip(clip.id)} className="accent-violet-400" />
                    {clip.title || clip.id}
                  </label>
                ))}
              </div>
            </div>
          ) : null}
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="text-xs text-slate-400">Date<input type="date" value={day} onChange={(event) => setDay(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm text-slate-100" /></label>
            <label className="text-xs text-slate-400">Timezone<input value={timezone} onChange={(event) => setTimezone(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm text-slate-100" /></label>
            <div className="text-xs text-slate-400">Peak slots<div className="mt-1 flex gap-1"><input value={peakHours[0]} onChange={(event) => setPeakHours([event.target.value, peakHours[1]])} className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm text-slate-100" /><input value={peakHours[1]} onChange={(event) => setPeakHours([peakHours[0], event.target.value])} className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-sm text-slate-100" /></div></div>
          </div>
          <div>
            <p className="mb-1 text-xs text-slate-400">Connected platforms with direct-public approval</p>
            <div className="flex flex-wrap gap-2">
              {Object.entries(publisherStatuses).map(([platform, status]) => (
                <label key={platform} title={status.message} className={`rounded-full border px-2 py-1 text-xs ${platforms.includes(platform) ? "border-emerald-300 text-emerald-100" : "border-slate-700 text-slate-500"} ${!status.configured || !status.direct_publish ? "opacity-40" : ""}`}>
                  <input type="checkbox" checked={platforms.includes(platform)} onChange={() => togglePlatform(platform)} disabled={!status.configured || !status.direct_publish} className="mr-1" />
                  {platform}
                </label>
              ))}
            </div>
          </div>
          <button type="button" onClick={schedule} disabled={busy === "schedule" || !campaignId || !jobId || clipIds.length !== 2 || !platforms.length} className="rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-40">
            {busy === "schedule" ? "Scheduling…" : "Schedule exactly two public clips"}
          </button>
        </div>
      </div>
      {message ? <p role="status" className="mt-3 text-xs text-emerald-300">{message}</p> : null}
      {error ? <p role="alert" className="mt-3 text-xs text-rose-300">{error}</p> : null}
    </section>
  );
}
