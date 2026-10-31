// U4: turning struck-out words into the cut ranges the backend expects.
//
// Kept out of the component because it is the part with rules. The editor's own state is a
// set of word indices, which is what a click naturally produces; the API wants time ranges.
// Those are not the same shape, and the conversion has one decision in it worth stating:
// **consecutive struck words become one cut**, which also removes the silence between them.
// Cutting each word separately would leave the pauses behind, so striking "um ... uh" would
// remove the words and keep the hesitation - the opposite of the point.

/**
 * Convert a set of struck word indices into merged, clip-relative cut ranges.
 *
 * @param {Array<{start:number,end:number}>} words - clip-relative words, in order.
 * @param {Iterable<number>} struck - indices into `words`.
 * @returns {Array<{start:number,end:number}>} ascending, non-adjacent cut ranges.
 */
export function wordsToCuts(words, struck) {
  const indices = [...new Set(Array.from(struck ?? []))]
    .filter((i) => Number.isInteger(i) && i >= 0 && i < (words?.length ?? 0))
    .sort((a, b) => a - b);
  if (indices.length === 0) return [];

  const cuts = [];
  let runStart = indices[0];
  let runEnd = indices[0];
  for (const index of indices.slice(1)) {
    if (index === runEnd + 1) {
      runEnd = index;
    } else {
      cuts.push({ start: words[runStart].start, end: words[runEnd].end });
      runStart = index;
      runEnd = index;
    }
  }
  cuts.push({ start: words[runStart].start, end: words[runEnd].end });
  return cuts;
}

/**
 * Seconds removed by `cuts`. Used only for the editor's preview of the new length —
 * the backend recomputes it, and its answer is the one recorded on the clip.
 */
export function cutSeconds(cuts) {
  return (cuts ?? []).reduce((total, cut) => total + Math.max(0, cut.end - cut.start), 0);
}

/**
 * Internal gaps between words that are long enough to be worth removing.
 *
 * These are real cut ranges, not word indices: the silence between two words belongs to
 * neither word, so striking a word cannot remove it. A leading or trailing gap is left alone
 * because the renderer already trims clip edges, and removing the tail here would hide the
 * natural end of the last word from the reviewer.
 *
 * @param {Array<{start:number,end:number}>} words - clip-relative words, in order.
 * @param {number} minGapSeconds - smallest gap to expose, in seconds.
 * @returns {Array<{start:number,end:number,seconds:number,afterIndex:number}>}
 */
export function longPauses(words, minGapSeconds = 0.9) {
  const threshold = Number(minGapSeconds);
  if (!Array.isArray(words) || words.length < 2 || !Number.isFinite(threshold) || threshold <= 0) {
    return [];
  }
  const pauses = [];
  for (let index = 1; index < words.length; index += 1) {
    const previous = words[index - 1];
    const next = words[index];
    const start = Number(previous?.end);
    const end = Number(next?.start);
    if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
    const seconds = end - start;
    if (seconds < threshold) continue;
    pauses.push({ start, end, seconds, afterIndex: index - 1 });
  }
  return pauses;
}

/**
 * Merge overlapping or touching ranges into the disjoint list the renderer expects.
 *
 * Word cuts and pause cuts are produced independently and can meet exactly at a word boundary.
 * Two adjacent ranges would otherwise become two keep intervals separated by a few frames of
 * audio, which is the audible seam this editor exists to avoid.
 *
 * @param {Array<{start:number,end:number}>} cuts
 * @returns {Array<{start:number,end:number}>}
 */
export function mergeCuts(cuts) {
  const ranges = (cuts ?? [])
    .map((cut) => ({
      start: Number(cut?.start),
      end: Number(cut?.end),
    }))
    .filter((cut) => Number.isFinite(cut.start) && Number.isFinite(cut.end))
    .map((cut) => (cut.end < cut.start ? { start: cut.end, end: cut.start } : cut))
    .filter((cut) => cut.end - cut.start > 0)
    .sort((left, right) => left.start - right.start);

  const merged = [];
  for (const cut of ranges) {
    const previous = merged[merged.length - 1];
    if (previous && cut.start <= previous.end + 0.001) {
      previous.end = Math.max(previous.end, cut.end);
    } else {
      merged.push({ ...cut });
    }
  }
  return merged;
}
