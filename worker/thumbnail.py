"""Pick a good thumbnail frame instead of an arbitrary one (V17).

The thumbnail was taken at ``min(1.0, duration / 2)`` - a fixed position with no relationship to
what is in the frame. On a clip that opens on a cut, a hand across the lens, or a mid-blink, that
is the still representing the clip everywhere it is listed.

**Three candidate frames are scored, not one.** The scoring is deliberately cheap and CPU-only,
in the same spirit as the existing visual-cue proxies:

* **Sharpness**, via mean edge energy. This is the one that matters: motion blur and a frame
  caught mid-pan are the two most common ways an automatic thumbnail looks bad, and both show up
  as an absence of edges. A blurred frame is unrecoverable, whereas a slightly dark one is not.
* **Mid-range exposure.** A frame that is nearly black (a cut, a fade) or blown out (a flash, a
  white screen) scores low. Measured as distance from the middle of the range rather than as
  "brighter is better", because an over-exposed frame is exactly as useless as a dark one and a
  linear preference for brightness would pick the flash.
* **A late-start preference**, small. The first moments of a clip are the most likely to contain
  a transition artefact, and the last to be a trailing pause.

**No face detection**, though a face is what the plan suggests looking for. Two reasons: the only
detector available here is the 2001-era Haar cascade V2 exists to replace, whose false positives
on texture would actively mislead this; and a thumbnail chosen for containing *a face* rather than
a *sharp* face is worse than one chosen for sharpness alone. When V2 lands this is the natural
place to add it.

Every failure degrades to the previous behaviour - the midpoint frame - because a thumbnail is
worth exactly one ffmpeg pass and nothing more.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Callable, Optional, Sequence

from config import settings
from worker import ffmpeg_utils as fu

logger = logging.getLogger(__name__)

#: Fractions of the clip's duration to consider.
#:
#: Three, not more: each one is an ffmpeg seek-and-decode, and the thumbnail is a still nobody
#: will scrutinise. The values skip the first fifth of the clip, where a transition or a cut is
#: most likely, and the last fifth, which is often a trailing pause.
CANDIDATE_FRACTIONS: tuple[float, ...] = (0.3, 0.5, 0.7)

#: Width the candidates are sampled at for scoring. Small - the score is a whole-frame statistic,
#: and decoding at full resolution three times to compute two averages would be wasteful.
SCORE_WIDTH = 320


def _score_frame(path: str | Path) -> Optional[float]:
    """A ``[0, 1]`` quality score for one sampled frame, or ``None`` if it cannot be read.

    PIL is imported lazily and its absence is not an error - it is optional throughout this
    codebase - in which case there is no signal and the caller keeps the default frame.
    """
    try:
        from PIL import Image, ImageFilter, ImageStat  # type: ignore
    except Exception:
        return None

    try:
        with Image.open(path) as image:
            grey = image.convert("L")
            brightness = ImageStat.Stat(grey).mean[0] / 255.0
            # Mean edge magnitude as a sharpness proxy. FIND_EDGES rather than a variance of
            # Laplacian because it needs no numpy, and the two rank frames the same way for this
            # purpose - only the absolute scale differs, and only the ranking is used.
            edges = ImageStat.Stat(grey.filter(ImageFilter.FIND_EDGES)).mean[0] / 255.0
    except Exception:
        return None

    # Normalised against a value a normally-detailed frame reaches, then clamped. Without a
    # ceiling, a single frame of noise or hard text would dominate the ranking outright.
    sharpness = min(1.0, edges / 0.12)
    # Distance from mid-grey, so both too-dark and blown-out frames are penalised equally.
    exposure = 1.0 - min(1.0, abs(brightness - 0.5) / 0.5)
    return 0.7 * sharpness + 0.3 * exposure


def best_thumbnail_time(
    source: str | Path,
    duration: float,
    *,
    scorer: Callable[[str | Path], Optional[float]] = _score_frame,
    fractions: Sequence[float] = CANDIDATE_FRACTIONS,
) -> float:
    """The timestamp of the best-scoring candidate frame (V17).

    Falls back to the previous rule - ``min(1.0, duration / 2)`` - whenever there is no usable
    signal: no PIL, no ffmpeg, an unreadable clip, or a duration too short for the candidates to
    be distinguishable.
    """
    # Coerce before computing the default: `float(duration)` was raising here on junk input,
    # which turned an unusable duration into an exception instead of the documented fallback.
    try:
        total = float(duration)
    except (TypeError, ValueError):
        return 0.0
    if total != total:      # NaN
        return 0.0
    default = min(1.0, max(0.0, total) / 2.0)
    # Below a couple of seconds the candidates land within a few frames of each other, so the
    # extra passes buy nothing.
    if total < 2.0:
        return default

    times = [round(total * f, 3) for f in fractions if 0.0 < f < 1.0]
    if not times:
        return default

    best_time, best_score = default, -1.0
    try:
        with tempfile.TemporaryDirectory(prefix="thumb-") as work:
            for t in times:
                frame = Path(work) / f"cand_{t:.3f}.jpg"
                try:
                    fu.generate_thumbnail(source, frame, at=t, width=SCORE_WIDTH)
                except Exception:
                    continue
                score = scorer(frame)
                if score is None:
                    # No scoring available at all - stop rather than decode the rest for nothing.
                    return default
                if score > best_score:
                    best_time, best_score = t, score
    except Exception:
        return default

    if best_score < 0:
        return default
    logger.debug("V17: thumbnail at %.3fs (score %.3f)", best_time, best_score)
    return best_time


def choose_thumbnail_time(source: str | Path, duration: float) -> float:
    """``best_thumbnail_time`` when enabled, otherwise the original midpoint rule."""
    if not getattr(settings, "smart_thumbnail", True):
        return min(1.0, max(0.0, float(duration)) / 2.0)
    return best_thumbnail_time(source, duration)
