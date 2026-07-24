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
import shutil
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

from config import settings

# Directories the sweeper is allowed to clean. The uploads/source directory is
# deliberately excluded — sources are only ever removed on explicit request.
_CLEANABLE = ("clips", "temp")


# --------------------------------------------------------------------------- #
# Disk usage
# --------------------------------------------------------------------------- #
def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def disk_usage(warn_free_gb: Optional[float] = None,
               warn_percent: Optional[float] = None) -> dict[str, Any]:
    """Return disk usage for the storage volume plus per-area sizes.

    ``low_space`` is ``True`` when free space drops below ``warn_free_gb`` **or**
    used percentage exceeds ``warn_percent``.
    """
    warn_free_gb = settings.disk_warn_free_gb if warn_free_gb is None else warn_free_gb
    warn_percent = settings.disk_warn_percent if warn_percent is None else warn_percent

    root = Path(settings.storage_root)
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(root)
    used_percent = (usage.used / usage.total * 100) if usage.total else 0.0
    free_gb = usage.free / (1024 ** 3)

    areas = {
        "clips": _dir_size(Path(settings.clips_dir)),
        "uploads": _dir_size(Path(settings.uploads_dir)),
        "temp": _dir_size(Path(settings.temp_dir)),
    }
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


def write_sidecar(clip_path: str | Path, clip: Any, extra: Optional[dict] = None) -> Path:
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
def cleanup_expired(retention_days: Optional[int] = None,
                    now: Optional[float] = None) -> dict[str, Any]:
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
        return {"removed": 0, "freed_bytes": 0, "retention_days": 0,
                "kept_forever": True}

    cutoff = now - retention_days * 86400
    removed = 0
    freed = 0
    root = Path(settings.storage_root)

    for area in _CLEANABLE:
        base = root / area
        if not base.exists():
            continue
        for path in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    size = path.stat().st_size
                    path.unlink()
                    removed += 1
                    freed += size
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                continue

    return {"removed": removed, "freed_bytes": freed,
            "retention_days": retention_days, "kept_forever": False}


def cleanup_temp(job_id: Optional[str] = None) -> int:
    """Remove scratch files. With ``job_id`` remove only that job's temp dir.

    Returns the number of top-level entries removed. Honours the
    ``auto_delete_temp`` runtime toggle when called without a specific job is
    left to the caller; this helper always performs the deletion it is asked to.
    """
    temp_root = Path(settings.temp_dir)
    target = temp_root / job_id if job_id else temp_root
    if not target.exists():
        return 0
    if job_id:
        shutil.rmtree(target, ignore_errors=True)
        return 1
    count = 0
    for child in target.iterdir():
        try:
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

    def __init__(self, interval_hours: Optional[float] = None) -> None:
        self.interval = (interval_hours if interval_hours is not None
                         else settings.retention_sweep_hours) * 3600
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: dict[str, Any] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="retention-sweeper")
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
                pass
            if self._stop.wait(max(60.0, self.interval)):
                break


_sweeper: Optional[RetentionSweeper] = None
_sweeper_lock = threading.Lock()


def get_sweeper() -> RetentionSweeper:
    """Return the shared retention sweeper singleton."""
    global _sweeper
    with _sweeper_lock:
        if _sweeper is None:
            _sweeper = RetentionSweeper()
        return _sweeper
