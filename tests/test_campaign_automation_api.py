"""API contracts for the campaign automation surface."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app
from publishers.base import PublisherStatus
from publishers.history import HistoryStore
from publishers.manager import PublishManager
from worker.campaign_brief_sync import DriveBriefSnapshot
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


def test_info_advertises_twenty_to_sixty_second_clip_range():
    with TestClient(app) as client:
        lengths = client.get("/api/info").json()["clip_lengths"]
    assert "20-60s" in lengths
    assert "30-60s" in lengths
    assert "60-90s" in lengths


def test_campaign_profile_parse_endpoint_returns_full_import_profile():
    with TestClient(app) as client:
        response = client.post(
            "/api/campaign-profiles/parse",
            json={
                "raw_text": (
                    "Campaign: Whop launch\n"
                    "Font: Inter Bold\n"
                    "Primary colour: #7c3aed\n"
                    "Approved video: https://youtu.be/abc123\n"
                    "Public visibility: required\n"
                )
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["profile"]["branding"]["font"] == "Inter Bold"
    assert body["profile"]["branding"]["colors"]["primary"] == "#7c3aed"
    assert body["profile"]["video_links"] == ["https://youtu.be/abc123"]
    assert body["profile"]["watermark_end_cards"]["public_visibility_required"] is True


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


def test_campaign_preset_drive_endpoint_reads_and_parses_without_saving_campaign(
    monkeypatch,
):
    """A mocked authenticated Drive read returns provenance and only a reviewable preset."""
    from worker import campaign_preset_sync

    class Reader:
        def fetch(self, file_id, **kwargs):
            assert file_id == "drive-brief-12345"
            return DriveBriefSnapshot.from_content(
                file_id,
                "Campaign: Spring Launch\nAspect ratios: 9:16\nHashtags: #launch",
                revision_id="rev-7",
            )

    monkeypatch.setattr(campaign_preset_sync, "configured", lambda: True)
    monkeypatch.setattr(campaign_preset_sync, "GoogleDriveBriefReader", Reader)
    with TestClient(app) as client:
        before = client.get("/api/campaigns").json()["campaigns"]
        response = client.post(
            "/api/campaign-presets/from-drive",
            json={
                "url": "https://docs.google.com/document/d/drive-brief-12345/edit",
            },
        )
        after = client.get("/api/campaigns").json()["campaigns"]

    assert response.status_code == 200
    body = response.json()
    assert body["requires_review"] is True
    assert body["preset"]["name"] == "Spring Launch"
    assert body["preset"]["hashtags"] == ["#launch"]
    assert body["sync"]["status"] == "synced"
    assert body["provenance"]["source_type"] == "google_docs"
    assert body["provenance"]["file_id"] == "drive-brief-12345"
    assert body["sync"]["revision_id"] == "rev-7"
    assert after == before


def test_campaign_preset_public_google_link_fallback_works_without_oauth(monkeypatch):
    from worker import campaign_preset_sync

    monkeypatch.setattr(campaign_preset_sync, "configured", lambda: False)
    monkeypatch.setattr(
        campaign_preset_sync,
        "download_public_text_file",
        lambda file_id, source_type: (
            "Campaign: Public Brief\nAllowed platforms: YouTube Shorts\nBest 20-60 seconds",
            {"id": file_id, "mimeType": "text/plain"},
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/campaign-presets/from-drive",
            json={"url": "https://docs.google.com/document/d/drive-brief-12345/edit"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["preset"]["name"] == "Public Brief"
    assert body["preset"]["allowed_platforms"] == ["youtube_shorts"]
    assert body["preset"]["brief_rules"]["clip_duration_seconds"] == {"min": 20.0, "max": 60.0}
    assert body["provenance"]["source_type"] == "google_docs"


def test_public_drive_share_link_retries_as_a_google_doc_export(monkeypatch):
    from worker import campaign_preset_sync
    from worker.google_drive import GoogleDriveError

    calls = []

    def public_download(file_id, source_type):
        calls.append(source_type)
        if source_type == "google_drive":
            raise GoogleDriveError("The shared Drive download did not return readable text.")
        return "Campaign: Shared Doc\nBest 20-60 seconds", {"id": file_id}

    monkeypatch.setattr(campaign_preset_sync, "configured", lambda: False)
    monkeypatch.setattr(campaign_preset_sync, "download_public_text_file", public_download)
    with TestClient(app) as client:
        response = client.post(
            "/api/campaign-presets/from-drive",
            json={"url": "https://drive.google.com/open?id=drive-doc-12345"},
        )

    assert response.status_code == 200
    assert calls == ["google_drive", "google_docs"]
    assert response.json()["preset"]["name"] == "Shared Doc"
    assert response.json()["preset"]["brief_rules"]["clip_duration_seconds"] == {
        "min": 20.0,
        "max": 60.0,
    }


def test_campaign_preset_drive_endpoint_surfaces_safe_fallback_errors(monkeypatch):
    from worker import campaign_preset_sync
    from worker.google_drive import GoogleDriveError

    monkeypatch.setattr(campaign_preset_sync, "configured", lambda: False)
    monkeypatch.setattr(
        campaign_preset_sync,
        "download_public_text_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            GoogleDriveError("The Google link is private or access was denied.")
        ),
    )
    with TestClient(app) as client:
        invalid = client.post(
            "/api/campaign-presets/from-drive", json={"url": "https://example.com/brief"}
        )
        unavailable = client.post(
            "/api/campaign-presets/from-drive",
            json={"url": "https://drive.google.com/file/d/drive-brief-12345/view"},
        )

    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_url"
    assert invalid.json()["detail"]["fall_back_to_paste"] is True
    assert unavailable.status_code == 422
    assert unavailable.json()["detail"]["code"] == "drive_access"
    assert unavailable.json()["detail"]["fall_back_to_paste"] is True


def test_oauth_preset_read_falls_back_to_public_export(monkeypatch):
    from worker import campaign_preset_sync
    from worker.google_drive import GoogleDriveError

    class Reader:
        def fetch(self, _file_id, **_kwargs):
            raise GoogleDriveError("Google Drive file access was denied. Confirm Viewer access.")

    monkeypatch.setattr(campaign_preset_sync, "configured", lambda: True)
    monkeypatch.setattr(campaign_preset_sync, "GoogleDriveBriefReader", Reader)
    monkeypatch.setattr(
        campaign_preset_sync,
        "download_public_text_file",
        lambda file_id, source_type: (
            "Campaign: OAuth Fallback\nHashtags: #public",
            {"id": file_id, "mimeType": "text/plain"},
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/campaign-presets/from-drive",
            json={"url": "https://docs.google.com/document/d/drive-brief-12345/edit"},
        )

    assert response.status_code == 200
    assert response.json()["preset"]["name"] == "OAuth Fallback"
    assert response.json()["preset"]["hashtags"] == ["#public"]
