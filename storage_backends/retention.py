"""Retention, cleanup, disk usage, and sidecar metadata.

Responsibilities:

* **Disk usage** reporting for the UI (with a low-space warning).
* **Sidecar metadata** — a ``<clip>.json`` written next to each clip capturing
  its title/description/hashtags/effects so a downloaded or archived clip is
  self-describing.
* **Retention sweep** — deletes finished clips older than the retention window
  (``0`` / "keep forever" disables it). It **never** touches the uploads
  (source) directory, so original source video is never auto-deleted.
* A lightweight **background sweeper** thread that runs the sweep periodically.

Everything is best-effort and never raises into the request/worker path.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

# Directories the sweeper is allowed to clean. The uploads/source directory is
# deliberately excluded — sources are only ever removed on explicit request.
_CLEANABLE = ("clips", "temp")

#: How long an empty directory must have been untouched before the sweep removes it.
#:
#: The empty-directory branch of :func:`cleanup_expired` used to have no age check at all - unlike
#: the file branch next to it - so it deleted *any* empty directory under ``clips``/``temp`` the
#: moment it saw one, retention window irrelevant. That races every running job.
#: ``run_pipeline`` creates ``storage/clips/<job_id>/`` before it encodes anything, so the directory
#: is legitimately empty for as long as the first clip takes to render; a sweep landing in that
#: window removed a live job's output directory from under ffmpeg, which then failed with
#: "Error opening output ...: No such file or directory" and took the whole job down.
#:
#: A grace period rather than ``cutoff``: a directory's mtime updates when its contents are
#: unlinked, so age-checking against the retention window would never remove the directories this
#: branch exists to tidy - they look freshly modified on the very sweep that emptied them, and
#: would then be 30 days from eligible again. An hour is far longer than any single clip encode and
#: far shorter than the default six-hour sweep interval, so tidying still happens on the next sweep.
_EMPTY_DIR_GRACE_SECONDS = 3600.0


def active_job_ids() -> frozenset[str]:
    """Ids of jobs a worker may still be writing files for.

    Every path this module deletes is named after a job id -- ``storage/clips/<job_id>/`` and
    ``storage/temp/<job_id>/`` -- so knowing which ids are live is what separates tidying from
    sabotage. The empty-directory branch of :func:`cleanup_expired` was hardened against this
    race with a grace period (see :data:`_EMPTY_DIR_GRACE_SECONDS`), but the *file* branch beside
    it and :func:`cleanup_temp` were not, and both can be reached while a render is mid-write:
    the API process runs the render, so the sweeper thread and the endpoint share a filesystem
    with an active job by construction.

    Imported lazily. This module is imported *by* ``worker.jobs``, so a module-level import would
    be circular, and it must also keep working in a process with no job store at all.

    Returns an empty set when the store cannot be reached, which deliberately means "delete
    nothing on liveness grounds". The callers treat an empty set as "no protection available"
    rather than "nothing is running" -- see how each uses it.
    """
    try:
        from worker.jobs import get_manager
        from worker.models import JobStatus

        live = {JobStatus.QUEUED, JobStatus.PROCESSING}
        return frozenset(job.id for job in get_manager().store.all() if job.status in live)
    except Exception:  # pragma: no cover - defensive
        # A store that cannot be read must not stop the sweep entirely, but it also must not
        # licence deleting a live job's workspace. Callers err towards keeping files.
        logger.warning("could not determine which jobs are live; skipping liveness pruning")
        return frozenset()


def _owning_job_id(path: Path, base: Path) -> str:
    """The job id a path under ``base`` belongs to, or ``""``.

    Both cleanable areas are laid out one directory per job, so the first path component below
    the area root is the job id. A file sitting directly in the area root belongs to no job.
    """
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return ""
    return parts[0] if len(parts) > 1 else ""


# --------------------------------------------------------------------------- #
# Disk usage
# --------------------------------------------------------------------------- #
def _dir_size(path: Path) -> int:
    """Total size of every file under ``path``.

    Uses ``os.scandir`` rather than ``Path.rglob`` + ``Path.stat``: on Linux a
    ``DirEntry`` already carries the stat result from the directory read, so this avoids
    one extra syscall per file and skips constructing a ``Path`` object for each entry.
    On a clips directory holding thousands of files that is the difference between a
    noticeable stall and a prompt response.

    Symlinked directories are not followed, so a link into a large tree elsewhere cannot
    make this walk unbounded, and a symlink loop cannot hang it.
    """
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except (OSError, ValueError):
            continue
    return total


#: Cached per-area sizes: ``(computed_at, {area: bytes})``. Guarded by :data:`_AREA_LOCK`.
_AREA_CACHE: tuple[float, dict[str, int]] | None = None
_AREA_LOCK = threading.Lock()


def _area_sizes(*, refresh: bool = False) -> dict[str, int]:
    """Sizes of the clips/uploads/temp areas, cached briefly.

    Every ``/api/storage`` poll used to walk all three directories in full, and the
    storage panel polls. Directory sizes change slowly and are displayed as a rough
    gauge, so serving a value up to ``settings.disk_usage_cache_seconds`` old costs
    nothing and removes a full filesystem walk from a request path.

    Args:
        refresh: Recompute and reseed the cache. Callers that have just changed the
            contents on disk — the cleanup endpoint — must pass this, otherwise they
            would report the sizes from before their own deletions.
    """
    global _AREA_CACHE

    ttl = float(getattr(settings, "disk_usage_cache_seconds", 30.0))
    with _AREA_LOCK:
        cached = _AREA_CACHE
        if not refresh and cached is not None and ttl > 0:
            computed_at, areas = cached
            if (time.time() - computed_at) < ttl:
                return dict(areas)

    # Computed outside the lock: the walk is the slow part, and holding the lock across
    # it would serialise every concurrent poll behind one filesystem traversal. A racing
    # duplicate walk is harmless — both produce the same answer.
    areas = {
        "clips": _dir_size(Path(settings.clips_dir)),
        "uploads": _dir_size(Path(settings.uploads_dir)),
        "temp": _dir_size(Path(settings.temp_dir)),
    }
    with _AREA_LOCK:
        _AREA_CACHE = (time.time(), dict(areas))
    return areas


def invalidate_disk_usage_cache() -> None:
    """Drop the cached area sizes so the next read recomputes them."""
    global _AREA_CACHE
    with _AREA_LOCK:
        _AREA_CACHE = None


def disk_usage(
    warn_free_gb: float | None = None, warn_percent: float | None = None, *, refresh: bool = False
) -> dict[str, Any]:
    """Return disk usage for the storage volume plus per-area sizes.

    ``low_space`` is ``True`` when free space drops below ``warn_free_gb`` **or**
    used percentage exceeds ``warn_percent``.

    The volume figures come from ``shutil.disk_usage`` and are always current — they are
    a single cheap syscall. The per-area sizes require a directory walk and are cached;
    pass ``refresh=True`` after changing what is on disk.
    """
    warn_free_gb = settings.disk_warn_free_gb if warn_free_gb is None else warn_free_gb
    warn_percent = settings.disk_warn_percent if warn_percent is None else warn_percent

    root = Path(settings.storage_root)
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(root)
    used_percent = (usage.used / usage.total * 100) if usage.total else 0.0
    free_gb = usage.free / (1024**3)

    areas = _area_sizes(refresh=refresh)
    low_space = free_gb < warn_free_gb or used_percent >= warn_percent
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round(used_percent, 1),
        "free_gb": round(free_gb, 2),
        "areas": areas,
        "storage_bytes": sum(areas.values()),
        "low_space": low_space,
        "warn_free_gb": warn_free_gb,
        "warn_percent": warn_percent,
    }


# --------------------------------------------------------------------------- #
# Sidecar metadata
# --------------------------------------------------------------------------- #
def sidecar_path(clip_path: str | Path) -> Path:
    """Return the sidecar JSON path for a clip file (``clip.mp4`` -> ``clip.json``)."""
    p = Path(clip_path)
    return p.with_suffix(".json")


def write_sidecar(clip_path: str | Path, clip: Any, extra: dict | None = None) -> Path:
    """Write a ``<clip>.json`` sidecar describing ``clip`` next to the media.

    ``clip`` may be a dataclass (``ClipResult``), an object with ``to_dict``, or
    a plain dict.
    """
    if is_dataclass(clip) and not isinstance(clip, type):
        data = asdict(clip)
    elif hasattr(clip, "to_dict"):
        data = clip.to_dict()
    elif isinstance(clip, dict):
        data = dict(clip)
    else:
        data = {"value": str(clip)}
    if extra:
        data.update(extra)
    data.setdefault("saved_at", time.time())

    dest = sidecar_path(clip_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return dest


# --------------------------------------------------------------------------- #
# Retention sweep
# --------------------------------------------------------------------------- #
def cleanup_expired(retention_days: int | None = None, now: float | None = None) -> dict[str, Any]:
    """Delete clip artefacts older than the retention window.

    Args:
        retention_days: overrides the configured window. ``0`` (or negative)
            means *keep forever* — the sweep is a no-op.
        now: current epoch time (injectable for tests).

    Returns a summary ``{removed, freed_bytes, retention_days, kept_forever}``.
    Only the ``clips``/``temp`` areas are considered — sources are never touched.
    """
    from runtime_config import get_runtime_config

    if retention_days is None:
        retention_days = get_runtime_config().retention_days
    now = now if now is not None else time.time()

    if not retention_days or retention_days <= 0:
        return {"removed": 0, "freed_bytes": 0, "retention_days": 0, "kept_forever": True}

    cutoff = now - retention_days * 86400
    removed = 0
    freed = 0
    skipped_live = 0
    root = Path(settings.storage_root)
    # Resolved once: every candidate is checked against this to refuse deletions that leave the
    # storage root via a symlink (see the `is_symlink` guard below).
    root_resolved = root.resolve()
    live = active_job_ids()

    for area in _CLEANABLE:
        base = root / area
        if not base.exists():
            continue
        for path in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                # A job still being written to is off limits regardless of mtime. Resuming a job
                # re-reads cached intermediates that can legitimately be older than the retention
                # window (worker/intermediate_cache.py), so age is not evidence that a file is
                # finished with - only the job's status is.
                owner = _owning_job_id(path, base)
                if owner and owner in live:
                    skipped_live += 1
                    continue
                # `rglob` does not filter symlinks, so a link under storage/clips pointed
                # anywhere on the host was followed and `unlink`ed on mtime alone - deleting
                # outside the storage root, which nothing here is allowed to do. `_dir_size`
                # already refuses to follow links and documents why; this is the same rule on
                # the destructive path, where it matters more. The link itself is left alone
                # rather than removed: it is not ours to reason about.
                if path.is_symlink():
                    continue
                if path.is_file() and path.stat().st_mtime < cutoff:
                    # Belt and braces for a link *inside* a resolved parent chain: confirm the
                    # real file is still under the storage root before unlinking it.
                    if root_resolved not in path.resolve().parents:
                        continue
                    size = path.stat().st_size
                    path.unlink()
                    removed += 1
                    freed += size
                elif (
                    path.is_dir()
                    and now - path.stat().st_mtime >= _EMPTY_DIR_GRACE_SECONDS
                    and not any(path.iterdir())
                ):
                    path.rmdir()
            except OSError:
                continue

    if skipped_live:
        logger.info(
            "retention sweep kept %d path(s) belonging to %d running job(s)",
            skipped_live,
            len(live),
        )
    return {
        "removed": removed,
        "freed_bytes": freed,
        "retention_days": retention_days,
        "kept_forever": False,
        # Reported rather than merely logged: an operator who expected a sweep to free space
        # needs to be able to tell "nothing was old enough" from "it was all in use".
        "skipped_active": skipped_live,
    }


def cleanup_temp(job_id: str | None = None, *, skip_active: bool = True) -> int:
    """Remove scratch files. With ``job_id`` remove only that job's temp dir.

    Returns the number of top-level entries removed. Honours the
    ``auto_delete_temp`` runtime toggle when called without a specific job is
    left to the caller; this helper always performs the deletion it is asked to.

    ``skip_active`` protects the unscoped form from deleting a running job's workspace.
    ``POST /api/storage/cleanup`` defaults ``temp`` to true, so an operator clicking "clean up
    temp files" while a render was in flight used to ``rmtree`` the scratch directory ffmpeg was
    reading from - extracted audio, transcripts, intermediate segments. The render then failed in
    the generic handler with a message naming a missing temp file, which points at neither the
    cause nor the click that caused it.

    The scoped form deliberately ignores ``skip_active``: ``JobManager._cleanup_temp`` calls it in
    a ``finally`` for the job that has just stopped, and that job is still briefly PROCESSING in
    the store. Refusing there would leave every job's scratch space behind forever, which is the
    opposite of the intent - an explicit id is a caller who knows which job they mean.
    """
    temp_root = Path(settings.temp_dir)
    target = temp_root / job_id if job_id else temp_root
    if not target.exists():
        return 0
    if job_id:
        shutil.rmtree(target, ignore_errors=True)
        return 1
    live = active_job_ids() if skip_active else frozenset()
    count = 0
    for child in target.iterdir():
        try:
            if child.name in live:
                logger.info("keeping temp workspace %s: job %s is still running", child, child.name)
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
            count += 1
        except OSError:
            continue
    return count


class RetentionSweeper:
    """Background thread that periodically runs :func:`cleanup_expired`."""

    def __init__(self, interval_hours: float | None = None) -> None:
        self.interval = (
            interval_hours if interval_hours is not None else settings.retention_sweep_hours
        ) * 3600
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: dict[str, Any] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="retention-sweeper")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        # Run one sweep shortly after startup, then on the configured interval.
        while not self._stop.wait(5):
            try:
                self.last_result = cleanup_expired()
            except Exception:
                # Logged, not swallowed. A sweep that raises every cycle - an unmounted storage
                # root, a permission change - meant retention silently never ran: the disk filled
                # up, `last_result` stayed `{}`, and `GET /api/storage` went on reporting healthy
                # usage numbers with no error field anywhere. Every other best-effort site in this
                # codebase uses `logger.exception`; this one was the outlier.
                #
                # `last_result` records the failure too, so the state is visible through the API
                # and not only to whoever reads the log.
                logger.exception("retention sweep failed; disk usage will keep growing")
                self.last_result = {"error": "the last retention sweep failed - see the log"}
            if self._stop.wait(max(60.0, self.interval)):
                break


_sweeper: RetentionSweeper | None = None
_sweeper_lock = threading.Lock()


def get_sweeper() -> RetentionSweeper:
    """Return the shared retention sweeper singleton."""
    global _sweeper
    with _sweeper_lock:
        if _sweeper is None:
            _sweeper = RetentionSweeper()
        return _sweeper
