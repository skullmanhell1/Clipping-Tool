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
from pathlib import Path as _Path
import sys as _sys

from hypothesis import given, settings, strategies as st

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

    opts_on = ProcessingOptions(
        captions=False, metadata=False, aspect="9:16", speaker_reframe=True
    )
    clips = pl.run_pipeline(
        src, opts_on, clips_dir=tmp_path / "c_on", temp_dir=tmp_path / "t_on"
    )
    assert len(clips) == 3
    assert len(calls) == 1  # once per source, independent of clip count

    # (B) both toggles OFF -> no diarisation and no face sampling at all.
    calls_off = _spy_diarize_source(monkeypatch, pl)
    sampler_off = CannedSampler([[FaceBox(0.0, 100, 100, 80, 80)]])
    monkeypatch.setattr(pl, "FRAME_SAMPLER", sampler_off)
    _stub_selection_multi(monkeypatch, [(0.0, 1.5), (1.5, 3.0)])
    opts_off = ProcessingOptions(captions=False, metadata=False, aspect="9:16")
    clips_off = pl.run_pipeline(
        src, opts_off, clips_dir=tmp_path / "c_off", temp_dir=tmp_path / "t_off"
    )
    assert len(clips_off) == 2
    assert calls_off == []          # diariser never invoked
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
    opts = ProcessingOptions(captions=False, metadata=False, aspect="9:16")  # both OFF
    clips = pl.run_pipeline(
        src, opts, clips_dir=tmp_path / "c", temp_dir=tmp_path / "t"
    )

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
def test_p27_reframe_auto_enables_diarisation_without_flip_sdr(
    make_video, tmp_path, monkeypatch
):
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
        captions=False, metadata=False, aspect="9:16",
        speaker_reframe=True, diarization=False,
    )
    clips = pl.run_pipeline(
        src, opts, clips_dir=tmp_path / "c", temp_dir=tmp_path / "t"
    )

    assert len(clips) == 1
    assert len(calls) == 1                 # diarisation happened internally
    assert opts.diarization is False       # persisted toggle NOT flipped/mutated
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
        captions=False, metadata=False, aspect="9:16",
        speaker_reframe=True, permissibility_mode=True,
    )
    clips = pl.run_pipeline(
        src, opts, clips_dir=tmp_path / "c", temp_dir=tmp_path / "t"
    )

    assert len(clips) == 1
    clip = clips[0]
    assert backend.calls == []                                  # backend bypassed (offline only)
    assert "diarization:transcript" in clip.effects_applied     # offline segmentation used
    assert "speaker_reframe:follow_active" in clip.effects_applied
    assert constructed == []                                    # no external provider / network


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
    opts_a = ProcessingOptions(captions=False, metadata=False, aspect="9:16", speaker_reframe=True)
    clips_a = pl.run_pipeline(
        src, opts_a, clips_dir=tmp_path / "ca", temp_dir=tmp_path / "ta"
    )
    assert len(clips_a) == 1
    assert "speaker_reframe_degraded" in clips_a[0].effects_applied
    assert (tmp_path / "ca" / clips_a[0].filename).exists()

    # (b) forced FFmpegError on the speaker-aware pass -> fallback clip.
    monkeypatch.setattr(pl.reframe, "apply_speaker_reframe", _reframe_raise_ffmpeg)
    monkeypatch.setattr(pl, "FRAME_SAMPLER", CannedSampler([[FaceBox(0.0, 100, 100, 80, 80)]]))
    opts_b = ProcessingOptions(captions=False, metadata=False, aspect="9:16", speaker_reframe=True)
    clips_b = pl.run_pipeline(
        src, opts_b, clips_dir=tmp_path / "cb", temp_dir=tmp_path / "tb"
    )
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
        captions=False, metadata=False, aspect="9:16",
        speaker_reframe=True, permissibility_mode=True,
    )
    clips_c = pl.run_pipeline(
        src, opts_c, clips_dir=tmp_path / "cc", temp_dir=tmp_path / "tc"
    )
    assert len(clips_c) == 1
    assert backend.calls == []  # permissibility -> backend bypassed, no network
    assert "speaker_reframe:follow_active" in clips_c[0].effects_applied
    assert (tmp_path / "cc" / clips_c[0].filename).exists()
