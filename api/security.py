"""Inbound request security: a shared secret, and an in-process rate limiter.

Every one of the 47 routes was unauthenticated, and `render.yaml` publishes this with
`autoDeploy: true` — so the deployed state was an open API that downloads arbitrary URLs, writes
to disk, spends LLM tokens and publishes to social accounts.

**A single shared secret is the whole scheme, on purpose.** This is a single-tenant self-hosted
tool. Per-user accounts with per-user storage are plan item `U12` (P2/L), a much larger change
that would need a user model, a session store and a migration; building a cut-down version of it
here would mean shipping something that looks like multi-tenancy and is not.

Two mechanisms are needed rather than one, and the reason is structural:

* ``require_api_token`` is a FastAPI dependency, registered once on the app so it covers all
  routes, present and future. Phase 4 splits ``api/main.py`` into routers; an app-level
  dependency survives that untouched, whereas 47 per-decorator copies would not.
* ``ClipsAuthMiddleware`` exists because ``StaticFiles`` mounts are plain ASGI sub-apps.
  ``dependencies=[...]`` on ``FastAPI(...)`` injects into *routes*, so it never runs for
  ``/clips/...`` — the mount would stay open while looking protected.

Both delegate to :func:`token_matches`, so the comparison itself has one definition.
"""

from __future__ import annotations

import hmac
import logging
import re
import threading
import time

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from config import settings

logger = logging.getLogger(__name__)

#: Paths that never require the secret.
#:
#: ``/healthz`` must stay open or container health checks and uptime probes fail — it returns
#: ``{"status": "ok"}`` and nothing else, so it leaks nothing. The docs endpoints are FastAPI
#: defaults; they describe the API rather than exposing data, and locking them would make the
#: deployment undebuggable for its own operator.
_EXEMPT_PATHS = frozenset({"/healthz", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})

#: The static mount that serves rendered clips and thumbnails off disk.
_CLIPS_MOUNT_PREFIX = "/clips/"

#: Paths where the secret may travel in the query string.
#:
#: The category is precise, and it is not "whatever is convenient": **read-only media retrieval
#: that a browser reaches by navigation rather than by `fetch`.** Three places qualify, and all
#: three are GET requests that return bytes and change nothing:
#:
#:   /clips/...                          <video src> and poster in ClipCard.jsx
#:   /api/clips/{job}/{file}/video       an <a href> download link
#:   /api/clips/{job}/{file}/download    an <a href> download link (zip + metadata)
#:
#: A browser cannot attach a header to any of those, so without this, enabling auth would break
#: playback and downloads in the UI that ships with the app — and the symptom would look like
#: broken video rather than like a security setting.
#:
#: Everything else is header-only, which is the part that matters: no token ever appears in the
#: URL of a request that mutates state, so none reaches an access log, browser history or a
#: `Referer` header for anything that writes.
_QUERY_TOKEN_PATHS = re.compile(r"^(?:/clips/|/api/clips/[^/]+/[^/]+/(?:download|video)$)")

#: How a caller supplies the secret.
_BEARER = "bearer "


def token_matches(supplied: str | None) -> bool:
    """Whether ``supplied`` is the configured secret.

    ``hmac.compare_digest`` rather than ``==``: string comparison returns as soon as it finds a
    difference, so the time it takes reveals how many leading characters were right, and a secret
    that leaks its own prefix can be guessed a character at a time.
    """
    expected = settings.api_auth_token_value
    if not expected:
        return True  # auth disabled; see _run_startup for the warning this produces
    if not supplied:
        return False
    return hmac.compare_digest(supplied.strip(), expected)


def extract_token(request: Request) -> str | None:
    """Pull the secret from a request, preferring headers.

    ``Authorization: Bearer <t>`` and ``X-API-Token: <t>`` are the supported headers, and are the
    only option for anything that changes state. A ``?token=`` query parameter is accepted only
    for the read-only media paths in :data:`_QUERY_TOKEN_PATHS`, which documents why.
    """
    header = request.headers.get("authorization") or ""
    if header.lower().startswith(_BEARER):
        return header[len(_BEARER) :]
    direct = request.headers.get("x-api-token")
    if direct:
        return direct
    if request.method in ("GET", "HEAD") and _QUERY_TOKEN_PATHS.match(request.url.path):
        # Method-checked as well as path-checked. The two /api/clips routes are GET-only today,
        # so this changes nothing now - but if a POST were ever added under one of those paths it
        # would otherwise inherit the query-token allowance silently.
        return request.query_params.get("token")
    return None


def is_exempt(path: str) -> bool:
    """Whether ``path`` is reachable without the secret."""
    return path in _EXEMPT_PATHS


def require_api_token(request: Request) -> None:
    """Reject a request that does not carry the shared secret.

    Registered once as an app-level dependency, so it applies to every route including any added
    later. Returns ``None`` — it is a guard, not a provider of a value.

    ``401`` with ``WWW-Authenticate`` rather than ``403``: the caller has supplied no usable
    credential, which is what 401 means, and the header tells a client what to send.
    """
    if is_exempt(request.url.path):
        return
    if token_matches(extract_token(request)):
        return
    raise HTTPException(
        status_code=401,
        detail="Missing or invalid API token. Send Authorization: Bearer <token>.",
        headers={"WWW-Authenticate": "Bearer"},
    )


class ClipsAuthMiddleware(BaseHTTPMiddleware):
    """Apply the shared secret to the ``/clips`` static mount.

    The mount serves finished clips and thumbnails straight off disk. The two per-clip endpoints
    in ``api.main`` normalise the filename to a basename and (for the zip route) cross-check the
    clip against the job store, but the mount does none of that — so it was both the least
    guarded and the widest path to rendered output.

    Scoped to one prefix rather than wrapping everything: routes are already covered by
    :func:`require_api_token`, and a middleware that duplicated that would give the same rule two
    implementations to drift apart.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(_CLIPS_MOUNT_PREFIX) and not token_matches(
            extract_token(request)
        ):
            return JSONResponse(
                {"detail": "Missing or invalid API token."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


def client_identity(request: Request) -> str:
    """A stable key for the caller, for rate limiting only.

    ``X-Forwarded-For`` is consulted only when ``TRUST_FORWARDED_FOR`` says to, and the
    distinction is load-bearing in both directions. Directly exposed, the header is caller-supplied
    and trusting it lets one client present as unlimited distinct clients — the limiter would be
    decorative. Behind a proxy that sets it, ignoring it makes every request look like the proxy,
    so all callers share one bucket and the limiter throttles the whole deployment at once.

    The left-most entry is taken: proxies append, so the original client is first.
    """
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for") or ""
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client and client.host else "unknown"


class RateLimiter:
    """A fixed-window request counter, keyed by caller.

    In-process and deliberately not Redis. ``redis`` and ``rq`` are declared dependencies that no
    code imports; making the app safe should not be the thing that finally turns an unused
    optional dependency into a mandatory service.

    A fixed window rather than a sliding log: it needs one integer and one timestamp per caller
    instead of a list of arrival times, which matters because the key space is attacker-influenced.
    The cost is that a caller can send up to twice the limit across a window boundary, which for
    protecting expensive routes on a self-hosted tool is an acceptable trade and is stated here
    rather than discovered.

    Thread-safe because the sync endpoints run in Starlette's threadpool, so several requests
    touch this concurrently.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[float, int]] = {}

    def check(self, key: str, *, limit: int, window: float, now: float | None = None) -> bool:
        """Record a request and return whether it is allowed."""
        if limit <= 0 or window <= 0:
            return True
        moment = time.monotonic() if now is None else now
        with self._lock:
            started, count = self._windows.get(key, (moment, 0))
            if moment - started >= window:
                started, count = moment, 0
            count += 1
            self._windows[key] = (started, count)
            # Opportunistic eviction, under the lock we already hold. Without it the dict grows
            # once per distinct client forever, which on a public endpoint is a memory leak an
            # attacker chooses the size of.
            if len(self._windows) > 4096:
                cutoff = moment - window
                self._windows = {k: v for k, v in self._windows.items() if v[0] > cutoff}
            return count <= limit

    def reset(self) -> None:
        """Forget all windows. For tests, and for a settings change at runtime."""
        with self._lock:
            self._windows.clear()


#: Module-level limiter shared by every rate-limited route, so the budget is per client rather
#: than per endpoint - twenty submissions and twenty uploads is forty expensive operations.
limiter = RateLimiter()


def rate_limit(request: Request) -> None:
    """Reject a request from a caller that has exceeded its budget.

    Applied to the expensive routes only — job submission, upload, preview, rerender, regenerate,
    resume and caption preview. Deliberately **not** applied to the read routes: ``App.jsx`` polls
    ``/api/jobs`` every 1200 ms and the publish history alongside it, which is 50 requests a minute
    from a single idle browser, so a 30-per-minute budget on reads would throttle the UI that ships
    with this app rather than an abuser. Phase 5 replaces that poll loop with SSE; limiting reads
    becomes reasonable once it has.
    """
    if not settings.rate_limit_enabled:
        return
    key = client_identity(request)
    allowed = limiter.check(
        key,
        limit=settings.rate_limit_requests,
        window=settings.rate_limit_window_seconds,
    )
    if allowed:
        return
    retry_after = max(1, int(settings.rate_limit_window_seconds))
    raise HTTPException(
        status_code=429,
        detail=(
            f"Rate limit exceeded: more than {settings.rate_limit_requests} requests in "
            f"{settings.rate_limit_window_seconds:g}s. Retry in {retry_after}s."
        ),
        headers={"Retry-After": str(retry_after)},
    )
