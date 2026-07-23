import React from "react";
import ClipCard from "./ClipCard.jsx";
import { formatDuration } from "../api.js";

const STATUS_STYLES = {
  queued: "bg-slate-700 text-slate-200",
  processing: "bg-amber-500/20 text-amber-300",
  completed: "bg-emerald-500/20 text-emerald-300",
  failed: "bg-rose-500/20 text-rose-300",
};

/**
 * Per-video card: header (title/source/status), live progress bar with the
 * current stage, and — once complete — a gallery grid of finished clips.
 */
export default function JobCard({ job }) {
  const pct = Math.round((job.progress || 0) * 100);
  const badge = STATUS_STYLES[job.status] || STATUS_STYLES.queued;

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
        <span className={`whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-medium ${badge}`}>
          {job.status}
        </span>
      </div>

      {(job.status === "processing" || job.status === "queued") && (
        <div className="mt-4">
          <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
            <div
              className="h-full rounded-full bg-gradient-to-r from-brand-accent to-brand transition-all duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
          <div className="mt-2 flex justify-between text-xs text-slate-400">
            <span>{job.stage}</span>
            <span>{pct}%</span>
          </div>
        </div>
      )}

      {job.status === "failed" && (
        <div className="mt-3 rounded-lg border border-rose-800 bg-rose-950/40 p-3 text-sm text-rose-300">
          {job.error || "Processing failed."}
        </div>
      )}

      {job.status === "completed" && (
        <div className="mt-4">
          {job.clips.length === 0 ? (
            <p className="text-sm text-slate-400">No clips were generated.</p>
          ) : (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
              {job.clips.map((clip) => (
                <ClipCard key={clip.id} jobId={job.id} clip={clip} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
