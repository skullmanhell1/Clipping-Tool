import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

const splitLines = (value) => value.split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean);

const defaultProfile = {
  name: "",
  aspect_ratios: [],
  allowed_platforms: [],
  hashtags: [],
  forbidden_words: [],
  required_words: [],
  caption_style: "",
  cta: "",
  branding: { logo: "", font: "", colors: { primary: "", accent: "" } },
};

function profileFromPreset(preset = {}) {
  const isStructuredProfile = preset.aspect_resolution || preset.copy_rules || preset.banned_words_topics;
  if (isStructuredProfile) {
    return {
      ...defaultProfile,
      id: preset.id || "",
      name: preset.name || defaultProfile.name,
      aspect_ratios: preset.aspect_resolution?.aspect_ratios || [],
      allowed_platforms: preset.platforms || [],
      hashtags: preset.copy_rules?.hashtags || [],
      forbidden_words: preset.banned_words_topics?.words || [],
      required_words: preset.copy_rules?.required_copy || [],
      caption_style: preset.captions_language?.style || "",
      cta: preset.copy_rules?.cta || "",
      branding: {
        ...defaultProfile.branding,
        logo: preset.watermark_end_cards?.watermark || "",
      },
      ambiguities: preset.ambiguities || [],
      uncertainties: preset.uncertainties || [],
      rawProfile: preset,
    };
  }
  return {
    ...defaultProfile,
    ...preset,
    branding: { ...defaultProfile.branding, ...(preset.branding || {}), colors: { ...defaultProfile.branding.colors, ...(preset.branding?.colors || {}) } },
    aspect_ratios: Array.isArray(preset.aspect_ratios) ? preset.aspect_ratios : [],
    allowed_platforms: Array.isArray(preset.allowed_platforms) ? preset.allowed_platforms : [],
    hashtags: Array.isArray(preset.hashtags) ? preset.hashtags : [],
    forbidden_words: Array.isArray(preset.forbidden_words) ? preset.forbidden_words : [],
    required_words: Array.isArray(preset.required_words) ? preset.required_words : [],
  };
}

function validationTone(status) {
  if (status === "pass" || status === "ok") return "border-emerald-400/20 bg-emerald-400/[0.05] text-emerald-200";
  if (status === "fail" || status === "blocked") return "border-rose-400/20 bg-rose-400/[0.05] text-rose-200";
  return "border-amber-400/20 bg-amber-400/[0.05] text-amber-200";
}

export default function CampaignImportPanel({ onPresetApplied }) {
  const [sources, setSources] = useState("");
  const [items, setItems] = useState([]);
  const [profile, setProfile] = useState(defaultProfile);
  const [briefText, setBriefText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [standaloneValidation, setStandaloneValidation] = useState(null);
  const [showProfile, setShowProfile] = useState(true);
  const [savedProfiles, setSavedProfiles] = useState([]);
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [savedProfileId, setSavedProfileId] = useState("");

  useEffect(() => {
    api.campaignProfiles().then((data) => {
      setSavedProfiles(data.profiles || []);
      setSelectedProfileId(data.selected_id || "");
      setSavedProfileId(data.selected_id || "");
    }).catch(() => {});
  }, []);

  const warnings = useMemo(() => {
    const result = [];
    if (!profile.name.trim()) result.push("Campaign name is missing.");
    if (!profile.aspect_ratios.length) result.push("No aspect ratio was extracted; the current format will be kept.");
    if (!profile.allowed_platforms.length) result.push("No allowed platforms were extracted; review publishing routes.");
    if (profile.hashtags.some((tag) => !tag.startsWith("#"))) result.push("Some hashtags do not start with #.");
    return result;
  }, [profile]);

  const updateProfile = (key, value) => setProfile((current) => ({ ...current, [key]: value }));
  const importSources = async (force = false) => {
    const urls = splitLines(sources);
    if (!urls.length) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const importer = force ? api.syncCampaignProfiles : api.importCampaignProfiles;
      const imported = await importer(urls, { save: false, select: false });
      const results = (imported.sources || []).map((source) => ({
        url: source.source_url,
        status: source.status === "error" ? "error" : "synced",
        result: imported,
        error: source.error,
      }));
      setItems(results.length ? results : urls.map((url) => ({ url, status: "synced", result: imported })));
       if (imported.profile) {
         const nextProfile = profileFromPreset(imported.profile);
         setProfile(nextProfile);
         setSavedProfileId("");
         setBriefText(nextProfile.cta || nextProfile.required_words.join(" ") || "");
       }
       const failures = results.filter((item) => item.status === "error").length;
      setMessage(failures ? `${results.length - failures} source${results.length - failures === 1 ? "" : "s"} synced; ${failures} need attention.` : `${urls.length} campaign source${urls.length === 1 ? "" : "s"} synced.`);
    } catch (importError) {
      setError(importError.message || "Could not import campaign sources.");
    }
    setBusy(false);
  };

  const apply = () => {
    const preset = { ...profile, hashtags: profile.hashtags.map((tag) => tag.startsWith("#") ? tag : `#${tag}`) };
    onPresetApplied?.(preset);
    setMessage("Reviewed campaign profile applied to job settings.");
  };

  const saveProfile = async (select = false) => {
    setBusy(true);
    setError("");
    try {
      const payload = profile.rawProfile
        ? {
            ...profile.rawProfile,
            id: profile.rawProfile.id || profile.id || "",
            name: profile.name,
            platforms: profile.allowed_platforms,
            aspect_resolution: {
              ...(profile.rawProfile.aspect_resolution || {}),
              aspect_ratios: profile.aspect_ratios,
            },
            copy_rules: {
              ...(profile.rawProfile.copy_rules || {}),
              hashtags: profile.hashtags,
              required_copy: profile.required_words,
              cta: profile.cta,
            },
            banned_words_topics: {
              ...(profile.rawProfile.banned_words_topics || {}),
              words: profile.forbidden_words,
            },
          }
        : {
            name: profile.name,
            platforms: profile.allowed_platforms,
            aspect_resolution: { aspect_ratios: profile.aspect_ratios },
            copy_rules: { hashtags: profile.hashtags, required_copy: profile.required_words, cta: profile.cta },
            banned_words_topics: { words: profile.forbidden_words },
            captions_language: { style: profile.caption_style },
            watermark_end_cards: { watermark: profile.branding.logo },
          };
      const saved = await api.saveCampaignProfile(payload);
      setProfile((current) => ({ ...current, id: saved.id, rawProfile: saved }));
      setSavedProfileId(saved.id);
      setSavedProfiles((current) => [saved, ...current.filter((item) => item.id !== saved.id)]);
      if (select) {
        await api.selectCampaignProfile(saved.id);
        setSelectedProfileId(saved.id);
      }
      setMessage(select ? "Campaign profile saved and selected." : "Campaign profile saved.");
    } catch (saveError) {
      setError(saveError.message || "Could not save campaign profile.");
    } finally {
      setBusy(false);
    }
  };

  const validate = async () => {
    setBusy(true);
    setError("");
    try {
      const validationItems = await Promise.all(items.map(async (item) => {
        const imported = profileFromPreset(item.result?.preset || profile);
        const text = item.url === items[0]?.url && briefText.trim()
          ? briefText
          : [imported.name, imported.cta, ...imported.required_words, ...imported.hashtags].join(" ");
        try {
             const validation = savedProfileId
               ? await api.campaignProfilesCompliance({
                 profile_id: savedProfileId,
                clip: {
                  caption: text,
                  description: text,
                  hashtags: imported.hashtags,
                  platform: imported.allowed_platforms[0] || "",
                  aspect_ratio: imported.aspect_ratios[0] || "",
                  captions: true,
                },
              })
            : await api.checkBriefCompliance(text, imported.forbidden_words, imported.required_words);
          return { ...item, validation };
        } catch (validationError) {
          return { ...item, validation: { status: "unavailable", blockers: [validationError.message || "Compliance endpoint unavailable."] } };
        }
      }));
      const fallback = items.length === 0
         ? savedProfileId
           ? await api.campaignProfilesCompliance({
               profile_id: savedProfileId,
              clip: {
                caption: briefText || [profile.name, ...profile.required_words].join(" "),
                description: briefText,
                hashtags: profile.hashtags,
                platform: profile.allowed_platforms[0] || "",
                aspect_ratio: profile.aspect_ratios[0] || "",
                captions: true,
              },
            })
          : await api.checkBriefCompliance(briefText || [profile.name, ...profile.required_words, ...profile.hashtags].join(" "), profile.forbidden_words, profile.required_words)
        : null;
      setItems(validationItems);
      setStandaloneValidation(fallback);
      const results = validationItems.map((item) => item.validation).filter(Boolean);
      const failures = results.filter((result) => ["fail", "blocked"].includes(result.status)).length;
      setMessage(fallback ? `Validation ${fallback.status || "complete"}. Review blockers before publishing.` : `${results.length} campaign${results.length === 1 ? "" : "s"} validation result${results.length === 1 ? "" : "s"}; ${failures} blocked.`);
    } catch (validationError) {
      setError(validationError.message || "Campaign validation is unavailable.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="rounded-2xl border border-indigo-400/20 bg-indigo-400/[0.035] p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-indigo-200">Campaign workspace</p>
          <h3 className="mt-1 text-lg font-semibold text-white">Import &amp; compliance</h3>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">Import several Google Docs or Drive folder/file links, review what was extracted, then apply a single approved profile to this project.</p>
        </div>
        <span className="rounded-full border border-indigo-300/20 bg-indigo-400/10 px-2.5 py-1 text-[10px] text-indigo-100">Review required</span>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[1.05fr_0.95fr]">
        <div className="space-y-3">
          <label className="flex flex-col gap-1.5 text-sm"><span className="text-slate-400">Docs / folders / files</span><textarea value={sources} onChange={(event) => setSources(event.target.value)} rows="5" placeholder="One Google Doc or Drive URL per line" className="resize-y rounded-xl border border-slate-700 bg-slate-950/80 p-3 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-indigo-300" /><span className="text-[11px] text-slate-500">Folder URLs depend on the connected Drive importer. If a folder is not supported, import its documents individually.</span></label>
            <div className="flex flex-wrap gap-2"><button type="button" onClick={() => importSources(false)} disabled={busy || !sources.trim()} className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white hover:bg-indigo-500 disabled:opacity-40">{busy ? "Syncing…" : "Sync sources"}</button><button type="button" onClick={() => importSources(true)} disabled={busy || !sources.trim()} className="rounded-lg border border-indigo-300/30 px-3 py-2 text-xs font-semibold text-indigo-100 hover:bg-indigo-400/10 disabled:opacity-40">Force re-sync</button><button type="button" onClick={() => saveProfile(false)} disabled={busy || !profile.name.trim()} className="rounded-lg border border-indigo-300/30 px-3 py-2 text-xs font-semibold text-indigo-100 hover:bg-indigo-400/10 disabled:opacity-40">Save profile</button><button type="button" onClick={() => saveProfile(true)} disabled={busy || !profile.name.trim()} className="rounded-lg border border-emerald-300/30 px-3 py-2 text-xs font-semibold text-emerald-100 hover:bg-emerald-400/10 disabled:opacity-40">Save &amp; select</button><button type="button" onClick={validate} disabled={busy || (!briefText.trim() && !profile.name.trim())} className="rounded-lg border border-indigo-300/30 px-3 py-2 text-xs font-semibold text-indigo-100 hover:bg-indigo-400/10 disabled:opacity-40">Validate campaign</button></div>
            {savedProfiles.length ? <label className="flex flex-col gap-1.5 text-xs text-slate-400"><span>Saved campaign profiles</span><select value={selectedProfileId} onChange={async (event) => { const id = event.target.value; setSelectedProfileId(id); setSavedProfileId(id); const saved = savedProfiles.find((item) => item.id === id); if (saved) { setProfile(profileFromPreset(saved)); await api.selectCampaignProfile(id).catch(() => {}); } }} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100"><option value="">No selected profile</option>{savedProfiles.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label> : null}
          {items.length ? <div className="space-y-2">{items.map((item) => <div key={item.url} className="rounded-lg border border-slate-800 bg-slate-950/50 p-2.5 text-xs"><div className="flex items-center justify-between gap-2"><span className="max-w-[75%] truncate text-slate-300">{item.url}</span><span className={item.status === "synced" ? "text-emerald-300" : "text-rose-300"}>{item.status === "synced" ? (item.result?.sync_status || item.result?.sync?.status || "synced") : "needs attention"}</span></div>{item.error ? <p className="mt-1 text-[11px] text-rose-300">{item.error}</p> : null}{item.validation ? <p className={`mt-2 rounded border px-2 py-1 text-[11px] ${validationTone(item.validation.status)}`}>{item.validation.status || "validated"} · {(item.validation.blockers || []).join(" ") || "No blockers reported."}</p> : null}</div>)}</div> : <p className="text-xs text-slate-500">No sources synced yet.</p>}
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
          <button type="button" onClick={() => setShowProfile((value) => !value)} className="flex w-full items-center justify-between text-left"><span><span className="block text-xs font-semibold text-slate-200">Extracted campaign profile</span><span className="mt-1 block text-[11px] text-slate-500">Edit ambiguous fields before applying.</span></span><span className="text-slate-500">{showProfile ? "▾" : "▸"}</span></button>
          {showProfile ? <div className="mt-3 space-y-3"><label className="flex flex-col gap-1.5 text-xs text-slate-400">Name<input value={profile.name} onChange={(event) => updateProfile("name", event.target.value)} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100" /></label><div className="grid gap-3 sm:grid-cols-2"><label className="flex flex-col gap-1.5 text-xs text-slate-400">Aspect ratios<input value={profile.aspect_ratios.join(", ")} onChange={(event) => updateProfile("aspect_ratios", splitLines(event.target.value))} placeholder="9:16, 1:1" className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100" /></label><label className="flex flex-col gap-1.5 text-xs text-slate-400">Platforms<input value={profile.allowed_platforms.join(", ")} onChange={(event) => updateProfile("allowed_platforms", splitLines(event.target.value))} placeholder="tiktok, instagram" className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100" /></label></div><label className="flex flex-col gap-1.5 text-xs text-slate-400">Hashtags<input value={profile.hashtags.join(" ")} onChange={(event) => updateProfile("hashtags", splitLines(event.target.value))} placeholder="#brand #campaign" className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100" /></label><div className="grid gap-3 sm:grid-cols-2"><label className="flex flex-col gap-1.5 text-xs text-slate-400">Required words<textarea rows="2" value={profile.required_words.join(", ")} onChange={(event) => updateProfile("required_words", splitLines(event.target.value))} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100" /></label><label className="flex flex-col gap-1.5 text-xs text-slate-400">Forbidden words<textarea rows="2" value={profile.forbidden_words.join(", ")} onChange={(event) => updateProfile("forbidden_words", splitLines(event.target.value))} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100" /></label></div><div className="grid gap-3 sm:grid-cols-3"><label className="flex flex-col gap-1.5 text-xs text-slate-400">Caption style<input value={profile.caption_style} onChange={(event) => updateProfile("caption_style", event.target.value)} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100" /></label><label className="flex flex-col gap-1.5 text-xs text-slate-400">CTA<input value={profile.cta} onChange={(event) => updateProfile("cta", event.target.value)} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100" /></label><label className="flex flex-col gap-1.5 text-xs text-slate-400">Primary colour<input value={profile.branding.colors.primary} onChange={(event) => setProfile((current) => ({ ...current, branding: { ...current.branding, colors: { ...current.branding.colors, primary: event.target.value } } }))} placeholder="#7c3aed" className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100" /></label></div><div className="grid gap-2 sm:grid-cols-2"><div className="rounded-lg border border-slate-800 p-2.5 text-[11px] text-slate-500">{warnings.length ? warnings.map((warning) => <p key={warning} className="text-amber-200">⚠ {warning}</p>) : <p className="text-emerald-300">No obvious ambiguity found. Still review the source wording.</p>}</div><button type="button" onClick={apply} disabled={!profile.name.trim()} className="rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-40">Apply profile to settings</button></div></div> : null}
        </div>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2"><label className="flex flex-col gap-1.5 text-sm"><span className="text-slate-400">Caption / compliance text preview</span><textarea value={briefText} onChange={(event) => setBriefText(event.target.value)} rows="3" placeholder="Paste caption copy or a short brief for validation" className="resize-y rounded-xl border border-slate-700 bg-slate-950/80 p-3 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-indigo-300" /></label><div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3"><p className="text-xs font-semibold text-slate-200">Publish preview</p><p className="mt-2 text-sm leading-6 text-slate-300">{briefText || profile.cta || "Your caption preview will appear here after import."}</p><p className="mt-3 text-xs text-indigo-200">{profile.hashtags.map((tag) => tag.startsWith("#") ? tag : `#${tag}`).join(" ") || "#yourbrand #campaign"}</p><p className="mt-2 text-[11px] text-slate-500">Preview is editorial guidance; it does not publish or save a campaign.</p>{standaloneValidation ? <p className={`mt-3 rounded border px-2 py-1 text-[11px] ${validationTone(standaloneValidation.status)}`}>{standaloneValidation.status || "validated"} · {(standaloneValidation.blockers || []).join(" ") || "No blockers reported."}</p> : null}</div></div>
      {message ? <p role="status" className="mt-3 text-xs text-emerald-300">{message}</p> : null}{error ? <p role="alert" className="mt-3 text-xs text-rose-300">{error}</p> : null}
    </section>
  );
}
