"""Tests for the selection evaluation harness (S1).

The harness is the instrument every §3 change will be judged with, which makes its own
correctness unusually load-bearing: a bug here does not produce a visible failure, it produces
a *plausible number*. If matching is too generous, a change that did nothing looks like an
improvement and gets shipped; if the baselines are wrong, a selector that is no better than
guessing looks like it works. Nothing downstream would contradict either.

So these tests are mostly about the scoring being hard to fool.
"""

from __future__ import annotations

import json

import pytest

from evaluation import baselines, harness
from evaluation.dataset import DatasetError, load_dataset, load_label_file
from evaluation.metrics import (
    IOU_THRESHOLDS,
    PRIMARY_IOU,
    AggregateScore,
    iou,
    match_predictions,
    score_source,
)
from evaluation.report import render_comparison, render_text


class Span:
    """A minimal ``start``/``end`` carrier, standing in for a ClipCandidate or a moment."""

    def __init__(self, start: float, end: float):
        self.start = float(start)
        self.end = float(end)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Span({self.start}, {self.end})"


def _write_labels(directory, name, moments, source="video.mp4"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(
        json.dumps({
            "source": source,
            "moments": [{"start": s, "end": e, "note": ""} for s, e in moments],
        }),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# Overlap                                                                      #
# --------------------------------------------------------------------------- #
def test_iou_of_identical_ranges_is_one():
    assert iou(Span(10, 40), Span(10, 40)) == pytest.approx(1.0)


def test_iou_of_disjoint_ranges_is_zero():
    assert iou(Span(0, 10), Span(20, 30)) == 0.0
    # Touching at a point is not overlap.
    assert iou(Span(0, 10), Span(10, 20)) == 0.0


def test_iou_is_symmetric_and_bounded():
    a, b = Span(10, 40), Span(25, 60)
    assert iou(a, b) == pytest.approx(iou(b, a))
    assert 0.0 < iou(a, b) < 1.0


def test_iou_penalises_a_prediction_that_swallows_the_moment():
    """Union is the denominator on purpose.

    Returning the whole video would otherwise "contain" every labelled moment and score
    perfectly - the easiest way to game a selection benchmark, and the least useful output.
    """
    moment = Span(100, 130)
    whole_video = Span(0, 3600)
    assert iou(whole_video, moment) < 0.02


def test_a_zero_length_prediction_never_matches():
    """Otherwise a selector could score by returning timestamps instead of clips."""
    assert iou(Span(50, 50), Span(40, 60)) == 0.0
    assert match_predictions([Span(50, 50)], [Span(40, 60)], 0.01) == []


# --------------------------------------------------------------------------- #
# Matching                                                                     #
# --------------------------------------------------------------------------- #
def test_matching_is_one_to_one():
    """Five near-identical clips over one moment is one hit, not five.

    Without this the benchmark would reward exactly the redundancy S15 exists to remove: a
    selector could return the same good moment repeatedly and score perfect precision.
    """
    moment = Span(100, 130)
    duplicates = [Span(100, 130), Span(101, 131), Span(99, 129), Span(102, 132)]
    matches = match_predictions(duplicates, [moment], PRIMARY_IOU)
    assert len(matches) == 1


def test_each_prediction_matches_at_most_one_label():
    """A single long clip spanning two labelled moments is one hit, not two."""
    labels = [Span(100, 130), Span(140, 170)]
    greedy = [Span(100, 170)]
    assert len(match_predictions(greedy, labels, 0.3)) == 1


def test_matching_prefers_the_better_overlap():
    """When two predictions compete for one label, the closer one wins it."""
    label = Span(100, 130)
    predictions = [Span(105, 140), Span(100, 130)]   # second is exact
    matches = match_predictions(predictions, [label], PRIMARY_IOU)
    assert [m.prediction_index for m in matches] == [1]


def test_matching_is_deterministic_for_equally_good_pairs():
    """Two runs must produce identical numbers, or the harness cannot detect a change."""
    labels = [Span(0, 30), Span(100, 130)]
    predictions = [Span(0, 30), Span(100, 130)]
    first = match_predictions(predictions, labels, PRIMARY_IOU)
    for _ in range(5):
        assert match_predictions(predictions, labels, PRIMARY_IOU) == first


def test_threshold_is_respected():
    label = Span(0, 100)
    half = Span(50, 150)      # IoU = 50/150 = 0.333
    assert match_predictions([half], [label], 0.3)
    assert not match_predictions([half], [label], 0.5)


# --------------------------------------------------------------------------- #
# Scoring                                                                      #
# --------------------------------------------------------------------------- #
def test_precision_and_recall_are_distinct():
    """Returning one safe clip while missing nine moments is not success.

    Precision alone would call that perfect, which is why recall is reported beside it.
    """
    labels = [Span(i * 100, i * 100 + 30) for i in range(10)]
    score = score_source("s", [Span(0, 30)], labels, k=5)
    at = score.at(PRIMARY_IOU)
    assert at.precision == pytest.approx(1.0)
    assert at.recall == pytest.approx(0.1)
    assert at.f1 < 0.2


def test_k_truncates_before_scoring():
    """precision@k asks about the *top* k, because that is what a user looks at.

    A selector returning thirty clips to be sure of covering five moments has not solved the
    problem, and without truncation it would score as though it had.
    """
    labels = [Span(500, 530)]
    padding = [Span(i * 10, i * 10 + 5) for i in range(20)]
    predictions = padding + [Span(500, 530)]

    assert score_source("s", predictions, labels, k=5).at(PRIMARY_IOU).matched == 0
    assert score_source("s", predictions, labels, k=25).at(PRIMARY_IOU).matched == 1


def test_mean_best_iou_separates_near_misses_from_wrong_answers():
    """The diagnostic that precision cannot express.

    Both selectors below score zero precision at 0.5. One is cutting the right moment badly
    (a boundary problem, S9); the other is looking somewhere else entirely (a targeting
    problem). Treating those the same would send the next change in the wrong direction.
    """
    labels = [Span(100, 140)]
    near = score_source("near", [Span(115, 155)], labels, k=5)
    far = score_source("far", [Span(900, 940)], labels, k=5)

    assert near.at(PRIMARY_IOU).matched == 0
    assert far.at(PRIMARY_IOU).matched == 0
    assert near.mean_best_iou > 0.3
    assert far.mean_best_iou == 0.0


def test_every_threshold_is_scored():
    score = score_source("s", [Span(0, 30)], [Span(0, 30)], k=5)
    assert set(score.thresholds) == set(IOU_THRESHOLDS)


def test_aggregate_pools_rather_than_averaging_per_source():
    """A source with eight labelled moments must count for more than one with a single moment.

    Averaging per-source rates would let one sparsely-labelled video swing the headline figure.
    """
    dense = score_source("dense", [Span(i * 100, i * 100 + 30) for i in range(8)],
                         [Span(i * 100, i * 100 + 30) for i in range(8)], k=8)
    sparse = score_source("sparse", [Span(0, 30)], [Span(500, 530)], k=8)

    aggregate = AggregateScore(label="x", k=8, sources=[dense, sparse])
    at = aggregate.at(PRIMARY_IOU)
    assert at.matched == 8
    assert at.labels == 9
    # Pooled recall is 8/9; the mean of the per-source recalls would be (1.0 + 0.0) / 2 = 0.5.
    assert at.recall == pytest.approx(8 / 9)


def test_scoring_an_empty_prediction_list_is_zero_not_an_error():
    score = score_source("s", [], [Span(0, 30)], k=5)
    at = score.at(PRIMARY_IOU)
    assert at.precision == 0.0 and at.recall == 0.0 and at.f1 == 0.0


# --------------------------------------------------------------------------- #
# Baselines                                                                    #
# --------------------------------------------------------------------------- #
def test_baselines_use_the_labelled_clip_length():
    """A baseline handicapped by guessing the wrong length would flatter the selector.

    Giving the naive methods the same target length a human chose leaves *placement* as the
    only thing being compared.
    """
    labels = [Span(0, 60), Span(200, 260)]     # median duration 60
    for prediction in baselines.uniform_baseline(600.0, labels, 5):
        assert prediction.duration == pytest.approx(60.0, abs=0.1)


def test_uniform_baseline_spreads_across_the_video():
    predictions = baselines.uniform_baseline(600.0, [Span(0, 30)], 5)
    assert len(predictions) == 5
    starts = [p.start for p in predictions]
    assert starts == sorted(starts)
    assert starts[0] < 100 and starts[-1] > 400
    for prediction in predictions:
        assert 0.0 <= prediction.start and prediction.end <= 600.0


def test_random_baseline_is_seeded():
    """An unreproducible baseline makes two runs incomparable."""
    labels = [Span(0, 30)]
    first = baselines.random_baseline(600.0, labels, 5, seed=7)
    again = baselines.random_baseline(600.0, labels, 5, seed=7)
    other = baselines.random_baseline(600.0, labels, 5, seed=8)
    assert [p.start for p in first] == [p.start for p in again]
    assert [p.start for p in first] != [p.start for p in other]


def test_longest_segment_baseline_ranks_by_duration_not_position():
    """The longest-first floor S11's scoring has to beat.

    Until S11 this was also what the shipped fallback did when it capped the count. It no
    longer is - the fallback now ranks on measured hook/pace/energy signals - so this baseline
    is the *before* picture that makes S11's change measurable rather than a description of
    current behaviour. It stays an independent implementation for exactly that reason.
    """
    segments = [Span(0, 10), Span(20, 80), Span(100, 130), Span(200, 500)]
    picked = baselines.longest_segment_baseline(segments, 2)
    assert [(p.start, p.end) for p in picked] == [(200.0, 500.0), (20.0, 80.0)]


def test_baselines_handle_a_video_shorter_than_a_clip():
    """Degenerate footage must not raise inside the harness."""
    labels = [Span(0, 60)]
    assert baselines.random_baseline(10.0, labels, 3) is not None
    assert baselines.uniform_baseline(0.0, labels, 3) == []


# --------------------------------------------------------------------------- #
# Dataset loading                                                              #
# --------------------------------------------------------------------------- #
def test_a_valid_dataset_loads(tmp_path):
    _write_labels(tmp_path / "labels", "ep1", [(10, 40), (100, 140)])
    dataset = load_dataset(tmp_path / "labels")
    assert len(dataset) == 1
    assert dataset.moment_count == 2


def test_overlapping_labels_are_rejected(tmp_path):
    """An overlap lets one returned clip match two "different" wanted moments.

    The selector would be rewarded twice for one decision, which quietly inflates precision.
    """
    path = _write_labels(tmp_path / "labels", "ep1", [(10, 60), (50, 100)])
    with pytest.raises(DatasetError, match="overlap"):
        load_label_file(path)


def test_malformed_labels_fail_loudly(tmp_path):
    directory = tmp_path / "labels"
    directory.mkdir()

    cases = {
        "backwards": {"source": "v.mp4", "moments": [{"start": 90, "end": 30}]},
        "negative": {"source": "v.mp4", "moments": [{"start": -5, "end": 30}]},
        "no_source": {"moments": [{"start": 0, "end": 30}]},
        "no_moments": {"source": "v.mp4", "moments": []},
        "not_numeric": {"source": "v.mp4", "moments": [{"start": "soon", "end": 30}]},
    }
    for name, payload in cases.items():
        path = directory / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(DatasetError):
            load_label_file(path)

    bad_json = directory / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(DatasetError):
        load_label_file(bad_json)


def test_moments_are_sorted_regardless_of_file_order(tmp_path):
    path = _write_labels(tmp_path / "labels", "ep1", [(300, 340), (10, 40)])
    source = load_label_file(path)
    assert [m.start for m in source.moments] == [10.0, 300.0]


def test_a_relative_source_resolves_against_the_label_file(tmp_path):
    """So a dataset directory can travel with its footage."""
    labels = tmp_path / "labels"
    path = _write_labels(labels, "ep1", [(0, 30)], source="media/ep1.mp4")
    source = load_label_file(path)
    assert source.source == (labels / "media" / "ep1.mp4").resolve()


def test_missing_media_is_reported_not_raised(tmp_path):
    """A dataset is often shared without its footage; labels can still be validated."""
    _write_labels(tmp_path / "labels", "ep1", [(0, 30)])
    dataset = load_dataset(tmp_path / "labels")
    assert len(dataset.missing_media()) == 1


# --------------------------------------------------------------------------- #
# Running                                                                      #
# --------------------------------------------------------------------------- #
def _dataset(tmp_path, sources):
    directory = tmp_path / "labels"
    for name, moments in sources.items():
        _write_labels(directory, name, moments, source=f"{name}.mp4")
    return load_dataset(directory)


def test_run_selector_scores_every_source(tmp_path):
    dataset = _dataset(tmp_path, {"ep1": [(10, 40)], "ep2": [(100, 140)]})

    def selector(source, duration, transcript, k):
        return [Span(10, 40)]

    score, runs = harness.run_selector(
        dataset, selector, k=5, label="test",
        duration_of=lambda _p: 600.0, transcript_of=lambda _p: None,
    )
    assert len(score.sources) == 2
    assert len(runs) == 2
    # ep1 hit, ep2 missed.
    assert score.at(PRIMARY_IOU).matched == 1


def test_a_failing_source_is_recorded_and_the_run_continues(tmp_path):
    """Nineteen usable results with one failure named beats a traceback and nothing."""
    dataset = _dataset(tmp_path, {"ep1": [(10, 40)], "ep2": [(10, 40)]})

    def selector(source, duration, transcript, k):
        if source.name == "ep1.mp4":
            raise RuntimeError("whisper exploded")
        return [Span(10, 40)]

    score, runs = harness.run_selector(
        dataset, selector, k=5, label="test",
        duration_of=lambda _p: 600.0, transcript_of=lambda _p: None,
    )
    errors = [run for run in runs if run.error]
    assert len(errors) == 1
    assert "whisper exploded" in errors[0].error
    assert score.at(PRIMARY_IOU).matched == 1        # the healthy source still scored


def test_baselines_run_over_the_same_dataset(tmp_path):
    dataset = _dataset(tmp_path, {"ep1": [(10, 40)], "ep2": [(100, 140)]})
    scores = harness.run_baselines(dataset, k=5, duration_of=lambda _p: 600.0)
    assert {score.label for score in scores} == {"baseline:uniform", "baseline:random"}
    for score in scores:
        assert score.total_labels == 2


def test_report_names_the_best_baseline_and_whether_it_was_beaten(tmp_path):
    dataset = _dataset(tmp_path, {"ep1": [(10, 40), (100, 140)]})

    def perfect(source, duration, transcript, k):
        return [Span(10, 40), Span(100, 140)]

    score, runs = harness.run_selector(
        dataset, perfect, k=5, label="selector:ai",
        duration_of=lambda _p: 600.0, transcript_of=lambda _p: None,
    )
    bases = harness.run_baselines(dataset, k=5, duration_of=lambda _p: 600.0)
    report = harness.build_report(dataset, score, bases, runs)

    assert report.beats_baseline
    assert report.best_baseline is not None
    text = render_text(report)
    assert "selector:ai" in text and "baseline:uniform" in text
    assert "beats" in text


def test_a_selector_no_better_than_guessing_is_called_out(tmp_path):
    """The failure the harness exists to make impossible to miss."""
    dataset = _dataset(tmp_path, {"ep1": [(10, 40)]})

    def useless(source, duration, transcript, k):
        return [Span(500, 530)]

    score, runs = harness.run_selector(
        dataset, useless, k=5, label="selector:ai",
        duration_of=lambda _p: 600.0, transcript_of=lambda _p: None,
    )
    bases = harness.run_baselines(dataset, k=5, duration_of=lambda _p: 600.0)
    report = harness.build_report(dataset, score, bases, runs)
    text = render_text(report)
    assert not report.beats_baseline
    assert "not selecting" in text, "a no-better-than-chance result must say so"


def test_a_report_without_baselines_says_it_cannot_be_interpreted(tmp_path):
    dataset = _dataset(tmp_path, {"ep1": [(10, 40)]})
    score, runs = harness.run_selector(
        dataset, lambda *a: [Span(10, 40)], k=5, label="selector:ai",
        duration_of=lambda _p: 600.0, transcript_of=lambda _p: None,
    )
    report = harness.build_report(dataset, score, [], runs)
    assert "cannot be interpreted" in render_text(report)


def test_report_round_trips_through_json(tmp_path):
    dataset = _dataset(tmp_path, {"ep1": [(10, 40)]})
    score, runs = harness.run_selector(
        dataset, lambda *a: [Span(10, 40)], k=5, label="selector:ai",
        duration_of=lambda _p: 600.0, transcript_of=lambda _p: None,
    )
    report = harness.build_report(dataset, score, [], runs)
    payload = json.loads(report.to_json())
    assert payload["dataset"]["sources"] == 1
    assert payload["selector"]["label"] == "selector:ai"
    assert payload["primary_iou"] == PRIMARY_IOU


def test_comparison_warns_when_the_datasets_differ(tmp_path):
    """Comparing runs over different footage is meaningless, and easy to do by accident."""
    dataset = _dataset(tmp_path, {"ep1": [(10, 40)]})
    score, runs = harness.run_selector(
        dataset, lambda *a: [Span(10, 40)], k=5, label="s",
        duration_of=lambda _p: 600.0, transcript_of=lambda _p: None,
    )
    before = harness.build_report(dataset, score, [], runs)
    after = harness.build_report(dataset, score, [], runs)
    after.moment_count = 99

    assert "WARNING" in render_comparison(before, after)
    assert "WARNING" not in render_comparison(before, before)


# --------------------------------------------------------------------------- #
# Transcript cache                                                             #
# --------------------------------------------------------------------------- #
def test_transcript_cache_round_trips(tmp_path):
    """Caching is what makes the harness usable more than once.

    Transcribing twenty long sources costs far more than selecting from them, and every §3
    change is a change to selection.
    """
    from worker.transcribe import Transcript, TranscriptSegment, Word

    media = tmp_path / "ep1.mp4"
    media.write_bytes(b"pretend video")
    transcript = Transcript(
        language="en",
        segments=[TranscriptSegment(0.0, 2.0, "hello there",
                                   words=[Word(0.0, 1.0, "hello", 0.9),
                                          Word(1.0, 2.0, "there", 0.8)])],
    )

    assert harness.save_cached_transcript(tmp_path / "cache", media, transcript)
    loaded = harness.load_cached_transcript(tmp_path / "cache", media)
    assert loaded is not None
    assert loaded.language == "en"
    assert [s.text for s in loaded.segments] == ["hello there"]
    assert [w.text for w in loaded.segments[0].words] == ["hello", "there"]


def test_changing_the_media_invalidates_the_cache(tmp_path):
    """Re-exported footage must not be scored against a stale transcript."""
    from worker.transcribe import Transcript, TranscriptSegment

    media = tmp_path / "ep1.mp4"
    media.write_bytes(b"first version")
    harness.save_cached_transcript(
        tmp_path / "cache", media,
        Transcript(language="en", segments=[TranscriptSegment(0.0, 1.0, "first")]),
    )
    assert harness.load_cached_transcript(tmp_path / "cache", media) is not None

    media.write_bytes(b"a different, longer second version")
    assert harness.load_cached_transcript(tmp_path / "cache", media) is None


def test_a_corrupt_cache_entry_is_ignored_rather_than_fatal(tmp_path):
    media = tmp_path / "ep1.mp4"
    media.write_bytes(b"video")
    cache = tmp_path / "cache"
    cache.mkdir()

    # Keyed through the shared T8 cache, which the harness now delegates to rather than
    # carrying its own copy.
    from evaluation.harness import _harness_key
    from worker import transcript_cache

    transcript_cache.cache_path(_harness_key(media), cache).write_text(
        "{ not json", encoding="utf-8"
    )
    assert harness.load_cached_transcript(cache, media) is None


def test_a_missing_source_does_not_raise_in_the_cache_lookup(tmp_path):
    assert harness.load_cached_transcript(tmp_path / "cache", tmp_path / "gone.mp4") is None
