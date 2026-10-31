"""Offline tests for dynamic campaign brand assets."""

from __future__ import annotations

from types import SimpleNamespace

from worker.branding import logo_filter, materialize_font, materialize_video
from worker.campaign_policy import campaign_brand_config, preset_settings


def test_local_font_asset_is_copied_to_configured_font_directory(tmp_path):
    source = tmp_path / "client.ttf"
    source.write_bytes(b"font-data")

    result = materialize_font(str(source), dest_dir=tmp_path / "fonts")

    assert result is not None
    assert result.parent == tmp_path / "fonts"
    assert result.read_bytes() == b"font-data"


def test_logo_filter_supports_explicit_white_matte_cleanup(tmp_path):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"not-an-image-but-a-valid-test-path")
    options = SimpleNamespace(
        brand_logo=str(logo),
        brand_logo_position="top_right",
        brand_logo_scale=0.16,
        brand_logo_opacity=0.85,
    )

    graph = logo_filter(options, 1080, 1920, background="white")

    assert graph is not None
    assert "chromakey=0xffffff" in graph
    assert "overlay=W-w-43:43" in graph


def test_invalid_font_assets_fail_softly(tmp_path):
    assert materialize_font(str(tmp_path / "missing.ttf"), dest_dir=tmp_path / "fonts") is None


def test_local_video_asset_is_copied_to_the_managed_brand_directory(tmp_path):
    source = tmp_path / "intro.mp4"
    source.write_bytes(b"video-data")

    result = materialize_video(str(source), dest_dir=tmp_path / "render")

    assert result is not None
    assert result.parent == tmp_path / "render" / "brand-assets"
    assert result.read_bytes() == b"video-data"

    source.write_bytes(b"updated-video-data")
    refreshed = materialize_video(str(source), dest_dir=tmp_path / "render")
    assert refreshed is not None
    assert refreshed != result
    assert refreshed.read_bytes() == b"updated-video-data"


def test_invalid_video_assets_fail_softly(tmp_path):
    assert materialize_video(str(tmp_path / "missing.mp4"), dest_dir=tmp_path / "render") is None


def test_campaign_brand_name_is_the_default_card_text():
    """The exact campaign spelling must survive into the options the renderer reads."""
    preset = {"settings": {"brand_name": "BOXABL"}}
    assert campaign_brand_config(preset)["card_text"] == "BOXABL"
    assert preset_settings(preset)["brand_name"] == "BOXABL"
