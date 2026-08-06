/**
 * U6: the brand kit — font, colour pair, logo and standing call to action.
 *
 * These lived in places that could not be saved together: the caption font and colours inside a
 * preset editable only in source, the CTA regenerated per clip by the LLM (so a creator with one
 * standing ask got a different wording on every clip), and no way to put a logo on a clip at all.
 *
 * The kit is part of `settings`, which is what makes it persist: saved profiles store the whole
 * settings blob, so a kit is saved, applied and set as default by machinery that already exists
 * rather than by a parallel system of its own.
 *
 * The logo is a **server-side path**, not an upload. This is a self-hosted tool whose renders
 * already read local files (fonts, music, b-roll), and adding an upload endpoint for one image
 * would mean a storage location, a cleanup policy and a retention rule — none of which exist for
 * assets. A path is honest about where the file has to be.
 */

const inputClass =
  "mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 p-2 text-sm text-slate-100 outline-none focus:border-brand-accent";

const LOGO_POSITIONS = [
  ["top_left", "Top left"],
  ["top_right", "Top right"],
  ["bottom_left", "Bottom left"],
  ["bottom_right", "Bottom right"],
];

function Row({ label, hint, children }) {
  return (
    <label className="block text-xs text-slate-400">
      <span className="font-medium text-slate-300">{label}</span>
      {children}
      {hint ? (
        <span className="mt-1 block text-[10px] leading-snug text-slate-500">{hint}</span>
      ) : null}
    </label>
  );
}

export default function BrandKitPanel({ settings, onChange, fonts = [] }) {
  const set = (key) => (value) => onChange({ ...settings, [key]: value });

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900/50 p-4">
      <header className="mb-3">
        <h3 className="text-sm font-semibold text-slate-100">Brand kit</h3>
        <p className="mt-0.5 text-[11px] leading-snug text-slate-500">
          Applied on top of whichever caption style you pick — the style decides how captions move,
          the kit decides whose they look like. Save it as a profile to reuse it.
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-2">
        <Row
          label="Caption font"
          hint="Overrides the style's own font. Only the vendored faces are listed, because a font that is not installed is silently substituted at render time."
        >
          <select
            value={settings.brand_font || ""}
            onChange={(event) => set("brand_font")(event.target.value)}
            className={inputClass}
          >
            <option value="">Use the caption style's font</option>
            {fonts.map((font) => (
              <option key={font.name || font} value={font.name || font}>
                {font.name || font}
              </option>
            ))}
          </select>
        </Row>

        <Row
          label="Standing call to action"
          hint="Also used as the end card, replacing the per-clip wording the model would otherwise invent each time."
        >
          <input
            type="text"
            value={settings.brand_cta || ""}
            onChange={(event) => set("brand_cta")(event.target.value)}
            placeholder="Follow for more"
            className={inputClass}
          />
        </Row>

        <Row label="Caption colour">
          <div className="mt-1 flex items-center gap-2">
            <input
              type="color"
              value={settings.brand_primary_color || "#ffffff"}
              onChange={(event) => set("brand_primary_color")(event.target.value)}
              aria-label="Caption colour"
              className="h-9 w-12 rounded border border-slate-700 bg-slate-950"
            />
            <button
              type="button"
              onClick={() => set("brand_primary_color")("")}
              className="rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-400 hover:border-brand-accent"
            >
              Use style default
            </button>
          </div>
        </Row>

        <Row label="Highlight colour">
          <div className="mt-1 flex items-center gap-2">
            <input
              type="color"
              value={settings.brand_highlight_color || "#ffe500"}
              onChange={(event) => set("brand_highlight_color")(event.target.value)}
              aria-label="Highlight colour"
              className="h-9 w-12 rounded border border-slate-700 bg-slate-950"
            />
            <button
              type="button"
              onClick={() => set("brand_highlight_color")("")}
              className="rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-400 hover:border-brand-accent"
            >
              Use style default
            </button>
          </div>
        </Row>

        <Row
          label="Logo file"
          hint="A path on the machine running the renderer (png, jpg or webp). A missing file costs the watermark, not the clip."
        >
          <input
            type="text"
            value={settings.brand_logo || ""}
            onChange={(event) => set("brand_logo")(event.target.value)}
            placeholder="./assets/brand/logo.png"
            className={inputClass}
          />
        </Row>

        <Row label="Logo position">
          <select
            value={settings.brand_logo_position || "top_right"}
            onChange={(event) => set("brand_logo_position")(event.target.value)}
            className={inputClass}
            disabled={!settings.brand_logo}
          >
            {LOGO_POSITIONS.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
        </Row>

        <Row
          label={`Logo size — ${Math.round((settings.brand_logo_scale ?? 0.16) * 100)}% of width`}
        >
          <input
            type="range"
            min={0.04}
            max={0.4}
            step={0.01}
            value={settings.brand_logo_scale ?? 0.16}
            onChange={(event) => set("brand_logo_scale")(Number(event.target.value))}
            disabled={!settings.brand_logo}
            aria-label="Logo size"
            className="mt-2 w-full accent-brand-accent"
          />
        </Row>

        <Row label={`Logo opacity — ${Math.round((settings.brand_logo_opacity ?? 0.85) * 100)}%`}>
          <input
            type="range"
            min={0.05}
            max={1}
            step={0.05}
            value={settings.brand_logo_opacity ?? 0.85}
            onChange={(event) => set("brand_logo_opacity")(Number(event.target.value))}
            disabled={!settings.brand_logo}
            aria-label="Logo opacity"
            className="mt-2 w-full accent-brand-accent"
          />
        </Row>
      </div>
    </section>
  );
}
