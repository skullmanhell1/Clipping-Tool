import { useMemo, useState } from "react";
import { api } from "../api.js";

const today = () => new Date().toISOString().slice(0, 10);

export default function CampaignAutomationPanel({
  campaigns = [],
  publisherStatuses = {},
  jobs = [],
  onPresetApplied,
  onScheduled,
}) {
  const [briefText, setBriefText] = useState("");
  const [preset, setPreset] = useState(null);
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
    if (!briefText.trim()) return;
    setBusy("parse");
    setError("");
    setMessage("");
    try {
      const result = await api.parseCampaignPreset(briefText);
      setPreset(result.preset);
      onPresetApplied?.(result.preset);
      setMessage("Preset parsed. Review the summary, then generate or schedule with this policy.");
    } catch (parseError) {
      setError(parseError.message || "Could not parse the campaign brief.");
    } finally {
      setBusy("");
    }
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

  return (
    <section className="rounded-2xl border border-violet-400/20 bg-violet-400/[0.04] p-4 shadow-lg shadow-violet-950/10">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-violet-300">Campaign automation</p>
          <h2 className="mt-1 text-lg font-semibold text-white">Brief → preset → public schedule</h2>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">
            Paste the Whop or Google Doc requirements. The parser creates a reviewable preset for
            brand, captions, formats, hashtags and hooks. Scheduling refuses drafts/private posts
            and queues exactly two clips at distinct peak-time slots.
          </p>
        </div>
        <span className="rounded-full border border-emerald-400/20 bg-emerald-400/[0.08] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-emerald-200">
          Public-only policy
        </span>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <label className="text-xs font-semibold text-slate-300" htmlFor="campaign-brief">
            Campaign brief text
          </label>
          <textarea
            id="campaign-brief"
            value={briefText}
            onChange={(event) => setBriefText(event.target.value)}
            className="h-36 w-full resize-y rounded-xl border border-slate-700 bg-slate-950/80 p-3 text-sm text-slate-100 outline-none focus:border-violet-300"
            placeholder={'Campaign: Creator Growth\nLogo: assets/brand/logo.png\nAspect ratios: 9:16\nHashtags: #brand #podcast\nDaily clips: 2\nPublic visibility: required\nPattern interrupter: every 5 seconds'}
          />
          <button
            type="button"
            onClick={parseBrief}
            disabled={busy === "parse" || !briefText.trim()}
            className="rounded-lg bg-violet-600 px-3 py-2 text-xs font-semibold text-white hover:bg-violet-500 disabled:opacity-40"
          >
            {busy === "parse" ? "Parsing…" : "Parse and apply preset"}
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
