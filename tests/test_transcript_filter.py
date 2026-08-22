"""Hallucination and repetition filtering (T3).

Whisper invents text over music, applause and silence, and gets stuck in decode loops that
repeat a phrase for tens of seconds. Nothing filtered either, so invented text reached the LLM
selector as though it were speech and reached the viewer burned into captions.

The asymmetry that shapes every test here: **a false positive deletes real speech and nothing
downstream can notice**, while a missed hallucination is visible - you watch the clip and read
nonsense. So roughly half of these tests assert that real speech *survives*, including the
awkward cases (deliberate repetition, quiet speech, short utterances). A filter that removes
hallucinations and also removes dialogue is worse than no filter.
"""

from __future__ import annotations

import pytest

from config import settings
from worker import transcribe as transcribe_module
from worker import transcript_filter
from worker.transcribe import Transcript, TranscriptSegment, Word


def _segment(text, start=0.0, end=2.0, *, no_speech=0.0, logprob=0.0, probability=0.95):
    words = [
        Word(start=start, end=end, text=token, probability=probability)
        for token in (text.split() or ["x"])
    ]
    return TranscriptSegment(
        start=start,
        end=end,
        text=text,
        words=words,
        no_speech_prob=no_speech,
        avg_logprob=logprob,
    )


def _transcript(*segments):
    return Transcript(language="en", segments=list(segments))


def _texts(result):
    return [segment.text for segment in result.transcript.segments]


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "transcript_filter_enabled", True)


# --------------------------------------------------------------------------- #
# What must be removed                                                         #
# --------------------------------------------------------------------------- #
def test_a_segment_the_model_doubts_twice_over_is_dropped():
    """Both of Whisper's own indicators agreeing is the strongest signal available."""
    result = transcript_filter.filter_transcript(
        _transcript(
            _segment("real speech that stays", 0, 2),
            _segment("Thanks for watching!", 2, 4, no_speech=0.9, logprob=-1.4),
            _segment("more real speech", 4, 6),
        )
    )
    assert _texts(result) == ["real speech that stays", "more real speech"]
    assert result.removed_count == 1
    assert "no_speech" in result.reasons[0]


def test_a_decode_loop_is_dropped():
    """No speaker says the same word four times with nothing in between."""
    result = transcript_filter.filter_transcript(
        _transcript(
            _segment("you you you you you", 0, 8),
            _segment("actual speech", 8, 10),
            _segment("actual speech continues", 10, 12),
        )
    )
    assert "you you you you you" not in _texts(result)
    assert "repeated" in result.reasons[0]


def test_a_phrase_looping_across_segments_is_dropped():
    """A loop can span segment boundaries, which no per-segment rule can see.

    The first occurrence is kept: the speaker may well have said it once.
    """
    result = transcript_filter.filter_transcript(
        _transcript(
            _segment("opening line", 0, 2),
            _segment("subscribe to my channel", 2, 4),
            _segment("subscribe to my channel", 4, 6),
            _segment("subscribe to my channel", 6, 8),
            _segment("subscribe to my channel", 8, 10),
            _segment("closing line", 10, 12),
            _segment("and another closing thought", 12, 14),
            _segment("and one more real sentence", 14, 16),
        )
    )
    kept = _texts(result)
    assert kept.count("subscribe to my channel") == 2, kept
    assert "opening line" in kept and "closing line" in kept


def test_a_low_confidence_repetitive_segment_is_dropped():
    """The loop-over-music case: barely-confident words with almost no unique tokens."""
    segment = _segment("na na na na na na", 0, 6, probability=0.2)
    result = transcript_filter.filter_transcript(
        _transcript(
            _segment("clear speech here", 0, 2),
            segment,
            _segment("more clear speech", 6, 8),
        )
    )
    assert "na na na na na na" not in _texts(result)


# --------------------------------------------------------------------------- #
# What must survive - the expensive half                                       #
# --------------------------------------------------------------------------- #
def test_quiet_speech_is_not_deleted():
    """``no_speech_prob`` alone must never be acted on.

    It runs high on whispered, distant or heavily-accented speech that is entirely real. Acting
    on it alone would delete exactly the dialogue a creator most wants captioned.
    """
    result = transcript_filter.filter_transcript(
        _transcript(
            _segment("a quietly delivered but real sentence", 0, 3, no_speech=0.95, logprob=-0.2),
        )
    )
    assert result.removed_count == 0


def test_low_confidence_alone_is_not_enough():
    """Nor is the other signal on its own: hard audio is not the same as invented audio."""
    result = transcript_filter.filter_transcript(
        _transcript(
            _segment("a difficult to hear but genuine sentence", 0, 3, no_speech=0.1, logprob=-1.8),
        )
    )
    assert result.removed_count == 0


def test_deliberate_repetition_by_a_speaker_survives():
    """People do repeat themselves for emphasis, and it is often the best moment in a clip.

    "no, no, no" is three tokens - under the loop threshold - and said with confidence.
    """
    result = transcript_filter.filter_transcript(
        _transcript(
            _segment("no no no that is not what happened", 0, 3),
        )
    )
    assert result.removed_count == 0


def test_a_genuine_outro_survives():
    """The reason there is no boilerplate phrase list.

    Whisper's inventions cluster around phrases like this, and they are also things people
    genuinely say - especially in the footage this tool is pointed at. A phrase list would
    delete the outro of every video that actually has one.
    """
    result = transcript_filter.filter_transcript(
        _transcript(
            _segment("so thanks for watching and I will see you next week", 0, 4),
        )
    )
    assert result.removed_count == 0


def test_a_transcript_that_looks_mostly_invented_is_left_alone():
    """The circuit breaker.

    If most segments trip the rules, the thresholds are wrong for this audio - an unusual
    accent, a heavy music bed, a language the model is weak in. Emptying the transcript would
    turn a poor result into no clips at all. Keeping a bad transcript is recoverable.
    """
    suspect = [
        _segment(f"invented line {i}", i * 2, i * 2 + 2, no_speech=0.9, logprob=-1.5)
        for i in range(4)
    ]
    result = transcript_filter.filter_transcript(
        _transcript(_segment("one real line", 100, 102), *suspect)
    )
    assert result.removed_count == 0, "the filter gutted the transcript"
    assert len(result.transcript.segments) == 5


def test_filtering_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "transcript_filter_enabled", False)
    result = transcript_filter.filter_transcript(
        _transcript(
            _segment("you you you you you", 0, 8),
        )
    )
    assert result.removed_count == 0


# --------------------------------------------------------------------------- #
# Shape and purity                                                             #
# --------------------------------------------------------------------------- #
def test_the_input_transcript_is_never_mutated():
    """Callers hold the transcript; the pipeline also reads its words for captions."""
    original = _transcript(
        _segment("real", 0, 2),
        _segment("you you you you", 2, 4),
    )
    before = [segment.text for segment in original.segments]
    transcript_filter.filter_transcript(original)
    assert [segment.text for segment in original.segments] == before


def test_surviving_timings_are_untouched():
    """Captions, emphasis and selection all key off source-relative time.

    A dropped segment takes its own words and leaves everything else where it was - it does not
    close the gap, because shifting timings would desynchronise the clip from its own audio.
    """
    result = transcript_filter.filter_transcript(
        _transcript(
            _segment("first", 0, 2),
            _segment("you you you you", 2, 10),
            _segment("second", 10, 12),
        )
    )
    kept = result.transcript.segments
    assert [(s.start, s.end) for s in kept] == [(0.0, 2.0), (10.0, 12.0)]


def test_an_empty_or_textless_transcript_is_handled():
    assert transcript_filter.filter_transcript(_transcript()).removed_count == 0
    blank = TranscriptSegment(0.0, 1.0, "", words=[])
    assert transcript_filter.filter_transcript(_transcript(blank)).removed_count == 0


def test_hostile_segments_do_not_raise():
    """The filter runs on every transcription, so it must be total."""

    class Hostile:
        text = "some text here"
        start = 0.0
        end = 1.0
        words = None
        no_speech_prob = "not a number"
        avg_logprob = None

    transcript_filter.filter_transcript(_transcript(Hostile()))


def test_reasons_name_the_time_so_a_log_is_actionable():
    result = transcript_filter.filter_transcript(
        _transcript(
            _segment("real one", 0, 2),
            _segment("you you you you", 12.5, 20.0),
            _segment("real two", 20, 22),
        )
    )
    assert result.reasons == ["token repeated 4x @ 12.50-20.00"]


# --------------------------------------------------------------------------- #
# Wiring                                                                       #
# --------------------------------------------------------------------------- #
def test_transcribe_filters_after_the_cache(tmp_path, monkeypatch):
    """The cache holds *raw* ASR, and filtering happens on the way out.

    So tuning a threshold takes effect on the next run instead of invalidating hours of
    transcription - filtering is microseconds, transcribing is minutes. It also keeps the cache
    lossless: a segment dropped by a rule we later decide was wrong is still recoverable.
    """
    from worker import transcript_cache
    from worker.transcribe import transcribe

    media = tmp_path / "source.mp4"
    media.write_bytes(b"pretend video")
    monkeypatch.setattr(settings, "transcript_cache_dir", tmp_path / "cache")
    monkeypatch.setattr(settings, "transcript_cache_enabled", True)

    raw = _transcript(
        _segment("real speech", 0, 2),
        _segment("you you you you", 2, 6),
        _segment("more real speech", 6, 8),
    )
    monkeypatch.setattr("worker.transcribe.transcribe_uncached", lambda *a, **k: raw)

    first = transcribe(media)
    assert [s.text for s in first.segments] == ["real speech", "more real speech"]

    # What landed in the cache is unfiltered.
    #
    # The key comes from `transcribe.cache_key_for` rather than being rebuilt here. Rebuilding it
    # duplicated the key's composition in a test, so adding a component to the real key -- the
    # decoder's device and compute_type, which change word timings -- made this test look up an
    # entry that was never written and fail with `NoneType has no attribute 'segments'`, several
    # inferences away from the cause. Asking the code for its own key is also the stronger
    # assertion: it tests the cache, not this test's copy of the key format.
    key = transcribe_module.cache_key_for(media)
    cached = transcript_cache.load(key)
    assert [s.text for s in cached.segments] == [
        "real speech",
        "you you you you",
        "more real speech",
    ], "the cache stored the filtered transcript, making it lossy"

    # And a cache hit is filtered on the way out too.
    second = transcribe(media)
    assert [s.text for s in second.segments] == ["real speech", "more real speech"]


def test_the_cache_carries_the_signals_the_filter_reads(tmp_path, monkeypatch):
    """An entry without them would look like a segment with nothing to doubt.

    This is why the cache schema was bumped rather than extended in place.
    """
    from worker import transcript_cache

    monkeypatch.setattr(settings, "transcript_cache_dir", tmp_path / "cache")
    key = "k" * 32
    transcript_cache.store(key, _transcript(_segment("hmm", 0, 2, no_speech=0.77, logprob=-1.25)))
    loaded = transcript_cache.load(key)
    assert loaded.segments[0].no_speech_prob == pytest.approx(0.77)
    assert loaded.segments[0].avg_logprob == pytest.approx(-1.25)
