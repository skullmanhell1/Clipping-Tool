import React from "react";
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

/**
 * The settings panel: Language, Clip Length, Aspect Ratio, Number of Clips,
 * plus captions and watch-folder toggles.
 */
export default function SettingsPanel({ settings, onChange, watch, onToggleWatch }) {
  const set = (key) => (value) => onChange({ ...settings, [key]: value });

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
