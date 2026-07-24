"""End-to-end API tests for Phase 3 publishing endpoints."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config import settings
from worker.jobs import get_manager
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def seeded_job():
    """Insert a completed job with one real clip file on disk."""
    manager = get_manager()
    job = Job(input_type="file", source="seed.mp4", options=ProcessingOptions())
    clip = ClipResult(
        id="clipA", filename="clipA.mp4", start=0.0, end=12.0, duration=12.0,
        title="Amazing moment", description="The description",
        hashtags=["#one", "#two"], hook_text="Hook!", cta="Subscribe",
        mentions=["@handle"], thumbnail_text="WOW", score=91.0,
    )
    job.clips = [clip]
    job.status = JobStatus.COMPLETED
    manager.store.add(job)

    clip_dir = Path(settings.clips_dir) / job.id
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / clip.filename).write_bytes(b"FAKEVIDEODATA")
    return job, clip


def test_publisher_statuses(client):
    resp = client.get("/api/publishers")
    assert resp.status_code == 200
    platforms = resp.json()["platforms"]
    assert set(platforms) == {"whop", "youtube", "tiktok", "instagram", "x"}
    for status in platforms.values():
        assert "configured" in status
        assert "message" in status


def test_campaign_create_and_list(client):
    routes = {"youtube": {"account_id": "chanX", "target_type": "", "target_id": ""}}
    resp = client.post("/api/campaigns", json={"name": "My Campaign", "routes": routes})
    assert resp.status_code == 200
    campaign_id = resp.json()["id"]
    assert campaign_id

    listing = client.get("/api/campaigns").json()["campaigns"]
    assert any(c["id"] == campaign_id for c in listing)


def test_campaign_requires_routes(client):
    resp = client.post("/api/campaigns", json={"name": "Empty", "routes": {}})
    assert resp.status_code == 400


def test_download_returns_zip_with_metadata(client, seeded_job):
    job, clip = seeded_job
    resp = client.get(f"/api/clips/{job.id}/{clip.filename}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    names = archive.namelist()
    assert "clipA.mp4" in names
    assert "clipA_metadata.txt" in names

    metadata = archive.read("clipA_metadata.txt").decode()
    assert "Amazing moment" in metadata
    assert "#one #two" in metadata
    assert "Subscribe" in metadata


def test_video_only_download(client, seeded_job):
    job, clip = seeded_job
    resp = client.get(f"/api/clips/{job.id}/{clip.filename}/video")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content == b"FAKEVIDEODATA"


def test_publish_clip_creates_attempts(client, seeded_job):
    # No platform credentials are set, so the background scheduler fails the
    # attempt fast (not configured) without any network call — we only assert
    # that the attempt was recorded and is visible in history.
    job, clip = seeded_job

    resp = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/publish",
        json={"platforms": ["youtube"], "mode": "review",
              "routes": {"youtube": {"account_id": "chan1"}}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["attempt_ids"]) == 1
    assert body["attempts"][0]["platform"] == "youtube"

    history = client.get("/api/history").json()
    assert any(a["platform"] == "youtube" for a in history["publish_attempts"])


def test_publish_unknown_job_404(client):
    resp = client.post("/api/jobs/nope/clips/none/publish",
                       json={"platforms": ["youtube"], "mode": "review"})
    assert resp.status_code == 404


def test_info_reports_platforms(client):
    resp = client.get("/api/info")
    assert resp.status_code == 200
    assert "platforms" in resp.json()
