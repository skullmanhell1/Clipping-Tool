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
import io
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import settings
from profiles import get_profile_store
from publishers.history import get_history
from publishers.manager import get_publish_manager
from runtime_config import RETENTION_CHOICES, get_runtime_store
from storage_backends.retention import cleanup_expired, cleanup_temp, disk_usage
from updates import get_update_checker
from worker.download import DownloadError, fetch_metadata, is_url
from worker.jobs import get_manager
from worker.metadata import PLATFORM_PROFILES, REGENERATABLE_FIELDS, regenerate_field
from worker.models import ProcessingOptions
from worker.watch_folder import get_watcher

def _read_version() -> str:
    """Read the semantic version from the VERSION file (fallback to a default)."""
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text(
            encoding="utf-8"
        ).strip() or "0.0.0"
    except OSError:
        return "0.0.0"


APP_VERSION = _read_version()

app = FastAPI(
    title=settings.app_name,
    version=APP_VERSION,
    description="AI-powered video clipping & auto-publishing tool — Phase 5 (storage, profiles & updates).",
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
    """Ensure storage dirs exist and start the background retention sweeper."""
    settings.ensure_local_dirs()
    Path(settings.clips_dir).mkdir(parents=True, exist_ok=True)
    try:
        from storage_backends.retention import get_sweeper

        get_sweeper().start()
    except Exception:
        pass


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
    # Phase 3 — publishing
    publish_to: list[str] = []
    campaign_id: str = ""
    publish_mode: str = "review"
    schedule_at: Optional[float] = None
    # Phase 4 — visual effects (all individually toggleable)
    reframe: bool = False
    zoom: bool = False
    transitions: bool = False
    hook_title: bool = False
    music: str = ""
    music_volume: float = 0.12
    fades: bool = False
    color: str = ""
    progress_bar: bool = False
    emoji: str = "off"
    emoji_mode: str = "keyword"
    emoji_animate: bool = True
    filler_removal: bool = False
    caption_template: str = "karaoke"
    caption_position: str = "bottom"

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


class PublishClipRequest(BaseModel):
    platforms: list[str] = []
    campaign_id: str = ""
    mode: str = "auto"
    schedule_at: Optional[float] = None
    routes: dict[str, dict[str, str]] = {}


class CampaignModel(BaseModel):
    name: str
    routes: dict[str, dict[str, str]]
    id: str = ""


class StorageSettingsModel(BaseModel):
    """User-tunable storage settings (runtime-persisted)."""

    retention_days: Optional[int] = None
    auto_delete_temp: Optional[bool] = None
    delete_local_after_publish: Optional[bool] = None


class ProfileModel(BaseModel):
    """Create/update a saved settings profile."""

    name: str
    settings: dict = {}
    publishing: dict = {}
    id: str = ""
    make_default: bool = False


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
        "effects": {
            "music_moods": ["upbeat", "chill", "dramatic", "corporate", "suspense"],
            "color_presets": ["vivid", "warm", "cool", "cinematic", "bw"],
            "emoji_intensities": ["off", "subtle", "standard", "heavy"],
            "emoji_modes": ["keyword", "ai"],
            "caption_templates": ["karaoke", "boxed", "minimal"],
            "caption_positions": ["bottom", "center", "top"],
        },
        "storage_backend": settings.storage_backend.value,
        "retention_choices": list(RETENTION_CHOICES),
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
    get_history().sync_clip(job_id, clip)
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
    get_history().sync_clip(job_id, updated)
    return {"field": req.field, "value": value, "clip": updated.to_dict()}


# ---------------------------------------------------------------------------
# Publishing, campaigns, scheduling, and history
# ---------------------------------------------------------------------------
@app.get("/api/publishers", tags=["publishing"])
def publisher_statuses() -> dict:
    return {"platforms": get_publish_manager().statuses()}


@app.get("/api/campaigns", tags=["publishing"])
def list_campaigns() -> dict:
    return {"campaigns": [c.to_dict() for c in get_history().campaigns()]}


@app.post("/api/campaigns", tags=["publishing"])
def save_campaign(req: CampaignModel) -> dict:
    if not req.name.strip() or not req.routes:
        raise HTTPException(status_code=400, detail="Campaign name and routes are required")
    return get_history().save_campaign(req.name.strip(), req.routes, req.id).to_dict()


@app.post("/api/jobs/{job_id}/clips/{clip_id}/publish", tags=["publishing"])
def publish_clip(job_id: str, clip_id: str, req: PublishClipRequest) -> dict:
    manager=get_manager(); job=manager.store.get(job_id); clip=manager.store.get_clip(job_id,clip_id)
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


@app.get("/api/history", tags=["publishing"])
def history(limit: int=200, platform: str="") -> dict:
    return get_history().history(max(1,min(limit,500)),platform)


@app.get("/api/publish-attempts/{attempt_id}", tags=["publishing"])
def publish_attempt(attempt_id: str) -> dict:
    item=get_history().get_attempt(attempt_id)
    if not item: raise HTTPException(status_code=404,detail="Publish attempt not found")
    return item


# ---------------------------------------------------------------------------
# Storage: disk usage, runtime settings, cleanup, and protected source deletion
# ---------------------------------------------------------------------------
def _storage_state() -> dict:
    """Combined disk usage + runtime storage settings + backend name."""
    cfg = get_runtime_store().get()
    return {
        "backend": settings.storage_backend.value,
        "settings": cfg.to_dict(),
        "retention_choices": list(RETENTION_CHOICES),
        "usage": disk_usage(),
    }


@app.get("/api/storage", tags=["storage"])
def storage_status() -> dict:
    return _storage_state()


@app.post("/api/storage/settings", tags=["storage"])
def update_storage_settings(req: StorageSettingsModel) -> dict:
    get_runtime_store().update(**{k: v for k, v in req.model_dump().items() if v is not None})
    return _storage_state()


@app.post("/api/storage/cleanup", tags=["storage"])
def storage_cleanup(temp: bool = True, expired: bool = True) -> dict:
    """Run cleanup now: expired clips (per retention) and/or all temp files."""
    result: dict = {}
    if expired:
        result["expired"] = cleanup_expired()
    if temp:
        result["temp_removed"] = cleanup_temp()
    result["usage"] = disk_usage()
    return result


@app.delete("/api/jobs/{job_id}/source", tags=["storage"])
def delete_source(job_id: str, confirm: bool = False) -> dict:
    """Delete a job's original source video. Requires ``confirm=true``.

    Source video is never auto-deleted; this endpoint is the only way to remove
    it, and it refuses to act without explicit confirmation.
    """
    if not confirm:
        raise HTTPException(status_code=400,
                            detail="Deleting the original source requires confirm=true")
    job = get_manager().store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.input_type != "file":
        raise HTTPException(status_code=400,
                            detail="Only uploaded/downloaded source files can be deleted here")
    src = Path(job.source).resolve()
    uploads_root = Path(settings.uploads_dir).resolve()
    if uploads_root not in src.parents:
        raise HTTPException(status_code=400, detail="Source is not in the uploads directory")
    existed = src.is_file()
    if existed:
        src.unlink(missing_ok=True)
    return {"deleted": existed, "source": str(src)}


# ---------------------------------------------------------------------------
# Saved settings profiles
# ---------------------------------------------------------------------------
@app.get("/api/profiles", tags=["profiles"])
def list_profiles() -> dict:
    store = get_profile_store()
    default = store.get_default()
    return {
        "profiles": [p.to_dict() for p in store.list()],
        "default_id": default.id if default else None,
    }


@app.post("/api/profiles", tags=["profiles"])
def save_profile(req: ProfileModel) -> dict:
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Profile name is required")
    prof = get_profile_store().save(
        req.name, req.settings, req.publishing,
        profile_id=req.id, make_default=req.make_default,
    )
    return prof.to_dict()


@app.post("/api/profiles/{profile_id}/default", tags=["profiles"])
def set_default_profile(profile_id: str) -> dict:
    prof = get_profile_store().set_default(profile_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return prof.to_dict()


@app.delete("/api/profiles/{profile_id}", tags=["profiles"])
def delete_profile(profile_id: str) -> dict:
    if not get_profile_store().delete(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"deleted": True, "id": profile_id}


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------
@app.get("/api/updates", tags=["updates"])
def check_updates(force: bool = False) -> dict:
    return get_update_checker().check(force=force)


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
# Clip downloads. The primary download is a ZIP containing video + metadata TXT.
# ---------------------------------------------------------------------------
def _clip_metadata_text(clip) -> str:
    return (f"Title\n{clip.title}\n\nCaption / Description\n{clip.description}\n\n"
            f"Hashtags\n{' '.join(clip.hashtags)}\n\nHook\n{clip.hook_text}\n\n"
            f"CTA\n{clip.cta}\n\nMentions\n{' '.join(clip.mentions)}\n")


@app.get("/api/clips/{job_id}/{filename}/download", tags=["clips"])
def download_clip(job_id: str, filename: str) -> StreamingResponse:
    safe_name=Path(filename).name; path=Path(settings.clips_dir)/Path(job_id).name/safe_name
    job=get_manager().store.get(job_id)
    clip=next((c for c in job.clips if c.filename==safe_name),None) if job else None
    if not path.exists() or not path.is_file() or clip is None:
        raise HTTPException(status_code=404, detail="Clip not found")
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as archive:
        archive.write(path,arcname=safe_name)
        archive.writestr(f"{Path(safe_name).stem}_metadata.txt",_clip_metadata_text(clip))
    buf.seek(0)
    return StreamingResponse(buf,media_type="application/zip",headers={
      "Content-Disposition":f'attachment; filename="{Path(safe_name).stem}_package.zip"'})


@app.get("/api/clips/{job_id}/{filename}/video", tags=["clips"])
def download_video_only(job_id: str, filename: str) -> FileResponse:
    safe_name=Path(filename).name; path=Path(settings.clips_dir)/Path(job_id).name/safe_name
    if not path.exists() or not path.is_file(): raise HTTPException(status_code=404,detail="Clip not found")
    return FileResponse(path,filename=safe_name,media_type="video/mp4")


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
