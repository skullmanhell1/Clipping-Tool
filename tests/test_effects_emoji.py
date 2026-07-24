"""Tests for auto-emoji planning, Twemoji resolution, and overlay building."""
from __future__ import annotations

from pathlib import Path

from tests.conftest import FakeWord, probe_duration, requires_ffmpeg
from worker.effects import emoji as em


def test_twemoji_filename_strips_variation_selector():
    # Heart is U+2764 U+FE0F -> "2764.png"; fire is a single codepoint.
    assert em.twemoji_filename("\u2764\ufe0f") == "2764.png"
    assert em.twemoji_filename("\U0001F525") == "1f525.png"


def test_plan_emoji_off_returns_nothing():
    words = [FakeWord(0.0, 0.5, "money")]
    assert em.plan_emoji(words, 5.0, intensity="off") == []


def test_plan_emoji_respects_spacing():
    words = [
        FakeWord(0.5, 0.9, "money"),   # 💰
        FakeWord(1.0, 1.4, "fire"),    # 🔥 but within 5s spacing -> skipped
        FakeWord(6.0, 6.4, "love"),    # ❤️ far enough -> kept
    ]
    cues = em.plan_emoji(words, 8.0, intensity="standard")  # spacing 5s
    assert [c.char for c in cues] == ["💰", "❤️"]
    assert cues[0].start == 0.5
    assert cues[1].start == 6.0


def test_plan_emoji_heavy_allows_more():
    words = [FakeWord(i * 0.5, i * 0.5 + 0.3, "fire") for i in range(8)]
    heavy = em.plan_emoji(words, 6.0, intensity="heavy")   # spacing 2.5s
    subtle = em.plan_emoji(words, 6.0, intensity="subtle")  # spacing 10s
    assert len(heavy) > len(subtle)


def test_ai_mode_uses_llm_map():
    from worker.llm_client import MockLLMClient

    client = MockLLMClient(responses=['{"widget": "🧩"}'])
    words = [FakeWord(0.2, 0.6, "widget")]
    cues = em.plan_emoji(words, 5.0, intensity="standard", mode="ai", client=client)
    assert cues and cues[0].char == "🧩"


def test_resolve_asset_uses_local_then_downloader(tmp_path, monkeypatch):
    monkeypatch.setattr(em.settings, "emoji_assets_dir", tmp_path)

    calls = {"n": 0}

    def fake_downloader(url, dest: Path) -> bool:
        calls["n"] += 1
        dest.write_bytes(b"PNGDATA")
        return True

    # First resolve downloads and caches.
    p1 = em.resolve_asset("🔥", downloader=fake_downloader)
    assert p1 is not None and p1.exists()
    assert calls["n"] == 1
    # Second resolve hits the cache (no extra download).
    p2 = em.resolve_asset("🔥", downloader=fake_downloader)
    assert p2 == p1
    assert calls["n"] == 1


def test_resolve_asset_returns_none_when_download_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(em.settings, "emoji_assets_dir", tmp_path)
    assert em.resolve_asset("🔥", downloader=lambda u, d: False) is None


def test_build_overlay_skips_unresolved():
    cues = [em.EmojiCue("🔥", 0.0, 1.0, 0)]
    inputs, graph = em.build_overlay(cues, "0:v", "vout", duration=2.0,
                                     resolver=lambda c: None)
    assert inputs == [] and graph == ""


@requires_ffmpeg
def test_emoji_overlay_renders(make_video, png_asset, tmp_path):
    from config import settings
    from worker.ffmpeg_utils import _run

    src = make_video("base.mp4", duration=2.0, w=1080, h=1920)
    asset = png_asset("e.png")
    cues = [em.EmojiCue("💰", 0.2, 1.2, 0), em.EmojiCue("🔥", 1.0, 1.8, 1)]
    inputs, graph = em.build_overlay(
        cues, base_label="0:v", out_label="vout", duration=2.0, animate=True,
        resolver=lambda c: asset, input_offset=1,
    )
    assert graph and len(inputs) == 12  # 2 emoji * ["-loop","1","-t",D,"-i",path]

    dest = tmp_path / "emoji_out.mp4"
    _run([settings.ffmpeg_binary, "-y", "-i", str(src), *inputs,
          "-filter_complex", graph, "-map", "[vout]", "-map", "0:a",
          "-c:v", "libx264", "-c:a", "copy", str(dest)])
    assert dest.exists()
    assert probe_duration(dest) > 1.5
