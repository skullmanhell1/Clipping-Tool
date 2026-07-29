import { useState } from "react";

/**
 * Saved settings profiles: quick-switch dropdown, save current config as a new
 * (or existing) profile, set default, and delete. Applying a profile pre-fills
 * all settings for the next run (handled by the parent via onApply).
 */
export default function ProfilesBar({
  profiles,
  defaultId,
  activeId,
  onApply,
  onSave,
  onSetDefault,
  onDelete,
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const active = profiles.find((p) => p.id === activeId);

  const run = (fn) => async (...args) => {
    setBusy(true);
    try {
      await fn(...args);
    } finally {
      setBusy(false);
    }
  };

  const handleSave = run(async () => {
    const targetName = name.trim() || active?.name;
    if (!targetName) return;
    // If the typed name matches the active profile, update it; else create new.
    const existing = profiles.find(
      (p) => p.name.toLowerCase() === targetName.toLowerCase()
    );
    await onSave(targetName, existing?.id || "");
    setName("");
  });

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-4">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold uppercase tracking-wider text-slate-400">
          Settings profiles
        </span>
        {active && (
          <span className="text-xs text-slate-500">
            Active: {active.name}
            {active.id === defaultId ? " (default)" : ""}
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          value={activeId || ""}
          disabled={busy}
          onChange={(e) => onApply(e.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 p-2 text-sm text-slate-100 outline-none focus:border-brand-accent"
        >
          <option value="">Select a profile…</option>
          {profiles.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
              {p.id === defaultId ? " ★" : ""}
            </option>
          ))}
        </select>

        <button
          type="button"
          disabled={busy || !activeId}
          onClick={run(() => onSetDefault(activeId))}
          className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:border-brand-accent disabled:opacity-40"
          title="Make the selected profile the default (pre-filled on load)"
        >
          ★ Default
        </button>
        <button
          type="button"
          disabled={busy || !activeId}
          onClick={run(() => onDelete(activeId))}
          className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-rose-300 hover:border-rose-700 disabled:opacity-40"
        >
          Delete
        </button>

        <div className="ml-auto flex items-center gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={active ? `Update "${active.name}" or new name` : "New profile name"}
            className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-brand-accent"
          />
          <button
            type="button"
            disabled={busy}
            onClick={handleSave}
            className="rounded-lg bg-brand px-3 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50"
          >
            Save current
          </button>
        </div>
      </div>
      <p className="mt-2 text-xs text-slate-500">
        Save the full current configuration (clip length, aspect, captions,
        effects, publishing) as a named profile. Selecting one pre-fills every
        setting for the next run.
      </p>
    </div>
  );
}
