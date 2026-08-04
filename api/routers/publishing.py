"""Publishing, campaigns, scheduling, and history routes.

Campaigns, the schedule window and suggestions, the publish-attempt lifecycle and
the history feed are all tagged ``publishing`` and all read or write the same two
singletons (``get_history`` and ``get_publish_manager``), so they share a module.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from api.routers._models import CampaignModel, PublishClipRequest, RescheduleModel
from config import settings
from publishers import best_times
from publishers.base import PublishState
from publishers.history import get_history
from publishers.manager import get_publish_manager
from worker.jobs import get_manager

router = APIRouter()


# ---------------------------------------------------------------------------
# Publishing, campaigns, scheduling, and history
# ---------------------------------------------------------------------------
@router.get("/api/publishers", tags=["publishing"])
def publisher_statuses() -> dict:
    return {"platforms": get_publish_manager().statuses()}


@router.get("/api/campaigns", tags=["publishing"])
def list_campaigns() -> dict:
    return {"campaigns": [c.to_dict() for c in get_history().campaigns()]}


@router.post("/api/campaigns", tags=["publishing"])
def save_campaign(req: CampaignModel) -> dict:
    if not req.name.strip() or not req.routes:
        raise HTTPException(status_code=400, detail="Campaign name and routes are required")
    return get_history().save_campaign(req.name.strip(), req.routes, req.id).to_dict()


@router.post("/api/jobs/{job_id}/clips/{clip_id}/publish", tags=["publishing"])
def publish_clip(job_id: str, clip_id: str, req: PublishClipRequest) -> dict:
    manager = get_manager()
    job = manager.store.get(job_id)
    clip = manager.store.get_clip(job_id, clip_id)
    if job is None or clip is None:
        raise HTTPException(status_code=404, detail="Job or clip not found")
    if req.mode not in ("auto","review"):
        raise HTTPException(status_code=400, detail="mode must be auto or review")
    path=Path(settings.clips_dir)/job_id/clip.filename
    ids=get_publish_manager().submit(job_id=job_id,clip=clip,video_path=path,
      platforms=req.platforms,campaign_id=req.campaign_id,mode=req.mode,
      schedule_at=req.schedule_at,route_overrides=req.routes)
    if not ids:
        raise HTTPException(status_code=400, detail="No valid publishing routes")
    return {"attempt_ids":ids,"attempts":[get_history().get_attempt(i) for i in ids]}


@router.get("/api/history", tags=["publishing"])
def history(limit: int=200, platform: str="") -> dict:
    return get_history().history(max(1,min(limit,500)),platform)


@router.get("/api/publish-attempts/{attempt_id}", tags=["publishing"])
def publish_attempt(attempt_id: str) -> dict:
    item=get_history().get_attempt(attempt_id)
    if not item:
        raise HTTPException(status_code=404, detail="Publish attempt not found")
    return item


#: States a publish attempt can be moved out of. ``review_required`` is awaiting a
#: human decision and ``failed`` is terminal-but-retryable; anything else is either
#: already progressing (``queued``/``scheduled``/``uploading``) or finished
#: (``published``/``private``/``draft``), and re-queueing those risks a double post.
RESUMABLE_PUBLISH_STATES = frozenset(
    {PublishState.REVIEW_REQUIRED.value, PublishState.FAILED.value}
)


def _resume_attempt(attempt_id: str, *, force_direct: bool) -> dict:
    """Move a stalled publish attempt back into the scheduler's queue.

    Shared by ``/approve`` and ``/retry``. The only difference between them is
    ``force_direct``: approving is an explicit instruction to publish for real, so it
    rewrites the stored request to ``mode="auto"``, whereas a retry re-runs the attempt
    exactly as it was first submitted.

    Raises:
        HTTPException: 404 when the attempt is unknown, 409 when its state is not
            resumable or when the platform cannot honour the request.
    """
    store = get_history()
    item = store.get_attempt(attempt_id)
    if not item:
        raise HTTPException(status_code=404, detail="Publish attempt not found")

    state = str(item.get("state") or "")
    if state not in RESUMABLE_PUBLISH_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"Attempt is {state!r}; only {sorted(RESUMABLE_PUBLISH_STATES)} can be resumed",
        )

    platform = str(item.get("platform") or "")
    manager = get_publish_manager()
    publisher = manager.publishers.get(platform)
    if publisher is None:
        raise HTTPException(status_code=409, detail=f"Unknown platform {platform!r}")

    request = dict(item.get("request_json") or {})
    if force_direct:
        # Without this the publisher re-reads mode="review" and returns
        # review_required again — the attempt would bounce between the queue and
        # review forever, looking like a scheduler bug rather than a missing
        # permission.
        request["mode"] = "auto"

    status = publisher.status(str(request.get("account_id") or ""))
    if not status.configured:
        raise HTTPException(
            status_code=409, detail=f"{platform} is not configured: {status.message}"
        )
    if force_direct and not status.direct_publish:
        # Approving cannot bypass a platform-side permission. Refusing here — with the
        # platform's own explanation — tells the operator *why* nothing will happen,
        # instead of accepting the approval and silently reproducing review_required.
        raise HTTPException(
            status_code=409,
            detail=(
                f"{platform} cannot publish directly yet, so approval cannot proceed: "
                f"{status.message}"
            ),
        )

    # A clip that has since been cleaned up cannot be republished, and finding that out
    # now is far better than a "file no longer exists" failure minutes later.
    video_path = Path(str(request.get("video_path") or ""))
    if not video_path.is_file():
        raise HTTPException(
            status_code=409, detail=f"Clip file no longer exists: {video_path}"
        )

    store.update_attempt(
        attempt_id,
        state=PublishState.QUEUED.value,
        scheduled_at=time.time(),
        request_json=request,
        # The previous attempt's outcome is cleared so the record describes the run in
        # flight rather than a mix of old and new.
        started_at=None,
        completed_at=None,
        error="",
        message="",
    )
    return store.get_attempt(attempt_id) or {}


@router.post("/api/publish-attempts/{attempt_id}/approve", tags=["publishing"])
def approve_publish_attempt(attempt_id: str) -> dict:
    """Approve a ``review_required`` attempt and queue it for direct publishing.

    Three of the five publishers can return ``review_required`` — Instagram and X when
    the account lacks direct-publish approval, Whop when the upload could not be
    attached to a target — and before this endpoint existed there was no way to act on
    it, so such attempts stopped permanently.
    """
    return _resume_attempt(attempt_id, force_direct=True)


#: States whose scheduled time can still be changed (PB7).
#:
#: An attempt that is uploading or finished has no future to move. ``failed`` is excluded too:
#: rescheduling a failure would look like a retry while skipping every check ``/retry`` performs.
RESCHEDULABLE_PUBLISH_STATES = frozenset(
    {PublishState.QUEUED.value, PublishState.SCHEDULED.value}
)


@router.get("/api/schedule", tags=["publishing"])
def schedule_window(start: Optional[float] = None, end: Optional[float] = None) -> dict:
    """Publish attempts scheduled within a window, for the calendar view (PB7).

    Defaults to the 30 days around now. Returns every state, not just pending ones: a calendar
    that hid what had already gone out would show an operator an empty week they had in fact
    filled, and "what did I post on Tuesday" is the same question as "what am I posting Thursday".
    """
    now = time.time()
    begin = float(start) if start is not None else now - 30 * 86400
    finish = float(end) if end is not None else now + 30 * 86400
    if finish < begin:
        raise HTTPException(status_code=400, detail="end must not be before start")
    return {
        "start": begin,
        "end": finish,
        "attempts": get_history().scheduled_between(begin, finish),
    }


@router.get("/api/schedule/suggestions", tags=["publishing"])
def schedule_suggestions(platform: str = "", days: int = 7, per_day: int = 2) -> dict:
    """Suggested posting times for a platform (PB7).

    The response carries ``basis`` describing where the numbers come from, and it is not
    flattering: these are published third-party heuristics, not measurements of this account's
    audience. Per-account timing needs post-publish engagement data (PB8), which is not collected
    yet, and a UI that presented a guess as an analysis would be the actual harm here.
    """
    horizon = max(1, min(int(days), 30))
    each = max(1, min(int(per_day), 6))
    now = time.time()
    taken = [
        float(a["scheduled_at"])
        for a in get_history().scheduled_between(now, now + horizon * 86400)
        if a.get("scheduled_at")
        and (not platform or a.get("platform") == platform)
    ]
    found = best_times.suggest(
        platform, days=horizon, per_day=each, now=now, taken=taken
    )
    return {
        "platform": platform,
        "basis": best_times.BASIS,
        "suggestions": [s.to_dict() for s in found],
    }


@router.patch("/api/publish-attempts/{attempt_id}/schedule", tags=["publishing"])
def reschedule_publish_attempt(attempt_id: str, req: RescheduleModel) -> dict:
    """Move a pending attempt to a different time (PB7).

    Before this, a scheduled post could not be moved at all: the time was fixed when the attempt
    was created, and the only recourse was to let it publish or leave it stuck.
    """
    store = get_history()
    item = store.get_attempt(attempt_id)
    if not item:
        raise HTTPException(status_code=404, detail="Publish attempt not found")
    state = str(item.get("state") or "")
    if state not in RESCHEDULABLE_PUBLISH_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"Attempt is {state!r}; only "
                   f"{sorted(RESCHEDULABLE_PUBLISH_STATES)} can be rescheduled",
        )
    when = float(req.schedule_at)
    # A time in the past means "publish now", which is a legitimate request, but it must be
    # recorded as `queued` rather than left `scheduled` in the past - the scheduler treats both as
    # due, and a state that disagrees with the clock is what makes a queue hard to reason about.
    state_now = (
        PublishState.SCHEDULED.value if when > time.time() + 1
        else PublishState.QUEUED.value
    )
    store.update_attempt(attempt_id, scheduled_at=when, state=state_now)
    return store.get_attempt(attempt_id) or {}


@router.post("/api/publish-attempts/{attempt_id}/cancel", tags=["publishing"])
def cancel_publish_attempt(attempt_id: str) -> dict:
    """Cancel a pending attempt so it never publishes (PB7).

    Recorded as ``failed`` with an explicit message rather than deleted. The attempt is part of
    the audit trail - somebody chose to schedule it - and a row that vanishes is
    indistinguishable from one that never existed when a post is later found missing.
    """
    store = get_history()
    item = store.get_attempt(attempt_id)
    if not item:
        raise HTTPException(status_code=404, detail="Publish attempt not found")
    state = str(item.get("state") or "")
    if state not in RESCHEDULABLE_PUBLISH_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"Attempt is {state!r} and can no longer be cancelled",
        )
    store.update_attempt(
        attempt_id,
        state=PublishState.FAILED.value,
        error="Cancelled before publishing",
        completed_at=time.time(),
    )
    return store.get_attempt(attempt_id) or {}


@router.post("/api/publishers/{platform}/refresh", tags=["publishing"])
def refresh_publisher_credentials(platform: str) -> dict:
    """Force an OAuth token refresh for one platform (PB4).

    Returns ``refreshed: false`` for the four publishers that cannot refresh - TikTok, Instagram
    and X use long-lived tokens an operator pasted in, Whop an API key - rather than pretending to
    act. The status in the response says which kind each is, so the answer is actionable.
    """
    manager = get_publish_manager()
    publisher = manager.publishers.get(platform)
    if publisher is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform {platform!r}")
    refreshed = bool(publisher.refresh_credentials())
    return {
        "platform": platform,
        "refreshed": refreshed,
        "status": publisher.status().to_dict(),
    }


@router.post("/api/publish-attempts/{attempt_id}/retry", tags=["publishing"])
def retry_publish_attempt(attempt_id: str) -> dict:
    """Re-queue a failed (or still-in-review) attempt without changing its mode.

    Separate from ``/approve`` on purpose: a retry is for transient trouble — an expired
    token, a network blip, a clip that was briefly missing — and must not silently
    escalate a review-mode submission into a live post.
    """
    return _resume_attempt(attempt_id, force_direct=False)
