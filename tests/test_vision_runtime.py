"""The vision dependencies are really usable, not merely declared.

:func:`worker.effects.reframe.detect_faces` imports ``cv2`` lazily and returns ``[]`` on
*any* failure — a missing module, an unopenable video, an unloadable cascade. The caller
turns ``[]`` into ``ReframeUnavailable`` and the pipeline falls back to a static
``crop_blur``. That is the right runtime behaviour: a render should not die because a
vision library is absent. It is also completely silent, so nothing in a green suite
distinguishes "reframe works" from "reframe has degraded on every clip ever produced".

This is not hypothetical. With ``libGL.so.1`` absent, ``import cv2`` raises and the suite
still reported **1037 passed, 0 skipped** — opencv and mediapipe were installed, and every
test that appears to cover face tracking was exercising the degraded branch instead.
``.github/workflows/ci.yml`` installs ``libgl1``/``libglib2.0-0`` for exactly this reason,
with a comment explaining that CI "was installing opencv and getting no coverage from it",
but nothing detected the condition returning — so the fix held only for as long as nobody
edited that step.

The same shape as the ``ffmpeg -filters`` probe and the font chain before it: a capability
whose absence nothing measured.

**These tests skip rather than fail, on purpose.** That is the mechanism ``requires_ffmpeg``
already uses: a bare developer checkout without the GL libraries stays usable, while CI
fails the run because it rejects *any* skip. A skip is not a pass, so a skip here is a CI
failure with a precise reason attached — which is the outcome wanted, and the reason this
does not simply `assert` the import and break every lightweight checkout.

Each call passes ``exc_type=ImportError`` explicitly, which is load-bearing rather than
tidy. By default :func:`pytest.importorskip` only treats a *missing* module as skippable;
a module that exists but raises ``ImportError`` while loading — precisely the libGL case —
makes it emit a ``PytestDeprecationWarning``, and ``filterwarnings = ["error", ...]`` in
``pyproject.toml`` promotes that to a failure. Without the argument these tests fail
instead of skipping, which breaks a bare checkout *and* makes the paragraph above untrue.
"""

from __future__ import annotations

import pytest

#: Why a skip here is a CI failure rather than a shrug. Kept in one place so all three
#: reasons read identically in the ``-ra`` summary that ``pyproject.toml`` enables.
_REASON = (
    "opencv/mediapipe not importable (usually missing libGL.so.1); every face-tracking "
    "path silently degrades to a static crop. CI installs libgl1 and fails on any skip."
)


def test_opencv_is_importable():
    """``cv2`` imports, so face detection can run at all.

    Installing the wheel is not the same as being able to import it: the manylinux build
    links against ``libGL``, which is not present in a slim base image.
    """
    cv2 = pytest.importorskip("cv2", reason=_REASON, exc_type=ImportError)
    assert cv2.__version__, "cv2 imported but reports no version"


def test_mediapipe_is_importable():
    """``mediapipe`` imports.

    Nothing in the shipped reframe path uses it yet — the current detector is the Haar
    cascade below, and **V2** in ``docs/IMPROVEMENT_PLAN.md`` is the item that replaces it.
    It is a declared dependency that CI installs, so it is asserted here rather than left
    to fail for the first time inside whoever implements V2.
    """
    mediapipe = pytest.importorskip("mediapipe", reason=_REASON, exc_type=ImportError)
    assert mediapipe.__version__, "mediapipe imported but reports no version"


def test_the_default_face_cascade_actually_loads():
    """The detector `reframe` really builds, not a proxy for it.

    ``_default_haar_detector`` returns ``None`` when ``CascadeClassifier`` comes back
    empty, and ``detect_faces`` turns that into ``[]`` — indistinguishable from a video
    with no faces in it. So an importable ``cv2`` whose bundled cascade data is missing
    degrades exactly as silently as no ``cv2`` at all, and asserting the import alone
    would not catch it.
    """
    cv2 = pytest.importorskip("cv2", reason=_REASON, exc_type=ImportError)

    from worker.effects import reframe

    detector = reframe._default_haar_detector(cv2)
    assert detector is not None, (
        "haarcascade_frontalface_default.xml did not load from "
        f"{cv2.data.haarcascades!r}; face detection would return no faces on every "
        "frame and every clip would fall back to a static centre crop"
    )


def _face_frame(cv2, width: int = 1920, height: int = 1080):
    """A frame the real cascade actually detects, drawn rather than photographed.

    Enough of the light/dark structure the frontal cascade keys on — brow bars over dark
    eyes, a nose shadow, a mouth, a dark hairline — over a textured background so the
    detector has something to reject. Verified to detect straight-on and, usefully, to stop
    detecting past about 60 degrees of head turn, which is the known weakness of a frontal
    cascade rather than an artefact of the drawing.

    Drawn, not a bundled photograph, because the assertion below is an *equivalence* between
    two resolutions of the same image. That holds for any image the cascade fires on, and a
    drawing keeps the test self-contained instead of resting on another package shipping a
    sample file.
    """
    import numpy as np

    canvas = np.full((height, width, 3), 95, np.uint8)
    for i in range(0, width, 37):
        cv2.line(canvas, (i, 0), (i + 60, height), (108, 104, 100), 2)

    cx, cy, face_h = width // 2, height // 2, int(height * 0.45)
    face_w = int(face_h * 0.75)
    cv2.ellipse(canvas, (cx, cy), (face_w // 2, face_h // 2), 0, 0, 360, (170, 190, 210), -1)
    eye_dx, eye_dy = int(0.20 * face_w), -int(0.13 * face_h)
    eye_r = max(2, int(0.070 * face_h))
    for side in (-1, 1):
        ex = cx + side * eye_dx
        cv2.ellipse(
            canvas, (ex, cy + eye_dy), (eye_r, int(eye_r * 0.72)), 0, 0, 360, (40, 40, 45), -1
        )
        cv2.rectangle(
            canvas,
            (ex - eye_r - 2, cy + eye_dy - int(0.075 * face_h)),
            (ex + eye_r + 2, cy + eye_dy - int(0.045 * face_h)),
            (55, 50, 50),
            -1,
        )
    cv2.ellipse(
        canvas,
        (cx, cy + int(0.06 * face_h)),
        (max(2, int(0.05 * face_w)), int(0.10 * face_h)),
        0,
        0,
        360,
        (140, 158, 178),
        -1,
    )
    cv2.ellipse(
        canvas,
        (cx, cy + int(0.24 * face_h)),
        (int(0.17 * face_w), max(2, int(0.045 * face_h))),
        0,
        0,
        360,
        (70, 70, 95),
        -1,
    )
    cv2.ellipse(
        canvas,
        (cx, cy - int(0.42 * face_h)),
        (face_w // 2, int(0.16 * face_h)),
        0,
        0,
        360,
        (45, 40, 40),
        -1,
    )
    return cv2.GaussianBlur(canvas, (5, 5), 0), (cx, cy)


def test_detecting_on_a_downscaled_frame_resolves_the_same_face_centre():
    """``reframe_detect_width`` must change the cost and not the answer.

    Detection is scaled down to buy back the cost of sampling more often, and boxes are
    scaled back to native coordinates afterwards. The thing that could silently go wrong is
    the scaling arithmetic: an inverse applied to the wrong quantity, or a minimum face size
    left in working pixels, moves every crop without failing anything — the render still
    succeeds and the clip record still says ``reframe``.

    So this asserts the **resolved centre**, through the real cascade, at native resolution
    and at the shipped working width, and requires them to agree to within a few pixels. The
    tolerance is in native pixels and is far below what is visible: the 9:16 crop of a 1080p
    frame is 608 px wide, so 12 px is 2% of the window.
    """
    cv2 = pytest.importorskip("cv2", reason=_REASON, exc_type=ImportError)
    pytest.importorskip("numpy", reason=_REASON, exc_type=ImportError)

    from worker.effects import reframe

    frame, (true_cx, true_cy) = _face_frame(cv2)

    resolved = {}
    for width in (0, 640):
        detector = reframe._default_haar_detector(cv2, detect_width=width)
        assert detector is not None
        boxes = detector(frame)
        assert boxes, f"the cascade found no face at detect_width={width}"
        x, y, w, h = max(boxes, key=lambda b: b[2] * b[3])
        resolved[width] = (x + w / 2.0, y + h / 2.0)
        # Sanity: the box is in NATIVE coordinates. A missing inverse scale would put the
        # centre at a third of the frame and still look like a plausible face position.
        assert abs(resolved[width][0] - true_cx) < 0.10 * frame.shape[1]
        assert abs(resolved[width][1] - true_cy) < 0.10 * frame.shape[0]

    dx = abs(resolved[0][0] - resolved[640][0])
    dy = abs(resolved[0][1] - resolved[640][1])
    assert dx < 12 and dy < 12, (
        f"detecting at 640 px moved the resolved centre by ({dx:.1f}, {dy:.1f}) native px "
        f"against detecting at {frame.shape[1]} px; the box scale-back is wrong"
    )


def test_the_minimum_face_size_is_carried_into_working_coordinates():
    """Downscaling must not quietly make detection stricter.

    The cascade's minimum face size is in pixels, so leaving it at its native value while
    shrinking the frame would reject every face between the old minimum and that minimum
    times the scale factor — detection would get *pickier* as a side effect of a change made
    for speed, and the only symptom would be more clips falling back to a static crop.

    Asserted through the real ``detectMultiScale`` by giving it a face just above the native
    minimum and requiring it to be found at both widths.
    """
    cv2 = pytest.importorskip("cv2", reason=_REASON, exc_type=ImportError)
    pytest.importorskip("numpy", reason=_REASON, exc_type=ImportError)

    from worker.effects import reframe

    # A frame whose face is ~150 px tall: comfortably above the 60 px native minimum, and
    # small enough that a minimum left in native pixels (60 px at a 3x downscale means
    # 180 px native) would reject it.
    frame, _ = _face_frame(cv2, width=1920, height=333)

    for width in (0, 640):
        detector = reframe._default_haar_detector(cv2, detect_width=width)
        assert detector is not None
        assert detector(frame), (
            f"a ~150 px face was not found at detect_width={width}; the minimum face size "
            "is not being scaled into working coordinates"
        )
