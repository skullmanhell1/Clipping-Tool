"""Focused offline regressions for aspect-aware smart reframing.

These tests stay on the pure crop-path helpers: no OpenCV, ffmpeg, rendering, or network access is
needed to prove the requested geometry, continuity, and detector-jitter behaviour.
"""

from __future__ import annotations

import pytest

from worker.diarization import Speaker_Turn
from worker.effects.reframe import (
    Association,
    Face_Track,
    FaceBox,
    build_follow_active_path,
    compute_crop_size,
    smooth_bounding_boxes,
)


@pytest.mark.parametrize(
    ("aspect", "expected"),
    [
        ("9:16", (608, 1080)),
        ("1:1", (1080, 1080)),
        ("16:9", (1920, 1080)),
    ],
)
def test_smart_reframe_uses_the_requested_aspect_crop(aspect, expected):
    """The crop window is aspect-aware for portrait, square, and landscape output."""
    aw, ah = (int(part) for part in aspect.split(":"))
    crop = compute_crop_size(1920, 1080, aw, ah)

    assert crop == expected
    assert all(value % 2 == 0 for value in crop)
    assert crop[0] <= 1920 and crop[1] <= 1080


@pytest.mark.parametrize("aspect", ["9:16", "1:1", "16:9"])
def test_follow_active_path_clamps_to_each_aspect_window(aspect):
    """A moving active region remains continuous and inside the aspect-specific crop bounds."""
    aw, ah = (int(part) for part in aspect.split(":"))
    crop_w, crop_h = compute_crop_size(1920, 1080, aw, ah)
    boxes = [
        FaceBox(0.0, 240, 420, 160, 160),
        FaceBox(0.5, 680, 420, 160, 160),
        FaceBox(1.0, 1160, 420, 160, 160),
        FaceBox(1.5, 1480, 420, 160, 160),
    ]
    tracks = [Face_Track("speaker", boxes)]
    path = build_follow_active_path(
        [Speaker_Turn("S1", 0.0, 2.0)],
        Association(by_turn={0: "speaker"}, shown_order=["speaker"]),
        tracks,
        src_w=1920,
        src_h=1080,
        crop_w=crop_w,
        crop_h=crop_h,
        command_fps=10.0,
        duration=2.0,
    )

    assert path
    assert path[0].t == pytest.approx(0.0)
    assert path[-1].t == pytest.approx(2.0)
    assert all(crop_w / 2 <= c.cx <= 1920 - crop_w / 2 for c in path)
    assert all(crop_h / 2 <= c.cy <= 1080 - crop_h / 2 for c in path)
    assert all(b.cx >= a.cx - 1e-6 for a, b in zip(path, path[1:]))


def test_follow_active_path_rejects_a_one_frame_bbox_spike():
    """An isolated detector jump does not become a visible crop jump."""
    boxes = [
        FaceBox(0.0, 320, 380, 120, 120),
        FaceBox(0.2, 340, 380, 120, 120),
        # A false ROI spike between otherwise continuous detections.
        FaceBox(0.4, 1500, 380, 120, 120),
        FaceBox(0.6, 360, 380, 120, 120),
        FaceBox(0.8, 380, 380, 120, 120),
    ]
    path = build_follow_active_path(
        [Speaker_Turn("S1", 0.0, 1.0)],
        Association(by_turn={0: "speaker"}, shown_order=["speaker"]),
        [Face_Track("speaker", boxes)],
        src_w=1920,
        src_h=1080,
        crop_w=608,
        crop_h=1080,
        command_fps=5.0,
        duration=1.0,
    )

    jumps = [abs(b.cx - a.cx) for a, b in zip(path, path[1:])]
    assert max(jumps) < 100.0
    assert path[2].cx < 600.0


def test_smooth_bounding_boxes_reduces_alternating_detector_jitter():
    """The robust box profile reduces repeated +/- detector wobble while retaining its path."""
    boxes = [
        FaceBox(index * 0.2, 400 + (60 if index % 2 else 0), 300, 100, 100) for index in range(8)
    ]
    raw_centers = [box.center[0] for box in boxes]
    smoothed = smooth_bounding_boxes(boxes)
    smoothed_centers = [box.center[0] for box in smoothed]

    raw_span = max(raw_centers) - min(raw_centers)
    smoothed_span = max(smoothed_centers) - min(smoothed_centers)
    assert smoothed_span < raw_span * 0.75
    mean = sum(smoothed_centers) / len(smoothed_centers)
    assert min(raw_centers) <= mean <= max(raw_centers)


def test_follow_active_holds_the_safe_center_when_no_region_is_available():
    """The existing no-detection fallback remains a centered, bounded crop path."""
    path = build_follow_active_path(
        [Speaker_Turn("S1", 0.0, 1.0)],
        Association(by_turn={0: None}, unassociated=[0]),
        [],
        src_w=1920,
        src_h=1080,
        crop_w=608,
        crop_h=1080,
        command_fps=5.0,
        duration=1.0,
    )

    assert path
    assert all(c.cx == pytest.approx(960.0) for c in path)
    assert all(c.cy == pytest.approx(540.0) for c in path)
