import React, { useCallback, useEffect, useRef, useState } from "react";
import { api, resolveLanguage } from "./api.js";
import HistoryView from "./components/HistoryView.jsx";
import InputBar from "./components/InputBar.jsx";
import JobCard from "./components/JobCard.jsx";
import PreviewCard from "./components/PreviewCard.jsx";
import PublishingPanel from "./components/PublishingPanel.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";

const DEFAULT_SETTINGS = {
  language: "auto",
  clip_length: "auto",
  aspect: "9:16",
  num_clips: "auto",
  strategy: "ai",
  captions: true,
  topic: "",
  vibe: "",
  platform: "generic",
  hashtag_count: 5,
  range_start: "",
  range_end: "",
  metadata: true,
};

const DEFAULT_PUBLISHING = {
  platforms: [],
  campaign_id: "",
  mode: "review",
  schedule: "",
  account_id: "",
  target_type: "",
  target_id: "",
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
    topic: settings.topic,
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
  };
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
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [watch, setWatch] = useState({ enabled: false, folder: "" });
  const [llmAvailable, setLlmAvailable] = useState(false);
  const pollRef = useRef(null);

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

  useEffect(() => {
    api.watchStatus().then(setWatch).catch(() => {});
    api.info().then((info) => setLlmAvailable(!!info.llm_available)).catch(() => {});
    loadPublishingData();
  }, [loadPublishingData]);

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

  useEffect(() => {
    const active =
      watch.enabled || jobs.some((job) => ["queued", "processing"].includes(job.status));
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
      newJobs.forEach((job) => next.add(job.id));
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
    try {
      let created = [];
      if (files.length > 0) {
        const result = await api.upload(files, options);
        created = result.jobs || [];
      } else if (urls.length === 1) {
        created = [await api.submitUrl(urls[0], options)];
      } else {
        const result = await api.submitBatch(urls, options);
        created = result.jobs || [];
      }
      track(created);
      setJobs((previous) => [...created, ...previous]);
    } catch (submissionError) {
      setError(submissionError.message || "Failed to submit.");
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

  const handlePublished = (attempts) => {
    setPublishAttempts((previous) => [
      ...(attempts || []),
      ...previous.filter((item) => !(attempts || []).some((next) => next.id === item.id)),
    ]);
  };

  const visibleJobs = jobs.filter(
    (job) => trackedIds.has(job.id) || job.source?.includes("/watch/")
  );

  return (
    <div className="min-h-full bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-6xl px-6 py-10">
        <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="bg-gradient-to-r from-brand-accent to-brand bg-clip-text text-3xl font-bold text-transparent">
              AI Video Clipper
            </h1>
            <p className="mt-1 text-slate-400">
              Create, package, schedule, and publish short-form clips.
            </p>
          </div>
          <nav className="flex rounded-xl border border-slate-800 bg-slate-900 p-1">
            {[
              ["create", "Create"],
              ["history", "History"],
            ].map(([id, label]) => (
              <button
                key={id}
                type="button"
                onClick={() => setActiveView(id)}
                className={`rounded-lg px-4 py-2 text-sm font-medium transition ${
                  activeView === id
                    ? "bg-brand text-white"
                    : "text-slate-400 hover:text-white"
                }`}
              >
                {label}
              </button>
            ))}
          </nav>
        </header>

        {activeView === "history" ? (
          <HistoryView />
        ) : (
          <>
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
              <PublishingPanel
                value={publishing}
                onChange={setPublishing}
                statuses={publisherStatuses}
                campaigns={campaigns}
                onCampaignSaved={handleCampaignSaved}
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
                    publishing={publishing}
                    publisherStatuses={publisherStatuses}
                    publishAttempts={publishAttempts}
                    onClipUpdated={handleClipUpdated}
                    onPublished={handlePublished}
                  />
                ))}
              </section>
            )}
          </>
        )}

        <footer className="mt-12 border-t border-slate-800 pt-6 text-xs text-slate-500">
          You are responsible for holding the rights to any source footage you process.
          See the README for content and copyright guidance.
        </footer>
      </div>
    </div>
  );
}
