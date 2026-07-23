import React, { useCallback, useEffect, useRef, useState } from "react";
import { api, resolveLanguage } from "./api.js";
import InputBar from "./components/InputBar.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";
import PreviewCard from "./components/PreviewCard.jsx";
import JobCard from "./components/JobCard.jsx";

const DEFAULT_SETTINGS = {
  language: "auto",
  clip_length: "auto",
  aspect: "9:16",
  num_clips: "auto",
  strategy: "ai",
  captions: true,
  // Advanced
  topic: "",
  vibe: "",
  platform: "generic",
  hashtag_count: 5,
  range_start: "",
  range_end: "",
  metadata: true,
};

const numOrNull = (v) => (v === "" || v === null || v === undefined ? null : Number(v));

// Translate UI settings into the backend ProcessingOptions shape.
function toOptions(settings) {
  const { language, translate } = resolveLanguage(settings.language);
  return {
    language,
    translate,
    clip_length: settings.clip_length,
    aspect: settings.aspect,
    num_clips: settings.num_clips,
    strategy: settings.strategy,
    captions: settings.captions,
    topic: settings.topic,
    vibe: settings.vibe,
    platform: settings.platform,
    hashtag_count: Number(settings.hashtag_count) || 0,
    range_start: numOrNull(settings.range_start),
    range_end: numOrNull(settings.range_end),
    metadata: settings.metadata,
  };
}

export default function App() {
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [input, setInput] = useState({ urls: [], files: [] });
  const [preview, setPreview] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [jobs, setJobs] = useState([]);
  const [trackedIds, setTrackedIds] = useState(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [watch, setWatch] = useState({ enabled: false, folder: "" });
  const [llmAvailable, setLlmAvailable] = useState(false);

  const pollRef = useRef(null);

  // Load watch-folder status + LLM availability once on mount.
  useEffect(() => {
    api.watchStatus().then(setWatch).catch(() => {});
    api.info().then((i) => setLlmAvailable(!!i.llm_available)).catch(() => {});
  }, []);

  // Merge a server-updated clip back into the jobs state (after edit/regen).
  const handleClipUpdated = useCallback((jobId, updatedClip) => {
    setJobs((prev) =>
      prev.map((j) =>
        j.id === jobId
          ? { ...j, clips: j.clips.map((c) => (c.id === updatedClip.id ? updatedClip : c)) }
          : j
      )
    );
  }, []);

  // Poll all jobs while any tracked job is unfinished (or watch mode is on).
  const poll = useCallback(async () => {
    try {
      const { jobs: all } = await api.listJobs();
      setJobs(all);
    } catch {
      /* transient; keep last state */
    }
  }, []);

  useEffect(() => {
    const active =
      watch.enabled ||
      jobs.some((j) => ["queued", "processing"].includes(j.status));
    if (pollRef.current) clearInterval(pollRef.current);
    if (trackedIds.size > 0 || watch.enabled) {
      poll();
      pollRef.current = setInterval(poll, active ? 1200 : 4000);
    }
    return () => pollRef.current && clearInterval(pollRef.current);
  }, [trackedIds, watch.enabled, poll, jobs.length]);

  const handlePreview = useCallback(async (url) => {
    setPreview(null);
    setPreviewLoading(true);
    try {
      const meta = await api.preview(url);
      setPreview(meta);
    } catch {
      setPreview(null);
    } finally {
      setPreviewLoading(false);
    }
  }, []);

  const track = (newJobs) =>
    setTrackedIds((prev) => {
      const next = new Set(prev);
      newJobs.forEach((j) => next.add(j.id));
      return next;
    });

  const handleGetClips = async () => {
    setError("");
    const options = toOptions(settings);
    const { urls, files } = input;

    if (files.length === 0 && urls.length === 0) {
      setError("Add a video URL or upload a file first.");
      return;
    }

    setSubmitting(true);
    try {
      let created = [];
      if (files.length > 0) {
        const res = await api.upload(files, options);
        created = res.jobs || [];
      } else if (urls.length === 1) {
        created = [await api.submitUrl(urls[0], options)];
      } else {
        const res = await api.submitBatch(urls, options);
        created = res.jobs || [];
      }
      track(created);
      setJobs((prev) => [...created, ...prev]);
    } catch (e) {
      setError(e.message || "Failed to submit.");
    } finally {
      setSubmitting(false);
    }
  };

  const handleToggleWatch = async (enabled) => {
    try {
      const status = await api.watchToggle(enabled, toOptions(settings));
      setWatch(status);
    } catch (e) {
      setError(e.message || "Watch toggle failed.");
    }
  };

  // Jobs to display: tracked ones plus any produced by watch mode.
  const visibleJobs = jobs.filter(
    (j) => trackedIds.has(j.id) || j.source?.includes("/watch/")
  );

  return (
    <div className="min-h-full bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-6xl px-6 py-10">
        <header className="mb-8">
          <h1 className="bg-gradient-to-r from-brand-accent to-brand bg-clip-text text-3xl font-bold text-transparent">
            AI Video Clipper
          </h1>
          <p className="mt-1 text-slate-400">
            Paste a link or upload video — get short, captioned, vertical clips.
          </p>
        </header>

        <div className="space-y-4">
          <InputBar onChange={setInput} onPreview={handlePreview} />
          {input.files.length === 0 && (
            <PreviewCard preview={preview} loading={previewLoading} />
          )}
          <SettingsPanel
            settings={settings}
            onChange={setSettings}
            watch={watch}
            onToggleWatch={handleToggleWatch}
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
                onClipUpdated={handleClipUpdated}
              />
            ))}
          </section>
        )}

        <footer className="mt-12 border-t border-slate-800 pt-6 text-xs text-slate-500">
          You are responsible for holding the rights to any source footage you
          process. See the README for content &amp; copyright guidance.
        </footer>
      </div>
    </div>
  );
}
