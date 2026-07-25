"""Property + unit tests for speaker diarisation (``worker/diarization.py``).

Covers tasks 2.5–2.13. Property tests use ``hypothesis`` with
``@settings(max_examples=100)``, one property per test, tagged with the design
property text (``# Feature: speaker-diarization-reframe, Property N: ...``) and
a ``Validates: Requirements ...`` docstring.

All tests are pure/offline/CPU-only — no ffmpeg, no OpenCV, no network. Reuses
the ``FakeWord`` helper from ``tests/conftest.py`` and the
``FakeDiarizationBackend`` / ``RaisingDiarizationBackend`` doubles from
``tests/fakes.py``.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import FakeWord
from tests.fakes import FakeDiarizationBackend, RaisingDiarizationBackend
from worker.diarization import (
    Speaker_Turn,
    diarize_source,
    segment_by_words,
    turns_from_dicts,
    turns_to_dicts,
)

_EPS = 1e-6

_TOKENS = ["the", "strategy", "and", "growth", "win", "now", "algorithm", "go"]


# --------------------------------------------------------------------------- #
# Strategies                                                                    #
# --------------------------------------------------------------------------- #
@st.composite
def _timeline_and_duration(draw):
    """An ordered, non-overlapping clip-relative Word_Timeline (built from
    ``FakeWord``) paired with a duration ``>=`` the max word end.

    Gaps between words vary from 0 up to well beyond the default pause gap
    (0.9s), so multiple speech runs / speaker turns arise across examples.
    """
    n = draw(st.integers(min_value=0, max_value=12))
    words = []
    t = draw(st.floats(min_value=0.0, max_value=2.0))
    for _ in range(n):
        gap = draw(st.floats(min_value=0.0, max_value=2.5))
        dur = draw(st.floats(min_value=0.05, max_value=1.5))
        start = t + gap
        end = start + dur
        words.append(FakeWord(round(start, 3), round(end, 3), draw(st.sampled_from(_TOKENS))))
        t = end
    max_end = max((w.end for w in words), default=0.0)
    duration = round(max_end + draw(st.floats(min_value=0.0, max_value=3.0)), 3)
    if duration <= 0.0:
        duration = 1.0
    return words, duration


@st.composite
def _turn_lists(draw):
    """A list of already-normalised-shaped ``Speaker_Turn``s (for round-trip)."""
    n = draw(st.integers(min_value=0, max_value=8))
    out = []
    for _ in range(n):
        a = round(draw(st.floats(min_value=0.0, max_value=100.0)), 3)
        b = round(draw(st.floats(min_value=0.0, max_value=100.0)), 3)
        label = draw(st.sampled_from(["S1", "S2", "S3", "S4"]))
        out.append(Speaker_Turn(label, min(a, b), max(a, b)))
    return out


# --------------------------------------------------------------------------- #
# 2.5 — Property 1                                                              #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 1: Speaker-turn structural well-formedness
@settings(max_examples=100)
@given(data=_timeline_and_duration())
def test_p1_speaker_turn_structural_well_formedness(data):
    """Validates: Requirements 1.1, 1.4, 1.5, 1.6

    Every produced turn has ``start <= end``, lies within ``[0, D]``, and the
    list is ordered by ascending ``start``.
    """
    words, duration = data
    turns = segment_by_words(words, duration)

    prev = None
    for t in turns:
        assert t.start <= t.end
        assert -_EPS <= t.start <= duration + _EPS
        assert -_EPS <= t.end <= duration + _EPS
        if prev is not None:
            assert t.start >= prev.start - _EPS
        prev = t


# --------------------------------------------------------------------------- #
# 2.6 — Property 2                                                              #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 2: Speaker-turns are non-overlapping
@settings(max_examples=100)
@given(data=_timeline_and_duration())
def test_p2_speaker_turns_non_overlapping(data):
    """Validates: Requirements 2.1

    The ``[start, end)`` intervals of any two distinct produced turns do not
    overlap.
    """
    words, duration = data
    turns = segment_by_words(words, duration)

    for i in range(len(turns)):
        for j in range(i + 1, len(turns)):
            a, b = turns[i], turns[j]
            # Non-overlap of half-open [start, end) intervals.
            assert a.end <= b.start + _EPS or b.end <= a.start + _EPS


# --------------------------------------------------------------------------- #
# 2.7 — Property 3                                                              #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 3: Adjacent same-label contiguous turns are merged
@settings(max_examples=100)
@given(data=_timeline_and_duration())
def test_p3_adjacent_same_label_contiguous_turns_merged(data):
    """Validates: Requirements 1.7

    No two adjacent produced turns share the same label while being contiguous
    (such pairs are always merged into one, so a shared label implies a gap).
    """
    words, duration = data
    turns = segment_by_words(words, duration)

    for a, b in zip(turns, turns[1:]):
        if a.speaker_label == b.speaker_label:
            # Same label => must be separated by a real gap (not contiguous).
            assert b.start > a.end + _EPS


# --------------------------------------------------------------------------- #
# 2.8 — Property 4                                                              #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 4: Empty timeline yields zero turns without failure
@settings(max_examples=100)
@given(duration=st.floats(min_value=0.0, max_value=1000.0))
def test_p4_empty_timeline_yields_zero_turns(duration):
    """Validates: Requirements 2.2

    An empty Word_Timeline produces zero turns and does not raise.
    """
    assert segment_by_words([], duration) == []


# --------------------------------------------------------------------------- #
# 2.9 — Property 5                                                              #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 5: Speaker cap is never exceeded
@settings(max_examples=100)
@given(data=_timeline_and_duration(), max_speakers=st.integers(min_value=1, max_value=5))
def test_p5_speaker_cap_never_exceeded(data, max_speakers):
    """Validates: Requirements 2.4, 2.5

    The number of distinct labels never exceeds the configured maximum ``M``
    across a range of caps; excess speakers are merged into retained labels
    rather than exceeding the cap (every speech run still yields a turn).
    """
    words, duration = data
    turns = segment_by_words(words, duration, max_speakers=max_speakers)

    labels = {t.speaker_label for t in turns}
    assert len(labels) <= max_speakers

    # Merging (rather than dropping) keeps coverage: whenever there is speech,
    # at least one turn is produced.
    if any(w.end > w.start for w in words):
        assert turns


# --------------------------------------------------------------------------- #
# 2.10 — Property 6                                                             #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 6: Speaker-turn serialisation round-trip
@settings(max_examples=100)
@given(turns=_turn_lists())
def test_p6_speaker_turn_serialisation_round_trip(turns):
    """Validates: Requirements 3.2

    ``turns_from_dicts(turns_to_dicts(t))`` produces an equivalent list.
    """
    assert turns_from_dicts(turns_to_dicts(turns)) == turns


# --------------------------------------------------------------------------- #
# 2.11 — Property 7                                                             #
# --------------------------------------------------------------------------- #
@st.composite
def _mixed_records(draw):
    """A list mixing valid serialised turn records with clearly malformed ones,
    paired with the list of turns the valid records should parse to (in order).
    """
    _GARBAGE = [
        {},
        {"speaker_label": "S1"},                       # missing start/end
        {"start": 1.0, "end": 2.0},                    # missing label
        {"speaker_label": "S", "start": "abc", "end": "xyz"},  # unparseable
        "not-a-dict",
        None,
        42,
        [1, 2, 3],
    ]
    n = draw(st.integers(min_value=0, max_value=10))
    records: list = []
    expected: list[Speaker_Turn] = []
    for _ in range(n):
        if draw(st.booleans()):
            label = draw(st.sampled_from(["S1", "S2", "S3"]))
            s = round(draw(st.floats(min_value=0.0, max_value=50.0)), 3)
            e = round(draw(st.floats(min_value=0.0, max_value=50.0)), 3)
            rec = {"speaker_label": label, "start": s, "end": e}
            records.append(rec)
            expected.append(Speaker_Turn.from_dict(rec))
        else:
            records.append(draw(st.sampled_from(_GARBAGE)))
    return records, expected


# Feature: speaker-diarization-reframe, Property 7: Malformed turn records are discarded, valid ones retained
@settings(max_examples=100)
@given(data=_mixed_records())
def test_p7_malformed_records_discarded_valid_retained(data):
    """Validates: Requirements 3.3

    Parsing a mixed list keeps exactly the valid records (in order) and drops
    the malformed ones without raising.
    """
    records, expected = data
    assert turns_from_dicts(records) == expected


# --------------------------------------------------------------------------- #
# 2.12 — Property 8                                                            #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 8: Backend absence or failure degrades to offline segmentation
@settings(max_examples=100)
@given(data=_timeline_and_duration())
def test_p8_backend_absence_or_failure_degrades_to_offline(data):
    """Validates: Requirements 4.2, 4.4

    With no backend, ``diarize_source`` returns exactly the offline
    ``segment_by_words`` result (recording ``diarization:transcript``); with a
    raising backend it returns the SAME offline result AND records the
    ``diarization_degraded`` degradation marker.
    """
    words, duration = data
    offline = segment_by_words(words, duration)

    # No backend -> pure offline segmentation.
    notes_none: list[str] = []
    result_none = diarize_source(words, duration, notes=notes_none)
    assert result_none == offline
    assert "diarization:transcript" in notes_none

    # Raising backend -> same offline result + degradation marker.
    notes_raise: list[str] = []
    backend = RaisingDiarizationBackend()
    result_raise = diarize_source(words, duration, backend=backend, notes=notes_raise)
    assert result_raise == offline
    assert "diarization_degraded" in notes_raise
    assert backend.calls  # the backend was actually consulted before degrading


# --------------------------------------------------------------------------- #
# 2.13 — Unit tests: backend alignment, single-speaker, marker selection        #
# --------------------------------------------------------------------------- #
def test_backend_spans_aligned_to_word_boundaries_and_model_marker():
    """Validates: Requirements 1.3, 4.1

    An injected backend's spans are snapped to Word_Timeline boundaries and the
    ``diarization:model`` marker is recorded.
    """
    words = [
        FakeWord(0.0, 1.0, "a"),
        FakeWord(1.0, 2.0, "b"),
        FakeWord(3.0, 4.0, "c"),
    ]
    duration = 4.0
    boundaries = {0.0, 1.0, 2.0, 3.0, 4.0}
    # Spans deliberately offset from the real word boundaries.
    backend = FakeDiarizationBackend(spans=[("A", 0.05, 1.9), ("B", 3.1, 3.95)])

    notes: list[str] = []
    turns = diarize_source(words, duration, backend=backend, notes=notes)

    assert "diarization:model" in notes
    assert backend.calls  # backend was consulted
    assert turns  # produced some turns
    for t in turns:
        assert t.start in boundaries
        assert t.end in boundaries


def test_single_speaker_input_yields_one_turn():
    """Validates: Requirements 2.3

    A single continuous speech run (no gap exceeding the pause gap) yields
    exactly one turn with one label.
    """
    words = [
        FakeWord(0.0, 0.5, "one"),
        FakeWord(0.6, 1.0, "continuous"),
        FakeWord(1.1, 1.5, "run"),
    ]
    turns = segment_by_words(words, 2.0)
    assert len(turns) == 1
    assert turns[0].speaker_label == "S1"


def test_marker_selection_transcript_model_degraded():
    """Validates: Requirements 1.3, 4.1

    Marker selection: no backend or permissibility -> ``diarization:transcript``;
    a working backend -> ``diarization:model``; a raising backend ->
    ``diarization_degraded``.
    """
    words = [FakeWord(0.0, 1.0, "a"), FakeWord(2.0, 3.0, "b")]
    duration = 3.0

    # No backend -> transcript.
    notes: list[str] = []
    diarize_source(words, duration, notes=notes)
    assert notes == ["diarization:transcript"]

    # Permissibility on (even with a backend present) -> transcript only.
    notes = []
    diarize_source(
        words, duration,
        backend=FakeDiarizationBackend(spans=[("A", 0.0, 1.0), ("B", 2.0, 3.0)]),
        permissibility=True, notes=notes,
    )
    assert notes == ["diarization:transcript"]

    # Working backend -> model.
    notes = []
    diarize_source(
        words, duration,
        backend=FakeDiarizationBackend(spans=[("A", 0.0, 1.0), ("B", 2.0, 3.0)]),
        notes=notes,
    )
    assert notes == ["diarization:model"]

    # Raising backend -> degraded.
    notes = []
    diarize_source(words, duration, backend=RaisingDiarizationBackend(), notes=notes)
    assert notes == ["diarization_degraded"]
