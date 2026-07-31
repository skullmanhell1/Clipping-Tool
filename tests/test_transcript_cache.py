"""Transcript caching by source content and ASR parameters (T8).

ASR is the most expensive stage in the pipeline and the most repeated: re-running a source to
try a different caption preset or aspect ratio re-transcribed audio that had not changed.

The failure worth testing hardest is not a miss - a miss just costs what it always cost. It is
a **wrong hit**: serving a transcript produced by different settings. That is invisible
(captions appear, timings look plausible) and permanent (the cache keeps answering), so the key
covering the model and the parameters is the load-bearing part of the design, not the file
hashing.
"""

from __future__ import annotations

import json

import pytest

from config import settings
from worker import transcript_cache
from worker.transcribe import Transcript, TranscriptSegment, Word, transcribe


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"pretend video content")
    return path


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    directory = tmp_path / "transcripts"
    monkeypatch.setattr(settings, "transcript_cache_dir", directory)
    monkeypatch.setattr(settings, "transcript_cache_enabled", True)
    return directory


def _transcript(text="hello there"):
    return Transcript(
        language="en",
        segments=[
            TranscriptSegment(
                0.0, 2.0, text,
                words=[Word(0.0, 1.0, "hello", 0.9), Word(1.0, 2.0, "there", 0.8)],
            )
        ],
    )


def _key(media, **overrides):
    params = {"model": "small", "language": None, "translate": False, "beam_size": 5}
    params.update(overrides)
    return transcript_cache.cache_key(transcript_cache.hash_source(media), **params)


# --------------------------------------------------------------------------- #
# Hashing and keys                                                             #
# --------------------------------------------------------------------------- #
def test_the_hash_follows_content_not_the_path(tmp_path):
    """Content hashing is why a re-exported file misses the cache.

    Path-and-mtime keying is the usual shortcut and it is wrong in exactly the case that
    matters: footage re-exported over the same filename. Here, identical bytes under two names
    are the same transcript, and different bytes are not - which is the correct reading.
    """
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"identical content")
    second.write_bytes(b"identical content")
    assert transcript_cache.hash_source(first) == transcript_cache.hash_source(second)

    second.write_bytes(b"different content")
    assert transcript_cache.hash_source(first) != transcript_cache.hash_source(second)


def test_hashing_streams_rather_than_loading_the_file(tmp_path, monkeypatch):
    """Sources are routinely gigabytes; a cache that needs the file in memory is a liability.

    Asserted by counting reads rather than by inspecting the implementation: a file larger than
    the chunk size must take more than one read.
    """
    big = tmp_path / "big.mp4"
    big.write_bytes(b"x" * (transcript_cache._CHUNK * 2 + 17))

    reads = {"count": 0}
    real_open = open

    def counting_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        real_read = handle.read

        def read(*a, **k):
            reads["count"] += 1
            return real_read(*a, **k)

        handle.read = read
        return handle

    monkeypatch.setattr("builtins.open", counting_open)
    transcript_cache.hash_source(big)
    assert reads["count"] >= 3, "the file was not read in chunks"


def test_the_model_is_part_of_the_key(media):
    """The wrong-hit case, and the reason this is not "cache by source hash" alone.

    T1 just changed the default model from `base` to `small`. Keying on the file alone would
    have served every existing `base` transcript to the upgraded model forever - silently, and
    with no way for anything downstream to notice.
    """
    assert _key(media, model="base") != _key(media, model="small")


def test_every_asr_parameter_is_part_of_the_key(media):
    baseline = _key(media)
    assert _key(media, language="en") != baseline        # forced vs auto-detect
    assert _key(media, translate=True) != baseline       # different task entirely
    assert _key(media, beam_size=1) != baseline          # different decoding
    # And the same inputs are stable, or nothing would ever hit.
    assert _key(media) == baseline


def test_the_schema_version_is_in_the_key(media, monkeypatch):
    """A serialisation change must not mis-parse old entries; it must ignore them."""
    before = _key(media)
    monkeypatch.setattr(transcript_cache, "SCHEMA_VERSION", 99)
    assert _key(media) != before


# --------------------------------------------------------------------------- #
# Round trip                                                                   #
# --------------------------------------------------------------------------- #
def test_a_transcript_survives_the_round_trip(media, cache_dir):
    key = _key(media)
    original = _transcript()
    assert transcript_cache.store(key, original) is not None

    loaded = transcript_cache.load(key)
    assert loaded is not None
    assert loaded.language == "en"
    assert [s.text for s in loaded.segments] == ["hello there"]
    word = loaded.segments[0].words[1]
    assert (word.text, word.start, word.end) == ("there", 1.0, 2.0)
    assert word.probability == pytest.approx(0.8)


def test_word_probabilities_survive(media, cache_dir):
    """C11 emphasis and the kinetic confidence floor both read word probability.

    Dropping it in serialisation would silently change caption emphasis on any cached run,
    which is the kind of bug a cache is uniquely good at hiding.
    """
    key = _key(media)
    transcript_cache.store(key, _transcript())
    loaded = transcript_cache.load(key)
    assert [w.probability for w in loaded.segments[0].words] == [
        pytest.approx(0.9), pytest.approx(0.8)
    ]


def test_a_missing_entry_is_a_miss_not_an_error(media, cache_dir):
    assert transcript_cache.load(_key(media)) is None


def test_a_corrupt_entry_is_a_miss_not_an_error(media, cache_dir):
    key = _key(media)
    cache_dir.mkdir(parents=True, exist_ok=True)
    transcript_cache.cache_path(key).write_text("{ truncated", encoding="utf-8")
    assert transcript_cache.load(key) is None


def test_an_entry_from_another_schema_is_ignored(media, cache_dir):
    """Cheaper and safer than migrating data that can always be regenerated."""
    key = _key(media)
    cache_dir.mkdir(parents=True, exist_ok=True)
    transcript_cache.cache_path(key).write_text(
        json.dumps({"schema": 0, "language": "en", "segments": []}), encoding="utf-8"
    )
    assert transcript_cache.load(key) is None


def test_writing_is_atomic(media, cache_dir):
    """A job killed mid-write must not leave a truncated entry every later run must detect.

    Verified through the artefacts: the final file exists and no partial file is left behind.
    """
    key = _key(media)
    transcript_cache.store(key, _transcript())
    assert transcript_cache.cache_path(key).exists()
    assert not list(cache_dir.glob("*.partial"))


def test_storing_into_an_unwritable_location_returns_none(media, monkeypatch, tmp_path):
    """A cache is an optimisation; failing to write one must never fail a job."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setattr(settings, "transcript_cache_dir", blocker / "sub")
    assert transcript_cache.store(_key(media), _transcript()) is None


# --------------------------------------------------------------------------- #
# transcribe_cached                                                            #
# --------------------------------------------------------------------------- #
def test_a_second_call_skips_asr_entirely(media, cache_dir, monkeypatch):
    """The whole point of T8, asserted by counting model invocations."""
    calls = {"count": 0}

    def fake_transcribe(path, language=None, translate=False, beam_size=5, **_kw):
        calls["count"] += 1
        return _transcript()

    monkeypatch.setattr("worker.transcribe.transcribe_uncached", fake_transcribe)
    monkeypatch.setattr(settings, "whisper_model", "small")

    first = transcribe(media)
    second = transcribe(media)

    assert calls["count"] == 1, "the second call re-ran ASR"
    assert [s.text for s in second.segments] == [s.text for s in first.segments]


def test_changing_the_model_re_transcribes(media, cache_dir, monkeypatch):
    """The stale-transcript case, end to end."""
    calls = {"count": 0}

    def fake_transcribe(path, language=None, translate=False, beam_size=5, **_kw):
        calls["count"] += 1
        return _transcript(f"model {settings.whisper_model}")

    monkeypatch.setattr("worker.transcribe.transcribe_uncached", fake_transcribe)

    monkeypatch.setattr(settings, "whisper_model", "base")
    assert transcribe(media).segments[0].text == "model base"

    monkeypatch.setattr(settings, "whisper_model", "small")
    assert transcribe(media).segments[0].text == "model small"
    assert calls["count"] == 2, "the upgraded model was served a cached transcript"


def test_editing_the_source_re_transcribes(media, cache_dir, monkeypatch):
    """Footage re-exported over the same name must not reuse the old transcript."""
    calls = {"count": 0}

    def fake_transcribe(path, language=None, translate=False, beam_size=5, **_kw):
        calls["count"] += 1
        return _transcript()

    monkeypatch.setattr("worker.transcribe.transcribe_uncached", fake_transcribe)
    transcribe(media)
    media.write_bytes(b"a re-exported, different version of the same footage")
    transcribe(media)
    assert calls["count"] == 2


def test_the_cache_can_be_turned_off(media, cache_dir, monkeypatch):
    calls = {"count": 0}

    def fake_transcribe(path, language=None, translate=False, beam_size=5, **_kw):
        calls["count"] += 1
        return _transcript()

    monkeypatch.setattr("worker.transcribe.transcribe_uncached", fake_transcribe)
    monkeypatch.setattr(settings, "transcript_cache_enabled", False)

    transcribe(media)
    transcribe(media)
    assert calls["count"] == 2
    assert not cache_dir.exists() or not list(cache_dir.glob("*.json"))


def test_hit_and_miss_are_reportable(media, cache_dir, monkeypatch):
    """Callers need to be able to say which path ran - a silent cache is hard to trust."""
    monkeypatch.setattr("worker.transcribe.transcribe_uncached",
                        lambda *a, **k: _transcript())
    events: list[str] = []
    transcribe(media, on_hit=lambda key: events.append("hit"),
                      on_miss=lambda key: events.append("miss"))
    transcribe(media, on_hit=lambda key: events.append("hit"),
                      on_miss=lambda key: events.append("miss"))
    assert events == ["miss", "hit"]


def test_an_unreadable_source_still_reaches_asr(tmp_path, cache_dir, monkeypatch):
    """A hashing failure must not pre-empt the real error from ASR.

    Reporting "cache problem" for a missing file would send the reader in the wrong direction.
    """
    calls = {"count": 0}

    def fake_transcribe(path, language=None, translate=False, beam_size=5, **_kw):
        calls["count"] += 1
        return _transcript()

    monkeypatch.setattr("worker.transcribe.transcribe_uncached", fake_transcribe)
    transcribe(tmp_path / "does-not-exist.mp4")
    assert calls["count"] == 1


def test_the_harness_and_the_pipeline_agree_on_the_key(media, monkeypatch, tmp_path):
    """The duplication T8 removed.

    The S1 harness carried its own cache keyed on path/size/mtime with its own JSON shape. Two
    caches of the same thing, disagreeing exactly where it matters: a re-exported file with an
    unchanged name and size was a hit in one and a miss in the other. Both now key through
    this module.
    """
    from evaluation.harness import _harness_key

    monkeypatch.setattr(settings, "whisper_model", "small")
    assert _harness_key(media) == _key(media, model="small")
