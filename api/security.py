"""Request authentication, per-user authorization (U12), and rate limiting.

**Two authentication schemes live here, and they do not stack.** ``AUTH_ENABLED`` selects
between them:

* **on** — accounts. A session cookie (or bearer token) identifies a :class:`~auth.store.User`,
  and ``/clips`` media is additionally checked against the owning job. This is U12.
* **off** — the single shared secret, ``API_AUTH_TOKEN``. This is what main already shipped and
  what most installs are running; it stays in force so that turning accounts *off*, or upgrading
  to a build that has them, cannot silently reopen a deployment that was closed.

Selecting rather than combining is the decision worth defending. Requiring both would mean a
browser needs a secret *and* an account, so the login page could not load without the secret
already in hand — and the secret would then be a credential shared by every user, which is the
thing accounts exist to replace. Requiring either would make the shared secret a permanent
bypass of per-user ownership: anyone holding it could read every user's clips. So with accounts
on, the secret is not consulted at all, and ``require_api_token`` and ``ClipsAuthMiddleware``
both become no-ops.

Rate limiting is orthogonal to both and applies in either mode: a session says who you are, not
how much CPU you may spend. :class:`RateLimiter` guards the expensive routes;
:class:`LoginRateLimiter` guards password guessing. They are separate because the budgets differ
by two orders of magnitude and one is keyed by username.

**One choke point for authentication, deliberately.** It is an ASGI middleware rather than a set
of ``Depends(...)`` on 45 handlers, for two reasons that are not stylistic:

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

import hmac
import json
import logging
import re
import threading
import time

from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

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


def may_access_job(job: object, user: User | None) -> bool:
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


def current_user(request: Request) -> User | None:
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


def client_identity(request: Request) -> str:
    """A stable key for the caller, for rate limiting only.

    ``X-Forwarded-For`` is consulted only when ``TRUST_FORWARDED_FOR`` says to, and the
    distinction is load-bearing in both directions. Directly exposed, the header is
    caller-supplied and trusting it lets one client present as unlimited distinct clients — the
    limiter would be decorative. Behind a proxy that sets it, ignoring it makes every request
    look like the proxy, so all callers share one bucket and the limiter throttles the whole
    deployment at once.

    The left-most entry is taken: proxies append, so the original client is first.

    This replaces U12's ``client_key``, which unconditionally ignored the header. Both branches
    reasoned correctly about the risk they were looking at and reached opposite conclusions;
    the setting is what lets a deployment answer for itself, and one definition means the login
    limiter and the route limiter cannot disagree about who a caller is.
    """
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for") or ""
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client and client.host else "unknown"


#: U12 named this ``client_key``. Kept as an alias rather than renaming the call sites in
#: ``api/main.py``, because the two names read correctly in their own contexts and an alias
#: cannot drift from the implementation.
client_key = client_identity


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
            await _send_json(send, 401, {"detail": "Not authenticated. Sign in to continue."})
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
        remainder = path[len(CLIPS_PREFIX) :]
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


def bootstrap_admin() -> User | None:
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


# ---------------------------------------------------------------------------------------------
# The single-tenant scheme: one shared secret. Active only while AUTH_ENABLED is off.
#
# This is main's implementation, carried through the U12 merge unchanged in behaviour and gated
# on `auth_enabled`. Keeping it is not tidiness: API_AUTH_TOKEN is what closed the deployment
# described in `render.yaml`, most installs are running it, and a build that quietly stopped
# honouring it because a *different* auth feature was added would reopen every one of them. With
# accounts on it is not consulted, for the reasons in the module docstring.
# ---------------------------------------------------------------------------------------------

#: Paths that never require the secret.
#:
#: ``/healthz`` must stay open or container health checks and uptime probes fail — it returns
#: ``{"status": "ok"}`` and nothing else, so it leaks nothing. The docs endpoints are FastAPI
#: defaults; they describe the API rather than exposing data, and locking them would make the
#: deployment undebuggable for its own operator.
#:
#: Deliberately a *different* list from :data:`EXEMPT_EXACT`, which governs sessions. The two
#: schemes exempt different things for different reasons — ``/api/auth/login`` has to be reachable
#: without a session but has no reason to be reachable without the shared secret, and ``/docs``
#: is the reverse — so merging them would widen both to the union of their exemptions.
_SECRET_EXEMPT_PATHS = frozenset(
    {"/healthz", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
)

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
#:
#: With accounts on, the equivalent problem is solved by the session *cookie*, which a browser
#: does attach to ``<video src>`` — which is why U12 needs no query-parameter escape hatch.
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


def extract_api_token(request: Request) -> str | None:
    """Pull the shared secret from a request, preferring headers.

    Named apart from :func:`extract_token`, which pulls a U12 *session* token from a cookie or
    bearer header. Both branches called their extractor ``extract_token``; they return different
    kinds of credential, and one name for two would be the sort of collision that type-checks.

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


def secret_is_the_active_scheme() -> bool:
    """Whether the shared secret is the credential right now.

    One place to ask, so the dependency and the middleware cannot answer differently — which is
    the failure this merge was most able to introduce, given each of them lived in a different
    branch.
    """
    return not settings.auth_enabled


def require_api_token(request: Request) -> None:
    """Reject a request that does not carry the shared secret.

    Registered once as an app-level dependency, so it applies to every route including any added
    later. Returns ``None`` — it is a guard, not a provider of a value.

    ``401`` with ``WWW-Authenticate`` rather than ``403``: the caller has supplied no usable
    credential, which is what 401 means, and the header tells a client what to send.

    A no-op when accounts are on: :class:`AuthMiddleware` has already established who the caller
    is, and demanding a second, shared credential on top would make the login page unreachable
    for anyone who did not already hold it.
    """
    if not secret_is_the_active_scheme():
        return
    if request.url.path in _SECRET_EXEMPT_PATHS:
        return
    if token_matches(extract_api_token(request)):
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

    A no-op when accounts are on, where :class:`AuthMiddleware` guards the same prefix and does
    strictly more — it checks the *owning job*, not merely that the caller has a credential.
    """

    async def dispatch(self, request: Request, call_next):
        if (
            secret_is_the_active_scheme()
            and request.url.path.startswith(_CLIPS_MOUNT_PREFIX)
            and not token_matches(extract_api_token(request))
        ):
            return JSONResponse(
                {"detail": "Missing or invalid API token."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


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

    Distinct from :class:`LoginRateLimiter`, which counts *failed logins* per username and client
    over a 15-minute window. Same shape, different budgets by two orders of magnitude, and only
    one of them is keyed by a name a caller supplies — collapsing them into one would mean a
    password guess and a render request sharing a bucket.
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

    Applies in **both** auth modes, unlike the shared secret. An authenticated user is still a
    caller who can queue unbounded transcode work, and with accounts on there are more of them.
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
