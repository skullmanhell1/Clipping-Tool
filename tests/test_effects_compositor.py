"""Integration tests for the single-pass effect compositor."""
from __future__ import annotations

from tests.conftest import FakeWord, probe_duration, probe_size, requires_ffmpeg
from worker.effects import compositor
from worker.models import ProcessingOptions


def _words():
    return [FakeWord(0.2, 0.6, "This"), FakeWord(0.7, 1.1, "is"),
            FakeWord(1.2, 1.6, "fire"), FakeWord(1.7, 2.2, "money")]


@requires_ffmpeg
def test_noop_returns_none(make_video, tmp_path):
    base = make_video("base.mp4", duration=2.0, w=1080, h=1920)
    opts = ProcessingOptions(captions=False)  # nothing enabled
    result = compositor.render_clip(base, tmp_path / "out.mp4", opts, _words(), tmp_path)
    assert result is None


@requires_ffmpeg
def test_all_effects_single_pass(make_video, png_asset, tmp_path):
    base = make_video("base.mp4", duration=3.0, w=1080, h=1920)
    asset = png_asset("e.png")
    opts = ProcessingOptions(
        captions=True, hook_title=True, color="vivid", zoom=True, transitions=True,
        fades=True, progress_bar=True, emoji="heavy", music="chill",
        caption_template="boxed", caption_position="bottom",
    )
    result = compositor.render_clip(
        base, tmp_path / "all.mp4", opts, _words(), tmp_path,
        hook_text="WAIT FOR IT", emoji_resolver=lambda c: asset,
    )
    assert result is not None
    applied = result.effects_applied
    for fx in ("captions", "hook_title", "color:vivid", "zoom", "transitions",
               "fades", "progress_bar", "emoji:heavy", "music:chill"):
        assert fx in applied
    assert result.path.exists()
    assert probe_size(result.path) == (1080, 1920)


@requires_ffmpeg
def test_music_only_copies_video(make_video, tmp_path):
    base = make_video("base.mp4", duration=2.0, w=640, h=360)
    opts = ProcessingOptions(captions=False, music="upbeat")
    result = compositor.render_clip(base, tmp_path / "m.mp4", opts, _words(), tmp_path)
    assert result is not None
    assert result.effects_applied == ["music:upbeat"]
    assert probe_duration(result.path) > 1.5


@requires_ffmpeg
def test_captions_only(make_video, tmp_path):
    base = make_video("base.mp4", duration=2.0, w=1080, h=1920)
    opts = ProcessingOptions(captions=True)
    result = compositor.render_clip(base, tmp_path / "c.mp4", opts, _words(), tmp_path)
    assert result is not None
    assert "captions" in result.effects_applied
