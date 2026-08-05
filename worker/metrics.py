"""Process-lifetime metric aggregation, for the Prometheus ``/metrics`` endpoint (Phase 7).

``worker/observability.py`` is good at what it does - contextvars job attribution plus per-stage
timings - but it terminates at log lines and one job's ``/timings`` response. Nothing aggregated
across jobs, so "is p95 render time regressing?" and "has the failure rate moved?" were
unanswerable. This is the missing accumulator.

Stdlib only, and deliberately no ``config`` import, so it can be fed from
``worker/observability.py`` without giving that module a settings dependency it does not have.

**Why this exists rather than deriving everything from the job store.** Reading
``store.all()`` at scrape time is enough for a *gauge* - how many jobs are queued right now - and
is wrong for a *counter*. ``JobStore`` is a rolling window pruned to ``max_persisted_jobs``
(default 500), so "jobs completed" computed from it **goes down** when pruning happens.
Prometheus reads a counter that decreases as a process restart and assumes it resumed from zero,
so a prune would be interpreted as a reset and ``rate()`` would invent a burst of traffic that
never occurred. The number would look plausible and be fabricated.

So: events are counted here, monotonically, for the life of the process. A real restart *is* a
reset, which Prometheus detects and handles correctly. Current state is read live from the store
at scrape time, where a gauge is the honest type.

**Cardinality is bounded on purpose.** Every label value here comes from a fixed vocabulary - job
statuses, stage names, degradation-marker prefixes - but "fixed" is a property of today's code,
and a metrics endpoint that grows a new time series per clip keyword would degrade the monitoring
system rather than the app. ``_MAX_LABEL_VALUES`` caps each metric and folds the rest into
``other``, so the failure mode is a lost distinction instead of an unbounded scrape.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping

#: Upper bound on distinct label combinations per metric name. Anything beyond it is folded into
#: ``other``, which keeps a scrape bounded even if a marker vocabulary grows unexpectedly.
_MAX_LABEL_VALUES = 64

#: Bucket boundaries for stage durations, in seconds.
#:
#: Chosen against what this pipeline actually does rather than from a template. Measured stage
#: times in this repo span microseconds (metadata generation with no LLM configured returns a
#: fallback immediately) to minutes (transcription, and the three re-encodes per clip), so the
#: buckets are spread logarithmically across that whole range. Ten buckets is enough for
#: `histogram_quantile` to place a p95 usefully and few enough that the scrape stays small -
#: every bucket is a separate time series per stage.
STAGE_DURATION_BUCKETS: tuple[float, ...] = (
    0.05,
    0.25,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    180.0,
    600.0,
)


class Histogram:
    """A cumulative histogram over one label set.

    Hand-rolled rather than pulled from ``prometheus_client``, because the brief asks for no new
    heavy dependency and this is the only shape needed. Counts are *per bucket* here and made
    cumulative at render time - storing them cumulatively would mean touching every bucket above
    the observation on each record, which is the same work done more often.
    """

    __slots__ = ("buckets", "counts", "total", "sum")

    def __init__(self, buckets: tuple[float, ...] = STAGE_DURATION_BUCKETS) -> None:
        self.buckets = buckets
        self.counts = [0] * len(buckets)
        self.total = 0
        self.sum = 0.0

    def observe(self, value: float) -> None:
        # Negatives are clamped rather than dropped: a clock adjustment mid-stage can produce
        # one, and losing the observation would understate the count as well as the time.
        value = max(0.0, float(value))
        self.total += 1
        self.sum += value
        for index, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[index] += 1
                return
        # Above the last edge: counted in `total`, and therefore in `+Inf`, but in no bucket.

    def cumulative(self) -> list[tuple[float, int]]:
        """``(le, count)`` pairs with counts accumulated, as the exposition format requires."""
        running = 0
        out: list[tuple[float, int]] = []
        for edge, count in zip(self.buckets, self.counts, strict=True):
            running += count
            out.append((edge, running))
        return out


def _key(labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """A hashable, order-independent label key.

    Sorted so ``{"a": "1", "b": "2"}`` and ``{"b": "2", "a": "1"}`` are one series rather than
    two - otherwise the same event counted from two call sites written in different orders would
    silently split in half.
    """
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


class _Registry:
    """Counters and histograms for the life of the process.

    One lock for everything. The alternative - a lock per metric - buys nothing here: recording
    is a dict lookup and an addition, contention is between one worker thread and an occasional
    scrape, and a single lock cannot deadlock against itself.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._histograms: dict[str, dict[tuple[tuple[str, str], ...], Histogram]] = {}

    def increment(
        self, name: str, labels: Mapping[str, str] | None = None, by: float = 1.0
    ) -> None:
        if by < 0:
            # A counter must never decrease. Refusing is better than accepting and producing a
            # series Prometheus will read as a restart.
            return
        with self._lock:
            series = self._counters.setdefault(name, {})
            key = self._bounded_key(series, _key(labels))
            series[key] = series.get(key, 0.0) + float(by)

    def observe(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
        buckets: tuple[float, ...] = STAGE_DURATION_BUCKETS,
    ) -> None:
        with self._lock:
            series = self._histograms.setdefault(name, {})
            key = self._bounded_key(series, _key(labels))
            histogram = series.get(key)
            if histogram is None:
                histogram = Histogram(buckets)
                series[key] = histogram
            histogram.observe(value)

    @staticmethod
    def _bounded_key(
        series: Mapping[tuple[tuple[str, str], ...], object],
        key: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[str, str], ...]:
        """``key``, or an ``other`` key once the series is at its cap.

        Folding rather than dropping: losing the distinction between rare label values is
        recoverable by reading the logs, whereas dropping the observation entirely would make the
        totals wrong, and a wrong total is the thing that gets acted on.
        """
        if key in series or len(series) < _MAX_LABEL_VALUES:
            return key
        return (("overflow", "other"),)

    def counters(self) -> dict[str, dict[tuple[tuple[str, str], ...], float]]:
        with self._lock:
            return {name: dict(series) for name, series in self._counters.items()}

    def histograms(self) -> dict[str, dict[tuple[tuple[str, str], ...], Histogram]]:
        with self._lock:
            # The Histogram objects are shared rather than copied: rendering reads `sum`,
            # `total` and `counts`, and a scrape that catches a mid-record write reports one
            # observation late. Copying them under the lock would be exact and would also mean
            # allocating a list per series per scrape for a discrepancy nobody can observe.
            return {name: dict(series) for name, series in self._histograms.items()}

    def reset(self) -> None:
        """Forget everything. For tests - a process-global accumulator otherwise leaks between
        them, and a metric asserted after another test contributed to it fails for the wrong
        reason."""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


_registry = _Registry()


# --------------------------------------------------------------------------- #
# Metric names, in one place
# --------------------------------------------------------------------------- #
#: Stage durations. `_seconds` suffix because the exposition format's convention is base units,
#: and a dashboard that has to know whether a number is seconds or milliseconds is one that will
#: eventually be read wrong.
STAGE_DURATION = "clipping_stage_duration_seconds"
JOBS_FINISHED = "clipping_jobs_finished_total"
CLIP_DEGRADATIONS = "clipping_clip_degradations_total"
CLIPS_RENDERED = "clipping_clips_rendered_total"
LLM_CALLS = "clipping_llm_calls_total"
LLM_TOKENS = "clipping_llm_tokens_total"
LLM_COST = "clipping_llm_cost_usd_total"
#: Calls whose response carried no readable usage. Exposed separately for the same reason
#: ``Model_Usage.unmetered_calls`` exists: without it a dashboard showing "4000 calls, 0 tokens"
#: reads as "the models are free" rather than "the token counts could not be read".
LLM_UNMETERED = "clipping_llm_unmetered_calls_total"
WEBHOOKS = "clipping_webhook_deliveries_total"


# --------------------------------------------------------------------------- #
# Recording helpers - named so a call site reads as what happened
# --------------------------------------------------------------------------- #
def observe_stage(stage: str, seconds: float) -> None:
    """Record one completed stage.

    Fed from ``Job_Metrics.record``, which is where *every* pipeline stage timing already lands -
    both the ``progress`` callback's transitions and the ``observability.stage()`` context
    manager. Hooking there rather than at each call site means a stage cannot be added without
    appearing here, which is the same property M5 relies on.
    """
    _registry.observe(STAGE_DURATION, seconds, {"stage": str(stage or "unknown")})


def count_job_finished(status: str) -> None:
    """Record a job reaching a terminal state."""
    _registry.increment(JOBS_FINISHED, {"status": str(status or "unknown")})


def count_clips_rendered(count: int) -> None:
    if count > 0:
        _registry.increment(CLIPS_RENDERED, by=float(count))


def marker_name(marker: str) -> str:
    """The label value for a degradation marker: everything before the first ``:``.

    The markers are ``name`` or ``name:detail`` - ``music_degraded:synthesised``,
    ``font_substituted:<face>``, ``encoder_unavailable:<name>``, ``broll:<keyword>``. The detail
    is dropped because ``broll:<keyword>`` is unbounded by construction: keeping it would add a
    time series per keyword any clip ever matched, which is how a metrics endpoint becomes the
    thing that breaks the monitoring system. The prefix is what a dashboard alerts on - "are
    fonts being substituted" - and the specific face is in the clip record and the logs.
    """
    return str(marker or "").split(":", 1)[0].strip() or "unknown"


def count_degradations(markers: Iterable[str]) -> None:
    """Record every marker on one clip.

    Counts *all* markers, not only the ones whose name suggests failure. ``captions`` and
    ``music_degraded:synthesised`` are the same kind of fact - what the render did - and deciding
    here which are interesting would bake today's opinion into the data. A query can filter;
    a metric that was never recorded cannot be recovered.
    """
    for marker in markers or ():
        _registry.increment(CLIP_DEGRADATIONS, {"marker": marker_name(marker)})


def count_llm_call(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    *,
    unmetered: bool = False,
) -> None:
    """Record one LLM request and its tokens.

    Counted per call rather than totalled at the end of a job, because tokens are spent whether
    or not the job finishes - a run that fails after selection still paid for selection, and
    attributing spend only at a terminal transition would under-report exactly the jobs an
    operator is investigating.
    """
    label = {"model": str(model or "unknown")}
    _registry.increment(LLM_CALLS, label)
    if unmetered:
        _registry.increment(LLM_UNMETERED, label)
        return
    if prompt_tokens > 0:
        _registry.increment(LLM_TOKENS, {**label, "kind": "prompt"}, by=float(prompt_tokens))
    if completion_tokens > 0:
        _registry.increment(
            LLM_TOKENS, {**label, "kind": "completion"}, by=float(completion_tokens)
        )


def count_llm_cost(cost_usd: float, model: str) -> None:
    """Record spend for one call. Only called when a rate is configured.

    An unpriced call contributes nothing here rather than zero, so ``clipping_llm_cost_usd_total``
    being absent means "no price configured" while its being ``0`` would mean "priced, and
    free" - the same distinction ``worker/llm_cost.py`` keeps in the API.
    """
    if cost_usd > 0:
        _registry.increment(LLM_COST, {"model": str(model or "unknown")}, by=float(cost_usd))


def count_webhook(outcome: str) -> None:
    """Record a webhook delivery attempt and how it went."""
    _registry.increment(WEBHOOKS, {"outcome": str(outcome or "unknown")})


# --------------------------------------------------------------------------- #
# Read side
# --------------------------------------------------------------------------- #
def counters() -> dict[str, dict[tuple[tuple[str, str], ...], float]]:
    return _registry.counters()


def histograms() -> dict[str, dict[tuple[tuple[str, str], ...], Histogram]]:
    return _registry.histograms()


def reset() -> None:
    _registry.reset()
