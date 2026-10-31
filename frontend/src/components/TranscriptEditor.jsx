import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

import { api, formatDuration } from "../api.js";
import { cutSeconds, longPauses, mergeCuts, wordsToCuts } from "../transcriptCuts.js";

// Keep this deliberately conservative: "like" can be a verb and "so" can carry meaning, so
// selecting them automatically would cut real speech. This mirrors the renderer's filler list.
const FILLER_WORDS = new Set([
  "um",
  "umm",
  "ummm",
  "uh",
  "uhh",
  "uhhh",
  "er",
  "err",
  "erm",
  "ah",
  "ahh",
  "hmm",
  "mmm",
  "mm",
  "uhm",
]);

function normalizedWord(text) {
  const match = String(text || "").toLowerCase().match(/[a-z']+/);
  return match ? match[0] : "";
}

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
  const [selectedPauses, setSelectedPauses] = useState([]);
  const [pauseThreshold, setPauseThreshold] = useState(0.9);

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
    setSelectedPauses([]);
  }, [jobId, clipId]);

  // Memoised on `state.data` rather than read inline: a fresh `[]` on every render would
  // change the identity of the `cuts` dependency each time and defeat its memo.
  const words = useMemo(() => state.data?.words ?? [], [state.data]);
  const fillerIndices = useMemo(
    () =>
      words.flatMap((word, index) =>
        FILLER_WORDS.has(normalizedWord(word.text)) ? [index] : [],
      ),
    [words],
  );
  const remainingFillerIndices = useMemo(
    () => fillerIndices.filter((index) => !struck.has(index)),
    [fillerIndices, struck],
  );
  const pauses = useMemo(() => longPauses(words, pauseThreshold), [words, pauseThreshold]);
  const selectedPauseStarts = useMemo(
    () => new Set(selectedPauses.map((pause) => pause.start)),
    [selectedPauses],
  );
  const remainingPauses = useMemo(
    () => pauses.filter((pause) => !selectedPauseStarts.has(pause.start)),
    [pauses, selectedPauseStarts],
  );
  const pauseByAfterIndex = useMemo(
    () => new Map(pauses.map((pause) => [pause.afterIndex, pause])),
    [pauses],
  );
  const cuts = useMemo(
    () => mergeCuts([...wordsToCuts(words, struck), ...selectedPauses]),
    [words, struck, selectedPauses],
  );
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

  const selectFillers = useCallback(() => {
    if (remainingFillerIndices.length === 0) return;
    setStruck((current) => new Set([...current, ...remainingFillerIndices]));
  }, [remainingFillerIndices]);

  const togglePause = useCallback((pause) => {
    setSelectedPauses((current) =>
      current.some((item) => item.start === pause.start)
        ? current.filter((item) => item.start !== pause.start)
        : [...current, pause],
    );
  }, []);

  const selectPauses = useCallback(() => {
    if (remainingPauses.length === 0) return;
    setSelectedPauses((current) => [...current, ...remainingPauses]);
  }, [remainingPauses]);

  const clearSelection = useCallback(() => {
    setStruck(new Set());
    setSelectedPauses([]);
  }, []);

  const changePauseThreshold = useCallback((value) => {
    setPauseThreshold(Number(value));
    // A threshold change can hide a selected pause; clearing keeps the visible timeline and
    // the cut list in agreement rather than retaining an invisible removal.
    setSelectedPauses([]);
  }, []);

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
  const selectionParts = [];
  if (struck.size > 0) selectionParts.push(`${struck.size} word${struck.size === 1 ? "" : "s"} cut`);
  if (selectedPauses.length > 0) {
    selectionParts.push(
      `${selectedPauses.length} pause${selectedPauses.length === 1 ? "" : "s"} cut`,
    );
  }
  selectionParts.push(
    `${cuts.length} range${cuts.length === 1 ? "" : "s"}`,
    `new length ~${formatDuration(newLength)}`,
  );

  return (
    <div className="space-y-2" data-testid={`transcript-editor-${clipId}`}>
      {/* The one case where the offsets shown do not line up with the media being played:
          something already tightened this clip, and the removed regions are not recorded on
          it, so they cannot be compensated for. Said plainly rather than hidden — a silently
          misaligned editor has the user striking the wrong words. */}
      {state.data?.trimmed ? (
        <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-300">
          This clip was already tightened when it was rendered, so these word times run ahead of
          the video you are playing. The cuts still apply to the original window.
        </p>
      ) : null}

      <div className="flex flex-wrap items-center gap-x-1 gap-y-1 rounded-lg border border-slate-800 bg-slate-950 p-2 text-sm leading-relaxed">
        {words.map((word, index) => {
          const isStruck = struck.has(index);
          const pause = pauseByAfterIndex.get(index - 1);
          const isPauseSelected = pause ? selectedPauseStarts.has(pause.start) : false;
          return (
            <Fragment key={`${index}-${word.start}`}>
              {pause ? (
                <button
                  type="button"
                  onClick={() => togglePause(pause)}
                  aria-pressed={isPauseSelected}
                  aria-label={`${isPauseSelected ? "Restore" : "Cut"} ${pause.seconds.toFixed(
                    1,
                  )} second pause after “${words[index - 1].text}”`}
                  title={
                    isPauseSelected
                      ? "Restore this pause"
                      : "Remove this gap between words"
                  }
                  className={`rounded border px-1 text-[10px] transition ${
                    isPauseSelected
                      ? "border-cyan-400/40 bg-cyan-400/15 text-cyan-200"
                      : "border-dashed border-slate-700 text-slate-500 hover:border-cyan-300/50"
                  }`}
                >
                  ⏸ {pause.seconds.toFixed(1)}s
                </button>
              ) : null}
              <button
                type="button"
                onClick={() => toggle(index)}
                aria-pressed={isStruck}
                aria-label={`${isStruck ? "Restore" : "Cut"} “${word.text}” at ${word.start.toFixed(
                  2,
                )} seconds`}
                className={`rounded px-1 transition ${
                  isStruck
                    ? "bg-rose-500/20 text-rose-300 line-through"
                    : "text-slate-200 hover:bg-slate-800"
                }`}
              >
                {word.text}
              </button>
            </Fragment>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span className="text-slate-500">
          {struck.size === 0 && selectedPauses.length === 0
            ? "Click words to cut them, or select a pause."
            : selectionParts.join(" · ")}
        </span>
        <span className="flex flex-wrap items-center gap-1.5">
          <label className="flex items-center gap-1 text-[10px] text-slate-500">
            Pause ≥
            <select
              aria-label="Minimum pause length"
              value={pauseThreshold}
              onChange={(event) => changePauseThreshold(event.target.value)}
              disabled={applying}
              className="rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[10px] text-slate-300"
            >
              <option value="0.6">0.6s</option>
              <option value="0.9">0.9s</option>
              <option value="1.2">1.2s</option>
              <option value="1.8">1.8s</option>
              <option value="2.5">2.5s</option>
            </select>
          </label>
          {pauses.length > 0 ? (
            <button
              type="button"
              onClick={selectPauses}
              disabled={remainingPauses.length === 0 || applying}
              title="Select every internal pause at or above the chosen length"
              className="rounded-lg border border-slate-700 px-2 py-1 text-slate-300 hover:border-cyan-300/50 disabled:opacity-40"
            >
              {remainingPauses.length > 0
                ? `Select pauses (${remainingPauses.length})`
                : "Pauses selected"}
            </button>
          ) : null}
          {fillerIndices.length > 0 ? (
            <button
              type="button"
              onClick={selectFillers}
              disabled={remainingFillerIndices.length === 0 || applying}
              title="Select only conservative filler words such as um and uh; review them before applying"
              className="rounded-lg border border-slate-700 px-2 py-1 text-slate-300 hover:border-cyan-300/50 disabled:opacity-40"
            >
              {remainingFillerIndices.length > 0
                ? `Select filler words (${remainingFillerIndices.length})`
                : "Filler words selected"}
            </button>
          ) : null}
          <button
            type="button"
            onClick={clearSelection}
            disabled={(struck.size === 0 && selectedPauses.length === 0) || applying}
            className="rounded-lg border border-slate-700 px-2 py-1 text-slate-400 hover:border-slate-500 disabled:opacity-40"
          >
            Clear
          </button>
          <button
            type="button"
            onClick={apply}
            disabled={
              (struck.size === 0 && selectedPauses.length === 0) || applying || overLimit
            }
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
