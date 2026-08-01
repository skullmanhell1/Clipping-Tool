"""Naive selectors to measure the real one against (S1).

A precision figure on its own cannot be read. Is precision@5 of 0.40 good? It depends entirely
on what picking clips *without thinking* would score on the same footage, and that number is
not guessable: it moves with how long the sources are, how many moments were labelled, and how
long those moments are. On a 3-minute video with 5 labelled moments, evenly spaced guesses
score well; on a 3-hour podcast with 5, they score near zero.

So every run reports baselines alongside the selector:

* :func:`uniform_baseline` — evenly spaced clips. The "no information at all" floor, and the
  one that exposes short or densely-labelled sources where any pick lands on something.
* :func:`longest_segment_baseline` — the longest spans between silences. This is what the
  shipped deterministic fallback actually does when it has to cap the count
  (``segmentation.py``: "keep the longest segments"), so it is both a baseline and a measure
  of a code path in production today.
* :func:`random_baseline` — seeded random placement. Averaged over several draws it gives the
  chance level directly.

The one that matters is *beating uniform*. A selector that does not is not selecting; it is
sampling, and the LLM call is being paid for nothing.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Prediction:
    """A predicted clip range. Structurally compatible with ``ClipCandidate``."""

    start: float
    end: float
    score: float = 0.0
    reason: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


def _clip_length(labels: Sequence, fallback: float = 30.0) -> float:
    """A representative clip length, taken from the labels themselves.

    Deliberately not a constant: a baseline handicapped by guessing 30 s against 60 s labels
    would flatter the selector, and the point of a baseline is to be hard to beat by accident.
    Using the median labelled duration gives the naive methods the same target length a human
    chose, so the only thing being compared is *where* the clips are placed.
    """
    durations = sorted(float(label.end) - float(label.start) for label in labels)
    if not durations:
        return fallback
    middle = len(durations) // 2
    if len(durations) % 2:
        return durations[middle]
    return (durations[middle - 1] + durations[middle]) / 2.0


def uniform_baseline(duration: float, labels: Sequence, k: int) -> list[Prediction]:
    """``k`` evenly spaced clips of the median labelled length.

    Placed at the centre of each of ``k`` equal slices rather than back-to-back from zero, so
    the coverage is spread across the video instead of clustered at the start.
    """
    length = _clip_length(labels)
    if duration <= 0 or k <= 0:
        return []

    out: list[Prediction] = []
    slice_width = duration / k
    for index in range(k):
        centre = slice_width * (index + 0.5)
        start = max(0.0, min(centre - length / 2.0, duration - length))
        start = max(0.0, start)
        end = min(duration, start + length)
        if end > start:
            out.append(Prediction(start=round(start, 3), end=round(end, 3),
                                  reason="uniform baseline"))
    return out


def random_baseline(
    duration: float, labels: Sequence, k: int, *, seed: int = 0
) -> list[Prediction]:
    """``k`` randomly placed clips of the median labelled length.

    Seeded, because an unreproducible baseline makes two runs incomparable - the whole point
    of the harness is that a change in the number means a change in the selector.
    """
    length = _clip_length(labels)
    if duration <= length or k <= 0:
        return uniform_baseline(duration, labels, k)

    rng = random.Random(seed)  # noqa: S311 - a reproducible baseline sampler; the fixed seed is the requirement
    starts = sorted(rng.uniform(0.0, duration - length) for _ in range(k))
    return [
        Prediction(start=round(start, 3), end=round(start + length, 3),
                   reason="random baseline")
        for start in starts
    ]


def longest_segment_baseline(
    segments: Sequence, k: int, *, min_duration: float = 0.0
) -> list[Prediction]:
    """The ``k`` longest ``segments``, longest first.

    ``segments`` are (start, end)-bearing spans - in practice the output of
    ``worker.segmentation``.

    This *used* to be what the shipped fallback did when it capped the count, which made it the
    most interesting of the three baselines. S11 replaced that rule with measured scoring, so it
    is now a historical floor rather than a mirror of production: it answers "is the new scoring
    actually better than picking the longest segments?", which is the question S11 needs
    answered and cannot answer about itself.

    Deliberately still an independent implementation, importing nothing from ``worker``. A
    baseline that shared code with the thing it measures would move whenever production moved
    and could never report a regression.
    """
    ranked = sorted(
        (
            Prediction(start=float(s.start), end=float(s.end),
                       reason="longest-segment baseline")
            for s in segments
            if float(s.end) - float(s.start) > min_duration
        ),
        key=lambda prediction: (-prediction.duration, prediction.start),
    )
    return ranked[: max(0, k)]


#: Baselines that need only the source duration and the labels, so they can run on any dataset
#: whether or not the footage or a transcript is present.
CHEAP_BASELINES = {
    "uniform": uniform_baseline,
    "random": random_baseline,
}
