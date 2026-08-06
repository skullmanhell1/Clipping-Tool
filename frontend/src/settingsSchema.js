// The settings schema: one list of fields, and the handful of exceptions to forwarding them.
//
// This used to be spelled three times in App.jsx:
//
//   DEFAULT_ENGINE_SETTINGS   the AV engines' flags and options
//   DEFAULT_SETTINGS          every field and its default (spreading the above)
//   toOptions()               ~60 lines of `field: settings.field`, plus 5 real transforms
//
// The third list was the problem. Almost all of it was an identity mapping, so a field added
// to DEFAULT_SETTINGS and forgotten in toOptions would appear in the UI, be saved into
// profiles, round-trip correctly, and **never reach the backend** — a control that visibly
// moves and silently does nothing. Nothing failed in that case: the key was simply absent
// from the request and the backend applied its own default.
//
// (Checked before changing anything: the two lists had not actually drifted. 75 fields, 80
// forwarded keys — the extra 5 being `translate` and the four publishing fields. The bug was
// available, not yet made.)
//
// Now DEFAULT_SETTINGS is the only list of fields, and the code below states only where a
// value is *not* passed through unchanged. Adding a setting means adding one line.

import { resolveLanguage } from "./api.js";

// Advanced AV engines (Req 20.4): a sibling engine spec adds its `<engine_id>_enabled` flag
// and option defaults *here only*. They are forwarded by the same generic loop as everything
// else, and profiles persist them automatically because they round-trip through the opaque
// settings blob.
//
// Keys use the snake_case API spellings, because they are forwarded verbatim to the
// `/api/upload` Form fields and `OptionsModel` — a camelCase key here would silently never
// reach the backend.
//
// Kept as its own object rather than merged into DEFAULT_SETTINGS so that "what does this
// engine contribute" stays answerable, and so `ENGINE_FIELDS` below can be derived.
export const DEFAULT_ENGINE_SETTINGS = {
  // Kinetic typography engine (kinetic-typography spec, Req 17.5). Defaults mirror
  // `ProcessingOptions` / `Kinetic_Options` exactly; the flag is off, so a stock install
  // still renders exactly as v0.8.0.
  kinetic_typography_enabled: false,
  kinetic_style: "karaoke_fill",
  kinetic_reveal: "cumulative",
  kinetic_font: "",
  kinetic_max_lines: 2,
  kinetic_max_line_width: 22,
  kinetic_safe_area_x_pct: 6.0,
  kinetic_safe_area_y_pct: 10.0,
  kinetic_motion_ms: 120,
  kinetic_confidence_floor: 0.0,

  // Stem inpainting engine (audio-stem-inpainting spec). Defaults mirror
  // `ProcessingOptions` / `Stem_Options` exactly; the flag is off, so a stock install still
  // renders exactly as v0.8.0.
  stem_inpainting_enabled: false,
  stem_mix_preset: "custom",
  stem_gain_vocals: 1.0,
  stem_gain_music: 1.0,
  stem_gain_other: 1.0,
  stem_repair_mode: "crossfade",
  stem_repair_window_ms: 12,
  stem_declick: false,
  stem_backend: "auto",
  stem_model: "htdemucs",
  stem_retain_stems: false,
};

// Every setting the UI owns, with its default. **This is the schema.**
export const DEFAULT_SETTINGS = {
  language: "auto",
  clip_length: "auto",
  aspect: "9:16",
  num_clips: "auto",
  strategy: "ai",
  captions: true,
  subtitle_sidecar: false,
  topic: "",
  vocabulary: "",
  vibe: "",
  platform: "generic",
  hashtag_count: 5,
  range_start: "",
  range_end: "",
  metadata: true,
  // Phase 4 — visual effects (all individually toggleable)
  caption_template: "karaoke",
  caption_position: "bottom",
  reframe: false,
  zoom: false,
  transitions: false,
  hook_title: false,
  fades: false,
  progress_bar: false,
  color: "",
  music: "",
  music_volume: 0.12,
  emoji: "off",
  emoji_mode: "keyword",
  emoji_animate: true,
  filler_removal: false,
  // Tier 1 — animated captions / b-roll / visual selection (all default OFF / karaoke)
  caption_preset: "karaoke",
  // U6: the brand kit. Part of `settings` on purpose — saved profiles store the whole
  // settings blob, so a kit is saved, applied and set as default by machinery that already
  // exists.
  brand_font: "",
  brand_primary_color: "",
  brand_highlight_color: "",
  brand_cta: "",
  brand_logo: "",
  brand_logo_position: "top_right",
  brand_logo_scale: 0.16,
  brand_logo_opacity: 0.85,
  caption_animation: "",
  caption_keyword_highlight: false,
  caption_keyword_ai: false,
  caption_emoji: false,
  broll: false,
  broll_intensity: "standard",
  asset_sourcing_mode: "off",
  broll_provider: "",
  selection_prompt: "",
  visual_selection: false,
  permissibility_mode: false,
  // Speaker diarisation & multi-speaker reframe (all default OFF / follow_active / standard)
  diarization: false,
  speaker_reframe: false,
  reframe_layout: "follow_active",
  reframe_intensity: "standard",
  // Advanced AV engines — every flag and option default, forwarded generically
  ...DEFAULT_ENGINE_SETTINGS,
};

export const DEFAULT_PUBLISHING = {
  platforms: [],
  campaign_id: "",
  mode: "review",
  schedule: "",
  account_id: "",
  target_type: "",
  target_id: "",
};

// An empty numeric text input means "unset", not zero: a clip range of 0-0 is a different
// request from no range at all, and the backend distinguishes them by null.
const numOrNull = (value) =>
  value === "" || value === null || value === undefined ? null : Number(value);

const toNumberOrZero = (value) => Number(value) || 0;

const scheduleToEpoch = (value) => {
  if (!value) return null;
  const milliseconds = new Date(value).getTime();
  return Number.isNaN(milliseconds) ? null : milliseconds / 1000;
};

// The only fields whose value is not passed through as-is. Everything absent from here is
// forwarded verbatim, which is the point.
const COERCIONS = {
  hashtag_count: toNumberOrZero,
  music_volume: toNumberOrZero,
  range_start: numOrNull,
  range_end: numOrNull,
};

// The one field that is not one-to-one. The UI presents a single "language" control whose
// list includes "Translate to English", so one input encodes both the target language and
// whether to translate — and the API takes those as two separate fields.
const EXPANSIONS = {
  language: resolveLanguage,
};

// Publishing lives in its own state object in App, and its API field names differ from its
// UI ones, so it cannot go through the generic loop.
function publishingOptions(publishing) {
  return {
    // Only an explicit "auto" publishes without review. In review mode the platforms are
    // still selected in the UI, but sending them would publish immediately.
    publish_to: publishing.mode === "auto" ? publishing.platforms : [],
    campaign_id: publishing.campaign_id,
    publish_mode: publishing.mode,
    schedule_at: scheduleToEpoch(publishing.schedule),
  };
}

/**
 * Build the `/api/upload` (and `/api/jobs/url`) options payload from UI state.
 *
 * Every field in {@link DEFAULT_SETTINGS} is forwarded. A field missing from `settings`
 * falls back to its default rather than being sent as `undefined`: `JSON.stringify` drops
 * an undefined value, and the multipart Form path would send the literal string
 * `"undefined"`. This is what `engineOptions` used to do for engine fields alone; it now
 * applies to all of them, which is what let that special case go away.
 */
export function toApiOptions(settings, publishing) {
  const out = {};
  for (const key of Object.keys(DEFAULT_SETTINGS)) {
    const value = settings[key] === undefined ? DEFAULT_SETTINGS[key] : settings[key];
    const expand = EXPANSIONS[key];
    if (expand) {
      Object.assign(out, expand(value));
      continue;
    }
    const coerce = COERCIONS[key];
    out[key] = coerce ? coerce(value) : value;
  }
  return { ...out, ...publishingOptions(publishing) };
}

/** The keys `toApiOptions` sends that are not settings fields. Used by its tests. */
export const NON_SETTINGS_API_KEYS = [
  "translate",
  ...Object.keys(publishingOptions(DEFAULT_PUBLISHING)),
];
