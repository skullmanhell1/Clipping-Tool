"""Scoring a selector's output against hand-labelled moments (S1).

Three decisions shape everything the harness reports, so they are stated here rather than
buried in the arithmetic.

**Overlap, not equality.** A clip that captures the moment you wanted but starts two seconds
early is a success; demanding matching timestamps would score a good selector as a failure.
Overlap is measured as intersection-over-union, and a prediction *matches* a label when their
IoU reaches a threshold.

**One prediction per label, and one label per prediction.** Without that constraint a selector
could return five near-identical clips over one good moment and score five hits - rewarding
exactly the redundancy S15 exists to remove. Matching is greedy on descending IoU, which is
deterministic and, for the small non-overlapping sets here, optimal.

**The threshold is a judgement, so results are reported at several.** At IoU 0.7 a clip must be
almost exactly the labelled range; at 0.3 it need only substantially overlap. Which is "right"
depends on whether trimming is cheap for you, and a single number would hide that a change
improved coarse targeting while worsening precise boundaries. ``IOU_THRESHOLDS`` is the sweep.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

#: The IoU thresholds every result is reported at.
#:
#: 0.3 - "found the right part of the video", tolerant of loose boundaries.
#: 0.5 - the headline figure: majority overlap, the usual convention.
#: 0.7 - "got the boundaries right too".
IOU_THRESHOLDS: tuple[float, ...] = (0.3, 0.5, 0.7)

#: The threshold used for the single headline number when one is needed.
PRIMARY_IOU = 0.5


class TimeRange(Protocol):
    """Anything with ``start`` and ``end`` - a ``ClipCandidate`` or a ``LabelledMoment``."""

    start: float
    end: float


def iou(a: TimeRange, b: TimeRange) -> float:
    """Intersection over union of two time ranges, in ``[0, 1]``.

    Zero for ranges that do not overlap, and for degenerate ranges - a zero-length prediction
    technically intersects a label at a point, and calling that a match would let a selector
    score by returning timestamps rather than clips.
    """
    a_start, a_end = float(a.start), float(a.end)
    b_start, b_end = float(b.start), float(b.end)
    if a_end <= a_start or b_end <= b_start:
        return 0.0

    intersection = min(a_end, b_end) - max(a_start, b_start)
    if intersection <= 0:
        return 0.0
    union = (a_end - a_start) + (b_end - b_start) - intersection
    if union <= 0:
        return 0.0
    return intersection / union


@dataclass(frozen=True)
class Match:
    """A matched (prediction, label) pair and the overlap that matched them."""

    prediction_index: int
    label_index: int
    iou: float


def match_predictions(
    predictions: Sequence[TimeRange],
    labels: Sequence[TimeRange],
    threshold: float = PRIMARY_IOU,
) -> list[Match]:
    """Pair predictions with labels, one-to-one, by descending overlap.

    Only pairs at or above ``threshold`` are considered. The result is sorted by prediction
    index so a report reads in the order the selector returned its clips.
    """
    pairs: list[tuple[float, int, int]] = []
    for p_index, prediction in enumerate(predictions):
        for l_index, label in enumerate(labels):
            overlap = iou(prediction, label)
            if overlap >= threshold and overlap > 0.0:
                # p_index/l_index in the sort key make ties resolve by position rather than
                # by dict order, so a rerun cannot reshuffle equally good matches.
                pairs.append((overlap, p_index, l_index))

    pairs.sort(key=lambda item: (-item[0], item[1], item[2]))

    used_predictions: set[int] = set()
    used_labels: set[int] = set()
    matches: list[Match] = []
    for overlap, p_index, l_index in pairs:
        if p_index in used_predictions or l_index in used_labels:
            continue
        used_predictions.add(p_index)
        used_labels.add(l_index)
        matches.append(Match(prediction_index=p_index, label_index=l_index, iou=overlap))

    matches.sort(key=lambda match: match.prediction_index)
    return matches


@dataclass
class ThresholdScore:
    """Precision and recall at one IoU threshold."""

    threshold: float
    matched: int
    predictions: int
    labels: int

    @property
    def precision(self) -> float:
        """Of the clips returned, the fraction that hit a wanted moment.

        This is what a user experiences as "how much of this output is worth keeping".
        """
        return self.matched / self.predictions if self.predictions else 0.0

    @property
    def recall(self) -> float:
        """Of the wanted moments, the fraction found.

        The complement of precision, and the one that exposes a selector returning few but
        safe clips: perfect precision over one clip while missing nine moments is not success.
        """
        return self.matched / self.labels if self.labels else 0.0

    @property
    def f1(self) -> float:
        if not (self.precision and self.recall):
            return 0.0
        return 2 * self.precision * self.recall / (self.precision + self.recall)

    def to_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "matched": self.matched,
            "predictions": self.predictions,
            "labels": self.labels,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


@dataclass
class SourceScore:
    """How a selector did on one source."""

    name: str
    k: int
    predictions: int
    labels: int
    thresholds: dict[float, ThresholdScore] = field(default_factory=dict)
    #: Best overlap achieved for each labelled moment, in label order, regardless of
    #: threshold. This is the diagnostic channel: a selector that consistently lands 0.45 is
    #: nearly right and needs boundary work, while one at 0.05 is looking in the wrong place.
    #: Precision alone reports both as zero.
    best_iou_per_label: list[float] = field(default_factory=list)

    @property
    def mean_best_iou(self) -> float:
        if not self.best_iou_per_label:
            return 0.0
        return sum(self.best_iou_per_label) / len(self.best_iou_per_label)

    def at(self, threshold: float = PRIMARY_IOU) -> ThresholdScore:
        return self.thresholds[threshold]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "k": self.k,
            "predictions": self.predictions,
            "labels": self.labels,
            "mean_best_iou": round(self.mean_best_iou, 4),
            "thresholds": [score.to_dict() for score in self.thresholds.values()],
        }


def score_source(
    name: str,
    predictions: Sequence[TimeRange],
    labels: Sequence[TimeRange],
    k: int,
    thresholds: Iterable[float] = IOU_THRESHOLDS,
) -> SourceScore:
    """Score one source's predictions against its labels.

    ``k`` truncates the predictions first: precision@k asks how good the *top* k are, which is
    the question that matters, because a user looks at the first handful. A selector that
    returns thirty clips to be sure of covering five moments has not solved the problem.
    """
    top = list(predictions)[: max(0, k)]
    score = SourceScore(name=name, k=k, predictions=len(top), labels=len(labels))

    for threshold in thresholds:
        matches = match_predictions(top, labels, threshold)
        score.thresholds[threshold] = ThresholdScore(
            threshold=threshold,
            matched=len(matches),
            predictions=len(top),
            labels=len(labels),
        )

    score.best_iou_per_label = [
        max((iou(prediction, label) for prediction in top), default=0.0) for label in labels
    ]
    return score


@dataclass
class AggregateScore:
    """Scores across a whole dataset."""

    label: str
    k: int
    sources: list[SourceScore] = field(default_factory=list)

    @property
    def total_predictions(self) -> int:
        return sum(source.predictions for source in self.sources)

    @property
    def total_labels(self) -> int:
        return sum(source.labels for source in self.sources)

    def at(self, threshold: float = PRIMARY_IOU) -> ThresholdScore:
        """Dataset-wide precision/recall at ``threshold``.

        Pooled over all sources rather than averaged per source, so a source with eight
        labelled moments counts for more than one with a single moment. Averaging the
        per-source rates would let one sparsely-labelled video swing the headline figure.
        """
        return ThresholdScore(
            threshold=threshold,
            matched=sum(source.at(threshold).matched for source in self.sources),
            predictions=self.total_predictions,
            labels=self.total_labels,
        )

    @property
    def mean_best_iou(self) -> float:
        overlaps = [value for source in self.sources for value in source.best_iou_per_label]
        return sum(overlaps) / len(overlaps) if overlaps else 0.0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "k": self.k,
            "sources": [source.to_dict() for source in self.sources],
            "aggregate": {
                "mean_best_iou": round(self.mean_best_iou, 4),
                "thresholds": [self.at(threshold).to_dict() for threshold in IOU_THRESHOLDS],
            },
        }
