"""Auto-contrast: choose the caption's outline/box colour from the video behind it (C20).

Every preset carries fixed colours, and the default is white text with a black outline. That is
the right guess most of the time and wrong in the two cases that matter: a caption over a bright
sky or a white studio wall, where a black outline around white glyphs is the only thing keeping
them visible and it is doing that job badly; and a caption over dark footage, where a heavy black
outline merges with the background into a shapeless mass.

Neither failure is visible to the creator in the settings panel. It appears in the finished clip,
in the one element the viewer is guaranteed to look at.

**What this measures and what it does with it.** A handful of frames are sampled from the region
the caption will actually occupy - derived from the same position/safe-area maths the renderer uses,
so the measurement is of the pixels the text will sit on rather than of the frame's average. From
their mean luma it picks:

* a **dark** outline (or box) under light text on a bright background - the default case, kept
  because it is correct there;
* a **light** outline under dark text on a dark background, which is the case nothing handled.

**What it deliberately does not do.** It never changes the *fill* colour. A preset's fill is a
brand decision (U6 makes that explicit), and a tool that silently recoloured a creator's captions
because a shot was bright would be overruling them on the one thing they chose. Only the
legibility layer - outline and box - is adjusted, which is the part nobody picks on purpose.

It also does not attempt per-cue adaptation. A caption whose outline changes colour partway
through a clip draws attention to itself far more than a slightly suboptimal constant choice, so
one decision is made per clip from several samples.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

#: Mean luma (0-255) above which the background counts as bright.
#:
#: Deliberately above mid-grey. The dark outline is the safe default - it works over most footage -
#: so the light outline should only engage when the background is genuinely dark, not merely
#: below average. Erring towards the default is erring towards the shipped behaviour.
BRIGHT_THRESHOLD = 110.0

#: How many frames to sample across the clip.
#:
#: Three, not one: a single frame can be a cut, a flash or a title card, and one unlucky sample
#: would set the outline colour for the whole clip. Three is enough to outvote an outlier while
#: costing three seeks.
SAMPLE_COUNT = 3

#: ASS colours for the two outcomes.
DARK_OUTLINE = "&H00000000"
LIGHT_OUTLINE = "&H00FFFFFF"

#: Semi-opaque versions for ``border_style=3`` (box) presets, so a box stays a box.
DARK_BOX = "&H96000000"
LIGHT_BOX = "&H96FFFFFF"


@dataclass(frozen=True)
class BackgroundSample:
    """What the video looks like where the caption will sit."""

    mean_luma: float
    samples: int

    @property
    def bright(self) -> bool:
        return self.mean_luma >= BRIGHT_THRESHOLD


def caption_band(
    position: str,
    video_width: int,
    video_height: int,
    *,
    font_size: int = 96,
    max_lines: int = 2,
) -> tuple[int, int, int, int]:
    """The ``(w, h, x, y)`` rectangle a caption occupies, for sampling.

    Height is derived from the font size and line budget rather than fixed, because a three-line
    `headline` caption at 104 px covers a very different part of the frame from a one-line
    `subtitle` at 72 - and sampling the wrong band is how an auto-contrast feature confidently
    picks the wrong colour.
    """
    width = max(1, int(video_width))
    height = max(1, int(video_height))
    # 1.35 line height, which is roughly what libass produces for these faces, plus the outline.
    band_h = min(height, max(1, int(round(font_size * 1.35 * max(1, max_lines)))))

    margins = safe_area_bottom(width, height)
    place = (position or "bottom").strip().lower()
    if place.startswith("top"):
        y = margins
    elif place.startswith("center"):
        y = max(0, (height - band_h) // 2)
    else:
        y = max(0, height - band_h - margins)
    return (width, band_h, 0, min(y, max(0, height - band_h)))


def safe_area_bottom(video_width: int, video_height: int) -> int:
    """The configured bottom inset in pixels, reusing the C12 profiles."""
    try:
        from worker.captions import safe_area_margins

        profile = str(getattr(settings, "caption_safe_area", "") or "") or None
        return int(safe_area_margins(video_width, video_height, profile)["bottom"])
    except Exception:
        return int(video_height * 0.11)


def sample_background(
    video: str | Path,
    duration: float,
    band: tuple[int, int, int, int],
    *,
    count: int = SAMPLE_COUNT,
) -> BackgroundSample | None:
    """Mean luma of ``band`` across ``count`` frames, or ``None`` when unmeasurable.

    ``None`` means "no information", and every caller treats that as "keep the preset's colours" -
    which is the behaviour before C20. An auto-contrast feature that failed a render would be a bad
    trade for a legibility improvement.
    """
    width, height, x, y = band
    span = max(0.1, float(duration))
    total = max(1, int(count))
    readings: list[float] = []

    for index in range(total):
        at = span * (index + 0.5) / total
        try:
            result = subprocess.run(
                [
                    settings.ffmpeg_binary,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{at:.3f}",
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    # Reduced to a single pixel: the mean of the band is the whole measurement, and
                    # letting the scaler compute it avoids moving a megabyte of pixels per sample.
                    "-vf",
                    f"crop={width}:{height}:{x}:{y},scale=1:1,format=gray",
                    "-f",
                    "rawvideo",
                    "-",
                ],
                capture_output=True,
                timeout=60,
            )
        except Exception:
            continue
        if result.returncode == 0 and result.stdout:
            readings.append(float(result.stdout[0]))

    if not readings:
        return None
    return BackgroundSample(mean_luma=sum(readings) / len(readings), samples=len(readings))


def apply_auto_contrast(preset, sample: BackgroundSample | None) -> tuple[object, list[str]]:
    """Return ``(preset, markers)`` with legibility colours chosen for the background (C20).

    Only ``outline`` and ``box`` are touched. The fill is a brand decision (U6) and silently
    recolouring it because a shot was bright would overrule the one thing the creator chose.
    """
    if sample is None:
        return preset, []

    from dataclasses import replace

    colors = getattr(preset, "colors", None)
    if colors is None:
        return preset, []

    boxed = int(getattr(preset, "border_style", 1) or 1) == 3
    if sample.bright:
        outline, box = DARK_OUTLINE, DARK_BOX
        marker = "auto_contrast:dark"
    else:
        outline, box = LIGHT_OUTLINE, LIGHT_BOX
        marker = "auto_contrast:light"

    wanted = {"box": box} if boxed else {"outline": outline}
    current = {key: getattr(colors, key, None) for key in wanted}
    if current == wanted:
        # Already correct: record nothing. A marker for a value that did not change is noise, and
        # `effects_applied` is read as a list of decisions.
        return preset, []

    return replace(preset, colors=replace(colors, **wanted)), [marker]


def choose_for_clip(
    video: str | Path,
    preset,
    *,
    duration: float,
    video_width: int,
    video_height: int,
    position: str | None = None,
) -> tuple[object, list[str]]:
    """Measure the caption region of ``video`` and adapt ``preset``'s legibility colours (C20).

    Off unless ``settings.caption_auto_contrast`` is set, because it costs three seeks per clip and
    changes rendered output - which by the convention followed throughout this project means it
    cannot be a silent default.
    """
    if not getattr(settings, "caption_auto_contrast", False):
        return preset, []
    band = caption_band(
        position or str(getattr(preset, "position", "bottom")),
        video_width,
        video_height,
        font_size=int(getattr(preset, "font_size", 96) or 96),
        max_lines=int(getattr(preset, "max_lines", 2) or 2),
    )
    sample = sample_background(video, duration, band)
    if sample is None:
        logger.debug("C20: could not sample the caption region of %s", video)
    return apply_auto_contrast(preset, sample)
