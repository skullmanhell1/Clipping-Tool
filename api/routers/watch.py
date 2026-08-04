"""Watch-folder status and control routes."""

from __future__ import annotations

from fastapi import APIRouter

from api.routers._models import OptionsModel, WatchToggleRequest
from worker.watch_folder import get_watcher

router = APIRouter()


# ---------------------------------------------------------------------------
# Watch folder
# ---------------------------------------------------------------------------
@router.get("/api/watch", tags=["watch"])
def watch_status() -> dict:
    return get_watcher().status()


@router.post("/api/watch/toggle", tags=["watch"])
def watch_toggle(req: WatchToggleRequest) -> dict:
    watcher = get_watcher()
    watcher.set_options(req.options.to_options())
    return watcher.start() if req.enabled else watcher.stop()


@router.post("/api/watch/options", tags=["watch"])
def watch_options(options: OptionsModel) -> dict:
    watcher = get_watcher()
    watcher.set_options(options.to_options())
    return watcher.status()
