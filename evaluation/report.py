"""Rendering evaluation results (S1).

Two audiences, so two formats. The text report is for the person deciding whether a change
helped, and its job is to make the comparison against the baselines unavoidable - a precision
figure printed on its own invites the reader to judge it against a number they have imagined.
The JSON is for tracking the same measurements across releases.

The text report deliberately leads with the *delta* against the strongest baseline, because
that is the only figure that answers "did this change do anything".
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from evaluation.metrics import IOU_THRESHOLDS, PRIMARY_IOU, AggregateScore


@dataclass
class Report:
    """A complete evaluation result."""

    dataset_size: int
    moment_count: int
    selector: AggregateScore
    baselines: list[AggregateScore] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def best_baseline(self) -> AggregateScore | None:
        """The baseline that scored highest at the primary threshold.

        Compared against the *best* baseline rather than the average or the weakest, because
        beating a deliberately weak floor is not evidence of anything.
        """
        if not self.baselines:
            return None
        return max(self.baselines, key=lambda score: score.at(PRIMARY_IOU).f1)

    @property
    def beats_baseline(self) -> bool:
        best = self.best_baseline
        if best is None:
            return False
        return self.selector.at(PRIMARY_IOU).f1 > best.at(PRIMARY_IOU).f1

    def to_dict(self) -> dict:
        return {
            "dataset": {"sources": self.dataset_size, "labelled_moments": self.moment_count},
            "k": self.selector.k,
            "primary_iou": PRIMARY_IOU,
            "selector": self.selector.to_dict(),
            "baselines": [score.to_dict() for score in self.baselines],
            "beats_best_baseline": self.beats_baseline,
            "errors": [{"source": name, "error": message} for name, message in self.errors],
            "seconds": round(self.seconds, 1),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _row(label: str, score: AggregateScore) -> str:
    cells = [f"{label:<22}"]
    for threshold in IOU_THRESHOLDS:
        at = score.at(threshold)
        cells.append(f"{at.precision:>6.2f} {at.recall:>6.2f}")
    cells.append(f"{score.mean_best_iou:>11.2f}")
    return "".join(cells)


def render_text(report: Report) -> str:
    """A readable summary, leading with the comparison that matters."""
    lines: list[str] = []
    selector = report.selector

    lines.append("")
    lines.append(
        f"Selection evaluation  ·  k={selector.k}  ·  "
        f"{report.dataset_size} sources, {report.moment_count} labelled moments"
    )
    lines.append("=" * 78)

    # --- the table ---------------------------------------------------------
    header = f"{'':<22}"
    for threshold in IOU_THRESHOLDS:
        header += f"{'IoU ' + format(threshold, '.1f'):>13}"
    header += f"{'mean best':>11}"
    lines.append(header)
    lines.append(
        f"{'':<22}" + "".join(f"{'prec':>7}{'rec':>6}" for _ in IOU_THRESHOLDS) + f"{'IoU':>11}"
    )
    lines.append("-" * 78)
    lines.append(_row(selector.label, selector))
    for baseline in report.baselines:
        lines.append(_row(baseline.label, baseline))
    lines.append("-" * 78)

    # --- the verdict -------------------------------------------------------
    best = report.best_baseline
    if best is not None:
        mine = selector.at(PRIMARY_IOU)
        theirs = best.at(PRIMARY_IOU)
        delta = mine.f1 - theirs.f1
        verdict = "beats" if delta > 0 else ("ties" if delta == 0 else "LOSES TO")
        lines.append(
            f"At IoU {PRIMARY_IOU}: F1 {mine.f1:.2f} vs best baseline "
            f"({best.label}) {theirs.f1:.2f}  ->  selector {verdict} it "
            f"({delta:+.2f})"
        )
        if delta <= 0:
            lines.append(
                "  A selector that does not beat a naive baseline is not selecting; it is "
                "sampling, and the LLM call is being paid for nothing."
            )
    else:
        lines.append(
            "No baselines were run, so these figures cannot be interpreted: whether "
            "precision 0.40 is good depends entirely on what guessing scores on this footage."
        )

    # --- per source --------------------------------------------------------
    lines.append("")
    lines.append(f"{'source':<34}{'clips':>6}{'moments':>9}{'hit@0.5':>9}{'meanIoU':>9}")
    lines.append("-" * 78)
    for source in selector.sources:
        at = source.at(PRIMARY_IOU)
        lines.append(
            f"{source.name[:33]:<34}{source.predictions:>6}{source.labels:>9}"
            f"{at.matched:>9}{source.mean_best_iou:>9.2f}"
        )

    # --- diagnostics -------------------------------------------------------
    near_misses = [
        source.name
        for source in selector.sources
        if source.at(PRIMARY_IOU).matched == 0 and source.mean_best_iou >= 0.25
    ]
    if near_misses:
        lines.append("")
        lines.append(
            "Near misses (nothing matched at 0.5, but mean best IoU >= 0.25): "
            + ", ".join(near_misses)
        )
        lines.append(
            "  These are boundary problems, not targeting problems - the selector is finding "
            "the right part of the video and cutting it wrong. Look at S9 (scene changes) and "
            "sentence snapping before touching the scoring signals."
        )

    if report.errors:
        lines.append("")
        lines.append("Errors:")
        for name, message in report.errors:
            lines.append(f"  {name}: {message}")

    lines.append("")
    lines.append(f"({report.seconds:.1f}s)")
    return "\n".join(lines)


def render_comparison(before: Report, after: Report) -> str:
    """The change between two runs, for judging whether a §3 item helped."""
    lines = ["", "Selection evaluation: change", "=" * 78]
    for threshold in IOU_THRESHOLDS:
        b = before.selector.at(threshold)
        a = after.selector.at(threshold)
        lines.append(
            f"IoU {threshold:.1f}   precision {b.precision:.2f} -> {a.precision:.2f} "
            f"({a.precision - b.precision:+.2f})   "
            f"recall {b.recall:.2f} -> {a.recall:.2f} ({a.recall - b.recall:+.2f})"
        )
    lines.append(
        f"mean best IoU {before.selector.mean_best_iou:.2f} -> "
        f"{after.selector.mean_best_iou:.2f} "
        f"({after.selector.mean_best_iou - before.selector.mean_best_iou:+.2f})"
    )
    if before.moment_count != after.moment_count or before.dataset_size != after.dataset_size:
        lines.append(
            "  WARNING: the datasets differ "
            f"({before.dataset_size}/{before.moment_count} vs "
            f"{after.dataset_size}/{after.moment_count} sources/moments), so these numbers "
            "are not comparable."
        )
    return "\n".join(lines)


def sequence_summary(scores: Sequence[AggregateScore]) -> str:
    """One line per score, for a quick multi-selector comparison."""
    return "\n".join(
        f"{score.label:<24} F1@{PRIMARY_IOU} {score.at(PRIMARY_IOU).f1:.3f}" for score in scores
    )
