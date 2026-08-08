"""Caption polish: C6/C16 wrapping, C9 word pill, C14 preset library, C17 dual stroke,
C18 preview render, C20 auto-contrast.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tests.conftest import FFMPEG, requires_ffmpeg
from worker import caption_contrast as cc
from worker import caption_preview as cp
from worker import captions as cap
from worker import text_metrics as tm
from worker.effects.caption_presets import BUILTIN_PRESETS, CaptionPreset, resolve_preset


class _W:
    """A minimal word with the attributes the caption renderer reads."""

    def __init__(self, text, start, end, probability=1.0):
        self.text = text
        self.start = start
        self.end = end
        self.probability = probability


def _words(text: str, step: float = 0.4) -> list[_W]:
    return [
        _W(word, index * step, index * step + step * 0.9) for index, word in enumerate(text.split())
    ]


# --------------------------------------------------------------------------- #
# C6 - measured text width
# --------------------------------------------------------------------------- #


def test_c6_characters_are_not_equal_width():
    """The reason a character budget is the wrong tool for these fonts.

    In Anton a ``W`` is roughly three times the advance of an ``i``, so a 24-character budget is a
    comfortable line of one and an overflowing line of the other.
    """
    metrics = tm.metrics_for_font("Anton")
    assert metrics is not None, "the vendored Anton could not be measured"
    narrow = metrics.text_width("i" * 24, 96)
    wide = metrics.text_width("W" * 24, 96)
    assert wide > narrow * 2


def test_c6_width_scales_with_font_size():
    metrics = tm.metrics_for_font("Anton")
    assert metrics.text_width("hello", 200) == pytest.approx(
        metrics.text_width("hello", 100) * 2, rel=1e-6
    )


def test_c6_every_vendored_preset_font_can_be_measured():
    """A preset whose font cannot be measured silently falls back to a character budget."""
    unmeasurable = [
        preset.font
        for preset in BUILTIN_PRESETS.values()
        if tm.metrics_for_font(preset.font) is None
    ]
    assert unmeasurable == []


def test_c6_an_unknown_font_falls_back_rather_than_failing():
    """A wrap that is approximately right beats no wrap."""
    assert tm.metrics_for_font("No Such Face") is None
    lines = tm.wrap_text(
        "one two three four five six seven eight nine ten",
        font="No Such Face",
        font_size=96,
        max_width_px=900,
        max_lines=3,
    )
    assert len(lines) > 1


def test_c6_wrapping_respects_the_width_budget():
    text = "This one small change completely transformed my entire workflow"
    for line in tm.wrap_text(text, font="Anton", font_size=96, max_width_px=900, max_lines=4):
        assert tm.metrics_for_font("Anton").text_width(line, 96) <= 900


def test_c6_words_are_never_split():
    """A hyphenated break mid-word is more distracting than an uneven line."""
    text = "extraordinarily complicated arrangements"
    lines = tm.wrap_text(text, font="Anton", font_size=96, max_width_px=400, max_lines=5)
    for line in lines:
        for word in line.split():
            assert word in text.split()


def test_c6_a_condensed_face_fits_more_per_line():
    """The property that makes measurement worth doing at all."""
    text = "This one small change transformed my entire podcast workflow forever"
    condensed = tm.wrap_word_groups(text.split(), font="Anton", font_size=96, max_width_px=900)
    wide = tm.wrap_word_groups(text.split(), font="Archivo Black", font_size=96, max_width_px=900)
    assert len(condensed[0]) > len(wide[0])


def test_c6_letter_spacing_and_scale_x_widen_the_measurement():
    """Both change the drawn width, so a wrap that ignored them would overflow."""
    words = "one two three four five six".split()
    plain = tm.wrap_word_groups(words, font="Anton", font_size=96, max_width_px=600)
    spaced = tm.wrap_word_groups(words, font="Anton", font_size=96, max_width_px=600, spacing=12)
    stretched = tm.wrap_word_groups(
        words, font="Anton", font_size=96, max_width_px=600, scale_x=160
    )
    assert len(spaced[0]) <= len(plain[0])
    assert len(stretched[0]) <= len(plain[0])


def test_c6_word_groups_never_drop_a_word():
    """A silently truncated caption is worse than one line more than requested."""
    words = ("word " * 40).split()
    groups = tm.wrap_word_groups(words, font="Anton", font_size=96, max_width_px=500, max_lines=2)
    assert sum(len(group) for group in groups) == len(words)


# --------------------------------------------------------------------------- #
# C6/C16 - it reaches the rendered ASS
# --------------------------------------------------------------------------- #


def test_c6_the_ass_carries_real_line_breaks(tmp_path):
    """`WrapStyle: 2` means libass breaks *only* at an explicit \\N, and nothing inserted one."""
    preset, _ = resolve_preset("karaoke")
    words = _words("This one small change completely transformed my entire workflow forever")
    fit = cap.TextFit.for_preset(preset, video_width=1080)
    cues = cap.words_to_cues(words, max_words=12, fit=fit)
    dest = cap.build_ass(cues, tmp_path / "c.ass", preset=preset, clip_duration=6.0)
    body = dest.read_text(encoding="utf-8")
    assert "\\N" in body, "no line break was emitted"


def test_c6_a_short_cue_gets_no_line_break(tmp_path):
    """The wrap is a budget, not a target: one line stays one line."""
    preset, _ = resolve_preset("karaoke")
    cues = cap.words_to_cues(
        _words("two words"), max_words=6, fit=cap.TextFit.for_preset(preset, video_width=1080)
    )
    dest = cap.build_ass(cues, tmp_path / "c.ass", preset=preset, clip_duration=2.0)
    dialogue = [
        line
        for line in dest.read_text(encoding="utf-8").splitlines()
        if line.startswith("Dialogue")
    ]
    assert dialogue
    assert all("\\N" not in line for line in dialogue)


def test_c6_break_positions_are_measured_from_plain_words_not_tagged_spans(tmp_path):
    """A single `{\\kf36}` is longer than the word it decorates.

    Measured on this fixture: the plain text is 1237 px against a 929 px budget, so it wraps to two
    lines. The same words as *tagged spans* measure 2807 px and wrap to four - one word per line.
    That gap is the discriminator, so the assertion is on the resulting line count rather than on
    "no break at all", which was the first version of this test and merely encoded a wrong guess
    about the fixture's width.
    """
    preset, _ = resolve_preset("karaoke")  # karaoke_fill emits a \kf tag per word
    text = "alpha beta gamma delta"
    words = _words(text)
    fit = cap.TextFit.for_preset(preset, video_width=1080)
    cues = cap.words_to_cues(words, max_words=12, fit=fit)
    dest = cap.build_ass(cues, tmp_path / "c.ass", preset=preset, clip_duration=3.0)
    dialogue = next(
        line
        for line in dest.read_text(encoding="utf-8").splitlines()
        if line.startswith("Dialogue")
    )

    plain_lines = len(
        tm.wrap_word_groups(
            text.split(),
            font=preset.font,
            font_size=preset.font_size,
            max_width_px=fit.max_width_px,
        )
    )
    tagged = [f"{{\\kf36}}{word}" for word in text.split()]
    tagged_lines = len(
        tm.wrap_word_groups(
            tagged,
            font=preset.font,
            font_size=preset.font_size,
            max_width_px=fit.max_width_px,
        )
    )
    assert tagged_lines > plain_lines, "fixture does not distinguish the two measurements"
    assert dialogue.count("\\N") == plain_lines - 1


def test_c16_cue_length_follows_the_font(tmp_path):
    """A word count cannot decide this: the same three words are a comfortable line in one face
    and an overflowing one in another."""
    words = _words("This one small change transformed my entire podcast workflow forever")
    condensed, _ = resolve_preset("hormozi")  # Anton
    wide, _ = resolve_preset("boxed")  # Archivo Black
    condensed_cues = cap.words_to_cues(
        words, max_words=12, fit=cap.TextFit.for_preset(condensed, video_width=1080)
    )
    wide_cues = cap.words_to_cues(
        words, max_words=12, fit=cap.TextFit.for_preset(wide, video_width=1080)
    )
    assert len(condensed_cues[0].words) > len(wide_cues[0].words)


@pytest.mark.parametrize("name", sorted(BUILTIN_PRESETS))
def test_c16_a_fitted_cue_never_exceeds_its_line_budget(name):
    """The invariant that makes C6 and C16 one feature rather than two.

    ``wrap_word_groups`` deliberately never drops a word, so a cue holding more text than fits will
    simply produce more lines than ``max_lines`` - a caption overflowing the frame. Keeping cues
    short enough is the *cue builder's* job, using the same measurement.

    Measured on the karaoke preset with an 11-word sample: grouping with the fit gives cues needing
    at most 2 lines against a budget of 2, and grouping without it gives a cue needing **4**.
    """
    preset = BUILTIN_PRESETS[name]
    fit = cap.TextFit.for_preset(preset, video_width=1080)
    words = _words(
        "This one small change completely transformed my entire podcast workflow forever today"
    )
    for cue in cap.words_to_cues(words, max_words=12, fit=fit):
        text = " ".join(word.text for word in cue.words)
        needed = len(
            tm.wrap_word_groups(
                text.split(),
                font=fit.font,
                font_size=fit.font_size,
                max_width_px=fit.max_width_px,
                spacing=fit.spacing,
                scale_x=fit.scale_x,
            )
        )
        assert needed <= fit.max_lines, f"{name}: {text!r} needs {needed} lines"


def test_c16_without_a_fit_a_cue_can_overflow_its_budget():
    """Records *why* the fit is threaded through cue building rather than applied only at render.

    This is the pre-C6 behaviour, kept as the default for every existing caller - so it is worth
    pinning that it really does overflow, otherwise the fitted test above proves nothing.
    """
    preset, _ = resolve_preset("karaoke")
    fit = cap.TextFit.for_preset(preset, video_width=1080)
    words = _words(
        "This one small change completely transformed my entire podcast workflow forever today"
    )
    worst = 0
    for cue in cap.words_to_cues(words, max_words=12):
        text = " ".join(word.text for word in cue.words)
        worst = max(
            worst,
            len(
                tm.wrap_word_groups(
                    text.split(),
                    font=fit.font,
                    font_size=fit.font_size,
                    max_width_px=fit.max_width_px,
                )
            ),
        )
    assert worst > fit.max_lines


def test_c16_max_lines_is_on_the_preset_and_round_trips():
    preset = CaptionPreset("x", max_lines=3)
    assert CaptionPreset.from_dict(preset.to_dict()).max_lines == 3
    assert BUILTIN_PRESETS["headline"].max_lines == 3


def test_c16_no_fit_means_the_previous_behaviour():
    """Every pre-C6 caller passes no fit and must be unaffected."""
    words = _words("one two three four five six seven eight")
    assert len(cap.words_to_cues(words, max_words=3)) == 3


# --------------------------------------------------------------------------- #
# C9 - the per-word pill
# --------------------------------------------------------------------------- #


def test_c9_is_off_by_default():
    preset, _ = resolve_preset("karaoke")
    assert preset.word_pill == 0.0
    span = cap.build_word_span(_W("money", 0.0, 0.4), preset, False, cue_start=0.0)
    assert "\\3c" not in span


def test_c9_draws_a_border_pill_around_the_word():
    preset = replace(BUILTIN_PRESETS["karaoke"], word_pill=0.5, word_pill_color="&H0000FF00")
    span = cap.build_word_span(_W("money", 0.0, 0.4), preset, False, cue_start=0.0)
    assert "\\bord" in span
    assert "\\3c&H0000FF00&" in span


def test_c9_restores_the_style_border_afterwards():
    """An ASS override persists to the end of the event, so a pill left open would swallow the
    rest of the line."""
    preset = replace(BUILTIN_PRESETS["karaoke"], word_pill=0.5)
    span = cap.build_word_span(_W("money", 0.0, 0.4), preset, False, cue_start=0.0)
    assert span.rstrip().endswith("}")
    assert span.count("\\bord") == 2
    assert f"\\bord{preset.outline}" in span


def test_c9_defaults_to_the_presets_highlight_colour():
    preset = replace(BUILTIN_PRESETS["karaoke"], word_pill=0.5, word_pill_color="")
    span = cap.build_word_span(_W("money", 0.0, 0.4), preset, False, cue_start=0.0)
    assert preset.colors.highlight in span


def test_c9_thickness_scales_with_the_font_size():
    """One preset has to work at every output resolution (O9 renders 720 to 2160)."""
    small = replace(BUILTIN_PRESETS["karaoke"], word_pill=0.5, font_size=48)
    large = replace(BUILTIN_PRESETS["karaoke"], word_pill=0.5, font_size=192)

    def border(preset):
        span = cap.build_word_span(_W("x", 0.0, 0.4), preset, False, cue_start=0.0)
        return int(span.split("\\bord", 1)[1].split("\\", 1)[0])

    assert border(large) > border(small)


def test_c9_thickness_is_capped():
    """Past a certain thickness adjacent words' pills merge into a bar."""
    absurd = replace(BUILTIN_PRESETS["karaoke"], word_pill=99.0, font_size=400)
    span = cap.build_word_span(_W("x", 0.0, 0.4), absurd, False, cue_start=0.0)
    assert int(span.split("\\bord", 1)[1].split("\\", 1)[0]) <= 40


def test_c9_the_highlight_still_wraps_the_plain_span():
    """The documented contract, which the pill must not break.

    The pill is applied *inside* the highlight so `plain in highlighted` stays literally true - a
    contract enforced by substring has to stay syntactically true, not merely true in spirit.
    """
    preset = BUILTIN_PRESETS["pill"]
    word = _W("the", 0.0, 0.4)
    plain = cap.build_word_span(word, preset, False, cue_start=0.0)
    highlighted = cap.build_word_span(word, preset, True, cue_start=0.0)
    assert plain in highlighted


# --------------------------------------------------------------------------- #
# C17 - the dual stroke
# --------------------------------------------------------------------------- #


def test_c17_is_off_by_default():
    preset, _ = resolve_preset("karaoke")
    assert preset.outline2 == 0
    line = cap._preset_style_line(preset, "Anton", 96, 2, 220)
    # The shadow slot keeps the preset's own shadow when no second stroke is asked for.
    assert line.split(",")[17] == str(preset.shadow)


def test_c17_repurposes_the_shadow_slot_as_an_outer_stroke():
    preset = replace(
        BUILTIN_PRESETS["karaoke"], outline=4, outline2=12, outline2_color="&H00FF00FF"
    )
    fields = cap._preset_style_line(preset, "Anton", 96, 2, 220).split(",")
    assert fields[16] == "4", "the inner stroke should be unchanged"
    assert fields[17] == "12", "the outer stroke should occupy the shadow slot"
    assert "&H00FF00FF" in ",".join(fields), "the outer stroke colour is not applied"


def test_c17_the_sticker_preset_uses_both_strokes():
    """A single stroke is just a heavy outline; the two-tone edge needs both."""
    sticker = BUILTIN_PRESETS["sticker"]
    assert sticker.outline > 0
    assert sticker.outline2 > sticker.outline


# --------------------------------------------------------------------------- #
# C14 - the preset library
# --------------------------------------------------------------------------- #


def test_c14_the_library_is_expanded():
    assert len(BUILTIN_PRESETS) >= 14


def test_c14_the_six_original_presets_survive():
    """New presets must be additions, not replacements: an existing name changing meaning would
    silently restyle every job configured with it."""
    for name in ("karaoke", "boxed", "minimal", "pop", "typewriter", "hormozi"):
        assert name in BUILTIN_PRESETS


def test_c14_every_preset_names_a_vendored_font():
    """A font that is not installed is silently substituted at render time (C1)."""
    for name, preset in BUILTIN_PRESETS.items():
        assert cap.font_available(preset.font), f"{name} names {preset.font!r}"


def test_c14_every_preset_round_trips():
    for name, preset in BUILTIN_PRESETS.items():
        assert CaptionPreset.from_dict(preset.to_dict()) == preset, name


def test_c14_every_preset_names_itself():
    for name, preset in BUILTIN_PRESETS.items():
        assert preset.name == name


def test_c14_the_new_presets_are_genuinely_different_treatments():
    """A preset whose only difference is a hue is a colour picker pretending to be a style."""
    signatures = {
        (
            preset.animation,
            preset.font,
            preset.uppercase,
            preset.border_style,
            bool(preset.word_pill),
            bool(preset.outline2),
            preset.max_lines,
        )
        for preset in BUILTIN_PRESETS.values()
    }
    # Allowing for the two pill presets differing only in colour, which is deliberate.
    assert len(signatures) >= len(BUILTIN_PRESETS) - 2


@pytest.mark.parametrize("name", sorted(BUILTIN_PRESETS))
def test_c14_every_preset_renders_valid_ass(tmp_path, name):
    preset = BUILTIN_PRESETS[name]
    cues = cap.words_to_cues(
        _words("This one change made everything click"),
        max_words=6,
        fit=cap.TextFit.for_preset(preset, video_width=1080),
    )
    dest = cap.build_ass(
        cues, tmp_path / f"{name}.ass", preset=preset, keyword_indices={2}, clip_duration=3.0
    )
    body = dest.read_text(encoding="utf-8")
    assert "[V4+ Styles]" in body and "[Events]" in body
    assert any(line.startswith("Dialogue") for line in body.splitlines())


# --------------------------------------------------------------------------- #
# C18 - the preview render
# --------------------------------------------------------------------------- #


def test_c18_sample_words_are_evenly_spaced():
    """A preview is a comparison between presets; irregular timings would make two presets look
    different for a reason unrelated to either."""
    words = cp.sample_words("one two three four", duration=2.0)
    assert len(words) == 4
    gaps = [words[i + 1].start - words[i].start for i in range(len(words) - 1)]
    assert max(gaps) - min(gaps) < 1e-6
    assert words[-1].end <= 2.0


def test_c18_empty_text_produces_no_words():
    assert cp.sample_words("  ", duration=2.0) == []


@requires_ffmpeg
@pytest.mark.parametrize("name", ["karaoke", "pill", "sticker", "subtitle"])
def test_c18_a_preview_actually_draws_the_caption(tmp_path, name):
    """Asserted on pixels: ffmpeg exiting 0 on a grey rectangle is not a preview."""
    out = cp.render_preview(name, tmp_path / f"{name}.mp4")
    assert out.is_file() and out.stat().st_size > 1000

    preset = BUILTIN_PRESETS[name]
    band_y = 760 if preset.position == "center" else 1400
    raw = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "1.2",
            "-i",
            str(out),
            "-frames:v",
            "1",
            "-vf",
            f"crop=1080:400:0:{band_y},format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    # The background is mid-grey, so anything far from it is drawn text.
    ink = sum(1 for value in raw if value < 100 or value > 200) / max(1, len(raw))
    assert ink > 0.01, f"{name} drew no caption in its own position band"


@requires_ffmpeg
def test_c18_the_intermediate_ass_is_cleaned_up(tmp_path):
    out = cp.render_preview("karaoke", tmp_path / "p.mp4")
    assert not out.with_suffix(".ass").exists()


@requires_ffmpeg
def test_c18_a_serialised_preset_can_be_previewed(tmp_path):
    """So a panel can preview an *edited* style, not just a shipped one."""
    edited = BUILTIN_PRESETS["karaoke"].to_dict()
    edited["font"] = "Bangers"
    edited["uppercase"] = True
    out = cp.render_preview(edited, tmp_path / "edited.mp4")
    assert out.is_file()


def _text_rows(video: Path, at: float, band: tuple[int, int]) -> int:
    """How many distinct horizontal rows of text appear in ``band`` at time ``at``.

    Counts runs of rows containing ink, over the mid-grey preview background. This is how a
    *wrapped* caption is distinguished from an unwrapped one in the rendered pixels: two lines paint
    two separated rows, one line paints one.
    """
    top, height = band
    raw = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{at}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"crop=1080:{height}:0:{top},format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    width = 1080
    rows = 0
    inside = False
    for index in range(height):
        row = raw[index * width : (index + 1) * width]
        inked = sum(1 for value in row if value < 100 or value > 200) > width * 0.02
        if inked and not inside:
            rows += 1
        inside = inked
    return rows


@requires_ffmpeg
def test_c18_the_preview_wraps_like_a_real_render(tmp_path):
    """Previewing an unwrapped caption would misrepresent exactly what C6 fixes.

    Asserted on rendered rows of text, not on the file existing - which was the first version of
    this test and passed happily with the fit removed from the preview path entirely.
    """
    long_text = "This one small change completely transformed my entire podcast workflow forever"
    wrapped = cp.render_preview("karaoke", tmp_path / "wrapped.mp4", text=long_text)
    short = cp.render_preview("karaoke", tmp_path / "short.mp4", text="Two words")

    # The caption sits at the bottom; sample generously upward to catch a second line.
    band = (1200, 700)
    assert _text_rows(wrapped, 1.2, band) >= 2, "the long caption was not wrapped"
    assert _text_rows(short, 1.2, band) == 1


def test_c18_an_unknown_preset_falls_back_rather_than_failing(tmp_path):
    from worker.effects.caption_presets import load_preset

    preset, substituted = load_preset("no-such-preset")
    assert substituted is True
    assert preset.name == "karaoke"


# --------------------------------------------------------------------------- #
# C20 - auto-contrast
# --------------------------------------------------------------------------- #


def test_c20_is_off_by_default():
    from config import settings

    assert settings.caption_auto_contrast is False
    preset, _ = resolve_preset("karaoke")
    adapted, markers = cc.choose_for_clip(
        "/nonexistent.mp4", preset, duration=2.0, video_width=1080, video_height=1920
    )
    assert adapted is preset
    assert markers == []


def test_c20_no_sample_means_no_change():
    """An auto-contrast feature that failed a render would be a bad trade."""
    preset, _ = resolve_preset("karaoke")
    adapted, markers = cc.apply_auto_contrast(preset, None)
    assert adapted is preset
    assert markers == []


def test_c20_the_band_follows_the_position():
    top = cc.caption_band("top", 1080, 1920)
    centre = cc.caption_band("center", 1080, 1920)
    bottom = cc.caption_band("bottom", 1080, 1920)
    assert top[3] < centre[3] < bottom[3]
    for band in (top, centre, bottom):
        assert band[3] >= 0
        assert band[3] + band[1] <= 1920


def test_c20_the_band_grows_with_the_font_and_line_budget():
    """A three-line headline at 104 px covers a very different part of the frame from a one-line
    subtitle at 72, and sampling the wrong band is how this picks the wrong colour."""
    small = cc.caption_band("bottom", 1080, 1920, font_size=72, max_lines=1)
    large = cc.caption_band("bottom", 1080, 1920, font_size=104, max_lines=3)
    assert large[1] > small[1]


def test_c20_a_dark_background_gets_a_light_outline():
    preset, _ = resolve_preset("karaoke")
    adapted, markers = cc.apply_auto_contrast(preset, cc.BackgroundSample(10.0, 3))
    assert adapted.colors.outline == cc.LIGHT_OUTLINE
    assert markers == ["auto_contrast:light"]


def test_c20_a_bright_background_keeps_the_dark_outline_and_records_nothing():
    """A marker for a value that did not change is noise; `effects_applied` lists decisions."""
    preset, _ = resolve_preset("karaoke")
    adapted, markers = cc.apply_auto_contrast(preset, cc.BackgroundSample(240.0, 3))
    assert adapted.colors.outline == cc.DARK_OUTLINE
    assert markers == []


def test_c20_never_changes_the_fill_colour():
    """The fill is a brand decision (U6); recolouring it because a shot was bright would overrule
    the one thing the creator chose."""
    preset = replace(
        BUILTIN_PRESETS["karaoke"],
        colors=replace(BUILTIN_PRESETS["karaoke"].colors, primary="&H000000FF"),
    )
    for luma in (5.0, 128.0, 250.0):
        adapted, _ = cc.apply_auto_contrast(preset, cc.BackgroundSample(luma, 3))
        assert adapted.colors.primary == "&H000000FF"
        assert adapted.colors.highlight == preset.colors.highlight


def test_c20_a_boxed_preset_adapts_its_box_not_its_outline():
    boxed = BUILTIN_PRESETS["subtitle"]
    assert boxed.border_style == 3
    adapted, markers = cc.apply_auto_contrast(boxed, cc.BackgroundSample(10.0, 3))
    assert adapted.colors.box == cc.LIGHT_BOX
    assert markers == ["auto_contrast:light"]


def test_c20_the_threshold_favours_the_shipped_default():
    """The dark outline works over most footage, so the light one should engage only when the
    background is genuinely dark - not merely below average."""
    assert cc.BRIGHT_THRESHOLD < 128


@requires_ffmpeg
def test_c20_samples_the_caption_band_not_the_frame_average(tmp_path):
    """The case a frame average gets wrong: a frame that is dark overall and bright where the
    caption sits."""
    video = tmp_path / "gradient.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "gradients=s=1080x1920:c0=black:c1=white:x0=540:y0=0:x1=540:y1=1919:d=2",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    band = cc.caption_band("bottom", 1080, 1920)
    sample = cc.sample_background(video, 2.0, band)
    assert sample is not None
    # The bottom of a black-to-white vertical gradient is bright, while the frame average is mid.
    assert sample.bright is True
    assert sample.mean_luma > 150


@requires_ffmpeg
def test_c20_an_unreadable_video_yields_no_sample(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    assert cc.sample_background(broken, 2.0, (1080, 200, 0, 1600)) is None


@requires_ffmpeg
def test_c20_reaches_the_render(tmp_path, monkeypatch, make_video):
    """A contrast function nothing calls is not a feature."""
    from config import settings

    monkeypatch.setattr(settings, "caption_auto_contrast", True, raising=False)
    dark = tmp_path / "dark.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1080x1920:r=25:d=2",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(dark),
        ],
        check=True,
        capture_output=True,
    )
    preset, _ = resolve_preset("karaoke")
    adapted, markers = cc.choose_for_clip(
        dark, preset, duration=2.0, video_width=1080, video_height=1920
    )
    assert markers == ["auto_contrast:light"]
    assert adapted.colors.outline == cc.LIGHT_OUTLINE
