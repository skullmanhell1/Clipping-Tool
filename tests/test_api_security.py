"""The API is not open to anyone who can reach the host.

All 47 routes were unauthenticated, and ``render.yaml`` deploys this publicly with
``autoDeploy: true`` — so the shipped state was an open API that downloads arbitrary URLs, writes
to disk, spends LLM tokens and publishes to social accounts.

Three things are asserted here, and the third is the one most likely to be quietly wrong:

1. A configured secret is required, and an unset one still allows everything (an upgrade must not
   lock an existing deployment out of its own installation).
2. Expensive routes are throttled, and cheap polled ones are **not** — the UI polls
   ``/api/jobs`` every 1200 ms, so throttling reads would break the app rather than an abuser.
3. The ``/clips`` static mount is covered. ``dependencies=[...]`` on ``FastAPI(...)`` injects into
   *routes*, and a ``StaticFiles`` mount is an ASGI sub-app with no route to inject into — so the
   obvious wiring protects every endpoint and leaves the entire rendered-video directory open
   while looking finished. That is why a middleware exists alongside the dependency, and why it
   is asserted separately.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import security
from api.main import InsecureDeploymentError, _check_deployment_security, app
from config import settings

TOKEN = "test-shared-secret"


@pytest.fixture(autouse=True)
def _reset_limiter():
    """A fresh rate-limit window per test.

    The limiter is module-level so one budget is shared across routes (twenty uploads and twenty
    previews are forty expensive operations, not two separate allowances). That also means state
    leaks between tests unless it is cleared.
    """
    security.limiter.reset()
    yield
    security.limiter.reset()


@pytest.fixture
def authed(monkeypatch):
    """A client with the shared secret configured but not supplied."""
    monkeypatch.setattr(settings, "api_auth_token", TOKEN)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def open_client(monkeypatch):
    """A client with no secret configured — the default, and the upgrade path."""
    monkeypatch.setattr(settings, "api_auth_token", None)
    with TestClient(app) as client:
        yield client


# --------------------------------------------------------------------------- #
# The secret                                                                    #
# --------------------------------------------------------------------------- #
def test_no_token_configured_allows_everything(open_client):
    """Unset means open, deliberately.

    An existing deployment upgrading to this version must not lose access to itself. The loud
    startup warning and the production boot refusal are what stop that being a silent default;
    see :func:`test_production_refuses_to_boot_without_a_token`.
    """
    assert open_client.get("/api/info").status_code == 200
    assert open_client.get("/api/jobs").status_code == 200


def test_a_configured_token_is_required(authed):
    assert authed.get("/api/info").status_code == 401
    assert authed.get("/api/jobs").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {TOKEN}"},
        {"Authorization": f"bearer {TOKEN}"},  # scheme is case-insensitive per RFC 7235
        {"X-API-Token": TOKEN},
    ],
)
def test_both_header_forms_are_accepted(authed, headers):
    assert authed.get("/api/info", headers=headers).status_code == 200


@pytest.mark.parametrize(
    "supplied",
    ["", "wrong", TOKEN[:-1], TOKEN + "x", TOKEN.upper(), f" {TOKEN}x"],
    ids=["empty", "wrong", "prefix", "suffix", "wrong-case", "trailing-junk"],
)
def test_near_miss_tokens_are_rejected(authed, supplied):
    """Including a correct prefix, which is what a timing attack builds up one byte at a time."""
    response = authed.get("/api/info", headers={"X-API-Token": supplied})
    assert response.status_code == 401


def test_the_401_tells_the_client_what_to_send(authed):
    """A bare 401 with no ``WWW-Authenticate`` leaves a client guessing the scheme."""
    response = authed.get("/api/info")
    assert response.headers.get("www-authenticate") == "Bearer"
    assert "token" in response.json()["detail"].lower()


def test_healthz_stays_open(authed):
    """Container health checks and uptime probes have no way to hold a secret.

    It returns ``{"status": "ok"}`` and nothing else, so leaving it open leaks nothing.
    """
    response = authed.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_a_query_token_does_not_work_on_api_routes(authed):
    """``?token=`` is accepted under ``/clips/`` only, and that narrowness is the design.

    A token in a URL reaches access logs, browser history and ``Referer`` headers. Media has no
    alternative — a browser cannot put a header on ``<video src>`` — but nothing that *changes*
    state should ever carry its credential that way, so the two cases are separated rather than
    one blanket rule being applied for convenience.
    """
    assert authed.get(f"/api/info?token={TOKEN}").status_code == 401


def test_whitespace_only_token_counts_as_unset(monkeypatch):
    """``API_AUTH_TOKEN=" "`` is a mistake, not a secret.

    Reading it literally would require a token nobody can guess and nobody intended, which
    presents as the application being broken rather than as being misconfigured.
    """
    monkeypatch.setattr(settings, "api_auth_token", "   ")
    # `api_token_configured`, not `auth_enabled`: the U12 merge took the latter name for the
    # accounts field, so the property that answers "is a shared secret set" was renamed.
    assert settings.api_token_configured is False
    with TestClient(app) as client:
        assert client.get("/api/info").status_code == 200


def test_every_api_route_is_covered_by_the_dependency(authed):
    """Spot-check across tag groups, so the app-level registration is not assumed.

    Registered once on ``FastAPI(...)`` rather than on 47 decorators, which is also what makes the
    Phase 4 router split safe: an app-level dependency survives moving routes between modules,
    whereas 47 copies would have to be re-applied correctly every time.
    """
    for path in (
        "/api/info",
        "/api/jobs",
        "/api/publishers",
        "/api/history",
        "/api/storage",
        "/api/profiles",
        "/api/watch",
        "/api/updates",
        "/api/schedule",
    ):
        assert authed.get(path).status_code == 401, f"{path} was reachable without a token"


# --------------------------------------------------------------------------- #
# The /clips static mount                                                       #
# --------------------------------------------------------------------------- #
def test_the_clips_mount_requires_the_token(authed):
    """The widest path to rendered output, and the one a route dependency cannot reach."""
    assert authed.get("/clips/job1/clip.mp4").status_code == 401


def test_the_clips_mount_accepts_a_query_token(authed):
    """A 404 here is the pass condition: auth was satisfied, the file simply does not exist.

    ``ClipCard.jsx`` renders clip media as ``<video src>`` and ``poster``, which cannot carry a
    header — so without this, turning auth on would break playback in the UI that ships with the
    app, and the failure would look like broken video rather than like a security setting.
    """
    assert authed.get(f"/clips/job1/clip.mp4?token={TOKEN}").status_code == 404


def test_the_clips_mount_rejects_a_wrong_query_token(authed):
    assert authed.get("/clips/job1/clip.mp4?token=nope").status_code == 401


def test_the_clips_mount_accepts_a_header_too(authed):
    """Programmatic callers should not have to put the secret in a URL."""
    response = authed.get("/clips/job1/clip.mp4", headers={"X-API-Token": TOKEN})
    assert response.status_code == 404


def test_the_clips_mount_is_open_when_no_token_is_configured(open_client):
    assert open_client.get("/clips/job1/clip.mp4").status_code == 404


# --------------------------------------------------------------------------- #
# Rate limiting                                                                 #
# --------------------------------------------------------------------------- #
#: A rate-limited route that fails fast and touches no network.
#:
#: ``/api/preview`` and ``/api/jobs/url`` would be the obvious subjects, but their handlers reach
#: the internet through yt-dlp — and a test that needs the public internet fails for reasons
#: unrelated to this repository, which is the reasoning ``tests/test_url_ingest.py`` already
#: records for serving its fixtures locally. Resuming an unknown job returns 404 immediately.
#:
#: The limiter is a dependency, so it runs *before* the handler either way. That ordering is also
#: deliberate: an endpoint that rejects its input must not be a free way to make the server work.
_CHEAP_LIMITED_ROUTE = "/api/jobs/does-not-exist/resume"


def test_expensive_routes_are_throttled(open_client, monkeypatch):
    """The budget applies, and the 429 says when to come back."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_requests", 3)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60.0)

    codes = [open_client.post(_CHEAP_LIMITED_ROUTE).status_code for _ in range(4)]
    assert codes[:3] == [404, 404, 404], codes
    assert codes[-1] == 429, codes


def test_the_429_carries_retry_after(open_client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_requests", 1)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 30.0)
    open_client.post(_CHEAP_LIMITED_ROUTE)
    response = open_client.post(_CHEAP_LIMITED_ROUTE)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"


def test_the_limiter_covers_every_expensive_route(open_client, monkeypatch):
    """The eight routes that spend disk, network, CPU or LLM tokens all share one budget.

    Checked by exhausting the budget on one route and observing another refuse — which asserts
    both that the decorator is present and that the allowance is per client rather than per
    endpoint. Twenty submissions plus twenty uploads is forty expensive operations, not two
    separate allowances of twenty.
    """
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_requests", 1)
    assert open_client.post(_CHEAP_LIMITED_ROUTE).status_code == 404
    for path in (
        "/api/preview",
        "/api/jobs/url",
        "/api/jobs/batch",
        "/api/captions/preview",
        "/api/jobs/j/clips/c/rerender",
        "/api/jobs/j/clips/c/regenerate",
    ):
        # 429 before the handler runs, so no URL is fetched and no body is needed.
        assert open_client.post(path, json={}).status_code == 429, path


def test_polled_read_routes_are_not_throttled(open_client, monkeypatch):
    """The deliberate exclusion, asserted so nobody "completes" the limiter by adding reads.

    ``App.jsx`` polls ``/api/jobs`` every 1200 ms and the publish history alongside it — roughly
    50 requests a minute from a single idle browser, well past the default budget of 30. Limiting
    reads would therefore throttle the shipped UI rather than an abuser. Phase 5 replaces that
    poll loop with SSE; this exclusion should be revisited then, and this test is where to start.
    """
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_requests", 2)
    codes = {open_client.get("/api/jobs").status_code for _ in range(10)}
    assert codes == {200}, codes


def test_rate_limiting_can_be_switched_off(open_client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_requests", 1)
    codes = {open_client.post(_CHEAP_LIMITED_ROUTE).status_code for _ in range(5)}
    assert codes == {404}, codes


def test_the_budget_is_per_client_not_global():
    """One noisy caller must not exhaust everyone else's allowance."""
    limiter = security.RateLimiter()
    assert limiter.check("1.2.3.4", limit=1, window=60)
    assert not limiter.check("1.2.3.4", limit=1, window=60)
    assert limiter.check("5.6.7.8", limit=1, window=60), "a different client shares the budget"


def test_the_window_expires():
    """A fixed window, driven by an injected clock rather than by sleeping."""
    limiter = security.RateLimiter()
    assert limiter.check("c", limit=1, window=10, now=1000.0)
    assert not limiter.check("c", limit=1, window=10, now=1005.0)
    assert limiter.check("c", limit=1, window=10, now=1011.0), "window did not roll over"


def test_the_window_table_does_not_grow_without_bound():
    """The key space is attacker-influenced, so unbounded growth is a memory leak by request."""
    limiter = security.RateLimiter()
    for index in range(5000):
        limiter.check(f"client-{index}", limit=1, window=1, now=float(index))
    assert len(limiter._windows) < 5000


def test_forwarded_for_is_ignored_unless_trusted(monkeypatch):
    """Both directions of this are load-bearing, so both are asserted.

    Directly exposed, believing the header lets one caller present as unlimited distinct clients
    and the limiter becomes decorative. Behind a proxy that sets it, ignoring the header makes
    every request look like the proxy, so one bucket throttles the whole deployment at once.
    """
    from starlette.requests import Request

    def make(host: str, forwarded: str) -> Request:
        return Request(
            {
                "type": "http",
                "client": (host, 1234),
                "headers": [(b"x-forwarded-for", forwarded.encode())],
            }
        )

    monkeypatch.setattr(settings, "trust_forwarded_for", False)
    assert security.client_identity(make("10.0.0.1", "1.2.3.4")) == "10.0.0.1"

    monkeypatch.setattr(settings, "trust_forwarded_for", True)
    # Left-most entry: proxies append, so the original client is first.
    assert security.client_identity(make("10.0.0.1", "1.2.3.4, 10.0.0.9")) == "1.2.3.4"


# --------------------------------------------------------------------------- #
# Startup refusal                                                               #
# --------------------------------------------------------------------------- #
def test_production_refuses_to_boot_without_a_token(monkeypatch):
    """A warning would scroll past in a platform log; a refusal cannot be missed.

    The failure mode being guarded is not "an operator forgot to set a variable" — it is that
    ``render.yaml`` publishes this with ``autoDeploy: true``, so the *default* deployment was
    public. That warrants refusing rather than noting.
    """
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_auth_token", None)
    monkeypatch.setattr(settings, "cors_origins", "https://app.example.com")
    with pytest.raises(InsecureDeploymentError, match="API_AUTH_TOKEN"):
        _check_deployment_security()


def test_production_refuses_to_boot_with_wildcard_cors(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_auth_token", TOKEN)
    monkeypatch.setattr(settings, "cors_origins", "*")
    with pytest.raises(InsecureDeploymentError, match="CORS_ORIGINS"):
        _check_deployment_security()


def test_the_refusal_names_every_problem_at_once(monkeypatch):
    """Two misconfigurations should not require two deploy cycles to discover."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_auth_token", None)
    monkeypatch.setattr(settings, "cors_origins", "*")
    with pytest.raises(InsecureDeploymentError) as caught:
        _check_deployment_security()
    message = str(caught.value)
    assert "API_AUTH_TOKEN" in message and "CORS_ORIGINS" in message


def test_a_correctly_configured_production_boots(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_auth_token", TOKEN)
    monkeypatch.setattr(settings, "cors_origins", "https://app.example.com")
    _check_deployment_security()


@pytest.mark.parametrize("environment", ["development", "dev", "local", "test", "  DEV  "])
def test_local_environments_only_warn(monkeypatch, caplog, environment):
    """Running on a laptop must need no configuration at all."""
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "api_auth_token", None)
    monkeypatch.setattr(settings, "cors_origins", "*")
    with caplog.at_level("WARNING"):
        _check_deployment_security()
    assert "API_AUTH_TOKEN" in caplog.text
    assert "CORS_ORIGINS" in caplog.text


def test_staging_is_treated_as_a_deployment(monkeypatch):
    """``.env.example`` documents development | staging | production.

    Only the four local names are exempt, so anything else — including a typo — is treated as a
    deployment. Failing closed is the right default for a security check.
    """
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "api_auth_token", None)
    monkeypatch.setattr(settings, "cors_origins", "*")
    with pytest.raises(InsecureDeploymentError):
        _check_deployment_security()
