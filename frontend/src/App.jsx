import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import { POLL_INTERVALS_MS, usePolling } from "./hooks/usePolling.js";
import { DEFAULT_PUBLISHING, DEFAULT_SETTINGS, toApiOptions } from "./settingsSchema.js";
import HistoryView from "./components/HistoryView.jsx";
import InputBar from "./components/InputBar.jsx";
import JobCard from "./components/JobCard.jsx";
import PreviewCard from "./components/PreviewCard.jsx";
import ProfilesBar from "./components/ProfilesBar.jsx";
import PublishingPanel from "./components/PublishingPanel.jsx";
import ScheduleCalendar from "./components/ScheduleCalendar.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";
import StorageSettings from "./components/StorageSettings.jsx";

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
  const [version, setVersion] = useState("");
  const [effects, setEffects] = useState(null);
  const [engines, setEngines] = useState([]);
  // /api/info's `capabilities` block: probe results keyed by `<kind>:<name>`
  // plus engine-specific option domains keyed by Engine_Id (e.g.
  // capabilities.kinetic_typography.styles / .reveal_modes).
  const [capabilities, setCapabilities] = useState(null);
  const [updateInfo, setUpdateInfo] = useState(null);
  const [profiles, setProfiles] = useState([]);
  const [defaultProfileId, setDefaultProfileId] = useState(null);
  const [activeProfileId, setActiveProfileId] = useState("");
  const defaultAppliedRef = useRef(false);

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

  const applyProfile = useCallback(
    (id) => {
      setActiveProfileId(id);
      if (!id) return;
      const profile = profiles.find((p) => p.id === id);
      if (!profile) return;
      setSettings({ ...DEFAULT_SETTINGS, ...(profile.settings || {}) });
      setPublishing({ ...DEFAULT_PUBLISHING, ...(profile.publishing || {}) });
    },
    [profiles],
  );

  const loadProfiles = useCallback(async () => {
    try {
      const data = await api.profiles();
      setProfiles(data.profiles || []);
      setDefaultProfileId(data.default_id || null);
      return data;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    api
      .watchStatus()
      .then(setWatch)
      .catch(() => {});
    api
      .info()
      .then((info) => {
        setLlmAvailable(!!info.llm_available);
        setVersion(info.version || "");
        setEffects(info.effects || null);
        setEngines(Array.isArray(info.engines) ? info.engines : []);
        setCapabilities(info.capabilities || null);
      })
      .catch(() => {});
    api
      .updates()
      .then(setUpdateInfo)
      .catch(() => {});
    loadPublishingData();
    // Load profiles and pre-fill from the default profile once on startup.
    loadProfiles().then((data) => {
      if (data && data.default_id && !defaultAppliedRef.current) {
        defaultAppliedRef.current = true;
        const profile = (data.profiles || []).find((p) => p.id === data.default_id);
        if (profile) {
          setActiveProfileId(profile.id);
          setSettings({ ...DEFAULT_SETTINGS, ...(profile.settings || {}) });
          setPublishing({ ...DEFAULT_PUBLISHING, ...(profile.publishing || {}) });
        }
      }
    });
  }, [loadPublishingData, loadProfiles]);

  const handleSaveProfile = useCallback(
    async (name, id) => {
      const saved = await api.saveProfile({ name, id, settings, publishing });
      await loadProfiles();
      setActiveProfileId(saved.id);
    },
    [settings, publishing, loadProfiles],
  );

  const handleSetDefaultProfile = useCallback(
    async (id) => {
      await api.setDefaultProfile(id);
      await loadProfiles();
    },
    [loadProfiles],
  );

  const handleDeleteProfile = useCallback(
    async (id) => {
      await api.deleteProfile(id);
      if (id === activeProfileId) setActiveProfileId("");
      await loadProfiles();
    },
    [activeProfileId, loadProfiles],
  );

  const handleClipUpdated = useCallback((jobId, updatedClip) => {
    setJobs((previous) =>
      previous.map((job) =>
        job.id === jobId
          ? {
              ...job,
              clips: job.clips.map((clip) => (clip.id === updatedClip.id ? updatedClip : clip)),
            }
          : job,
      ),
    );
  }, []);

  const poll = useCallback(async () => {
    try {
      const [{ jobs: allJobs }, history] = await Promise.all([api.listJobs(), api.history()]);
      setJobs(allJobs);
      setPublishAttempts(history.publish_attempts || []);
    } catch {
      // Keep the last known state through transient API failures.
    }
  }, []);

  // I11: derived outside the effect so the effect can depend on a *boolean* rather than on the
  // jobs array. Depending on `jobs` directly would tear down and rebuild the interval on every
  // poll, since each response is a new array; depending on `jobs.length` — the previous
  // suppression — silently missed the case that matters, because a job going from processing to
  // completed does not change the count, so the fast 1.2s poll continued indefinitely after
  // everything had finished. This fixes that as well as the warning.
  const hasActiveJobs = useMemo(
    () => jobs.some((job) => ["queued", "processing"].includes(job.status)),
    [jobs],
  );

  // Nothing to ask about means no requests at all, not one: `usePolling` skips the initial
  // call too when the interval is null.
  const pollInterval = useMemo(() => {
    if (trackedIds.size === 0 && !watch.enabled) return null;
    return watch.enabled || hasActiveJobs
      ? POLL_INTERVALS_MS.jobsActive
      : POLL_INTERVALS_MS.jobsIdle;
  }, [trackedIds, watch.enabled, hasActiveJobs]);

  usePolling(poll, pollInterval);

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
    const options = toApiOptions(settings, publishing);
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
      setWatch(await api.watchToggle(enabled, toApiOptions(settings, publishing)));
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
    (job) => trackedIds.has(job.id) || job.source?.includes("/watch/"),
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
              {version && <span className="ml-2 text-xs text-slate-600">v{version}</span>}
            </p>
          </div>
          <nav className="flex rounded-xl border border-slate-800 bg-slate-900 p-1">
            {[
              ["create", "Create"],
              ["schedule", "Schedule"],
              ["history", "History"],
              ["settings", "Settings"],
            ].map(([id, label]) => (
              <button
                key={id}
                type="button"
                onClick={() => setActiveView(id)}
                className={`rounded-lg px-4 py-2 text-sm font-medium transition ${
                  activeView === id ? "bg-brand text-white" : "text-slate-400 hover:text-white"
                }`}
              >
                {label}
              </button>
            ))}
          </nav>
        </header>

        {updateInfo?.update_available && (
          <div className="mb-6 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-700/60 bg-amber-950/40 p-3 text-sm text-amber-200">
            <span>
              🎉 Update available — v{updateInfo.latest} is out (you're on v{updateInfo.current}).
              Update with{" "}
              <code className="rounded bg-slate-900 px-1">
                git pull &amp;&amp; docker compose up --build
              </code>
              .
            </span>
            <a
              href={updateInfo.html_url}
              target="_blank"
              rel="noreferrer"
              className="rounded-lg border border-amber-600 px-3 py-1 font-medium hover:bg-amber-900/40"
            >
              Release notes
            </a>
          </div>
        )}

        {/* PB7: the schedule was previously a single datetime input with no way to
            see, move or cancel what had been scheduled. */}
        {activeView === "schedule" && <ScheduleCalendar onError={setError} />}

        {activeView === "history" && <HistoryView />}

        {activeView === "settings" && (
          <div className="space-y-6">
            <StorageSettings />
            <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5 text-sm text-slate-400">
              <div className="mb-1 font-semibold text-slate-300">Version &amp; updates</div>
              <p>
                Running <span className="text-slate-200">v{version || "?"}</span>
                {updateInfo?.latest ? ` · latest v${updateInfo.latest}` : ""}
                {updateInfo && !updateInfo.update_available && updateInfo.latest
                  ? " · up to date ✓"
                  : ""}
              </p>
              <p className="mt-1 text-xs text-slate-500">
                Update the app with{" "}
                <code className="rounded bg-slate-950 px-1">
                  git pull &amp;&amp; docker compose up --build
                </code>
                .
              </p>
            </div>
          </div>
        )}

        {activeView === "create" && (
          <>
            <div className="space-y-4">
              <ProfilesBar
                profiles={profiles}
                defaultId={defaultProfileId}
                activeId={activeProfileId}
                onApply={applyProfile}
                onSave={handleSaveProfile}
                onSetDefault={handleSetDefaultProfile}
                onDelete={handleDeleteProfile}
              />
              <InputBar onChange={setInput} onPreview={handlePreview} />
              {input.files.length === 0 && (
                <PreviewCard preview={preview} loading={previewLoading} />
              )}
              <SettingsPanel
                settings={settings}
                onChange={setSettings}
                watch={watch}
                onToggleWatch={handleToggleWatch}
                effects={effects}
                engines={engines}
                capabilities={capabilities}
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
                    // U7: a re-render applies whatever is selected in the panel right now, which
                    // is what makes "change one setting and see it" a single click.
                    settings={toApiOptions(settings, publishing)}
                  />
                ))}
              </section>
            )}
          </>
        )}

        <footer className="mt-12 border-t border-slate-800 pt-6 text-xs text-slate-500">
          You are responsible for holding the rights to any source footage you process. See the
          README for content and copyright guidance.
        </footer>
      </div>
    </div>
  );
}
