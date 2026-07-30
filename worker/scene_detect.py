"""Snap clip starts to shot boundaries so clips don't open mid-shot (S9).

A clip that begins two seconds into a shot opens on a fragment - half a gesture, the tail of a
camera move - and reads as a careless cut before the viewer has heard a word. The S1 harness
made the scale of it visible: at IoU 0.7, which is the threshold that asks whether *boundaries*
are right rather than whether the right moment was found, the selector scored **zero across the
board**.

Three implementation decisions worth stating.

**ffmpeg's scene score, not PySceneDetect.** The plan names PySceneDetect as the standard tool and
it is, but this needs one number - "is there a hard cut near here" - and ffmpeg is already the
dependency every other stage shells out to. Adding a package to answer a question the existing
binary answers is a cost with no matching benefit.

**Narrow windows, not a full scan.** Detection decodes video, so scanning an hour-long source to
move a boundary by under a second is wildly disproportionate. Only a couple of seconds either
side of each candidate start is examined, which is a few dozen frames per candidate.

**Luma-based detection has a real blind spot, and it is not a bug to work around.** ffmpeg scores
scene change on the luma plane, so a cut between two shots of similar brightness scores near zero
- ffmpeg's own ``red`` and ``green`` differ by one unit of luma and register as no cut at all.
That means this finds *most* hard cuts and misses some, which is exactly why every snap is capped
and optional: a missed cut leaves the boundary where the selector put it, which is the previous
behaviour and perfectly acceptable.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Optional, Sequence

from config import settings

#: ``pts_time`` of a frame the scene filter selected, relative to the seek point.
_PTS = re.compile(r"pts_time:([0-9.]+)")


def detect_cuts(
    path: Any,
    around: float,
    *,
    window: Optional[float] = None,
    threshold: Optional[float] = None,
) -> list[float]:
    """Absolute times of hard cuts within ``window`` seconds either side of ``around``.

    Returns ``[]`` on any failure - no ffmpeg, unreadable file, no cuts found - because a
    boundary that cannot be improved should simply stay where it is.

    The seek offset is added back: with ``-ss``, ffmpeg reports ``pts_time`` relative to the seek
    point, so a cut at 4.0 s scanned from 3.0 s is reported as 1.0. Forgetting that would snap
    every boundary towards the start of the video.
    """
    window = float(settings.scene_snap_window_s if window is None else window)
    threshold = float(settings.scene_snap_threshold if threshold is None else threshold)

    seek = max(0.0, float(around) - window)
    duration = window * 2.0
    command = [
        settings.ffmpeg_binary, "-nostdin", "-hide_banner",
        "-ss", f"{seek:.3f}", "-t", f"{duration:.3f}", "-i", str(path),
        "-filter:v", f"select='gt(scene,{threshold:g})',metadata=print:file=-",
        # No audio decoding: this is a video-only question, and decoding audio would roughly
        # double the work for nothing.
        "-an", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except Exception:
        return []

    # The filter prints to stdout (file=-) but ffmpeg's own log goes to stderr; scan both rather
    # than depend on which stream a given build chose.
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    cuts = []
    for match in _PTS.finditer(text):
        try:
            cuts.append(round(seek + float(match.group(1)), 3))
        except ValueError:
            continue
    return sorted(set(cuts))


def snap_start(
    start: float,
    end: float,
    cuts: Sequence[float],
    *,
    max_shift: Optional[float] = None,
    min_duration: float = 1.0,
) -> tuple[float, float]:
    """Move ``start`` onto the nearest cut, if one is close enough.

    Pure. ``end`` is never moved: the clip's ending is chosen by the selector for content
    reasons - a punchline, a completed thought - and a shot change near the end is not a reason
    to truncate it. Only the opening frame is a presentation problem.

    Guarantees: the shift never exceeds ``max_shift``, the clip never becomes shorter than
    ``min_duration``, and the range never inverts. If any of those would be violated the original
    values come back unchanged, because a boundary that was merely inelegant is better than one
    that is wrong.
    """
    max_shift = float(settings.scene_snap_max_shift_s if max_shift is None else max_shift)
    if not cuts or end <= start or max_shift <= 0:
        return start, end

    candidates = [
        cut for cut in cuts
        if abs(cut - start) <= max_shift and cut < end - min_duration and cut >= 0
    ]
    if not candidates:
        return start, end

    best = min(candidates, key=lambda cut: (abs(cut - start), cut))
    if end - best < min_duration:
        return start, end
    return round(best, 3), end


def snap_candidates(path: Any, candidates: Sequence[Any]) -> int:
    """Snap each candidate's start to a nearby shot boundary, in place (S9).

    Returns how many were moved. Best-effort throughout: a candidate whose window cannot be
    scanned keeps the boundary the selector chose.
    """
    if not settings.scene_snap_enabled or not candidates:
        return 0

    moved = 0
    for candidate in candidates:
        try:
            start, end = float(candidate.start), float(candidate.end)
        except (AttributeError, TypeError, ValueError):
            continue
        cuts = detect_cuts(path, start)
        if not cuts:
            continue
        new_start, new_end = snap_start(start, end, cuts)
        if new_start != start:
            candidate.start = new_start
            candidate.end = new_end
            moved += 1
    return moved
