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

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from config import settings
from worker import download as dl
from worker.models import Job, JobStatus, ProcessingOptions
from worker.pipeline import run_pipeline

# Fraction of total progress reserved for downloading a URL before processing.
_DOWNLOAD_BUDGET = 0.10


class JobStore:
    """Thread-safe in-memory store of :class:`Job` objects."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

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

            self.store.update(
                job_id,
                clips=clips,
                status=JobStatus.COMPLETED,
                progress=1.0,
                stage=f"Completed - {len(clips)} clip(s)",
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
    def _cleanup_temp(job_id: str) -> None:
        import shutil

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
