"""Task orchestration entrypoints.

Phase 1 executes jobs in-process via :class:`worker.jobs.JobManager` (a
background thread pool). This module is the stable entrypoint the API imports;
a Redis + RQ backend can later be introduced behind :func:`get_manager` without
changing callers.
"""

from __future__ import annotations

from worker.jobs import JobManager, JobStore, get_manager
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions
from worker.pipeline import run_pipeline

__all__ = [
    "JobManager",
    "JobStore",
    "get_manager",
    "run_pipeline",
    "Job",
    "JobStatus",
    "ClipResult",
    "ProcessingOptions",
]
