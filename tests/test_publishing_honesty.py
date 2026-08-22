"""A publish could report success when nothing was posted — or post the same clip twice.

The publishing layer is where this project's failures become **irreversible**. A wrong caption can
be re-rendered; a duplicate tweet cannot be un-tweeted, and a post recorded as sent but never made
is discovered weeks later by someone wondering why a clip got no views.

Four families of defect are pinned here:

1. **False success.** A failed upload recorded as a successful one, or a success claimed on a
   response whose id was never checked — which also let the manager delete the operator's local
   copy of the clip.
2. **Double posting.** Every platform here is a multi-request flow, and an automatic retry re-ran
   it from step one. A timeout on the *last* call of an upload the platform accepted produced a
   second post, and no idempotency key existed anywhere.
3. **Credential leakage.** Publishers record `str(exc)`, and an httpx error message embeds the
   request URL. One publisher put its access token in that URL.
4. **Unreachable states.** An attempt abandoned mid-upload became invisible to the scheduler *and*
   refused by every human endpoint, so the post was silently lost and the audit trail was wrong.
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import httpx
import pytest

from config import settings
from publishers.base import PublishRequest, PublishResult, PublishState, redact
from publishers.whop import WhopPublisher


def _request(video_file, **overrides):
    base = {
        "video_path": video_file,
        "title": "Title",
        "description": "Description",
        "mode": "auto",
    }
    base.update(overrides)
    return PublishRequest(**base)


def _pretend_node_is_installed(monkeypatch):
    """The Whop publisher probes for `node` before shelling out (I7).

    Mocking `subprocess.run` does not reach a `shutil.which` gate sitting in front of it, so
    without this the tests below assert on whether *this host* has node on PATH.
    """
    monkeypatch.setattr("publishers.whop.shutil.which", lambda _name: "/usr/bin/node")
    monkeypatch.setattr(settings, "whop_api_key", "key")


# --------------------------------------------------------------------------- #
# 1. False success                                                              #
# --------------------------------------------------------------------------- #
def test_a_failed_whop_bridge_run_is_a_failure(monkeypatch, video_file):
    """The bridge reports failure **in its payload and exits 0**, and nobody read it.

    `publisher_bridge/whop.mjs`'s `fail()` writes `{"success": false, ...}` and calls
    `process.exit(0)`, so `check=True` cannot fire. The publisher only read `data["attached"]`,
    which is absent on failure — so a Whop upload that never happened (no API key inside the
    bridge, an SDK throw, an unreadable file, a network failure mid-upload) was stored as a
    *successful* attempt in `review_required` with an empty external id, and the real error was
    buried in `result_json.raw.error`.

    Because the result was not FAILED, the retry policy was never consulted either — so even a
    transient bridge failure was terminal *and* labelled fine, and the operator was invited to
    "approve" a file that does not exist on Whop.
    """
    _pretend_node_is_installed(monkeypatch)
    payload = json.dumps({"success": False, "error": "WHOP_API_KEY is not set"})
    monkeypatch.setattr(
        "publishers.whop.subprocess.run",
        lambda *a, **k: SimpleNamespace(stdout=payload, stderr="", returncode=0),
    )

    result = WhopPublisher().publish(_request(video_file))

    assert result.success is False
    assert result.state == PublishState.FAILED
    assert "WHOP_API_KEY" in result.error


def test_a_whop_success_without_a_file_id_is_not_claimed(monkeypatch, video_file):
    """A success with nothing identifying the upload cannot be verified, resumed or attached."""
    _pretend_node_is_installed(monkeypatch)
    monkeypatch.setattr(
        "publishers.whop.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            stdout=json.dumps({"success": True}), stderr="", returncode=0
        ),
    )
    result = WhopPublisher().publish(_request(video_file))
    assert result.success is False
    assert "no file id" in result.error


def test_the_bridge_stderr_reaches_the_record(monkeypatch, video_file):
    """`str(CalledProcessError)` is only "returned non-zero exit status 1".

    The Node stack trace lives in `exc.stderr` and was discarded, so the most common real cause —
    `ERR_MODULE_NOT_FOUND: Cannot find package '@whop/sdk'`, i.e. nobody ran `npm install` in
    `publisher_bridge/` — was invisible.
    """
    _pretend_node_is_installed(monkeypatch)

    def boom(*a, **k):
        raise subprocess.CalledProcessError(
            1, ["node"], output="", stderr="ERR_MODULE_NOT_FOUND: Cannot find package '@whop/sdk'"
        )

    monkeypatch.setattr("publishers.whop.subprocess.run", boom)
    result = WhopPublisher().publish(_request(video_file))
    assert result.success is False
    assert "ERR_MODULE_NOT_FOUND" in result.error


def test_non_json_bridge_output_names_both_streams(monkeypatch, video_file):
    """A Node warning on stdout produced "Expecting value: line 1 column 1"."""
    _pretend_node_is_installed(monkeypatch)
    monkeypatch.setattr(
        "publishers.whop.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            stdout="(node:1) DeprecationWarning: blah", stderr="", returncode=0
        ),
    )
    result = WhopPublisher().publish(_request(video_file))
    assert result.success is False
    assert "did not return JSON" in result.error
    assert "DeprecationWarning" in result.error


def test_x_refuses_to_claim_a_tweet_without_an_id(video_file):
    """`str(post.json().get("data", {}).get("id", ""))` produced a URL ending in nothing.

    `https://x.com/i/web/status/` was recorded as the post's URL, `external_id` was empty, and
    `_maybe_delete_local` then deleted the operator's local clip on the strength of it.
    """
    from publishers.x import XPublisher
    from tests.fakes import FakeHTTPClient, FakeResponse

    def handler(method, url, kwargs):
        if "initialize" in url:
            return FakeResponse(json_data={"id": "media1"})
        if "tweets" in url:
            return FakeResponse(json_data={})  # 200, no data.id
        return FakeResponse(json_data={})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "x_access_token", "tok")
        mp.setattr(settings, "x_direct_post_approved", True)
        pub = XPublisher(client=FakeHTTPClient(handler), sleep=lambda _s: None)
        result = pub.publish(_request(video_file))

    assert result.success is False
    assert "no tweet id" in result.error
    assert result.url == ""


def test_x_refuses_a_media_id_of_the_literal_string_none(video_file):
    """`str(a or b)` yields "None" when both keys are absent.

    That "None" was then APPENDed and FINALIZEd against a media id that does not exist — the whole
    file uploaded before X says anything useful.
    """
    from publishers.x import XPublisher
    from tests.fakes import FakeHTTPClient, FakeResponse

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "x_access_token", "tok")
        mp.setattr(settings, "x_direct_post_approved", True)
        pub = XPublisher(
            client=FakeHTTPClient(lambda m, u, k: FakeResponse(json_data={})),
            sleep=lambda _s: None,
        )
        result = pub.publish(_request(video_file))

    assert result.success is False
    assert "no media id" in result.error


def test_youtube_refuses_to_claim_a_video_without_an_id(video_file):
    from publishers.youtube import YouTubePublisher
    from tests.fakes import FakeHTTPClient, FakeResponse

    def handler(method, url, kwargs):
        if "oauth2" in url:
            return FakeResponse(json_data={"access_token": "tok"})
        if url.endswith("/videos"):
            return FakeResponse(headers={"location": "https://upload/here"})
        return FakeResponse(json_data={})  # the PUT: 200, no id

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "youtube_client_id", "cid")
        mp.setattr(settings, "youtube_client_secret", "secret")
        mp.setattr(settings, "youtube_refresh_token", "refresh")
        result = YouTubePublisher(client=FakeHTTPClient(handler)).publish(_request(video_file))

    assert result.success is False
    assert "no video id" in result.error
    assert result.url == ""


def test_a_long_youtube_upload_is_not_labelled_a_short(video_file):
    """The URL was hard-coded to `/shorts/<id>` for every upload.

    `preflight.PLATFORM_LIMITS["youtube"]` deliberately permits 900 s and 16:9 landscape, so a
    five-minute landscape upload got a Shorts link that does not resolve to what was posted.
    """
    from publishers.youtube import YouTubePublisher
    from tests.fakes import FakeHTTPClient, FakeResponse

    def handler(method, url, kwargs):
        if "oauth2" in url:
            return FakeResponse(json_data={"access_token": "tok"})
        if url.endswith("/videos"):
            return FakeResponse(headers={"location": "https://upload/here"})
        return FakeResponse(json_data={"id": "vid123"})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "youtube_client_id", "cid")
        mp.setattr(settings, "youtube_client_secret", "secret")
        mp.setattr(settings, "youtube_refresh_token", "refresh")
        pub = YouTubePublisher(client=FakeHTTPClient(handler))
        long_clip = pub.publish(_request(video_file, metadata={"duration": 400.0}))
        short_clip = pub.publish(_request(video_file, metadata={"duration": 45.0}))

    assert "watch?v=vid123" in long_clip.url, "a 400s video was labelled a Short"
    # The common case is unchanged: this tool makes Shorts, and that stays the default.
    assert "shorts/vid123" in short_clip.url


# --------------------------------------------------------------------------- #
# 2. Double posting                                                             #
# --------------------------------------------------------------------------- #
def test_a_failure_after_the_post_may_exist_is_not_auto_retried(tmp_path, video_file):
    """A retry re-runs the whole multi-request flow, so it can post twice.

    X is initialize → append → finalize → tweet; Instagram is create-container → upload → publish;
    YouTube is initiate → PUT. A read timeout on the *last* call of an upload the platform actually
    accepted was classified transient, re-queued, and re-run from step one. There is no idempotency
    key anywhere to prevent the duplicate.

    The asymmetry is deliberate and worth stating: a duplicate post cannot be undone by this tool,
    whereas a post it declines to retry is one click away from being published.
    """
    from publishers.history import HistoryStore
    from publishers.manager import PublishManager

    store = HistoryStore(path=tmp_path / "history.db")
    # A real row, because asserting against an id that was never created makes the test vacuous:
    # `get_attempt` returns None and every assertion about the stored state passes regardless.
    attempt_id = store.create_attempt(
        job_id="j1",
        clip_id="c1",
        platform="x",
        request={},
        scheduled_at=time.time(),
        state=PublishState.QUEUED.value,
    )
    manager = PublishManager(publishers={}, history=store, autostart=False)
    result = PublishResult(
        False,
        PublishState.FAILED,
        "x",
        error="timed out",  # a transient error by every classifier
        side_effect_possible=True,
    )

    assert manager._schedule_retry({"id": attempt_id, "retry_count": 0}, None, result) is True

    stored = store.get_attempt(attempt_id)
    assert stored is not None
    # Routed to a person, not re-queued: the scheduler selects queued/scheduled, so anything else
    # means it will not run again on its own.
    assert stored["state"] == PublishState.REVIEW_REQUIRED.value, (
        f"a possibly-posted attempt was left in {stored['state']!r}, which the scheduler may re-run"
    )
    assert "post twice" in (stored.get("message") or ""), (
        "the record does not explain why this was not retried, so an operator cannot act on it"
    )


def test_a_failure_before_any_side_effect_is_still_retried(tmp_path):
    """The guard must not disable retrying, which is the whole point of `retry.py`."""
    from publishers.history import HistoryStore
    from publishers.manager import PublishManager

    store = HistoryStore(path=tmp_path / "history.db")
    attempt_id = store.create_attempt(
        job_id="j1",
        clip_id="c1",
        platform="x",
        request={},
        scheduled_at=time.time(),
        state=PublishState.QUEUED.value,
    )
    manager = PublishManager(publishers={}, history=store, autostart=False)
    result = PublishResult(False, PublishState.FAILED, "x", error="timed out")

    assert manager._schedule_retry({"id": attempt_id, "retry_count": 0}, None, result) is True
    assert store.get_attempt(attempt_id)["state"] == PublishState.SCHEDULED.value


def test_submitting_the_same_clip_twice_does_not_create_two_attempts(tmp_path, video_file):
    """`publish_attempts` has no uniqueness constraint, unlike `clips`.

    Two clicks of Publish — or an auto-publish racing a manual one — created two rows and therefore
    two posts.
    """
    from publishers.history import HistoryStore
    from publishers.manager import PublishManager
    from tests.fakes import FakePublisher

    store = HistoryStore(path=tmp_path / "history.db")
    manager = PublishManager(publishers={"x": FakePublisher("x")}, history=store, autostart=False)
    clip = SimpleNamespace(
        id="c1",
        title="t",
        description="d",
        hashtags=[],
        hook_text="",
        cta="",
        mentions=[],
    )
    first = manager.submit(job_id="j1", clip=clip, video_path=video_file, platforms=["x"])
    second = manager.submit(job_id="j1", clip=clip, video_path=video_file, platforms=["x"])

    assert first == second, "a repeated submission created a second attempt"


# --------------------------------------------------------------------------- #
# 3. Credential leakage                                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Client error '400' for url 'https://graph.facebook.com/v25.0/1?access_token=EAAGsecret'",
        "https://api.example.com/x?api_key=abcdef123456&other=1",
        "Authorization: Bearer ya29.averylongsecretvalue",
        "OAuth EAAGsomethinglong",
        "refresh_token=1//0gsecretvalue",
    ],
)
def test_credentials_are_stripped_from_recorded_errors(text):
    """Publishers record `str(exc)`, and httpx embeds the request URL in its message.

    Instagram passed its access token as a *query parameter*, so one 4xx on the container poll
    produced an error string containing the token — which `manager._execute` wrote into the `error`
    column and `result_json`, and which `GET /api/history` serves. A long-lived credential that
    only ever needed to be in a header ended up in the database, in every backup of it, and in the
    dashboard.
    """
    cleaned = redact(text)
    assert "REDACTED" in cleaned
    for secret in ("EAAGsecret", "abcdef123456", "ya29.averylongsecretvalue", "1//0gsecretvalue"):
        assert secret not in cleaned


def test_redaction_is_applied_at_the_result_boundary():
    """Applied on `PublishResult` itself, so no publisher can leak by forgetting to."""
    result = PublishResult(
        False,
        PublishState.FAILED,
        "instagram",
        error="failed for url 'https://g.fb.com/x?access_token=EAAGsecret'",
    )
    assert "EAAGsecret" not in result.error
    assert "EAAGsecret" not in json.dumps(result.to_dict())


def test_instagram_does_not_put_its_token_in_a_url(video_file):
    """The fix at source, not only the net: the poll uses a header."""
    from publishers.instagram import InstagramPublisher
    from tests.fakes import FakeHTTPClient, FakeResponse

    seen_urls: list[str] = []

    def handler(method, url, kwargs):
        seen_urls.append(url + json.dumps(kwargs.get("params") or {}))
        if url.endswith("/media"):
            return FakeResponse(json_data={"id": "cid1"})
        if "rupload" in url:
            return FakeResponse(json_data={"success": True})
        if "media_publish" in url:
            return FakeResponse(json_data={"id": "media1"})
        return FakeResponse(json_data={"status_code": "FINISHED"})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "instagram_access_token", "EAAGsecret")
        mp.setattr(settings, "instagram_account_id", "ig1")
        mp.setattr(settings, "instagram_content_publish_approved", True)
        pub = InstagramPublisher(client=FakeHTTPClient(handler), sleep=lambda _s: None)
        result = pub.publish(_request(video_file))

    assert result.success
    polls = [u for u in seen_urls if "status_code" in u or "fields" in u]
    assert polls, "the container status was never polled"
    for url in polls:
        assert "EAAGsecret" not in url, "the access token is still in the poll URL"


# --------------------------------------------------------------------------- #
# 4. Unreachable states                                                         #
# --------------------------------------------------------------------------- #
def test_an_abandoned_upload_is_reclaimed_as_unknown(tmp_path):
    """A stale `uploading` row was invisible to everything.

    `due_attempts` selects only queued/scheduled, and `/retry`, `/approve`, `/reschedule` and
    `/cancel` all refused `uploading`. So a process that died between the platform call and the
    write-back left an attempt that was silently lost *and* wrong in the audit trail — with no
    action available to anyone.

    `unknown` rather than `failed`, because a failed attempt is safe to retry automatically and
    this one is not: the post may be live.
    """
    from publishers.history import HistoryStore
    from publishers.manager import PublishManager

    store = HistoryStore(path=tmp_path / "history.db")
    attempt_id = store.create_attempt(
        job_id="j1",
        clip_id="c1",
        platform="x",
        request={},
        scheduled_at=time.time(),
        state=PublishState.QUEUED.value,
    )
    store.update_attempt(
        attempt_id, state=PublishState.UPLOADING.value, started_at=time.time() - 7200
    )

    manager = PublishManager(publishers={}, history=store, autostart=False)
    assert manager.reclaim_stale_uploads() == 1
    assert store.get_attempt(attempt_id)["state"] == PublishState.UNKNOWN.value


def test_an_upload_still_in_flight_is_left_alone(tmp_path):
    """The reaper must not reclassify a live upload out from under itself."""
    from publishers.history import HistoryStore
    from publishers.manager import PublishManager

    store = HistoryStore(path=tmp_path / "history.db")
    attempt_id = store.create_attempt(
        job_id="j1",
        clip_id="c1",
        platform="x",
        request={},
        scheduled_at=time.time(),
        state=PublishState.QUEUED.value,
    )
    store.update_attempt(attempt_id, state=PublishState.UPLOADING.value, started_at=time.time())

    manager = PublishManager(publishers={}, history=store, autostart=False)
    assert manager.reclaim_stale_uploads() == 0
    assert store.get_attempt(attempt_id)["state"] == PublishState.UPLOADING.value


def test_an_unknown_attempt_can_be_acted_on():
    """Otherwise `unknown` is just a differently-named zombie.

    Every endpoint refusing a state is precisely what made stale `uploading` unrecoverable, so the
    new state has to be both resumable and cancellable by a person.
    """
    from api.main import RESUMABLE_PUBLISH_STATES

    assert PublishState.UNKNOWN.value in RESUMABLE_PUBLISH_STATES


# --------------------------------------------------------------------------- #
# 5. Retry classification actually uses the status code                          #
# --------------------------------------------------------------------------- #
def test_the_manager_passes_the_status_code_to_the_classifier(tmp_path):
    """`retry.classify`'s precise path was dead code in production.

    It has always accepted a status code and decided purely from it when given one — but no
    publisher populated one and the manager called `should_retry(count, error)` with two arguments.
    So **every** production retry decision was made by substring-matching an exception message,
    which is the fallback `retry.py`'s own docstring apologises for. Its unit tests passed because
    they call `classify` directly.
    """
    from publishers import retry
    from publishers.history import HistoryStore
    from publishers.manager import PublishManager

    store = HistoryStore(path=tmp_path / "history.db")
    attempt_id = store.create_attempt(
        job_id="j1",
        clip_id="c1",
        platform="x",
        request={},
        scheduled_at=time.time(),
        state=PublishState.QUEUED.value,
    )
    manager = PublishManager(publishers={}, history=store, autostart=False)

    seen: list[tuple] = []
    original = retry.should_retry

    def spy(count, error, status_code=None):
        seen.append((count, error, status_code))
        return original(count, error, status_code)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(retry, "should_retry", spy)
        # A 503 with text that says nothing: only the code can classify this correctly.
        manager._schedule_retry(
            {"id": attempt_id, "retry_count": 0},
            None,
            PublishResult(
                False, PublishState.FAILED, "x", error="upstream said no", status_code=503
            ),
        )

    assert seen, "should_retry was never called"
    assert seen[0][2] == 503, f"the status code was not passed: {seen[0]!r}"


def test_a_status_code_is_captured_from_an_http_error():
    """The code was on the exception object all along and nobody read it."""
    from publishers.base import status_of

    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(429, request=request)
    exc = httpx.HTTPStatusError("rate limited", request=request, response=response)
    assert status_of(exc) == 429
    assert status_of(RuntimeError("no http here")) is None


# --------------------------------------------------------------------------- #
# 6. Instagram readiness                                                        #
# --------------------------------------------------------------------------- #
def test_instagram_does_not_publish_an_unready_container(video_file):
    """The loop fell through to `media_publish` after ~5 seconds regardless.

    Reel transcoding routinely takes longer, so the common outcome was a Graph 4xx *after* the whole
    file had been uploaded — and "not ready" matches no transient pattern, so it was classified
    permanent and a perfectly good container was abandoned. The container is valid for 24 hours, so
    the honest result is a resumable DRAFT carrying its id.
    """
    from publishers.instagram import InstagramPublisher
    from tests.fakes import FakeHTTPClient, FakeResponse

    published_calls: list[str] = []

    def handler(method, url, kwargs):
        if url.endswith("/media"):
            return FakeResponse(json_data={"id": "cid1"})
        if "rupload" in url:
            return FakeResponse(json_data={"success": True})
        if "media_publish" in url:
            published_calls.append(url)
            return FakeResponse(json_data={"id": "should-not-happen"})
        return FakeResponse(json_data={"status_code": "IN_PROGRESS"})  # never ready

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "instagram_access_token", "tok")
        mp.setattr(settings, "instagram_account_id", "ig1")
        mp.setattr(settings, "instagram_content_publish_approved", True)
        pub = InstagramPublisher(client=FakeHTTPClient(handler), sleep=lambda _s: None)
        pub.readiness_attempts = 3
        pub.readiness_interval_seconds = 0.0
        result = pub.publish(_request(video_file))

    assert not published_calls, "published a container Instagram had not finished processing"
    assert result.success is True
    assert result.state == PublishState.DRAFT
    assert result.external_id == "cid1", (
        "the container id must survive so approving costs no re-upload"
    )


# --------------------------------------------------------------------------- #
# 7. Scheduling timezone                                                        #
# --------------------------------------------------------------------------- #
def test_suggested_times_honour_the_configured_timezone(monkeypatch):
    """The module's premise is that "post at 7pm" means 7pm where the audience is.

    The implementation built a **naive** datetime, so `timestamp()` interpreted it in the server's
    zone — UTC in the container. "19:00 TikTok prime time" therefore scheduled 2 p.m. in New York.
    The docstring described a feature the code did not have.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from publishers import best_times

    monkeypatch.setattr(settings, "schedule_timezone", "America/New_York")
    # A fixed reference so the assertion does not depend on when the suite runs.
    reference = datetime(2026, 6, 1, 8, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
    suggestions = best_times.suggest("tiktok", now=reference, days=1, per_day=1)

    assert suggestions, "no suggestion produced"
    local = datetime.fromtimestamp(suggestions[0].at, tz=ZoneInfo("America/New_York"))
    assert (local.hour, local.minute) == (19, 0), (
        f"19:00 New York was scheduled for {local.hour}:{local.minute:02d} New York"
    )


def test_an_unknown_timezone_falls_back_rather_than_failing(monkeypatch):
    """A misconfiguration must not break scheduling."""
    from publishers import best_times

    monkeypatch.setattr(settings, "schedule_timezone", "Mars/Olympus_Mons")
    assert best_times.suggest("tiktok", now=time.time(), days=1, per_day=1)
