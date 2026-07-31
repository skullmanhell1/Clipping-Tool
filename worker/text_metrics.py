"""Measured text width and real line wrapping for captions (C6, C16).

Captions relied on ASS ``WrapStyle: 2``, which means *no automatic wrapping at all* - libass
breaks only where the text already contains an explicit ``\\N``. Since nothing inserted one, a
long cue was laid out as a single line and either ran past the frame edge or was shrunk by
libass' own fitting, depending on the build. Neither is a decision anyone made.

The obvious fix is a character budget ("wrap at 24 characters"), and it is wrong for the fonts
this product ships. Characters are not equal: in Anton, ``W`` is roughly four times the advance of
``i``. A 24-character budget is a comfortable line of ``MINIMUM WIDTH`` and an overflowing one of
``WWWWWWWWWWWWWWWWWWWWWWWW``. So width is *measured*, from the vendored font's own advance
widths - the same files libass renders from, so the numbers describe the text that will actually
be drawn.

Three honest limitations:

* **Advance widths, not shaped text.** Kerning pairs, ligatures and contextual alternates are not
  applied, so a measurement can be a few per cent wide of what libass produces. That is the right
  error direction - slightly conservative - and getting it exact would mean reimplementing HarfBuzz.
* **A missing or unreadable font falls back to a character budget**, derived from the font's own
  average advance where possible. A wrap that is approximately right beats no wrap.
* **`spacing` and `scale_x`** (C15) are applied as multipliers on the measurement, because they
  change the drawn width and are already part of a preset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Fraction of the frame width a caption line may occupy before it is wrapped.
#:
#: Not 1.0: a line touching the frame edge reads as clipped even when it is not, and the C12 safe
#: areas already keep captions clear of platform chrome horizontally. 0.86 leaves a visible margin
#: at every output resolution.
DEFAULT_LINE_WIDTH_FRACTION = 0.86

#: Characters per line when no font can be measured. A last resort, not a target.
FALLBACK_CHARS_PER_LINE = 24


@dataclass(frozen=True)
class FontMetrics:
    """Advance widths for one font, in font units, plus the unit scale."""

    units_per_em: int
    #: Advance width per character, in font units. Missing characters use ``average``.
    advances: dict[str, int]
    average: int

    def text_width(self, text: str, font_size: float) -> float:
        """Width of ``text`` at ``font_size`` pixels, in pixels."""
        if not text or self.units_per_em <= 0:
            return 0.0
        total = sum(self.advances.get(char, self.average) for char in text)
        return total * float(font_size) / float(self.units_per_em)


@lru_cache(maxsize=32)
def _load_metrics(font_path: str) -> Optional[FontMetrics]:
    """Advance widths for the font at ``font_path``, or ``None`` when unreadable.

    Cached because a cue is measured word by word and a job renders many cues: parsing a TTF per
    measurement would make wrapping cost more than the render it feeds.
    """
    try:
        from fontTools.ttLib import TTFont

        with TTFont(font_path, lazy=True, fontNumber=0) as font:
            upem = int(font["head"].unitsPerEm)
            hmtx = font["hmtx"]
            cmap = font.getBestCmap()
            advances: dict[str, int] = {}
            for codepoint, glyph_name in cmap.items():
                try:
                    advances[chr(codepoint)] = int(hmtx[glyph_name][0])
                except (KeyError, IndexError, TypeError):
                    continue
        if not advances:
            return None
        # The average over *letters* rather than every mapped glyph: a font's cmap includes
        # box-drawing and punctuation whose widths would skew a fallback used for real words.
        letters = [w for char, w in advances.items() if char.isalpha()]
        average = int(sum(letters) / len(letters)) if letters else int(upem / 2)
        return FontMetrics(units_per_em=upem, advances=advances, average=average)
    except Exception:
        logger.debug("C6: could not read font metrics from %s", font_path, exc_info=True)
        return None


def metrics_for_font(font_name: str) -> Optional[FontMetrics]:
    """Metrics for a *family name*, resolved against the vendored fonts, or ``None``.

    Resolution goes through the same manifest the renderer uses, so a name that libass will
    substitute is measured as substituted rather than as requested - measuring a font that will not
    be drawn is worse than not measuring, because it produces confident wrong numbers.
    """
    path = _font_file(font_name)
    return _load_metrics(str(path)) if path else None


@lru_cache(maxsize=64)
def _font_file(font_name: str) -> Optional[Path]:
    """The vendored file for ``font_name``, or ``None``."""
    try:
        import json

        from config import settings

        # `font_assets_dir` is the setting the renderer passes to libass as `fontsdir`, so
        # measuring from the same directory is what keeps the numbers describing the drawn text.
        fonts_dir = Path(getattr(settings, "font_assets_dir", "") or "assets/fonts")
        manifest = fonts_dir.parent / "fonts.json"
        if not manifest.is_file():
            return None
        entries = json.loads(manifest.read_text(encoding="utf-8")).get("fonts") or []
        wanted = (font_name or "").strip().lower()
        for entry in entries:
            names = {
                str(entry.get("name") or "").lower(),
                str(entry.get("family") or "").lower(),
            }
            if wanted in names and entry.get("file"):
                candidate = fonts_dir / str(entry["file"])
                if candidate.is_file():
                    return candidate
        return None
    except Exception:
        return None


def wrap_text(
    text: str,
    *,
    font: str,
    font_size: float,
    max_width_px: float,
    max_lines: int = 2,
    spacing: float = 0.0,
    scale_x: float = 100.0,
) -> list[str]:
    """Split ``text`` into at most ``max_lines`` lines that fit ``max_width_px``.

    Words are never split: a hyphenated break mid-word is more distracting in a caption than a
    slightly uneven line, and short-form captions are read in one glance.

    When the text cannot fit in ``max_lines``, the **line limit wins and the remainder is dropped**
    rather than overflowing the frame. That is the lesser of two bad outcomes and it is why
    :func:`fits_in_lines` exists - the caller's real fix is to put fewer words in the cue, which is
    what the cue builder does with this function's help.
    """
    words = (text or "").split()
    if not words:
        return []

    metrics = metrics_for_font(font)
    scale = max(1.0, float(scale_x)) / 100.0
    budget = max(1.0, float(max_width_px))

    def width(candidate: str) -> float:
        if metrics is not None:
            measured = metrics.text_width(candidate, font_size)
        else:
            # No metrics: approximate with a character budget, scaled by font size so the
            # fallback at least tracks the text's real size.
            measured = len(candidate) * float(font_size) * 0.5
        # C15: letter-spacing adds a fixed amount per gap; scale_x multiplies the advance.
        return measured * scale + max(0.0, float(spacing)) * max(0, len(candidate) - 1)

    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and width(candidate) > budget:
            lines.append(" ".join(current))
            current = [word]
            if len(lines) >= max(1, int(max_lines)):
                # Out of lines: stop rather than run off the frame.
                return lines[: max(1, int(max_lines))]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines[: max(1, int(max_lines))]


def wrap_word_groups(
    words: list[str],
    *,
    font: str,
    font_size: float,
    max_width_px: float,
    max_lines: int = 2,
    spacing: float = 0.0,
    scale_x: float = 100.0,
) -> list[list[int]]:
    """Group word *indices* into lines that fit ``max_width_px``.

    Indices rather than text, because each caption word is rendered as an ASS span carrying its own
    override tags (karaoke fill, keyword highlight, punch, confidence dimming). Re-wrapping the
    joined *span* text would measure the tags as if they were letters, which is both wrong and
    wildly so - a single ``{\\kf34\\c&H0000E5FF&}`` is longer than the word it decorates. So the
    plain words are measured and the spans are joined at the resulting break points.

    Unlike :func:`wrap_text` this never drops a word: the caller is emitting a caption and a
    silently truncated one is worse than a line more than requested. Keeping the cue short enough is
    :func:`fits_in_lines`' job, applied earlier when the cue is built.
    """
    metrics = metrics_for_font(font)
    scale = max(1.0, float(scale_x)) / 100.0
    budget = max(1.0, float(max_width_px))

    def width(candidate: str) -> float:
        if metrics is not None:
            measured = metrics.text_width(candidate, font_size)
        else:
            measured = len(candidate) * float(font_size) * 0.5
        return measured * scale + max(0.0, float(spacing)) * max(0, len(candidate) - 1)

    groups: list[list[int]] = []
    current: list[int] = []
    for index, word in enumerate(words):
        candidate = " ".join([*(words[i] for i in current), word])
        if current and width(candidate) > budget:
            groups.append(current)
            current = [index]
        else:
            current.append(index)
    if current:
        groups.append(current)
    return groups


def fits_in_lines(
    text: str,
    *,
    font: str,
    font_size: float,
    max_width_px: float,
    max_lines: int = 2,
    spacing: float = 0.0,
    scale_x: float = 100.0,
) -> bool:
    """Whether ``text`` fits in ``max_lines`` at this size without losing words (C16).

    This is what lets the cue builder decide *how many words a cue may hold* from the font that
    will draw them, instead of a fixed word count that is too many for a wide face and too few for
    a condensed one.
    """
    words = (text or "").split()
    if not words:
        return True
    wrapped = wrap_text(
        text, font=font, font_size=font_size, max_width_px=max_width_px,
        max_lines=max_lines, spacing=spacing, scale_x=scale_x,
    )
    return sum(len(line.split()) for line in wrapped) == len(words)


def line_budget_px(video_width: int, fraction: float = DEFAULT_LINE_WIDTH_FRACTION) -> float:
    """The pixel width a caption line may occupy in a frame ``video_width`` wide."""
    return max(1.0, float(video_width) * max(0.1, min(1.0, float(fraction))))
