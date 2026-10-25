import Dropdown from "./Dropdown.jsx";

export default function FacialTrackingPanel({ settings, onChange, effects = null }) {
  const detectors = Array.isArray(effects?.face_detectors) && effects.face_detectors.length
    ? effects.face_detectors.map((item) => ({ value: item.name, label: item.available === false ? `${item.name} · unavailable` : item.name }))
    : [{ value: "haar", label: "Haar cascade · default" }, { value: "mediapipe", label: "MediaPipe BlazeFace" }];
  const set = (key) => (value) => onChange({ ...settings, [key]: value });
  const split = settings.reframe_layout === "split_screen";

  return (
    <section className="rounded-2xl border border-emerald-400/20 bg-emerald-400/[0.035] p-5">
      <div>
        <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-emerald-200">Speaker framing</p>
        <h3 className="mt-1 text-lg font-semibold text-white">Facial tracking</h3>
        <p className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">Tune who the crop follows, how quickly it moves, and what happens when a face is uncertain.</p>
      </div>
      <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <label className="flex items-center gap-2 rounded-xl border border-slate-800 bg-slate-950/50 p-3 text-sm text-slate-200"><input type="checkbox" checked={!!settings.reframe} onChange={(event) => onChange({ ...settings, reframe: event.target.checked })} className="accent-emerald-400" />Enable face tracking</label>
        <label className="flex items-center gap-2 rounded-xl border border-slate-800 bg-slate-950/50 p-3 text-sm text-slate-200"><input type="checkbox" checked={!!settings.speaker_reframe} onChange={(event) => onChange({ ...settings, speaker_reframe: event.target.checked, diarization: event.target.checked ? true : settings.diarization })} className="accent-emerald-400" />Follow active speaker</label>
        <label className="flex items-center gap-2 rounded-xl border border-slate-800 bg-slate-950/50 p-3 text-sm text-slate-200"><input type="checkbox" checked={!!settings.diarization} onChange={(event) => onChange({ ...settings, diarization: event.target.checked })} className="accent-emerald-400" />Detect speaker turns</label>
        <Dropdown label="Active speaker layout" value={settings.reframe_layout || "follow_active"} onChange={set("reframe_layout")} options={[{ value: "follow_active", label: "Follow active speaker" }, { value: "split_screen", label: "Split screen" }]} />
        <Dropdown label="Smoothing" value={settings.reframe_zero_phase === false ? "causal" : "zero_phase"} onChange={(value) => onChange({ ...settings, reframe_zero_phase: value === "zero_phase" })} options={[{ value: "zero_phase", label: "Zero-phase · less lag" }, { value: "causal", label: "Causal · lower look-ahead" }]} />
        <Dropdown label="Face detector" value={settings.face_detector || "haar"} onChange={set("face_detector")} options={detectors} />
        <label className="flex flex-col gap-1.5 text-sm"><span className="text-slate-400">Confidence floor · {Math.round((Number(settings.face_detector_min_score) || 0.5) * 100)}%</span><input type="range" min="0" max="1" step="0.05" value={settings.face_detector_min_score ?? 0.5} onChange={(event) => onChange({ ...settings, face_detector_min_score: Number(event.target.value) })} className="accent-emerald-400" /><span className="text-[11px] text-slate-500">MediaPipe only; Haar does not expose confidence.</span></label>
         <Dropdown label="Low-confidence fallback" value={settings.facial_fallback || "static_crop_blur"} onChange={set("facial_fallback")} options={[{ value: "static_crop_blur", label: "Blurred static crop" }, { value: "static_crop", label: "Static center crop" }, { value: "none", label: "Letterbox / no crop" }]} />
      </div>
      {split ? <div className="mt-3 grid gap-4 sm:grid-cols-2"><label className="flex flex-col gap-1.5 text-sm"><span className="text-slate-400">Split-screen regions</span><input type="number" min="2" max="4" value={settings.split_screen_max_regions ?? 2} onChange={(event) => onChange({ ...settings, split_screen_max_regions: Number(event.target.value) })} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100" /></label><label className="flex items-center gap-2 rounded-xl border border-slate-800 bg-slate-950/50 p-3 text-sm text-slate-300"><input type="checkbox" checked={settings.reframe_reset_on_cut !== false} onChange={(event) => onChange({ ...settings, reframe_reset_on_cut: event.target.checked })} className="accent-emerald-400" />Reset tracking at shot cuts</label></div> : null}
       <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/40 p-2.5 text-[11px] leading-5 text-slate-500">Face tracking adds decode/detection work. Confidence filtering, zero-phase smoothing, shot-cut resets, split-screen limits, active-speaker association, and safe fallbacks are captured per job; if detection is unavailable the existing static crop/blur ladder remains in charge.</div>
    </section>
  );
}
