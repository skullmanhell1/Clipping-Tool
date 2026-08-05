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

    # Patches ``track_faces_report`` rather than ``track_faces``: ``apply_reframe`` needs the
    # detection-coverage figure alongside the path, so it calls the report. Patching the
    # wrapper would leave the real cascade running and this test dependent on cv2.
    def fake_report(video, **kwargs):
        return rf.Track_Report(
            [rf.Center(0.0, 300, 360), rf.Center(1.0, 640, 360), rf.Center(2.0, 980, 360)],
            sampled=3, detected=3, tracked=3,
        )

    monkeypatch.setattr(rf, "track_faces_report", fake_report)
    dest = tmp_path / "reframed.mp4"
    rf.apply_reframe(src, dest, aspect="9:16")
    assert dest.exists()
    assert probe_size(dest) == (1080, 1920)


@requires_ffmpeg
def test_apply_reframe_no_faces_raises(make_video, tmp_path, monkeypatch):
    src = make_video("land2.mp4", duration=1.0, w=1280, h=720)
    monkeypatch.setattr(rf, "track_faces_report", lambda *a, **k: rf.Track_Report([]))
    with pytest.raises(rf.ReframeUnavailable):
        rf.apply_reframe(src, tmp_path / "out.mp4", aspect="9:16")



# --------------------------------------------------------------------------- #
# Choosing the subject: presence, not size
# --------------------------------------------------------------------------- #
def _frames(script: list[list[tuple[int, int, int, int]]]) -> list[list[rf.FaceBox]]:
    """Per-frame boxes as FaceBoxes on a 5 fps grid."""
    return [
        [rf.FaceBox(round(i * 0.2, 3), *box) for box in boxes]
        for i, boxes in enumerate(script)
    ]


def test_the_subject_is_the_persistent_face_not_the_biggest_one():
    """The defect this replaces, stated as a test.

    A cascade phantom that happens to be larger than the real face beat it outright under
    largest-box-wins, and the crop jumped to it for as long as it persisted. PR #92 measured
    the shipped cascade at 1.32 faces per detecting frame on a two-shot, so this is a
    documented behaviour of the detector rather than a hypothetical.

    Presence is the discriminator because it is what a phantom lacks: it fires on one
    alignment of background texture and stops, while a person stays in frame.
    """
    real = (760, 470, 140, 140)
    bigger_phantom = (120, 120, 200, 200)
    script = [[real] if i % 3 else [real, bigger_phantom] for i in range(30)]

    # The old rule, for contrast: on every frame the phantom appears, it wins.
    assert rf.pick_main_face([real, bigger_phantom]) == pytest.approx((220.0, 220.0))

    main = rf.choose_main_track(rf.build_face_tracks(_frames(script)))
    assert main is not None
    assert len(main.boxes) == 30, "the subject should be the track present in every frame"
    assert main.center_at(3.0) == pytest.approx((830.0, 540.0))


def test_size_only_breaks_a_tie_in_presence():
    """Two faces present equally often: the larger is the more likely subject."""
    small = (100, 100, 80, 80)
    large = (700, 400, 220, 220)
    script = [[small, large] for _ in range(20)]

    main = rf.choose_main_track(rf.build_face_tracks(_frames(script)))
    assert main is not None
    assert main.center_at(2.0) == pytest.approx((810.0, 510.0)), "the larger face won the tie"


def test_size_cannot_outweigh_much_greater_presence():
    """`sqrt` of the area, not the area, is what makes this hold.

    Under plain area weighting a phantom twice the width has four times the area and needs
    only a quarter of the presence to win. Under `sqrt` it needs half. Here the distractor is
    twice the width and present a third as often, so it must lose.
    """
    real = (760, 470, 140, 140)
    huge = (100, 100, 280, 280)
    script = [[real] if i % 3 else [real, huge] for i in range(30)]

    main = rf.choose_main_track(rf.build_face_tracks(_frames(script)))
    assert main is not None
    assert main.center_at(3.0) == pytest.approx((830.0, 540.0))


def test_choose_main_track_handles_nothing_to_choose():
    assert rf.choose_main_track([]) is None
    assert rf.choose_main_track([rf.Face_Track("F1", [])]) is None


def test_choose_main_track_is_deterministic_on_an_exact_tie():
    """Two identical tracks must resolve the same way every run, or the render is not
    reproducible. `max` returns the first maximal element, which is frame order."""
    a = rf.Face_Track("F1", [rf.FaceBox(0.0, 0, 0, 100, 100)])
    b = rf.Face_Track("F2", [rf.FaceBox(0.0, 500, 0, 100, 100)])
    assert rf.choose_main_track([a, b]).track_id == "F1"
    assert rf.choose_main_track([b, a]).track_id == "F2"


def test_track_report_coverage_describes_the_tracked_subject():
    """Coverage is tracked/sampled, not detected/sampled.

    A frame where some *other* face was found is still a frame the subject was not found in,
    and therefore a frame the crop is holding a stale position on.
    """
    report = rf.Track_Report([], sampled=100, detected=90, tracked=60)
    assert report.coverage == pytest.approx(0.60)
    assert rf.Track_Report([]).coverage == 0.0, "no samples must not divide by zero"


# --------------------------------------------------------------------------- #
# The detection coercion trap
# --------------------------------------------------------------------------- #
def test_a_facebox_shaped_detection_is_not_silently_discarded():
    """`FaceBox` has five fields, so positional unpacking to (x, y, w, h) rejected it.

    The old coercion caught the resulting ValueError and skipped the box, so a detector
    returning FaceBoxes produced empty frames - a working render, a `reframe` marker, and a
    crop that never moved. Inert with the built-in cascade, which yields 4-tuples, and a trap
    for precisely the person plugging in a better detector.
    """
    assert rf._as_rect(rf.FaceBox(1.5, 10, 20, 30, 40)) == (10, 20, 30, 40)
    assert rf._as_rect((10, 20, 30, 40)) == (10, 20, 30, 40)
    assert rf._as_rect([10, 20, 30, 40]) == (10, 20, 30, 40)
    assert rf._as_rect((10, 20, 30)) is None
    assert rf._as_rect("nonsense") is None
    assert rf._as_rect(None) is None


@requires_ffmpeg
def test_detect_faces_keeps_boxes_from_a_detector_that_returns_faceboxes(make_video, tmp_path):
    """End-to-end of the same trap, through the real sampling loop."""
    src = make_video("coerce.mp4", duration=1.0, w=640, h=360)

    def detector(_frame):
        return [rf.FaceBox(0.0, 100, 80, 120, 120)]

    per_frame = rf.detect_faces(src, sample_fps=2.0, max_samples=4, detector=detector)
    assert per_frame, "no frames were sampled at all"
    assert any(boxes for boxes in per_frame), (
        "every FaceBox-shaped detection was discarded by the coercion"
    )
    assert per_frame[0][0].center == pytest.approx((160.0, 140.0))
