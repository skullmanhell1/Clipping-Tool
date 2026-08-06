import { describe, expect, it } from "vitest";

import { cutSeconds, wordsToCuts } from "./transcriptCuts.js";

const WORDS = [
  { start: 0.0, end: 0.4, text: "one" },
  { start: 0.6, end: 1.0, text: "two" },
  { start: 1.4, end: 1.8, text: "three" },
  { start: 2.2, end: 2.6, text: "four" },
  { start: 3.0, end: 3.4, text: "five" },
];

describe("wordsToCuts (U4)", () => {
  it("returns nothing when no word is struck", () => {
    expect(wordsToCuts(WORDS, [])).toEqual([]);
    expect(wordsToCuts(WORDS, null)).toEqual([]);
  });

  it("turns one struck word into one cut spanning that word", () => {
    expect(wordsToCuts(WORDS, [2])).toEqual([{ start: 1.4, end: 1.8 }]);
  });

  it("merges consecutive struck words into a single cut", () => {
    // The silence between them goes too — cutting each separately would remove the words
    // and keep the hesitation, which is the opposite of the point.
    expect(wordsToCuts(WORDS, [1, 2])).toEqual([{ start: 0.6, end: 1.8 }]);
  });

  it("keeps non-consecutive words as separate cuts", () => {
    expect(wordsToCuts(WORDS, [0, 4])).toEqual([
      { start: 0.0, end: 0.4 },
      { start: 3.0, end: 3.4 },
    ]);
  });

  it("does not care what order the indices arrive in", () => {
    expect(wordsToCuts(WORDS, [4, 0])).toEqual(wordsToCuts(WORDS, [0, 4]));
  });

  it("ignores duplicates", () => {
    expect(wordsToCuts(WORDS, [2, 2, 2])).toEqual([{ start: 1.4, end: 1.8 }]);
  });

  it("ignores indices outside the word list", () => {
    // A stale selection against a re-fetched, shorter transcript must not read past the end.
    expect(wordsToCuts(WORDS, [99, -1, 1.5, 2])).toEqual([{ start: 1.4, end: 1.8 }]);
  });

  it("accepts a Set, which is what the editor holds", () => {
    expect(wordsToCuts(WORDS, new Set([1, 2]))).toEqual([{ start: 0.6, end: 1.8 }]);
  });

  it("produces ascending, non-overlapping cuts", () => {
    const cuts = wordsToCuts(WORDS, [0, 2, 4]);
    cuts.slice(1).forEach((cut, index) => {
      expect(cut.start).toBeGreaterThanOrEqual(cuts[index].end);
    });
  });
});

describe("cutSeconds", () => {
  it("sums the cut durations", () => {
    expect(cutSeconds([{ start: 1, end: 2 }, { start: 4, end: 4.5 }])).toBeCloseTo(1.5);
  });

  it("is zero for no cuts", () => {
    expect(cutSeconds([])).toBe(0);
    expect(cutSeconds(undefined)).toBe(0);
  });
});
