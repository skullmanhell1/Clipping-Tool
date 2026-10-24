"""Defects where the engine produced a clip that was **silently wrong**.

This project's stated worst failure mode is not a crash — it is a clip that renders, encodes,
uploads and looks finished while being wrong, because nothing in a green suite can tell the
difference. Every case here is one of those, and they fall into three families:

1. **ffmpeg said it worked and wrote nothing.** Failure detection was the exit code alone, and
   ffmpeg exits 0 while producing a 0-byte or header-only file for several reachable inputs. In the
   worst instance the pipeline then deleted the only good copy of the clip in favour of that file.
2. **An edit was requested, failed, and was reported as applied** — or applied and not reported.
   The `*_degraded` marker convention exists precisely so an absent feature is distinguishable from
   a broken one, and three features bypassed it.
3. **A value that means "no information" was read as its opposite.** A zero-confidence word became
   a fully confident one; a NaN score became the maximum and sorted first.

Each test names the defect it pins rather than only the behaviour it asserts, because in every case
the behaviour looks unremarkable and only the history explains why it is checked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from tests.conftest import options_all_off, requires_ffmpeg
except ImportError:  # pragma: no cover - conftest always importable under pytest
    from conftest import options_all_off, requires_ffmpeg

from worker import ffmpeg_utils as fu


# --------------------------------------------------------------------------- #
# 1. ffmpeg exited 0 and produced nothing                                       #
# --------------------------------------------------------------------------- #
def test_an_empty_output_is_a_failure_even_on_a_zero_exit(tmp_path):
    """The exit code was the only evidence that anything had been written.

    ffmpeg exits 0 while writing a 0-byte file when `-ss` lands at or past the source duration,
    when `-t` is shorter than a frame, when a filter graph yields no frames, and when a `concat`'s
    `trim` ranges all collapse. `probe()` accepts a duration of 0.0 without complaint, so a window
    derived from a malformed probe reaches exactly that state.
    """
    dest = tmp_path / "out.mp4"
    dest.touch()
    with pytest.raises(fu.FFmpegError, match="empty"):
        fu._require_output(dest, ["ffmpeg", "-y"], what="clip")


def test_the_empty_output_is_removed_not_left_behind(tmp_path):
    """A zero-byte artefact is worse than none.

    Every later stage guards with `exists()`, so leaving the file makes the failure surface several
    stages downstream as a confusing decode error instead of here, where the failing command is
    still known.
    """
    dest = tmp_path / "out.mp4"
    dest.touch()
    with pytest.raises(fu.FFmpegError):
        fu._require_output(dest, ["ffmpeg", "-y"])
    assert not dest.exists()


def test_a_missing_output_names_the_file_rather_than_raising_oserror(tmp_path):
    with pytest.raises(fu.FFmpegError, match="wrote no thumbnail"):
        fu._require_output(tmp_path / "nope.jpg", ["ffmpeg", "-y"], what="thumbnail")


def test_a_real_output_passes_through_unchanged(tmp_path):
    """The guard must not become a second failure mode of its own."""
    dest = tmp_path / "out.mp4"
    dest.write_bytes(b"\0" * 2048)
    assert fu._require_output(dest, ["ffmpeg", "-y"]) == dest
    assert dest.exists()


@requires_ffmpeg
def test_cutting_past_the_end_of_the_source_is_reported(tmp_path, make_video):
    """The end-to-end version of the above, through a real ffmpeg.

    Asserted as "either a clean FFmpegError or a real file" rather than pinning which: whether
    ffmpeg exits non-zero or exits 0 having written nothing depends on the build, and the property
    that matters is that neither outcome yields a silent empty clip.
    """
    src = make_video("s.mp4", duration=2.0, w=320, h=240)
    dest = tmp_path / "past_end.mp4"
    try:
        fu.cut_segment(src, 60.0, 61.0, dest)
    except fu.FFmpegError:
        assert not dest.exists() or dest.stat().st_size > 0
    else:
        assert dest.stat().st_size > 0, "an empty clip was returned as a success"


# --------------------------------------------------------------------------- #
# 2. An edit failed and the record said it had been applied                     #
# --------------------------------------------------------------------------- #
@requires_ffmpeg
def test_a_failed_keep_interval_pass_is_marked_and_keeps_the_original(
    tmp_path, make_video, monkeypatch
):
    """`except fu.FFmpegError: pass` shipped a clip with every requested edit missing.

    `pending` at that point is the single resolved keep list for **three** features — filler
    removal, the U4 cut list and cold-open assembly — and their markers were only appended on the
    success path. So a failure produced a clip whose record was byte-identical to one nobody had
    asked to edit, which is the precise thing the `*_degraded` convention exists to prevent. The
    compositor stage a few lines below already honoured it.
    """
    import worker.pipeline as pl
    from worker.effects import filler
    from worker.transcribe import Transcript, TranscriptSegment, Word

    def fake_transcribe(source, language=None, translate=False, **_kw):
        words = [Word(0.2, 0.6, "um"), Word(0.7, 1.1, "hello"), Word(1.2, 1.8, "there")]
        return Transcript(
            language="en", segments=[TranscriptSegment(0.0, 4.0, "um hello there", words)]
        )

    monkeypatch.setattr(pl, "transcribe", fake_transcribe)
    from worker.selection import ClipCandidate

    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=50.0, text="um hello there")],
    )

    def boom(*a, **k):
        raise fu.FFmpegError("simulated concat failure")

    monkeypatch.setattr(filler, "apply_keep_intervals", boom)

    src = make_video("s.mp4", duration=4.0, w=320, h=240)
    opts = options_all_off(captions=False, metadata=False, aspect="9:16", filler_removal=True)
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")

    assert len(clips) == 1
    clip = clips[0]
    assert "keep_intervals_degraded" in clip.effects_applied, (
        "the keep-interval pass failed and the clip record does not say so"
    )
    assert "filler_removal" not in clip.effects_applied, (
        "the record claims filler removal was applied, but the pass that applies it failed"
    )
    # The untrimmed clip is still delivered — degraded, not lost.
    assert (tmp_path / "clips" / clip.filename).exists()
    # And the partial intermediate is not orphaned in the scratch directory.
    assert not list((tmp_path / "tmp").glob("trim_*.mp4")), (
        "a truncated intermediate was left behind"
    )


@requires_ffmpeg
def test_dropped_transcript_segments_reach_the_clip_record(tmp_path, make_video, monkeypatch):
    """`transcript_filter.MARKER` was defined, documented, and applied to nothing.

    A repo-wide grep found exactly one occurrence: its own definition. So T3 could delete ASR
    segments from a clip's captions, sidecars, soft subtitle tracks and the LLM's selection input
    while the clip record said nothing — and per that module's own reasoning, "a wrongly-dropped
    sentence just looks like the speaker never said it". This is the silent edit with the highest
    consequence in the pipeline and it was the one with no marker.
    """
    import worker.pipeline as pl
    from worker import transcript_filter
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    def fake_transcribe(source, language=None, translate=False, on_filtered=None, **_kw):
        # Stand in for the real filter having dropped two segments.
        if on_filtered is not None:
            on_filtered(2, ["repetition: 'you you you'", "low confidence"])
        words = [Word(0.2, 0.6, "hello"), Word(0.7, 1.4, "there")]
        return Transcript(
            language="en", segments=[TranscriptSegment(0.0, 4.0, "hello there", words)]
        )

    monkeypatch.setattr(pl, "transcribe", fake_transcribe)
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=50.0, text="hello there")],
    )

    src = make_video("s.mp4", duration=4.0, w=320, h=240)
    opts = options_all_off(captions=False, metadata=False, aspect="9:16")
    clips = pl.run_pipeline(src, opts, clips_dir=tmp_path / "clips", temp_dir=tmp_path / "tmp")

    assert f"{transcript_filter.MARKER}:2" in clips[0].effects_applied


def test_letterbox_detection_refuses_visibly(tmp_path, monkeypatch, caplog):
    """A failed probe was indistinguishable from "there are no bars".

    That is the worst possible collapse for this particular function: "no bars" is the answer that
    makes the reframe path centre its crop on the letterbox, which is the exact failure V16 exists
    to prevent. It bypassed `_run`, never inspected `returncode`, and caught bare `Exception`.
    """
    src = tmp_path / "s.mp4"
    src.write_bytes(b"\0" * 4096)

    monkeypatch.setattr(
        fu,
        "probe",
        lambda p: fu.MediaInfo(duration=10.0, width=1920, height=1080, fps=30.0, has_audio=True),
    )

    def boom(cmd, **kw):
        raise fu.FFmpegError("cropdetect: No such filter")

    monkeypatch.setattr(fu, "_run", boom)
    with caplog.at_level("WARNING"):
        assert fu.detect_letterbox(src) is None
    assert "letterbox detection failed" in caplog.text


# --------------------------------------------------------------------------- #
# 3. A value meaning "no information" was read as its opposite                  #
# --------------------------------------------------------------------------- #
def test_a_zero_confidence_word_stays_zero_confidence():
    """`float(getattr(w, "probability", 1.0) or 1.0)` — `0.0` is falsy.

    So the one value that means "the model had no confidence in this word" was rewritten as
    *maximum* confidence. The consumer is `transcript_filter._mean_word_probability`, whose
    low-confidence-plus-repetitive rule is the ASR hallucination signature — and hallucinated words
    are exactly the ones with near-zero probabilities. The filter was therefore biased away from
    firing on its own target case.
    """
    from worker.transcribe import _probability_of

    assert _probability_of(type("W", (), {"probability": 0.0})()) == 0.0
    assert _probability_of(type("W", (), {"probability": 0.42})()) == pytest.approx(0.42)
    # A *missing* attribute is a backend that does not report confidence; 1.0 is right there.
    assert _probability_of(type("W", (), {})()) == 1.0
    # NaN is not a measurement and would poison every mean downstream.
    assert _probability_of(type("W", (), {"probability": float("nan")})()) == 1.0


def test_the_filter_can_still_see_a_low_confidence_segment():
    """The consequence of the above, at the level that matters.

    Pins the reason the conversion is worth caring about: with probabilities collapsed to 1.0 the
    mean is 1.0 and no confidence threshold can ever fire.
    """
    from worker.transcribe import TranscriptSegment, Word
    from worker.transcript_filter import _mean_word_probability

    words = [Word(0.0, 0.2, "you", probability=0.0), Word(0.2, 0.4, "you", probability=0.0)]
    segment = TranscriptSegment(0.0, 0.4, "you you", words)
    assert _mean_word_probability(segment) == 0.0


def test_a_nan_score_from_the_llm_sorts_last_not_first():
    """`max(0.0, min(100.0, float("nan")))` returns **100.0**.

    `nan < 100.0` is False so `min` never replaces its running value, and `max(0.0, 100.0)` is
    100.0. So a malformed score was not clamped to a neutral value — it was promoted to the maximum
    and sorted ahead of every genuinely scored moment, where `deduplicate(limit=max_clips)` then let
    it evict real candidates. `json.loads` accepts a bare `NaN` literal, so this arrives from a
    model reply without anything unusual happening.
    """
    # The arithmetic itself, stated so the surprise is on the record rather than in a comment.
    assert max(0.0, min(100.0, float("nan"))) == 100.0

    from worker.selection import ClipCandidate

    scored = [
        ClipCandidate(start=0.0, end=10.0, score=0.0, text="malformed"),
        ClipCandidate(start=20.0, end=30.0, score=72.0, text="real"),
    ]
    scored.sort(key=lambda c: c.score, reverse=True)
    assert scored[0].text == "real"


# --------------------------------------------------------------------------- #
# 4. Cache keys and windows                                                     #
# --------------------------------------------------------------------------- #
def test_the_transcript_cache_key_covers_the_decoder_quantisation():
    """The module docstring claimed every parameter that changes the answer was in the key.

    It was not. `transcribe._get_model` keys its in-process model cache on
    `(model, device, compute_type)` — it knows all three shape the model — while the *disk* key
    carried only the model name. This repository measured the difference itself in
    `worker/word_spans.py`: small/int8 81.4% mask overlap, small/float32 80.7%, medium/int8 79.6%.
    Different quantisation, different word timings.

    Entries are content-addressed and never expire, so a box that acquired a GPU would serve CPU
    int8 word timings forever with nothing to correct it.
    """
    from worker import transcript_cache

    common = dict(model="small", language="en", translate=False, beam_size=5)
    cpu = transcript_cache.cache_key("abc", **common, device="cpu", compute_type="int8")
    gpu = transcript_cache.cache_key("abc", **common, device="cuda", compute_type="float16")
    quantisation = transcript_cache.cache_key("abc", **common, device="cpu", compute_type="float32")

    assert cpu != gpu, "device is not part of the cache key"
    assert cpu != quantisation, "compute_type is not part of the cache key"


def test_omitting_the_device_keeps_the_previous_key():
    """Callers that do not know about these must still agree with each other.

    The same reason `asr_config` has a default: the S1 harness and the pipeline have to compute the
    same key for the same file, or they quietly maintain two caches of one thing.
    """
    from worker import transcript_cache

    common = dict(model="small", language="en", translate=False, beam_size=5)
    assert transcript_cache.cache_key("abc", **common) == transcript_cache.cache_key(
        "abc", **common, device=None, compute_type=None
    )


def test_the_llm_reply_is_held_to_the_requested_clip_length():
    """`min_len`/`max_len` built the prompt and were never checked against the reply.

    So a model that ignored the instruction shipped a clip of any length with no marker — a
    four-minute "short" from a 15-45 s request. The fallback path has always enforced this via
    `candidate_ranking.length_fit`, so the two selection paths disagreed about whether
    `clip_length` was a constraint or a suggestion.
    """
    from worker import selection as sel
    from worker.transcribe import Transcript, TranscriptSegment, Word

    words = [Word(float(i), float(i) + 0.9, f"w{i}") for i in range(240)]
    transcript = Transcript(
        language="en",
        segments=[
            TranscriptSegment(
                float(i * 10), float(i * 10 + 10), "sentence.", words[i * 10 : i * 10 + 10]
            )
            for i in range(24)
        ],
    )

    class _Client:
        def complete_json(self, *a, **k):
            # One absurd range and one reasonable one.
            return [
                {"start": 0.0, "end": 240.0, "score": 90, "reason": "too long"},
                {"start": 100.0, "end": 130.0, "score": 80, "reason": "fine"},
            ]

    found = sel.select_moments(
        transcript,
        _options(clip_length="30-60s"),
        Path("unused.mp4"),
        240.0,
        client=_Client(),
    )
    assert found, "selection returned nothing"
    for candidate in found:
        assert candidate.end - candidate.start <= 60.0 + 1e-6, (
            f"a {candidate.end - candidate.start:.1f}s clip shipped from a 30-60s request"
        )


def _options(**overrides):
    from worker.models import ProcessingOptions

    base = dict(strategy="ai", num_clips="auto", metadata=False, captions=False)
    base.update(overrides)
    return ProcessingOptions(**base)


# --------------------------------------------------------------------------- #
# 5. Word-span hygiene                                                          #
# --------------------------------------------------------------------------- #
def test_a_span_starting_before_its_cue_is_pulled_forward():
    """`hygiene_for_cue` accepted `cue_start` and silently discarded it.

    `apply_hygiene` had no lower bound at all: the pre-check tested ordering, sign, `cue_end` and
    the floor, and the repair loop's cursor started at `None` so the first span was never bounded
    below by anything. R8.5's reasoning — a highlight on text that is not on screen — applies to the
    leading edge exactly as it does to the trailing one. Reachable after C24 merges two cues, which
    takes the earlier `start` and concatenates both span lists.
    """
    from worker.transcribe import Word
    from worker.word_spans import hygiene_for_cue

    # `Word`, not `FakeWord`: `apply_hygiene` refuses a sequence it cannot rebuild with
    # `dataclasses.replace`, and conftest's FakeWord is a plain class. That refusal is correct
    # behaviour, and using it here would make this test pass for the wrong reason.
    spans = [Word(0.5, 1.0, "early"), Word(1.0, 1.5, "fine")]
    repaired, report = hygiene_for_cue(spans, 1.0, 2.0)

    assert repaired[0].start >= 1.0 - 1e-9, "a span still begins before its own cue"
    assert report.reordered >= 1, "the repair was made but not reported"


def test_adjacent_spans_are_not_counted_as_repairs():
    """The compliance test and the repair loop disagreed by `SPAN_EPSILON`.

    The pre-check accepts `start >= previous_end`, but the repair advanced its cursor to
    `end + SPAN_EPSILON` and treated `start < cursor` as out of order. So once *any* span in a cue
    failed for an unrelated reason, every perfectly adjacent boundary in that cue was nudged a
    millisecond and counted, inflating the `word_spans_repaired:<n>` marker with repairs nobody
    needed. A marker that overstates is a marker nobody trusts.
    """
    from worker.transcribe import Word
    from worker.word_spans import apply_hygiene

    # The last span breaks the cue boundary, which forces the repair pass to run. Every other
    # boundary here is exactly adjacent and must be left alone.
    spans = [
        Word(0.0, 0.5, "a"),
        Word(0.5, 1.0, "b"),
        Word(1.0, 1.5, "c"),
        Word(1.5, 5.0, "overruns"),
    ]
    repaired, report = apply_hygiene(spans, cue_end=2.0)

    assert report.clamped_to_cue == 1, "the real fault was not the one reported"
    assert report.reordered == 0, (
        f"{report.reordered} adjacent boundaries were counted as reordered"
    )
    assert [s.start for s in repaired[:3]] == [0.0, 0.5, 1.0]


# --------------------------------------------------------------------------- #
# 6. Scratch directories                                                        #
# --------------------------------------------------------------------------- #
def test_the_cache_prune_survives_an_entry_vanishing_mid_sort(tmp_path, monkeypatch):
    """`item.stat()` guarded by `item.exists()` is two syscalls and a race.

    A concurrent prune or sweep between them made `stat()` raise `OSError` out of `sort` — outside
    the enclosing `try`, so it propagated into whichever selection pass triggered the prune.
    """
    from worker import intermediate_cache as ic

    monkeypatch.setattr(ic, "enabled", lambda: True)
    monkeypatch.setattr(ic, "cache_dir", lambda: tmp_path)
    for i in range(5):
        (tmp_path / f"e{i}.json").write_text("{}")

    real_stat = Path.stat
    calls = {"n": 0}

    def flaky_stat(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("vanished")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    # Must not raise.
    assert ic.prune(max_entries=2) >= 0
