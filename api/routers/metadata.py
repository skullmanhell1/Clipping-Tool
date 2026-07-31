"""Per-clip metadata: editing, review state, caption preview, re-render and regeneration."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from api import deps
from api.capabilities import _llm_available_safe
from api.models import (
    BatchReviewModel,
    CaptionPreviewModel,
    ClipEditModel,
    ClipReviewModel,
    RegenerateRequest,
    RerenderRequest,
)
from api.security import rate_limit
from config import settings
from worker.metadata import REGENERATABLE_FIELDS, regenerate_field

#: Every route below carries the ``metadata`` tag, supplied here rather
#: than repeated on each decorator.
router = APIRouter(tags=["metadata"])

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Clip metadata editing + per-field regeneration
# ---------------------------------------------------------------------------
@router.patch("/api/jobs/{job_id}/clips/{clip_id}")
def edit_clip(job_id: str, clip_id: str, edit: ClipEditModel) -> dict:
    """Update editable metadata fields on a clip (title, hashtags, hook, ...)."""
    fields = {k: v for k, v in edit.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    clip = deps.get_manager().store.update_clip(job_id, clip_id, fields)
    if clip is None:
        raise HTTPException(status_code=404, detail="Job or clip not found")
    deps.get_history().sync_clip(job_id, clip)
    return clip.to_dict()


@router.post("/api/captions/preview", dependencies=[Depends(rate_limit)])
def caption_preview(req: CaptionPreviewModel) -> FileResponse:
    """Render a two-second caption sample for a preset (C18).

    The settings panel's style picker (U5) draws its preview in CSS, which can show the typeface,
    colours, case and placement but *not* the things that distinguish these presets: the word-by-word
    fill, the active-word punch, the per-word pill, the dual stroke, the measured wrapping. Those are
    libass' work, so previewing them honestly means letting libass do it.

    Returns the video inline. Two seconds rather than a still, because a still cannot show a sweep or
    a reveal - which is most of what a preset is.
    """
    from worker import caption_preview as preview_module
    from worker.ffmpeg_utils import ASPECT_PRESETS as ASPECT_CHOICES

    reference: object = req.preset
    if req.overrides:
        # A caller that has already changed the font or colours (U6) wants to preview *that*, not
        # the shipped preset. Merging here rather than making the client send a whole preset keeps
        # the request small and the defaults authoritative.
        from worker.effects.caption_presets import resolve_preset

        base, _ = resolve_preset(req.preset)
        merged = base.to_dict()
        merged.update({k: v for k, v in req.overrides.items() if k in merged})
        reference = merged

    target = Path(settings.temp_dir) / "previews" / f"caption_{uuid.uuid4().hex[:10]}.mp4"
    try:
        preview_module.render_preview(
            reference,
            target,
            text=req.text or preview_module.SAMPLE_TEXT,
            aspect=req.aspect if req.aspect in ASPECT_CHOICES else "9:16",
            position=req.position or None,
        )
    except Exception as exc:
        logger.exception("C18: caption preview failed")
        raise HTTPException(status_code=500, detail=f"Preview failed: {exc}") from exc

    return FileResponse(
        target,
        media_type="video/mp4",
        filename="caption-preview.mp4",
        # The preview is disposable and named with a random id, so nothing benefits from caching it
        # and a stale one would show the previous preset after a settings change.
        headers={"Cache-Control": "no-store"},
    )


#: Review states a clip may be moved to (U9).
REVIEW_STATES = frozenset({"pending", "approved", "rejected"})


def _set_review(job_id: str, clip_ids: list[str], state: str, note: str) -> list[dict]:
    """Apply a review state to several clips of one job. Returns the updated clips."""
    if state not in REVIEW_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"review_state must be one of {sorted(REVIEW_STATES)}",
        )
    manager = deps.get_manager()
    if manager.store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")

    updated: list[dict] = []
    missing: list[str] = []
    for clip_id in clip_ids:
        clip = manager.store.update_clip(
            job_id, clip_id, {"review_state": state, "review_note": note}
        )
        if clip is None:
            missing.append(clip_id)
        else:
            updated.append(clip.to_dict())
    if missing and not updated:
        raise HTTPException(status_code=404, detail=f"No such clip(s): {', '.join(missing)}")
    # A partial result is reported rather than raised: the point of a batch action is to get
    # through a list, and failing the whole call because one clip has since been deleted would
    # discard the decisions the user made about all the others.
    return updated


@router.post("/api/jobs/{job_id}/clips/{clip_id}/review")
def review_clip(job_id: str, clip_id: str, req: ClipReviewModel) -> dict:
    """Approve, reject or reset one clip (U9)."""
    updated = _set_review(job_id, [clip_id], req.review_state, req.review_note)
    return updated[0]


@router.post("/api/jobs/{job_id}/clips/review")
def review_clips(job_id: str, req: BatchReviewModel) -> dict:
    """Approve or reject many clips of one job in a single call (U9).

    A job produces up to ten clips and each had to be judged individually with nowhere to record
    the verdict, so an interrupted review pass had to be redone from the top.
    """
    if not req.clip_ids:
        raise HTTPException(status_code=400, detail="clip_ids must not be empty")
    updated = _set_review(job_id, req.clip_ids, req.review_state, req.review_note)
    return {"updated": updated, "count": len(updated)}


@router.post("/api/jobs/{job_id}/clips/{clip_id}/rerender", dependencies=[Depends(rate_limit)])
def rerender_clip_endpoint(job_id: str, clip_id: str, req: RerenderRequest) -> dict:
    """Re-render one clip, optionally with changed settings (U7).

    Changing one setting previously meant resubmitting the whole source: the download, the
    transcription, the selection call, the metadata generation and every *other* clip. It also
    produced a different set of clips, because selection is not deterministic with an LLM in it.

    This runs synchronously. A re-render is a cut, a geometry pass and a composite of one clip -
    seconds to a minute - and the caller is a user who has just pressed a button and is watching
    for the result. Handing back a job id to poll would be the right shape for a whole-source
    run and the wrong one here.
    """
    manager = deps.get_manager()
    job = manager.store.get(job_id)
    clip = manager.store.get_clip(job_id, clip_id)
    if job is None or clip is None:
        raise HTTPException(status_code=404, detail="Job or clip not found")

    from worker import rerender as rerender_module

    try:
        updated = rerender_module.rerender_clip(job, clip, option_overrides=req.settings or None)
    except rerender_module.RerenderError as exc:
        # 409 rather than 500: the request was well-formed and the state of the world is the
        # problem (a deleted source, most often), which the message names.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("U7: re-render failed for %s/%s", job_id, clip_id)
        raise HTTPException(status_code=500, detail=f"Re-render failed: {exc}") from exc

    fields = {
        name: getattr(updated, name)
        for name in ("duration", "effects_applied", "broll_assets", "start", "end")
    }
    stored = manager.store.update_clip(job_id, clip_id, fields)
    return (stored or updated).to_dict()


@router.post("/api/jobs/{job_id}/clips/{clip_id}/regenerate", dependencies=[Depends(rate_limit)])
def regenerate_clip_field(job_id: str, clip_id: str, req: RegenerateRequest) -> dict:
    """Regenerate a single metadata field for a clip via the LLM.

    Requires an LLM to be configured; returns 400 for unknown fields and 409
    when no LLM is available.
    """
    if req.field not in REGENERATABLE_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"Field must be one of {list(REGENERATABLE_FIELDS)}",
        )
    manager = deps.get_manager()
    job = manager.store.get(job_id)
    clip = manager.store.get_clip(job_id, clip_id)
    if job is None or clip is None:
        raise HTTPException(status_code=404, detail="Job or clip not found")

    if not _llm_available_safe():
        raise HTTPException(
            status_code=409,
            detail="No LLM configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY.",
        )

    # Apply any per-request platform override on top of the job's options.
    options = job.options
    if req.platform:
        from dataclasses import replace

        options = replace(options, platform=req.platform)

    try:
        value = regenerate_field(
            req.field, clip.transcript_text or clip.description or clip.title, options
        )
    except Exception as exc:  # LLMError or parsing issue
        raise HTTPException(status_code=502, detail=f"Regeneration failed: {exc}") from exc

    updated = manager.store.update_clip(job_id, clip_id, {req.field: value})
    if updated is None:  # pragma: no cover - the clip was located immediately above
        raise HTTPException(status_code=404, detail="Job or clip not found")
    deps.get_history().sync_clip(job_id, updated)
    return {"field": req.field, "value": value, "clip": updated.to_dict()}
