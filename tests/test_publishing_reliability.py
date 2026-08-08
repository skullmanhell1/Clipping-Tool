"""Publishing reliability and scheduling: PB4, PB5, PB6, PB7.

* **PB4** OAuth token caching, expiry and refresh;
* **PB5** automatic retry of transient failures with exponential backoff;
* **PB6** per-platform caption/hashtag fitting at publish time;
* **PB7** the scheduling window, rescheduling, cancellation and best-time suggestions.

The invariant threaded through the PB5 tests is that automatic retry must never touch a
``review_required`` attempt. That state is waiting on a person, and re-queueing it would either
loop forever or silently escalate a review-mode submission into a live post - the same line
``/approve`` and ``/retry`` already draw.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from config import settings as app_settings
from publishers import best_times, retry, tailoring
from publishers.base import PublisherStatus, PublishResult, PublishState
from publishers.history import HistoryStore
from publishers.manager import PublishManager
from worker.metadata import get_profile

# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class _Clip:
    id = "0"
    title = "A title that is quite long but not absurdly so for testing purposes"
    # Long enough that X's 260-character caption budget genuinely has to cut it, and short enough
    # that YouTube's 1000 keeps all of it. Written first at 170 characters, which fit *both* - so
    # the fitting tests passed without any fitting happening.
    description = (
        "First sentence carries the hook and is complete on its own. Second sentence adds the "
        "detail that makes the whole thing land properly for a viewer. Third sentence is where a "
        "platform limit usually starts to bite hard on a caption like this. Fourth sentence is "
        "past every short-form limit worth respecting. Fifth sentence exists only to be dropped."
    )
    hashtags = ["#one", "#two", "#three", "#four", "#five", "#six", "#seven", "#eight", "#nine"]
    hook_text = "watch this"
    cta = "Follow for more"
    mentions = ["@someone"]
    filename = "clip_0.mp4"
    score = 1.0


class _ScriptedPublisher:
    """A publisher returning queued results in order, recording each call."""

    name = "tiktok"
    min_interval_seconds = 0.0

    def __init__(self, results, *, token_kind="static", expires_at=None):
        self.results = list(results)
        self.calls: list = []
        self.refreshes = 0
        self._token_kind = token_kind
        self._expires_at = expires_at

    def status(self, account_id=""):
        return PublisherStatus(
            self.name,
            True,
            True,
            True,
            "ready",
            "ready",
            account_id,
            token_expires_at=self._expires_at,
            token_kind=self._token_kind,
        )

    def is_configured(self):
        return True

    def refresh_credentials(self, account_id=""):
        self.refreshes += 1
        self._expires_at = time.time() + 3600
        return True

    def invalidate_credentials(self, account_id=""):
        self._expires_at = None

    def publish(self, request):
        self.calls.append(request)
        if self.results:
            return self.results.pop(0)
        return PublishResult(True, PublishState.PUBLISHED, self.name, url="https://example/1")


@pytest.fixture
def store(tmp_path):
    return HistoryStore(tmp_path / "history.db")


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "clip_0.mp4"
    path.write_bytes(b"\x00" * 2048)
    return path


def _manager(store, publisher, monkeypatch):
    manager = PublishManager(publishers={publisher.name: publisher}, history=store, autostart=False)
    # Preflight probes the file with ffprobe; the fixture is not real video.
    monkeypatch.setattr(
        "publishers.preflight.validate_clip",
        lambda *_a, **_k: type(
            "R", (), {"ok": True, "summary": lambda self: "ok", "to_dict": lambda self: {}}
        )(),
    )
    return manager


def _submit(manager, video):
    ids = manager.submit(
        job_id="job1", clip=_Clip(), video_path=video, platforms=["tiktok"], mode="auto"
    )
    assert ids
    return ids[0]


# --------------------------------------------------------------------------- #
# PB5 - classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "error",
    [
        "Server error '503 Service Unavailable' for url 'https://api'",
        "429 Too Many Requests",
        "Read timed out.",
        "Connection reset by peer",
        "Temporary failure in name resolution",
        "The remote end closed the connection without response",
    ],
)
def test_pb5_transient_failures_are_recognised(error):
    assert retry.classify(error) is True


@pytest.mark.parametrize(
    "error",
    [
        "Video is too long for this platform",
        "invalid_grant: Token has been revoked",
        "403 Forbidden: missing permission",
        "Clip rejected before upload - tiktok: 700s exceeds 600s maximum",
        "Duplicate upload detected",
        "",
    ],
)
def test_pb5_permanent_failures_are_not_retried(error):
    assert retry.classify(error) is False


def test_pb5_a_permanent_error_saying_try_again_is_still_permanent():
    """Platform error bodies are chatty, and precedence has to be right.

    "video too long, please try again with a shorter clip" contains "try again" while being the
    least retryable error available. Without permanent patterns taking precedence, a rejected clip
    would be uploaded once per retry before failing.
    """
    assert retry.classify("Video too long, please try again with a shorter clip") is False


def test_pb5_an_unrecognised_error_is_treated_as_permanent():
    """Retrying something we cannot identify is how a broken config hides behind a retry loop."""
    assert retry.classify("something inexplicable happened") is False


def test_pb5_a_status_code_wins_over_the_text():
    assert retry.classify("anything at all", status_code=503) is True
    assert retry.classify("timed out", status_code=400) is False


def test_pb5_a_401_is_not_retried_on_its_own():
    """An expired token needs a refresh, not a wait; retrying a revoked one spins to the cap."""
    assert retry.classify("401 Unauthorized") is False


# --------------------------------------------------------------------------- #
# PB5 - backoff
# --------------------------------------------------------------------------- #


def test_pb5_backoff_grows_exponentially():
    delays = [retry.backoff_seconds(n, jitter=0.0) for n in range(1, 5)]
    assert delays == sorted(delays)
    assert delays[1] == pytest.approx(delays[0] * 2)
    assert delays[2] == pytest.approx(delays[0] * 4)


def test_pb5_backoff_is_capped(monkeypatch):
    monkeypatch.setattr(app_settings, "publish_retry_max_seconds", 120.0, raising=False)
    assert retry.backoff_seconds(20, jitter=0.0) == pytest.approx(120.0)


def test_pb5_jitter_only_ever_adds():
    """Jitter must not make a delay shorter than the backoff it came from."""
    base = retry.backoff_seconds(2, jitter=0.0)
    for fraction in (0.0, 0.5, 1.0):
        assert retry.backoff_seconds(2, jitter=fraction) >= base


def test_pb5_jitter_spreads_simultaneous_retries():
    """Without this, every attempt failing on one outage retries in lockstep forever."""
    delays = {retry.backoff_seconds(3) for _ in range(40)}
    assert len(delays) > 1


def test_pb5_retry_budget_is_respected(monkeypatch):
    monkeypatch.setattr(app_settings, "publish_max_retries", 2, raising=False)
    assert retry.max_attempts() == 3
    assert retry.should_retry(0, "503 Service Unavailable") is True
    assert retry.should_retry(1, "503 Service Unavailable") is True
    assert retry.should_retry(2, "503 Service Unavailable") is False


def test_pb5_zero_retries_restores_single_shot_behaviour(monkeypatch):
    monkeypatch.setattr(app_settings, "publish_max_retries", 0, raising=False)
    assert retry.max_attempts() == 1
    assert retry.should_retry(0, "503 Service Unavailable") is False


# --------------------------------------------------------------------------- #
# PB5 - the manager actually retries
# --------------------------------------------------------------------------- #


def test_pb5_a_transient_failure_is_rescheduled_not_failed(store, video, monkeypatch):
    monkeypatch.setattr(app_settings, "publish_max_retries", 3, raising=False)
    publisher = _ScriptedPublisher(
        [
            PublishResult(False, PublishState.FAILED, "tiktok", error="503 Service Unavailable"),
        ]
    )
    manager = _manager(store, publisher, monkeypatch)
    attempt_id = _submit(manager, video)

    manager.run_due_once()
    item = store.get_attempt(attempt_id)
    assert item["state"] == PublishState.SCHEDULED.value
    assert item["retry_count"] == 1
    assert item["scheduled_at"] > time.time()
    assert item["completed_at"] is None
    assert "retry 1 of" in (item["message"] or "")


def test_pb5_a_permanent_failure_fails_immediately(store, video, monkeypatch):
    publisher = _ScriptedPublisher(
        [
            PublishResult(False, PublishState.FAILED, "tiktok", error="Video is too long"),
        ]
    )
    manager = _manager(store, publisher, monkeypatch)
    attempt_id = _submit(manager, video)

    manager.run_due_once()
    item = store.get_attempt(attempt_id)
    assert item["state"] == PublishState.FAILED.value
    assert item["retry_count"] == 0
    assert item["completed_at"] is not None
    # Not dressed up as a retry that never happened.
    assert "gave up after" not in (item["error"] or "")


def test_pb5_a_retry_that_succeeds_publishes(store, video, monkeypatch):
    monkeypatch.setattr(app_settings, "publish_max_retries", 3, raising=False)
    monkeypatch.setattr(app_settings, "publish_retry_base_seconds", 1.0, raising=False)
    publisher = _ScriptedPublisher(
        [
            PublishResult(False, PublishState.FAILED, "tiktok", error="502 Bad Gateway"),
            PublishResult(True, PublishState.PUBLISHED, "tiktok", url="https://example/ok"),
        ]
    )
    manager = _manager(store, publisher, monkeypatch)
    attempt_id = _submit(manager, video)

    manager.run_due_once()
    assert store.get_attempt(attempt_id)["state"] == PublishState.SCHEDULED.value

    # Make the retry due and run the scheduler again.
    store.update_attempt(attempt_id, scheduled_at=time.time() - 1)
    manager._last.clear()
    manager.run_due_once()

    item = store.get_attempt(attempt_id)
    assert item["state"] == PublishState.PUBLISHED.value
    assert item["url"] == "https://example/ok"
    assert len(publisher.calls) == 2


def test_pb5_retries_are_exhausted_and_say_so(store, video, monkeypatch):
    monkeypatch.setattr(app_settings, "publish_max_retries", 2, raising=False)
    monkeypatch.setattr(app_settings, "publish_retry_base_seconds", 1.0, raising=False)
    publisher = _ScriptedPublisher(
        [
            PublishResult(False, PublishState.FAILED, "tiktok", error="503 Service Unavailable")
            for _ in range(5)
        ]
    )
    manager = _manager(store, publisher, monkeypatch)
    attempt_id = _submit(manager, video)

    for _ in range(4):
        store.update_attempt(attempt_id, scheduled_at=time.time() - 1)
        manager._last.clear()
        manager.run_due_once()

    item = store.get_attempt(attempt_id)
    assert item["state"] == PublishState.FAILED.value
    # 1 initial + 2 retries, and no more.
    assert len(publisher.calls) == 3
    assert "gave up after 3 attempts" in item["error"]
    # The platform's own error survives alongside the summary.
    assert "503" in item["error"]


def test_pb5_review_required_is_never_retried_automatically(store, video, monkeypatch):
    """The line PB5 must not cross: that state is waiting on a person."""
    monkeypatch.setattr(app_settings, "publish_max_retries", 3, raising=False)
    publisher = _ScriptedPublisher(
        [
            PublishResult(True, PublishState.REVIEW_REQUIRED, "tiktok", message="approve first"),
        ]
    )
    manager = _manager(store, publisher, monkeypatch)
    attempt_id = _submit(manager, video)

    manager.run_due_once()
    item = store.get_attempt(attempt_id)
    assert item["state"] == PublishState.REVIEW_REQUIRED.value
    assert item["retry_count"] == 0
    assert item["completed_at"] is not None


def test_pb5_review_required_is_not_retried_even_with_a_transient_error(store, video, monkeypatch):
    """Pins the *state* guard, not the error classification.

    The previous test passes either way: a review_required result carries no error, so the
    classifier declines it and the outcome is the same whether or not the state is checked.
    Removing the state guard therefore changed nothing observable - which is exactly the sort of
    invariant that quietly stops holding.

    A publisher *can* return review_required alongside a transient-sounding message (a platform
    that timed out while checking permissions, say). With the guard, that still waits for a human.
    Without it, the attempt would be silently re-queued - and re-queueing a review-mode submission
    is how it eventually gets posted without anyone approving it.
    """
    monkeypatch.setattr(app_settings, "publish_max_retries", 3, raising=False)
    publisher = _ScriptedPublisher(
        [
            PublishResult(
                True,
                PublishState.REVIEW_REQUIRED,
                "tiktok",
                error="503 Service Unavailable",
                message="approval check timed out",
            ),
        ]
    )
    manager = _manager(store, publisher, monkeypatch)
    attempt_id = _submit(manager, video)

    manager.run_due_once()
    item = store.get_attempt(attempt_id)
    assert item["state"] == PublishState.REVIEW_REQUIRED.value
    assert item["retry_count"] == 0
    assert item["completed_at"] is not None


def test_pb5_a_retrying_attempt_does_not_delete_the_local_clip(store, video, monkeypatch):
    """The file is needed for the next try."""
    monkeypatch.setattr(app_settings, "publish_max_retries", 3, raising=False)
    publisher = _ScriptedPublisher(
        [
            PublishResult(False, PublishState.FAILED, "tiktok", error="timed out"),
        ]
    )
    manager = _manager(store, publisher, monkeypatch)
    _submit(manager, video)
    manager.run_due_once()
    assert video.exists()


# --------------------------------------------------------------------------- #
# PB4 - tokens
# --------------------------------------------------------------------------- #


def test_pb4_the_token_store_round_trips(store):
    assert store.get_token("youtube") is None
    store.save_token("youtube", "abc", expires_at=1234.0)
    cached = store.get_token("youtube")
    assert cached["access_token"] == "abc"
    assert cached["expires_at"] == 1234.0
    assert cached["refreshed_at"] > 0
    store.clear_token("youtube")
    assert store.get_token("youtube") is None


def test_pb4_tokens_are_scoped_per_account(store):
    store.save_token("youtube", "one", account_id="chan-a", expires_at=1.0)
    store.save_token("youtube", "two", account_id="chan-b", expires_at=2.0)
    assert store.get_token("youtube", "chan-a")["access_token"] == "one"
    assert store.get_token("youtube", "chan-b")["access_token"] == "two"


def test_pb4_youtube_caches_its_access_token(store, monkeypatch):
    """It used to exchange the refresh token on *every* publish."""
    from publishers.youtube import YouTubePublisher

    for key, value in (
        ("youtube_client_id", "id"),
        ("youtube_client_secret", "secret"),
        ("youtube_refresh_token", "refresh"),
        ("youtube_channel_id", "chan"),
    ):
        monkeypatch.setattr(app_settings, key, value, raising=False)

    exchanges = []

    class _Client:
        def post(self, url, **kwargs):
            exchanges.append(url)
            return _FakeTokenResponse({"access_token": "tok", "expires_in": 3600})

    pub = YouTubePublisher(client=_Client(), history=store)
    assert pub._token() == "tok"
    assert pub._token() == "tok"
    assert pub._token() == "tok"
    assert len(exchanges) == 1, "the cached token was not reused"


def test_pb4_an_expiring_token_is_exchanged_again(store, monkeypatch):
    """A token with seconds left would expire *during* the upload it was fetched for."""
    from publishers import youtube as youtube_module
    from publishers.youtube import YouTubePublisher

    for key, value in (
        ("youtube_client_id", "id"),
        ("youtube_client_secret", "secret"),
        ("youtube_refresh_token", "refresh"),
        ("youtube_channel_id", "chan"),
    ):
        monkeypatch.setattr(app_settings, key, value, raising=False)

    exchanges = []

    class _Client:
        def post(self, url, **kwargs):
            exchanges.append(url)
            return _FakeTokenResponse({"access_token": f"tok{len(exchanges)}", "expires_in": 3600})

    pub = YouTubePublisher(client=_Client(), history=store)
    # Cached, but inside the safety margin.
    store.save_token(
        "youtube",
        "stale",
        account_id="chan",
        expires_at=time.time() + youtube_module.TOKEN_EXPIRY_MARGIN_S / 2,
    )
    assert pub._token() == "tok1"
    assert len(exchanges) == 1


def test_pb4_status_reports_the_expiry_and_the_token_kind(store, monkeypatch):
    from publishers.youtube import YouTubePublisher

    for key, value in (
        ("youtube_client_id", "id"),
        ("youtube_client_secret", "secret"),
        ("youtube_refresh_token", "refresh"),
        ("youtube_channel_id", "chan"),
    ):
        monkeypatch.setattr(app_settings, key, value, raising=False)
    expiry = time.time() + 3600
    store.save_token("youtube", "tok", account_id="chan", expires_at=expiry)
    status = YouTubePublisher(client=object(), history=store).status()
    assert status.token_kind == "refreshable"
    assert status.token_expires_at == pytest.approx(expiry)


def test_pb4_static_token_publishers_say_they_cannot_refresh():
    """Four of the five cannot renew themselves, and saying so is the useful answer.

    Reporting them as "no expiry" would tell an operator their Instagram token is fine right up
    until the day it is not.
    """
    from publishers import build_publishers

    for name in ("tiktok", "instagram", "x"):
        pub = build_publishers()[name]
        assert pub.status().token_kind == "static"
        assert pub.refresh_credentials() is False
    whop = build_publishers()["whop"]
    assert whop.status().token_kind == "none"
    assert whop.refresh_credentials() is False


def test_pb4_the_manager_refreshes_an_expiring_token(store, video, monkeypatch):
    publisher = _ScriptedPublisher(
        [PublishResult(True, PublishState.PUBLISHED, "tiktok")],
        token_kind="refreshable",
        expires_at=time.time() + 10,  # inside the margin
    )
    manager = _manager(store, publisher, monkeypatch)
    _submit(manager, video)
    manager.run_due_once()
    assert publisher.refreshes == 1


def test_pb4_a_healthy_token_is_not_refreshed(store, video, monkeypatch):
    publisher = _ScriptedPublisher(
        [PublishResult(True, PublishState.PUBLISHED, "tiktok")],
        token_kind="refreshable",
        expires_at=time.time() + 86400,
    )
    manager = _manager(store, publisher, monkeypatch)
    _submit(manager, video)
    manager.run_due_once()
    assert publisher.refreshes == 0


def test_pb4_a_static_token_publisher_is_never_asked_to_refresh(store, video, monkeypatch):
    """Refreshing what cannot be refreshed would be a wasted call before every upload."""
    publisher = _ScriptedPublisher(
        [PublishResult(True, PublishState.PUBLISHED, "tiktok")],
        token_kind="static",
        expires_at=time.time() + 10,
    )
    manager = _manager(store, publisher, monkeypatch)
    _submit(manager, video)
    manager.run_due_once()
    assert publisher.refreshes == 0


class _FakeTokenResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


# --------------------------------------------------------------------------- #
# PB6 - per-platform tailoring
# --------------------------------------------------------------------------- #


def _request():
    return {
        "video_path": "/tmp/clip.mp4",
        "title": _Clip.title,
        "description": _Clip.description,
        "hashtags": list(_Clip.hashtags),
        "cta": _Clip.cta,
        "mentions": list(_Clip.mentions),
    }


@pytest.mark.parametrize("platform", ["tiktok", "x", "instagram", "youtube", "whop"])
def test_pb6_the_rendered_caption_fits_the_platform(platform):
    """The limit applies to the caption a platform receives, not to one field of it."""
    from publishers.base import PublishRequest

    profile = get_profile(platform)
    tailored = tailor = tailoring.tailor_request(_request(), platform)
    request = PublishRequest(
        video_path=Path(tailor["video_path"]),
        title=tailored["title"],
        description=tailored["description"],
        hashtags=tailored["hashtags"],
        cta=tailored["cta"],
        mentions=tailored["mentions"],
    )
    assert len(request.title) <= profile.title_max
    assert len(request.caption) <= profile.desc_max
    assert len(tailored["hashtags"]) <= profile.hashtag_max


def test_pb6_the_call_to_action_and_hashtags_survive():
    """These are the parts truncation removed first, being at the end."""
    tailored = tailoring.tailor_request(_request(), "x")
    assert tailored["cta"], "the CTA was dropped"
    assert tailored["hashtags"], "every hashtag was dropped"


def test_pb6_the_description_is_cut_at_a_sentence_boundary():
    """Not mid-word, which is what a character-index truncation does."""
    tailored = tailoring.tailor_request(_request(), "x")
    description = tailored["description"]
    # It really was shortened - otherwise this asserts nothing about cutting.
    assert len(description) < len(_Clip.description)
    assert description.endswith((".", "!", "?")), description
    # And it is a prefix of the original: fitting must not invent text.
    assert _Clip.description.startswith(description)


def test_pb6_short_copy_is_left_alone():
    """Tailoring is a fit, not a rewrite: text that already fits must be untouched."""
    request = {
        "video_path": "/tmp/c.mp4",
        "title": "Short title",
        "description": "Short body.",
        "hashtags": ["#a"],
        "cta": "Go",
        "mentions": [],
    }
    tailored = tailoring.tailor_request(request, "youtube")
    assert tailored["title"] == "Short title"
    assert tailored["description"] == "Short body."
    assert tailored["cta"] == "Go"
    assert tailored["hashtags"] == ["#a"]


def test_pb6_tailoring_does_not_mutate_the_stored_request():
    """Otherwise every retry would be a little shorter than the last."""
    original = _request()
    snapshot = dict(original)
    tailoring.tailor_request(original, "x")
    assert original == snapshot


def test_pb6_re_tailoring_starts_from_the_full_text():
    """Cross-posting must not compound: X then YouTube should not give X-length copy."""
    request = _request()
    for_x = tailoring.tailor_request(request, "x")
    for_youtube = tailoring.tailor_request(request, "youtube")
    assert len(for_youtube["description"]) > len(for_x["description"])
    assert len(for_youtube["hashtags"]) > len(for_x["hashtags"])


def test_pb6_hashtags_keep_their_order_and_uniqueness():
    tags = tailoring.fit_hashtags(["#b", "#a", "#b", "#c"], get_profile("x"))
    assert tags == ["#b", "#a", "#c"]


def test_pb6_a_platform_with_no_profile_uses_the_generic_one():
    tailored = tailoring.tailor_request(_request(), "myspace")
    generic = get_profile("generic")
    assert len(tailored["hashtags"]) <= generic.hashtag_max


def test_pb6_the_manager_tailors_per_platform(store, video, monkeypatch):
    """A fitting function nothing calls is not a feature."""
    publisher = _ScriptedPublisher([PublishResult(True, PublishState.PUBLISHED, "tiktok")])
    manager = _manager(store, publisher, monkeypatch)
    _submit(manager, video)
    manager.run_due_once()

    assert publisher.calls, "nothing was published"
    sent = publisher.calls[0]
    profile = get_profile("tiktok")
    assert len(sent.title) <= profile.title_max
    assert len(sent.caption) <= profile.desc_max
    assert len(sent.hashtags) <= profile.hashtag_max


def test_pb6_the_stored_request_keeps_the_full_copy(store, video, monkeypatch):
    """So the record shows what was generated, and a retry re-fits from it."""
    publisher = _ScriptedPublisher([PublishResult(True, PublishState.PUBLISHED, "tiktok")])
    manager = _manager(store, publisher, monkeypatch)
    attempt_id = _submit(manager, video)
    manager.run_due_once()
    stored = store.get_attempt(attempt_id)["request_json"]
    assert stored["description"] == _Clip.description
    assert len(stored["hashtags"]) == len(_Clip.hashtags)


def test_pb6_llm_tailoring_is_off_by_default():
    """It costs a model call per platform per clip."""
    assert app_settings.publish_tailor_with_llm is False


# --------------------------------------------------------------------------- #
# PB7 - scheduling
# --------------------------------------------------------------------------- #


def test_pb7_the_window_query_returns_attempts_in_range(store, video):
    now = time.time()
    inside = store.create_attempt(
        job_id="j",
        clip_id="0",
        platform="tiktok",
        request={"video_path": str(video)},
        scheduled_at=now + 3600,
        state="scheduled",
    )
    store.create_attempt(
        job_id="j",
        clip_id="1",
        platform="tiktok",
        request={"video_path": str(video)},
        scheduled_at=now + 40 * 86400,
        state="scheduled",
    )
    found = store.scheduled_between(now, now + 86400)
    assert [a["id"] for a in found] == [inside]


def test_pb7_the_window_includes_finished_attempts(store, video):
    """A calendar that hid what already published would show an empty week that was full."""
    now = time.time()
    store.create_attempt(
        job_id="j",
        clip_id="0",
        platform="tiktok",
        request={"video_path": str(video)},
        scheduled_at=now - 3600,
        state="published",
    )
    found = store.scheduled_between(now - 86400, now)
    assert len(found) == 1
    assert found[0]["state"] == "published"


def test_pb7_suggestions_are_never_in_the_past():
    now = time.time()
    for suggestion in best_times.suggest("tiktok", days=3, now=now):
        assert suggestion.at > now


def test_pb7_suggestions_avoid_slots_already_taken():
    """Otherwise the calendar keeps recommending the one best hour that is already full."""
    now = time.time()
    first = best_times.suggest("tiktok", days=2, per_day=1, now=now)
    assert first
    taken = [first[0].at]
    second = best_times.suggest("tiktok", days=2, per_day=1, now=now, taken=taken)
    assert all(abs(s.at - taken[0]) >= 3600 for s in second)


def test_pb7_suggestions_are_ranked_best_first():
    found = best_times.suggest("tiktok", days=5, per_day=2)
    ranks = [s.rank for s in found]
    assert ranks == sorted(ranks, reverse=True)


def test_pb7_each_platform_has_its_own_windows():
    """X is a working-hours conversation; TikTok is an evening one. One table would be wrong."""
    assert best_times.windows_for("x") != best_times.windows_for("tiktok")
    assert best_times.windows_for("nonsense") == best_times.DEFAULT_WINDOWS


def test_pb7_suggestions_state_what_they_are_based_on():
    """These are published heuristics, not this account's measured engagement.

    PB8 is what would make them measured, and it does not exist yet. The API returns this string
    so a UI cannot present a guess as an analysis.
    """
    assert "not measured" in best_times.BASIS.lower()
    assert "pb8" in best_times.BASIS.lower()


def test_pb7_per_day_limit_is_respected():
    from collections import Counter
    from datetime import datetime

    found = best_times.suggest("tiktok", days=4, per_day=2)
    per_day = Counter(datetime.fromtimestamp(s.at).date() for s in found)
    assert all(count <= 2 for count in per_day.values())


# --------------------------------------------------------------------------- #
# PB7 - the API
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch, store):
    from fastapi.testclient import TestClient

    import api.main as main

    monkeypatch.setattr(main, "get_history", lambda: store)
    return TestClient(main.app)


def test_pb7_schedule_endpoint_rejects_a_backwards_window(client):
    response = client.get("/api/schedule?start=200&end=100")
    assert response.status_code == 400


def test_pb7_suggestions_endpoint_returns_the_basis(client):
    payload = client.get("/api/schedule/suggestions?platform=tiktok&days=2").json()
    assert payload["basis"]
    assert payload["suggestions"]
    assert payload["platform"] == "tiktok"


def test_pb7_a_pending_attempt_can_be_rescheduled(client, store, video):
    attempt_id = store.create_attempt(
        job_id="j",
        clip_id="0",
        platform="tiktok",
        request={"video_path": str(video)},
        scheduled_at=time.time() + 60,
        state="scheduled",
    )
    when = time.time() + 7200
    response = client.patch(
        f"/api/publish-attempts/{attempt_id}/schedule", json={"schedule_at": when}
    )
    assert response.status_code == 200
    assert response.json()["scheduled_at"] == pytest.approx(when)


def test_pb7_rescheduling_into_the_past_queues_it_now(client, store, video):
    """A state that disagrees with the clock is what makes a queue hard to reason about."""
    attempt_id = store.create_attempt(
        job_id="j",
        clip_id="0",
        platform="tiktok",
        request={"video_path": str(video)},
        scheduled_at=time.time() + 600,
        state="scheduled",
    )
    response = client.patch(
        f"/api/publish-attempts/{attempt_id}/schedule",
        json={"schedule_at": time.time() - 100},
    )
    assert response.status_code == 200
    assert response.json()["state"] == PublishState.QUEUED.value


@pytest.mark.parametrize("state", ["uploading", "published", "failed", "review_required"])
def test_pb7_only_pending_attempts_can_be_rescheduled(client, store, video, state):
    """`failed` is excluded too: that would be a retry that skipped every check /retry makes."""
    attempt_id = store.create_attempt(
        job_id="j",
        clip_id="0",
        platform="tiktok",
        request={"video_path": str(video)},
        scheduled_at=time.time() + 60,
        state=state,
    )
    response = client.patch(
        f"/api/publish-attempts/{attempt_id}/schedule",
        json={"schedule_at": time.time() + 3600},
    )
    assert response.status_code == 409


def test_pb7_a_pending_attempt_can_be_cancelled(client, store, video):
    attempt_id = store.create_attempt(
        job_id="j",
        clip_id="0",
        platform="tiktok",
        request={"video_path": str(video)},
        scheduled_at=time.time() + 60,
        state="scheduled",
    )
    response = client.post(f"/api/publish-attempts/{attempt_id}/cancel")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == PublishState.FAILED.value
    assert "cancelled" in body["error"].lower()


def test_pb7_a_cancelled_attempt_is_kept_not_deleted(client, store, video):
    """A row that vanishes is indistinguishable from one that never existed."""
    attempt_id = store.create_attempt(
        job_id="j",
        clip_id="0",
        platform="tiktok",
        request={"video_path": str(video)},
        scheduled_at=time.time() + 60,
        state="queued",
    )
    client.post(f"/api/publish-attempts/{attempt_id}/cancel")
    assert store.get_attempt(attempt_id) is not None


def test_pb7_a_cancelled_attempt_is_not_picked_up_by_the_scheduler(
    client, store, video, monkeypatch
):
    attempt_id = store.create_attempt(
        job_id="j",
        clip_id="0",
        platform="tiktok",
        request={"video_path": str(video)},
        scheduled_at=time.time() - 10,
        state="queued",
    )
    client.post(f"/api/publish-attempts/{attempt_id}/cancel")
    publisher = _ScriptedPublisher([PublishResult(True, PublishState.PUBLISHED, "tiktok")])
    manager = _manager(store, publisher, monkeypatch)
    assert manager.run_due_once() == []
    assert publisher.calls == []


def test_pb4_the_refresh_endpoint_is_honest_about_static_tokens(client):
    payload = client.post("/api/publishers/tiktok/refresh").json()
    assert payload["refreshed"] is False
    assert payload["status"]["token_kind"] == "static"


def test_pb4_the_refresh_endpoint_404s_on_an_unknown_platform(client):
    assert client.post("/api/publishers/myspace/refresh").status_code == 404
