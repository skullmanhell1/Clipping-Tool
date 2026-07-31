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
    "auto": (10.0, 90.0, 45.0),  # Auto (<90s)
    "<30s": (5.0, 30.0, 20.0),
    "30-60s": (30.0, 60.0, 45.0),
    "60-90s": (60.0, 90.0, 75.0),
    "90s-3min": (90.0, 180.0, 120.0),
}

# UI "Number of Clips" option -> max clips (None = unbounded / "Max").
CLIP_COUNT_PRESETS: dict[str, int | None] = {
    "auto": None,
    "1": 1,
    "3": 3,
    "5": 5,
    "10": 10,
    "max": None,
}


def resolve_length_range(option: str) -> tuple[float, float, float]:
    """Return ``(min, max, target)`` seconds for a Clip Length UI option.

    O7: when a platform output profile is active, its duration ceiling caps the range. A clip
    longer than the destination accepts is not a clip - it is a file that fails at upload after
    the whole render has been paid for, and the pre-flight check that catches it runs at the end.
    Capping here means the clip is *made* publishable instead of being rejected later.

    The floor is never raised, only the ceiling lowered, and the target is pulled down with it so
    a capped range cannot produce a target above its own maximum.
    """
    min_len, max_len, target = CLIP_LENGTH_PRESETS.get(
        (option or "auto").lower(), CLIP_LENGTH_PRESETS["auto"]
    )

    from worker import output_profiles

    ceiling = output_profiles.duration_ceiling_s()
    if ceiling is not None and ceiling > 0 and ceiling < max_len:
        max_len = max(min_len, float(ceiling))
        target = min(target, max_len)
    return min_len, max_len, target


def resolve_max_clips(option: str) -> int | None:
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
        settings.ffmpeg_binary,
        "-i",
        str(path),
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f",
        "null",
        "-",
    ]
    # silencedetect writes to stderr; this command is expected to "succeed".
    # The except branch below deliberately rebinds this to the error, whose stderr still
    # carries the silencedetect output worth parsing.
    proc: subprocess.CompletedProcess[str] | subprocess.CalledProcessError
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise FFmpegError(f"Binary not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        proc = exc  # still parse whatever was emitted

    log = getattr(proc, "stderr", "") or ""
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    # strict=False on purpose: a clip that ends *during* silence yields a silence_start with
    # no matching silence_end, so `starts` can legitimately be one longer. Dropping the
    # unterminated interval is the correct reading -- it has no end to cut at.
    return list(zip(starts, ends, strict=False))


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
    cut_points = sorted(p for s, e in silences if 0 < (p := (s + e) / 2) < total_duration)

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
    max_clips: int | None = None,
) -> list[Segment]:
    """Produce clip segments for a video.

    Args:
        path: Source video path (needed for silence analysis).
        total_duration: Probed duration in seconds.
        clip_length: UI Clip Length option (see :data:`CLIP_LENGTH_PRESETS`).
        strategy: ``"silence"`` (default) or ``"fixed"``.
        max_clips: Optional cap on the number of clips returned.

    Returns:
        Ordered list of :class:`Segment`. When ``max_clips`` caps the count, the *longest*
        segments are kept.

    Note:
        That longest-first capping is deliberately still here, and production no longer uses
        it. This module is pure geometry with no opinion about clip quality, and length is a
        poor proxy for it - the longest silence-delimited segment is usually the stretch where
        nobody paused. :func:`worker.selection._fallback` therefore calls this with
        ``max_clips=None`` and ranks the full set on measured signals instead (S11).

        It is kept rather than removed because the S1 evaluation harness needs a *longest*
        baseline to measure against, and a baseline that shares code with production would
        stop being an independent floor. Direct callers who genuinely want length-capping,
        including that baseline's own reference implementation, still get it.
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


# --------------------------------------------------------------------------- #
# Edge silence trimming (AU7)
# --------------------------------------------------------------------------- #
# A clip that opens on half a second of dead air reads as an amateur cut, and the first
# second is where a viewer decides whether to keep watching. Trailing silence is less costly
# but still pads the clip with nothing.
#
# The window is tightened rather than the audio being filtered: moving the cut points means
# no extra pass, and it keeps video and audio in step by construction. Filtering silence out
# of the audio alone would desynchronise them.

#: How much may be trimmed from each edge, in seconds.
#:
#: A cap is essential. ``silencedetect`` reports a *pause*, and a pause at a clip boundary is
#: often the intake of breath before the first word or the beat after a punchline - both of
#: which belong to the clip. Without a ceiling, a moment selected mid-pause could have
#: seconds removed and start abruptly on a syllable.
MAX_EDGE_TRIM_S = 1.25

#: Never trim a clip below this. A window that collapses is worse than dead air.
MIN_TRIMMED_DURATION_S = 1.0


def trim_edge_silence(
    start: float,
    end: float,
    silences: list[tuple[float, float]],
    *,
    max_trim: float = MAX_EDGE_TRIM_S,
    min_duration: float = MIN_TRIMMED_DURATION_S,
) -> tuple[float, float]:
    """Tighten ``[start, end]`` onto speech, given source-relative ``silences`` (AU7).

    Pure, and the only place the trimming policy lives - :func:`detect_silences` supplies the
    measurements and the pipeline supplies the window, so this can be tested exhaustively
    without ffmpeg.

    A silence only counts when it *straddles or touches* the boundary: a silence wholly
    inside the clip is a pause in the middle of speech, which is content. Both edges are
    capped by ``max_trim``, and the result is never shorter than ``min_duration`` nor
    inverted.
    """
    if end <= start:
        return start, end

    new_start, new_end = float(start), float(end)

    # Leading: a silence covering the start point moves it to where speech resumes.
    for silence_start, silence_end in silences:
        if silence_start <= new_start < silence_end:
            new_start = min(silence_end, start + max_trim)
            break

    # Trailing: a silence covering the end point moves it back to where speech stopped.
    for silence_start, silence_end in silences:
        if silence_start < new_end <= silence_end:
            new_end = max(silence_start, end - max_trim)
            break

    # Never invert, never collapse, never extend beyond the original window.
    new_start = max(start, min(new_start, end))
    new_end = min(end, max(new_end, start))
    if new_end - new_start < min_duration:
        return start, end
    return round(new_start, 3), round(new_end, 3)
