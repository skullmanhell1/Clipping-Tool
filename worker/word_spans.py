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

**Measured again, from the other end, after a desync was reported. T11 still is not the answer.**

The note above says a 20 ms envelope would be needed. That is correct, and one now exists —
``evaluation.caption_timing.speech_mask``, at ``ENVELOPE_HOP_S = 0.02``. It is in `evaluation/`
rather than here on purpose: an instrument may spend a second audio pass, the render path may not,
so its existence does not lift R7.8. What it did do is make the premise testable, and the numbers
came out against building T11:

* On a 120 s source, whisper's own word spans already overlap the 20 ms speech mask **81.4%**
  (IoU), best-fit lag **+0.10 s**. There is no gross mis-timing for snapping to recover.
* Across ten rendered clips the median best-fit lag was **-0.04 s** — two envelope frames, below
  the 100 ms the M10 instrument records as perceptible.
* The four clips that looked worst had best-fit lags of **+1.52, -1.52, -1.16 and +1.58 s** and
  gained only 3-10 points of overlap at that lag. Disagreeing signs plus a marginal gain is the
  signature of a spurious alignment in continuous speech, not a shift. A constant compensation
  cannot fix errors that point both ways, and snapping to onsets in near-continuous speech has
  onsets everywhere to choose from.
* Raising ASR precision does not help either, which bounds how much of the residual is ASR
  quantisation at all: ``small``/``int8`` (the default) scored **81.4%**, ``small``/``float32``
  **80.7%**, ``medium``/``int8`` **79.6%**, ``medium``/``float32`` **81.1%**. The cheapest
  configuration is the most accurate of the four, and the two slower ones are worse.

So the residual is per-word ASR jitter of a few tens of milliseconds, distributed both ways. Onset
snapping against an envelope would not address it, because it has no way to tell which of the many
onsets under a word is the one that word began at. Reproduce any of the above with
``scripts/measure_caption_sync.py``.

**Forced alignment was then built as a prototype and also rejected — and the way it failed is worth
more than the result.**

The paragraph above suggested forced alignment as the fix for the residual. It was tried:
torchaudio's ``MMS_FA`` CTC aligner, on the same source. Two findings, in the order they arrived.

*First, it barely moved the number it was meant to fix.* Median edge-anchored error went 130 ms to
110 ms, a 15% improvement, for a **1.18 GB** model download and a ``torch`` dependency. Below the
20% threshold set before running it, so: rejected on cost.

*Second, and this is the part that matters, it produced a wrong answer that looked completely
right.* Comparing whisper's word starts against the aligner's gave a median difference of **-94 ms,
-104 ms and -105 ms** across three recordings — two different windows of one voice, plus a second,
synthesised voice. Consistent sign, 12 ms spread, three sources. Every test for "this is a real
systematic bias" passed, and the indicated fix was a calibrated +100 ms shift of every word start.

That fix would have injected 100 ms of error into a component that was already correct. Checked
against **pause-preceded words**, where 300 ms of silence means the audio's rising edge settles the
onset with no model involved, whisper reads **-10 ms, -20 ms and +50 ms**. It is accurate. The
100 ms belonged to ``MMS_FA``: a CTC span opens at the first strongly-voiced frame, so it starts
*after* fricatives and plosive releases that are genuinely part of the word.

Both traps are now instrumented rather than only described. ``evaluation.caption_timing.
verifiable_word_errors`` measures word onsets **only** where a real pause makes them verifiable, and
skips the rest instead of estimating them — which is what the 130 ms figure was: a number reported
for every word when only about one in ten carried information. Its tests include the continuous-speech
case, asserting that nothing is reported there.

The transferable lesson, since this cost two rounds: a reference is a hypothesis, not a truth. Two
references disagreeing is information, and the one that can be checked against physics wins.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, is_dataclass, replace
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


def _rebuildable(span: Any) -> bool:
    """Whether ``dataclasses.replace`` can produce an adjusted copy of ``span``.

    Being *readable* and being *rebuildable* are different questions, and conflating them raised
    ``TypeError: replace() should be called on dataclass instances`` from inside the caption
    renderer. The caption paths are duck-typed on purpose -- ``captions._word_bounds`` accepts
    anything with ``start``/``end``, and ``captions._Uppercased`` is a ``__slots__`` wrapper rather
    than a dataclass -- so "it has the attributes I read" does not imply "I can copy it".

    ``is_dataclass`` is true for a dataclass *class* as well as an instance, hence the ``isinstance``
    exclusion: a class object would pass the check and then fail the copy.
    """
    return is_dataclass(span) and not isinstance(span, type)


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
    if not all(_rebuildable(span) for _bounds_, span in parsed):
        # Readable but not copyable. Refused for the *whole* sequence rather than per span, and that
        # is the substance rather than caution: repairing only the copyable half would emit a
        # timeline that is neither the transcript's nor a corrected one, and would report repairs
        # while leaving the overlaps that motivated them. An empty report is then accurate.
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
        if limit is not None and end > limit + 1e-9:
            # R8.5, and it was missing.
            #
            # The pre-check tested ordering, sign and the floor but never the cue boundary, so a
            # span outliving its own cue was declared compliant and returned untouched -- the one
            # defect `hygiene_for_cue` exists to catch. It made `cue_end` inert for any sequence
            # that was otherwise well-formed, which is most of them: the repair below was reachable
            # only when some *other* fault had already failed this check.
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

        # The latest this span may end: the next span's start, or -- for the last one -- the cue's
        # own end. Which of the two it was decides how the repair is *reported*: truncating against
        # a neighbour is de-overlapping (R8.2), truncating against the cue boundary is R8.5, and a
        # clip where every repair was the latter says something different about its transcript.
        if index + 1 < len(parsed):
            ceiling = parsed[index + 1][0][0]  # type: ignore[index]
            bounded_by_cue = False
        else:
            ceiling = limit
            bounded_by_cue = True

        if ceiling is not None and end > ceiling:
            end = max(start, ceiling - SPAN_EPSILON)
            if bounded_by_cue:
                report.clamped_to_cue += 1
            else:
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
