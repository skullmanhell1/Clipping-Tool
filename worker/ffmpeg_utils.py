"""Thin FFmpeg/FFprobe helpers.

Wraps common FFmpeg operations (probing, cutting, scaling, muxing) behind small
Python functions so the rest of the pipeline never shells out directly. Uses the
binary paths from :data:`config.settings`.

STUB ONLY.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import settings


def probe(path: str | Path) -> dict[str, Any]:
    """Return media metadata for ``path`` via ffprobe (duration, streams, ...).

    TODO(phase-video): call ``settings.ffprobe_binary`` and parse JSON output.
    """
    raise NotImplementedError


def cut_segment(
    source: str | Path, start: float, end: float, dest: str | Path
) -> Path:
    """Losslessly (or re-encoded) cut ``[start, end]`` from ``source`` to ``dest``.

    TODO(phase-video): build and run the ffmpeg command.
    """
    raise NotImplementedError


def to_vertical(source: str | Path, dest: str | Path, width: int = 1080,
                height: int = 1920) -> Path:
    """Reformat ``source`` to a vertical canvas (default 1080x1920).

    TODO(phase-video): compose scale/pad/crop filters. Face-aware cropping is
    handled by ``worker.effects.reframe``.
    """
    raise NotImplementedError
