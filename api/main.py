"""FastAPI application entrypoint.

Boots a minimal, production-shaped web app:

* ``GET /``          -> dark-themed placeholder landing page
* ``GET /healthz``   -> liveness probe (JSON)
* ``GET /api/info``  -> basic app/config info (JSON)

No pipeline features are wired up yet -- this is the foundation the later
phases build on. Run locally with::

    uvicorn api.main:app --reload

or via Docker Compose (see docker-compose.yml).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: ensure local storage directories exist on boot."""
    settings.ensure_local_dirs()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="AI-powered video clipping & auto-publishing tool (scaffold).",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz", tags=["system"])
def healthz() -> dict[str, str]:
    """Liveness probe used by Docker/Kubernetes health checks."""
    return {"status": "ok"}


@app.get("/api/info", tags=["system"])
def info() -> dict[str, object]:
    """Return non-sensitive application/configuration information."""
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "version": app.version,
        "llm_provider": settings.llm_provider.value,
        "storage_backend": settings.storage_backend.value,
    }


@app.get("/", response_class=HTMLResponse, tags=["ui"])
def index() -> str:
    """Serve a self-contained, dark-themed placeholder landing page."""
    return _PLACEHOLDER_PAGE


# ---------------------------------------------------------------------------
# Inline placeholder page. The full React + Tailwind UI lives in ``frontend/``;
# this keeps the API self-contained and immediately viewable before the SPA is
# built and served behind a reverse proxy.
# ---------------------------------------------------------------------------
_PLACEHOLDER_PAGE = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{settings.app_name}</title>
  <style>
    :root {{
      --bg: #0b0f17;
      --panel: #131a26;
      --border: #223049;
      --text: #e6edf3;
      --muted: #8b98a9;
      --accent: #6c5ce7;
      --accent-2: #00d2ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: var(--text);
      background:
        radial-gradient(1200px 600px at 20% -10%, rgba(108,92,231,.25), transparent),
        radial-gradient(900px 500px at 100% 0%, rgba(0,210,255,.15), transparent),
        var(--bg);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .card {{
      width: 100%;
      max-width: 640px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 40px;
      box-shadow: 0 20px 60px rgba(0,0,0,.45);
    }}
    .badge {{
      display: inline-block;
      font-size: 12px;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: var(--accent-2);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 4px 12px;
      margin-bottom: 20px;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 30px;
      background: linear-gradient(90deg, var(--accent-2), var(--accent));
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
    }}
    p {{ color: var(--muted); line-height: 1.6; margin: 0 0 20px; }}
    ul {{ color: var(--muted); line-height: 1.8; padding-left: 20px; margin: 0 0 20px; }}
    .links a {{
      color: var(--accent-2);
      text-decoration: none;
      margin-right: 16px;
      font-size: 14px;
    }}
    .links a:hover {{ text-decoration: underline; }}
    .dot {{ color: #2ecc71; }}
  </style>
</head>
<body>
  <main class="card">
    <span class="badge">Scaffold - v0.1.0</span>
    <h1>{settings.app_name}</h1>
    <p><span class="dot">&#9679;</span> The API is up and running. This is a
       placeholder page; the full React + Tailwind dashboard will be served here
       in a later phase.</p>
    <p>Planned pipeline:</p>
    <ul>
      <li>Transcribe long-form video (faster-whisper)</li>
      <li>AI selects the best moments</li>
      <li>Cut clips &amp; reframe to vertical with face-tracking</li>
      <li>Burn in captions, emoji &amp; overlays</li>
      <li>Auto-generate titles / hashtags</li>
      <li>Optional auto-publish to Whop / YouTube / TikTok / Instagram / X</li>
    </ul>
    <div class="links">
      <a href="/docs">API docs</a>
      <a href="/healthz">Health</a>
      <a href="/api/info">Info</a>
    </div>
  </main>
</body>
</html>"""
