"""Offline tests for the in-process render queue.

These tests replace the expensive pipeline with tiny worker functions.  They exercise the queue's
ordering/capacity and lifecycle guarantees without ffmpeg, a network, Redis, or a running API.
"""

from __future__ import annotations

import threading
import time

import pytest
from pydantic import ValidationError

from config import Settings
from worker.jobs import QUEUE_FULL_ERROR, JobManager, JobStore
from worker.models import Job, JobStatus, ProcessingOptions


def _options() -> ProcessingOptions:
    return ProcessingOptions()


@pytest.fixture
def manager():
    instance = JobManager(store=JobStore(persistence=False))
    yield instance
    instance.shutdown(wait=True)


def _finish(store: JobStore, job_id: str) -> None:
    assert store.update(job_id, status=JobStatus.PROCESSING)
    assert store.update(job_id, status=JobStatus.COMPLETED, progress=1.0, stage="Completed")


def test_single_worker_keeps_submission_order(manager, monkeypatch):
    order: list[str] = []
    first_started = threading.Event()
    release_first = threading.Event()
    finished = threading.Event()

    def fake_run(job_id: str, resume: bool = False) -> None:
        order.append(job_id)
        if len(order) == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        _finish(manager.store, job_id)
        if len(order) == 3:
            finished.set()

    monkeypatch.setattr(manager, "_run", fake_run)
    jobs = [manager.submit("file", f"source-{index}.mp4", _options()) for index in range(3)]
    assert first_started.wait(timeout=2)
    release_first.set()
    assert finished.wait(timeout=2)

    assert order == [job.id for job in jobs]


def test_worker_count_bounds_heavy_jobs(manager, monkeypatch):
    manager.shutdown(wait=True)
    manager = JobManager(store=JobStore(persistence=False), max_workers=2, max_queue_size=3)
    active = 0
    maximum = 0
    started = 0
    finished = 0
    lock = threading.Lock()
    two_started = threading.Event()
    release = threading.Event()
    all_finished = threading.Event()

    def fake_run(job_id: str, resume: bool = False) -> None:
        nonlocal active, maximum, started, finished
        with lock:
            active += 1
            started += 1
            maximum = max(maximum, active)
            if started == 2:
                two_started.set()
        assert release.wait(timeout=2)
        _finish(manager.store, job_id)
        with lock:
            active -= 1
            finished += 1
            if finished == 5:
                all_finished.set()

    try:
        monkeypatch.setattr(manager, "_run", fake_run)
        jobs = [manager.submit("file", f"source-{index}.mp4", _options()) for index in range(5)]
        assert two_started.wait(timeout=2)
        assert manager.pending_count == 5
        assert maximum == 2
        release.set()
        assert all_finished.wait(timeout=2)
        assert maximum == 2
        assert all(job.status is JobStatus.COMPLETED for job in jobs)
    finally:
        release.set()
        manager.shutdown(wait=True)


def test_full_queue_rejects_without_blocking_and_persists_failure(monkeypatch):
    manager = JobManager(store=JobStore(persistence=False), max_workers=1, max_queue_size=0)
    release = threading.Event()
    started = threading.Event()

    def fake_run(job_id: str, resume: bool = False) -> None:
        assert manager.store.update(job_id, status=JobStatus.PROCESSING)
        started.set()
        assert release.wait(timeout=2)
        _finish(manager.store, job_id)

    try:
        monkeypatch.setattr(manager, "_run", fake_run)
        manager.submit("file", "first.mp4", _options())
        assert started.wait(timeout=2)

        began = time.monotonic()
        rejected = manager.submit("file", "second.mp4", _options())
        elapsed = time.monotonic() - began

        assert elapsed < 0.5
        assert rejected.status is JobStatus.FAILED
        assert rejected.stage == "Queue full"
        assert rejected.error == QUEUE_FULL_ERROR
        assert manager.pending_count == 1
        release.set()
    finally:
        release.set()
        manager.shutdown(wait=True)


def test_duplicate_schedule_and_executor_errors_do_not_leave_jobs_stuck(monkeypatch):
    manager = JobManager(store=JobStore(persistence=False), max_queue_size=2)
    completed = threading.Event()

    def fake_run(job_id: str, resume: bool = False) -> None:
        _finish(manager.store, job_id)
        completed.set()

    try:
        monkeypatch.setattr(manager, "_run", fake_run)
        job = manager.submit("file", "one.mp4", _options())
        assert completed.wait(timeout=2)
        assert manager._schedule(job.id) is False
        assert job.status is JobStatus.COMPLETED
        assert manager.pending_count == 0

        def fail_submit(*args, **kwargs):
            raise RuntimeError("executor stopped")

        monkeypatch.setattr(manager._executor, "submit", fail_submit)
        failed = manager.submit("file", "two.mp4", _options())
        assert failed.status is JobStatus.FAILED
        assert failed.stage == "Failed"
        assert "executor stopped" in (failed.error or "")
        assert manager.pending_count == 0
    finally:
        manager.shutdown(wait=True)


def test_duplicate_job_records_are_rejected_without_replacing_the_original():
    store = JobStore(persistence=False)
    original = Job(input_type="file", source="original.mp4", options=_options())
    duplicate = Job(
        input_type="file",
        source="duplicate.mp4",
        options=_options(),
        id=original.id,
    )

    assert store.add(original)
    assert store.add(duplicate) is False
    assert store.get(original.id) is original
    assert store.get(original.id).source == "original.mp4"


def test_worker_error_releases_capacity_for_the_next_job(monkeypatch):
    manager = JobManager(store=JobStore(persistence=False), max_workers=1, max_queue_size=0)
    failed_worker = threading.Event()
    completed_worker = threading.Event()

    def fake_run(job_id: str, resume: bool = False) -> None:
        job = manager.store.get(job_id)
        if job and job.source == "boom.mp4":
            failed_worker.set()
            raise RuntimeError("render exploded")
        _finish(manager.store, job_id)
        completed_worker.set()

    try:
        monkeypatch.setattr(manager, "_run", fake_run)
        failed = manager.submit("file", "boom.mp4", _options())
        assert failed_worker.wait(timeout=2)
        # The first worker's exception is contained by the executor wrapper, so its slot is
        # available immediately for a subsequent submission.
        deadline = time.monotonic() + 2
        while failed.status is not JobStatus.FAILED and time.monotonic() < deadline:
            time.sleep(0.005)
        assert failed.status is JobStatus.FAILED
        next_job = manager.submit("file", "next.mp4", _options())
        assert completed_worker.wait(timeout=2)
        assert "render exploded" in (failed.error or "")
        assert next_job.status is JobStatus.COMPLETED
        assert manager.pending_count == 0
    finally:
        manager.shutdown(wait=True)


def test_terminal_status_wins_over_late_worker_writes():
    store = JobStore(persistence=False)
    job = Job(input_type="file", source="source.mp4", options=_options())
    store.add(job)

    assert store.update(job.id, status=JobStatus.PROCESSING)
    assert store.update(job.id, status=JobStatus.CANCELLED, stage="Cancelled")
    assert store.update(job.id, status=JobStatus.PROCESSING, progress=0.9) is False
    assert store.update(job.id, status=JobStatus.COMPLETED, progress=1.0) is False
    assert job.status is JobStatus.CANCELLED
    assert job.stage == "Cancelled"

    # A cancelled job can be explicitly resumed through the normal queued transition, but a
    # stale worker cannot jump directly back into processing.
    assert store.update(job.id, status=JobStatus.QUEUED, stage="Queued (resuming)")
    assert store.update(job.id, status=JobStatus.PROCESSING)


def test_queue_capacity_settings_are_environment_configurable(monkeypatch):
    monkeypatch.setenv("JOB_MAX_WORKERS", "3")
    monkeypatch.setenv("JOB_QUEUE_SIZE", "17")

    configured = Settings(_env_file=None)

    assert configured.job_max_workers == 3
    assert configured.job_queue_size == 17


def test_queue_capacity_settings_keep_the_safe_defaults(monkeypatch):
    monkeypatch.delenv("JOB_MAX_WORKERS", raising=False)
    monkeypatch.delenv("JOB_QUEUE_SIZE", raising=False)

    configured = Settings(_env_file=None)

    assert configured.job_max_workers == 1
    assert configured.job_queue_size == 100


@pytest.mark.parametrize(
    ("name", "value"),
    [("JOB_MAX_WORKERS", "0"), ("JOB_QUEUE_SIZE", "-1")],
)
def test_queue_capacity_settings_reject_invalid_bounds(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_manager_singleton_passes_configured_capacity_to_the_pool(monkeypatch):
    import worker.jobs as jobs

    captured = {}

    class RecordingManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(jobs, "JobManager", RecordingManager)
    monkeypatch.setattr(jobs, "_manager", None)
    monkeypatch.setattr(jobs.settings, "job_max_workers", 4)
    monkeypatch.setattr(jobs.settings, "job_queue_size", 23)

    manager = jobs.get_manager()

    assert isinstance(manager, RecordingManager)
    assert captured == {"max_workers": 4, "max_queue_size": 23}
