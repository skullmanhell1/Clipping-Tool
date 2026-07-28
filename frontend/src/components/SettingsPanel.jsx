import React, { useState } from "react";
import Dropdown from "./Dropdown.jsx";

// Option lists mirror the backend's accepted values (see /api/info).
const LANGUAGES = [
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

const CLIP_LENGTHS = [
  { value: "auto", label: "Auto (< 90s)" },
  { value: "<30s", label: "< 30s" },
  { value: "30-60s", label: "30 - 60s" },
  { value: "60-90s", label: "60 - 90s" },
  { value: "90s-3min", label: "90s - 3min" },
];

const ASPECTS = [
  { value: "9:16", label: "9:16 (Vertical)" },
  { value: "1:1", label: "1:1 (Square)" },
  { value: "16:9", label: "16:9 (Wide)" },
  { value: "4:5", label: "4:5 (Portrait)" },
];

const CLIP_COUNTS = [
  { value: "auto", label: "Auto" },
  { value: "1", label: "1" },
  { value: "3", label: "3" },
  { value: "5", label: "5" },
  { value: "10", label: "10" },
  { value: "max", label: "Max" },
];

const PLATFORMS = [
  { value: "generic", label: "Generic" },
  { value: "youtube", label: "YouTube" },
  { value: "tiktok", label: "TikTok" },
  { value: "instagram", label: "Instagram" },
  { value: "x", label: "X (Twitter)" },
  { value: "whop", label: "Whop" },
];

const STRATEGIES = [
  { value: "ai", label: "AI highlights" },
  { value: "silence", label: "Silence-based" },
  { value: "fixed", label: "Fixed length" },
];

const VIBES = [
  { value: "", label: "Auto" },
  { value: "energetic", label: "Energetic" },
  { value: "educational", label: "Educational" },
  { value: "funny", label: "Funny" },
  { value: "inspirational", label: "Inspirational" },
  { value: "dramatic", label: "Dramatic" },
  { value: "chill", label: "Chill" },
];

// --- Phase 4: visual effects option lists ---------------------------------
const CAPTION_TEMPLATES = [
  { value: "karaoke", label: "Karaoke (word fill)" },
  { value: "boxed", label: "Boxed" },
  { value: "minimal", label: "Minimal" },
];

const CAPTION_POSITIONS = [
  { value: "bottom", label: "Bottom" },
  { value: "center", label: "Center" },
  { value: "top", label: "Top" },
];

const COLOR_PRESETS = [
  { value: "", label: "None" },
  { value: "vivid", label: "Vivid" },
  { value: "warm", label: "Warm" },
  { value: "cool", label: "Cool" },
  { value: "cinematic", label: "Cinematic" },
  { value: "bw", label: "Black & White" },
];

const MUSIC_MOODS = [
  { value: "", label: "No music" },
  { value: "upbeat", label: "Upbeat" },
  { value: "chill", label: "Chill" },
  { value: "dramatic", label: "Dramatic" },
  { value: "corporate", label: "Corporate" },
  { value: "suspense", label: "Suspense" },
];

const EMOJI_INTENSITIES = [
  { value: "off", label: "Off" },
  { value: "subtle", label: "Subtle" },
  { value: "standard", label: "Standard" },
  { value: "heavy", label: "Heavy" },
];

const EMOJI_MODES = [
  { value: "keyword", label: "Keyword map" },
  { value: "ai", label: "AI (context-aware)" },
];

// --- Tier 1: animated captions / b-roll / visual selection option lists ----
const CAPTION_PRESETS = [
  { value: "karaoke", label: "Karaoke" },
  { value: "boxed", label: "Boxed" },
  { value: "minimal", label: "Minimal" },
  { value: "pop", label: "Pop" },
  { value: "typewriter", label: "Typewriter" },
  { value: "hormozi", label: "Hormozi" },
];

const BROLL_INTENSITIES = [
  { value: "off", label: "Off" },
  { value: "subtle", label: "Subtle" },
  { value: "standard", label: "Standard" },
  { value: "heavy", label: "Heavy" },
];

const ASSET_SOURCING_MODES = [
  { value: "off", label: "Off" },
  { value: "local_only", label: "Local only" },
  { value: "local_then_external", label: "Local, then external" },
];

// --- Speaker diarisation & multi-speaker reframe option lists --------------
// Known values used as fallbacks / friendly labels; the accepted values mirror
// /api/info's effects.reframe_layouts / effects.reframe_intensities.
const REFRAME_LAYOUTS = [
  { value: "follow_active", label: "Follow active speaker" },
  { value: "split_screen", label: "Split screen" },
];

const REFRAME_INTENSITIES = [
  { value: "subtle", label: "Subtle" },
  { value: "standard", label: "Standard" },
  { value: "heavy", label: "Heavy" },
];

// Friendly labels applied to the raw values advertised by /api/info.
const REFRAME_LAYOUT_LABELS = {
  follow_active: "Follow active speaker",
  split_screen: "Split screen",
};
const REFRAME_INTENSITY_LABELS = {
  subtle: "Subtle",
  standard: "Standard",
  heavy: "Heavy",
};

// Build a labelled option list from a raw list of values advertised by
// /api/info's effects object, falling back to the known values when the info
// payload has not loaded (or omits the list) so the control still renders.
const labelledOptions = (values, labels, fallback) =>
  Array.isArray(values) && values.length > 0
    ? values.map((value) => ({ value, label: labels[value] || value }))
    : fallback;

// --- Kinetic typography (kinetic-typography spec, Req 17.6) ----------------
// Known values used as fallbacks / friendly labels; the accepted values are
// advertised by /api/info under capabilities.kinetic_typography.
const KINETIC_ENGINE_ID = "kinetic_typography";

const KINETIC_STYLES = [
  { value: "bounce", label: "Bounce" },
  { value: "highlight_sweep", label: "Highlight sweep" },
  { value: "karaoke_fill", label: "Karaoke fill" },
  { value: "none", label: "None" },
  { value: "pop", label: "Pop" },
  { value: "slide_up", label: "Slide up" },
  { value: "typewriter", label: "Typewriter" },
];

const KINETIC_REVEALS = [
  { value: "cumulative", label: "Cumulative (line builds up)" },
  { value: "word_by_word", label: "Word by word" },
];

const KINETIC_STYLE_LABELS = {
  bounce: "Bounce",
  highlight_sweep: "Highlight sweep",
  karaoke_fill: "Karaoke fill",
  none: "None",
  pop: "Pop",
  slide_up: "Slide up",
  typewriter: "Typewriter",
};

const KINETIC_REVEAL_LABELS = {
  cumulative: "Cumulative (line builds up)",
  word_by_word: "Word by word",
};

// --- Advanced AV engines (Reqs 20.1, 20.3, 20.4) ---------------------------
// Engine ids are snake_case ("stem_separation"); show a friendly name.
const engineLabel = (engine) =>
  String(engine?.id || "")
    .split(/[_\-.]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ") || "Engine";

// An unavailable engine says which capabilities it is missing, so a creator
// cannot enable something that would silently degrade (Req 20.1).
const engineHint = (engine) => {
  const missing = Array.isArray(engine?.missing) ? engine.missing : [];
  if (engine?.available === false) {
    return missing.length > 0
      ? `Unavailable — missing ${missing.join(", ")}`
      : "Unavailable on this install";
  }
  return engine?.requires_network
    ? "Requires network access (blocked in permissibility mode)"
    : "";
};

// A small labelled checkbox toggle used across the effects section.
function Toggle({ label, checked, onChange, hint, disabled }) {
  return (
    <label
      className={`flex items-start gap-2 text-sm text-slate-300 ${
        disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer"
      }`}
    >
      <input
        type="checkbox"
        checked={!!checked}
        disabled={!!disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 accent-emerald-500"
      />
      <span>
        {label}
        {hint && <span className="block text-xs text-slate-500">{hint}</span>}
      </span>
    </label>
  );
}

/**
 * The settings panel: core dropdowns (Language, Clip Length, Aspect Ratio,
 * Number of Clips) plus a collapsible Advanced section (Selection method,
 * Platform, Vibe/Tone, Clip Topic, Process Range, Hashtag count) and
 * captions / watch-folder toggles.
 */
export default function SettingsPanel({
  settings,
  onChange,
  watch,
  onToggleWatch,
  effects,
  engines = [],
  capabilities = null,
}) {
  const reframeLayoutOptions = labelledOptions(
    effects?.reframe_layouts,
    REFRAME_LAYOUT_LABELS,
    REFRAME_LAYOUTS
  );
  const reframeIntensityOptions = labelledOptions(
    effects?.reframe_intensities,
    REFRAME_INTENSITY_LABELS,
    REFRAME_INTENSITIES
  );
  const engineRows = Array.isArray(engines) ? engines : [];
  // Kinetic typography: the option domains ride in /api/info's `capabilities`
  // block under the Engine_Id, while availability is reported on the engine row
  // (`available` / `missing`). An install that does not advertise the engine at
  // all leaves the controls enabled on the known fallback values.
  const kineticEngine = engineRows.find((engine) => engine?.id === KINETIC_ENGINE_ID);
  const kineticAvailable = !kineticEngine || kineticEngine.available !== false;
  const kineticDomains = capabilities?.[KINETIC_ENGINE_ID] || null;
  const kineticStyleOptions = labelledOptions(
    kineticDomains?.styles,
    KINETIC_STYLE_LABELS,
    KINETIC_STYLES
  );
  const kineticRevealOptions = labelledOptions(
    kineticDomains?.reveal_modes,
    KINETIC_REVEAL_LABELS,
    KINETIC_REVEALS
  );
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showEffects, setShowEffects] = useState(false);
  const [showEngines, setShowEngines] = useState(false);
  const set = (key) => (value) => onChange({ ...settings, [key]: value });
  const setFlag = (key) => (checked) => onChange({ ...settings, [key]: checked });
  const setNum = (key) => (e) => {
    const v = e.target.value;
    onChange({ ...settings, [key]: v === "" ? "" : v });
  };

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
      <h3 className="mb-4 text-sm font-semibold uppercase tracking-wider text-slate-400">
        Settings
      </h3>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Dropdown label="Language" value={settings.language} onChange={set("language")} options={LANGUAGES} />
        <Dropdown label="Clip Length" value={settings.clip_length} onChange={set("clip_length")} options={CLIP_LENGTHS} />
        <Dropdown label="Aspect Ratio" value={settings.aspect} onChange={set("aspect")} options={ASPECTS} />
        <Dropdown label="Number of Clips" value={settings.num_clips} onChange={set("num_clips")} options={CLIP_COUNTS} />
      </div>

      <button
        type="button"
        onClick={() => setShowAdvanced((v) => !v)}
        className="mt-4 flex items-center gap-2 text-sm font-medium text-brand-accent hover:underline"
      >
        <span>{showAdvanced ? "▾" : "▸"}</span> Advanced settings
      </button>

      {showAdvanced && (
        <div className="mt-4 space-y-4 rounded-xl border border-slate-800 bg-slate-950/40 p-4">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Dropdown label="Selection" value={settings.strategy} onChange={set("strategy")} options={STRATEGIES} />
            <Dropdown label="Platform" value={settings.platform} onChange={set("platform")} options={PLATFORMS} />
            <Dropdown label="Vibe / Tone" value={settings.vibe} onChange={set("vibe")} options={VIBES} />
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-slate-400">Hashtag count</span>
              <input
                type="number"
                min="0"
                max="30"
                value={settings.hashtag_count}
                onChange={setNum("hashtag_count")}
                className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 outline-none focus:border-brand-accent"
              />
            </label>
          </div>

          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-slate-400">Clip Topic / Keywords</span>
            <input
              type="text"
              value={settings.topic}
              onChange={(e) => onChange({ ...settings, topic: e.target.value })}
              placeholder="e.g. startup advice, growth hacks, funny moments"
              className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 placeholder-slate-500 outline-none focus:border-brand-accent"
            />
          </label>

          <div className="grid grid-cols-2 gap-4">
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-slate-400">Process from (sec)</span>
              <input
                type="number"
                min="0"
                value={settings.range_start}
                onChange={setNum("range_start")}
                placeholder="start"
                className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 placeholder-slate-500 outline-none focus:border-brand-accent"
              />
            </label>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-slate-400">Process to (sec)</span>
              <input
                type="number"
                min="0"
                value={settings.range_end}
                onChange={setNum("range_end")}
                placeholder="end"
                className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 placeholder-slate-500 outline-none focus:border-brand-accent"
              />
            </label>
          </div>

          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-slate-400">Selection prompt</span>
            <textarea
              rows="2"
              value={settings.selection_prompt}
              onChange={(e) => onChange({ ...settings, selection_prompt: e.target.value })}
              placeholder="Describe the moments to find, e.g. 'every time the speaker laughs'"
              className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 placeholder-slate-500 outline-none focus:border-brand-accent"
            />
          </label>

          <Toggle
            label="Visual selection"
            hint="Use visual/scene cues from sampled keyframes in addition to the transcript"
            checked={settings.visual_selection}
            onChange={setFlag("visual_selection")}
          />

          <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={settings.metadata}
              onChange={(e) => onChange({ ...settings, metadata: e.target.checked })}
              className="h-4 w-4 accent-emerald-500"
            />
            Generate AI titles &amp; hashtags
          </label>
        </div>
      )}

      <button
        type="button"
        onClick={() => setShowEffects((v) => !v)}
        className="mt-4 flex items-center gap-2 text-sm font-medium text-brand-accent hover:underline"
      >
        <span>{showEffects ? "▾" : "▸"}</span> Visual effects
      </button>

      {showEffects && (
        <div className="mt-4 space-y-5 rounded-xl border border-slate-800 bg-slate-950/40 p-4">
          {/* Captions styling */}
          <div>
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Captions
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Dropdown label="Caption preset" value={settings.caption_preset} onChange={set("caption_preset")} options={CAPTION_PRESETS} />
              <Dropdown label="Caption Position" value={settings.caption_position} onChange={set("caption_position")} options={CAPTION_POSITIONS} />
            </div>
            <div className="mt-2 grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Dropdown label="Caption Template (legacy)" value={settings.caption_template} onChange={set("caption_template")} options={CAPTION_TEMPLATES} />
            </div>
            <p className="mt-2 text-xs text-slate-500">
              The caption preset supersedes the legacy template when set to a non-default value.
            </p>
            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <Toggle
                label="Highlight keywords"
                hint="Emphasize important words with a distinct colour/scale"
                checked={settings.caption_keyword_highlight}
                onChange={setFlag("caption_keyword_highlight")}
              />
              <Toggle
                label="AI keyword highlight"
                hint="Use the LLM to pick keywords (falls back to rules if unavailable)"
                checked={settings.caption_keyword_ai}
                onChange={setFlag("caption_keyword_ai")}
              />
              <Toggle
                label="Emoji in captions"
                hint="Insert emoji inline within caption text"
                checked={settings.caption_emoji}
                onChange={setFlag("caption_emoji")}
              />
            </div>
          </div>

          {/* Kinetic typography (Req 17.6) — an unavailable engine disables the
              whole group so a creator cannot enable a silent degradation. A
              native <fieldset disabled> also disables every control inside it. */}
          <fieldset
            disabled={!kineticAvailable}
            className={kineticAvailable ? "" : "opacity-60"}
          >
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Kinetic typography
            </div>
            <div className="mb-3">
              <Toggle
                label="Kinetic typography captions"
                hint={
                  kineticAvailable
                    ? "Animated word-level captions; replaces the standard caption render"
                    : engineHint(kineticEngine)
                }
                checked={kineticAvailable && !!settings.kinetic_typography_enabled}
                onChange={setFlag("kinetic_typography_enabled")}
                disabled={!kineticAvailable}
              />
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Dropdown
                label="Kinetic style"
                value={settings.kinetic_style}
                onChange={set("kinetic_style")}
                options={kineticStyleOptions}
              />
              <Dropdown
                label="Reveal mode"
                value={settings.kinetic_reveal}
                onChange={set("kinetic_reveal")}
                options={kineticRevealOptions}
              />
            </div>
            <p className="mt-2 text-xs text-slate-500">
              {kineticAvailable
                ? "Inherits the caption preset's font, colours, and position; off by default."
                : engineHint(kineticEngine) ||
                  "Unavailable on this install — captions render with the standard engine."}
            </p>
          </fieldset>

          {/* B-roll overlays */}
          <div>
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              B-roll overlays
            </div>
            <div className="mb-3">
              <Toggle
                label="B-roll overlays"
                hint="Insert relevant images / short clips over key phrases"
                checked={settings.broll}
                onChange={setFlag("broll")}
              />
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <Dropdown label="B-roll intensity" value={settings.broll_intensity} onChange={set("broll_intensity")} options={BROLL_INTENSITIES} />
              <Dropdown label="Asset sourcing" value={settings.asset_sourcing_mode} onChange={set("asset_sourcing_mode")} options={ASSET_SOURCING_MODES} />
              <label className="flex flex-col gap-1.5 text-sm">
                <span className="text-slate-400">Provider</span>
                <input
                  type="text"
                  value={settings.broll_provider}
                  onChange={(e) => onChange({ ...settings, broll_provider: e.target.value })}
                  placeholder="e.g. openverse, pexels"
                  className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 placeholder-slate-500 outline-none focus:border-brand-accent"
                />
              </label>
            </div>
            <p className="mt-2 text-xs text-slate-500">
              External sourcing ("Local, then external") requires a configured provider API key;
              without one it behaves as "Local only". All external downloading is off by default.
            </p>
          </div>

          {/* Look / grade / music */}
          <div>
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Look &amp; sound
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <Dropdown label="Color grade" value={settings.color} onChange={set("color")} options={COLOR_PRESETS} />
              <Dropdown label="Background music" value={settings.music} onChange={set("music")} options={MUSIC_MOODS} />
              <label className="flex flex-col gap-1.5 text-sm">
                <span className="text-slate-400">
                  Music volume ({Math.round((Number(settings.music_volume) || 0) * 100)}%)
                </span>
                <input
                  type="range"
                  min="0"
                  max="0.5"
                  step="0.02"
                  value={settings.music_volume}
                  disabled={!settings.music}
                  onChange={(e) => onChange({ ...settings, music_volume: Number(e.target.value) })}
                  className="accent-emerald-500 disabled:opacity-40"
                />
              </label>
            </div>
            <div className="mt-3">
              <Toggle
                label="Permissibility mode (no added audio, local assets only)"
                hint="Disables all added audio and forces b-roll sourcing to local only"
                checked={settings.permissibility_mode}
                onChange={setFlag("permissibility_mode")}
              />
            </div>
          </div>

          {/* Emoji */}
          <div>
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Auto-emoji
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <Dropdown label="Intensity" value={settings.emoji} onChange={set("emoji")} options={EMOJI_INTENSITIES} />
              <Dropdown label="Mode" value={settings.emoji_mode} onChange={set("emoji_mode")} options={EMOJI_MODES} />
              <div className="flex items-end pb-2">
                <Toggle
                  label="Pop animation"
                  checked={settings.emoji_animate}
                  onChange={setFlag("emoji_animate")}
                />
              </div>
            </div>
          </div>

          {/* Toggleable frame effects */}
          <div>
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Effects
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <Toggle
                label="Face-tracking reframe"
                hint="Follows the speaker instead of a static crop (adds render time)"
                checked={settings.reframe}
                onChange={setFlag("reframe")}
              />
              <Toggle
                label="Speaker-aware reframe"
                hint="Reframes to the active speaker across multiple faces (adds render time)"
                checked={settings.speaker_reframe}
                onChange={setFlag("speaker_reframe")}
              />
              <Toggle
                label="Diarisation"
                hint="Detect who is speaking when; auto-enabled by speaker-aware reframe"
                checked={settings.diarization}
                onChange={setFlag("diarization")}
              />
              <Toggle label="Zoom / Ken Burns" checked={settings.zoom} onChange={setFlag("zoom")} />
              <Toggle label="Punch-in intro" checked={settings.transitions} onChange={setFlag("transitions")} />
              <Toggle label="Hook title overlay" hint="Burns the AI hook text at the start" checked={settings.hook_title} onChange={setFlag("hook_title")} />
              <Toggle label="Fade in / out" checked={settings.fades} onChange={setFlag("fades")} />
              <Toggle label="Progress bar" checked={settings.progress_bar} onChange={setFlag("progress_bar")} />
              <Toggle
                label="Filler-word removal"
                hint='Cuts "um"/"uh" and long pauses'
                checked={settings.filler_removal}
                onChange={setFlag("filler_removal")}
              />
            </div>
            <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Dropdown
                label="Reframe layout"
                value={settings.reframe_layout}
                onChange={set("reframe_layout")}
                options={reframeLayoutOptions}
              />
              <Dropdown
                label="Reframe intensity"
                value={settings.reframe_intensity}
                onChange={set("reframe_intensity")}
                options={reframeIntensityOptions}
              />
            </div>
          </div>

          <p className="text-xs text-slate-500">
            Frame-by-frame effects (reframe, zoom, emoji) are optional and add
            render time. Everything here is applied per clip in a single pass.
          </p>
        </div>
      )}

      {/* Advanced engines — rendered only when /api/info advertises one, so the
          v0.8.0 UI is unchanged until an engine ships (Reqs 20.1, 20.3, 20.4). */}
      {engineRows.length > 0 && (
        <>
          <button
            type="button"
            onClick={() => setShowEngines((v) => !v)}
            className="mt-4 flex items-center gap-2 text-sm font-medium text-brand-accent hover:underline"
          >
            <span>{showEngines ? "▾" : "▸"}</span> Advanced engines
          </button>

          {showEngines && (
            <div className="mt-4 space-y-3 rounded-xl border border-slate-800 bg-slate-950/40 p-4">
              {engineRows.map((engine, index) => {
                const flag = engine?.flag || `${engine?.id || ""}_enabled`;
                const available = engine?.available !== false;
                return (
                  <Toggle
                    key={flag || index}
                    label={engineLabel(engine)}
                    hint={engineHint(engine)}
                    checked={available && !!settings[flag]}
                    onChange={setFlag(flag)}
                    disabled={!available}
                  />
                );
              })}
            </div>
          )}
        </>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-6">
        <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={settings.captions}
            onChange={(e) => onChange({ ...settings, captions: e.target.checked })}
            className="h-4 w-4 accent-emerald-500"
          />
          Burn captions
        </label>

        <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={watch.enabled}
            onChange={(e) => onToggleWatch(e.target.checked)}
            className="h-4 w-4 accent-emerald-500"
          />
          Watch-folder mode
          {watch.enabled && watch.folder && (
            <span className="text-xs text-slate-500">({watch.folder})</span>
          )}
        </label>
      </div>
    </div>
  );
}
