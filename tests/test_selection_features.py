"""Speech-rate features for clip selection (S4).

There are no audio features in selection at all — verified by grep across the repository: no
pitch, no energy, no speech rate, no laughter. The LLM sees only ``[i] start-end: text`` lines,
so it cannot tell that a moment was delivered fast, slowly, or after a pause.

Speech rate is the cheapest of those signals because the data already exists: word timestamps
are already produced. This is one pass over a list.

Two things these tests are careful about:

* **Relative rate is the signal; absolute rate is context.** A measured lecturer and an excitable
  streamer sit at very different words-per-second, and neither figure says which *moment*
  mattered. Deviation from that speaker's own baseline does.
* **Nothing here may change ranking.** The features exist to be measured against the S1
  benchmark and fed to the model later (S10). A weight chosen before the benchmark can judge it
  is guesswork indistinguishable from improvement — so there is an explicit test that scores and
  ordering are untouched.
"""

from __future__ import annotations

import pytest

from worker import selection_features as sf
from worker.models import ProcessingOptions
from worker.selection import ClipCandidate, select_moments
from worker.transcribe import Transcript, TranscriptSegment, Word


def _words(spans):
    """Words from ``(start, end)`` pairs."""
    return [Word(start=s, end=e, text="word", probability=0.95) for s, e in spans]


def _paced(duration, rate, *, fast_from=None, fast_to=None, fast_rate=5.0):
    """A word timeline at ``rate`` words/sec, optionally faster in one stretch."""
    words, t = [], 0.0
    while t < duration:
        current = fast_rate if (fast_from is not None and fast_from <= t < fast_to) else rate
        words.append(Word(start=round(t, 3), end=round(t + 0.2, 3), text="word", probability=0.95))
        t += 1.0 / current
    return words


# --------------------------------------------------------------------------- #
# Windowing                                                                    #
# --------------------------------------------------------------------------- #
def test_a_word_is_counted_by_the_window_its_midpoint_falls_in():
    """Midpoint, not any-overlap: a straddling word must not be counted twice.

    Counting overlap in both windows would inflate the rate of every window in a dense
    transcript, which is precisely where the signal is supposed to be discriminating.
    """
    # Mostly in the first window: 9.4-10.2, midpoint 9.8.
    early = _words([(9.4, 10.2)])
    assert len(sf.words_in_window(early, 0.0, 10.0)) == 1
    assert len(sf.words_in_window(early, 10.0, 20.0)) == 0

    # Mostly in the second: 9.8-10.6, midpoint 10.2.
    late = _words([(9.8, 10.6)])
    assert len(sf.words_in_window(late, 0.0, 10.0)) == 0
    assert len(sf.words_in_window(late, 10.0, 20.0)) == 1

    # A midpoint exactly on the boundary belongs to the later window, because the interval is
    # half-open ``[start, end)``. Pinned so consecutive windows can never double-count.
    exact = _words([(9.6, 10.4)])
    assert len(sf.words_in_window(exact, 0.0, 10.0)) == 0
    assert len(sf.words_in_window(exact, 10.0, 20.0)) == 1


def test_words_outside_the_window_are_excluded():
    words = _words([(1.0, 1.2), (50.0, 50.2)])
    assert len(sf.words_in_window(words, 0.0, 10.0)) == 1


def test_malformed_words_are_skipped_rather_than_raising():
    """Selection runs on whatever ASR produced; a rate helper must not be the thing that fails."""

    class Broken:
        start = None
        end = "later"

    class NaNs:
        start = float("nan")
        end = 5.0

    assert sf.words_in_window([Broken(), NaNs()], 0.0, 10.0) == []


# --------------------------------------------------------------------------- #
# The baseline                                                                 #
# --------------------------------------------------------------------------- #
def test_the_baseline_is_a_median_not_a_mean():
    """A silent stretch must not drag the baseline down.

    With a mean, a music interlude or a long pause produces near-zero slices, the baseline sinks,
    and *ordinary* speech then reads as fast — inverting the signal exactly where footage is
    hardest. Here: 90 s of speech at 2 wps followed by 90 s of silence.
    """
    words = _paced(90.0, 2.0)
    baseline = sf.source_median_rate(words, 180.0)
    assert baseline == pytest.approx(2.0, abs=0.3), baseline

    mean_rate = len(words) / 180.0  # what a mean would have produced
    assert mean_rate < 1.2, "fixture no longer distinguishes median from mean"


def test_no_baseline_when_the_source_is_too_sparse():
    """Returning a number here would invent a denominator the audio does not support."""
    assert sf.source_median_rate([], 120.0) is None
    assert sf.source_median_rate(_words([(0.0, 0.2)]), 120.0) is None
    assert sf.source_median_rate(_paced(60.0, 2.0), 0.0) is None


# --------------------------------------------------------------------------- #
# The rate itself                                                              #
# --------------------------------------------------------------------------- #
def test_a_faster_stretch_reads_as_faster_than_the_speaker_baseline():
    """The signal, stated plainly."""
    words = _paced(120.0, 2.0, fast_from=60.0, fast_to=75.0, fast_rate=5.0)
    baseline = sf.source_median_rate(words, 120.0)

    normal = sf.speech_rate(words, 10.0, 40.0, baseline=baseline)
    burst = sf.speech_rate(words, 60.0, 75.0, baseline=baseline)

    assert normal.relative_speech_rate == pytest.approx(1.0, abs=0.15)
    assert burst.relative_speech_rate > 2.0
    assert burst.words_per_second > normal.words_per_second


def test_the_same_absolute_rate_is_relative_to_the_speaker():
    """Why absolute words-per-second is the weak reading.

    3 words/sec is a fast burst from a slow speaker and a slow patch for a fast one. Identical
    absolute rate, opposite meaning — which is what a ranking would need to know.
    """
    window = _paced(20.0, 3.0)
    slow_speaker = sf.speech_rate(window, 0.0, 20.0, baseline=2.0)
    fast_speaker = sf.speech_rate(window, 0.0, 20.0, baseline=4.0)

    assert slow_speaker.words_per_second == pytest.approx(fast_speaker.words_per_second)
    assert slow_speaker.relative_speech_rate > 1.0
    assert fast_speaker.relative_speech_rate < 1.0


def test_a_window_too_short_or_too_sparse_is_marked_unreliable():
    """ "Not measurable" and "average pace" must be distinguishable.

    Both report a relative rate of 1.0, so a caller feeding these to a model or a weight needs
    the flag to tell them apart. Two words in 0.3 s is 6.7 words/sec, which describes a
    measurement artefact rather than fast speech.
    """
    words = _paced(60.0, 2.0)
    tiny = sf.speech_rate(words, 10.0, 10.4, baseline=2.0)
    assert not tiny.reliable and tiny.relative_speech_rate == 1.0

    sparse = sf.speech_rate(_words([(5.0, 5.2)]), 0.0, 30.0, baseline=2.0)
    assert not sparse.reliable and sparse.relative_speech_rate == 1.0

    good = sf.speech_rate(words, 10.0, 40.0, baseline=2.0)
    assert good.reliable


def test_relative_rate_is_one_when_there_is_no_baseline():
    """1.0 reads as "no information", which is why ``reliable`` exists beside it."""
    words = _paced(60.0, 2.0)
    result = sf.speech_rate(words, 10.0, 40.0, baseline=None)
    assert result.relative_speech_rate == 1.0
    assert result.words_per_second > 0


def test_rate_counts_silence_in_the_window():
    """Density over the window, not articulation rate.

    A window that is half silence *is* slower paced, and for selection that is the useful
    reading: it describes how the moment feels, not how fast the speaker's mouth moved.
    """
    words = _words([(0.0, 0.2), (0.5, 0.7), (1.0, 1.2), (1.5, 1.7)])
    packed = sf.speech_rate(words, 0.0, 2.0)
    padded = sf.speech_rate(words, 0.0, 10.0)
    assert packed.words_per_second > padded.words_per_second


def test_features_serialise_flat_for_a_prompt_or_a_report():
    result = sf.speech_rate(_paced(60.0, 2.0), 10.0, 40.0, baseline=2.0)
    payload = result.to_dict()
    assert set(payload) == {"word_count", "words_per_second", "relative_speech_rate", "reliable"}
    assert all(isinstance(value, float) for value in payload.values())


# --------------------------------------------------------------------------- #
# Attaching to candidates                                                      #
# --------------------------------------------------------------------------- #
def test_candidates_are_annotated_in_place():
    words = _paced(120.0, 2.0, fast_from=60.0, fast_to=75.0, fast_rate=5.0)
    candidates = [ClipCandidate(10.0, 40.0), ClipCandidate(60.0, 75.0)]
    sf.annotate_candidates(candidates, words, 120.0)

    for candidate in candidates:
        assert "relative_speech_rate" in candidate.features
        assert "source_median_wps" in candidate.features
    assert (
        candidates[1].features["relative_speech_rate"]
        > candidates[0].features["relative_speech_rate"]
    )


def test_annotating_never_changes_score_or_order():
    """The invariant that keeps this measurable rather than a blind tuning change.

    The features are computed so the S1 benchmark can judge whether they *should* influence
    ranking. Until it can, influencing it would make an improvement and a regression look
    identical.
    """
    candidates = [
        ClipCandidate(10.0, 40.0, score=80.0),
        ClipCandidate(60.0, 75.0, score=20.0),
    ]
    before = [(c.start, c.end, c.score) for c in candidates]
    sf.annotate_candidates(candidates, _paced(120.0, 2.0, fast_from=60.0, fast_to=75.0), 120.0)
    assert [(c.start, c.end, c.score) for c in candidates] == before


def test_annotating_an_empty_list_or_a_wordless_source_is_safe():
    sf.annotate_candidates([], _paced(60.0, 2.0), 60.0)
    candidates = [ClipCandidate(0.0, 30.0)]
    sf.annotate_candidates(candidates, [], 60.0)
    assert candidates[0].features.get("word_count") == 0.0


# --------------------------------------------------------------------------- #
# Through the real selector                                                    #
# --------------------------------------------------------------------------- #
def test_the_fallback_path_is_measured_too(tmp_path):
    """The fallback runs whenever there is no LLM key or the call fails.

    Leaving it unmeasured would mean the S1 benchmark could not compare the two selection paths
    on the same terms — and comparing them is the first thing the benchmark is for.
    """
    words = _paced(120.0, 2.0)
    transcript = Transcript(
        language="en",
        segments=[TranscriptSegment(0.0, 120.0, "some text", words=words)],
    )
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stub")

    found = select_moments(
        transcript,
        ProcessingOptions(strategy="fixed", clip_length="30-60s"),
        source,
        120.0,
    )
    assert found, "the fallback produced no candidates"
    for candidate in found:
        assert "relative_speech_rate" in candidate.features


def test_features_default_to_empty_and_are_not_shared_between_candidates():
    """A mutable default shared across instances would cross-contaminate every candidate."""
    first, second = ClipCandidate(0.0, 10.0), ClipCandidate(10.0, 20.0)
    assert first.features == {} and second.features == {}
    first.features["x"] = 1.0
    assert second.features == {}
