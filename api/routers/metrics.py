"""``GET /metrics`` - Prometheus exposition (Phase 7).

``worker/observability.py`` records per-stage timings and attributes log lines to a job, and then
stops: the numbers reach a log line and ``GET /api/jobs/{id}/timings``, both of which answer a
question about *one* job. Nothing aggregated across jobs, so "is p95 render time regressing?",
"has the failure rate moved since the last release?" and "how often are fonts being substituted?"
had no answer short of reading every job record by hand.

**Behind the Phase 1 auth, like everything else.** The route is declared on a normal router, so
the app-level ``dependencies=[Depends(require_api_token)]`` in ``api/main.py`` covers it with no
work here - and it is deliberately *not* added to ``api.security._EXEMPT_PATHS`` or
``_QUERY_TOKEN_PATHS``. Exempting it would be the easy thing and it would publish the job volume,
failure rate and token spend of the deployment to anyone who found the port. Prometheus can send
an ``Authorization`` header (``authorization`` in a scrape config's ``basic_auth``/
``bearer_token``), so there is no reason to widen the query-token allowance that exists only
because a browser cannot put a header on a ``<video src>``.

**Hand-written exposition, no ``prometheus_client``.** The brief asks for no new heavy dependency
and the format is a few lines of text. The parts that are easy to get wrong are covered by tests
rather than by trusting a library: label escaping, cumulative buckets, and the ``+Inf`` bucket.

**Counters come from the process, gauges from the store.** That split is the substantive decision
here and it is not stylistic. See ``worker/metrics.py``: a counter derived from ``JobStore`` would
decrease when the store is pruned to ``max_persisted_jobs``, Prometheus reads a decreasing counter
as a restart, and ``rate()`` would then report a burst of traffic that never happened.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from worker import llm_cost, metrics
from worker.jobs import get_manager
from worker.models import ACTIVE_JOB_STATUSES, JobStatus

logger = logging.getLogger(__name__)

router = APIRouter()

#: The exposition format version this renders. Sent in the Content-Type because Prometheus uses it
#: to choose a parser; omitting it works today and is one negotiation change away from not.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: Descriptions, kept beside the names so a ``# HELP`` line cannot drift from what it describes.
_HELP: dict[str, str] = {
    metrics.STAGE_DURATION: "Time spent in each pipeline stage.",
    metrics.JOBS_FINISHED: "Jobs that reached a terminal state, by status.",
    metrics.CLIP_DEGRADATIONS: "Effect markers recorded on rendered clips, by marker name.",
    metrics.CLIPS_RENDERED: "Clips written.",
    metrics.LLM_CALLS: "LLM requests made, by model.",
    metrics.LLM_TOKENS: "LLM tokens consumed, by model and prompt/completion.",
    metrics.LLM_COST: "LLM spend in USD, by model. Absent when no price is configured.",
    metrics.LLM_UNMETERED: "LLM calls whose response carried no readable token usage, by model.",
    metrics.WEBHOOKS: "Job webhook delivery attempts, by outcome.",
}

_JOBS_GAUGE = "clipping_jobs"
_ACTIVE_GAUGE = "clipping_jobs_active"
_QUEUE_GAUGE = "clipping_jobs_queued"
_TRACKED_GAUGE = "clipping_jobs_tracked"


def escape_label_value(value: str) -> str:
    """Escape a label value for the exposition format.

    Required, and the failure mode is total rather than partial: a stray ``"`` produces a line
    Prometheus cannot parse, and it rejects the **whole scrape**, so one bad label value loses
    every metric in it. The three escapes the format specifies are backslash, double quote and
    newline - backslash first, or it would double-escape the ones added after it.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: tuple[tuple[str, str], ...], extra: tuple[tuple[str, str], ...] = ()) -> str:
    """Render ``{a="1",b="2"}``, or an empty string when there are no labels."""
    combined = tuple(pairs) + tuple(extra)
    if not combined:
        return ""
    inner = ",".join(f'{name}="{escape_label_value(value)}"' for name, value in combined)
    return "{" + inner + "}"


def _format_number(value: float) -> str:
    """Render a value the way the format expects.

    Integral floats are written without a trailing ``.0``: Prometheus parses either, but a counter
    rendered as ``3`` rather than ``3.0`` is what every other exporter emits, and matching that
    keeps a hand-comparison against a real one honest.
    """
    if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf
        # Neither is meaningful for a counter or a gauge here, and emitting one would make the
        # series unusable rather than merely wrong. Report zero and let the log carry the fault.
        logger.warning("refusing to expose a non-finite metric value")
        return "0"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _emit_gauge(
    lines: list[str], name: str, help_text: str, series: list[tuple[str, float]]
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    for labels, value in series:
        lines.append(f"{name}{labels} {_format_number(value)}")


def render() -> str:
    """The complete exposition text.

    Assembled as a list of lines and joined once, and every metric family emits its ``# HELP`` and
    ``# TYPE`` immediately before its samples, because the format requires a family's samples to be
    contiguous - interleaving two families is a parse error, not a cosmetic problem.
    """
    lines: list[str] = []

    # ---- gauges, read live from the store ----------------------------------------------
    # A gauge is the honest type for these: they describe the store's current contents, which
    # legitimately goes down. See the module docstring for why the counters are not derived here.
    try:
        jobs = get_manager().store.all()
    except Exception:  # pragma: no cover - defensive; a scrape must not 500 the app
        logger.warning("could not read the job store for /metrics", exc_info=True)
        jobs = []

    counts = {status.value: 0 for status in JobStatus}
    for job in jobs:
        value = getattr(job.status, "value", str(job.status))
        counts[value] = counts.get(value, 0) + 1

    _emit_gauge(
        lines,
        _JOBS_GAUGE,
        "Jobs currently in the store, by status. A rolling window, not a lifetime total.",
        [
            (_labels((("status", status),)), float(count))
            for status, count in sorted(counts.items())
        ],
    )
    active = sum(1 for job in jobs if job.status in ACTIVE_JOB_STATUSES)
    _emit_gauge(
        lines,
        _ACTIVE_GAUGE,
        "Jobs holding or waiting for the worker (queued, processing or cancelling).",
        [("", float(active))],
    )
    _emit_gauge(
        lines,
        _QUEUE_GAUGE,
        "Jobs waiting for the worker to start them.",
        [("", float(counts.get(JobStatus.QUEUED.value, 0)))],
    )
    _emit_gauge(
        lines,
        _TRACKED_GAUGE,
        "Jobs retained in the store, bounded by MAX_PERSISTED_JOBS.",
        [("", float(len(jobs)))],
    )

    # A gauge rather than a counter deliberately: this is the size of a bounded in-process
    # registry, and it falls when the oldest entry is evicted.
    try:
        tracked_usage = len(llm_cost.snapshot())
    except Exception:  # pragma: no cover - defensive
        tracked_usage = 0
    _emit_gauge(
        lines,
        "clipping_llm_usage_tracked_jobs",
        "Jobs with LLM usage held in the in-process registry.",
        [("", float(tracked_usage))],
    )

    # ---- counters ---------------------------------------------------------------------
    for name, series in sorted(metrics.counters().items()):
        lines.append(f"# HELP {name} {_HELP.get(name, name)}")
        lines.append(f"# TYPE {name} counter")
        for key, value in sorted(series.items()):
            lines.append(f"{name}{_labels(key)} {_format_number(value)}")

    # ---- histograms -------------------------------------------------------------------
    for name, buckets in sorted(metrics.histograms().items()):
        lines.append(f"# HELP {name} {_HELP.get(name, name)}")
        lines.append(f"# TYPE {name} histogram")
        for key, histogram in sorted(buckets.items()):
            for edge, count in histogram.cumulative():
                lines.append(
                    f"{name}_bucket{_labels(key, (('le', _format_number(edge)),))} {count}"
                )
            # `+Inf` carries the total, which is how an observation above the last edge is
            # represented - it is in no finite bucket but must still be counted.
            lines.append(f"{name}_bucket{_labels(key, (('le', '+Inf'),))} {histogram.total}")
            lines.append(f"{name}_sum{_labels(key)} {_format_number(histogram.sum)}")
            lines.append(f"{name}_count{_labels(key)} {histogram.total}")

    # A trailing newline: the format specifies the body ends with one, and some parsers treat a
    # final line without it as truncated.
    return "\n".join(lines) + "\n"


@router.get("/metrics", tags=["system"], response_class=PlainTextResponse)
def prometheus_metrics() -> PlainTextResponse:
    """Prometheus exposition for this instance.

    Authenticated by the app-level dependency, like every other route. Not rate limited: a scrape
    is one cheap request on a fixed interval, and throttling it would drop samples and leave gaps
    that look like the app was down.
    """
    return PlainTextResponse(content=render(), media_type=CONTENT_TYPE)
