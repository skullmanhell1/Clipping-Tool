"""Property + unit/edge tests for prompt / visual clip finding (Feature C).

Covers tasks 8.3–8.6. Property tests use ``hypothesis`` with
``@settings(max_examples=100)``, one property per test, tagged with the design
property text and a ``Validates: Requirements ...`` docstring. Reuses the
``Transcript``/``TranscriptSegment``/``Word`` types from ``worker.transcribe``,
``ClipCandidate`` from ``worker.selection``, and ``MockLLMClient`` from
``worker.llm_client``. All tests are fully offline: the deterministic paths use
``strategy="fixed"`` (no ffmpeg) and an injected keyframe ``sampler``.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import requires_ffmpeg
from worker import selection as sel
from worker import visual_selection
from worker.llm_client import MockLLMClient
from worker.selection import ClipCandidate
from worker.transcribe import Transcript, TranscriptSegment, Word
from worker.visual_selection import (
    Keyframe,
    derive_visual_cues,
    merge_scores,
    sample_keyframes,
    select_moments_visual,
)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _fake_sampler(source, t):
    """A sampler that returns a (non-existent) path so no ffmpeg is needed.

    ``derive_visual_cues`` cannot open the path, so brightness/motion degrade to
    ``0.0`` — deterministic and offline.
    """
    return f"/tmp/kf_{t:.3f}.jpg"


def _make_transcript(specs):
    """Build a Transcript from ``[(start, end, text), ...]`` specs."""
    segs = [
        TranscriptSegment(start=s, end=e, text=txt, words=[Word(s, e, txt)])
        for (s, e, txt) in specs
    ]
    return Transcript(language="en", segments=segs)


@st.composite
def _clean_transcript(draw):
    """A contiguous, non-overlapping transcript with distinct boundaries.

    Distinct, strictly-increasing segment starts and ends (with a positive gap
    between segments) make ``snap_to_sentences`` idempotent on those boundaries.
    """
    n = draw(st.integers(min_value=1, max_value=6))
    specs = []
    cursor = 0.0
    for i in range(n):
        seg_len = draw(st.floats(min_value=1.0, max_value=5.0))
        start = round(cursor, 3)
        end = round(start + seg_len, 3)
        specs.append((start, end, f"seg{i}"))
        gap = draw(st.floats(min_value=0.5, max_value=2.0))
        cursor = round(end + gap, 3)
    return specs, round(cursor, 3)


def _mock_from_segments(specs):
    """A MockLLMClient returning candidates aligned to real segment boundaries."""
    n = len(specs)
    pairs = [(0, 0)] if n == 1 else [(0, 0), (0, n - 1), (n - 1, n - 1)]
    cands = []
    for idx, (j, k) in enumerate(pairs):
        cands.append(
            {
                "start": specs[j][0],
                "end": specs[k][1],
                "score": 90 - idx * 10,
                "reason": f"reason{idx}",
                "title": f"title{idx}",
            }
        )
    return MockLLMClient(responses=[json.dumps(cands)])


# --------------------------------------------------------------------------- #
# 8.3 — Property 21
# --------------------------------------------------------------------------- #
# Feature: tier1-creator-output-upgrade, Property 21: Visual merge yields ranked, shape-preserving, snapped candidates
@settings(max_examples=100, suppress_health_check=[HealthCheck.filter_too_much])
@given(data=_clean_transcript())
def test_p21_visual_merge_ranked_shape_preserving_snapped(data):
    """Validates: Requirements 14.2, 14.4, 14.5

    ``merge_scores`` yields a ranking ordered by combined score, every item
    keeps the ``ClipCandidate`` shape, and (via ``select_moments_visual``) each
    candidate's start/end are snapped to natural segment boundaries.
    """
    specs, duration = data
    transcript = _make_transcript(specs)

    # Direct merge_scores check: ordered by combined score, shape preserved.
    raw_cands = [
        ClipCandidate(start=0.0, end=1.0, score=10.0, reason="a", title="A", text="t1"),
        ClipCandidate(start=1.0, end=2.0, score=50.0, reason="b", title="B", text="t2"),
        ClipCandidate(start=2.0, end=3.0, score=30.0, reason="c", title="C", text="t3"),
    ]
    frames = [Keyframe(t=0.5, path="/x.jpg", brightness=0.0, motion=0.0)]
    merged = merge_scores(raw_cands, frames)
    scores = [m.score for m in merged]
    assert scores == sorted(scores, reverse=True)
    for m in merged:
        assert isinstance(m, ClipCandidate)
        for attr in ("start", "end", "score", "reason", "title", "text"):
            assert hasattr(m, attr)

    # End-to-end: candidates are snapped to segment boundaries.
    mock = _mock_from_segments(specs)
    opts = _Opts(visual_selection=True, strategy="ai", num_clips="max")
    result = select_moments_visual(
        transcript, opts, "fake.mp4", duration, client=mock, sampler=_fake_sampler
    )

    assert result  # at least one candidate produced
    out_scores = [c.score for c in result]
    assert out_scores == sorted(out_scores, reverse=True)

    starts = {round(s, 2) for (s, _e, _t) in specs}
    ends = {round(e, 2) for (_s, e, _t) in specs}
    for c in result:
        assert isinstance(c, ClipCandidate)
        for attr in ("start", "end", "score", "reason", "title", "text"):
            assert hasattr(c, attr)
        assert c.start in starts
        assert c.end in ends


# --------------------------------------------------------------------------- #
# 8.4 — Property 22
# --------------------------------------------------------------------------- #
# Feature: tier1-creator-output-upgrade, Property 22: Keyframe sampling is bounded
@settings(max_examples=100)
@given(
    duration=st.floats(min_value=0.0, max_value=600.0),
    limit=st.integers(min_value=0, max_value=40),
)
def test_p22_keyframe_sampling_is_bounded(duration, limit):
    """Validates: Requirements 15.1

    For any duration and limit, the number of sampled keyframes never exceeds
    the configured limit (an injected sampler avoids invoking ffmpeg).
    """
    frames = sample_keyframes("src.mp4", duration, limit=limit, sampler=_fake_sampler)
    assert len(frames) <= limit
    for f in frames:
        assert isinstance(f, Keyframe)
        assert 0.0 <= f.t <= duration + 1e-6


# --------------------------------------------------------------------------- #
# 8.5 — Property 23
# --------------------------------------------------------------------------- #
# Feature: tier1-creator-output-upgrade, Property 23: Visual selection degrades to transcript-only and is a pass-through when disabled
@settings(max_examples=100, suppress_health_check=[HealthCheck.filter_too_much])
@given(data=_clean_transcript())
def test_p23_degrades_to_transcript_only_and_passthrough_when_disabled(data):
    """Validates: Requirements 13.3, 15.2, 15.3, 15.4

    Disabled visual selection equals ``select_moments``; and with it enabled but
    keyframe sampling failing / returning nothing (and no LLM), the outcome is
    the transcript-only ``select_moments`` result.
    """
    specs, duration = data
    transcript = _make_transcript(specs)

    # Pass-through when disabled (Req 15.4).
    opts_off = _Opts(visual_selection=False, strategy="fixed", num_clips="3")
    got_off = select_moments_visual(
        transcript, opts_off, "fake.mp4", duration, sampler=_fake_sampler
    )
    expected = sel.select_moments(transcript, opts_off, "fake.mp4", duration)
    assert got_off == expected

    opts_on = _Opts(visual_selection=True, strategy="fixed", num_clips="3")

    # Sampler raises -> degrade to transcript-only (Req 15.2).
    def _boom(source, t):
        raise RuntimeError("keyframe sampling failed")

    got_raise = select_moments_visual(transcript, opts_on, "fake.mp4", duration, sampler=_boom)
    assert got_raise == expected

    # Sampler returns nothing (empty frames) -> transcript-only (Reqs 15.2, 15.3).
    got_empty = select_moments_visual(
        transcript, opts_on, "fake.mp4", duration, sampler=lambda s, t: None
    )
    assert got_empty == expected


# --------------------------------------------------------------------------- #
# 8.6 — Unit / edge tests
# --------------------------------------------------------------------------- #
class _Opts:
    """A minimal ProcessingOptions-like stand-in for selection.

    Uses only the attributes the selection code reads, kept tiny so tests stay
    fast and independent of unrelated option fields. ``dataclasses.replace`` in
    the module works on real ``ProcessingOptions``; here we implement the same
    surface plus a ``__replace__``-compatible shape via ``clone``.
    """

    def __init__(
        self,
        *,
        visual_selection=False,
        strategy="fixed",
        num_clips="auto",
        clip_length="auto",
        topic="",
        vibe="",
        selection_prompt="",
    ):
        self.visual_selection = visual_selection
        self.strategy = strategy
        self.num_clips = num_clips
        self.clip_length = clip_length
        self.topic = topic
        self.vibe = vibe
        self.selection_prompt = selection_prompt


def _real_opts(**kwargs):
    """Build a real ProcessingOptions so dataclasses.replace works (prompt bias)."""
    from worker.models import ProcessingOptions

    return ProcessingOptions(**kwargs)


_SIX_SPECS = [
    (0.0, 2.0, "alpha"),
    (3.0, 5.0, "beta"),
    (6.0, 8.0, "gamma"),
    (9.0, 11.0, "delta"),
    (12.0, 14.0, "epsilon"),
    (15.0, 17.0, "zeta"),
]


def test_selection_prompt_reaches_llm_request():
    """Validates: Requirements 13.1, 13.2 — prompt reaches the LLM request."""
    transcript = _make_transcript(_SIX_SPECS)
    mock = MockLLMClient(responses=["[]"])  # empty -> deterministic fallback
    opts = _real_opts(
        visual_selection=True,
        strategy="ai",
        num_clips="max",
        selection_prompt="find every moment where the speaker laughs",
    )
    select_moments_visual(transcript, opts, "fake.mp4", 17.0, client=mock, sampler=_fake_sampler)
    joined = " ".join(c["prompt"] for c in mock.calls)
    assert "find every moment where the speaker laughs" in joined


def test_num_clips_cap_is_honoured():
    """Validates: Requirements 13.4 — the num_clips cap is enforced."""
    transcript = _make_transcript(_SIX_SPECS)
    mock = _mock_from_segments(_SIX_SPECS)  # yields up to 3 candidates
    opts = _Opts(visual_selection=True, strategy="ai", num_clips="1")
    result = select_moments_visual(
        transcript, opts, "fake.mp4", 17.0, client=mock, sampler=_fake_sampler
    )
    assert len(result) <= 1


def test_sampler_called_at_most_once_per_source(monkeypatch):
    """Validates: Requirements 17.4 — keyframe sampling runs once per source."""
    transcript = _make_transcript(_SIX_SPECS)
    calls = []
    real = visual_selection.sample_keyframes

    def _spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(visual_selection, "sample_keyframes", _spy)

    opts = _Opts(visual_selection=True, strategy="ai", num_clips="max")
    mock = _mock_from_segments(_SIX_SPECS)
    select_moments_visual(transcript, opts, "fake.mp4", 17.0, client=mock, sampler=_fake_sampler)
    assert len(calls) == 1


def test_no_audio_ranking_path_works():
    """Validates: Requirements 14.3 — no-audio (empty transcript) still ranks."""
    transcript = Transcript(language="en", segments=[])  # no transcript / audio
    opts = _Opts(visual_selection=True, strategy="fixed", num_clips="max")
    result = select_moments_visual(transcript, opts, "fake.mp4", 30.0, sampler=_fake_sampler)
    assert isinstance(result, list)
    assert result  # visual-cue ranking still produces at least one candidate
    for c in result:
        assert isinstance(c, ClipCandidate)


def test_catastrophic_failure_returns_empty(monkeypatch):
    """Validates: Requirements 15.5 — catastrophic failure yields []."""
    transcript = _make_transcript(_SIX_SPECS)

    def _explode(*args, **kwargs):
        raise RuntimeError("selection blew up")

    monkeypatch.setattr(sel, "select_moments", _explode)

    def _boom(source, t):
        raise RuntimeError("keyframe sampling failed")

    opts = _Opts(visual_selection=True, strategy="fixed", num_clips="max")
    result = select_moments_visual(transcript, opts, "fake.mp4", 17.0, sampler=_boom)
    assert result == []


def test_keyframe_timestamps_clamped_for_sub_millisecond_durations():
    """Validates: Requirements 15.1 — rounding never pushes t past the duration.

    Regression for duration=0.0078125, limit=13: rounding a bucket midpoint to
    3 decimals used to round up beyond the clip duration.
    """
    duration = 0.0078125
    frames = sample_keyframes("src.mp4", duration, limit=13, sampler=_fake_sampler)
    assert len(frames) == 13
    for f in frames:
        assert 0.0 <= f.t <= duration


def test_derive_visual_cues_degrades_without_imaging_libs():
    """Validates: Requirements 14.1 — cue derivation never fails on bad paths."""
    frames = [
        Keyframe(t=1.0, path="/does/not/exist_1.jpg"),
        Keyframe(t=2.0, path="/does/not/exist_2.jpg"),
    ]
    enriched = derive_visual_cues(frames)
    assert len(enriched) == 2
    for f in enriched:
        assert f.brightness == 0.0
        assert f.motion == 0.0


# --------------------------------------------------------------------------- #
# The transcript/visual blend weight is configurable                            #
# --------------------------------------------------------------------------- #
def _one_candidate_and_frames():
    """A candidate with a known transcript score plus bright, high-motion frames.

    Chosen so the transcript-only and visual-only answers differ sharply, which is what
    makes the weight's effect observable.
    """
    from worker.selection import ClipCandidate
    from worker.visual_selection import Keyframe

    cand = ClipCandidate(start=0.0, end=10.0, score=20.0, reason="r", title="t", text="x")
    # brightness + motion == 2.0 -> visual_score saturates at 1.0 -> 100 on the 0..100 scale.
    frames = [Keyframe(t=5.0, path="frame.jpg", brightness=1.0, motion=1.0)]
    return cand, frames


def test_weight_zero_ignores_the_visual_signal():
    """With weight 0 the combined score is exactly the transcript score."""
    from worker.visual_selection import merge_scores

    cand, frames = _one_candidate_and_frames()
    merged = merge_scores([cand], frames, weight=0.0)
    assert merged[0].score == 20.0


def test_weight_one_ignores_the_transcript_score():
    """With weight 1 the combined score is entirely visual."""
    from worker.visual_selection import merge_scores

    cand, frames = _one_candidate_and_frames()
    merged = merge_scores([cand], frames, weight=1.0)
    assert merged[0].score == 100.0


def test_the_default_weight_comes_from_settings_not_a_literal():
    """The blend is configuration, not a constant.

    Regression: ``merge_scores`` defaulted ``weight`` to a literal 0.5 and its only call
    site never passed the argument, so the 50/50 blend could not be changed at all.
    """
    from config import settings as app_settings
    from worker.visual_selection import merge_scores

    cand, frames = _one_candidate_and_frames()
    original = app_settings.visual_selection_weight
    try:
        app_settings.visual_selection_weight = 0.0
        assert merge_scores([cand], frames)[0].score == 20.0
        app_settings.visual_selection_weight = 1.0
        assert merge_scores([cand], frames)[0].score == 100.0
    finally:
        app_settings.visual_selection_weight = original


def test_the_shipped_default_is_an_even_blend():
    """Pins the out-of-the-box behaviour, so this change is not also a tuning change."""
    from config import Settings

    assert Settings(_env_file=None).visual_selection_weight == 0.5


@pytest.mark.parametrize(
    ("given_weight", "expected"),
    [(-1.0, 20.0), (5.0, 100.0)],
)
def test_out_of_range_weights_are_clamped(given_weight, expected):
    """A weight outside ``[0, 1]`` is clamped rather than producing a corrupt score.

    Unclamped, a negative weight yields scores above the transcript value and a weight
    above 1 yields negative contributions — either silently reorders the ranking.
    """
    from worker.visual_selection import merge_scores

    cand, frames = _one_candidate_and_frames()
    assert merge_scores([cand], frames, weight=given_weight)[0].score == expected


# --------------------------------------------------------------------------- #
# Sampled keyframes do not leak a temp directory                                #
# --------------------------------------------------------------------------- #
def test_sample_keyframes_uses_a_supplied_frames_dir(tmp_path):
    """The default sampler writes into the directory it is given.

    That parameter is what lets the caller own the files' lifetime; without it the
    function had to create its own directory, which nothing then removed.
    """
    from pathlib import Path

    from worker import ffmpeg_utils as fu
    from worker.visual_selection import sample_keyframes

    def fake_thumbnail(src, dest, at, width=160):
        Path(dest).write_bytes(b"jpeg")
        return dest

    original = fu.generate_thumbnail
    fu.generate_thumbnail = fake_thumbnail
    try:
        frames = sample_keyframes("source.mp4", 10.0, limit=3, frames_dir=str(tmp_path / "kf"))
    finally:
        fu.generate_thumbnail = original

    assert len(frames) == 3
    for frame in frames:
        assert Path(frame.path).parent == tmp_path / "kf"
        assert Path(frame.path).exists()


@requires_ffmpeg
def test_visual_selection_leaves_no_keyframe_temp_directory(tmp_path, monkeypatch):
    """A visual-selection run removes its sampled frames.

    Gated on ffmpeg: this drives the real ``select_moments_visual``, whose transcript-free
    path probes the source for its duration before any sampling happens. Without the
    binary that probe fails, the function's own ``except`` swallows it, and sampling is
    never reached — the assertion below then correctly reports that the test proved
    nothing. Stubbing the probe as well would leave almost none of the real code under
    test, so the gate is the honest option. CI installs ffmpeg, so it runs there.

    Regression: ``sample_keyframes`` created its scratch directory with
    ``tempfile.mkdtemp`` and no code path ever deleted it, so every run left a ``kf-*``
    directory of JPEGs behind in the system temp space. On a long-running instance that
    grows without bound, and it falls outside the retention sweeper's remit.
    """
    import tempfile as tempfile_module
    from pathlib import Path

    from worker import ffmpeg_utils as fu
    from worker.models import ProcessingOptions
    from worker.transcribe import Transcript
    from worker.visual_selection import select_moments_visual

    # Redirect the system temp dir so only this test's directories are observable.
    monkeypatch.setattr(tempfile_module, "tempdir", str(tmp_path))

    # No `sampler` is injected: the leak was in the *default* sampler's directory, so
    # injecting one would bypass the code under test entirely. Stubbing thumbnail
    # generation instead keeps the real directory handling in play without ffmpeg.
    thumbnails: list[str] = []

    def fake_thumbnail(src, dest, at, width=160):
        thumbnails.append(dest)
        Path(dest).write_bytes(b"not-a-real-jpeg")
        return dest

    monkeypatch.setattr(fu, "generate_thumbnail", fake_thumbnail)

    options = ProcessingOptions(visual_selection=True, strategy="silence")
    transcript = Transcript(language="en", segments=[])
    select_moments_visual(transcript, options, "source.mp4", 10.0, client=None)

    assert thumbnails, "the default sampler never ran, so the test proved nothing"
    # The directory the frames were written into must be gone.
    frame_dirs = {Path(p).parent for p in thumbnails}
    for directory in frame_dirs:
        assert not directory.exists(), f"keyframe temp directory survived: {directory}"

    leftovers = [p for p in tmp_path.iterdir() if p.is_dir() and p.name.startswith("kf-")]
    assert leftovers == [], f"keyframe temp directories left behind: {leftovers}"
