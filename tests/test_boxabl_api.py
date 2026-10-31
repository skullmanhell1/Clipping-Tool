from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.main import app
from config import settings
from worker.boxabl_campaign import REQUIRED_REVIEW_CHECKS
from worker.campaign_profile import parse_campaign_profile
from worker.campaign_profile_store import CampaignProfileStore
from worker.jobs import get_manager
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions


@pytest.fixture
def client():
    return TestClient(app)


def _campaign_job():
    options = ProcessingOptions.from_dict({"profile": "boxabl_campaign"})
    job = Job(input_type="url", source="https://youtu.be/JbS_VhA0rRo", options=options)
    clip = ClipResult(
        id="boxabl-clip",
        filename="boxabl-clip.mp4",
        start=0.0,
        end=35.0,
        duration=35.0,
        title="BOXABL: Casita tour",
        description="Explore the BOXABL Casita. @boxabl",
        hashtags=["#BOXABL", "#Casita"],
        hook_text="BOXABL: A 20×20 home that folds?",
        mentions=["@boxabl"],
        transcript_text="Here is a BOXABL Casita tour.",
        thumbnail_text="BOXABL CASITA",
        review_state="approved",
    )
    job.clips = [clip]
    job.status = JobStatus.COMPLETED
    get_manager().store.add(job)
    clip_dir = Path(settings.clips_dir) / job.id
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / clip.filename).write_bytes(b"test-video")
    return job, clip


def test_boxabl_url_job_rejects_links_outside_the_source_brief(client, monkeypatch):
    monkeypatch.setattr(api_main, "validate_public_url", lambda url: url)
    response = client.post(
        "/api/jobs/url",
        json={
            "url": "https://youtu.be/not-listed-in-the-campaign-brief",
            "options": {"profile": "boxabl_campaign"},
        },
    )
    assert response.status_code == 400
    assert "approved" in response.json()["detail"].lower()


def test_boxabl_metadata_edit_invalidates_previous_review(client):
    job, clip = _campaign_job()
    response = client.patch(
        f"/api/jobs/{job.id}/clips/{clip.id}",
        json={"title": "BOXABL: updated Casita tour"},
    )
    assert response.status_code == 200
    assert response.json()["review_state"] == "pending"
    assert "re-review" in response.json()["review_note"].lower()


def test_boxabl_rerender_invalidates_previous_review(client, monkeypatch):
    job, clip = _campaign_job()
    from worker import rerender as rerender_module

    monkeypatch.setattr(rerender_module, "rerender_clip", lambda *_args, **_kwargs: clip)
    response = client.post(f"/api/jobs/{job.id}/clips/{clip.id}/rerender", json={})
    assert response.status_code == 200
    assert response.json()["review_state"] == "pending"
    assert "re-review" in response.json()["review_note"].lower()


def test_boxabl_publish_api_requires_manual_review_before_queueing(client, monkeypatch):
    job, clip = _campaign_job()

    class PublisherQueue:
        called = False

        def submit(self, **kwargs):
            self.called = True
            return ["not-queued"]

    queue = PublisherQueue()
    monkeypatch.setattr(api_main, "get_publish_manager", lambda: queue)
    response = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/publish",
        json={"platforms": ["youtube"], "mode": "auto"},
    )
    assert response.status_code == 409
    assert "review mode" in response.json()["detail"].lower()
    assert not queue.called


def test_boxabl_publish_api_blocks_instagram_without_native_account_tag(client, monkeypatch):
    job, clip = _campaign_job()

    class PublisherQueue:
        called = False

        def submit(self, **kwargs):
            self.called = True
            return ["not-queued"]

    queue = PublisherQueue()
    monkeypatch.setattr(api_main, "get_publish_manager", lambda: queue)
    response = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/publish",
        json={
            "platforms": [" instagram "],
            "mode": "review",
            "campaign_checks": {key: True for key in REQUIRED_REVIEW_CHECKS},
        },
    )
    assert response.status_code == 409
    assert "native @boxabl account tag" in response.json()["detail"]
    assert not queue.called


def test_boxabl_publish_api_passes_compliant_manual_review_to_publisher(client, monkeypatch):
    job, clip = _campaign_job()

    class PublisherQueue:
        kwargs = None

        def submit(self, **kwargs):
            self.kwargs = kwargs
            return ["not-queued"]

    queue = PublisherQueue()
    monkeypatch.setattr(api_main, "get_publish_manager", lambda: queue)
    response = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/publish",
        json={
            "platforms": ["youtube"],
            "mode": "review",
            "campaign_checks": {key: True for key in REQUIRED_REVIEW_CHECKS},
        },
    )
    assert response.status_code == 200
    assert queue.kwargs["mode"] == "review"
    assert queue.kwargs["platforms"] == ["youtube"]


def test_x_campaign_link_is_carried_to_the_x_publisher_route(client, monkeypatch):
    job, clip = _campaign_job()

    class PublisherQueue:
        kwargs = None

        def submit(self, **kwargs):
            self.kwargs = kwargs
            return ["not-queued"]

    queue = PublisherQueue()
    monkeypatch.setattr(api_main, "get_publish_manager", lambda: queue)
    affiliate_url = "https://affiliate.example/track/creator"
    checks = {key: True for key in REQUIRED_REVIEW_CHECKS}
    response = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/publish",
        json={
            "platforms": [" X "],
            "mode": "review",
            "affiliate_url": affiliate_url,
            "campaign_checks": checks,
        },
    )
    assert response.status_code == 200
    assert queue.kwargs["route_overrides"]["x"]["affiliate_url"] == affiliate_url


def test_selected_profile_does_not_block_an_unrelated_publish(client, monkeypatch, tmp_path):
    """The library's selected preset is not an implicit gate on an existing job."""
    job, clip = _campaign_job()
    store = CampaignProfileStore(tmp_path / "profiles.json")
    unrelated = store.save(
        parse_campaign_profile("Campaign: Unrelated\nAspect ratio: 1:1\nBanned words: boxabl")
    )
    assert store.select(unrelated.id) is not None
    monkeypatch.setattr(api_main, "get_campaign_profile_store", lambda: store)

    class PublisherQueue:
        kwargs = None

        def submit(self, **kwargs):
            self.kwargs = kwargs
            return ["not-queued"]

    queue = PublisherQueue()
    monkeypatch.setattr(api_main, "get_publish_manager", lambda: queue)
    response = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/publish",
        json={
            "platforms": ["youtube"],
            "mode": "review",
            "campaign_checks": {key: True for key in REQUIRED_REVIEW_CHECKS},
        },
    )

    assert response.status_code == 200, response.text
    assert queue.kwargs is not None
