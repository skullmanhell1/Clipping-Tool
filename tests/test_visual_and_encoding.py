"""Tests for V4, V9, V11, V13, V17, O5, O9 and O11.

Two themes run through these.

**Backwards compatibility is load-bearing.** Every item here adds a *choice* where a value used
to be hard-coded, and the default of each choice is the old value. The v0.8.0 parity gate
compares whole filter graphs, so a new argument with a changed default would not fail here - it
would fail there, far from the change, as an inexplicable graph mismatch. Several tests below
therefore assert nothing except that the default output is byte-identical to what shipped.

**Filter strings are asserted for the property, not for the substring.** A ``drawbox`` can be
present and ineffective, and an expression can be syntactically fine and evaluate to a constant.
Where it matters the expression is evaluated rather than matched - the same approach the stem
engine's notch tests use.
"""

from __future__ import annotations

import subprocess

import pytest

from config import settings
from worker import ffmpeg_utils as fu
from worker import scene_detect, subtitle_export, thumbnail
from worker.effects import overlays
from worker.effects.reframe import Center, cut_indices, ema_smooth, smooth_centers

requires_ffmpeg = pytest.mark.skipif(
    subprocess.run(["which", settings.ffmpeg_binary], capture_output=True).returncode != 0,
    reason="ffmpeg not on PATH",
)


class Word:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


# --------------------------------------------------------------------------- #
# V4 - reset reframe tracking at shot changes
# --------------------------------------------------------------------------- #
def test_without_a_cut_the_smoother_is_unchanged():
    """The whole point is that nothing changes when there is no cut to react to."""
    values = [0.0, 100.0, 100.0, 100.0]
    assert ema_smooth(values, 0.35) == ema_smooth(values, 0.35, reset_at=())


def test_a_reset_makes_the_output_follow_the_input_immediately():
    """Across a cut the crop must jump, not drift.

    Fails if the reset merely nudges the average: the value at the cut has to *be* the new
    input, because the previous shot's framing carries no information about the new one.
    """
    values = [0.0, 0.0, 900.0, 900.0]
    drifting = ema_smooth(values, 0.35)
    snapping = ema_smooth(values, 0.35, reset_at=(2,))
    assert snapping[2] == 900.0
    assert drifting[2] < 400.0, "sanity: without the reset the EMA lags well behind"
    # And the frames after the cut are already settled rather than still converging.
    assert snapping[3] == 900.0


def test_a_reset_does_not_leak_backwards():
    values = [10.0, 10.0, 900.0]
    assert ema_smooth(values, 0.35, reset_at=(2,))[:2] == ema_smooth(values, 0.35)[:2]


def test_cut_times_map_to_the_first_sample_after_the_cut():
    samples = [Center(0.0, 1, 1), Center(0.5, 2, 2), Center(1.0, 3, 3), Center(1.5, 4, 4)]
    assert cut_indices(samples, [0.75]) == [2]
    assert cut_indices(samples, [0.5]) == [1]  # a cut exactly on a sample time


def test_cuts_outside_the_sample_range_are_ignored():
    """Index 0 already starts the average fresh, so a cut before it has nothing to reset."""
    samples = [Center(1.0, 1, 1), Center(2.0, 2, 2)]
    assert cut_indices(samples, [0.1]) == []
    assert cut_indices(samples, [99.0]) == []
    assert cut_indices([], [1.0]) == []


def test_several_cuts_each_produce_one_reset():
    samples = [Center(float(i), i, i) for i in range(6)]
    assert cut_indices(samples, [1.5, 3.5, 3.6]) == [2, 4]


def test_smooth_centers_applies_the_cuts():
    samples = [Center(0.0, 0.0, 0.0), Center(1.0, 0.0, 0.0), Center(2.0, 900.0, 900.0)]
    with_cut = smooth_centers(samples, 0.35, cuts=[1.5])
    without = smooth_centers(samples, 0.35)
    assert with_cut[2].cx == 900.0
    assert without[2].cx < 900.0


@requires_ffmpeg
def test_scan_cuts_finds_the_cuts_in_a_real_file(tmp_path):
    """A full scan, distinct from S9's narrow window.

    Black -> white -> black at 2 s and 4 s. Uses luma extremes deliberately: ffmpeg's scene
    score is luma-based, which S9 documented as a real blind spot for equiluminant cuts.
    """
    src = tmp_path / "cuts.mp4"
    subprocess.run(
        [
            settings.ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x120:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=160x120:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x120:r=10:d=2",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(src),
            "-y",
        ],
        check=True,
        capture_output=True,
    )
    cuts = scene_detect.scan_cuts(src)
    assert cuts, "no cuts detected in a black/white/black source"
    assert any(1.8 <= c <= 2.3 for c in cuts), cuts
    assert any(3.8 <= c <= 4.3 for c in cuts), cuts


def test_scan_cuts_degrades_to_empty_rather_than_raising():
    """An empty list restores the previous behaviour - smoothing through cuts."""
    assert scene_detect.scan_cuts("/nonexistent/none.mp4") == []


# --------------------------------------------------------------------------- #
# V9 - opening transition styles
# --------------------------------------------------------------------------- #
def _zoom_expression(filter_string: str) -> str:
    return filter_string.split("z='", 1)[1].split("'", 1)[0]


def test_the_default_transition_is_byte_identical_to_what_shipped():
    """A changed default would fail the v0.8.0 parity gate, far from this change."""
    before = overlays.zoom_filter(10.0, 30.0, 1080, 1920, ken_burns=False, punch_in=True)
    after = overlays.zoom_filter(
        10.0, 30.0, 1080, 1920, ken_burns=False, punch_in=True, style="punch_in"
    )
    assert before == after
    assert "1.18" in before


def test_each_style_produces_a_different_filter():
    """A style that is read but has no effect is worse than no style."""
    made = {
        style: overlays.zoom_filter(
            10.0, 30.0, 1080, 1920, ken_burns=False, punch_in=True, style=style
        )
        for style in ("punch_in", "zoom_cut", "whip_pan")
    }
    assert len(set(made.values())) == 3, made


def test_zoom_cut_steps_rather_than_easing():
    """The absence of easing is the whole effect: it must read as an edit, not a move.

    Asserted on the expression's *shape* - a constant either side of the settle point - because
    a ramp and a step both contain the same numbers.
    """
    expr = _zoom_expression(
        overlays.zoom_filter(10.0, 30.0, 1080, 1920, punch_in=True, style="zoom_cut")
    )
    assert "1.35" in expr
    assert "on/" not in expr, f"zoom_cut is interpolating, so it is not a cut: {expr}"


def test_punch_in_does_ease():
    expr = _zoom_expression(
        overlays.zoom_filter(10.0, 30.0, 1080, 1920, punch_in=True, style="punch_in")
    )
    assert "on/" in expr, "punch_in must interpolate, or it is a zoom_cut"


def test_whip_pan_moves_horizontally_and_settles():
    """The x expression must depend on time, and must resolve to centred after the transition."""
    made = overlays.zoom_filter(10.0, 30.0, 1080, 1920, punch_in=True, style="whip_pan")
    x_expr = made.split("x='", 1)[1].split("'", 1)[0]
    assert "on" in x_expr, "whip_pan's x is constant, so nothing pans"
    # After the settle window the expression must be the plain centred form, or the crop stays
    # permanently off-centre for the rest of the clip.
    assert x_expr.endswith("iw/2-(iw/zoom/2))")


def test_a_dissolve_contributes_a_fade_and_no_zoom():
    """A dissolve is not a zoom; expressing it as one would mean faking a fade."""
    assert overlays.zoom_filter(10.0, 30.0, 1080, 1920, punch_in=True, style="dissolve") is None
    assert overlays.dissolve_filter("dissolve", 10.0).startswith("fade=t=in")
    assert overlays.dissolve_filter("punch_in", 10.0) is None


def test_a_dissolve_on_a_very_short_clip_does_not_outlast_it():
    made = overlays.dissolve_filter("dissolve", 0.8)
    length = float(made.split("d=", 1)[1])
    assert length <= 0.8 / 4 + 1e-6


def test_ken_burns_still_works_under_every_transition_style():
    """The two are independent controls and must compose."""
    for style in overlays.TRANSITION_STYLES:
        made = overlays.zoom_filter(
            10.0, 30.0, 1080, 1920, ken_burns=True, punch_in=True, style=style
        )
        assert made is not None, style
        assert "0.12*on" in _zoom_expression(made), style


def test_an_unknown_style_falls_back_to_the_shipped_behaviour():
    fallback = overlays.zoom_filter(10.0, 30.0, 1080, 1920, punch_in=True, style="nonsense")
    shipped = overlays.zoom_filter(10.0, 30.0, 1080, 1920, punch_in=True, style="punch_in")
    assert fallback == shipped


# --------------------------------------------------------------------------- #
# V13 - progress bar styles and positions
# --------------------------------------------------------------------------- #
def test_the_default_progress_bar_is_unchanged():
    made = overlays.progress_bar_filter(10.0, 1080, 1920)
    assert made == ("drawbox=x=0:y=ih-12:w='iw*t/10.000':h=12:color=0x22D3EE@0.9:t=fill")


def test_top_position_draws_at_the_top():
    made = overlays.progress_bar_filter(10.0, 1080, 1920, position="top")
    assert "y=0:" in made
    assert "ih-" not in made


def test_the_track_style_draws_the_rail_before_the_fill():
    """Drawn after, the rail would cover the progress it sits behind."""
    made = overlays.progress_bar_filter(10.0, 1080, 1920, style="track")
    boxes = made.split(",")
    assert len(boxes) == 2, made
    assert "w=iw:" in boxes[0], "the first box is not the full-width rail"
    assert "iw*t/" in boxes[1], "the second box is not the time-driven fill"


def test_the_fill_actually_depends_on_time():
    """A bar that is present and constant looks deliberate and conveys nothing."""
    for style in overlays.PROGRESS_STYLES:
        made = overlays.progress_bar_filter(10.0, 1080, 1920, style=style)
        assert "iw*t/" in made, style


def test_a_zero_duration_clip_does_not_divide_by_zero():
    assert "t/0.100" in overlays.progress_bar_filter(0.0, 1080, 1920)


def test_thickness_is_clamped_to_something_drawable():
    assert "h=1:" in overlays.progress_bar_filter(10.0, 1080, 1920, thickness=0)


# --------------------------------------------------------------------------- #
# V11 - letterbox background styles
# --------------------------------------------------------------------------- #
def test_the_default_background_is_the_original_blur():
    chain = fu.background_chain("blur", 1080, 1920)
    assert "boxblur=luma_radius=40:luma_power=1" in chain
    assert "eq=brightness=-0.1" in chain


def test_every_style_ends_with_the_expected_label():
    """The overlay downstream consumes ``[bgb]``; a style that named its output anything else
    would produce an ffmpeg graph error rather than a wrong-looking frame."""
    for style in fu.BACKGROUND_STYLES:
        chain = fu.background_chain(style, 1080, 1920)
        assert chain.endswith("[bgb];"), (style, chain)
        assert chain.startswith("[bg]"), (style, chain)


def test_the_styles_are_actually_different():
    made = {s: fu.background_chain(s, 1080, 1920) for s in fu.BACKGROUND_STYLES}
    assert len(set(made.values())) == len(fu.BACKGROUND_STYLES), made


def test_mirror_flips_and_does_not_blur():
    chain = fu.background_chain("mirror", 1080, 1920)
    assert "hflip" in chain
    assert "boxblur" not in chain


def test_black_derives_nothing_from_the_source():
    """A screen recording's background should be nothing, not a smear of the text."""
    chain = fu.background_chain("black", 1080, 1920)
    assert "boxblur" not in chain and "hflip" not in chain
    assert "color=black" in chain


def test_the_gradient_touches_luma_only():
    """A full RGB gradient would shift the source's colour, not just its brightness."""
    chain = fu.background_chain("gradient", 1080, 1920)
    assert "lum=" in chain
    assert "cb='p(X,Y)'" in chain and "cr='p(X,Y)'" in chain


def test_an_unknown_background_style_falls_back_to_blur():
    assert fu.background_chain("nonsense", 1080, 1920) == fu.background_chain("blur", 1080, 1920)


def test_the_gradient_declares_the_filter_it_needs():
    """``geq`` is GPL-only, so a build without ``--enable-gpl`` has every other filter here and
    not that one. Naming the dependency is what lets the style degrade instead of failing."""
    assert fu.BACKGROUND_STYLE_FILTERS["gradient"] == "geq"


def test_a_style_needing_no_extra_filter_is_always_available():
    for style in ("blur", "mirror", "black", "color"):
        assert fu.background_style_available(style) is True
        assert fu.resolve_background_style(style) == style


def test_an_unavailable_filter_degrades_the_style_rather_than_the_clip(monkeypatch):
    monkeypatch.setattr(fu, "background_style_available", lambda style: False)
    assert fu.resolve_background_style("gradient") == "blur"


def test_the_availability_check_really_consults_the_capability_report(monkeypatch):
    """The assertion that caught a real bug in my own first version.

    It called a ``probe_capability`` function that does not exist. The ``except Exception``
    swallowed the ``ImportError`` and returned ``True``, so the check *always* said "available"
    and never probed anything - and it looked correct, because ``geq`` genuinely is available
    here. A test asserting only ``is True`` would have passed on the broken version too.
    """
    import worker.engines.capabilities as caps

    calls: list = []
    real = caps.get_report

    def recording_get_report(*args, **kwargs):
        report = real(*args, **kwargs)
        original_status = report.status

        def status(capability_id):
            calls.append(capability_id)
            return original_status(capability_id)

        monkeypatch.setattr(report, "status", status, raising=False)
        return report

    monkeypatch.setattr(caps, "get_report", recording_get_report)
    fu.background_style_available("gradient")
    assert calls == ["ffmpeg_filter:geq"], f"the report was not consulted: {calls}"


def test_a_probe_failure_keeps_the_requested_style(monkeypatch):
    """A probe that cannot run is not evidence the filter is missing.

    Treating it as such would silently downgrade the look on every host where the capability
    system itself is unavailable - a degradation nobody asked for and nobody would see reported.
    """
    import worker.engines.capabilities as caps

    def explode(*_args, **_kwargs):
        raise RuntimeError("probe unavailable")

    monkeypatch.setattr(caps, "get_report", explode)
    assert fu.background_style_available("gradient") is True


@requires_ffmpeg
@pytest.mark.parametrize("style", list(fu.BACKGROUND_STYLES))
def test_every_background_style_renders(style, tmp_path):
    """Each chain has to be a graph ffmpeg accepts.

    A malformed filtergraph is the failure mode here, and it cannot be caught by string
    assertions - a plausible-looking chain with an unconnected pad fails only when run.
    """
    src = tmp_path / "in.mp4"
    subprocess.run(
        [
            settings.ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x240:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=300:duration=1:sample_rate=48000",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(src),
            "-y",
        ],
        check=True,
        capture_output=True,
    )
    out = tmp_path / f"out_{style}.mp4"
    # Renders whether or not the style's optional filter exists on this host: an unavailable
    # filter degrades to blur, so the assertion is about the *clip* being produced rather than
    # about which background it got. Gating the test on availability instead would make it skip,
    # and CI treats a skip as a failure - correctly, since a guard that stops running is worse
    # than no guard.
    fu.reformat_aspect(src, out, aspect="9:16", mode="crop_blur", background=style)
    assert out.is_file() and out.stat().st_size > 0
    info = fu.probe(out)
    assert (info.width, info.height) == fu.aspect_size("9:16")


# --------------------------------------------------------------------------- #
# O5 / O9 - output resolution
# --------------------------------------------------------------------------- #
def test_the_default_resolution_is_the_shipped_one():
    assert fu.aspect_size("9:16") == (1080, 1920)
    assert fu.aspect_size("16:9") == (1920, 1080)
    assert settings.output_short_side == 1080


@pytest.mark.parametrize(
    "short,expected", [(720, (720, 1280)), (1440, (1440, 2560)), (2160, (2160, 3840))]
)
def test_other_resolutions_scale_the_long_side_with_the_aspect(short, expected):
    assert fu.aspect_size("9:16", short) == expected


def test_every_offered_resolution_yields_even_dimensions():
    """libx264's 4:2:0 subsampling requires it, and an odd dimension fails the encode."""
    for short in fu.OUTPUT_SHORT_SIDES:
        for aspect in fu.ASPECT_PRESETS:
            w, h = fu.aspect_size(aspect, short)
            assert w % 2 == 0 and h % 2 == 0, (aspect, short, w, h)


def test_an_unrecognised_resolution_falls_back_rather_than_rounding():
    """A resolution nobody chose is worse than the documented default."""
    assert fu.aspect_size("9:16", 999) == fu.aspect_size("9:16", 1080)


def test_the_setting_is_honoured(monkeypatch):
    monkeypatch.setattr(settings, "output_short_side", 720)
    assert fu.aspect_size("9:16") == (720, 1280)


def test_an_unknown_aspect_still_raises():
    with pytest.raises(ValueError):
        fu.aspect_size("3:2")


# --------------------------------------------------------------------------- #
# O11 - sidecar subtitles
# --------------------------------------------------------------------------- #
def test_srt_and_vtt_use_different_timestamp_separators():
    """Not cosmetic: an SRT with a full stop is rejected by some parsers and silently
    mis-timed by others, and VTT requires the full stop."""
    assert subtitle_export.format_timestamp(3661.5) == "01:01:01,500"
    assert subtitle_export.format_timestamp(3661.5, vtt=True) == "01:01:01.500"


def test_the_hours_field_is_always_present():
    """``MM:SS.mmm`` is legal VTT but not legal SRT, so one shape for both would produce an
    SRT most players accept and a few quietly drop."""
    assert subtitle_export.format_timestamp(1.25) == "00:00:01,250"


def test_a_negative_timestamp_is_clamped_not_rendered_negative():
    assert subtitle_export.format_timestamp(-5.0) == "00:00:00,000"


def test_vtt_starts_with_the_mandatory_header():
    """Without it a browser rejects the file outright and shows no captions and no error."""
    out = subtitle_export.render_vtt([(0.0, 1.0, "hello")])
    assert out.startswith("WEBVTT\n")
    assert "\n\n" in out, "the blank line after the header is mandatory"


def test_vtt_escapes_markup_but_srt_does_not():
    """VTT is parsed as markup, so a line containing '5 < 10' truncates at the '<'."""
    cues = [(0.0, 1.0, "5 < 10 & rising")]
    assert "&lt;" in subtitle_export.render_vtt(cues)
    assert "&amp;" in subtitle_export.render_vtt(cues)
    assert "<" in subtitle_export.render_srt(cues), "SRT must not be HTML-escaped"


def test_vtt_escaping_does_not_double_escape():
    """``&`` must be replaced first, or the viewer sees ``&amp;lt;``."""
    out = subtitle_export.render_vtt([(0.0, 1.0, "<b>")])
    assert "&amp;lt;" not in out
    assert "&lt;b&gt;" in out


def test_srt_cues_are_numbered_from_one():
    out = subtitle_export.render_srt([(0.0, 1.0, "a"), (1.0, 2.0, "b")])
    assert out.startswith("1\n")
    assert "\n2\n" in out


def test_cues_break_on_a_pause():
    words = [Word(0.0, 0.4, "one"), Word(0.5, 0.9, "two"), Word(5.0, 5.4, "later")]
    cues = subtitle_export.cues_from_words(words)
    assert len(cues) == 2, cues
    assert cues[0][2] == "one two"
    assert cues[1][2] == "later"


def test_cues_break_on_word_count():
    words = [Word(i * 0.3, i * 0.3 + 0.2, f"w{i}") for i in range(20)]
    cues = subtitle_export.cues_from_words(words, max_words=8)
    assert all(len(text.split()) <= 8 for _, _, text in cues)
    assert len(cues) >= 3


def test_cues_break_on_elapsed_time_even_without_a_pause():
    """One long unbroken sentence must not become a cue that outstays the speech."""
    words = [Word(i * 0.2, i * 0.2 + 0.15, "w") for i in range(100)]
    cues = subtitle_export.cues_from_words(words, max_words=1000, max_gap=99.0, max_duration=5.0)
    assert all((end - start) <= 5.2 for start, end, _ in cues), cues


def test_sidecar_cues_are_longer_than_burned_cues():
    """Different jobs: burned captions are read in a glance at full width, a sidecar is read as
    subtitles in a player's own small type, where three-word cues flicker once a second."""
    from worker import captions

    words = [Word(i * 0.3, i * 0.3 + 0.2, f"w{i}") for i in range(12)]
    sidecar = subtitle_export.cues_from_words(words)
    burned = captions.words_to_cues(words)
    assert len(sidecar) < len(burned)


def test_a_word_with_broken_timing_is_skipped_not_fatal():
    class Bad:
        start, end, text = "nope", None, "x"

    words = [Bad(), Word(0.0, 0.4, "good"), Word(float("nan"), 1.0, "nan")]
    cues = subtitle_export.cues_from_words(words)
    assert cues == [(0.0, 0.4, "good")]


def test_writing_sidecars_produces_both_files(tmp_path):
    words = [Word(0.0, 0.4, "hello"), Word(0.5, 0.9, "there")]
    written = subtitle_export.write_sidecars(words, tmp_path / "clip_01")
    assert sorted(p.suffix for p in written) == [".srt", ".vtt"]
    assert (tmp_path / "clip_01.srt").read_text().startswith("1\n")
    assert (tmp_path / "clip_01.vtt").read_text().startswith("WEBVTT")


def test_no_words_writes_nothing_and_does_not_raise(tmp_path):
    """A clip over music has nothing to export, and that is not a failure."""
    assert subtitle_export.write_sidecars([], tmp_path / "clip_01") == []
    assert list(tmp_path.iterdir()) == []


def test_an_unknown_format_is_ignored(tmp_path):
    written = subtitle_export.write_sidecars(
        [Word(0.0, 0.4, "hi there friend")],
        tmp_path / "c",
        formats=("srt", "ass"),
    )
    assert [p.suffix for p in written] == [".srt"]


# --------------------------------------------------------------------------- #
# V17 - thumbnail frame selection
# --------------------------------------------------------------------------- #
def test_a_sharper_candidate_wins(monkeypatch, tmp_path):
    """Sharpness is the component that matters: motion blur is unrecoverable."""

    # The sampler has to be stubbed as well as the scorer: without it the real ffmpeg call fails
    # on a non-existent source, every candidate is skipped, and the function correctly returns
    # the fallback - so the test would pass for the wrong reason if it asserted that.
    def fake_thumbnail(source, dest, at=0.0, width=0):
        from pathlib import Path as _P

        _P(dest).write_bytes(b"stub")
        return dest

    monkeypatch.setattr(fu, "generate_thumbnail", fake_thumbnail)
    scores = {"3.000": 0.1, "5.000": 0.2, "7.000": 0.9}

    def scorer(path):
        for marker, score in scores.items():
            if marker in str(path):
                return score
        return 0.0

    assert thumbnail.best_thumbnail_time("x.mp4", 10.0, scorer=scorer) == pytest.approx(7.0)


def test_no_scoring_available_falls_back_to_the_old_rule():
    """PIL is optional throughout this codebase, so its absence must not be an error."""
    assert thumbnail.best_thumbnail_time("x.mp4", 10.0, scorer=lambda p: None) == 1.0


def test_a_very_short_clip_skips_the_extra_decodes():
    """Below a couple of seconds the candidates land within a few frames of each other."""
    calls = []
    thumbnail.best_thumbnail_time("x.mp4", 1.0, scorer=lambda p: calls.append(p) or 1.0)
    assert calls == []


def test_the_fallback_matches_the_previous_behaviour_exactly():
    for duration in (0.5, 1.0, 1.5, 3.0, 60.0):
        expected = min(1.0, duration / 2.0)
        assert thumbnail.choose_thumbnail_time.__doc__  # sanity: it is the documented seam
        assert thumbnail.best_thumbnail_time("x.mp4", duration, scorer=lambda p: None) == expected


def test_the_feature_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "smart_thumbnail", False)
    calls = []
    monkeypatch.setattr(fu, "generate_thumbnail", lambda *a, **k: calls.append(a))
    assert thumbnail.choose_thumbnail_time("x.mp4", 30.0) == 1.0
    assert calls == [], "candidates were sampled with the feature disabled"


def test_a_junk_duration_does_not_raise():
    assert thumbnail.best_thumbnail_time("x.mp4", "nonsense", scorer=lambda p: 1.0) == 0.0


def test_candidates_avoid_the_clip_edges():
    """The first fifth carries transition artefacts and the last is often a trailing pause."""
    assert all(0.2 < f < 0.8 for f in thumbnail.CANDIDATE_FRACTIONS)


@requires_ffmpeg
def test_a_blurred_frame_loses_to_a_detailed_one(tmp_path):
    """The scorer against real images, not a stub - the point is that it ranks correctly."""
    pytest.importorskip("PIL")
    sharp = tmp_path / "sharp.jpg"
    blurred = tmp_path / "blurred.jpg"
    for dest, extra in ((sharp, "null"), (blurred, "boxblur=luma_radius=20:luma_power=2")):
        subprocess.run(
            [
                settings.ffmpeg_binary,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=320x240:d=1",
                "-frames:v",
                "1",
                "-vf",
                extra,
                str(dest),
                "-y",
            ],
            check=True,
            capture_output=True,
        )
    sharp_score = thumbnail._score_frame(sharp)
    blurred_score = thumbnail._score_frame(blurred)
    assert sharp_score is not None and blurred_score is not None
    assert sharp_score > blurred_score, (sharp_score, blurred_score)


@requires_ffmpeg
def test_a_black_frame_loses_to_an_exposed_one(tmp_path):
    pytest.importorskip("PIL")
    made = {}
    for name, source in (
        ("black", "color=c=black:s=320x240:d=1"),
        ("white", "color=c=white:s=320x240:d=1"),
        ("mid", "testsrc2=s=320x240:d=1"),
    ):
        path = tmp_path / f"{name}.jpg"
        subprocess.run(
            [
                settings.ffmpeg_binary,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                source,
                "-frames:v",
                "1",
                str(path),
                "-y",
            ],
            check=True,
            capture_output=True,
        )
        made[name] = thumbnail._score_frame(path)
    assert made["mid"] > made["black"], made
    # Blown out must be penalised as much as dark, not rewarded for being bright.
    assert made["mid"] > made["white"], made
