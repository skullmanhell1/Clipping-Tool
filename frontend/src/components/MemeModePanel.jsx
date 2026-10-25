import { useMemo, useState } from "react";
import { api } from "../api.js";

const PACKS = [
  { id: "reaction", label: "Reaction", detail: "Pop, click, air horn, record scratch" },
  { id: "classic", label: "Classic internet", detail: "Whoosh, swipe, alert, hit (local or generated)" },
  { id: "gaming", label: "Gaming", detail: "UI ticks, level-up, impact (local or generated)" },
];

const SFX_LIBRARY = [
  { id: "pop", label: "Pop", tags: "reaction short" },
  { id: "click", label: "Click", tags: "ui short" },
  { id: "whoosh", label: "Whoosh", tags: "transition movement" },
  { id: "swipe", label: "Swipe", tags: "transition movement" },
  { id: "record_scratch", label: "Record scratch", tags: "reaction stop" },
  { id: "airhorn", label: "Air horn", tags: "reaction loud" },
  { id: "vine_boom", label: "Boom", tags: "impact dramatic" },
  { id: "dramatic_sting", label: "Dramatic sting", tags: "reveal impact" },
  { id: "crickets", label: "Crickets", tags: "awkward silence" },
  { id: "ding", label: "Ding", tags: "success notification" },
  { id: "bass_drop", label: "Bass drop", tags: "gaming impact" },
];

const SUGGESTIONS = [
  { type: "sfx", asset: "pop", time: 1.8, label: "Pop on the first punchline" },
  { type: "overlay", effect: "text", payload: { text: "REACTION", position: "center", scale: 0.08, animate: "pop" }, time: 4.2, label: "Add a reaction sticker" },
  { type: "effect", asset: "punch_in", time: 7.5, label: "Punch in on the reveal" },
];

const uid = () => `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;

function Field({ label, hint, children }) {
  return (
    <label className="flex flex-col gap-1.5 text-sm">
      <span className="text-slate-400">{label}</span>
      {children}
      {hint ? <span className="text-[11px] leading-4 text-slate-500">{hint}</span> : null}
    </label>
  );
}

function eventLabel(event) {
  return event.label || event.asset || event.effect || event.type || "Timeline event";
}

export default function MemeModePanel({ settings, onChange }) {
  const [undoStack, setUndoStack] = useState([]);
  const [search, setSearch] = useState("");
  const [suggestions, setSuggestions] = useState(() =>
    SUGGESTIONS.map((item) => ({ ...item, id: uid(), selected: true })),
  );
  const [uploadedNames, setUploadedNames] = useState([]);
  const [uploadedOverlays, setUploadedOverlays] = useState([]);
  const [uploadingAssets, setUploadingAssets] = useState(false);
  const [assetError, setAssetError] = useState("");

  const events = Array.isArray(settings.meme_events)
    ? settings.meme_events
    : Array.isArray(settings.meme_timeline_events)
      ? settings.meme_timeline_events
      : [];
  const filteredSfx = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return SFX_LIBRARY;
    return SFX_LIBRARY.filter((item) => `${item.label} ${item.tags}`.toLowerCase().includes(query));
  }, [search]);

  const commit = (next) => {
    setUndoStack((current) => [...current.slice(-9), events]);
    onChange({ ...settings, meme_events: next, meme_timeline_events: next });
  };

  const updateEvent = (id, patch) =>
    commit(events.map((event) => (event.id === id ? { ...event, ...patch } : event)));

  const addEvent = (patch = {}) => {
    commit([
      ...events,
      {
        id: uid(),
        kind: "sfx",
        type: "sfx",
        asset: settings.meme_sfx_name || "pop",
        label: "New meme beat",
        time: 0,
        duration: 0.6,
        volume: Number(settings.meme_sfx_volume ?? 0.35),
        duck: Number(settings.meme_default_duck ?? settings.meme_ducking ?? 0.65),
        ducking: Number(settings.meme_default_duck ?? settings.meme_ducking ?? 0.65),
        ...patch,
      },
    ]);
  };

  const removeEvent = (id) => commit(events.filter((event) => event.id !== id));

  const undo = () => {
    const previous = undoStack.at(-1);
    if (!previous) return;
    setUndoStack((current) => current.slice(0, -1));
    onChange({ ...settings, meme_events: previous, meme_timeline_events: previous });
  };

  const reviewSuggestions = () => {
    const approved = suggestions.filter((item) => item.selected);
    if (approved.length) {
      commit([
        ...events,
        ...approved.map(({ id: _id, selected: _selected, ...item }) => ({
          ...item,
          id: uid(),
          duration: item.type === "effect" ? 1.2 : 0.6,
          kind: item.type === "sfx" ? "sfx" : item.type === "overlay" ? "overlay" : "visual",
          volume: Number(settings.meme_default_volume ?? settings.meme_sfx_volume ?? 0.35),
          duck: Number(settings.meme_default_duck ?? settings.meme_ducking ?? 0.65),
          ducking: Number(settings.meme_default_duck ?? settings.meme_ducking ?? 0.65),
        })),
      ]);
    }
    setSuggestions([]);
  };

  const selectSfx = (asset) => {
    const nextEvents = [
      ...events,
      {
        id: uid(),
        kind: "sfx",
        type: "sfx",
        asset,
        label: SFX_LIBRARY.find((item) => item.id === asset)?.label || asset,
        time: 0,
        duration: 0.6,
        volume: Number(settings.meme_default_volume ?? settings.meme_sfx_volume ?? 0.35),
        duck: Number(settings.meme_default_duck ?? settings.meme_ducking ?? 0.65),
        ducking: Number(settings.meme_default_duck ?? settings.meme_ducking ?? 0.65),
      },
    ];
    setUndoStack((current) => [...current.slice(-9), events]);
    onChange({ ...settings, meme_sfx_name: asset, meme_events: nextEvents, meme_timeline_events: nextEvents });
  };

  const selectOverlay = (asset) => {
    addEvent({
      kind: "overlay",
      type: "overlay",
      asset,
      effect: asset,
      label: asset,
      time: 0,
      duration: 1.0,
    });
  };

  return (
    <section className="rounded-2xl border border-fuchsia-400/20 bg-fuchsia-400/[0.04] p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-fuchsia-200">Optional edit layer</p>
          <h3 className="mt-1 text-lg font-semibold text-white">Meme mode</h3>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">
            Build a reviewable beat sheet for reactions, sound effects and visual punch-ins. Nothing is added to a render until you turn this on.
          </p>
        </div>
        <label className="flex items-center gap-2 rounded-full border border-fuchsia-300/20 bg-fuchsia-400/10 px-3 py-2 text-xs font-semibold text-fuchsia-100">
          <input
            type="checkbox"
             checked={!!(settings.meme_mode || settings.meme_mode_enabled)}
             onChange={(event) => onChange({ ...settings, meme_mode: event.target.checked, meme_mode_enabled: event.target.checked })}
            className="accent-fuchsia-400"
          />
          Enable meme mode
        </label>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_1fr]">
        <div className="space-y-4">
          <div>
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Choose a pack</p>
            <div className="grid gap-2 sm:grid-cols-3">
              {PACKS.map((pack) => (
                <button
                  key={pack.id}
                  type="button"
                   aria-pressed={(settings.meme_pack || "reaction") === pack.id}
                   onClick={() => onChange({
                     ...settings,
                     meme_pack: pack.id,
                     meme_preset: pack.id === "reaction" ? "chaotic" : pack.id === "classic" ? "subtle" : "gaming_rage",
                   })}
                  className={`rounded-xl border p-3 text-left transition ${settings.meme_pack === pack.id ? "border-fuchsia-300/50 bg-fuchsia-400/10" : "border-slate-700 bg-slate-950/40 hover:border-slate-500"}`}
                >
                  <span className="block text-xs font-semibold text-slate-100">{pack.label}</span>
                  <span className="mt-1 block text-[10px] leading-4 text-slate-500">{pack.detail}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <p className="text-xs font-semibold text-slate-200">SFX library</p>
               <p className="mt-1 text-[11px] text-slate-500">Generated tones and local/user-supplied sounds only; no copyrighted assets are downloaded. Uploaded files remain local and are marked with unknown license provenance until you add a manifest.</p>
              </div>
              <label className="cursor-pointer rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 hover:border-fuchsia-300/40">
                Upload SFX
                <input
                  type="file"
                  accept="audio/*"
                  multiple
                  className="sr-only"
                  onChange={async (event) => {
                    const files = Array.from(event.target.files || []);
                    if (!files.length) return;
                    setUploadingAssets(true);
                    setAssetError("");
                    try {
                      const result = await api.uploadMemeAssets(files, "sfx");
                      const names = (result.assets || []).map((asset) => asset.id || asset.name);
                      setUploadedNames((current) => [...new Set([...current, ...names])]);
                    } catch (uploadError) {
                      setAssetError(uploadError.message || "SFX upload failed.");
                    } finally {
                      setUploadingAssets(false);
                      event.target.value = "";
                    }
                  }}
                />
              </label>
              <label className="cursor-pointer rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 hover:border-fuchsia-300/40">
                Upload overlay
                <input
                  type="file"
                  accept="image/*,video/*"
                  multiple
                  className="sr-only"
                  onChange={async (event) => {
                    const files = Array.from(event.target.files || []);
                    if (!files.length) return;
                    setUploadingAssets(true);
                    setAssetError("");
                    try {
                       const result = await api.uploadMemeAssets(files, "overlay");
                       const names = (result.assets || []).map((asset) => asset.id || asset.name);
                       setUploadedOverlays((current) => [...new Set([...current, ...names])]);
                    } catch (uploadError) {
                      setAssetError(uploadError.message || "Overlay upload failed.");
                    } finally {
                      setUploadingAssets(false);
                      event.target.value = "";
                    }
                  }}
                />
              </label>
              {uploadingAssets ? <span className="text-[11px] text-fuchsia-200">Uploading…</span> : null}
            </div>
            {assetError ? <p className="mt-2 text-[11px] text-rose-300">{assetError}</p> : null}
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search pop, whoosh, reaction…"
              className="mt-3 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-fuchsia-300"
              aria-label="Search SFX library"
            />
            <div className="mt-2 flex flex-wrap gap-2">
              {filteredSfx.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => selectSfx(item.id)}
                  className={`rounded-full border px-2.5 py-1 text-xs ${settings.meme_sfx_name === item.id ? "border-fuchsia-300 text-fuchsia-100" : "border-slate-700 text-slate-400 hover:border-slate-500"}`}
                >
                  {item.label}
                </button>
              ))}
              {uploadedNames.map((name) => (
                <button key={name} type="button" onClick={() => selectSfx(name)} className="rounded-full border border-emerald-400/30 px-2.5 py-1 text-xs text-emerald-200">
                  {name}
                </button>
              ))}
            </div>
            {uploadedOverlays.length ? (
              <div className="mt-3 border-t border-slate-800 pt-3">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Uploaded overlays</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {uploadedOverlays.map((name) => (
                    <button key={name} type="button" onClick={() => selectOverlay(name)} className="rounded-full border border-emerald-400/30 px-2.5 py-1 text-xs text-emerald-200">
                      Add {name}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
          </div>

           <div className="grid gap-4 sm:grid-cols-3">
            <Field label={`Intensity · ${Math.round((Number(settings.meme_intensity_value) || 0.55) * 100)}%`}>
              <input type="range" min="0" max="1" step="0.05" value={settings.meme_intensity_value ?? 0.55} onChange={(event) => onChange({ ...settings, meme_intensity_value: Number(event.target.value) })} className="accent-fuchsia-400" />
            </Field>
            <Field label={`SFX volume · ${Math.round((Number(settings.meme_sfx_volume) || 0) * 100)}%`}>
              <input type="range" min="0" max="1" step="0.05" value={settings.meme_sfx_volume ?? 0.35} onChange={(event) => onChange({ ...settings, meme_sfx_volume: Number(event.target.value) })} className="accent-fuchsia-400" />
            </Field>
             <Field label={`Audio ducking · ${Math.round((Number(settings.meme_default_duck ?? settings.meme_ducking) || 0) * 100)}%`} hint="Briefly lowers the original bed under the SFX.">
               <input type="range" min="0" max="1" step="0.05" value={settings.meme_default_duck ?? settings.meme_ducking ?? 0.65} onChange={(event) => onChange({ ...settings, meme_default_duck: Number(event.target.value), meme_ducking: Number(event.target.value) })} className="accent-fuchsia-400" />
            </Field>
           </div>
           <label className="flex items-center gap-2 rounded-lg border border-fuchsia-300/15 bg-fuchsia-400/[0.04] p-2.5 text-xs text-slate-300">
             <input
               type="checkbox"
               checked={!!settings.meme_auto_suggest}
               onChange={(event) => onChange({ ...settings, meme_auto_suggest: event.target.checked })}
               className="accent-fuchsia-400"
             />
             Ask the renderer to suggest beats from transcript keywords, energy peaks, silence and scene cuts
           </label>
           {settings.permissibility_mode && (settings.meme_mode || settings.meme_mode_enabled) ? (
            <p className="rounded-lg border border-amber-400/20 bg-amber-400/[0.06] p-2 text-[11px] leading-5 text-amber-200">
              Permissibility mode may suppress added audio during rendering. Review the event list after the backend confirms asset availability.
            </p>
          ) : null}
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="text-xs font-semibold text-slate-200">Exact timeline</p>
              <p className="mt-1 text-[11px] text-slate-500">Seconds are clip-relative. Edit a card, then review before render.</p>
            </div>
            <div className="flex gap-2">
              <button type="button" onClick={undo} disabled={!undoStack.length} className="rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 disabled:opacity-40">Undo</button>
              <button type="button" onClick={() => addEvent()} className="rounded-lg bg-fuchsia-600 px-2.5 py-1.5 text-xs font-semibold text-white hover:bg-fuchsia-500">Add event</button>
            </div>
          </div>
          <div className="mt-3 space-y-2">
            {events.length === 0 ? <p className="rounded-lg border border-dashed border-slate-700 p-3 text-xs text-slate-500">No events yet. Pick an SFX or add a card to start the beat sheet.</p> : null}
            {events.map((event) => (
              <div key={event.id} className="rounded-lg border border-slate-800 bg-slate-900/80 p-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-xs font-semibold text-slate-200">{eventLabel(event)}</span>
                  <button type="button" onClick={() => removeEvent(event.id)} className="text-xs text-slate-500 hover:text-rose-300" aria-label={`Remove ${eventLabel(event)}`}>Remove</button>
                </div>
                <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
                   <label className="text-[10px] text-slate-500">Type<select value={event.kind || event.type || "sfx"} onChange={(e) => updateEvent(event.id, { kind: e.target.value, type: e.target.value })} className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-1.5 py-1 text-xs text-slate-200"><option value="sfx">SFX</option><option value="overlay">Overlay</option><option value="visual">Effect</option><option value="caption">Caption</option></select></label>
                   <label className="text-[10px] text-slate-500">At (sec)<input type="number" min="0" step="0.1" value={event.start ?? event.time ?? 0} onChange={(e) => updateEvent(event.id, { start: Number(e.target.value), time: Number(e.target.value) })} className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-1.5 py-1 text-xs text-slate-200" /></label>
                   <label className="text-[10px] text-slate-500">Length<input type="number" min="0.1" step="0.1" value={event.duration ?? 0.6} onChange={(e) => updateEvent(event.id, { duration: Number(e.target.value), end: Number(event.start ?? event.time ?? 0) + Number(e.target.value) })} className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-1.5 py-1 text-xs text-slate-200" /></label>
                   <label className="text-[10px] text-slate-500">Asset/effect<input value={event.asset || event.effect || ""} onChange={(e) => updateEvent(event.id, { asset: e.target.value, effect: e.target.value })} className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-1.5 py-1 text-xs text-slate-200" /></label>
                </div>
              </div>
            ))}
          </div>
          {suggestions.length > 0 ? (
            <div className="mt-3 rounded-lg border border-fuchsia-400/20 bg-fuchsia-400/[0.04] p-2.5">
              <div className="flex items-center justify-between gap-2"><p className="text-xs font-semibold text-fuchsia-100">Auto-suggest review</p><button type="button" onClick={reviewSuggestions} className="rounded bg-fuchsia-500/20 px-2 py-1 text-[11px] font-semibold text-fuchsia-100">Add selected</button></div>
              <div className="mt-2 space-y-1.5">{suggestions.map((item) => <label key={item.id} className="flex items-center gap-2 text-[11px] text-slate-300"><input type="checkbox" checked={item.selected} onChange={(event) => setSuggestions((current) => current.map((entry) => entry.id === item.id ? { ...entry, selected: event.target.checked } : entry))} className="accent-fuchsia-400" /><span>{item.time.toFixed(1)}s · {item.label}</span></label>)}</div>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
