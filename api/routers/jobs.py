"""Job submission and lifecycle: upload, URL ingest, batches, status, cancel, resume.

The upload handler is the largest single route in the app and deliberately so: it is the only
place untrusted bytes enter the system, and every check it performs (extension, magic bytes,
declared size, real size, ffprobe) has to happen before the file is accepted rather than after.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from api import deps
from api.models import BatchRequest, UrlJobRequest
from api.security import rate_limit
from config import settings
from worker.download import UnsafeURLError, is_url, validate_public_url
from worker.models import ProcessingOptions

#: Every route below carries the ``jobs`` tag, supplied here rather
#: than repeated on each decorator.
router = APIRouter(tags=["jobs"])

# ---------------------------------------------------------------------------
# Job submission
# ---------------------------------------------------------------------------
@router.post("/api/jobs/url", dependencies=[Depends(rate_limit)])
def submit_url(req: UrlJobRequest) -> dict:
    """Submit a single URL for processing."""
    if not is_url(req.url):
        raise HTTPException(status_code=400, detail="Not a valid URL")
    # Rejected at submission as well as at download. `download_video` validates too, so nothing
    # unsafe is fetched either way - but a job that is going to be refused should not first be
    # accepted, queued and reported as running, only to fail minutes later with a security error
    # the submitter cannot act on.
    try:
        validate_public_url(req.url)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = deps.get_manager().submit("url", req.url, req.options.to_options())
    return job.to_dict()


@router.post("/api/jobs/batch", dependencies=[Depends(rate_limit)])
def submit_batch(req: BatchRequest) -> dict:
    """Submit a batch of URLs; they are processed in line (sequentially)."""
    urls = [u for u in req.urls if is_url(u)]
    if not urls:
        raise HTTPException(status_code=400, detail="No valid URLs provided")
    # Unsafe URLs fail the whole batch rather than being filtered out like malformed ones. The
    # existing filter silently drops anything `is_url` rejects, which is defensible for a typo in
    # a pasted list; silently dropping an attempt to reach 169.254.169.254 would hide it instead,
    # and a submitter who included one deserves to be told which.
    for candidate in urls:
        try:
            validate_public_url(candidate)
        except UnsafeURLError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    items = [{"input_type": "url", "source": u} for u in urls]
    batch_id = deps.get_manager().submit_batch(items, req.options.to_options())
    jobs = deps.get_manager().store.by_batch(batch_id)
    return {"batch_id": batch_id, "jobs": [j.to_dict() for j in jobs]}


async def _save_upload(upload_file: UploadFile, uploads_dir: Path) -> dict:
    """Stream one uploaded file to ``uploads_dir``, validating name and size.

    Streamed in chunks with ``await``, not ``shutil.copyfileobj``. The endpoint is
    ``async``, so a synchronous copy blocks the event loop for the whole transfer —
    during a multi-gigabyte upload the server answers nothing at all, including the
    progress polls the UI depends on.

    The size ceiling is enforced *while writing* rather than by trusting a
    ``Content-Length`` header, which a client controls and may omit entirely under
    chunked transfer encoding. A file that exceeds the ceiling is deleted rather than
    left as a truncated partial that ffmpeg would later fail on for a confusing reason.

    Returns:
        The ``{"input_type", "source", "title"}`` record the job manager expects.

    Raises:
        HTTPException: 400 for a disallowed extension, 413 when too large.
    """
    safe_name = Path(upload_file.filename or "upload").name
    suffix = Path(safe_name).suffix.lower()
    allowed = settings.allowed_upload_extensions_set
    if allowed and suffix not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type {suffix or '(none)'!r} for {safe_name!r}. "
                f"Allowed: {', '.join(sorted(allowed))}"
            ),
        )

    limit = int(settings.max_upload_bytes)
    chunk_size = max(1, int(settings.upload_chunk_bytes))
    dest = uploads_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := await upload_file.read(chunk_size):
                written += len(chunk)
                if limit > 0 and written > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"{safe_name!r} exceeds the maximum upload size of "
                            f"{limit} bytes"
                        ),
                    )
                out.write(chunk)
    except BaseException:
        # Covers the size rejection, a disconnect mid-transfer, and a disk error.
        dest.unlink(missing_ok=True)
        raise

    if written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"{safe_name!r} is empty")

    return {"input_type": "file", "source": str(dest), "title": safe_name}


@router.post("/api/upload", dependencies=[Depends(rate_limit)])
async def upload(
    files: list[UploadFile] = File(...),
    language: Optional[str] = Form(None),
    translate: bool = Form(False),
    clip_length: str = Form("auto"),
    aspect: str = Form("9:16"),
    num_clips: str = Form("auto"),
    strategy: str = Form("ai"),
    captions: bool = Form(True),
    subtitle_sidecar: str = Form("false"),
    topic: str = Form(""),
    vocabulary: str = Form(""),
    vibe: str = Form(""),
    platform: str = Form("generic"),
    hashtag_count: int = Form(5),
    range_start: Optional[float] = Form(None),
    range_end: Optional[float] = Form(None),
    metadata: bool = Form(True),
    publish_to: str = Form(""),
    campaign_id: str = Form(""),
    publish_mode: str = Form("review"),
    schedule_at: Optional[float] = Form(None),
    # Phase 4 — visual effects
    reframe: bool = Form(False),
    zoom: bool = Form(False),
    transitions: bool = Form(False),
    hook_title: bool = Form(False),
    music: str = Form(""),
    music_volume: float = Form(0.12),
    fades: bool = Form(False),
    color: str = Form(""),
    progress_bar: bool = Form(False),
    emoji: str = Form("off"),
    emoji_mode: str = Form("keyword"),
    emoji_animate: bool = Form(True),
    filler_removal: bool = Form(False),
    caption_template: str = Form("karaoke"),
    caption_position: str = Form("bottom"),
    # Tier 1 — Feature A: animated caption presets
    caption_preset: str = Form("karaoke"),
    caption_animation: str = Form(""),
    caption_keyword_highlight: bool = Form(False),
    caption_keyword_ai: bool = Form(False),
    caption_emoji: bool = Form(False),
    # Tier 1 — Feature B: b-roll overlays
    broll: bool = Form(False),
    broll_intensity: str = Form("standard"),
    asset_sourcing_mode: str = Form("off"),
    broll_provider: str = Form(""),
    # Tier 1 — Feature C: prompt / visual selection
    selection_prompt: str = Form(""),
    visual_selection: bool = Form(False),
    # Tier 1 — cross-cutting
    permissibility_mode: bool = Form(False),
    # v0.8.0 — Speaker diarisation & multi-speaker reframe
    diarization: bool = Form(False),
    speaker_reframe: bool = Form(False),
    reframe_layout: str = Form("follow_active"),
    reframe_intensity: str = Form("standard"),
    # Kinetic typography engine (default OFF; Reqs 17.4, 17.7).
    #
    # Declared as loose optional strings on purpose: form values arrive as text,
    # and a typed ``bool``/``int``/``float`` parameter would make FastAPI reject
    # an unrecognised payload with 422 — but an unrecognised value must never
    # fail the job, it must fall back to the documented default. ``None`` means
    # "not supplied", so the field keeps its ``ProcessingOptions`` default;
    # anything else is normalised by ``ProcessingOptions.from_dict`` (the flag)
    # or coerced by the engine's ``resolve_options`` (every other field).
    kinetic_typography_enabled: Optional[str] = Form(None),
    kinetic_style: Optional[str] = Form(None),
    kinetic_reveal: Optional[str] = Form(None),
    kinetic_font: Optional[str] = Form(None),
    kinetic_max_lines: Optional[str] = Form(None),
    kinetic_max_line_width: Optional[str] = Form(None),
    kinetic_safe_area_x_pct: Optional[str] = Form(None),
    kinetic_safe_area_y_pct: Optional[str] = Form(None),
    kinetic_motion_ms: Optional[str] = Form(None),
    kinetic_confidence_floor: Optional[str] = Form(None),
    # Stem inpainting engine (default OFF). Loose optional strings for exactly the
    # reason the kinetic fields above are: a typed Form parameter makes FastAPI reject
    # an unrecognised payload with 422, but an unrecognised value must never fail the
    # job — it must fall back to the documented default (Reqs 18.1, 18.5).
    stem_inpainting_enabled: Optional[str] = Form(None),
    stem_mix_preset: Optional[str] = Form(None),
    stem_gain_vocals: Optional[str] = Form(None),
    stem_gain_music: Optional[str] = Form(None),
    stem_gain_other: Optional[str] = Form(None),
    stem_repair_mode: Optional[str] = Form(None),
    stem_repair_window_ms: Optional[str] = Form(None),
    stem_declick: Optional[str] = Form(None),
    stem_backend: Optional[str] = Form(None),
    stem_model: Optional[str] = Form(None),
    stem_retain_stems: Optional[str] = Form(None),
) -> dict:
    """Upload one or more video files and submit them for processing.

    A single file creates one job; multiple files create a batch processed in
    line.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    # Kinetic typography fields, forwarded only when actually supplied so an
    # omitted field keeps its documented ProcessingOptions default (Req 17.4).
    kinetic_form: dict[str, Optional[str]] = {
        "kinetic_typography_enabled": kinetic_typography_enabled,
        "kinetic_style": kinetic_style,
        "kinetic_reveal": kinetic_reveal,
        "kinetic_font": kinetic_font,
        "kinetic_max_lines": kinetic_max_lines,
        "kinetic_max_line_width": kinetic_max_line_width,
        "kinetic_safe_area_x_pct": kinetic_safe_area_x_pct,
        "kinetic_safe_area_y_pct": kinetic_safe_area_y_pct,
        "kinetic_motion_ms": kinetic_motion_ms,
        "kinetic_confidence_floor": kinetic_confidence_floor,
        "stem_inpainting_enabled": stem_inpainting_enabled,
        "stem_mix_preset": stem_mix_preset,
        "stem_gain_vocals": stem_gain_vocals,
        "stem_gain_music": stem_gain_music,
        "stem_gain_other": stem_gain_other,
        "stem_repair_mode": stem_repair_mode,
        "stem_repair_window_ms": stem_repair_window_ms,
        "stem_declick": stem_declick,
        "stem_backend": stem_backend,
        "stem_model": stem_model,
        "stem_retain_stems": stem_retain_stems,
    }

    options = ProcessingOptions.from_dict(
        {
            "language": language,
            "translate": translate,
            "clip_length": clip_length,
            "aspect": aspect,
            "num_clips": num_clips,
            "strategy": strategy,
            "captions": captions,
            "subtitle_sidecar": subtitle_sidecar,
            "topic": topic,
            "vocabulary": vocabulary,
            "vibe": vibe,
            "platform": platform,
            "hashtag_count": hashtag_count,
            "range_start": range_start,
            "range_end": range_end,
            "metadata": metadata,
            "publish_to": publish_to,
            "campaign_id": campaign_id,
            "publish_mode": publish_mode,
            "schedule_at": schedule_at,
            "reframe": reframe,
            "zoom": zoom,
            "transitions": transitions,
            "hook_title": hook_title,
            "music": music,
            "music_volume": music_volume,
            "fades": fades,
            "color": color,
            "progress_bar": progress_bar,
            "emoji": emoji,
            "emoji_mode": emoji_mode,
            "emoji_animate": emoji_animate,
            "filler_removal": filler_removal,
            "caption_template": caption_template,
            "caption_position": caption_position,
            "caption_preset": caption_preset,
            "caption_animation": caption_animation,
            "caption_keyword_highlight": caption_keyword_highlight,
            "caption_keyword_ai": caption_keyword_ai,
            "caption_emoji": caption_emoji,
            "broll": broll,
            "broll_intensity": broll_intensity,
            "asset_sourcing_mode": asset_sourcing_mode,
            "broll_provider": broll_provider,
            "selection_prompt": selection_prompt,
            "visual_selection": visual_selection,
            "permissibility_mode": permissibility_mode,
            "diarization": diarization,
            "speaker_reframe": speaker_reframe,
            "reframe_layout": reframe_layout,
            "reframe_intensity": reframe_intensity,
            **{key: value for key, value in kinetic_form.items() if value is not None},
        }
    )

    uploads_dir = Path(settings.uploads_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    try:
        for f in files:
            saved.append(await _save_upload(f, uploads_dir))
    except HTTPException:
        # A rejected file in the middle of a batch would otherwise leave the earlier
        # ones on disk with no job referencing them — invisible litter that the
        # retention sweeper does not own. Roll the whole request back.
        for item in saved:
            Path(item["source"]).unlink(missing_ok=True)
        raise

    manager = deps.get_manager()
    if len(saved) == 1:
        job = manager.submit(
            "file", saved[0]["source"], options, title=saved[0]["title"]
        )
        return {"jobs": [job.to_dict()]}

    batch_id = manager.submit_batch(saved, options)
    jobs = manager.store.by_batch(batch_id)
    return {"batch_id": batch_id, "jobs": [j.to_dict() for j in jobs]}


# ---------------------------------------------------------------------------
# Job status
# ---------------------------------------------------------------------------
@router.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": [j.to_dict() for j in deps.get_manager().store.all()]}


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = deps.get_manager().store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """Ask a queued or running job to stop (I4).

    ``409`` rather than ``404`` for a job that has already finished: the job exists, it simply
    cannot be cancelled, and answering 404 would tell the client the wrong thing about why.

    The response says ``cancelling`` for a job that was mid-render, because the worker stops at
    its next checkpoint and a job already inside an ffmpeg pass finishes that pass first. Saying
    "cancelled" while a render is still writing would be a claim the API cannot back.
    """
    manager = deps.get_manager()
    job = manager.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    was_running = job.status.value == "processing"
    if not manager.cancel(job_id):
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.status.value} and cannot be cancelled",
        )
    return {
        "job_id": job_id,
        "state": "cancelling" if was_running else "cancelled",
        "detail": (
            "Stopping at the next checkpoint; a pass already in progress will finish first."
            if was_running else "Stopped before processing began."
        ),
    }


@router.get("/api/jobs/{job_id}/timings")
def get_job_timings(job_id: str) -> dict:
    """Per-stage render timings for a job (M5).

    Read from the job record rather than from the live metrics registry, so the numbers survive
    a restart and remain available for a job that finished long ago - which is when someone
    actually asks where the time went.
    """
    job = deps.get_manager().store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    timings = list(job.stage_timings or [])
    return {
        "job_id": job_id,
        "status": job.status.value,
        "total_seconds": round(sum(float(t.get("seconds") or 0.0) for t in timings), 3),
        "stages": timings,
    }


@router.get("/api/batches/{batch_id}")
def get_batch(batch_id: str) -> dict:
    jobs = deps.get_manager().store.by_batch(batch_id)
    if not jobs:
        raise HTTPException(status_code=404, detail="Batch not found")
    return {"batch_id": batch_id, "jobs": [j.to_dict() for j in jobs]}


@router.post("/api/jobs/{job_id}/resume", dependencies=[Depends(rate_limit)])
def resume_job(job_id: str) -> dict:
    """Render a failed job's unfinished clips, keeping the ones it already produced (I5).

    An interrupted job was marked failed *wholesale*: the clips it had already rendered were on
    disk and listed in the record, and the only way forward was to re-submit the source and pay for
    everything again - including re-rendering the clips that had succeeded.

    ``409`` names why a job cannot be resumed rather than silently starting a full re-run, because
    a full re-run is exactly the expensive thing the caller was trying to avoid.
    """
    manager = deps.get_manager()
    job = manager.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status.value not in ("failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.status.value!r}; only a failed or cancelled job can be resumed",
        )
    if not job.planned_clips:
        raise HTTPException(
            status_code=409,
            detail="This job was interrupted before it chose its clips, so there is nothing to "
                   "resume. Re-submit the source.",
        )
    if not manager.resume(job_id):
        raise HTTPException(
            status_code=409,
            detail="Every planned clip for this job has already been rendered.",
        )
    resumed = manager.store.get(job_id)
    if resumed is None:  # pragma: no cover - resume() just confirmed it exists
        raise HTTPException(status_code=404, detail="Job not found")
    return resumed.to_dict()
