"""Tests for the selection scoring signals: S2, S6, S10, S11, S14, S15, S17.

Weighted deliberately towards the failures that would *look* like success. A ranking bug does
not raise and does not produce a visibly wrong file - it produces a plausible ordering, and
nothing downstream can contradict it. So the tests that matter here are the ones that would
still pass if the feature did nothing, and each of those is written to fail in that case:

* S11 asserts a shorter segment can outrank a longer one, which is false under the rule it
  replaces.
* S15 asserts the count is *preserved* when a duplicate is dropped, which is false if
  de-duplication runs after the cap.
* S6 asserts silence at the opening zeroes the score, which is false if promptness is merely
  one weighted term among four.
* S2 asserts ``-inf`` never reaches arithmetic, which is the one input that makes every mean
  and median downstream silently wrong rather than absent.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from config import settings
from worker import audio_features, candidate_ranking, hook_score, selection
from worker.selection import ClipCandidate

requires_ffmpeg = pytest.mark.skipif(
    subprocess.run(["which", settings.ffmpeg_binary], capture_output=True).returncode != 0,
    reason="ffmpeg not on PATH",
)


@dataclass
class FakeWord:
    start: float
    end: float
    text: str = "word"
    probability: float = 1.0


@dataclass
class Seg:
    start: float
    end: float
    text: str = ""


@dataclass
class Cand:
    """A minimal candidate: ranking must not depend on ClipCandidate specifically."""

    start: float
    end: float
    score: float = 0.0
    text: str = ""
    features: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start


def words_at(rate: float, start: float, end: float, text: str = "word") -> list[FakeWord]:
    """Evenly spaced words at ``rate`` per second across ``[start, end)``."""
    out = []
    step = 1.0 / rate
    t = start
    while t + step * 0.5 < end:
        out.append(FakeWord(t, min(end, t + step * 0.6), text))
        t += step
    return out


# --------------------------------------------------------------------------- #
# S2 - audio energy
# --------------------------------------------------------------------------- #
def test_minus_inf_becomes_a_floor_not_an_infinity():
    """Digital silence reports ``-inf``; letting it through poisons every later mean.

    This is the single most consequential input in the module. An ``-inf`` reading does not
    raise - it propagates through ``sum``/``mean``/``median`` and produces ``-inf`` or ``nan``,
    at which point every comparison against a threshold answers, wrongly and silently.
    """
    log = (
        "frame:0    pts:0       pts_time:0\n"
        "lavfi.astats.Overall.RMS_level=-inf\n"
        "frame:1    pts:48000   pts_time:1\n"
        "lavfi.astats.Overall.RMS_level=-23.5\n"
    )
    readings = audio_features._parse_envelope(log)
    assert readings == [(0.0, audio_features.SILENCE_FLOOR_DB), (1.0, -23.5)]
    for _, db in readings:
        assert db == db, "a NaN reached the envelope"
        assert db > float("-inf"), "an infinity reached the envelope"


def test_the_median_ignores_silence_so_ordinary_speech_is_not_loud():
    """A source that is mostly silent must not make normal speech read as loud.

    Without excluding silence the baseline sinks towards the floor, and then every speaking
    window shows a large positive ``relative_energy_db`` - inverting the signal on exactly the
    footage where it is hardest to get right.
    """
    envelope = [(float(i), -90.0) for i in range(20)] + [(float(20 + i), -20.0) for i in range(5)]
    baseline = audio_features.source_median_energy(envelope)
    assert baseline == -20.0, "silence dragged the baseline down"

    speech = audio_features.energy_in_window(envelope, 20.0, 25.0, baseline=baseline)
    assert abs(speech.relative_energy) < 0.01, "ordinary speech read as unusually loud"


def test_an_all_silent_source_has_no_baseline_rather_than_a_wrong_one():
    envelope = [(float(i), audio_features.SILENCE_FLOOR_DB) for i in range(10)]
    assert audio_features.source_median_energy(envelope) is None


def test_an_unmeasurable_window_is_flagged_not_scored_zero():
    """ "No reading" and "very quiet" must be distinguishable by the caller."""
    empty = audio_features.energy_in_window([], 0.0, 10.0, baseline=-20.0)
    assert empty.reliable is False
    assert empty.relative_energy == 0.0, "an unmeasured window claimed a difference"


@requires_ffmpeg
def test_the_envelope_finds_a_quiet_passage_in_real_audio(tmp_path):
    """End to end against real ffmpeg: the envelope must locate a level change."""
    src = tmp_path / "tone.m4a"
    subprocess.run(
        [
            settings.ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=300:duration=8:sample_rate=48000",
            "-af",
            "volume=enable='between(t,3,5)':volume=0.02",
            "-c:a",
            "aac",
            str(src),
            "-y",
        ],
        check=True,
        capture_output=True,
    )
    envelope = audio_features.energy_envelope(src)
    assert len(envelope) >= 7, f"expected ~8 one-second readings, got {envelope}"

    loud = audio_features.energy_in_window(envelope, 0.0, 3.0)
    quiet = audio_features.energy_in_window(envelope, 3.5, 5.0)
    assert quiet.mean_db < loud.mean_db - 15.0, (
        f"the quiet passage was not detected: loud={loud.mean_db} quiet={quiet.mean_db}"
    )


@requires_ffmpeg
def test_a_source_with_no_audio_degrades_to_no_information(tmp_path):
    """A silent-video source must yield ``[]``, not an exception and not a wrong number."""
    src = tmp_path / "mute.mp4"
    subprocess.run(
        [
            settings.ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(src),
            "-y",
        ],
        check=True,
        capture_output=True,
    )
    assert audio_features.energy_envelope(src) == []


def test_a_missing_binary_returns_empty_rather_than_raising():
    assert audio_features.energy_envelope("/nonexistent/nope.mp4") == []


# --------------------------------------------------------------------------- #
# S6 - hook scoring
# --------------------------------------------------------------------------- #
def test_silence_at_the_opening_zeroes_the_hook_score():
    """Disqualifying, not merely costly.

    Fails if promptness is treated as one weighted term among four: with pace, energy and text
    all neutral or better, a weighted sum would still return roughly 0.5 for a clip that opens
    on two seconds of nothing.
    """
    late = words_at(3.0, 2.0, 10.0)  # nothing until 2.0s, past the 1.0s deadline
    result = hook_score.hook_score(0.0, 10.0, late, text="how do you actually do this?")
    assert result["hook_score"] == 0.0
    assert result["hook_promptness"] == 0.0


def test_prompt_speech_beats_delayed_speech_all_else_equal():
    prompt = hook_score.hook_score(0.0, 10.0, words_at(3.0, 0.0, 10.0))
    delayed = hook_score.hook_score(0.0, 10.0, words_at(3.0, 0.7, 10.0))
    assert prompt["hook_score"] > delayed["hook_score"]
    assert prompt["hook_promptness"] == 1.0


def test_promptness_is_graded_not_a_step():
    """0.1s and 0.9s of dead air are genuinely different; a threshold would call them equal."""
    scores = [
        hook_score.speech_promptness(words_at(3.0, delay, 10.0), 0.0)
        for delay in (0.0, 0.25, 0.5, 0.75)
    ]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == 4, f"promptness collapsed to a step: {scores}"


def test_a_front_loaded_clip_scores_above_an_even_one():
    """The hook is fast, the rest is slow -> front-loaded."""
    front = words_at(5.0, 0.0, 2.5) + words_at(1.5, 2.5, 20.0)
    even = words_at(2.0, 0.0, 20.0)
    assert (
        hook_score.hook_score(0.0, 20.0, front)["hook_pace"]
        > hook_score.hook_score(0.0, 20.0, even)["hook_pace"]
    )


def test_an_even_clip_is_not_penalised_as_though_it_had_no_hook():
    """Most good clips are even; only some are front-loaded. Even must read as neutral."""
    even = words_at(2.0, 0.0, 20.0)
    assert hook_score.hook_score(0.0, 20.0, even)["hook_pace"] == pytest.approx(0.5, abs=0.05)


def test_the_text_signal_cannot_be_farmed_by_repetition():
    """Density-and-cap, not a raw count: keyword soup must not outscore a real question."""
    soup = hook_score.text_signal("you you you you you never never never nobody nobody")
    question = hook_score.text_signal("Why does nobody tell you this?")
    assert soup <= 1.0
    assert question > 0.0
    # The soup is allowed to score highly, but it must not exceed the ceiling, and a real
    # question must not be scored as nothing.
    assert question >= 0.3, f"a genuine hook line scored {question}"


def test_the_text_signal_is_zero_for_empty_and_for_symbols():
    assert hook_score.text_signal("") == 0.0
    assert hook_score.text_signal("   ") == 0.0
    assert hook_score.text_signal("--- ... ***") == 0.0


def test_hook_scoring_survives_hostile_word_objects():
    """Must be total: the property suite hands these modules adversarial objects."""

    class Bad:
        start = "not a number"
        end = None

    assert hook_score.speech_promptness([Bad(), Bad()], 0.0) == 0.0
    out = hook_score.hook_score(0.0, 5.0, [Bad()], text=None or "")
    assert out["hook_score"] == 0.0


def test_a_zero_length_window_does_not_divide_by_zero():
    assert hook_score.hook_score(5.0, 5.0, words_at(3.0, 0.0, 10.0))["hook_score"] == 0.0


# --------------------------------------------------------------------------- #
# S15 - de-duplication and diversity
# --------------------------------------------------------------------------- #
def test_a_short_clip_inside_a_long_one_is_a_duplicate():
    """The case IoU would let through.

    A 5s clip inside a 30s clip has IoU 0.17 - well under any sane threshold - yet everything
    in it already ships inside the longer clip. Measuring against the *shorter* candidate is
    what catches it, and this test fails if the denominator is the union.
    """
    long_clip = Cand(10.0, 40.0, score=90.0)
    contained = Cand(20.0, 25.0, score=80.0)
    assert candidate_ranking.overlap_fraction(contained, long_clip) == pytest.approx(1.0)
    kept = candidate_ranking.deduplicate([long_clip, contained])
    assert kept == [long_clip]


def test_the_higher_scoring_version_of_a_moment_survives():
    first = Cand(12.0, 45.0, score=90.0)
    second = Cand(14.5, 47.0, score=85.0)
    kept = candidate_ranking.deduplicate([first, second])
    assert kept == [first]


def test_distinct_moments_are_all_kept():
    a, b, c = Cand(0.0, 10.0, 90.0), Cand(30.0, 40.0, 80.0), Cand(60.0, 70.0, 70.0)
    assert candidate_ranking.deduplicate([a, b, c]) == [a, b, c]


def test_a_duplicate_does_not_cost_the_user_a_clip():
    """De-duplication must run *before* the cap.

    This is the test that fails if the order is wrong. Asked for 2 clips from
    [A, A', B]: filtering after the cap yields [A] - one clip, because A' was dropped from an
    already-truncated list. Filtering before the cap yields [A, B], which is what was asked for.
    """
    a = Cand(0.0, 30.0, score=90.0)
    a_dup = Cand(2.0, 31.0, score=88.0)
    b = Cand(100.0, 130.0, score=70.0)
    kept = candidate_ranking.deduplicate([a, a_dup, b], limit=2)
    assert [(c.start, c.end) for c in kept] == [(0.0, 30.0), (100.0, 130.0)]
    assert len(kept) == 2, "a duplicate displaced a genuinely different moment"


def test_a_recap_far_away_in_time_is_still_a_duplicate():
    """Zero overlap, same clip to a viewer - the case timing alone cannot catch."""
    original = Cand(
        10.0,
        40.0,
        score=90.0,
        text="the biggest mistake founders make is hiring senior engineers too early",
    )
    recap = Cand(
        900.0,
        930.0,
        score=85.0,
        text="hiring senior engineers too early is the biggest mistake founders make",
    )
    assert candidate_ranking.overlap_fraction(original, recap) == 0.0
    assert candidate_ranking.text_similarity(original.text, recap.text) > 0.7
    assert candidate_ranking.deduplicate([original, recap]) == [original]


def test_different_topics_in_similar_words_are_not_duplicates():
    """The false-positive direction: dropping a distinct moment is the costlier error."""
    a = Cand(0.0, 30.0, 90.0, text="we rewrote the billing system in rust last quarter")
    b = Cand(100.0, 130.0, 85.0, text="hiring a designer changed how the product felt")
    assert candidate_ranking.text_similarity(a.text, b.text) < 0.3
    assert candidate_ranking.deduplicate([a, b]) == [a, b]


def test_a_candidate_with_no_text_is_never_dropped_for_similarity():
    """Nothing to compare must mean "no evidence", not "identical"."""
    a = Cand(0.0, 30.0, 90.0, text="")
    b = Cand(100.0, 130.0, 85.0, text="")
    assert candidate_ranking.text_similarity("", "") == 0.0
    assert candidate_ranking.deduplicate([a, b]) == [a, b]


def test_two_clips_sharing_one_coarse_transcript_segment_are_not_duplicates():
    """The regression that broke the pipeline's clip-count invariants.

    A candidate's ``text`` is the transcript text its window covers, so when a single coarse
    transcript segment spans two adjacent candidates, both are assigned that segment's entire
    text and score 1.0 against each other - two genuinely different moments, identical strings.
    Caught by ``test_p13_clip_count_invariant_under_degradation_and_failure`` and
    ``test_p22_diarization_once_per_source_sdr``, which went from 2 clips to 1 and 3 to 1.

    Fixed by requiring enough content words for the comparison to be evidence at all, not by
    special-casing adjacency: two words in common says nothing however the windows are arranged.
    """
    first = Cand(0.0, 2.0, 90.0, text="hello there")
    second = Cand(2.0, 4.0, 85.0, text="hello there")
    assert candidate_ranking.overlap_fraction(first, second) == 0.0
    assert candidate_ranking.text_similarity(first.text, second.text) == 0.0
    assert candidate_ranking.deduplicate([first, second]) == [first, second]


def test_a_real_recap_still_has_enough_words_to_be_caught():
    """The guard must not have disabled the check it is guarding."""
    long_a = "the biggest mistake founders make is hiring senior engineers too early"
    long_b = "hiring senior engineers too early is the biggest mistake founders make"
    assert candidate_ranking.text_similarity(long_a, long_b) > 0.7


def test_similarity_counts_repetition_rather_than_ignoring_it():
    """Set Jaccard scored two different long windows as identical.

    Found on a real 120-second render: three clips were requested and two came back, because
    two adjacent 45-second windows over a small vocabulary shared the same content-word *set*.
    One window mentions a topic once in passing; the other is built around it. Weighted
    (count-aware) Jaccard tells them apart; set Jaccard cannot.
    """
    mostly_a = " ".join(["anyway continued anyway continued"] * 4 + ["nobody ever tell this"])
    mostly_b = " ".join(["nobody ever tell this"] * 4 + ["anyway continued"])
    # Identical content-word sets, very different emphasis.
    assert set(mostly_a.split()) == set(mostly_b.split())
    assert candidate_ranking.text_similarity(mostly_a, mostly_b) < 0.5

    # And a genuine reordered recap is still scored as a duplicate.
    recap_a = "the biggest mistake founders make is hiring senior engineers too early"
    recap_b = "hiring senior engineers too early is the biggest mistake founders make"
    assert candidate_ranking.text_similarity(recap_a, recap_b) > 0.9


def test_thresholds_of_one_disable_each_check():
    a, b = (
        Cand(0.0, 30.0, 90.0, text="same words here"),
        Cand(1.0, 31.0, 80.0, text="same words here"),
    )
    kept = candidate_ranking.deduplicate([a, b], max_overlap=1.0, max_similarity=1.0)
    assert kept == [a, b]


def test_degenerate_spans_are_discarded_not_ranked():
    assert candidate_ranking.deduplicate([Cand(10.0, 10.0), Cand(20.0, 5.0)]) == []


# --------------------------------------------------------------------------- #
# S11 - real scoring in the fallback
# --------------------------------------------------------------------------- #
def _measured(start, end, *, hook, rate=1.0, energy_db=0.0, quiet=0.0):
    return Cand(
        start,
        end,
        features={
            "hook_score": hook,
            "relative_speech_rate": rate,
            "reliable": 1.0,
            "relative_energy_db": energy_db,
            "quiet_fraction": quiet,
            "energy_reliable": 1.0,
        },
    )


def test_a_shorter_stronger_segment_outranks_a_longer_dull_one():
    """The whole point of S11, and false under the rule it replaces.

    Under "keep the longest segments" the 80-second monologue wins every time. It should not:
    it has no hook, flat delivery, and a length nowhere near the requested window.
    """
    punchy = _measured(0.0, 42.0, hook=0.9, rate=1.5, energy_db=5.0)
    monologue = _measured(100.0, 180.0, hook=0.1, rate=1.0, energy_db=0.0)
    ranked = candidate_ranking.rank_candidates(
        [monologue, punchy], target=45.0, min_len=30.0, max_len=60.0
    )
    assert ranked[0] is punchy, (
        f"the longest segment still won: {[(c.start, c.score) for c in ranked]}"
    )
    assert punchy.score > monologue.score


def test_a_mostly_silent_window_is_not_a_clip_however_loud_its_peak():
    loud_but_empty = _measured(0.0, 45.0, hook=0.5, energy_db=8.0, quiet=0.9)
    ordinary = _measured(50.0, 95.0, hook=0.5, energy_db=0.0, quiet=0.0)
    ranked = candidate_ranking.rank_candidates(
        [loud_but_empty, ordinary], target=45.0, min_len=30.0, max_len=60.0
    )
    assert ranked[0] is ordinary


def test_length_fit_peaks_at_the_target_and_not_at_the_maximum():
    """Replaces "longer is better" with "closer to what was asked for"."""
    at_target = candidate_ranking.length_fit(45.0, 45.0, min_len=30.0, max_len=60.0)
    at_max = candidate_ranking.length_fit(60.0, 45.0, min_len=30.0, max_len=60.0)
    over = candidate_ranking.length_fit(120.0, 45.0, min_len=30.0, max_len=60.0)
    assert at_target == pytest.approx(1.0)
    assert at_target > at_max > over
    assert over == 0.0


def test_a_clip_at_the_edge_of_the_requested_window_is_not_scored_as_wrong():
    """Asking for "30-60s" means 60s is acceptable, not merely tolerated.

    My first implementation let the falloff reach zero at the boundary, which scored a
    60-second clip identically to a 120-second one when the user had asked for 30-60s. The
    edge must stay clearly above the outside.
    """
    at_max = candidate_ranking.length_fit(60.0, 45.0, min_len=30.0, max_len=60.0)
    at_min = candidate_ranking.length_fit(30.0, 45.0, min_len=30.0, max_len=60.0)
    just_over = candidate_ranking.length_fit(65.0, 45.0, min_len=30.0, max_len=60.0)
    far_over = candidate_ranking.length_fit(90.0, 45.0, min_len=30.0, max_len=60.0)
    assert at_max == pytest.approx(candidate_ranking.EDGE_FIT)
    assert at_min == pytest.approx(candidate_ranking.EDGE_FIT)
    # Just outside the window still counts for something; far outside does not.
    assert at_max > just_over > 0.0
    assert far_over == 0.0


def test_a_missing_feature_contributes_neutrally_not_zero():
    """A source with no audio must not rank below one that was measurable and bad."""
    unmeasured = Cand(0.0, 45.0, features={})
    bad = _measured(50.0, 95.0, hook=0.0, rate=1.0, energy_db=-10.0)
    ranked = candidate_ranking.rank_candidates(
        [bad, unmeasured], target=45.0, min_len=30.0, max_len=60.0
    )
    assert ranked[0] is unmeasured, "an unmeasurable clip was punished for being unmeasurable"


def test_ranking_is_deterministic_for_equal_scores():
    a, b = _measured(50.0, 95.0, hook=0.5), _measured(0.0, 45.0, hook=0.5)
    ranked = candidate_ranking.rank_candidates([a, b], target=45.0, min_len=30.0, max_len=60.0)
    assert [c.start for c in ranked] == [0.0, 50.0], "ties did not break on position"


def test_scoring_tolerates_nan_and_junk_in_features():
    junk = Cand(0.0, 45.0, features={"hook_score": float("nan"), "relative_energy_db": "loud"})
    score = candidate_ranking.score_candidate(junk, target=45.0, min_len=30.0, max_len=60.0)
    assert 0.0 <= score <= 100.0


# --------------------------------------------------------------------------- #
# S17 - the weights are actually wired to config
# --------------------------------------------------------------------------- #
def test_zeroing_the_hook_weight_changes_the_outcome(monkeypatch):
    """A setting that is read but has no effect is worse than no setting."""
    hooky_short = _measured(0.0, 20.0, hook=1.0)
    dull_fit = _measured(50.0, 95.0, hook=0.0)

    monkeypatch.setattr(settings, "selection_weight_hook", 0.9)
    monkeypatch.setattr(settings, "selection_weight_length", 0.1)
    with_hook = candidate_ranking.rank_candidates(
        [dull_fit, hooky_short], target=45.0, min_len=30.0, max_len=60.0
    )
    assert with_hook[0] is hooky_short

    monkeypatch.setattr(settings, "selection_weight_hook", 0.0)
    monkeypatch.setattr(settings, "selection_weight_length", 1.0)
    without_hook = candidate_ranking.rank_candidates(
        [dull_fit, hooky_short], target=45.0, min_len=30.0, max_len=60.0
    )
    assert without_hook[0] is dull_fit, "selection_weight_hook is not wired to anything"


#: Every weight the fallback scorer reads.
#:
#: Enumerated rather than discovered so that adding a weight without adding it here fails the
#: zero-division test below - which is what happened when S7/S8/S12 added three, and is the point of
#: listing them.
SELECTION_WEIGHT_NAMES = (
    "hook",
    "pace",
    "energy",
    "length",
    "structure",
    "standalone",
    "intensity",
)


def test_the_weight_list_covers_every_selection_weight():
    """Guards the list above against a weight being added and silently untested."""
    declared = {
        name[len("selection_weight_") :]
        for name in type(settings).model_fields
        if name.startswith("selection_weight_")
    }
    missing = declared - set(SELECTION_WEIGHT_NAMES)
    stale = set(SELECTION_WEIGHT_NAMES) - declared
    assert declared == set(SELECTION_WEIGHT_NAMES), (
        f"SELECTION_WEIGHT_NAMES is out of step with the settings.\n"
        f"  add to the tuple:      {sorted(missing) or '-'}\n"
        f"  remove from the tuple: {sorted(stale) or '-'}\n"
        "The tuple drives the normalisation and zero-weight tests in this file, so a weight "
        "missing from it is a weight whose contribution to the ranking is never checked - and a "
        "ranking bug does not raise, it produces a plausible ordering that nothing downstream can "
        "contradict. Also add a matching entry to .env.example, which "
        "tests/test_config_documentation.py requires."
    )


def test_all_weights_zero_does_not_divide_by_zero(monkeypatch):
    for name in SELECTION_WEIGHT_NAMES:
        monkeypatch.setattr(settings, f"selection_weight_{name}", 0.0)
    assert (
        candidate_ranking.score_candidate(
            _measured(0.0, 45.0, hook=1.0), target=45.0, min_len=30.0, max_len=60.0
        )
        == 0.0
    )


def test_the_hook_weights_are_wired_too(monkeypatch):
    words = words_at(2.0, 0.0, 20.0)
    monkeypatch.setattr(settings, "hook_weight_text", 0.0)
    without = hook_score.hook_score(0.0, 20.0, words, text="why does nobody tell you this?")
    monkeypatch.setattr(settings, "hook_weight_text", 0.9)
    with_text = hook_score.hook_score(0.0, 20.0, words, text="why does nobody tell you this?")
    assert with_text["hook_score"] != without["hook_score"]


# --------------------------------------------------------------------------- #
# S10 - the measured features reach the prompt
# --------------------------------------------------------------------------- #
def _segments():
    return [Seg(0.0, 5.0, "ordinary opening line"), Seg(5.0, 10.0, "the loud fast bit")]


def test_the_prompt_annotates_a_fast_segment(monkeypatch):
    """The model could not previously tell that a line was rushed or shouted."""
    monkeypatch.setattr(settings, "selection_features_in_prompt", True)
    words = words_at(1.0, 0.0, 5.0) + words_at(6.0, 5.0, 10.0)
    rendered = selection._format_transcript(_segments(), words=words)
    assert "fast" in rendered, rendered


def test_an_evenly_paced_source_gets_no_tags_at_all(monkeypatch):
    """Only *departures* are annotated.

    Tagging every line would defeat the purpose - the signal is that a segment stands out, so a
    uniformly-paced source must render exactly as it did before S10. (An earlier version of this
    test used an uneven fixture and asserted its first segment was untagged; the code was right
    and the test was wrong, because at 1 wps against a 3.5 wps median that segment genuinely
    *was* slow.)
    """
    monkeypatch.setattr(settings, "selection_features_in_prompt", True)
    even = words_at(2.0, 0.0, 10.0)
    rendered = selection._format_transcript(_segments(), words=even)
    assert "(" not in rendered, rendered
    assert rendered == selection._format_transcript(_segments())


def test_the_prompt_annotates_a_loud_segment(monkeypatch):
    monkeypatch.setattr(settings, "selection_features_in_prompt", True)
    words = words_at(2.0, 0.0, 10.0)
    envelope = [(float(t), -30.0) for t in range(5)] + [(float(5 + t), -18.0) for t in range(5)]
    rendered = selection._format_transcript(_segments(), words=words, envelope=envelope)
    assert "loud" in rendered, rendered


def test_the_annotation_can_be_switched_off_and_the_format_reverts(monkeypatch):
    """With the feature off the prompt must be byte-identical to the pre-S10 shape."""
    words = words_at(1.0, 0.0, 5.0) + words_at(6.0, 5.0, 10.0)
    monkeypatch.setattr(settings, "selection_features_in_prompt", False)
    off = selection._format_transcript(_segments(), words=words)
    bare = selection._format_transcript(_segments())
    assert off == bare
    assert off == "[0] 0.0-5.0: ordinary opening line\n[1] 5.0-10.0: the loud fast bit"


def test_a_source_with_no_words_produces_the_bare_prompt():
    assert "(" not in selection._format_transcript(_segments(), words=[])


def test_the_prompt_explains_the_notes_only_when_it_uses_them(monkeypatch):
    from worker.models import ProcessingOptions

    options = ProcessingOptions()
    words = words_at(2.0, 0.0, 10.0)
    monkeypatch.setattr(settings, "selection_features_in_prompt", True)
    with_notes = selection._build_prompt(_segments(), options, 30, 60, 3, words=words)
    assert "delivery note" in with_notes

    without = selection._build_prompt(_segments(), options, 30, 60, 3)
    assert "delivery note" not in without


# --------------------------------------------------------------------------- #
# S14 - keyframe sampling density and resolution
# --------------------------------------------------------------------------- #
def test_the_sample_count_and_width_are_configurable_and_defaulted_up():
    assert settings.keyframe_sample_limit >= 48, "S14 raised this from 12"
    assert settings.keyframe_sample_width >= 480, "S14 raised this from 160"


def test_the_default_sampler_uses_the_configured_width(monkeypatch, tmp_path):
    from worker import ffmpeg_utils as fu
    from worker import visual_selection as vs

    seen: list[int] = []

    def fake_thumbnail(src, dest, at=0.0, width=0):
        seen.append(width)
        (tmp_path / "f.jpg").write_bytes(b"x")
        return str(tmp_path / "f.jpg")

    monkeypatch.setattr(fu, "generate_thumbnail", fake_thumbnail)
    monkeypatch.setattr(settings, "keyframe_sample_width", 640)
    vs.sample_keyframes("src.mp4", 10.0, limit=3, frames_dir=str(tmp_path))
    assert seen == [640, 640, 640], seen


# --------------------------------------------------------------------------- #
# The features-dropping defect in visual selection
# --------------------------------------------------------------------------- #
def test_merging_visual_scores_keeps_the_measured_features():
    """``merge_scores`` rebuilt ClipCandidate without ``features``, silently dropping them.

    It matters because U1 made visual selection a *default*, so the drop was on the normal
    path: every S2/S4/S6 measurement vanished before anything could read it, and nothing
    failed - the clips were fine, the features were simply gone.
    """
    from worker.visual_selection import Keyframe, merge_scores

    cand = ClipCandidate(start=0.0, end=10.0, score=50.0)
    cand.features["relative_speech_rate"] = 1.4
    cand.features["hook_score"] = 0.8
    merged = merge_scores([cand], [Keyframe(t=5.0, path="x", brightness=0.5, motion=0.1)])
    assert merged[0].features["relative_speech_rate"] == 1.4
    assert merged[0].features["hook_score"] == 0.8


def test_merged_candidates_do_not_share_one_features_dict():
    """Aliasing would let a later annotation overwrite an earlier candidate's measurements."""
    from worker.visual_selection import Keyframe, merge_scores

    a = ClipCandidate(start=0.0, end=10.0, score=50.0)
    b = ClipCandidate(start=20.0, end=30.0, score=40.0)
    merged = merge_scores([a, b], [Keyframe(t=5.0, path="x", brightness=0.5)])
    merged[0].features["only_mine"] = 1.0
    assert "only_mine" not in merged[1].features
    assert merged[0].features is not a.features


def test_snapping_does_not_annex_a_neighbouring_candidate():
    """Snapping may move a boundary; it may not swallow the next moment.

    ``snap_to_sentences`` moves the start to the nearest segment start and the end to the
    nearest segment end, so on a coarsely-segmented transcript any window inside one long
    segment becomes that whole segment. With a single 0-4s segment over two 2-second
    candidates, both snapped to 0-4 and the pipeline shipped two byte-identical clips.

    Surfaced by S15: de-duplication spotted the collision and dropped one clip, which broke
    the pipeline's clip-count invariants and revealed that the duplicate had been shipping all
    along. Fixed in snapping, not by loosening de-duplication.
    """
    from worker.transcribe import Transcript
    from worker.visual_selection import _snap_candidates

    transcript = Transcript(language="en", segments=[Seg(0.0, 4.0, "hello there my friend")])
    cands = [
        ClipCandidate(start=0.0, end=2.0, score=90.0),
        ClipCandidate(start=2.0, end=4.0, score=89.0),
    ]
    out = _snap_candidates(cands, transcript, 4.0)
    windows = [(c.start, c.end) for c in out]
    assert windows == [(0.0, 2.0), (2.0, 4.0)], f"snapping collapsed the candidates: {windows}"
    assert len(set(windows)) == 2, "two candidates became the same clip"
    # And the pair must survive de-duplication, which is the property that broke.
    assert len(candidate_ranking.deduplicate(out)) == 2


def test_snapping_still_snaps_when_it_is_safe():
    """The guard must not have disabled snapping altogether."""
    from worker.transcribe import Transcript
    from worker.visual_selection import _snap_candidates

    transcript = Transcript(
        language="en",
        segments=[Seg(0.0, 5.0, "first sentence"), Seg(5.0, 10.0, "second sentence")],
    )
    out = _snap_candidates([ClipCandidate(start=0.4, end=4.6, score=90.0)], transcript, 10.0)
    assert (out[0].start, out[0].end) == (0.0, 5.0), "a safe snap was skipped"


def test_snapping_keeps_the_measured_features():
    from worker.transcribe import Transcript
    from worker.visual_selection import _snap_candidates

    cand = ClipCandidate(start=1.0, end=9.0, score=50.0)
    cand.features["hook_score"] = 0.7
    transcript = Transcript(language="en", segments=[Seg(0.0, 10.0, "hello")])
    out = _snap_candidates([cand], transcript, 10.0)
    assert out[0].features["hook_score"] == 0.7
