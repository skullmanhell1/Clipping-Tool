"""Face detection verified against the real MediaPipe library — no mocks.

Spec `.kiro/specs/face-detection-upgrade` tasks 1b.6, 3.1, 3.2 (Requirements 11, 13).

**Why this file exists.** The MediaPipe backend converts a detector's bounding box into the
absolute-pixel boxes the rest of the reframe path assumes. If a normalised box ever leaked
through as pixels, the result is a one-pixel face at the frame origin and *nothing objects*:
``pick_main_face`` returns a centre, ``FaceBox`` validates, ``build_face_tracks`` builds
tracks, ``build_sendcmd`` clamps to a valid window, ffmpeg encodes, and the clip record says
``reframe``. Every clip would be cropped to the frame's left edge and the only evidence would
be the pixels. A suite of fake detectors returning pixel tuples cannot catch that, because the
fakes would be right and the real backend wrong — which is the same shape as the
``font_substituted:Arial`` defect.

**No availability skip, deliberately.** ``mediapipe`` is a hard dependency in
``requirements.txt`` and the model is committed under ``assets/models/``. A skip here would
mean one of those two vanished, which is exactly the condition the no-skips rule exists to
surface. If this file errors, that is the correct outcome.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from worker.effects.reframe import Detection, relative_box_to_pixels, resolve_detector
from worker.face_models import MODEL_MANIFEST, resolve_model

FRAME_W, FRAME_H = 640, 480


def _synthetic_face_rgb(width: int = FRAME_W, height: int = FRAME_H) -> np.ndarray:
    """A face-like image built with PIL rather than a vendored photograph.

    Deliberately generated instead of committed: a photograph of a real person is a licensing
    and privacy question this feature does not need to answer, and the assertions here are
    about *coordinates*, not about detection quality. The drawing is crude but BlazeFace scores
    it around 0.89, which is ample for a geometry test.
    """
    image = Image.new("RGB", (width, height), (70, 90, 120))
    draw = ImageDraw.Draw(image)
    cx, cy = width // 2, height // 2
    rx, ry = int(width * 0.148), int(height * 0.26)
    draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=(226, 190, 160))
    draw.ellipse([cx - rx - 6, cy - ry - 30, cx + rx + 6, cy - ry + 55], fill=(60, 42, 32))
    for offset in (-38, 38):
        draw.ellipse([cx + offset - 16, cy - 32, cx + offset + 16, cy - 10], fill=(250, 250, 250))
        draw.ellipse([cx + offset - 7, cy - 27, cx + offset + 7, cy - 13], fill=(35, 28, 24))
        draw.rectangle([cx + offset - 18, cy - 46, cx + offset + 18, cy - 40], fill=(60, 42, 32))
    draw.polygon([(cx, cy - 8), (cx - 11, cy + 26), (cx + 11, cy + 26)], fill=(203, 165, 138))
    draw.ellipse([cx - 34, cy + 45, cx + 34, cy + 72], fill=(150, 70, 70))
    return np.array(image)


def _synthetic_face_bgr(width: int = FRAME_W, height: int = FRAME_H) -> np.ndarray:
    """The same image in BGR, which is what OpenCV hands the sampler."""
    return np.ascontiguousarray(_synthetic_face_rgb(width, height)[:, :, ::-1])


@pytest.fixture
def mediapipe_detector():
    """The real backend, resolved exactly as a render resolves it."""
    detector, label = resolve_detector("mediapipe")
    assert label == "mediapipe", (
        f"the mediapipe backend did not resolve (label={label!r}). mediapipe is a hard "
        "dependency and the model is vendored, so this is a real failure, not a skip."
    )
    try:
        yield detector
    finally:
        close = getattr(detector, "close", None)
        if close is not None:
            close()


# --------------------------------------------------------------------------- #
# 1b.6 — the installed library exposes the API the backend calls               #
# --------------------------------------------------------------------------- #
def test_the_installed_mediapipe_exposes_the_api_the_backend_calls():
    """Requirements 13.1, 13.3 — a drift pin on the dependency's API surface.

    Fails loudly when a resolver upgrade moves the API out from under the backend, which is
    the whole reason the pin was narrowed to ``>=0.10.30,<0.11.0``.
    """
    from mediapipe.tasks.python import BaseOptions
    from mediapipe.tasks.python.vision import FaceDetector, FaceDetectorOptions

    assert callable(FaceDetector.create_from_options)
    assert callable(BaseOptions)
    # The two options the backend actually passes.
    annotations = getattr(FaceDetectorOptions, "__annotations__", {})
    assert "base_options" in annotations
    assert "min_detection_confidence" in annotations


def test_the_removed_solutions_namespace_is_not_relied_upon():
    """Requirement 2.3b / 13.2 — ``mediapipe.solutions`` was removed in 0.10.x.

    Pinned from two directions. First, the installed library genuinely does not have it, so any
    code reaching for ``mp.solutions.face_detection`` would raise. Second, the reframe module's
    own source must not mention it — a future contributor following a tutorial would otherwise
    reintroduce a namespace that does not exist, and ``model_selection`` with it.
    """
    import mediapipe as mp

    assert not hasattr(mp, "solutions"), (
        "mediapipe.solutions is back; the backend and its pin comment both assume it is gone"
    )
    assert set(dir(mp)) >= {"Image", "ImageFormat", "tasks"}

    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "worker" / "effects" / "reframe.py").read_text()
    offending = [
        line
        for line in source.splitlines()
        if "mp.solutions" in line or "mediapipe.solutions" in line.replace("``", "")
    ]
    # The docstrings deliberately *name* the removed namespace to warn about it, so only
    # executable references count: a line that is not a comment and not inside prose.
    executable = [line for line in offending if not line.lstrip().startswith("#") and "=" in line]
    assert executable == [], f"reframe.py appears to call the removed namespace: {executable}"


def test_the_vendored_model_is_where_the_backend_looks_for_it():
    """Requirement 12.9 — resolved from the setting, defaulting to the in-repo path."""
    path = resolve_model("mediapipe")
    assert path is not None, "the vendored model did not resolve"
    assert path.is_file()
    assert path.name == MODEL_MANIFEST[0].filename
    assert path.stat().st_size == MODEL_MANIFEST[0].size_bytes


# --------------------------------------------------------------------------- #
# 3.1 — detections are in pixels and in bounds                                 #
# --------------------------------------------------------------------------- #
def test_real_detections_are_absolute_pixels_and_in_bounds(mediapipe_detector):
    """Requirements 2.4, 2.5, 2.6, 11.1, 11.2 — the load-bearing test of this feature.

    ``w > 1 or h > 1`` is the assertion that fails if normalised coordinates leak through: a
    normalised box scaled by nothing is at most one pixel across.
    """
    frame = _synthetic_face_bgr()
    detections = mediapipe_detector(frame)

    assert detections, "the real backend found no face in the synthetic image"
    assert all(isinstance(d, Detection) for d in detections)

    # Pixels, not normalised.
    assert any(d.w > 1 and d.h > 1 for d in detections), (
        f"every returned box is at most one pixel across, which is what normalised "
        f"coordinates look like after int(): {detections}"
    )
    # And in bounds, for every box.
    for d in detections:
        assert d.w > 0 and d.h > 0, d
        assert 0 <= d.x and d.x + d.w <= FRAME_W, d
        assert 0 <= d.y and d.y + d.h <= FRAME_H, d

    # A face occupying a good part of a 640x480 frame is tens of pixels across at least; this
    # would fail on a box scaled by 1/640 even if the one-pixel check somehow passed.
    biggest = max(detections, key=lambda d: d.w * d.h)
    assert biggest.w >= 20 and biggest.h >= 20, biggest


def test_real_detections_carry_a_confidence_score(mediapipe_detector):
    """Requirement 7.1 needs scores to exist on this backend; Haar has none."""
    detections = mediapipe_detector(_synthetic_face_bgr())
    assert detections
    assert all(d.score is not None for d in detections), detections
    assert all(0.0 <= d.score <= 1.0 for d in detections), detections


def test_the_detected_box_actually_covers_the_drawn_face(mediapipe_detector):
    """Guards against a box that is in bounds, in pixels, and in the wrong place.

    The face is drawn at the centre, so a detection whose centre is near the frame origin is
    the exact symptom of a normalised box surviving as pixels — in bounds and plausible, but
    framing the top-left corner.
    """
    detections = mediapipe_detector(_synthetic_face_bgr())
    biggest = max(detections, key=lambda d: d.w * d.h)
    cx, cy = biggest.x + biggest.w / 2, biggest.y + biggest.h / 2
    assert abs(cx - FRAME_W / 2) < FRAME_W * 0.2, f"centre x {cx} is not near the drawn face"
    assert abs(cy - FRAME_H / 2) < FRAME_H * 0.2, f"centre y {cy} is not near the drawn face"


@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
def test_boxes_stay_in_bounds_at_several_frame_sizes(size):
    """A conversion that used a hard-coded or mismatched frame size would pass at one size."""
    width, height = size
    detector, label = resolve_detector("mediapipe")
    assert label == "mediapipe"
    try:
        detections = detector(_synthetic_face_bgr(width, height))
        for d in detections:
            assert 0 <= d.x and d.x + d.w <= width, (size, d)
            assert 0 <= d.y and d.y + d.h <= height, (size, d)
    finally:
        detector.close()


# --------------------------------------------------------------------------- #
# 3.2 — independent cross-check of the conversion                              #
# --------------------------------------------------------------------------- #
def test_the_conversion_matches_an_independent_calculation():
    """Requirements 11.1, 11.2 — cross-checked through a path sharing no code.

    The expected box is computed **here**, from MediaPipe's own output, using arithmetic
    written out in the test rather than by calling ``relative_box_to_pixels``. A cross-check
    that reuses the function under test verifies only that the function agrees with itself,
    which is the rule the working agreement states and the reason this test is not simply a
    second call to the converter.
    """
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    rgb = _synthetic_face_rgb()
    model = resolve_model("mediapipe")
    assert model is not None

    options = vision.FaceDetectorOptions(
        base_options=BaseOptions(model_asset_path=str(model)),
        min_detection_confidence=0.3,
    )
    with vision.FaceDetector.create_from_options(options) as raw_detector:
        result = raw_detector.detect(
            mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        )

    assert result.detections, "the raw library found no face, so there is nothing to cross-check"
    box = result.detections[0].bounding_box

    # Independent expectation, written out longhand. The measured fact this encodes is that the
    # tasks API reports ABSOLUTE PIXELS (see the spec's design document, task 3.0): a box whose
    # coordinates already exceed 1 needs clipping to the frame, not scaling by it.
    left = max(0, min(FRAME_W, int(box.origin_x)))
    top = max(0, min(FRAME_H, int(box.origin_y)))
    right = max(0, min(FRAME_W, int(box.origin_x) + int(box.width)))
    bottom = max(0, min(FRAME_H, int(box.origin_y) + int(box.height)))
    expected = (left, top, right - left, bottom - top)

    actual = relative_box_to_pixels(
        box.origin_x, box.origin_y, box.width, box.height, width=FRAME_W, height=FRAME_H
    )
    assert actual == expected, (
        f"conversion disagrees with an independent calculation: {actual} != {expected} "
        f"for raw box origin=({box.origin_x},{box.origin_y}) size=({box.width},{box.height})"
    )


def test_the_raw_library_reports_pixels_not_a_normalised_box():
    """Pins the measured finding itself (task 3.0), so a library change is visible here.

    If a future MediaPipe returns a normalised box from the tasks API, this fails and points
    directly at the conversion — rather than the change surfacing as every clip cropped to the
    left edge.
    """
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    model = resolve_model("mediapipe")
    options = vision.FaceDetectorOptions(
        base_options=BaseOptions(model_asset_path=str(model)),
        min_detection_confidence=0.3,
    )
    with vision.FaceDetector.create_from_options(options) as detector:
        result = detector.detect(
            mp.Image(
                image_format=mp.ImageFormat.SRGB,
                data=np.ascontiguousarray(_synthetic_face_rgb()),
            )
        )
    box = result.detections[0].bounding_box
    assert box.width > 1 and box.height > 1, f"tasks API returned a normalised box: {box}"
    assert not hasattr(result.detections[0], "relative_bounding_box"), (
        "the tasks-API detection grew a relative_bounding_box; re-measure task 3.0"
    )
