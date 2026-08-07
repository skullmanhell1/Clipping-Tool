"""Run a selector over a labelled dataset and score it (S1).

Two things make this usable rather than a one-off script.

**Transcripts are cached.** Transcribing twenty long sources takes far longer than selecting
from them, and every §3 change is a change to *selection*. Without a cache, iterating on a
scoring tweak would mean re-running Whisper over hours of audio each time, which in practice
means the harness gets run once and then abandoned. The cache is keyed on the source's path,
size and mtime, so replacing the footage invalidates it.

**The selector is injected.** The harness never reaches for the configured LLM client itself,
so it can be tested without one, a candidate scoring change can be evaluated by passing a
different function, and comparing two selectors is the same code path as scoring one.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from evaluation import baselines
from evaluation.dataset import Dataset, LabelledSource
from evaluation.metrics import AggregateScore, score_source
from evaluation.report import Report
from worker import transcript_cache


class Selector(Protocol):
    """What the harness needs from anything it scores.

    Intentionally narrower than ``worker.selection.select_moments``: given a source, its
    duration and a transcript, return ranked clip ranges. Anything with ``start``/``end`` will
    do, so a baseline and the real selector are interchangeable here.
    """

    def __call__(self, source: Path, duration: float, transcript: object, k: int) -> Sequence: ...


@dataclass
class SourceRun:
    """What happened for one source: the transcript used and the predictions produced."""

    source: LabelledSource
    duration: float
    transcript: object | None
    predictions: Sequence
    error: str = ""
    seconds: float = 0.0


# --------------------------------------------------------------------------- #
# Transcript cache
# --------------------------------------------------------------------------- #
# Delegated to worker.transcript_cache (T8), which the pipeline now uses too. This module
# previously carried its own copy keyed on path/size/mtime, with its own JSON shape - two
# caches of the same thing, diverging on the detail that matters: a re-exported file with the
# same name and size would have been a hit here and a miss in the pipeline.
#
# The harness keeps its own directory (--cache) rather than sharing storage/transcripts,
# because a benchmark dataset is not production media and mixing them makes "clear the cache"
# ambiguous.


def _harness_key(source: Path) -> str:
    """Cache key for a benchmark source.

    Uses the same content hash and the same ASR parameters as the pipeline, read from
    settings, so a dataset transcribed by the harness and the same file transcribed by a real
    job agree - and both miss when the model changes.
    """
    from config import settings

    return transcript_cache.cache_key(
        transcript_cache.hash_source(source),
        model=settings.whisper_model,
        language=None,
        translate=False,
        beam_size=5,
    )


def load_cached_transcript(cache_dir: Path, source: Path):
    """A previously cached transcript for ``source``, or ``None``."""
    try:
        key = _harness_key(Path(source))
    except OSError:
        return None
    return transcript_cache.load(key, cache_dir)


def save_cached_transcript(cache_dir: Path, source: Path, transcript) -> Path | None:
    """Cache ``transcript`` for ``source``. Best-effort; returns the path written."""
    try:
        key = _harness_key(Path(source))
    except OSError:
        return None
    return transcript_cache.store(key, transcript, cache_dir)


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def run_selector(
    dataset: Dataset,
    selector: Selector,
    *,
    k: int,
    label: str,
    duration_of: Callable[[Path], float],
    transcript_of: Callable[[Path], object],
) -> tuple[AggregateScore, list[SourceRun]]:
    """Score ``selector`` over ``dataset``.

    ``duration_of`` and ``transcript_of`` are injected so the harness itself needs neither
    ffmpeg nor Whisper - the CLI supplies real ones, tests supply fixtures.

    A source that raises is recorded as an error and scored as zero predictions rather than
    aborting the run. Nineteen usable results with one failure named is more useful than a
    traceback and nothing, especially on a long run.
    """
    runs: list[SourceRun] = []
    scores = []

    for source in dataset.sources:
        started = time.time()
        run = SourceRun(source=source, duration=0.0, transcript=None, predictions=[])
        try:
            run.duration = float(duration_of(source.source))
            run.transcript = transcript_of(source.source)
            run.predictions = list(selector(source.source, run.duration, run.transcript, k))
        except Exception as exc:  # one bad source must not end the run
            run.error = f"{type(exc).__name__}: {exc}"
        run.seconds = time.time() - started
        runs.append(run)

        scores.append(
            score_source(
                name=source.name,
                predictions=run.predictions,
                labels=source.moments,
                k=k,
            )
        )

    return AggregateScore(label=label, k=k, sources=scores), runs


def run_baselines(
    dataset: Dataset,
    *,
    k: int,
    duration_of: Callable[[Path], float],
    segments_of: Callable[[Path, float], Sequence] | None = None,
) -> list[AggregateScore]:
    """Score the naive selectors on the same dataset.

    ``segments_of`` enables the longest-segment baseline, which needs silence detection and so
    needs the media present. It is optional because the other two baselines need only a
    duration, and a dataset shared without its footage should still produce a readable floor.
    """
    results: list[AggregateScore] = []

    for name, function in baselines.CHEAP_BASELINES.items():

        def _selector(source, duration, transcript, k_, _fn=function, _src=None):
            labels = _labels_for(dataset, source)
            return _fn(duration, labels, k_)

        score, _runs = run_selector(
            dataset,
            _selector,
            k=k,
            label=f"baseline:{name}",
            duration_of=duration_of,
            transcript_of=lambda _path: None,
        )
        results.append(score)

    if segments_of is not None:

        def _longest(source, duration, transcript, k_):
            return baselines.longest_segment_baseline(segments_of(source, duration), k_)

        score, _runs = run_selector(
            dataset,
            _longest,
            k=k,
            label="baseline:longest",
            duration_of=duration_of,
            transcript_of=lambda _path: None,
        )
        results.append(score)

    return results


def _labels_for(dataset: Dataset, source: Path) -> list:
    for entry in dataset.sources:
        if entry.source == source:
            return entry.moments
    return []


def build_report(
    dataset: Dataset,
    selector_score: AggregateScore,
    baseline_scores: Sequence[AggregateScore],
    runs: Sequence[SourceRun],
) -> Report:
    return Report(
        dataset_size=len(dataset),
        moment_count=dataset.moment_count,
        selector=selector_score,
        baselines=list(baseline_scores),
        errors=[(run.source.name, run.error) for run in runs if run.error],
        seconds=sum(run.seconds for run in runs),
    )
