"""Tests for I4 (cancellation), I6 (log context), M5 (stage timings), U8/U13.

The failure this file guards hardest against is a **cancellation that does not cancel**, and its
mirror, a cancellation reported as a failure. Both look plausible: the first shows a job that
carries on with a "cancelled" badge, the second an error message for something the user asked
for. Neither raises.

Second theme: the log context is a `contextvars` variable, which is invisible in a signature. A
leaked context is the worst possible failure for a logging feature - lines attributed
*confidently* to the wrong job - so several tests do nothing but assert that it is restored.
"""

from __future__ import annotations

import logging
import threading

import pytest

from worker import cancellation, jobs, observability
from worker.models import Job, JobStatus, ProcessingOptions


@pytest.fixture(autouse=True)
def _clean_registries():
    cancellation.reset()
    observability.clear_metrics()
    yield
    cancellation.reset()
    observability.clear_metrics()


# --------------------------------------------------------------------------- #
# I4 - cancellation
# --------------------------------------------------------------------------- #
def test_nothing_is_cancelled_by_default():
    assert cancellation.is_cancelled("abc") is False
    cancellation.checkpoint("abc")  # must not raise


def test_a_request_makes_the_checkpoint_raise():
    cancellation.request_cancel("abc")
    assert cancellation.is_cancelled("abc") is True
    with pytest.raises(cancellation.Job_Cancelled):
        cancellation.checkpoint("abc")


def test_a_request_is_scoped_to_one_job():
    """Cancelling one job must not stop another. Obvious, and catastrophic if wrong."""
    cancellation.request_cancel("first")
    cancellation.checkpoint("second")


def test_a_run_with_no_job_id_is_never_cancellable():
    """The smoke reel and the evaluation harness run the pipeline with no job id."""
    cancellation.request_cancel("abc")
    cancellation.checkpoint(None)
    assert cancellation.is_cancelled(None) is False


def test_clearing_lets_an_id_be_reused():
    """A repeated id must not inherit a previous job's cancellation."""
    cancellation.request_cancel("abc")
    cancellation.clear("abc")
    assert cancellation.is_cancelled("abc") is False
    cancellation.checkpoint("abc")


def test_the_registry_is_safe_across_threads():
    """The checkpoint runs on a worker thread while cancel is called from a request thread."""
    errors: list[BaseException] = []
    stop = threading.Event()

    def worker():
        try:
            while not stop.is_set():
                cancellation.checkpoint("threaded")
        except cancellation.Job_Cancelled:
            pass
        except BaseException as exc:  # recorded and re-raised below
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    for i in range(50):
        cancellation.request_cancel(f"other-{i}")
    cancellation.request_cancel("threaded")
    thread.join(timeout=5)
    stop.set()
    assert not thread.is_alive()
    assert errors == []


def test_cancelling_a_queued_job_marks_it_cancelled_not_failed():
    """A job the user stopped did not go wrong.

    Collapsing the two states would mislead the operator and inflate any failure rate computed
    from these records.
    """
    store = jobs.JobStore(persistence=False)
    job = Job(input_type="file", source="x.mp4", options=ProcessingOptions())
    store.add(job)
    manager = jobs.JobManager(store=store)

    assert manager.cancel(job.id) is True
    updated = store.get(job.id)
    assert updated.status is JobStatus.CANCELLED
    assert updated.error is None, "a cancellation must not populate the error field"


def test_cancelling_a_finished_job_is_a_no_op_rather_than_an_error():
    """A double-click on the button must not look like a failure."""
    store = jobs.JobStore(persistence=False)
    job = Job(
        input_type="file", source="x.mp4", options=ProcessingOptions(), status=JobStatus.COMPLETED
    )
    store.add(job)
    manager = jobs.JobManager(store=store)
    assert manager.cancel(job.id) is False
    assert store.get(job.id).status is JobStatus.COMPLETED


def test_cancelling_twice_is_harmless():
    store = jobs.JobStore(persistence=False)
    job = Job(input_type="file", source="x.mp4", options=ProcessingOptions())
    store.add(job)
    manager = jobs.JobManager(store=store)
    assert manager.cancel(job.id) is True
    assert manager.cancel(job.id) is False


def test_cancelling_an_unknown_job_returns_false():
    manager = jobs.JobManager(store=jobs.JobStore(persistence=False))
    assert manager.cancel("nope") is False


def test_submitting_clears_a_stale_request(monkeypatch):
    """Ids are short hex; a collision is unlikely rather than impossible, and inheriting a
    previous cancellation would stop a brand-new job before it started.

    Drives the real ``submit`` with the executor stubbed, rather than re-running what submit
    does - a test that reimplements the code under test would pass even if submit stopped doing
    it.
    """
    store = jobs.JobStore(persistence=False)
    manager = jobs.JobManager(store=store)
    scheduled: list = []
    monkeypatch.setattr(manager._executor, "submit", lambda fn, *a, **k: scheduled.append(a))

    # Pre-poison every id this submit could be given.
    original_clear = cancellation.clear
    seen_ids: list = []

    def recording_clear(job_id):
        seen_ids.append(job_id)
        return original_clear(job_id)

    monkeypatch.setattr(cancellation, "clear", recording_clear)
    job = manager.submit("file", "x.mp4", ProcessingOptions())

    assert seen_ids == [job.id], "submit did not clear a stale cancellation before scheduling"
    assert scheduled, "the job was never handed to the executor"
    assert cancellation.is_cancelled(job.id) is False


def test_cancelled_is_a_distinct_job_status():
    assert JobStatus.CANCELLED.value == "cancelled"
    assert JobStatus.CANCELLED is not JobStatus.FAILED


# --------------------------------------------------------------------------- #
# I6 - job-scoped log context
# --------------------------------------------------------------------------- #
def test_there_is_no_job_context_by_default():
    assert observability.current_job_id() is None


def test_the_context_is_visible_inside_and_gone_outside():
    with observability.job_context("job-1"):
        assert observability.current_job_id() == "job-1"
    assert observability.current_job_id() is None


def test_the_context_is_restored_even_when_the_body_raises():
    """A worker thread reused for a second job must not inherit the first job's id.

    Lines attributed *confidently* to the wrong job are worse than unattributed ones.
    """
    with pytest.raises(ValueError):
        with observability.job_context("job-1"):
            raise ValueError("boom")
    assert observability.current_job_id() is None


def test_contexts_nest_and_unwind_in_order():
    with observability.job_context("outer"):
        with observability.job_context("inner"):
            assert observability.current_job_id() == "inner"
        assert observability.current_job_id() == "outer"


def test_a_plain_thread_does_not_inherit_the_context():
    """A real constraint, pinned because I assumed the opposite while writing this.

    ``contextvars`` are copied for asyncio *tasks*, not for ``threading.Thread`` — a new thread
    starts with an empty context. That is why ``JobManager`` enters the context *inside* the
    worker (in ``_run``, which already runs on the pool thread) rather than around
    ``executor.submit``: wrapping the submit would attribute nothing at all, and the log would
    look exactly as it did before the feature existed.

    The consequence for later work: anything that spawns its own threads beneath a job — a
    parallel encode, the concurrency of I1 — has to re-enter the context itself.
    """
    seen: list = []

    def worker():
        seen.append(observability.current_job_id())

    with observability.job_context("main-job"):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
    assert seen == [None]


def test_entering_the_context_on_the_worker_thread_does_attribute():
    """The shape JobManager actually uses, so the mechanism is pinned end to end."""
    seen: list = []

    def worker(job_id):
        with observability.job_context(job_id):
            seen.append(observability.current_job_id())

    thread = threading.Thread(target=worker, args=("job-on-thread",))
    thread.start()
    thread.join()
    assert seen == ["job-on-thread"]


def test_the_filter_stamps_the_job_id_onto_a_record():
    record = logging.LogRecord("n", logging.INFO, "f", 1, "msg", None, None)
    with observability.job_context("job-9"):
        assert observability.Job_Context_Filter().filter(record) is True
    assert record.job_id == "job-9"


def test_the_filter_stamps_a_placeholder_when_unattributed():
    """An unattributed line is still a line worth having, so this must not raise."""
    record = logging.LogRecord("n", logging.INFO, "f", 1, "msg", None, None)
    observability.Job_Context_Filter().filter(record)
    assert record.job_id == "-"
    assert record.stage == "-"


def test_the_format_string_can_render_a_stamped_record():
    """A filter that stamps a field the format does not use, or vice versa, raises at log time."""
    record = logging.LogRecord("n", logging.INFO, "f", 1, "hello", None, None)
    observability.Job_Context_Filter().filter(record)
    rendered = logging.Formatter(observability.LOG_FORMAT).format(record)
    assert "job=-" in rendered and "hello" in rendered


def test_installing_twice_does_not_double_the_filter():
    """install() is called from startup and is useful from a script."""
    observability.install()
    observability.install()
    root = logging.getLogger()
    for handler in root.handlers:
        matching = [f for f in handler.filters if isinstance(f, observability.Job_Context_Filter)]
        assert len(matching) <= 1, "the filter was attached more than once"


# --------------------------------------------------------------------------- #
# M5 - per-stage timings
# --------------------------------------------------------------------------- #
def test_a_stage_is_timed_and_counted():
    metrics = observability.metrics_for("job-a")
    metrics.record("Rendering clip", 1.5)
    metrics.record("Rendering clip", 2.5)
    rows = metrics.to_list()
    assert rows[0]["stage"] == "Rendering clip"
    assert rows[0]["count"] == 2
    assert rows[0]["seconds"] == pytest.approx(4.0)
    assert rows[0]["mean_seconds"] == pytest.approx(2.0)


def test_the_mean_distinguishes_per_clip_stages_from_per_job_ones():
    """Both exist in this pipeline, and total time alone cannot compare them."""
    metrics = observability.metrics_for("job-b")
    metrics.record("Transcribing audio", 30.0)  # once per job
    for _ in range(10):
        metrics.record("Rendering clip", 4.0)  # once per clip
    rows = {r["stage"]: r for r in metrics.to_list()}
    assert rows["Rendering clip"]["seconds"] > rows["Transcribing audio"]["seconds"]
    assert rows["Rendering clip"]["mean_seconds"] < rows["Transcribing audio"]["mean_seconds"]


def test_timings_are_reported_slowest_first():
    """The only order worth reading."""
    metrics = observability.metrics_for("job-c")
    metrics.record("fast", 1.0)
    metrics.record("slow", 9.0)
    metrics.record("medium", 5.0)
    assert [r["stage"] for r in metrics.to_list()] == ["slow", "medium", "fast"]


def test_a_stage_that_raises_is_still_measured():
    """A stage that reliably burns time and then throws is the most useful row in the report."""
    with pytest.raises(RuntimeError):
        with observability.stage("doomed", job_id="job-d"):
            raise RuntimeError("boom")
    rows = observability.metrics_for("job-d").to_list()
    assert [r["stage"] for r in rows] == ["doomed"]
    assert rows[0]["count"] == 1


def test_the_stage_context_sets_and_restores_the_stage_name():
    with observability.stage("Transcribing audio", job_id="job-e"):
        assert observability.current_stage() == "Transcribing audio"
    assert observability.current_stage() is None


def test_a_stage_outside_a_job_is_not_recorded():
    """A pipeline run with no job id has nowhere to record to, and that is not an error."""
    with observability.stage("orphan"):
        pass
    assert observability.metrics_for("no-such-job").to_list() == []


def test_negative_durations_are_clamped():
    """time.monotonic cannot go backwards, but an injected value could."""
    metrics = observability.metrics_for("job-f")
    metrics.record("weird", -5.0)
    assert metrics.to_list()[0]["seconds"] == 0.0


def test_the_summary_names_where_the_time_went():
    metrics = observability.metrics_for("job-g")
    metrics.record("Rendering clip", 75.0)
    metrics.record("Transcribing audio", 25.0)
    summary = metrics.summary()
    assert "Rendering clip 75.0s (75%)" in summary
    assert summary.startswith("total 100.0s")


def test_an_empty_summary_does_not_divide_by_zero():
    assert observability.metrics_for("job-h").summary() == "no stages recorded"


def test_the_metrics_registry_is_bounded():
    """Process-global state that grows per job is a leak that looks like nothing."""
    observability.clear_metrics()
    for i in range(observability.MAX_TRACKED_JOBS + 40):
        observability.metrics_for(f"job-{i}")
    assert len(observability._metrics) <= observability.MAX_TRACKED_JOBS + 1


def test_metrics_are_safe_under_concurrent_recording():
    """Stages are recorded from a worker thread while the API may read them for a response."""
    metrics = observability.metrics_for("job-race")
    errors: list = []

    def writer():
        try:
            for _ in range(400):
                metrics.record("s", 0.001)
        except BaseException as exc:
            errors.append(exc)

    def reader():
        try:
            for _ in range(400):
                metrics.to_list()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    assert metrics.to_list()[0]["count"] == 400


# --------------------------------------------------------------------------- #
# U8 - stage position
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "stage,expected",
    [
        ("Starting", 1),
        ("Analyzing video", 2),
        ("Transcribing audio", 3),
        ("Finding the best moments", 4),
        ("Completed - 3 clip(s)", len(jobs.JOB_STAGES)),
    ],
)
def test_known_stages_map_to_their_position(stage, expected):
    assert jobs.stage_position(stage) == expected


def test_a_per_clip_stage_matches_despite_its_detail():
    assert jobs.stage_position("Rendering clip 2 of 5") == jobs.stage_position("Rendering clip")


def test_the_longest_matching_prefix_wins():
    """Scanning in order would let an earlier, shorter entry claim a later stage."""
    assert jobs._stage_label("Adding effects to clip 1") == "Adding effects"


def test_an_unrecognised_stage_reports_zero_rather_than_a_wrong_number():
    """A wrong step number is worse than none, so the UI hides it on 0."""
    assert jobs.stage_position("Doing something new") == 0
    assert jobs.stage_position("") == 0
    assert jobs.stage_position(None) == 0


def test_per_clip_stages_group_into_one_timing_row():
    """Five clips are one stage measured five times, not five stages.

    Without grouping, a five-clip job produces five one-off rows instead of one row with a count
    and a mean - and the mean is the number that says whether rendering dominates.
    """
    labels = {jobs._stage_label(f"Rendering clip {i} of 5") for i in range(1, 6)}
    assert labels == {"Rendering clip"}


def test_the_job_record_carries_the_stage_detail():
    job = Job(input_type="file", source="x", options=ProcessingOptions())
    job.stage_index, job.stage_total = 3, 9
    job.stage_timings = [{"stage": "Transcribing audio", "seconds": 12.0, "count": 1}]
    payload = job.to_dict()
    assert payload["stage_index"] == 3
    assert payload["stage_total"] == 9
    assert payload["stage_timings"][0]["stage"] == "Transcribing audio"


def test_the_stage_detail_survives_a_restart():
    """Timings are most often asked for about a job that finished a while ago."""
    job = Job(input_type="file", source="x", options=ProcessingOptions())
    job.stage_index, job.stage_total = 4, 9
    job.stage_timings = [{"stage": "Rendering clip", "seconds": 40.0, "count": 5}]
    restored = Job.from_dict(job.to_dict())
    assert restored.stage_index == 4
    assert restored.stage_timings == job.stage_timings


def test_a_record_written_by_an_older_build_still_loads():
    """The persisted rows predate these fields, so their absence must be a default not a crash."""
    payload = Job(input_type="file", source="x", options=ProcessingOptions()).to_dict()
    for key in ("stage_index", "stage_total", "stage_timings"):
        payload.pop(key)
    restored = Job.from_dict(payload)
    assert (restored.stage_index, restored.stage_total, restored.stage_timings) == (0, 0, [])


# --------------------------------------------------------------------------- #
# U13 - the fallback landing page
# --------------------------------------------------------------------------- #
def test_the_fallback_page_reports_real_state():
    """Someone reaching this page needs "is the backend healthy" answered, not prose."""
    from api.main import fallback_index_html

    html = fallback_index_html()
    assert "Version" in html
    assert "ffmpeg" in html
    assert "Engines" in html
    assert "Jobs" in html


def test_the_fallback_page_can_actually_read_the_engine_list():
    """The assertion that caught a real bug.

    ``_engines_info`` returns a *tuple* of ``(rows, capabilities)``. Iterating it enumerated the
    capabilities mapping instead, ``e['id']`` raised, and the page reported "could not be listed"
    on a completely healthy instance - the exact class of failure this page exists to surface.

    Asserted as the *absence* of that message rather than the presence of a named engine,
    deliberately: several tests in this suite call ``reset_registry()`` without restoring, so an
    engine name would make this pass or fail on file ordering. "none registered" is a legitimate
    answer here; "could not be listed" never is.
    """
    from api.main import fallback_index_html

    html = fallback_index_html()
    assert "could not be listed" not in html


def test_the_fallback_page_renders_when_the_job_store_is_broken(monkeypatch):
    """It must render when things are broken, because that is when it is read."""
    import api.main as main

    monkeypatch.setattr(main, "get_manager", lambda: (_ for _ in ()).throw(RuntimeError("no")))
    html = main.fallback_index_html()
    assert "job store unavailable" in html


def test_the_fallback_page_names_a_missing_ffmpeg(monkeypatch):
    """The single most common reason a deploy of this app does not work."""
    import api.main as main

    monkeypatch.setattr(main.settings, "ffmpeg_binary", "definitely-not-a-real-binary")
    html = main.fallback_index_html()
    assert "NOT FOUND" in html
