"""In-process job store and background job manager.

Phase 1 runs jobs in a background thread pool with a single worker, so a batch
of videos/links is processed **in line** (sequentially), matching the product
requirement. Progress is tracked live in an in-memory, thread-safe store that
the API polls.

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
from pathlib import Path
from typing import Any

from config import settings
from worker import cancellation, llm_cost, observability, webhook
from worker import download as dl
from worker.models import (
    CANCELLABLE_JOB_STATUSES,
    ClipResult,
    Job,
    JobStatus,
    ProcessingOptions,
)
from worker.pipeline import run_pipeline

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

    def _persist(self, job: Job | None) -> None:
        """Mirror ``job`` into the durable store, if one is configured.

        Swallows every failure. This runs on the hot path of a live render — a progress
        update mid-encode reaches it — so a full disk or a locked database must cost the
        durability of one record, not the job itself. ``Job_Persistence`` is already
        defensive internally; this guard also covers an injected or third-party backend.
        """
        if self._persistence is None or job is None:
            return
        try:
            self._persistence.save(job)
        except Exception:
            logger.exception("failed to persist job %s", getattr(job, "id", "?"))

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
        # Written outside the lock: SQLite has its own locking and holding both would
        # serialise every API poll behind a disk write.
        self._persist(job)

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

    def update(self, job_id: str, **fields) -> None:
        """Atomically update fields on a stored job."""
        import time

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = time.time()
        self._persist(job)

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
        # Clip metadata edits are user-visible and must survive a restart just as job
        # state does, so this mirrors too.
        self._persist(job)
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


class JobManager:
    """Owns the worker pool and drives jobs through the pipeline."""

    def __init__(self, store: JobStore | None = None, max_workers: int = 1) -> None:
        self.store = store or JobStore()
        # A single worker => batch items processed one after another, in order.
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    # -- submission --------------------------------------------------------

    def submit(
        self,
        input_type: str,
        source: str,
        options: ProcessingOptions,
        batch_id: str | None = None,
        title: str = "",
    ) -> Job:
        """Create a job, store it as queued, and schedule it for processing."""
        job = Job(
            input_type=input_type,
            source=source,
            options=options,
            batch_id=batch_id,
            title=title or (Path(source).name if input_type == "file" else source),
        )
        self.store.add(job)
        # I4: clear any stale request under this id before the work is scheduled. Ids are short
        # hex and a collision is unlikely rather than impossible, and inheriting a previous
        # job's cancellation would stop a brand-new job before it started.
        cancellation.clear(job.id)
        # Phase 7: and for the same reason, do not inherit a previous job's token spend. That
        # failure would be quieter than the cancellation one - the new job would simply be
        # billed for work it did not do, and the number would look ordinary.
        llm_cost.clear_usage(job.id)
        self._executor.submit(self._run, job.id)
        return job

    # -- cancellation (I4) -------------------------------------------------

    def cancel(self, job_id: str) -> bool:
        """Ask a job to stop, returning whether it was in a cancellable state.

        The two cases genuinely differ, and Phase 7 stopped flattening them:

        * A **queued** job is marked ``CANCELLED`` immediately. No worker has claimed it, so
          there is nothing to wait for and reporting it as "cancelling" would be a lie.
        * A **processing** job is marked ``CANCELLING``. The worker stops at its next
          checkpoint, and a job already inside an ffmpeg pass finishes that pass first - so the
          honest answer is "stopping", and it becomes ``CANCELLED`` when the worker actually
          unwinds (see the ``Job_Cancelled`` handler in ``_execute``).

          This used to write ``CANCELLED`` here, with the *API response* carrying the word
          "cancelling". That put the truth in the transient half: the record said cancelled, the
          UI re-read it a moment later, and the job appeared to have stopped while it was still
          rendering.

        A finished job - completed, failed, already cancelled, or already cancelling - returns
        ``False`` rather than raising: cancelling something that has already stopped or is
        already stopping is a harmless no-op, and reporting it as an error would make a
        double-click on the button look like a failure.
        """
        job = self.store.get(job_id)
        if job is None or job.status not in CANCELLABLE_JOB_STATUSES:
            return False
        cancellation.request_cancel(job_id)
        stopping = job.status is JobStatus.PROCESSING
        self.store.update(
            job_id,
            status=JobStatus.CANCELLING if stopping else JobStatus.CANCELLED,
            stage="Cancelling" if stopping else "Cancelled",
            error=None,
        )
        logger.info(
            "job %s %s by request (was %s)",
            job_id,
            "cancelling" if stopping else "cancelled",
            job.status.value,
        )
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
            self.store.update(
                job_id,
                progress=fraction,
                stage=stage,
                status=JobStatus.PROCESSING,
                # U8: which step of how many, so the UI can show structure rather than one bar.
                stage_index=stage_position(stage),
                stage_total=len(JOB_STAGES),
                stage_timings=metrics.to_list(),
                llm_usage=llm_cost.usage_for(job_id).to_dict(),
            )

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

        cancellation.clear(job_id)
        self.store.update(
            job_id,
            status=JobStatus.QUEUED,
            stage="Queued (resuming)",
            error=None,
            progress=0.0,
        )
        self._executor.submit(self._run, job_id, True)
        return True

    def _execute(self, job, job_id, progress, close_stage, metrics, resume: bool = False) -> None:
        """The job body. Split out so ``_run`` is only the context and callback wiring."""
        try:
            # I4: a queued job stops here, before any work begins - no worker has claimed it, so
            # there is nothing to wait for.
            cancellation.checkpoint(job_id)
            self.store.update(
                job_id,
                status=JobStatus.PROCESSING,
                stage="Starting",
                progress=0.0,
                stage_index=1,
                stage_total=len(JOB_STAGES),
            )

            source_path = Path(job.source)
            start_progress = 0.0

            # URL inputs are downloaded first (reserving a slice of progress).
            if job.input_type == "url":

                def dl_progress(frac: float, msg: str) -> None:
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
                        }
                        for c in candidates
                    ],
                )

            clips = run_pipeline(
                source_path,
                job.options,
                clips_dir=clips_dir,
                temp_dir=temp_dir,
                progress_cb=progress,
                start_progress=start_progress,
                explicit_candidates=resume_windows,
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

            close_stage()
            self.store.update(
                job_id,
                clips=clips,
                status=JobStatus.COMPLETED,
                progress=1.0,
                stage=f"Completed - {len(clips)} clip(s)",
                stage_index=len(JOB_STAGES),
                stage_total=len(JOB_STAGES),
                stage_timings=metrics.to_list(),
                llm_usage=llm_cost.usage_for(job_id).to_dict(),
            )
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
            self.store.update(
                job_id,
                status=JobStatus.CANCELLED,
                stage="Cancelled",
                error=None,
                stage_timings=metrics.to_list(),
                llm_usage=llm_cost.usage_for(job_id).to_dict(),
            )
            logger.info("job stopped at a checkpoint after %s", metrics.summary())
        except Exception as exc:  # capture any failure and surface it
            close_stage()
            self.store.update(
                job_id,
                status=JobStatus.FAILED,
                error=str(exc),
                stage="Failed",
                stage_timings=metrics.to_list(),
                llm_usage=llm_cost.usage_for(job_id).to_dict(),
            )
            # M5: timings for a *failed* render are the most useful rows in a performance
            # report - a stage that reliably burns ninety seconds and then throws would be
            # invisible if only successes were measured.
            logger.warning("job failed after %s", metrics.summary(), exc_info=True)
        finally:
            # Best-effort cleanup of per-job scratch space.
            self._cleanup_temp(job_id)
            # Phase 7: notify once, here, because this is the only point every terminal path
            # reaches - completed, cancelled and failed all pass through it exactly once. Hooking
            # the three `store.update` calls instead would be three sites to keep in step, and a
            # fourth outcome added later would silently send nothing.
            #
            # Re-read rather than using `job`: the local was captured before the run and its
            # status, clips and timings are all stale by now. `notify` never raises, so a
            # webhook cannot turn a finished render into a failed one.
            final = self.store.get(job_id)
            if final is not None:
                webhook.notify(final)
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
            _manager = JobManager()
        return _manager
