"""Transcript-based trimming (U4): struck-out words become a render plan.

The user edits a clip by striking words out of its transcript; the frontend sends the
resulting **cut** ranges and this module turns them into the keep segments the renderer
already knows how to concatenate — :func:`worker.effects.filler.apply_keep_intervals`.
No new ffmpeg path is introduced, because a cut list and filler removal are the same
operation with a different reason for wanting the region gone.

**Cuts are the wire format, not keeps.** Three reasons, all of which bit an earlier
sketch that sent keeps:

* A cut list is what a transcript editor naturally produces ("remove these words").
  Keeps are its complement, which the client can only compute if it knows the exact
  clip duration the renderer will use — and after AU7 edge-silence trimming, it does
  not.
* Cuts compose with filler removal by **union**: both features want regions gone, so
  the render keeps what neither asked to remove. Two independent *keep* lists have no
  correct way to combine — intersecting them is right, but only because they are
  complements of removals, which is the cut representation wearing a disguise.
* An empty cut list is unambiguously "change nothing". An empty keep list would mean
  "remove everything", so a dropped field would destroy the clip rather than no-op.

Refusals are explicit rather than approximate, following the rest of the pipeline: a
cut list that would leave nothing behind, or one long enough that the filter graph
becomes the problem, is reported as a marker on the clip record and the clip renders
as it would have without it. An absent feature with no explanation is
indistinguishable from a broken one.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from worker.effects.filler import Interval

#: Keep segments shorter than this are dropped rather than rendered. Two consecutive
#: cuts that leave a sliver between them produce a frame or two of video and an
#: audible click, which is not what "keep this word" meant. Matches the floor
#: ``filler.plan_keep_intervals`` already applies, so a clip trimmed by both features
#: has one minimum-segment rule rather than two.
MIN_SEGMENT_S = 0.2

#: Cuts closer together than this are coalesced. Word boundaries from ASR are rarely
#: exactly adjacent, and a 5 ms gap between two struck words is not a segment worth
#: keeping — it is rounding.
MERGE_GAP_S = 0.02

#: Upper bound on cuts honoured in one request. ``apply_keep_intervals`` builds a
#: ``trim``/``atrim`` pair per keep and one ``concat`` across all of them, so the
#: filter graph grows linearly with the cut count; a few hundred is already a graph
#: measured in tens of kilobytes. This is a guard against a runaway or hostile
#: request, not a considered editorial limit.
MAX_CUTS = 200

#: Recorded on the clip when a cut list was applied.
MARKER = "transcript_trim"

#: Recorded, with a reason suffix, when a cut list was *not* applied.
REFUSED_MARKER = "transcript_trim_refused"


@dataclass
class TrimPlan:
    """The outcome of resolving a cut list against a clip."""

    keeps: list[Interval] = field(default_factory=list)
    removed_seconds: float = 0.0
    cut_count: int = 0
    #: Empty when the plan is usable; otherwise the reason it was declined.
    refusal: str = ""

    @property
    def changed(self) -> bool:
        """True when applying this plan would actually shorten the clip.

        A refused plan is never ``changed``: ``keeps`` then describes what the clip
        was already going to be, so rendering it would be a re-encode for nothing.
        """
        return not self.refusal and self.removed_seconds > 0.01

    @property
    def marker(self) -> str:
        """The clip-record marker describing what happened."""
        if self.refusal:
            return f"{REFUSED_MARKER}:{self.refusal}"
        return MARKER


def _coerce(item: object) -> tuple[float, float] | None:
    """Read one cut as a ``(start, end)`` pair, or ``None`` if it is not one.

    Accepts the three shapes a cut arrives in — an object with ``start``/``end``
    (``Interval``, a pydantic model), a mapping (parsed JSON), or a two-element
    sequence (the compact wire form) — so callers do not each convert.
    """
    start: object = getattr(item, "start", None)
    end: object = getattr(item, "end", None)
    if start is None or end is None:
        if isinstance(item, Mapping):
            start, end = item.get("start"), item.get("end")
        elif (
            isinstance(item, Sequence)
            and not isinstance(item, str | bytes | bytearray)
            and len(item) == 2
        ):
            start, end = item[0], item[1]
        else:
            return None
    try:
        lo, hi = float(start), float(end)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # NaN and infinity survive float() and then poison every comparison downstream:
    # sorting is undefined and `end - start` is not a duration. Reject them here
    # rather than letting them reach an ffmpeg argument.
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return None
    return lo, hi


def normalise_cuts(cuts: Iterable[object] | None, duration: float) -> list[Interval]:
    """Clamp, drop and coalesce a raw cut list into disjoint ascending intervals.

    Unusable entries are dropped rather than raising: the list comes from a UI, and
    one stale word offset should not cost the user the whole edit. What *is* rejected
    loudly is a cut list that has no usable entries at all — the caller sees an empty
    result and can tell nothing was applied.
    """
    span = max(0.0, float(duration))
    if not cuts or span <= 0.0:
        return []

    spans: list[Interval] = []
    for item in cuts:
        pair = _coerce(item)
        if pair is None:
            continue
        lo, hi = pair
        if hi < lo:
            lo, hi = hi, lo
        lo = max(0.0, min(lo, span))
        hi = max(0.0, min(hi, span))
        if hi - lo <= 0.0:
            continue
        spans.append(Interval(lo, hi))

    if not spans:
        return []

    spans.sort(key=lambda s: s.start)
    merged = [Interval(spans[0].start, spans[0].end)]
    for cur in spans[1:]:
        last = merged[-1]
        if cur.start <= last.end + MERGE_GAP_S:
            last.end = max(last.end, cur.end)
        else:
            merged.append(Interval(cur.start, cur.end))
    return merged


def _complement(regions: Sequence[Interval], duration: float) -> list[Interval]:
    """The parts of ``[0, duration]`` that ``regions`` does not cover.

    ``regions`` must already be disjoint and ascending — :func:`normalise_cuts`
    guarantees both. No minimum-segment filtering happens here: that is applied after
    any intersection, so a sliver created by combining two features is judged once
    against the final timeline rather than twice against intermediate ones.
    """
    keeps: list[Interval] = []
    cursor = 0.0
    for region in regions:
        if region.start > cursor:
            keeps.append(Interval(round(cursor, 3), round(region.start, 3)))
        cursor = max(cursor, region.end)
    if duration > cursor:
        keeps.append(Interval(round(cursor, 3), round(duration, 3)))
    return keeps


def _intersect(a: Sequence[Interval], b: Sequence[Interval]) -> list[Interval]:
    """Overlapping parts of two disjoint ascending interval lists."""
    out: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i].start, b[j].start)
        hi = min(a[i].end, b[j].end)
        if hi > lo:
            out.append(Interval(round(lo, 3), round(hi, 3)))
        # Advance whichever interval ends first; a tie advances `a` and the next
        # comparison against the same `b` finds no overlap, so neither is skipped.
        if a[i].end <= b[j].end:
            i += 1
        else:
            j += 1
    return out


def plan_cuts(
    cuts: Iterable[object] | None,
    duration: float,
    *,
    base_keeps: Sequence[Interval] | None = None,
) -> TrimPlan:
    """Resolve ``cuts`` against a clip of ``duration`` seconds.

    Args:
        cuts: clip-relative ranges to remove, in any shape :func:`_coerce` accepts.
        duration: the clip's duration *before* trimming, in seconds.
        base_keeps: keeps another feature already decided on — in practice filler
            removal's plan. The result is intersected with these, so both removals
            are honoured rather than the later one replacing the earlier.

    Returns a :class:`TrimPlan`. When there is nothing to do, or when the request is
    declined, ``keeps`` describes the clip as it would have been without any cut list,
    so a caller can render ``plan.keeps`` unconditionally.
    """
    span = max(0.0, float(duration))
    fallback = list(base_keeps) if base_keeps else ([Interval(0.0, span)] if span > 0 else [])
    ordered = normalise_cuts(cuts, span)
    if not ordered:
        return TrimPlan(keeps=fallback, removed_seconds=0.0, cut_count=0)

    if len(ordered) > MAX_CUTS:
        return TrimPlan(keeps=fallback, cut_count=len(ordered), refusal="too_many_cuts")

    keeps = _complement(ordered, span)
    if base_keeps:
        keeps = _intersect(keeps, list(base_keeps))
    keeps = [k for k in keeps if k.duration >= MIN_SEGMENT_S]
    if not keeps:
        return TrimPlan(keeps=fallback, cut_count=len(ordered), refusal="empty_result")

    # Measured against what the clip was *already* going to be, not against the full
    # duration. Cuts that fall entirely inside a region filler removal had claimed
    # remove nothing further, and re-encoding to achieve nothing is worth avoiding.
    base_total = sum(k.duration for k in fallback) if fallback else span
    kept = sum(k.duration for k in keeps)
    return TrimPlan(
        keeps=keeps,
        removed_seconds=round(max(0.0, base_total - kept), 3),
        cut_count=len(ordered),
    )
