import { describe, expect, it } from "vitest";

import { longPauses, mergeCuts } from "./transcriptCuts.js";

describe("longPauses", () => {
  const words = [
    { start: 0, end: 0.4 },
    { start: 1.6, end: 2.0 },
  ];

  it("returns internal gaps at or above the threshold", () => {
    const pauses = longPauses(words, 0.9);
    expect(pauses).toHaveLength(1);
    expect(pauses[0].start).toBeCloseTo(0.4);
    expect(pauses[0].end).toBeCloseTo(1.6);
    expect(pauses[0].seconds).toBeCloseTo(1.2);
    expect(pauses[0].afterIndex).toBe(0);
  });

  it("ignores gaps below the threshold and malformed timings", () => {
    expect(longPauses(words, 1.5)).toEqual([]);
    expect(longPauses([{ start: 0, end: 0.4 }, { start: Number.NaN, end: 2 }], 0.1)).toEqual([]);
  });

  it("does not remove the clip's leading or trailing edge", () => {
    expect(longPauses([{ start: 1, end: 2 }], 0.1)).toEqual([]);
  });
});

describe("mergeCuts", () => {
  it("joins touching word and pause ranges into one cut", () => {
    expect(
      mergeCuts([
        { start: 0.6, end: 1.0 },
        { start: 1.0, end: 2.2 },
      ]),
    ).toEqual([{ start: 0.6, end: 2.2 }]);
  });

  it("sorts overlapping ranges and drops empty entries", () => {
    expect(
      mergeCuts([
        { start: 2, end: 1.5 },
        { start: 0.4, end: 0.8 },
        { start: 0.7, end: 1.1 },
        { start: 3, end: 3 },
      ]),
    ).toEqual([
      { start: 0.4, end: 1.1 },
      { start: 1.5, end: 2 },
    ]);
  });
});
