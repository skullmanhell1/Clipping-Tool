"""Per-speaker level matching (AU12).

A quiet guest against a loud host makes a viewer reach for the volume control mid-clip. `AU1`'s
two-pass ``loudnorm`` cannot fix it: it normalises the clip as a whole, so it moves both speakers by
the same amount and preserves the imbalance exactly.

Diarisation has existed here since `T6`'s offline path and **has never been used for gain** — the
plan's own note for AU12 says so. This uses it.

**The existing envelope is enough, and that is worth contrasting with T11.** Turn levels are measured
from the 1-second energy envelope `S2` already computes, so this adds no audio pass (R7.10). A
*turn* is seconds long, so one reading per second describes it well. `T11` needed to snap individual
*word* starts and one second of resolution erased the transients entirely — same envelope, opposite
verdict, because the thing being measured is a different size.

**Gain is ramped, never stepped** (R7.4). A step change in level at a turn boundary is an audible
click, and a click is a defect a viewer notices far more readily than the imbalance it was fixing.
The ramp is expressed as a piecewise-linear ``volume`` expression evaluated per frame, so it costs a
filter rather than a pass.

**Bounded** (R7.3). Diarisation misattributes turns, and an unbounded correction on a misattributed
one produces a large, confident, wrong level jump. A bound means the worst case is a partial
correction rather than a new defect.

**One honest gap.** R7.7 asks that gain not be applied to intervals diarisation attributed with *low
confidence*, and `Speaker_Turn` carries **no confidence field** — the offline path is described in its
own docstring as "attribution by proxy" and reports no score. So a minimum-turn-duration gate stands
in for it: a very short turn is disproportionately likely to be a misattribution, and skipping those
is the closest available approximation. It is named as a proxy in the marker rather than described as
confidence, because reporting a proxy as the thing it approximates is how `S5` would have gone wrong.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

#: Largest correction applied to any turn, in dB (R7.3).
#:
#: 6 dB is a doubling of perceived loudness and is enough to bring a noticeably quiet speaker into
#: line. Beyond it, a misattributed turn would produce a jump louder than the imbalance being fixed,
#: which is the failure this bound exists to prevent rather than a tuning preference.
MAX_GAIN_DB = 6.0

#: Ramp duration across a turn boundary, in seconds (R7.4).
#:
#: 120 ms. Long enough that the level change is inaudible as an event, short enough that the
#: correction is fully applied within the first syllable of the new speaker. A step here is a click.
RAMP_SECONDS = 0.12

#: Shortest turn that will be corrected, in seconds.
#:
#: The stand-in for R7.7's confidence gate, because `Speaker_Turn` carries no confidence. Under half
#: a second a turn is usually a cross-talk fragment or a misattributed breath, and correcting those
#: is how a level match becomes a level *wobble*. Named as a proxy wherever it is reported.
MIN_TURN_SECONDS = 0.5

#: Most turns a single clip's gain expression will describe.
#:
#: The expression nests one conditional per boundary, so its length grows with the turn count. Past
#: this the graph is the problem rather than the imbalance, and the honest outcome is to decline and
#: say so -- the same refusal `U4` makes for a cut list beyond 200 entries.
MAX_TURNS = 24

#: Level below which a turn is treated as having no measurable speech.
#:
#: Turns that are effectively silent must not be *boosted*: the loudest thing in them is room tone,
#: and lifting it is the one change guaranteed to make a clip worse.
SILENCE_FLOOR_DB = -50.0


@dataclass(frozen=True)
class Turn_Gain:
    """One turn's correction."""

    start: float
    end: float
    speaker: str
    level_db: float
    gain_db: float
    applied: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Turn_Gain_Plan:
    """The filter to apply, the per-turn detail, and the markers."""

    filter_chain: str = ""
    gains: tuple[Turn_Gain, ...] = ()
    markers: tuple[str, ...] = ()
    detail: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.filter_chain)

    @property
    def applied_range_db(self) -> tuple[float, float]:
        """``(min, max)`` of the gains actually applied, for the marker (R7.9)."""
        values = [g.gain_db for g in self.gains if g.applied]
        return (min(values), max(values)) if values else (0.0, 0.0)

    def to_dict(self) -> dict:
        low, high = self.applied_range_db
        return {
            "filter_chain": self.filter_chain,
            "gains": [g.to_dict() for g in self.gains],
            "markers": list(self.markers),
            "applied_range_db": [low, high],
            "detail": self.detail,
        }


def clamp_gain(gain_db: float) -> float:
    """Bound a correction to +/-:data:`MAX_GAIN_DB` (R7.3)."""
    try:
        value = float(gain_db)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return max(-MAX_GAIN_DB, min(MAX_GAIN_DB, value))


def turn_level_db(
    envelope: Sequence[tuple[float, float]], start: float, end: float
) -> float | None:
    """Mean level of ``[start, end]`` from the existing envelope, or ``None`` if unmeasurable.

    Reads the 1-second envelope `S2` already computed (R7.10). A turn is seconds long, so that
    resolution describes it well -- see the module docstring for why the same envelope was useless
    for `T11`.
    """
    # Half-open on purpose: ``[start, end)``.
    #
    # An earlier version used ``t < end + 1e-9`` and so included the reading *at* ``end``, which
    # belongs to the next turn. On a two-speaker clip split at 4.0 s that pulled one of the quiet
    # speaker's readings into the loud speaker's mean and measured -16.4 dB where the envelope plainly
    # said -14.0 -- understating the imbalance, and therefore under-correcting it, on every clip.
    # Each envelope reading describes the window *starting* at its timestamp, so the boundary reading
    # is the next turn's first, not this turn's last.
    readings = [db for t, db in envelope if start - 1e-9 <= t < end]
    if not readings:
        return None
    return statistics.fmean(readings)


def plan_turn_gain(
    turns: Sequence[Any],
    envelope: Sequence[tuple[float, float]],
    *,
    enabled: bool = False,
    diarization_available: bool = True,
) -> Turn_Gain_Plan:
    """Decide the per-turn corrections for one clip.

    ``turns`` must already be rebased onto the **delivered** timeline (R7.5) -- the pipeline rebases
    them after filler removal, and correcting against source-relative times would apply every gain at
    the wrong moment.

    ``diarization_available`` is passed in rather than probed, because R7.12 forbids enabling
    diarisation as a side effect: if the operator has it off, this must be unavailable and say so
    rather than quietly turning it on to get its own job done.
    """
    if not enabled:
        return Turn_Gain_Plan(detail="per-speaker level matching disabled")

    if not diarization_available:
        # R7.12. Naming the dependency rather than silently doing nothing, and explicitly not
        # enabling it: an audio feature that switches on a transcription-adjacent stage would be
        # spending someone else's budget.
        return Turn_Gain_Plan(
            markers=("turn_gain_unavailable:diarization_disabled",),
            detail="diarisation is disabled, and this will not enable it",
        )

    usable = [t for t in turns if _bounds(t) is not None]
    speakers = {str(getattr(t, "speaker_label", "")) for t in usable}
    if len(usable) < 2 or len(speakers) < 2:
        # R7.6. With one speaker there is nothing to balance, and `loudnorm` already sets the level.
        return Turn_Gain_Plan(
            markers=("turn_gain_skipped:single_speaker",),
            detail=f"{len(speakers)} speaker(s) in this clip; nothing to balance",
        )

    if len(usable) > MAX_TURNS:
        return Turn_Gain_Plan(
            markers=(f"turn_gain_skipped:too_many_turns:{len(usable)}",),
            detail=(
                f"{len(usable)} turns exceeds the {MAX_TURNS}-turn limit; the gain expression "
                "grows with the turn count, so past this the filter graph is the problem"
            ),
        )

    # Measure every turn first, then correct towards the median. The median rather than the mean
    # because one very quiet or very loud turn should not drag the target it is being corrected
    # towards -- which is the same reason `pitch_features` uses a median baseline.
    measured: list[tuple[Any, float | None]] = []
    for turn in usable:
        # `usable` was filtered on `_bounds(...) is not None`, so the fallback is unreachable -- but
        # it is written rather than asserted, because `assert` is a test construct and is stripped
        # under `-O`, which would turn an impossible case into a crash rather than a bad number.
        start, end = _bounds(turn) or (0.0, 0.0)
        measured.append((turn, turn_level_db(envelope, start, end)))

    levels = [level for _t, level in measured if level is not None and level > SILENCE_FLOOR_DB]
    if len(levels) < 2:
        # The measurements are reported even though nothing is applied. An earlier version returned
        # an empty `gains` tuple here, which meant an operator asking "why was my quiet guest not
        # lifted?" got a bare marker and no levels -- the same absent-explanation failure the marker
        # catalogue exists to prevent.
        return Turn_Gain_Plan(
            gains=tuple(
                Turn_Gain(
                    *(_bounds(turn) or (0.0, 0.0)),
                    str(getattr(turn, "speaker_label", "")),
                    level if level is not None else 0.0,
                    0.0,
                    False,
                    "no measurable speech" if level is None else "below the speech floor",
                )
                for turn, level in measured
            ),
            markers=("turn_gain_skipped:unmeasurable",),
            detail="fewer than two turns had measurable speech in the envelope",
        )
    target = statistics.median(levels)

    gains: list[Turn_Gain] = []
    for turn, level in measured:
        start, end = _bounds(turn) or (0.0, 0.0)
        speaker = str(getattr(turn, "speaker_label", ""))

        if level is None:
            gains.append(
                Turn_Gain(start, end, speaker, 0.0, 0.0, False, "no envelope reading in this turn")
            )
            continue
        if level <= SILENCE_FLOOR_DB:
            # Boosting a silent turn amplifies room tone, which is the one change guaranteed to
            # make a clip worse.
            gains.append(
                Turn_Gain(start, end, speaker, level, 0.0, False, "below the speech floor")
            )
            continue
        if (end - start) < MIN_TURN_SECONDS:
            # The stand-in for R7.7's confidence gate. Named as a proxy, not as confidence.
            gains.append(
                Turn_Gain(
                    start,
                    end,
                    speaker,
                    level,
                    0.0,
                    False,
                    f"turn shorter than {MIN_TURN_SECONDS}s; duration proxy for low attribution "
                    "confidence, since Speaker_Turn carries no confidence score",
                )
            )
            continue

        gains.append(Turn_Gain(start, end, speaker, level, clamp_gain(target - level), True))

    if not any(g.applied and abs(g.gain_db) > 0.01 for g in gains):
        return Turn_Gain_Plan(
            gains=tuple(gains),
            markers=("turn_gain_skipped:already_balanced",),
            detail="every correction rounded to zero; speakers are already comparable",
        )

    chain = gain_expression(gains)
    low, high = (
        min(g.gain_db for g in gains if g.applied),
        max(g.gain_db for g in gains if g.applied),
    )
    return Turn_Gain_Plan(
        filter_chain=chain,
        gains=tuple(gains),
        # R7.9: the range of gains actually applied, so a reviewer can see how hard this worked.
        markers=(f"turn_gain:{low:+.1f}..{high:+.1f}dB",),
        detail=f"balanced {sum(1 for g in gains if g.applied)} turn(s) towards {target:.1f} dB",
    )


def _bounds(turn: Any) -> tuple[float, float] | None:
    try:
        start, end = float(turn.start), float(turn.end)
    except (AttributeError, TypeError, ValueError):
        return None
    return (start, end) if end > start else None


def gain_expression(gains: Sequence[Turn_Gain], *, ramp: float = RAMP_SECONDS) -> str:
    """A ``volume`` filter whose gain follows the turns, ramped at every boundary (R7.4).

    Built as nested conditionals on ``t`` and evaluated per frame. Each boundary gets a linear ramp
    of ``ramp`` seconds *ending* at the turn's start, so the new speaker's first syllable is already
    at the corrected level rather than sliding up during it.

    Returns ``""`` when nothing is applied, so the caller adds no filter at all.
    """
    applied = [g for g in gains if g.applied and abs(g.gain_db) > 0.01]
    if not applied:
        return ""

    ramp_s = max(0.001, float(ramp))
    ordered = sorted(applied, key=lambda g: g.start)

    # Built inside-out: the innermost value is 0 dB, and each turn wraps it with "if we are inside
    # this turn, its gain; if we are in its lead-in ramp, interpolate".
    expr = "0"
    for gain in reversed(ordered):
        db = f"{gain.gain_db:.3f}"
        ramp_start = max(0.0, gain.start - ramp_s)
        inside = f"between(t,{gain.start:.3f},{gain.end:.3f})"
        if ramp_start < gain.start:
            # Interpolated in dB and converted once at the end, because loudness is logarithmic:
            # ramping the linear factor would move fast at the start and crawl at the end.
            lead_in = (
                f"if(between(t,{ramp_start:.3f},{gain.start:.3f}),"
                f"{db}*(t-{ramp_start:.3f})/{ramp_s:.3f},{expr})"
            )
        else:
            # A turn starting at t=0 has no room for a lead-in. Emitting one produced a zero-width
            # `between(t,0.000,0.000)` -- harmless but dead expression text, and the expression is
            # already the longest thing this module writes.
            lead_in = expr
        expr = f"if({inside},{db},{lead_in})"

    # **`dB` cannot suffix an expression.** ffmpeg's `volume` accepts `volume=-6dB` as a literal but
    # rejects `volume='<expr>dB'` outright: "Invalid chars 'dB' at the end of expression". So the dB
    # value is converted to a linear factor here, in the expression itself.
    #
    # Found by running it. The filter string read perfectly plausibly and the graph failed to
    # initialise -- which is the argument for rendering rather than asserting on argv.
    #
    # `precision=float` keeps the per-frame result in floating point; the default rounds, which would
    # turn a 0.4 dB correction into none and a ramp into a stair.
    return f"volume=eval=frame:precision=float:volume='pow(10,({expr})/20)'"
