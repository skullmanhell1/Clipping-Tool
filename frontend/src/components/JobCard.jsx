import { useState } from "react";
import ClipCard from "./ClipCard.jsx";
import { api, formatDuration } from "../api.js";

const STATUS_STYLES = {
  queued: "bg-slate-700 text-slate-200",
  processing: "bg-amber-500/20 text-amber-300",
  completed: "bg-emerald-500/20 text-emerald-300",
  failed: "bg-rose-500/20 text-rose-300",
  // I4: cancelled is styled distinctly from failed, not merged with it. A job the user stopped
  // did not go wrong, and colouring it as an error tells them it did.
  cancelled: "bg-slate-600/40 text-slate-300",
};

// U10: the raw exception text a job fails with is written for a log, not a person. These map the
// causes that are actually actionable onto what to do about them. Anything unrecognised falls
// through to the original message rather than being replaced by something vaguer — a message we
// cannot interpret is still the best evidence available.
const ERROR_HINTS = [
  [/source not found|no such file/i, "The source file is missing. It may have been cleaned up by the retention sweeper — try uploading it again."],
  [/binary not found|ffmpeg/i, "ffmpeg could not be run. Check that it is installed and on PATH on the server."],
  [/timed out/i, "A processing step exceeded its time limit. A very long source, or a slow host — try a shorter range with the Process from/to settings."],
  [/no video stream/i, "No video track was found in this file. Audio-only sources cannot be clipped."],
  [/unsupported|invalid data|moov atom/i, "This file could not be decoded. It may be corrupt or in a container ffmpeg cannot read."],
  [/disk|no space/i, "The server ran out of disk space. Clear old clips from the Storage panel."],
  [/network|connection|resolve/i, "A network request failed. If this was a URL, the site may be blocking downloads."],
];

function errorHint(message) {
  const text = String(message || "");
  for (const [pattern, hint] of ERROR_HINTS) {
    if (pattern.test(text)) return hint;
  }
  return null;
}

export default function JobCard({
  job,
  llmAvailable,
  publishing,
  publisherStatuses,
  publishAttempts,
  onClipUpdated,
  onPublished,
}) {
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState("");
  const [timings, setTimings] = useState(null);

  const percentage = Math.round((job.progress || 0) * 100);
  const badge = STATUS_STYLES[job.status] || STATUS_STYLES.queued;
  const inFlight = job.status === "processing" || job.status === "queued";
  const hint = job.status === "failed" ? errorHint(job.error) : null;

  const cancel = async () => {
    setCancelling(true);
    setCancelError("");
    try {
      await api.cancelJob(job.id);
    } catch (err) {
      setCancelError(err?.message || "Could not cancel this job.");
    } finally {
      setCancelling(false);
    }
  };

  const loadTimings = async () => {
    try {
      setTimings(await api.jobTimings(job.id));
    } catch {
      setTimings({ stages: [] });
    }
  };

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate font-medium text-slate-100">
            {job.title || job.source}
          </div>
          <div className="truncate text-xs text-slate-500">
            {job.input_type === "url" ? job.source : "Uploaded file"}
            {job.duration ? ` · ${formatDuration(job.duration)}` : ""}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {inFlight && (
            <button
              type="button"
              disabled={cancelling}
              onClick={cancel}
              title="Stop this job. A processing step already underway will finish first."
              className="rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50"
            >
              {cancelling ? "Stopping…" : "Cancel"}
            </button>
          )}
          <span
            className={`whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-medium ${badge}`}
          >
            {job.status}
          </span>
        </div>
      </div>

      {inFlight && (
        <div className="mt-4">
          <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
            <div
              className="h-full rounded-full bg-gradient-to-r from-brand-accent to-brand transition-all duration-500"
              style={{ width: `${percentage}%` }}
            />
          </div>
          <div className="mt-2 flex justify-between text-xs text-slate-400">
            <span>
              {/* U8: which step of how many, so a long stage reads as progress rather than as
                  a stalled bar. Only shown when the stage was recognised — a wrong step number
                  is worse than none. */}
              {job.stage_index > 0 && job.stage_total > 0 && (
                <span className="mr-2 text-slate-500">
                  Step {job.stage_index}/{job.stage_total}
                </span>
              )}
              {job.stage}
            </span>
            <span>{percentage}%</span>
          </div>
        </div>
      )}

      {cancelError && (
        <div className="mt-3 rounded-lg border border-amber-800 bg-amber-950/30 p-2 text-xs text-amber-300">
          {cancelError}
        </div>
      )}

      {job.status === "cancelled" && (
        <div className="mt-3 rounded-lg border border-slate-700 bg-slate-800/40 p-3 text-sm text-slate-300">
          Stopped before finishing. Any clips already written are kept.
        </div>
      )}

      {job.status === "failed" && (
        <div className="mt-3 rounded-lg border border-rose-800 bg-rose-950/40 p-3 text-sm text-rose-300">
          {/* U10: the hint comes first because it is what the reader can act on; the original
              message stays below it, because it is the evidence and we may have guessed wrong. */}
          {hint && <p className="mb-2 font-medium">{hint}</p>}
          <p className={hint ? "text-xs text-rose-400/80" : ""}>
            {job.error || "Processing failed."}
          </p>
          {(job.stage_timings || []).length > 0 && (
            <p className="mt-2 text-xs text-rose-400/70">
              Failed during: {job.stage || "an unknown stage"}
            </p>
          )}
        </div>
      )}

      {job.status === "completed" && (
        <div className="mt-4">
          {job.clips.length === 0 ? (
            // U10: an empty result is not an error, and it has specific likely causes. Saying
            // only "no clips" leaves the user with nothing to change.
            <div className="rounded-lg border border-slate-700 bg-slate-800/40 p-4 text-sm text-slate-300">
              <p className="font-medium">No clips were generated.</p>
              <p className="mt-1 text-xs text-slate-400">
                Usually one of: the source is shorter than the clip length you asked for, it
                contains no detectable speech, or the Process from/to range excluded everything.
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
              {job.clips.map((clip) => (
                <ClipCard
                  key={clip.id}
                  jobId={job.id}
                  clip={clip}
                  llmAvailable={llmAvailable}
                  publishing={publishing}
                  publisherStatuses={publisherStatuses}
                  attempts={(publishAttempts || []).filter(
                    (attempt) =>
                      attempt.job_id === job.id && attempt.clip_id === clip.id
                  )}
                  onUpdated={(updated) => onClipUpdated?.(job.id, updated)}
                  onPublished={onPublished}
                />
              ))}
            </div>
          )}

          {/* M5: the timing breakdown is behind a click rather than always shown. It answers a
              question most users never ask, and putting it inline would compete with the clips,
              which are what they came for. */}
          <div className="mt-3">
            {timings === null ? (
              <button
                type="button"
                onClick={loadTimings}
                className="text-xs text-slate-500 underline hover:text-slate-300"
              >
                Show render timings
              </button>
            ) : (
              <div className="rounded-lg border border-slate-800 bg-slate-900 p-3 text-xs">
                <div className="mb-1 text-slate-400">
                  Render timings — {(timings.total_seconds || 0).toFixed(1)}s total
                </div>
                {(timings.stages || []).length === 0 ? (
                  <div className="text-slate-500">No timings were recorded.</div>
                ) : (
                  <table className="w-full text-left">
                    <tbody>
                      {timings.stages.map((s) => (
                        <tr key={s.stage}>
                          <td className="py-0.5 pr-3 text-slate-300">{s.stage}</td>
                          <td className="py-0.5 pr-3 text-slate-400">
                            {(s.seconds || 0).toFixed(1)}s
                          </td>
                          <td className="py-0.5 text-slate-500">
                            {s.count > 1 ? `${s.count}× · ${(s.mean_seconds || 0).toFixed(1)}s each` : ""}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
