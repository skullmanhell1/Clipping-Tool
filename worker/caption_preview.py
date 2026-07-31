"""Render a short caption sample so a style can be chosen without a full render (C18).

`U5` gave the settings panel a style picker, and its preview is drawn in CSS - which can show the
typeface, the colour pair, the case and roughly the placement, and cannot show any of the things
that actually distinguish these presets: the word-by-word karaoke fill, the active-word punch, the
per-word pill (C9), the dual stroke (C17), the measured line wrapping (C6). Those are libass'
work, and the only honest way to preview them is to let libass do it.

The alternative - what this replaces - is rendering a whole clip to find out what a preset looks
like, which is minutes of work to answer a question about typography.

Two deliberate choices:

* **The background is generated, not taken from the user's video.** A preview should show the
  caption, and a real frame makes it a test of that frame's legibility instead. A mid-grey field is
  the honest neutral - and a caller who wants to check legibility over their own footage has C20 for
  the decision and the clip itself for the check.
* **Two seconds, not a still.** A still cannot show a karaoke sweep, a typewriter reveal or a punch,
  which is most of what a preset *is*. The word timings below are synthetic and evenly spaced, so
  the animation is visible without needing a transcript.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from config import settings
from worker import captions as cap
from worker.effects.caption_presets import load_preset
from worker.ffmpeg_utils import _run, aspect_size, h264_args

#: The sample text. Short, mixed-length words, and one obvious candidate for keyword emphasis.
SAMPLE_TEXT = "This one change made everything click"

#: Length of the preview, in seconds.
PREVIEW_SECONDS = 2.0

#: Background of the preview frame.
#:
#: Mid-grey rather than black or white: both extremes flatter one outline choice and punish the
#: other, so a preview over either would misrepresent every preset that assumed the opposite.
PREVIEW_BACKGROUND = "gray"


@dataclass
class _SampleWord:
    """A synthetic word with the attributes the caption renderer reads."""

    text: str
    start: float
    end: float
    probability: float = 1.0


def sample_words(
    text: str = SAMPLE_TEXT, duration: float = PREVIEW_SECONDS
) -> list[_SampleWord]:
    """Evenly-spaced synthetic word timings across ``duration``.

    Evenly spaced on purpose: a preview is a comparison between presets, and irregular timings would
    make two presets look different for a reason that has nothing to do with either.
    """
    words = [w for w in (text or "").split() if w]
    if not words:
        return []
    span = max(0.2, float(duration))
    step = span / len(words)
    return [
        _SampleWord(text=word, start=index * step, end=(index + 1) * step - step * 0.08)
        for index, word in enumerate(words)
    ]


def render_preview(
    preset_ref: Any,
    dest: str | Path,
    *,
    text: str = SAMPLE_TEXT,
    aspect: str = "9:16",
    position: Optional[str] = None,
    duration: float = PREVIEW_SECONDS,
    highlight_index: Optional[int] = 2,
    brand: Optional[Any] = None,
) -> Path:
    """Render a caption sample to ``dest`` and return the path.

    ``preset_ref`` is a preset name or a serialised preset dict, so a caller can preview a *modified*
    preset - which is what makes this usable from a settings panel where the user has already changed
    the font or the colours (U6) and wants to see that, not the shipped preset.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    preset, _substituted = load_preset(preset_ref)
    if brand is not None:
        from worker import branding

        preset, _markers = branding.apply_brand(preset, brand)

    width, height = aspect_size(aspect)
    words = sample_words(text, duration)
    if not words:
        raise ValueError("preview needs at least one word")

    # Cue grouping uses the same measured fit as a real render (C6/C16), so the preview shows the
    # same line breaks the clip would get. Previewing an unwrapped caption would misrepresent
    # exactly the property C6 exists to fix.
    fit = cap.TextFit.for_preset(preset, video_width=width)
    # `_SampleWord` is a local stand-in with the start/end/text that `words_to_cues` reads;
    # it is not the transcribe.Word dataclass, which carries ASR fields a preview has no
    # source for.
    cues = cap.words_to_cues(cast(Any, words), max_words=6, fit=fit)

    keyword_indices = set()
    if highlight_index is not None and 0 <= highlight_index < len(words):
        keyword_indices = {highlight_index}

    ass_path = dest.with_suffix(".ass")
    cap.build_ass(
        cues,
        ass_path,
        video_width=width,
        video_height=height,
        preset=preset,
        keyword_indices=keyword_indices,
        position=position,
        clip_duration=float(duration),
    )

    try:
        _run([
            settings.ffmpeg_binary, "-y",
            "-f", "lavfi",
            "-i", f"color=c={PREVIEW_BACKGROUND}:s={width}x{height}:r=25:d={duration:.2f}",
            "-vf", cap.subtitles_filter(ass_path),
            *h264_args(),
            "-movflags", "+faststart",
            str(dest),
        ])
    finally:
        # The ASS is an intermediate; leaving it beside the preview would accumulate one per
        # preview per preset in whatever directory the caller chose.
        ass_path.unlink(missing_ok=True)
    return dest
