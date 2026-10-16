"""Focused offline checks for dynamic subtitle styling and its safe fallback.

These tests intentionally stop at the generated ASS document. They exercise the
same pure keyword, timing, serialisation, and legacy-rendering seams used by the
ffmpeg compositor without requiring ffmpeg, libass, a model, or a network call.
"""

from __future__ import annotations

from worker import captions
from worker.effects.caption_presets import BUILTIN_PRESETS, CaptionPreset, plan_keywords
from worker.engines.kinetic import Kinetic_Options
from worker.transcribe import Word


def test_keyword_selection_keeps_unicode_content_words_and_ignores_stopwords():
    """Keyword ranking should not silently drop non-ASCII transcript tokens."""
    words = [
        Word(0.0, 0.4, "the"),
        Word(0.4, 0.8, "¡revolución!"),
        Word(0.8, 1.2, "重要内容"),
    ]

    selected = plan_keywords(words, use_ai=False)

    # Three words receive one emphasis slot; both non-ASCII content tokens are
    # eligible and the stronger, longer token wins deterministically.
    assert selected == {1}


def test_ass_timestamp_carries_at_centisecond_and_minute_boundaries():
    """Rounding must never emit an invalid ``SS=60`` ASS timestamp."""
    assert captions._ass_timestamp(59.9999) == "0:01:00.00"
    assert captions._ass_timestamp(3599.9999) == "1:00:00.00"
    assert captions._ass_timestamp(float("nan")) == "0:00:00.00"


def test_word_span_turns_non_finite_word_bounds_into_a_visible_minimum_span():
    """A malformed transcript word must not crash ASS tag generation."""
    span = captions.build_word_span(
        Word(0.0, float("nan"), "word"),
        BUILTIN_PRESETS["karaoke"],
        False,
    )

    assert "\\kf1" in span


def test_legacy_block_fallback_clamps_events_to_clip_duration(tmp_path):
    """Static/block captions remain usable when a dynamic path is unavailable."""
    dest = captions.build_ass(
        [captions.Cue(4.0, 5.0, [Word(4.0, 5.0, "late")])],
        tmp_path / "fallback.ass",
        template="boxed",
        clip_duration=1.0,
    )

    dialogue = next(
        line
        for line in dest.read_text(encoding="utf-8").splitlines()
        if line.startswith("Dialogue:")
    )
    assert dialogue.startswith("Dialogue: 0,0:00:01.00,0:00:01.00,Default")
    assert "\\t(" not in dialogue
    assert "\\kf" not in dialogue


def test_caption_and_kinetic_style_records_round_trip_without_loss():
    """Saved style choices reload with both look and motion fields intact."""
    preset = BUILTIN_PRESETS["karaoke_bold"]
    assert CaptionPreset.from_dict(preset.to_dict()) == preset

    options = Kinetic_Options(
        style="bounce",
        reveal="word_by_word",
        preset_name=preset.name,
        font_override="Anton",
        max_lines=3,
        max_line_width=30,
        safe_area_x_pct=8.0,
        safe_area_y_pct=12.0,
        motion_duration_ms=240,
        highlight_keywords=True,
        confidence_floor=0.65,
    )
    encoded = options.to_dict()
    assert Kinetic_Options.parse(encoded).to_dict() == encoded
