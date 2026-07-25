"""Tests for caption templates/positions and the hook-title ASS builder."""
from __future__ import annotations

import pytest

from worker.captions import (
    Cue,
    build_ass,
    burn_captions,
    caption_emoji_glyph,
    subtitles_filter,
    words_to_cues,
)
from worker.effects.caption_presets import BUILTIN_PRESETS
from worker.transcribe import Word


def _cues():
    return [Cue(0.0, 1.0, [Word(0.0, 0.5, "Hello"), Word(0.5, 1.0, "world")])]


@pytest.mark.parametrize("template", ["karaoke", "boxed", "minimal"])
@pytest.mark.parametrize("position", ["bottom", "center", "top"])
def test_build_ass_templates_and_positions(template, position, tmp_path):
    dest = tmp_path / "c.ass"
    build_ass(_cues(), dest, template=template, position=position)
    text = dest.read_text()
    assert "[V4+ Styles]" in text
    assert "Style: Default" in text
    assert "Dialogue:" in text
    # Alignment reflects position (2=bottom, 5=center, 8=top).
    align = {"bottom": ",2,", "center": ",5,", "top": ",8,"}[position]
    assert align in text


def test_karaoke_has_fill_tags(tmp_path):
    dest = tmp_path / "k.ass"
    build_ass(_cues(), dest, template="karaoke")
    assert "\\kf" in dest.read_text()


def test_minimal_has_no_karaoke(tmp_path):
    dest = tmp_path / "m.ass"
    build_ass(_cues(), dest, template="minimal")
    assert "\\kf" not in dest.read_text()


def test_hook_title_event(tmp_path):
    dest = tmp_path / "h.ass"
    build_ass([], dest, hook_text="Wait for it")
    text = dest.read_text()
    assert "Style: Hook" in text
    assert "WAIT FOR IT" in text  # hook is upper-cased
    assert "\\fad(" in text  # fade in/out


def test_subtitles_filter_escapes_path():
    f = subtitles_filter("/tmp/a:bّ.ass")
    assert f.startswith("subtitles='")
    assert "\\:" in f  # colon escaped for ffmpeg filter syntax



# --- Task 2.8: keyword highlighting disabled skips all LLM work ------------
def test_keyword_highlight_disabled_makes_zero_llm_calls():
    """Validates: Requirements 3.6

    A highlight-disabled caller passes ``use_ai=False``; the planner must make
    zero LLM calls even when a client is supplied (spy counts invocations).
    """
    from worker.effects.caption_presets import plan_keywords
    from worker.llm_client import MockLLMClient
    from worker.transcribe import Word

    spy = MockLLMClient(responses=["[]"])
    words = [
        Word(0.0, 0.4, "the"),
        Word(0.4, 1.0, "revolutionary"),
        Word(1.0, 1.4, "AI"),
    ]

    # Highlighting disabled path: use_ai=False, but a client is available.
    result = plan_keywords(words, use_ai=False, client=spy)

    assert spy.calls == []  # zero LLM calls
    # Deterministic emphasis still identifies the content/ALL-CAPS words.
    assert result == {1, 2}



# ===========================================================================
# Task 3.8 — legacy behaviour, emoji independence, font substitution (unit)
# ===========================================================================
def test_legacy_templates_unchanged_by_new_params(tmp_path):
    """Validates: Requirements 1.1

    The three legacy templates render exactly as before — the new preset-only
    animation tags never leak into the legacy (``preset is None``) path.
    """
    karaoke = tmp_path / "k.ass"
    build_ass(_cues(), karaoke, template="karaoke")
    k_text = karaoke.read_text()
    assert "\\kf" in k_text
    assert "\\fscx60" not in k_text  # pop-style ramp is preset-only

    minimal = tmp_path / "m.ass"
    build_ass(_cues(), minimal, template="minimal")
    m_text = minimal.read_text()
    assert "\\kf" not in m_text
    assert "\\fscx60" not in m_text

    boxed = tmp_path / "b.ass"
    build_ass(_cues(), boxed, template="boxed", position="top")
    assert ",8," in boxed.read_text()  # alignment still honoured


def test_in_caption_emoji_independent_of_overlay_emoji(tmp_path):
    """Validates: Requirements 4.1, 4.2

    In-caption emoji are inserted as font glyphs directly into cue text and are
    fully independent of the overlay ``emoji`` effect — no asset resolver or
    downloader is ever invoked.
    """
    downloads: list = []

    def downloader(*args, **kwargs):
        downloads.append(1)
        return None

    preset = BUILTIN_PRESETS["hormozi"]  # emoji_inline=True
    words = [Word(0.0, 0.5, "money"), Word(0.5, 1.0, "today")]
    dest = tmp_path / "emoji.ass"
    build_ass(
        [Cue(0.0, 1.0, words)], dest, preset=preset, clip_duration=1.0,
        emoji_downloader=downloader,
    )
    text = dest.read_text()
    assert "\U0001f4b0" in text  # money glyph appears inline in the cue text
    assert downloads == []       # overlay/download machinery is never used

    # A preset with in-caption emoji disabled inserts no glyph.
    assert caption_emoji_glyph(Word(0.0, 1.0, "money"), BUILTIN_PRESETS["karaoke"]) == ""


def test_missing_preset_font_falls_back_and_surfaces_note(tmp_path, monkeypatch):
    """Validates: Requirements 5.3

    When the preset font is unavailable the caption still renders using a
    fallback font and a ``font_substituted:<name>`` note is surfaced on the
    exposed notes channel.
    """
    import worker.captions as cap

    monkeypatch.setattr(cap, "font_available", lambda _n: False)
    preset = BUILTIN_PRESETS["pop"]
    notes: list[str] = []
    dest = tmp_path / "fallback.ass"
    cap.build_ass(_cues(), dest, preset=preset, clip_duration=1.0, notes=notes)

    text = dest.read_text()
    assert f"font_substituted:{preset.font}" in notes
    style = next(ln for ln in text.splitlines() if ln.startswith("Style: Default"))
    assert style.split("Style: ", 1)[1].split(",")[1] == "Arial"  # fallback font
    assert "Dialogue:" in text  # clip still renders


# ===========================================================================
# Task 3.9 — Property 10: every preset/position combination renders (ffmpeg)
# ===========================================================================
try:  # module-level helpers (not fixtures) from the shared conftest
    from tests.conftest import probe_size, requires_ffmpeg
except ImportError:  # pragma: no cover - conftest always importable under pytest
    from conftest import probe_size, requires_ffmpeg


# Feature: tier1-creator-output-upgrade, Property 10: Every preset/position combination yields a parseable ASS file
@requires_ffmpeg
@pytest.mark.parametrize("preset_name", list(BUILTIN_PRESETS))
@pytest.mark.parametrize("position", ["bottom", "center", "top"])
def test_p10_every_preset_position_renders(preset_name, position, make_video, tmp_path):
    """Validates: Requirements 1.3, 5.4

    For each built-in preset × each caption position, the generated ASS is
    burned into a tiny clip via libass and yields a valid, correctly-sized
    output (libass parses it without error).
    """
    src = make_video("src.mp4", duration=2.0, w=240, h=240, audio=True)
    preset = BUILTIN_PRESETS[preset_name]
    words = [
        Word(0.0, 0.5, "Hello"),
        Word(0.5, 1.0, "WORLD"),
        Word(1.0, 1.6, "money"),
    ]
    cues = words_to_cues(words)
    ass = tmp_path / f"{preset_name}_{position}.ass"
    build_ass(
        cues, ass, preset=preset, keyword_indices={1}, position=position,
        clip_duration=2.0,
    )
    out = tmp_path / f"{preset_name}_{position}.mp4"
    burn_captions(src, ass, out)
    assert out.exists() and out.stat().st_size > 0
    assert probe_size(out) == (240, 240)
