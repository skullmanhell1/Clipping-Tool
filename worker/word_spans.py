"""Word-span hygiene for caption rendering (C23), and why T11 is not here.

Karaoke fills, per-word highlights and the kinetic engine all key on individual word times. Whisper
occasionally emits spans that are degenerate for rendering — zero-length, overlapping the next word,
or out of order after a rebase — and every one of those becomes a visible defect: a highlight that
flashes for a single frame, or two words lit at once.

``words_to_cues`` has cue-level ceilings and `C24` added cue-level floors, but neither looks *inside*
a cue at the spans the fill actually sweeps across. This does.

**Non-overlap outranks the minimum duration** (R8.4). Two words highlighted simultaneously is a
rendering fault; one word highlighted briefly is a legibility problem. The same ordering `C24`
applies at cue level, for the same reason.

**Nothing here touches the cached transcript.** Spans are copied before adjustment, so the T8
transcript cache keeps the ASR's own output and a re-render produces the same corrections rather
than compounding them.

---

**T11 (snapping word starts to audio onsets) is deliberately absent, and this is a measurement
rather than an omission.**

R7.8 requires reusing the energy envelope already computed for the source and forbids a second audio
pass. That envelope is built at ``ENVELOPE_WINDOW_S = 1.0`` — one RMS reading per second — because
`S2` uses it to compare candidate *windows*, where a second of resolution is ample.

Measured on an 8-second source containing 20 distinct bursts at 2.5 per second:

* the envelope produced **8 readings**, one per second;
* ``detect_onsets`` found **zero onsets**.

One-second RMS averaging smooths word-rate transients away completely. There is nothing in that
envelope to snap a word start to, so a T11 built on it would either move nothing or move spans onto
coarse second boundaries — displacing them by up to 500 ms, which is far worse than the drift it is
meant to correct.

Implementing it needs a word-scale envelope, on the order of 20 ms readings, and that is precisely
the second audio pass R7.8 rules out. So the requirement is self-limiting as written: its constraint
and its purpose are incompatible. Recorded here so the next person measures the envelope before
building against it rather than after.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

#: Minimum rendered duration for one word span, in seconds. ``0.0`` disables the floor.
#:
#: Two frames at 30 fps is 67 ms; below about 80 ms a highlight reads as a flicker rather than as a
#: word being lit. PROVISIONAL: the value that matters is perceptual and only `M12` preference trials
#: could settle it, so the floor ships off and the mechanism ships on.
DEFAULT_MIN_SPAN_SECONDS = 0.0

#: Smallest gap kept between one span's end and the next span's start, in seconds.
#:
#: Not zero. Adjacent spans that share an exact boundary render as a highlight with no visible
#: transition, and floating-point comparison at the boundary makes which word is lit depend on
#: rounding. One millisecond is inaudible and unambiguous.
SPAN_EPSILON = 0.001


class HasSpan(Protocol):
    """Anything with ``start``/``end`` floats — `transcribe.Word` and the kinetic engine's spans."""

    start: float
    end: float


@dataclass
class Hygiene_Report:
    """What span hygiene changed, for the clip record (R8.8)."""

    reordered: int = 0
    deoverlapped: int = 0
    lengthened: int = 0
    clamped_to_cue: int = 0

    @property
    def altered(self) -> int:
        return self.reordered + self.deoverlapped + self.lengthened + self.clamped_to_cue

    @property
    def markers(self) -> list[str]:
        """Empty when nothing changed: a marker on every clip is noise."""
        if not self.altered:
            return []
        return [f"word_spans_repaired:{self.altered}"]

    def to_dict(self) -> dict:
        return {
            "reordered": self.reordered,
            "deoverlapped": self.deoverlapped,
            "lengthened": self.lengthened,
            "clamped_to_cue": self.clamped_to_cue,
            "altered": self.altered,
        }


def _bounds(span: Any) -> tuple[float, float] | None:
    try:
        return float(span.start), float(span.end)
    except (AttributeError, TypeError, ValueError):
        return None


def apply_hygiene(
    spans: Sequence[Any],
    *,
    min_seconds: float = DEFAULT_MIN_SPAN_SECONDS,
    cue_end: float | None = None,
) -> tuple[list[Any], Hygiene_Report]:
    """Return spans that are ordered, non-overlapping and long enough to see (C23).

    A sequence already satisfying all three is returned **as the same objects** (R8.7) — the
    strongest available form of bit-identical, and what makes the disabled floor a true no-op.

    Order of repair, and each step's reason:

    1. **Monotonic starts** (R8.1). A start earlier than its predecessor's is pulled forward. Out of
       order, a fill sweeps backwards, which reads as the caption glitching.
    2. **Non-overlap** (R8.2). A span ending after the next one begins is truncated. Two words lit at
       once is a rendering fault.
    3. **Minimum duration** (R8.3), applied last and yielding to the two above (R8.4) — and never
       past the cue's own end (R8.5), since a highlight outliving its line has nothing to highlight.
    """
    report = Hygiene_Report()
    if not spans:
        return [], report

    parsed = [(_bounds(s), s) for s in spans]
    if any(bounds is None for bounds, _ in parsed):
        # A span that cannot be read is left entirely alone rather than guessed at; the caller's
        # renderer already tolerates whatever shape it is.
        return list(spans), report

    floor = max(0.0, float(min_seconds))
    limit = float(cue_end) if cue_end is not None else None

    # A cheap pre-check so the common case allocates nothing and returns the inputs themselves.
    compliant = True
    previous_end = None
    for bounds, _span in parsed:
        start, end = bounds  # type: ignore[misc]
        if previous_end is not None and start < previous_end - 1e-9:
            compliant = False
            break
        if end < start:
            compliant = False
            break
        if floor > 0 and (end - start) < floor - 1e-9:
            # Flagged whether or not there is room to fix it.
            #
            # An earlier version only flagged spans that *could* reach the floor, on the reasoning
            # that an unfixable span needs no work. That made `clamped_to_cue` unreachable: any span
            # with room enough to be flagged also had room enough to be fully lengthened, so the
            # partial case never occurred and the counter was dead code the tests could not observe.
            #
            # Flagging unconditionally means a span pinned by its neighbour is lengthened as far as
            # it can go and the shortfall is *reported* -- which is what makes R8.4's preference
            # visible rather than silent.
            compliant = False
            break
        previous_end = end
    if compliant:
        return list(spans), report

    out: list[Any] = []
    cursor = None
    for index, (bounds, span) in enumerate(parsed):
        start, end = bounds  # type: ignore[misc]
        original = (start, end)

        if cursor is not None and start < cursor:
            start = cursor
            report.reordered += 1
        if end < start:
            end = start

        # The latest this span may end.
        if index + 1 < len(parsed):
            ceiling = parsed[index + 1][0][0]  # type: ignore[index]
        else:
            ceiling = limit

        if ceiling is not None and end > ceiling:
            end = max(start, ceiling - SPAN_EPSILON)
            report.deoverlapped += 1

        if floor > 0 and (end - start) < floor:
            wanted = start + floor
            if ceiling is None:
                end = wanted
                report.lengthened += 1
            else:
                allowed = max(start, ceiling - SPAN_EPSILON)
                if allowed > end:
                    end = min(wanted, allowed)
                    report.lengthened += 1
                    if end < wanted:
                        # R8.4/R8.5: non-overlap and the cue boundary win. Counted separately so a
                        # clip whose spans are all pinned by their neighbours is distinguishable from
                        # one where the floor was simply met.
                        report.clamped_to_cue += 1

        cursor = end + SPAN_EPSILON
        out.append(span if (start, end) == original else replace(span, start=start, end=end))

    return out, report


def hygiene_for_cue(
    spans: Sequence[Any], cue_start: float, cue_end: float, *, min_seconds: float = 0.0
) -> tuple[list[Any], Hygiene_Report]:
    """:func:`apply_hygiene` bounded by a cue's own window (R8.5).

    A convenience for the rendering paths, which always know the containing cue — and the reason
    R8.5 exists: a word span extending past the line it belongs to is highlighting text that is no
    longer on screen.
    """
    repaired, report = apply_hygiene(spans, min_seconds=min_seconds, cue_end=cue_end)
    return repaired, report
