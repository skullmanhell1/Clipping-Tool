import PropTypes from "prop-types";

import { formatDuration } from "../api.js";

/**
 * Video preview card shown for a single pasted URL: thumbnail, title, source,
 * and duration.
 */
export default function PreviewCard({ preview, loading }) {
  if (!loading && !preview) return null;

  return (
    <div className="flex items-center gap-4 rounded-2xl border border-slate-800 bg-slate-900/50 p-4">
      <div className="h-20 w-36 flex-shrink-0 overflow-hidden rounded-lg bg-slate-800">
        {preview?.thumbnail ? (
          <img src={preview.thumbnail} alt="" className="h-full w-full object-cover" />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-xs text-slate-600">
            {loading ? "Loading..." : "No preview"}
          </div>
        )}
      </div>
      <div className="min-w-0 flex-1">
        {loading ? (
          <div className="text-sm text-slate-400">Fetching video info…</div>
        ) : (
          <>
            <div className="truncate font-medium text-slate-100">{preview.title}</div>
            {preview.uploader && (
              <div className="truncate text-sm text-slate-400">{preview.uploader}</div>
            )}
            <div className="mt-1 flex items-center gap-3 text-xs text-slate-500">
              <span>{formatDuration(preview.duration)}</span>
              {preview.source && (
                <a
                  href={preview.source}
                  target="_blank"
                  rel="noreferrer"
                  className="truncate text-brand-accent hover:underline"
                >
                  source
                </a>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

PreviewCard.propTypes = {
  // Nothing here is required. A null `preview` is the normal state — before a URL is pasted, while
  // one is being fetched, and after a fetch that failed — and the early return above is what makes
  // that renderable. Inside the payload, only what the extractor happened to report is present: a
  // live stream has no duration and the card says "--:--" rather than "0:00", and a source with no
  // uploader simply loses that line.
  preview: PropTypes.shape({
    title: PropTypes.string,
    uploader: PropTypes.string,
    duration: PropTypes.number,
    thumbnail: PropTypes.string,
    source: PropTypes.string,
  }),
  loading: PropTypes.bool,
};
