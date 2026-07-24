"""API tests for Phase 5 storage, profiles, and update endpoints."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config import settings
from worker.jobs import get_manager
from worker.models import Job, JobStatus, ProcessingOptions


@pytest.fixture
def client():
    return TestClient(app)


def test_storage_status(client):
    resp = client.get("/api/storage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] in ("local", "s3")
    assert "usage" in body and "settings" in body
    assert "retention_days" in body["settings"]


def test_update_storage_settings(client):
    resp = client.post("/api/storage/settings",
                       json={"retention_days": 14, "auto_delete_temp": False})
    assert resp.status_code == 200
    settings_out = resp.json()["settings"]
    assert settings_out["retention_days"] == 14
    assert settings_out["auto_delete_temp"] is False
    # Restore a sane default for other tests.
    client.post("/api/storage/settings", json={"retention_days": 30,
                                                "auto_delete_temp": True})


def test_storage_cleanup(client):
    resp = client.post("/api/storage/cleanup?temp=true&expired=true")
    assert resp.status_code == 200
    assert "usage" in resp.json()


def test_info_exposes_version_and_backend(client):
    body = client.get("/api/info").json()
    assert body["version"]
    assert body["storage_backend"] in ("local", "s3")
    assert 0 in body["retention_choices"]


def test_updates_endpoint(client, monkeypatch):
    from updates import get_update_checker
    checker = get_update_checker()
    monkeypatch.setattr(checker, "_http_get", lambda url: {"tag_name": "v0.0.1"})
    resp = client.get("/api/updates?force=true")
    assert resp.status_code == 200
    assert "current" in resp.json()


# --- Profiles --------------------------------------------------------------
def test_profile_crud(client):
    created = client.post("/api/profiles", json={
        "name": "Test Profile",
        "settings": {"aspect": "1:1"},
        "publishing": {"mode": "review"},
        "make_default": True,
    })
    assert created.status_code == 200
    pid = created.json()["id"]

    listing = client.get("/api/profiles").json()
    assert any(p["id"] == pid for p in listing["profiles"])
    assert listing["default_id"] == pid

    # Update in place.
    updated = client.post("/api/profiles", json={
        "name": "Test Profile", "settings": {"aspect": "9:16"},
        "publishing": {}, "id": pid,
    })
    assert updated.json()["settings"]["aspect"] == "9:16"

    # Delete.
    deleted = client.delete(f"/api/profiles/{pid}")
    assert deleted.status_code == 200
    assert all(p["id"] != pid for p in client.get("/api/profiles").json()["profiles"])


def test_profile_name_required(client):
    assert client.post("/api/profiles", json={"name": "  "}).status_code == 400


def test_set_default_missing_profile(client):
    assert client.post("/api/profiles/nope/default").status_code == 404


# --- Protected source deletion --------------------------------------------
def test_delete_source_requires_confirm(client):
    resp = client.request("DELETE", "/api/jobs/whatever/source")
    assert resp.status_code == 400  # confirm=false by default


def test_delete_source_protected_and_scoped(client, tmp_path, monkeypatch):
    # Seed a file-input job whose source lives under uploads_dir.
    uploads = Path(settings.uploads_dir)
    uploads.mkdir(parents=True, exist_ok=True)
    src = uploads / "seed_source.mp4"
    src.write_bytes(b"data")

    manager = get_manager()
    job = Job(input_type="file", source=str(src), options=ProcessingOptions())
    job.status = JobStatus.COMPLETED
    manager.store.add(job)

    resp = client.request("DELETE", f"/api/jobs/{job.id}/source?confirm=true")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert not src.exists()
