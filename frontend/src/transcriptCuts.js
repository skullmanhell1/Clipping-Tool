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
