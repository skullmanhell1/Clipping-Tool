import { describe, expect, it } from "vitest";

import { clipQuality } from "./clipQuality.js";

const measured = (overrides = {}) => ({
  engagement_score: 84,
  engagement_features: {
    hook_score: 0.82,
    quality_scores: { flow: 0.74, impact: 0.7, clarity: 0.86, relevance: 0.68 },
  },
  ...overrides,
});

describe("clipQuality", () => {
  it("uses the measured engagement score and marks a clean high-signal clip as a top pick", () => {
    const quality = clipQuality(measured());
    expect(quality.overall).toBe(84);
    expect(quality.engagement).toBe(84);
    expect(quality.signals.map((signal) => signal.key)).toEqual([
      "hook",
      "flow",
      "impact",
      "clarity",
      "relevance",
    ]);
    expect(quality.topPick).toBe(true);
    expect(quality.needsAttention).toBe(false);
  });

  it("flags weak axes even when another measurement is strong", () => {
    const quality = clipQuality(
      measured({
        engagement_score: 55,
        engagement_features: {
          hook_score: 0.3,
          quality_scores: { clarity: 0.4, impact: 0.8 },
        },
      }),
    );
    expect(quality.needsAttention).toBe(true);
    expect(quality.topPick).toBe(false);
    expect(quality.attention).toEqual(["weak opening", "unclear thought"]);
  });

  it("does not invent a score for legacy or unmeasured clips", () => {
    const quality = clipQuality({ title: "Legacy clip" });
    expect(quality.overall).toBeNull();
    expect(quality.engagement).toBeNull();
    expect(quality.topPick).toBe(false);
    expect(quality.needsAttention).toBe(false);
    expect(quality.signals).toEqual([]);
  });

  it("prefers the selection hook when the retention plan did not run", () => {
    const quality = clipQuality({
      retention_plan: { mode: "off", hook_score: 0 },
      engagement_features: { hook_score: 0.9 },
    });
    expect(quality.signals).toEqual([
      {
        key: "hook",
        label: "Hook",
        hint: "Opening strength",
        percent: 90,
        tone: "bg-emerald-400",
      },
    ]);
  });
});
