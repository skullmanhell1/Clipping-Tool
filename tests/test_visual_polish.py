"""Visual polish: V5, V6, V8, V14, V16, V18, V19.

These cover the second visual batch:

* **V16** de-letterbox before reframing, so already-boxed source footage does not get its bars
  baked into the vertical output;
* **V18** an optional 3D LUT after the colour preset;
* **V19** eased Ken Burns and scale bumps on real audio accents;
* **V8** the crop-update rate as a setting rather than a hardcoded 12/s;
* **V6** three and four speakers laid out as a 2-column grid instead of letterbox slivers;
* **V5** per-tile motion inside split-screen regions;
* **V14** a closing call-to-action.

The split-screen filtergraph tests run **real ffmpeg** rather than asserting on strings. The V5
mechanism depends on ``sendcmd`` addressing a *filter instance* (``crop@t0``) rather than a filter
name, and whether that works is a fact about ffmpeg, not about this module - a string assertion
would have passed just as happily on the version where every tile received every tile's commands.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.conftest import FFMPEG, probe_size, requires_ffmpeg
from worker import captions as cap
from worker.audio_features import detect_onsets
from worker.diarization import Speaker_Turn
from worker.effects import overlays
from worker.effects.reframe import (
    Association,
    Center,
    Face_Track,
    FaceBox,
    Region,
    _grid_regions,
    build_reframe_filter,
    build_region_centers,
    build_sendcmd,
    build_split_screen_layout,
)
from worker.ffmpeg_utils import detect_letterbox

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _boxed_video(dest, inner_w=960, inner_h=540, out_w=1080, out_h=1080, duration=3.0):
    """A clip whose real picture is ``inner`` centred in a larger black frame.

    Longer than a second by necessity: detection deliberately skips the opening second, because
    an opening fade from black is indistinguishable from a fully-letterboxed frame.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    pad_x = (out_w - inner_w) // 2
    pad_y = (out_h - inner_h) // 2
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={inner_w}x{inner_h}:rate=15:duration={duration}",
            "-vf",
            f"pad={out_w}:{out_h}:{pad_x}:{pad_y}:black",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def _still_gradient_video(dest, w=1920, h=1080, duration=6.0):
    """A clip of a single unchanging horizontal gradient.

    Nothing in the picture moves, so any change between two output frames of a render driven by
    this source can only have come from the crop moving.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    still = dest.with_suffix(".png")
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"gradients=s={w}x{h}:c0=black:c1=white:x0=0:y0={h // 2}"
            f":x1={w - 1}:y1={h // 2}:d=1:nb_colors=2",
            "-frames:v",
            "1",
            str(still),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-loop",
            "1",
            "-i",
            str(still),
            "-t",
            str(duration),
            "-r",
            "25",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def _mean_luma(video, at, band):
    """Mean luma of a horizontal ``band`` (slice of rows) of the frame at time ``at``."""
    top, bottom = band
    height = bottom - top
    out = subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(at),
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"crop=iw:{height}:0:{top},format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    data = out.stdout
    assert data, "no pixels decoded"
    return sum(data) / len(data)


def _zoom_at(filter_string, on):
    """Evaluate a ``zoompan`` zoom expression at frame ``on``.

    Only valid for the Ken-Burns-only forms, which are plain arithmetic in ``on`` and therefore
    happen to be valid Python too. Expressions containing ``if()``/``pow()`` (the opening
    transitions and the beat bumps) are ffmpeg-only and are asserted structurally instead.
    """
    expr = filter_string.split("z='", 1)[1].split("'", 1)[0]
    assert "if(" not in expr and "pow(" not in expr, f"not arithmetic: {expr}"
    return eval(expr, {"__builtins__": {}}, {"on": on})  # noqa: S307 - fixed local input


def _bright_fraction(video, at, band, threshold=240):
    """Fraction of pixels in ``band`` brighter than ``threshold`` at time ``at``."""
    top, bottom = band
    out = subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(at),
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"crop=iw:{bottom - top}:0:{top},format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    data = out.stdout
    assert data, "no pixels decoded"
    return sum(1 for value in data if value > threshold) / len(data)


def _track(track_id, cx, cy, times=(0.0,)):
    return Face_Track(
        track_id=track_id,
        boxes=[FaceBox(t, int(cx) - 80, int(cy) - 80, 160, 160) for t in times],
    )


# --------------------------------------------------------------------------- #
# V16 - de-letterbox
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_v16_finds_the_content_rectangle_inside_a_boxed_frame(tmp_path):
    """A 960x540 picture padded into 1080x1080 is reported as exactly that picture."""
    video = _boxed_video(tmp_path / "boxed.mp4")
    found = detect_letterbox(video)
    assert found is not None, "bars not detected"
    width, height, x, y = found
    # cropdetect works on encoded pixels, so allow a couple of pixels of slack rather than
    # demanding the exact geometry back.
    assert width == pytest.approx(960, abs=4)
    assert height == pytest.approx(540, abs=4)
    assert x == pytest.approx(60, abs=4)
    assert y == pytest.approx(270, abs=4)


@requires_ffmpeg
def test_v16_reports_nothing_on_a_clip_with_no_bars(tmp_path):
    """The common case must cost nothing and change nothing."""
    video = tmp_path / "clean.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1280x720:rate=15:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    assert detect_letterbox(video) is None


@requires_ffmpeg
def test_v16_a_clip_shorter_than_the_skipped_opening_is_not_an_error(tmp_path):
    """Documents the cost of skipping the first second: very short clips go undetected.

    That is the right trade. A clip shorter than the skip window yields no sampled frames and so
    no detection, which falls back to using the frame as-is - whereas probing from zero would
    read an opening fade as a fully-letterboxed frame and crop the picture away entirely.
    """
    video = _boxed_video(tmp_path / "brief.mp4", duration=0.6)
    assert detect_letterbox(video) is None


def test_v16_missing_file_is_not_an_error():
    """Detection is an optimisation; it must never be the reason a render fails."""
    assert detect_letterbox("/nonexistent/nope.mp4") is None


def test_v16_crop_is_confined_to_the_content_rectangle():
    """The whole point: with an origin, the crop window cannot land on the bars.

    The centres below all sit inside the bars (y around 30 in a frame whose picture starts at
    y=270). Confined to the content rectangle, every emitted y must still be within it.
    """
    centers = [Center(t / 4, 540, 30) for t in range(8)]
    script = build_sendcmd(
        centers,
        540,
        540,
        960,
        540,
        origin_x=60,
        origin_y=270,
    )
    ys = [int(line.split(" y ")[1].rstrip(";")) for line in script.strip().splitlines()]
    assert ys, "no commands emitted"
    for y in ys:
        assert 270 <= y <= 270 + 540 - 540, y


def test_v16_default_arguments_reproduce_the_unconfined_script():
    """No origin means the previous behaviour, exactly."""
    centers = [Center(0.0, 500, 400), Center(0.5, 600, 450)]
    assert build_sendcmd(centers, 400, 400, 1000, 800) == build_sendcmd(
        centers, 400, 400, 1000, 800, origin_x=0, origin_y=0
    )


# --------------------------------------------------------------------------- #
# V18 - LUT
# --------------------------------------------------------------------------- #


def test_v18_lut_is_applied_after_the_preset(tmp_path):
    """Order matters: a LUT maps final values, so it cannot precede a contrast curve."""
    lut = tmp_path / "look.cube"
    lut.write_text("LUT_3D_SIZE 2\n", encoding="utf-8")
    made = overlays.color_filter("vivid", str(lut))
    assert made is not None
    assert "eq=" in made and "lut3d" in made
    assert made.index("eq=") < made.index("lut3d")


def test_v18_lut_works_with_no_preset(tmp_path):
    """A LUT is a complete look on its own; requiring a preset too would be arbitrary."""
    lut = tmp_path / "look.cube"
    lut.write_text("LUT_3D_SIZE 2\n", encoding="utf-8")
    assert "lut3d" in (overlays.color_filter("", str(lut)) or "")


def test_v18_a_missing_lut_is_ignored_rather_than_failing_the_render(tmp_path):
    """A typo in a path should cost the look, not the clip."""
    assert overlays.color_filter("", str(tmp_path / "absent.cube")) is None
    assert overlays.color_filter("vivid", str(tmp_path / "absent.cube")) == (
        overlays.color_filter("vivid")
    )


def test_v18_a_file_that_is_not_a_lut_is_ignored(tmp_path):
    """`lut3d` fails the whole graph on a file it cannot parse, so screen by extension."""
    bogus = tmp_path / "notes.txt"
    bogus.write_text("this is not a lut", encoding="utf-8")
    assert overlays.color_filter("", str(bogus)) is None


def test_v18_no_lut_configured_changes_nothing():
    """The default must be byte-identical to the pre-V18 filter."""
    for preset in ("", "vivid", "warm", "bw"):
        assert overlays.color_filter(preset, "") == overlays.color_filter(preset)
        assert overlays.color_filter(preset, None) == overlays.color_filter(preset)


# --------------------------------------------------------------------------- #
# V19 - easing and beat bumps
# --------------------------------------------------------------------------- #


def test_v19_easing_keeps_the_endpoints_and_only_changes_the_curve():
    """An eased push must start and end where the linear one did.

    Otherwise this is not "ease the move", it is "change the move", and a caller who tuned the
    zoom depth would find it silently different.
    """
    linear = overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=True)
    eased = overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=True, ease=True)
    assert linear is not None and eased is not None
    assert linear != eased

    total = 300  # 10 s * 30 fps
    for frame in (0, total):
        assert _zoom_at(eased, frame) == pytest.approx(_zoom_at(linear, frame), abs=1e-9)
    # ...and it really is a 1.0 -> 1.12 push at both ends.
    assert _zoom_at(eased, 0) == pytest.approx(1.0, abs=1e-9)
    assert _zoom_at(eased, total) == pytest.approx(1.12, abs=1e-9)


def test_v19_easing_is_slower_at_the_ends_than_in_the_middle():
    """The defining property of a smoothstep: it starts and ends at rest."""
    eased = overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=True, ease=True)
    assert eased is not None
    early = _zoom_at(eased, 15) - _zoom_at(eased, 0)
    middle = _zoom_at(eased, 165) - _zoom_at(eased, 150)
    late = _zoom_at(eased, 300) - _zoom_at(eased, 285)
    assert middle > early
    assert middle > late


def test_v19_the_linear_ramp_it_replaces_moves_at_a_constant_rate():
    """Establishes the contrast the easing exists to fix, rather than asserting it in prose."""
    linear = overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=True)
    assert linear is not None
    first = _zoom_at(linear, 15) - _zoom_at(linear, 0)
    middle = _zoom_at(linear, 165) - _zoom_at(linear, 150)
    assert first == pytest.approx(middle, rel=1e-9)


def test_v19_easing_is_off_by_default_in_the_builder():
    """Guards the frozen v0.8.0 graph goldens, which this function is compared against."""
    assert overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=True) == (
        overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=True, ease=False)
    )


def test_v19_beats_add_bumps_that_multiply_rather_than_replace_the_zoom():
    """A bump has to compose with the Ken Burns push, not override it."""
    plain = overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=True)
    bumped = overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=True, beats=[2.0, 4.0])
    assert plain is not None and bumped is not None
    assert bumped != plain
    # The Ken Burns ramp survives *inside* the bumped expression rather than being replaced,
    # and the bump is applied to it as a product.
    assert "0.12*on" in bumped
    ramp = plain.split("z='", 1)[1].split("'", 1)[0]
    assert f"({ramp})*" in bumped


def test_v19_no_beats_changes_nothing():
    assert overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=True, beats=[]) == (
        overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=True)
    )


def test_v19_bumps_are_capped_so_a_busy_track_does_not_become_a_wobble():
    """Every onset in a dense track would produce continuous motion, not accents."""
    many = [i * 0.2 for i in range(200)]
    made = overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=True, beats=many)
    assert made is not None
    assert made.count("gte(t,") <= overlays.MAX_BEAT_PUNCHES


def test_v19_onsets_are_rises_not_loud_stretches():
    """A sustained loud passage is not a series of accents; the *change* is the signal."""
    steady = [(i * 0.1, -20.0) for i in range(30)]
    assert detect_onsets(steady, rise_db=6.0) == []

    rising = [(0.0, -40.0), (0.1, -40.0), (0.2, -20.0), (0.3, -40.0), (0.4, -18.0)]
    found = detect_onsets(rising, rise_db=6.0)
    assert found, "a 20 dB jump is an accent by any definition"
    assert all(isinstance(t, float) for t in found)


def test_v19_empty_envelope_yields_no_onsets():
    assert detect_onsets([], rise_db=6.0) == []


# --------------------------------------------------------------------------- #
# V8 - crop-update rate
# --------------------------------------------------------------------------- #


def test_v8_the_command_rate_is_a_setting_and_higher_than_the_old_literal():
    """12/s is visible as stepping on fast movement; the rate is now configurable."""
    from config import settings

    assert float(settings.reframe_command_fps) > 12.0


def test_v8_a_higher_rate_produces_more_crop_updates():
    """The rate has to actually reach the emitted path, not just exist as a field."""
    track = Face_Track(
        track_id="t0",
        boxes=[FaceBox(t / 10, 400 + t * 5, 400, 160, 160) for t in range(60)],
    )
    slow = build_region_centers(
        track,
        src_w=1920,
        src_h=1080,
        dst_w=540,
        dst_h=960,
        duration=6.0,
        command_fps=12.0,
    )
    fast = build_region_centers(
        track,
        src_w=1920,
        src_h=1080,
        dst_w=540,
        dst_h=960,
        duration=6.0,
        command_fps=24.0,
    )
    assert len(fast) > len(slow)
    # Same span either way - a finer grid, not a longer one.
    assert fast[-1].t == pytest.approx(slow[-1].t, abs=1e-6)


def test_v8_the_rate_only_affects_sampling_density_not_the_path_shape():
    """Raising it must not move the subject, only describe the same move more often."""
    track = Face_Track(
        track_id="t0",
        boxes=[FaceBox(t / 10, 400 + t * 5, 400, 160, 160) for t in range(60)],
    )
    common = dict(src_w=1920, src_h=1080, dst_w=540, dst_h=960, duration=6.0)
    slow = build_region_centers(track, command_fps=12.0, **common)
    fast = build_region_centers(track, command_fps=24.0, **common)
    # Both start in the same place and travel in the same direction.
    assert fast[0].cx == pytest.approx(slow[0].cx, abs=1e-6)
    assert (fast[-1].cx - fast[0].cx) > 0
    assert (slow[-1].cx - slow[0].cx) > 0


# --------------------------------------------------------------------------- #
# settings actually reach the render
# --------------------------------------------------------------------------- #
#
# Each builder above can be perfectly correct and completely unreachable. These render through
# the compositor with the ffmpeg call spied on, so a setting that is never read fails here rather
# than shipping as a field in a config file that does nothing.


def _graph_with(tmp_path, monkeypatch, **overrides):
    """Render through the compositor offline and return the ``-filter_complex`` graph."""
    from config import settings as app_settings
    from tests.test_kinetic_compositor import (
        MATRIX_HOOK,
        MATRIX_WORDS,
        _matrix_options,
        _parity_render,
    )
    from worker.effects import compositor

    for key, value in overrides.items():
        monkeypatch.setattr(app_settings, key, value, raising=False)
    record = _parity_render(
        compositor,
        tmp_path / "work",
        options=_matrix_options(zoom=True, captions=True),
        words=MATRIX_WORDS,
        hook_text=MATRIX_HOOK,
        contributions=None,
    )
    return record.graph


def test_v19_the_easing_setting_reaches_the_render(tmp_path, monkeypatch):
    off = _graph_with(tmp_path / "off", monkeypatch, zoom_ease=False)
    on = _graph_with(tmp_path / "on", monkeypatch, zoom_ease=True)
    assert off != on
    assert "3-2*" in on and "3-2*" not in off


def test_v19_easing_defaults_to_the_shipped_linear_ramp():
    """Every visual setting here defaults to the previously shipped behaviour.

    That convention is what makes the v0.8.0 byte-parity gate mean anything: if a new setting
    defaulted to on, the gate would have to be re-frozen with each release and would stop
    detecting accidental change. ``TRANSITION_STYLE`` and the ``PROGRESS_BAR_*`` settings follow
    the same rule.
    """
    from config import settings as app_settings

    assert app_settings.zoom_ease is False
    assert app_settings.beat_sync_zoom is False
    assert app_settings.color_lut == ""
    assert app_settings.end_card_text == ""


def test_v18_the_lut_setting_reaches_the_render(tmp_path, monkeypatch):
    lut = tmp_path / "look.cube"
    lut.parent.mkdir(parents=True, exist_ok=True)
    lut.write_text("LUT_3D_SIZE 2\n", encoding="utf-8")
    without = _graph_with(tmp_path / "a", monkeypatch, color_lut="")
    with_lut = _graph_with(tmp_path / "b", monkeypatch, color_lut=str(lut))
    assert "lut3d" not in without
    assert "lut3d" in with_lut


def test_v14_the_end_card_setting_reaches_the_render(tmp_path, monkeypatch):
    without = _graph_with(tmp_path / "a", monkeypatch, end_card_text="")
    with_card = _graph_with(tmp_path / "b", monkeypatch, end_card_text="follow for more")
    assert without.count("subtitles=") == 1
    # A second libass pass: the card's own ASS, on top of the captions.
    assert with_card.count("subtitles=") == 2


def test_v14_the_end_card_renders_with_captions_switched_off(tmp_path, monkeypatch):
    """The card must not depend on captions - that is why it is a standalone ASS."""
    from config import settings as app_settings
    from tests.test_kinetic_compositor import _matrix_options, _parity_render
    from worker.effects import compositor

    monkeypatch.setattr(app_settings, "end_card_text", "follow for more", raising=False)
    record = _parity_render(
        compositor,
        tmp_path / "nocaps",
        options=_matrix_options(captions=False, hook_title=False, zoom=True),
        words=[],
        hook_text="",
        contributions=None,
    )
    assert record.graph.count("subtitles=") == 1
    assert "end_card" in record.effects_applied


def test_v8_the_command_rate_setting_reaches_the_follow_active_path(monkeypatch):
    """``build_follow_active_path`` had the 12/s literal as its own default."""
    from config import settings as app_settings
    from worker.effects.reframe import build_follow_active_path

    tracks = [
        Face_Track(
            track_id="t0",
            boxes=[FaceBox(t / 10, 400 + t * 4, 400, 160, 160) for t in range(60)],
        )
    ]
    assoc = Association(by_turn={0: "t0"}, unassociated=[], shown_order=["t0"])
    turns = [Speaker_Turn("S0", 0.0, 6.0)]
    common = dict(
        src_w=1920,
        src_h=1080,
        crop_w=608,
        crop_h=1080,
        intensity="standard",
        duration=6.0,
    )

    monkeypatch.setattr(app_settings, "reframe_command_fps", 12.0, raising=False)
    slow = build_follow_active_path(turns, assoc, tracks, **common)
    monkeypatch.setattr(app_settings, "reframe_command_fps", 24.0, raising=False)
    fast = build_follow_active_path(turns, assoc, tracks, **common)
    assert len(fast) > len(slow)


# --------------------------------------------------------------------------- #
# V6 - grid layout
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("n", [3, 4])
def test_v6_grid_tiles_the_portrait_frame_exactly(n):
    """Tiles must partition the frame: hstack/vstack reject mismatched sizes outright."""
    shown = [f"t{i}" for i in range(n)]
    tracks = {tid: _track(tid, 300 + 400 * i, 400) for i, tid in enumerate(shown)}
    regions = _grid_regions(shown, tracks, 1080, 1920, 1920, 1080)

    assert len(regions) == n
    assert sum(r.dst_w * r.dst_h for r in regions) == 1080 * 1920
    for r in regions:
        assert r.dst_x >= 0 and r.dst_y >= 0
        assert r.dst_x + r.dst_w <= 1080
        assert r.dst_y + r.dst_h <= 1920


def test_v6_grid_tiles_are_taller_than_wide():
    """The reason the grid exists: a stacked 4-up gives 2.25:1 slots for faces."""
    shown = [f"t{i}" for i in range(4)]
    tracks = {tid: _track(tid, 400, 400) for tid in shown}
    for r in _grid_regions(shown, tracks, 1080, 1920, 1920, 1080):
        assert r.dst_h > r.dst_w, (r.dst_w, r.dst_h)


def test_v6_an_odd_final_tile_spans_the_full_width():
    """Leaving a black half-cell would read as a participant who dropped out."""
    shown = ["t0", "t1", "t2"]
    tracks = {tid: _track(tid, 400, 400) for tid in shown}
    last = _grid_regions(shown, tracks, 1080, 1920, 1920, 1080)[-1]
    assert last.dst_x == 0
    assert last.dst_w == 1080


@pytest.mark.parametrize("n", [3, 4])
def test_v6_the_layout_builder_chooses_the_grid_for_a_portrait_target(n):
    """Goes through ``build_split_screen_layout``, not ``_grid_regions``.

    Testing the grid helper on its own says nothing about whether anything calls it: with the
    dispatch removed, a 4-up portrait layout silently returns to four 1080x480 slivers and a
    direct test of the helper still passes.
    """
    shown = [f"t{i}" for i in range(n)]
    tracks = [_track(tid, 300 + 400 * i, 400) for i, tid in enumerate(shown)]
    assoc = Association(
        by_turn={i: shown[i] for i in range(n)},
        unassociated=[],
        shown_order=list(shown),
    )
    turns = [Speaker_Turn(f"S{i}", float(i), float(i) + 1.0) for i in range(n)]

    regions = build_split_screen_layout(
        turns,
        assoc,
        tracks,
        target_w=1080,
        target_h=1920,
        src_w=1920,
        src_h=1080,
        max_regions=n,
    )
    assert len(regions) == n
    # Two columns: some tile starts at a non-zero x, which a stack never produces.
    assert any(r.dst_x > 0 for r in regions)
    # Every *paired* tile is a portrait slot. The odd final tile of a 3-up deliberately spans the
    # full width instead of leaving a black half-cell, so it is landscape by design.
    paired = [r for r in regions if r.dst_w < 1080]
    assert len(paired) == (n if n % 2 == 0 else n - 1)
    for r in paired:
        assert r.dst_h > r.dst_w, (r.dst_w, r.dst_h)


def test_v6_a_two_up_portrait_layout_still_stacks():
    """V6 must not change the 2-up case, which is the default and the shipped behaviour."""
    shown = ["t0", "t1"]
    tracks = [_track(tid, 400 + 600 * i, 400) for i, tid in enumerate(shown)]
    assoc = Association(by_turn={0: "t0", 1: "t1"}, unassociated=[], shown_order=list(shown))
    turns = [Speaker_Turn("S0", 0.0, 1.0), Speaker_Turn("S1", 1.0, 2.0)]
    regions = build_split_screen_layout(
        turns,
        assoc,
        tracks,
        target_w=1080,
        target_h=1920,
        src_w=1920,
        src_h=1080,
        max_regions=2,
    )
    assert len(regions) == 2
    for r in regions:
        assert r.dst_x == 0
        assert r.dst_w == 1080


def test_v6_a_landscape_target_keeps_its_side_by_side_layout():
    """The grid is a fix for *portrait* slivers and must not spread to landscape.

    A 1920x1080 3-up side-by-side already gives 640x1080 tiles, which are portrait slots holding
    a face perfectly well. Re-laying those out as a grid would be a change with no problem behind
    it - and nothing else in the suite noticed when the dispatch was widened to all aspects.
    """
    shown = [f"t{i}" for i in range(3)]
    tracks = [_track(tid, 300 + 400 * i, 400) for i, tid in enumerate(shown)]
    assoc = Association(
        by_turn={i: shown[i] for i in range(3)}, unassociated=[], shown_order=list(shown)
    )
    turns = [Speaker_Turn(f"S{i}", float(i), float(i) + 1.0) for i in range(3)]
    regions = build_split_screen_layout(
        turns,
        assoc,
        tracks,
        target_w=1920,
        target_h=1080,
        src_w=1920,
        src_h=1080,
        max_regions=3,
    )
    assert len(regions) == 3
    for r in regions:
        assert r.dst_y == 0
        assert r.dst_h == 1080


def test_v5_the_layout_builder_attaches_a_centre_path_to_each_tile():
    """Goes through ``build_split_screen_layout``: without this, V5 is never reached.

    ``build_region_centers`` can be perfectly correct and still never called - dropping the
    ``replace(region, centers=...)`` leaves every tile static and a direct test of the path
    builder passes regardless.
    """
    shown = ["t0", "t1"]
    tracks = [
        Face_Track(
            track_id=tid,
            boxes=[FaceBox(t / 2, 300 + 500 * i + t * 20, 400, 160, 160) for t in range(12)],
        )
        for i, tid in enumerate(shown)
    ]
    assoc = Association(by_turn={0: "t0", 1: "t1"}, unassociated=[], shown_order=list(shown))
    turns = [Speaker_Turn("S0", 0.0, 3.0), Speaker_Turn("S1", 3.0, 6.0)]

    regions = build_split_screen_layout(
        turns,
        assoc,
        tracks,
        target_w=1080,
        target_h=1920,
        src_w=1920,
        src_h=1080,
        max_regions=2,
        duration=6.0,
    )
    assert len(regions) == 2
    for r in regions:
        assert r.centers, f"tile {r.track_id} got no centre path"
        assert len(r.centers) > 1


def test_v5_no_duration_means_the_previous_static_layout():
    """Callers that do not supply a duration keep exactly the pre-V5 behaviour."""
    shown = ["t0", "t1"]
    tracks = [
        Face_Track(
            track_id=tid,
            boxes=[FaceBox(t / 2, 300 + 500 * i + t * 20, 400, 160, 160) for t in range(12)],
        )
        for i, tid in enumerate(shown)
    ]
    assoc = Association(by_turn={0: "t0", 1: "t1"}, unassociated=[], shown_order=list(shown))
    turns = [Speaker_Turn("S0", 0.0, 3.0), Speaker_Turn("S1", 3.0, 6.0)]
    regions = build_split_screen_layout(
        turns,
        assoc,
        tracks,
        target_w=1080,
        target_h=1920,
        src_w=1920,
        src_h=1080,
        max_regions=2,
    )
    assert all(r.centers == () for r in regions)


@requires_ffmpeg
@pytest.mark.parametrize("n", [3, 4])
def test_v6_the_grid_filtergraph_actually_renders(tmp_path, n):
    """The graph has to be one ffmpeg accepts, not merely one that looks right."""
    src = _still_gradient_video(tmp_path / "src.mp4", duration=1.0)
    shown = [f"t{i}" for i in range(n)]
    tracks = {tid: _track(tid, 300 + 400 * i, 400) for i, tid in enumerate(shown)}
    regions = _grid_regions(shown, tracks, 1080, 1920, 1920, 1080)
    _inputs, graph, _notes = build_reframe_filter(
        "split_screen",
        regions=regions,
        crop_w=0,
        crop_h=0,
        src_w=1920,
        src_h=1080,
        target_w=1080,
        target_h=1920,
    )
    out = tmp_path / f"grid{n}.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-i",
            str(src),
            "-filter_complex",
            graph,
            "-map",
            "[vout]",
            "-frames:v",
            "10",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    assert probe_size(out) == (1080, 1920)


# --------------------------------------------------------------------------- #
# V5 - per-tile motion
# --------------------------------------------------------------------------- #


def test_v5_a_tile_gets_a_centre_path_that_follows_its_own_track():
    """Split-screen froze each tile on the mean of its track for the whole clip."""
    moving = _track("t0", 0, 400, times=(0.0,))
    moving = Face_Track(
        track_id="t0",
        boxes=[FaceBox(t / 2, 200 + int(t * 60), 320, 160, 160) for t in range(12)],
    )
    centers = build_region_centers(
        moving,
        src_w=1920,
        src_h=1080,
        dst_w=540,
        dst_h=960,
        duration=6.0,
        command_fps=10.0,
    )
    assert centers, "no path produced for a track that clearly moves"
    assert centers[-1].cx > centers[0].cx, "the path did not follow the subject"
    assert all(0.0 <= c.t <= 6.0 for c in centers)


def test_v5_the_path_is_clamped_inside_the_source_frame():
    """A centre outside the frame produces a crop ffmpeg rejects."""
    off = Face_Track(
        track_id="t0",
        boxes=[FaceBox(t / 2, -400, -400, 160, 160) for t in range(6)],
    )
    centers = build_region_centers(
        off,
        src_w=1920,
        src_h=1080,
        dst_w=540,
        dst_h=960,
        duration=3.0,
        command_fps=10.0,
    )
    assert centers
    for c in centers:
        assert 0 <= c.cx <= 1920
        assert 0 <= c.cy <= 1080


def test_v5_no_track_or_no_duration_means_a_static_tile():
    """Falls back to exactly the pre-V5 behaviour rather than inventing a path."""
    assert (
        build_region_centers(None, src_w=1920, src_h=1080, dst_w=540, dst_h=960, duration=6.0) == ()
    )
    assert (
        build_region_centers(
            _track("t0", 400, 400), src_w=1920, src_h=1080, dst_w=540, dst_h=960, duration=0.0
        )
        == ()
    )


def test_v5_each_tile_addresses_its_own_crop_instance(tmp_path):
    """The mechanism, asserted directly.

    ``sendcmd`` dispatches by target name across the entire filtergraph. With several plain
    ``crop`` filters in one graph, every tile's commands reach every tile.
    """
    regions = [
        Region(0, 0, 1080, 960, 480, 270, "t0", (Center(0.0, 300, 270), Center(1.0, 600, 270))),
        Region(
            0, 960, 1080, 960, 1400, 270, "t1", (Center(0.0, 1500, 270), Center(1.0, 1200, 270))
        ),
    ]
    tiles = [str(tmp_path / "tile0.cmd"), str(tmp_path / "tile1.cmd")]
    _inputs, graph, _notes = build_reframe_filter(
        "split_screen",
        regions=regions,
        crop_w=0,
        crop_h=0,
        src_w=1920,
        src_h=1080,
        target_w=1080,
        target_h=1920,
        tile_sendcmd_paths=tiles,
    )
    assert "crop@t0" in graph and "crop@t1" in graph
    for path in tiles:
        with open(path, encoding="utf-8") as handle:
            script = handle.read()
        # Each script addresses exactly one instance.
        assert script.count("crop@t0") == 0 or script.count("crop@t1") == 0


def test_v5_a_static_region_emits_no_sendcmd(tmp_path):
    """No path, no script: the tile renders as one fixed crop, as before."""
    regions = [
        Region(0, 0, 1080, 960, 480, 270, "t0"),
        Region(0, 960, 1080, 960, 1400, 270, "t1"),
    ]
    _inputs, graph, _notes = build_reframe_filter(
        "split_screen",
        regions=regions,
        crop_w=0,
        crop_h=0,
        src_w=1920,
        src_h=1080,
        target_w=1080,
        target_h=1920,
        tile_sendcmd_paths=[str(tmp_path / "a.cmd"), str(tmp_path / "b.cmd")],
    )
    assert "sendcmd" not in graph
    assert "crop@" not in graph


@requires_ffmpeg
def test_v5_tiles_move_independently_through_real_ffmpeg(tmp_path):
    """The end-to-end proof, and the one test that would have caught the naive version.

    The source is a single unchanging gradient, so any luma change between two output frames must
    be crop motion. The two tiles are given opposing paths: the top pans toward the bright end,
    the bottom toward the dark end. Opposite signs can only happen if each tile followed its own
    script - with a shared ``crop`` target one of them does not move at all.
    """
    src = _still_gradient_video(tmp_path / "src.mp4", duration=6.0)
    # A 1080x960 tile from a 1920x1080 source crops 1214 px wide, so the crop's x can only range
    # over 0..706 and the centres have to stay inside that band to move at all. Centres outside
    # it are clamped - which is correct behaviour, and would make this test silently vacuous.
    regions = [
        Region(
            0,
            0,
            1080,
            960,
            700,
            540,
            "t0",
            tuple(Center(i / 4, 700 + 100 * (i / 4), 540) for i in range(0, 25)),
        ),
        Region(
            0,
            960,
            1080,
            960,
            1300,
            540,
            "t1",
            tuple(Center(i / 4, 1300 - 100 * (i / 4), 540) for i in range(0, 25)),
        ),
    ]
    tiles = [str(tmp_path / "t0.cmd"), str(tmp_path / "t1.cmd")]
    _inputs, graph, _notes = build_reframe_filter(
        "split_screen",
        regions=regions,
        crop_w=0,
        crop_h=0,
        src_w=1920,
        src_h=1080,
        target_w=1080,
        target_h=1920,
        tile_sendcmd_paths=tiles,
    )
    out = tmp_path / "out.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-i",
            str(src),
            "-filter_complex",
            graph,
            "-map",
            "[vout]",
            "-frames:v",
            "125",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    assert probe_size(out) == (1080, 1920)

    top_start = _mean_luma(out, 0.0, (0, 960))
    top_end = _mean_luma(out, 4.5, (0, 960))
    bottom_start = _mean_luma(out, 0.0, (960, 1920))
    bottom_end = _mean_luma(out, 4.5, (960, 1920))

    assert top_end - top_start > 10, (top_start, top_end)
    assert bottom_start - bottom_end > 10, (bottom_start, bottom_end)


# --------------------------------------------------------------------------- #
# V14 - end card
# --------------------------------------------------------------------------- #


def test_v14_no_text_configured_means_no_card(tmp_path):
    """Off by default; an unconfigured install renders exactly as before."""
    assert cap.end_card_dialogue(30.0, text="") == ""
    # No text configured -> no file at all, not an empty one for ffmpeg to load.
    assert cap.write_end_card_ass(tmp_path / "e.ass", 30.0, text="") is None
    assert not (tmp_path / "e.ass").exists()


def test_v14_the_card_sits_at_the_end_of_the_clip():
    line = cap.end_card_dialogue(30.0, text="follow for more", seconds=2.0)
    assert line
    start, end = line.split(",")[1], line.split(",")[2]
    assert start == "0:00:28.00"
    assert end == "0:00:30.00"


def test_v14_the_hold_is_capped_at_half_the_clip():
    """A 2 s card on a 3 s clip is an advert with a clip attached."""
    line = cap.end_card_dialogue(3.0, text="follow", seconds=2.0)
    assert line
    start = line.split(",")[1]
    assert start == "0:00:01.50"


def test_v14_a_clip_too_short_to_show_the_card_gets_none():
    """A card that fades in as the video cuts is worse than no card."""
    assert cap.end_card_dialogue(0.4, text="follow", seconds=2.0) == ""
    assert cap.end_card_dialogue(0.0, text="follow", seconds=2.0) == ""


def test_v14_the_card_does_not_fade_out():
    """The clip ends under it; fading would remove the words at the decision point."""
    line = cap.end_card_dialogue(30.0, text="follow", seconds=2.0)
    assert "\\fad(300,0)" in line


def test_v14_position_arguments_are_numeric():
    """ASS override tags take literal numbers - an expression is silently mispositioned."""
    line = cap.end_card_dialogue(30.0, text="follow", seconds=2.0, video_width=1080)
    move = line.split("\\move(", 1)[1].split(")", 1)[0]
    for arg in move.split(","):
        int(arg.strip())
    assert "\\move(540," in line


def test_v14_writes_a_standalone_ass_that_libass_can_parse(tmp_path):
    """Standalone so the card is independent of captions being on, or engine-owned."""
    path = cap.write_end_card_ass(tmp_path / "end.ass", 30.0, text="follow for more", seconds=2.0)
    assert path is not None
    body = path.read_text(encoding="utf-8")
    assert "[Script Info]" in body
    assert "[V4+ Styles]" in body
    assert "[Events]" in body
    # The event references a style the file actually defines.
    assert "Style: End," in body
    assert ",End,," in body
    assert "FOLLOW FOR MORE" in body


def test_v14_text_is_escaped(tmp_path):
    """Braces are ASS override syntax; unescaped user text becomes a tag."""
    line = cap.end_card_dialogue(30.0, text="follow {me} now", seconds=2.0)
    assert line
    tags, _, body = line.partition("}")
    assert "{" not in body


@requires_ffmpeg
def test_v14_the_card_renders_through_libass(tmp_path):
    """Renders as a real subtitle burn, which is how it reaches the viewer."""
    # A flat mid-grey source, so any bright pixel in the output is drawn text rather than part of
    # the picture. Measuring mean luma over a gradient would drown a line of text in the ~0.5
    # levels it moves across a 300-row band.
    src = tmp_path / "grey.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:s=1080x1920:r=25:d=4",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    path = cap.write_end_card_ass(tmp_path / "end.ass", 4.0, text="follow for more", seconds=2.0)
    assert path is not None
    out = tmp_path / "carded.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-i",
            str(src),
            "-vf",
            cap.subtitles_filter(path),
            "-frames:v",
            "100",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    # Absent for the first half, present for the last: text appears only over the tail.
    assert _bright_fraction(out, 0.5, (1400, 1700)) < 0.001
    assert _bright_fraction(out, 3.5, (1400, 1700)) > 0.005
