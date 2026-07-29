import React from "react";

/**
 * Labelled <select> styled for the dark theme.
 *
 * @param {string} label   Field label.
 * @param {string} value   Current value.
 * @param {function} onChange  Receives the new value.
 * @param {Array<{value:string,label:string,disabled?:boolean}>} options  Option list. A
 *   `disabled` option stays visible but unselectable, which is how a mode that exists but is
 *   unavailable on this install is shown with its reason rather than silently hidden.
 * @param {boolean} [disabled]  Disable the whole control.
 */
export default function Dropdown({ label, value, onChange, options, disabled }) {
  return (
    <label className="flex flex-col gap-1.5 text-sm">
      <span className="text-slate-400">{label}</span>
      <select
        value={value}
        disabled={!!disabled}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 outline-none transition focus:border-brand-accent disabled:cursor-not-allowed disabled:opacity-60"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value} disabled={!!o.disabled}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}
