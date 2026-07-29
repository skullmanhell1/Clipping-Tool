import { useEffect, useState } from "react";
import { api, formatDuration } from "../api.js";

const PLATFORM_LABELS = {
  whop: "Whop",
  youtube: "YouTube",
  tiktok: "TikTok",
  instagram: "Instagram",
  x: "X",
};

const ATTEMPT_COLORS = {
  published: "text-emerald-400",
  private: "text-blue-400",
  draft: "text-blue-400",
  failed: "text-rose-400",
  review_required: "text-amber-400",
  scheduled: "text-violet-400",
  queued: "text-slate-300",
  uploading: "text-cyan-400",
};

function scoreColor(score) {
  if (score >= 80) return "bg-emerald-500/90";
  if (score >= 60) return "bg-lime-500/90";
  if (score >= 40) return "bg-amber-500/90";
  if (score > 0) return "bg-orange-500/90";
  return "bg-slate-600/90";
}

function Field({ label, children, onRegenerate, busy }) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
          {label}
        </span>
        {onRegenerate && (
          <button
            type="button"
            onClick={onRegenerate}
            disabled={busy}
            title={`Regenerate ${label}`}
            className="text-xs text-brand-accent hover:underline disabled:opacity-50"
          >
            {busy ? "…" : "↻ regenerate"}
          </button>
        )}
      </div>
      {children}
    </div>
  );
}

const toEpoch = (localDateTime) => {
  if (!localDateTime) return null;
  const milliseconds = new Date(localDateTime).getTime();
  return Number.isNaN(milliseconds) ? null : milliseconds / 1000;
};

export default function ClipCard({
  jobId,
  clip,
  llmAvailable,
  publishing,
  publisherStatuses,
  attempts,
  onUpdated,
  onPublished,
}) {
  const [form, setForm] = useState({
    title: clip.title || "",
    description: clip.description || "",
    hashtags: (clip.hashtags || []).join(" "),
    hook_text: clip.hook_text || "",
    cta: clip.cta || "",
    thumbnail_text: clip.thumbnail_text || "",
  });
  const [selectedPlatforms, setSelectedPlatforms] = useState(
    publishing?.platforms || []
  );
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [publishingNow, setPublishingNow] = useState(false);
  const [busyField, setBusyField] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    setSelectedPlatforms(publishing?.platforms || []);
  }, [publishing?.platforms]);

  const update = (key) => (event) => {
    setForm((current) => ({ ...current, [key]: event.target.value }));
    setDirty(true);
  };

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const payload = {
        title: form.title,
        description: form.description,
        hashtags: form.hashtags.split(/[\s,]+/).filter(Boolean),
        hook_text: form.hook_text,
        cta: form.cta,
        thumbnail_text: form.thumbnail_text,
      };
      const updated = await api.editClip(jobId, clip.id, payload);
      onUpdated?.(updated);
      setDirty(false);
    } catch (saveError) {
      setError(saveError.message || "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const regenerate = (field) => async () => {
    setBusyField(field);
    setError("");
    try {
      const { value } = await api.regenerateField(jobId, clip.id, field);
      if (field === "hashtags") {
        setForm((current) => ({
          ...current,
          hashtags: (value || []).join(" "),
        }));
      } else {
        setForm((current) => ({ ...current, [field]: value }));
      }
      setDirty(true);
    } catch (regenerateError) {
      setError(regenerateError.message || "Regenerate failed");
    } finally {
      setBusyField("");
    }
  };

  // Named `applyAlternative`, not `useAlternative`: the `use` prefix is reserved for
  // hooks, and React's rules-of-hooks lint treats a `use*` call inside a callback as a
  // hook-order violation. This is an ordinary event handler.
  const applyAlternative = (alternative) => {
    setForm((current) => ({ ...current, title: alternative }));
    setDirty(true);
  };

  const togglePlatform = (platform) => {
    setSelectedPlatforms((current) =>
      current.includes(platform)
        ? current.filter((item) => item !== platform)
        : [...current, platform]
    );
  };

  const publish = async () => {
    setError("");
    if (!selectedPlatforms.length) {
      setError("Select at least one configured platform.");
      return;
    }
    if (dirty) {
      setError("Save metadata changes before publishing.");
      return;
    }

    const routes = {};
    selectedPlatforms.forEach((platform) => {
      routes[platform] = {
        account_id: publishing.account_id || "",
        target_type: platform === "whop" ? publishing.target_type || "" : "",
        target_id: publishing.target_id || "",
      };
    });

    setPublishingNow(true);
    try {
      const result = await api.publishClip(jobId, clip.id, {
        platforms: selectedPlatforms,
        campaign_id: publishing.campaign_id || "",
        mode: publishing.mode || "review",
        schedule_at: toEpoch(publishing.schedule),
        routes,
      });
      onPublished?.(result.attempts || []);
    } catch (publishError) {
      setError(publishError.message || "Publish request failed.");
    } finally {
      setPublishingNow(false);
    }
  };

  const inputClass =
    "w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-brand-accent";
  const sortedAttempts = [...(attempts || [])].sort(
    (left, right) => (right.created_at || 0) - (left.created_at || 0)
  );

  return (
    <div className="flex flex-col gap-3 overflow-hidden rounded-xl border border-slate-800 bg-slate-900 p-3 sm:flex-row">
      <div className="relative w-full flex-shrink-0 sm:w-44">
        <video
          src={api.clipUrl(clip.video_url)}
          poster={clip.thumbnail_url ? api.clipUrl(clip.thumbnail_url) : undefined}
          controls
          preload="metadata"
          className="aspect-[9/16] w-full rounded-lg bg-black object-contain"
        />
        <span
          className={`absolute left-2 top-2 rounded-full px-2 py-0.5 text-xs font-bold text-white ${scoreColor(
            clip.score
          )}`}
          title="Virality score"
        >
          🔥 {Math.round(clip.score)}
        </span>
        <div className="mt-1 text-center text-xs text-slate-500">
          {formatDuration(clip.duration)} · {clip.start}s–{clip.end}s
        </div>
        <a
          href={api.downloadUrl(jobId, clip.filename)}
          className="mt-2 block rounded-lg bg-emerald-600 py-1.5 text-center text-xs font-semibold text-white transition hover:bg-emerald-500"
        >
          Download video + metadata
        </a>
        <a
          href={api.videoDownloadUrl(jobId, clip.filename)}
          className="mt-1 block text-center text-xs text-slate-500 hover:text-slate-300"
        >
          Video only
        </a>
      </div>

      <div className="flex-1 space-y-3">
        {clip.reason && <p className="text-xs italic text-slate-500">“{clip.reason}”</p>}

        {clip.effects_applied?.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {clip.effects_applied.map((fx) => (
              <span
                key={fx}
                className="rounded-full bg-slate-800 px-2 py-0.5 text-[10px] font-medium text-slate-300"
              >
                {fx.replace(/[:_]/g, " ")}
              </span>
            ))}
          </div>
        )}

        <Field
          label="Title"
          onRegenerate={llmAvailable ? regenerate("title") : null}
          busy={busyField === "title"}
        >
          <input className={inputClass} value={form.title} onChange={update("title")} />
        </Field>

        {clip.title_alternatives?.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {clip.title_alternatives.map((alternative, index) => (
              <button
                key={index}
                type="button"
                onClick={() => applyAlternative(alternative)}
                className="rounded-full border border-slate-700 px-2 py-0.5 text-xs text-slate-300 hover:border-brand-accent hover:text-white"
                title="Use this title"
              >
                {alternative}
              </button>
            ))}
          </div>
        )}

        <Field
          label="Description"
          onRegenerate={llmAvailable ? regenerate("description") : null}
          busy={busyField === "description"}
        >
          <textarea
            className={`${inputClass} h-16 resize-y`}
            value={form.description}
            onChange={update("description")}
          />
        </Field>

        <Field
          label="Hashtags"
          onRegenerate={llmAvailable ? regenerate("hashtags") : null}
          busy={busyField === "hashtags"}
        >
          <input
            className={inputClass}
            value={form.hashtags}
            onChange={update("hashtags")}
            placeholder="#one #two #three"
          />
        </Field>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field
            label="Hook text"
            onRegenerate={llmAvailable ? regenerate("hook_text") : null}
            busy={busyField === "hook_text"}
          >
            <input
              className={inputClass}
              value={form.hook_text}
              onChange={update("hook_text")}
            />
          </Field>
          <Field
            label="Thumbnail text"
            onRegenerate={llmAvailable ? regenerate("thumbnail_text") : null}
            busy={busyField === "thumbnail_text"}
          >
            <input
              className={inputClass}
              value={form.thumbnail_text}
              onChange={update("thumbnail_text")}
            />
          </Field>
        </div>

        <Field
          label="Call to action"
          onRegenerate={llmAvailable ? regenerate("cta") : null}
          busy={busyField === "cta"}
        >
          <input className={inputClass} value={form.cta} onChange={update("cta")} />
        </Field>

        {clip.mentions?.length > 0 && (
          <div className="text-xs text-slate-400">
            Mentions: {clip.mentions.join(" ")}
          </div>
        )}

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={save}
            disabled={!dirty || saving}
            className="rounded-lg bg-brand px-4 py-1.5 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-40"
          >
            {saving ? "Saving…" : dirty ? "Save changes" : "Saved"}
          </button>
          {!llmAvailable && (
            <span className="text-xs text-slate-500">
              Set an LLM key to enable regenerate
            </span>
          )}
        </div>

        <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
              Publish this clip
            </span>
            <span className="text-xs text-slate-500">
              {publishing?.schedule
                ? `Scheduled ${new Date(publishing.schedule).toLocaleString()}`
                : "Post now"}
            </span>
          </div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(publisherStatuses || {}).map(([platform, status]) => (
              <label
                key={platform}
                className={`rounded-full border px-2 py-1 text-xs ${
                  selectedPlatforms.includes(platform)
                    ? "border-brand-accent text-white"
                    : "border-slate-700 text-slate-500"
                } ${!status.configured ? "opacity-40" : ""}`}
                title={status.message}
              >
                <input
                  type="checkbox"
                  className="mr-1"
                  checked={selectedPlatforms.includes(platform)}
                  onChange={() => togglePlatform(platform)}
                  disabled={!status.configured}
                />
                {PLATFORM_LABELS[platform] || platform}
              </label>
            ))}
          </div>
          <button
            type="button"
            onClick={publish}
            disabled={publishingNow || dirty || selectedPlatforms.length === 0}
            className="mt-3 rounded-lg bg-violet-600 px-4 py-1.5 text-sm font-semibold text-white hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {publishingNow
              ? "Queueing…"
              : publishing?.schedule
                ? "Schedule selected"
                : publishing?.mode === "review"
                  ? "Upload for review"
                  : "Publish selected"}
          </button>

          {sortedAttempts.length > 0 && (
            <div className="mt-3 space-y-1 border-t border-slate-800 pt-2">
              {sortedAttempts.slice(0, 8).map((attempt) => (
                <div key={attempt.id} className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="capitalize text-slate-300">{attempt.platform}</span>
                  <span className={ATTEMPT_COLORS[attempt.state] || "text-slate-400"}>
                    {attempt.state.replaceAll("_", " ")}
                  </span>
                  {attempt.error && <span className="text-rose-400">{attempt.error}</span>}
                  {!attempt.error && attempt.message && (
                    <span className="text-slate-500">{attempt.message}</span>
                  )}
                  {attempt.url && (
                    <a
                      href={attempt.url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-brand-accent hover:underline"
                    >
                      Open
                    </a>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {error && <div className="text-xs text-rose-400">{error}</div>}
      </div>
    </div>
  );
}
