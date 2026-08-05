"""Tests for resuming stalled publish attempts (``/approve`` and ``/retry``).

``review_required`` was a reachable dead end: ``publishers/instagram.py``,
``publishers/x.py`` and ``publishers/whop.py`` all return it, but no route could move an
attempt out of that state, so a post that landed there stopped permanently.

The subtle part is that Instagram and X gate ``review_required`` on a platform-side
approval flag rather than on the submitted mode. Re-queueing such an attempt without
checking the flag reproduces ``review_required`` on the next scheduler tick — an
infinite bounce that looks like a scheduler bug. These tests pin the refusal.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.deps as api_deps
import api.main as api_main
from publishers import preflight
from publishers.base import PublishState
from publishers.manager import PublishManager
from tests.fakes import FakePublisher


@pytest.fixture
def clip_on_disk(tmp_path: Path) -> Path:
    """A real file, since resuming refuses to queue an attempt whose clip is gone."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"FAKEVIDEODATA")
    return path


@pytest.fixture
def resume_env(monkeypatch, history, clip_on_disk):
    """Wire the API's history store and publish manager to per-test doubles.

    Both are process-wide singletons reached through ``get_history`` /
    ``get_publish_manager`` in ``api.main``, so they are patched at that module rather
    than mutated in place — no test leaks a publisher or a SQLite file into another.
    """
    publishers = {
        # Approval granted: an approve may proceed.
        "whop": FakePublisher("whop"),
        # Configured but not approved for direct publishing — the Instagram/X shape.
        "instagram": FakePublisher(
            "instagram", direct_publish=False,
            message="Professional account/app approval required; review mode only",
        ),
        # Not configured at all.
        "tiktok": FakePublisher(
            "tiktok", configured=False, message="Set TIKTOK_ACCESS_TOKEN"
        ),
    }
    manager = PublishManager(publishers=publishers, history=history, autostart=False)
    monkeypatch.setattr(api_deps, "get_history", lambda: history)
    monkeypatch.setattr(api_deps, "get_publish_manager", lambda: manager)
    return manager, history, clip_on_disk


@pytest.fixture
def client():
    return TestClient(api_main.app)


def _attempt(history, clip_path: Path, *, platform="whop", state="review_required",
             mode="review") -> str:
    return history.create_attempt(
        job_id="job1", clip_id="c1", platform=platform,
        request={"video_path": str(clip_path), "mode": mode, "account_id": ""},
        scheduled_at=time.time() - 10, state=state, mode=mode,
    )


# ---------------------------------------------------------------------------
# Approving
# ---------------------------------------------------------------------------


def test_approving_a_review_required_attempt_queues_it_for_direct_publish(
    client, resume_env
):
    """The dead end now has an exit: the attempt returns to ``queued`` as ``mode=auto``.

    Rewriting the mode is the substance of an approval. Leaving it as ``review`` would
    re-park the attempt in review on the very next tick.
    """
    _manager, history, clip = resume_env
    attempt_id = _attempt(history, clip)

    resp = client.post(f"/api/publish-attempts/{attempt_id}/approve")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["state"] == PublishState.QUEUED.value
    assert body["request_json"]["mode"] == "auto"
    # A resumed attempt describes the run in flight, not the previous outcome.
    assert not body["error"]
    assert body["completed_at"] is None


def test_an_approved_attempt_is_actually_picked_up_by_the_scheduler(client, resume_env):
    """Approval is only meaningful if the scheduler then runs the attempt.

    ``due_attempts`` selects on ``state IN ('queued','scheduled')`` with a due
    ``scheduled_at``, so this asserts the endpoint leaves the row in a genuinely
    runnable shape rather than merely a plausible-looking one.
    """
    manager, history, clip = resume_env
    attempt_id = _attempt(history, clip)
    client.post(f"/api/publish-attempts/{attempt_id}/approve")

    processed = manager.run_due_once()
    assert attempt_id in processed

    published = history.get_attempt(attempt_id)
    assert published["state"] == PublishState.PUBLISHED.value
    # The publisher saw the approved mode, not the original review mode.
    assert manager.publishers["whop"].published[0].mode == "auto"


def test_approval_is_refused_when_the_platform_cannot_publish_directly(
    client, resume_env
):
    """Approving an unapproved platform fails loudly instead of bouncing forever.

    This is the case that matters most: Instagram and X return ``review_required``
    because of a *permission*, not a mode. Accepting the approval would queue the
    attempt, have the publisher return ``review_required`` again, and leave the operator
    watching it flap with no explanation.
    """
    _manager, history, clip = resume_env
    attempt_id = _attempt(history, clip, platform="instagram")

    resp = client.post(f"/api/publish-attempts/{attempt_id}/approve")
    assert resp.status_code == 409
    # The platform's own wording is surfaced, so the operator learns what to fix.
    assert "approval required" in resp.json()["detail"].lower()

    # Crucially the attempt is left untouched, not half-transitioned.
    unchanged = history.get_attempt(attempt_id)
    assert unchanged["state"] == PublishState.REVIEW_REQUIRED.value
    assert unchanged["request_json"]["mode"] == "review"


def test_approval_is_refused_for_an_unconfigured_platform(client, resume_env):
    """No credentials means no publish; say so rather than queueing a doomed attempt."""
    _manager, history, clip = resume_env
    attempt_id = _attempt(history, clip, platform="tiktok")

    resp = client.post(f"/api/publish-attempts/{attempt_id}/approve")
    assert resp.status_code == 409
    assert "not configured" in resp.json()["detail"].lower()


def test_approval_is_refused_when_the_clip_file_is_gone(client, resume_env, tmp_path):
    """A deleted clip is reported now, not as a failure minutes later.

    ``delete_local_after_publish`` and the retention sweeper both remove clips, so this
    is a normal end state rather than a corner case.
    """
    _manager, history, _clip = resume_env
    missing = tmp_path / "already-deleted.mp4"
    attempt_id = _attempt(history, missing)

    resp = client.post(f"/api/publish-attempts/{attempt_id}/approve")
    assert resp.status_code == 409
    assert "no longer exists" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Which states may be resumed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["review_required", "failed"])
def test_resumable_states_can_be_retried(client, resume_env, state):
    """Both stalled states — awaiting review, and failed — can be re-queued."""
    _manager, history, clip = resume_env
    attempt_id = _attempt(history, clip, state=state)

    resp = client.post(f"/api/publish-attempts/{attempt_id}/retry")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == PublishState.QUEUED.value


@pytest.mark.parametrize(
    "state", ["queued", "scheduled", "uploading", "published", "draft", "private"]
)
def test_non_resumable_states_are_refused(client, resume_env, state):
    """In-flight and finished attempts are not resumable.

    Re-queueing a ``published`` attempt would post the clip twice, and re-queueing an
    ``uploading`` one would run two uploads concurrently. Both are worse than refusing.
    """
    _manager, history, clip = resume_env
    attempt_id = _attempt(history, clip, state=state)

    resp = client.post(f"/api/publish-attempts/{attempt_id}/retry")
    assert resp.status_code == 409
    assert history.get_attempt(attempt_id)["state"] == state


def test_retry_does_not_escalate_a_review_submission_into_a_live_post(
    client, resume_env
):
    """``/retry`` preserves ``mode``; only ``/approve`` may change it.

    Otherwise "retry this stuck item" would silently publish something the operator had
    deliberately submitted for review — the one outcome a review mode exists to prevent.
    """
    _manager, history, clip = resume_env
    attempt_id = _attempt(history, clip, state="failed", mode="review")

    resp = client.post(f"/api/publish-attempts/{attempt_id}/retry")
    assert resp.status_code == 200
    assert resp.json()["request_json"]["mode"] == "review"


# ---------------------------------------------------------------------------
# Unknown identifiers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["approve", "retry"])
def test_unknown_attempt_is_404(client, resume_env, action):
    resp = client.post(f"/api/publish-attempts/does-not-exist/{action}")
    assert resp.status_code == 404


@pytest.mark.parametrize("action", ["approve", "retry"])
def test_attempt_for_an_unknown_platform_is_409(client, resume_env, action):
    """A platform that is no longer registered cannot be resumed."""
    _manager, history, clip = resume_env
    attempt_id = _attempt(history, clip, platform="myspace")

    resp = client.post(f"/api/publish-attempts/{attempt_id}/{action}")
    assert resp.status_code == 409
    assert "unknown platform" in resp.json()["detail"].lower()


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
