"""API contracts for the campaign automation surface."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app
from publishers.base import PublisherStatus
from publishers.history import HistoryStore
from publishers.manager import PublishManager
from worker.models import ClipResult


def test_campaign_preset_parse_endpoint_returns_reviewable_policy():
    with TestClient(app) as client:
        response = client.post(
            "/api/campaign-presets/parse",
            json={
                "text": (
                    "Campaign: Launch\n"
                    "Logo: assets/logo.png\n"
                    "Aspect ratios: 9:16\n"
                    "Hashtags: #launch #podcast\n"
                    "Daily clips: 2\n"
                    "Public visibility: required\n"
                    "Pattern interrupter: every 5 seconds\n"
                )
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["requires_review"] is True
    assert body["preset"]["daily_clip_target"] == 2
    assert body["preset"]["require_public_visibility"] is True
    assert body["preset"]["hashtags"] == ["#launch", "#podcast"]


def test_info_advertises_campaign_automation():
    with TestClient(app) as client:
        body = client.get("/api/info").json()
    assert body["campaign_automation"]["daily_clip_target"] == 2
    assert body["campaign_automation"]["public_visibility_required"] is True


def test_daily_schedule_rejects_anything_other_than_two_clips():
    with TestClient(app) as client:
        response = client.post(
            "/api/campaigns/not-needed/daily-schedule",
            json={"job_id": "job", "clip_ids": ["one"], "visibility": "public"},
        )
    assert response.status_code == 400
    assert "Exactly two" in response.json()["detail"]


def test_public_only_flag_is_persisted_on_publish_attempt(tmp_path):
    history = HistoryStore(tmp_path / "history.db")

    class FakePublisher:
        name = "youtube"
        min_interval_seconds = 0

        def status(self, account_id=""):
            return PublisherStatus(
                platform=self.name,
                configured=True,
                available=True,
                direct_publish=True,
                state="ready",
                message="ready",
                account_id=account_id,
            )

    manager = PublishManager(
        publishers={"youtube": FakePublisher()}, history=history, autostart=False
    )
    clip = ClipResult(id="clip", filename="clip.mp4", start=0, end=5, duration=5)
    ids = manager.submit(
        job_id="job",
        clip=clip,
        video_path=tmp_path / "clip.mp4",
        platforms=["youtube"],
        mode="auto",
        public_only=True,
    )
    attempt = history.get_attempt(ids[0])
    assert attempt["request_json"]["public_only"] is True
