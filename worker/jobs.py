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
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from config import settings
from worker import download as dl
from worker.models import Job, JobStatus, ProcessingOptions
from worker.pipeline import run_pipeline

logger = logging.getLogger(__name__)

# Fraction of total progress reserved for downloading a URL before processing.
_DOWNLOAD_BUDGET = 0.10


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

    def _persist(self, job: Optional[Job]) -> None:
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

    def get(self, job_id: str) -> Optional[Job]:
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

    def update_clip(self, job_id: str, clip_id: str, fields: dict) -> Optional[object]:
        """Atomically update editable fields on one clip within a job.

        Only known :class:`ClipResult` attributes are updated (unknown keys are
        ignored). Returns the updated clip, or ``None`` if not found.
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

    def get_clip(self, job_id: str, clip_id: str) -> Optional[object]:
        """Return a single clip within a job, or ``None``."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return next((c for c in job.clips if c.id == clip_id), None)


class JobManager:
    """Owns the worker pool and drives jobs through the pipeline."""

    def __init__(self, store: Optional[JobStore] = None, max_workers: int = 1) -> None:
        self.store = store or JobStore()
        # A single worker => batch items processed one after another, in order.
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    # -- submission --------------------------------------------------------

    def submit(
        self,
        input_type: str,
        source: str,
        options: ProcessingOptions,
        batch_id: Optional[str] = None,
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
        self._executor.submit(self._run, job.id)
        return job

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

    def _run(self, job_id: str) -> None:
        """Worker entrypoint: ingest then run the pipeline, tracking progress."""
        job = self.store.get(job_id)
        if job is None:
            return

        def progress(fraction: float, stage: str) -> None:
            self.store.update(job_id, progress=fraction, stage=stage,
                              status=JobStatus.PROCESSING)

        try:
            self.store.update(job_id, status=JobStatus.PROCESSING,
                              stage="Starting", progress=0.0)

            source_path = Path(job.source)
            start_progress = 0.0

            # URL inputs are downloaded first (reserving a slice of progress).
            if job.input_type == "url":
                def dl_progress(frac: float, msg: str) -> None:
                    self.store.update(job_id, progress=frac * _DOWNLOAD_BUDGET,
                                      stage=msg, status=JobStatus.PROCESSING)

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

            if not source_path.exists():
                raise FileNotFoundError(f"Source not found: {source_path}")

            # Probe to record duration for the preview card (file inputs).
            if job.duration is None:
                from worker.ffmpeg_utils import probe

                try:
                    job_info = probe(source_path)
                    self.store.update(job_id, duration=job_info.duration)
                except Exception:
                    pass

            clips_dir = Path(settings.clips_dir) / job_id
            temp_dir = Path(settings.temp_dir) / job_id

            clips = run_pipeline(
                source_path,
                job.options,
                clips_dir=clips_dir,
                temp_dir=temp_dir,
                progress_cb=progress,
                start_progress=start_progress,
            )

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

            self.store.update(
                job_id,
                clips=clips,
                status=JobStatus.COMPLETED,
                progress=1.0,
                stage=f"Completed - {len(clips)} clip(s)",
            )

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
        except Exception as exc:  # capture any failure and surface it
            self.store.update(
                job_id,
                status=JobStatus.FAILED,
                error=str(exc),
                stage="Failed",
            )
        finally:
            # Best-effort cleanup of per-job scratch space.
            self._cleanup_temp(job_id)

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
            pass

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
_manager: Optional[JobManager] = None
_manager_lock = threading.Lock()


def get_manager() -> JobManager:
    """Return the shared :class:`JobManager` singleton."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = JobManager()
        return _manager
