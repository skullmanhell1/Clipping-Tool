"""Tests for durable job records.

The job store was process memory only, so any restart discarded every job while the
clips stayed on disk and in the publish history. The visible symptom was a history view
listing clips whose downloads 404, which reads as data corruption rather than as the
plain state loss it actually was.

The tests below simulate a restart the only way that is meaningful: build a *second*
store over the same database file and assert what the new process can see.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from worker.job_persistence import (
    INTERRUPTED_ERROR,
    INTERRUPTED_STAGE,
    Job_Persistence,
)
from worker.jobs import JobStore
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "jobs.db"


@pytest.fixture
def store(db_path: Path) -> JobStore:
    """A store whose durability is backed by a per-test SQLite file."""
    return JobStore(persistence=Job_Persistence(db_path))


def _restart(db_path: Path) -> JobStore:
    """A brand-new store over the same file — i.e. what the next process would see."""
    return JobStore(persistence=Job_Persistence(db_path))


def _completed_job(**overrides) -> Job:
    job = Job(
        input_type="file",
        source="/tmp/source.mp4",
        options=ProcessingOptions(
            aspect="1:1",
            hashtag_count=9,
            topic="woodworking",
            publish_to=["youtube", "tiktok"],
        ),
        title="A source video",
    )
    job.status = JobStatus.COMPLETED
    job.stage = "Completed - 1 clip(s)"
    job.progress = 1.0
    job.clips = [
        ClipResult(
            id="c1",
            filename="clip_c1.mp4",
            start=1.5,
            end=13.5,
            duration=12.0,
            title="The good bit",
            description="why it is good",
            hashtags=["#a", "#b"],
            score=77.5,
            effects_applied=["engine:x:applied"],
        )
    ]
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


# ---------------------------------------------------------------------------
# Surviving a restart
# ---------------------------------------------------------------------------


def test_a_completed_job_survives_a_restart(store, db_path):
    """The central guarantee: the record is still there in the next process."""
    job = _completed_job()
    store.add(job)

    restored = _restart(db_path).get(job.id)
    assert restored is not None, "the job was lost across the restart"
    assert restored.id == job.id
    assert restored.status is JobStatus.COMPLETED
    assert restored.title == "A source video"


def test_clips_survive_with_the_fields_the_history_view_needs(store, db_path):
    """Clip records round-trip, since it is their absence that produced 404s."""
    job = _completed_job()
    store.add(job)

    restored = _restart(db_path).get(job.id)
    assert len(restored.clips) == 1
    clip = restored.clips[0]
    assert clip.id == "c1"
    assert clip.filename == "clip_c1.mp4"
    assert clip.duration == 12.0
    assert clip.hashtags == ["#a", "#b"]
    assert clip.score == 77.5
    # A list field that is neither a str nor a number, to catch shallow serialisation.
    assert clip.effects_applied == ["engine:x:applied"]


def test_processing_options_round_trip(store, db_path):
    """Options are restored, so a resumed/inspected job reports what was requested."""
    job = _completed_job()
    store.add(job)

    restored = _restart(db_path).get(job.id)
    assert restored.options.aspect == "1:1"
    assert restored.options.hashtag_count == 9
    assert restored.options.topic == "woodworking"
    # A list-valued option, to catch shallow serialisation.
    assert restored.options.publish_to == ["youtube", "tiktok"]


def test_updates_are_written_through_not_just_held_in_memory(store, db_path):
    """A crash gives no chance to flush, so each mutation must already be durable."""
    job = _completed_job(status=JobStatus.QUEUED)
    store.add(job)
    store.update(job.id, status=JobStatus.COMPLETED, stage="Completed - 2 clip(s)", progress=1.0)

    restored = _restart(db_path).get(job.id)
    assert restored.status is JobStatus.COMPLETED
    assert restored.stage == "Completed - 2 clip(s)"


def test_clip_metadata_edits_are_durable(store, db_path):
    """User edits are exactly the data least acceptable to lose."""
    job = _completed_job()
    store.add(job)
    store.update_clip(job.id, "c1", {"title": "An edited title"})

    restored = _restart(db_path).get(job.id)
    assert restored.clips[0].title == "An edited title"


def test_batch_grouping_survives(store, db_path):
    """``by_batch`` still resolves after a restart, so batch views keep working."""
    first = _completed_job(batch_id="batch7")
    second = _completed_job(batch_id="batch7")
    store.add(first)
    store.add(second)

    grouped = _restart(db_path).by_batch("batch7")
    assert {j.id for j in grouped} == {first.id, second.id}


def test_ordering_is_newest_first_after_a_restart(store, db_path):
    """``all()`` keeps its contract, which the jobs list relies on."""
    older = _completed_job(created_at=time.time() - 500)
    newer = _completed_job(created_at=time.time())
    store.add(older)
    store.add(newer)

    listed = _restart(db_path).all()
    assert [j.id for j in listed][:2] == [newer.id, older.id]


# ---------------------------------------------------------------------------
# Jobs that were mid-flight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.PROCESSING])
def test_an_interrupted_job_is_reported_as_failed_not_still_running(store, db_path, status):
    """A job that was running when the process died is resolved, not left spinning.

    Restoring it as ``processing`` would leave a progress bar advancing toward nothing
    forever, because the thread that owned it no longer exists. Failure is both true and
    actionable.
    """
    job = _completed_job(status=status, progress=0.4, stage="Rendering clip 1 of 3")
    store.add(job)

    restored = _restart(db_path).get(job.id)
    assert restored.status is JobStatus.FAILED
    assert restored.stage == INTERRUPTED_STAGE
    assert restored.error == INTERRUPTED_ERROR


def test_the_interrupted_resolution_is_itself_persisted(db_path):
    """The rewrite is stored, so it is not recomputed on every subsequent start-up."""
    first = JobStore(persistence=Job_Persistence(db_path))
    job = _completed_job(status=JobStatus.PROCESSING)
    first.add(job)

    _restart(db_path)  # resolves and should save
    third = _restart(db_path).get(job.id)
    assert third.status is JobStatus.FAILED
    assert third.error == INTERRUPTED_ERROR


def test_a_completed_job_is_not_touched_by_interrupt_handling(store, db_path):
    """Only in-flight statuses are rewritten; finished work is left alone."""
    job = _completed_job()
    store.add(job)

    restored = _restart(db_path).get(job.id)
    assert restored.status is JobStatus.COMPLETED
    assert restored.error is None


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_persistence_failures_do_not_break_the_store(db_path):
    """A broken backend degrades to in-memory rather than failing a running job.

    Losing a record is a reporting problem; raising here would abort a render that may
    already have taken minutes of CPU.
    """

    class Exploding:
        def load_all(self):
            raise RuntimeError("disk on fire")

        def save(self, job):
            raise RuntimeError("disk on fire")

        def prune(self, *, keep):
            raise RuntimeError("disk on fire")

    store = JobStore(persistence=Exploding())
    job = _completed_job()
    store.add(job)  # must not raise
    assert store.get(job.id) is job


def test_opting_out_keeps_the_store_purely_in_memory(db_path):
    """``persistence=False`` writes nothing, for tests that do not want durability."""
    store = JobStore(persistence=False)
    store.add(_completed_job())
    assert Job_Persistence(db_path).count() == 0


def test_an_unreadable_record_is_skipped_rather_than_poisoning_the_load(db_path):
    """One corrupt row must not prevent every other job from loading."""
    backend = Job_Persistence(db_path)
    good = _completed_job()
    backend.save(good)
    with backend._connect() as db:
        db.execute(
            "INSERT INTO jobs (id,batch_id,created_at,updated_at,status,data) "
            "VALUES('broken',NULL,?,?,'completed','{not valid json')",
            (time.time(), time.time()),
        )

    loaded = Job_Persistence(db_path).load_all()
    assert [j.id for j in loaded] == [good.id]


def test_an_unknown_status_degrades_to_failed(db_path):
    """A record from another build cannot be running, so it loads as failed."""
    backend = Job_Persistence(db_path)
    job = _completed_job()
    backend.save(job)
    with backend._connect() as db:
        db.execute(
            "UPDATE jobs SET data=REPLACE(data,'\"completed\"','\"teleporting\"') WHERE id=?",
            (job.id,),
        )

    restored = Job_Persistence(db_path).load_all()[0]
    assert restored.status is JobStatus.FAILED


# ---------------------------------------------------------------------------
# Bounding the table
# ---------------------------------------------------------------------------


def test_prune_keeps_only_the_newest_records(db_path):
    """The table is bounded so a long-lived instance does not grow forever."""
    backend = Job_Persistence(db_path)
    now = time.time()
    for index in range(5):
        backend.save(_completed_job(created_at=now + index))

    removed = backend.prune(keep=2)
    assert removed == 3
    assert backend.count() == 2


def test_prune_with_a_non_positive_keep_is_a_no_op(db_path):
    """A misconfigured bound must not wipe the store."""
    backend = Job_Persistence(db_path)
    backend.save(_completed_job())
    assert backend.prune(keep=0) == 0
    assert backend.count() == 1


def test_delete_removes_a_record(db_path):
    backend = Job_Persistence(db_path)
    job = _completed_job()
    backend.save(job)
    backend.delete(job.id)
    assert backend.count() == 0
