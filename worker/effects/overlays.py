"""General graphic/text overlays.

Reusable overlay primitives: progress bars, brand watermarks, hook text,
b-roll, and other decorative layers composited onto clips.

STUB ONLY.
"""

from __future__ import annotations

from pathlib import Path


def add_text_overlay(
    video: str | Path, text: str, dest: str | Path, **style
) -> Path:
    """Overlay ``text`` onto ``video`` with the given ``style`` options.

    TODO(phase-overlays): build ffmpeg drawtext filters from ``style``.
    """
    raise NotImplementedError


def add_watermark(video: str | Path, image: str | Path, dest: str | Path) -> Path:
    """Composite a watermark ``image`` onto ``video``.

    TODO(phase-overlays): overlay the watermark via ffmpeg.
    """
    raise NotImplementedError
