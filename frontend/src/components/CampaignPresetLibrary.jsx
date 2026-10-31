import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

function presetMeta(preset) {
  const approved = (preset.source_library || []).filter((source) => source.status === "approved").length;
  return [
    preset.length?.raw || preset.settings?.clip_length || "Auto length",
    (preset.aspect_resolution?.aspect_ratios || []).join(", ") || "Any format",
    ((preset.platforms || preset.posting?.platforms || [])).join(", ") || "Any platform",
    `${approved} approved source${approved === 1 ? "" : "s"}`,
  ];
}

export default function CampaignPresetLibrary({ onPresetApplied, refreshToken = 0 }) {
  const [presets, setPresets] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await api.campaignPresets();
      setPresets(Array.isArray(data?.presets) ? data.presets : data?.profiles || []);
      setSelectedId(data?.selected_id || "");
      setError("");
    } catch (loadError) {
      setError(loadError.message || "Could not load saved campaign presets.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshToken]);

  const apply = async (preset) => {
    setBusy(preset.id);
    setMessage("");
    setError("");
    try {
      const result = await api.selectCampaignPreset(preset.id);
      const applied = result?.preset || result?.profile || preset;
      setSelectedId(preset.id);
      onPresetApplied?.(applied);
      setMessage(`Applied “${applied.name || preset.name || "campaign preset"}” to the next job.`);
    } catch (applyError) {
      setError(applyError.message || "Could not apply this campaign preset.");
    } finally {
      setBusy("");
    }
  };

  const duplicate = async (preset) => {
    setBusy(`duplicate-${preset.id}`);
    setMessage("");
    setError("");
    try {
      const result = await api.duplicateCampaignPreset(preset.id);
      const copy = result?.preset || result?.profile || result;
      if (copy?.id) {
        setPresets((current) => [copy, ...current]);
        setMessage(`Duplicated as “${copy.name || "campaign copy"}”. Select it when you are ready to edit or apply it.`);
      }
    } catch (duplicateError) {
      setError(duplicateError.message || "Could not duplicate this campaign preset.");
    } finally {
      setBusy("");
    }
  };

  const remove = async (preset) => {
    if (!window.confirm(`Delete the saved campaign preset “${preset.name || preset.id}”?`)) return;
    setBusy(`delete-${preset.id}`);
    setMessage("");
    setError("");
    try {
      await api.deleteCampaignPreset(preset.id);
      setPresets((current) => current.filter((item) => item.id !== preset.id));
      if (selectedId === preset.id) setSelectedId("");
      setMessage("Campaign preset deleted.");
    } catch (deleteError) {
      setError(deleteError.message || "Could not delete this campaign preset.");
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="rounded-2xl border border-cyan-400/20 bg-cyan-400/[0.035] p-4 shadow-xl shadow-cyan-950/10">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-cyan-200">
            Campaign library
          </p>
          <h2 className="mt-1 text-lg font-semibold text-white">Saved campaign presets</h2>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">
            Apply any saved campaign to the next job, or duplicate one as a starting point. Each
            preset keeps its own rules, sources, captions, and posting policy.
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          className="rounded-lg border border-slate-700 px-3 py-2 text-xs font-semibold text-slate-300 hover:border-cyan-300/40 hover:text-white"
        >
          Refresh
        </button>
      </div>

      {presets.length === 0 ? (
        <div className="mt-4 rounded-xl border border-dashed border-cyan-300/25 bg-slate-950/40 p-4 text-sm text-slate-300">
          No saved campaign presets yet. Load a brief in the next section, then choose
          <span className="font-semibold text-cyan-100"> Save as campaign preset</span> to keep it
          here.
        </div>
      ) : (
        <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {presets.map((preset) => {
            const active = selectedId === preset.id;
            return (
              <article
                key={preset.id}
                className={`flex flex-col rounded-xl border p-4 ${
                  active
                    ? "border-cyan-300/50 bg-cyan-400/[0.07]"
                    : "border-slate-800 bg-slate-950/50"
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <h3 className="truncate text-sm font-semibold text-white">
                      {preset.name || "Untitled campaign"}
                    </h3>
                    <p className="mt-1 text-[10px] uppercase tracking-wide text-slate-500">
                      {preset.posting?.manual_only || preset.posting?.review_only
                        ? "Human review required"
                        : "Review before publishing"}
                    </p>
                  </div>
                  {active ? (
                    <span className="shrink-0 rounded-full border border-cyan-300/30 bg-cyan-400/10 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-cyan-100">
                      Active
                    </span>
                  ) : null}
                </div>

                <ul className="mt-3 flex flex-wrap gap-1.5 text-[10px] text-slate-400">
                  {presetMeta(preset).map((item) => (
                    <li key={item} className="rounded-full border border-slate-800 bg-slate-900/80 px-2 py-1">
                      {item}
                    </li>
                  ))}
                </ul>

                <div className="mt-4 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => apply(preset)}
                    disabled={busy === preset.id}
                    className="rounded-lg bg-cyan-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-cyan-500 disabled:opacity-40"
                  >
                    {busy === preset.id ? "Applying…" : active ? "Apply again" : "Apply"}
                  </button>
                  <button
                    type="button"
                    onClick={() => duplicate(preset)}
                    disabled={busy === `duplicate-${preset.id}`}
                    className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-cyan-300/40 disabled:opacity-40"
                  >
                    {busy === `duplicate-${preset.id}` ? "Duplicating…" : "Duplicate"}
                  </button>
                  <button
                    type="button"
                    onClick={() => remove(preset)}
                    disabled={busy === `delete-${preset.id}`}
                    className="rounded-lg border border-rose-400/20 px-3 py-1.5 text-xs text-rose-200 hover:bg-rose-400/10 disabled:opacity-40"
                  >
                    {busy === `delete-${preset.id}` ? "Deleting…" : "Delete"}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}

      {message ? <p role="status" className="mt-3 text-xs text-emerald-300">{message}</p> : null}
      {error ? <p role="alert" className="mt-3 text-xs text-rose-300">{error}</p> : null}
    </section>
  );
}
