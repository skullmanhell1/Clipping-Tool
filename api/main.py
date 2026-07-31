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

import io
import logging
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import settings
from profiles import get_profile_store
from publishers import best_times
from publishers.base import PublishState
from publishers.history import get_history
from publishers.manager import get_publish_manager
from runtime_config import RETENTION_CHOICES, get_runtime_store
from storage_backends.retention import cleanup_expired, cleanup_temp, disk_usage
from updates import get_update_checker
from worker import captions as cap
from worker.download import DownloadError, fetch_metadata, is_url
from worker.effects import broll, caption_presets

# Side-effect import: populates the default engine registry so `/api/info`
# advertises every AV engine (each still default-off). See worker/engines/loader.py.
from worker.engines import loader  # noqa: F401
from worker.jobs import get_manager
from worker.metadata import PLATFORM_PROFILES, REGENERATABLE_FIELDS, regenerate_field
from worker.models import BUILTIN_PROFILES, ProcessingOptions
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

logger = logging.getLogger(__name__)


def _run_startup() -> None:
    """Ensure storage dirs exist and start the background retention sweeper."""
    # I6: attach the job-attribution filter before anything can log. Installed here rather than
    # at import time so a host that configures its own logging (a container platform capturing
    # stdout) has already done so and keeps its handlers - this only adds the filter and format.
    try:
        from worker import observability

        observability.install()
    except Exception:  # pragma: no cover - logging setup must never stop the app booting
        logger.exception("could not install job-scoped log context")
    settings.ensure_local_dirs()
    Path(settings.clips_dir).mkdir(parents=True, exist_ok=True)
    if settings.cors_allow_wildcard and settings.environment.strip().lower() not in (
        "development", "dev", "local", "test",
    ):
        # A wildcard is a sensible default for local work and a poor one on a public
        # host, where it lets any site call this API. Warning rather than refusing to
        # boot: an operator may be fronting the app with a proxy that handles CORS.
        logger.warning(
            "CORS_ORIGINS is '*' with environment=%r. Set an explicit origin list for "
            "a public deployment; credentialed cross-origin requests are also disabled "
            "while the wildcard is in use.",
            settings.environment,
        )
    try:
        from storage_backends.retention import get_sweeper

        get_sweeper().start()
    except Exception:
        pass


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Application lifespan.

    Replaces ``@app.on_event("startup")``, which FastAPI deprecates and which therefore
    emitted a DeprecationWarning on every import — noise that also prevented the test
    suite from treating warnings as errors.
    """
    _run_startup()
    yield


app = FastAPI(
    lifespan=_lifespan,
    title=settings.app_name,
    version=APP_VERSION,
    description="AI-powered video clipping & auto-publishing tool — Phase 5 (storage, profiles & updates).",
)

# allow_credentials is derived rather than hard-coded: a wildcard origin and
# credentials are mutually exclusive per the CORS spec, and browsers drop such
# responses. Hard-coding True alongside the default "*" therefore disabled every
# credentialed cross-origin request while appearing to allow them. See
# Settings.cors_allow_credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    # T4: per-video names/jargon fed to the ASR decode as a prompt.
    vocabulary: str = ""
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
    # Tier 1 — Feature A: animated caption presets
    caption_preset: str = "karaoke"
    caption_animation: str = ""
    caption_keyword_highlight: bool = False
    caption_keyword_ai: bool = False
    caption_emoji: bool = False
    # Tier 1 — Feature B: b-roll overlays
    broll: bool = False
    broll_intensity: str = "standard"
    asset_sourcing_mode: str = "off"
    broll_provider: str = ""
    # Tier 1 — Feature C: prompt / visual selection
    selection_prompt: str = ""
    visual_selection: bool = False
    # Tier 1 — cross-cutting
    permissibility_mode: bool = False
    # v0.8.0 — Speaker diarisation & multi-speaker reframe (default OFF)
    diarization: bool = False
    speaker_reframe: bool = False
    reframe_layout: str = "follow_active"
    reframe_intensity: str = "standard"
    # Kinetic typography engine (default OFF). Same fields and same defaults as
    # ``ProcessingOptions``; unrecognised *choice* values are not rejected here
    # but coerced by the engine's ``resolve_options`` (Reqs 17.4, 17.7).
    kinetic_typography_enabled: bool = False
    kinetic_style: str = "karaoke_fill"
    kinetic_reveal: str = "cumulative"
    kinetic_font: str = ""
    kinetic_max_lines: int = 2
    kinetic_max_line_width: int = 22
    kinetic_safe_area_x_pct: float = 6.0
    kinetic_safe_area_y_pct: float = 10.0
    kinetic_motion_ms: int = 120
    kinetic_confidence_floor: float = 0.0
    # Stem inpainting engine (default OFF). Same defaults as ``ProcessingOptions`` /
    # ``Stem_Options``; unrecognised *choice* values are coerced by the engine's
    # ``resolve_options`` rather than rejected here (Reqs 18.1, 18.5).
    stem_inpainting_enabled: bool = False
    stem_mix_preset: str = "custom"
    stem_gain_vocals: float = 1.0
    stem_gain_music: float = 1.0
    stem_gain_other: float = 1.0
    stem_repair_mode: str = "crossfade"
    stem_repair_window_ms: int = 12
    stem_declick: bool = False
    stem_backend: str = "auto"
    stem_model: str = "htdemucs"
    stem_retain_stems: bool = False

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


class CaptionPreviewModel(BaseModel):
    """Request a rendered caption sample for a preset (C18)."""

    preset: str = "karaoke"
    text: str = ""
    aspect: str = "9:16"
    position: str = ""
    #: Preset fields to override before rendering, so a panel can preview an edited style.
    overrides: dict = {}


class CutRange(BaseModel):
    """One clip-relative range to remove (U4)."""

    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)


class RerenderRequest(BaseModel):
    """Re-render one clip, optionally with changed settings (U7) or a cut list (U4).

    ``settings`` is a partial options blob; unknown keys are ignored, so a UI can send its whole
    settings object without knowing which fields this build understands.

    ``cuts`` is a **separate, typed field rather than a key inside** ``settings``, for two
    reasons. ``settings`` is filtered against ``ProcessingOptions`` fields and unknown keys are
    dropped silently, so a cut list sent that way would be discarded without a word - the worst
    possible failure for a destructive edit the user is watching for. And a cut list describes
    one clip, whereas everything in ``settings`` describes the job, so putting it there would
    invite it being applied to clips it was not drawn against.
    """

    settings: dict = {}
    cuts: list[CutRange] = Field(default_factory=list)


class ClipReviewModel(BaseModel):
    """Set the review state of one clip (U9)."""

    review_state: str
    review_note: str = ""


class BatchReviewModel(BaseModel):
    """Set the review state of many clips at once (U9).

    ``clip_ids`` is scoped to one job, which is how review actually happens - you work through
    the clips a job produced. A cross-job version would need per-job permission checks that do
    not exist yet in a single-tenant product.
    """

    clip_ids: list[str]
    review_state: str
    review_note: str = ""


class PublishClipRequest(BaseModel):
    platforms: list[str] = []
    campaign_id: str = ""
    mode: str = "auto"
    schedule_at: Optional[float] = None
    routes: dict[str, dict[str, str]] = {}


class RescheduleModel(BaseModel):
    """A new time for a pending publish attempt (PB7)."""

    schedule_at: float


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
    engines, capabilities = _engines_info()
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
        # U2: names only, so a client can offer the picker from one call. The full bundles,
        # with the reasoning behind each, come from GET /api/profiles/builtin.
        "builtin_profiles": list(BUILTIN_PROFILES),
        "llm_available": _llm_available_safe(),
        "effects": {
            "music_moods": ["upbeat", "chill", "dramatic", "corporate", "suspense"],
            "color_presets": ["vivid", "warm", "cool", "cinematic", "bw"],
            "emoji_intensities": ["off", "subtle", "standard", "heavy"],
            "emoji_modes": ["keyword", "ai"],
            "caption_templates": ["karaoke", "boxed", "minimal"],
            # C13: nine positions, up from three. The original three stay first and keep their
            # names, so a client that only knows them is unaffected.
            "caption_positions": list(cap.VALID_CAPTION_POSITIONS),
            # A4: the twelve vendored faces were shipped with licences and a manifest and
            # nothing exposed them, so the only way to change a caption font was to edit a
            # preset in source. Variable fonts are filtered out here rather than offered and
            # then silently substituted (C1).
            "caption_fonts": cap.available_fonts(),
            # C12: the platform safe-area profiles a client may select.
            "caption_safe_areas": list(cap.SAFE_AREA_INSETS.keys()),
            # Tier 1 — Creator Output Upgrade (additive; Reqs 1.4, 8.7, 22.3)
            "caption_presets": list(caption_presets.BUILTIN_PRESETS.keys()),
            # U5: the presets' actual values, not just their names. A style picker cannot
            # preview a look it only knows the name of, so the previous names-only list left
            # the UI unable to show a creator what "hormozi" or "typewriter" would look like
            # before spending a render finding out. Colours are added in `#RRGGBB` form
            # alongside the ASS originals, because a colour input cannot display `&H00FFFFFF`.
            "caption_preset_details": [
                _preset_detail(preset)
                for preset in caption_presets.BUILTIN_PRESETS.values()
            ],
            "caption_animations": ["none", "pop", "typewriter", "karaoke_fill"],
            "asset_sourcing_modes": ["off", "local_only", "local_then_external"],
            "broll_intensities": list(broll.BROLL_INTENSITY.keys()),
            "broll_providers": _available_broll_providers(),
            # v0.8.0 — Speaker Diarisation & Multi-Speaker Reframe (additive;
            # Reqs 7.4, 10.6, 17.5, 18.1). Sourced from the ProcessingOptions
            # known-value sets so the API stays in lockstep with the model.
            "reframe_layouts": list(ProcessingOptions._REFRAME_LAYOUTS),
            "reframe_intensities": list(ProcessingOptions._REFRAME_INTENSITIES),
        },
        "broll_available": bool(
            settings.broll_provider_api_key and settings.broll_allow_download
        ),
        "storage_backend": settings.storage_backend.value,
        "retention_choices": list(RETENTION_CHOICES),
        # Advanced AV engines foundation (additive; Reqs 20.1, 20.2, 20.6).
        # Both are empty until an engine spec registers one, so a stock install
        # sees the v0.8.0 payload plus two inert keys.
        "engines": engines,
        "capabilities": capabilities,
    }


def _engines_info() -> tuple[list[dict[str, object]], dict[str, object]]:
    """Return the ``(engines, capabilities)`` pair advertised by ``/api/info``.

    Reqs 20.1/20.2/20.6 — additive only: one row per registered AV engine in the
    registry's deterministic order, plus the serialisable Capability_Report.

    The report is consulted **only** for capability ids that registered engines
    actually declare, so with no engine registered this returns ``([], {})``
    having performed **zero** capability probes (Req 20.2). Never raises: a
    broken engine declaration must not take ``/api/info`` down.
    """
    try:
        from worker.engines.registry import get_registry

        engines = get_registry().all()
    except Exception:
        return [], {}
    if not engines:
        # No engine registered => nothing to probe (Reqs 20.2, 20.6).
        return [], {}

    from worker.engines.capabilities import get_report

    report = get_report()
    rows: list[dict[str, object]] = []
    for engine in engines:
        try:
            required = list(getattr(engine, "required_capabilities", ()) or ())
            optional = list(getattr(engine, "optional_capabilities", ()) or ())
            missing = report.missing(required)
            for capability_id in optional:
                # Declared by this engine, so it belongs in the advertised
                # report even when it is only an optional degradation.
                report.available(capability_id)
            stage = getattr(engine, "stage", None)
            rows.append(
                {
                    "id": str(getattr(engine, "engine_id", "")),
                    "stage": getattr(stage, "value", stage),
                    "priority": getattr(engine, "priority", 100),
                    "flag": engine.flag_field(),
                    "enabled_by_default": False,
                    "available": not missing,
                    "missing": missing,
                    "requires_network": bool(getattr(engine, "requires_network", False)),
                    "time_budget_s": getattr(engine, "time_budget_s", None),
                }
            )
        except Exception:
            continue
    capabilities = report.to_dict()
    _add_engine_option_domains(rows, capabilities)
    return rows, capabilities


def _add_engine_option_domains(
    rows: list[dict[str, object]], capabilities: dict[str, object]
) -> None:
    """Advertise engine-specific option domains inside the ``capabilities`` block.

    The per-engine row stays generic (`id`/`stage`/`priority`/`flag`/
    `enabled_by_default`/`available`/`missing`/`requires_network`/
    `time_budget_s`), so a new engine never changes that schema. Engine-specific
    option vocabularies therefore ride in ``capabilities`` under an
    Engine_Id-namespaced key — capability ids are always ``<kind>:<name>``, so a
    bare engine id can never collide with one.

    Kinetic typography advertises its supported Kinetic_Style and Reveal_Mode
    values (Reqs 17.2, 17.3), and stem inpainting its Mix_Preset / Repair_Mode /
    backend vocabularies plus the numeric bounds the UI's sliders need — both
    **imported** from their engine module so the endpoint cannot drift from it.

    Note the audio-stem spec's task 17.3 asks for these on the engine *row*
    (``engines.stem_inpainting.mix_presets`` and friends). They are placed here
    instead, deliberately: the per-engine row schema is fixed and generic so that
    adding an engine never changes it, which is the rule this function exists to
    uphold. The availability facts that task also asks for are already covered —
    the row carries ``available``/``missing``, and each engine's *optional*
    capability ids (``python_pkg:demucs``, ``model:htdemucs``) are forced into the
    report by :func:`_engines_info`, so they appear in the top-level
    ``capabilities`` map under their own ``<kind>:<name>`` keys.

    Each block is emitted only when that engine is actually registered, so the
    no-engine-registered payload stays empty, and each is independently guarded:
    one engine module failing to import must not cost the other its domains, and
    must not take ``/api/info`` down.
    """
    if any(row.get("id") == "kinetic_typography" for row in rows):
        try:
            from worker.engines.kinetic import ENGINE_ID, KINETIC_STYLES, REVEAL_MODES

            capabilities[ENGINE_ID] = {
                "styles": list(KINETIC_STYLES),
                "reveal_modes": list(REVEAL_MODES),
            }
        except Exception:
            pass

    if any(row.get("id") == "stem_inpainting" for row in rows):
        try:
            from worker.engines.stems import (
                BACKEND_IDS,
                ENGINE_ID,
                GAIN_DEFAULT,
                GAIN_MAX,
                GAIN_MIN,
                MIX_PRESET_CHOICES,
                REPAIR_MODES,
                STEM_NAMES,
                WINDOW_DEFAULT_MS,
                WINDOW_MAX_MS,
                WINDOW_MIN_MS,
            )

            capabilities[ENGINE_ID] = {
                "mix_presets": list(MIX_PRESET_CHOICES),
                "repair_modes": list(REPAIR_MODES),
                "backends": list(BACKEND_IDS),
                "stem_set": list(STEM_NAMES),
                "gain": {
                    "min": GAIN_MIN,
                    "max": GAIN_MAX,
                    "default": GAIN_DEFAULT,
                },
                "repair_window_ms": {
                    "min": WINDOW_MIN_MS,
                    "max": WINDOW_MAX_MS,
                    "default": WINDOW_DEFAULT_MS,
                },
            }
        except Exception:
            pass


def _available_broll_providers() -> list[str]:
    """Return configured external b-roll providers ([] when none configured)."""
    return [settings.broll_provider] if settings.broll_provider else []


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


@app.post("/api/jobs/{job_id}/cancel", tags=["jobs"])
def cancel_job(job_id: str) -> dict:
    """Ask a queued or running job to stop (I4).

    ``409`` rather than ``404`` for a job that has already finished: the job exists, it simply
    cannot be cancelled, and answering 404 would tell the client the wrong thing about why.

    The response says ``cancelling`` for a job that was mid-render, because the worker stops at
    its next checkpoint and a job already inside an ffmpeg pass finishes that pass first. Saying
    "cancelled" while a render is still writing would be a claim the API cannot back.
    """
    manager = get_manager()
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


@app.get("/api/jobs/{job_id}/timings", tags=["jobs"])
def get_job_timings(job_id: str) -> dict:
    """Per-stage render timings for a job (M5).

    Read from the job record rather than from the live metrics registry, so the numbers survive
    a restart and remain available for a job that finished long ago - which is when someone
    actually asks where the time went.
    """
    job = get_manager().store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    timings = list(job.stage_timings or [])
    return {
        "job_id": job_id,
        "status": job.status.value,
        "total_seconds": round(sum(float(t.get("seconds") or 0.0) for t in timings), 3),
        "stages": timings,
    }


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


def _preset_detail(preset) -> dict:
    """A caption preset serialised for the UI, with web-usable colours (U5).

    The preset's own ``to_dict`` keeps ASS ``&HAABBGGRR`` colours, which is right for the
    renderer and unusable in a browser: no colour input or CSS property accepts one. The hex
    equivalents are *added* rather than substituted, so the API still reports exactly what the
    renderer will use.
    """
    from worker import branding

    data = preset.to_dict()
    colors = data.get("colors") or {}
    data["colors_hex"] = {
        key: branding.ass_to_hex(value)
        for key, value in colors.items()
        if branding.ass_to_hex(value)
    }
    return data


@app.post("/api/jobs/{job_id}/resume", tags=["jobs"])
def resume_job(job_id: str) -> dict:
    """Render a failed job's unfinished clips, keeping the ones it already produced (I5).

    An interrupted job was marked failed *wholesale*: the clips it had already rendered were on
    disk and listed in the record, and the only way forward was to re-submit the source and pay for
    everything again - including re-rendering the clips that had succeeded.

    ``409`` names why a job cannot be resumed rather than silently starting a full re-run, because
    a full re-run is exactly the expensive thing the caller was trying to avoid.
    """
    manager = get_manager()
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
    return manager.store.get(job_id).to_dict()


@app.post("/api/captions/preview", tags=["metadata"])
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


@app.post("/api/jobs/{job_id}/clips/{clip_id}/review", tags=["metadata"])
def review_clip(job_id: str, clip_id: str, req: ClipReviewModel) -> dict:
    """Approve, reject or reset one clip (U9)."""
    updated = _set_review(job_id, [clip_id], req.review_state, req.review_note)
    return updated[0]


@app.post("/api/jobs/{job_id}/clips/review", tags=["metadata"])
def review_clips(job_id: str, req: BatchReviewModel) -> dict:
    """Approve or reject many clips of one job in a single call (U9).

    A job produces up to ten clips and each had to be judged individually with nowhere to record
    the verdict, so an interrupted review pass had to be redone from the top.
    """
    if not req.clip_ids:
        raise HTTPException(status_code=400, detail="clip_ids must not be empty")
    updated = _set_review(job_id, req.clip_ids, req.review_state, req.review_note)
    return {"updated": updated, "count": len(updated)}


@app.get("/api/jobs/{job_id}/clips/{clip_id}/transcript", tags=["metadata"])
def clip_transcript(job_id: str, clip_id: str) -> dict:
    """Word-level timings for one rendered clip, for the transcript editor (U4).

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


@app.post("/api/jobs/{job_id}/clips/{clip_id}/rerender", tags=["metadata"])
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


@app.get("/api/history", tags=["publishing"])
def history(limit: int=200, platform: str="") -> dict:
    return get_history().history(max(1,min(limit,500)),platform)


@app.get("/api/publish-attempts/{attempt_id}", tags=["publishing"])
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


@app.post("/api/publish-attempts/{attempt_id}/approve", tags=["publishing"])
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


@app.get("/api/schedule", tags=["publishing"])
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


@app.get("/api/schedule/suggestions", tags=["publishing"])
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


@app.patch("/api/publish-attempts/{attempt_id}/schedule", tags=["publishing"])
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


@app.post("/api/publish-attempts/{attempt_id}/cancel", tags=["publishing"])
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


@app.post("/api/publishers/{platform}/refresh", tags=["publishing"])
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


@app.post("/api/publish-attempts/{attempt_id}/retry", tags=["publishing"])
def retry_publish_attempt(attempt_id: str) -> dict:
    """Re-queue a failed (or still-in-review) attempt without changing its mode.

    Separate from ``/approve`` on purpose: a retry is for transient trouble — an expired
    token, a network blip, a clip that was briefly missing — and must not silently
    escalate a review-mode submission into a live post.
    """
    return _resume_attempt(attempt_id, force_direct=False)


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
    # refresh=True: this endpoint has just deleted files, so the cached area sizes are
    # stale by construction and would report the pre-cleanup totals.
    result["usage"] = disk_usage(refresh=True)
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
@app.get("/api/profiles/builtin", tags=["profiles"])
def list_builtin_profiles() -> dict:
    """The shipped opinionated profiles (U2).

    Distinct from ``GET /api/profiles``, which lists profiles a *user* saved. These are
    read-only bundles: pass ``profile: "<name>"`` alongside a process request and the bundle
    is expanded into the individual options, with anything else in the request overriding it.

    ``settings`` is returned in full rather than summarised so a client can show what
    picking a profile will actually change, and ``rationale`` is included because a profile
    is a set of judgement calls a user is entitled to disagree with.
    """
    return {
        "profiles": [
            {
                "name": profile.name,
                "label": profile.label,
                "description": profile.description,
                "rationale": profile.rationale,
                "settings": dict(profile.settings),
            }
            for profile in BUILTIN_PROFILES.values()
        ]
    }


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
    safe_name = Path(filename).name
    path = Path(settings.clips_dir) / Path(job_id).name / safe_name
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
    safe_name = Path(filename).name
    path = Path(settings.clips_dir) / Path(job_id).name / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Clip not found")
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
        """Fallback page when the frontend has not been built (U13)."""
        return fallback_index_html()


def fallback_index_html() -> str:
    """Fallback page when the frontend has not been built (U13).

    Reports the instance's *actual* state rather than static prose. Someone who reaches this
    page has almost always got here by accident - a bare API, a deploy where the frontend
    build did not run - and the questions they need answered are "is the backend healthy"
    and "what is missing". A page that only says "the UI is not built" answers neither, and
    sends them to read logs for facts the process already knows.

    Every probe is individually guarded: this page must render when things are broken, since
    that is precisely when it is read.
    """
    def _row(label: str, value: str, ok: bool = True) -> str:
        colour = "#3fb950" if ok else "#f85149"
        return (
            f"<tr><td style='padding:4px 16px 4px 0;color:#8b949e'>{label}</td>"
            f"<td style='color:{colour}'>{value}</td></tr>"
        )

    rows = [_row("Version", APP_VERSION), _row("Environment", settings.environment)]

    try:
        import shutil

        # Resolved rather than merely reported: "ffmpeg" as a configured value tells the
        # reader nothing, and a missing binary is the single most common reason a deploy of
        # this app does not work. shutil.which answers the question they actually have.
        resolved = shutil.which(str(settings.ffmpeg_binary))
        rows.append(_row("ffmpeg", resolved or f"NOT FOUND ({settings.ffmpeg_binary})",
                         bool(resolved)))
    except Exception:
        rows.append(_row("ffmpeg", "could not be resolved", ok=False))

    try:
        rows.append(_row("Whisper model", str(settings.whisper_model)))
        rows.append(_row("Storage backend", str(settings.storage_backend.value)))
    except Exception:
        pass

    try:
        jobs = get_manager().store.all()
        active = sum(1 for j in jobs if j.status.value in ("queued", "processing"))
        rows.append(_row("Jobs", f"{len(jobs)} known, {active} active"))
    except Exception:
        rows.append(_row("Jobs", "job store unavailable", ok=False))

    try:
        # _engines_info returns (engine_rows, capabilities); only the rows are wanted here.
        # Unpacking explicitly, because iterating the tuple enumerates the capabilities mapping
        # instead - which is how the first version of this quietly reported "could not be
        # listed" on a perfectly healthy instance, the exact class of failure this page exists
        # to make visible.
        engines, _capabilities = _engines_info()
        names = ", ".join(
            f"{e['id']}{'' if e.get('available', True) else ' (unavailable)'}"
            for e in engines
        ) or "none registered"
        rows.append(_row("Engines", names))
    except Exception:
        rows.append(_row("Engines", "could not be listed", ok=False))

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{settings.app_name}</title>"
        "<style>body{background:#0b0f17;color:#e6edf3;font-family:ui-sans-serif,sans-serif;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}"
        "a{color:#22d3ee}.c{max-width:640px;padding:40px}code{background:#161b22;"
        "padding:2px 6px;border-radius:4px;font-size:13px}"
        "table{border-collapse:collapse;font-size:14px;margin:20px 0}"
        "h1{margin:0 0 4px;font-size:22px}.s{color:#8b949e;font-size:14px}"
        "</style></head><body><div class='c'>"
        f"<h1>{settings.app_name}</h1>"
        "<p class='s'>The API is running. The React UI has not been built, so this page is "
        "standing in for it.</p>"
        f"<table>{''.join(rows)}</table>"
        "<p class='s'>To get the interface: <code>cd frontend &amp;&amp; npm install "
        "&amp;&amp; npm run build</code>, then reload. For development use "
        "<code>npm run dev</code>, which proxies to this API.</p>"
        "<p><a href='/docs'>API docs</a> &middot; <a href='/api/info'>Capabilities</a> "
        "&middot; <a href='/api/jobs'>Jobs</a> &middot; <a href='/healthz'>Health</a></p>"
        "</div></body></html>"
    )
