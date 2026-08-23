"""Pins for the worker modules the six earlier passes never reached.

Four of these are the project's own recurring failure families, in modules that had never been
audited:

* **an effect that exists in the text and not at evaluation** — `turn_gain`'s ramp was present in
  the emitted filter string and unreachable when ffmpeg evaluated it, and the test asserted the
  string;
* **two index spaces** — speaker turns rebased with a single-match remap while words used the
  duplicate-aware one, so an assembled clip carried turns and words on different timelines;
* **a parse that pairs two independently-collected lists by position** — `detect_silences` dropped
  any silence that ran to the end of the file;
* **an unbounded ingest** — no size cap, no socket timeout and no `noplaylist` on a user-supplied
  URL.

The first is the one worth reading the detail of, because the *test* was the defect as much as the
code: it asserted a substring on the exact fixture that demonstrated the bug.
"""

from __future__ import annotations

import math
import re
from dataclasses import replace

import pytest

from worker import assembly, segmentation
from worker import turn_gain as tg
from worker.models import ProcessingOptions, effective_options, normalisation_markers


# --------------------------------------------------------------------------- #
# turn_gain: the ramp has to survive evaluation, not just appear in the text   #
# --------------------------------------------------------------------------- #
def _gain_db_at(chain: str, t: float) -> float:
    """Evaluate the emitted ``volume`` expression at ``t`` and return the gain in dB.

    ffmpeg's expression syntax is valid Python once ``if(`` and ``between(`` become ordinary
    function calls, so no parser is needed.
    """
    match = re.search(r"volume='(.+)'", chain)
    assert match, f"no volume expression in {chain!r}"
    body = match.group(1).replace("between(", "_between(").replace("if(", "_if(")
    env = {
        "t": float(t),
        "pow": pow,
        "_if": lambda cond, a, b: a if cond else b,
        "_between": lambda x, lo, hi: lo <= x <= hi,
    }
    return 20.0 * math.log10(eval(body, {"__builtins__": {}}, env))


def _turn(start: float, end: float, speaker: str, gain_db: float) -> tg.Turn_Gain:
    return tg.Turn_Gain(
        start=start,
        end=end,
        speaker=speaker,
        level_db=-18.0,
        gain_db=gain_db,
        applied=True,
    )


def test_the_ramp_is_reachable_at_a_contiguous_boundary():
    """`if` short-circuits, so a window nested inside an earlier one is dead expression text.

    The old build wrapped each turn's lead-in *inside* the preceding turn's
    ``between(t,start,end)``. For contiguous turns — the production case, since filler removal
    concatenates keeps and closes the inter-turn silence — the lead-in window lies entirely within
    the previous turn's window and is tested after it. So the level stepped, which is the audible
    click R7.4 exists to prevent, and the module shipped the defect it was written to fix.
    """
    chain = tg.gain_expression([_turn(0.0, 4.0, "A", -6.0), _turn(4.0, 8.0, "B", +6.0)])
    assert chain

    mid_ramp = _gain_db_at(chain, 4.0 - tg.RAMP_SECONDS / 2.0)
    assert -6.0 + 0.5 < mid_ramp < 6.0 - 0.5, (
        f"halfway through the lead-in the gain is {mid_ramp:.3f} dB, i.e. still one of the two "
        "settled levels -- the level stepped instead of ramping"
    )


def test_the_ramp_starts_from_the_previous_turns_level():
    """Interpolating from 0 dB introduces the discontinuity the ramp exists to remove.

    With the previous turn corrected to -6 dB, ramping from unity jumped -6 -> 0 at the ramp's own
    start and then climbed to the new level. Two clicks instead of none.
    """
    chain = tg.gain_expression([_turn(0.0, 4.0, "A", -6.0), _turn(4.0, 8.0, "B", +6.0)])

    assert _gain_db_at(chain, 4.0 - tg.RAMP_SECONDS + 0.001) == pytest.approx(-6.0, abs=0.15)
    assert _gain_db_at(chain, 4.0 - 0.001) == pytest.approx(6.0, abs=0.15)
    # Monotone across the whole window: no reversal, which a from-unity ramp would show.
    samples = [
        _gain_db_at(chain, 4.0 - tg.RAMP_SECONDS + step * tg.RAMP_SECONDS / 8.0)
        for step in range(9)
    ]
    assert samples == sorted(samples), f"the ramp is not monotone: {samples}"


def test_a_gap_between_turns_still_ramps_from_unity():
    """Where there is silence between turns, the level there really is 0 dB."""
    chain = tg.gain_expression([_turn(0.0, 3.0, "A", -6.0), _turn(5.0, 8.0, "B", +6.0)])

    assert abs(_gain_db_at(chain, 4.0)) < 0.01  # mid-gap, untouched
    assert _gain_db_at(chain, 5.0 - tg.RAMP_SECONDS + 0.001) == pytest.approx(0.0, abs=0.2)
    assert _gain_db_at(chain, 5.0 - 0.001) == pytest.approx(6.0, abs=0.2)


def test_a_ramp_never_consumes_more_than_half_of_the_turn_before_it():
    """A correction that owns less than half its own turn is not a correction."""
    chain = tg.gain_expression([_turn(0.0, 0.2, "A", -4.0), _turn(0.2, 3.0, "B", +4.0)])

    # Half of a 0.2 s turn is 0.1 s, so nothing may ramp before t=0.1. Sampled at 0.09,
    # which is inside the window an uncapped 0.12 s lead-in would have claimed (it would start
    # at 0.08) and outside the capped one -- t=0.05 precedes both and cannot tell them apart.
    assert _gain_db_at(chain, 0.09) == pytest.approx(-4.0, abs=0.01)
    assert _gain_db_at(chain, 0.05) == pytest.approx(-4.0, abs=0.01)


def test_a_single_turn_starting_at_zero_emits_no_dead_lead_in():
    """There is no earlier level to ramp from, and a zero-width window is dead text."""
    chain = tg.gain_expression([_turn(0.0, 4.0, "A", -6.0)])
    assert "between(t,0.000,0.000)" not in chain
    assert _gain_db_at(chain, 0.001) == pytest.approx(-6.0, abs=0.01)


# --------------------------------------------------------------------------- #
# assembly: turns and words must land on the same timeline                     #
# --------------------------------------------------------------------------- #
def test_the_assembly_rebaser_maps_a_duplicated_range_to_both_occurrences():
    """`rebase_turns` was written and tested for this, and never called.

    A retained cold open puts one source range in the keep list **twice**.
    `diarization.rebase_turns` carries `filler.rebase_words`' single-match ``break``, so a turn
    overlapping that range got one output position for two occurrences — while the words, which do
    use the duplicate-aware remap, got both. Turns and words then describe the same clip on two
    timelines, diverging by the cold open's duration for everything after it.

    Nothing about that is visible in a rendered frame: it points the speaker-aware reframe crop and
    the AU12 gain ramp at the wrong person for part of the clip, and emits no marker, because from
    their side the turn list is perfectly well-formed.
    """
    from worker.diarization import Speaker_Turn
    from worker.effects.filler import Interval

    # The cold open (8-10 s) is lifted to the front *and* retained in the body.
    keeps = [Interval(8.0, 10.0), Interval(0.0, 12.0)]
    turn = Speaker_Turn(speaker_label="S1", start=8.5, end=9.5)

    assembly_mapped = assembly.rebase_turns([turn], keeps)
    assert len(assembly_mapped) == 2, (
        "the duplicate-aware rebase produced one output position for a range that airs twice"
    )
    starts = sorted(round(t.start, 3) for t in assembly_mapped)
    assert starts == [0.5, 10.5], starts


def test_the_pipeline_chooses_the_duplicate_aware_rebase_for_turns():
    """The wiring, not just the function. This is the half that was missing.

    Asserted against the source because the alternative is a full pipeline run with diarisation and
    an assembled clip; the thing that was wrong was a single unconditional call, and that is
    exactly what this reads.
    """
    from pathlib import Path

    source = Path("worker/pipeline.py").read_text(encoding="utf-8")
    assert "assembly.rebase_turns if assembly_plan.assembled else diarization.rebase_turns" in (
        source
    ), "pipeline no longer selects the assembly-aware turn rebase"


# --------------------------------------------------------------------------- #
# segmentation: a silence that runs to the end of the file still counts        #
# --------------------------------------------------------------------------- #
def _detect(log: str, monkeypatch) -> list[tuple[float, float]]:
    class _Proc:
        stderr = log
        stdout = ""
        returncode = 0

    monkeypatch.setattr(segmentation.subprocess, "run", lambda *a, **k: _Proc())
    return segmentation.detect_silences("clip.mp4")


def test_a_silence_that_never_ends_is_dropped_not_mispaired(monkeypatch):
    """`silencedetect` emits a final `silence_start` with no matching end at EOF.

    The old parse collected starts and ends into two lists and `zip`ped them by position, so this
    trailing interval was silently truncated away — the ordinary case for anything with dead air or
    a fade-out at the end.
    """
    log = (
        "[silencedetect @ 0x1] silence_start: 1.0\n"
        "[silencedetect @ 0x1] silence_end: 2.0 | silence_duration: 1.0\n"
        "[silencedetect @ 0x1] silence_start: 9.5\n"  # runs to EOF, never closed
    )
    assert _detect(log, monkeypatch) == [(1.0, 2.0)]


def test_a_partial_log_does_not_shift_every_later_interval(monkeypatch):
    """An `end` with no preceding `start` used to offset the whole remaining list by one.

    Position-pairing two lists means one missing entry misaligns everything after it. An interval
    list shifted wholesale reads as "the silence detector is inaccurate", which is a far harder
    thing to find than a parse bug.
    """
    log = (
        "[silencedetect @ 0x1] silence_end: 0.5 | silence_duration: 0.5\n"  # orphaned
        "[silencedetect @ 0x1] silence_start: 3.0\n"
        "[silencedetect @ 0x1] silence_end: 4.0 | silence_duration: 1.0\n"
        "[silencedetect @ 0x1] silence_start: 6.0\n"
        "[silencedetect @ 0x1] silence_end: 7.0 | silence_duration: 1.0\n"
    )
    assert _detect(log, monkeypatch) == [(3.0, 4.0), (6.0, 7.0)]


def test_ordinary_alternating_output_is_unchanged(monkeypatch):
    """The rewrite must produce exactly the old answer on well-formed input."""
    log = (
        "silence_start: 1.25\n"
        "silence_end: 2.5 | silence_duration: 1.25\n"
        "silence_start: 10.0\n"
        "silence_end: 11.5 | silence_duration: 1.5\n"
    )
    assert _detect(log, monkeypatch) == [(1.25, 2.5), (10.0, 11.5)]


def test_no_silences_is_an_empty_list(monkeypatch):
    assert _detect("nothing here\n", monkeypatch) == []


# --------------------------------------------------------------------------- #
# models: a normalisation that discards a request has to say so               #
# --------------------------------------------------------------------------- #
def test_a_music_bed_removed_by_permissibility_mode_is_recorded():
    """The user asked for music and got none, with an `effects_applied` list that said nothing.

    Byte-identical to a run that never asked for music. `music_degraded:synthesised` — a strictly
    lesser degradation — was already both documented and emitted, which is what made this easy to
    miss.
    """
    requested = ProcessingOptions(music="upbeat", permissibility_mode=True)
    effective = effective_options(requested)

    assert effective.music == ""
    assert "music_suppressed:permissibility" in normalisation_markers(requested, effective)


def test_an_external_sourcing_downgrade_is_recorded():
    """`broll_source:local_only` was in the documented marker catalogue and emitted nowhere.

    The catalogue on `ClipResult` described it, `tests/test_engines_base.py` asserted it was
    documented, and no code path produced it — a marker that existed only as documentation.
    """
    requested = ProcessingOptions(asset_sourcing_mode="local_then_external")
    effective = replace(requested, asset_sourcing_mode="local_only")

    assert normalisation_markers(requested, effective) == ["broll_source:local_only"]


def test_an_untouched_request_records_nothing():
    """The markers must describe a real loss, not appear on every run."""
    requested = ProcessingOptions(music="upbeat", asset_sourcing_mode="local_only")
    assert normalisation_markers(requested, effective_options(requested)) == []

    off = ProcessingOptions()
    assert normalisation_markers(off, effective_options(off)) == []


def test_the_markers_are_ordered_so_a_clip_record_is_comparable():
    requested = ProcessingOptions(music="upbeat", asset_sourcing_mode="local_then_external")
    effective = replace(requested, music="", asset_sourcing_mode="local_only")
    assert normalisation_markers(requested, effective) == [
        "music_suppressed:permissibility",
        "broll_source:local_only",
    ]
