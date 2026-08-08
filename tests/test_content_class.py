"""Screen-recording and graphics detection (V24).

The classifier is tested against real rendered fixtures (R10.3), and the assertions encode what the
measurements actually support rather than what would be convenient.

The important test is the **negative** one: nearly-still camera footage must not be classified as
screen content. Its measured entropy (0.232) sits inside the range flat UI occupies, so a
single-threshold classifier on entropy alone would letterbox a face — which is a far worse outcome
than missing a slide, because `UNKNOWN` costs nothing new while a false positive wastes half the
frame.

Two classes are knowingly missed and asserted as missed, so nobody later "fixes" a test by loosening
a threshold: flat-colour animation and screen recordings of moving video both fall through to
`UNKNOWN`.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from config import settings as app_settings
from worker import content_class as cc
from worker.content_class import Content
from worker.engines.capabilities import Capability_Status

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None, reason="no ffmpeg on PATH; content classification needs it"
)


def _render(path, source_args, vf=None, seconds=4):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *source_args]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, timeout=600)
    return path


def _flat_ui(path):
    """A static slide: flat background, crisp blocks. The case this exists to catch."""
    return _render(
        path,
        ["-f", "lavfi", "-i", "color=c=0xF2F2F2:s=1280x720:r=25:d=4"],
        vf=(
            "drawbox=x=40:y=40:w=1200:h=90:color=0x2B579A:t=fill,"
            "drawbox=x=80:y=200:w=900:h=14:color=0x333333:t=fill,"
            "drawbox=x=80:y=260:w=700:h=14:color=0x333333:t=fill,"
            "drawbox=x=80:y=320:w=1000:h=14:color=0x333333:t=fill"
        ),
    )


def _camera_moving(path):
    return _render(
        path,
        ["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=4"],
        vf="noise=alls=14:allf=t+u,gblur=sigma=0.8,hue=s=1.1",
    )


def _camera_nearly_still(path):
    """A blurred noise field: about as static as camera footage gets.

    The fixture that matters most. Its low entropy overlaps flat UI, so it is what proves the
    conjunction rule is doing work that entropy alone could not.
    """
    return _render(
        path,
        ["-f", "lavfi", "-i", "color=c=0x6B5B4A:s=1280x720:r=25:d=4"],
        vf="noise=alls=12:allf=t+u,gblur=sigma=1.0",
    )


def _prober(available=True):
    def prober(capability_id: str) -> Capability_Status:
        return Capability_Status(capability_id, available, "injected")

    return prober


# --- R10.3: the real classifier against real footage ----------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_static_slide_is_classified_as_screen(tmp_path):
    """The case V24 exists for: a 16:9 slide cropped to 9:16 is an unreadable middle third."""
    report = cc.classify(_flat_ui(tmp_path / "ui.mp4"), prober=_prober())
    assert report.content is Content.SCREEN, report
    assert report.is_synthetic is True
    assert report.marker == "content_class:screen"


@requires_ffmpeg
@pytest.mark.real_binary
def test_moving_camera_footage_is_not_classified_as_screen(tmp_path):
    """No false positive on ordinary footage."""
    report = cc.classify(_camera_moving(tmp_path / "cam.mp4"), prober=_prober())
    assert report.is_synthetic is False, report


@requires_ffmpeg
@pytest.mark.real_binary
def test_nearly_still_camera_footage_is_not_classified_as_screen(tmp_path):
    """**The test the conjunction rule exists for.**

    Measured entropy of this fixture is 0.232 — *inside* the range flat UI occupies (0.116–0.150).
    A classifier thresholding entropy alone would call it synthetic and letterbox a face.

    What separates it is temporal difference: sensor noise means camera footage cannot produce a zero
    YDIF even pointed at a blank wall, and this fixture measured 0.899 against 0.000 for the slides.
    """
    path = _camera_nearly_still(tmp_path / "still.mp4")
    ydif, entropy, frames = cc.measure(path)
    assert frames > 0

    # Precondition: the entropy really does overlap the synthetic range, or this test is not
    # exercising the rule it claims to.
    assert entropy < 0.35, f"fixture entropy {entropy:.3f} no longer overlaps the UI range"
    assert ydif > cc.MAX_SYNTHETIC_YDIF, f"YDIF {ydif:.3f} should exceed the synthetic bound"

    assert cc.classify_features(ydif, entropy, frames).is_synthetic is False


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_two_features_are_both_read(tmp_path):
    """A missing parser would silently zero one feature and make the conjunction trivially true."""
    ydif, entropy, frames = cc.measure(_camera_moving(tmp_path / "c.mp4"))
    assert frames > 0
    assert ydif > 0.0, "YDIF was not parsed"
    assert entropy > 0.0, "entropy was not parsed"


@requires_ffmpeg
@pytest.mark.real_binary
def test_classification_is_per_clip_not_per_source(tmp_path):
    """R6. A recording that alternates camera and screen must be judged where the clip is.

    Built as a genuine concatenation so the seek is doing real work: slide first, camera second.
    """
    ui = _flat_ui(tmp_path / "a.mp4")
    cam = _camera_moving(tmp_path / "b.mp4")
    listing = tmp_path / "list.txt"
    listing.write_text(f"file '{ui.name}'\nfile '{cam.name}'\n", encoding="utf-8")
    joined = tmp_path / "mixed.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c:v",
            "libx264",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(joined),
        ],
        check=True,
        timeout=600,
    )

    first = cc.classify(joined, start=0.5, prober=_prober())
    second = cc.classify(joined, start=5.5, prober=_prober())
    assert first.content is Content.SCREEN, first
    assert second.is_synthetic is False, second


# --- the threshold rule, without media ------------------------------------------------------


def test_both_conditions_are_required():
    """The conjunction, stated directly. Either alone misclassifies real footage."""
    assert cc.classify_features(0.0, 0.10, 75).content is Content.SCREEN
    # Still but not flat: animation, a screen recording of video.
    assert cc.classify_features(0.0, 0.50, 75).content is Content.UNKNOWN
    # Flat but not still: nearly-still camera footage.
    assert cc.classify_features(0.9, 0.10, 75).content is Content.UNKNOWN


def test_anything_unidentifiable_is_unknown_rather_than_camera():
    """R5. Asserting "camera" would be a claim the measurement cannot support.

    And `UNKNOWN` behaves exactly as today, so the distinction costs nothing while keeping the
    report honest about what was established.
    """
    report = cc.classify_features(2.8, 0.63, 75)
    assert report.content is Content.UNKNOWN
    assert report.is_synthetic is False
    assert "not identifiable as synthetic" in report.detail


def test_no_frames_measured_is_unknown():
    report = cc.classify_features(0.0, 0.0, 0)
    assert report.content is Content.UNKNOWN
    assert "existing behaviour" in report.detail


@pytest.mark.parametrize(
    ("ydif", "entropy"),
    [(cc.MAX_SYNTHETIC_YDIF, cc.MAX_SYNTHETIC_ENTROPY), (0.0, 0.0)],
)
def test_the_thresholds_are_inclusive(ydif, entropy):
    """Boundary values count as synthetic, pinned so a later `<` versus `<=` change is visible."""
    assert cc.classify_features(ydif, entropy, 75).content is Content.SCREEN


def test_just_outside_either_threshold_is_not_synthetic():
    assert cc.classify_features(cc.MAX_SYNTHETIC_YDIF + 0.01, 0.10, 75).content is Content.UNKNOWN
    assert cc.classify_features(0.0, cc.MAX_SYNTHETIC_ENTROPY + 0.01, 75).content is Content.UNKNOWN


# --- R8: the override -----------------------------------------------------------------------


def test_an_operator_can_force_either_class():
    """R8. Someone who knows what they uploaded should not have to argue with a classifier."""
    forced_screen = cc.classify("irrelevant.mp4", override="screen")
    assert forced_screen.content is Content.SCREEN
    assert forced_screen.forced is True
    assert forced_screen.marker == "content_class:screen:forced"

    forced_camera = cc.classify("irrelevant.mp4", override="camera")
    assert forced_camera.content is Content.CAMERA
    assert forced_camera.is_synthetic is False


def test_forcing_runs_no_measurement(monkeypatch):
    """An override must not pay for a decode it cannot use."""
    called: list[str] = []
    monkeypatch.setattr(cc, "measure", lambda *a, **k: called.append("measured") or (0, 0, 0))
    cc.classify("irrelevant.mp4", override="screen")
    assert called == []


def test_an_unrecognised_override_falls_back_to_auto():
    """A typo must not fail a render, and must not silently mean "screen"."""
    report = cc.classify("irrelevant.mp4", override="sceen", enabled=False)
    assert report.forced is False
    assert report.content is Content.UNKNOWN


def test_classification_can_be_disabled():
    report = cc.classify("irrelevant.mp4", enabled=False)
    assert report.content is Content.UNKNOWN
    assert "disabled" in report.detail


# --- availability ---------------------------------------------------------------------------


def test_a_build_without_the_filters_classifies_unknown():
    """Fails closed. The alternative -- classifying on one feature -- misclassifies real footage."""
    report = cc.classify("irrelevant.mp4", prober=_prober(available=False))
    assert report.content is Content.UNKNOWN
    assert "lacks" in report.detail


def test_a_raising_prober_classifies_unknown():
    def exploding(capability_id: str) -> Capability_Status:
        raise RuntimeError("probe unavailable")

    assert cc.filters_available(exploding) is False


# --- R3, R4, R11: what consumers ask --------------------------------------------------------


def test_screen_content_is_fitted_and_skips_face_tracking():
    """R3 and R4. Kept as separate predicates because they are separate requirements."""
    screen = cc.classify_features(0.0, 0.10, 75)
    assert cc.fit_instead_of_crop(screen) is True
    assert cc.skip_face_tracking(screen) is True


def test_unknown_content_changes_nothing():
    """R5, from the consumer side: the existing behaviour must run untouched."""
    unknown = cc.classify_features(2.0, 0.60, 75)
    assert cc.fit_instead_of_crop(unknown) is False
    assert cc.skip_face_tracking(unknown) is False


def test_the_synthetic_flag_is_exposed_for_other_components():
    """R11. `V21` stabilisation refuses synthetic content and must not re-derive the rule.

    A second copy of the comparison would be a second thing to get wrong when a threshold moves.
    """
    assert cc.classify_features(0.0, 0.10, 75).is_synthetic is True
    assert cc.classify_features(0.0, 0.10, 75).content is Content.SCREEN


# --- R9: measured behaviour, including what is missed ---------------------------------------


def test_the_measured_behaviour_is_reported_rather_than_accuracy_asserted():
    """R9. The thresholds came from measurement, and the record includes the failures."""
    data = cc.measured_behaviour()
    assert data["false_positives_on_camera"] == 0
    assert data["measured"], "the measurements the thresholds came from must be committed"
    for entry in data["measured"]:
        assert {"source", "ydif", "entropy", "truth", "classified"} <= set(entry)


def test_the_missed_classes_are_recorded_not_hidden():
    """Two classes are knowingly missed. Recording them stops a later threshold loosening.

    Loosening either bound to catch animation would pull nearly-still camera footage in with it,
    which is the trade this refuses to make.
    """
    data = cc.measured_behaviour()
    missed = " ".join(data["missed_screen_content"]).lower()
    assert "animation" in missed
    assert "moving video" in missed
    # And every "screen" fixture the classifier misses is recorded as unknown, not as a success.
    misses = [e for e in data["measured"] if e["truth"] == "screen" and e["classified"] != "screen"]
    assert len(misses) == 2


def test_no_camera_fixture_is_classified_as_screen_in_the_record():
    """R10. Automatic classification is only defensible if it does not degrade camera handling."""
    for entry in cc.measured_behaviour()["measured"]:
        if entry["truth"] == "camera":
            assert entry["classified"] != "screen", entry


def test_the_record_explains_why_one_feature_is_insufficient():
    """The reasoning has to travel with the thresholds, or the next person tries a simpler rule."""
    why = cc.measured_behaviour()["why_neither_feature_alone"].lower()
    assert "0.232" in why
    assert "conjunction" in why


def test_the_module_uses_no_model_or_network():
    """R2, asserted on imports rather than on prose."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cc))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("torch", "onnxruntime", "cv2", "mediapipe", "requests", "urllib"):
        assert forbidden not in imported, forbidden
