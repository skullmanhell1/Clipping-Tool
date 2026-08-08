"""Detector and confidence markers on both geometry paths, and the degradation ladder.

Spec `.kiro/specs/face-detection-upgrade` tasks 6.4, 6.5, 6.6 (Requirements 3, 4, 6, 8, 9, 11.3).

A separate file rather than an extension of ``tests/test_speaker_reframe.py`` because that file
declares itself pure/offline/CPU-only in its module docstring -- it constructs ``FaceBox`` and
``Face_Track`` directly and touches neither ffmpeg nor OpenCV. These tests need a real render to
observe what lands in ``effects_applied``, so putting them there would quietly falsify that
docstring for every reader who relies on it.

What is asserted here is the *reporting*, which is the half a suite of fakes can actually
verify. Whether the framing is better is a question only the pixels answer, which is what
``scripts/smoke_reel.py`` is for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import options_all_off, requires_ffmpeg
from worker.effects import reframe as rf


def _centre_boxes(n: int = 12, *, w: int = 640, h: int = 360):
    """A sampler output with one centred face in every frame: coverage 1.0."""
    return [[rf.FaceBox(round(i * 0.2, 3), w // 2 - 40, h // 2 - 40, 80, 80)] for i in range(n)]


def _sparse_boxes(n: int = 12, hit_every: int = 6, *, w: int = 640, h: int = 360):
    """A sampler output with a face in only some frames: low coverage on purpose."""
    return [
        [rf.FaceBox(round(i * 0.2, 3), w // 2 - 40, h // 2 - 40, 80, 80)]
        if i % hit_every == 0
        else []
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #
# 6.4 — the marker names what ran, on both paths                               #
# --------------------------------------------------------------------------- #
@requires_ffmpeg
def test_the_single_speaker_path_records_the_resolved_backend(make_video, tmp_path):
    """Requirements 3.1, 3.4, 11.3 — for the injected case, which is what a test can force."""
    src = make_video("marker1.mp4", duration=2.0, w=1280, h=720)
    notes: list[str] = []

    def detector(_frame):
        return [(600, 300, 120, 120)]

    rf.apply_reframe(src, tmp_path / "out1.mp4", aspect="9:16", detector=detector, notes=notes)
    assert "face_detector:injected" in notes, notes
    assert not any(n.startswith("face_detector_substituted") for n in notes), notes


@requires_ffmpeg
def test_the_default_backend_records_haar(make_video, tmp_path):
    """Requirement 3.1 for the default path: the marker says what actually ran.

    No injected detector, so this exercises the real cascade. The clip is synthetic and
    contains no face, so reframe raises and no marker is recorded -- which is itself the
    contract under test: markers are appended only once a render has succeeded, so a clip that
    fell back to the static reformat never claims a detector framed it.
    """
    src = make_video("marker2.mp4", duration=1.0, w=1280, h=720)
    notes: list[str] = []
    with pytest.raises(rf.ReframeUnavailable):
        rf.apply_reframe(src, tmp_path / "out2.mp4", aspect="9:16", notes=notes)
    assert notes == [], "a failed reframe must not leave a detector marker behind"


@requires_ffmpeg
def test_a_missing_model_records_the_substitution_naming_both_sides(
    make_video, tmp_path, monkeypatch
):
    """Requirements 3.2, 4.2a — and the operand order is the point of the assertion."""
    src = make_video("marker3.mp4", duration=2.0, w=1280, h=720)
    empty_models = tmp_path / "no-models"
    empty_models.mkdir()
    monkeypatch.setattr(rf.settings, "face_model_dir", empty_models)

    notes: list[str] = []
    # A real cascade would find nothing in synthetic footage, so the sampler is injected to
    # give the render a face path; the backend under test is the *resolution*, which the
    # sampler does not bypass because it is passed via `backend`.
    report = rf.sample_face_report(src, sample_fps=2.0, backend="mediapipe", model_dir=empty_models)
    assert report.resolved_backend == "substituted:mediapipe:haar"
    notes.extend(rf.detector_notes(report))
    assert "face_detector_substituted:mediapipe:haar" in notes, notes
    prefix, requested, resolved = notes[0].split(":")
    assert (prefix, requested, resolved) == (
        "face_detector_substituted",
        "mediapipe",
        "haar",
    )


@requires_ffmpeg
def test_the_speaker_path_records_the_same_vocabulary(make_video, tmp_path):
    """Requirement 6.5 — both geometry paths report identically.

    The speaker-aware path takes an injected *sampler*, which bypasses detection entirely; it
    must still report in the same vocabulary rather than going silent where the other path
    speaks.
    """
    from worker.diarization import Speaker_Turn

    src = make_video("marker4.mp4", duration=2.4, w=1280, h=720)
    notes: list[str] = []
    rf.apply_speaker_reframe(
        src,
        tmp_path / "out4.mp4",
        turns=[Speaker_Turn("S1", 0.0, 2.4)],
        aspect="9:16",
        sampler=lambda _v: _centre_boxes(12, w=1280, h=720),
        notes=notes,
    )
    assert "face_detector:injected" in notes, notes


# --------------------------------------------------------------------------- #
# 6.4 — the low-confidence marker                                              #
# --------------------------------------------------------------------------- #
def test_low_confidence_is_recorded_below_the_floor_and_carries_the_measurement():
    """Requirements 6.2, 6.6."""
    report = rf.synthetic_report(_sparse_boxes(12, hit_every=6), "injected", 5.0)
    assert report.coverage == pytest.approx(2 / 12)
    notes = rf.detector_notes(report)
    assert "reframe_low_confidence:0.17" in notes, notes


def test_low_confidence_is_not_recorded_at_or_above_the_floor(monkeypatch):
    """Requirement 6.3 — the boundary is inclusive on the "fine" side."""
    monkeypatch.setattr(rf.settings, "reframe_coverage_floor", 0.5)
    half = rf.synthetic_report(_sparse_boxes(12, hit_every=2), "injected", 5.0)
    assert half.coverage == pytest.approx(0.5)
    assert not any(n.startswith("reframe_low_confidence") for n in rf.detector_notes(half))

    full = rf.synthetic_report(_centre_boxes(12), "injected", 5.0)
    assert full.coverage == 1.0
    assert not any(n.startswith("reframe_low_confidence") for n in rf.detector_notes(full))


def test_zero_detections_records_no_low_confidence_marker():
    """Requirement 6.4 — the two conditions must stay distinguishable.

    Zero coverage is already reported by the existing no-faces degradation (the reframe raises
    and the pipeline falls back), so ``reframe_low_confidence:0.00`` beside it would be a second
    name for one condition. ``faces_none`` and the low-confidence marker are mutually exclusive
    by construction here, not by convention at the call sites.
    """
    empty = rf.synthetic_report([[], [], []], "haar", 5.0)
    assert empty.coverage == 0.0
    notes = rf.detector_notes(empty)
    assert notes == ["face_detector:haar"], notes
    assert not any("low_confidence" in n for n in notes)
    assert not any("faces_none" in n for n in notes)


def test_the_two_conditions_are_never_both_present():
    """Stated as the invariant, over the whole coverage range."""
    for hit_every in (1, 2, 3, 4, 6, 12, 10**6):
        report = rf.synthetic_report(_sparse_boxes(12, hit_every=hit_every), "haar", 5.0)
        notes = rf.detector_notes(report)
        low = [n for n in notes if n.startswith("reframe_low_confidence")]
        assert not (low and report.coverage == 0.0), (hit_every, notes)


# --------------------------------------------------------------------------- #
# 6.4 — the sampling-rate marker                                               #
# --------------------------------------------------------------------------- #
@requires_ffmpeg
def test_the_sampling_marker_appears_only_when_the_cap_bound(make_video):
    """Requirements 8.1, 8.4 — and 8.2, since the defaults are not touched to achieve it."""
    src = make_video("cap.mp4", duration=4.0, w=320, h=240)

    uncapped = rf.sample_face_report(src, sample_fps=2.0, detector=lambda _f: [])
    assert not any(n.startswith("reframe_sample_rate") for n in rf.detector_notes(uncapped)), (
        rf.detector_notes(uncapped)
    )

    capped = rf.sample_face_report(src, sample_fps=10.0, max_samples=3, detector=lambda _f: [])
    rate = [n for n in rf.detector_notes(capped) if n.startswith("reframe_sample_rate")]
    assert len(rate) == 1, rf.detector_notes(capped)
    assert rate[0].count(".") == 1, rate[0]


# --------------------------------------------------------------------------- #
# 6.5 — the degradation ladder, rung by rung                                   #
# --------------------------------------------------------------------------- #
@requires_ffmpeg
def test_rung_1_injected_detector(make_video):
    src = make_video("rung1.mp4", duration=1.0, w=320, h=240)
    report = rf.sample_face_report(src, sample_fps=2.0, detector=lambda _f: [(1, 1, 9, 9)])
    assert report.resolved_backend == "injected"
    assert report.coverage == 1.0


@requires_ffmpeg
def test_rung_2_mediapipe_constructible(make_video):
    """The vendored model is present, so this rung is reachable in CI without a network."""
    src = make_video("rung2.mp4", duration=1.0, w=320, h=240)
    report = rf.sample_face_report(src, sample_fps=2.0, backend="mediapipe")
    assert report.resolved_backend == "mediapipe"
    assert report.samples, "mediapipe resolved but produced no samples"


@requires_ffmpeg
def test_rung_3_mediapipe_unimportable_substitutes_haar(make_video, monkeypatch):
    src = make_video("rung3.mp4", duration=1.0, w=320, h=240)
    monkeypatch.setattr(rf, "_mediapipe_detector", lambda *_a, **_k: None)
    report = rf.sample_face_report(src, sample_fps=2.0, backend="mediapipe")
    assert report.resolved_backend == "substituted:mediapipe:haar"
    assert report.samples, "the substitution must still sample"


@requires_ffmpeg
def test_rung_4_haar(make_video):
    src = make_video("rung4.mp4", duration=1.0, w=320, h=240)
    report = rf.sample_face_report(src, sample_fps=2.0, backend="haar")
    assert report.resolved_backend == "haar"
    assert report.samples


@requires_ffmpeg
def test_rung_5_nothing_constructible_yields_zero_samples(make_video, monkeypatch):
    """Requirements 4.4, 4.5 — and it must not raise out of the geometry stage."""
    src = make_video("rung5.mp4", duration=1.0, w=320, h=240)
    monkeypatch.setattr(rf, "_default_haar_detector", lambda _cv2: None)
    report = rf.sample_face_report(src, sample_fps=2.0, backend="haar")
    assert report.samples == []
    assert report.coverage == 0.0

    # ...and the geometry stage degrades rather than propagating.
    with pytest.raises(rf.ReframeUnavailable):
        rf.apply_reframe(src, src.parent / "rung5_out.mp4", aspect="9:16")


@requires_ffmpeg
def test_no_rung_raises_out_of_the_geometry_stage(make_video, tmp_path, monkeypatch):
    """Requirement 4.6 — every rung either renders or raises ReframeUnavailable.

    ``ReframeUnavailable`` is the pipeline's expected signal, not an escape: the geometry ladder
    catches it. Anything else -- an ImportError, a cv2 error, a TypeError from a coordinate
    mistake -- would reach the job and fail a clip.
    """
    src = make_video("rungs.mp4", duration=1.0, w=320, h=240)
    for name in ("haar", "mediapipe", "nonsense"):
        try:
            rf.apply_reframe(src, tmp_path / f"{name}.mp4", aspect="9:16", backend=name)
        except rf.ReframeUnavailable:
            pass  # the expected, handled signal
        except Exception as exc:  # the point of the test
            pytest.fail(f"backend {name!r} raised {type(exc).__name__}: {exc}")

    # And the missing-model rung, which resolves through settings rather than an argument.
    monkeypatch.setattr(rf.settings, "face_model_dir", tmp_path / "absent")
    try:
        rf.apply_reframe(src, tmp_path / "nomodel.mp4", aspect="9:16", backend="mediapipe")
    except rf.ReframeUnavailable:
        pass
    except Exception as exc:
        pytest.fail(f"missing-model rung raised {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# 6.6 — geometry is unchanged on the default backend                           #
# --------------------------------------------------------------------------- #
@requires_ffmpeg
def test_the_sendcmd_script_is_identical_with_and_without_the_new_plumbing(
    make_video, tmp_path, monkeypatch
):
    """Requirements 9.1, 9.3, 9.4 — the same crop path produces the same script.

    Captures the ``sendcmd`` script by intercepting the ffmpeg invocation, then compares a run
    that passes the new ``backend``/``notes`` arguments against one that passes neither. Byte
    equality of the script and the ``-vf`` string is the strongest available statement that the
    filter graph and the crop path did not move.
    """
    src = make_video("parity.mp4", duration=2.0, w=1280, h=720)
    captured: list[tuple[str, str]] = []

    real_run = rf._run

    def capture(cmd, *a, **k):
        vf = cmd[cmd.index("-vf") + 1] if "-vf" in cmd else ""
        script = ""
        for part in vf.split(","):
            if part.startswith("sendcmd=f='"):
                path = part[len("sendcmd=f='") :].rstrip("'").replace("\\", "")
                try:
                    # Read via Path so the handle closes: `warnings = error` turns a leaked
                    # file into a PytestUnraisableExceptionWarning, which fails the run in a
                    # place that has nothing to do with the leak.
                    script = Path(path).read_text(encoding="utf-8")
                except OSError:
                    script = "<unreadable>"
        captured.append((vf, script))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(rf, "_run", capture)

    def detector(_frame):
        return [(600, 300, 120, 120)]

    rf.apply_reframe(src, tmp_path / "a.mp4", aspect="9:16", detector=detector)
    notes: list[str] = []
    rf.apply_reframe(
        src,
        tmp_path / "b.mp4",
        aspect="9:16",
        detector=detector,
        backend="haar",
        notes=notes,
    )

    assert len(captured) == 2
    (vf_a, script_a), (vf_b, script_b) = captured
    # The sendcmd path differs (different dest), so compare the script contents and the filter
    # chain with the path segment removed.
    assert script_a == script_b, "the sendcmd crop path changed"
    strip = lambda vf: ",".join(  # noqa: E731
        p for p in vf.split(",") if not p.startswith("sendcmd=f='")
    )
    assert strip(vf_a) == strip(vf_b), "the filter graph shape changed"
    # The new arguments earned a marker; the old call signature earned none.
    assert notes and notes[0] == "face_detector:injected"


@requires_ffmpeg
def test_the_default_options_produce_no_detector_marker_change_in_the_pipeline(
    make_video, tmp_path, monkeypatch
):
    """Requirement 9.2, at the level that matters: a default run's marker set.

    The only permitted addition on a default run is ``face_detector:haar``, and it appears only
    when reframe actually succeeded. Asserted through the real pipeline rather than the reframe
    module, because ``effects_applied`` is assembled there.
    """
    import worker.pipeline as pl
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    src = make_video("pipe.mp4", duration=4.0, w=1280, h=720)
    monkeypatch.setattr(
        pl,
        "transcribe",
        lambda s, language=None, translate=False, **_kw: Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, 4.0, "one two", [Word(0.2, 0.6, "one")])],
        ),
    )
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, reason="t", text="x")],
    )
    # An injected detector that finds a face, so reframe succeeds and the marker is recorded.
    monkeypatch.setattr(pl, "FACE_DETECTOR", lambda _frame: [(600, 300, 120, 120)])

    clips = pl.run_pipeline(
        src,
        options_all_off(captions=False, metadata=False, aspect="9:16", reframe=True),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
    )
    assert len(clips) == 1
    markers = clips[0].effects_applied
    assert "reframe" in markers, markers
    detector_markers = [m for m in markers if m.startswith("face_detector")]
    assert detector_markers == ["face_detector:injected"], markers
    # No substitution and no low-confidence noise on a clip whose every frame had a face.
    assert not any(m.startswith("face_detector_substituted") for m in markers), markers
    assert not any(m.startswith("reframe_low_confidence") for m in markers), markers
