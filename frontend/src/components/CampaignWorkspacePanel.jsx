import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

const today = () => new Date().toISOString().slice(0, 10);
const splitValues = (value) =>
  String(value || "")
    .split(/\r?\n|,|;/)
    .map((item) => item.trim())
    .filter(Boolean);

const EMPTY_PROFILE = {
  id: "",
  name: "",
  length: {},
  aspect_resolution: { aspect_ratios: [], resolutions: [] },
  platforms: [],
  branding: { logo: "", font: "", colors: {}, assets: [] },
  video_links: [],
  captions_language: { required: null, languages: [], style: "" },
  copy_rules: { hashtags: [], mentions: [], required_copy: [], cta: "", language: "" },
  watermark_end_cards: {
    watermark_required: null,
    watermark: "",
    end_card_required: null,
    end_card: "",
    public_visibility_required: null,
  },
  banned_words_topics: { words: [], topics: [] },
  source_rules: { allowed: [], prohibited: [], attribution: "", video_links: [] },
  audio_rules: { allowed: [], prohibited: [], attribution: "" },
  meme_policy: { allowed: null, rules: [] },
  payout_deadlines: { payout: "", deadlines: [] },
  ambiguities: [],
  uncertainties: [],
  provenance: [],
  raw_text_hash: "",
  parser: "deterministic",
  editable: true,
};

const profileFromApi = (value = {}) => ({
  ...EMPTY_PROFILE,
  ...value,
  length: { ...EMPTY_PROFILE.length, ...(value.length || {}) },
  aspect_resolution: { ...EMPTY_PROFILE.aspect_resolution, ...(value.aspect_resolution || {}) },
  branding: { ...EMPTY_PROFILE.branding, ...(value.branding || {}), colors: { ...(value.branding?.colors || {}) } },
  captions_language: { ...EMPTY_PROFILE.captions_language, ...(value.captions_language || {}) },
  copy_rules: { ...EMPTY_PROFILE.copy_rules, ...(value.copy_rules || {}) },
  watermark_end_cards: { ...EMPTY_PROFILE.watermark_end_cards, ...(value.watermark_end_cards || {}) },
  banned_words_topics: { ...EMPTY_PROFILE.banned_words_topics, ...(value.banned_words_topics || {}) },
  source_rules: { ...EMPTY_PROFILE.source_rules, ...(value.source_rules || {}) },
  audio_rules: { ...EMPTY_PROFILE.audio_rules, ...(value.audio_rules || {}) },
  meme_policy: { ...EMPTY_PROFILE.meme_policy, ...(value.meme_policy || {}) },
  payout_deadlines: { ...EMPTY_PROFILE.payout_deadlines, ...(value.payout_deadlines || {}) },
  platforms: Array.isArray(value.platforms) ? value.platforms : [],
  video_links: Array.isArray(value.video_links) ? value.video_links : [],
});

const profileToPreset = (profile) => ({
  name: profile.name,
  aspect_ratios: profile.aspect_resolution?.aspect_ratios || [],
  allowed_platforms: profile.platforms || [],
  hashtags: profile.copy_rules?.hashtags || [],
  forbidden_words: profile.banned_words_topics?.words || [],
  required_words: profile.copy_rules?.required_copy || [],
  caption_style: profile.captions_language?.style || "",
  cta: profile.copy_rules?.cta || "",
  branding: profile.branding || {},
  video_links: profile.video_links || [],
  campaign_profile: profile,
});

function validationTone(status) {
  if (status === "pass" || status === "ok") return "border-emerald-400/20 bg-emerald-400/[0.05] text-emerald-200";
  if (status === "fail" || status === "blocked") return "border-rose-400/20 bg-rose-400/[0.05] text-rose-200";
  return "border-amber-400/20 bg-amber-400/[0.05] text-amber-200";
}

function TextField({ label, value, onChange, multiline = false, placeholder = "" }) {
  const shared = {
    value: value || "",
    onChange: (event) => onChange(event.target.value),
    placeholder,
    className: "rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-indigo-300",
  };
  return (
    <label className="flex flex-col gap-1.5 text-xs text-slate-400">
      <span>{label}</span>
      {multiline ? <textarea {...shared} rows="2" /> : <input {...shared} />}
    </label>
  );
}

export default function CampaignWorkspacePanel({
  onPresetApplied,
  onSourcesExtracted,
  campaigns = [],
  publisherStatuses = {},
  jobs = [],
  onScheduled,
}) {
  const [sourceText, setSourceText] = useState("");
  const [profile, setProfile] = useState(profileFromApi());
  const [savedProfiles, setSavedProfiles] = useState([]);
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [savedProfileId, setSavedProfileId] = useState("");
  const [briefPreview, setBriefPreview] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [showRules, setShowRules] = useState(true);
  const [showSchedule, setShowSchedule] = useState(false);
  const [campaignId, setCampaignId] = useState("");
  const [jobId, setJobId] = useState("");
  const [clipIds, setClipIds] = useState([]);
  const [day, setDay] = useState(today);
  const [timezone, setTimezone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [peakHours, setPeakHours] = useState(["12:00", "19:00"]);
  const [platforms, setPlatforms] = useState([]);
  const [validation, setValidation] = useState(null);

  useEffect(() => {
    api.campaignProfiles().then((data) => {
      setSavedProfiles(data.profiles || []);
      setSelectedProfileId(data.selected_id || "");
      setSavedProfileId(data.selected_id || "");
      const selected = (data.profiles || []).find((item) => item.id === data.selected_id);
      if (selected) setProfile(profileFromApi(selected));
    }).catch(() => {});
  }, []);

  const completedJobs = useMemo(
    () => jobs.filter((job) => Array.isArray(job.clips) && job.clips.length > 1),
    [jobs],
  );
  const selectedJob = completedJobs.find((job) => job.id === jobId);
  const sourceLinks = profile.video_links || profile.source_rules?.video_links || [];
  const issueCount = (profile.ambiguities || []).length + (profile.uncertainties || []).length;
  const update = (key, value) => setProfile((current) => ({ ...current, [key]: value }));
  const updateNested = (section, key, value) =>
    setProfile((current) => ({ ...current, [section]: { ...(current[section] || {}), [key]: value } }));

  const importBrief = async (force = false) => {
    const raw = sourceText.trim();
    if (!raw) return;
    setBusy("import");
    setError("");
    setMessage("");
    setValidation(null);
    try {
      const lines = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
      const urlLines = lines.filter((line) => /^https?:\/\//i.test(line));
      const result = urlLines.length === lines.length
        ? await (force ? api.syncCampaignProfiles : api.importCampaignProfiles)(urlLines, { save: false, select: false })
        : await api.parseCampaignProfile(raw);
      const next = profileFromApi(result.profile || {});
      setProfile(next);
      setBriefPreview(raw);
      setSavedProfileId("");
      const importedSources = result.sources || [];
      if (next.video_links.length) onSourcesExtracted?.(next.video_links);
      setMessage(`${next.video_links.length ? `${next.video_links.length} video source${next.video_links.length === 1 ? "" : "s"} found. ` : ""}Review the extracted rules before applying them.`);
      if (importedSources.length) setMessage(`${importedSources.length} source${importedSources.length === 1 ? "" : "s"} synced. ${next.video_links.length ? `${next.video_links.length} video links found. ` : ""}Review the extracted rules before applying them.`);
    } catch (importError) {
      setError(importError.message || "Could not import the campaign brief.");
    } finally {
      setBusy("");
    }
  };

  const applyProfile = () => {
    const preset = profileToPreset(profile);
    onPresetApplied?.(preset);
    if (sourceLinks.length) onSourcesExtracted?.(sourceLinks);
    setMessage("Campaign rules applied to clipping settings and source queue.");
  };

  const saveProfile = async (select = false) => {
    setBusy("save");
    setError("");
    try {
      const saved = await api.saveCampaignProfile(profile);
      setProfile(profileFromApi(saved));
      setSavedProfiles((current) => [saved, ...current.filter((item) => item.id !== saved.id)]);
      setSavedProfileId(saved.id);
      if (select) {
        await api.selectCampaignProfile(saved.id);
        setSelectedProfileId(saved.id);
      }
      setMessage(select ? "Campaign profile saved and selected." : "Campaign profile saved.");
    } catch (saveError) {
      setError(saveError.message || "Could not save campaign profile.");
    } finally {
      setBusy("");
    }
  };

  const validate = async () => {
    setBusy("validate");
    setError("");
    try {
      const text = briefPreview || [profile.name, ...(profile.copy_rules?.required_copy || []), profile.copy_rules?.cta].filter(Boolean).join(" ");
      const result = savedProfileId
        ? await api.campaignProfilesCompliance({ profile_id: savedProfileId, clip: { caption: text, description: text, hashtags: profile.copy_rules?.hashtags || [], platform: profile.platforms?.[0] || "", aspect_ratio: profile.aspect_resolution?.aspect_ratios?.[0] || "", captions: true } })
        : await api.checkBriefCompliance(text, profile.banned_words_topics?.words || [], profile.copy_rules?.required_copy || []);
      setValidation(result);
      setMessage(`Validation ${result.status || "complete"}. Review any blockers before publishing.`);
    } catch (validationError) {
      setError(validationError.message || "Campaign validation is unavailable.");
    } finally {
      setBusy("");
    }
  };

  const schedule = async () => {
    setBusy("schedule");
    setError("");
    try {
      const result = await api.scheduleCampaignDay(campaignId, { job_id: jobId, clip_ids: clipIds, platforms, day, timezone, peak_hours: peakHours, visibility: "public" });
      onScheduled?.(result);
      setMessage(`Queued exactly two public clips for ${result.day}.`);
    } catch (scheduleError) {
      setError(scheduleError.message || "Could not schedule the campaign day.");
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="rounded-2xl border border-indigo-400/20 bg-indigo-400/[0.035] p-5 shadow-lg shadow-indigo-950/10">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-indigo-200">Campaign setup</p>
          <h3 className="mt-1 text-lg font-semibold text-white">One brief. One approved profile.</h3>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">
            Paste Google Docs/Drive links or the brief itself. We extract sources, branding, formats,
            captions, copy rules and restrictions once, then apply the approved profile everywhere.
          </p>
        </div>
        <span className="rounded-full border border-indigo-300/20 bg-indigo-400/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-indigo-100">
          Review before apply
        </span>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,0.92fr)_minmax(0,1.08fr)]">
        <div className="space-y-3">
          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-slate-300">Campaign brief or source links</span>
            <textarea
              aria-label="Campaign brief or source links"
              value={sourceText}
              onChange={(event) => setSourceText(event.target.value)}
              rows="8"
              placeholder="Paste one Google Doc / Drive link per line, or paste the Whop brief text here"
              className="resize-y rounded-xl border border-slate-700 bg-slate-950/80 p-3 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-indigo-300"
            />
            <span className="text-[11px] leading-5 text-slate-500">Links use the server's read-only Drive access. Pasted briefs work offline.</span>
          </label>
          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={() => importBrief(false)} disabled={busy === "import" || !sourceText.trim()} className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-500 disabled:opacity-40">
              {busy === "import" ? "Reading brief…" : "Read brief"}
            </button>
            <button type="button" onClick={() => importBrief(true)} disabled={busy === "import" || !sourceText.trim() || !/^https?:\/\//im.test(sourceText)} className="rounded-lg border border-indigo-300/30 px-3 py-2 text-xs font-semibold text-indigo-100 hover:bg-indigo-400/10 disabled:opacity-40">
              Force re-sync links
            </button>
            <button type="button" onClick={() => saveProfile(false)} disabled={busy === "save" || !profile.name.trim()} className="rounded-lg border border-indigo-300/30 px-3 py-2 text-xs font-semibold text-indigo-100 hover:bg-indigo-400/10 disabled:opacity-40">
              Save profile
            </button>
            <button type="button" onClick={() => saveProfile(true)} disabled={busy === "save" || !profile.name.trim()} className="rounded-lg border border-emerald-300/30 px-3 py-2 text-xs font-semibold text-emerald-100 hover:bg-emerald-400/10 disabled:opacity-40">
              Save &amp; select
            </button>
          </div>

          {savedProfiles.length ? (
            <label className="flex flex-col gap-1.5 text-xs text-slate-400">
              <span>Saved campaign profile</span>
              <select
                value={selectedProfileId}
                onChange={async (event) => {
                  const id = event.target.value;
                  setSelectedProfileId(id);
                  setSavedProfileId(id);
                  const saved = savedProfiles.find((item) => item.id === id);
                  if (saved) {
                    setProfile(profileFromApi(saved));
                    await api.selectCampaignProfile(id).catch(() => {});
                  }
                }}
                className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100"
              >
                <option value="">Choose a saved profile…</option>
                {savedProfiles.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
              </select>
            </label>
          ) : null}

          <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-xs font-semibold text-slate-200">Extracted sources</p>
                <p className="mt-1 text-[11px] text-slate-500">These links are added to the video queue when you apply the profile.</p>
              </div>
              <span className="rounded-full bg-slate-800 px-2 py-1 text-[11px] text-slate-300">{sourceLinks.length} found</span>
            </div>
            {sourceLinks.length ? (
              <ul className="mt-3 max-h-36 space-y-1 overflow-auto text-xs text-slate-300">
                {sourceLinks.map((url) => <li key={url} className="truncate rounded bg-slate-900 px-2 py-1">{url}</li>)}
              </ul>
            ) : <p className="mt-3 text-xs text-slate-500">No video links found yet.</p>}
          </div>

          {issueCount ? (
            <div className="rounded-xl border border-amber-400/20 bg-amber-400/[0.05] p-3 text-xs text-amber-100">
              <p className="font-semibold">{issueCount} item{issueCount === 1 ? " needs" : "s need"} review</p>
              <ul className="mt-2 list-disc space-y-1 pl-4 text-[11px] text-amber-200/80">
                {[...(profile.ambiguities || []), ...(profile.uncertainties || [])].slice(0, 5).map((item, index) => <li key={`${item.field || "issue"}-${index}`}>{item.field}: {item.reason || "conflicting values found"}</li>)}
              </ul>
            </div>
          ) : null}
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
          <button type="button" onClick={() => setShowRules((value) => !value)} className="flex w-full items-center justify-between text-left">
            <span>
              <span className="block text-xs font-semibold text-slate-200">Campaign rules</span>
              <span className="mt-1 block text-[11px] text-slate-500">Edit anything the importer could not infer perfectly.</span>
            </span>
            <span className="text-slate-500" aria-hidden="true">{showRules ? "▾" : "▸"}</span>
          </button>
          {showRules ? (
            <div className="mt-3 space-y-3">
              <TextField label="Campaign name" value={profile.name} onChange={(value) => update("name", value)} placeholder="Untitled campaign" />
              <div className="grid gap-3 sm:grid-cols-2">
                <TextField label="Clip length" value={profile.length?.raw} onChange={(value) => update("length", { ...profile.length, raw: value })} placeholder="15–30 seconds" />
                <TextField label="Aspect ratios" value={(profile.aspect_resolution?.aspect_ratios || []).join(", ")} onChange={(value) => updateNested("aspect_resolution", "aspect_ratios", splitValues(value))} placeholder="9:16, 1:1" />
                <TextField label="Platforms" value={(profile.platforms || []).join(", ")} onChange={(value) => update("platforms", splitValues(value))} placeholder="youtube_shorts, instagram" />
                <TextField label="Caption style" value={profile.captions_language?.style} onChange={(value) => updateNested("captions_language", "style", value)} placeholder="Karaoke, boxed…" />
              </div>
              <TextField label="Required hashtags" value={(profile.copy_rules?.hashtags || []).join(" ")} onChange={(value) => updateNested("copy_rules", "hashtags", splitValues(value))} placeholder="#brand #campaign" />
              <div className="grid gap-3 sm:grid-cols-2">
                <TextField label="Required copy / words" value={(profile.copy_rules?.required_copy || []).join(", ")} onChange={(value) => updateNested("copy_rules", "required_copy", splitValues(value))} multiline />
                <TextField label="Forbidden words" value={(profile.banned_words_topics?.words || []).join(", ")} onChange={(value) => updateNested("banned_words_topics", "words", splitValues(value))} multiline />
                <TextField label="Forbidden topics" value={(profile.banned_words_topics?.topics || []).join(", ")} onChange={(value) => updateNested("banned_words_topics", "topics", splitValues(value))} multiline />
                <TextField label="CTA" value={profile.copy_rules?.cta} onChange={(value) => updateNested("copy_rules", "cta", value)} />
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <TextField label="Brand logo / watermark" value={profile.branding?.logo || profile.watermark_end_cards?.watermark} onChange={(value) => { updateNested("branding", "logo", value); updateNested("watermark_end_cards", "watermark", value); }} />
                <TextField label="Brand font" value={profile.branding?.font} onChange={(value) => updateNested("branding", "font", value)} />
                <TextField label="Primary colour" value={profile.branding?.colors?.primary} onChange={(value) => updateNested("branding", "colors", { ...(profile.branding?.colors || {}), primary: value })} placeholder="#7c3aed" />
                <TextField label="Accent colour" value={profile.branding?.colors?.accent} onChange={(value) => updateNested("branding", "colors", { ...(profile.branding?.colors || {}), accent: value })} placeholder="#22d3ee" />
                <TextField label="Allowed source rules" value={(profile.source_rules?.allowed || []).join(", ")} onChange={(value) => updateNested("source_rules", "allowed", splitValues(value))} multiline />
                <TextField label="Prohibited source rules" value={(profile.source_rules?.prohibited || []).join(", ")} onChange={(value) => updateNested("source_rules", "prohibited", splitValues(value))} multiline />
              </div>
              <TextField label="All extracted video links" value={sourceLinks.join("\n")} onChange={(value) => { const links = splitValues(value); update("video_links", links); updateNested("source_rules", "video_links", links); }} multiline />
              <div className="grid gap-3 sm:grid-cols-2">
                <TextField label="End card" value={profile.watermark_end_cards?.end_card} onChange={(value) => updateNested("watermark_end_cards", "end_card", value)} />
                <TextField label="Deadlines" value={(profile.payout_deadlines?.deadlines || []).join(", ")} onChange={(value) => updateNested("payout_deadlines", "deadlines", splitValues(value))} />
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button type="button" onClick={applyProfile} disabled={!profile.name.trim() && !sourceLinks.length} className="rounded-lg bg-emerald-600 px-4 py-2 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-40">Apply all rules to clipping</button>
        <button type="button" onClick={validate} disabled={busy === "validate"} className="rounded-lg border border-indigo-300/30 px-3 py-2 text-xs font-semibold text-indigo-100 hover:bg-indigo-400/10 disabled:opacity-40">{busy === "validate" ? "Validating…" : "Validate campaign"}</button>
        <button type="button" onClick={() => setShowSchedule((value) => !value)} className="rounded-lg border border-slate-700 px-3 py-2 text-xs font-semibold text-slate-300 hover:border-slate-500">{showSchedule ? "Hide publishing schedule" : "Schedule selected clips"}</button>
      </div>

      {validation ? <div className={`mt-3 rounded-lg border px-3 py-2 text-xs ${validationTone(validation.status)}`}>{validation.status || "validated"} · {(validation.blockers || []).join(" ") || "No blockers reported."}</div> : null}

      {showSchedule ? (
        <div className="mt-4 rounded-xl border border-slate-800 bg-slate-950/50 p-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-xs text-slate-400">Campaign<select value={campaignId} onChange={(event) => setCampaignId(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-2 text-sm text-slate-100"><option value="">Choose campaign…</option>{campaigns.map((campaign) => <option key={campaign.id} value={campaign.id}>{campaign.name}</option>)}</select></label>
            <label className="text-xs text-slate-400">Rendered job<select value={jobId} onChange={(event) => { setJobId(event.target.value); setClipIds([]); }} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-2 text-sm text-slate-100"><option value="">Choose job…</option>{completedJobs.map((job) => <option key={job.id} value={job.id}>{job.title || job.id}</option>)}</select></label>
          </div>
          {selectedJob ? <div className="mt-3 rounded-lg border border-slate-800 p-3"><p className="mb-2 text-xs font-semibold text-slate-300">Select exactly two clips</p><div className="grid gap-1 sm:grid-cols-2">{selectedJob.clips.map((clip) => <label key={clip.id} className="flex items-center gap-2 text-xs text-slate-400"><input type="checkbox" checked={clipIds.includes(clip.id)} onChange={() => setClipIds((current) => current.includes(clip.id) ? current.filter((item) => item !== clip.id) : current.length < 2 ? [...current, clip.id] : current)} className="accent-indigo-400" />{clip.title || clip.id}</label>)}</div></div> : null}
          <div className="mt-3 grid gap-3 sm:grid-cols-3">
            <label className="text-xs text-slate-400">Date<input type="date" value={day} onChange={(event) => setDay(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-2 text-sm text-slate-100" /></label>
            <label className="text-xs text-slate-400">Timezone<input value={timezone} onChange={(event) => setTimezone(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-2 text-sm text-slate-100" /></label>
            <div className="text-xs text-slate-400">Peak slots<div className="mt-1 flex gap-1"><input value={peakHours[0]} onChange={(event) => setPeakHours([event.target.value, peakHours[1]])} className="w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-2 text-sm text-slate-100" /><input value={peakHours[1]} onChange={(event) => setPeakHours([peakHours[0], event.target.value])} className="w-full rounded-lg border border-slate-700 bg-slate-900 px-2 py-2 text-sm text-slate-100" /></div></div>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">{Object.entries(publisherStatuses).map(([platform, status]) => <label key={platform} title={status.message} className={`rounded-full border px-2 py-1 text-xs ${platforms.includes(platform) ? "border-emerald-300 text-emerald-100" : "border-slate-700 text-slate-500"} ${!status.configured || !status.direct_publish ? "opacity-40" : ""}`}><input type="checkbox" checked={platforms.includes(platform)} onChange={() => setPlatforms((current) => current.includes(platform) ? current.filter((item) => item !== platform) : [...current, platform])} disabled={!status.configured || !status.direct_publish} className="mr-1" />{platform}</label>)}</div>
          <button type="button" onClick={schedule} disabled={busy === "schedule" || !campaignId || !jobId || clipIds.length !== 2 || !platforms.length} className="mt-3 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-40">{busy === "schedule" ? "Scheduling…" : "Schedule exactly two public clips"}</button>
        </div>
      ) : null}

      {message ? <p role="status" className="mt-3 text-xs text-emerald-300">{message}</p> : null}
      {error ? <p role="alert" className="mt-3 text-xs text-rose-300">{error}</p> : null}
    </section>
  );
}
