"""Delivery readiness: preview the exact file and blocker for each destination."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config import settings
from publishers.base import PublisherStatus
from publishers.history import get_history
from publishers.manager import get_publish_manager
from worker.jobs import get_manager
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions


@pytest.fixture
def client():
    return TestClient(app)


def _job_and_clip(duration: float = 6.0):
    job = Job(
        input_type="file",
        source="source.mp4",
        options=ProcessingOptions(aspect="9:16", output_platform="tiktok"),
    )
    clip = ClipResult(
        id="readiness-clip",
        filename="clip_readiness.mp4",
        start=1.0,
        end=1.0 + duration,
        duration=duration,
        title="A selected moment",
        video_url=f"clips/{job.id}/clip_readiness.mp4",
    )
    job.clips = [clip]
    job.status = JobStatus.COMPLETED
    get_manager().store.add(job)

    clip_dir = Path(settings.clips_dir) / job.id
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / clip.filename).write_bytes(b"BASE-VIDEO")
    return job, clip, clip_dir


def _configure(monkeypatch, platform: str = "tiktok"):
    calls: list[str] = []
    status = PublisherStatus(
        platform=platform,
        configured=True,
        available=True,
        direct_publish=True,
        state="ready",
        message="",
    )

    def fake_status(account_id=""):
        calls.append(account_id)
        return status

    monkeypatch.setattr(get_publish_manager().publishers[platform], "status", fake_status)
    return calls


def _variant(filename: str, platform: str = "tiktok"):
    return {
        "platform": platform,
        "label": "TikTok",
        "filename": filename,
        "aspect": "9:16",
        "width": 1080,
        "height": 1920,
    }


def test_reports_the_exact_variant_file_as_ready(client, monkeypatch):
    job, clip, clip_dir = _job_and_clip()
    record = _variant("clip_readiness_tiktok.mp4")
    (clip_dir / record["filename"]).write_bytes(b"VARIANT")
    get_manager().store.update_clip(job.id, clip.id, {"variants": [record]})
    _configure(monkeypatch)

    response = client.get(
        f"/api/jobs/{job.id}/clips/{clip.id}/delivery-readiness",
        params={"platforms": "tiktok"},
    )

    assert response.status_code == 200
    body = response.json()
    destination = body["destinations"][0]
    assert destination["status"] == "ready"
    assert destination["delivery"]["kind"] == "variant"
    assert destination["delivery"]["filename"] == record["filename"]
    assert destination["file_exists"] is True
    assert destination["publishable"] is True
    assert destination["action"] == "none"
    assert (destination["width"], destination["height"]) == (1080, 1920)
    assert body["summary"] == {"ready": 1, "fallback": 0, "blocked": 0}


def test_reports_a_canonical_fallback_and_the_action_that_fixes_it(client, monkeypatch):
    job, clip, _ = _job_and_clip()
    _configure(monkeypatch)

    response = client.get(
        f"/api/jobs/{job.id}/clips/{clip.id}/delivery-readiness",
        params={"platforms": "tiktok"},
    )

    destination = response.json()["destinations"][0]
    assert response.status_code == 200
    assert destination["status"] == "fallback"
    assert destination["delivery"]["kind"] == "canonical"
    assert destination["publishable"] is True
    assert destination["action"] == "render_variant"
    assert any("No TikTok variant" in warning for warning in destination["warnings"])


def test_a_missing_variant_file_is_a_fallback_not_a_ready_file(client, monkeypatch):
    job, clip, _ = _job_and_clip()
    get_manager().store.update_clip(
        job.id, clip.id, {"variants": [_variant("clip_readiness_tiktok.mp4")]}
    )
    _configure(monkeypatch)

    response = client.get(
        f"/api/jobs/{job.id}/clips/{clip.id}/delivery-readiness",
        params={"platforms": "tiktok"},
    )

    destination = response.json()["destinations"][0]
    assert destination["status"] == "fallback"
    assert destination["delivery"]["fallback"] == "variant_file_missing"
    assert destination["publishable"] is True
    assert destination["action"] == "render_variant"
    assert any("variant file" in warning for warning in destination["warnings"])


def test_duration_is_reported_as_a_blocker_against_the_product_limit(client, monkeypatch):
    job, clip, _ = _job_and_clip(duration=200.0)
    _configure(monkeypatch, "youtube")

    response = client.get(
        f"/api/jobs/{job.id}/clips/{clip.id}/delivery-readiness",
        params={"platforms": "youtube_shorts"},
    )

    destination = response.json()["destinations"][0]
    assert destination["status"] == "blocked"
    assert destination["publishable"] is False
    assert destination["action"] == "shorten_clip"
    assert destination["max_duration_s"] == 180.0
    assert any("180s limit" in blocker for blocker in destination["blockers"])


def test_an_unknown_platform_is_reported_instead_of_being_dropped(client):
    job, clip, _ = _job_and_clip()

    response = client.get(
        f"/api/jobs/{job.id}/clips/{clip.id}/delivery-readiness",
        params={"platforms": "myspace"},
    )

    destination = response.json()["destinations"][0]
    assert response.status_code == 200
    assert destination["status"] == "blocked"
    assert destination["publishable"] is False
    assert any("No publisher" in blocker for blocker in destination["blockers"])


def test_the_campaign_route_account_is_checked(client, monkeypatch):
    job, clip, _ = _job_and_clip()
    calls = _configure(monkeypatch)
    campaign = get_history().save_campaign(
        "Readiness campaign",
        {"tiktok": {"account_id": "tt-campaign-account"}},
    )

    response = client.get(
        f"/api/jobs/{job.id}/clips/{clip.id}/delivery-readiness",
        params={"platforms": "tiktok", "campaign_id": campaign.id},
    )

    assert response.status_code == 200
    assert calls == ["tt-campaign-account"]
    assert response.json()["destinations"][0]["account_id"] == "tt-campaign-account"


def test_unconfigured_publishers_block_the_destination(client, monkeypatch):
    job, clip, _ = _job_and_clip()
    status = PublisherStatus(
        platform="tiktok",
        configured=False,
        available=False,
        direct_publish=False,
        state="not_configured",
        message="TIKTOK_ACCESS_TOKEN is missing",
    )
    monkeypatch.setattr(
        get_publish_manager().publishers["tiktok"], "status", lambda account_id="": status
    )

    response = client.get(
        f"/api/jobs/{job.id}/clips/{clip.id}/delivery-readiness",
        params={"platforms": "tiktok"},
    )

    destination = response.json()["destinations"][0]
    assert destination["status"] == "blocked"
    assert destination["action"] == "configure_publisher"
    assert any("not configured" in blocker for blocker in destination["blockers"])
