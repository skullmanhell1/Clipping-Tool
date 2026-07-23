"""Task orchestration for the clipping pipeline.

Defines the high-level job that ties together the individual pipeline stages
(download/ingest -> transcribe -> select -> cut -> reframe -> caption ->
metadata -> optional publish). Uses Redis + RQ when available, falling back to
running in-process (see :data:`config.settings.use_inprocess_fallback`).

STUB ONLY: no stage is implemented yet. Later phases will flesh these out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from config import settings


@dataclass
class ClipJob:
    """Represents a single end-to-end clipping job.

    Attributes:
        source: Path or URL of the source video.
        options: Free-form processing options (aspect ratio, captions, etc.).
        job_id: Assigned by the queue once enqueued.
    """

    source: str
    options: dict[str, Any] = field(default_factory=dict)
    job_id: Optional[str] = None


def get_queue():
    """Return an RQ queue, or ``None`` to signal in-process execution.

    TODO(phase-queue): connect to Redis via ``settings.redis_url`` and return an
    ``rq.Queue``. On connection failure, honour ``use_inprocess_fallback``.
    """
    raise NotImplementedError("Queue wiring is implemented in a later phase.")


def enqueue_clip_job(job: ClipJob) -> str:
    """Enqueue a :class:`ClipJob` for processing and return its job id.

    TODO(phase-queue): push onto RQ or run synchronously as a fallback.
    """
    raise NotImplementedError("Job enqueue is implemented in a later phase.")


def process_clip_job(job: ClipJob) -> dict[str, Any]:
    """Run the full pipeline for a single job (the RQ worker entrypoint).

    TODO(phase-pipeline): orchestrate transcribe -> select -> cut -> reframe ->
    caption -> metadata -> publish, writing artefacts via the storage backend.
    """
    raise NotImplementedError("Pipeline orchestration is implemented later.")
