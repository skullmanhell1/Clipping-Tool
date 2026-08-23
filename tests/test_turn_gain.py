"""Per-speaker level matching (AU12).

The test that mattered most was not any of the ones below — it was **rendering the filter**. ffmpeg's
`volume` accepts `volume=-6dB` as a literal but rejects `volume='<expr>dB'` outright, and the filter
string read perfectly plausibly right up until the graph failed to initialise. So there is an
end-to-end test here that measures the two speakers' loudness in the delivered file, and it is the
one that would catch that class of fault again.

Everything else guards the arithmetic: the bound (R7.3), the ramp (R7.4), and the four refusals —
single speaker, diarisation off, unmeasurable, already balanced.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess

import pytest

from config import settings as app_settings
from worker import turn_gain as tg
from worker.diarization import Speaker_Turn

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None, reason="no ffmpeg on PATH; the gain expression must be rendered to be verified"
)


def _envelope(loud_until: int = 4, loud_db: float = -14.0, quiet_db: float = -26.0, total: int = 8):
    return [(float(i), loud_db if i < loud_until else quiet_db) for i in range(total)]


def _turns(*spans):
    return [Speaker_Turn(label, start, end) for label, start, end in spans]


# --- the end-to-end check, which is the one that caught the real bug ------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_expression_renders_and_narrows_the_imbalance(tmp_path):
    """The test that earns its keep.

    An earlier version of `gain_expression` suffixed the expression with `dB`. That is valid for a
    *literal* (`volume=-6dB`) and rejected for an expression — "Invalid chars 'dB' at the end of
    expression" — so the graph failed to initialise. Nothing about the filter string looked wrong.

    Measured here: a 12.0 LU imbalance narrows to 2.4 LU, which is exactly the 2 x 4.8 dB the bound
    permitted for this envelope.
    """
    source = tmp_path / "two.wav"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc='(0.5*sin(2*PI*200*t))*(lt(t,4)+0.25*gte(t,4))':d=8:s=48000",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        check=True,
        timeout=600,
    )

    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 4.0), ("S2", 4.0, 8.0)), _envelope(), enabled=True)
    assert plan.enabled

    dest = tmp_path / "balanced.wav"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            plan.filter_chain,
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        check=True,
        timeout=600,
    )

    def half(path, start):
        proc = subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-nostats",
                "-ss",
                str(start),
                "-t",
                "4",
                "-i",
                str(path),
                "-af",
                "ebur128",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        found = re.findall(r"I:\s*(-?[\d.]+)", proc.stderr)
        assert found, proc.stderr[-400:]
        return float(found[-1])

    before = abs(half(source, 0) - half(source, 4))
    after = abs(half(dest, 0) - half(dest, 4))
    assert before > 10.0, f"precondition: the fixture should be badly imbalanced, got {before:.1f}"
    assert after < before - 5.0, f"imbalance {before:.1f} LU -> {after:.1f} LU"


# --- R7.1/R7.2: measurement and correction --------------------------------------------------


def test_the_loud_speaker_is_cut_and_the_quiet_one_lifted():
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 4.0), ("S2", 4.0, 8.0)), _envelope(), enabled=True)
    by_speaker = {g.speaker: g for g in plan.gains}
    assert by_speaker["S1"].gain_db < 0
    assert by_speaker["S2"].gain_db > 0


def test_turn_levels_come_from_the_existing_envelope():
    """R7.10, and the contrast with T11 worth keeping in view.

    A *turn* is seconds long, so one reading per second describes it well. T11 needed to snap
    individual *word* starts and the same envelope erased the transients entirely — same data,
    opposite verdict, because the thing being measured is a different size.
    """
    assert tg.turn_level_db(_envelope(), 0.0, 4.0) == pytest.approx(-14.0)
    assert tg.turn_level_db(_envelope(), 4.0, 8.0) == pytest.approx(-26.0)


def test_a_turn_with_no_envelope_reading_is_unmeasurable():
    assert tg.turn_level_db(_envelope(), 20.0, 21.0) is None


def test_corrections_are_made_towards_the_median_not_the_mean():
    """One extreme turn must not drag the target every other turn is corrected towards.

    The same reasoning `pitch_features` uses for its baseline.
    """
    envelope = [(0.0, -12.0), (1.0, -12.0), (2.0, -12.0), (3.0, -40.0)]
    plan = tg.plan_turn_gain(
        _turns(("S1", 0.0, 1.0), ("S2", 1.0, 2.0), ("S3", 2.0, 3.0), ("S4", 3.0, 4.0)),
        envelope,
        enabled=True,
    )
    # Three turns at -12 and one near the floor: the target must sit at -12, so those three need
    # essentially no correction.
    for gain in plan.gains:
        if gain.applied and gain.level_db == pytest.approx(-12.0):
            assert abs(gain.gain_db) < 0.5, gain


# --- R7.3: the bound ------------------------------------------------------------------------


def test_the_correction_is_bounded():
    """R7.3. Diarisation misattributes turns, and an unbounded correction on one is a new defect.

    A 40 dB gap must not produce a 20 dB lift; the worst case has to be a partial correction.
    """
    envelope = [(0.0, -6.0), (1.0, -6.0), (2.0, -46.0), (3.0, -46.0)]
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 2.0), ("S2", 2.0, 4.0)), envelope, enabled=True)
    for gain in plan.gains:
        assert abs(gain.gain_db) <= tg.MAX_GAIN_DB + 1e-9, gain


@pytest.mark.parametrize("value", [100.0, -100.0, float("nan"), "x", None])
def test_clamp_handles_every_unusable_value(value):
    assert abs(tg.clamp_gain(value)) <= tg.MAX_GAIN_DB


# --- R7.4: ramped, not stepped --------------------------------------------------------------


def _gain_db_at(filter_chain: str, t: float) -> float:
    """Evaluate the emitted ``volume`` expression at time ``t`` and return the gain in dB.

    The whole point of this helper. The previous version of the test below asserted that the
    substring ``between(t,3.880,4.000)`` appeared in the filter string — which it did, on the very
    fixture that demonstrated the bug, because the window was *present in the text* and
    *unreachable at evaluation*. `if` short-circuits, and that window was nested inside the
    preceding turn's window, which is tested first.

    ffmpeg's expression syntax is valid Python once ``if(`` and ``between(`` become ordinary
    function calls, so no parser is needed. Eager evaluation of both branches is harmless here —
    every divisor is a positive span.
    """
    match = re.search(r"volume='(.+)'", filter_chain)
    assert match, f"no volume expression in {filter_chain!r}"
    body = match.group(1).replace("between(", "_between(").replace("if(", "_if(")
    env = {
        "t": float(t),
        "pow": pow,
        "_if": lambda cond, a, b: a if cond else b,
        "_between": lambda x, lo, hi: lo <= x <= hi,
    }
    factor = eval(body, {"__builtins__": {}}, env)
    return 20.0 * math.log10(factor)


def test_the_gain_ramps_at_a_turn_boundary():
    """R7.4. A step change in level is an audible click, which a viewer notices more than the
    imbalance it was fixing.

    Asserted by **evaluating** the expression across the boundary rather than by looking for a
    window in its text. The two turns here are contiguous, which is the production case — filler
    removal concatenates keeps, closing the inter-turn silence — and it is exactly the case where
    the old build's ramp was shadowed by the preceding turn's own window and the level stepped.
    """
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 4.0), ("S2", 4.0, 8.0)), _envelope(), enabled=True)
    chain = plan.filter_chain
    assert chain, "no filter emitted"

    before = _gain_db_at(chain, 4.0 - tg.RAMP_SECONDS - 0.05)  # settled in the first turn
    after = _gain_db_at(chain, 4.05)  # settled in the second turn
    assert abs(after - before) > 0.5, (
        f"the two turns were corrected to within {abs(after - before):.2f} dB of each other, "
        "so this fixture cannot demonstrate a ramp at all"
    )

    # The level must move monotonically across the boundary, and be strictly between the two
    # settled levels partway through -- which is what "ramp" means and what a step is not.
    low, high = sorted((before, after))
    midpoint = _gain_db_at(chain, 4.0 - tg.RAMP_SECONDS / 2.0)
    assert low + 0.01 < midpoint < high - 0.01, (
        f"halfway through the lead-in the gain is {midpoint:.3f} dB, which is not between the "
        f"settled levels {before:.3f} and {after:.3f} -- the level stepped instead of ramping"
    )

    # And it is continuous at both ends of the ramp: no jump into or out of it.
    assert abs(_gain_db_at(chain, 4.0 - tg.RAMP_SECONDS + 0.001) - before) < 0.15
    assert abs(_gain_db_at(chain, 4.0 - 0.001) - after) < 0.15


def test_a_gap_between_turns_ramps_from_unity():
    """With silence between the turns the ramp lives in the gap, where the level is 0 dB.

    This is the case the old expression assumed universally, and it still has to work.
    """
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 3.0), ("S2", 5.0, 8.0)), _envelope(), enabled=True)
    chain = plan.filter_chain
    assert chain

    assert abs(_gain_db_at(chain, 4.0)) < 0.01  # mid-gap: untouched
    settled = _gain_db_at(chain, 5.05)
    midpoint = _gain_db_at(chain, 5.0 - tg.RAMP_SECONDS / 2.0)
    assert abs(midpoint - settled / 2.0) < 0.35, (
        f"halfway through a from-silence lead-in the gain is {midpoint:.3f} dB, expected about "
        f"half of the settled {settled:.3f} dB"
    )


def test_a_ramp_never_takes_more_than_half_of_the_turn_before_it():
    """The lead-in comes out of the previous turn, so it is capped at half of it.

    A correction that owns less than half its own turn is not a correction, and without the cap a
    turn shorter than the ramp would be consumed entirely by the next boundary's lead-in.

    Driven through ``gain_expression`` directly rather than ``plan_turn_gain``, because
    ``MIN_TURN_SECONDS`` refuses turns under 0.5 s and the cap only binds below ``2 * ramp``
    (0.24 s). That makes this a guard on the arithmetic rather than on a reachable plan — which is
    worth having, since the two thresholds are independent and either could move.
    """
    short = tg.Turn_Gain(
        start=0.0, end=0.2, speaker="S1", level_db=-20.0, gain_db=-4.0, applied=True
    )
    following = tg.Turn_Gain(
        start=0.2, end=3.0, speaker="S2", level_db=-14.0, gain_db=+4.0, applied=True
    )
    chain = tg.gain_expression([short, following])
    assert chain

    # Half of a 0.2 s turn is 0.1 s, so the ramp may not begin before t=0.1. Sampled just
    # inside that boundary: an uncapped 0.12 s lead-in would already be ramping at 0.09.
    assert _gain_db_at(chain, 0.09) == pytest.approx(-4.0, abs=0.01)
    # ...and by its own end the level has reached the new turn's gain.
    assert _gain_db_at(chain, 0.199) == pytest.approx(4.0, abs=0.2)


def test_a_ramp_is_continuous_from_the_previous_turns_level():
    """The interpolation starts from the preceding turn's gain, not from 0 dB.

    The old expression always ramped from unity, so with the previous turn corrected to -6 dB the
    level jumped -6 -> 0 at the ramp's start and then climbed. That is a discontinuity introduced
    by the very code meant to remove one, and it was invisible because the ramp was unreachable in
    the contiguous case anyway.
    """
    first = tg.Turn_Gain(
        start=0.0, end=4.0, speaker="S1", level_db=-8.0, gain_db=-6.0, applied=True
    )
    second = tg.Turn_Gain(
        start=4.0, end=8.0, speaker="S2", level_db=-20.0, gain_db=+6.0, applied=True
    )
    chain = tg.gain_expression([first, second])

    at_ramp_start = _gain_db_at(chain, 4.0 - tg.RAMP_SECONDS + 0.001)
    assert at_ramp_start == pytest.approx(-6.0, abs=0.15), (
        f"the ramp begins at {at_ramp_start:.3f} dB rather than the previous turn's -6 dB"
    )
    assert _gain_db_at(chain, 4.0 - tg.RAMP_SECONDS / 2.0) == pytest.approx(0.0, abs=0.2)
    assert _gain_db_at(chain, 4.0 - 0.001) == pytest.approx(6.0, abs=0.15)


def test_the_expression_is_evaluated_per_frame():
    """A once-per-file evaluation would apply one gain to the whole clip, which is what
    `loudnorm` already does and what AU12 exists to go beyond."""
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 4.0), ("S2", 4.0, 8.0)), _envelope(), enabled=True)
    assert "eval=frame" in plan.filter_chain
    assert "precision=float" in plan.filter_chain, (
        "the default rounds to integer dB, turning a 0.4 dB correction into none and a ramp "
        "into a stair"
    )


def test_the_gain_is_a_linear_factor_not_a_db_suffix():
    """The real bug, pinned.

    `volume` accepts `-6dB` as a literal and rejects `'<expr>dB'`. The conversion has to happen
    inside the expression, and interpolating in dB before converting is what keeps the ramp
    perceptually even.
    """
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 4.0), ("S2", 4.0, 8.0)), _envelope(), enabled=True)
    assert "pow(10," in plan.filter_chain
    assert not plan.filter_chain.rstrip("'").endswith("dB")


def test_a_turn_starting_at_zero_gets_no_lead_in():
    """There is no room for one, and emitting a zero-width `between` is dead expression text."""
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 4.0), ("S2", 4.0, 8.0)), _envelope(), enabled=True)
    assert "between(t,0.000,0.000)" not in plan.filter_chain


# --- the refusals ---------------------------------------------------------------------------


def test_disabled_by_default():
    """R7.8. A preference trial decides this, not an assertion."""
    assert (
        tg.plan_turn_gain(_turns(("S1", 0.0, 4.0), ("S2", 4.0, 8.0)), _envelope()).enabled is False
    )


def test_a_single_speaker_is_skipped():
    """R7.6. Nothing to balance, and `loudnorm` already sets the level."""
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 4.0), ("S1", 4.0, 8.0)), _envelope(), enabled=True)
    assert plan.enabled is False
    assert plan.markers == ("turn_gain_skipped:single_speaker",)


def test_diarisation_being_off_is_reported_and_not_worked_around():
    """R7.12. An audio feature must not switch on a transcription-adjacent stage to do its job.

    That would be spending someone else's budget, so the marker names the dependency instead.
    """
    plan = tg.plan_turn_gain(
        _turns(("S1", 0.0, 4.0), ("S2", 4.0, 8.0)),
        _envelope(),
        enabled=True,
        diarization_available=False,
    )
    assert plan.enabled is False
    assert plan.markers == ("turn_gain_unavailable:diarization_disabled",)


def test_already_balanced_speakers_get_no_filter():
    """No filter at all rather than a no-op one, so an unconfigured graph is untouched."""
    flat = [(float(i), -18.0) for i in range(8)]
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 4.0), ("S2", 4.0, 8.0)), flat, enabled=True)
    assert plan.filter_chain == ""
    assert plan.markers == ("turn_gain_skipped:already_balanced",)


def test_too_many_turns_is_refused_with_the_count():
    """The expression nests one conditional per boundary, so its length grows with the turn count.

    Past the limit the filter graph is the problem rather than the imbalance -- the same refusal U4
    makes for a cut list beyond 200 entries.
    """
    many = [("S1" if i % 2 else "S2", float(i), float(i) + 1.0) for i in range(tg.MAX_TURNS + 5)]
    plan = tg.plan_turn_gain(_turns(*many), _envelope(total=40), enabled=True)
    assert plan.enabled is False
    assert plan.markers[0].startswith("turn_gain_skipped:too_many_turns:")


def test_a_silent_turn_is_never_boosted():
    """The loudest thing in a silent turn is room tone, and lifting it is the one change
    guaranteed to make a clip worse."""
    envelope = [(0.0, -12.0), (1.0, -12.0), (2.0, -80.0), (3.0, -80.0)]
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 2.0), ("S2", 2.0, 4.0)), envelope, enabled=True)
    silent = [g for g in plan.gains if g.level_db <= tg.SILENCE_FLOOR_DB]
    assert silent and all(not g.applied for g in silent)
    assert all("floor" in g.reason for g in silent)


def test_a_very_short_turn_is_skipped_and_the_proxy_is_named():
    """R7.7 cannot be honoured as written, and the reason is recorded rather than glossed.

    `Speaker_Turn` carries **no confidence field** -- the offline diarisation path is described in
    its own docstring as "attribution by proxy" and reports no score. So turn duration stands in:
    a very short turn is disproportionately likely to be a misattribution.

    The reason string says it is a proxy. Reporting a proxy as the thing it approximates is how S5
    would have gone wrong.
    """
    envelope = [(float(i), -14.0 if i < 4 else -26.0) for i in range(8)]
    plan = tg.plan_turn_gain(
        _turns(("S1", 0.0, 4.0), ("S2", 4.0, 4.2), ("S3", 5.0, 8.0)), envelope, enabled=True
    )
    short = [g for g in plan.gains if (g.end - g.start) < tg.MIN_TURN_SECONDS]
    assert short and all(not g.applied for g in short)
    assert all("proxy" in g.reason and "confidence" in g.reason for g in short)


# --- R7.9: the marker -----------------------------------------------------------------------


def test_the_marker_reports_the_range_of_gains_applied():
    """R7.9, so a reviewer can see how hard this worked without opening the clip."""
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 4.0), ("S2", 4.0, 8.0)), _envelope(), enabled=True)
    assert plan.markers and plan.markers[0].startswith("turn_gain:")
    assert "dB" in plan.markers[0]
    low, high = plan.applied_range_db
    assert low < 0 < high


def test_the_plan_serialises_for_the_clip_record():
    plan = tg.plan_turn_gain(_turns(("S1", 0.0, 4.0), ("S2", 4.0, 8.0)), _envelope(), enabled=True)
    data = plan.to_dict()
    assert len(data["gains"]) == 2
    assert data["applied_range_db"][0] < 0
    assert data["detail"]


# --- R7.10: no extra pass -------------------------------------------------------------------


def test_the_module_spawns_no_process():
    """R7.10. The measurement reuses the envelope and the correction is one filter."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(tg))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "subprocess" not in imported
