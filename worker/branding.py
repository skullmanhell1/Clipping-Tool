"""Brand kit: a persisted font, colour pair, logo and call to action (U6).

A creator's look was spread across places that could not be saved together. The caption font and
colours lived inside a *preset*, editable only in source; the call to action was regenerated per
clip by the LLM, so it varied run to run; and there was no way to put a logo on a clip at all.
Anyone with a house style had to re-pick settings for every job and accept that the CTA would be
reworded each time.

The brand kit is deliberately a small set of fields - font, primary colour, highlight colour,
logo, CTA - rather than a full theming system. Those five are what a brand guideline actually
specifies for short-form video, and each maps onto something the renderer already does.

Two decisions worth knowing about:

* **The kit overrides the preset, not the other way round.** A preset is a *look* (how captions
  animate, where they sit, how heavy the face is); the kit is an *identity*. Choosing the
  ``hormozi`` preset with a brand font should give hormozi's animation in the brand's typeface,
  which is not what "the preset wins" would produce.
* **The logo is drawn with the ``movie`` source filter rather than a second ffmpeg input.** The
  compositor's input indices are load-bearing - engine contributions, music, b-roll and emoji all
  compute offsets from them, and that accounting is documented as the thing that keeps v0.8.0
  parity - so adding an input for a watermark would risk breaking every one of those offsets to
  save nothing. ``movie`` reads the file inside the filtergraph and shifts no index.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from worker.effects.caption_presets import CaptionPreset
from worker.ffmpeg_utils import escape_filter_path

#: Where a logo may sit. Corners only: a watermark belongs out of the way, and the centre of a
#: vertical clip is where the subject and the captions are.
LOGO_POSITIONS: tuple[str, ...] = (
    "top_left",
    "top_right",
    "bottom_left",
    "bottom_right",
)

#: Logo inset from the frame edge, as a fraction of frame width.
LOGO_MARGIN_FRAC = 0.04

#: Default logo width as a fraction of frame width, and the range a caller may ask for.
DEFAULT_LOGO_SCALE = 0.16
MIN_LOGO_SCALE = 0.04
MAX_LOGO_SCALE = 0.40

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def hex_to_ass(color: Any) -> str | None:
    """Convert ``#RRGGBB`` to an ASS ``&H00BBGGRR`` colour, or ``None`` if unparseable.

    ASS stores colours **byte-reversed** relative to HTML - blue, green, red - with an alpha byte
    in front. Getting that wrong does not fail: it renders, in the wrong colour, so a brand's red
    silently becomes its blue. That is the whole reason this is one named function with tests
    rather than an inline expression.

    Already-ASS values are passed through, so a kit stored from a preset value keeps working.
    """
    if color is None:
        return None
    text = str(color).strip()
    if not text:
        return None
    if text.upper().startswith("&H"):
        return text
    found = _HEX_RE.match(text)
    if not found:
        return None
    digits = found.group(1)
    red, green, blue = digits[0:2], digits[2:4], digits[4:6]
    return f"&H00{blue}{green}{red}".upper()


def ass_to_hex(color: Any) -> str | None:
    """Convert an ASS ``&HAABBGGRR`` colour back to ``#RRGGBB``, for a UI colour input.

    Needed because the presets' colours are stored in ASS form and a colour picker cannot show
    one. Without this the brand-kit UI could set a colour but never display the current value.
    """
    text = str(color or "").strip().upper()
    if not text.startswith("&H"):
        return None
    digits = text[2:].lstrip("&").rstrip("&")
    if len(digits) == 8:
        digits = digits[2:]
    if len(digits) != 6:
        return None
    blue, green, red = digits[0:2], digits[2:4], digits[4:6]
    return f"#{red}{green}{blue}".lower()


def brand_from_options(options: Any) -> dict[str, Any]:
    """The brand-kit values set on ``options``, omitting the empty ones.

    Returning only what is set is what makes the kit additive: an unset field must leave the
    preset's own value alone rather than overwriting it with a default.
    """
    kit: dict[str, Any] = {}
    font = str(getattr(options, "brand_font", "") or "").strip()
    if font:
        kit["font"] = font
    primary = hex_to_ass(getattr(options, "brand_primary_color", ""))
    if primary:
        kit["primary"] = primary
    highlight = hex_to_ass(getattr(options, "brand_highlight_color", ""))
    if highlight:
        kit["highlight"] = highlight
    cta = str(getattr(options, "brand_cta", "") or "").strip()
    if cta:
        kit["cta"] = cta
    logo = str(getattr(options, "brand_logo", "") or "").strip()
    if logo:
        kit["logo"] = logo
    return kit


def apply_brand(preset: CaptionPreset, options: Any) -> tuple[CaptionPreset, list[str]]:
    """Apply the brand kit's typography to ``preset``. Returns ``(preset, markers)``.

    Only the font and the two colours: a kit that also carried position or animation would be a
    second preset system, and the preset is the right home for those.

    A requested font that is not available is **not** substituted here. ``captions.resolve_font``
    already owns that decision and records a ``font_substituted`` marker, so silently picking
    something else here would both duplicate that logic and hide the substitution.
    """
    kit = brand_from_options(options)
    markers: list[str] = []
    if not kit:
        return preset, markers

    if "font" in kit and kit["font"] != preset.font:
        preset = replace(preset, font=kit["font"])
        markers.append("brand_font")

    colors = preset.colors
    changed = {}
    if "primary" in kit and kit["primary"] != colors.primary:
        changed["primary"] = kit["primary"]
    if "highlight" in kit and kit["highlight"] != colors.highlight:
        changed["highlight"] = kit["highlight"]
    if changed:
        preset = replace(preset, colors=replace(colors, **changed))
        markers.append("brand_colors")

    return preset, markers


def logo_filter(
    options: Any,
    width: int,
    height: int,
    *,
    base_label: str = "vbase",
    out_label: str = "vbrand",
) -> str | None:
    """A watermark graph segment for the brand logo, or ``None`` when there is none (U6).

    Returns a **labelled graph segment**, not a chain filter: ``overlay`` takes two inputs, so a
    watermark cannot sit inside the comma-joined caption chain. The logo is sized relative to the
    frame so one kit works at every output resolution (O9 renders 720 to 2160).

    A missing or non-image file yields ``None`` rather than an error: a broken logo path should
    cost the watermark, not the clip.
    """
    raw = str(getattr(options, "brand_logo", "") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return None
    if not path.is_file():
        return None

    position = str(getattr(options, "brand_logo_position", "top_right") or "top_right")
    if position not in LOGO_POSITIONS:
        position = "top_right"

    try:
        scale = float(
            getattr(options, "brand_logo_scale", DEFAULT_LOGO_SCALE) or DEFAULT_LOGO_SCALE
        )
    except (TypeError, ValueError):
        scale = DEFAULT_LOGO_SCALE
    scale = max(MIN_LOGO_SCALE, min(MAX_LOGO_SCALE, scale))

    try:
        opacity = float(getattr(options, "brand_logo_opacity", 0.85) or 0.85)
    except (TypeError, ValueError):
        opacity = 0.85
    opacity = max(0.05, min(1.0, opacity))

    # Even width: the overlay is composited into a 4:2:0 frame, and an odd-width source makes
    # ffmpeg pick a chroma alignment rather than failing, which shows as a soft one-pixel edge.
    logo_w = max(2, int(round(width * scale)))
    logo_w -= logo_w % 2
    margin = max(2, int(round(width * LOGO_MARGIN_FRAC)))

    x = f"W-w-{margin}" if position.endswith("right") else str(margin)
    y = f"H-h-{margin}" if position.startswith("bottom") else str(margin)

    # `-1` height preserves the logo's aspect ratio; `format=rgba` before the alpha multiply is
    # required because a JPEG logo has no alpha channel to scale.
    return (
        f"movie=filename='{escape_filter_path(path)}',"
        f"scale={logo_w}:-1,format=rgba,colorchannelmixer=aa={opacity:.3f}[brandlogo];"
        f"[{base_label}][brandlogo]overlay={x}:{y}[{out_label}]"
    )


def end_card_text(options: Any) -> str:
    """The brand CTA, for the V14 end card. ``""`` when the kit sets none.

    This is why the CTA belongs in the kit: it was regenerated per clip by the LLM, so a creator
    with one standing ask ("link in bio") got a different wording on every clip of every job.
    """
    return str(getattr(options, "brand_cta", "") or "").strip()
