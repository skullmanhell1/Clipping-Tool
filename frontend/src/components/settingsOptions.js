// The settings panel's option lists and the two pure helpers that shape them.
//
// Split out of SettingsPanel.jsx, which was 1040 lines. This is 260 of them: static data plus
// `labelledOptions` and `engineHint`. Neither reads state nor renders anything, so keeping them
// beside a 650-line component only made the component harder to find the top of.
//
// Every list "mirrors the backend's accepted values" -- which is a promise, not an observation.
// The lists are *fallbacks*: /api/info advertises the real domains, and `labelledOptions` prefers
// them. A list here that has drifted from the backend shows up only when /api/info is unreachable,
// which is exactly when nobody is looking, so `tests/test_stems_api.py` checks the field spellings
// against `ProcessingOptions` from the Python side.

// Option lists mirror the backend's accepted values (see /api/info).
export const LANGUAGES = [
  { value: "auto", label: "Auto-detect" },
  { value: "translate", label: "Translate to English" },
  { value: "en", label: "English" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "de", label: "German" },
  { value: "pt", label: "Portuguese" },
  { value: "hi", label: "Hindi" },
  { value: "ja", label: "Japanese" },
];

export const CLIP_LENGTHS = [
  { value: "auto", label: "Auto (< 90s)" },
  { value: "<30s", label: "< 30s" },
  { value: "30-60s", label: "30 - 60s" },
  { value: "60-90s", label: "60 - 90s" },
  { value: "90s-3min", label: "90s - 3min" },
];

export const ASPECTS = [
  { value: "9:16", label: "9:16 (Vertical)" },
  { value: "1:1", label: "1:1 (Square)" },
  { value: "16:9", label: "16:9 (Wide)" },
  { value: "4:5", label: "4:5 (Portrait)" },
];

export const CLIP_COUNTS = [
  { value: "auto", label: "Auto" },
  { value: "1", label: "1" },
  { value: "3", label: "3" },
  { value: "5", label: "5" },
  { value: "10", label: "10" },
  { value: "max", label: "Max" },
];

export const PLATFORMS = [
  { value: "generic", label: "Generic" },
  { value: "youtube", label: "YouTube" },
  { value: "tiktok", label: "TikTok" },
  { value: "instagram", label: "Instagram" },
  { value: "x", label: "X (Twitter)" },
  { value: "whop", label: "Whop" },
];

export const STRATEGIES = [
  { value: "ai", label: "AI highlights" },
  { value: "silence", label: "Silence-based" },
  { value: "fixed", label: "Fixed length" },
];

export const VIBES = [
  { value: "", label: "Auto" },
  { value: "energetic", label: "Energetic" },
  { value: "educational", label: "Educational" },
  { value: "funny", label: "Funny" },
  { value: "inspirational", label: "Inspirational" },
  { value: "dramatic", label: "Dramatic" },
  { value: "chill", label: "Chill" },
];

// --- Phase 4: visual effects option lists ---------------------------------
export const CAPTION_TEMPLATES = [
  { value: "karaoke", label: "Karaoke (word fill)" },
  { value: "boxed", label: "Boxed" },
  { value: "minimal", label: "Minimal" },
];

export const CAPTION_POSITIONS = [
  { value: "bottom", label: "Bottom" },
  { value: "center", label: "Center" },
  { value: "top", label: "Top" },
];

export const COLOR_PRESETS = [
  { value: "", label: "None" },
  { value: "vivid", label: "Vivid" },
  { value: "warm", label: "Warm" },
  { value: "cool", label: "Cool" },
  { value: "cinematic", label: "Cinematic" },
  { value: "bw", label: "Black & White" },
];

export const MUSIC_MOODS = [
  { value: "", label: "No music" },
  { value: "upbeat", label: "Upbeat" },
  { value: "chill", label: "Chill" },
  { value: "dramatic", label: "Dramatic" },
  { value: "corporate", label: "Corporate" },
  { value: "suspense", label: "Suspense" },
];

export const EMOJI_INTENSITIES = [
  { value: "off", label: "Off" },
  { value: "subtle", label: "Subtle" },
  { value: "standard", label: "Standard" },
  { value: "heavy", label: "Heavy" },
];

export const EMOJI_MODES = [
  { value: "keyword", label: "Keyword map" },
  { value: "ai", label: "AI (context-aware)" },
];

// --- Tier 1: animated captions / b-roll / visual selection option lists ----
export const CAPTION_PRESETS = [
  { value: "karaoke", label: "Karaoke" },
  { value: "boxed", label: "Boxed" },
  { value: "minimal", label: "Minimal" },
  { value: "pop", label: "Pop" },
  { value: "typewriter", label: "Typewriter" },
  { value: "hormozi", label: "Hormozi" },
];

export const BROLL_INTENSITIES = [
  { value: "off", label: "Off" },
  { value: "subtle", label: "Subtle" },
  { value: "standard", label: "Standard" },
  { value: "heavy", label: "Heavy" },
];

export const ASSET_SOURCING_MODES = [
  { value: "off", label: "Off" },
  { value: "local_only", label: "Local only" },
  { value: "local_then_external", label: "Local, then external" },
];

// --- Speaker diarisation & multi-speaker reframe option lists --------------
// Known values used as fallbacks / friendly labels; the accepted values mirror
// /api/info's effects.reframe_layouts / effects.reframe_intensities.
export const REFRAME_LAYOUTS = [
  { value: "follow_active", label: "Follow active speaker" },
  { value: "split_screen", label: "Split screen" },
];

export const REFRAME_INTENSITIES = [
  { value: "subtle", label: "Subtle" },
  { value: "standard", label: "Standard" },
  { value: "heavy", label: "Heavy" },
];

// Friendly labels applied to the raw values advertised by /api/info.
export const REFRAME_LAYOUT_LABELS = {
  follow_active: "Follow active speaker",
  split_screen: "Split screen",
};
export const REFRAME_INTENSITY_LABELS = {
  subtle: "Subtle",
  standard: "Standard",
  heavy: "Heavy",
};

// Build a labelled option list from a raw list of values advertised by
// /api/info's effects object, falling back to the known values when the info
// payload has not loaded (or omits the list) so the control still renders.
export const labelledOptions = (values, labels, fallback) =>
  Array.isArray(values) && values.length > 0
    ? values.map((value) => ({ value, label: labels[value] || value }))
    : fallback;

// --- Kinetic typography (kinetic-typography spec, Req 17.6) ----------------
// Known values used as fallbacks / friendly labels; the accepted values are
// advertised by /api/info under capabilities.kinetic_typography.
export const KINETIC_ENGINE_ID = "kinetic_typography";

export const KINETIC_STYLES = [
  { value: "bounce", label: "Bounce" },
  { value: "highlight_sweep", label: "Highlight sweep" },
  { value: "karaoke_fill", label: "Karaoke fill" },
  { value: "none", label: "None" },
  { value: "pop", label: "Pop" },
  { value: "slide_up", label: "Slide up" },
  { value: "typewriter", label: "Typewriter" },
];

export const KINETIC_REVEALS = [
  { value: "cumulative", label: "Cumulative (line builds up)" },
  { value: "word_by_word", label: "Word by word" },
];

export const KINETIC_STYLE_LABELS = {
  bounce: "Bounce",
  highlight_sweep: "Highlight sweep",
  karaoke_fill: "Karaoke fill",
  none: "None",
  pop: "Pop",
  slide_up: "Slide up",
  typewriter: "Typewriter",
};

export const KINETIC_REVEAL_LABELS = {
  cumulative: "Cumulative (line builds up)",
  word_by_word: "Word by word",
};

// --- Stem inpainting (audio-stem-inpainting spec, Req 18.4) ----------------
// Known values used as fallbacks / friendly labels; the accepted values and the numeric
// bounds are advertised by /api/info under capabilities.stem_inpainting.
export const STEM_ENGINE_ID = "stem_inpainting";

export const STEM_MIX_PRESETS = [
  { value: "custom", label: "Custom (use the sliders)" },
  { value: "speech_focus", label: "Speech focus" },
  { value: "music_focus", label: "Music focus" },
  { value: "clean_speech", label: "Clean speech (mute music)" },
];

export const STEM_REPAIR_MODES = [
  { value: "off", label: "Off" },
  { value: "crossfade", label: "Crossfade (equal-power notch)" },
  { value: "spectral", label: "Spectral (per-stem + music bridge)" },
];

export const STEM_BACKENDS = [
  { value: "auto", label: "Auto" },
  { value: "ml", label: "Local model (demucs)" },
  { value: "ffmpeg", label: "ffmpeg approximation" },
];

export const STEM_MIX_PRESET_LABELS = {
  custom: "Custom (use the sliders)",
  speech_focus: "Speech focus",
  music_focus: "Music focus",
  clean_speech: "Clean speech (mute music)",
};
export const STEM_REPAIR_MODE_LABELS = {
  off: "Off",
  crossfade: "Crossfade (equal-power notch)",
  spectral: "Spectral (per-stem + music bridge)",
};
export const STEM_BACKEND_LABELS = {
  auto: "Auto",
  ml: "Local model (demucs)",
  ffmpeg: "ffmpeg approximation",
};

// The three Stem_Gain fields, in the canonical (sorted) Stem_Set order.
export const STEM_GAIN_FIELDS = [
  ["stem_gain_music", "Music"],
  ["stem_gain_other", "Other"],
  ["stem_gain_vocals", "Vocals"],
];

// --- Advanced AV engines (Reqs 20.1, 20.3, 20.4) ---------------------------
// Engine ids are snake_case ("stem_separation"); show a friendly name.
export const engineLabel = (engine) =>
  String(engine?.id || "")
    .split(/[_\-.]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ") || "Engine";

// An unavailable engine says which capabilities it is missing, so a creator
// cannot enable something that would silently degrade (Req 20.1).
export const engineHint = (engine) => {
  const missing = Array.isArray(engine?.missing) ? engine.missing : [];
  if (engine?.available === false) {
    return missing.length > 0
      ? `Unavailable — missing ${missing.join(", ")}`
      : "Unavailable on this install";
  }
  return engine?.requires_network ? "Requires network access (blocked in permissibility mode)" : "";
};
