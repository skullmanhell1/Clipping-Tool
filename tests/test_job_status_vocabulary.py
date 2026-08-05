"""The job status vocabulary, and the two languages that have to agree about it (Phase 7).

`cancelling` was added as a real persisted status because the previous arrangement put the truth
in the transient half: `JobManager.cancel` wrote `CANCELLED` immediately while the API *response*
said "cancelling", so the record the UI polls contradicted the response a moment later and a job
still inside an ffmpeg pass appeared to have stopped.

Adding a status to an enum is easy. The risk is everything that had the old vocabulary written
out as a literal - `("queued", "processing")` appeared in `api/main.py` and again in `App.jsx` -
because a missing entry there does not raise. It undercounts, and both undercounts are the kind
nobody reports: a busy instance shown as idle, and a frontend dropping to its slow interval
exactly while a user waits for a cancel to land.

So the classifications are named once per language and pinned against each other here, the same
way `tests/test_stems_api.py` pins the settings schema across the two.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from worker.models import (
    ACTIVE_JOB_STATUSES,
    CANCELLABLE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    JobStatus,
)

_JOB_STATUS_JS = Path(__file__).resolve().parent.parent / "frontend" / "src" / "jobStatus.js"


def _js_list(name: str) -> list[str]:
    """The string entries of an exported array in `frontend/src/jobStatus.js`.

    Parsed rather than executed because the test suite is Python and adding a Node dependency to
    read three lists would be a heavier commitment than the lists are worth. The regex is
    anchored on the export so a renamed constant fails loudly instead of matching nothing.
    """
    source = _JOB_STATUS_JS.read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = \[(.*?)\];", source, re.DOTALL)
    assert match, f"{name} is not exported from {_JOB_STATUS_JS.name}"
    return re.findall(r'"([^"]+)"', match.group(1))


# --------------------------------------------------------------------------- #
# The enum itself
# --------------------------------------------------------------------------- #
def test_cancelling_exists_and_is_distinct_from_cancelled():
    assert JobStatus.CANCELLING.value == "cancelling"
    assert JobStatus.CANCELLED.value == "cancelled"
    assert JobStatus.CANCELLING is not JobStatus.CANCELLED


def test_every_status_is_classified_as_active_or_terminal():
    """The pin that makes the next status safe to add.

    A new member that is neither active nor terminal fails here rather than being silently
    omitted from the gauge, the landing page and the frontend's activity check - three places
    where the symptom is a wrong number rather than an error.
    """
    classified = ACTIVE_JOB_STATUSES | TERMINAL_JOB_STATUSES
    unclassified = set(JobStatus) - classified
    assert not unclassified, (
        f"unclassified job status: {sorted(s.value for s in unclassified)}. Add each to "
        "ACTIVE_JOB_STATUSES or TERMINAL_JOB_STATUSES in worker/models.py, and to the matching "
        "list in frontend/src/jobStatus.js."
    )


def test_active_and_terminal_do_not_overlap():
    assert not (ACTIVE_JOB_STATUSES & TERMINAL_JOB_STATUSES)


def test_cancelling_is_active_not_terminal():
    """It still holds the worker: an ffmpeg pass in progress runs to completion."""
    assert JobStatus.CANCELLING in ACTIVE_JOB_STATUSES
    assert JobStatus.CANCELLING not in TERMINAL_JOB_STATUSES


def test_cancelling_is_not_itself_cancellable():
    """A second cancel is a no-op, not an error - the button is live until the next frame."""
    assert JobStatus.CANCELLING not in CANCELLABLE_JOB_STATUSES
    assert CANCELLABLE_JOB_STATUSES == {JobStatus.QUEUED, JobStatus.PROCESSING}


# --------------------------------------------------------------------------- #
# The cross-language pin
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("name", "python_set"),
    [
        ("ACTIVE_JOB_STATUSES", ACTIVE_JOB_STATUSES),
        ("TERMINAL_JOB_STATUSES", TERMINAL_JOB_STATUSES),
        ("CANCELLABLE_JOB_STATUSES", CANCELLABLE_JOB_STATUSES),
    ],
)
def test_the_frontend_lists_match_the_python_sets(name, python_set):
    assert set(_js_list(name)) == {
        s.value for s in python_set
    }, f"{name} has drifted between worker/models.py and frontend/src/jobStatus.js"


def test_the_javascript_lists_contain_only_real_statuses():
    """A typo in the JS would classify nothing and fail no assertion of its own."""
    valid = {s.value for s in JobStatus}
    for name in ("ACTIVE_JOB_STATUSES", "TERMINAL_JOB_STATUSES", "CANCELLABLE_JOB_STATUSES"):
        assert set(_js_list(name)) <= valid, f"{name} names a status that does not exist"


def test_no_module_spells_the_old_active_tuple_inline():
    """The literal this change existed to remove.

    `("queued", "processing")` as an inline activity test is now wrong, and wrong quietly. This
    is a grep rather than a type-level guard because that is the only thing that can catch it
    coming back in a module nobody thought to look at.
    """
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in list(root.glob("api/**/*.py")) + list(root.glob("worker/**/*.py")):
        text = path.read_text(encoding="utf-8")
        # The module that *defines* the classification is exempt: its comment quotes the literal
        # it replaced, which is worth keeping. Matched on the definition rather than on the
        # filename so the exemption follows the constant if it ever moves - a hardcoded path
        # would silently stop exempting the right file and start exempting the wrong one.
        if "ACTIVE_JOB_STATUSES = " in text:
            continue
        if '("queued", "processing")' in text or '["queued", "processing"]' in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        f"{offenders} classify job activity with an inline literal. Use "
        "worker.models.ACTIVE_JOB_STATUSES so a new status cannot be missed."
    )


# --------------------------------------------------------------------------- #
# The transition, which is the behaviour the status exists for
# --------------------------------------------------------------------------- #
def _manager():
    from worker.jobs import JobManager, JobStore

    # `persistence=False` keeps this in memory: these tests are about state transitions, and a
    # SQLite round trip would only add a way for them to fail for another reason.
    return JobManager(store=JobStore(persistence=False))


def _job(store, status):
    from worker.models import Job, ProcessingOptions

    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.status = status
    store.add(job)
    return job


def test_cancelling_a_queued_job_is_immediate():
    """No worker has claimed it, so there is nothing to wait for and "cancelling" would be a lie."""
    manager = _manager()
    job = _job(manager.store, JobStatus.QUEUED)
    assert manager.cancel(job.id) is True
    assert manager.store.get(job.id).status is JobStatus.CANCELLED
    assert manager.store.get(job.id).stage == "Cancelled"


def test_cancelling_a_processing_job_reports_cancelling_not_cancelled():
    """The whole point.

    Writing CANCELLED here is what made the record contradict the API response: the UI re-read it
    a moment later and showed a job that was still rendering as stopped.
    """
    manager = _manager()
    job = _job(manager.store, JobStatus.PROCESSING)
    assert manager.cancel(job.id) is True
    assert manager.store.get(job.id).status is JobStatus.CANCELLING
    assert manager.store.get(job.id).stage == "Cancelling"


def test_cancelling_still_requests_the_stop():
    """The status change is the report; the flag is what actually stops the worker.

    Recording CANCELLING without requesting the cancel would produce a job that says it is
    stopping forever and never does.
    """
    from worker import cancellation

    manager = _manager()
    job = _job(manager.store, JobStatus.PROCESSING)
    manager.cancel(job.id)
    assert cancellation.is_cancelled(job.id) is True


def test_a_second_cancel_is_a_no_op_rather_than_an_error():
    """The button stays live until the next frame arrives, so a double-click is expected."""
    manager = _manager()
    job = _job(manager.store, JobStatus.PROCESSING)
    assert manager.cancel(job.id) is True
    assert manager.cancel(job.id) is False
    assert manager.store.get(job.id).status is JobStatus.CANCELLING


@pytest.mark.parametrize(
    "status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.CANCELLING]
)
def test_a_job_that_cannot_be_cancelled_is_left_alone(status):
    manager = _manager()
    job = _job(manager.store, status)
    assert manager.cancel(job.id) is False
    assert manager.store.get(job.id).status is status


def test_a_cancelling_job_cannot_be_resumed():
    """Its worker is still running; resuming would submit a second one for the same job."""
    manager = _manager()
    job = _job(manager.store, JobStatus.CANCELLING)
    job.planned_clips = [{"start": 0.0, "end": 5.0}]
    assert manager.resume(job.id) is False


# --------------------------------------------------------------------------- #
# The restart path - a non-terminal status with no worker left
# --------------------------------------------------------------------------- #
def test_a_job_left_cancelling_by_a_restart_resolves_to_cancelled(tmp_path):
    """It reached the end the user asked for, just not by the checkpoint.

    Two wrong answers were available. Leaving it `cancelling` strands it in a non-terminal state
    with nothing to advance it - it would claim to be stopping forever. Marking it `failed`, which
    is what `INTERRUPTED_STATUSES` does to `queued` and `processing`, inflates exactly the failure
    rate `CANCELLED` exists to keep honest, and attaches "re-submit the source" advice to a job
    nobody wanted finished.
    """
    from worker.job_persistence import Job_Persistence
    from worker.models import Job, ProcessingOptions

    store = Job_Persistence(tmp_path / "jobs.db")
    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.status = JobStatus.CANCELLING
    job.stage = "Cancelling"
    store.save(job)

    restored = {j.id: j for j in Job_Persistence(tmp_path / "jobs.db").load_all()}
    assert restored[job.id].status is JobStatus.CANCELLED
    assert restored[job.id].stage == "Cancelled"
    # Not an error condition, so no error text is attached.
    assert restored[job.id].error is None


def test_the_resolution_is_persisted_not_recomputed(tmp_path):
    """A second start-up must see `cancelled` on disk, not re-derive it from `cancelling`."""
    from worker.job_persistence import Job_Persistence
    from worker.models import Job, ProcessingOptions

    path = tmp_path / "jobs.db"
    store = Job_Persistence(path)
    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.status = JobStatus.CANCELLING
    store.save(job)
    Job_Persistence(path).load_all()

    import json
    import sqlite3

    with sqlite3.connect(path) as db:
        row = db.execute("SELECT data FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert json.loads(row[0])["status"] == "cancelled"


def test_a_cancelling_job_is_not_reported_as_an_interrupted_failure(tmp_path, caplog):
    """The log line must not say "as failed" about a cancellation.

    `interrupted` and the resolved list are tracked separately for this reason - reporting a
    cancellation as a failure in the log is the same conflation the status split removed from the
    record.
    """
    import logging

    from worker.job_persistence import Job_Persistence
    from worker.models import Job, ProcessingOptions

    store = Job_Persistence(tmp_path / "jobs.db")
    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.status = JobStatus.CANCELLING
    store.save(job)

    with caplog.at_level(logging.INFO):
        Job_Persistence(tmp_path / "jobs.db").load_all()
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "still cancelling at restart" in messages
    assert "as failed" not in messages


def test_queued_and_processing_still_become_failed_on_restart(tmp_path):
    """The pre-existing behaviour, unchanged - the new branch must not have widened."""
    from worker.job_persistence import Job_Persistence
    from worker.models import Job, ProcessingOptions

    store = Job_Persistence(tmp_path / "jobs.db")
    ids = {}
    for status in (JobStatus.QUEUED, JobStatus.PROCESSING):
        job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
        job.status = status
        store.save(job)
        ids[status] = job.id

    restored = {j.id: j for j in Job_Persistence(tmp_path / "jobs.db").load_all()}
    for status, job_id in ids.items():
        assert restored[job_id].status is JobStatus.FAILED, status
        assert restored[job_id].error


@pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
def test_a_terminal_status_survives_a_restart_untouched(status, tmp_path):
    from worker.job_persistence import Job_Persistence
    from worker.models import Job, ProcessingOptions

    store = Job_Persistence(tmp_path / "jobs.db")
    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.status = status
    store.save(job)

    restored = {j.id: j for j in Job_Persistence(tmp_path / "jobs.db").load_all()}
    assert restored[job.id].status is status


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #
@pytest.fixture
def cancel_client(monkeypatch):
    from fastapi.testclient import TestClient

    import api.main as main
    from api.routers import jobs as jobs_router

    manager = _manager()
    # Patched on the router module that owns the route, not on `api.main`: a route resolves
    # globals in its own module.
    monkeypatch.setattr(jobs_router, "get_manager", lambda: manager)
    return TestClient(main.app), manager


def test_the_route_reports_cancelling_for_a_running_job(cancel_client):
    client, manager = cancel_client
    job = _job(manager.store, JobStatus.PROCESSING)
    body = client.post(f"/api/jobs/{job.id}/cancel").json()
    assert body["state"] == "cancelling"
    assert "finish" in body["detail"]
    # The response and the record now agree, which is the fix.
    assert manager.store.get(job.id).status.value == body["state"]


def test_the_route_reports_cancelled_for_a_queued_job(cancel_client):
    client, manager = cancel_client
    job = _job(manager.store, JobStatus.QUEUED)
    body = client.post(f"/api/jobs/{job.id}/cancel").json()
    assert body["state"] == "cancelled"
    assert manager.store.get(job.id).status.value == body["state"]


def test_the_route_409s_for_a_job_already_cancelling(cancel_client):
    client, manager = cancel_client
    job = _job(manager.store, JobStatus.CANCELLING)
    response = client.post(f"/api/jobs/{job.id}/cancel")
    assert response.status_code == 409
    assert "cancelling" in response.json()["detail"]


def test_the_route_404s_for_an_unknown_job(cancel_client):
    client, _ = cancel_client
    assert client.post("/api/jobs/nope/cancel").status_code == 404
