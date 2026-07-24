"""Unit tests for the easy-effect ffmpeg filter builders."""
from __future__ import annotations

from worker.effects import overlays as ov


def test_color_filter_presets():
    assert ov.color_filter("") is None
    assert ov.color_filter("unknown") is None
    assert "saturation" in ov.color_filter("vivid")
    assert ov.color_filter("bw").startswith("hue=s=0")


def test_zoom_filter_modes():
    assert ov.zoom_filter(10, 30, 1080, 1920) is None  # neither -> disabled
    kb = ov.zoom_filter(10, 30, 1080, 1920, ken_burns=True)
    assert "zoompan" in kb and "on/300" in kb  # 10s * 30fps = 300 frames
    punch = ov.zoom_filter(10, 30, 1080, 1920, punch_in=True)
    assert "1.18" in punch  # starts zoomed in
    assert "s=1080x1920" in punch


def test_video_fade_filter():
    long = ov.video_fade_filter(10)
    assert "fade=t=in" in long and "fade=t=out" in long
    # Very short clip -> fade in only.
    short = ov.video_fade_filter(0.8)
    assert "fade=t=in" in short and "fade=t=out" not in short


def test_progress_bar_filter():
    f = ov.progress_bar_filter(10, 1080, 1920)
    assert f.startswith("drawbox")
    assert "iw*t/10.000" in f  # width grows with time


def test_build_video_chain_order_and_toggles():
    # All off, no subtitles -> empty chain.
    assert ov.build_video_chain(duration=10, fps=30, width=1080, height=1920) == []

    chain = ov.build_video_chain(
        duration=10, fps=30, width=1080, height=1920,
        color="warm", zoom=True, fades=True, progress_bar=True,
        subtitles="subtitles='x.ass'",
    )
    joined = ",".join(chain)
    # Colour precedes zoom precedes fade precedes subtitles precedes progress bar.
    assert joined.index("eq=") < joined.index("zoompan")
    assert joined.index("zoompan") < joined.index("fade=t=in")
    assert joined.index("fade=t=in") < joined.index("subtitles")
    assert joined.index("subtitles") < joined.index("drawbox")
