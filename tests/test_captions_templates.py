"""Tests for caption templates/positions and the hook-title ASS builder."""
from __future__ import annotations

import pytest

from worker.captions import Cue, build_ass, subtitles_filter
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
