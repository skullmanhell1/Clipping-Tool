"""Job-scoped logging context and per-stage timings (I6, M5).

Two problems, one mechanism.

**I6 - a log line could not be attributed to a job.** With a single worker that was survivable
by reading timestamps; the moment concurrency lands (I1) it is not, because two renders
interleave their output and every line becomes ambiguous. Worse, the lines that matter most are
the degradations - ``loudness_degraded``, ``font_substituted``, a storage mirror failure - and
those are exactly the ones an operator needs to trace back to one clip in one job.

**M5 - nobody knew where the minutes went.** A render takes minutes and the only measurement was
the wall-clock of the whole thing. Which stage dominates is not guessable: ASR is the obvious
suspect, but the pipeline performs at least three full re-encodes per clip (O6), so on a short
source with a long clip list the encodes can outweigh the transcription entirely. Optimising
before measuring would be guessing.

**A ``contextvars`` context, not a parameter threaded through every call.** The alternative is
passing a job id into every function that might log, which touches most of the codebase, gets
forgotten at exactly the sites added later, and is impossible to enforce. A context variable
survives the call depth without any function needing to know about it.

The tradeoff is real and worth naming: a context variable is invisible in a signature, so a
reader cannot tell from a function that its log lines are attributed. That is why the filter
degrades to ``job=-`` rather than raising - an unattributed line is still a line worth having.

**One constraint that is easy to get wrong.** ``contextvars`` are copied for asyncio *tasks*, not
for ``threading.Thread``: a new thread starts with an *empty* context. So the context must be
entered **on** the worker thread, inside the function the pool runs - which is what
``JobManager._run`` does. Wrapping ``executor.submit`` instead would attribute nothing at all,
and the log would look exactly as it did before this existed. Anything that spawns further
threads beneath a job has to re-enter the context itself; there is a test pinning both halves.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

# Aliased because `metrics` already means "this job's Stage_Timing accumulator" throughout this
# module and its callers, and reusing the name for the process-wide registry would make every
# existing `metrics.record(...)` line ambiguous to a reader. Import-safe: `worker.metrics` is
# stdlib-only and imports nothing from this package, so there is no cycle.
from worker import metrics as process_metrics

#: The job whose work the current thread/task is doing, or ``None``.
_current_job: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "clipping_job_id", default=None
)

#: The stage within that job. Separate from the job id because a stage is entered and left many
#: times per job, and nesting them in one variable would make either restoring or reading it
#: awkward.
_current_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "clipping_stage", default=None
)


def current_job_id() -> str | None:
    return _current_job.get()


def current_stage() -> str | None:
    return _current_stage.get()


@contextmanager
def job_context(job_id: str | None) -> Iterator[None]:
    """Attribute everything logged inside this block to ``job_id``.

    The token is reset in a ``finally`` rather than the variable being reassigned, which is what
    makes nesting safe: a worker thread reused for a second job cannot inherit the first job's
    id, and that would be the worst possible failure for a logging feature - lines attributed
    confidently to the wrong job.
    """
    token = _current_job.set(job_id)
    try:
        yield
    finally:
        _current_job.reset(token)


class Job_Context_Filter(logging.Filter):
    """Adds ``job_id`` and ``stage`` to every record so a format string can use them.

    A :class:`logging.Filter` rather than a :class:`logging.LoggerAdapter`, because an adapter
    has to be threaded to each call site and a filter attaches once to a handler and covers
    every logger in the process - including third-party ones, where an unexpected warning
    during a render is precisely the thing worth attributing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.job_id = _current_job.get() or "-"
        record.stage = _current_stage.get() or "-"
        return True


#: A format that keeps the job id in a fixed column so a log is scannable by eye.
LOG_FORMAT = "%(asctime)s %(levelname)-7s job=%(job_id)s stage=%(stage)s %(name)s: %(message)s"


def install(level: int | None = None) -> None:
    """Attach the filter and format to the root handlers (idempotent).

    Idempotent because it is called from the API's startup *and* is useful from a script, and
    attaching the same filter twice would double every record's work for no benefit. Existing
    handlers are reused rather than replaced, so a host that has configured its own logging
    (a container platform capturing stdout, say) keeps its configuration and only gains the
    job attribution.
    """
    root = logging.getLogger()
    if level is not None:
        root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        if not any(isinstance(f, Job_Context_Filter) for f in handler.filters):
            handler.addFilter(Job_Context_Filter())
        handler.setFormatter(logging.Formatter(LOG_FORMAT))


# --------------------------------------------------------------------------- #
# M5 - per-stage timings
# --------------------------------------------------------------------------- #
@dataclass
class Stage_Timing:
    """How long one named stage took, and how many times it ran."""

    name: str
    seconds: float = 0.0
    count: int = 0

    def to_dict(self) -> dict:
        return {
            "stage": self.name,
            "seconds": round(self.seconds, 3),
            "count": self.count,
            # Reported because a stage that runs once per clip and one that runs once per job
            # are not comparable on total time alone - and both exist in this pipeline.
            "mean_seconds": round(self.seconds / self.count, 3) if self.count else 0.0,
        }


@dataclass
class Job_Metrics:
    """Accumulated stage timings for one job.

    Locked, because stages are recorded from the worker thread while the API may read them for a
    progress response at any moment. Without the lock a read during a write can see a partially
    updated mapping - which would not raise, it would report a wrong number, and a wrong number
    in a performance report is worse than no number because it gets acted on.
    """

    stages: dict[str, Stage_Timing] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, name: str, seconds: float) -> None:
        with self._lock:
            timing = self.stages.get(name)
            if timing is None:
                timing = Stage_Timing(name)
                self.stages[name] = timing
            timing.seconds += max(0.0, float(seconds))
            timing.count += 1
        # Phase 7: the same observation into the process-lifetime histogram that backs
        # `/metrics`. Hooked *here* rather than at each call site because this method is where
        # every stage timing in the pipeline already lands - both the `progress` callback's
        # stage transitions and the `stage()` context manager below funnel through it - so a
        # stage cannot be added in future without appearing in the histogram too.
        #
        # Outside the lock: `observe_stage` takes its own, and while the nesting could not
        # deadlock (nothing in `worker.metrics` calls back into this module), holding a lock
        # across a call into another locked component is a habit that eventually does.
        process_metrics.observe_stage(name, seconds)

    def total_seconds(self) -> float:
        with self._lock:
            return sum(t.seconds for t in self.stages.values())

    def to_list(self) -> list[dict]:
        """Timings as a list, slowest first - which is the only order worth reading."""
        with self._lock:
            timings = sorted(self.stages.values(), key=lambda t: -t.seconds)
        return [t.to_dict() for t in timings]

    def summary(self) -> str:
        """A single log line naming where the time went."""
        rows = self.to_list()
        if not rows:
            return "no stages recorded"
        total = sum(r["seconds"] for r in rows) or 1.0
        parts = [f"{r['stage']} {r['seconds']:.1f}s ({r['seconds'] / total:.0%})" for r in rows]
        return f"total {total:.1f}s: " + ", ".join(parts)


#: Metrics per job id. Bounded, because this is process-global and a long-running instance would
#: otherwise accumulate one entry per job forever - a slow leak that looks like nothing.
_metrics: dict[str, Job_Metrics] = {}
_metrics_lock = threading.Lock()
MAX_TRACKED_JOBS = 200


def metrics_for(job_id: str) -> Job_Metrics:
    """The metrics record for ``job_id``, creating it if needed."""
    with _metrics_lock:
        found = _metrics.get(job_id)
        if found is None:
            found = Job_Metrics()
            _metrics[job_id] = found
            if len(_metrics) > MAX_TRACKED_JOBS:
                # Drop the oldest inserted key. Python dicts preserve insertion order, so this
                # is a bounded FIFO without a second structure to keep in step.
                oldest = next(iter(_metrics))
                if oldest != job_id:
                    _metrics.pop(oldest, None)
        return found


def clear_metrics(job_id: str | None = None) -> None:
    """Forget one job's metrics, or all of them (used by tests)."""
    with _metrics_lock:
        if job_id is None:
            _metrics.clear()
        else:
            _metrics.pop(job_id, None)


@contextmanager
def stage(name: str, job_id: str | None = None) -> Iterator[None]:
    """Time a named stage and attribute log lines inside it (I6, M5).

    The timing is recorded in a ``finally``, so a stage that *fails* is still measured. That is
    deliberate: a stage that reliably takes ninety seconds and then throws is the most useful
    row in a performance report, and recording only successes would hide it entirely.

    ``time.monotonic`` rather than ``time.time``, because a clock adjustment mid-render would
    otherwise produce a negative duration.
    """
    job = job_id if job_id is not None else _current_job.get()
    token = _current_stage.set(name)
    started = time.monotonic()
    try:
        yield
    finally:
        _current_stage.reset(token)
        if job:
            metrics_for(job).record(name, time.monotonic() - started)
