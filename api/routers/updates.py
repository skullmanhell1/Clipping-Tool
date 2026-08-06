"""Update availability: ``/api/updates``."""

from __future__ import annotations

from fastapi import APIRouter

from updates import get_update_checker

#: Every route below carries the ``updates`` tag, supplied here rather
#: than repeated on each decorator.
router = APIRouter(tags=["updates"])


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------
@router.get("/api/updates")
def check_updates(force: bool = False) -> dict:
    return get_update_checker().check(force=force)
