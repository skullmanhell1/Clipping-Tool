"""Tests for the publish manager: routing, scheduling, and throttling."""
from __future__ import annotations

import time

from publishers.base import PublishResult, PublishState
from publishers.history import HistoryStore
from publishers.manager import PublishManager
from tests.fakes import FakePublisher


def _manager(tmp_path, publishers):
    store = HistoryStore(tmp_path / "history.db")
    return PublishManager(publishers=publishers, history=store, autostart=False), store


def test_submit_routes_via_campaign(tmp_path, fake_clip, video_file):
    pub = FakePublisher("youtube")
    manager, store = _manager(tmp_path, {"youtube": pub})
    campaign = store.save_campaign(
        "camp", {"youtube": {"account_id": "chan9", "target_type": "", "target_id": ""}}
    )

    ids = manager.submit(job_id="j", clip=fake_clip, video_path=video_file,
                         platforms=[], campaign_id=campaign.id, mode="auto")
    assert len(ids) == 1
    attempt = store.get_attempt(ids[0])
    assert attempt["platform"] == "youtube"
    assert attempt["account_id"] == "chan9"
    assert attempt["state"] == "queued"


def test_submit_skips_unknown_platform(tmp_path, fake_clip, video_file):
    manager, store = _manager(tmp_path, {"youtube": FakePublisher("youtube")})
    ids = manager.submit(job_id="j", clip=fake_clip, video_path=video_file,
                         platforms=["youtube", "myspace"], mode="auto")
    assert len(ids) == 1


def test_scheduled_attempt_not_due_until_time(tmp_path, fake_clip, video_file):
    pub = FakePublisher("x")
    manager, store = _manager(tmp_path, {"x": pub})
    ids = manager.submit(job_id="j", clip=fake_clip, video_path=video_file,
                         platforms=["x"], mode="auto",
                         schedule_at=time.time() + 3600)
    assert store.get_attempt(ids[0])["state"] == "scheduled"

    processed = manager.run_due_once()
    assert processed == []
    assert pub.published == []


def test_run_due_executes_and_records_result(tmp_path, fake_clip, video_file):
    pub = FakePublisher("youtube")
    manager, store = _manager(tmp_path, {"youtube": pub})
    ids = manager.submit(job_id="j", clip=fake_clip, video_path=video_file,
                         platforms=["youtube"], mode="auto")

    processed = manager.run_due_once()
    assert ids[0] in processed
    assert len(pub.published) == 1

    attempt = store.get_attempt(ids[0])
    assert attempt["state"] == "published"
    assert attempt["url"] == "https://example.com/youtube"
    assert attempt["external_id"] == "ext123"


def test_throttling_defers_second_post(tmp_path, fake_clip, video_file, monkeypatch):
    import publishers.manager as manager_mod

    # Force a long per-platform interval so the second attempt is throttled.
    monkeypatch.setattr(manager_mod.settings, "publish_default_interval_seconds", 999)
    pub = FakePublisher("youtube", min_interval_seconds=999)
    manager, store = _manager(tmp_path, {"youtube": pub})

    first = manager.submit(job_id="j", clip=fake_clip, video_path=video_file,
                          platforms=["youtube"], mode="auto")[0]
    second = manager.submit(job_id="j2", clip=fake_clip, video_path=video_file,
                           platforms=["youtube"], mode="auto")[0]

    manager.run_due_once()
    # Only one should have gone out because of the throttle window.
    assert len(pub.published) == 1
    states = {store.get_attempt(first)["state"], store.get_attempt(second)["state"]}
    assert "published" in states
    assert "queued" in states


def test_missing_file_marks_failed(tmp_path, fake_clip):
    pub = FakePublisher("youtube")
    manager, store = _manager(tmp_path, {"youtube": pub})
    ids = manager.submit(job_id="j", clip=fake_clip,
                         video_path=tmp_path / "does_not_exist.mp4",
                         platforms=["youtube"], mode="auto")
    manager.run_due_once()
    attempt = store.get_attempt(ids[0])
    assert attempt["state"] == "failed"
    assert "no longer exists" in (attempt["error"] or "")
    assert pub.published == []


def test_failed_publish_result_recorded(tmp_path, fake_clip, video_file):
    failing = FakePublisher(
        "tiktok",
        result=PublishResult(False, PublishState.FAILED, "tiktok", error="boom"),
    )
    manager, store = _manager(tmp_path, {"tiktok": failing})
    ids = manager.submit(job_id="j", clip=fake_clip, video_path=video_file,
                         platforms=["tiktok"], mode="auto")
    manager.run_due_once()
    attempt = store.get_attempt(ids[0])
    assert attempt["state"] == "failed"
    assert attempt["error"] == "boom"
