"""Disk usage, retention settings, cleanup, and source-file deletion."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from api import deps
from api.models import StorageSettingsModel
from config import settings
from runtime_config import RETENTION_CHOICES, get_runtime_store
from storage_backends.retention import cleanup_expired, cleanup_temp, disk_usage

#: Every route below carries the ``storage`` tag, supplied here rather
#: than repeated on each decorator.
router = APIRouter(tags=["storage"])

# ---------------------------------------------------------------------------
# Storage: disk usage, runtime settings, cleanup, and protected source deletion
# ---------------------------------------------------------------------------
def _storage_state() -> dict:
    """Combined disk usage + runtime storage settings + backend name."""
    cfg = get_runtime_store().get()
    return {
        "backend": settings.storage_backend.value,
        "settings": cfg.to_dict(),
        "retention_choices": list(RETENTION_CHOICES),
        "usage": disk_usage(),
    }


@router.get("/api/storage")
def storage_status() -> dict:
    return _storage_state()


@router.post("/api/storage/settings")
def update_storage_settings(req: StorageSettingsModel) -> dict:
    get_runtime_store().update(**{k: v for k, v in req.model_dump().items() if v is not None})
    return _storage_state()


@router.post("/api/storage/cleanup")
def storage_cleanup(temp: bool = True, expired: bool = True) -> dict:
    """Run cleanup now: expired clips (per retention) and/or all temp files."""
    result: dict = {}
    if expired:
        result["expired"] = cleanup_expired()
    if temp:
        result["temp_removed"] = cleanup_temp()
    # refresh=True: this endpoint has just deleted files, so the cached area sizes are
    # stale by construction and would report the pre-cleanup totals.
    result["usage"] = disk_usage(refresh=True)
    return result


@router.delete("/api/jobs/{job_id}/source")
def delete_source(job_id: str, confirm: bool = False) -> dict:
    """Delete a job's original source video. Requires ``confirm=true``.

    Source video is never auto-deleted; this endpoint is the only way to remove
    it, and it refuses to act without explicit confirmation.
    """
    if not confirm:
        raise HTTPException(status_code=400,
                            detail="Deleting the original source requires confirm=true")
    job = deps.get_manager().store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.input_type != "file":
        raise HTTPException(status_code=400,
                            detail="Only uploaded/downloaded source files can be deleted here")
    src = Path(job.source).resolve()
    uploads_root = Path(settings.uploads_dir).resolve()
    if uploads_root not in src.parents:
        raise HTTPException(status_code=400, detail="Source is not in the uploads directory")
    existed = src.is_file()
    if existed:
        src.unlink(missing_ok=True)
    return {"deleted": existed, "source": str(src)}
