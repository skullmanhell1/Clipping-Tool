"""Request authentication and per-user authorization (U12).

**One choke point, deliberately.** Authentication is an ASGI middleware rather than a set of
``Depends(...)`` on 45 handlers, for two reasons that are not stylistic:

* ``app.mount("/clips", StaticFiles(...))`` is not a route, so **route dependencies do not
  apply to it**. The clip media - the actual product - would stay world-readable while every
  JSON endpoint looked protected. A middleware sees mounts.
* A global default is fail-closed. Per-route dependencies mean a handler added later is
  public until someone remembers; here it is protected until someone adds it to
  :data:`EXEMPT_EXACT`, and that list is short enough to review.

It is also *pure* ASGI rather than ``BaseHTTPMiddleware``: an unauthenticated request is
answered from the scope without ever constructing the downstream response, and an authorized
one is passed through untouched. That last part matters for ``/clips`` — ``StaticFiles``
implements HTTP Range, which is what makes the review player able to seek, and wrapping its
response is a good way to break scrubbing without breaking any test.

Authorization is separate and lives with the handlers, because only they know which job a
request refers to. :func:`may_access_job` is the single rule.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from fastapi import HTTPException, Request
from starlette.datastructures import Headers

from auth import get_auth_store
from auth.store import User
from config import settings

logger = logging.getLogger(__name__)

#: Paths served without a session when auth is on.
#:
#: Each entry is here for a stated reason, because "why is this public?" is the only
#: question that matters about this list:
#:
#: * ``/healthz`` - a liveness probe has no credentials and must not need any. It returns
#:   status only.
#: * ``/api/auth/login`` - the request that obtains a session cannot require one.
#: * ``/api/auth/config`` - one boolean, ``auth_enabled``, so the SPA knows whether to render
#:   a login form at all. Without it the frontend cannot tell "auth is off" from "auth is on
#:   and I am logged out", and a single-tenant install would show a login box for an account
#:   system it does not have.
#: * ``/``, ``/index.html``, ``/favicon.ico`` - the SPA shell. It is a static bundle that can
#:   do nothing without an authenticated API, and it has to load in order to show the login
#:   form.
#:
#: Note what is *not* here: ``/api/info`` (it enumerates capabilities, configured providers
#: and the storage backend), and ``/docs``/``/openapi.json``/``/redoc`` (they describe every
#: endpoint). None is catastrophic to leak and all of them are avoidable.
EXEMPT_EXACT = frozenset(
    {
        "/healthz",
        "/api/auth/login",
        "/api/auth/config",
        "/",
        "/index.html",
        "/favicon.ico",
    }
)

#: Prefixes served without a session: the SPA's hashed asset bundles, which the shell
#: cannot render without.
EXEMPT_PREFIXES = ("/assets/",)

#: Media lives under this prefix and is checked against the owning job rather than merely
#: requiring a session - otherwise any logged-in user could read every other user's clips
#: by path, which is the whole thing this feature exists to prevent.
CLIPS_PREFIX = "/clips/"


def is_exempt(path: str) -> bool:
    """Whether ``path`` is served without a session."""
    if path in EXEMPT_EXACT:
        return True
    return path.startswith(EXEMPT_PREFIXES)


def extract_token(headers: Headers) -> str:
    """The session token from a cookie or a bearer header, or ``""``.

    Both are accepted. The cookie is what the SPA uses; the bearer header is for scripts and
    the CLI, which have no cookie jar. The cookie is preferred when both are present, on the
    grounds that a browser sends it automatically and an explicit header is more likely to be
    a stale copy pasted into a shell.
    """
    cookie_name = str(settings.auth_session_cookie or "clipper_session")
    raw_cookie = headers.get("cookie") or ""
    for part in raw_cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == cookie_name and value:
            return value.strip()

    authorization = headers.get("authorization") or ""
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip()
    return ""


def may_access_job(job: object, user: Optional[User]) -> bool:
    """Whether ``user`` may see ``job``. The single authorization rule.

    * No job - no access. A caller with a job id that resolves to nothing gets the same
      answer as one asking for someone else's, so the two are not distinguishable.
    * An admin sees everything, which is what makes an operator able to support users.
    * Otherwise the job's ``owner`` must be exactly this user's id.

    A job with no owner (created before U12, or while auth was off) is therefore **visible
    to admins only**. The alternative - treating unowned as public - would hand a stranger's
    whole library to the first account created after enabling auth.
    """
    if job is None:
        return False
    if user is None:
        # Only reachable with auth disabled, where the middleware never consults this and
        # ownership is not a concept. Returning True keeps the single-tenant path identical.
        return not settings.auth_enabled
    if user.is_admin:
        return True
    return str(getattr(job, "owner", "") or "") == user.id


def current_user(request: Request) -> Optional[User]:
    """The authenticated user, or ``None`` when auth is disabled."""
    return getattr(request.state, "user", None)


def require_admin(request: Request) -> User:
    """The authenticated user, if they are an admin. Raises 401/403 otherwise."""
    if not settings.auth_enabled:
        raise HTTPException(
            status_code=404,
            detail="User administration is unavailable because AUTH_ENABLED is off.",
        )
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="This action requires an administrator.")
    return user


class LoginRateLimiter:
    """Failed-login counter, per username and client address, in memory.

    In memory because the app is a single process with one worker thread; a shared store
    would be the right answer for several replicas and is not what this deploys as. The
    consequence - a restart forgets the counters - is acceptable for a brute-force guard and
    is preferable to the alternative of a database write on every failed login, which is
    itself a way to be attacked.

    Keyed on username **and** client, so one user under attack cannot lock out everyone, and
    an attacker rotating usernames from one address still accumulates a count.
    """

    def __init__(self) -> None:
        self._attempts: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def _window(self) -> float:
        return max(1.0, float(settings.auth_login_window_seconds or 900.0))

    def _limit(self) -> int:
        return int(settings.auth_login_max_attempts or 0)

    def check(self, username: str, client: str) -> bool:
        """Whether another attempt is allowed right now."""
        limit = self._limit()
        if limit <= 0:
            return True
        cutoff = time.time() - self._window()
        key = (username, client)
        with self._lock:
            recent = [t for t in self._attempts.get(key, []) if t > cutoff]
            self._attempts[key] = recent
            return len(recent) < limit

    def record_failure(self, username: str, client: str) -> None:
        cutoff = time.time() - self._window()
        key = (username, client)
        with self._lock:
            recent = [t for t in self._attempts.get(key, []) if t > cutoff]
            recent.append(time.time())
            self._attempts[key] = recent

    def clear(self, username: str, client: str) -> None:
        """Forget the failures for one key, called after a successful login."""
        with self._lock:
            self._attempts.pop((username, client), None)

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()


login_rate_limiter = LoginRateLimiter()


def client_key(request: Request) -> str:
    """A stable-enough client identifier for rate limiting.

    ``request.client.host`` is the peer address. Behind a reverse proxy that is the proxy,
    which would make the limit global rather than per client. ``X-Forwarded-For`` is
    deliberately **not** consulted: it is caller-supplied and trivially rotated, so honouring
    it would let an attacker reset their own budget by changing a header - strictly worse
    than a shared bucket.
    """
    client = request.client
    return client.host if client and client.host else "unknown"


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AuthMiddleware:
    """Require a session, and gate ``/clips`` media on the owning job (U12)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        # Settings are read *per request*, not captured at construction. The middleware is
        # installed unconditionally at import time, so a value captured here could never be
        # changed afterwards - which is exactly the trap documented for the CORS middleware,
        # where tests cannot reach an import-time decision.
        if scope["type"] != "http" or not settings.auth_enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        if is_exempt(path):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        # No `if token` guard: resolve_session already returns None for an empty token, and
        # stating that twice would mean one of the two copies is never exercised.
        resolved = get_auth_store().resolve_session(extract_token(headers))
        if resolved is None:
            await _send_json(
                send, 401, {"detail": "Not authenticated. Sign in to continue."}
            )
            return

        user, _session = resolved
        # `request.state` reads through to scope["state"], so this is how a handler sees the
        # authenticated user without the middleware having to construct a Request.
        scope.setdefault("state", {})["user"] = user

        if path.startswith(CLIPS_PREFIX) and not self._may_read_media(path, user):
            # 404, not 403: a wrong guess about someone else's job id should not be
            # answerable, and "this exists but is not yours" is an answer.
            await _send_json(send, 404, {"detail": "Not found."})
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _may_read_media(path: str, user: User) -> bool:
        """Whether ``user`` may read a ``/clips/<job_id>/...`` path.

        Fails closed when there is no job record for the id. Files can outlive their record
        - the job store is pruned to ``MAX_PERSISTED_JOBS`` while the media stays on disk -
        and serving those to anyone with a session would leak precisely the clips of the
        users who have been using the tool longest.
        """
        remainder = path[len(CLIPS_PREFIX):]
        job_id = remainder.split("/", 1)[0]
        # No empty-job_id guard: an empty id finds no job, and `may_access_job(None, ...)`
        # already refuses. A separate check here would be a second statement of the same
        # rule, and the copy that is never reached is the one free to be wrong.
        #
        # Imported here rather than at module scope: api.main imports this module, and
        # worker.jobs pulls in the pipeline, so a top-level import would build the whole
        # worker import graph to answer a static file request.
        from worker.jobs import get_manager

        return may_access_job(get_manager().store.get(job_id), user)


def bootstrap_admin() -> Optional[User]:
    """Create the configured first admin when auth is on and there are no users.

    Raises ``RuntimeError`` when auth is enabled, no user exists, and no bootstrap account is
    configured. That is a loud failure at startup rather than a server that accepts requests
    and can only refuse them: with an empty user table every login fails, so the alternative
    is a deployment that looks healthy, answers 401 to everything, and gives no clue why.
    """
    if not settings.auth_enabled:
        return None
    store = get_auth_store()
    if store.count_users() > 0:
        return None

    username = (settings.auth_bootstrap_username or "").strip()
    password = settings.auth_bootstrap_password or ""
    if not username or not password:
        raise RuntimeError(
            "AUTH_ENABLED is on but there are no user accounts and no bootstrap admin is "
            "configured. Set AUTH_BOOTSTRAP_USERNAME and AUTH_BOOTSTRAP_PASSWORD (then "
            "change the password after signing in), or set AUTH_ENABLED=false."
        )
    user = store.create_user(username, password, is_admin=True)
    logger.warning(
        "U12: created bootstrap admin %r from AUTH_BOOTSTRAP_USERNAME. Change this "
        "password after signing in.",
        user.username,
    )
    return user
