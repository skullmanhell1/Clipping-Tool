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



# --------------------------------------------------------------------------- #
# Task 6 — B-roll single-pass compositor integration (6.5, 6.6, 6.7)
# --------------------------------------------------------------------------- #
import re

from worker.effects.broll import AssetRef, BrollCue


def _spy_run(monkeypatch):
    """Wrap ``compositor._run`` to record every ffmpeg command (calls through)."""
    calls: list[list[str]] = []
    real = compositor._run

    def _wrapper(cmd):
        calls.append(list(cmd))
        return real(cmd)

    monkeypatch.setattr(compositor, "_run", _wrapper)
    return calls


def _image_resolver(png_path, keyword="fire", start=0.4, end=1.4):
    """A b-roll resolver returning a single resolved (image) cue."""
    asset = AssetRef(path=str(png_path), kind="image", provider="local",
                     license="local")
    return lambda: [BrollCue(start, end, keyword, asset=asset)]


def _maps_and_codecs(cmd):
    """Extract the ordered (-map / -c:v / -c:a) argument pairs from a command."""
    out = []
    i = 0
    while i < len(cmd):
        if cmd[i] in ("-map", "-c:v", "-c:a"):
            out.append((cmd[i], cmd[i + 1]))
            i += 2
        else:
            i += 1
    return out


@requires_ffmpeg
def test_broll_single_pass_with_captions_and_emoji(make_video, png_asset,
                                                   monkeypatch, tmp_path):
    """Validates: Requirements 10.1, 10.3, 17.1 — one ffmpeg pass, distinct indices."""
    base = make_video("base.mp4", duration=3.0, w=1080, h=1920)
    emoji_png = png_asset("emoji.png", color="red")
    broll_png = png_asset("broll.png", color="blue")

    calls = _spy_run(monkeypatch)
    opts = ProcessingOptions(captions=True, emoji="heavy")
    result = compositor.render_clip(
        base, tmp_path / "single.mp4", opts, _words(), tmp_path,
        emoji_resolver=lambda c: emoji_png,
        broll_resolver=_image_resolver(broll_png, keyword="fire"),
    )

    assert result is not None
    assert result.path.exists()
    # Exactly ONE ffmpeg invocation for the render (Reqs 10.1, 17.1).
    assert len(calls) == 1
    cmd = calls[0]

    # b-roll marker composited and provenance recorded (Reqs 9.4, 12.2).
    assert "broll:fire" in result.effects_applied
    assert result.broll_records and result.broll_records[0]["keyword"] == "fire"

    # Distinct, collision-free input indices in the filter graph (Req 10.3).
    fc = cmd[cmd.index("-filter_complex") + 1]
    non_base = [int(m) for m in re.findall(r"\[(\d+):v\]", fc) if int(m) != 0]
    assert non_base, "expected overlay inputs referenced in the graph"
    assert len(non_base) == len(set(non_base))  # no index collisions
    # One base input + one input per distinct overlay asset.
    assert cmd.count("-i") == 1 + len(set(non_base))


@requires_ffmpeg
def test_zero_resolvable_cues_equals_broll_disabled(make_video, monkeypatch,
                                                    tmp_path):
    """Validates: Requirements 9.3 — no resolvable assets == b-roll disabled.

    Property 18 (as an integration example): enabling b-roll with an empty
    resolved-cue list yields the same maps/codecs as rendering with b-roll off.
    """
    base = make_video("base.mp4", duration=2.0, w=1080, h=1920)
    opts = ProcessingOptions(captions=True)

    calls = _spy_run(monkeypatch)
    # b-roll "enabled" but the resolver yields nothing resolvable.
    with_broll = compositor.render_clip(
        base, tmp_path / "with.mp4", opts, _words(), tmp_path,
        broll_resolver=lambda: [],
    )
    disabled = compositor.render_clip(
        base, tmp_path / "without.mp4", opts, _words(), tmp_path,
    )

    assert with_broll is not None and disabled is not None
    assert len(calls) == 2
    # Same stream maps + codecs => rendered identically to b-roll disabled.
    assert _maps_and_codecs(calls[0]) == _maps_and_codecs(calls[1])
    assert with_broll.effects_applied == disabled.effects_applied
    assert with_broll.broll_records == []


@requires_ffmpeg
def test_stream_copy_and_noop_contract(make_video, monkeypatch, tmp_path):
    """Validates: Requirements 17.2, 17.3 — audio stream-copy + None no-op."""
    base = make_video("base.mp4", duration=2.0, w=1080, h=1920)

    # Only video changes (captions) -> audio must be stream-copied.
    calls = _spy_run(monkeypatch)
    result = compositor.render_clip(
        base, tmp_path / "vidonly.mp4", ProcessingOptions(captions=True),
        _words(), tmp_path,
    )
    assert result is not None
    cmd = calls[-1]
    assert ("-c:a", "copy") in _maps_and_codecs(cmd)  # unmodified audio copied

    # Everything off (even with a b-roll resolver that yields nothing) -> None.
    noop = compositor.render_clip(
        base, tmp_path / "noop.mp4", ProcessingOptions(captions=False),
        _words(), tmp_path, broll_resolver=lambda: [],
    )
    assert noop is None
