// The two pure helpers behind the settings panel, and the shape its option lists must keep.
//
// `labelledOptions` is the one with real consequences: it decides whether a control offers what
// the backend actually accepts or what this file happens to say, and it has to keep working when
// /api/info has not loaded.

import { describe, expect, it } from "vitest";
import {
  ASPECTS,
  CLIP_COUNTS,
  KINETIC_REVEALS,
  KINETIC_REVEAL_LABELS,
  KINETIC_STYLES,
  KINETIC_STYLE_LABELS,
  LANGUAGES,
  REFRAME_INTENSITIES,
  REFRAME_INTENSITY_LABELS,
  REFRAME_LAYOUTS,
  REFRAME_LAYOUT_LABELS,
  STEM_BACKENDS,
  STEM_BACKEND_LABELS,
  STEM_GAIN_FIELDS,
  STEM_MIX_PRESETS,
  STEM_MIX_PRESET_LABELS,
  STEM_REPAIR_MODES,
  STEM_REPAIR_MODE_LABELS,
  engineHint,
  engineLabel,
  labelledOptions,
} from "./settingsOptions.js";

const FALLBACK = [
  { value: "a", label: "Fallback A" },
  { value: "b", label: "Fallback B" },
];
const LABELS = { a: "Pretty A", b: "Pretty B" };

describe("labelledOptions", () => {
  it("prefers what the backend advertises over the hardcoded list", () => {
    // The whole point: /api/info is authoritative, and this file is only a fallback. Offering a
    // value the backend has dropped produces a 422 the user cannot explain.
    expect(labelledOptions(["b"], LABELS, FALLBACK)).toEqual([{ value: "b", label: "Pretty B" }]);
  });

  it("falls back to the known list when /api/info has not loaded", () => {
    // Not an empty dropdown: an unreachable info endpoint must not make the control unusable.
    expect(labelledOptions(null, LABELS, FALLBACK)).toEqual(FALLBACK);
    expect(labelledOptions(undefined, LABELS, FALLBACK)).toEqual(FALLBACK);
  });

  it("falls back when the payload omits the list, or sends an empty one", () => {
    expect(labelledOptions([], LABELS, FALLBACK)).toEqual(FALLBACK);
  });

  it("falls back when the payload is not a list at all", () => {
    // A backend that answers with an object here would otherwise crash the panel on `.map`.
    expect(labelledOptions({ a: 1 }, LABELS, FALLBACK)).toEqual(FALLBACK);
    expect(labelledOptions("a,b", LABELS, FALLBACK)).toEqual(FALLBACK);
  });

  it("shows the raw value when there is no friendly label for it", () => {
    // A value added on the backend has to remain selectable before the frontend learns its name.
    expect(labelledOptions(["c"], LABELS, FALLBACK)).toEqual([{ value: "c", label: "c" }]);
  });

  it("keeps the backend's order, which is the order the picker shows", () => {
    expect(labelledOptions(["b", "a"], LABELS, FALLBACK).map((o) => o.value)).toEqual(["b", "a"]);
  });
});

describe("engineLabel", () => {
  it("turns a snake_case id into words", () => {
    expect(engineLabel({ id: "stem_inpainting" })).toBe("Stem Inpainting");
  });

  it("handles hyphens and dots too", () => {
    expect(engineLabel({ id: "kinetic-typography" })).toBe("Kinetic Typography");
    expect(engineLabel({ id: "a.b" })).toBe("A B");
  });

  it('falls back to "Engine" rather than rendering an empty heading', () => {
    expect(engineLabel({})).toBe("Engine");
    expect(engineLabel(null)).toBe("Engine");
    expect(engineLabel({ id: "" })).toBe("Engine");
  });

  it("collapses repeated separators instead of emitting blank words", () => {
    expect(engineLabel({ id: "a__b" })).toBe("A B");
  });
});

describe("engineHint", () => {
  it("names the missing capabilities, so the reason is actionable", () => {
    // "Unavailable" alone sends the operator to the logs; naming the dependency does not.
    expect(engineHint({ available: false, missing: ["demucs", "torch"] })).toBe(
      "Unavailable — missing demucs, torch",
    );
  });

  it("still says it is unavailable when it cannot say why", () => {
    expect(engineHint({ available: false, missing: [] })).toBe("Unavailable on this install");
    expect(engineHint({ available: false })).toBe("Unavailable on this install");
  });

  it("tolerates a non-list `missing` from the backend", () => {
    expect(engineHint({ available: false, missing: "demucs" })).toBe("Unavailable on this install");
  });

  it("warns that a network engine is blocked in permissibility mode", () => {
    expect(engineHint({ available: true, requires_network: true })).toBe(
      "Requires network access (blocked in permissibility mode)",
    );
  });

  it("says nothing for an available local engine", () => {
    // An empty hint is what suppresses the hint line entirely.
    expect(engineHint({ available: true })).toBe("");
    expect(engineHint({})).toBe("");
    expect(engineHint(null)).toBe("");
  });

  it("reports unavailability ahead of the network note", () => {
    // Both can be true; "you cannot use this at all" is the more useful of the two.
    expect(engineHint({ available: false, requires_network: true, missing: ["x"] })).toMatch(
      /^Unavailable/,
    );
  });
});

describe("the option lists", () => {
  const lists = {
    LANGUAGES,
    ASPECTS,
    CLIP_COUNTS,
    KINETIC_STYLES,
    KINETIC_REVEALS,
    STEM_MIX_PRESETS,
    STEM_REPAIR_MODES,
    STEM_BACKENDS,
    REFRAME_LAYOUTS,
    REFRAME_INTENSITIES,
  };

  it.each(Object.keys(lists))("%s is a non-empty list of {value,label}", (name) => {
    const list = lists[name];
    expect(list.length).toBeGreaterThan(0);
    for (const option of list) {
      expect(Object.keys(option).sort()).toEqual(["label", "value"]);
      expect(typeof option.value).toBe("string");
      expect(option.label).toBeTruthy();
    }
  });

  it.each(Object.keys(lists))("%s has no duplicate values", (name) => {
    const values = lists[name].map((o) => o.value);
    expect(new Set(values).size).toBe(values.length);
  });

  const labelMaps = {
    KINETIC_STYLE_LABELS: [KINETIC_STYLE_LABELS, KINETIC_STYLES],
    KINETIC_REVEAL_LABELS: [KINETIC_REVEAL_LABELS, KINETIC_REVEALS],
    STEM_MIX_PRESET_LABELS: [STEM_MIX_PRESET_LABELS, STEM_MIX_PRESETS],
    STEM_REPAIR_MODE_LABELS: [STEM_REPAIR_MODE_LABELS, STEM_REPAIR_MODES],
    STEM_BACKEND_LABELS: [STEM_BACKEND_LABELS, STEM_BACKENDS],
    REFRAME_LAYOUT_LABELS: [REFRAME_LAYOUT_LABELS, REFRAME_LAYOUTS],
    REFRAME_INTENSITY_LABELS: [REFRAME_INTENSITY_LABELS, REFRAME_INTENSITIES],
  };

  it.each(Object.keys(labelMaps))("%s covers every value in its fallback list", (name) => {
    // The label map is what `labelledOptions` consults for backend-advertised values. A fallback
    // value with no label would render as its raw id even though a name exists for it.
    const [labels, fallback] = labelMaps[name];
    for (const option of fallback) {
      expect(labels).toHaveProperty(option.value);
    }
  });

  it("declares the three stem gains in sorted Stem_Set order", () => {
    // The order is the canonical one the backend uses, so the sliders read the same way as the
    // API's own field ordering.
    expect(STEM_GAIN_FIELDS.map(([field]) => field)).toEqual([
      "stem_gain_music",
      "stem_gain_other",
      "stem_gain_vocals",
    ]);
  });

  it('offers "auto" first wherever a control has an automatic mode', () => {
    // Auto is the default for each of these, and a default buried mid-list reads as one choice
    // among many rather than as the recommended one.
    expect(LANGUAGES[0].value).toBe("auto");
    expect(CLIP_COUNTS[0].value).toBe("auto");
  });
});
