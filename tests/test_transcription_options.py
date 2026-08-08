"""Tests for T4 (vocabulary prompt), T5 (VAD parameters), T7 (confidence) and M3 (WER).

The failure to guard hardest against here is a **wrong cache hit**. T4 and T5 change what the
decoder produces, so if they are not part of the cache key then changing a VAD threshold or
adding a name to the vocabulary appears to do nothing at all - forever, because the cache keeps
answering with the transcript decoded under the old settings. That failure is invisible (a
transcript appears, timings look plausible) and permanent, which is why several tests below do
nothing but assert that a key moved.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from config import settings
from evaluation import wer
from worker import captions, transcribe, transcript_cache
from worker.effects.caption_presets import CaptionPreset


@dataclass
class W:
    start: float
    end: float
    text: str
    probability: float = 1.0


# --------------------------------------------------------------------------- #
# T4 - vocabulary / initial prompt
# --------------------------------------------------------------------------- #
def test_nothing_configured_means_no_prompt_at_all(monkeypatch):
    """``None``, not ``""``.

    faster-whisper treats an empty string as a real (empty) prompt rather than as absence, so
    returning ``""`` would condition every decode on nothing in particular instead of leaving
    the parameter alone.
    """
    monkeypatch.setattr(settings, "whisper_initial_prompt", "")
    assert transcribe.resolve_initial_prompt("") is None
    assert transcribe.resolve_initial_prompt() is None


def test_the_job_vocabulary_reaches_the_prompt(monkeypatch):
    monkeypatch.setattr(settings, "whisper_initial_prompt", "")
    assert transcribe.resolve_initial_prompt("Kubernetes, Siobhan") == "Kubernetes, Siobhan"


def test_the_standing_prompt_comes_first_and_the_job_terms_last(monkeypatch):
    """Whisper's conditioning weakens with distance from the decode.

    The per-video terms are the ones that matter most, so they sit closest to the audio.
    """
    monkeypatch.setattr(settings, "whisper_initial_prompt", "ACME Corp")
    assert transcribe.resolve_initial_prompt("Siobhan") == "ACME Corp Siobhan"


def test_whitespace_only_input_is_treated_as_absent(monkeypatch):
    monkeypatch.setattr(settings, "whisper_initial_prompt", "   ")
    assert transcribe.resolve_initial_prompt("  \n ") is None


def test_the_vocabulary_changes_the_cache_key():
    """Otherwise adding a name would appear to do nothing, permanently.

    A transcript decoded without a vocabulary prompt is not interchangeable with one decoded
    with it. If the key ignored it, the first run's transcript would be served forever and the
    setting would look broken in a way nothing downstream could report.
    """
    base = transcript_cache.cache_key(
        "abc",
        model="small",
        language=None,
        translate=False,
        beam_size=5,
        asr_config=transcript_cache.asr_fingerprint(""),
    )
    with_vocab = transcript_cache.cache_key(
        "abc",
        model="small",
        language=None,
        translate=False,
        beam_size=5,
        asr_config=transcript_cache.asr_fingerprint("Kubernetes"),
    )
    assert base != with_vocab


def test_the_standing_prompt_changes_the_cache_key(monkeypatch):
    before = transcript_cache.asr_fingerprint("")
    monkeypatch.setattr(settings, "whisper_initial_prompt", "ACME Corp")
    assert transcript_cache.asr_fingerprint("") != before


# --------------------------------------------------------------------------- #
# T5 - VAD parameters
# --------------------------------------------------------------------------- #
def test_the_vad_parameters_are_read_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "whisper_vad_threshold", 0.31)
    monkeypatch.setattr(settings, "whisper_vad_min_silence_ms", 1234)
    monkeypatch.setattr(settings, "whisper_vad_min_speech_ms", 99)
    monkeypatch.setattr(settings, "whisper_vad_speech_pad_ms", 321)
    assert transcribe.vad_parameters() == {
        "threshold": 0.31,
        "min_silence_duration_ms": 1234,
        "min_speech_duration_ms": 99,
        "speech_pad_ms": 321,
    }


def test_the_defaults_are_the_librarys_own_so_behaviour_is_unchanged():
    """T5 exposes the parameters; it must not silently retune them."""
    assert transcribe.vad_parameters() == {
        "threshold": 0.5,
        "min_silence_duration_ms": 2000,
        "min_speech_duration_ms": 250,
        "speech_pad_ms": 400,
    }
    assert settings.whisper_vad_filter is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("whisper_vad_threshold", 0.2),
        ("whisper_vad_min_silence_ms", 500),
        ("whisper_vad_min_speech_ms", 50),
        ("whisper_vad_speech_pad_ms", 100),
        ("whisper_vad_filter", False),
    ],
)
def test_every_vad_setting_changes_the_cache_key(monkeypatch, field, value):
    """Each one changes the transcript, so each one must change the key."""
    before = transcript_cache.asr_fingerprint("")
    monkeypatch.setattr(settings, field, value)
    assert transcript_cache.asr_fingerprint("") != before, f"{field} is not in the cache key"


def test_vad_parameters_are_excluded_from_the_key_when_vad_is_off(monkeypatch):
    """A parameter with no effect must not cause a miss.

    With the filter off the thresholds are inert, so keying on them would re-transcribe for a
    change that provably cannot alter the output.
    """
    monkeypatch.setattr(settings, "whisper_vad_filter", False)
    before = transcript_cache.asr_fingerprint("")
    monkeypatch.setattr(settings, "whisper_vad_threshold", 0.9)
    assert transcript_cache.asr_fingerprint("") == before


def test_the_harness_and_the_pipeline_agree_on_the_default_key():
    """The S1 harness does not know about T4/T5 and must still compute the pipeline's key.

    Without the default on ``asr_config`` the harness and production would quietly maintain two
    separate caches of the same thing - the exact duplication T8 removed.
    """
    from evaluation.harness import _harness_key

    monkey_free = transcript_cache.cache_key(
        "deadbeef",
        model=settings.whisper_model,
        language=None,
        translate=False,
        beam_size=5,
    )
    explicit = transcript_cache.cache_key(
        "deadbeef",
        model=settings.whisper_model,
        language=None,
        translate=False,
        beam_size=5,
        asr_config=transcript_cache.asr_fingerprint(""),
    )
    assert monkey_free == explicit
    assert callable(_harness_key)


# --------------------------------------------------------------------------- #
# T7 - confidence-driven captions
# --------------------------------------------------------------------------- #
def test_confidence_dimming_is_off_by_default():
    """A default that dimmed words would change every existing caption."""
    preset = CaptionPreset(name="t")
    assert preset.low_confidence_threshold == 0.0
    span = captions.build_word_span(W(0.0, 0.5, "maybe", probability=0.01), preset, False)
    assert "\\alpha" not in span


def test_a_doubted_word_is_dimmed_when_the_threshold_is_set():
    preset = CaptionPreset(name="t", low_confidence_threshold=0.5, low_confidence_alpha=0.5)
    span = captions.build_word_span(W(0.0, 0.5, "maybe", probability=0.1), preset, False)
    assert "\\alpha&H80&" in span, span
    assert span.endswith("{\\alpha&H00&}"), "the dim was not closed, so it would bleed onward"


def test_a_confident_word_is_untouched():
    preset = CaptionPreset(name="t", low_confidence_threshold=0.5)
    span = captions.build_word_span(W(0.0, 0.5, "certain", probability=0.99), preset, False)
    assert "\\alpha" not in span


def test_the_alpha_runs_the_right_way_round():
    """ASS alpha is inverted: ``&H00`` is opaque, ``&HFF`` transparent.

    Getting this backwards would make a doubted word *more* prominent than a confident one -
    the exact opposite of the intent, and it would still look deliberate on screen.
    """
    dim = CaptionPreset(name="t", low_confidence_threshold=0.9, low_confidence_alpha=0.2)
    bright = CaptionPreset(name="t", low_confidence_threshold=0.9, low_confidence_alpha=0.9)
    dim_tag = captions._dim_alpha_tag(dim)
    bright_tag = captions._dim_alpha_tag(bright)
    dim_value = int(dim_tag.split("&H")[1][:2], 16)
    bright_value = int(bright_tag.split("&H")[1][:2], 16)
    assert dim_value > bright_value, "lower opacity must mean a higher ASS alpha value"


def test_a_word_with_no_probability_reads_as_confident():
    """ "Unknown" must not mean "unsure".

    Treating a missing probability as doubt would dim every caption on any transcript without
    per-word confidence - the same failure C11 had, where a rule that fired on everything was
    indistinguishable from a rule that fired on nothing.
    """

    class Bare:
        start, end, text = 0.0, 0.5, "word"

    preset = CaptionPreset(name="t", low_confidence_threshold=0.9)
    assert "\\alpha" not in captions.build_word_span(Bare(), preset, False)


def test_emphasis_wins_over_doubt():
    """A word that earned emphasis has already been judged worth stating.

    Dimming and highlighting the same word are contradictory claims, and doing both would also
    put an alpha override inside the highlight colour span.
    """
    preset = CaptionPreset(name="t", low_confidence_threshold=0.9)
    span = captions.build_word_span(W(0.0, 0.5, "money", probability=0.1), preset, True)
    assert "\\alpha" not in span
    assert "\\c" in span


def test_dimming_survives_hostile_word_objects():
    class Bad:
        start, end, text = 0.0, 0.5, "x"
        probability = "very sure"

    preset = CaptionPreset(name="t", low_confidence_threshold=0.9)
    assert captions._is_doubted(Bad(), preset) is False
    assert captions._is_doubted(W(0, 1, "x", probability=float("nan")), preset) is False


# --------------------------------------------------------------------------- #
# M3 - WER benchmark
# --------------------------------------------------------------------------- #
def test_identical_text_scores_zero():
    assert wer.word_error_rate("hello there my friend", "hello there my friend").wer == 0.0


def test_normalisation_does_not_punish_punctuation_or_case():
    """Otherwise every model looks far worse than it is and the *differences* drown in noise."""
    result = wer.word_error_rate("Hello, there! My friend.", "hello there my friend")
    assert result.wer == 0.0


def test_contractions_and_digits_are_folded():
    assert wer.word_error_rate("do not go", "don't go").wer == 0.0
    assert wer.word_error_rate("ten people", "10 people").wer == 0.0
    assert wer.word_error_rate("I am here", "I'm here").wer == 0.0


def test_a_typographic_apostrophe_matches_an_ascii_one():
    """NFKC first, or a smart-quoted reference scores every contraction as an error."""
    assert wer.word_error_rate("don\u2019t go", "don't go").wer == 0.0


def test_the_three_error_kinds_are_counted_separately():
    """The kind of error is what makes the number actionable: deletions point at VAD (T5),
    substitutions at vocabulary (T4), and they call for opposite fixes."""
    sub = wer.word_error_rate("the quick brown fox", "the quick brown dog")
    assert (sub.substitutions, sub.deletions, sub.insertions) == (1, 0, 0)

    deleted = wer.word_error_rate("the quick brown fox", "the quick fox")
    assert (deleted.substitutions, deleted.deletions, deleted.insertions) == (0, 1, 0)

    inserted = wer.word_error_rate("the quick fox", "the quick brown fox")
    assert (inserted.substitutions, inserted.deletions, inserted.insertions) == (0, 0, 1)


def test_substitution_examples_are_reported():
    """The diagnostic the number cannot express."""
    result = wer.word_error_rate(
        "kubernetes is hard and kubernetes is slow", "coober netties is hard and cubanetes is slow"
    )
    assert result.examples, "no substitution examples were reported"
    assert any("kubernetes" in pair[0] for pair in result.examples)


def test_stemming_is_not_applied():
    """ "engineer" for "engineers" is a mistake a viewer sees; a stemmer would hide it."""
    assert wer.word_error_rate("the engineers left", "the engineer left").wer > 0.0


def test_an_empty_hypothesis_is_total_loss_not_a_crash():
    result = wer.word_error_rate("one two three", "")
    assert result.deletions == 3
    assert result.wer == 1.0


def test_an_empty_reference_does_not_divide_by_zero():
    assert wer.word_error_rate("", "").wer == 0.0
    assert wer.word_error_rate("", "spurious words here").wer == 1.0


def test_aggregate_pools_errors_rather_than_averaging_rates():
    """Averaging rates weights a ten-second clip like an hour-long talk.

    One short difficult file would then dominate a figure meant to describe the whole dataset.
    """
    short_bad = wer.word_error_rate("a b", "x y")  # 2 words, 100%
    long_good = wer.word_error_rate(" ".join(["w"] * 98), " ".join(["w"] * 98))  # 98 words, 0%
    pooled = wer.aggregate([short_bad, long_good])
    assert pooled.reference_words == 100
    assert pooled.wer == pytest.approx(0.02)
    # The naive mean of the two rates would be 50%.
    assert pooled.wer < 0.5


def test_aggregate_of_nothing_is_empty_not_an_error():
    assert wer.aggregate([]).wer == 0.0


def test_the_comparison_table_ranks_best_first_and_shows_the_gap():
    rows = [
        ("small", wer.word_error_rate("a b c d e f g h i j", "a b c d e f g h i x")),
        ("base", wer.word_error_rate("a b c d e f g h i j", "a b c d e f g x y z")),
    ]
    table = wer.format_comparison(rows)
    lines = [line for line in table.splitlines() if line.strip()]
    assert "small" in lines[2], table
    assert "vs best" in lines[0]


def test_the_table_names_the_dominant_error_kind():
    """Points the reader at T4 or T5 rather than leaving them with a percentage."""
    deletions = wer.word_error_rate("a b c d e f g h", "a b")
    assert "VAD" in wer.format_comparison([("m", deletions)])

    subs = wer.word_error_rate("a b c d", "w x y z")
    assert "vocabulary" in wer.format_comparison([("m", subs)])


def test_the_empty_table_does_not_raise():
    assert wer.format_comparison([]) == "no results"
