import PropTypes from "prop-types";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ClipCard from "./ClipCard.jsx";
import ReviewBar from "./ReviewBar.jsx";
import { api, formatDuration } from "../api.js";
import {
  CLIP_SHAPE,
  PUBLISHER_STATUSES_SHAPE,
  PUBLISHING_SHAPE,
  PUBLISH_ATTEMPT_SHAPE,
  WIRE_OPTIONS_SHAPE,
} from "./shapes.js";

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
  [
    /source not found|no such file/i,
    "The source file is missing. It may have been cleaned up by the retention sweeper — try uploading it again.",
  ],
  [
    /binary not found|ffmpeg/i,
    "ffmpeg could not be run. Check that it is installed and on PATH on the server.",
  ],
  [
    /timed out/i,
    "A processing step exceeded its time limit. A very long source, or a slow host — try a shorter range with the Process from/to settings.",
  ],
  [
    /no video stream/i,
    "No video track was found in this file. Audio-only sources cannot be clipped.",
  ],
  [
    /unsupported|invalid data|moov atom/i,
    "This file could not be decoded. It may be corrupt or in a container ffmpeg cannot read.",
  ],
  [/disk|no space/i, "The server ran out of disk space. Clear old clips from the Storage panel."],
  [
    /network|connection|resolve/i,
    "A network request failed. If this was a URL, the site may be blocking downloads.",
  ],
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
  settings,
}) {
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState("");
  const [timings, setTimings] = useState(null);
  // U9: which clips a batch action applies to. Scoped to this job, because that is how a review
  // pass actually happens - you work through what one source produced.
  const [selectedClips, setSelectedClips] = useState(() => new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchError, setBatchError] = useState("");
  // U11: which clip the keyboard is acting on.
  const [focusIndex, setFocusIndex] = useState(0);
  const playerRef = useRef(null);

  // Memoised because `job.clips || []` allocates a fresh array on every render, which would make
  // the keyboard effect below re-bind its window listener continuously.
  const clips = useMemo(() => job.clips || [], [job.clips]);

  const toggleSelected = useCallback((clipId) => {
    setSelectedClips((previous) => {
      const next = new Set(previous);
      if (next.has(clipId)) next.delete(clipId);
      else next.add(clipId);
      return next;
    });
  }, []);

  const applyBatch = useCallback(
    async (state) => {
      const ids = [...selectedClips];
      if (!ids.length) return;
      setBatchBusy(true);
      setBatchError("");
      try {
        const result = await api.reviewClips(job.id, ids, state);
        (result.updated || []).forEach((clip) => onClipUpdated?.(job.id, clip));
        // Cleared only on success: leaving the selection intact after a failure means the user can
        // retry without picking twenty clips again.
        setSelectedClips(new Set());
      } catch (error) {
        setBatchError(error.message || "Batch review failed.");
      } finally {
        setBatchBusy(false);
      }
    },
    [job.id, onClipUpdated, selectedClips]
  );

  const reviewFocused = useCallback(
    async (state) => {
      const clip = clips[focusIndex];
      if (!clip) return;
      const next = clip.review_state === state ? "pending" : state;
      try {
        const updated = await api.reviewClip(job.id, clip.id, next);
        onClipUpdated?.(job.id, updated);
      } catch (error) {
        setBatchError(error.message || "Could not record the review.");
      }
    },
    [clips, focusIndex, job.id, onClipUpdated]
  );

  const counts = useMemo(() => {
    const tally = { approved: 0, rejected: 0, pending: 0 };
    clips.forEach((clip) => {
      const state = clip.review_state || "pending";
      tally[state] = (tally[state] || 0) + 1;
    });
    return tally;
  }, [clips]);

  // U11: keyboard shortcuts for review.
  //
  // Reviewing twenty clips is twenty rounds of aim-and-click at small controls. These are the
  // standard review keys (j/k to move, a/x to judge, space to play) so they need no learning.
  //
  // Bound on the window rather than per card, because the target is "the clip I am looking at"
  // rather than whatever the browser thinks has focus. Deliberately inert while a text field or
  // a select has focus: `a` must type an `a` when the user is writing a caption, and getting that
  // wrong would silently approve clips while someone edits metadata.
  useEffect(() => {
    if (!clips.length) return undefined;
    const onKeyDown = (event) => {
      const tag = (event.target?.tagName || "").toLowerCase();
      if (
        tag === "input" ||
        tag === "textarea" ||
        tag === "select" ||
        event.target?.isContentEditable
      ) {
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      switch (event.key) {
        case "j":
          setFocusIndex((index) => Math.min(clips.length - 1, index + 1));
          break;
        case "k":
          setFocusIndex((index) => Math.max(0, index - 1));
          break;
        case "a":
          reviewFocused("approved");
          break;
        case "x":
          reviewFocused("rejected");
          break;
        case "s":
          toggleSelected(clips[focusIndex]?.id);
          break;
        case " ":
          // Prevented, or space scrolls the page out from under the clip being reviewed.
          event.preventDefault();
          playerRef.current?.togglePlay?.();
          break;
        case ",":
          playerRef.current?.step?.(-1);
          break;
        case ".":
          playerRef.current?.step?.(1);
          break;
        case "ArrowLeft":
          playerRef.current?.skip?.(-1);
          break;
        case "ArrowRight":
          playerRef.current?.skip?.(1);
          break;
        default:
          return;
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [clips, focusIndex, reviewFocused, toggleSelected]);

  useEffect(() => {
    // Keep the focus inside the list when clips arrive or disappear.
    setFocusIndex((index) => Math.max(0, Math.min(index, Math.max(0, clips.length - 1))));
  }, [clips.length]);

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
          <div className="truncate font-medium text-slate-100">{job.title || job.source}</div>
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
            <>
              <ReviewBar
                counts={counts}
                total={clips.length}
                selectedCount={selectedClips.size}
                busy={batchBusy}
                error={batchError}
                onSelectAll={() => setSelectedClips(new Set(clips.map((clip) => clip.id)))}
                onSelectNone={() => setSelectedClips(new Set())}
                onSelectPending={() =>
                  setSelectedClips(
                    new Set(
                      clips
                        .filter((clip) => (clip.review_state || "pending") === "pending")
                        .map((clip) => clip.id)
                    )
                  )
                }
                onApprove={() => applyBatch("approved")}
                onReject={() => applyBatch("rejected")}
                onReset={() => applyBatch("pending")}
              />
              <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
                {clips.map((clip, index) => (
                  <ClipCard
                    key={clip.id}
                    jobId={job.id}
                    clip={clip}
                    settings={settings}
                    selected={selectedClips.has(clip.id)}
                    onToggleSelected={toggleSelected}
                    focused={index === focusIndex}
                    onRegisterPlayer={
                      index === focusIndex
                        ? (controls) => {
                            playerRef.current = controls;
                          }
                        : undefined
                    }
                    llmAvailable={llmAvailable}
                    publishing={publishing}
                    publisherStatuses={publisherStatuses}
                    attempts={(publishAttempts || []).filter(
                      (attempt) => attempt.job_id === job.id && attempt.clip_id === clip.id
                    )}
                    onUpdated={(updated) => onClipUpdated?.(job.id, updated)}
                    onPublished={onPublished}
                  />
                ))}
              </div>
            </>
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
                            {s.count > 1
                              ? `${s.count}× · ${(s.mean_seconds || 0).toFixed(1)}s each`
                              : ""}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <LlmUsage usage={timings.llm_usage} />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Phase 7: what this job spent on LLM tokens, shown beside where its time went.
 *
 * Rendered only when there were calls, because a row reading "0 tokens" on a job that never
 * used the LLM is noise — and with no API key configured that is every job.
 */
function LlmUsage({ usage }) {
  const calls = usage?.calls || 0;
  if (!usage || calls === 0) return null;

  const tokens = usage.total_tokens || 0;
  const cost = usage.cost_usd;
  const unmetered = usage.unmetered_calls || 0;

  return (
    <div className="mt-2 border-t border-slate-800 pt-2">
      <div className="text-slate-400">
        LLM — {calls} call{calls === 1 ? "" : "s"} · {tokens.toLocaleString()} tokens
        {/* The distinction this whole feature turns on. `cost_usd` is null when no price is
            configured, and showing "$0.00" there would be read as "this was free" and believed.
            So an unpriced job says so, and points at the setting that changes it. */}
        {cost === null || cost === undefined ? (
          <span className="text-slate-500"> · cost not priced</span>
        ) : (
          <span className="text-slate-300"> · ${cost.toFixed(4)}</span>
        )}
      </div>
      {cost === null || cost === undefined ? (
        <div className="mt-0.5 text-slate-500">
          Set LLM_PRICE_INPUT_PER_MTOK and LLM_PRICE_OUTPUT_PER_MTOK to see spend.
        </div>
      ) : null}
      {/* A cost derived from an incomplete token count is a lower bound, and saying so is the
          difference between an understated bill and an unexplained one. */}
      {unmetered > 0 ? (
        <div className="mt-0.5 text-amber-300/80">
          {unmetered} call{unmetered === 1 ? "" : "s"} reported no token count, so this is a
          minimum.
        </div>
      ) : null}
      {(usage.models || []).length > 1 ? (
        <table className="mt-1 w-full text-left">
          <tbody>
            {usage.models.map((m) => (
              <tr key={m.model}>
                <td className="py-0.5 pr-3 text-slate-300">{m.model}</td>
                <td className="py-0.5 pr-3 text-slate-400">
                  {(m.total_tokens || 0).toLocaleString()} tokens
                </td>
                <td className="py-0.5 text-slate-500">
                  {m.calls}× {m.cost_usd === null ? "" : `· $${(m.cost_usd || 0).toFixed(4)}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

LlmUsage.propTypes = {
  // Optional: absent for a job that predates the field, and `{}` for one that made no calls.
  usage: PropTypes.shape({
    calls: PropTypes.number,
    total_tokens: PropTypes.number,
    unmetered_calls: PropTypes.number,
    // Deliberately nullable — null means "no rate configured", which is not zero.
    cost_usd: PropTypes.number,
    priced: PropTypes.bool,
    models: PropTypes.array,
  }),
};

JobCard.propTypes = {
  // Required, and required in the two fields the card cannot do without: `id` addresses every call
  // it makes, and `status` decides which of the five bodies it renders — a job with no status would
  // fall through to the queued badge and a progress bar for work that may have finished.
  job: PropTypes.shape({
    id: PropTypes.string.isRequired,
    status: PropTypes.string.isRequired,
    // Always sent by `/api/jobs`, and dereferenced unguarded in the completed branch.
    clips: PropTypes.arrayOf(CLIP_SHAPE).isRequired,
    title: PropTypes.string,
    source: PropTypes.string,
    input_type: PropTypes.string,
    duration: PropTypes.number,
    progress: PropTypes.number,
    stage: PropTypes.string,
    // U8: the step counter, shown only when both are positive, because a wrong step number is
    // worse than none.
    stage_index: PropTypes.number,
    stage_total: PropTypes.number,
    stage_timings: PropTypes.array,
    error: PropTypes.string,
  }).isRequired,
  llmAvailable: PropTypes.bool,
  publishing: PUBLISHING_SHAPE,
  publisherStatuses: PUBLISHER_STATUSES_SHAPE,
  // Every attempt in the app; this card filters out the ones belonging to its own clips.
  publishAttempts: PropTypes.arrayOf(PUBLISH_ATTEMPT_SHAPE),
  // Called with `?.` after a verdict or an edit. Without it the batch bar's tally never moves,
  // because the counts are derived from the clips the parent holds.
  onClipUpdated: PropTypes.func,
  onPublished: PropTypes.func,
  // U7: forwarded verbatim to each clip's re-render. Already in wire form.
  settings: WIRE_OPTIONS_SHAPE,
};
