"""``GET /metrics`` exposes aggregates across jobs, correctly and safely (Phase 7).

``worker/observability.py`` timed every pipeline stage and then dropped the numbers into a log
line and one job's ``/timings`` response. Both answer a question about a single job, so "is p95
render time regressing?", "has the failure rate moved since the last release?" and "how often are
fonts being substituted?" had no answer that did not involve reading every job record by hand.

Four things are asserted here, and the first two are the ones most likely to be quietly wrong:

1. **The exposition text is parseable.** The format's failure mode is total, not partial: a stray
   ``"`` in a label value produces a line Prometheus cannot parse and it rejects the *whole
   scrape*, so one bad marker name silently loses every metric in the response. Escaping, family
   contiguity and the ``+Inf`` bucket are pinned individually.
2. **Counters are monotonic.** They are accumulated in-process rather than derived from
   ``JobStore``, because the store is pruned to ``max_persisted_jobs`` - a "total" read from it
   goes *down*, Prometheus reads a decreasing counter as a restart, and ``rate()`` then reports a
   burst of traffic that never happened. A plausible, fabricated number.
3. **It is authenticated.** Not in ``_EXEMPT_PATHS`` and not in ``_QUERY_TOKEN_PATHS``: an open
   ``/metrics`` publishes job volume, failure rate and token spend to anyone who finds the port.
4. **Cardinality is bounded**, so a marker vocabulary that grows cannot turn this endpoint into
   the thing that degrades the monitoring system.
"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from api import security
from api.main import app
from api.routers import metrics as endpoint
from config import settings
from worker import llm_cost, metrics, observability, webhook
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions

TOKEN = "metrics-secret"


@pytest.fixture(autouse=True)
def _clean_registry():
    """A fresh registry per test.

    The registry is process-global by design - that is what makes a counter monotonic across
    requests - which also means one test's observations leak into the next. Without this, an
    assertion about a token total fails because an unrelated test happened to run first, and the
    failure points at the wrong code.
    """
    metrics.reset()
    security.limiter.reset()
    yield
    metrics.reset()
    security.limiter.reset()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", None)
    with TestClient(app) as test_client:
        yield test_client


def _scrape(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.text


def _samples(text: str, name: str) -> dict[str, float]:
    """``{label_text: value}`` for one metric name, ignoring ``#`` lines."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        head, _, value = line.rpartition(" ")
        metric, _, labels = head.partition("{")
        if metric != name:
            continue
        out[labels.rstrip("}")] = float(value)
    return out


# --------------------------------------------------------------------------- #
# The format                                                                    #
# --------------------------------------------------------------------------- #
def test_content_type_declares_the_exposition_version(client):
    """Prometheus selects a parser from the version parameter.

    It works today without one because 0.0.4 is the default, which is exactly why omitting it is
    the kind of thing that breaks on somebody else's upgrade rather than in this repo.
    """
    response = client.get("/metrics")
    assert response.headers["content-type"] == endpoint.CONTENT_TYPE
    assert "version=0.0.4" in response.headers["content-type"]


def test_every_sample_line_is_preceded_by_its_type(client):
    """A family's ``# TYPE`` must appear before its samples, and its samples must be contiguous.

    Interleaving two families is a parse error rather than a cosmetic problem, so this pins the
    structural property rather than the exact bytes - which would make the test a change detector.
    """
    metrics.observe_stage("render", 1.0)
    metrics.count_job_finished("completed")
    text = _scrape(client)

    declared: set[str] = set()
    seen_families: list[str] = []
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            declared.add(line.split()[2])
            continue
        if line.startswith("#") or not line.strip():
            continue
        metric = line.partition("{")[0].partition(" ")[0]
        # A histogram's samples are suffixed; the declared family name is the stem.
        family = metric
        for suffix in ("_bucket", "_sum", "_count"):
            if metric.endswith(suffix):
                family = metric[: -len(suffix)]
                break
        assert family in declared, f"{metric} appears before its # TYPE"
        if not seen_families or seen_families[-1] != family:
            assert family not in seen_families, f"{family} samples are not contiguous"
            seen_families.append(family)

    assert text.endswith("\n"), "the body must end with a newline"


def test_body_has_no_blank_lines_between_samples(client):
    """A blank line mid-body is tolerated by some parsers and not by others."""
    metrics.count_job_finished("failed")
    body = _scrape(client)
    assert "\n\n" not in body


# --------------------------------------------------------------------------- #
# Escaping - the failure that loses every metric, not just one                   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('has"quote', 'has\\"quote'),
        ("has\\backslash", "has\\\\backslash"),
        ("has\nnewline", "has\\nnewline"),
        ('all\\three"and\nmore', 'all\\\\three\\"and\\nmore'),
        ("plain", "plain"),
    ],
)
def test_label_values_are_escaped(raw, expected):
    assert endpoint.escape_label_value(raw) == expected


def test_backslash_is_escaped_before_the_characters_added_after_it():
    r"""Order matters: escaping quotes first would turn ``"`` into ``\"`` and then into ``\\"``.

    That produces an escaped backslash followed by a *bare* quote - which terminates the label
    value early and makes the line unparseable, the exact failure escaping exists to prevent.
    """
    assert endpoint.escape_label_value('"') == '\\"'
    assert endpoint.escape_label_value("\\") == "\\\\"


def test_a_hostile_label_value_does_not_break_the_scrape(client):
    """An unescaped quote would make Prometheus discard the entire response."""
    metrics.count_job_finished('completed" evil="yes')
    text = _scrape(client)
    line = next(line for line in text.splitlines() if line.startswith(metrics.JOBS_FINISHED + "{"))
    assert line.count('"') % 2 == 0
    assert '\\"' in line
    # And the rest of the document survived.
    assert "clipping_jobs_tracked" in text


# --------------------------------------------------------------------------- #
# Histogram                                                                     #
# --------------------------------------------------------------------------- #
def test_buckets_are_cumulative():
    """Each ``le`` counts every observation at or below it, not just those in its own band."""
    histogram = metrics.Histogram((1.0, 2.0, 3.0))
    for value in (0.5, 1.5, 2.5):
        histogram.observe(value)
    assert histogram.cumulative() == [(1.0, 1), (2.0, 2), (3.0, 3)]


def test_observations_above_the_last_edge_reach_inf_only():
    """The top finite bucket must not absorb them, and they must still be counted.

    Dropping them would silently understate both the count and the sum - and a p95 computed from
    a histogram missing its slowest observations is precisely backwards.
    """
    histogram = metrics.Histogram((1.0, 2.0))
    histogram.observe(0.5)
    histogram.observe(99.0)
    assert histogram.cumulative() == [(1.0, 1), (2.0, 1)]
    assert histogram.total == 2
    assert histogram.sum == pytest.approx(99.5)


def test_inf_bucket_is_emitted_and_equals_the_count(client):
    metrics.observe_stage("transcribe", 0.1)
    metrics.observe_stage("transcribe", 10_000.0)
    text = _scrape(client)
    buckets = _samples(text, metrics.STAGE_DURATION + "_bucket")
    counts = _samples(text, metrics.STAGE_DURATION + "_count")
    inf = next(value for labels, value in buckets.items() if 'le="+Inf"' in labels)
    assert inf == 2
    assert inf == next(iter(counts.values()))
    top = max(metrics.STAGE_DURATION_BUCKETS)
    finite = next(value for labels, value in buckets.items() if f'le="{int(top)}"' in labels)
    assert finite == 1, "the largest finite bucket must not absorb the outlier"


def test_sum_is_the_total_time_not_the_bucket_midpoints(client):
    metrics.observe_stage("render", 2.0)
    metrics.observe_stage("render", 3.0)
    total = _samples(_scrape(client), metrics.STAGE_DURATION + "_sum")
    assert next(iter(total.values())) == pytest.approx(5.0)


def test_a_negative_duration_is_clamped_not_dropped():
    """A clock adjustment mid-stage can produce one; losing it would understate the count too."""
    histogram = metrics.Histogram((1.0,))
    histogram.observe(-5.0)
    assert histogram.total == 1
    assert histogram.sum == 0.0


def test_buckets_span_the_range_this_pipeline_actually_produces():
    """Sub-second (a fallback metadata call) through minutes (transcription, three re-encodes).

    Pinned because buckets copied from a template are the usual reason a histogram cannot resolve
    the latency that matters: every observation lands in one bucket and p95 becomes a guess.
    """
    assert min(metrics.STAGE_DURATION_BUCKETS) <= 0.05
    assert max(metrics.STAGE_DURATION_BUCKETS) >= 600
    assert list(metrics.STAGE_DURATION_BUCKETS) == sorted(metrics.STAGE_DURATION_BUCKETS)
    assert len(set(metrics.STAGE_DURATION_BUCKETS)) == len(metrics.STAGE_DURATION_BUCKETS)


# --------------------------------------------------------------------------- #
# Counters are monotonic - the reason they are not derived from the store        #
# --------------------------------------------------------------------------- #
def test_a_counter_never_decreases_when_the_store_is_pruned(client, monkeypatch):
    """The central design decision, pinned.

    ``JobStore`` is a rolling window. A "jobs completed" figure computed from it falls when the
    oldest record is evicted; Prometheus interprets a fall as a process restart and assumes the
    series resumed from zero, so a prune is read as a reset and ``rate()`` invents traffic. Here
    the store empties completely and the counter is unmoved.
    """
    metrics.count_job_finished("completed")
    metrics.count_job_finished("completed")

    full = _samples(_scrape(client), metrics.JOBS_FINISHED)['status="completed"']
    assert full == 2

    monkeypatch.setattr(endpoint, "get_manager", lambda: _Manager([]))
    pruned = _samples(_scrape(client), metrics.JOBS_FINISHED)['status="completed"']
    assert pruned == 2, "a counter must survive the store being pruned"


def test_increment_refuses_a_negative_delta():
    """Accepting one would emit a series Prometheus reads as a restart."""
    metrics.count_clips_rendered(3)
    metrics.count_clips_rendered(-99)
    assert metrics.counters()[metrics.CLIPS_RENDERED][()] == 3


def test_zero_clips_records_nothing(client):
    """A job that produced no clips must not create a series claiming it produced zero."""
    metrics.count_clips_rendered(0)
    assert metrics.CLIPS_RENDERED not in metrics.counters()


def test_label_order_does_not_split_a_series():
    """The same event counted from two call sites written in a different order is one series.

    Otherwise the total silently halves and both halves look like plausible traffic.
    """
    metrics.count_llm_call("m", 10, 5)
    series = metrics.counters()[metrics.LLM_TOKENS]
    assert set(series) == {
        (("kind", "prompt"), ("model", "m")),
        (("kind", "completion"), ("model", "m")),
    }


# --------------------------------------------------------------------------- #
# Number formatting                                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [(3.0, "3"), (0, "0"), (0.5, "0.5"), (1e-6, "1e-06")],
)
def test_integral_values_render_without_a_trailing_zero(value, expected):
    assert endpoint._format_number(value) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_are_not_exposed(value, caplog):
    """``NaN`` is legal in the format and unusable in a query; ``Inf`` poisons any aggregation.

    Reported as zero *and* logged, so the fault is discoverable without making the series
    unreadable for every other consumer of the same dashboard.
    """
    assert endpoint._format_number(value) == "0"
    assert not math.isnan(float(endpoint._format_number(value)))


# --------------------------------------------------------------------------- #
# Degradation markers                                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("captions", "captions"),
        ("music_degraded:synthesised", "music_degraded"),
        ("font_substituted:Anton", "font_substituted"),
        ("broll:a sunset over water", "broll"),
        ("encoder_unavailable:h264_nvenc", "encoder_unavailable"),
        ("a:b:c", "a"),
        ("", "unknown"),
        ("  spaced  ", "spaced"),
    ],
)
def test_only_the_marker_prefix_becomes_a_label(marker, expected):
    """``broll:<keyword>`` is unbounded by construction.

    Keeping the detail would add a time series per keyword any clip ever matched. The prefix is
    what a dashboard alerts on - "are fonts being substituted" - and the specific face is in the
    clip record and the logs.
    """
    assert metrics.marker_name(marker) == expected


def test_every_marker_on_a_clip_is_counted_not_only_the_failures(client):
    """``captions`` and ``music_degraded:synthesised`` are the same kind of fact.

    Deciding here which markers are interesting would bake today's opinion into the data; a query
    can filter, but a metric never recorded cannot be recovered.
    """
    metrics.count_degradations(["captions", "music_degraded:synthesised", "captions"])
    series = _samples(_scrape(client), metrics.CLIP_DEGRADATIONS)
    assert series['marker="captions"'] == 2
    assert series['marker="music_degraded"'] == 1


def test_marker_cardinality_is_capped_and_folded_into_other():
    """A vocabulary that grows must lose a distinction, not bound the scrape's size.

    Folded rather than dropped: losing which rare marker fired is recoverable from the logs,
    whereas discarding the observation makes the total wrong, and a wrong total is what gets
    acted on.
    """
    for index in range(200):
        metrics.count_degradations([f"marker_{index}"])
    series = metrics.counters()[metrics.CLIP_DEGRADATIONS]
    assert len(series) <= 65, "one slot per capped value plus the overflow bucket"
    assert (("overflow", "other"),) in series
    assert sum(series.values()) == 200, "no observation was lost"


# --------------------------------------------------------------------------- #
# Gauges read live, because current state legitimately falls                     #
# --------------------------------------------------------------------------- #
class _Manager:
    def __init__(self, jobs):
        self.store = _Store(jobs)


class _Store:
    def __init__(self, jobs):
        self._jobs = jobs

    def all(self):
        return list(self._jobs)


def _job(status):
    job = Job(input_type="url", source="https://example.com/v", options=ProcessingOptions())
    job.status = status
    return job


def test_gauges_reflect_the_store(client, monkeypatch):
    jobs = [
        _job(JobStatus.QUEUED),
        _job(JobStatus.QUEUED),
        _job(JobStatus.PROCESSING),
        _job(JobStatus.CANCELLING),
        _job(JobStatus.COMPLETED),
        _job(JobStatus.FAILED),
    ]
    monkeypatch.setattr(endpoint, "get_manager", lambda: _Manager(jobs))
    text = _scrape(client)

    by_status = _samples(text, "clipping_jobs")
    assert by_status['status="queued"'] == 2
    assert by_status['status="completed"'] == 1
    assert _samples(text, "clipping_jobs_queued")[""] == 2
    assert _samples(text, "clipping_jobs_tracked")[""] == 6
    # queued + processing + cancelling: everything holding or waiting for the worker.
    assert _samples(text, "clipping_jobs_active")[""] == 4


def test_a_status_with_no_jobs_is_still_reported_as_zero(client, monkeypatch):
    """An absent series and a zero are different on a dashboard.

    A panel plotting the failed count would show a gap rather than a flat zero, which reads as
    "the exporter stopped" instead of "nothing failed".
    """
    monkeypatch.setattr(endpoint, "get_manager", lambda: _Manager([]))
    by_status = _samples(_scrape(client), "clipping_jobs")
    assert {status.value for status in JobStatus} <= {
        labels.partition('="')[2].rstrip('"') for labels in by_status
    }
    assert set(by_status.values()) == {0}


def test_cancelling_is_reported_as_active(client, monkeypatch):
    """It holds the worker: ffmpeg is still running while the job winds down.

    Counting it as finished would make the queue look emptier than it is, which is exactly the
    number an operator uses to decide whether to submit more work.
    """
    monkeypatch.setattr(endpoint, "get_manager", lambda: _Manager([_job(JobStatus.CANCELLING)]))
    text = _scrape(client)
    assert _samples(text, "clipping_jobs_active")[""] == 1
    assert _samples(text, "clipping_jobs_queued")[""] == 0


def test_a_broken_store_does_not_fail_the_scrape(client, monkeypatch):
    """A monitoring endpoint that 500s during an incident is worse than one reporting less.

    The counters are in memory and still correct; only the gauges depend on the store.
    """

    class _Broken:
        @property
        def store(self):
            raise RuntimeError("database is locked")

    metrics.count_job_finished("failed")
    monkeypatch.setattr(endpoint, "get_manager", _Broken)
    text = _scrape(client)
    assert _samples(text, metrics.JOBS_FINISHED)['status="failed"'] == 1
    assert _samples(text, "clipping_jobs_tracked")[""] == 0


# --------------------------------------------------------------------------- #
# Authentication                                                                #
# --------------------------------------------------------------------------- #
def test_a_token_is_required(monkeypatch):
    """Open, this publishes job volume, failure rate and token spend to anyone on the port."""
    monkeypatch.setattr(settings, "api_auth_token", TOKEN)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        ok = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200


def test_metrics_is_not_exempt_from_auth():
    """Pinned against the convenient future change of adding it to the exempt list."""
    assert "/metrics" not in security._EXEMPT_PATHS


def test_a_query_token_is_not_accepted(monkeypatch):
    """``?token=`` exists only because a browser cannot put a header on a ``<video src>``.

    Prometheus can send an ``Authorization`` header, so widening the allowance - which puts the
    secret in access logs and referrers - buys nothing.
    """
    monkeypatch.setattr(settings, "api_auth_token", TOKEN)
    assert not security._QUERY_TOKEN_PATHS.match("/metrics")
    with TestClient(app) as client:
        assert client.get(f"/metrics?token={TOKEN}").status_code == 401


def test_the_route_is_registered_before_the_spa_catch_all():
    """The mount at ``/`` swallows any path no route claimed first.

    Asserted through the OpenAPI document plus the *source order* of the registrations, rather
    than by looking for the path in ``app.routes``: this Starlette version keeps an included
    router as a single opaque entry, so a naive membership check passes vacuously and would keep
    passing if the router were dropped.

    Source order rather than the position of the final route, because whether a catch-all exists
    at all depends on whether ``frontend/dist`` has been built - the backend CI job does not
    build it. A test that only passes on a built checkout is a test that reddens CI for a reason
    unrelated to what it is checking.
    """
    import inspect

    from api import main

    assert "/metrics" in app.openapi()["paths"]
    source = inspect.getsource(main)
    registration = source.index("include_router(metrics.router)")
    first_mount = source.index("app.mount(")
    assert registration < first_mount, "the metrics router must be registered before the mounts"


# --------------------------------------------------------------------------- #
# The collection hooks - a metric nobody feeds is worse than no metric            #
# --------------------------------------------------------------------------- #
def test_recording_a_stage_timing_reaches_the_histogram():
    """Hooked at ``Job_Metrics.record``, which every stage timing already passes through.

    Hooking each call site instead would mean a stage added later is timed per job and invisible
    in the aggregate - present in ``/timings`` and missing from the dashboard.
    """
    observability.metrics_for("job-a").record("render", 2.0)
    series = metrics.histograms()[metrics.STAGE_DURATION]
    assert series[(("stage", "render"),)].total == 1
    assert series[(("stage", "render"),)].sum == pytest.approx(2.0)


def test_the_stage_context_manager_feeds_the_same_histogram():
    with observability.job_context("job-b"):
        with observability.stage("transcribe"):
            pass
    assert (("stage", "transcribe"),) in metrics.histograms()[metrics.STAGE_DURATION]


def test_a_finished_job_records_its_outcome_clips_and_markers():
    from worker.jobs import JobManager

    job = _job(JobStatus.COMPLETED)
    job.clips = [
        ClipResult(
            id="c1",
            filename="a.mp4",
            start=0.0,
            end=5.0,
            duration=5.0,
            effects_applied=["captions", "broll:sunset"],
        ),
        ClipResult(
            id="c2",
            filename="b.mp4",
            start=5.0,
            end=9.0,
            duration=4.0,
            effects_applied=["captions"],
        ),
    ]
    JobManager._record_outcome_metrics(job)

    counters = metrics.counters()
    assert counters[metrics.JOBS_FINISHED][(("status", "completed"),)] == 1
    assert counters[metrics.CLIPS_RENDERED][()] == 2
    assert counters[metrics.CLIP_DEGRADATIONS][(("marker", "captions"),)] == 2
    assert counters[metrics.CLIP_DEGRADATIONS][(("marker", "broll"),)] == 1


def test_recording_an_outcome_never_raises():
    """It runs in the ``finally`` of the job body: anything it raises replaces the real outcome."""
    from worker.jobs import JobManager

    class _Hostile:
        @property
        def status(self):
            raise RuntimeError("no")

    JobManager._record_outcome_metrics(_Hostile())  # must not raise
    assert metrics.JOBS_FINISHED not in metrics.counters()


def test_llm_tokens_are_counted_per_call(monkeypatch):
    """Tokens are spent whether or not the job finishes.

    Totalling only at a terminal transition would under-report exactly the failed runs an
    operator is investigating.
    """
    monkeypatch.setattr(settings, "llm_price_input_per_mtok", 0.0, raising=False)
    monkeypatch.setattr(settings, "llm_price_output_per_mtok", 0.0, raising=False)

    class _Response:
        usage = type("U", (), {"prompt_tokens": 1000, "completion_tokens": 500})()

    llm_cost.record_response("gpt-4o-mini", _Response(), job_id="job-c")
    counters = metrics.counters()
    assert counters[metrics.LLM_CALLS][(("model", "gpt-4o-mini"),)] == 1
    tokens = counters[metrics.LLM_TOKENS]
    assert tokens[(("kind", "prompt"), ("model", "gpt-4o-mini"))] == 1000
    assert tokens[(("kind", "completion"), ("model", "gpt-4o-mini"))] == 500
    assert metrics.LLM_COST not in counters, "unpriced must be absent, not zero"


def test_cost_is_this_calls_tokens_not_the_running_total(monkeypatch):
    """``Model_Usage.cost_usd()`` is cumulative.

    Adding it to a counter on every call would count the first call once, the second twice and
    the tenth ten times - a total that grows quadratically and still looks like money.
    """
    monkeypatch.setattr(settings, "llm_price_input_per_mtok", 1.0, raising=False)
    monkeypatch.setattr(settings, "llm_price_output_per_mtok", 1.0, raising=False)

    class _Response:
        usage = type("U", (), {"prompt_tokens": 1_000_000, "completion_tokens": 0})()

    for _ in range(3):
        llm_cost.record_response("m", _Response(), job_id="job-d")
    assert metrics.counters()[metrics.LLM_COST][(("model", "m"),)] == pytest.approx(3.0)


def test_an_unreadable_usage_object_is_counted_as_unmetered(monkeypatch):
    """ "4000 calls, 0 tokens" reads as "the models are free" rather than "we could not tell"."""

    class _Response:
        pass

    llm_cost.record_response("mystery", _Response(), job_id="job-e")
    counters = metrics.counters()
    assert counters[metrics.LLM_CALLS][(("model", "mystery"),)] == 1
    assert counters[metrics.LLM_UNMETERED][(("model", "mystery"),)] == 1
    assert metrics.LLM_TOKENS not in counters


def test_webhook_outcomes_are_distinguished(monkeypatch):
    """``rejected`` means the receiver refused it; ``error`` means it was never reached.

    They need different responses, so a single ``failed`` count would hide which happened.
    """
    monkeypatch.setattr(settings, "job_webhook_url", "https://example.com/hook", raising=False)
    monkeypatch.setattr(settings, "job_webhook_events", "completed", raising=False)

    class _Ok:
        def post(self, *a, **k):
            return type("R", (), {"status_code": 204, "text": ""})()

        def close(self):
            pass

    class _Refuses:
        def post(self, *a, **k):
            return type("R", (), {"status_code": 500, "text": "nope"})()

        def close(self):
            pass

    class _Unreachable:
        def post(self, *a, **k):
            raise OSError("connection refused")

        def close(self):
            pass

    webhook.notify(_job(JobStatus.COMPLETED), client=_Ok())
    webhook.notify(_job(JobStatus.COMPLETED), client=_Refuses())
    webhook.notify(_job(JobStatus.COMPLETED), client=_Unreachable())

    series = metrics.counters()[metrics.WEBHOOKS]
    assert series[(("outcome", "delivered"),)] == 1
    assert series[(("outcome", "rejected"),)] == 1
    assert series[(("outcome", "error"),)] == 1


def test_webhooks_switched_off_record_no_deliveries(monkeypatch):
    """A deployment with no webhook must not show a stream of attempts that never left."""
    monkeypatch.setattr(settings, "job_webhook_url", None, raising=False)
    webhook.notify(_job(JobStatus.COMPLETED))
    assert metrics.WEBHOOKS not in metrics.counters()


# --------------------------------------------------------------------------- #
# Names                                                                         #
# --------------------------------------------------------------------------- #
def test_every_metric_name_is_valid_and_documented(client):
    """An undocumented metric is one nobody can safely alert on.

    Also pins the naming rules the format requires, and the base-unit suffix convention - a
    dashboard that has to guess whether a number is seconds or milliseconds is one that will
    eventually be read wrong.
    """
    import re

    pattern = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    metrics.observe_stage("render", 1.0)
    metrics.count_job_finished("completed")
    metrics.count_webhook("delivered")

    helps: dict[str, str] = {}
    for line in _scrape(client).splitlines():
        if line.startswith("# HELP "):
            _, _, rest = line.partition("# HELP ")
            name, _, text = rest.partition(" ")
            assert pattern.match(name), name
            assert name.startswith("clipping_"), f"{name} is not namespaced"
            assert text.strip(), f"{name} has an empty HELP"
            helps[name] = text

    assert helps, "nothing was exposed"
    assert metrics.STAGE_DURATION.endswith("_seconds")
    for name in (metrics.JOBS_FINISHED, metrics.CLIPS_RENDERED, metrics.WEBHOOKS):
        assert name.endswith("_total"), f"{name} is a counter and should say so"


def test_a_zero_cost_creates_no_series():
    """The guard inside ``count_llm_cost``, pinned separately from the caller's early return.

    Two layers keep "unpriced" out of ``clipping_llm_cost_usd_total``: this one, and
    ``_record_process_metrics`` returning before it computes a cost. Mutating either alone leaves
    the other holding, so each needs its own test - otherwise the pair passes every test while
    only one of them still works, and the day the second is removed nothing fails.
    """
    metrics.count_llm_cost(0.0, "m")
    metrics.count_llm_cost(-1.0, "m")
    assert metrics.LLM_COST not in metrics.counters()


def test_an_unpriced_call_never_reaches_the_cost_counter(monkeypatch):
    """Pins the early return itself, not the effect the downstream guard also produces.

    Asserted with a spy because the observable output is identical either way: absent because the
    caller declined to compute a cost, and absent because the counter refused a zero, look the
    same in a scrape.
    """
    monkeypatch.setattr(settings, "llm_price_input_per_mtok", 0.0, raising=False)
    monkeypatch.setattr(settings, "llm_price_output_per_mtok", 0.0, raising=False)
    calls: list[tuple[float, str]] = []
    monkeypatch.setattr(
        llm_cost.process_metrics,
        "count_llm_cost",
        lambda cost, model: calls.append((cost, model)),
    )

    class _Response:
        usage = type("U", (), {"prompt_tokens": 5000, "completion_tokens": 5000})()

    llm_cost.record_response("m", _Response(), job_id="job-f")
    assert calls == [], "an unpriced call must not compute a cost at all"


def test_a_priced_call_passes_only_this_calls_cost(monkeypatch):
    monkeypatch.setattr(settings, "llm_price_input_per_mtok", 2.0, raising=False)
    monkeypatch.setattr(settings, "llm_price_output_per_mtok", 4.0, raising=False)
    calls: list[tuple[float, str]] = []
    monkeypatch.setattr(
        llm_cost.process_metrics,
        "count_llm_cost",
        lambda cost, model: calls.append((cost, model)),
    )

    class _Response:
        usage = type("U", (), {"prompt_tokens": 1_000_000, "completion_tokens": 500_000})()

    llm_cost.record_response("m", _Response(), job_id="job-g")
    llm_cost.record_response("m", _Response(), job_id="job-g")
    # 1M at $2/M + 0.5M at $4/M = $4.00, the same on both calls rather than $4 then $8.
    assert calls == [(pytest.approx(4.0), "m"), (pytest.approx(4.0), "m")]


def test_a_real_run_records_its_outcome_through_execute(tmp_path):
    """End to end through ``_execute``, not just a direct call to the recorder.

    The distinction matters and was found by mutation: deleting the call from the ``finally``
    block left every other test in this file passing, because they exercise
    ``_record_outcome_metrics`` directly. A metric nobody feeds is worse than no metric - the
    dashboard renders, the line sits flat at zero, and it reads as "nothing is failing".

    A ``file`` job whose source does not exist reaches a terminal state before any work starts,
    which is the cheapest route through the real code. A ``url`` job would hand the source to
    yt-dlp and actually hit the network.
    """
    from worker.jobs import JobManager, JobStore

    manager = JobManager(store=JobStore(persistence=False))
    job = Job(
        input_type="file",
        source=str(tmp_path / "does-not-exist.mp4"),
        options=ProcessingOptions(),
    )
    job.status = JobStatus.QUEUED
    manager.store.add(job)
    manager._execute(job, job.id, lambda *a: None, lambda: None, observability.metrics_for(job.id))

    counters = metrics.counters()
    assert counters[metrics.JOBS_FINISHED][(("status", "failed"),)] == 1
    # No clips series at all, rather than one reporting zero: this run produced nothing, and a
    # failed job must not contribute a number to a clip total.
    assert metrics.CLIPS_RENDERED not in counters
    # No stage histogram either, and that is correct rather than a gap: this job raises while
    # resolving its source, before the first stage opens. The histogram's wiring is pinned by
    # test_recording_a_stage_timing_reaches_the_histogram, which does not need a full run.
    assert metrics.STAGE_DURATION not in metrics.histograms()


def test_the_outcome_is_recorded_from_the_finally_not_a_status_branch():
    """Pinned structurally, for the reason the webhook's placement is.

    Recording at each of the three ``store.update`` calls would be three sites to keep in step,
    and a fourth terminal outcome added later would silently count nothing. Reading the source is
    a blunt instrument, but it is the only way to assert *where* the call lives rather than that
    it happened to run for the outcomes tested today.
    """
    import inspect

    from worker.jobs import JobManager

    source = inspect.getsource(JobManager._execute)
    finally_block = source.rpartition("finally:")[2]
    assert "_record_outcome_metrics(final)" in finally_block
    # It must read the re-fetched terminal record, not the stale local captured before the run.
    assert "_record_outcome_metrics(job)" not in source
