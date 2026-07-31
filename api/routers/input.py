"""Clip-selection preview: ``/api/preview``.

Runs selection against a URL without downloading or rendering, so the UI can show what *would* be
cut before committing a job to the queue.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.models import PreviewRequest
from api.security import rate_limit
from worker.download import DownloadError, UnsafeURLError, fetch_metadata, is_url

#: Every route below carries the ``input`` tag, supplied here rather
#: than repeated on each decorator.
router = APIRouter(tags=["input"])


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
@router.post("/api/preview", dependencies=[Depends(rate_limit)])
def preview(req: PreviewRequest) -> dict:
    """Return preview metadata for a URL (title, duration, thumbnail)."""
    if not is_url(req.url):
        raise HTTPException(status_code=400, detail="Not a valid URL")
    try:
        meta = fetch_metadata(req.url)
    except UnsafeURLError as exc:
        # 400, not 422: the URL is well-formed and we simply will not fetch it, which is a
        # problem with the request rather than with the resource. Caught before DownloadError
        # because UnsafeURLError is a subclass of it and except clauses match in order.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DownloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "title": meta.title,
        "duration": meta.duration,
        "thumbnail": meta.thumbnail,
        "source": meta.source,
        "uploader": meta.uploader,
    }
