import Dropdown from "./Dropdown.jsx";

const RESOLUTIONS = [
  { value: 720, label: "720p · faster" },
  { value: 1080, label: "1080p · recommended" },
  { value: 1440, label: "1440p · source permitting" },
  { value: 2160, label: "2160p / 4K · heavy" },
];

const setField = (settings, onChange, key) => (value) => onChange({ ...settings, [key]: value });

export default function QualitySettingsPanel({ settings, onChange, outputProfiles = [] }) {
  const resolution = Number(settings.output_short_side || 1080);
  const qualityWarning = resolution >= 1440
    ? "Higher output sizes can increase render time and file size; the source still limits real detail."
    : "Upscaling cannot recreate detail that is not present in the source.";
  const encoderOptions = [
    { value: "libx264", label: "CPU · libx264 (reliable)" },
    { value: "auto", label: "Auto-detect hardware" },
    { value: "h264_nvenc", label: "NVIDIA NVENC" },
    { value: "h264_qsv", label: "Intel Quick Sync" },
    { value: "h264_videotoolbox", label: "Apple VideoToolbox" },
    { value: "h264_vaapi", label: "Linux VAAPI" },
  ];

  return (
    <section className="rounded-2xl border border-cyan-400/20 bg-cyan-400/[0.035] p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-cyan-200">Render controls</p>
          <h3 className="mt-1 text-lg font-semibold text-white">Quality &amp; delivery</h3>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-slate-400">Choose the output target without hiding the trade-offs between fidelity, speed and file size.</p>
        </div>
        <label className="flex items-center gap-2 rounded-full border border-cyan-300/20 bg-cyan-400/10 px-3 py-2 text-xs font-semibold text-cyan-100"><input type="checkbox" checked={!!settings.quality_enabled} onChange={(event) => onChange({ ...settings, quality_enabled: event.target.checked })} className="accent-cyan-400" />Enable quality delivery</label>
        {outputProfiles.length > 0 ? <span className="rounded-full border border-cyan-300/20 bg-cyan-400/10 px-2.5 py-1 text-[10px] text-cyan-100">Server profiles available</span> : null}
      </div>
      <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Dropdown label="Output resolution" value={resolution} onChange={setField(settings, onChange, "output_short_side")} options={RESOLUTIONS} />
        <Dropdown label="Upscale" value={settings.quality_upscale || "off"} onChange={setField(settings, onChange, "quality_upscale")} options={[{ value: "off", label: "Off · preserve source" }, { value: "auto", label: "Auto · resize only" }, { value: "2x", label: "2× · resize only" }]} />
        <Dropdown label="Speech denoise" value={settings.speech_denoise || "off"} onChange={setField(settings, onChange, "speech_denoise")} options={[{ value: "off", label: "Off" }, { value: "light", label: "Light" }, { value: "standard", label: "Standard" }, { value: "strong", label: "Strong" }]} />
        <Dropdown label="Sharpen" value={settings.quality_sharpen || "off"} onChange={setField(settings, onChange, "quality_sharpen")} options={[{ value: "off", label: "Off" }, { value: "subtle", label: "Subtle" }, { value: "standard", label: "Standard" }]} />
        <Dropdown label="Colour" value={settings.color || ""} onChange={setField(settings, onChange, "color")} options={[{ value: "", label: "No grade" }, { value: "vivid", label: "Vivid" }, { value: "warm", label: "Warm" }, { value: "cool", label: "Cool" }, { value: "cinematic", label: "Cinematic" }, { value: "bw", label: "Black & white" }]} />
        <label className="flex items-center gap-2 rounded-xl border border-slate-800 bg-slate-950/50 p-3 text-sm text-slate-200"><input type="checkbox" checked={!!settings.quality_colour_correction} onChange={(event) => onChange({ ...settings, quality_colour_correction: event.target.checked })} className="accent-cyan-400" />Mild delivery correction</label>
        <Dropdown label="Frame-rate policy" value={settings.frame_rate_policy || "auto"} onChange={setField(settings, onChange, "frame_rate_policy")} options={[{ value: "auto", label: "Auto · preserve CFR" }, { value: "always", label: "Always normalize" }]} />
        <Dropdown label="Interpolation" value={settings.quality_interpolation || "off"} onChange={setField(settings, onChange, "quality_interpolation")} options={[{ value: "off", label: "Off" }, { value: "blend", label: "Blend" }, { value: "motion", label: "Motion compensated" }]} />
        <Dropdown label="Hardware encoder" value={settings.video_encoder || "libx264"} onChange={setField(settings, onChange, "video_encoder")} options={encoderOptions} />
      </div>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1.5 text-sm"><span className="text-slate-400">x264 quality · CRF {settings.x264_crf ?? 20}</span><input type="range" min="16" max="30" step="1" value={settings.x264_crf ?? 20} onChange={(event) => onChange({ ...settings, x264_crf: Number(event.target.value) })} className="accent-cyan-400" /><span className="text-[11px] text-slate-500">Lower is higher quality and usually larger. This only affects the CPU x264 path.</span></label>
        <label className="flex flex-col gap-1.5 text-sm"><span className="text-slate-400">Scaler</span><select value={settings.scaler_flags || "bicubic"} onChange={(event) => onChange({ ...settings, scaler_flags: event.target.value })} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100"><option value="bicubic">Bicubic · default</option><option value="lanczos">Lanczos · sharper edges</option><option value="bilinear">Bilinear · faster/softer</option><option value="area">Area · downscale</option></select><span className="text-[11px] text-slate-500">Lanczos is not automatically better on every source.</span></label>
      </div>
      <div className="mt-4 flex flex-wrap gap-3">
        <label className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2 text-xs text-slate-300"><input type="checkbox" checked={!!settings.quality_audio_loudness} onChange={(event) => onChange({ ...settings, quality_audio_loudness: event.target.checked })} className="accent-cyan-400" />Measured platform loudness</label>
        <label className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2 text-xs text-slate-300"><input type="checkbox" checked={!!settings.quality_voice_clarity} onChange={(event) => onChange({ ...settings, quality_voice_clarity: event.target.checked })} className="accent-cyan-400" />Voice clarity</label>
      </div>
      <p className="mt-3 rounded-lg border border-amber-400/20 bg-amber-400/[0.05] p-2.5 text-[11px] leading-5 text-amber-200">{qualityWarning}</p>
       <p className="mt-2 text-[11px] leading-5 text-slate-500">Quality controls are now part of the per-job request. Unsupported filters or hardware encoders degrade to a documented marker and the CPU path; upscaling can resize pixels but cannot recreate source detail.</p>
    </section>
  );
}
