"""Fresh-project cleanup shared by job submission and the storage layer."""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from config import settings
from worker.models import JobStatus

logger = logging.getLogger(__name__)

PROJECT_CLEANUP_LOCK = threading.RLock()
_ACTIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.PROCESSING})
_JOB_STORAGE_AREAS = ("clips", "temp")


def _safe_job_id(job_id: str) -> bool:
    """Return whether ``job_id`` is safe to use as one directory component."""
    return bool(job_id) and job_id not in {".", ".."} and "/" not in job_id and "\\" not in job_id


def _remove_job_directory(base: Path, job_id: str) -> bool:
    """Remove one job directory only when it resolves directly under ``base``."""
    if not _safe_job_id(job_id):
        logger.warning("refusing to remove unsafe job storage id %r", job_id)
        return False
    try:
        base_resolved = base.resolve()
        target = base / job_id
        if target.is_symlink() or not target.exists():
            return False
        target_resolved = target.resolve()
        if target_resolved.parent != base_resolved or target_resolved == base_resolved:
            logger.warning("refusing to remove job storage outside %s: %s", base, target)
            return False
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return True
    except OSError:
        logger.exception("could not remove job storage for %s under %s", job_id, base)
        return False


def _remove_local_job_storage(job_ids: Iterable[str]) -> dict[str, int]:
    """Remove local clip/temp directories for the supplied job ids."""
    removed = {area: 0 for area in _JOB_STORAGE_AREAS}
    ids = tuple(job_ids)
    for area in _JOB_STORAGE_AREAS:
        base = Path(getattr(settings, f"{area}_dir"))
        for job_id in ids:
            if _remove_job_directory(base, job_id):
                removed[area] += 1
    return removed


def _remove_remote_clip_storage(job_ids: Iterable[str]) -> int:
    """Best-effort removal of mirrored clip keys for non-local storage backends."""
    try:
        from storage_backends import get_storage

        storage = get_storage()
        if getattr(storage, "name", "local") == "local":
            return 0
        removed = 0
        for job_id in job_ids:
            if not _safe_job_id(job_id):
                continue
            for key in storage.list(f"clips/{job_id}"):
                storage.delete(key)
                removed += 1
        return removed
    except Exception:  # pragma: no cover - optional remote backend failure
        logger.exception("could not remove mirrored project clips")
        return 0


def cleanup_for_new_project(
    job_store: Any,
    history_store: Any,
    *,
    include_active: bool = False,
) -> dict[str, Any]:
    """Remove prior project state while keeping sources and account settings intact.

    ``include_active`` is deliberately opt-in. Normal new submissions remove
    only terminal jobs, so a queued/processing render cannot lose its workspace
    underneath ffmpeg. Callers that explicitly choose to clear active work can
    request it, and the returned ids make that choice visible to the API/UI.
    """
    with PROJECT_CLEANUP_LOCK:
        jobs_result = job_store.clear_for_new_project(include_active=include_active)
        protected = set(jobs_result.get("protected_job_ids", []))
        removed_ids = list(jobs_result.get("removed_job_ids", []))
        try:
            history_result = history_store.clear_for_new_project(
                protected_job_ids=protected,
            )
        except Exception:  # pragma: no cover - defensive for injected stores
            logger.exception("could not clear previous project history")
            history_result = {
                "clips_removed": 0,
                "publish_attempts_removed": 0,
                "error": "history cleanup failed; see server log",
            }
        filesystem = _remove_local_job_storage(removed_ids)
        filesystem["remote_clips_removed"] = _remove_remote_clip_storage(removed_ids)
        return {
            "performed": True,
            "removed_jobs": len(removed_ids),
            "removed_job_ids": removed_ids,
            "protected_active_jobs": sorted(protected),
            "history": history_result,
            "filesystem": filesystem,
            "sources_protected": True,
        }
