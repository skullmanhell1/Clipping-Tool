import React from "react";

/**
 * Labelled <select> styled for the dark theme.
 *
 * @param {string} label   Field label.
 * @param {string} value   Current value.
 * @param {function} onChange  Receives the new value.
 * @param {Array<{value:string,label:string}>} options  Option list.
 */
export default function Dropdown({ label, value, onChange, options }) {
  return (
    <label className="flex flex-col gap-1.5 text-sm">
      <span className="text-slate-400">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 outline-none transition focus:border-brand-accent"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}
