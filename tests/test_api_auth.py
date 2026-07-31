"""API authentication and SSRF-guard tests.

Both features are inert by default, which is the point: an unset ``API_AUTH_TOKEN`` must
leave every existing caller and every existing test untouched. So the interesting
assertions here are in both directions - that the guard bites when configured, and that it
does not exist when it is not.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config import settings
from worker import download as dl

client = TestClient(app)

TOKEN = "test-secret-token"


def _basic(user: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


# --------------------------------------------------------------------------- #
# Auth disabled (the default) - nothing may change
# --------------------------------------------------------------------------- #
def test_no_token_configured_leaves_routes_open():
    """The default install has no auth, and must behave exactly as it always did."""
    assert settings.api_auth_token is None
    assert client.get("/api/info").status_code == 200


def test_no_token_configured_ignores_a_presented_secret():
    """A caller sending credentials to an open instance is not rejected for it."""
    assert client.get("/api/info", headers={"X-API-Key": "anything"}).status_code == 200


# --------------------------------------------------------------------------- #
# Auth enabled
# --------------------------------------------------------------------------- #
@pytest.fixture()
def auth_on(monkeypatch):
    # Patch the settings *instance*, matching the existing convention in
    # tests/test_assets_expansion.py. Setting the attribute on the class does not work:
    # the pydantic instance carries its own value and would shadow it, so the middleware
    # would keep reading None and every assertion below would silently pass for the wrong
    # reason.
    monkeypatch.setattr(settings, "api_auth_token", TOKEN, raising=False)
    yield TOKEN


def test_missing_credentials_are_rejected(auth_on):
    resp = client.get("/api/info")
    assert resp.status_code == 401
    # The Basic challenge is what makes a browser prompt, which is how the SPA reaches a
    # protected instance without shipping the secret in JavaScript.
    assert resp.headers["www-authenticate"].startswith("Basic ")


def test_wrong_credentials_are_rejected(auth_on):
    assert client.get("/api/info", headers={"X-API-Key": "wrong"}).status_code == 401


@pytest.mark.parametrize(
    "headers_factory",
    [
        lambda t: {"X-API-Key": t},
        lambda t: {"Authorization": f"Bearer {t}"},
        lambda t: _basic("operator", t),
        # The Basic username is deliberately ignored: there is one operator and one secret.
        lambda t: _basic("", t),
        lambda t: _basic("anything-at-all", t),
    ],
)
def test_every_accepted_credential_form_works(auth_on, headers_factory):
    resp = client.get("/api/info", headers=headers_factory(auth_on))
    assert resp.status_code == 200


def test_healthz_is_exempt_so_orchestrators_can_probe_a_locked_instance(auth_on):
    assert client.get("/healthz").status_code == 200


def test_clips_mount_is_protected(auth_on):
    """The StaticFiles mount was serving every rendered clip unauthenticated.

    It cannot take a per-route dependency, which is why the gate is middleware.
    """
    assert client.get("/clips/does-not-exist.mp4").status_code == 401


def test_spa_shell_is_reachable_without_credentials(auth_on):
    """The browser must be able to load the page that then prompts for credentials."""
    resp = client.get("/")
    assert resp.status_code != 401


def test_preflight_is_exempt(auth_on):
    """A CORS preflight carries no credentials by definition.

    Rejecting it would surface in a browser as an unexplained CORS failure rather than a
    401, which is a much worse thing to debug.
    """
    resp = client.options(
        "/api/info",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code != 401


def test_malformed_basic_header_is_rejected_not_crashed(auth_on):
    assert client.get(
        "/api/info", headers={"Authorization": "Basic not-valid-base64!!"}
    ).status_code == 401


# --------------------------------------------------------------------------- #
# SSRF guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/video.mp4",
        "http://localhost/video.mp4",
        # The one that matters most: cloud instance metadata, which hands out credentials.
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/admin",
        "http://10.0.0.5/internal",
        "http://172.16.0.1/internal",
        "http://[::1]/video.mp4",
        "http://0.0.0.0/video.mp4",
    ],
)
def test_private_and_metadata_targets_are_refused(url):
    with pytest.raises(dl.UnsafeURLError):
        dl.assert_safe_url(url, allow_private=False)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/v.mp4", "gopher://x/"])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(dl.UnsafeURLError):
        dl.assert_safe_url(url, allow_private=False)


def test_url_without_host_is_refused():
    with pytest.raises(dl.UnsafeURLError):
        dl.assert_safe_url("http:///no-host", allow_private=False)


def test_opt_in_allows_private_targets():
    """Clipping from a media server on your own LAN is a legitimate thing to want."""
    dl.assert_safe_url("http://192.168.1.50/video.mp4", allow_private=True)


def test_public_literal_address_is_allowed():
    dl.assert_safe_url("https://8.8.8.8/video.mp4", allow_private=False)


def test_unresolvable_host_is_allowed_through_to_fail_at_the_fetch():
    """A name that does not resolve is not an SSRF risk.

    There is nothing to reach, so the guard defers and lets the download fail with an
    accurate message. Refusing here would misdescribe a DNS problem as a security one, and
    would make the guard require working DNS for any ingest at all.
    """
    dl.assert_safe_url("https://this-host-does-not-exist.invalid/v.mp4", allow_private=False)


def test_every_resolved_address_is_checked(monkeypatch):
    """A name with one public and one private record must not pass.

    Checking only the first result would let a hostname whose records include a private
    address through, and then the connection could land on the private one.
    """
    def fake_getaddrinfo(host, port, **kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", 80)),   # public
            (2, 1, 6, "", ("127.0.0.1", 80)),       # private - must be caught
        ]

    monkeypatch.setattr(dl.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(dl.UnsafeURLError):
        dl.assert_safe_url("https://mixed-records.example/v.mp4", allow_private=False)


def test_unsafe_url_error_is_a_download_error():
    """Callers that already handle DownloadError keep working unchanged."""
    assert issubclass(dl.UnsafeURLError, dl.DownloadError)
