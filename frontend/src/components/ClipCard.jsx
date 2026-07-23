import React from "react";
import { api, formatDuration } from "../api.js";

/**
 * A single finished clip: inline video preview + download button.
 */
export default function ClipCard({ jobId, clip }) {
  return (
    <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900">
      <video
        src={api.clipUrl(clip.video_url)}
        poster={clip.thumbnail_url ? api.clipUrl(clip.thumbnail_url) : undefined}
        controls
        preload="metadata"
        className="aspect-[9/16] w-full bg-black object-contain"
      />
      <div className="flex items-center justify-between gap-2 p-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-slate-100">{clip.title}</div>
          <div className="text-xs text-slate-500">{formatDuration(clip.duration)}</div>
        </div>
        <a
          href={api.downloadUrl(jobId, clip.filename)}
          className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-emerald-500"
        >
          Download
        </a>
      </div>
    </div>
  );
}
