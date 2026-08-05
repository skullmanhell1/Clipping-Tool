"""The API surface is committed, so a change to it appears in the PR diff (Phase 7).

``/openapi.json`` was served at runtime and recorded nowhere. The only way to see that a change
renamed a field, dropped a route or altered a response shape was to notice it in the diff of the
code that produced it - and a breaking change to a response model reads as a small refactor. The
frontend and any external caller consume that surface, so a silent change to it is a silent
integration break.

``openapi.json`` is now committed and this module is what stops it becoming a lie: a document that
has drifted is worse than none, because it looks authoritative while describing an API that no
longer exists. Same argument the repo already makes for ``black --check`` and the dependency locks.

The subtle one here is :func:`test_the_served_document_reports_the_real_version`. ``app.openapi()``
caches its result *on the app object*, so an exporter that normalised the version by mutating that
result would poison ``/openapi.json`` for every real client for the life of the process - and in
the test suite, for every test that ran afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routers._shared import APP_VERSION
from config import settings
from scripts import export_openapi

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "openapi.json"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", None)
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# The drift pin                                                                 #
# --------------------------------------------------------------------------- #
def test_the_committed_document_matches_the_code():
    """The whole point. A stale document describes an API that does not exist.

    If this fails, the API surface changed: run ``python scripts/export_openapi.py`` and review
    the resulting diff, which is the record this file exists to produce.
    """
    assert COMMITTED.is_file(), "openapi.json is missing; run scripts/export_openapi.py"
    generated = export_openapi.serialise(export_openapi.document())
    committed = COMMITTED.read_text(encoding="utf-8")
    if committed != generated:
        # Reported as a path count and a route delta rather than 3800 lines of JSON, because the
        # actionable information is which routes moved.
        before = set(json.loads(committed).get("paths", {}))
        after = set(json.loads(generated).get("paths", {}))
        pytest.fail(
            "openapi.json is stale. Run: python scripts/export_openapi.py\n"
            f"  routes added:   {sorted(after - before)}\n"
            f"  routes removed: {sorted(before - after)}\n"
            f"  (a change with no route delta means a schema or parameter changed)"
        )


def test_check_mode_agrees_with_the_committed_file():
    """The CI gate and this test must not be able to disagree."""
    assert export_openapi.main(["--check"]) == 0


def test_check_mode_fails_on_a_stale_document(tmp_path, monkeypatch):
    """A gate that cannot fail reports nothing - the lesson this repo already learned from
    ``ruff check . || true``."""
    stale = tmp_path / "openapi.json"
    stale.write_text('{"openapi": "3.1.0", "paths": {}}\n', encoding="utf-8")
    monkeypatch.setattr(export_openapi, "TARGET", stale)
    assert export_openapi.main(["--check"]) == 1


def test_check_mode_fails_when_the_document_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(export_openapi, "TARGET", tmp_path / "nope.json")
    assert export_openapi.main(["--check"]) == 1


# --------------------------------------------------------------------------- #
# Version normalisation                                                         #
# --------------------------------------------------------------------------- #
def test_the_committed_version_is_a_placeholder():
    """``info.version`` is normalised out on purpose.

    It comes from ``VERSION``, and ``release.yml`` fires on a change to that file - so a real
    version would make this document stale on every release, failing the release PR for a line
    that says nothing about the API. A check that fails predictably is one people learn to
    override. The version is not API surface.
    """
    document = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert document["info"]["version"] == export_openapi.VERSION_PLACEHOLDER


def test_the_placeholder_is_not_the_real_fallback_version():
    """``0.0.0`` is what ``_read_version`` returns when the VERSION file cannot be read.

    Reusing it as the placeholder would make a genuine failure indistinguishable from deliberate
    normalisation.
    """
    assert export_openapi.VERSION_PLACEHOLDER != "0.0.0"
    assert not export_openapi.VERSION_PLACEHOLDER[:1].isdigit()


def test_the_served_document_reports_the_real_version(client):
    """The trap. ``app.openapi()`` caches its result on the app object.

    An exporter that normalised the version by mutating that cached dict would serve the
    placeholder to every real client for the life of the process - and, because the test suite
    imports the same ``app``, would corrupt every later test that read the document. The exporter
    copies before mutating; this is what proves it.
    """
    served = client.get("/openapi.json").json()
    assert served["info"]["version"] == APP_VERSION
    assert served["info"]["version"] != export_openapi.VERSION_PLACEHOLDER
    # And exporting again must not change what is served.
    export_openapi.document()
    assert client.get("/openapi.json").json()["info"]["version"] == APP_VERSION


def test_the_version_is_the_only_difference_from_the_served_document(client):
    """Nothing else may be normalised.

    A committed document that quietly omitted a field would be the same failure as a stale one,
    and harder to notice: the drift check would pass forever while the record was incomplete.
    """
    served = client.get("/openapi.json").json()
    exported = export_openapi.document()
    served["info"]["version"] = export_openapi.VERSION_PLACEHOLDER
    assert exported == served


# --------------------------------------------------------------------------- #
# The document is complete and usable                                           #
# --------------------------------------------------------------------------- #
def test_it_is_sorted_and_ends_with_a_newline():
    """Sorted so the diff describes API changes, not route-registration order.

    FastAPI's ordering follows ``include_router`` order, so moving one call would otherwise
    produce a large diff describing no change at all - the kind of noise that gets a review
    skimmed.
    """
    raw = COMMITTED.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert raw == export_openapi.serialise(json.loads(raw))


def test_every_documented_route_is_reachable(client):
    """A documented route that does not exist is a promise to a client that will fail."""
    documented = set(json.loads(COMMITTED.read_text(encoding="utf-8"))["paths"])
    live = set(client.get("/openapi.json").json()["paths"])
    assert documented == live


def test_the_metrics_endpoint_is_documented():
    """This phase's addition, and the reason the export runs after it rather than before."""
    document = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert "/metrics" in document["paths"]
    assert "get" in document["paths"]["/metrics"]


def test_the_document_covers_the_whole_api_not_a_subset():
    """A floor, not an exact count - an exact one would fail on every added route and teach
    nobody anything. This catches an export that silently produced an almost-empty document,
    which the drift check alone would happily bless once committed."""
    document = json.loads(COMMITTED.read_text(encoding="utf-8"))
    paths = document["paths"]
    assert len(paths) >= 40, f"only {len(paths)} paths; an export this small is a bug"
    for expected in ("/api/jobs", "/api/info", "/metrics"):
        assert expected in paths


def test_it_is_a_valid_openapi_document():
    document = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert document["openapi"].startswith("3.")
    assert document["info"]["title"]
    for path, operations in document["paths"].items():
        assert path.startswith("/"), path
        assert operations, f"{path} documents no operations"
        for method, operation in operations.items():
            assert method in {
                "get",
                "put",
                "post",
                "delete",
                "options",
                "head",
                "patch",
                "trace",
            }, f"{path} has an unexpected method {method!r}"
            assert operation.get("responses"), f"{path} {method} documents no responses"
