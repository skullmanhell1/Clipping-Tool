"""Foundation-layer defects that a green suite could not see.

Every case here is a real defect that shipped, and they share a shape: each involves **two things
happening at once** — a background thread and a live job, an API poll and a render, a cancel
request and a worker — which is precisely what a suite of single-threaded unit tests cannot
observe. The modules were individually correct and collectively wrong.

Grouped by the interaction rather than by module, because the module is never where the problem
was.
"""

from __future__ import annotations

import os
import threading
import time
import zipfile

import pytest

from storage_backends import retention
from worker import cancellation
from worker.jobs import JobStore
from worker.models import Job, JobStatus, ProcessingOptions


def _job(status=JobStatus.PROCESSING, **kwargs) -> Job:
    kwargs.setdefault("input_type", "file")
    kwargs.setdefault("source", "/tmp/x.mp4")
    job = Job(options=ProcessingOptions(), **kwargs)
    job.status = status
    return job


# --------------------------------------------------------------------------- #
# Cleanup vs. a live job                                                        #
# --------------------------------------------------------------------------- #
class _FakeManager:
    """Stands in for the process-wide JobManager singleton."""

    def __init__(self, jobs):
        self.store = type("S", (), {"all": staticmethod(lambda: list(jobs))})()


@pytest.fixture
def live_jobs(monkeypatch):
    """Install a fake job store and return a mutable list of jobs it reports."""
    jobs: list[Job] = []
    monkeypatch.setattr("worker.jobs.get_manager", lambda: _FakeManager(jobs))
    return jobs


def test_unscoped_cleanup_temp_spares_a_running_job(tmp_path, monkeypatch, live_jobs):
    """``POST /api/storage/cleanup`` defaults ``temp=true`` and used to kill live renders.

    ``run_pipeline`` works in ``storage/temp/<job_id>/`` — extracted audio, transcripts,
    intermediate segments. The endpoint called ``cleanup_temp()`` with no job id, which
    ``rmtree``'d every child of ``temp/``. The render then failed inside the generic handler with
    a message naming a missing temp file, which points at neither the cause nor the click that
    caused it.
    """
    from config import settings

    temp_root = tmp_path / "temp"
    monkeypatch.setattr(settings, "temp_dir", temp_root)
    running = _job(JobStatus.PROCESSING)
    finished = _job(JobStatus.COMPLETED)
    live_jobs.extend([running, finished])

    for job in (running, finished):
        workspace = temp_root / job.id
        workspace.mkdir(parents=True)
        (workspace / "audio.wav").write_bytes(b"x")

    removed = retention.cleanup_temp()

    assert (temp_root / running.id).exists(), "deleted a running job's scratch directory"
    assert not (temp_root / finished.id).exists(), "should still tidy finished jobs"
    assert removed == 1


def test_queued_jobs_are_protected_too(tmp_path, monkeypatch, live_jobs):
    """A queued job's directory may not exist yet, but the id must still be spared.

    QUEUED is a job the single worker has not reached. Sweeping its workspace is the same defect
    one moment earlier, and the pool is serial so the wait can be long.
    """
    from config import settings

    temp_root = tmp_path / "temp"
    monkeypatch.setattr(settings, "temp_dir", temp_root)
    queued = _job(JobStatus.QUEUED)
    live_jobs.append(queued)
    (temp_root / queued.id).mkdir(parents=True)

    assert retention.cleanup_temp() == 0
    assert (temp_root / queued.id).exists()


def test_scoped_cleanup_temp_still_deletes_its_own_job(tmp_path, monkeypatch, live_jobs):
    """The protection must not break ``JobManager._cleanup_temp``.

    That runs in a ``finally`` for the job that has just stopped, and the job is still briefly
    PROCESSING in the store. If an explicit id were refused, every job's scratch space would leak
    forever — the opposite of the intent. An id names a caller who knows which job they mean.
    """
    from config import settings

    temp_root = tmp_path / "temp"
    monkeypatch.setattr(settings, "temp_dir", temp_root)
    job = _job(JobStatus.PROCESSING)
    live_jobs.append(job)
    (temp_root / job.id).mkdir(parents=True)

    assert retention.cleanup_temp(job.id) == 1
    assert not (temp_root / job.id).exists()


def test_the_retention_sweep_spares_a_running_jobs_old_intermediates(
    tmp_path, monkeypatch, live_jobs
):
    """Age is not evidence that a file is finished with.

    The empty-directory branch was hardened against this race with a grace period; the *file*
    branch beside it kept deleting anything older than the cutoff. A resumed job legitimately
    re-reads cached intermediates older than the retention window, so the sweeper could remove an
    input from under a running job. Only the job's status settles it.
    """
    from config import settings

    root = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_root", root)
    running = _job(JobStatus.PROCESSING)
    live_jobs.append(running)

    for owner in (running.id, "finished-job"):
        area = root / "clips" / owner
        area.mkdir(parents=True)
        stale = area / "intermediate.mp4"
        stale.write_bytes(b"x")
        ancient = time.time() - 100 * 86400
        os.utime(stale, (ancient, ancient))

    result = retention.cleanup_expired(retention_days=30)

    assert (root / "clips" / running.id / "intermediate.mp4").exists()
    assert not (root / "clips" / "finished-job" / "intermediate.mp4").exists()
    assert result["removed"] == 1
    # Reported, so an operator can tell "nothing was old enough" from "it was all in use".
    assert result["skipped_active"] >= 1


def test_the_sweep_does_not_follow_a_symlink_out_of_the_storage_root(tmp_path, monkeypatch):
    """``rglob`` does not filter symlinks, so the sweep could unlink outside its own tree.

    ``_dir_size`` goes out of its way to use ``scandir(follow_symlinks=False)`` and documents why.
    The destructive path had no equivalent guard, which is the wrong way round.
    """
    from config import settings

    root = tmp_path / "storage"
    (root / "clips").mkdir(parents=True)
    monkeypatch.setattr(settings, "storage_root", root)

    outsider = tmp_path / "important.txt"
    outsider.write_text("not yours to delete")
    ancient = time.time() - 100 * 86400
    os.utime(outsider, (ancient, ancient))
    (root / "clips" / "link.txt").symlink_to(outsider)

    retention.cleanup_expired(retention_days=30)

    assert outsider.exists(), "the sweep deleted a file outside the storage root"


def test_a_failing_sweep_is_visible_rather_than_silent(monkeypatch, caplog):
    """``except Exception: pass`` meant retention could never run and never say so.

    The disk filled, ``last_result`` stayed ``{}``, and ``GET /api/storage`` went on reporting
    healthy usage with no error field anywhere.
    """
    sweeper = retention.RetentionSweeper(interval_hours=0.0)

    def boom():
        raise OSError("storage root is not mounted")

    monkeypatch.setattr(retention, "cleanup_expired", boom)
    # Drive one iteration of the loop body rather than the thread, so the test is deterministic.
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(sweeper, "_stop", type("E", (), {"wait": staticmethod(lambda t: False)})())

    with caplog.at_level("ERROR"):
        with pytest.raises(SystemExit):
            # The loop is infinite by design; break out after the first failure is recorded.
            def _once(_timeout):
                if sweeper.last_result:
                    raise SystemExit
                return False

            sweeper._stop = type("E", (), {"wait": staticmethod(_once)})()
            sweeper._loop()

    assert "retention sweep failed" in caplog.text
    assert "error" in sweeper.last_result


# --------------------------------------------------------------------------- #
# Cancellation vs. the worker                                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clear_cancellations():
    cancellation.reset()
    yield
    cancellation.reset()


def test_a_cancel_during_download_is_not_undone(monkeypatch, tmp_path):
    """The download progress callback wrote PROCESSING back over CANCELLED.

    ``cancel()`` set CANCELLED; the next yt-dlp tick — sub-second — overwrote it, so the UI showed
    "Cancelled" and then flipped to "Processing 6%" and kept climbing. The download itself ran to
    completion, so a multi-gigabyte fetch the user had stopped still spent the bandwidth and still
    landed in ``uploads/``. The callback is now a checkpoint, like the pipeline's.
    """
    from worker.jobs import JobManager

    manager = JobManager(store=JobStore(persistence=False))
    job = _job(JobStatus.PROCESSING, input_type="url")
    job.source = "https://example.com/v"
    manager.store.add(job)

    ticks: list[float] = []

    def fake_download(url, dest, progress_cb=None):
        # Two ticks: the cancel lands between them, so the second must raise.
        progress_cb(0.05, "Downloading 5%")
        ticks.append(0.05)
        cancellation.request_cancel(job.id)
        progress_cb(0.40, "Downloading 40%")
        ticks.append(0.40)
        raise AssertionError("the download continued past a requested cancellation")

    monkeypatch.setattr("worker.jobs.dl.download_video", fake_download)
    manager._run(job.id)

    assert ticks == [0.05], "the second progress tick should have raised Job_Cancelled"
    assert manager.store.get(job.id).status is JobStatus.CANCELLED
    assert manager.store.get(job.id).error is None, "a cancelled job did not go wrong"


def test_a_late_cancel_is_not_overwritten_by_completion(monkeypatch, tmp_path):
    """Two writers for one terminal field: the worker must get the last word.

    ``cancel()`` wrote CANCELLED and ``_execute``'s success path wrote COMPLETED without
    re-checking, so a cancel arriving after the final ``progress()`` call produced a job the user
    stopped and the API reported as completed.
    """
    from worker.jobs import JobManager

    manager = JobManager(store=JobStore(persistence=False))
    source = tmp_path / "in.mp4"
    source.write_bytes(b"x")
    job = _job(JobStatus.PROCESSING)
    job.source = str(source)
    manager.store.add(job)

    def fake_pipeline(*args, **kwargs):
        # The render finishes, and the cancel lands while the last pass was still running.
        cancellation.request_cancel(job.id)
        return []

    monkeypatch.setattr("worker.jobs.run_pipeline", fake_pipeline)
    monkeypatch.setattr(
        "worker.jobs.probe", lambda p: type("I", (), {"duration": 1.0})(), raising=False
    )
    manager._run(job.id)

    stored = manager.store.get(job.id)
    assert stored.status is JobStatus.CANCELLED, "COMPLETED overwrote the user's cancellation"


# --------------------------------------------------------------------------- #
# API reads vs. the render thread                                               #
# --------------------------------------------------------------------------- #
def test_snapshots_are_taken_under_the_lock(monkeypatch):
    """Serialising a live Job races the worker mutating it.

    ``to_dict`` hands out the same ``planned_clips`` / ``stage_timings`` list objects that
    ``update()`` rebinds, and JSON-encoding a list another thread is replacing raises
    ``RuntimeError: list changed size during iteration`` — an intermittent 500 on the route the
    shipped UI polls every 1200 ms.

    Asserted structurally rather than by trying to win a race: the snapshot must not share list
    identity with the live object, which is the property that makes the encode safe.
    """
    store = JobStore(persistence=False)
    job = _job()
    job.planned_clips = [{"start": 0.0, "end": 1.0}]
    store.add(job)

    snapshot = store.snapshot(job.id)
    assert snapshot is not None
    live = store.get(job.id)
    store.update(job.id, planned_clips=[{"start": 5.0, "end": 6.0}])

    # The snapshot was taken before the update and must not have followed it.
    assert snapshot["planned_clips"] == [{"start": 0.0, "end": 1.0}]
    assert live.planned_clips == [{"start": 5.0, "end": 6.0}]


def test_snapshot_all_survives_concurrent_updates():
    """The property that actually matters, exercised concurrently.

    A reader looping over ``snapshot_all`` while a writer rebinds the same lists must never raise.
    Before the change this is the exact shape that produced the intermittent 500.
    """
    store = JobStore(persistence=False)
    jobs = [_job() for _ in range(12)]
    for job in jobs:
        job.stage_timings = [{"stage": "s", "seconds": 1.0}]
        store.add(job)

    errors: list[BaseException] = []
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            n += 1
            for job in jobs:
                store.update(job.id, stage_timings=[{"stage": "s", "seconds": float(n)}] * (n % 7))

    def reader():
        try:
            while not stop.is_set():
                for record in store.snapshot_all():
                    list(record["stage_timings"])
        except BaseException as exc:  # pragma: no cover - the defect being guarded
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    time.sleep(0.4)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"serialising raced the writer: {errors[0]!r}"


def test_persistence_writes_are_monotonic(tmp_path):
    """Two overlapping updates committed in scheduling order, not in the order they happened.

    ``save()`` re-read the live object outside the store lock, so the stored row was
    last-writer-by-scheduling. After a restart a job could report an earlier stage than it had
    reached — progress going backwards, which reads as corruption rather than as a race.
    """
    from worker.job_persistence import Job_Persistence

    store = Job_Persistence(path=tmp_path / "jobs.db")
    # COMPLETED, not PROCESSING: `load_all` deliberately rewrites a persisted PROCESSING job as
    # "Interrupted by restart", which would mask what this test is measuring.
    job = _job(JobStatus.COMPLETED)
    job.stage = "Transcribing"
    job.updated_at = 1000.0
    store.save(job)

    # A stale payload arriving late — the shape a lost race produces.
    stale = dict(job.to_dict())
    stale["stage"] = "Starting"
    stale["updated_at"] = 900.0
    store.save(job, payload=stale)

    (restored,) = [j for j in store.load_all() if j.id == job.id]
    assert restored.stage == "Transcribing", "a stale write overwrote a newer state"


# --------------------------------------------------------------------------- #
# Packaging a download                                                          #
# --------------------------------------------------------------------------- #
def test_the_clip_package_is_streamed_and_readable(tmp_path):
    """The zip is produced incrementally and is still a valid archive.

    It used to be built entirely in a ``BytesIO`` first, so peak memory was one clip — routinely
    100-400 MB — per concurrent request, in a container the blueprint provisions at 2 GB that also
    hosts whisper and ffmpeg. Streaming is only worth anything if the result still unzips, and the
    media member must be *stored* rather than deflated: H.264 does not compress, so deflating it
    spent API CPU against the render worker for nothing.
    """
    from api.main import _iter_clip_package

    media = tmp_path / "clip_01.mp4"
    payload = os.urandom(300_000)
    media.write_bytes(payload)

    chunks = list(_iter_clip_package(media, "clip_01.mp4", "Title\nHello\n", chunk_size=16_384))

    assert len(chunks) > 5, "produced in one lump, so nothing was actually streamed"
    archive_bytes = b"".join(chunks)
    out = tmp_path / "package.zip"
    out.write_bytes(archive_bytes)
    with zipfile.ZipFile(out) as archive:
        assert archive.testzip() is None
        assert sorted(archive.namelist()) == ["clip_01.mp4", "clip_01_metadata.txt"]
        assert archive.read("clip_01.mp4") == payload
        assert "Hello" in archive.read("clip_01_metadata.txt").decode()
        assert archive.getinfo("clip_01.mp4").compress_type == zipfile.ZIP_STORED


# --------------------------------------------------------------------------- #
# Diagnostics that must survive the platform they describe                      #
# --------------------------------------------------------------------------- #
def test_the_store_failure_message_survives_a_missing_getuid(tmp_path, monkeypatch):
    """``os.getuid`` does not exist on Windows, and Windows is what this text is *for*.

    The whole purpose of ``describe_store_failure`` is Docker Desktop and native Windows bind-mount
    failures. Calling ``os.getuid()`` unguarded meant the error *formatter* raised AttributeError
    and replaced the diagnostic with exactly the opaque failure it exists to prevent.
    """
    from worker import job_persistence

    monkeypatch.delattr(os, "getuid", raising=False)
    unwritable = tmp_path / "locked"
    unwritable.mkdir()
    # `os.access` is forced rather than relying on a chmod: the suite runs as root in CI and in
    # this sandbox, and root bypasses the permission bits — so a 0o500 directory reads as writable
    # and the test would silently exercise the wrong branch.
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    message = job_persistence.describe_store_failure(
        unwritable / "jobs.db", "job persistence", OSError("unable to open database file")
    )

    assert "not writable" in message
    assert str(unwritable) in message
    # And it still gives the actionable Docker advice, which is the point of the function.
    assert "chown" in message
