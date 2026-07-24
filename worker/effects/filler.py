"""Filler-word and dead-air removal.

Uses the word-level transcript to cut out disfluencies ("um", "uh", ...) and
long silent pauses, tightening the clip. The timeline changes, so the module
also **rebases** the surviving words onto the new (shortened) timeline, which
keeps captions and emoji overlays in sync afterwards.

Pipeline:
    plan_keep_intervals(words, duration) -> keep segments (clip-relative)
    apply_keep_intervals(raw_clip, keeps) -> concatenated, tightened clip
    rebase_words(words, keeps)            -> words on the new timeline
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from config import settings
from worker.ffmpeg_utils import _run

# Disfluencies removed by default. Kept deliberately conservative so real words
# (e.g. "like" as a verb) are never cut.
FILLER_WORDS = {
    "um", "umm", "ummm", "uh", "uhh", "uhhh", "er", "err", "erm",
    "ah", "ahh", "hmm", "mmm", "mm", "uhm",
}

_WORD_RE = re.compile(r"[a-z']+")


@dataclass
class Interval:
    """A ``[start, end]`` time interval in seconds."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class FillerPlan:
    """Result of planning: keep segments plus what was removed."""

    keeps: list[Interval]
    removed_fillers: int
    removed_seconds: float

    @property
    def changed(self) -> bool:
        """True when the plan actually removes something."""
        return self.removed_seconds > 0.01


def _norm(text: str) -> str:
    m = _WORD_RE.findall((text or "").lower())
    return m[0] if m else ""


def _merge(intervals: list[Interval], gap: float = 0.02) -> list[Interval]:
    """Sort and coalesce overlapping/near-adjacent intervals."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda i: i.start)
    merged = [Interval(ordered[0].start, ordered[0].end)]
    for cur in ordered[1:]:
        last = merged[-1]
        if cur.start <= last.end + gap:
            last.end = max(last.end, cur.end)
        else:
            merged.append(Interval(cur.start, cur.end))
    return merged


def plan_keep_intervals(
    words: list,
    duration: float,
    max_gap: float = 0.7,
    pad: float = 0.12,
    min_segment: float = 0.2,
) -> FillerPlan:
    """Plan which parts of ``[0, duration]`` to keep.

    Args:
        words: clip-relative words (``.start``/``.end``/``.text``).
        duration: clip duration in seconds.
        max_gap: silences longer than this (between words, or at the head/tail)
            are trimmed, leaving ``pad`` seconds of breathing room.
        pad: padding kept around speech when trimming pauses.
        min_segment: keep segments shorter than this are dropped.

    Returns a :class:`FillerPlan`. When nothing meaningful is removed the plan
    keeps the whole clip (a safe no-op).
    """
    removed: list[Interval] = []
    filler_count = 0

    speech: list[Interval] = []
    for w in words:
        start = float(getattr(w, "start", 0.0))
        end = float(getattr(w, "end", start))
        if end <= start:
            continue
        if _norm(getattr(w, "text", "")) in FILLER_WORDS:
            removed.append(Interval(max(0.0, start - 0.02), min(duration, end + 0.02)))
            filler_count += 1
        else:
            speech.append(Interval(start, end))

    speech.sort(key=lambda i: i.start)

    # Leading dead air.
    if speech and speech[0].start > max_gap:
        removed.append(Interval(0.0, max(0.0, speech[0].start - pad)))
    # Gaps between consecutive speech words.
    for prev, cur in zip(speech, speech[1:]):
        gap = cur.start - prev.end
        if gap > max_gap:
            removed.append(Interval(prev.end + pad, cur.start - pad))
    # Trailing dead air.
    if speech and duration - speech[-1].end > max_gap:
        removed.append(Interval(speech[-1].end + pad, duration))

    removed = [r for r in _merge(removed) if r.duration > 0.05]
    if not removed:
        return FillerPlan([Interval(0.0, duration)], 0, 0.0)

    # Complement -> keep segments.
    keeps: list[Interval] = []
    cursor = 0.0
    for r in removed:
        if r.start - cursor >= min_segment:
            keeps.append(Interval(round(cursor, 3), round(r.start, 3)))
        cursor = max(cursor, r.end)
    if duration - cursor >= min_segment:
        keeps.append(Interval(round(cursor, 3), round(duration, 3)))

    if not keeps:
        # Everything would be removed — bail out to a safe no-op.
        return FillerPlan([Interval(0.0, duration)], 0, 0.0)

    removed_seconds = round(duration - sum(k.duration for k in keeps), 3)
    return FillerPlan(keeps, filler_count, max(0.0, removed_seconds))


def rebase_words(words: list, keeps: list[Interval]):
    """Return words remapped onto the tightened timeline defined by ``keeps``.

    Words whose midpoint lies inside a removed region are dropped; survivors are
    shifted so their timing matches the concatenated output. The returned list
    contains :class:`worker.transcribe.Word` objects.
    """
    from worker.transcribe import Word

    # Build (source_start, source_end, new_offset) for each keep segment.
    mapped: list[tuple[float, float, float]] = []
    offset = 0.0
    for k in keeps:
        mapped.append((k.start, k.end, offset))
        offset += k.duration

    out: list[Word] = []
    for w in words:
        ws = float(getattr(w, "start", 0.0))
        we = float(getattr(w, "end", ws))
        mid = (ws + we) / 2.0
        for ks, ke, new_off in mapped:
            if ks <= mid < ke:
                ns = new_off + (max(ws, ks) - ks)
                ne = new_off + (min(we, ke) - ks)
                out.append(Word(start=round(ns, 3), end=round(max(ns, ne), 3),
                                text=getattr(w, "text", ""),
                                probability=getattr(w, "probability", 1.0)))
                break
    return out


def apply_keep_intervals(
    source: str | Path, keeps: list[Interval], dest: str | Path
) -> Path:
    """Concatenate ``keeps`` from ``source`` into ``dest`` in one ffmpeg pass.

    Uses ``trim``/``atrim`` + ``concat``. Assumes the source has audio (clips
    produced by the pipeline always do).
    """
    source = Path(source)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    parts: list[str] = []
    labels: list[str] = []
    for i, k in enumerate(keeps):
        parts.append(
            f"[0:v]trim=start={k.start:.3f}:end={k.end:.3f},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={k.start:.3f}:end={k.end:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
        labels.append(f"[v{i}][a{i}]")
    concat = f"{''.join(labels)}concat=n={len(keeps)}:v=1:a=1[v][a]"
    graph = ";".join(parts + [concat])

    cmd = [
        settings.ffmpeg_binary, "-y", "-i", str(source),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dest),
    ]
    _run(cmd)
    return dest
