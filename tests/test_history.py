"""Tests for the SQLite history/campaign store."""
from __future__ import annotations

import time


def test_record_and_sync_clip(history, fake_clip, tmp_path):
    path = tmp_path / "clip_c1.mp4"
    history.record_clip("job1", fake_clip, path, campaign_id="camp1")

    data = history.history()
    assert len(data["clips"]) == 1
    row = data["clips"][0]
    assert row["job_id"] == "job1"
    assert row["clip_id"] == "c1"
    assert row["hashtags"] == ["#viral", "#shorts"]
    assert row["campaign_id"] == "camp1"

    # record_clip is idempotent on (job_id, clip_id) and updates metadata.
    fake_clip.title = "Updated title"
    fake_clip.hashtags = ["#new"]
    history.record_clip("job1", fake_clip, path, campaign_id="camp1")
    history.sync_clip("job1", fake_clip)
    data = history.history()
    assert len(data["clips"]) == 1
    assert data["clips"][0]["title"] == "Updated title"
    assert data["clips"][0]["hashtags"] == ["#new"]


def test_attempt_lifecycle_and_due(history):
    now = time.time()
    past = history.create_attempt(job_id="j", clip_id="c", platform="youtube",
                                  request={"video_path": "x"}, scheduled_at=now - 5,
                                  state="queued")
    future = history.create_attempt(job_id="j", clip_id="c", platform="tiktok",
                                    request={"video_path": "x"}, scheduled_at=now + 3600,
                                    state="scheduled")

    due = history.due_attempts(now)
    due_ids = {d["id"] for d in due}
    assert past in due_ids
    assert future not in due_ids

    history.update_attempt(past, state="published", url="https://y/1", external_id="v1")
    updated = history.get_attempt(past)
    assert updated["state"] == "published"
    assert updated["url"] == "https://y/1"


def test_history_platform_filter(history):
    history.create_attempt(job_id="j", clip_id="c", platform="youtube",
                           request={}, scheduled_at=0, state="queued")
    history.create_attempt(job_id="j", clip_id="c", platform="x",
                           request={}, scheduled_at=0, state="queued")
    only_x = history.history(platform="x")["publish_attempts"]
    assert len(only_x) == 1
    assert only_x[0]["platform"] == "x"


def test_campaign_crud(history):
    routes = {"youtube": {"account_id": "chan1", "target_type": "", "target_id": ""}}
    saved = history.save_campaign("Launch", routes)
    assert saved.id
    fetched = history.campaign(saved.id)
    assert fetched is not None
    assert fetched.routes["youtube"]["account_id"] == "chan1"
    assert any(c.id == saved.id for c in history.campaigns())
