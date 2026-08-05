"""Per-job LLM token accounting (Phase 7).

The failure this guards against is silence. A cost report is believed - that is the whole point
of having one - so a wrong number here is worse than no number, and every way it can be wrong is
quiet: an unread field reports zero tokens, an unpriced job reports free, a provider whose SDK
spells `input_tokens` instead of `prompt_tokens` reports a complete-looking total of nothing.

So the tests are weighted towards *reported values that look plausible and are wrong*, not
towards the accounting failing to run.
"""

from __future__ import annotations

import threading

import pytest

from worker import llm_cost, observability
from worker.llm_client import MockLLMClient


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty registry - it is process-global."""
    llm_cost.clear_usage()
    yield
    llm_cost.clear_usage()


@pytest.fixture
def priced(monkeypatch):
    """Configure a round pair of rates: $1 per million in, $10 per million out."""
    from config import settings

    monkeypatch.setattr(settings, "llm_price_input_per_mtok", 1.0, raising=False)
    monkeypatch.setattr(settings, "llm_price_output_per_mtok", 10.0, raising=False)


# --------------------------------------------------------------------------- #
# Extraction: the two provider spellings of the same two numbers
# --------------------------------------------------------------------------- #
class _OpenAI_Usage:
    """The shape `openai` returns.

    `total_tokens` is only derived when both parts are usable, so the class can also stand in
    for a malformed response without failing to construct.
    """

    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
            self.total_tokens = prompt_tokens + completion_tokens


class _Anthropic_Usage:
    """The shape `anthropic` returns - the same numbers under different names."""

    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    def __init__(self, usage):
        self.usage = usage


def test_openai_usage_is_read():
    usage = llm_cost.extract_usage(_Response(_OpenAI_Usage(120, 34)))
    assert usage == llm_cost.Token_Usage(prompt_tokens=120, completion_tokens=34)
    assert usage.total_tokens == 154


def test_anthropic_usage_is_read_despite_the_different_field_names():
    """The regression that would otherwise be invisible.

    Reading only OpenAI's spelling leaves every Anthropic call recorded with zero tokens: the
    call count is right, the cost is zero, and nothing indicates the total is fictional. This is
    the working agreement's "assert the resolved value" rule applied to a field *name*.
    """
    usage = llm_cost.extract_usage(_Response(_Anthropic_Usage(200, 50)))
    assert usage == llm_cost.Token_Usage(prompt_tokens=200, completion_tokens=50)


def test_a_dict_shaped_response_is_read_too():
    """Some compatible endpoints return plain JSON rather than an SDK object."""
    usage = llm_cost.extract_usage({"usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    assert usage == llm_cost.Token_Usage(prompt_tokens=7, completion_tokens=3)


@pytest.mark.parametrize(
    "response",
    [
        _Response(None),
        _Response(object()),  # a usage object with neither spelling
        object(),  # no usage attribute at all
        {},
        {"usage": {"something_else": 4}},
        _Response(_OpenAI_Usage("not-a-number", None)),
        _Response(_OpenAI_Usage(-5, -5)),  # negative counts are not a measurement
    ],
)
def test_an_unreadable_response_yields_none_rather_than_zero(response):
    """`None` and `Token_Usage(0, 0)` must not be confused.

    Zero tokens is a claim about the request. `None` is an admission that we do not know, and
    the caller turns it into an *unmetered* call so the shortfall is visible.
    """
    assert llm_cost.extract_usage(response) is None


def test_extraction_never_raises_on_a_hostile_object():
    """It runs on the success path of every LLM call; it may not be the thing that fails."""

    class Hostile:
        @property
        def usage(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _ = Hostile().usage  # confirm the fixture really does raise
    # record_response swallows it; the call is simply not counted.
    llm_cost.record_response("m", Hostile(), job_id="j1")
    assert llm_cost.usage_for("j1").to_dict()["calls"] == 0


# --------------------------------------------------------------------------- #
# Accumulation
# --------------------------------------------------------------------------- #
def test_calls_accumulate_per_model():
    llm_cost.record_response("gpt-4o-mini", _Response(_OpenAI_Usage(100, 10)), job_id="j1")
    llm_cost.record_response("gpt-4o-mini", _Response(_OpenAI_Usage(50, 5)), job_id="j1")
    llm_cost.record_response("claude", _Response(_Anthropic_Usage(20, 2)), job_id="j1")

    report = llm_cost.usage_for("j1").to_dict()
    assert report["calls"] == 3
    assert report["prompt_tokens"] == 170
    assert report["completion_tokens"] == 17
    assert report["total_tokens"] == 187
    assert [m["model"] for m in report["models"]] == ["gpt-4o-mini", "claude"]


def test_models_are_ordered_by_total_tokens_descending():
    """The model doing the work is the first row, whatever order the calls arrived in."""
    llm_cost.record_response("small", _Response(_OpenAI_Usage(1, 1)), job_id="j1")
    llm_cost.record_response("big", _Response(_OpenAI_Usage(900, 90)), job_id="j1")
    assert [m["model"] for m in llm_cost.usage_for("j1").to_dict()["models"]] == ["big", "small"]


def test_usage_is_attributed_per_job_not_pooled():
    llm_cost.record_response("m", _Response(_OpenAI_Usage(10, 1)), job_id="j1")
    llm_cost.record_response("m", _Response(_OpenAI_Usage(999, 99)), job_id="j2")
    assert llm_cost.usage_for("j1").to_dict()["total_tokens"] == 11
    assert llm_cost.usage_for("j2").to_dict()["total_tokens"] == 1098


def test_an_unmetered_call_is_counted_but_adds_no_tokens():
    """Both halves matter: the call is not lost, and the token total is not inflated."""
    llm_cost.record_response("m", _Response(_OpenAI_Usage(100, 10)), job_id="j1")
    llm_cost.record_response("m", _Response(None), job_id="j1")

    report = llm_cost.usage_for("j1").to_dict()
    assert report["calls"] == 2
    assert report["unmetered_calls"] == 1
    assert report["total_tokens"] == 110


def test_a_call_outside_a_job_is_not_attributed_to_a_fabricated_one():
    """A capability probe makes an LLM call with no job. It must not invent a bucket.

    Recording it under a placeholder id would put real spend against a job that does not exist,
    and the row would look exactly like a real one.
    """
    assert observability.current_job_id() is None
    llm_cost.record_response("m", _Response(_OpenAI_Usage(10, 1)))
    assert llm_cost.snapshot() == {}


def test_the_job_is_resolved_from_the_observability_context():
    """No call site passes a job id; attribution comes from the context the worker entered."""
    with observability.job_context("ctx-job"):
        llm_cost.record_response("m", _Response(_OpenAI_Usage(30, 3)))
    assert llm_cost.usage_for("ctx-job").to_dict()["total_tokens"] == 33


def test_concurrent_recording_loses_nothing():
    """The lock is load-bearing: a lost update would understate the bill by a plausible amount."""

    def worker():
        for _ in range(50):
            llm_cost.record_response("m", _Response(_OpenAI_Usage(2, 1)), job_id="j1")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    report = llm_cost.usage_for("j1").to_dict()
    assert report["calls"] == 400
    assert report["prompt_tokens"] == 800
    assert report["completion_tokens"] == 400


# --------------------------------------------------------------------------- #
# Pricing: the distinction between "free" and "unpriced"
# --------------------------------------------------------------------------- #
def test_an_unpriced_job_reports_none_not_zero():
    """The central claim of this module.

    `0.0` would be read as "this job cost nothing" and acted on. `None` says the rate is not
    configured, which is what is actually true.
    """
    llm_cost.record_response("m", _Response(_OpenAI_Usage(1_000_000, 0)), job_id="j1")
    report = llm_cost.usage_for("j1").to_dict()
    assert report["cost_usd"] is None
    assert report["priced"] is False
    assert report["models"][0]["cost_usd"] is None
    # The tokens are still counted - that is the point of separating the two.
    assert report["total_tokens"] == 1_000_000


def test_a_priced_job_reports_the_arithmetic(priced):
    """1M prompt tokens at $1/M plus 1M completion at $10/M is $11."""
    llm_cost.record_response("m", _Response(_OpenAI_Usage(1_000_000, 1_000_000)), job_id="j1")
    report = llm_cost.usage_for("j1").to_dict()
    assert report["priced"] is True
    assert report["cost_usd"] == pytest.approx(11.0)


def test_input_and_output_are_priced_at_their_own_rates(priced):
    """Charging output at the input rate understates every bill; the rates differ ~10x in reality."""
    llm_cost.record_response("m", _Response(_OpenAI_Usage(0, 1_000_000)), job_id="j1")
    assert llm_cost.usage_for("j1").to_dict()["cost_usd"] == pytest.approx(10.0)
    llm_cost.clear_usage("j1")
    llm_cost.record_response("m", _Response(_OpenAI_Usage(1_000_000, 0)), job_id="j1")
    assert llm_cost.usage_for("j1").to_dict()["cost_usd"] == pytest.approx(1.0)


def test_one_rate_alone_is_enough_to_be_priced(monkeypatch):
    """Requiring both would decline to cost a model that charges for input only."""
    from config import settings

    monkeypatch.setattr(settings, "llm_price_input_per_mtok", 2.0, raising=False)
    monkeypatch.setattr(settings, "llm_price_output_per_mtok", 0.0, raising=False)
    llm_cost.record_response("m", _Response(_OpenAI_Usage(1_000_000, 1_000_000)), job_id="j1")
    report = llm_cost.usage_for("j1").to_dict()
    assert report["priced"] is True
    assert report["cost_usd"] == pytest.approx(2.0)


def test_a_small_cost_is_not_rounded_away(priced):
    """Two decimal places would report most single jobs as costing nothing.

    That is the same false zero as the unpriced case, arrived at by rounding instead of by
    missing configuration.
    """
    llm_cost.record_response("m", _Response(_OpenAI_Usage(1_000, 100)), job_id="j1")
    cost = llm_cost.usage_for("j1").to_dict()["cost_usd"]
    assert cost is not None
    assert cost > 0.0
    assert round(cost, 2) == 0.0  # confirms the fixture really is in that range


def test_cost_understates_when_a_call_was_unmetered(priced):
    """Documented behaviour, pinned: the count is the flag that the total is a lower bound."""
    llm_cost.record_response("m", _Response(_OpenAI_Usage(1_000_000, 0)), job_id="j1")
    llm_cost.record_response("m", _Response(None), job_id="j1")
    report = llm_cost.usage_for("j1").to_dict()
    assert report["cost_usd"] == pytest.approx(1.0)
    assert report["unmetered_calls"] == 1


def test_an_empty_record_reports_no_cost_even_when_priced(priced):
    """A job that made no LLM calls has no spend - and that IS a zero, not an unknown."""
    report = llm_cost.usage_for("j1").to_dict()
    assert report["calls"] == 0
    # None, because there are no priced rows to sum. A job with no calls has no cost row at
    # all, which is different again from a call whose price is unknown.
    assert report["cost_usd"] is None


# --------------------------------------------------------------------------- #
# The bounded registry
# --------------------------------------------------------------------------- #
def test_the_registry_is_bounded():
    """Process-global state that grows once per job is a leak that looks like nothing."""
    for index in range(observability.MAX_TRACKED_JOBS + 25):
        llm_cost.record_response("m", _Response(_OpenAI_Usage(1, 1)), job_id=f"job-{index}")
    assert len(llm_cost.snapshot()) <= observability.MAX_TRACKED_JOBS + 1


def test_the_bound_is_shared_with_the_timings_registry():
    """One constant, not two.

    Both registries hold one entry per job for the same lifetime. Two limits would drift, and
    the smaller would silently decide the behaviour of both.
    """
    import inspect

    source = inspect.getsource(llm_cost)
    assert "observability.MAX_TRACKED_JOBS" in source
    assert "MAX_TRACKED_JOBS = " not in source


def test_the_newest_job_is_never_the_one_evicted():
    """Evicting the job currently recording would lose the spend it is in the middle of."""
    for index in range(observability.MAX_TRACKED_JOBS + 5):
        llm_cost.record_response("m", _Response(_OpenAI_Usage(1, 1)), job_id=f"job-{index}")
    newest = f"job-{observability.MAX_TRACKED_JOBS + 4}"
    assert newest in llm_cost.snapshot()


def test_clear_usage_removes_one_job_or_all():
    llm_cost.record_response("m", _Response(_OpenAI_Usage(1, 1)), job_id="j1")
    llm_cost.record_response("m", _Response(_OpenAI_Usage(1, 1)), job_id="j2")
    llm_cost.clear_usage("j1")
    assert set(llm_cost.snapshot()) == {"j2"}
    llm_cost.clear_usage()
    assert llm_cost.snapshot() == {}


def test_snapshot_reports_every_tracked_job():
    llm_cost.record_response("m", _Response(_OpenAI_Usage(4, 1)), job_id="j1")
    llm_cost.record_response("m", _Response(_OpenAI_Usage(6, 2)), job_id="j2")
    snap = llm_cost.snapshot()
    assert snap["j1"]["total_tokens"] == 5
    assert snap["j2"]["total_tokens"] == 8


# --------------------------------------------------------------------------- #
# The mock client, which is how every other test in the suite reaches an LLM
# --------------------------------------------------------------------------- #
def test_the_mock_records_nothing_by_default():
    """A mock has no real token counts, and fabricating some would poison a cost report.

    This is also why adding the accounting did not perturb the rest of the suite: every
    existing MockLLMClient construction still records nothing at all.
    """
    client = MockLLMClient(responses=["ok"])
    with observability.job_context("j1"):
        assert client.complete("hello") == "ok"
    assert llm_cost.usage_for("j1").to_dict()["calls"] == 0


def test_the_mock_records_usage_when_told_to():
    """The only way to exercise the accounting end to end without a live API key."""
    client = MockLLMClient(
        responses=["ok"],
        usage=llm_cost.Token_Usage(prompt_tokens=11, completion_tokens=5),
        model="mock-1",
    )
    with observability.job_context("j1"):
        client.complete("hello")
        client.complete("again")

    report = llm_cost.usage_for("j1").to_dict()
    assert report["calls"] == 2
    assert report["total_tokens"] == 32
    assert report["models"][0]["model"] == "mock-1"


def test_complete_json_records_exactly_one_call():
    """`complete_json` delegates to `complete`; it must not be billed twice."""
    client = MockLLMClient(
        responses=['{"a": 1}'],
        usage=llm_cost.Token_Usage(prompt_tokens=10, completion_tokens=2),
    )
    with observability.job_context("j1"):
        assert client.complete_json("hello") == {"a": 1}
    assert llm_cost.usage_for("j1").to_dict()["calls"] == 1


# --------------------------------------------------------------------------- #
# The job record, and the route that reports it
# --------------------------------------------------------------------------- #
def test_the_job_record_carries_the_usage():
    from worker.models import Job, ProcessingOptions

    job = Job(input_type="file", source="x", options=ProcessingOptions())
    job.llm_usage = {"calls": 3, "total_tokens": 500, "cost_usd": None}
    payload = job.to_dict()
    assert payload["llm_usage"]["calls"] == 3
    assert payload["llm_usage"]["cost_usd"] is None


def test_the_usage_survives_a_restart():
    """The in-process registry is bounded and dies with the process.

    A cost report that vanishes on restart cannot be reconciled against a bill, which is the
    one thing anybody wants it for.
    """
    from worker.models import Job, ProcessingOptions

    job = Job(input_type="file", source="x", options=ProcessingOptions())
    job.llm_usage = {"calls": 2, "total_tokens": 40, "cost_usd": 0.0004, "priced": True}
    restored = Job.from_dict(job.to_dict())
    assert restored.llm_usage == job.llm_usage


def test_a_record_written_before_this_field_existed_still_loads():
    """Every persisted row predates it, so absence must be a default and not a crash."""
    from worker.models import Job, ProcessingOptions

    payload = Job(input_type="file", source="x", options=ProcessingOptions()).to_dict()
    payload.pop("llm_usage")
    assert Job.from_dict(payload).llm_usage == {}


def test_a_null_usage_in_a_persisted_row_loads_as_empty():
    from worker.models import Job, ProcessingOptions

    payload = Job(input_type="file", source="x", options=ProcessingOptions()).to_dict()
    payload["llm_usage"] = None
    assert Job.from_dict(payload).llm_usage == {}


def test_the_timings_route_reports_the_usage_alongside_the_stages(monkeypatch):
    """Both currencies of "what did this job cost" on one route.

    A caller asking for one nearly always wants the other, and a separate route would mean the
    UI needed two requests to fill one panel.
    """
    from fastapi.testclient import TestClient

    import api.main as main
    from api.routers import jobs as jobs_router
    from worker.jobs import JobStore
    from worker.models import Job, JobStatus, ProcessingOptions

    store = JobStore()
    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.status = JobStatus.COMPLETED
    job.stage_timings = [{"stage": "Transcribing audio", "seconds": 12.0, "count": 1}]
    job.llm_usage = {"calls": 2, "total_tokens": 900, "cost_usd": None, "priced": False}
    store.add(job)

    class _Manager:
        def __init__(self):
            self.store = store

    # Patched on the router module holding the route, not on `api.main`: a route resolves
    # globals in its own module, so patching `api.main` would silently exercise the real store.
    monkeypatch.setattr(jobs_router, "get_manager", lambda: _Manager())
    response = TestClient(main.app).get(f"/api/jobs/{job.id}/timings")

    assert response.status_code == 200
    body = response.json()
    assert body["stages"][0]["stage"] == "Transcribing audio"
    assert body["llm_usage"]["total_tokens"] == 900
    # Null rather than absent: a consumer must be able to tell "unpriced" from "no spend".
    assert body["llm_usage"]["cost_usd"] is None
    assert body["llm_usage"]["priced"] is False


def test_the_timings_route_reports_an_empty_usage_for_an_older_job(monkeypatch):
    """A job that predates the field must not make the route 500."""
    from fastapi.testclient import TestClient

    import api.main as main
    from api.routers import jobs as jobs_router
    from worker.jobs import JobStore
    from worker.models import Job, JobStatus, ProcessingOptions

    store = JobStore()
    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.status = JobStatus.COMPLETED
    store.add(job)

    class _Manager:
        def __init__(self):
            self.store = store

    monkeypatch.setattr(jobs_router, "get_manager", lambda: _Manager())
    body = TestClient(main.app).get(f"/api/jobs/{job.id}/timings").json()
    assert body["llm_usage"] == {}


def test_submitting_a_job_does_not_inherit_a_previous_job_s_spend():
    """Ids are 12 hex characters: a collision is unlikely rather than impossible.

    Inheriting a cancellation stops a new job visibly. Inheriting a bill is quieter - the new
    job is simply charged for work it never did, and the number looks ordinary.
    """
    import inspect

    from worker import jobs as jobs_module

    source = inspect.getsource(jobs_module.JobManager.submit)
    assert "llm_cost.clear_usage(job.id)" in source


def test_every_update_that_reports_timings_also_reports_usage():
    """The two are the same kind of per-job telemetry and must not drift apart.

    If a terminal path recorded timings but not usage, a job's spend would be whatever it was at
    the last progress callback - stale, plausible, and wrong for exactly the LLM calls that run
    late in the pipeline (metadata generation is one of the last stages).
    """
    import inspect

    from worker import jobs as jobs_module

    source = inspect.getsource(jobs_module.JobManager)
    assert source.count("stage_timings=metrics.to_list()") == source.count(
        "llm_usage=llm_cost.usage_for(job_id).to_dict()"
    )
    assert source.count("llm_usage=llm_cost.usage_for(job_id).to_dict()") >= 4
