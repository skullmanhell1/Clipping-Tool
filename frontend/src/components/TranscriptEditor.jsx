import { useCallback, useEffect, useMemo, useState } from "react";

import { api, formatDuration } from "../api.js";
import { cutSeconds, wordsToCuts } from "../transcriptCuts.js";

// U4: transcript-based trimming — strike words out, re-render without them.
//
// The transcript is fetched lazily, when the editor is opened rather than with the clip, because
// most clips are never edited and the request is one per clip. A 409 from the endpoint is a
// normal outcome, not a bug: word timings come from the cache the render used, and it can have
// been swept or disabled. It is rendered as the API's own explanation, because "no transcript
// available" and "this clip has no speech" want different things from the user.

export default function TranscriptEditor({ jobId, clipId, onApply, applying = false }) {
  const [state, setState] = useState({ status: "loading", data: null, error: "" });
  const [struck, setStruck] = useState(() => new Set());

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading", data: null, error: "" });
    api
      .clipTranscript(jobId, clipId)
      .then((data) => {
        if (!cancelled) setState({ status: "ready", data, error: "" });
      })
      .catch((error) => {
        if (!cancelled) {
          setState({
            status: "error",
            data: null,
            error: error.message || "Could not load the transcript.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, clipId]);

  // Reset the selection when the editor moves to a different clip, so struck indices from
  // one transcript can never be applied to another's words.
  useEffect(() => {
    setStruck(new Set());
  }, [jobId, clipId]);

  // Memoised on `state.data` rather than read inline: a fresh `[]` on every render would
  // change the identity of the `cuts` dependency each time and defeat its memo.
  const words = useMemo(() => state.data?.words ?? [], [state.data]);
  const cuts = useMemo(() => wordsToCuts(words, struck), [words, struck]);
  const removed = cutSeconds(cuts);
  const maxCuts = state.data?.max_cuts ?? Infinity;
  const overLimit = cuts.length > maxCuts;

  const toggle = useCallback((index) => {
    setStruck((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }, []);

  const apply = useCallback(() => {
    if (cuts.length === 0 || overLimit) return;
    onApply?.(cuts);
  }, [cuts, onApply, overLimit]);

  if (state.status === "loading") {
    return (
      <p className="text-xs text-slate-500" role="status">
        Loading transcript…
      </p>
    );
  }

  if (state.status === "error") {
    return (
      <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-xs text-amber-300">
        {state.error}
      </p>
    );
  }

  if (words.length === 0) {
    return (
      <p className="text-xs text-slate-500">
        No words were transcribed for this clip, so there is nothing to trim.
      </p>
    );
  }

  const newLength = Math.max(0, (state.data?.duration ?? 0) - removed);

  return (
    <div className="space-y-2" data-testid={`transcript-editor-${clipId}`}>
      {/* The one case where the offsets shown do not line up with the media being played:
          something already tightened this clip, and the removed regions are not recorded on
          it, so they cannot be compensated for. Said plainly rather than hidden — a silently
          misaligned editor has the user striking the wrong words. */}
      {state.data?.trimmed ? (
        <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-300">
          This clip was already tightened when it was rendered, so these word times run ahead of the
          video you are playing. The cuts still apply to the original window.
        </p>
      ) : null}

      <div className="flex flex-wrap gap-x-1 gap-y-1 rounded-lg border border-slate-800 bg-slate-950 p-2 text-sm leading-relaxed">
        {words.map((word, index) => {
          const isStruck = struck.has(index);
          return (
            <button
              key={`${index}-${word.start}`}
              type="button"
              onClick={() => toggle(index)}
              aria-pressed={isStruck}
              aria-label={`${isStruck ? "Restore" : "Cut"} “${word.text}” at ${word.start.toFixed(
                2
              )} seconds`}
              className={`rounded px-1 transition ${
                isStruck
                  ? "bg-rose-500/20 text-rose-300 line-through"
                  : "text-slate-200 hover:bg-slate-800"
              }`}
            >
              {word.text}
            </button>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span className="text-slate-500">
          {struck.size === 0
            ? "Click words to cut them."
            : `${struck.size} word${struck.size === 1 ? "" : "s"} cut · ${cuts.length} range${
                cuts.length === 1 ? "" : "s"
              } · new length ~${formatDuration(newLength)}`}
        </span>
        <span className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setStruck(new Set())}
            disabled={struck.size === 0 || applying}
            className="rounded-lg border border-slate-700 px-2 py-1 text-slate-400 hover:border-slate-500 disabled:opacity-40"
          >
            Clear
          </button>
          <button
            type="button"
            onClick={apply}
            disabled={struck.size === 0 || applying || overLimit}
            className="rounded-lg bg-brand-accent px-2 py-1 font-semibold text-slate-950 disabled:opacity-40"
          >
            {applying ? "Re-rendering…" : "Apply cuts & re-render"}
          </button>
        </span>
      </div>

      {overLimit ? (
        <p className="text-[11px] text-rose-400">
          {cuts.length} separate cuts is over the limit of {maxCuts}. Each one adds filters to the
          render, so join them up or trim in a few passes.
        </p>
      ) : null}
    </div>
  );
}
