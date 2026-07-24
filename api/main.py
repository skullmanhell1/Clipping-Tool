"""FastAPI application — Phase 2 (smart selection & metadata).

Exposes the endpoints the web UI needs:

Input / jobs
    * ``POST /api/preview``           -> metadata for a URL (preview card)
    * ``POST /api/jobs/url``          -> submit a single URL job
    * ``POST /api/upload``            -> upload file(s) and submit job(s)
    * ``POST /api/jobs/batch``        -> submit a batch of URLs
    * ``GET  /api/jobs``              -> list all jobs
    * ``GET  /api/jobs/{job_id}``     -> single job status/progress
    * ``GET  /api/batches/{batch_id}``-> jobs in a batch

Watch folder
    * ``GET  /api/watch``             -> status
    * ``POST /api/watch/toggle``      -> enable/disable
    * ``POST /api/watch/options``     -> update default settings

Clips
    * ``GET  /clips/...``             -> static preview (mounted)
    * ``GET  /api/clips/{job}/{name}/download`` -> download with attachment

System
    * ``GET /healthz``, ``GET /api/info``

Run: ``uvicorn api.main:app --reload``
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import settings
from worker import metadata as meta_mod
from worker.download import DownloadError, fetch_metadata, is_url
from worker.jobs import get_manager
from worker.metadata import PLATFORM_PROFILES, REGENERATABLE_FIELDS, regenerate_field
from worker.models import ProcessingOptions
from worker.watch_folder import get_watcher

app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
    description="AI-powered video clipping & auto-publishing tool — Phase 2 (smart selection & metadata).",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    """Ensure storage directories exist before serving."""
    settings.ensure_local_dirs()
    Path(settings.clips_dir).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------
class OptionsModel(BaseModel):
    """Processing options accepted from the UI (all optional, sane defaults)."""

    language: Optional[str] = None
    translate: bool = False
    clip_length: str = "auto"
    aspect: str = "9:16"
    num_clips: str = "auto"
    strategy: str = "ai"
    captions: bool = True
    # Phase 2 — Advanced settings
    topic: str = ""
    vibe: str = ""
    platform: str = "generic"
    hashtag_count: int = 5
    range_start: Optional[float] = None
    range_end: Optional[float] = None
    metadata: bool = True

    def to_options(self) -> ProcessingOptions:
        return ProcessingOptions.from_dict(self.model_dump())


class ClipEditModel(BaseModel):
    """Editable clip metadata fields (all optional; only provided ones apply)."""

    title: Optional[str] = None
    title_alternatives: Optional[list[str]] = None
    description: Optional[str] = None
    hashtags: Optional[list[str]] = None
    hook_text: Optional[str] = None
    cta: Optional[str] = None
    mentions: Optional[list[str]] = None
    thumbnail_text: Optional[str] = None


class RegenerateRequest(BaseModel):
    """Request to regenerate a single metadata field for a clip."""

    field: str
    platform: Optional[str] = None


class UrlJobRequest(BaseModel):
    url: str
    options: OptionsModel = OptionsModel()


class BatchRequest(BaseModel):
    urls: list[str]
    options: OptionsModel = OptionsModel()


class PreviewRequest(BaseModel):
    url: str


class WatchToggleRequest(BaseModel):
    enabled: bool
    options: OptionsModel = OptionsModel()


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@app.get("/healthz", tags=["system"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/info", tags=["system"])
def info() -> dict[str, object]:
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "version": app.version,
        "aspect_ratios": ["9:16", "1:1", "16:9", "4:5"],
        "clip_lengths": ["auto", "<30s", "30-60s", "60-90s", "90s-3min"],
        "clip_counts": ["auto", "1", "3", "5", "10", "max"],
        "platforms": list(PLATFORM_PROFILES.keys()),
        "strategies": ["ai", "silence", "fixed"],
        "regeneratable_fields": list(REGENERATABLE_FIELDS),
        "llm_available": _llm_available_safe(),
    }


def _llm_available_safe() -> bool:
    """Return whether an LLM is configured (never raises)."""
    try:
        from worker.llm_client import llm_available

        return llm_available()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
@app.post("/api/preview", tags=["input"])
def preview(req: PreviewRequest) -> dict:
    """Return preview metadata for a URL (title, duration, thumbnail)."""
    if not is_url(req.url):
        raise HTTPException(status_code=400, detail="Not a valid URL")
    try:
        meta = fetch_metadata(req.url)
    except DownloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "title": meta.title,
        "duration": meta.duration,
        "thumbnail": meta.thumbnail,
        "source": meta.source,
        "uploader": meta.uploader,
    }


# ---------------------------------------------------------------------------
# Job submission
# ---------------------------------------------------------------------------
@app.post("/api/jobs/url", tags=["jobs"])
def submit_url(req: UrlJobRequest) -> dict:
    """Submit a single URL for processing."""
    if not is_url(req.url):
        raise HTTPException(status_code=400, detail="Not a valid URL")
    job = get_manager().submit("url", req.url, req.options.to_options())
    return job.to_dict()


@app.post("/api/jobs/batch", tags=["jobs"])
def submit_batch(req: BatchRequest) -> dict:
    """Submit a batch of URLs; they are processed in line (sequentially)."""
    urls = [u for u in req.urls if is_url(u)]
    if not urls:
        raise HTTPException(status_code=400, detail="No valid URLs provided")
    items = [{"input_type": "url", "source": u} for u in urls]
    batch_id = get_manager().submit_batch(items, req.options.to_options())
    jobs = get_manager().store.by_batch(batch_id)
    return {"batch_id": batch_id, "jobs": [j.to_dict() for j in jobs]}


@app.post("/api/upload", tags=["jobs"])
async def upload(
    files: list[UploadFile] = File(...),
    language: Optional[str] = Form(None),
    translate: bool = Form(False),
    clip_length: str = Form("auto"),
    aspect: str = Form("9:16"),
    num_clips: str = Form("auto"),
    strategy: str = Form("ai"),
    captions: bool = Form(True),
    topic: str = Form(""),
    vibe: str = Form(""),
    platform: str = Form("generic"),
    hashtag_count: int = Form(5),
    range_start: Optional[float] = Form(None),
    range_end: Optional[float] = Form(None),
    metadata: bool = Form(True),
) -> dict:
    """Upload one or more video files and submit them for processing.

    A single file creates one job; multiple files create a batch processed in
    line.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    options = ProcessingOptions.from_dict(
        {
            "language": language,
            "translate": translate,
            "clip_length": clip_length,
            "aspect": aspect,
            "num_clips": num_clips,
            "strategy": strategy,
            "captions": captions,
            "topic": topic,
            "vibe": vibe,
            "platform": platform,
            "hashtag_count": hashtag_count,
            "range_start": range_start,
            "range_end": range_end,
            "metadata": metadata,
        }
    )

    uploads_dir = Path(settings.uploads_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    for f in files:
        safe_name = Path(f.filename or "upload").name
        dest = uploads_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append({"input_type": "file", "source": str(dest), "title": safe_name})

    manager = get_manager()
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
@app.get("/api/jobs", tags=["jobs"])
def list_jobs() -> dict:
    return {"jobs": [j.to_dict() for j in get_manager().store.all()]}


@app.get("/api/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: str) -> dict:
    job = get_manager().store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.get("/api/batches/{batch_id}", tags=["jobs"])
def get_batch(batch_id: str) -> dict:
    jobs = get_manager().store.by_batch(batch_id)
    if not jobs:
        raise HTTPException(status_code=404, detail="Batch not found")
    return {"batch_id": batch_id, "jobs": [j.to_dict() for j in jobs]}


# ---------------------------------------------------------------------------
# Clip metadata editing + per-field regeneration
# ---------------------------------------------------------------------------
@app.patch("/api/jobs/{job_id}/clips/{clip_id}", tags=["metadata"])
def edit_clip(job_id: str, clip_id: str, edit: ClipEditModel) -> dict:
    """Update editable metadata fields on a clip (title, hashtags, hook, ...)."""
    fields = {k: v for k, v in edit.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    clip = get_manager().store.update_clip(job_id, clip_id, fields)
    if clip is None:
        raise HTTPException(status_code=404, detail="Job or clip not found")
    return clip.to_dict()


@app.post("/api/jobs/{job_id}/clips/{clip_id}/regenerate", tags=["metadata"])
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

    updated = manager.store.update_clip(job_id, clip_id, {req.field: value})
    return {"field": req.field, "value": value, "clip": updated.to_dict()}


# ---------------------------------------------------------------------------
# Watch folder
# ---------------------------------------------------------------------------
@app.get("/api/watch", tags=["watch"])
def watch_status() -> dict:
    return get_watcher().status()


@app.post("/api/watch/toggle", tags=["watch"])
def watch_toggle(req: WatchToggleRequest) -> dict:
    watcher = get_watcher()
    watcher.set_options(req.options.to_options())
    return watcher.start() if req.enabled else watcher.stop()


@app.post("/api/watch/options", tags=["watch"])
def watch_options(options: OptionsModel) -> dict:
    watcher = get_watcher()
    watcher.set_options(options.to_options())
    return watcher.status()


# ---------------------------------------------------------------------------
# Clip download (attachment). Preview streaming is handled by the static mount.
# ---------------------------------------------------------------------------
@app.get("/api/clips/{job_id}/{filename}/download", tags=["clips"])
def download_clip(job_id: str, filename: str) -> FileResponse:
    # Guard against path traversal.
    safe_name = Path(filename).name
    path = Path(settings.clips_dir) / Path(job_id).name / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Clip not found")
    return FileResponse(path, filename=safe_name, media_type="video/mp4")


# ---------------------------------------------------------------------------
# Static mounts
#   /clips  -> finished clips + thumbnails (preview streaming)
#   /       -> built React frontend if present, else placeholder page
# ---------------------------------------------------------------------------
Path(settings.clips_dir).mkdir(parents=True, exist_ok=True)
app.mount("/clips", StaticFiles(directory=str(settings.clips_dir)), name="clips")

_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="ui")
else:

    @app.get("/", response_class=HTMLResponse, tags=["ui"])
    def index() -> str:
        """Fallback page when the frontend has not been built."""
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>AI Video Clipper</title>"
            "<style>body{background:#0b0f17;color:#e6edf3;font-family:sans-serif;"
            "display:flex;min-height:100vh;align-items:center;justify-content:center;"
            "margin:0}a{color:#00d2ff}.c{max-width:560px;padding:40px}</style></head>"
            "<body><div class='c'><h1>AI Video Clipper</h1>"
            "<p>API is running. The React UI has not been built yet.</p>"
            "<p>Build it with <code>cd frontend &amp;&amp; npm install &amp;&amp; "
            "npm run build</code>, or run the dev server with "
            "<code>npm run dev</code> (proxies to this API).</p>"
            "<p><a href='/docs'>API docs</a> &middot; <a href='/api/info'>Info</a> "
            "&middot; <a href='/healthz'>Health</a></p></div></body></html>"
        )
