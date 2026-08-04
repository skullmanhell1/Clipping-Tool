"""Clip metadata editing, review state, transcripts, re-render and regeneration.

Every route here carries the ``metadata`` tag.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from api.routers._models import (
    BatchReviewModel,
    CaptionPreviewModel,
    ClipEditModel,
    ClipReviewModel,
    RegenerateRequest,
    RerenderRequest,
)
from api.routers._shared import _llm_available_safe
from api.security import rate_limit
from config import settings
from publishers.history import get_history
from worker.jobs import get_manager
from worker.metadata import REGENERATABLE_FIELDS, regenerate_field

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Clip metadata editing + per-field regeneration
# ---------------------------------------------------------------------------
@router.patch("/api/jobs/{job_id}/clips/{clip_id}", tags=["metadata"])
def edit_clip(job_id: str, clip_id: str, edit: ClipEditModel) -> dict:
    """Update editable metadata fields on a clip (title, hashtags, hook, ...)."""
    fields = {k: v for k, v in edit.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    clip = get_manager().store.update_clip(job_id, clip_id, fields)
    if clip is None:
        raise HTTPException(status_code=404, detail="Job or clip not found")
    get_history().sync_clip(job_id, clip)
    return clip.to_dict()


@router.post("/api/captions/preview", tags=["metadata"], dependencies=[Depends(rate_limit)])
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
    manager = get_manager()
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
        raise HTTPException(
            status_code=404, detail=f"No such clip(s): {', '.join(missing)}"
        )
    # A partial result is reported rather than raised: the point of a batch action is to get
    # through a list, and failing the whole call because one clip has since been deleted would
    # discard the decisions the user made about all the others.
    return updated


@router.post("/api/jobs/{job_id}/clips/{clip_id}/review", tags=["metadata"])
def review_clip(job_id: str, clip_id: str, req: ClipReviewModel) -> dict:
    """Approve, reject or reset one clip (U9)."""
    updated = _set_review(job_id, [clip_id], req.review_state, req.review_note)
    return updated[0]


@router.post("/api/jobs/{job_id}/clips/review", tags=["metadata"])
def review_clips(job_id: str, req: BatchReviewModel) -> dict:
    """Approve or reject many clips of one job in a single call (U9).

    A job produces up to ten clips and each had to be judged individually with nowhere to record
    the verdict, so an interrupted review pass had to be redone from the top.
    """
    if not req.clip_ids:
        raise HTTPException(status_code=400, detail="clip_ids must not be empty")
    updated = _set_review(job_id, req.clip_ids, req.review_state, req.review_note)
    return {"updated": updated, "count": len(updated)}


@router.get("/api/jobs/{job_id}/clips/{clip_id}/transcript", tags=["metadata"])
def clip_transcript(job_id: str, clip_id: str) -> dict:
    """Word-level timings for one rendered clip, for the transcript editor (U4).

    Deliberately **not** rate limited, following the rule set when the limiter was added: the
    eight expensive routes are throttled and reads are not, because the UI polls. This is a
    cache read behind a click, and a budget on it would throttle the shipped editor rather
    than an abuser. It is still authenticated - the app-level dependency covers every route,
    which is why that was chosen over per-decorator wiring.

    Read-only and cheap: the words come from the T8 transcript cache entry the render itself
    consumed, so they are the words that were burned in, and no ASR runs. A miss is a **409**
    rather than an empty list, because "this clip has no words" and "I cannot tell you this
    clip's words" call for completely different things from the UI - the first should offer
    nothing to edit, the second should say why.

    Offsets are clip-relative, which is the frame a cut list must be expressed in. A clip
    already tightened by filler removal is the one case where these do not line up with the
    rendered media, and it is reported rather than papered over: see ``trimmed``.
    """
    manager = get_manager()
    job = manager.store.get(job_id)
    clip = manager.store.get_clip(job_id, clip_id)
    if job is None or clip is None:
        raise HTTPException(status_code=404, detail="Job or clip not found")

    from worker import clip_transcript as ct
    from worker import rerender as rerender_module

    try:
        source = rerender_module.resolve_source(job)
    except rerender_module.RerenderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    options = job.options
    try:
        words = ct.words_for_clip(
            source,
            float(clip.start),
            float(clip.end),
            language=getattr(options, "language", None) or None,
            translate=bool(getattr(options, "translate", False)),
            vocabulary=getattr(options, "vocabulary", "") or "",
        )
    except ct.TranscriptUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Whether the rendered media still matches these offsets. Filler removal (and a previous
    # U4 trim) concatenated the clip, so word timings drawn from the source window are ahead
    # of the media by the removed duration. Reported instead of corrected because the removed
    # regions are not recorded on the clip, so there is nothing to correct *with* - and a
    # silently misaligned editor would have the user striking the wrong words.
    #
    # Compared for equality against the applied marker, not by prefix: a *refused* trim
    # records `transcript_trim_refused:<reason>`, which shares the prefix but means the media
    # was left alone - so a prefix test would report a clip as trimmed precisely when the trim
    # did not happen.
    trim_mod = _trim_module()
    effects = list(getattr(clip, "effects_applied", None) or [])
    trimmed = any(marker in ("filler_removal", trim_mod.MARKER) for marker in effects)
    return {
        "job_id": job_id,
        "clip_id": clip_id,
        "start": float(clip.start),
        "end": float(clip.end),
        "duration": round(float(clip.end) - float(clip.start), 3),
        "trimmed": trimmed,
        "max_cuts": trim_mod.MAX_CUTS,
        "words": [
            {
                "start": round(float(w.start), 3),
                "end": round(float(w.end), 3),
                "text": w.text,
                "probability": round(float(getattr(w, "probability", 1.0)), 4),
            }
            for w in words
        ],
    }


def _trim_module():
    """The U4 trim module, imported lazily to keep the module import graph flat."""
    from worker import transcript_trim

    return transcript_trim


@router.post(
    "/api/jobs/{job_id}/clips/{clip_id}/rerender",
    tags=["metadata"],
    dependencies=[Depends(rate_limit)],
)
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
    manager = get_manager()
    job = manager.store.get(job_id)
    clip = manager.store.get_clip(job_id, clip_id)
    if job is None or clip is None:
        raise HTTPException(status_code=404, detail="Job or clip not found")

    from worker import rerender as rerender_module
    from worker import transcript_trim as trim

    # U4: refuse an oversized cut list here, with a status and a message, rather than letting
    # the pipeline decline it into a marker the caller has to go looking for. The request is
    # the thing that is wrong, and the caller is a UI waiting on this response.
    if len(req.cuts) > trim.MAX_CUTS:
        raise HTTPException(
            status_code=422,
            detail=f"Too many cuts: {len(req.cuts)} (limit {trim.MAX_CUTS}). Each cut adds a "
                   "pair of filters to the render graph.",
        )

    try:
        updated = rerender_module.rerender_clip(
            job, clip, option_overrides=req.settings or None,
            cuts=[(c.start, c.end) for c in req.cuts],
        )
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


@router.post("/api/jobs/{job_id}/clips/{clip_id}/regenerate", tags=["metadata"], dependencies=[Depends(rate_limit)])
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
    manager = get_manager()
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

    # `clip` was already resolved non-None above and nothing removes a clip from a job, so the
    # update cannot miss. Falling back to it keeps the expression total without inventing an
    # error path for a state the store cannot reach.
    updated = manager.store.update_clip(job_id, clip_id, {req.field: value}) or clip
    get_history().sync_clip(job_id, updated)
    return {"field": req.field, "value": value, "clip": updated.to_dict()}
