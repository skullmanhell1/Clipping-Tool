import React, { useState } from "react";
import { api, formatDuration } from "../api.js";

// Colour the virality score badge by strength.
function scoreColor(score) {
  if (score >= 80) return "bg-emerald-500/90";
  if (score >= 60) return "bg-lime-500/90";
  if (score >= 40) return "bg-amber-500/90";
  if (score > 0) return "bg-orange-500/90";
  return "bg-slate-600/90";
}

// A labelled field with an inline "regenerate" (↻) button.
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

/**
 * A single finished clip: video preview + virality score, plus an editable
 * metadata panel (title with alternatives, description, hashtags, hook, CTA,
 * thumbnail text). Each AI field can be regenerated individually; edits are
 * saved back to the server.
 */
export default function ClipCard({ jobId, clip, llmAvailable, onUpdated }) {
  const [form, setForm] = useState({
    title: clip.title || "",
    description: clip.description || "",
    hashtags: (clip.hashtags || []).join(" "),
    hook_text: clip.hook_text || "",
    cta: clip.cta || "",
    thumbnail_text: clip.thumbnail_text || "",
  });
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [busyField, setBusyField] = useState("");
  const [err, setErr] = useState("");

  const upd = (key) => (e) => {
    setForm({ ...form, [key]: e.target.value });
    setDirty(true);
  };

  const save = async () => {
    setSaving(true);
    setErr("");
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
    } catch (e) {
      setErr(e.message || "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const regenerate = (field) => async () => {
    setBusyField(field);
    setErr("");
    try {
      const { value } = await api.regenerateField(jobId, clip.id, field);
      if (field === "hashtags") {
        setForm((f) => ({ ...f, hashtags: (value || []).join(" ") }));
      } else {
        setForm((f) => ({ ...f, [field]: value }));
      }
      setDirty(true);
    } catch (e) {
      setErr(e.message || "Regenerate failed");
    } finally {
      setBusyField("");
    }
  };

  const useAlternative = (alt) => {
    setForm((f) => ({ ...f, title: alt }));
    setDirty(true);
  };

  const inputCls =
    "w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-brand-accent";

  return (
    <div className="flex flex-col gap-3 overflow-hidden rounded-xl border border-slate-800 bg-slate-900 p-3 sm:flex-row">
      {/* Preview + score */}
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
          Download
        </a>
      </div>

      {/* Editable metadata */}
      <div className="flex-1 space-y-3">
        {clip.reason && (
          <p className="text-xs italic text-slate-500">“{clip.reason}”</p>
        )}

        <Field label="Title" onRegenerate={llmAvailable ? regenerate("title") : null} busy={busyField === "title"}>
          <input className={inputCls} value={form.title} onChange={upd("title")} />
        </Field>

        {clip.title_alternatives?.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {clip.title_alternatives.map((alt, i) => (
              <button
                key={i}
                type="button"
                onClick={() => useAlternative(alt)}
                className="rounded-full border border-slate-700 px-2 py-0.5 text-xs text-slate-300 hover:border-brand-accent hover:text-white"
                title="Use this title"
              >
                {alt}
              </button>
            ))}
          </div>
        )}

        <Field label="Description" onRegenerate={llmAvailable ? regenerate("description") : null} busy={busyField === "description"}>
          <textarea className={`${inputCls} h-16 resize-y`} value={form.description} onChange={upd("description")} />
        </Field>

        <Field label="Hashtags" onRegenerate={llmAvailable ? regenerate("hashtags") : null} busy={busyField === "hashtags"}>
          <input className={inputCls} value={form.hashtags} onChange={upd("hashtags")} placeholder="#one #two #three" />
        </Field>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Hook text" onRegenerate={llmAvailable ? regenerate("hook_text") : null} busy={busyField === "hook_text"}>
            <input className={inputCls} value={form.hook_text} onChange={upd("hook_text")} />
          </Field>
          <Field label="Thumbnail text" onRegenerate={llmAvailable ? regenerate("thumbnail_text") : null} busy={busyField === "thumbnail_text"}>
            <input className={inputCls} value={form.thumbnail_text} onChange={upd("thumbnail_text")} />
          </Field>
        </div>

        <Field label="Call to action" onRegenerate={llmAvailable ? regenerate("cta") : null} busy={busyField === "cta"}>
          <input className={inputCls} value={form.cta} onChange={upd("cta")} />
        </Field>

        {clip.mentions?.length > 0 && (
          <div className="text-xs text-slate-400">
            Mentions: {clip.mentions.join(" ")}
          </div>
        )}

        {err && <div className="text-xs text-rose-400">{err}</div>}

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
      </div>
    </div>
  );
}
