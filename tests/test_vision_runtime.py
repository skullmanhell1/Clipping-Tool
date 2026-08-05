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
