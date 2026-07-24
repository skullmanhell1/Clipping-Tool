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

/**
 * The settings panel: core dropdowns (Language, Clip Length, Aspect Ratio,
 * Number of Clips) plus a collapsible Advanced section (Selection method,
 * Platform, Vibe/Tone, Clip Topic, Process Range, Hashtag count) and
 * captions / watch-folder toggles.
 */
export default function SettingsPanel({ settings, onChange, watch, onToggleWatch }) {
  const [showAdvanced, setShowAdvanced] = useState(false);
  const set = (key) => (value) => onChange({ ...settings, [key]: value });
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
