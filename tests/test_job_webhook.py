"""The job completion webhook (Phase 7).

The contract that matters most is negative: **this may never fail a job.** It fires in the
``finally`` of the job body, so anything it raises replaces the render's real outcome with a
delivery error - a successful ten-clip render reported as failed because a notifier was
unreachable. So roughly half of these tests break the sender on purpose and assert the job is
unaffected.

The second theme is the signature. A receiver verifies the HMAC against the **raw bytes it
received**, so if the body is re-serialised anywhere between signing and sending, every check
fails - and it fails at the receiver, in someone else's logs, which is the worst place to
diagnose it from.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from config import settings
from worker import webhook
from worker.models import Job, JobStatus, ProcessingOptions


class _Recorder:
    """A stand-in httpx client that records what it was handed."""

    def __init__(self, status_code: int = 200, text: str = "ok", raises: Exception | None = None):
        self.status_code = status_code
        self.text = text
        self.raises = raises
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url, content=None, headers=None):
        self.calls.append({"url": url, "content": content, "headers": dict(headers or {})})
        if self.raises is not None:
            raise self.raises
        return self

    def close(self):
        self.closed = True


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "job_webhook_url", "https://hooks.example/clip", raising=False)
    monkeypatch.setattr(settings, "job_webhook_secret", None, raising=False)
    monkeypatch.setattr(settings, "job_webhook_timeout_seconds", 5.0, raising=False)
    monkeypatch.setattr(settings, "job_webhook_events", "completed,failed,cancelled", raising=False)


def _job(status=JobStatus.COMPLETED, **kwargs):
    job = Job(input_type="url", source="https://example.com/v", options=ProcessingOptions())
    job.status = status
    for key, value in kwargs.items():
        setattr(job, key, value)
    return job


# --------------------------------------------------------------------------- #
# Off by default
# --------------------------------------------------------------------------- #
def test_no_url_means_no_request(monkeypatch):
    """Unset must cost nothing - not a request to nowhere, not an exception."""
    monkeypatch.setattr(settings, "job_webhook_url", None, raising=False)
    client = _Recorder()
    assert webhook.notify(_job(), client=client) is False
    assert client.calls == []


def test_a_blank_url_is_treated_as_unset(monkeypatch):
    monkeypatch.setattr(settings, "job_webhook_url", "   ", raising=False)
    client = _Recorder()
    assert webhook.notify(_job(), client=client) is False
    assert client.calls == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/1", "ftp://h/f", "/no/scheme"])
def test_a_non_http_url_is_refused_and_says_so(monkeypatch, caplog, url):
    """A typo guard, not a security boundary - but it must be loud rather than silent.

    An operator who mistyped the scheme should learn it from a log line naming the setting, not
    by wondering why nothing ever arrives.
    """
    import logging

    monkeypatch.setattr(settings, "job_webhook_url", url, raising=False)
    client = _Recorder()
    with caplog.at_level(logging.WARNING):
        assert webhook.notify(_job(), client=client) is False
    assert client.calls == []
    assert "JOB_WEBHOOK_URL" in " ".join(r.getMessage() for r in caplog.records)


def test_a_url_with_no_host_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "job_webhook_url", "https://", raising=False)
    assert webhook.notify(_job(), client=_Recorder()) is False


def test_a_private_url_is_allowed(monkeypatch):
    """The deliberate difference from URL ingest.

    `validate_public_url` exists because *callers* supply ingest URLs. This one comes from the
    operator's own environment, and the common target is a service on the same host. Refusing it
    would break the main use case to protect someone from their own machine.
    """
    monkeypatch.setattr(settings, "job_webhook_url", "http://localhost:5678/hook", raising=False)
    monkeypatch.setattr(settings, "job_webhook_secret", None, raising=False)
    monkeypatch.setattr(settings, "job_webhook_timeout_seconds", 5.0, raising=False)
    monkeypatch.setattr(settings, "job_webhook_events", "completed", raising=False)
    client = _Recorder()
    assert webhook.notify(_job(), client=client) is True
    assert client.calls[0]["url"] == "http://localhost:5678/hook"


# --------------------------------------------------------------------------- #
# Event filtering
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
def test_every_terminal_status_is_delivered_by_default(enabled, status):
    client = _Recorder()
    assert webhook.notify(_job(status), client=client) is True
    assert json.loads(client.calls[0]["content"])["status"] == status.value


def test_only_the_configured_events_are_delivered(monkeypatch, enabled):
    """An operator who only wants to hear about failures is the common configuration."""
    monkeypatch.setattr(settings, "job_webhook_events", "failed", raising=False)
    client = _Recorder()
    assert webhook.notify(_job(JobStatus.COMPLETED), client=client) is False
    assert webhook.notify(_job(JobStatus.FAILED), client=client) is True
    assert len(client.calls) == 1


def test_the_event_list_tolerates_spacing_and_case(monkeypatch, enabled):
    monkeypatch.setattr(settings, "job_webhook_events", " Failed , COMPLETED ", raising=False)
    assert webhook.configured_events() == {"failed", "completed"}


def test_an_empty_event_list_sends_nothing(monkeypatch, enabled):
    """Explicitly emptied is a way to switch it off without losing the URL."""
    monkeypatch.setattr(settings, "job_webhook_events", "", raising=False)
    client = _Recorder()
    assert webhook.notify(_job(), client=client) is False
    assert client.calls == []


def test_a_non_terminal_status_is_not_delivered(enabled):
    """`cancelling` is not a terminal state; the job still holds the worker.

    It cannot reach `notify` from the pipeline, because the `finally` runs after the handler has
    already written a terminal status - but the filter must not be the only thing preventing it.
    """
    client = _Recorder()
    assert webhook.notify(_job(JobStatus.CANCELLING), client=client) is False


# --------------------------------------------------------------------------- #
# The payload
# --------------------------------------------------------------------------- #
def test_the_payload_summarises_the_job(enabled):
    from worker.models import ClipResult

    clip = ClipResult(
        id="c1",
        filename="clip_01.mp4",
        start=0.0,
        end=5.0,
        duration=5.0,
        title="A clip",
        video_url="/clips/j/clip_01.mp4",
        thumbnail_url="",
    )
    clip.effects_applied = ["captions", "music_degraded:synthesised"]
    job = _job(JobStatus.COMPLETED, clips=[clip], title="Source", duration=61.5)

    client = _Recorder()
    assert webhook.notify(job, client=client) is True
    body = json.loads(client.calls[0]["content"])

    assert body["event"] == "job.completed"
    assert body["job_id"] == job.id
    assert body["clip_count"] == 1
    assert body["clips"][0]["filename"] == "clip_01.mp4"
    assert body["clips"][0]["video_url"] == "/clips/j/clip_01.mp4"
    # The degradation contract, where an integration can act on it.
    assert "music_degraded:synthesised" in body["clips"][0]["effects_applied"]
    assert body["duration"] == 61.5


def test_the_payload_omits_the_transcript(enabled):
    """Not `job.to_dict()`.

    That carries every clip's full transcript and the ~100-field options object - hundreds of
    kilobytes on a ten-clip job, which a receiver must accept before it can decide it does not
    care. The job id is the key to every detail endpoint if more is wanted.
    """
    from worker.models import ClipResult

    clip = ClipResult(
        id="c1",
        filename="clip_01.mp4",
        start=0.0,
        end=5.0,
        duration=5.0,
        title="A clip",
        video_url="/clips/j/clip_01.mp4",
        thumbnail_url="",
    )
    clip.transcript_text = "a very long transcript " * 500
    client = _Recorder()
    webhook.notify(_job(clips=[clip]), client=client)
    raw = client.calls[0]["content"].decode("utf-8")
    assert "very long transcript" not in raw
    assert "options" not in json.loads(raw)


def test_the_error_key_is_present_and_null_on_success(enabled):
    """A receiver logging one line reads it unconditionally; a missing key would raise there."""
    client = _Recorder()
    webhook.notify(_job(JobStatus.COMPLETED), client=client)
    body = json.loads(client.calls[0]["content"])
    assert "error" in body
    assert body["error"] is None


def test_a_failure_carries_its_reason(enabled):
    client = _Recorder()
    webhook.notify(_job(JobStatus.FAILED, error="ffmpeg exploded"), client=client)
    assert json.loads(client.calls[0]["content"])["error"] == "ffmpeg exploded"


def test_the_payload_carries_the_llm_usage(enabled):
    """Phase 7's other half, so an integration can total spend without a second request."""
    job = _job(llm_usage={"calls": 3, "total_tokens": 900, "cost_usd": None, "priced": False})
    client = _Recorder()
    webhook.notify(job, client=client)
    assert json.loads(client.calls[0]["content"])["llm_usage"]["total_tokens"] == 900


def test_the_event_header_lets_a_receiver_route_without_parsing(enabled):
    client = _Recorder()
    webhook.notify(_job(JobStatus.FAILED), client=client)
    assert client.calls[0]["headers"][webhook.EVENT_HEADER] == "job.failed"


def test_the_body_is_valid_json_with_a_json_content_type(enabled):
    client = _Recorder()
    webhook.notify(_job(), client=client)
    assert client.calls[0]["headers"]["Content-Type"] == "application/json"
    json.loads(client.calls[0]["content"])  # raises if it is not


# --------------------------------------------------------------------------- #
# The signature
# --------------------------------------------------------------------------- #
def test_no_secret_means_no_signature_header(enabled):
    client = _Recorder()
    webhook.notify(_job(), client=client)
    assert webhook.SIGNATURE_HEADER not in client.calls[0]["headers"]


def test_the_signature_verifies_against_the_bytes_that_were_sent(monkeypatch, enabled):
    """The property a receiver actually checks.

    A receiver hashes the raw body it received. If anything between signing and sending
    re-serialises the payload - a different key order, a space after a comma - every signature
    fails, and it fails in someone else's logs.
    """
    monkeypatch.setattr(settings, "job_webhook_secret", "s3cret", raising=False)
    client = _Recorder()
    assert webhook.notify(_job(), client=client) is True

    sent = client.calls[0]
    expected = hmac.new(b"s3cret", sent["content"], hashlib.sha256).hexdigest()
    assert sent["headers"][webhook.SIGNATURE_HEADER] == f"sha256={expected}"


def test_the_signature_is_algorithm_prefixed(monkeypatch, enabled):
    """`sha256=` matches GitHub, Stripe and Shopify, so a receiver written against any of their
    examples works unchanged - and an unprefixed hex string could never be migrated."""
    monkeypatch.setattr(settings, "job_webhook_secret", "s3cret", raising=False)
    client = _Recorder()
    webhook.notify(_job(), client=client)
    assert client.calls[0]["headers"][webhook.SIGNATURE_HEADER].startswith("sha256=")


def test_a_different_secret_produces_a_different_signature():
    body = b'{"a":1}'
    assert webhook.sign(body, "one") != webhook.sign(body, "two")


def test_a_changed_body_produces_a_different_signature():
    assert webhook.sign(b'{"a":1}', "k") != webhook.sign(b'{"a":2}', "k")


def test_the_body_is_deterministic(enabled):
    """Two deliveries of the same job must produce identical bytes.

    Not cosmetic: without it a receiver cannot cache or de-duplicate on a body hash, and a
    signature computed twice over the same payload would differ.
    """
    job = _job()
    job.updated_at = 123.0
    first, second = _Recorder(), _Recorder()
    webhook.notify(job, client=first)
    webhook.notify(job, client=second)
    a = json.loads(first.calls[0]["content"])
    b = json.loads(second.calls[0]["content"])
    a.pop("sent_at")
    b.pop("sent_at")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# --------------------------------------------------------------------------- #
# It may never fail a job
# --------------------------------------------------------------------------- #
def test_a_connection_error_is_swallowed(enabled, caplog):
    import logging

    client = _Recorder(raises=OSError("connection refused"))
    with caplog.at_level(logging.WARNING):
        assert webhook.notify(_job(), client=client) is False
    assert "could not be delivered" in " ".join(r.getMessage() for r in caplog.records)


def test_a_non_2xx_response_is_logged_not_raised(enabled, caplog):
    import logging

    client = _Recorder(status_code=500, text="upstream boom")
    with caplog.at_level(logging.WARNING):
        assert webhook.notify(_job(), client=client) is False
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "rejected with 500" in messages
    # The receiver's own message is usually the only thing that says why.
    assert "upstream boom" in messages


@pytest.mark.parametrize("code", [200, 201, 202, 204, 299])
def test_any_2xx_counts_as_delivered(enabled, code):
    assert webhook.notify(_job(), client=_Recorder(status_code=code)) is True


@pytest.mark.parametrize("code", [301, 400, 401, 404, 500, 503])
def test_a_non_2xx_does_not(enabled, code):
    assert webhook.notify(_job(), client=_Recorder(status_code=code)) is False


def test_a_job_missing_every_attribute_does_not_raise(enabled):
    """`notify` is handed whatever the store held. It must not be the thing that breaks."""

    class Barely:
        id = "x"
        status = JobStatus.COMPLETED

    assert webhook.notify(Barely(), client=_Recorder()) in (True, False)


def test_a_hostile_job_object_does_not_raise(enabled):
    class Hostile:
        id = "x"

        @property
        def status(self):
            raise RuntimeError("boom")

    assert webhook.notify(Hostile(), client=_Recorder()) is False


def test_a_broken_settings_value_does_not_raise(monkeypatch, enabled):
    monkeypatch.setattr(settings, "job_webhook_timeout_seconds", "not-a-number", raising=False)
    assert webhook.notify(_job(), client=_Recorder()) is False


# --------------------------------------------------------------------------- #
# Wiring: fired once, from the one place every terminal path reaches
# --------------------------------------------------------------------------- #
def test_it_is_fired_from_the_single_finally():
    """Hooking the three `store.update` sites instead would be three places to keep in step,
    and a fourth outcome added later would silently notify nobody."""
    import inspect

    from worker import jobs as jobs_module

    source = inspect.getsource(jobs_module.JobManager._execute)
    assert source.count("webhook.notify(") == 1
    # In the `finally`, after the terminal status has been written.
    finally_block = source.split("finally:")[-1]
    assert "webhook.notify(" in finally_block


def test_it_re_reads_the_job_rather_than_using_the_stale_local():
    """The `job` local was captured before the run: its status, clips and timings are all stale
    by the time the `finally` runs, so notifying with it would report a queued job with no clips."""
    import inspect

    from worker import jobs as jobs_module

    source = inspect.getsource(jobs_module.JobManager._execute)
    finally_block = source.split("finally:")[-1]
    assert "self.store.get(job_id)" in finally_block
    assert "webhook.notify(job)" not in source


def _drive_to_failure(manager, tmp_path):
    """Run `_execute` to its failure path with no network and no ffmpeg.

    A ``file`` job whose source does not exist raises ``FileNotFoundError`` before any work
    starts, which is the cheapest way to reach a terminal state through the real code.

    A ``url`` job would have been the obvious fixture and is a trap: ``_execute`` hands the
    source to yt-dlp, so the first draft of this test *actually fetched example.com*. This repo
    forbids that - a test needing the public internet fails for reasons unrelated to the
    repository - and it was only visible because the 404 traceback appeared in the output.
    """
    from worker import observability
    from worker.models import Job, ProcessingOptions

    job = Job(
        input_type="file",
        source=str(tmp_path / "does-not-exist.mp4"),
        options=ProcessingOptions(),
    )
    job.status = JobStatus.QUEUED
    manager.store.add(job)
    manager._execute(
        job,
        job.id,
        lambda *a: None,
        lambda: None,
        observability.metrics_for(job.id),
    )
    return job


def test_a_real_run_notifies_once_with_the_final_state(monkeypatch, tmp_path, enabled):
    """End to end through `_execute`.

    The assertion that matters is that the payload carries the *terminal* status, because that is
    exactly what notifying with the stale local would have got wrong - it would have reported a
    queued job with no clips.
    """
    from worker import jobs as jobs_module
    from worker.jobs import JobManager, JobStore

    sent: list[dict] = []
    monkeypatch.setattr(
        jobs_module.webhook, "notify", lambda job, **kw: sent.append(webhook.build_payload(job))
    )

    manager = JobManager(store=JobStore(persistence=False))
    _drive_to_failure(manager, tmp_path)

    assert len(sent) == 1
    assert sent[0]["status"] == "failed"
    assert sent[0]["event"] == "job.failed"
    assert "does-not-exist" in (sent[0]["error"] or "")


def test_a_webhook_that_raises_cannot_fail_the_job(monkeypatch, tmp_path):
    """The contract. `notify` is total, but the call site must not depend on that staying true
    forever - so this asserts the job's recorded outcome survives a sender that explodes."""
    from worker import jobs as jobs_module
    from worker.jobs import JobManager, JobStore

    def _explode(*args, **kwargs):
        raise RuntimeError("notifier exploded")

    monkeypatch.setattr(jobs_module.webhook, "notify", _explode)

    manager = JobManager(store=JobStore(persistence=False))
    with pytest.raises(RuntimeError, match="notifier exploded"):
        _drive_to_failure(manager, tmp_path)

    # The job's real outcome was recorded before the notifier ran, so the render's result
    # survived - which is what the `finally` ordering buys.
    job = manager.store.all()[0]
    assert job.status is JobStatus.FAILED
    assert "does-not-exist" in (job.error or "")


def test_the_real_notify_swallowing_means_the_job_completes_normally(
    monkeypatch, tmp_path, enabled
):
    """The same scenario through the *real* `notify`, with an exploding client.

    The test above pins the call-site ordering by forcing a raise past it. This one pins what
    actually ships: a dead receiver leaves the job's outcome untouched and raises nothing.
    """
    from worker.jobs import JobManager, JobStore

    monkeypatch.setattr(settings, "job_webhook_url", "http://127.0.0.1:1/hook", raising=False)
    manager = JobManager(store=JobStore(persistence=False))
    _drive_to_failure(manager, tmp_path)

    job = manager.store.all()[0]
    assert job.status is JobStatus.FAILED
    assert "does-not-exist" in (job.error or "")
