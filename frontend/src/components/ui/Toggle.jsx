/**
 * A labelled checkbox with an optional hint line.
 *
 * This was defined *inside* `SettingsPanel.jsx` and never exported, so the two other panels that
 * needed the same control could not have it: `PublishingPanel` and `StorageSettings` each
 * re-styled a bare `<input type="checkbox">` by hand, and `ClipCard` did it twice more. Four
 * spellings of one control, differing in the details that matter for a checkbox — whether the
 * label is clickable, whether the disabled state is visible, whether the hint is associated with
 * the input at all.
 *
 * Wrapping the input in the `<label>` rather than pairing them by `id` is deliberate: it makes the
 * whole row a hit target without needing a generated id per instance, and it is what keeps the
 * accessible name correct when the hint is present.
 *
 * `checked` and `disabled` are coerced with `!!` because callers pass settings values straight
 * through, and a setting absent from an older saved profile arrives as `undefined` — which React
 * treats as "uncontrolled" and then warns about on the first interaction.
 */
export default function Toggle({ label, checked, onChange, hint, disabled }) {
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
