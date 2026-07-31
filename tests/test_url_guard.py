"""URL ingest refuses to become a request forwarder into its own network.

``POST /api/jobs/url``, ``/api/jobs/batch`` and ``/api/preview`` hand a caller-supplied URL to
yt-dlp, which fetches whatever it is given. Combined with the fact that every route was
unauthenticated and ``render.yaml`` publishes this with ``autoDeploy: true``, that made the
deployed API a general-purpose fetcher positioned *inside* the deployment's network.

The target is not hypothetical or obscure: every major cloud serves instance credentials from
``169.254.169.254`` over plain HTTP with no authentication at all. ``is_url`` — the only check
that existed — matched ``^https?://`` and nothing else, so it accepted that address happily.

These tests call :func:`worker.download.validate_public_url` directly with an explicit
``allow_private`` rather than going through the endpoints, for two reasons: the rules are the
subject, and a pure call cannot be perturbed by the module-scoped opt-in fixture in
``tests/test_url_ingest.py`` (whose fixtures legitimately serve from ``127.0.0.1``).
"""

from __future__ import annotations

import ipaddress

import pytest

from worker import download
from worker.download import UnsafeURLError

#: Addresses and schemes that must never be fetched, with the reason each one matters.
#:
#: Several are the *same* address written differently, which is the point — a check that reasons
#: about the literal text of a URL rather than the address it resolves to will pass some of these
#: and fail others, and the pairs are what expose that.
HOSTILE = [
    ("http://169.254.169.254/latest/meta-data/", "cloud instance metadata, the actual prize"),
    ("http://169.254.170.2/v2/credentials", "ECS task credentials, same range"),
    ("http://127.0.0.1:8000/api/jobs", "the app calling itself, behind any network boundary"),
    ("http://localhost/admin", "loopback by name rather than by number"),
    ("http://[::1]/admin", "loopback over IPv6"),
    ("http://[::ffff:127.0.0.1]/x", "IPv4-mapped IPv6: loopback wearing a hat"),
    ("http://2130706433/x", "127.0.0.1 as a single decimal integer"),
    ("http://0x7f000001/x", "127.0.0.1 in hex"),
    ("http://10.0.0.5/internal", "RFC1918 class A"),
    ("http://172.16.31.9/internal", "RFC1918 class B, the range most often forgotten"),
    ("http://192.168.1.1/router", "RFC1918 class C, typically the router admin page"),
    ("http://0.0.0.0/x", "the unspecified address"),
    ("http://[fe80::1]/x", "IPv6 link-local"),
    ("http://[fd00::1]/x", "IPv6 unique-local, the RFC1918 equivalent"),
    ("file:///etc/passwd", "yt-dlp's generic extractor will read a local path"),
    ("ftp://example.com/a.mp4", "a scheme we never intend to fetch"),
    ("gopher://example.com/a", "protocol smuggling classic"),
    ("/etc/passwd", "no scheme at all"),
    ("", "empty input"),
]


@pytest.mark.parametrize("url,why", HOSTILE, ids=[u or "empty" for u, _ in HOSTILE])
def test_hostile_urls_are_refused(url, why):
    """Every entry in the table raises, and the message says what was wrong.

    Asserting on the message as well as the exception because this error goes straight back to
    the caller as a 400 — an operator who pasted an internal URL by mistake needs to know it was
    refused deliberately, not that the app is broken.
    """
    with pytest.raises(UnsafeURLError) as caught:
        download.validate_public_url(url, allow_private=False)
    assert str(caught.value), f"refused {url!r} ({why}) with an empty message"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a.mp4",
        "http://example.com/a.mp4",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://example.com:8443/a.mp4",
        "  https://example.com/a.mp4  ",
    ],
)
def test_ordinary_urls_are_allowed(url):
    """The guard must not break the feature it protects."""
    assert download.validate_public_url(url, allow_private=False)


def test_an_unresolvable_host_is_passed_through_rather_than_refused():
    """A name that resolves to nothing reaches nothing, so it is not a route into the network.

    Refusing it would also replace yt-dlp's accurate "no such host" with a security error, which
    sends someone who has simply mistyped a domain looking in entirely the wrong place. There is
    an existing test (``test_url_ingest.py``) that depends on exactly this: it drives
    ``download_video`` at ``https://example.invalid/`` with a stubbed yt-dlp and asserts on the
    *downstream* failure.
    """
    assert download.validate_public_url("https://example.invalid/x.mp4", allow_private=False)


def test_allow_private_is_an_opt_in_escape_hatch():
    """A self-hoster ingesting from a LAN media server can say so.

    Off by default, because the safe choice must not require someone to make a decision first.
    """
    url = "http://192.168.1.50:8096/media/a.mp4"
    with pytest.raises(UnsafeURLError):
        download.validate_public_url(url, allow_private=False)
    assert download.validate_public_url(url, allow_private=True) == url


def test_the_default_comes_from_settings(monkeypatch):
    """``allow_private=None`` reads ``URL_INGEST_ALLOW_PRIVATE``.

    This is the path every real caller takes — ``fetch_metadata`` and ``download_video`` pass no
    flag — so the parameter being correct is not enough on its own.
    """
    from config import settings

    monkeypatch.setattr(settings, "url_ingest_allow_private", False)
    with pytest.raises(UnsafeURLError):
        download.validate_public_url("http://127.0.0.1/x.mp4")

    monkeypatch.setattr(settings, "url_ingest_allow_private", True)
    assert download.validate_public_url("http://127.0.0.1/x.mp4")


def test_every_resolved_address_is_checked_not_just_the_first(monkeypatch):
    """A name returning one public and one private address must be refused.

    ``getaddrinfo`` can return several records, and connecting picks one. Checking only the first
    would let a host that advertises a public address alongside ``169.254.169.254`` through, and
    the resulting connection could go to either — a race that presents as an intermittent
    security hole, which is the worst kind to diagnose.
    """
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError, match="169.254.169.254"):
        download.validate_public_url("http://split-horizon.example.com/x", allow_private=False)


def test_unsafe_url_error_is_a_download_error():
    """Existing callers must keep working.

    Every caller of ``download_video``/``fetch_metadata`` already handles ``DownloadError``, so
    making the new error a subclass is what keeps a refused URL from becoming an unhandled 500 in
    a path nobody updated.
    """
    assert issubclass(UnsafeURLError, download.DownloadError)


def test_is_url_semantics_are_unchanged():
    """``is_url`` keeps its old, narrow job.

    It is pinned by ``tests/test_url_ingest.py`` and used for the "not even a URL" 400. The SSRF
    rules are a separate function on purpose: widening ``is_url`` would have changed what that
    400 means and coupled a cheap syntactic check to DNS resolution.
    """
    assert download.is_url("https://example.com/a.mp4")
    assert download.is_url("  http://example.com/a.mp4 ")
    assert download.is_url("http://127.0.0.1/a.mp4"), "still syntactically a URL"
    assert not download.is_url("ftp://example.com/a.mp4")
    assert not download.is_url("/tmp/a.mp4")


@pytest.mark.parametrize(
    "address,expected",
    [
        ("8.8.8.8", None),
        ("93.184.216.34", None),
        ("127.0.0.1", "a loopback address"),
        ("169.254.169.254", "a link-local address (cloud instance metadata lives here)"),
        ("10.1.2.3", "a private address"),
        ("224.0.0.1", "a reserved, multicast or unspecified address"),
        ("::1", "a loopback address"),
        ("2606:4700:4700::1111", None),
    ],
)
def test_address_classification(address, expected):
    """The classifier itself, so a wrong answer is attributed rather than inferred.

    Kept separate from the URL tests because a misclassification and a parsing bug produce the
    same failure through :func:`validate_public_url`, and they need different fixes.
    """
    assert download._is_disallowed_address(ipaddress.ip_address(address)) == expected
