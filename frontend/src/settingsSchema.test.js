// The settings schema's contract: every field reaches the API, and the payload is unchanged.
//
// The second half is a characterisation test. The golden below was captured from the previous
// three-list implementation *before* it was replaced, so these assertions say "the request the
// backend receives is byte-for-byte what it was", not "the new code agrees with itself".
//
// The first half is the guard that made the refactor worth doing. `toApiOptions` used to be
// ~60 lines of `field: settings.field`, and a field added to the defaults but forgotten there
// would show up in the UI, save into profiles, round-trip correctly, and never reach the
// backend — a control that visibly moves and silently does nothing, with nothing failing.

import { describe, expect, it } from "vitest";
import {
  DEFAULT_ENGINE_SETTINGS,
  DEFAULT_PUBLISHING,
  DEFAULT_SETTINGS,
  NON_SETTINGS_API_KEYS,
  toApiOptions,
} from "./settingsSchema.js";
// Captured from the previous three-list implementation, before it was replaced. Regenerate only
// if the API contract itself changes, and read the diff: every line is a field the backend sees.
import GOLDEN from "./test/golden/api-options.json";

// The fields whose UI control is a *numeric* text input. They default to "" but only ever
// hold a number, so the generic "fill an empty string with set_<key>" below would feed them
// text they cannot receive -- and `Number("set_range_start")` is NaN, which JSON cannot even
// represent (`JSON.stringify(NaN)` is `null`, so a golden could not record it either).
const NUMERIC_TEXT_FIELDS = { range_start: "12.5", range_end: "90" };

// A settings object with every field moved off its default, so a forwarding bug shows up as a
// wrong *value* and not just a missing key. Derived from the schema rather than written out,
// because a hand-written copy would be a fourth list to keep in step.
const populated = Object.fromEntries(
  Object.entries(DEFAULT_SETTINGS).map(([key, value]) => {
    if (key in NUMERIC_TEXT_FIELDS) return [key, NUMERIC_TEXT_FIELDS[key]];
    if (typeof value === "boolean") return [key, !value];
    if (typeof value === "number") return [key, value + 3];
    if (value === "") return [key, `set_${key}`];
    return [key, value];
  }),
);

const populatedPublishing = {
  platforms: ["tiktok", "x"],
  campaign_id: "c1",
  mode: "auto",
  schedule: "2026-01-02T03:04",
  account_id: "a",
  target_type: "t",
  target_id: "i",
};

describe("every setting reaches the API", () => {
  it("forwards each schema field, and nothing is silently dropped", () => {
    const sent = new Set(Object.keys(toApiOptions(DEFAULT_SETTINGS, DEFAULT_PUBLISHING)));
    // `language` is the one field that is not one-to-one: it expands to `language` + `translate`.
    const missing = Object.keys(DEFAULT_SETTINGS).filter((key) => !sent.has(key));
    expect(missing).toEqual([]);
  });

  it("sends nothing that is not a setting or a documented publishing field", () => {
    const sent = Object.keys(toApiOptions(DEFAULT_SETTINGS, DEFAULT_PUBLISHING));
    const unexpected = sent.filter(
      (key) => !(key in DEFAULT_SETTINGS) && !NON_SETTINGS_API_KEYS.includes(key),
    );
    expect(unexpected).toEqual([]);
  });

  it("carries every engine field, so an engine spec only has to touch the schema", () => {
    const sent = toApiOptions(populated, DEFAULT_PUBLISHING);
    for (const key of Object.keys(DEFAULT_ENGINE_SETTINGS)) {
      expect(sent).toHaveProperty(key, populated[key]);
    }
  });

  it("forwards a changed value, not just the key", () => {
    const sent = toApiOptions(populated, populatedPublishing);
    // Spot-checks across the categories, on top of the exhaustive golden below.
    expect(sent.captions).toBe(false); // boolean flipped
    expect(sent.topic).toBe("set_topic"); // empty string filled
    expect(sent.kinetic_typography_enabled).toBe(true); // engine flag
    expect(sent.reframe_layout).toBe("follow_active"); // non-empty string passed through
  });

  it("falls back to the default for a field the caller omitted", () => {
    // A profile saved before a setting existed has no key for it. Sending `undefined` would be
    // dropped by JSON.stringify and become the string "undefined" on the multipart Form path.
    const sent = toApiOptions({}, DEFAULT_PUBLISHING);
    expect(sent.aspect).toBe("9:16");
    expect(sent.captions).toBe(true);
    expect(sent.kinetic_style).toBe("karaoke_fill");
    expect(Object.values(sent)).not.toContain(undefined);
  });
});

describe("the four fields that are coerced", () => {
  it("turns an empty clip range into null rather than zero", () => {
    // 0-0 is a different request from "no range", and the backend tells them apart by null.
    const sent = toApiOptions(
      { ...DEFAULT_SETTINGS, range_start: "", range_end: "" },
      DEFAULT_PUBLISHING,
    );
    expect(sent.range_start).toBeNull();
    expect(sent.range_end).toBeNull();
  });

  it("reads a numeric range from the text input", () => {
    const sent = toApiOptions(
      { ...DEFAULT_SETTINGS, range_start: "12.5", range_end: "90" },
      DEFAULT_PUBLISHING,
    );
    expect(sent.range_start).toBe(12.5);
    expect(sent.range_end).toBe(90);
  });

  it("coerces hashtag_count and music_volume to numbers, defaulting to 0", () => {
    const sent = toApiOptions(
      { ...DEFAULT_SETTINGS, hashtag_count: "7", music_volume: "0.4" },
      DEFAULT_PUBLISHING,
    );
    expect(sent.hashtag_count).toBe(7);
    expect(sent.music_volume).toBe(0.4);
    const blank = toApiOptions(
      { ...DEFAULT_SETTINGS, hashtag_count: "", music_volume: "abc" },
      DEFAULT_PUBLISHING,
    );
    expect(blank.hashtag_count).toBe(0);
    expect(blank.music_volume).toBe(0);
  });
});

describe("the language control encodes two API fields", () => {
  it('maps "auto" to no language and no translation', () => {
    // null, not "": the backend distinguishes "detect it" from "the empty language".
    const sent = toApiOptions(DEFAULT_SETTINGS, DEFAULT_PUBLISHING);
    expect(sent.language).toBeNull();
    expect(sent.translate).toBe(false);
  });

  it('maps "translate" to translation on, with no source language', () => {
    const sent = toApiOptions({ ...DEFAULT_SETTINGS, language: "translate" }, DEFAULT_PUBLISHING);
    expect(sent.language).toBeNull();
    expect(sent.translate).toBe(true);
  });

  it("maps an explicit language through without translating", () => {
    const sent = toApiOptions({ ...DEFAULT_SETTINGS, language: "es" }, DEFAULT_PUBLISHING);
    expect(sent.language).toBe("es");
    expect(sent.translate).toBe(false);
  });
});

describe("publishing", () => {
  it("withholds the platform list unless the mode is auto", () => {
    // In review mode the platforms are still chosen in the UI; sending them would publish
    // immediately, which is the opposite of what review means.
    const review = toApiOptions(DEFAULT_SETTINGS, {
      ...DEFAULT_PUBLISHING,
      mode: "review",
      platforms: ["tiktok"],
    });
    expect(review.publish_to).toEqual([]);
    expect(review.publish_mode).toBe("review");
  });

  it("sends the platform list in auto mode", () => {
    const auto = toApiOptions(DEFAULT_SETTINGS, {
      ...DEFAULT_PUBLISHING,
      mode: "auto",
      platforms: ["tiktok", "x"],
    });
    expect(auto.publish_to).toEqual(["tiktok", "x"]);
  });

  it("converts the schedule to epoch seconds, and an unparseable one to null", () => {
    const at = toApiOptions(DEFAULT_SETTINGS, {
      ...DEFAULT_PUBLISHING,
      schedule: "2026-01-02T03:04Z",
    });
    expect(at.schedule_at).toBe(Date.parse("2026-01-02T03:04Z") / 1000);
    expect(
      toApiOptions(DEFAULT_SETTINGS, { ...DEFAULT_PUBLISHING, schedule: "" }).schedule_at,
    ).toBeNull();
    expect(
      toApiOptions(DEFAULT_SETTINGS, { ...DEFAULT_PUBLISHING, schedule: "not a date" }).schedule_at,
    ).toBeNull();
  });
});

describe("the payload is unchanged from the three-list implementation", () => {
  // Captured from the previous implementation before it was replaced. Every key and value.
  it("matches for default settings", () => {
    expect(toApiOptions(DEFAULT_SETTINGS, DEFAULT_PUBLISHING)).toEqual(GOLDEN.defaults);
  });

  it("matches for fully populated settings", () => {
    expect(toApiOptions(populated, populatedPublishing)).toEqual(GOLDEN.populated);
  });

  it("matches with translation requested", () => {
    expect(
      toApiOptions({ ...DEFAULT_SETTINGS, language: "translate" }, DEFAULT_PUBLISHING),
    ).toEqual(GOLDEN.translate);
  });

  it("matches with an explicit language", () => {
    expect(toApiOptions({ ...DEFAULT_SETTINGS, language: "es" }, DEFAULT_PUBLISHING)).toEqual(
      GOLDEN.explicit_lang,
    );
  });
});
