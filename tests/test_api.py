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



# ---------------------------------------------------------------------------
# Tier 1 — Creator Output Upgrade: /api/info superset + option passthrough
# ---------------------------------------------------------------------------
def test_info_advertises_tier1_option_lists(client):
    """`/api/info` advertises the new Tier 1 preset + sourcing-mode lists."""
    effects = client.get("/api/info").json()["effects"]

    # Caption presets include the legacy templates plus new animated presets.
    assert "caption_presets" in effects
    for name in ("karaoke", "pop", "typewriter", "hormozi", "boxed", "minimal"):
        assert name in effects["caption_presets"]

    # Asset sourcing modes are advertised exactly (Req 8.7).
    assert effects["asset_sourcing_modes"] == ["off", "local_only", "local_then_external"]

    # B-roll intensities and caption animations are advertised.
    assert effects["broll_intensities"] == ["off", "subtle", "standard", "heavy"]
    assert effects["caption_animations"] == ["none", "pop", "typewriter", "karaoke_fill"]

    # broll_providers is present (empty when none configured).
    assert isinstance(effects["broll_providers"], list)


def test_info_retains_existing_effects_keys(client):
    """New lists are additive — all pre-existing effects keys remain (Req 22.3)."""
    body = client.get("/api/info").json()
    effects = body["effects"]

    # Every pre-existing effects key is still present (superset guarantee).
    for key in (
        "music_moods",
        "color_presets",
        "emoji_intensities",
        "emoji_modes",
        "caption_templates",
        "caption_positions",
    ):
        assert key in effects, f"pre-existing effects key missing: {key}"

    # Pre-existing values are unchanged.
    assert effects["caption_templates"] == ["karaoke", "boxed", "minimal"]
    assert effects["caption_positions"] == ["bottom", "center", "top"]

    # New top-level broll_available flag is exposed as a bool.
    assert isinstance(body["broll_available"], bool)


def test_options_model_threads_new_fields_into_from_dict():
    """OptionsModel -> to_options carries the new Tier 1 fields into ProcessingOptions."""
    from api.main import OptionsModel

    opts = OptionsModel(
        caption_preset="pop",
        broll=True,
        broll_intensity="subtle",
        asset_sourcing_mode="local_only",
        visual_selection=True,
        selection_prompt="find X",
        caption_keyword_highlight=True,
        permissibility_mode=True,
    ).to_options()

    assert opts.caption_preset == "pop"
    assert opts.broll is True
    assert opts.broll_intensity == "subtle"
    assert opts.asset_sourcing_mode == "local_only"
    assert opts.visual_selection is True
    assert opts.selection_prompt == "find X"
    assert opts.caption_keyword_highlight is True
    assert opts.permissibility_mode is True


def test_url_job_carries_new_fields_through(client):
    """A URL job submitted with new option fields reflects them on the stored job."""
    resp = client.post(
        "/api/jobs/url",
        json={
            "url": "https://example.com/video",
            "options": {
                "caption_preset": "pop",
                "broll": True,
                "visual_selection": True,
                "selection_prompt": "find X",
            },
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["id"]

    job = get_manager().store.get(job_id)
    assert job is not None
    assert job.options.caption_preset == "pop"
    assert job.options.broll is True
    assert job.options.visual_selection is True
    assert job.options.selection_prompt == "find X"



# ---------------------------------------------------------------------------
# v0.8.0 — Speaker Diarisation & Multi-Speaker Reframe:
#          /api/info superset + upload option passthrough
# ---------------------------------------------------------------------------
def test_info_advertises_reframe_option_lists(client):
    """`/api/info` advertises the new reframe layout + intensity lists in
    addition to the pre-existing effects lists (superset guarantee)."""
    effects = client.get("/api/info").json()["effects"]

    # New v0.8.0 lists (Reqs 7.4, 10.6, 17.5, 18.1).
    assert effects["reframe_layouts"] == ["follow_active", "split_screen"]
    assert effects["reframe_intensities"] == ["subtle", "standard", "heavy"]

    # Additive — pre-existing effects keys/values remain present (superset).
    assert "caption_presets" in effects
    assert effects["caption_templates"] == ["karaoke", "boxed", "minimal"]


def test_upload_threads_reframe_fields_into_from_dict(client):
    """The new v0.8.0 upload Form fields reach ProcessingOptions.from_dict and
    land on the stored job's options (interception via the job store, mirroring
    the Tier 1 passthrough test)."""
    resp = client.post(
        "/api/upload",
        files={"files": ("clip.mp4", b"FAKEVIDEODATA", "video/mp4")},
        data={
            "speaker_reframe": "true",
            "diarization": "true",
            "reframe_layout": "split_screen",
            "reframe_intensity": "heavy",
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["id"]

    job = get_manager().store.get(job_id)
    assert job is not None
    assert job.options.speaker_reframe is True
    assert job.options.diarization is True
    assert job.options.reframe_layout == "split_screen"
    assert job.options.reframe_intensity == "heavy"


def test_upload_unknown_reframe_layout_falls_back_to_default(client):
    """An unknown `reframe_layout` submitted via the upload Form falls back to
    the documented default through the API path (Req 18.5)."""
    resp = client.post(
        "/api/upload",
        files={"files": ("clip.mp4", b"FAKEVIDEODATA", "video/mp4")},
        data={"speaker_reframe": "true", "reframe_layout": "bogus"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["id"]

    job = get_manager().store.get(job_id)
    assert job is not None
    assert job.options.reframe_layout == "follow_active"
