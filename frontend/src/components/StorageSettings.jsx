import { useCallback, useEffect, useState } from "react";
import { api, formatBytes } from "../api.js";

const RETENTION_LABELS = {
  0: "Keep forever",
  7: "7 days",
  14: "14 days",
  30: "30 days",
  60: "60 days",
  90: "90 days",
};

/**
 * Storage settings group: disk usage + low-space warning, retention window,
 * temp auto-delete, delete-after-publish, and a manual cleanup action.
 */
export default function StorageSettings() {
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    try {
      setState(await api.storage());
    } catch {
      /* ignore transient errors */
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [load]);

  if (!state) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5 text-sm text-slate-400">
        Loading storage…
      </div>
    );
  }

  const { usage, settings, backend, retention_choices: choices } = state;
  const patch = async (change) => {
    setBusy(true);
    setMessage("");
    try {
      setState(await api.updateStorageSettings(change));
    } catch (e) {
      setMessage(e.message || "Update failed");
    } finally {
      setBusy(false);
    }
  };

  const cleanup = async () => {
    setBusy(true);
    setMessage("");
    try {
      const res = await api.cleanupStorage({ temp: true, expired: true });
      const removed = res.expired?.removed ?? 0;
      setMessage(`Cleaned ${removed} expired file(s) and temp scratch.`);
      await load();
    } catch (e) {
      setMessage(e.message || "Cleanup failed");
    } finally {
      setBusy(false);
    }
  };

  const usedPct = usage.used_percent;

  return (
    <div className="space-y-5 rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-slate-400">Storage</h3>
        <span className="rounded-full border border-slate-700 px-2 py-0.5 text-xs text-slate-400">
          backend: {backend}
        </span>
      </div>

      {/* Disk usage */}
      <div>
        <div className="mb-1 flex justify-between text-xs text-slate-400">
          <span>Disk usage</span>
          <span>
            {formatBytes(usage.used_bytes)} / {formatBytes(usage.total_bytes)} ·{" "}
            {formatBytes(usage.free_bytes)} free
          </span>
        </div>
        <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
          <div
            className={`h-full rounded-full ${usage.low_space ? "bg-rose-500" : "bg-emerald-500"}`}
            style={{ width: `${Math.min(100, usedPct)}%` }}
          />
        </div>
        <div className="mt-1 text-xs text-slate-500">
          Clips {formatBytes(usage.areas.clips)} · Sources {formatBytes(usage.areas.uploads)} · Temp{" "}
          {formatBytes(usage.areas.temp)}
        </div>
        {usage.low_space && (
          <div className="mt-2 rounded-lg border border-rose-800 bg-rose-950/40 p-2 text-xs text-rose-300">
            ⚠ Low disk space ({usage.free_gb} GB free). Consider a shorter retention window or
            running cleanup.
          </div>
        )}
      </div>

      {/* Retention */}
      <label className="block text-sm">
        <span className="text-slate-400">Keep clips for</span>
        <select
          value={settings.retention_days}
          disabled={busy}
          onChange={(e) => patch({ retention_days: Number(e.target.value) })}
          className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 p-2 text-slate-100 outline-none focus:border-brand-accent"
        >
          {(choices || [0, 7, 14, 30, 60, 90]).map((d) => (
            <option key={d} value={d}>
              {RETENTION_LABELS[d] || `${d} days`}
            </option>
          ))}
        </select>
        <span className="mt-1 block text-xs text-slate-500">
          {settings.retention_days === 0
            ? "Clips are never auto-deleted."
            : `Finished clips older than ${settings.retention_days} days are removed automatically. Source video is never auto-deleted.`}
        </span>
      </label>

      {/* Toggles */}
      <div className="space-y-2">
        <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={settings.auto_delete_temp}
            disabled={busy}
            onChange={(e) => patch({ auto_delete_temp: e.target.checked })}
            className="h-4 w-4 accent-emerald-500"
          />
          Auto-delete temp files after each job
        </label>
        <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={settings.delete_local_after_publish}
            disabled={busy}
            onChange={(e) => patch({ delete_local_after_publish: e.target.checked })}
            className="h-4 w-4 accent-emerald-500"
          />
          Delete local clip copy after publishing
        </label>
      </div>

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={cleanup}
          disabled={busy}
          className="rounded-lg bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600 disabled:opacity-50"
        >
          Clean up now
        </button>
        {message && <span className="text-xs text-slate-400">{message}</span>}
      </div>
    </div>
  );
}

// This panel takes no props. Everything it shows comes from `/api/storage`, which it polls itself,
// and every change it makes goes straight back there — so there is no boundary here to validate.
// Declared empty rather than omitted, so "no props" is a statement someone made rather than a
// block that was forgotten.
StorageSettings.propTypes = {};
