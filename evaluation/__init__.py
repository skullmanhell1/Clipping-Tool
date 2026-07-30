"""Selection evaluation harness (S1).

The improvement plan puts this first in §3 and says why: *"Without this every change below is
unmeasurable."* Clip selection is the product's central claim, and its current implementation
shows an LLM nothing but ``[i] start-end: text`` lines while the deterministic fallback keeps
the longest segments — a heuristic its own docstring admits is standing in for real scoring.
Every proposed improvement (audio energy, pitch, speech rate, hook scoring, scene awareness)
is a change to a ranking, and a ranking cannot be improved by inspection.

The harness answers one question: **given sources with the moments you would actually post
marked by hand, how often does the selector find them?**

Layout:

* :mod:`evaluation.dataset` — the label format, loading and validation.
* :mod:`evaluation.metrics` — IoU, matching, precision@k / recall@k.
* :mod:`evaluation.baselines` — naive selectors to measure against.
* :mod:`evaluation.harness` — runs a selector over a dataset and scores it.
* :mod:`evaluation.report` — human-readable and machine-readable output.

``scripts/eval_selection.py`` is the CLI.
"""

from evaluation.dataset import Dataset, LabelledMoment, LabelledSource, load_dataset
from evaluation.metrics import SourceScore, iou, match_predictions, score_source
from evaluation.report import Report, render_text

__all__ = [
    "Dataset",
    "LabelledMoment",
    "LabelledSource",
    "Report",
    "SourceScore",
    "iou",
    "load_dataset",
    "match_predictions",
    "render_text",
    "score_source",
]
