"""Simple clip segmentation.

Phase 1 uses deterministic segmentation (no LLM "best moment" selection yet):

* ``fixed`` — evenly split the video into chunks of a target length.
* ``silence`` — detect silences (via FFmpeg ``silencedetect``) and break clips
  at natural pauses, keeping each clip within a min/max length window.

The UI's *Clip Length* and *Number of Clips* dropdowns are mapped to concrete
parameters via :func:`resolve_length_range` and :func:`resolve_max_clips`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import settings
from worker.ffmpeg_utils import FFmpegError


@dataclass
class Segment:
    """A proposed clip time range (source-relative seconds)."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


# UI "Clip Length" option -> (min_seconds, max_seconds, target_seconds).
# ``target`` drives fixed-length chunking; ``min``/``max`` bound silence splits.
CLIP_LENGTH_PRESETS: dict[str, tuple[float, float, float]] = {
    "auto": (10.0, 90.0, 45.0),      # Auto (<90s)
    "<30s": (5.0, 30.0, 20.0),
    "30-60s": (30.0, 60.0, 45.0),
    "60-90s": (60.0, 90.0, 75.0),
    "90s-3min": (90.0, 180.0, 120.0),
}

# UI "Number of Clips" option -> max clips (None = unbounded / "Max").
CLIP_COUNT_PRESETS: dict[str, Optional[int]] = {
    "auto": None,
    "1": 1,
    "3": 3,
    "5": 5,
    "10": 10,
    "max": None,
}


def resolve_length_range(option: str) -> tuple[float, float, float]:
    """Return ``(min, max, target)`` seconds for a Clip Length UI option."""
    return CLIP_LENGTH_PRESETS.get((option or "auto").lower(), CLIP_LENGTH_PRESETS["auto"])


def resolve_max_clips(option: str) -> Optional[int]:
    """Return the max clip count for a Number of Clips UI option (None = all)."""
    key = (option or "auto").lower()
    return CLIP_COUNT_PRESETS.get(key, None)


def fixed_length_segments(
    total_duration: float,
    target: float,
    min_len: float = 3.0,
) -> list[Segment]:
    """Split ``[0, total_duration]`` into consecutive chunks of ``target`` secs.

    A trailing chunk shorter than ``min_len`` is merged into the previous one so
    we never emit a tiny sliver.
    """
    if total_duration <= 0:
        return []
    segments: list[Segment] = []
    start = 0.0
    while start < total_duration:
        end = min(start + target, total_duration)
        segments.append(Segment(start, end))
        start = end

    if len(segments) >= 2 and segments[-1].duration < min_len:
        last = segments.pop()
        segments[-1] = Segment(segments[-1].start, last.end)
    return segments


def detect_silences(
    path: str | Path,
    noise_db: float = -30.0,
    min_silence: float = 0.4,
) -> list[tuple[float, float]]:
    """Return a list of ``(start, end)`` silence intervals via ffmpeg.

    Uses the ``silencedetect`` audio filter and parses its stderr log.
    """
    cmd = [
        settings.ffmpeg_binary, "-i", str(path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    # silencedetect writes to stderr; this command is expected to "succeed".
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise FFmpegError(f"Binary not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        proc = exc  # still parse whatever was emitted

    log = (getattr(proc, "stderr", "") or "")
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    return list(zip(starts, ends))


def silence_based_segments(
    path: str | Path,
    total_duration: float,
    min_len: float,
    max_len: float,
    noise_db: float = -30.0,
    min_silence: float = 0.4,
) -> list[Segment]:
    """Segment a video at natural silence boundaries.

    Silence midpoints are used as candidate cut points. We accumulate speech
    until adding more would exceed ``max_len``, preferring to cut at a silence
    point once past ``min_len``. Falls back to fixed-length chunking when no
    usable silences are found.
    """
    silences = detect_silences(path, noise_db=noise_db, min_silence=min_silence)
    cut_points = sorted(
        p for s, e in silences if 0 < (p := (s + e) / 2) < total_duration
    )

    if not cut_points:
        return fixed_length_segments(total_duration, target=max_len, min_len=min_len)

    segments: list[Segment] = []
    start = 0.0
    for point in cut_points:
        length = point - start
        if length < min_len:
            continue  # too short; keep accumulating
        if length >= max_len:
            # Silence too far away; hard-cut at max_len to respect the ceiling.
            while point - start >= max_len:
                segments.append(Segment(start, start + max_len))
                start += max_len
            if point - start >= min_len:
                segments.append(Segment(start, point))
                start = point
        else:
            segments.append(Segment(start, point))
            start = point

    # Tail.
    if total_duration - start >= min_len:
        segments.append(Segment(start, total_duration))
    elif segments:
        segments[-1] = Segment(segments[-1].start, total_duration)
    else:
        segments.append(Segment(start, total_duration))

    return segments


def segment_video(
    path: str | Path,
    total_duration: float,
    clip_length: str = "auto",
    strategy: str = "silence",
    max_clips: Optional[int] = None,
) -> list[Segment]:
    """Produce clip segments for a video.

    Args:
        path: Source video path (needed for silence analysis).
        total_duration: Probed duration in seconds.
        clip_length: UI Clip Length option (see :data:`CLIP_LENGTH_PRESETS`).
        strategy: ``"silence"`` (default) or ``"fixed"``.
        max_clips: Optional cap on the number of clips returned.

    Returns:
        Ordered list of :class:`Segment`. When capped, the longest segments are
        kept (a simple heuristic standing in for real scoring in later phases).
    """
    min_len, max_len, target = resolve_length_range(clip_length)

    if strategy == "fixed":
        segments = fixed_length_segments(total_duration, target=target, min_len=min_len)
    elif strategy == "silence":
        segments = silence_based_segments(path, total_duration, min_len, max_len)
    else:
        raise ValueError(f"Unknown strategy '{strategy}'. Use 'silence' or 'fixed'.")

    if max_clips is not None and len(segments) > max_clips:
        # Keep the longest segments, then restore chronological order.
        segments = sorted(segments, key=lambda s: s.duration, reverse=True)[:max_clips]
        segments = sorted(segments, key=lambda s: s.start)

    return segments
