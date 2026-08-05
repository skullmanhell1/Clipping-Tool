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

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from api.routers import (
    clips,
    jobs,
    metadata,
    metrics,
    profiles,
    publishing,
    storage,
    system,
    watch,
)

# Re-exported, not relocated behaviour: the pydantic models moved to
# `api.routers._models` when this file was split, and the test suite (and any caller
# that predates the split) still reaches for them on `api.main`. Listed by name so the
# split cannot silently drop one.
from api.routers._models import (  # noqa: F401
    BatchRequest,
    BatchReviewModel,
    CampaignModel,
    CaptionPreviewModel,
    ClipEditModel,
    ClipReviewModel,
    CutRange,
    OptionsModel,
    PreviewRequest,
    ProfileModel,
    PublishClipRequest,
    RegenerateRequest,
    RerenderRequest,
    RescheduleModel,
    StorageSettingsModel,
    UrlJobRequest,
    WatchToggleRequest,
)

# `APP_VERSION`/`_read_version` live in `_shared` for one reason: `GET /api/info` reports
# the version too, and a router may not import `api.main` (that edge is a cycle). They
# are re-exported here, so `api.main.APP_VERSION` still resolves and `app.version` below
# is still built from it. `_engines_info` is shared with `fallback_index_html`.
from api.routers._shared import (  # noqa: F401
    APP_VERSION,
    _engines_info,
    _read_version,
)
from api.routers.jobs import _save_upload, upload  # noqa: F401
from api.security import ClipsAuthMiddleware, require_api_token
from config import settings

# Side-effect import: populates the default engine registry so `/api/info`
# advertises every AV engine (each still default-off). See worker/engines/loader.py.
from worker.engines import loader  # noqa: F401
from worker.jobs import get_manager
from worker.models import ACTIVE_JOB_STATUSES

logger = logging.getLogger(__name__)


#: Environments where an insecure configuration is a convenience rather than a mistake.
#: ``settings.is_local_environment`` owns the list; it used to be inlined here, and the auth and
#: CORS checks below both need the same answer.
class InsecureDeploymentError(RuntimeError):
    """Raised at startup when a production deployment is configured to be wide open.

    A distinct type so a test can assert on the reason rather than on log text, and so the
    traceback names the problem in a container's crash log.
    """


def _check_deployment_security() -> None:
    """Warn about, or refuse, an unsafe configuration.

    Two rules with deliberately different severity:

    * **No shared secret** warns locally and **refuses to boot in production**. Every route was
      open, and ``render.yaml`` publishes this with ``autoDeploy: true`` — so the failure mode was
      not "an operator forgot", it was "the default deployment is public". A warning would scroll
      past in a platform log; refusing cannot be missed. Unset remains fine for local work, so
      running the app on a laptop needs no configuration.
    * **Wildcard CORS** refuses in production too, which is the change item 3 of the security phase
      asked for. It previously only warned, on the grounds that an operator might terminate CORS at
      a proxy — a real scenario, so the escape hatch is to set ``CORS_ORIGINS`` explicitly rather
      than to leave the wildcard and hope.

    Both are gated on ``ENVIRONMENT``, which is how the repository already distinguishes a
    developer machine from a deployment.
    """
    production = not settings.is_local_environment
    problems: list[str] = []

    if not settings.auth_enabled:
        if production:
            problems.append(
                "API_AUTH_TOKEN is unset, which leaves every /api and /clips route open to "
                "anyone who can reach this host"
            )
        else:
            logger.warning(
                "API_AUTH_TOKEN is unset with environment=%r, so every /api and /clips route "
                "is open. Fine for local work; set a token before exposing this host. A "
                "production environment refuses to boot without one.",
                settings.environment,
            )

    if settings.cors_allow_wildcard:
        if production:
            problems.append(
                "CORS_ORIGINS is '*', which lets any website call this API from a visitor's "
                "browser; set an explicit origin list"
            )
        else:
            logger.warning(
                "CORS_ORIGINS is '*' with environment=%r. Set an explicit origin list for "
                "a public deployment; credentialed cross-origin requests are also disabled "
                "while the wildcard is in use.",
                settings.environment,
            )

    if problems:
        joined = "; ".join(problems)
        raise InsecureDeploymentError(
            f"Refusing to start with environment={settings.environment!r}: {joined}. "
            "Set the variables, or set ENVIRONMENT=development if this really is a local run."
        )


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
    _check_deployment_security()
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
    # Registered once here rather than on 47 decorators, so a route added later is protected by
    # default instead of by remembering. The dependency exempts /healthz and the docs itself
    # (see api.security._EXEMPT_PATHS), and returns immediately when no token is configured.
    #
    # This does NOT cover the /clips static mount: `dependencies=` injects into routes, and a
    # StaticFiles mount is an ASGI sub-app with no route to inject into. ClipsAuthMiddleware
    # below is what closes that, and it is the reason there are two mechanisms.
    dependencies=[Depends(require_api_token)],
)


# Routers, in the order the routes were registered before the split. Order matters for
# path resolution, and the static mounts below must stay last for the same reason.
app.include_router(system.router)
app.include_router(jobs.router)
app.include_router(metadata.router)
app.include_router(publishing.router)
app.include_router(storage.router)
app.include_router(profiles.router)
app.include_router(watch.router)
app.include_router(clips.router)
# Phase 7. Registered before the static mounts like every other router, so that the SPA
# catch-all mount at "/" cannot shadow "/metrics" - the mount is last precisely because it
# swallows any path no route claimed first.
app.include_router(metrics.router)


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

# Added after CORSMiddleware and therefore *inside* it: Starlette applies middleware in reverse
# registration order, so CORS headers are still attached to the 401 this can return. Without that
# a browser reports a cross-origin failure instead of the actual authentication error.
app.add_middleware(ClipsAuthMiddleware)


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
        rows.append(
            _row("ffmpeg", resolved or f"NOT FOUND ({settings.ffmpeg_binary})", bool(resolved))
        )
    except Exception:
        rows.append(_row("ffmpeg", "could not be resolved", ok=False))

    try:
        rows.append(_row("Whisper model", str(settings.whisper_model)))
        rows.append(_row("Storage backend", str(settings.storage_backend.value)))
    except Exception:
        pass

    try:
        jobs = get_manager().store.all()
        # One definition of "active", shared with the metrics gauge and mirrored by the
        # frontend. Spelled as a literal tuple here once, which is how adding a status could
        # have made a busy instance report as idle.
        active = sum(1 for j in jobs if j.status in ACTIVE_JOB_STATUSES)
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
        names = (
            ", ".join(
                f"{e['id']}{'' if e.get('available', True) else ' (unavailable)'}" for e in engines
            )
            or "none registered"
        )
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
