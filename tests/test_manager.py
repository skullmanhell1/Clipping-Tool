"""Tests for the publish manager: routing, scheduling, and throttling."""
from __future__ import annotations

import time

import pytest

from publishers import preflight
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
    monkeypatch.setattr(manager_mod.settings, "publish_min_interval_floor_seconds", 999)
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



def test_each_publisher_own_rate_limit_governs(tmp_path, fake_clip, video_file, monkeypatch):
    """A publisher's declared ``min_interval_seconds`` is what throttles it.

    Regression: the scheduler applied
    ``max(publisher.min_interval_seconds, settings.publish_default_interval_seconds)``
    with that setting defaulting to 30s. Every real publisher declares 2-18s, so the
    30s floor overrode all of them and ``min_interval_seconds`` was dead on every
    publisher. The setting is now a floor defaulting to 0.

    Two platforms are used so the assertion cannot pass by accident, and two poll cycles
    are driven because ``run_due_once`` sends at most one attempt per platform per poll
    (it captures ``now`` once). Across two immediate polls the fast publisher — whose
    declared interval is well under the old 30s floor — must send both attempts, while the
    slow one is still inside its own window.
    """
    import publishers.manager as manager_mod

    monkeypatch.setattr(manager_mod.settings, "publish_min_interval_floor_seconds", 0.0)
    fast = FakePublisher("whop", min_interval_seconds=0.0)
    slow = FakePublisher("youtube", min_interval_seconds=999)
    manager, _store = _manager(tmp_path, {"whop": fast, "youtube": slow})

    for job in ("j1", "j2"):
        manager.submit(job_id=job, clip=fake_clip, video_path=video_file,
                       platforms=["whop", "youtube"], mode="auto")

    manager.run_due_once()
    manager.run_due_once()

    assert len(fast.published) == 2, "the fast publisher was throttled by a global floor"
    assert len(slow.published) == 1, "the slow publisher ignored its own rate limit"


def test_the_floor_can_still_slow_a_fast_publisher(tmp_path, fake_clip, video_file,
                                                   monkeypatch):
    """The floor remains available for operators who want to be more conservative."""
    import publishers.manager as manager_mod

    monkeypatch.setattr(manager_mod.settings, "publish_min_interval_floor_seconds", 999)
    fast = FakePublisher("whop", min_interval_seconds=0.0)
    manager, _store = _manager(tmp_path, {"whop": fast})

    for job in ("j1", "j2"):
        manager.submit(job_id=job, clip=fake_clip, video_path=video_file,
                       platforms=["whop"], mode="auto")

    manager.run_due_once()
    manager.run_due_once()

    # The floor, not the publisher's own 0s interval, is what stops the second send.
    assert len(fast.published) == 1


def test_the_shipped_floor_default_trusts_the_publishers():
    """0 by default, so per-publisher limits are effective out of the box."""
    from config import Settings

    assert Settings(_env_file=None).publish_min_interval_floor_seconds == 0.0


@pytest.fixture(autouse=True)
def _accept_stub_clips(monkeypatch):
    """Let the byte-stub ``video_file`` fixture through the O10 pre-flight.

    These tests are about routing, scheduling and throttling, and their clips are a few
    hundred bytes of fake MP4 header - ffprobe cannot read them, so the pre-flight correctly
    refuses to upload them and every scheduling assertion would fail for the wrong reason.

    Stubbed at the seam rather than weakened: ``validate_clip`` blocking an unprobeable file
    is deliberate (publishing a file ffprobe cannot read will not go well either), and it is
    covered by ``tests/test_publish_preflight.py`` against real media, including a
    manager-level test that a rejected clip never reaches the publisher.
    """
    monkeypatch.setattr(
        preflight, "validate_clip",
        lambda video_path, platform: preflight.PreflightReport(platform=platform),
    )
