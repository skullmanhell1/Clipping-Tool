"""Tests for face-tracking reframe: pure geometry/smoothing + ffmpeg apply."""

from __future__ import annotations

import pytest

from tests.conftest import probe_size, requires_ffmpeg
from worker.effects import reframe as rf


def test_compute_crop_size_vertical_from_landscape():
    # 9:16 crop of a 1920x1080 frame -> full height, narrow width.
    cw, ch = rf.compute_crop_size(1920, 1080, 9, 16)
    assert ch == 1080
    assert abs(cw - int(1080 * 9 / 16)) <= 2
    assert cw % 2 == 0 and ch % 2 == 0


def test_compute_crop_size_square():
    cw, ch = rf.compute_crop_size(1920, 1080, 1, 1)
    assert cw == ch == 1080


def test_pick_main_face_largest():
    assert rf.pick_main_face([]) is None
    center = rf.pick_main_face([(0, 0, 10, 10), (100, 100, 50, 50)])
    assert center == (125.0, 125.0)


def test_ema_smoothing_dampens_jumps():
    smoothed = rf.ema_smooth([0, 100, 100, 100], alpha=0.5)
    assert smoothed[0] == 0
    assert 0 < smoothed[1] < 100  # first jump is damped
    assert smoothed[-1] > smoothed[1]


def test_resample_centers_uniform_grid():
    samples = [rf.Center(0.0, 0, 50), rf.Center(2.0, 200, 50)]
    dense = rf.resample_centers(samples, fps=10, duration=2.0)
    assert len(dense) == 21  # 0..2s at 10fps inclusive
    assert dense[0].t == 0.0
    assert dense[-1].t == pytest.approx(2.0, abs=0.05)


def test_build_sendcmd_clamps_within_frame():
    centers = [rf.Center(0.0, 0, 0), rf.Center(1.0, 10000, 10000)]
    script = rf.build_sendcmd(centers, 400, 720, 1280, 720)
    lines = script.strip().splitlines()
    assert lines[0].startswith("0.000 crop x 0, crop y 0;")
    # Second center is way off-frame -> clamped to max x/y (880, 0).
    assert "crop x 880" in lines[1]


@requires_ffmpeg
def test_apply_reframe_with_synthetic_track(make_video, tmp_path, monkeypatch):
    """apply_reframe should produce a target-sized clip using a face path.

    Face detection (cv2) is monkeypatched with a synthetic moving path so the
    ffmpeg sendcmd+crop mechanics are exercised without OpenCV.
    """
    src = make_video("land.mp4", duration=2.0, w=1280, h=720)

    # Patches ``track_faces_report`` rather than ``track_faces``: since the
    # detection-confidence work, ``apply_reframe`` needs the centre path *and* the coverage
    # measured from the same sampling pass (a second pass could disagree with the first, and the
    # disagreement would be invisible), so it calls the reporting sibling. ``track_faces`` keeps
    # its signature and remains the public single-speaker entry point.
    def fake_track_report(video, sample_fps=5.0, **_kwargs):
        centres = [rf.Center(0.0, 300, 360), rf.Center(1.0, 640, 360), rf.Center(2.0, 980, 360)]
        report = rf.synthetic_report([[(0, 0, 10, 10)]] * 3, "injected", sample_fps)
        return centres, report

    monkeypatch.setattr(rf, "track_faces_report", fake_track_report)
    dest = tmp_path / "reframed.mp4"
    rf.apply_reframe(src, dest, aspect="9:16")
    assert dest.exists()
    assert probe_size(dest) == (1080, 1920)


@requires_ffmpeg
def test_apply_reframe_no_faces_raises(make_video, tmp_path, monkeypatch):
    src = make_video("land2.mp4", duration=1.0, w=1280, h=720)
    # See the note above on why the reporting sibling is the patch point.
    monkeypatch.setattr(
        rf,
        "track_faces_report",
        lambda *a, **k: ([], rf.synthetic_report([], "injected", 5.0)),
    )
    with pytest.raises(rf.ReframeUnavailable):
        rf.apply_reframe(src, tmp_path / "out.mp4", aspect="9:16")


# --------------------------------------------------------------------------- #
# 4.5 — the sampler wrapper is unchanged, and the report agrees with it         #
#                                                                             #
# Spec `.kiro/specs/face-detection-upgrade` task 4.5. `_sample_face_boxes` grew a
# sibling that reports what was learned while sampling; the wrapper's signature and
# return type are load-bearing, because `FRAME_SAMPLER` in worker/pipeline.py is
# patched by name and the tests above call it directly.
# --------------------------------------------------------------------------- #
@requires_ffmpeg
def test_sample_face_boxes_still_returns_plain_tuples(make_video, tmp_path):
    """Requirement 9.1 — the pre-existing contract, asserted on its exact shape.

    Not "something list-like": ``(float, [(int, int, int, int), ...])``. A report object or a
    ``Detection`` leaking out here would be a silent signature change for every caller that
    unpacks four values.
    """
    src = make_video("sampler.mp4", duration=2.0, w=320, h=240)

    def fake_detector(_frame):
        return [(10, 20, 30, 40)]

    samples = rf._sample_face_boxes(src, sample_fps=2.0, detector=fake_detector)
    assert samples, "no frames sampled"
    for t, boxes in samples:
        assert isinstance(t, float)
        assert boxes == [(10, 20, 30, 40)]
        for box in boxes:
            assert isinstance(box, tuple) and len(box) == 4
            assert all(isinstance(v, int) for v in box)


@requires_ffmpeg
def test_the_report_carries_the_same_samples_as_the_wrapper(make_video, tmp_path):
    """The wrapper is the report's ``as_tuples()``, so the two cannot drift apart."""
    src = make_video("sampler2.mp4", duration=2.0, w=320, h=240)

    def fake_detector(_frame):
        return [(1, 2, 3, 4)]

    report = rf.sample_face_report(src, sample_fps=2.0, detector=fake_detector)
    assert report.as_tuples() == rf._sample_face_boxes(src, sample_fps=2.0, detector=fake_detector)
    assert report.resolved_backend == "injected"
    assert report.coverage == 1.0


@requires_ffmpeg
def test_a_detector_raising_on_one_frame_yields_a_zero_detection_frame(make_video):
    """Requirement 4.3 — one bad frame is not a broken backend.

    Aborting would discard every frame that worked; the zero-detection frame is also the
    honest contribution to coverage, which correctly drops.
    """
    src = make_video("sampler3.mp4", duration=2.0, w=320, h=240)
    calls = {"n": 0}

    def flaky(_frame):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("this frame explodes")
        return [(5, 5, 20, 20)]

    report = rf.sample_face_report(src, sample_fps=2.0, detector=flaky)
    assert len(report.samples) >= 2, "sampling stopped at the raising frame"
    assert report.samples[0][1] == [], "the raising frame should contribute no detections"
    assert any(boxes for _t, boxes in report.samples[1:]), "later frames should still detect"
    assert 0.0 < report.coverage < 1.0, report.coverage


@requires_ffmpeg
def test_the_backend_close_is_called_even_when_sampling_raises(make_video, monkeypatch):
    """Requirement 2.9 — MediaPipe holds a native graph, released in a ``finally``."""
    src = make_video("sampler4.mp4", duration=1.0, w=320, h=240)
    closed = {"n": 0}

    def detector(_frame):
        return []

    detector.close = lambda: closed.__setitem__("n", closed["n"] + 1)  # type: ignore[attr-defined]

    # Force the read loop to blow up *after* the detector was resolved. A plain stand-in, not a
    # subclass of cv2.VideoCapture: subclassing the pybind11 type without running its native
    # constructor crashes the interpreter at collection time, after the suite has already
    # reported success — which is a far worse failure than the one being tested.
    import cv2

    class Exploding:
        def __init__(self, *_a, **_k):
            pass

        @staticmethod
        def isOpened():  # mirrors the cv2 API
            return True

        @staticmethod
        def get(_prop):
            return 30.0

        @staticmethod
        def read():
            raise RuntimeError("decode exploded")

        @staticmethod
        def release():
            return None

    monkeypatch.setattr(cv2, "VideoCapture", Exploding)
    with pytest.raises(RuntimeError):
        rf.sample_face_report(src, sample_fps=2.0, detector=detector)
    assert closed["n"] == 1, "close() was not called when sampling raised"


@requires_ffmpeg
def test_the_close_is_called_on_the_happy_path_too(make_video):
    src = make_video("sampler5.mp4", duration=1.0, w=320, h=240)
    closed = {"n": 0}

    def detector(_frame):
        return []

    detector.close = lambda: closed.__setitem__("n", closed["n"] + 1)  # type: ignore[attr-defined]

    rf.sample_face_report(src, sample_fps=2.0, detector=detector)
    assert closed["n"] == 1


def test_an_unopenable_video_reports_no_samples_and_never_raises():
    """Requirement 4.4 — and it still names the backend it resolved.

    "No samples because the video would not open" and "no samples because no detector could be
    built" are different faults, and the resolved label is the only place a caller can tell
    them apart.
    """
    report = rf.sample_face_report("/nonexistent/video.mp4", sample_fps=2.0, detector=lambda _f: [])
    assert report.samples == []
    assert report.coverage == 0.0
    assert report.resolved_backend == "injected"


@requires_ffmpeg
def test_the_cap_is_reported_only_when_it_binds(make_video):
    """Requirement 8.1, 8.4 — the marker exists to explain a *reduced* rate.

    Also pins that the cap's own default is untouched: this passes an explicit cap rather than
    changing ``reframe_sample_cap``.
    """
    src = make_video("sampler6.mp4", duration=4.0, w=320, h=240)

    uncapped = rf.sample_face_report(src, sample_fps=2.0, detector=lambda _f: [])
    assert not uncapped.capped, (uncapped.effective_fps, uncapped.requested_fps)

    capped = rf.sample_face_report(src, sample_fps=10.0, max_samples=3, detector=lambda _f: [])
    assert len(capped.samples) <= 3
    assert capped.capped, (capped.effective_fps, capped.requested_fps)
    assert capped.effective_fps < capped.requested_fps


@requires_ffmpeg
def test_coverage_comes_from_the_samples_the_crop_path_uses(make_video):
    """Requirement 5.5 — one sampling pass, so the report cannot describe other frames."""
    src = make_video("sampler7.mp4", duration=2.0, w=320, h=240)
    seen = {"n": 0}

    def alternating(_frame):
        seen["n"] += 1
        return [(1, 1, 10, 10)] if seen["n"] % 2 else []

    report = rf.sample_face_report(src, sample_fps=4.0, detector=alternating)
    hits = sum(1 for _t, boxes in report.samples if boxes)
    assert report.coverage == pytest.approx(hits / len(report.samples))
