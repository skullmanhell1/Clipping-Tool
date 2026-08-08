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

import pytest

try:  # module-level helpers (not fixtures) from the shared conftest
    from tests.conftest import FakeWord, options_all_off, requires_ffmpeg
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

    def fake_transcribe(source, language=None, translate=False, **_kw):
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
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=50.0, text=text)],
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
    opts = options_all_off(captions=False, metadata=False, aspect="9:16")
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")

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
        base,
        tmp_path / "out.mp4",
        options_all_off(captions=False),
        words,
        tmp_path,
        broll_resolver=lambda: [],
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
    opts = options_all_off(captions=False, metadata=False, aspect="9:16")
    clips = pl.run_pipeline(
        src,
        opts,
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
        llm_client=None,
    )
    assert len(clips) >= 1
    assert (tmp_path / "clips" / clips[0].filename).exists()


# Feature: tier1-creator-output-upgrade, Property 27: Missing dependencies still produce clips and record degradation
@requires_ffmpeg
def test_p27_broll_enabled_but_no_assets_still_produces_clips(make_video, tmp_path, monkeypatch):
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
        captions=False,
        metadata=False,
        aspect="9:16",
        broll=True,
        broll_intensity="standard",
        asset_sourcing_mode="local_only",
    )
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")
    assert len(clips) == 1
    clip = clips[0]
    assert not any(m.startswith("broll:") for m in clip.effects_applied)
    assert clip.broll_assets == []
    assert (tmp_path / "clips" / clip.filename).exists()


# Feature: tier1-creator-output-upgrade, Property 27: Missing dependencies still produce clips and record degradation
@requires_ffmpeg
def test_p27_visual_selection_failing_sampler_degrades(make_video, tmp_path, monkeypatch):
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
    opts = ProcessingOptions(captions=False, metadata=False, aspect="9:16", visual_selection=True)
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")
    assert len(clips) == 1
    clip = clips[0]
    assert "visual_selection" in clip.effects_applied
    assert (tmp_path / "clips" / clip.filename).exists()


# ===========================================================================
# 9.4 — Property 28: no external network when external features disabled
# ===========================================================================
# Feature: tier1-creator-output-upgrade, Property 28: No external network when external features are disabled
@requires_ffmpeg
def test_p28_no_external_provider_when_download_disabled(make_video, tmp_path, monkeypatch):
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
        captions=False,
        metadata=False,
        aspect="9:16",
        broll=True,
        broll_intensity="standard",
        asset_sourcing_mode="local_then_external",
    )
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")
    assert len(clips) == 1
    assert constructed == []  # ExternalProvider never constructed -> no network


# Feature: tier1-creator-output-upgrade, Property 28: No external network when external features are disabled
@requires_ffmpeg
def test_p28_permissibility_forces_local_only_no_external(make_video, tmp_path, monkeypatch):
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
        captions=False,
        metadata=False,
        aspect="9:16",
        broll=True,
        asset_sourcing_mode="local_then_external",
        permissibility_mode=True,
    )
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")
    assert len(clips) == 1
    assert constructed == []


# ===========================================================================
# Speaker Diarisation & Multi-Speaker Reframe (v0.8.0) — Task 7 (7.2–7.8)
# ---------------------------------------------------------------------------
# These exercise the JUST-WIRED pipeline geometry-stage integration (Task 7.1):
# the once-per-source ``diarization.diarize_source(...)`` call, the module-level
# DI seams ``pipeline.DIAR_BACKEND`` / ``pipeline.FACE_DETECTOR`` /
# ``pipeline.FRAME_SAMPLER``, and the geometry precedence ladder
# (speaker-aware -> single-speaker -> static crop_blur) with its
# ``effects_applied`` degradation markers.
#
# This feature intentionally REUSES design property numbers P22–P28 under a
# DIFFERENT feature tag than the Tier-1 tests earlier in this file, so every
# test below carries a UNIQUE ``_sdr`` function-name suffix and the
# ``# Feature: speaker-diarization-reframe, Property N`` tag to avoid any
# collision with the existing tier1 ``test_p24_*`` / ``test_p27_*`` /
# ``test_p28_*`` tests (which are left untouched).
#
# Balance (matching the existing tier1 pipeline-degradation tests in this file):
#   * pipeline-level properties that drive ``run_pipeline`` are written as tight
#     example-based / spy tests gated on ``requires_ffmpeg`` (a full ffmpeg
#     pipeline per hypothesis example would be far too slow); the heavy
#     speaker-aware geometry pass is mocked (``_reframe_ok`` renders a real but
#     cheap static crop via ``reformat_aspect``, ``_reframe_raise_ffmpeg`` forces
#     the fallback) so each run stays to ~1 ffmpeg pass per clip;
#   * P23 (frame-sampling cap) IS a cheap, fully-offline hypothesis property
#     test: a fake ``cv2`` module drives ``reframe.detect_faces`` so the real
#     ``max_samples`` cap logic is exercised over 100 generated inputs with no
#     ffmpeg and no OpenCV (cv2 is not importable in this environment).
# ===========================================================================
import sys as _sys
from pathlib import Path as _Path

from hypothesis import given, settings
from hypothesis import strategies as st

from worker.effects.reframe import FaceBox

try:  # module-level test doubles from the shared fakes module
    from tests.fakes import CannedSampler, FakeDiarizationBackend
except ImportError:  # pragma: no cover - importable either way under pytest
    from fakes import CannedSampler, FakeDiarizationBackend


def _spy_diarize_source(monkeypatch, pl):
    """Wrap ``pipeline.diarization.diarize_source`` with a call recorder that
    still delegates to the real implementation. Returns the ``calls`` list."""
    calls: list = []
    original = pl.diarization.diarize_source

    def _wrapper(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(pl.diarization, "diarize_source", _wrapper)
    return calls


def _reframe_ok(video, dest, **kwargs):
    """Stand-in for ``reframe.apply_speaker_reframe`` that renders a real (but
    cheap) geometry-prepared clip via the static crop_blur reformat, so the
    pipeline records ``speaker_reframe:<layout>`` and produces a valid clip
    without the heavy detect/track/associate/ffmpeg path."""
    from worker import ffmpeg_utils as fu

    fu.reformat_aspect(video, dest, aspect=kwargs.get("aspect", "9:16"), mode="crop_blur")
    return _Path(dest)


def _reframe_raise_ffmpeg(video, dest, **kwargs):
    """Stand-in for ``reframe.apply_speaker_reframe`` that forces an
    ``FFmpegError`` on the speaker-aware pass to drive the fallback chain."""
    from worker.ffmpeg_utils import FFmpegError

    raise FFmpegError("forced speaker-reframe ffmpeg failure")


def _stub_selection_multi(monkeypatch, spans):
    """Stub ``select_moments`` to emit one whole candidate per ``(start, end)``
    span (fresh objects each call, since the pipeline mutates candidate starts)."""
    import worker.pipeline as pl
    from worker.selection import ClipCandidate

    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [
            ClipCandidate(start=s, end=e, score=90.0 - i, text="hello there")
            for i, (s, e) in enumerate(spans)
        ],
    )


# ===========================================================================
# 7.2 — Property 22: diarisation runs at most once per source; disabled = no work
# ===========================================================================
# Feature: speaker-diarization-reframe, Property 22: Diarisation runs at most once per source; disabled means no work
@requires_ffmpeg
def test_p22_diarization_once_per_source_sdr(make_video, tmp_path, monkeypatch):
    """Validates: Requirements 15.1, 15.4

    With speaker-aware reframe enabled, ``diarize_source`` is invoked at most
    once per ``run_pipeline`` call regardless of how many clips are produced;
    with BOTH diarisation and speaker-reframe disabled it is never invoked and
    the injected frame sampler is never called.
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    src = make_video("s.mp4", duration=6.0, w=640, h=360)

    # (A) enabled + THREE clips -> diarise exactly once (<= once) per source.
    _stub_selection_multi(monkeypatch, [(0.0, 1.5), (1.5, 3.0), (3.0, 4.5)])
    calls = _spy_diarize_source(monkeypatch, pl)
    monkeypatch.setattr(pl, "FRAME_SAMPLER", CannedSampler([[FaceBox(0.0, 100, 100, 80, 80)]]))
    monkeypatch.setattr(pl.reframe, "apply_speaker_reframe", _reframe_ok)

    opts_on = ProcessingOptions(captions=False, metadata=False, aspect="9:16", speaker_reframe=True)
    clips = pl.run_pipeline(src, opts_on, clips_dir=tmp_path / "c_on", temp_dir=tmp_path / "t_on")
    assert len(clips) == 3
    assert len(calls) == 1  # once per source, independent of clip count

    # (B) both toggles OFF -> no diarisation and no face sampling at all.
    calls_off = _spy_diarize_source(monkeypatch, pl)
    sampler_off = CannedSampler([[FaceBox(0.0, 100, 100, 80, 80)]])
    monkeypatch.setattr(pl, "FRAME_SAMPLER", sampler_off)
    _stub_selection_multi(monkeypatch, [(0.0, 1.5), (1.5, 3.0)])
    opts_off = options_all_off(captions=False, metadata=False, aspect="9:16")
    clips_off = pl.run_pipeline(
        src, opts_off, clips_dir=tmp_path / "c_off", temp_dir=tmp_path / "t_off"
    )
    assert len(clips_off) == 2
    assert calls_off == []  # diariser never invoked
    assert sampler_off.calls == []  # frame sampler never invoked


# ===========================================================================
# 7.3 — Property 23: frame sampling is bounded
# ===========================================================================
class _FakeVideoCapture:
    """A cv2.VideoCapture stand-in yielding ``n_frames`` dummy frames at ``fps``."""

    def __init__(self, n_frames, fps):
        self._n = int(n_frames)
        self._fps = float(fps)
        self._i = 0

    def isOpened(self):
        return True

    def get(self, prop):
        if prop == "fps":
            return self._fps
        if prop == "count":
            return self._n
        return 0.0

    def read(self):
        if self._i >= self._n:
            return False, None
        self._i += 1
        return True, self._i  # dummy frame object (ignored by injected detector)

    def release(self):
        pass


class _FakeCv2:
    """Minimal fake ``cv2`` module exposing just what ``_sample_face_boxes``
    needs when an explicit detector is injected (no cascade / colour ops)."""

    CAP_PROP_FPS = "fps"
    CAP_PROP_FRAME_COUNT = "count"

    def __init__(self, n_frames, fps):
        self._n = n_frames
        self._fps = fps

    def VideoCapture(self, path):
        return _FakeVideoCapture(self._n, self._fps)


# Feature: speaker-diarization-reframe, Property 23: Frame sampling is bounded
@settings(max_examples=100, deadline=None)
@given(
    n_frames=st.integers(min_value=0, max_value=3000),
    fps=st.floats(min_value=1.0, max_value=120.0),
    sample_fps=st.floats(min_value=0.5, max_value=60.0),
    cap=st.integers(min_value=1, max_value=200),
)
def test_p23_frame_sampling_bounded_sdr(n_frames, fps, sample_fps, cap):
    """Validates: Requirements 15.2

    For any video length / fps / sampling rate, ``detect_faces`` never yields
    more than the configured ``max_samples`` frames. Driven fully offline via a
    fake ``cv2`` module and an injected no-op detector, so the real cap-widening
    + hard ``break`` logic in ``_sample_face_boxes`` is exercised without ffmpeg
    or OpenCV.
    """
    from worker.effects import reframe as rf

    fake = _FakeCv2(n_frames, fps)
    saved = _sys.modules.get("cv2")
    _sys.modules["cv2"] = fake
    try:
        out = rf.detect_faces(
            "dummy.mp4", sample_fps=sample_fps, max_samples=cap, detector=lambda frame: []
        )
    finally:
        if saved is not None:
            _sys.modules["cv2"] = saved
        else:
            _sys.modules.pop("cv2", None)

    assert len(out) <= cap


# ===========================================================================
# 7.4 — Property 24: the degradation chain always produces geometry + marker
# ===========================================================================
# Feature: speaker-diarization-reframe, Property 24: The degradation chain always produces geometry and records the right marker
@requires_ffmpeg
def test_p24_degradation_chain_records_markers_sdr(make_video, tmp_path, monkeypatch):
    """Validates: Requirements 14.1, 14.2, 14.3, 14.4, 14.5

    For (a) zero diarisation turns, (b) zero face tracks, and (c) a forced
    ffmpeg failure on the speaker-aware pass, the pipeline falls back along the
    chain and records ``speaker_reframe_degraded`` while still producing the
    clip; a successful speaker-aware run instead records
    ``speaker_reframe:<layout>`` plus the diarisation provenance note. (The
    wired pipeline surfaces ``speaker_reframe_degraded`` for the zero-track case
    rather than a separate ``faces_none`` marker.)
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    _stub_selection(monkeypatch)
    src = make_video("s.mp4", duration=4.0, w=640, h=360)
    orig_diar = pl.diarization.diarize_source
    base = dict(captions=False, metadata=False, aspect="9:16", speaker_reframe=True)

    def _run(tag, opts):
        return pl.run_pipeline(
            src, opts, clips_dir=tmp_path / f"c_{tag}", temp_dir=tmp_path / f"t_{tag}"
        )

    # (a) zero diarisation turns -> apply_speaker_reframe raises -> degraded.
    monkeypatch.setattr(pl.diarization, "diarize_source", lambda *a, **k: [])
    monkeypatch.setattr(pl, "FRAME_SAMPLER", CannedSampler([[FaceBox(0.0, 100, 100, 80, 80)]]))
    clips = _run("noturns", ProcessingOptions(**base))
    assert len(clips) == 1
    assert "speaker_reframe_degraded" in clips[0].effects_applied
    assert (tmp_path / "c_noturns" / clips[0].filename).exists()

    # (b) zero face tracks (sampler returns no frames) -> degraded.
    monkeypatch.setattr(pl.diarization, "diarize_source", orig_diar)
    monkeypatch.setattr(pl, "FRAME_SAMPLER", CannedSampler([]))
    clips = _run("notracks", ProcessingOptions(**base))
    assert "speaker_reframe_degraded" in clips[0].effects_applied
    assert (tmp_path / "c_notracks" / clips[0].filename).exists()

    # (c) forced FFmpegError on the speaker-aware pass -> degraded, clip produced.
    monkeypatch.setattr(pl, "FRAME_SAMPLER", CannedSampler([[FaceBox(0.0, 100, 100, 80, 80)]]))
    monkeypatch.setattr(pl.reframe, "apply_speaker_reframe", _reframe_raise_ffmpeg)
    clips = _run("ffmpegerr", ProcessingOptions(**base))
    assert "speaker_reframe_degraded" in clips[0].effects_applied
    assert (tmp_path / "c_ffmpegerr" / clips[0].filename).exists()

    # (success) speaker-aware pass succeeds -> applied-layout marker + diar note.
    monkeypatch.setattr(pl.reframe, "apply_speaker_reframe", _reframe_ok)
    clips = _run("ok", ProcessingOptions(**base))
    assert "speaker_reframe:follow_active" in clips[0].effects_applied
    assert "diarization:transcript" in clips[0].effects_applied
    assert (tmp_path / "c_ok" / clips[0].filename).exists()


# ===========================================================================
# 7.5 — Property 26: all-off reproduces v0.7.0 behaviour
# ===========================================================================
# Feature: speaker-diarization-reframe, Property 26: All-off reproduces v0.7.0 behaviour
@requires_ffmpeg
def test_p26_all_off_reproduces_v070_sdr(make_video, tmp_path, monkeypatch):
    """Validates: Requirements 16.4, 17.2

    With ``diarization`` and ``speaker_reframe`` both disabled the diariser is
    never called, no face sampling occurs, and the clip carries none of the new
    ``speaker_reframe*`` / ``diarization*`` markers — i.e. the exact v0.7.0
    static-reformat geometry path and ``effects_applied``.
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    _stub_selection(monkeypatch)
    calls = _spy_diarize_source(monkeypatch, pl)
    sampler = CannedSampler([[FaceBox(0.0, 100, 100, 80, 80)]])
    monkeypatch.setattr(pl, "FRAME_SAMPLER", sampler)

    src = make_video("s.mp4", duration=4.0, w=640, h=360)
    opts = options_all_off(captions=False, metadata=False, aspect="9:16")  # both OFF
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "c", temp_dir=tmp_path / "t")

    assert len(clips) == 1
    clip = clips[0]
    assert calls == []
    assert sampler.calls == []
    for marker in clip.effects_applied:
        assert not marker.startswith("speaker_reframe"), marker
        assert not marker.startswith("diarization"), marker
    assert (tmp_path / "c" / clip.filename).exists()


# ===========================================================================
# 7.6 — Property 27: reframe auto-enables diarisation without flipping the toggle
# ===========================================================================
# Feature: speaker-diarization-reframe, Property 27: Reframe auto-enables diarisation without flipping the persisted toggle
@requires_ffmpeg
def test_p27_reframe_auto_enables_diarisation_without_flip_sdr(make_video, tmp_path, monkeypatch):
    """Validates: Requirements 16.5

    With ``speaker_reframe`` enabled and ``diarization`` disabled, diarisation
    still runs (the reframe needs it) yet the persisted ``diarization`` option
    is never mutated — it remains ``False`` after ``run_pipeline``.
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    _stub_selection(monkeypatch)
    calls = _spy_diarize_source(monkeypatch, pl)
    monkeypatch.setattr(pl, "FRAME_SAMPLER", CannedSampler([[FaceBox(0.0, 100, 100, 80, 80)]]))
    monkeypatch.setattr(pl.reframe, "apply_speaker_reframe", _reframe_ok)

    src = make_video("s.mp4", duration=4.0, w=640, h=360)
    opts = ProcessingOptions(
        captions=False,
        metadata=False,
        aspect="9:16",
        speaker_reframe=True,
        diarization=False,
    )
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "c", temp_dir=tmp_path / "t")

    assert len(clips) == 1
    assert len(calls) == 1  # diarisation happened internally
    assert opts.diarization is False  # persisted toggle NOT flipped/mutated
    assert "speaker_reframe:follow_active" in clips[0].effects_applied


# ===========================================================================
# 7.7 — Property 28: permissibility forces offline, local, network-free diarisation
# ===========================================================================
# Feature: speaker-diarization-reframe, Property 28: Permissibility forces offline, local, network-free diarisation
@requires_ffmpeg
def test_p28_permissibility_offline_network_free_sdr(make_video, tmp_path, monkeypatch):
    """Validates: Requirements 19.1, 19.2, 19.3

    Under ``permissibility_mode`` with an injected diarisation backend, the
    backend is bypassed (its ``assign`` is never called) so diarisation uses
    only offline Word_Timeline segmentation (``diarization:transcript``), and no
    external provider is constructed (no network path). A reframed clip is still
    produced from local vision only.
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    _stub_selection(monkeypatch)

    backend = FakeDiarizationBackend(spans=[("SPK1", 0.0, 2.0), ("SPK2", 2.0, 4.0)])
    monkeypatch.setattr(pl, "DIAR_BACKEND", backend)
    monkeypatch.setattr(pl, "FRAME_SAMPLER", CannedSampler([[FaceBox(0.0, 100, 100, 80, 80)]]))
    monkeypatch.setattr(pl.reframe, "apply_speaker_reframe", _reframe_ok)

    # Guard: any external/network provider construction would show up here.
    constructed: list = []

    class SpyExternal(pl.broll.ExternalProvider):
        def __init__(self, *args, **kwargs):
            constructed.append((args, kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pl.broll, "ExternalProvider", SpyExternal)

    src = make_video("s.mp4", duration=4.0, w=640, h=360)
    opts = ProcessingOptions(
        captions=False,
        metadata=False,
        aspect="9:16",
        speaker_reframe=True,
        permissibility_mode=True,
    )
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "c", temp_dir=tmp_path / "t")

    assert len(clips) == 1
    clip = clips[0]
    assert backend.calls == []  # backend bypassed (offline only)
    assert "diarization:transcript" in clip.effects_applied  # offline segmentation used
    assert "speaker_reframe:follow_active" in clip.effects_applied
    assert constructed == []  # no external provider / network


# ===========================================================================
# 7.8 — ffmpeg integration: degradation + permissibility still produce clips
# ===========================================================================
# Feature: speaker-diarization-reframe, Property 24: The degradation chain always produces geometry and records the right marker
@requires_ffmpeg
def test_p24_ffmpeg_degradation_and_permissibility_sdr(make_video, tmp_path, monkeypatch):
    """Validates: Requirements 14.4, 14.6, 19.1, 19.4

    End-to-end with real ffmpeg and an injected ``FRAME_SAMPLER`` (no cv2):
    (a) the sampler yields no frames -> zero face tracks -> the real
    ``apply_speaker_reframe`` raises and the pipeline still produces a clip via
    the static fallback, recording ``speaker_reframe_degraded``; (b) a forced
    ``FFmpegError`` on the speaker-aware pass still yields a clip; (c) under
    ``permissibility_mode`` with a spy backend and canned face tracks, the
    backend is never called and a real reframed clip is still produced.
    """
    import worker.pipeline as pl

    _stub_transcribe(monkeypatch)
    _stub_selection(monkeypatch)
    src = make_video("s.mp4", duration=4.0, w=640, h=360)
    orig_reframe = pl.reframe.apply_speaker_reframe

    # (a) sampler -> no frames -> no tracks -> real reframe raises -> fallback clip.
    monkeypatch.setattr(pl, "FRAME_SAMPLER", CannedSampler([]))
    opts_a = options_all_off(captions=False, metadata=False, aspect="9:16", speaker_reframe=True)
    clips_a = pl.run_pipeline(src, opts_a, clips_dir=tmp_path / "ca", temp_dir=tmp_path / "ta")
    assert len(clips_a) == 1
    assert "speaker_reframe_degraded" in clips_a[0].effects_applied
    assert (tmp_path / "ca" / clips_a[0].filename).exists()

    # (b) forced FFmpegError on the speaker-aware pass -> fallback clip.
    monkeypatch.setattr(pl.reframe, "apply_speaker_reframe", _reframe_raise_ffmpeg)
    monkeypatch.setattr(pl, "FRAME_SAMPLER", CannedSampler([[FaceBox(0.0, 100, 100, 80, 80)]]))
    opts_b = options_all_off(captions=False, metadata=False, aspect="9:16", speaker_reframe=True)
    clips_b = pl.run_pipeline(src, opts_b, clips_dir=tmp_path / "cb", temp_dir=tmp_path / "tb")
    assert len(clips_b) == 1
    assert "speaker_reframe_degraded" in clips_b[0].effects_applied
    assert (tmp_path / "cb" / clips_b[0].filename).exists()

    # (c) permissibility + spy backend + canned tracks -> no backend call, real
    #     reframed clip still produced by the REAL apply_speaker_reframe.
    monkeypatch.setattr(pl.reframe, "apply_speaker_reframe", orig_reframe)
    backend = FakeDiarizationBackend(spans=[("A", 0.0, 2.0), ("B", 2.0, 4.0)])
    monkeypatch.setattr(pl, "DIAR_BACKEND", backend)
    canned = [[FaceBox(t, 100, 100, 80, 80)] for t in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5)]
    monkeypatch.setattr(pl, "FRAME_SAMPLER", CannedSampler(canned))
    opts_c = ProcessingOptions(
        captions=False,
        metadata=False,
        aspect="9:16",
        speaker_reframe=True,
        permissibility_mode=True,
    )
    clips_c = pl.run_pipeline(src, opts_c, clips_dir=tmp_path / "cc", temp_dir=tmp_path / "tc")
    assert len(clips_c) == 1
    assert backend.calls == []  # permissibility -> backend bypassed, no network
    assert "speaker_reframe:follow_active" in clips_c[0].effects_applied
    assert (tmp_path / "cc" / clips_c[0].filename).exists()


# ===========================================================================
# Advanced AV engines (av-engines-foundation) — Task 10.4
# ---------------------------------------------------------------------------
# Property 13: Clip count is invariant under degradation and failure.
#
# This property drives the REAL ``run_pipeline`` with the Task-10 engine hooks
# wired in, but **fully offline**: every ffmpeg/ffprobe touch point
# (``probe``/``cut_segment``/``reformat_aspect``/``generate_thumbnail`` plus the
# compositor pass) is replaced by a stub that writes a placeholder file, so one
# hypothesis example costs a handful of small writes instead of several ffmpeg
# encodes. That is what makes a *property* (rather than a single ffmpeg example)
# affordable here; the two ffmpeg integration examples for this task live in
# ``tests/test_pipeline_effects.py``.
#
# Global-state isolation: ``worker.engines`` owns a process-wide default
# Engine_Registry and Capability_Report which would leak from one hypothesis
# example into the next (a fixture runs once per *test*, not per *example*).
# Every example therefore resets both singletons **inside the property body**,
# builds its own ``Engine_Registry``, and injects it (plus its own
# Capability_Report, storage backend and clock) into the host that
# ``run_pipeline`` constructs — so the default registry stays empty and
# ``host.active`` remains False for the rest of the suite.
# ===========================================================================
import contextlib
import dataclasses
import tempfile
from unittest import mock

from hypothesis import HealthCheck

import worker.ffmpeg_utils as fu
from worker.engines.base import Engine_Stage
from worker.engines.capabilities import Capability_Report, reset_report
from worker.engines.host import Engine_Host
from worker.engines.registry import Engine_Registry, reset_registry

try:  # module-level test doubles / generators from the shared modules
    from tests.fakes import (
        FakeClock,
        FakeEngine,
        RaisingEngine,
        RecordingStorage,
        SlowEngine,
        StaticProber,
    )
    from tests.strategies import (
        st_availability_map,
        st_engine_outcomes,
        st_registrations,
    )
except ImportError:  # pragma: no cover - importable either way under pytest
    from fakes import (
        FakeClock,
        FakeEngine,
        RaisingEngine,
        RecordingStorage,
        SlowEngine,
        StaticProber,
    )
    from strategies import st_availability_map, st_engine_outcomes, st_registrations

#: The two candidate spans every engine run below produces clips for, so "same
#: number of ClipResults" is asserted against a known, non-zero count.
AV_CLIP_SPANS = ((0.0, 2.0), (2.0, 4.0))

#: Source duration the stubbed probe reports (covers both spans above).
AV_SOURCE_DURATION = 4.0


def av_options(flags, **overrides):
    """``ProcessingOptions`` carrying a real ``<engine_id>_enabled`` field per engine.

    ``AV_Engine.is_enabled`` reads its Feature_Flag off the *resolved* options, and
    ``run_pipeline`` passes those options through ``effective_options`` (which may
    return a ``dataclasses.replace`` copy). A dynamically attached attribute could
    be dropped by that copy, so the flags are added as genuine dataclass fields on
    a per-example subclass instead — that survives ``replace`` exactly like every
    other option field.
    """
    names = sorted(flags)
    cls = dataclasses.make_dataclass(
        "Engine_Processing_Options",
        [(f"{name}_enabled", bool, dataclasses.field(default=False)) for name in names],
        bases=(ProcessingOptions,),
    )
    base = dict(captions=False, metadata=False, aspect="9:16")
    base.update(overrides)
    return cls(**base, **{f"{name}_enabled": bool(flags[name]) for name in names})


def av_touch(path, payload=b"stub-media"):
    """Create ``path`` (with parents) so a stubbed ffmpeg step leaves real bytes."""
    path = _Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@contextlib.contextmanager
def av_ffmpeg_free_pipeline(pl, render_calls, *, spans=AV_CLIP_SPANS):
    """Patch every ffmpeg/ffprobe/LLM touch point ``run_pipeline`` reaches.

    ``render_calls`` collects one record per compositor invocation (its
    ``base_clip`` and the ``engine_contributions`` it was handed), so the COMPOSE
    seam can be asserted without running ffmpeg. ``render_clip`` returns ``None``,
    which is the compositor's documented "nothing changed" answer, so the pipeline
    promotes the geometry output to the final clip exactly as it does in
    production.
    """
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    info = fu.MediaInfo(
        duration=AV_SOURCE_DURATION, width=1280, height=720, fps=30.0, has_audio=True
    )

    def fake_probe(path):
        return info

    def fake_cut(source, start, end, dest, reencode=True, **_colour):
        # `**_colour` absorbs O13/O14's `video_filters`/`colour_tags`. This double stands in
        # for the real cut to assert *clip accounting*, so the colour argv is not its
        # subject -- but a stub with a narrower signature than the function it replaces
        # fails on the call rather than on the assertion, which is a confusing way to learn
        # that a parameter was added.
        return av_touch(dest)

    def fake_reformat(source, dest, aspect="9:16", mode="crop_blur", **_colour):
        # `**_colour` absorbs O14's `colour_tags`; see the note on `fake_cut` above.
        return av_touch(dest)

    def fake_thumbnail(source, dest, at=0.0, width=640):
        return av_touch(dest, b"stub-jpeg")

    def fake_render(base_clip, dest, options, words, temp_dir, **kwargs):
        render_calls.append(
            {
                "base_clip": _Path(base_clip),
                "contributions": kwargs.get("engine_contributions"),
            }
        )
        return None

    def fake_transcribe(source, language=None, translate=False, **_kw):
        words = [
            Word(0.2, 0.6, "hello"),
            Word(0.8, 1.2, "there"),
            Word(2.2, 2.6, "my"),
            Word(2.8, 3.2, "friend"),
        ]
        return Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, AV_SOURCE_DURATION, "hello there my friend", words)],
        )

    def fake_select(*args, **kwargs):
        return [
            ClipCandidate(start=s, end=e, score=90.0 - i, text="hello there")
            for i, (s, e) in enumerate(spans)
        ]

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(fu, "probe", fake_probe))
        stack.enter_context(mock.patch.object(fu, "cut_segment", fake_cut))
        stack.enter_context(mock.patch.object(fu, "reformat_aspect", fake_reformat))
        stack.enter_context(mock.patch.object(fu, "generate_thumbnail", fake_thumbnail))
        stack.enter_context(mock.patch.object(pl.compositor, "render_clip", fake_render))
        stack.enter_context(mock.patch.object(pl, "transcribe", fake_transcribe))
        stack.enter_context(mock.patch.object(pl.sel, "select_moments", fake_select))
        yield


@contextlib.contextmanager
def av_injected_host(pl, registry, report, storage, clock):
    """Make ``run_pipeline`` build its host on an ISOLATED registry + collaborators."""

    def factory(options, **kwargs):
        return Engine_Host(
            options,
            registry=registry,
            capabilities=report,
            storage=storage,
            clock=clock,
            **kwargs,
        )

    with mock.patch.object(pl, "Engine_Host", factory):
        yield


def av_engine_double(engine_id, stage, priority, outcome, *, exception, overrun, required, clock):
    """The ``tests.fakes`` double matching one generated engine outcome."""
    if exception is not None:
        return RaisingEngine(
            engine_id, stage, exc=exception, priority=priority, required_capabilities=required
        )
    if overrun:
        return SlowEngine(
            engine_id,
            stage,
            overrun=2.0,
            clock=clock,
            priority=priority,
            time_budget_s=1.0,
            required_capabilities=required,
        )
    return FakeEngine(
        engine_id,
        stage,
        status=outcome["status"],
        markers=outcome["markers"],
        artifacts=outcome["artifacts"],
        plan=outcome["plan"],
        detail=outcome["detail"],
        priority=priority,
        required_capabilities=required,
    )


def av_expected_stage_media(ctx, temp_dir, clips_dir):
    """The media the Pipeline owes an engine at ``ctx.stage`` (its pre-stage file).

    SOURCE sees no clip; AUDIO sees the cut clip; GEOMETRY and COMPOSE see the
    geometry output; POST sees the finished clip. A failing engine can never
    replace media, so every recorded context must name exactly these paths.
    """
    if ctx.stage is Engine_Stage.SOURCE:
        return None
    if ctx.stage is Engine_Stage.AUDIO:
        return temp_dir / f"raw_{ctx.clip_id}.mp4"
    if ctx.stage in (Engine_Stage.GEOMETRY, Engine_Stage.COMPOSE):
        return temp_dir / f"geo_{ctx.clip_id}.mp4"
    return clips_dir / f"clip_{ctx.clip_id}.mp4"


# Feature: av-engines-foundation, Property 13: Clip count is invariant under degradation and failure
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data())
def test_p13_clip_count_invariant_under_degradation_and_failure(data):
    """Validates: Requirements 7.3, 7.5, 8.3, 8.7

    For any capability availability map and any subset of engines forced to raise
    (``FFmpegError`` included) or to overrun their time budget, ``run_pipeline``
    produces the same number of ``ClipResult``s as the all-engines-disabled run of
    the same input, every clip file still exists, and the media handed to each
    stage is exactly the Pipeline's own pre-stage file — no failing engine can
    substitute media. The all-disabled run additionally carries no ``engine:``
    marker and invokes no engine at all.
    """
    import worker.pipeline as pl

    # Per-example isolation of the two process-wide engine singletons.
    reset_registry()
    reset_report()

    registrations = data.draw(st_registrations(min_size=1, max_size=4), label="registrations")
    availability = data.draw(st_availability_map(max_size=4), label="availability")
    capability_ids = sorted(availability)

    clock = FakeClock()
    engines = []
    for index, (engine_id, stage, priority) in enumerate(registrations):
        outcome = data.draw(st_engine_outcomes(engine_id=engine_id), label=f"outcome:{engine_id}")
        exception = outcome["exception"]
        # Cover the FFmpegError case explicitly (Req 8.7), not just by luck.
        if exception is not None and data.draw(st.booleans(), label=f"ffmpeg:{engine_id}"):
            exception = fu.FFmpegError("engine ffmpeg failure")
        overrun = (
            False
            if exception is not None
            else data.draw(st.booleans(), label=f"overrun:{engine_id}")
        )
        required = (capability_ids[index % len(capability_ids)],) if capability_ids else ()
        engines.append(
            av_engine_double(
                engine_id,
                stage,
                priority,
                outcome,
                exception=exception,
                overrun=overrun,
                required=required,
                clock=clock,
            )
        )

    registry = Engine_Registry()
    for engine in engines:
        registry.register(engine)
    report = Capability_Report(StaticProber(availability))
    storage = RecordingStorage()
    engine_ids = [engine.engine_id for engine in engines]

    with tempfile.TemporaryDirectory() as root:
        root = _Path(root)
        source = av_touch(root / "source.mp4", b"stub-source")

        def run(tag, flags):
            clips_dir = root / tag / "clips"
            temp_dir = root / tag / "tmp"
            renders: list = []
            with (
                av_ffmpeg_free_pipeline(pl, renders),
                av_injected_host(pl, registry, report, storage, clock),
            ):
                clips = pl.run_pipeline(
                    source, av_options(flags), clips_dir=clips_dir, temp_dir=temp_dir
                )
            return clips, renders, clips_dir, temp_dir

        # (A) every engine disabled — the reference run.
        off_flags = {engine_id: False for engine_id in engine_ids}
        baseline, baseline_renders, baseline_clips_dir, _ = run("off", off_flags)

        assert len(baseline) == len(AV_CLIP_SPANS)
        assert all(engine.run_count == 0 for engine in engines)  # Req 7.3 / 19.5
        for clip in baseline:
            assert not any(m.startswith("engine:") for m in clip.effects_applied)
            assert (baseline_clips_dir / clip.filename).exists()
        assert len(baseline_renders) == len(AV_CLIP_SPANS)
        assert all(call["contributions"] in (None, []) for call in baseline_renders)

        # (B) every engine enabled — degraded, failing and overrunning included.
        on_flags = {engine_id: True for engine_id in engine_ids}
        treatment, renders, clips_dir, temp_dir = run("on", on_flags)

        # Clip count is invariant (Reqs 7.3, 8.3) and every clip still exists.
        assert len(treatment) == len(baseline)
        for clip in treatment:
            assert (clips_dir / clip.filename).exists()

        # The media handed to each stage is the Pipeline's own pre-stage file, so
        # a failing engine leaves the preceding stage's media in place (Req 8.3).
        for engine in engines:
            for ctx in engine.contexts:
                assert ctx.clip_path == av_expected_stage_media(ctx, temp_dir, clips_dir)

        # The compositor still ran once per clip, always on the geometry output.
        assert len(renders) == len(AV_CLIP_SPANS)
        for call in renders:
            assert call["base_clip"].parent == temp_dir
            assert call["base_clip"].name.startswith("geo_")

        # Non-vacuity: every engine whose required capability is available really
        # was invoked (once per source at SOURCE, once per clip elsewhere), and
        # every engine gated off by a missing capability never ran (Req 7.1).
        for engine in engines:
            runnable = not report.missing(engine.required_capabilities)
            if not runnable:
                expected = 0
            elif engine.stage is Engine_Stage.SOURCE:
                expected = 1
            else:
                expected = len(treatment)
            assert engine.run_count == expected

    reset_registry()
    reset_report()


# ===========================================================================
# Advanced AV engines (av-engines-foundation) — Task 13.1 / 13.2
# ---------------------------------------------------------------------------
# Property 34: All engines off reproduces v0.8.0 exactly.
#
# This is the spec's central backward-compatibility guarantee, so the reference
# it compares against must be a *genuinely un-hooked* Pipeline — not merely a
# Pipeline whose host reports ``active == False``. Monkeypatching
# ``pipeline.Engine_Host`` with a factory that returns an inactive host would
# still CONSTRUCT a host and would still evaluate every ``if host.active`` guard,
# so it could never detect a hook that changed behaviour before the guard.
#
# ``UNHOOKED_PIPELINE`` therefore rebuilds the v0.8.0 code path from the real
# ``worker/pipeline.py`` source: the module is parsed, the single
# ``host = Engine_Host(...)`` construction and all six ``if host.active:`` blocks
# are removed from the syntax tree, and the result is executed as its own module.
# The transform asserts the exact number of removals, asserts that no ``host`` /
# ``Engine_Host`` name survives anywhere in the tree, and finally rebinds
# ``Engine_Host`` in the baseline module to a factory that RAISES — so if the
# strip ever silently stops matching, the baseline run fails loudly instead of
# quietly comparing a hooked run against another hooked run.
#
# The property then compares three runs of the same input and options:
#
#   (A) ``UNHOOKED_PIPELINE``      — no Engine_Host exists at all (true baseline)
#   (B) ``pipeline`` + EMPTY registry   — the real hooked code path, host inactive
#   (C) ``pipeline`` + LOADED registry  — engines registered, every flag off
#
# on clip count, per-clip ``effects_applied``, ffmpeg invocation count, ffprobe
# count, the full ffmpeg argv (and therefore the ``-filter_complex`` string,
# captured by spying on ``compositor._run``), and the recorded stage order
# ``cut -> filler removal -> geometry -> compositor -> thumbnail``.
#
# Every ffmpeg/ffprobe touch point is stubbed, so one hypothesis example costs a
# handful of small writes rather than nine ffmpeg encodes; the real-ffmpeg parity
# example lives in ``test_all_off_ffmpeg_parity_matches_unhooked_baseline`` below
# (task 13.2). As in P13 above, the two process-wide engine singletons are reset
# INSIDE the property body, because a fixture runs once per *test*, not per
# *example*.
# ===========================================================================
import ast as _ast
import re as _re
import types as _types

from worker.engines.registry import get_registry

try:  # the hostile-options generator from the shared strategies module
    from tests.strategies import st_options_mapping
except ImportError:  # pragma: no cover - importable either way under pytest
    from strategies import st_options_mapping

#: The Pipeline's fixed stage order (Req 23.2). Every per-clip stage sequence the
#: recorder observes must be a subsequence of this tuple, in this order.
P34_CANONICAL_STAGES = ("cut", "filler", "geometry", "compositor", "thumbnail")

#: Exact number of engine hook sites in ``worker/pipeline.py``: one host
#: construction plus six ``if host.active:`` guards (source, audio, geometry,
#: compose, post, finish_job). Pinned so the baseline builder fails loudly if the
#: hook shape ever changes.
P34_EXPECTED_STRIP = {"host_assignments": 1, "active_guards": 6}


def _p34_never_construct(*args, **kwargs):  # pragma: no cover - guard, must never run
    raise AssertionError(
        "the un-hooked baseline pipeline constructed an Engine_Host: the AST strip "
        "no longer removes every engine hook, so the P34 baseline is not a genuine "
        "v0.8.0 reference"
    )


def _build_unhooked_pipeline():
    """Return ``worker.pipeline`` with every engine hook removed at the AST level.

    The returned module is a real, importable module object sharing the same
    collaborator modules (``fu``, ``compositor``, ``filler``, ``sel``, ...) as
    ``worker.pipeline``, so patching those module attributes affects both. Its own
    ``Engine_Host`` name is rebound to :func:`_p34_never_construct`.
    """
    import worker.pipeline as pl

    source = _Path(pl.__file__).read_text(encoding="utf-8")
    tree = _ast.parse(source)
    removed = {"host_assignments": 0, "active_guards": 0}

    class _Strip(_ast.NodeTransformer):
        """Drop ``host = Engine_Host(...)`` and every ``if host.active:`` block."""

        def visit_Assign(self, node):
            self.generic_visit(node)
            targets = node.targets
            if len(targets) == 1 and isinstance(targets[0], _ast.Name) and targets[0].id == "host":
                removed["host_assignments"] += 1
                return None
            return node

        def visit_If(self, node):
            self.generic_visit(node)
            test = node.test
            if (
                isinstance(test, _ast.Attribute)
                and test.attr == "active"
                and isinstance(test.value, _ast.Name)
                and test.value.id == "host"
            ):
                removed["active_guards"] += 1
                return node.orelse or None
            return node

    stripped = _Strip().visit(tree)
    _ast.fix_missing_locations(stripped)

    assert removed == P34_EXPECTED_STRIP, (
        f"unexpected engine-hook shape in worker/pipeline.py: {removed}"
    )
    # No ``host`` / ``Engine_Host`` *name* may survive anywhere in the tree, so the
    # baseline cannot construct, consult or finalise a host by any route.
    names = {node.id for node in _ast.walk(stripped) if isinstance(node, _ast.Name)}
    assert "host" not in names, "a 'host' reference survived the strip"
    assert "Engine_Host" not in names, "an 'Engine_Host' reference survived the strip"

    module = _types.ModuleType("worker._pipeline_unhooked_baseline")
    module.__dict__["__file__"] = pl.__file__
    exec(compile(stripped, "<pipeline-unhooked>", "exec"), module.__dict__)
    # Any surviving hook would now blow up instead of silently re-hooking.
    module.Engine_Host = _p34_never_construct
    return module


#: The genuine v0.8.0 (un-hooked) Pipeline, built once for the whole module.
UNHOOKED_PIPELINE = _build_unhooked_pipeline()


@pytest.fixture
def p34_default_registry_restored():
    """Put the process-wide engine registry back exactly as it was found.

    The parity property below resets the two default engine singletons **inside**
    its body (a fixture runs once per *test*, not once per hypothesis *example*),
    and that per-example reset is what makes its ``len(get_registry()) == 0``
    assertion meaningful: whatever the registry held before, the three runs it
    compares must add nothing to it.

    What is no longer true is the assumption that empty is also the registry's
    *resting* state. ``worker/pipeline.py`` and ``api/main.py`` now import
    ``worker/engines/loader.py`` at module scope, so importing either one registers
    the shipped AV engines — registered, Feature_Flag-off. Resetting and walking
    away would therefore strip those production registrations from the rest of the
    session (and ``reset_registry()`` + a later import will **not** re-register
    them: the module is cached and its registration is guarded by
    ``find(...) is None``). So the registrations found on the way in are replayed
    verbatim on the way out. Nothing about the parity assertions changes: the
    property still compares against explicitly built ``Engine_Registry`` instances
    and still requires the default registry to be untouched by all three runs.
    """
    saved = list(get_registry().records())
    try:
        yield saved
    finally:
        reset_registry()
        for record in saved:
            get_registry().register(record.engine, priority=record.priority)
        reset_report()


class P34_Recorder:
    """Records every stubbed ffmpeg/ffprobe touch point in invocation order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.commands: list[list[str]] = []
        self.filter_graphs: list[str] = []
        self.probes = 0

    def stages(self) -> list[str]:
        """Only the pipeline stage events, in order."""
        return [event for event in self.events if event in P34_CANONICAL_STAGES]

    def per_clip(self) -> list[list[str]]:
        """The stage sequence of each clip (each clip begins with its ``cut``)."""
        clips: list[list[str]] = []
        for event in self.stages():
            if event == "cut":
                clips.append([])
            if clips:
                clips[-1].append(event)
        return clips


def p34_canonical(text, root, tag):
    """Strip run-specific paths and clip ids so two runs are comparable.

    Each run needs its own ``clips_dir``/``temp_dir`` and the Pipeline mints a
    fresh ``NN_<hex>`` clip id per clip, so raw ffmpeg argv can never be equal
    across runs. Replacing exactly those two sources of variation — and nothing
    else — keeps the comparison strict: a genuine difference in inputs, filters,
    codecs or maps still shows up.
    """
    rendered = str(text)
    rendered = rendered.replace(str(_Path(root) / tag), "<run>")
    rendered = rendered.replace(str(root), "<root>")
    return _re.sub(r"\d{2}_[0-9a-f]{6}", "<clip>", rendered)


@contextlib.contextmanager
def p34_stubbed_media(module, rec, *, spans=AV_CLIP_SPANS, duration=AV_SOURCE_DURATION):
    """Patch every ffmpeg/ffprobe/transcribe/selection touch point ``module`` reaches.

    ``module`` is either ``worker.pipeline`` or :data:`UNHOOKED_PIPELINE`; the
    ffmpeg-facing collaborators are shared module objects, while ``transcribe`` is
    a name bound in each pipeline module's own namespace and so is patched per
    module. ``compositor._run`` records the argv (including the
    ``-filter_complex`` graph) and creates the output file, exactly as a real
    ffmpeg pass would, so ``render_clip`` returns its usual ``RenderResult``.
    """
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    info = fu.MediaInfo(duration=duration, width=1280, height=720, fps=30.0, has_audio=True)

    def fake_probe(path):
        rec.probes += 1
        return info

    def fake_cut(source, start, end, dest, reencode=True, **_colour):
        # `**_colour` absorbs O13/O14's `video_filters`/`colour_tags`. This double stands in
        # for the real cut to assert *clip accounting*, so the colour argv is not its
        # subject -- but a stub with a narrower signature than the function it replaces
        # fails on the call rather than on the assertion, which is a confusing way to learn
        # that a parameter was added.
        rec.events.append("cut")
        return av_touch(dest)

    def fake_filler(source, keeps, dest, **_colour):
        # `**_colour` absorbs O14's `colour_tags`; see the note on `fake_cut` above.
        rec.events.append("filler")
        return av_touch(dest)

    def fake_reformat(source, dest, aspect="9:16", mode="crop_blur", **_colour):
        # `**_colour` absorbs O14's `colour_tags`; see the note on `fake_cut` above.
        rec.events.append("geometry")
        return av_touch(dest)

    def fake_thumbnail(source, dest, at=0.0, width=640):
        rec.events.append("thumbnail")
        return av_touch(dest, b"stub-jpeg")

    def fake_run(cmd):
        rec.events.append("compositor")
        argv = [str(part) for part in cmd]
        rec.commands.append(argv)
        rec.filter_graphs.append(
            argv[argv.index("-filter_complex") + 1] if "-filter_complex" in argv else ""
        )
        av_touch(_Path(argv[-1]))
        return None

    def fake_transcribe(source, language=None, translate=False, **_kw):
        # "um" / "uh" are real disfluencies, so the untouched filler planner
        # reports ``changed`` and the filler stage genuinely runs when the option
        # is on — no need to stub the planner itself.
        words = [
            Word(0.2, 0.6, "hello"),
            Word(0.7, 1.1, "um"),
            Word(1.2, 1.6, "there"),
            Word(2.2, 2.6, "my"),
            Word(2.7, 3.1, "uh"),
            Word(3.2, 3.6, "friend"),
        ]
        return Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, duration, "hello um there my uh friend", words)],
        )

    def fake_select(*args, **kwargs):
        return [
            ClipCandidate(start=s, end=e, score=90.0 - i, text="hello there")
            for i, (s, e) in enumerate(spans)
        ]

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(module.fu, "probe", fake_probe))
        stack.enter_context(mock.patch.object(module.fu, "cut_segment", fake_cut))
        stack.enter_context(mock.patch.object(module.fu, "reformat_aspect", fake_reformat))
        stack.enter_context(mock.patch.object(module.fu, "generate_thumbnail", fake_thumbnail))
        stack.enter_context(mock.patch.object(module.compositor, "probe", fake_probe))
        stack.enter_context(mock.patch.object(module.compositor, "_run", fake_run))
        stack.enter_context(mock.patch.object(module.filler, "apply_keep_intervals", fake_filler))
        stack.enter_context(mock.patch.object(module, "transcribe", fake_transcribe))
        stack.enter_context(mock.patch.object(module.sel, "select_moments", fake_select))
        yield


#: Effect flags that are safe to vary in a fully offline run: none of them needs
#: an asset library, a model, an LLM or the network.
P34_SAFE_FLAGS = (
    "captions",
    "zoom",
    "transitions",
    "fades",
    "progress_bar",
    "filler_removal",
    "hook_title",
    "caption_keyword_highlight",
    "caption_emoji",
)

P34_COLORS = ("", "vivid", "warm", "cinematic")
P34_PRESETS = ("karaoke", "boxed", "pop", "typewriter")
P34_POSITIONS = ("bottom", "center", "top")
P34_ASPECTS = ("9:16", "1:1", "16:9")

#: Options pinned OFF because they would reach an asset library, a model, an LLM,
#: OpenCV or the network — none of which this offline property may touch. They are
#: exercised elsewhere (the tier1 / sdr tests above, and the ffmpeg parity example
#: below covers the real geometry ladder end to end).
P34_PINNED = {
    "metadata": False,
    "music": "",
    "emoji": "off",
    "broll": False,
    "visual_selection": False,
    "reframe": False,
    "speaker_reframe": False,
    "diarization": False,
    "caption_keyword_ai": False,
    "permissibility_mode": False,
    "asset_sourcing_mode": "off",
    "range_start": None,
    "range_end": None,
    "language": None,
    "translate": False,
    "caption_animation": "",
}


def p34_options(data, flags):
    """Draw one ``ProcessingOptions`` (with every engine Feature_Flag off).

    A hostile ``st_options_mapping`` is merged in first — so ``from_dict`` really
    is fed junk keys and junk values — then the safe effect flags are drawn, then
    the asset/network-dependent fields are pinned. Both baseline and treatment
    runs receive the *same* resulting options object.
    """
    mapping = dict(data.draw(st_options_mapping(), label="junk-options"))
    for name in P34_SAFE_FLAGS:
        mapping[name] = data.draw(st.booleans(), label=name)
    mapping["color"] = data.draw(st.sampled_from(P34_COLORS), label="color")
    mapping["caption_preset"] = data.draw(st.sampled_from(P34_PRESETS), label="preset")
    mapping["caption_position"] = data.draw(st.sampled_from(P34_POSITIONS), label="position")
    mapping["aspect"] = data.draw(st.sampled_from(P34_ASPECTS), label="aspect")
    mapping.update(P34_PINNED)

    sanitized = ProcessingOptions.from_dict(mapping)
    fields = {
        entry.name: getattr(sanitized, entry.name)
        for entry in dataclasses.fields(ProcessingOptions)
    }
    return av_options(flags, **fields)


def p34_run(module, source, options, root, tag, *, registry=None, report=None):
    """Run one Pipeline variant and return everything P34 compares."""
    rec = P34_Recorder()
    clips_dir = _Path(root) / tag / "clips"
    temp_dir = _Path(root) / tag / "tmp"

    def factory(opts, **kwargs):
        return Engine_Host(opts, registry=registry, capabilities=report, **kwargs)

    with p34_stubbed_media(module, rec):
        if registry is None:
            clips = module.run_pipeline(source, options, clips_dir=clips_dir, temp_dir=temp_dir)
        else:
            with mock.patch.object(module, "Engine_Host", factory):
                clips = module.run_pipeline(source, options, clips_dir=clips_dir, temp_dir=temp_dir)

    return {
        "clip_count": len(clips),
        "effects": [list(clip.effects_applied) for clip in clips],
        "ffmpeg_calls": len(rec.stages()),
        "probes": rec.probes,
        "stages": rec.per_clip(),
        "graphs": [p34_canonical(graph, root, tag) for graph in rec.filter_graphs],
        "commands": [[p34_canonical(part, root, tag) for part in argv] for argv in rec.commands],
        "_clips": clips,
        "_clips_dir": clips_dir,
        "_temp_dir": temp_dir,
    }


# Feature: av-engines-foundation, Property 34: All engines off reproduces v0.8.0 exactly
# ``function_scoped_fixture`` is suppressed deliberately: the fixture below is not
# per-example state at all (the body still resets both singletons per example) — it
# only restores the process-wide registrations the module import left behind, once,
# after the whole test. Its not being reset between examples is exactly what is
# wanted.
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(data=st.data())
def test_p34_all_engines_off_reproduces_v080_exactly(p34_default_registry_restored, data):
    """Validates: Requirements 4.3, 9.4, 23.1, 23.2, 23.3

    For any ``ProcessingOptions`` with every engine Feature_Flag off and for any
    registry contents (including empty), the hooked Pipeline produces the same
    clip count, the same ``effects_applied``, the same ffmpeg invocation count and
    the same ffmpeg argv — ``-filter_complex`` string included — as a baseline run
    with **no** ``Engine_Host`` at all, and the recorded stage order remains
    ``cut -> filler removal -> geometry -> compositor -> thumbnail``.
    """
    import worker.pipeline as pl

    # V17's candidate scoring is switched off for this comparison, as the audio mastering
    # stages already are in ``tests/test_kinetic_compositor.py``'s goldens and for the same
    # reason: this test is a statement about *engines* being inert (Req 23.1), and it uses
    # pass-for-pass equality with a reconstructed v0.8.0 pipeline as its mechanism. Smart
    # thumbnail selection genuinely adds ffmpeg passes, so leaving it on would make this fail
    # for a reason that says nothing about engines.
    #
    # The tradeoff, named rather than hidden: each pipeline stage switched off here is a stage
    # this gate stops covering. V17 is covered directly by
    # ``tests/test_visual_and_encoding.py``, including that disabling it restores the exact
    # previous frame choice. If you would rather this gate absorb pipeline changes and act as a
    # full-output pin, that is a defensible alternative and a small change.
    _settings_backup = pl.settings.smart_thumbnail
    pl.settings.smart_thumbnail = False
    try:
        _run_p34_body(pl, data)
    finally:
        pl.settings.smart_thumbnail = _settings_backup


def _run_p34_body(pl, data):
    """The body of P34, extracted so the settings guard above stays readable."""
    # Per-example isolation of the two process-wide engine singletons. The default
    # registry is no longer empty at rest — ``worker/pipeline.py`` imports
    # ``worker/engines/loader.py``, which registers the shipped engines
    # (Feature_Flag-off) — so this reset is what gives the "the default singletons
    # were never touched" assertion below a known starting point, and the
    # ``p34_default_registry_restored`` fixture replays whatever was found once the
    # test is over.
    reset_registry()
    reset_report()

    registrations = data.draw(st_registrations(min_size=1, max_size=4), label="registrations")
    engines = [
        FakeEngine(engine_id, stage, priority=priority, markers=("ran",))
        for engine_id, stage, priority in registrations
    ]
    loaded = Engine_Registry()
    for engine in engines:
        loaded.register(engine)
    empty = Engine_Registry()
    report = Capability_Report(StaticProber({}, default=True))

    options = p34_options(data, {engine.engine_id: False for engine in engines})

    # Every flag really is off, so the host the Pipeline builds is inactive.
    from worker.models import effective_options

    assert (
        Engine_Host(
            effective_options(options),
            job_id="probe",
            temp_dir=_Path("/nonexistent"),
            registry=loaded,
            capabilities=report,
        ).active
        is False
    )

    with tempfile.TemporaryDirectory() as root:
        root = _Path(root)
        source = av_touch(root / "source.mp4", b"stub-source")

        # (A) the genuine v0.8.0 code path: no Engine_Host exists at all.
        baseline = p34_run(UNHOOKED_PIPELINE, source, options, root, "baseline")
        # (B) the hooked code path with an EMPTY registry.
        with_empty = p34_run(pl, source, options, root, "empty", registry=empty, report=report)
        # (C) the hooked code path with engines registered but every flag off.
        with_engines = p34_run(pl, source, options, root, "loaded", registry=loaded, report=report)

        # --- the parity gate itself (Reqs 23.1, 23.3) ---------------------
        for key in (
            "clip_count",
            "effects",
            "ffmpeg_calls",
            "probes",
            "stages",
            "graphs",
            "commands",
        ):
            assert with_empty[key] == baseline[key], f"empty-registry run differs: {key}"
            assert with_engines[key] == baseline[key], f"loaded-registry run differs: {key}"

        # Non-vacuity: clips were really produced and really written.
        assert baseline["clip_count"] == len(AV_CLIP_SPANS)
        for run in (baseline, with_empty, with_engines):
            for clip in run["_clips"]:
                assert (run["_clips_dir"] / clip.filename).exists()

        # Non-vacuity of the ffmpeg-argv comparison: whenever any look/caption
        # effect is enabled the compositor really ran once per clip and really
        # emitted a ``-filter_complex`` graph, so the equality above is comparing
        # real command lines rather than two empty lists.
        effect_on = bool(
            options.captions
            or options.color
            or options.zoom
            or options.transitions
            or options.fades
            or options.progress_bar
        )
        if effect_on:
            assert all(graph for graph in baseline["graphs"])
            assert len(baseline["graphs"]) == len(AV_CLIP_SPANS)
            for sequence in baseline["stages"]:
                assert "compositor" in sequence
        else:
            assert baseline["graphs"] == []

        # --- stage order (Req 23.2) ---------------------------------------
        assert len(baseline["stages"]) == len(AV_CLIP_SPANS)
        for sequence in baseline["stages"]:
            assert sequence[0] == "cut"
            assert sequence == [s for s in P34_CANONICAL_STAGES if s in sequence]
            assert "geometry" in sequence and "thumbnail" in sequence
            if options.filler_removal:
                assert "filler" in sequence

        # --- nothing engine-shaped happened (Reqs 4.3, 9.4, 19.5) ---------
        for engine in engines:
            assert engine.run_count == 0
        for run in (baseline, with_empty, with_engines):
            assert not (run["_temp_dir"] / "engines").exists()
            for markers in run["effects"]:
                assert not any(m.startswith("engine:") for m in markers)

    # The default singletons were never touched by any of the three runs: the
    # emptied-at-example-start registry is still empty, so no run registered
    # anything behind the explicitly built registries the parity used.
    assert len(get_registry()) == 0
    reset_registry()
    reset_report()


# ===========================================================================
# 13.2 — all-off ffmpeg parity on a tiny clip (non-optional, same reason as 13.1)
# ===========================================================================
@requires_ffmpeg
def test_all_off_ffmpeg_parity_matches_unhooked_baseline(make_video, tmp_path, monkeypatch):
    """Validates: Requirements 4.3, 19.5, 23.1, 23.3

    Real ffmpeg, one tiny clip, every engine off: ``compositor.render_clip`` still
    returns ``None`` (nothing enabled -> the caller keeps the geometry output), the
    ffmpeg invocation count equals the un-hooked v0.8.0 baseline's exactly, the
    engine ``-filter_complex``/contribution seam is never exercised, and no
    ``engines/`` directory is created beneath the job ``temp_dir``.
    """
    import worker.pipeline as pl
    from worker.effects import compositor
    from worker.selection import ClipCandidate

    reset_registry()
    reset_report()

    _stub_transcribe(monkeypatch)
    # The baseline module holds its own ``transcribe`` binding; ``sel`` and every
    # ffmpeg collaborator are shared module objects, so patching those once is enough.
    monkeypatch.setattr(UNHOOKED_PIPELINE, "transcribe", pl.transcribe)
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=1.5, score=50.0, text="hello there")],
    )

    calls: list[str] = []
    renders: list[dict] = []
    real_fu_run = fu._run
    real_compositor_run = compositor._run
    real_render_clip = compositor.render_clip

    def counting_fu_run(cmd):
        calls.append("ffmpeg")
        return real_fu_run(cmd)

    def counting_compositor_run(cmd):
        calls.append("ffmpeg")
        return real_compositor_run(cmd)

    def spying_render_clip(*args, **kwargs):
        result = real_render_clip(*args, **kwargs)
        renders.append({"contributions": kwargs.get("engine_contributions"), "result": result})
        return result

    monkeypatch.setattr(fu, "_run", counting_fu_run)
    monkeypatch.setattr(compositor, "_run", counting_compositor_run)
    monkeypatch.setattr(compositor, "render_clip", spying_render_clip)

    src = make_video("s.mp4", duration=2.0, w=320, h=240)
    opts = options_all_off(captions=False, metadata=False, aspect="9:16")

    def run(module, tag):
        calls.clear()
        renders.clear()
        clips_dir = tmp_path / tag / "clips"
        temp_dir = tmp_path / tag / "tmp"
        clips = module.run_pipeline(src, opts, clips_dir=clips_dir, temp_dir=temp_dir)
        return {
            "clips": clips,
            "ffmpeg": len(calls),
            "renders": list(renders),
            "clips_dir": clips_dir,
            "temp_dir": temp_dir,
        }

    hooked = run(pl, "hooked")
    baseline = run(UNHOOKED_PIPELINE, "baseline")

    # One clip each, produced on disk.
    assert len(hooked["clips"]) == len(baseline["clips"]) == 1
    assert (hooked["clips_dir"] / hooked["clips"][0].filename).exists()
    assert (baseline["clips_dir"] / baseline["clips"][0].filename).exists()

    # Same ffmpeg invocation count as the pre-hook baseline (Req 23.3), and a
    # non-zero one (cut + geometry + thumbnail + probes all shell out).
    assert hooked["ffmpeg"] == baseline["ffmpeg"] > 0

    # ``render_clip`` still returns None with nothing enabled, and the engine
    # contribution seam was never used (Reqs 4.3, 23.3).
    assert [r["result"] for r in hooked["renders"]] == [None]
    assert [r["result"] for r in baseline["renders"]] == [None]
    assert all(r["contributions"] in (None, []) for r in hooked["renders"])

    # Identical markers, none of them engine-namespaced (Reqs 23.1, 9.4).
    assert hooked["clips"][0].effects_applied == baseline["clips"][0].effects_applied
    assert not any(m.startswith("engine:") for m in hooked["clips"][0].effects_applied)

    # No workspace root was allocated anywhere under the job temp dir (Req 19.5).
    assert not (hooked["temp_dir"] / "engines").exists()
    assert not list(hooked["temp_dir"].glob("**/engines"))

    reset_registry()
    reset_report()
