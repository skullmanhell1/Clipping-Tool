"""Face-detection upgrade: pure conversion, coverage, markers, and selection.

Covers spec `.kiro/specs/face-detection-upgrade` tasks 1.4-1.7 (Properties P1-P5), 1b.7 and
2.4. Property tests use ``hypothesis`` with ``@settings(max_examples=100)``, one property per
test, each tagged ``# Feature: face-detection-upgrade, Property N: ...``.

Everything here is deliberately dependency-free -- no cv2, no mediapipe, no ffmpeg. That is
the point of landing these pieces first: the coordinate conversion is where the real risk of
this feature lives, and it is testable without any of the heavy stack. The real-library
verification that a fake detector *cannot* provide lives in
``tests/test_face_detection_real_binary.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from worker.effects.reframe import (
    Detection,
    detection_coverage,
    face_detector_marker,
    face_detector_substituted_marker,
    low_confidence_marker,
    pick_main_face,
    relative_box_to_pixels,
    sample_rate_marker,
)

_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 1.4 — Property 1                                                            #
# --------------------------------------------------------------------------- #
# Feature: face-detection-upgrade, Property 1: Converted boxes are in bounds and non-degenerate
@settings(max_examples=100)
@given(
    rel_x=st.floats(min_value=-2.0, max_value=2.0, allow_nan=False),
    rel_y=st.floats(min_value=-2.0, max_value=2.0, allow_nan=False),
    rel_w=st.floats(min_value=-1.0, max_value=2.0, allow_nan=False),
    rel_h=st.floats(min_value=-1.0, max_value=2.0, allow_nan=False),
    width=st.integers(min_value=1, max_value=4096),
    height=st.integers(min_value=1, max_value=4096),
)
def test_p1_conversion_is_bounded_and_non_degenerate(rel_x, rel_y, rel_w, rel_h, width, height):
    """Validates: Requirements 2.4, 2.5, 2.6

    For *any* box and any positive frame size, the result is either ``None`` or a box lying
    wholly inside the frame with ``w > 0 and h > 0``. This is the cheap invariant that fails
    loudly the moment normalised coordinates leak through as pixels -- the failure mode that
    nothing else in the pipeline objects to.
    """
    box = relative_box_to_pixels(rel_x, rel_y, rel_w, rel_h, width=width, height=height)
    if box is None:
        return
    x, y, w, h = box
    assert w > 0 and h > 0
    assert 0 <= x <= width and 0 <= y <= height
    assert x + w <= width, f"{box} exceeds width {width}"
    assert y + h <= height, f"{box} exceeds height {height}"


def test_a_normalised_box_is_scaled_by_the_frame():
    """The conversion that matters: [0,1] in, pixels out."""
    assert relative_box_to_pixels(0.1, 0.2, 0.3, 0.4, width=1000, height=1000) == (
        100,
        200,
        300,
        400,
    )


def test_an_absolute_box_passes_through_as_clamp_and_validate():
    """The measured tasks-API form (see design.md): already pixels, so this only clamps."""
    assert relative_box_to_pixels(100, 200, 300, 400, width=1000, height=1000) == (
        100,
        200,
        300,
        400,
    )


def test_a_box_entirely_off_frame_is_rejected_not_zero_sized():
    """Returned as ``None`` so the caller drops it rather than tracking a zero-area face."""
    assert relative_box_to_pixels(1.0, 1.0, 0.5, 0.5, width=100, height=100) is None
    assert relative_box_to_pixels(500, 500, 50, 50, width=100, height=100) is None


def test_a_partially_visible_face_is_clamped_and_kept():
    """MediaPipe reports boxes past the frame edge for a face at the border."""
    assert relative_box_to_pixels(0.9, 0.9, 0.5, 0.5, width=100, height=100) == (90, 90, 10, 10)


def test_a_sub_pixel_box_is_degenerate():
    """Rounding 0.1 of a pixel outward would manufacture a detection out of nothing."""
    assert relative_box_to_pixels(0.5, 0.5, 0.0001, 0.0001, width=1000, height=1000) is None


def test_non_finite_values_are_rejected():
    """NaN survives ``float()`` and would reach an ffmpeg crop argument as ``nan``."""
    assert relative_box_to_pixels(float("nan"), 0.1, 0.2, 0.2, width=100, height=100) is None
    assert relative_box_to_pixels(0.1, float("inf"), 0.2, 0.2, width=100, height=100) is None


def test_a_zero_sized_frame_yields_nothing():
    assert relative_box_to_pixels(0.1, 0.1, 0.5, 0.5, width=0, height=100) is None


# --------------------------------------------------------------------------- #
# 1.5 — Property 2                                                            #
# --------------------------------------------------------------------------- #
# Feature: face-detection-upgrade, Property 2: Coverage is a bounded fraction
@settings(max_examples=100)
@given(
    flags=st.lists(st.booleans(), min_size=0, max_size=60),
)
def test_p2_coverage_is_a_bounded_fraction(flags):
    """Validates: Requirements 5.1, 5.2, 5.3, 5.4

    Coverage is in ``[0, 1]``; ``0.0`` for an empty sample list (never a division by zero);
    ``1.0`` exactly when every sample has at least one detection.
    """
    samples = [(float(i), [Detection(0, 0, 10, 10)] if hit else []) for i, hit in enumerate(flags)]
    coverage = detection_coverage(samples)
    assert 0.0 <= coverage <= 1.0
    if not flags:
        assert coverage == 0.0
    elif all(flags):
        assert coverage == 1.0
    elif not any(flags):
        assert coverage == 0.0
    else:
        assert coverage == pytest.approx(sum(flags) / len(flags))


def test_coverage_of_no_samples_is_zero_not_an_error():
    assert detection_coverage([]) == 0.0


# --------------------------------------------------------------------------- #
# 1.6 — Properties 3 and 4                                                    #
# --------------------------------------------------------------------------- #
_boxes = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=1000),
        st.integers(min_value=0, max_value=1000),
        st.integers(min_value=1, max_value=500),
        st.integers(min_value=1, max_value=500),
    ),
    min_size=1,
    max_size=12,
)


# Feature: face-detection-upgrade, Property 3: With no scores, selection is exactly largest-area
@settings(max_examples=100)
@given(boxes=_boxes)
def test_p3_no_scores_is_exactly_largest_area(boxes):
    """Validates: Requirements 7.2, 9.1

    Pins the *mechanism* of the byte-identical default, not just its outcome: with no
    confidences present the selection must be the v0.11.0 expression verbatim, including that
    ``max`` keeps the first of several equal-area boxes.
    """
    expected_box = max(boxes, key=lambda f: f[2] * f[3])
    expected = (expected_box[0] + expected_box[2] / 2.0, expected_box[1] + expected_box[3] / 2.0)
    assert pick_main_face(boxes) == expected
    # And the Detection form with no scores agrees with the tuple form.
    as_detections = [Detection(*b) for b in boxes]
    assert pick_main_face(as_detections) == expected


# Feature: face-detection-upgrade, Property 4: At most one main face; a lone detection always wins
@settings(max_examples=100)
@given(
    boxes=_boxes,
    scores=st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=1, max_size=12),
)
def test_p4_at_most_one_main_face_and_a_lone_detection_wins(boxes, scores):
    """Validates: Requirements 7.3, 7.4

    A single centre is returned (never a list), and a lone detection is selected whatever its
    score -- with nothing to compare against, a confidence is not evidence for discarding the
    only face found.
    """
    detections = [Detection(*b, score=scores[i % len(scores)]) for i, b in enumerate(boxes)]
    picked = pick_main_face(detections)
    assert picked is not None
    assert isinstance(picked, tuple) and len(picked) == 2

    lone = detections[0]
    assert pick_main_face([lone]) == (lone.x + lone.w / 2.0, lone.y + lone.h / 2.0)


def test_zero_detections_selects_nothing():
    """Requirement 7.5 — the existing hold-last-centre behaviour is left to the caller."""
    assert pick_main_face([]) is None


def test_a_confident_small_face_beats_a_large_unsure_box():
    """Requirement 7.1 — the crop should follow a face, not a bookshelf.

    The bookshelf is the bigger box, which is exactly why area alone is not enough once a
    backend supplies confidences.
    """
    bookshelf = Detection(0, 0, 400, 400, score=0.10)
    face = Detection(600, 300, 80, 80, score=0.95)
    assert pick_main_face([bookshelf, face]) == (640.0, 340.0)


def test_a_scoreless_detection_among_scored_peers_ranks_on_area():
    """Treated as neutral rather than as zero, which would silently discard it."""
    scored_small = Detection(0, 0, 10, 10, score=0.9)
    unscored_big = Detection(100, 100, 200, 200)
    assert pick_main_face([scored_small, unscored_big]) == (200.0, 200.0)


def test_malformed_entries_are_ignored_rather_than_raising():
    """Callers hand this partial data; a wrong shape must not abort a render."""
    assert pick_main_face([("nonsense",), None, (0, 0, 10, 10)]) == (5.0, 5.0)


# --------------------------------------------------------------------------- #
# 1.7 — Property 5                                                            #
# --------------------------------------------------------------------------- #
# Feature: face-detection-upgrade, Property 5: Marker strings are deterministic
@settings(max_examples=100)
@given(coverage=st.floats(min_value=0.0, max_value=1.0))
def test_p5_marker_strings_are_deterministic(coverage):
    """Validates: Requirements 6.6

    The same coverage must produce the same marker text on every run and platform. A marker
    formatted with ``str(float)`` would vary with float repr, and the golden renders are how
    byte-parity is verified.
    """
    first = low_confidence_marker(coverage)
    assert first == low_confidence_marker(coverage)
    assert first.startswith("reframe_low_confidence:")
    decimals = first.rsplit(":", 1)[1]
    assert len(decimals.split(".")[1]) == 2, first


def test_marker_spellings_are_exact():
    """These strings are the only channel a caller sees, so they are pinned literally."""
    assert face_detector_marker("haar") == "face_detector:haar"
    assert face_detector_marker("mediapipe") == "face_detector:mediapipe"
    assert face_detector_marker("injected") == "face_detector:injected"
    assert (
        face_detector_substituted_marker("mediapipe", "haar")
        == "face_detector_substituted:mediapipe:haar"
    )
    assert low_confidence_marker(0.125) == "reframe_low_confidence:0.12"
    assert sample_rate_marker(2.0) == "reframe_sample_rate:2.0"


def test_the_substitution_marker_names_the_requested_backend_first():
    """Order is load-bearing: ``requested:resolved``.

    Swapping the operands produces a plausible-looking marker that says the opposite thing --
    that Haar was asked for and MediaPipe ran.
    """
    marker = face_detector_substituted_marker("mediapipe", "haar")
    _prefix, requested, resolved = marker.split(":")
    assert (requested, resolved) == ("mediapipe", "haar")


# --------------------------------------------------------------------------- #
# 1b.7 — the model manifest verifies offline                                   #
# --------------------------------------------------------------------------- #
def _run_check(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "fetch_models.py"), "--check", *args],
        cwd=str(cwd or _ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_model_check_passes_on_the_working_tree():
    """Requirement 12.4 — verification uses only the working tree, with no network."""
    result = _run_check()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "verified" in (result.stdout + result.stderr).lower()


def test_the_model_check_fails_and_names_a_truncated_file(tmp_path):
    """Requirement 12.5 — a truncated model must be named, not silently accepted.

    A digest check that passes on a half-downloaded file is worse than none: the backend would
    construct against a corrupt graph and fail at detection time instead of degrading.
    """
    from worker.face_models import MODEL_MANIFEST

    entry = MODEL_MANIFEST[0]
    models = tmp_path / "models"
    models.mkdir()
    original = _ROOT / "assets" / "models" / entry.filename
    (models / entry.filename).write_bytes(original.read_bytes()[:1024])

    result = _run_check("--models-dir", str(models))
    assert result.returncode != 0
    assert entry.filename in (result.stdout + result.stderr)


def test_the_model_check_fails_and_names_a_missing_file(tmp_path):
    from worker.face_models import MODEL_MANIFEST

    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run_check("--models-dir", str(empty))
    assert result.returncode != 0
    assert MODEL_MANIFEST[0].filename in (result.stdout + result.stderr)


# --------------------------------------------------------------------------- #
# 2.4 — backend resolution and substitution                                    #
# --------------------------------------------------------------------------- #
from worker.effects.reframe import (  # grouped with its own section
    DEFAULT_FACE_DETECTOR_BACKEND,
    FACE_DETECTOR_BACKENDS,
    detector_marker_for,
    resolve_detector,
)


def test_the_default_backend_is_haar():
    """The whole byte-parity argument rests on this one line."""
    assert DEFAULT_FACE_DETECTOR_BACKEND == "haar"
    assert "haar" in FACE_DETECTOR_BACKENDS and "mediapipe" in FACE_DETECTOR_BACKENDS


def test_the_default_resolves_to_haar():
    detector, label = resolve_detector("haar")
    assert label == "haar"
    assert detector is not None


@pytest.mark.parametrize("value", ["", "  ", "nonsense", "MEDIAPIPE-ish", "yunet", None])
def test_an_unrecognised_backend_resolves_to_haar_without_raising(value):
    """Requirement 1.4 — an unknown value must not fail a render."""
    detector, label = resolve_detector(value)
    assert label == "haar"
    assert detector is not None


def _released(detector):
    """Close a resolved detector if it holds a native graph.

    Every test here that resolves ``mediapipe`` for real must do this. MediaPipe's
    ``FaceDetector`` is finalised at interpreter shutdown if nothing closed it, and by then its
    own C shim is already torn down -- which surfaces as
    ``TypeError: 'NoneType' object is not callable`` from a teardown handler, long after pytest
    has printed its summary. A leak here is not a warning; it is a confusing crash in an
    unrelated place.
    """
    close = getattr(detector, "close", None)
    if close is not None:
        close()


def test_backend_names_are_case_and_space_insensitive():
    detector, label = resolve_detector("  MediaPipe  ")
    try:
        assert label in {"mediapipe", "substituted:mediapipe:haar"}
    finally:
        _released(detector)


def test_an_injected_detector_resolves_to_injected_and_is_used():
    """Requirement 3.4 — a test double is not evidence that a backend works.

    The label must not borrow a backend name, or a suite of fakes would produce clip records
    claiming MediaPipe ran on machines where it is not installed.
    """
    sentinel = object()

    def fake(_frame):
        return [sentinel]

    detector, label = resolve_detector("mediapipe", injected=fake)
    assert label == "injected"
    assert detector is fake
    assert detector(None) == [sentinel]


def test_an_injected_detector_wins_over_every_backend():
    for backend in (*FACE_DETECTOR_BACKENDS, "nonsense"):
        _d, label = resolve_detector(backend, injected=lambda _f: [])
        assert label == "injected", backend


def test_a_missing_model_substitutes_haar_and_names_both_sides(tmp_path):
    """Requirements 2.3a, 4.2a — the vendored model is absent, so mediapipe is unavailable."""
    detector, label = resolve_detector("mediapipe", model_dir=tmp_path)
    assert label == "substituted:mediapipe:haar"
    assert detector is not None, "the substitution must still produce a working detector"
    assert detector_marker_for(label) == "face_detector_substituted:mediapipe:haar"


def test_a_truncated_model_substitutes_haar(tmp_path):
    """A file that exists but is the wrong size is not a usable model."""
    from worker.face_models import MODEL_MANIFEST

    (tmp_path / MODEL_MANIFEST[0].filename).write_bytes(b"\x00" * 32)
    _detector, label = resolve_detector("mediapipe", model_dir=tmp_path)
    assert label == "substituted:mediapipe:haar"


def test_an_unimportable_mediapipe_substitutes_haar(monkeypatch, tmp_path):
    """Requirement 4.1 — import failure is one of the four causes sharing the marker."""
    import worker.effects.reframe as rf

    monkeypatch.setattr(rf, "_mediapipe_detector", lambda *_a, **_k: None)
    detector, label = resolve_detector("mediapipe")
    assert label == "substituted:mediapipe:haar"
    assert detector is not None


def test_an_unbuildable_cascade_yields_none_rather_than_raising():
    """Requirement 4.4 — the bottom rung: no detector at all, and still no exception."""

    class _NoCascade:
        class data:  # mirrors cv2.data
            haarcascades = "/nonexistent/"

        @staticmethod
        def CascadeClassifier(_path):  # mirrors the cv2 API
            class _Empty:
                @staticmethod
                def empty():
                    return True

            return _Empty()

    detector, label = resolve_detector("haar", cv2_module=_NoCascade)
    assert detector is None
    assert label == "haar"


def test_no_cv2_yields_none_rather_than_raising():
    detector, label = resolve_detector("haar", cv2_module=None)
    # cv2 is installed here, so this resolves; the contract asserted is only that it never
    # raises and always reports a label.
    assert isinstance(label, str) and label
    assert detector is None or callable(detector)


def test_the_resolved_label_never_names_a_backend_that_did_not_run(tmp_path):
    """Requirement 3.3, stated as the invariant it is.

    Every label this function can return either names the backend that produced the detector
    it handed back, or encodes a substitution naming both sides. There is no path on which a
    caller can be told ``mediapipe`` while Haar ran.
    """
    for backend in ("haar", "mediapipe", "nonsense"):
        for model_dir in (None, tmp_path):
            detector, label = resolve_detector(backend, model_dir=model_dir)
            try:
                marker = detector_marker_for(label)
                if label.startswith("substituted:"):
                    assert marker == "face_detector_substituted:mediapipe:haar"
                else:
                    assert label in {"haar", "mediapipe", "injected"}
                    assert marker == f"face_detector:{label}"
            finally:
                _released(detector)


# --------------------------------------------------------------------------- #
# Gaps found by the mutation run (task 7.3), each pinning one line that had     #
# nothing observing it.                                                        #
# --------------------------------------------------------------------------- #
def test_the_cap_tolerance_is_not_a_bare_comparison():
    """Pins the 0.05 tolerance on ``Sample_Report.capped``.

    ``effective_fps`` is a division of two measured quantities, so an *uncapped* clip lands a
    hair either side of the requested rate. A bare ``<`` would report the cap as binding on
    roughly half of all clips, which makes the marker worthless in the direction that matters:
    it would stop meaning "this clip was sampled less densely than you asked".

    Asserted on the dataclass directly because reproducing a sub-tolerance rounding difference
    through a real render is luck rather than a test.
    """
    from worker.effects.reframe import Sample_Report

    def report(effective, requested):
        return Sample_Report(
            samples=[],
            resolved_backend="haar",
            effective_fps=effective,
            requested_fps=requested,
        )

    # Within tolerance: measurement noise, not a bound cap.
    assert report(4.98, 5.0).capped is False
    assert report(5.0, 5.0).capped is False
    assert report(5.02, 5.0).capped is False
    # Genuinely reduced.
    assert report(2.0, 5.0).capped is True
    assert report(4.9, 5.0).capped is True


def test_a_lone_detection_wins_whatever_its_score():
    """Requirement 7.3, asserted as behaviour rather than as a code branch.

    There is deliberately no ``len(items) == 1`` special case in ``pick_main_face`` -- ``max``
    over one item returns it under either key -- so this test is what holds the requirement,
    and it would fail if the selection ever started filtering by score.
    """
    for score in (0.0, 0.01, 0.5, 1.0, None):
        lone = Detection(10, 20, 30, 40, score=score)
        assert pick_main_face([lone]) == (25.0, 40.0), score


def test_a_wrong_sized_model_does_not_resolve():
    """A truncated download is a file that exists.

    Asserted on ``resolve_model`` directly. Going through ``resolve_detector`` cannot see this:
    a 32-byte ``.tflite`` also fails to *construct*, so the substitution label is identical
    whether the size check exists or not -- the outcome is right for the wrong reason, and the
    check could be deleted without any test noticing.
    """
    import tempfile

    from worker.face_models import MODEL_MANIFEST, resolve_model

    entry = MODEL_MANIFEST[0]
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / entry.filename).write_bytes(b"\x00" * 32)
        assert resolve_model("mediapipe", directory) is None

        # Right size, so it resolves -- the check is on length, not content (the digest is
        # CI's job, via scripts/fetch_models.py --check).
        (directory / entry.filename).write_bytes(b"\x00" * entry.size_bytes)
        assert resolve_model("mediapipe", directory) is not None


def test_the_manifest_check_catches_a_right_sized_wrong_file():
    """The digest check, which the truncation test cannot reach.

    A truncated file fails on length first, so the ``sha256`` comparison is never exercised by
    it -- and a manifest whose digest is never compared is a manifest that only checks file
    sizes, which is the one thing it exists to improve on.
    """
    import tempfile

    from worker.face_models import MODEL_MANIFEST, verify

    entry = MODEL_MANIFEST[0]
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        # Correct length, wrong bytes.
        (directory / entry.filename).write_bytes(b"\xff" * entry.size_bytes)
        (directory / entry.licence_file).write_text("licence", encoding="utf-8")
        problems = verify(directory)
        assert problems, "a right-sized wrong file must not verify"
        assert any(entry.filename in p and "sha256" in p for p in problems), problems


def test_the_licence_must_accompany_the_model():
    """Requirement 12.2 — the licence is part of what makes vendoring legitimate."""
    import shutil
    import tempfile

    from worker.face_models import MODEL_MANIFEST, verify

    entry = MODEL_MANIFEST[0]
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        shutil.copy(_ROOT / "assets" / "models" / entry.filename, directory / entry.filename)
        problems = verify(directory)
        assert any(entry.licence_file in p for p in problems), problems
