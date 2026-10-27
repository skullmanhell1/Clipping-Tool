import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

const PLATFORM_OPTIONS = [
  {
    id: "tiktok",
    statusKey: "tiktok",
    label: "TikTok",
    detail: "9:16 · hook-first · up to 5 min",
  },
  {
    id: "youtube_shorts",
    statusKey: "youtube",
    label: "YouTube Shorts",
    detail: "9:16 · Shorts metadata · up to 3 min",
  },
  {
    id: "instagram",
    statusKey: "instagram",
    label: "Instagram Reels",
    detail: "9:16 · caption-first · up to 90 sec",
  },
];

const toLocalInput = (epoch) => {
  const date = new Date(epoch * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
};

const toEpoch = (value) => {
  if (!value) return null;
  const epoch = new Date(value).getTime() / 1000;
  return Number.isFinite(epoch) ? epoch : null;
};

const formatDuration = (seconds) => {
  const value = Number(seconds || 0);
  if (!Number.isFinite(value) || value <= 0) return "—";
  const minutes = Math.floor(value / 60);
  const remainder = Math.round(value % 60);
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
};

export default function MultiPlatformScheduler({
  jobs = [],
  campaigns = [],
  publisherStatuses = {},
  outputProfiles = [],
  campaignPreset = null,
  onError,
  onPublished,
}) {
  const completedClips = useMemo(
    () =>
      jobs.flatMap((job) =>
        job.status === "completed"
          ? (job.clips || []).map((clip) => ({ job, clip }))
          : [],
      ),
    [jobs],
  );
  const [selection, setSelection] = useState({ jobId: "", clipId: "" });
  const [selectedPlatforms, setSelectedPlatforms] = useState([]);
  const [campaignId, setCampaignId] = useState("");
  const [schedule, setSchedule] = useState("");
  const [suggestions, setSuggestions] = useState([]);
  const [suggestionBasis, setSuggestionBasis] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!completedClips.length) {
      setSelection({ jobId: "", clipId: "" });
      return;
    }
    const stillAvailable = completedClips.some(
      ({ job, clip }) => job.id === selection.jobId && clip.id === selection.clipId,
    );
    if (!stillAvailable) {
      const first = completedClips[0];
      setSelection({ jobId: first.job.id, clipId: first.clip.id });
    }
  }, [completedClips, selection.clipId, selection.jobId]);

  const selectedRecord = completedClips.find(
    ({ job, clip }) => job.id === selection.jobId && clip.id === selection.clipId,
  );
  const selectedCampaign = campaigns.find((campaign) => campaign.id === campaignId);
  const allowedPlatforms = Array.isArray(campaignPreset?.posting?.platforms) && campaignPreset.posting.platforms.length
    ? campaignPreset.posting.platforms
    : Array.isArray(campaignPreset?.platforms)
      ? campaignPreset.platforms
      : [];
  const manualOnly = !!(campaignPreset?.posting?.manual_only || campaignPreset?.posting?.review_only);
  const platformAllowed = (platform) =>
    !allowedPlatforms.length || allowedPlatforms.includes(platform) ||
    (platform === "youtube_shorts" && allowedPlatforms.includes("youtube"));

  useEffect(() => {
    if (!allowedPlatforms.length) return;
    setSelectedPlatforms((current) => current.filter(platformAllowed));
  }, [campaignPreset?.id, allowedPlatforms.join(",")]);

  const selectedProfiles = useMemo(
    () =>
      selectedPlatforms
        .map((platform) => outputProfiles.find((profile) => profile.name === platform))
        .filter(Boolean),
    [outputProfiles, selectedPlatforms],
  );

  const durationBlocker = selectedRecord
    ? selectedPlatforms.find((platform) => {
        const profile = outputProfiles.find((entry) => entry.name === platform);
        return profile?.max_duration_s && selectedRecord.clip.duration > profile.max_duration_s;
      })
    : "";

  const statusFor = (platform) => {
    const option = PLATFORM_OPTIONS.find((entry) => entry.id === platform);
    return publisherStatuses[option?.statusKey || platform] || {};
  };

  const togglePlatform = (platform) => {
    if (!platformAllowed(platform)) return;
    setSelectedPlatforms((current) =>
      current.includes(platform)
        ? current.filter((item) => item !== platform)
        : [...current, platform],
    );
    setMessage("");
    setError("");
  };

  const loadSuggestions = async () => {
    const platform = selectedPlatforms[0] || "tiktok";
    setError("");
    try {
      const result = await api.scheduleSuggestions(platform, 7, 2);
      setSuggestions(result.suggestions || []);
      setSuggestionBasis(result.basis || "");
    } catch (suggestionError) {
      const text = suggestionError.message || "Could not load suggested times.";
      setError(text);
      onError?.(text);
    }
  };

  const publish = async () => {
    setMessage("");
    setError("");
    if (!selectedRecord) {
      setError("Choose a completed clip first.");
      return;
    }
    if (!selectedPlatforms.length) {
      setError("Choose at least one platform.");
      return;
    }
    if (manualOnly) {
      setError("The active campaign requires human review. Approve and publish from the clip review card instead.");
      return;
    }
    if (selectedPlatforms.some((platform) => !platformAllowed(platform))) {
      setError("The active campaign preset does not allow one or more selected platforms.");
      return;
    }
    if (durationBlocker) {
      const label = PLATFORM_OPTIONS.find((entry) => entry.id === durationBlocker)?.label;
      setError(`This clip is longer than the ${label} limit. Re-render a shorter version first.`);
      return;
    }
    const scheduleAt = toEpoch(schedule);
    if (schedule && (!scheduleAt || scheduleAt <= Date.now() / 1000 - 60)) {
      setError("Choose a future schedule time, or clear the field to publish now.");
      return;
    }

    const routes = Object.fromEntries(
      Object.entries(selectedCampaign?.routes || {}).map(([platform, route]) => [
        platform,
        { ...route },
      ]),
    );
    // `youtube_shorts` is a product-level alias for the YouTube publisher. Copying the saved
    // YouTube route under the alias lets the API validate the account before the manager resolves
    // the alias and still preserves Shorts-specific metadata and preflight limits.
    if (routes.youtube && !routes.youtube_shorts) {
      routes.youtube_shorts = { ...routes.youtube };
    }
    if (routes.youtube_shorts && !routes.youtube) {
      routes.youtube = { ...routes.youtube_shorts };
    }

    setBusy(true);
    try {
      const result = await api.publishClip(selectedRecord.job.id, selectedRecord.clip.id, {
        platforms: selectedPlatforms,
        campaign_id: campaignId,
        mode: "auto",
        schedule_at: scheduleAt,
        routes,
      });
      onPublished?.(result.attempts || []);
      setMessage(
        scheduleAt
          ? `${selectedPlatforms.length} platform post${selectedPlatforms.length === 1 ? "" : "s"} scheduled.`
          : `${selectedPlatforms.length} platform post${selectedPlatforms.length === 1 ? "" : "s"} queued now.`,
      );
      setSchedule("");
    } catch (publishError) {
      const text = publishError.message || "Could not queue the selected platforms.";
      setError(text);
      onError?.(text);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="rounded-2xl border border-violet-400/20 bg-violet-400/[0.04] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-violet-300">
            Direct scheduler
          </p>
          <h3 className="mt-1 text-lg font-semibold text-white">Push one clip everywhere</h3>
          <p className="mt-1 max-w-2xl text-xs leading-5 text-slate-400">
            Choose a rendered clip, apply platform-aware metadata and limits, then publish now or
            queue TikTok, YouTube Shorts, and Instagram Reels together.
          </p>
        </div>
        <span className="rounded-full border border-violet-300/20 px-2 py-1 text-[10px] text-violet-200">
          {manualOnly ? "Manual review required" : "Direct / auto mode"}
        </span>
      </div>

      {!completedClips.length ? (
        <div className="mt-4 rounded-xl border border-slate-800 bg-slate-950/50 p-3 text-xs text-slate-500">
          Complete a clip job first. Finished clips will appear here for instant or scheduled
          publishing.
        </div>
      ) : (
        <div className="mt-4 space-y-4">
          <div className="grid gap-3 lg:grid-cols-[minmax(0,1.4fr)_minmax(220px,0.8fr)]">
            <label className="text-xs text-slate-400">
              Clip
              <select
                value={`${selection.jobId}:${selection.clipId}`}
                onChange={(event) => {
                  const [jobId, clipId] = event.target.value.split(":");
                  setSelection({ jobId, clipId });
                  setMessage("");
                  setError("");
                }}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 p-2 text-sm text-slate-100"
              >
                {completedClips.map(({ job, clip }) => (
                  <option key={`${job.id}:${clip.id}`} value={`${job.id}:${clip.id}`}>
                    {(job.title || job.source || "Project").slice(0, 48)} · {clip.title || clip.filename} · {formatDuration(clip.duration)}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs text-slate-400">
              Campaign / routing
              <select
                value={campaignId}
                onChange={(event) => setCampaignId(event.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 p-2 text-sm text-slate-100"
              >
                <option value="">Use platform defaults</option>
                {campaigns.map((campaign) => (
                  <option key={campaign.id} value={campaign.id}>
                    {campaign.name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div>
             <div className="mb-2 flex items-center justify-between gap-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                Platforms
              </span>
               <span className="text-[10px] text-slate-500">{allowedPlatforms.length ? `Campaign allows ${allowedPlatforms.join(", ")}` : "Metadata is adapted per destination"}</span>
            </div>
            <div className="grid gap-2 md:grid-cols-3">
              {PLATFORM_OPTIONS.map((option) => {
                const status = statusFor(option.id);
                const selected = selectedPlatforms.includes(option.id);
                const configured = status.configured === true;
                return (
                  <label
                    key={option.id}
                    className={`rounded-xl border p-3 transition ${
                      selected
                        ? "border-violet-300/50 bg-violet-400/[0.08]"
                        : "border-slate-800 bg-slate-950/40"
                    } ${configured ? "" : "opacity-60"}`}
                  >
                    <div className="flex items-start gap-2">
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={() => togglePlatform(option.id)}
                        disabled={!configured || !platformAllowed(option.id)}
                        className="mt-0.5 accent-violet-400"
                      />
                      <span className="min-w-0">
                        <span className="block text-sm font-semibold text-slate-100">{option.label}</span>
                        <span className="mt-1 block text-[10px] text-slate-500">{option.detail}</span>
                        <span
                          className={`mt-2 block text-[10px] ${
                            status.direct_publish ? "text-emerald-300" : configured ? "text-amber-300" : "text-rose-300"
                          }`}
                        >
                          {!configured ? "Not configured" : status.direct_publish ? "Direct ready" : "Review / limited"}
                        </span>
                      </span>
                    </div>
                  </label>
                );
              })}
            </div>
          </div>

          {selectedProfiles.length > 0 ? (
            <div className="flex flex-wrap gap-2 text-[10px] text-slate-400">
              {selectedProfiles.map((profile) => (
                <span key={profile.name} className="rounded-full border border-slate-700 px-2 py-1">
                  {profile.label}: {profile.width}×{profile.height} · max {profile.max_duration_s || "—"}s
                </span>
              ))}
            </div>
          ) : null}

          {selectedRecord ? (
            <p className="text-[10px] text-slate-500">
              Selected clip: {formatDuration(selectedRecord.clip.duration)}. A non-vertical source
              can still publish, but re-rendering with the matching output profile is recommended.
            </p>
          ) : null}

          <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
            <label className="text-xs text-slate-400">
              Schedule time (leave empty to push now)
              <input
                type="datetime-local"
                value={schedule}
                onChange={(event) => setSchedule(event.target.value)}
                className="mt-1 block w-full rounded-lg border border-slate-700 bg-slate-950 p-2 text-sm text-slate-100"
              />
            </label>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => setSchedule("")}
                className={`rounded-lg border px-3 py-2 text-xs font-semibold ${
                  !schedule ? "border-emerald-400/50 text-emerald-200" : "border-slate-700 text-slate-400"
                }`}
              >
                Push now
              </button>
              <button
                type="button"
                onClick={loadSuggestions}
                className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:border-violet-300/50"
              >
                Suggest times
              </button>
               <button
                type="button"
                onClick={publish}
                disabled={busy || manualOnly || !selectedPlatforms.length || Boolean(durationBlocker)}
                className="rounded-lg bg-violet-600 px-4 py-2 text-xs font-semibold text-white hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? "Queueing…" : manualOnly ? "Manual review required" : schedule ? "Schedule selected" : "Publish selected"}
              </button>
            </div>
          </div>

          {suggestions.length ? (
            <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs font-semibold text-slate-200">Suggested slots</span>
                <span className="text-[10px] text-slate-500">{suggestionBasis}</span>
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {suggestions.slice(0, 6).map((suggestion) => (
                  <button
                    key={suggestion.at}
                    type="button"
                    onClick={() => setSchedule(toLocalInput(suggestion.at))}
                    className="rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:border-violet-300/50"
                  >
                    {new Date(suggestion.at * 1000).toLocaleString([], {
                      weekday: "short",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {durationBlocker ? (
            <p className="rounded-lg border border-rose-400/20 bg-rose-400/[0.06] p-2 text-xs text-rose-200">
              This clip exceeds the selected platform&apos;s duration limit. Re-render it with the
              matching short-form profile before scheduling.
            </p>
          ) : null}
          {message ? <p className="text-xs text-emerald-300">{message}</p> : null}
          {error ? <p className="text-xs text-rose-300">{error}</p> : null}
        </div>
      )}
    </section>
  );
}
