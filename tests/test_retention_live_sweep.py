"""A render must survive the retention sweeper running underneath it.

This file exists because 1911 tests missed a defect that failed **every** job submitted
through the API, and it missed it by a five-second margin.

``RetentionSweeper._loop`` sweeps five seconds after startup. ``JobManager._execute``
creates ``clips/<job_id>/`` and ``temp/<job_id>/`` empty, and they stay empty until the
first clip lands — around twenty seconds into a real render. The directory branch of
``cleanup_expired`` had no age check, so the sweep removed both, and the render then died
at ``geo.replace(final)`` with a ``FileNotFoundError`` that names the *source* path first
and so read as a missing input.

Every API test finished inside five seconds, and every pipeline test calls ``run_pipeline``
directly with no sweeper thread in existence. Neither kind of test could see it. The gap
was structural, not a missing assertion, so the fix needs a test of a shape the suite did
not have: the real app, through its real lifespan, with the sweep actually running while a
job is in flight.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app

try:  # module-level helpers (not fixtures) from the shared conftest
    from tests.conftest import requires_ffmpeg
except ImportError:  # pragma: no cover - conftest always importable under pytest
    from conftest import requires_ffmpeg


def _stub_asr_and_selection(monkeypatch):
    """Replace transcription and selection so the render is fast and offline.

    Deliberately *not* stubbing anything below this: the cut, the geometry pass, the
    directory creation and the ``geo.replace(final)`` that actually broke all run for
    real. Whisper is stubbed because a test must not download a model, and because ASR
    time says nothing about the defect under test.
    """
    import worker.pipeline as pl
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    def fake_transcribe(source, language=None, translate=False, **_kw):
        words = [Word(0.2, 0.6, "hello"), Word(0.7, 1.2, "world")]
        return Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, 3.0, "hello world", words)],
        )

    monkeypatch.setattr(pl, "transcribe", fake_transcribe)
    monkeypatch.setattr(
        pl.sel, "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=3.0, score=50.0, text="hello world")],
    )


class _HammeringSweep:
    """Runs the real sweep repeatedly for the duration of a job.

    The production sweeper's first sweep is at t=5s and its second is at least an hour
    later, so waiting for it would make this test both slow and a coin toss on where the
    render happened to be at that instant. This calls the same ``cleanup_expired`` the
    sweeper thread calls, on the same directories, just far more often — which turns a
    race the suite could not see into one it cannot miss.
    """

    def __init__(self, retention_days: int = 30, period: float = 0.02) -> None:
        self.retention_days = retention_days
        self.period = period
        self.sweeps = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="test-sweep")

    def _loop(self) -> None:
        from storage_backends import retention

        while not self._stop.wait(self.period):
            try:
                retention.cleanup_expired(retention_days=self.retention_days)
                self.sweeps += 1
            except Exception:  # pragma: no cover - defensive, mirrors the real loop
                pass

    def __enter__(self) -> _HammeringSweep:
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _wait_for_terminal(client: TestClient, job_id: str, timeout: float = 180.0) -> dict:
    """Poll the real status endpoint until the job stops, or the timeout expires."""
    deadline = time.monotonic() + timeout
    job: dict = {}
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job.get("status") in ("completed", "failed", "cancelled"):
            return job
        time.sleep(0.1)
    return job


@requires_ffmpeg
def test_a_render_completes_with_the_retention_sweep_running(make_video, monkeypatch):
    """A job submitted through the API finishes while the sweep is active.

    ``TestClient`` as a context manager runs the app's real lifespan, which starts the
    real ``RetentionSweeper`` — that is what makes this reproduce the production failure
    rather than a model of it.

    Against the pre-fix code this fails with the job in ``failed`` and an error of
    ``[Errno 2] No such file or directory: '.../temp/<job>/geo_01_*.mp4' ->
    '.../clips/<job>/clip_01_*.mp4'``.
    """
    from config import settings
    from storage_backends.retention import get_sweeper

    _stub_asr_and_selection(monkeypatch)
    src = make_video("live-sweep.mp4", duration=3.0, w=320, h=180)

    try:
        with TestClient(app) as client, _HammeringSweep() as sweep:
            with src.open("rb") as handle:
                resp = client.post(
                    "/api/upload",
                    files={"files": ("live-sweep.mp4", handle, "video/mp4")},
                    data={
                        "captions": "false",
                        "metadata": "false",
                        "num_clips": "1",
                        "clip_length": "auto",
                        "strategy": "ai",
                    },
                )
            assert resp.status_code == 200, resp.text
            job_id = resp.json()["jobs"][0]["id"]

            job = _wait_for_terminal(client, job_id)

        assert sweep.sweeps > 0, "the sweep never ran, so this proves nothing"
        assert job.get("status") == "completed", (
            f"the render did not survive the sweep: {job.get('error')!r}"
        )
        assert job.get("clips"), "the job completed with no clips"

        clips_dir = Path(settings.clips_dir) / job_id
        assert clips_dir.is_dir(), "the job's clips directory was swept away"
        for clip in job["clips"]:
            assert (clips_dir / clip["filename"]).exists(), clip["filename"]
    finally:
        # The lifespan starts the sweeper and nothing stops it, so without this the
        # thread outlives the test and sweeps under every test that follows.
        get_sweeper().stop()


@requires_ffmpeg
def test_a_sweep_leaves_a_processing_jobs_directories_in_place(make_video, monkeypatch):
    """The narrower invariant, asserted directly: sweep during ``processing``.

    The end-to-end test above proves the whole path, but it can only observe the outcome.
    This one catches the sweep in the act — it blocks the render inside the pipeline, with
    both directories created and still empty, which is the exact state that used to be
    deleted — so a regression is reported as "the directory was removed" rather than as a
    confusing ``FileNotFoundError`` several stages later.
    """
    from config import settings
    from storage_backends import retention
    from worker import pipeline as pl
    from worker.jobs import JobManager, JobStore
    from worker.models import JobStatus, ProcessingOptions

    inside = threading.Event()
    release = threading.Event()
    real_cut = pl.fu.cut_segment

    def blocking_cut(*args, **kwargs):
        inside.set()
        release.wait(30)
        return real_cut(*args, **kwargs)

    _stub_asr_and_selection(monkeypatch)
    monkeypatch.setattr(pl.fu, "cut_segment", blocking_cut)

    src = make_video("mid-flight.mp4", duration=3.0, w=320, h=180)
    manager = JobManager(store=JobStore(persistence=False))
    options = ProcessingOptions(captions=False, metadata=False)
    job = manager.submit("file", str(src), options)

    try:
        assert inside.wait(60), "the render never reached the blocking point"
        clips_dir = Path(settings.clips_dir) / job.id
        temp_dir = Path(settings.temp_dir) / job.id
        assert clips_dir.is_dir() and temp_dir.is_dir()
        assert not any(clips_dir.iterdir()), "precondition: the clips dir is still empty"
        assert manager.store.get(job.id).status == JobStatus.PROCESSING

        result = retention.cleanup_expired(retention_days=30)

        assert clips_dir.is_dir(), "the sweep removed a processing job's clips directory"
        assert temp_dir.is_dir(), "the sweep removed a processing job's temp directory"
        # Nothing on disk here is older than the window, so an honest sweep removes
        # nothing at all. Pinned because ``removed`` was previously not incremented for
        # directories, which is how the deletions stayed invisible.
        assert result["removed"] == 0
    finally:
        release.set()
        for _ in range(600):
            if manager.store.get(job.id).status in (
                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED
            ):
                break
            time.sleep(0.1)
