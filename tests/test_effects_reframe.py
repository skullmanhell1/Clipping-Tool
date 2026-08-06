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

    def fake_track(video, sample_fps=5.0):
        return [rf.Center(0.0, 300, 360), rf.Center(1.0, 640, 360), rf.Center(2.0, 980, 360)]

    monkeypatch.setattr(rf, "track_faces", fake_track)
    dest = tmp_path / "reframed.mp4"
    rf.apply_reframe(src, dest, aspect="9:16")
    assert dest.exists()
    assert probe_size(dest) == (1080, 1920)


@requires_ffmpeg
def test_apply_reframe_no_faces_raises(make_video, tmp_path, monkeypatch):
    src = make_video("land2.mp4", duration=1.0, w=1280, h=720)
    monkeypatch.setattr(rf, "track_faces", lambda *a, **k: [])
    with pytest.raises(rf.ReframeUnavailable):
        rf.apply_reframe(src, tmp_path / "out.mp4", aspect="9:16")
