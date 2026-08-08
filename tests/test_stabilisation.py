"""Optional stabilisation (V21).

The requirement that carries the weight is **R10.5**: the margin `vidstab` consumes must be handed to
reframing, or the two each consume the same pixels and the crop drifts into the invalid band —
delivering black edges. So the tests centre on the content rectangle, and the end-to-end one asserts
**no black edges in the delivered frame**, which is the symptom a viewer would actually see.

The second is **R10.9**: never stabilise synthetic content. Screen recordings have no camera shake,
and `vidstab` finds spurious motion in scrolling text and introduces a wobble that was not there —
so this is a refusal to make things worse rather than a missing capability.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from config import settings as app_settings
from worker import stabilise
from worker.effects.reframe import Center, build_sendcmd
from worker.engines.capabilities import Capability_Status

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None, reason="no ffmpeg on PATH; stabilisation needs it"
)


def _prober(available=True):
    def prober(capability_id: str) -> Capability_Status:
        return Capability_Status(capability_id, available, "injected")

    return prober


def _shaky(path, *, seconds=3):
    """A source that genuinely moves: a pattern translated by a jittering offset."""
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=s=640x480:r=25:d={seconds}",
            "-vf",
            (
                "crop=560:400:'20+18*sin(t*9)+8*sin(t*23)':'20+14*cos(t*11)+6*cos(t*31)',"
                "scale=640:480"
            ),
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=600,
    )
    return path


# --- R10.3: default off ---------------------------------------------------------------------


def test_stabilisation_is_disabled_by_default():
    """R10.3. Slow, two-pass, and wrong for plenty of footage."""
    assert float(app_settings.stabilise_strength) == 0.0
    plan = stabilise.plan(src_w=1920, src_h=1080, strength=0.0)
    assert plan.enabled is False
    assert plan.markers == ()


def test_the_disabled_plan_leaves_the_content_rectangle_whole():
    """Off must mean the full frame, or every reframe geometry shifts for a disabled feature."""
    plan = stabilise.plan(src_w=1920, src_h=1080, strength=0.0)
    assert plan.content_rect(1920, 1080) == (0, 0, 1920, 1080)


def test_an_unusable_strength_disables_rather_than_maximising():
    for value in (float("nan"), "not-a-number", None, -1.0):
        assert stabilise.clamp_strength(value) == 0.0
        assert stabilise.plan(src_w=1920, src_h=1080, strength=value).enabled is False


# --- R10.5: the margin, and the rectangle it produces ---------------------------------------


def test_the_margin_scales_with_strength():
    """The margin is frame given up, so it must be proportional and bounded."""
    small = stabilise.margin_pixels(1920, 1080, 0.25)
    full = stabilise.margin_pixels(1920, 1080, 1.0)
    assert small[0] < full[0] and small[1] < full[1]
    assert full == (
        round(1920 * stabilise.MAX_SHIFT_FRACTION),
        round(1080 * stabilise.MAX_SHIFT_FRACTION),
    )


def test_the_content_rectangle_is_inset_on_both_sides():
    """The invalid band is on every edge, so the inset is doubled in each dimension."""
    plan = stabilise.plan(src_w=1920, src_h=1080, strength=1.0, prober=_prober())
    ox, oy, w, h = plan.content_rect(1920, 1080)
    assert ox == plan.margin_x and oy == plan.margin_y
    assert w == 1920 - 2 * plan.margin_x
    assert h == 1080 - 2 * plan.margin_y


def test_the_rectangle_dimensions_are_even():
    """libx264's 4:2:0 subsampling requires it; an odd crop fails the encode outright."""
    for width, height in ((1921, 1081), (1919, 1079), (1280, 719)):
        _ox, _oy, w, h = stabilise.plan(
            src_w=width, src_h=height, strength=1.0, prober=_prober()
        ).content_rect(width, height)
        assert w % 2 == 0 and h % 2 == 0, (width, height, w, h)


def test_the_inset_cannot_consume_the_whole_frame():
    """A pathological margin on a tiny source must still leave a usable rectangle."""
    _ox, _oy, w, h = stabilise.plan(
        src_w=64, src_h=64, strength=1.0, prober=_prober()
    ).content_rect(64, 64)
    assert w >= 2 and h >= 2


def test_the_rectangle_matches_the_shape_letterbox_detection_returns():
    """R10.5 says reuse the existing mechanism, and this is what makes that literal.

    `detect_letterbox` returns `(w, h, x, y)` and this returns `(x, y, w, h)` — different order, same
    four facts — so a caller can feed either into `build_sendcmd`'s origin/src parameters. Asserted
    as a shape and arity check so a future field addition cannot silently break the interchange.
    """
    rect = stabilise.plan(src_w=1920, src_h=1080, strength=0.5, prober=_prober()).content_rect(
        1920, 1080
    )
    assert len(rect) == 4
    assert all(isinstance(value, int) for value in rect)


def test_reframe_confines_its_crop_to_the_stabilised_rectangle():
    """The composition R10.5 exists to guarantee, asserted through the real `build_sendcmd`.

    A crop that started outside the rectangle would include pixels `vidstab` may have vacated, and
    the delivered clip would show black edges that no later stage can detect.
    """
    plan = stabilise.plan(src_w=1920, src_h=1080, strength=1.0, prober=_prober())
    ox, oy, w, h = plan.content_rect(1920, 1080)
    crop_w, crop_h = 600, 900

    # Centres deliberately pushed hard against every edge of the source.
    centers = [
        Center(0.0, 0.0, 0.0),
        Center(0.5, 1920.0, 1080.0),
        Center(1.0, 960.0, 540.0),
    ]
    script = build_sendcmd(centers, crop_w, crop_h, w, h, origin_x=ox, origin_y=oy)

    # Each command reads `0.000 crop x 0, crop y 0;` — so the first fragment carries the timestamp
    # too, which is why this matches on the `crop <axis> <value>` term rather than splitting on
    # commas and taking the last token.
    import re

    pattern = re.compile(r"crop\s+([xy])\s+(-?\d+)")
    checked = 0
    for line in script.splitlines():
        terms = {axis: int(value) for axis, value in pattern.findall(line)}
        if not terms:
            continue
        checked += 1
        assert ox <= terms["x"] and terms["x"] + crop_w <= ox + w, (terms, ox, w)
        assert oy <= terms["y"] and terms["y"] + crop_h <= oy + h, (terms, oy, h)
    assert checked == len(centers), "every emitted command must have been checked"


# --- R10.9: never on synthetic content ------------------------------------------------------


def test_synthetic_content_is_never_stabilised():
    """R10.9. `vidstab` finds spurious motion in scrolling text and adds a wobble.

    A refusal to make things worse, not a missing capability — hence a distinct marker from the
    unavailable case.
    """
    plan = stabilise.plan(src_w=1920, src_h=1080, strength=1.0, is_synthetic=True, prober=_prober())
    assert plan.enabled is False
    assert plan.markers == ("stabilise_skipped:synthetic_content",)
    assert plan.content_rect(1920, 1080) == (0, 0, 1920, 1080)


def test_the_synthetic_decision_is_delegated_not_reimplemented():
    """V24 owns the classification, so its thresholds live in exactly one place.

    Asserted on imports: a second copy of the rule would be a second thing to get wrong the next
    time those thresholds move.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(stabilise))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "content_class" not in imported, (
        "the synthetic decision must arrive as a parameter, not be recomputed here"
    )


def test_skipping_for_synthetic_content_outranks_unavailability():
    """Order matters for the marker: "nothing to stabilise" is more useful than "no filter".

    A screen recording on a build without libvidstab should report why it was *never appropriate*,
    not why it was impossible.
    """
    plan = stabilise.plan(
        src_w=1920, src_h=1080, strength=1.0, is_synthetic=True, prober=_prober(available=False)
    )
    assert plan.markers == ("stabilise_skipped:synthetic_content",)


# --- R10.2: availability --------------------------------------------------------------------


def test_a_build_without_libvidstab_degrades_with_a_named_marker():
    """R10.2. `libvidstab` is a build option and genuinely absent from several distributions."""
    plan = stabilise.plan(src_w=1920, src_h=1080, strength=1.0, prober=_prober(available=False))
    assert plan.enabled is False
    assert plan.markers == (f"stabilise_degraded:ffmpeg_filter:{stabilise.REQUIRED_FILTERS[0]}",)


def test_a_raising_prober_fails_closed():
    """Emitting `vidstabdetect` on a build without it is a filter-graph error, i.e. a failed render."""

    def exploding(capability_id: str) -> Capability_Status:
        raise RuntimeError("probe unavailable")

    assert stabilise.filters_available(exploding) is False


# --- the filters themselves -----------------------------------------------------------------


def test_optimal_zoom_is_disabled():
    """The decision this module is built around.

    With `optzoom` enabled, `vidstab` scales the picture to hide shifted edges — changing subject
    scale by an amount that depends on how shaky the footage was. That fights V23's scale
    normalisation and makes two clips from one source frame differently for reasons nobody chose.
    Disabled, the margin is explicit and reframing is told about it.
    """
    chain = stabilise.transform_filter("/tmp/t.trf", 1.0, src_w=1920, src_h=1080)
    assert "optzoom=0" in chain
    assert "zoom=" not in chain.replace("optzoom=0", "")


def test_the_transform_maxshift_matches_the_declared_margin():
    """If these disagreed, the rectangle would be wrong and the crop could reach invalid pixels."""
    plan = stabilise.plan(src_w=1920, src_h=1080, strength=1.0, prober=_prober())
    chain = stabilise.transform_filter("/tmp/t.trf", 1.0, src_w=1920, src_h=1080)
    assert f"maxshift={max(plan.margin_x, plan.margin_y)}" in chain


def test_detection_shakiness_scales_with_strength():
    """A low setting should not pay for a search it will not use."""
    low = stabilise.detect_filter("/tmp/t.trf", 0.1)
    high = stabilise.detect_filter("/tmp/t.trf", 1.0)
    assert "shakiness=1" in low
    assert f"shakiness={stabilise.DETECT_SHAKINESS}" in high


def test_paths_with_colons_are_escaped():
    """`:` separates filter options, so an unescaped path is a graph parse error naming the graph."""
    chain = stabilise.detect_filter("/tmp/odd:dir/t.trf", 1.0)
    assert "odd\\:dir" in chain


# --- R10.6/R10.7: markers and strength ------------------------------------------------------


def test_the_marker_names_the_strength_that_ran():
    """R10.6, and the project's standing rule: report the resolved value."""
    assert stabilise.plan(src_w=1920, src_h=1080, strength=0.6, prober=_prober()).markers == (
        "stabilise:0.60",
    )
    # Clamped, and reported as clamped.
    assert stabilise.plan(src_w=1920, src_h=1080, strength=5.0, prober=_prober()).markers == (
        "stabilise:1.00",
    )


# --- R10.8: progress ------------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_analysis_pass_reports_progress(tmp_path):
    """R10.8. A silent two-pass analysis on a long source is indistinguishable from a hung job.

    This is the one genuinely expensive addition in the spec: `vidstabdetect` must see every frame
    before `vidstabtransform` can act.
    """
    source = _shaky(tmp_path / "shaky.mp4")
    events: list[tuple[float, str]] = []
    ok = stabilise.run_analysis(
        source, tmp_path / "t.trf", 1.0, progress=lambda f, m: events.append((f, m))
    )
    assert ok is True
    assert (tmp_path / "t.trf").is_file()
    assert events and events[0][0] == 0.0 and events[-1][0] == 1.0
    assert all(message for _f, message in events)


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_failed_analysis_degrades_rather_than_raising(tmp_path):
    """A stabilisation that cannot run must not fail a deliverable clip."""
    assert stabilise.run_analysis(tmp_path / "missing.mp4", tmp_path / "t.trf", 1.0) is False


# --- R10.5 end to end: no black edges -------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_stabilised_crop_has_no_black_edges(tmp_path):
    """The symptom R10.5 exists to prevent, measured in the delivered pixels.

    Stabilise, then crop *within the declared content rectangle*, then check the frame border. If the
    rectangle were too generous the crop would include pixels `vidstab` vacated, and those are black.

    The border is sampled with `cropdetect`, which reports the content rectangle of the *result* — if
    it finds bars, the composition failed.
    """
    source = _shaky(tmp_path / "shaky.mp4")
    transforms = tmp_path / "t.trf"
    assert stabilise.run_analysis(source, transforms, 1.0)

    plan = stabilise.plan(src_w=640, src_h=480, strength=1.0, prober=_prober())
    ox, oy, w, h = plan.content_rect(640, 480)
    assert (ox, oy) != (0, 0), "precondition: a margin must actually have been reserved"

    dest = tmp_path / "out.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            (
                f"{stabilise.transform_filter(transforms, 1.0, src_w=640, src_h=480)},"
                f"crop={w}:{h}:{ox}:{oy}"
            ),
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
        timeout=900,
    )

    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i",
            str(dest),
            "-vf",
            "cropdetect=limit=24:round=2:reset=0",
            "-frames:v",
            "50",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    import re

    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", proc.stdout + proc.stderr)
    assert found, "cropdetect produced no report"
    det_w, det_h, _x, _y = (int(v) for v in found[-1])
    # Allow a couple of pixels for compression softening at the very edge.
    assert det_w >= w - 4 and det_h >= h - 4, (
        f"cropdetect found content of {det_w}x{det_h} inside a {w}x{h} crop, "
        "so the stabilised frame has black edges the content rectangle did not account for"
    )
