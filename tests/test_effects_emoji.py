"""Tests for auto-emoji planning, asset resolution, and overlay building."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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
        FakeWord(0.5, 0.9, "money"),  # 💰
        FakeWord(1.0, 1.4, "fire"),  # 🔥 but within 5s spacing -> skipped
        FakeWord(6.0, 6.4, "love"),  # ❤️ far enough -> kept
    ]
    cues = em.plan_emoji(words, 8.0, intensity="standard")  # spacing 5s
    assert [c.char for c in cues] == ["💰", "❤️"]
    assert cues[0].start == 0.5
    assert cues[1].start == 6.0


def test_plan_emoji_heavy_allows_more():
    """Intensity controls density.

    The fixture used eight copies of the word "fire", which made this a test that spacing lets
    more of the *same* glyph through - and A12 now deliberately forbids that, because the same
    emoji twice in one clip is the most obvious way an automatic overlay looks automatic. The
    keywords are distinct so the assertion is about intensity, which is what it is named for,
    rather than about repetition.
    """
    keywords = ["love", "amazing", "wow", "money", "cash", "rich", "fire", "best"]
    words = [FakeWord(i * 0.5, i * 0.5 + 0.3, k) for i, k in enumerate(keywords)]
    heavy = em.plan_emoji(words, 6.0, intensity="heavy")  # spacing 2.5s
    subtle = em.plan_emoji(words, 6.0, intensity="subtle")  # spacing 10s
    assert len(heavy) > len(subtle)


def test_the_same_glyph_is_never_used_twice_in_one_clip():
    """A12. Eight mentions of one topic get one emoji, not eight."""
    words = [FakeWord(i * 4.0, i * 4.0 + 0.3, "fire") for i in range(8)]
    cues = em.plan_emoji(words, 40.0, intensity="heavy")
    assert len(cues) == 1, [c.char for c in cues]


def test_the_count_is_capped_independently_of_spacing():
    """A12. Spacing alone scales with clip length, so a long clip could satisfy every gap and
    still carry sixty emoji."""
    keywords = list(em.KEYWORD_EMOJI)[:60]
    words = [FakeWord(i * 3.0, i * 3.0 + 0.3, k) for i, k in enumerate(keywords)]
    cues = em.plan_emoji(words, 180.0, intensity="heavy")
    assert len(cues) <= em._emoji_cap("heavy", 180.0)


def test_the_emoji_lands_on_the_more_salient_word():
    """A11. The old rule took whichever mapped word arrived first after the stopwatch."""
    words = [FakeWord(0.2, 0.6, "best"), FakeWord(0.7, 1.1, "money")]
    cues = em.plan_emoji(words, 20.0, intensity="subtle")
    assert len(cues) == 1
    assert (
        cues[0].char == em.KEYWORD_EMOJI["money"]
    ), "the filler-ish word took the slot the substantive one wanted"


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
    inputs, graph = em.build_overlay(cues, "0:v", "vout", duration=2.0, resolver=lambda c: None)
    assert inputs == [] and graph == ""


@requires_ffmpeg
def test_emoji_overlay_renders(make_video, png_asset, tmp_path):
    from config import settings
    from worker.ffmpeg_utils import _run

    src = make_video("base.mp4", duration=2.0, w=1080, h=1920)
    asset = png_asset("e.png")
    cues = [em.EmojiCue("💰", 0.2, 1.2, 0), em.EmojiCue("🔥", 1.0, 1.8, 1)]
    inputs, graph = em.build_overlay(
        cues,
        base_label="0:v",
        out_label="vout",
        duration=2.0,
        animate=True,
        resolver=lambda c: asset,
        input_offset=1,
    )
    assert graph and len(inputs) == 12  # 2 emoji * ["-loop","1","-t",D,"-i",path]

    dest = tmp_path / "emoji_out.mp4"
    _run(
        [
            settings.ffmpeg_binary,
            "-y",
            "-i",
            str(src),
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[vout]",
            "-map",
            "0:a",
            "-c:v",
            "libx264",
            "-c:a",
            "copy",
            str(dest),
        ]
    )
    assert dest.exists()
    assert probe_duration(dest) > 1.5


# ===========================================================================
# A6, A7 — the assets are vendored, and big enough not to be upscaled
# ===========================================================================
# ``.gitignore`` claimed "Emoji assets are downloaded at build time" and nothing did that,
# so ``assets/emoji`` was empty: every render either made a per-clip HTTP request or
# silently dropped the overlay. Same shape as the font bug — a declared asset that was
# never actually shipped.
_ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets" / "emoji"


def test_every_built_in_keyword_emoji_is_vendored():
    """A7: every glyph the built-in map can emit has a file in the repo.

    ``scripts/fetch_emoji.py --check`` is the same assertion for CI and for a developer;
    this is the one that fails the suite.
    """
    missing = [
        glyph
        for glyph in sorted(set(em.KEYWORD_EMOJI.values()))
        if not (_ASSETS_DIR / em.emoji_filename(glyph)).is_file()
    ]
    assert not missing, (
        f"{len(missing)} emoji are not vendored: {' '.join(missing)}. "
        "Run: python scripts/fetch_emoji.py"
    )


def test_vendored_emoji_are_large_enough_to_downscale():
    """A6: the source must be bigger than any size we render, not smaller.

    The overlay asks for roughly 14% of frame width — about 151px on a 1080-wide frame.
    The previous source was Twemoji 72x72, so that was a 2.1x *upscale* and visibly soft.
    Asserting the stored pixels rather than the URL is what makes this a real check.
    """
    from PIL import Image

    target_px = em._emoji_px(1080, 0.14)
    assert target_px >= 100, "sanity: the default overlay is not tiny"

    for glyph in sorted(set(em.KEYWORD_EMOJI.values())):
        path = _ASSETS_DIR / em.emoji_filename(glyph)
        with Image.open(path) as image:
            width, height = image.size
        assert width >= target_px and height >= target_px, (
            f"{glyph} is {width}x{height}, smaller than the {target_px}px it renders at, "
            "so it would be upscaled"
        )


def test_resolving_a_built_in_emoji_never_touches_the_network(monkeypatch):
    """A7: a vendored glyph resolves locally, with the downloader left untouched.

    The downloader is a spy that fails the test if called, rather than one that returns
    ``False`` — "did not need the network" is the property, and a call that happens to fail
    would otherwise look identical to a call that never happened.
    """
    monkeypatch.setattr(em.settings, "emoji_assets_dir", _ASSETS_DIR)

    def _never(url, dest):  # pragma: no cover - must not be reached
        raise AssertionError(f"render-time download attempted for {url}")

    for glyph in sorted(set(em.KEYWORD_EMOJI.values())):
        assert em.resolve_asset(glyph, downloader=_never) is not None, glyph


def test_render_time_download_is_off_by_default():
    """The default is local-only. A render is not the place to discover the network is down."""
    from config import Settings

    assert Settings().emoji_allow_download is False


def test_emoji_assets_are_tracked_by_git():
    """The `.gitignore` entry that made A7 invisible must stay gone.

    An ignored asset directory is exactly how this defect survived: the files could be
    present locally, pass every test, and be absent from the image.
    """
    import subprocess

    repo_root = _ASSETS_DIR.parents[1]
    sample = _ASSETS_DIR / em.emoji_filename(em.KEYWORD_EMOJI["fire"])
    proc = subprocess.run(
        ["git", "check-ignore", "-q", str(sample)],
        cwd=repo_root,
        capture_output=True,
    )
    # git check-ignore exits 0 when the path IS ignored, 1 when it is not.
    assert proc.returncode != 0, f"{sample.name} is git-ignored; the assets will not ship"


# ===========================================================================
# A8 — the emoji is sized against the real frame, not a hard-coded 1080
# ===========================================================================
def _scale_width(graph: str) -> int:
    """The pixel width from the first ``scale=<w>:-1`` in a filtergraph."""
    match = re.search(r"scale=(\d+):-1", graph)
    assert match, graph
    return int(match.group(1))


def test_emoji_scale_follows_the_frame_width():
    """A8: ``scale=int(1080 * size_frac)`` was hard-coded.

    The overlay *placement* already used ffmpeg's real ``W``, so on any output that was not
    1080 wide the scale and the placement disagreed about the frame: a 1920-wide frame got
    an emoji sized for a 1080 one, i.e. roughly half the intended size.
    """
    cues = [em.EmojiCue("🔥", 0.0, 1.0, 0)]

    def resolver(_char):
        return Path("/tmp/x.png")

    _inputs, narrow = em.build_overlay(
        cues, "0:v", "vout", duration=2.0, frame_width=1080, resolver=resolver
    )
    _inputs, wide = em.build_overlay(
        cues, "0:v", "vout", duration=2.0, frame_width=1920, resolver=resolver
    )

    assert _scale_width(narrow) == em._emoji_px(1080, 0.14)
    assert _scale_width(wide) == em._emoji_px(1920, 0.14)
    assert _scale_width(wide) > _scale_width(narrow), "width must track the frame"

    # The same fraction of each frame, which is what "size_frac" is supposed to mean.
    assert _scale_width(narrow) / 1080 == pytest.approx(0.14, abs=0.01)
    assert _scale_width(wide) / 1920 == pytest.approx(0.14, abs=0.01)


def test_emoji_scale_width_is_even_and_positive():
    """libx264's 4:2:0 chroma subsampling needs even dimensions; a 0-width scale fails."""
    for frame_width in (0, 1, 7, 100, 1080, 1920, 3840):
        px = em._emoji_px(frame_width, 0.14)
        assert px >= 2 and px % 2 == 0, (frame_width, px)


# ===========================================================================
# A10 — inflected speech matches the keyword map
# ===========================================================================
@pytest.mark.parametrize(
    ("spoken", "base"),
    [
        # The four misses the improvement plan verified by hand.
        ("winning", "win"),
        ("wins", "win"),
        ("won", "win"),
        ("fired", "fire"),
        # And the same shape across the rest of the rule set.
        ("firing", "fire"),
        ("ideas", "idea"),
        ("looked", "look"),
        ("stopping", "stop"),
        ("parties", "party"),
        ("richest", "rich"),
        ("celebrating", "celebrate"),
        ("growing", "grow"),
    ],
)
def test_inflected_words_find_their_base_form(spoken, base):
    """A10: emoji are planned from spoken words, which are usually inflected."""
    expected = em.KEYWORD_EMOJI[base]
    assert em.lookup_emoji(spoken, em.KEYWORD_EMOJI) == expected, spoken


def test_stemming_does_not_invent_matches():
    """Suffix rules must not fold an unrelated word onto a keyword.

    ``-ss`` is excluded from the plural rule for exactly this reason: stripping it turns
    "business" into "busines" and "success" into "succes", and a looser rule would let
    unrelated words collide with map keys.
    """
    assert em.lookup_emoji("business", em.KEYWORD_EMOJI) == em.KEYWORD_EMOJI["business"]
    assert em.lookup_emoji("success", em.KEYWORD_EMOJI) == em.KEYWORD_EMOJI["success"]
    for token in ("the", "and", "somethingelse", "as", "is", "us", "his", ""):
        assert em.lookup_emoji(token, em.KEYWORD_EMOJI) == "", token


def test_an_exact_match_always_wins_over_a_stemmed_one():
    """The map keeps exactly the meaning it had; stemming only adds reach."""
    mapping = {"wins": "🥇", "win": "🏆"}
    assert em.lookup_emoji("wins", mapping) == "🥇"
    assert em.lookup_emoji("win", mapping) == "🏆"


def test_plan_emoji_matches_inflected_speech_end_to_end():
    """The planner, not just the lookup helper, benefits from A10."""
    words = [
        FakeWord(0.0, 0.5, "we"),
        FakeWord(0.5, 1.0, "won"),  # missed entirely before A10
        FakeWord(1.0, 1.5, "everything"),
    ]
    cues = em.plan_emoji(words, duration=3.0, intensity="heavy")
    assert [cue.char for cue in cues] == [em.KEYWORD_EMOJI["win"]]
