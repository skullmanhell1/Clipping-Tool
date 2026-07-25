"""Pipeline back-compat + graceful-degradation tests (Task 9).

These exercise the pipeline wiring added in Task 9 — central ``effective_options``
normalisation, the ``select_moments_visual`` selection swap, and the b-roll
resolver threading — proving that:

* an all-features-off run reproduces v0.6.0 behaviour (Property 24),
* missing optional dependencies (LLM / assets / keyframe sampler) still produce
  clips and record degradation (Property 27), and
* no external provider is constructed / no network occurs when external
  download features are disabled (Property 28).

The pipeline's transcribe + (where appropriate) selection steps are stubbed the
same way ``tests/test_pipeline_effects.py`` does, so the tests stay fast and
offline. Renders use tiny ``make_video`` clips gated on ``requires_ffmpeg``.
"""
from __future__ import annotations

try:  # module-level helpers (not fixtures) from the shared conftest
    from tests.conftest import FakeWord, requires_ffmpeg
except ImportError:  # pragma: no cover - conftest always importable under pytest
    from conftest import FakeWord, requires_ffmpeg

from worker.models import ProcessingOptions

# Markers introduced by the Tier 1 Creator Output Upgrade. An "all-off" run must
# never surface any of these.
_NEW_MARKERS = (
    "caption_preset",
    "caption_preset_substituted",
    "keyword_highlight",
    "caption_emoji",
    "broll",
    "visual_selection",
    "visual_degraded",
    "font_substituted",
)


def _stub_transcribe(monkeypatch, text="hello there my friend today"):
    """Stub ``pipeline.transcribe`` with a small deterministic transcript."""
    import worker.pipeline as pl
    from worker.transcribe import Transcript, TranscriptSegment, Word

    def fake_transcribe(source, language=None, translate=False):
        words = [
            Word(0.2, 0.6, "hello"),
            Word(0.7, 1.1, "there"),
            Word(1.2, 1.6, "my"),
            Word(1.7, 2.3, "friend"),
            Word(2.4, 3.0, "today"),
        ]
        return Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, 4.0, text, words)],
        )

    monkeypatch.setattr(pl, "transcribe", fake_transcribe)


def _stub_selection(monkeypatch, text="hello there my friend today"):
    """Stub ``select_moments`` so one whole-clip candidate is always returned."""
    import worker.pipeline as pl
    from worker.selection import ClipCandidate

    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [
            ClipCandidate(start=0.0, end=4.0, score=50.0, text=text)
        ],
    )


# ===========================================================================
# 9.2 — Property 24: all new features off reproduces v0.6.0 behaviour
# ===========================================================================
# Feature: tier1-creator-output-upgrade, Property 24: All new features off reproduces v0.6.0 behaviour
@requires_ffmpeg
def test_p24_all_features_off_reproduces_legacy(make_video, tmp_path, monkeypatch):
    """Validates: Requirements 16.3, 17.2, 17.3, 22.2

    With every new option at its default/off value the produced clip carries
    none of the new-feature ``effects_applied`` markers, records no b-roll
    provenance, and still renders through the legacy path (captions off here).
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    _stub_selection(monkeypatch)

    src = make_video("s.mp4", duration=4.0, w=640, h=360)
    opts = ProcessingOptions(captions=False, metadata=False, aspect="9:16")
    clips = pl.run_pipeline(
        src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp"
    )

    assert len(clips) == 1
    clip = clips[0]
    for marker in clip.effects_applied:
        assert not any(marker.startswith(nm) for nm in _NEW_MARKERS), marker
    assert clip.broll_assets == []
    assert (tmp_path / "clips" / clip.filename).exists()


# Feature: tier1-creator-output-upgrade, Property 24: All new features off reproduces v0.6.0 behaviour
@requires_ffmpeg
def test_p24_compositor_all_off_returns_none(make_video, tmp_path):
    """Validates: Requirements 17.3, 22.2

    With no legacy effect and no new feature enabled — even with a b-roll
    resolver present that yields nothing — ``render_clip`` returns ``None`` so
    the caller keeps the input clip (no extra ffmpeg pass).
    """
    from worker.effects import compositor

    base = make_video("b.mp4", duration=2.0, w=640, h=360)
    words = [FakeWord(0.2, 0.6, "hello"), FakeWord(0.7, 1.1, "world")]
    result = compositor.render_clip(
        base, tmp_path / "out.mp4", ProcessingOptions(captions=False),
        words, tmp_path, broll_resolver=lambda: [],
    )
    assert result is None


# ===========================================================================
# 9.3 — Property 27: missing dependencies still produce clips + degrade
# ===========================================================================
# Feature: tier1-creator-output-upgrade, Property 27: Missing dependencies still produce clips and record degradation
@requires_ffmpeg
def test_p27_missing_llm_still_produces_clips(make_video, tmp_path, monkeypatch):
    """Validates: Requirements 18.1, 18.4

    With no LLM client and the LLM reported unavailable, selection degrades to
    deterministic segmentation and the pipeline still produces at least one clip.
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    # No stubbed selection: exercise the real select_moments fallback with the
    # LLM forced unavailable (missing-dependency degradation).
    monkeypatch.setattr(pl.sel, "llm_available", lambda: False)

    src = make_video("s.mp4", duration=6.0, w=640, h=360)
    opts = ProcessingOptions(captions=False, metadata=False, aspect="9:16")
    clips = pl.run_pipeline(
        src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp",
        llm_client=None,
    )
    assert len(clips) >= 1
    assert (tmp_path / "clips" / clips[0].filename).exists()


# Feature: tier1-creator-output-upgrade, Property 27: Missing dependencies still produce clips and record degradation
@requires_ffmpeg
def test_p27_broll_enabled_but_no_assets_still_produces_clips(
    make_video, tmp_path, monkeypatch
):
    """Validates: Requirements 18.1, 18.2

    B-roll enabled but the (empty) local library resolves no assets: the
    pipeline still produces the clip, composites no overlay, and records no
    ``broll:*`` markers or provenance.
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    _stub_selection(monkeypatch)
    # Point the local b-roll library at an empty directory (no matches).
    empty = tmp_path / "empty_broll"
    empty.mkdir()
    monkeypatch.setattr(pl.settings, "broll_dir", empty)

    src = make_video("s.mp4", duration=4.0, w=640, h=360)
    opts = ProcessingOptions(
        captions=False, metadata=False, aspect="9:16",
        broll=True, broll_intensity="standard", asset_sourcing_mode="local_only",
    )
    clips = pl.run_pipeline(
        src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp"
    )
    assert len(clips) == 1
    clip = clips[0]
    assert not any(m.startswith("broll:") for m in clip.effects_applied)
    assert clip.broll_assets == []
    assert (tmp_path / "clips" / clip.filename).exists()


# Feature: tier1-creator-output-upgrade, Property 27: Missing dependencies still produce clips and record degradation
@requires_ffmpeg
def test_p27_visual_selection_failing_sampler_degrades(
    make_video, tmp_path, monkeypatch
):
    """Validates: Requirements 18.1, 18.2, 18.4

    Visual selection enabled but keyframe sampling fails: selection degrades to
    transcript-only, the pipeline still produces the clip, and the
    ``visual_selection`` marker is recorded.
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    _stub_selection(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("sampler unavailable")

    # The entry point catches sampling failures and falls back to transcript
    # candidates (Req 15.2); the pipeline must not fail.
    monkeypatch.setattr(pl.visual_selection, "sample_keyframes", boom)

    src = make_video("s.mp4", duration=4.0, w=640, h=360)
    opts = ProcessingOptions(
        captions=False, metadata=False, aspect="9:16", visual_selection=True
    )
    clips = pl.run_pipeline(
        src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp"
    )
    assert len(clips) == 1
    clip = clips[0]
    assert "visual_selection" in clip.effects_applied
    assert (tmp_path / "clips" / clip.filename).exists()


# ===========================================================================
# 9.4 — Property 28: no external network when external features disabled
# ===========================================================================
# Feature: tier1-creator-output-upgrade, Property 28: No external network when external features are disabled
@requires_ffmpeg
def test_p28_no_external_provider_when_download_disabled(
    make_video, tmp_path, monkeypatch
):
    """Validates: Requirements 18.3

    With external downloading disabled (``broll_allow_download=False``) — even
    with an API key configured and b-roll enabled — the pipeline never
    constructs the ExternalProvider, so no downloader/network call can occur.
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    _stub_selection(monkeypatch)

    constructed: list = []

    class SpyExternal(pl.broll.ExternalProvider):
        def __init__(self, *args, **kwargs):
            constructed.append((args, kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pl.broll, "ExternalProvider", SpyExternal)
    # A key IS configured, but downloading is OFF -> external path must not run.
    monkeypatch.setattr(pl.settings, "broll_provider_api_key", "fake-key")
    monkeypatch.setattr(pl.settings, "broll_allow_download", False)
    empty = tmp_path / "empty_broll"
    empty.mkdir()
    monkeypatch.setattr(pl.settings, "broll_dir", empty)

    src = make_video("s.mp4", duration=4.0, w=640, h=360)
    opts = ProcessingOptions(
        captions=False, metadata=False, aspect="9:16",
        broll=True, broll_intensity="standard",
        asset_sourcing_mode="local_then_external",
    )
    clips = pl.run_pipeline(
        src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp"
    )
    assert len(clips) == 1
    assert constructed == []  # ExternalProvider never constructed -> no network


# Feature: tier1-creator-output-upgrade, Property 28: No external network when external features are disabled
@requires_ffmpeg
def test_p28_permissibility_forces_local_only_no_external(
    make_video, tmp_path, monkeypatch
):
    """Validates: Requirements 18.3

    Under ``permissibility_mode`` ``effective_options`` forces ``local_only``
    sourcing; combined with downloading disabled, the ExternalProvider is never
    constructed regardless of the requested sourcing mode.
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    _stub_selection(monkeypatch)

    constructed: list = []

    class SpyExternal(pl.broll.ExternalProvider):
        def __init__(self, *args, **kwargs):
            constructed.append((args, kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pl.broll, "ExternalProvider", SpyExternal)
    monkeypatch.setattr(pl.settings, "broll_provider_api_key", "fake-key")
    monkeypatch.setattr(pl.settings, "broll_allow_download", False)
    empty = tmp_path / "empty_broll"
    empty.mkdir()
    monkeypatch.setattr(pl.settings, "broll_dir", empty)

    src = make_video("s.mp4", duration=4.0, w=640, h=360)
    opts = ProcessingOptions(
        captions=False, metadata=False, aspect="9:16",
        broll=True, asset_sourcing_mode="local_then_external",
        permissibility_mode=True,
    )
    clips = pl.run_pipeline(
        src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp"
    )
    assert len(clips) == 1
    assert constructed == []
