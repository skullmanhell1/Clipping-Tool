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

#: Below this many measured sources, the text report labels its own comparison an anecdote.
#:
#: There is no interval and no significance test here, and a bare ``>`` on a single pair of F1
#: point estimates reads identically whether it came from one source or twenty. `preference.py`
#: already refuses to make a significance claim on a small trial and says so in the artefact
#: (``SMALL_TRIAL_COUNT``); this is the same discipline for the report whose verdict actually gets
#: quoted. Five is a judgement, not a power calculation — it is the point below which one source
#: can move pooled F1 by more than a typical selector-versus-baseline gap.
MIN_SOURCES_FOR_VERDICT = 5


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
    def sources_measured(self) -> int:
        """Sources that produced a result, i.e. the dataset minus the ones that raised.

        The harness scores an errored source as *zero predictions*, and
        ``ThresholdScore.precision``/``recall`` return ``0.0`` for "no predictions" and "no
        labels" alike — so "measured zero" and "did not measure" are the same number. The errored
        source's labels still enter ``total_labels``, so pooled recall is diluted while pooled
        precision's denominator is not: an asymmetric contamination of both headline figures.
        """
        return max(0, self.dataset_size - len(self.errors))

    @property
    def beats_baseline(self) -> bool:
        """Whether the selector beat the best baseline — ``False`` if nothing was measured.

        Without the measurement guard, a run in which *every* source raised rendered a full table
        of ``0.00`` and then asserted "a selector that does not beat a naive baseline is not
        selecting; it is sampling, and the LLM call is being paid for nothing" — a maximally strong
        quality claim derived from zero measurements, on a default exit code of 0.
        """
        if self.sources_measured == 0:
            return False
        best = self.best_baseline
        if best is None:
            return False
        return self.selector.at(PRIMARY_IOU).f1 > best.at(PRIMARY_IOU).f1

    def to_dict(self) -> dict:
        return {
            "dataset": {
                "sources": self.dataset_size,
                "labelled_moments": self.moment_count,
                # Carried explicitly so a partial run cannot be read as a complete one. This is
                # the `fidelity.Metric_Reading.available` convention applied to the aggregate.
                "sources_measured": self.sources_measured,
                "sources_failed": len(self.errors),
            },
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
    # Stated before the comparison, because it governs whether the comparison means anything. An
    # errored source is scored as zero predictions, so a run where every source failed renders a
    # table of zeroes that reads exactly like a selector performing badly.
    if report.sources_measured == 0:
        lines.append(
            f"NOTHING WAS MEASURED: all {report.dataset_size} source(s) failed. Every figure "
            "above is a zero from an absent measurement, not a score. See the errors below."
        )
        lines.append("-" * 78)
    elif report.errors:
        lines.append(
            f"PARTIAL: {report.sources_measured} of {report.dataset_size} source(s) measured; "
            f"{len(report.errors)} failed. Pooled recall counts the failed sources' labels, so "
            "it is understated relative to a complete run."
        )
        lines.append("-" * 78)
    if report.sources_measured and report.sources_measured < MIN_SOURCES_FOR_VERDICT:
        lines.append(
            f"Only {report.sources_measured} source(s) measured: below {MIN_SOURCES_FOR_VERDICT}, "
            "treat the comparison below as an anecdote. No interval is computed and a single "
            "source can move F1 by more than the gap being reported."
        )
        lines.append("-" * 78)

    best = report.best_baseline
    if report.sources_measured == 0:
        lines.append("No verdict: a comparison between two sets of zeroes is not a comparison.")
    elif best is not None:
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
