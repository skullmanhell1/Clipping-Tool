"""In-process job store and background job manager.

Phase 1 runs jobs in a background thread pool with one worker by default, so a
batch of videos/links is processed **in line** (sequentially), matching the
product requirement. The pool and its pending queue are bounded, while
progress is tracked live in an in-memory, thread-safe store that the API polls.

This is the "in-process fallback" referenced by the config. A Redis + RQ backend
can be swapped in later behind the same :class:`JobManager` interface without
changing the API or pipeline.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from config import settings
from worker import cancellation, observability, output_profiles
from worker import download as dl
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions
from worker.pipeline import run_pipeline
from worker.project_cleanup import _ACTIVE_STATUSES, PROJECT_CLEANUP_LOCK, cleanup_for_new_project

logger = logging.getLogger(__name__)

# Fraction of total progress reserved for downloading a URL before processing.
_DOWNLOAD_BUDGET = 0.10

#: The stages a job passes through, in order, for U8's "step N of M" display.
#:
#: Derived from the strings ``run_pipeline`` already reports rather than invented, so the count
#: cannot drift from what the pipeline actually does. Matching is on a prefix because the later
#: reports carry per-clip detail ("Rendering clip 2 of 5").
JOB_STAGES: tuple[str, ...] = (
    "Starting",
    "Analyzing video",
    "Transcribing audio",
    "Finding the best moments",
    "Creating",
    "Rendering clip",
    "Adding effects",
    "Writing copy",
    "Completed",
)

# A job is deliberately kept in-process for this deployment.  The executor gives us bounded
# workers, while this separate capacity guard bounds the executor's otherwise-unbounded pending
# work queue.  A finite queue is important here: accepting an unlimited number of video sources
# would let a burst of submissions consume memory long before the UI could report useful progress.
DEFAULT_QUEUE_SIZE = 100
QUEUE_FULL_ERROR = "The local job queue is full; try again when a worker is available."
SCHEDULE_ERROR_PREFIX = "The job could not be scheduled: "

_ALLOWED_STATUS_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {
            JobStatus.QUEUED,
            JobStatus.PROCESSING,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.PROCESSING: frozenset(
        {JobStatus.PROCESSING, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    # Resume is the only supported path out of a terminal failure/cancellation, and it always
    # passes through queued first.  Keeping this rule here prevents a stale worker from reviving a
    # failed job directly as processing.
    JobStatus.FAILED: frozenset({JobStatus.QUEUED}),
    JobStatus.CANCELLED: frozenset({JobStatus.QUEUED}),
    # A terminal state is immutable.  User clip edits do not pass a status field and therefore
    # remain allowed; a late worker completion/failure cannot overwrite the terminal result.
    JobStatus.COMPLETED: frozenset(),
}


def _stage_label(stage: str) -> str:
    """The stage name with its per-clip detail stripped, for grouping timings.

    "Rendering clip 2 of 5" and "Rendering clip 3 of 5" are the same stage measured twice, not
    two stages. Without this, a five-clip job produces five one-off rows instead of one row with
    a count and a mean - and the mean is the number that tells you whether rendering dominates.
    """
    index = stage_position(stage)
    return JOB_STAGES[index - 1] if index else (stage or "unknown")


def stage_position(stage: str) -> int:
    """The 1-based index of ``stage`` within :data:`JOB_STAGES`, or 0 if unrecognised.

    Prefix matching, and *longest* prefix wins: "Rendering clip 2 of 5" must not match
    "Starting" merely because the list is scanned in order, and a stage nobody anticipated
    returns 0 so the UI shows a plain bar rather than a wrong step number.
    """
    text = (stage or "").strip()
    best_index, best_length = 0, -1
    for index, known in enumerate(JOB_STAGES, start=1):
        if text.startswith(known) and len(known) > best_length:
            best_index, best_length = index, len(known)
    return best_index


class JobStore:
    """Thread-safe store of :class:`Job` objects, durable across restarts.

    Reads are served from memory — the API polls job state continuously and must not
    pay for a query each time — while every mutation is mirrored into
    :class:`worker.job_persistence.Job_Persistence` so a restart does not lose the
    record. Persistence is deliberately *write-through* rather than periodic: the
    interesting moment to survive is a crash, and a crash gives no chance to flush.

    Args:
        persistence: The durable backend. ``None`` uses the configured shared store;
            pass ``False`` to opt out entirely, which keeps the class usable as a pure
            in-memory store for tests that do not care about durability.
    """

    def __init__(self, persistence: Any = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._persistence = self._resolve_persistence(persistence)
        self._restore()

    @staticmethod
    def _resolve_persistence(persistence: Any) -> Any:
        """The durable backend to use, or ``None`` for memory-only operation."""
        if persistence is False:
            return None
        if persistence is not None:
            return persistence
        try:
            from worker.job_persistence import get_job_persistence

            return get_job_persistence()
        except Exception:  # pragma: no cover - defensive
            # A store that cannot be opened must not stop the process from serving
            # jobs; it only means this run is not durable.
            logger.exception("job persistence unavailable; continuing in memory only")
            return None

    def _restore(self) -> None:
        """Load persisted jobs into memory, then bound the table."""
        if self._persistence is None:
            return
        try:
            for job in self._persistence.load_all():
                self._jobs[job.id] = job
            self._persistence.prune(keep=int(settings.max_persisted_jobs))
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to restore persisted jobs")

    def _persist(self, job: Job | None, payload: dict | None = None) -> None:
        """Mirror ``job`` into the durable store, if one is configured.

        Swallows every failure. This runs on the hot path of a live render — a progress
        update mid-encode reaches it — so a full disk or a locked database must cost the
        durability of one record, not the job itself. ``Job_Persistence`` is already
        defensive internally; this guard also covers an injected or third-party backend.

        ``payload`` is the job serialised *inside* the store lock. Without it ``save()`` re-read
        the live object here, outside the lock, so the row could be written from a state that
        never existed as a whole — and two overlapping updates committed in scheduling order
        rather than in the order they happened, which shows up after a restart as a job reporting
        an earlier stage than it had reached.
        """
        if self._persistence is None or job is None:
            return
        try:
            # Cleanup and persistence share this lock. Re-check membership after
            # taking it: a mutation captured just before a reset must not
            # resurrect its row after the reset deletes it.
            with PROJECT_CLEANUP_LOCK:
                with self._lock:
                    if self._jobs.get(job.id) is not job:
                        return
                self._persistence.save(job, payload=payload)
        except Exception:
            logger.exception("failed to persist job %s", getattr(job, "id", "?"))

    def add(self, job: Job) -> bool:
        """Add a job once, returning ``False`` rather than replacing a duplicate id."""
        with self._lock:
            if job.id in self._jobs:
                # A short id collision must not replace a live job in memory or on disk.  The
                # manager retries allocation; direct callers get an explicit rejection and can
                # decide whether to retry or report it.
                return False
            self._jobs[job.id] = job
            payload = job.to_dict()
        # Written outside the lock: SQLite has its own locking and holding both would
        # serialise every API poll behind a disk write. The *payload* is captured inside it,
        # so what gets written is a state that actually existed.
        self._persist(job, payload)
        return True

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def by_batch(self, batch_id: str) -> list[Job]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.batch_id == batch_id]
        return sorted(jobs, key=lambda j: j.created_at)

    # -- serialised reads --------------------------------------------------
    #
    # `get`/`all`/`by_batch` hand out **live** Job objects, and the lock above only guards the
    # dict lookup. Serialising one of those outside the lock races the render thread, which is
    # inside `update()` several times a second on a busy job:
    #
    #   * `Job.to_dict` iterates `self.clips` while `_store_clip` mutates `clip.video_url` and
    #     `clip.thumbnail_url` in place, so a poll landing mid-loop can emit a clip whose video
    #     URL points at S3 and whose thumbnail still points at the local path.
    #   * It hands out the same `planned_clips` / `stage_timings` list objects that `update()`
    #     rebinds, and JSON-encoding a list another thread is replacing is where
    #     `RuntimeError: list changed size during iteration` comes from - an intermittent 500 on
    #     `/api/jobs`, which the shipped UI polls every 1200 ms.
    #
    # Serialising *inside* the lock is the fix, and it is cheap: `to_dict` is a dict build over
    # data already in memory, with no I/O. These are what the API read routes use; `get` stays as
    # it is for the worker, which legitimately wants the live object.

    def snapshot(self, job_id: str) -> dict | None:
        """One job serialised under the lock, or ``None`` if unknown."""
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job is not None else None

    def snapshot_all(self) -> list[dict]:
        """Every job serialised under the lock, newest first."""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.to_dict() for j in jobs]

    def snapshot_batch(self, batch_id: str) -> list[dict]:
        """Every job in one batch, serialised under the lock, oldest first."""
        with self._lock:
            jobs = sorted(
                (j for j in self._jobs.values() if j.batch_id == batch_id),
                key=lambda j: j.created_at,
            )
            return [j.to_dict() for j in jobs]

    def update(self, job_id: str, **fields) -> bool:
        """Atomically update fields on a stored job.

        Returns ``False`` when the requested status transition is stale or invalid.  Worker
        callbacks and the cancellation endpoint run on different threads, so a plain assignment
        allowed a late ``processing`` or ``completed`` write to undo a user's cancellation.  The
        transition check makes terminal states last-writer-safe without changing the public job
        representation.
        """
        import time

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False

            next_status: JobStatus | None = None
            if "status" in fields:
                try:
                    next_status = (
                        fields["status"]
                        if isinstance(fields["status"], JobStatus)
                        else JobStatus(fields["status"])
                    )
                except (TypeError, ValueError):
                    return False
                allowed = _ALLOWED_STATUS_TRANSITIONS.get(job.status, frozenset())
                if next_status != job.status and next_status not in allowed:
                    return False
                # Once a job is completed or failed, a second worker must not be able to replace
                # its result while reusing the same terminal status.  Cancellation is the one
                # same-status update we allow: the worker's cancellation handler may still need to
                # attach clips that finished just before the stop request.  Metadata edits
                # intentionally omit ``status`` and are still accepted below.
                if job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                    return False
                fields = {**fields, "status": next_status}

            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = time.time()
            payload = job.to_dict()
        self._persist(job, payload)
        return True

    def update_clip(self, job_id: str, clip_id: str, fields: dict) -> ClipResult | None:
        """Atomically update editable fields on one clip within a job.

        Only known :class:`ClipResult` attributes are updated (unknown keys are
        ignored). Returns the updated clip, or ``None`` if not found.

        Annotated as ``Optional[ClipResult]`` rather than the ``Optional[object]`` it used to
        be. The docstring already promised a ``ClipResult``, and `job.clips` is typed as one, so
        `object` was imprecision rather than a deliberate widening — but it propagated: every
        caller in ``api/main.py`` then had to reach for attributes mypy could not see, which was
        14 of the 18 findings in that file. Naming the real type here fixes them all at once.
        """
        import time

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            clip = next((c for c in job.clips if c.id == clip_id), None)
            if clip is None:
                return None
            for key, value in fields.items():
                if hasattr(clip, key):
                    setattr(clip, key, value)
            job.updated_at = time.time()
            payload = job.to_dict()
        # Clip metadata edits are user-visible and must survive a restart just as job
        # state does, so this mirrors too.
        self._persist(job, payload)
        return clip

    def get_clip(self, job_id: str, clip_id: str) -> ClipResult | None:
        """Return a single clip within a job, or ``None``.

        See :meth:`update_clip` for why this names ``ClipResult`` rather than ``object``.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return next((c for c in job.clips if c.id == clip_id), None)

    def clear_for_new_project(self, *, include_active: bool = False) -> dict[str, Any]:
        """Remove old jobs, retaining queued/processing jobs unless explicitly requested."""
        with PROJECT_CLEANUP_LOCK:
            with self._lock:
                protected = {
                    job.id for job in self._jobs.values() if job.status in _ACTIVE_STATUSES
                }
                if include_active:
                    protected.clear()
                removed_ids = [job_id for job_id in self._jobs if job_id not in protected]
                for job_id in removed_ids:
                    del self._jobs[job_id]

            persistence_removed = 0
            if self._persistence is not None and removed_ids:
                try:
                    delete_many = getattr(self._persistence, "delete_many", None)
                    if delete_many is not None:
                        persistence_removed = int(delete_many(removed_ids) or 0)
                    else:  # Compatibility with a small injected persistence fake.
                        for job_id in removed_ids:
                            self._persistence.delete(job_id)
                            persistence_removed += 1
                except Exception:  # pragma: no cover - defensive
                    logger.exception("failed to clear previous project jobs")

            return {
                "removed_job_ids": removed_ids,
                "protected_job_ids": sorted(protected),
                "persistence_removed": persistence_removed,
            }


class JobManager:
    """Owns the bounded worker pool and drives jobs through the pipeline.

    ``ThreadPoolExecutor`` already keeps API requests off the render path, but its internal work
    queue is unbounded.  ``max_queue_size`` adds a non-blocking capacity guard around it: callers
    never wait for a slot, and a burst beyond capacity becomes a durable failed job that the UI can
    display instead of a request timeout or an indefinitely spinning queued row.

    The default remains one worker, preserving the original in-order behaviour for heavy ingest and
    render work.  Deployments/tests may opt into a small number of workers explicitly; the semaphore
    bounds both running and pending jobs in that case.
    """

    def __init__(
        self,
        store: JobStore | None = None,
        max_workers: int = 1,
        max_queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self.store = store or JobStore()
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if max_queue_size is None:
            max_queue_size = DEFAULT_QUEUE_SIZE
        if (
            isinstance(max_queue_size, bool)
            or not isinstance(max_queue_size, int)
            or max_queue_size < 0
        ):
            raise ValueError("max_queue_size must be a non-negative integer")

        self.max_workers = max_workers
        self.max_queue_size = max_queue_size
        # The capacity includes running jobs.  ``acquire(False)`` below is what keeps submission
        # non-blocking when all workers and queue slots are occupied.
        self._capacity = threading.BoundedSemaphore(max_workers + max_queue_size)
        self._schedule_lock = threading.Lock()
        self._scheduled: set[str] = set()
        # A single worker => batch items processed one after another, in order.  A caller that opts
        # into >1 workers still gets a bounded executor with FIFO submission into its work queue.
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="clipper-job",
        )

    @property
    def pending_count(self) -> int:
        """Number of jobs currently reserved in the in-process queue or workers."""
        with self._schedule_lock:
            return len(self._scheduled)

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        """Stop accepting work and release executor resources.

        The API singleton lives for the process lifetime, but this small lifecycle seam is useful
        for embedders and keeps offline queue tests from leaking worker threads.  It intentionally
        mirrors :meth:`ThreadPoolExecutor.shutdown`; no public API route depends on it.
        """
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def start_new_project(self, *, include_active: bool = False) -> dict[str, Any]:
        """Clear prior project state before a fresh submission.

        Active jobs are protected by default because their workers may still be
        writing files. ``include_active`` is reserved for an explicit operator
        action that accepts clearing those jobs as well.
        """
        from publishers.history import get_history

        return cleanup_for_new_project(
            self.store,
            get_history(),
            include_active=include_active,
        )

    def _reserve_slot(self, job_id: str) -> bool:
        """Reserve one bounded queue slot, rejecting duplicate scheduling attempts."""
        with self._schedule_lock:
            if job_id in self._scheduled:
                return False
            if not self._capacity.acquire(blocking=False):
                return False
            self._scheduled.add(job_id)
            return True

    def _release_slot(self, job_id: str) -> None:
        """Release a reservation exactly once, including after executor submission errors."""
        with self._schedule_lock:
            if job_id not in self._scheduled:
                return
            self._scheduled.remove(job_id)
            self._capacity.release()

    def _mark_worker_error(self, job_id: str, exc: BaseException) -> None:
        """Persist an unexpected worker failure without replacing a terminal state."""
        self.store.update(
            job_id,
            status=JobStatus.FAILED,
            error=str(exc) or type(exc).__name__,
            stage="Failed",
        )

    def _worker_entry(self, job_id: str, resume: bool = False) -> None:
        """Executor wrapper that contains failures and always frees queue capacity."""
        try:
            # Keep the original one-argument call shape for normal jobs.  Besides being a small
            # compatibility courtesy for embedders that replace ``_run``, this makes the resume
            # flag visible only on the path that needs it.
            if resume:
                self._run(job_id, True)
            else:
                self._run(job_id)
        except BaseException as exc:
            # ``_run``/``_execute`` already handle normal pipeline errors.  This outer guard is
            # for failures in the worker harness itself (including an injected test worker): a
            # Future exception must not leave the job queued forever or consume a slot forever.
            # Release capacity before persisting FAILED so observers never see a terminal job
            # while the queue still appears full.
            cancellation.clear(job_id)
            self._release_slot(job_id)
            self._mark_worker_error(job_id, exc)
            logger.exception("unexpected worker error for job %s", job_id)
        finally:
            cancellation.clear(job_id)
            self._release_slot(job_id)

    def _schedule(
        self,
        job_id: str,
        *,
        resume: bool = False,
        prepare: Any = None,
    ) -> bool:
        """Submit one job without waiting for a worker or queue slot.

        ``prepare`` is used by resume to perform its queued transition only after this job has
        claimed a unique queue reservation.  This closes the double-resume race without changing
        the API's boolean return contract.
        """
        job = self.store.get(job_id)
        if job is None:
            return False
        if prepare is None and job.status is not JobStatus.QUEUED:
            # A completed/failed/cancelled job is not a fresh submission.  In particular, do not
            # let a duplicate callback re-run a completed job after its first reservation is
            # released.
            return False
        if not self._reserve_slot(job_id):
            return False

        submitted = False
        try:
            if prepare is not None and not prepare():
                return False
            self._executor.submit(self._worker_entry, job_id, resume)
            submitted = True
            return True
        except BaseException as exc:
            # Executor shutdown and executor/test-double failures must become a visible terminal
            # job state rather than an unobserved Future exception.
            self._mark_worker_error(job_id, RuntimeError(f"{SCHEDULE_ERROR_PREFIX}{exc}"))
            return False
        finally:
            if not submitted:
                self._release_slot(job_id)

    # -- submission --------------------------------------------------------

    def submit(
        self,
        input_type: str,
        source: str,
        options: ProcessingOptions,
        batch_id: str | None = None,
        title: str = "",
        avoid_windows: list[dict] | None = None,
    ) -> Job:
        """Create a job, store it as queued, and schedule it for processing."""
        for _ in range(8):
            job = Job(
                input_type=input_type,
                source=source,
                options=options,
                batch_id=batch_id,
                title=title or (Path(source).name if input_type == "file" else source),
                avoid_windows=list(avoid_windows or []),
            )
            if self.store.add(job):
                break
        else:
            raise RuntimeError("could not allocate a unique job id")
        # I4: clear any stale request under this id before the work is scheduled. Ids are short
        # hex and a collision is unlikely rather than impossible, and inheriting a previous
        # job's cancellation would stop a brand-new job before it started.
        cancellation.clear(job.id)
        if not self._schedule(job.id):
            # A full queue is a normal, actionable outcome rather than an exception.  Returning a
            # failed job preserves the existing submission response shape and lets the frontend
            # surface the reason through the normal status polling path.
            current = self.store.get(job.id)
            if current is not None and current.status is JobStatus.QUEUED:
                self.store.update(
                    job.id,
                    status=JobStatus.FAILED,
                    stage="Queue full",
                    error=QUEUE_FULL_ERROR,
                )
        return job

    # -- cancellation (I4) -------------------------------------------------

    def cancel(self, job_id: str) -> bool:
        """Ask a job to stop, returning whether it was in a cancellable state.

        A queued job is marked cancelled immediately - no worker has claimed it, so there is
        nothing to wait for and reporting it as "cancelling" would be a lie. A processing job is
        marked cancelled *and* flagged, because the worker stops at its next checkpoint and the
        user needs the acknowledgement now rather than whenever the current ffmpeg pass ends.

        A finished job (completed, failed, already cancelled) returns ``False`` rather than
        raising: cancelling something that already stopped is a harmless no-op, and reporting it
        as an error would make a double-click on the button look like a failure.
        """
        job = self.store.get(job_id)
        if job is None or job.status in (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            return False
        previous_status = job.status
        cancellation.request_cancel(job_id)
        updated = self.store.update(
            job_id,
            status=JobStatus.CANCELLED,
            stage="Cancelled",
            error=None,
        )
        if not updated:
            # The worker won a race and reached a terminal state.  Do not leave a cancellation
            # event behind for a later id reuse.
            cancellation.clear(job_id)
            return False
        logger.info("job %s cancelled by request (was %s)", job_id, previous_status.value)
        return True

    def submit_batch(self, items: list[dict], options: ProcessingOptions) -> str:
        """Submit multiple items as one batch; returns the batch id.

        Each item is ``{"input_type": "url"|"file", "source": str, "title"?: str}``.
        """
        batch_id = uuid.uuid4().hex[:12]
        for item in items:
            self.submit(
                input_type=item["input_type"],
                source=item["source"],
                options=options,
                batch_id=batch_id,
                title=item.get("title", ""),
            )
        return batch_id

    # -- execution ---------------------------------------------------------

    def _run(self, job_id: str, resume: bool = False) -> None:
        """Worker entrypoint: ingest then run the pipeline, tracking progress.

        The whole body runs inside :func:`worker.observability.job_context`, so every log line
        emitted anywhere beneath it - including from third-party libraries - carries this job's
        id (I6). Without that, two concurrent renders interleave their output and no line can be
        attributed; with a single worker that was survivable by reading timestamps, and it stops
        being survivable the moment I1 lands.
        """
        job = self.store.get(job_id)
        if job is None:
            return

        metrics = observability.metrics_for(job_id)
        # The stage currently being timed, so `progress` can close the previous one when the
        # pipeline moves on. A list because the closure below rebinds it.
        open_stage: list[tuple[str, float] | None] = [None]

        def close_stage() -> None:
            entry = open_stage[0]
            if entry is None:
                return
            name, started = entry
            metrics.record(name, time.monotonic() - started)
            open_stage[0] = None

        def progress(fraction: float, stage: str) -> None:
            # I4: the progress callback is the cancellation checkpoint. It is already invoked at
            # every stage boundary and once per clip, so this needs no new plumbing and lands
            # exactly where the job's state is consistent - between passes, not mid-encode.
            cancellation.checkpoint(job_id)
            # M5: close the previous stage and open this one. Timing is derived from the
            # transitions the pipeline already reports rather than from new instrumentation at
            # each site, so a stage cannot be added without its timing appearing.
            label = _stage_label(stage)
            current = open_stage[0]
            if current is None or current[0] != label:
                close_stage()
                open_stage[0] = (label, time.monotonic())
            updated = self.store.update(
                job_id,
                progress=fraction,
                stage=stage,
                status=JobStatus.PROCESSING,
                # U8: which step of how many, so the UI can show structure rather than one bar.
                stage_index=stage_position(stage),
                stage_total=len(JOB_STAGES),
                stage_timings=metrics.to_list(),
            )
            if not updated:
                # A cancellation (or another terminal worker) won the race between the
                # checkpoint above and this write.  Stop before the pipeline can perform more
                # expensive work or publish a result for a job that is no longer processing.
                raise cancellation.Job_Cancelled(job_id)

        with observability.job_context(job_id):
            self._execute(job, job_id, progress, close_stage, metrics, resume)

    #: How close two windows must be to count as the same planned moment, in seconds (I5).
    #:
    #: Not exact equality: `AU7` silence trimming and `S9` cut snapping both move a clip's
    #: boundaries *after* the plan is recorded, so a rendered clip's start rarely matches the
    #: window it came from to the millisecond. A second is comfortably larger than either
    #: adjustment and far smaller than the gap between two distinct moments.
    WINDOW_MATCH_TOLERANCE_S = 1.0

    def _missing_windows(self, job) -> list | None:
        """The planned windows with no rendered clip, as candidates. ``None`` when unknowable.

        Returning ``None`` means "no usable plan", and the caller then renders normally - which is
        the pre-I5 behaviour and the only honest answer for a job interrupted before selection
        finished.
        """
        planned = list(job.planned_clips or [])
        if not planned:
            return None

        from worker.selection import ClipCandidate

        done = [(float(c.start), float(c.end)) for c in (job.clips or [])]
        missing = []
        for window in planned:
            try:
                start = float(window.get("start"))
                end = float(window.get("end"))
            except (AttributeError, TypeError, ValueError):
                continue
            already = any(
                abs(start - s) <= self.WINDOW_MATCH_TOLERANCE_S
                and abs(end - e) <= self.WINDOW_MATCH_TOLERANCE_S
                for s, e in done
            )
            if not already:
                missing.append(
                    ClipCandidate(
                        start=start,
                        end=end,
                        reason=str(window.get("reason") or ""),
                        score=float(window.get("score") or 0.0),
                        features=dict(window.get("features") or {}),
                        cuts=list(window.get("cuts") or []),
                        fallback_reason=str(window.get("fallback_reason") or ""),
                    )
                )
        return missing or None

    def resume(self, job_id: str) -> bool:
        """Re-run a failed job's unfinished clips, keeping the ones it already produced (I5).

        An interrupted job was marked failed *wholesale*: the clips it had already rendered were
        on disk and listed in the record, and the only way forward was to re-submit the source and
        pay for everything again - including re-rendering the clips that had succeeded.

        Returns ``False`` when the job cannot be resumed, which the API turns into a 409 naming
        the reason rather than silently starting a full re-run.
        """
        job = self.store.get(job_id)
        if job is None:
            return False
        if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
            return False
        if self._missing_windows(job) is None:
            return False

        def prepare() -> bool:
            transitioned = self.store.update(
                job_id,
                status=JobStatus.QUEUED,
                stage="Queued (resuming)",
                error=None,
                progress=0.0,
            )
            if transitioned:
                cancellation.clear(job_id)
            return transitioned

        return self._schedule(job_id, resume=True, prepare=prepare)

    def _execute(self, job, job_id, progress, close_stage, metrics, resume: bool = False) -> None:
        """The job body. Split out so ``_run`` is only the context and callback wiring."""
        # Clips finished before a cancellation landed. Held outside the `try` so the
        # Job_Cancelled handler can record them: they are already encoded, mirrored and on
        # disk, and dropping them from the record would leave files no API response mentions.
        rendered_clips: list[ClipResult] = []
        try:
            # I4: a queued job stops here, before any work begins - no worker has claimed it, so
            # there is nothing to wait for.
            cancellation.checkpoint(job_id)
            started = self.store.update(
                job_id,
                status=JobStatus.PROCESSING,
                stage="Starting",
                progress=0.0,
                stage_index=1,
                stage_total=len(JOB_STAGES),
            )
            if not started:
                raise cancellation.Job_Cancelled(job_id)

            source_path = Path(job.source)
            start_progress = 0.0

            # URL inputs are downloaded first (reserving a slice of progress).
            if job.input_type == "url":

                def dl_progress(frac: float, msg: str) -> None:
                    # I4: the download callback is a cancellation checkpoint for the same reason
                    # the pipeline's `progress` is, and leaving it out did more than delay the
                    # stop - it *undid* it. `cancel()` writes CANCELLED, then the next yt-dlp
                    # progress tick (sub-second) wrote `status=PROCESSING` straight back over it,
                    # so the UI showed "Cancelled" and then flipped to "Processing 6%" and kept
                    # climbing. The download itself ran to completion, so a multi-gigabyte fetch
                    # the user had explicitly stopped still spent the bandwidth and still landed
                    # in uploads/. Checking here stops it at the next tick instead.
                    cancellation.checkpoint(job_id)
                    self.store.update(
                        job_id,
                        progress=frac * _DOWNLOAD_BUDGET,
                        stage=msg,
                        status=JobStatus.PROCESSING,
                    )

                source_path, meta = dl.download_video(
                    job.source, settings.uploads_dir, progress_cb=dl_progress
                )
                self.store.update(
                    job_id,
                    title=meta.title or job.title,
                    duration=meta.duration,
                    thumbnail=meta.thumbnail,
                )
                start_progress = _DOWNLOAD_BUDGET

            # U7: record the resolved local file. Without it a URL job's source is only a URL,
            # so re-rendering a single clip would have to download the video again - which is
            # most of what makes re-running a whole job slow in the first place.
            self.store.update(job_id, source_path=str(source_path))

            if not source_path.exists():
                raise FileNotFoundError(f"Source not found: {source_path}")

            # Probe to record duration for the preview card (file inputs).
            if job.duration is None:
                from worker.ffmpeg_utils import probe

                try:
                    job_info = probe(source_path)
                    self.store.update(job_id, duration=job_info.duration)
                except Exception:
                    # Duration only feeds the preview card, so a probe failure must not
                    # fail the job — but an unreadable source usually means the pipeline
                    # is about to fail too, and that is worth seeing in the log.
                    logger.warning(
                        "could not probe duration for job %s (%s)",
                        job_id,
                        source_path,
                        exc_info=True,
                    )

            clips_dir = Path(settings.clips_dir) / job_id
            temp_dir = Path(settings.temp_dir) / job_id

            # I5: on a resume, render only the windows that have no clip on disk yet. The
            # already-rendered clips are kept and the new ones appended, so a job interrupted
            # after seven of ten clips costs three renders rather than ten.
            resume_windows = self._missing_windows(job) if resume else None
            existing_clips = list(job.clips or []) if resume else []

            def record_plan(candidates) -> None:
                self.store.update(
                    job_id,
                    planned_clips=[
                        {
                            "start": float(getattr(c, "start", 0.0)),
                            "end": float(getattr(c, "end", 0.0)),
                            "reason": str(getattr(c, "reason", "")),
                            "score": float(getattr(c, "score", 0.0)),
                            "features": dict(getattr(c, "features", {}) or {}),
                            "cuts": list(getattr(c, "cuts", []) or []),
                            "fallback_reason": str(getattr(c, "fallback_reason", "") or ""),
                        }
                        for c in candidates
                    ],
                )

            profile_scope = (
                output_profiles.use_profile(job.options.output_platform)
                if job.options.output_platform
                else nullcontext()
            )
            with profile_scope:
                clips = run_pipeline(
                    source_path,
                    job.options,
                    clips_dir=clips_dir,
                    temp_dir=temp_dir,
                    progress_cb=progress,
                    start_progress=start_progress,
                    explicit_candidates=resume_windows,
                    avoid_windows=job.avoid_windows,
                    # Not recorded on a resume: the plan is already stored, and overwriting it with
                    # only the windows being retried would lose the ones already finished.
                    on_plan=None if resume else record_plan,
                )
            if resume:
                clips = existing_clips + clips

            # Persist every created clip: history record, sidecar metadata, and
            # mirror into the active storage backend (same call path for local
            # or S3). For non-local backends the clip URL points at the backend.
            from publishers.history import get_history
            from storage_backends import get_storage
            from storage_backends.retention import write_sidecar

            history = get_history()
            storage = get_storage()
            for clip in clips:
                path = clips_dir / clip.filename
                history.record_clip(job_id, clip, path, job.options.campaign_id)
                self._store_clip(storage, write_sidecar, job_id, clip, clips_dir)

            rendered_clips = list(clips)
            close_stage()
            # I4: a cancel that arrives during the final pass must not be overwritten by the
            # success write below. `cancel()` had already stored CANCELLED, and there was no
            # re-check here, so the terminal state was decided by whichever thread wrote last:
            # a job the user stopped could be reported as COMPLETED, and the UI flipped from
            # "Cancelled" back to "Completed". Two writers for one terminal field is the defect;
            # this makes the worker the only one that gets the last word.
            cancellation.checkpoint(job_id)
            # I4: a cancel that arrives during the final pass must not be overwritten by the
            # success write below. `cancel()` had already stored CANCELLED, and there was no
            # re-check here, so the terminal state was decided by whichever thread wrote last:
            # a job the user stopped could be reported as COMPLETED, and the UI flipped from
            # "Cancelled" back to "Completed". Two writers for one terminal field is the defect;
            # this makes the worker the only one that gets the last word.
            completed = self.store.update(
                job_id,
                clips=clips,
                status=JobStatus.COMPLETED,
                progress=1.0,
                stage=f"Completed - {len(clips)} clip(s)",
                stage_index=len(JOB_STAGES),
                stage_total=len(JOB_STAGES),
                stage_timings=metrics.to_list(),
            )
            if not completed:
                # Cancellation/another worker already owns the terminal state.  In particular,
                # never enter auto-publishing after a completion write was rejected.
                return
            # M5: one line naming where the minutes went. Logged at completion rather than
            # sampled, because the only comparison worth making is between whole renders.
            logger.info("render timings - %s", metrics.summary())

            # Auto mode routes each finished clip through its campaign/platforms.
            if job.options.publish_mode == "auto" and job.options.publish_to:
                from publishers.manager import get_publish_manager

                publisher = get_publish_manager()
                for clip in clips:
                    publisher.submit(
                        job_id=job_id,
                        clip=clip,
                        video_path=clips_dir / clip.filename,
                        platforms=job.options.publish_to,
                        campaign_id=job.options.campaign_id,
                        mode="auto",
                        schedule_at=job.options.schedule_at,
                    )
        except cancellation.Job_Cancelled:
            # I4: caught *before* the generic handler, and deliberately not recorded as a
            # failure. A job the user stopped did not go wrong, and reporting it as failed would
            # both mislead the operator and inflate any failure rate computed from these records.
            close_stage()
            # Any clip that finished before the stop is kept and reported. The files are on
            # disk and mirrored to storage either way, so omitting them from the record would
            # not save the work - it would only hide it, leaving clips the retention sweeper
            # owns and no API response names. A partial result is also the honest answer to
            # "what did I get?" after stopping a ten-clip job at clip seven.
            cancelled_fields: dict[str, Any] = {
                "status": JobStatus.CANCELLED,
                "stage": (
                    f"Cancelled - {len(rendered_clips)} clip(s) finished"
                    if rendered_clips
                    else "Cancelled"
                ),
                "error": None,
                "stage_timings": metrics.to_list(),
            }
            if rendered_clips:
                cancelled_fields["clips"] = rendered_clips
            self.store.update(job_id, **cancelled_fields)
            logger.info("job stopped at a checkpoint after %s", metrics.summary())
        except Exception as exc:  # capture any failure and surface it
            close_stage()
            self.store.update(
                job_id,
                status=JobStatus.FAILED,
                error=str(exc),
                stage="Failed",
                stage_timings=metrics.to_list(),
            )
            # M5: timings for a *failed* render are the most useful rows in a performance
            # report - a stage that reliably burns ninety seconds and then throws would be
            # invisible if only successes were measured.
            logger.warning("job failed after %s", metrics.summary(), exc_info=True)
        finally:
            # Best-effort cleanup of per-job scratch space.
            self._cleanup_temp(job_id)
            # I4: the request has been honoured, so forget it. Left in place it would grow
            # without bound on a long-running instance.
            cancellation.clear(job_id)

    @staticmethod
    def _store_clip(storage, write_sidecar, job_id: str, clip, clips_dir: Path) -> None:
        """Write a sidecar and mirror clip artefacts into the storage backend.

        Uses the same :class:`BaseStorage` interface for local and S3. For local
        storage the files already live at the destination (copies are skipped);
        for S3 they are uploaded and the clip's URLs are repointed at the bucket.
        Best-effort: a storage failure never fails the job.
        """
        try:
            clip_path = clips_dir / clip.filename
            if not clip_path.exists():
                return
            # 1. Sidecar metadata next to the clip (both backends).
            sidecar = write_sidecar(clip_path, clip)
            prefix = f"clips/{job_id}"

            # 2. Mirror media + sidecar via the storage interface.
            storage.save_file(f"{prefix}/{clip.filename}", clip_path)
            storage.save_file(f"{prefix}/{sidecar.name}", sidecar)
            thumb_name = Path(clip.thumbnail_url).name if clip.thumbnail_url else ""
            thumb_path = clips_dir / thumb_name if thumb_name else None
            if thumb_path and thumb_path.exists():
                storage.save_file(f"{prefix}/{thumb_name}", thumb_path)

            # 3. Repoint URLs at non-local backends (S3 presigned URLs).
            if getattr(storage, "name", "local") != "local":
                clip.video_url = storage.url(f"{prefix}/{clip.filename}")
                if thumb_name:
                    clip.thumbnail_url = storage.url(f"{prefix}/{thumb_name}")
        except Exception:
            # Mirroring is best-effort: the clip already exists locally, so a storage
            # failure must not fail the job. But it is *logged* rather than swallowed.
            # Silently, an operator running STORAGE_BACKEND=s3 would see clips finish
            # normally with local URLs while nothing ever reached the bucket — they
            # would believe the clips were backed up when they were not.
            logger.exception(
                "failed to mirror clip %s of job %s to the %s storage backend",
                getattr(clip, "filename", "?"),
                job_id,
                getattr(storage, "name", "unknown"),
            )

    @staticmethod
    def _cleanup_temp(job_id: str) -> None:
        """Remove a job's scratch dir when the auto-delete-temp toggle is on."""
        import shutil

        try:
            from runtime_config import get_runtime_config

            if not get_runtime_config().auto_delete_temp:
                return
        except Exception:
            pass
        temp_dir = Path(settings.temp_dir) / job_id
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


# --- process-wide singleton -------------------------------------------------
_manager: JobManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> JobManager:
    """Return the shared :class:`JobManager` singleton."""
    global _manager
    with _manager_lock:
        if _manager is None:
            # Keep the safe one-worker/100-pending defaults, while allowing a deployment to size
            # the in-process pool and its bounded admission window for the available hardware.
            _manager = JobManager(
                max_workers=settings.job_max_workers,
                max_queue_size=settings.job_queue_size,
            )
        return _manager
