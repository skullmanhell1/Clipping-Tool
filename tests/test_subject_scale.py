"""V23 subject-scale normalisation.

The measurement at the bottom is the point of the file. `test_the_magnification_actually_changes_the
_subject_size` renders through real ffmpeg and compares mean luma either side of a cut, and
`test_the_documented_crop_size_mechanism_really_does_crash_this_ffmpeg` demonstrates *why* the
spec's own mechanism was not used — R2.2 asks for the crop size to change per shot, and on the
ffmpeg this project ships that crashes the CLI outright. Without that second test the module
docstring's central claim would be folklore.

Everything above them guards the arithmetic: the bound (R2.3), the step-only shape (R2.4), reuse of
V4's cut list (R2.5), never reaching outside the source (R2.6), and leaving a faceless shot alone
(R2.7).
"""

from __future__ import annotations

import re
import subprocess

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import FFMPEG, requires_ffmpeg
from worker import subject_scale as ss


class _Det:
    """A detection duck-typed the way `measure_shots` reads them (`.w`, `.h`)."""

    def __init__(self, w: float, h: float) -> None:
        self.w = w
        self.h = h


def _samples(*shots: tuple[int, float | None], step: float = 0.2):
    """Build `(t, [detections])` samples from `(frame_count, face_height_or_None)` per shot.

    Returns `(samples, cut_indices)` so a test states the shot structure once and gets both the
    series and the boundaries that `reframe.cut_indices` would have produced for it.
    """
    samples: list[tuple[float, list[_Det]]] = []
    cuts: list[int] = []
    t = 0.0
    for index, (count, height) in enumerate(shots):
        if index > 0:
            cuts.append(len(samples))
        for _ in range(count):
            dets = [] if height is None else [_Det(height * 0.8, height)]
            samples.append((round(t, 3), dets))
            t += step
    return samples, cuts


# --------------------------------------------------------------------------- #
# R2.5 -- the shot segmentation is V4's, not a second mechanism               #
# --------------------------------------------------------------------------- #


def test_shot_bounds_partitions_the_whole_series_exactly_once():
    """Every sample belongs to exactly one shot, with no gap and no overlap.

    A gap would silently drop a shot from the median; an overlap would count one twice and bias it.
    """
    bounds = ss.shot_bounds([0.0, 0.2, 0.4, 0.6, 0.8], [2, 4])

    assert bounds == [(0, 2), (2, 4), (4, 5)]
    covered = [i for lo, hi in bounds for i in range(lo, hi)]
    assert covered == list(range(5))


def test_out_of_range_cut_indices_are_ignored_rather_than_creating_empty_shots():
    """`cut_indices` is derived from times, so a stale or clamped index must not invent a shot."""
    assert ss.shot_bounds([0.0, 0.2, 0.4], [0, 3, 9, -1]) == [(0, 3)]


def test_no_cuts_is_one_shot():
    assert ss.shot_bounds([0.0, 0.2, 0.4], []) == [(0, 3)]


# --------------------------------------------------------------------------- #
# R2.1 -- measurement                                                         #
# --------------------------------------------------------------------------- #


def test_scale_is_measured_relative_to_the_crop_not_in_absolute_pixels():
    """The same face box is a close-up in a tight crop and a mid-shot in a loose one.

    So an absolute pixel height would compare the wrong thing whenever the crop differs, and the
    quantity a viewer perceives is the fraction of frame the subject occupies.
    """
    samples, cuts = _samples((4, 200.0))

    tight = ss.measure_shots(samples, cuts, crop_h=400)
    loose = ss.measure_shots(samples, cuts, crop_h=800)

    assert tight[0].scale == pytest.approx(0.5)
    assert loose[0].scale == pytest.approx(0.25)


def test_the_largest_detection_in_a_frame_is_the_subject():
    """Matching `pick_main_face`, so V23 measures the same face the crop is following.

    Measuring a different face than the tracker chose would magnify towards the size of someone who
    is not the subject of the shot.
    """
    samples = [(0.0, [_Det(40, 50), _Det(160, 200)]), (0.2, [_Det(160, 200), _Det(30, 35)])]

    shots = ss.measure_shots(samples, [], crop_h=400)

    assert shots[0].scale == pytest.approx(0.5)


def test_a_single_outlying_detection_does_not_move_the_shot(monkeypatch):
    """The per-shot statistic is a median, not a mean.

    A detector that briefly latches onto a background face produces one wildly different box, and a
    mean would carry it into the magnification for the whole shot.
    """
    samples = [
        (0.0, [_Det(80, 100)]),
        (0.2, [_Det(80, 100)]),
        (0.4, [_Det(80, 100)]),
        (0.6, [_Det(320, 400)]),  # the outlier
    ]

    shots = ss.measure_shots(samples, [], crop_h=400)

    assert shots[0].scale == pytest.approx(0.25), "an outlier moved the shot's measured scale"


def test_a_shot_with_no_detection_is_unmeasurable_not_zero():
    """R2.7 needs "no face" to be distinguishable from "a very small face"."""
    samples, cuts = _samples((3, 100.0), (3, None))

    shots = ss.measure_shots(samples, cuts, crop_h=400)

    assert shots[0].measured
    assert not shots[1].measured
    assert shots[1].scale is None


def test_one_detection_is_too_few_to_trust():
    """A single frame would otherwise decide the magnification for an entire shot."""
    samples = [(0.0, [_Det(80, 100)]), (0.2, []), (0.4, [])]

    shots = ss.measure_shots(samples, [], crop_h=400)

    assert not shots[0].measured


# --------------------------------------------------------------------------- #
# R2.2, R2.3, R2.6, R2.7 -- the plan                                          #
# --------------------------------------------------------------------------- #


def test_a_wide_shot_is_magnified_towards_the_median():
    """The claim the feature exists to make."""
    samples, cuts = _samples((4, 200.0), (4, 200.0), (4, 100.0))

    shots = ss.measure_shots(samples, cuts, crop_h=400)
    mags = ss.plan_magnifications(shots)

    assert mags[0] == pytest.approx(1.0)
    assert mags[1] == pytest.approx(1.0)
    assert mags[2] == pytest.approx(2.0 if ss.MAX_MAGNIFICATION >= 2.0 else ss.MAX_MAGNIFICATION)


def test_a_shot_larger_than_the_target_is_left_alone_because_the_crop_cannot_widen():
    """R2.6, and the reason the target is the median rather than the maximum.

    The crop is already the largest window of the target aspect that fits the source, so a shot whose
    subject is *bigger* than the target would have to be widened — which means reaching outside the
    source. That case is common and is not a failure; it is left alone.
    """
    samples, cuts = _samples((4, 100.0), (4, 100.0), (4, 300.0))

    mags = ss.plan_magnifications(ss.measure_shots(samples, cuts, crop_h=400))

    assert mags[2] == pytest.approx(1.0), "a too-close shot was 'corrected' by cropping further in"
    assert all(m >= 1.0 for m in mags), (
        "a magnification below 1.0 would need pixels outside the crop"
    )


def test_the_target_is_the_median_and_not_the_maximum():
    """Which statistic is the target is a design decision, so it gets its own test.

    Found by mutation: every other test here used a fixture whose median and maximum coincided, so
    replacing one with the other changed nothing and the choice was untested.

    Normalising to the **maximum** would magnify almost every shot to match the tightest one,
    softening most of the clip to fix a minority of it. With the median at most half the shots move,
    and the ones that do are the wide shots — where magnification costs least, because the subject
    occupies few pixels and gains the most from being brought closer.

    Three shots at 300/200/100 px: the median is 200, so the 300 shot is left alone and the 100 shot
    is pulled to 200 (2.0x, bounded). Against the maximum the 200 shot would move too.
    """
    samples, cuts = _samples((4, 300.0), (4, 200.0), (4, 100.0))

    mags = ss.plan_magnifications(ss.measure_shots(samples, cuts, crop_h=400))

    assert mags[0] == pytest.approx(1.0), "the largest shot cannot be widened, so it must not move"
    assert mags[1] == pytest.approx(1.0), (
        "the median shot is the target and must not move; it does only if the target is the maximum"
    )
    assert mags[2] > 1.0


def test_the_magnification_is_bounded(monkeypatch):
    """R2.3: one outlying shot must not drive an extreme crop."""
    samples, cuts = _samples((4, 400.0), (4, 400.0), (4, 10.0))

    mags = ss.plan_magnifications(ss.measure_shots(samples, cuts, crop_h=400))

    assert max(mags) == pytest.approx(ss.MAX_MAGNIFICATION)


def test_a_negligible_difference_is_not_corrected():
    """Magnifying by 1.03 spends resolution to fix something no viewer would notice."""
    samples, cuts = _samples((4, 200.0), (4, 200.0), (4, 196.0))

    mags = ss.plan_magnifications(ss.measure_shots(samples, cuts, crop_h=400))

    assert all(m == pytest.approx(1.0) for m in mags)


def test_an_unmeasurable_shot_keeps_its_crop(monkeypatch):
    """R2.7. Guessing from no detection would put a size step at a cut for no reason."""
    samples, cuts = _samples((4, 300.0), (4, 300.0), (4, None), (4, 100.0))

    shots = ss.measure_shots(samples, cuts, crop_h=400)
    mags = ss.plan_magnifications(shots)

    assert mags[2] == pytest.approx(1.0)


def test_a_single_shot_is_never_normalised():
    """A shot cannot be inconsistent with itself, and R2.4 forbids changing anything within one."""
    samples, cuts = _samples((6, 100.0))

    assert ss.plan_magnifications(ss.measure_shots(samples, cuts, crop_h=400)) == [1.0]


# --------------------------------------------------------------------------- #
# R2.4 -- the step shape                                                      #
# --------------------------------------------------------------------------- #


def test_the_expression_is_constant_within_a_shot_and_steps_only_at_a_cut():
    """R2.4, asserted on the emitted expression rather than on intent.

    `zoompan` evaluates `z` per output frame, so "never within a shot" is a property of the
    expression's *shape*: nested `if(lt(on,<boundary>))` can only change value at a boundary frame.
    An interpolated size would be a zoom, which is exactly what the requirement forbids.
    """
    shots = [
        ss.Shot_Scale(0.0, 1.0, 0.5, 5),
        ss.Shot_Scale(2.0, 3.0, 0.25, 5),
    ]
    expr = ss.build_expression(shots, [1.0, 1.3], fps=25.0)

    assert expr == "if(lt(on,50),1.0000,1.3000)"
    assert "on" in expr and "t" not in expr.replace("lt", "").replace("if", "")


def test_no_change_means_no_expression_and_therefore_no_filter():
    """The default path must add nothing at all, or every reframe golden moves."""
    shots = [ss.Shot_Scale(0.0, 1.0, 0.5, 5), ss.Shot_Scale(2.0, 3.0, 0.5, 5)]

    assert ss.build_expression(shots, [1.0, 1.0], fps=25.0) == ""
    assert ss.build_filter("", crop_w=406, crop_h=720, fps=25.0) == ""


def test_the_step_frame_follows_the_shot_start_time():
    """The step has to land on the cut the measurement came from.

    Off-by-one here puts the size change a shot away from its own boundary, which is visibly worse
    than no normalisation at all.
    """
    shots = [ss.Shot_Scale(0.0, 1.0, 0.5, 5), ss.Shot_Scale(4.0, 5.0, 0.25, 5)]

    assert "lt(on,100)" in ss.build_expression(shots, [1.0, 1.2], fps=25.0)
    assert "lt(on,48)" in ss.build_expression(shots, [1.0, 1.2], fps=12.0)


def test_the_filter_hands_on_the_crop_dimensions_not_the_delivery_size():
    """`zoompan` sits between `crop` and `scale`.

    Setting `s=` to the delivery size here would resample twice and leave the later `scale` acting on
    an already-resampled frame, costing a generation of sharpness for nothing.
    """
    built = ss.build_filter("1.2000", crop_w=406, crop_h=720, fps=24.0)

    assert "s=406x720" in built
    assert "d=1" in built, "zoompan's default d=90 would hold one frame for ninety"
    assert "zoom" in built


# --------------------------------------------------------------------------- #
# R2.8, R2.9 -- default and marker                                            #
# --------------------------------------------------------------------------- #


def test_disabled_produces_an_inert_plan():
    samples, cuts = _samples((4, 400.0), (4, 100.0))

    plan = ss.plan(samples, cuts, crop_w=406, crop_h=720, fps=24.0, enabled=False)

    assert not plan.enabled
    assert plan.marker == ""
    assert plan.magnifications == ()


def test_an_enabled_plan_that_changes_nothing_still_adds_no_filter():
    samples, cuts = _samples((4, 200.0), (4, 200.0))

    plan = ss.plan(samples, cuts, crop_w=406, crop_h=400, fps=24.0, enabled=True)

    assert not plan.enabled
    assert plan.marker == ""


def test_the_marker_names_how_many_shots_moved_and_the_largest_magnification():
    samples, cuts = _samples((4, 200.0), (4, 200.0), (4, 120.0))

    plan = ss.plan(samples, cuts, crop_w=406, crop_h=400, fps=24.0, enabled=True)

    assert plan.enabled
    assert re.fullmatch(r"subject_scale:\d+:\d\.\d{3}", plan.marker), plan.marker
    assert plan.marker.startswith("subject_scale:1:")


def test_a_malformed_detection_does_not_fail_the_plan():
    """A framing refinement must never be the reason a clip fails."""

    class _Bad:
        w = "wide"
        h = None

    samples = [(0.0, [_Bad()]), (0.2, [_Bad()]), (0.4, [_Bad()])]

    plan = ss.plan(samples, [], crop_w=406, crop_h=720, fps=24.0, enabled=True)

    assert not plan.enabled


# --------------------------------------------------------------------------- #
# Properties                                                                  #
# --------------------------------------------------------------------------- #


# Feature: clip-presentation-polish, Property 1: magnification is bounded to [1.0, MAX] always.
@settings(max_examples=100)
@given(
    heights=st.lists(st.floats(min_value=1.0, max_value=900.0), min_size=1, max_size=8),
    crop_h=st.integers(min_value=64, max_value=1920),
)
def test_p1_magnifications_are_always_within_the_bound(heights, crop_h):
    """Validates: Requirements 2.3, 2.6

    Never below 1.0 (which would need pixels outside the crop) and never above the bound (which would
    let one mis-detected shot blow up a whole shot).
    """
    shots = [ss.Shot_Scale(float(i), float(i) + 1.0, h / crop_h, 5) for i, h in enumerate(heights)]

    for magnification in ss.plan_magnifications(shots):
        assert 1.0 <= magnification <= ss.MAX_MAGNIFICATION


# Feature: clip-presentation-polish, Property 2: the expression only ever changes at a shot boundary.
@settings(max_examples=100)
@given(
    count=st.integers(min_value=2, max_value=6),
    fps=st.floats(min_value=6.0, max_value=60.0),
)
def test_p2_the_expression_changes_value_only_at_shot_boundaries(count, fps):
    """Validates: Requirements 2.4

    Evaluated the way ffmpeg would: for every output frame, the value must equal the magnification of
    whichever shot that frame falls in. Any drift between shots would be a zoom.
    """
    shots = [ss.Shot_Scale(float(i) * 2.0, float(i) * 2.0 + 1.9, 0.5, 5) for i in range(count)]
    mags = [1.0] + [1.0 + 0.1 * (i + 1) for i in range(count - 1)]
    expr = ss.build_expression(shots, mags, fps=fps)
    if not expr:
        return

    boundaries = [max(1, int(round(shots[i].start * fps))) for i in range(1, count)]
    for frame in range(0, boundaries[-1] + 20):
        shot = sum(1 for b in boundaries if frame >= b)
        assert _evaluate(expr, frame) == pytest.approx(mags[shot], abs=1e-4)


def _evaluate(expr: str, on: int) -> float:
    """Evaluate a nested `if(lt(on,N),A,B)` expression the way ffmpeg would."""
    match = re.fullmatch(r"if\(lt\(on,(\d+)\),([0-9.]+),(.*)\)", expr)
    if match is None:
        return float(expr)
    boundary, then, otherwise = int(match.group(1)), float(match.group(2)), match.group(3)
    return then if on < boundary else _evaluate(otherwise, on)


# --------------------------------------------------------------------------- #
# The measurements: what the mechanism does, and why it is this mechanism     #
# --------------------------------------------------------------------------- #


def _square_source(tmp_path):
    """A black frame with a fixed white square, so magnification is measurable as mean luma."""
    dest = tmp_path / "square.mp4"
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
            "color=c=black:s=1280x720:r=25:d=4",
            "-vf",
            "drawbox=x=590:y=310:w=100:h=100:color=white:t=fill",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def _mean_luma(path, at: float) -> float:
    out = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{at}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    ).stdout
    values = re.findall(r"YAVG=([0-9.]+)", out)
    assert values, f"no luma measured in {out!r}"
    return float(values[0])


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_magnification_actually_changes_the_subject_size(tmp_path):
    """The claim the feature exists to make, measured rather than asserted.

    Both sides use the identical crop, so the only variable is the `zoompan` step. A magnified square
    covers more of the frame, so mean luma rises. The flat control is what makes this non-vacuous: it
    proves the rise comes from the step and not from the graph.

    Measured on this fixture: about 23.5 before the step and about 28.7 after, against a control that
    stays at 23.5 throughout. The assertion is a wide inequality rather than a pinned figure because
    the exact value depends on the x264 build; what must not regress is the direction.
    """
    source = _square_source(tmp_path)
    stepped = tmp_path / "stepped.mp4"
    flat = tmp_path / "flat.mp4"

    magnify = ss.build_filter("if(lt(on,50),1.0000,1.3000)", crop_w=406, crop_h=720, fps=25.0)
    control = ss.build_filter("1.0000", crop_w=406, crop_h=720, fps=25.0)
    for dest, fragment in ((stepped, magnify), (flat, control)):
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                f"crop=406:720:437:0,{fragment},scale=1080:1920,setsar=1",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                str(dest),
            ],
            check=True,
            capture_output=True,
        )

    before, after = _mean_luma(stepped, 1.0), _mean_luma(stepped, 3.0)
    flat_before, flat_after = _mean_luma(flat, 1.0), _mean_luma(flat, 3.0)

    assert after > before * 1.1, f"the step did not magnify the subject: {before} -> {after}"
    assert flat_after == pytest.approx(flat_before, rel=0.01), (
        f"the control moved on its own ({flat_before} -> {flat_after}), so the comparison above "
        "proves nothing"
    )


#: Bound on the two sendcmd renders below, in seconds.
#:
#: They encode about four seconds of 720p, which is well under a second of work. Thirty seconds is
#: generous for a slow shared runner and short enough that a wedge is reported as one.
CROP_COMMAND_TIMEOUT_S = 30.0


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_documented_crop_size_mechanism_really_does_crash_this_ffmpeg(tmp_path):
    """Why R2.2's own mechanism was not used, kept honest by a test.

    The spec asks for the **crop size** to change per shot, and `crop`'s `w`/`h` are advertised as
    commandable (`T` in `ffmpeg -h filter=crop`), so a `sendcmd` script changing them is the obvious
    implementation. On the ffmpeg this project ships it aborts the CLI outright — changing a crop's
    output dimensions reconfigures the filter link and the command-line tool cannot follow it
    mid-stream.

    Asserted from both sides, because "it crashed" is only meaningful next to a command that does not:
    an `x`/`y`-only script through the same graph renders fine. If a future ffmpeg fixes this, **this
    test fails**, which is exactly the notification needed to revisit the design.

    **Two failure modes, both accepted.** The build used locally aborts the CLI. The build on
    GitHub's runners *blocks indefinitely* instead — and unbounded, that single call is what kept
    this repository's CI job from ever finishing. Either outcome supports the claim that the
    mechanism is unusable; they are distinguished in the assertion message rather than collapsed,
    because they call for different words in the design note.
    """
    source = _square_source(tmp_path)

    def render(script: str, dest_name: str):
        script_file = tmp_path / f"{dest_name}.txt"
        script_file.write_text(script, encoding="utf-8")
        return subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                # **This test is why CI never finished.** It deliberately runs a command expected to
                # abort, and on the CI ffmpeg build it does not abort — it blocks, reading the stdin
                # it inherited from pytest, which is a pipe that never reaches EOF. The suite then
                # ran to the 360-minute job ceiling with a final progress line of `82%`, which is
                # this file's position in the run order.
                #
                # `-nostdin` at the argv, *and* the bound the conftest seam now applies to every
                # `subprocess.run` in the suite. Belt and braces deliberately: this particular call
                # is the one that cost months of CI, and an argv flag is visible where a fixture is
                # not.
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-vf",
                f"sendcmd=f={script_file},crop=406:720:437:0,scale=1080:1920,setsar=1",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                str(tmp_path / f"{dest_name}.mp4"),
            ],
            capture_output=True,
            text=True,
            timeout=CROP_COMMAND_TIMEOUT_S,
        )

    moving = render("0.000 crop x 437, crop y 0;\n2.000 crop x 488, crop y 90;\n", "xy")

    # **The mechanism fails in two different ways depending on the ffmpeg build, and both count.**
    #
    # On the apt/johnvansickle builds used locally it aborts the CLI outright. On the build GitHub's
    # runners carry it does something worse: it neither completes nor exits, blocking indefinitely.
    # Unbounded, that is what held this job to its 360-minute ceiling for months (see
    # `tests/conftest.py`), and it is why the last progress line was always `82%` -- this file's
    # position in the run order.
    #
    # The claim this test exists to support is "the crop-size mechanism R2.2 asks for is not usable
    # here, which is why V23 was implemented another way". A hang supports that claim exactly as
    # well as an abort does. So both are accepted -- but recorded distinctly, because "it crashes"
    # and "it wedges the process" call for different words in the design note, and collapsing them
    # into one boolean is what let the hang hide.
    how: str
    try:
        resizing = render("2.000 crop w 304, crop h 540, crop x 488, crop y 90;\n", "wh")
    except subprocess.TimeoutExpired:
        usable = False
        how = f"blocked indefinitely (no exit within {CROP_COMMAND_TIMEOUT_S:g}s)"
    else:
        usable = resizing.returncode == 0
        how = f"exited {resizing.returncode}"

    assert moving.returncode == 0, (
        f"an x/y-only sendcmd script failed, so the comparison below proves nothing: {moving.stderr}"
    )
    assert not usable, (
        "this ffmpeg now accepts mid-stream `crop w`/`crop h` commands (it "
        f"{how}), so V23 can be reimplemented with the crop-size mechanism R2.2 actually asks "
        "for -- see worker/subject_scale.py"
    )


# --------------------------------------------------------------------------- #
# The call sites. Everything above tests the module; these test whether        #
# anything calls it, which is the gap that let three features ship dead.      #
# --------------------------------------------------------------------------- #


def _run_clip(tmp_path, monkeypatch, make_video, **option_overrides):
    """One clip through `run_pipeline`; returns `(clip, captured apply_reframe kwargs)`."""
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    src = make_video("v23_pipeline.mp4", duration=3.0, w=1280, h=720)
    words = [Word(0.2, 0.6, "one"), Word(0.8, 1.2, "two"), Word(1.4, 1.8, "three")]
    monkeypatch.setattr(
        pl,
        "transcribe",
        lambda *a, **k: Transcript(
            language="en", segments=[TranscriptSegment(0.0, 3.0, "one two three", words)]
        ),
    )
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [
            ClipCandidate(start=0.0, end=3.0, score=90.0, reason="r", title="T", text="t")
        ],
    )
    monkeypatch.setattr(pl.compositor, "render_clip", lambda *a, **k: None)

    captured: dict = {}

    def spy(video, dest, **kwargs):
        captured.update(kwargs)
        pl.fu.reformat_aspect(video, dest, aspect=kwargs.get("aspect", "9:16"), mode="crop_blur")
        return dest

    monkeypatch.setattr(pl.reframe, "apply_reframe", spy)

    clips = pl.run_pipeline(
        src,
        options_all_off(aspect="9:16", reframe=True, **option_overrides),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
    )
    assert len(clips) == 1
    return clips[0], captured


@requires_ffmpeg
def test_the_pipeline_enables_normalisation_when_configured(tmp_path, monkeypatch, make_video):
    """Deleting `normalise_scale=` from the pipeline fails here and nowhere else."""
    import worker.pipeline as pl

    monkeypatch.setattr(pl.settings, "subject_scale_normalise", True)
    _clip, captured = _run_clip(tmp_path, monkeypatch, make_video)

    assert captured.get("normalise_scale") is True


@requires_ffmpeg
def test_it_is_off_by_default(tmp_path, monkeypatch, make_video):
    """R2.8. The default must reproduce previous framing exactly."""
    _clip, captured = _run_clip(tmp_path, monkeypatch, make_video)

    assert captured.get("normalise_scale") is False


@requires_ffmpeg
@pytest.mark.parametrize("zoom_option", ["zoom", "transitions"])
def test_it_declines_when_a_zoom_is_also_running_and_says_so(
    tmp_path, monkeypatch, make_video, zoom_option
):
    """R2.10, from both sides.

    V23's mechanism *is* a magnification, so composing it with `zoompan`'s own ramp multiplies two
    scale changes on one shot into a curve neither feature intended. The refusal is recorded because an
    operator who sets `SUBJECT_SCALE_NORMALISE` and sees nothing happen needs to know a zoom outranked
    it rather than infer it.

    Both `zoom` and `transitions` are checked: `transitions` also produces a `zoompan` (the punch-in
    settle), so gating on `zoom` alone would leave the compounding in place for half the cases.
    """
    import worker.pipeline as pl

    monkeypatch.setattr(pl.settings, "subject_scale_normalise", True)
    clip, captured = _run_clip(tmp_path, monkeypatch, make_video, **{zoom_option: True})

    assert captured.get("normalise_scale") is False
    assert "subject_scale_skipped:zoom_active" in clip.effects_applied


@requires_ffmpeg
def test_no_refusal_marker_when_the_feature_was_never_asked_for(tmp_path, monkeypatch, make_video):
    """A refusal marker on a clip nobody configured is noise.

    `subject_scale_skipped:zoom_active` must mean "you asked and a zoom outranked it", not "a zoom was
    on" — otherwise it appears on every zoomed clip and stops carrying information.
    """
    clip, _captured = _run_clip(tmp_path, monkeypatch, make_video, zoom=True)

    assert not [m for m in clip.effects_applied if m.startswith("subject_scale")]


@requires_ffmpeg
def test_the_magnify_filter_reaches_the_rendered_filter_chain(tmp_path, monkeypatch, make_video):
    """Through `apply_reframe` itself, capturing the `-vf` it hands ffmpeg.

    The module tests prove the expression is correct; this proves it is *inserted*, and between `crop`
    and `scale` rather than anywhere else — position is the whole design.
    """
    from worker.effects import reframe as rf

    src = make_video("v23_vf.mp4", duration=3.0, w=1280, h=720)

    # Two shots with different subject sizes, so a magnification is actually planned.
    def fake_report(video, sample_fps=5.0, backend=None, detector=None):
        samples = []
        detections = []
        for i in range(20):
            t = round(i * 0.2, 3)
            height = 200.0 if i < 10 else 90.0
            samples.append(rf.Center(t, 640.0, 360.0))
            detections.append((t, [_Det(height * 0.8, height)]))
        return samples, rf.Sample_Report(
            samples=detections, resolved_backend="injected", effective_fps=5.0, requested_fps=5.0
        )

    monkeypatch.setattr(rf, "track_faces_report", fake_report)
    monkeypatch.setattr(rf.scene_detect, "scan_cuts", lambda *a, **k: [2.0])

    seen: dict = {}

    def fake_run(cmd, *a, **k):
        seen["vf"] = cmd[cmd.index("-vf") + 1]
        return None

    monkeypatch.setattr(rf, "_run", fake_run)
    notes: list[str] = []
    rf.apply_reframe(src, tmp_path / "out.mp4", aspect="9:16", notes=notes, normalise_scale=True)

    vf = seen["vf"]
    assert "zoompan" in vf, f"the magnification was not inserted: {vf}"
    assert vf.index("crop=") < vf.index("zoompan"), (
        "zoompan must follow the crop it magnifies within"
    )
    assert vf.index("zoompan") < vf.index("scale="), "zoompan must precede the delivery scale"
    assert any(m.startswith("subject_scale:") for m in notes), notes


@requires_ffmpeg
def test_disabled_leaves_the_filter_chain_character_for_character_unchanged(
    tmp_path, monkeypatch, make_video
):
    """The discriminator for the test above, and what protects the reframe goldens."""
    from worker.effects import reframe as rf

    src = make_video("v23_vf_off.mp4", duration=3.0, w=1280, h=720)

    def fake_report(video, sample_fps=5.0, backend=None, detector=None):
        samples = [rf.Center(round(i * 0.2, 3), 640.0, 360.0) for i in range(20)]
        detections = [
            (round(i * 0.2, 3), [_Det(160.0, 200.0 if i < 10 else 90.0)]) for i in range(20)
        ]
        return samples, rf.Sample_Report(
            samples=detections, resolved_backend="injected", effective_fps=5.0, requested_fps=5.0
        )

    monkeypatch.setattr(rf, "track_faces_report", fake_report)
    monkeypatch.setattr(rf.scene_detect, "scan_cuts", lambda *a, **k: [2.0])

    captured: list[str] = []
    monkeypatch.setattr(rf, "_run", lambda cmd, *a, **k: captured.append(cmd[cmd.index("-vf") + 1]))

    notes: list[str] = []
    rf.apply_reframe(src, tmp_path / "off.mp4", aspect="9:16", notes=notes, normalise_scale=False)

    assert "zoompan" not in captured[0]
    assert not [m for m in notes if m.startswith("subject_scale")]
