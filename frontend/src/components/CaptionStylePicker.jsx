import PropTypes from "prop-types";
import { useMemo } from "react";

/**
 * U5: a caption style picker that shows what each preset looks like.
 *
 * The presets were a dropdown of six names. Choosing between "pop", "typewriter" and "hormozi"
 * meant rendering a clip to find out what you had picked — a minutes-long round trip to answer a
 * question about typography.
 *
 * **The preview is an approximation and is labelled as one.** The real captions are rendered by
 * libass from an ASS script: it does the word-by-word karaoke fill, the per-word punch, the
 * outline geometry and the exact font metrics, none of which CSS reproduces faithfully. What the
 * preview is honest about is the part that decides which preset you want — the typeface, the
 * weight, the colour pair, the case, the box and roughly where the text sits. Claiming more than
 * that would be worse than showing nothing, because a preview that lies is trusted once.
 */

const POSITION_CLASS = {
  bottom: "items-end pb-3",
  center: "items-center",
  top: "items-start pt-3",
  bottom_left: "items-end justify-start pb-3 pl-2",
  bottom_right: "items-end justify-end pb-3 pr-2",
  top_left: "items-start justify-start pt-3 pl-2",
  top_right: "items-start justify-end pt-3 pr-2",
  center_left: "items-center justify-start pl-2",
  center_right: "items-center justify-end pr-2",
};

const SAMPLE = "This changed everything";

/**
 * One caption preset, as `/api/info` reports it under `effects.caption_preset_details`.
 *
 * Only `name` is required: it is the identity — the React key, the accessible label, the value
 * reported on selection — while every visual field has a fallback in the swatch, because an older
 * backend advertises fewer of them and a preset that renders in the app's default typography is
 * more use than a preset that is not offered.
 */
const PRESET_SHAPE = PropTypes.shape({
  name: PropTypes.string.isRequired,
  font: PropTypes.string,
  colors_hex: PropTypes.shape({
    primary: PropTypes.string,
    highlight: PropTypes.string,
  }),
  position: PropTypes.string,
  font_weight: PropTypes.number,
  uppercase: PropTypes.bool,
  spacing: PropTypes.number,
  scale_x: PropTypes.number,
  border_style: PropTypes.number,
});

/**
 * The brand-kit fields the preview honours.
 *
 * This is a *view* of the settings object — the whole blob is passed in, because the kit lives
 * inside it — so the shape names only the three keys read here rather than restating the schema.
 */
const BRAND_SHAPE = PropTypes.shape({
  brand_font: PropTypes.string,
  brand_primary_color: PropTypes.string,
  brand_highlight_color: PropTypes.string,
});

/** One preset rendered as a phone-shaped swatch. */
function Swatch({ preset, active, onSelect, brand }) {
  const colors = preset.colors_hex || {};
  // The brand kit overrides the preset here for the same reason it does in the renderer: the kit
  // is an identity and the preset is a look, so the preview has to show the combination that will
  // actually be rendered rather than the preset alone.
  const font = brand?.brand_font || preset.font;
  const primary = brand?.brand_primary_color || colors.primary || "#ffffff";
  const highlight = brand?.brand_highlight_color || colors.highlight || "#ffe500";
  const boxed = preset.border_style === 3 || preset.border_style === 4;

  const words = useMemo(() => {
    const text = preset.uppercase ? SAMPLE.toUpperCase() : SAMPLE;
    return text.split(" ");
  }, [preset.uppercase]);

  return (
    <button
      type="button"
      onClick={() => onSelect(preset.name)}
      aria-pressed={active}
      aria-label={`Caption style ${preset.name}`}
      className={`relative overflow-hidden rounded-lg border p-0 transition ${
        active
          ? "border-brand-accent ring-1 ring-brand-accent"
          : "border-slate-700 hover:border-slate-500"
      }`}
    >
      <div
        className={`flex aspect-[9/16] w-full justify-center bg-gradient-to-b from-slate-700 to-slate-900 ${
          POSITION_CLASS[preset.position] || POSITION_CLASS.bottom
        }`}
      >
        <div
          className={`mx-1 max-w-full text-center leading-tight ${boxed ? "rounded px-1 py-0.5" : ""}`}
          style={{
            fontFamily: `"${font}", system-ui, sans-serif`,
            fontWeight: preset.font_weight >= 700 ? 800 : 600,
            // Scaled to the swatch rather than the preset's real pt size, which is sized for a
            // 1920-tall frame and would overflow a thumbnail.
            fontSize: "0.62rem",
            letterSpacing: `${(preset.spacing || 0) * 0.06}em`,
            transform: `scaleX(${(preset.scale_x || 100) / 100})`,
            color: primary,
            background: boxed ? "rgba(0,0,0,0.55)" : "transparent",
            textShadow: boxed ? "none" : "0 1px 2px rgba(0,0,0,0.9), 0 0 3px rgba(0,0,0,0.8)",
          }}
        >
          {words.map((word, index) => (
            <span
              key={`${word}-${index}`}
              // One word shown in the highlight colour, because the colour pair is the thing a
              // creator is choosing between and a single-colour sample hides half of it.
              style={index === 1 ? { color: highlight } : undefined}
            >
              {word}
              {index < words.length - 1 ? " " : ""}
            </span>
          ))}
        </div>
      </div>
      <span className="block truncate border-t border-slate-800 bg-slate-950 px-1 py-0.5 text-[10px] text-slate-300">
        {preset.name}
      </span>
    </button>
  );
}

Swatch.propTypes = {
  preset: PRESET_SHAPE.isRequired,
  active: PropTypes.bool,
  // Required: a swatch that cannot report its own selection is a picture of a control.
  onSelect: PropTypes.func.isRequired,
  brand: BRAND_SHAPE,
};

export default function CaptionStylePicker({ presets = [], value, onChange, brand }) {
  if (!presets.length) {
    return (
      <p className="text-xs text-slate-500">
        Caption styles are unavailable — the server did not report any.
      </p>
    );
  }

  return (
    <div>
      <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
        {presets.map((preset) => (
          <Swatch
            key={preset.name}
            preset={preset}
            active={preset.name === value}
            onSelect={onChange}
            brand={brand}
          />
        ))}
      </div>
      <p className="mt-2 text-[10px] leading-snug text-slate-500">
        Approximate preview: typeface, colours, case and placement. The word-by-word fill, the
        active-word punch and the exact outline are rendered by libass at export and are not
        reproduced here.
      </p>
    </div>
  );
}

CaptionStylePicker.propTypes = {
  // Not required: an empty list is the documented state for a backend that does not advertise the
  // preset details, and it renders as the sentence saying so rather than as an empty grid.
  presets: PropTypes.arrayOf(PRESET_SHAPE),
  // The currently chosen preset name. Absent means none of the swatches reads as active, which is
  // what a settings object predating the preset field should look like.
  value: PropTypes.string,
  onChange: PropTypes.func.isRequired,
  brand: BRAND_SHAPE,
};
