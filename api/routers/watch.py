"""The watch folder: status, toggle, and the options new drops are processed with."""

from __future__ import annotations

from fastapi import APIRouter

from api.models import OptionsModel, WatchToggleRequest
from worker.watch_folder import get_watcher

#: Every route below carries the ``watch`` tag, supplied here rather
#: than repeated on each decorator.
router = APIRouter(tags=["watch"])

# ---------------------------------------------------------------------------
# Watch folder
# ---------------------------------------------------------------------------
@router.get("/api/watch")
def watch_status() -> dict:
    return get_watcher().status()


@router.post("/api/watch/toggle")
def watch_toggle(req: WatchToggleRequest) -> dict:
    watcher = get_watcher()
    watcher.set_options(req.options.to_options())
    return watcher.start() if req.enabled else watcher.stop()


@router.post("/api/watch/options")
def watch_options(options: OptionsModel) -> dict:
    watcher = get_watcher()
    watcher.set_options(options.to_options())
    return watcher.status()
