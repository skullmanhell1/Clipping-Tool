"""Focused tests for starting a fresh dashboard project."""

from __future__ import annotations

from types import SimpleNamespace

from publishers.history import HistoryStore
from worker.job_persistence import Job_Persistence
from worker.jobs import JobStore
from worker.models import Job, JobStatus, ProcessingOptions
from worker.project_cleanup import cleanup_for_new_project


def _job(source: str, *, status: JobStatus) -> Job:
    return Job(input_type="file", source=source, options=ProcessingOptions(), status=status)


def test_new_project_removes_past_state_but_protects_active_jobs_and_sources(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    clips = root / "clips"
    temp = root / "temp"
    uploads = root / "uploads"
    for directory in (clips, temp, uploads):
        directory.mkdir(parents=True)
    monkeypatch.setattr("worker.project_cleanup.settings.clips_dir", clips)
    monkeypatch.setattr("worker.project_cleanup.settings.temp_dir", temp)
    monkeypatch.setattr("worker.project_cleanup.settings.uploads_dir", uploads)

    old = _job(str(uploads / "old.mp4"), status=JobStatus.COMPLETED)
    active = _job(str(uploads / "active.mp4"), status=JobStatus.PROCESSING)
    persistence = Job_Persistence(root / "jobs.db")
    store = JobStore(persistence=persistence)
    assert store.add(old)
    assert store.add(active)

    history = HistoryStore(root / "history.db")
    clip = SimpleNamespace(
        id="clip1",
        filename="clip1.mp4",
        title="old",
        description="old",
        hashtags=[],
        score=1,
    )
    history.record_clip(old.id, clip, clips / old.id / clip.filename)
    history.create_attempt(
        job_id=old.id,
        clip_id=clip.id,
        platform="youtube",
        request={},
        scheduled_at=0,
        state="published",
    )
    history.record_clip(active.id, clip, clips / active.id / clip.filename)
    campaign = history.save_campaign("Keep me", {"youtube": {"account_id": "channel"}})

    (clips / old.id).mkdir()
    (clips / old.id / "clip1.mp4").write_bytes(b"clip")
    (temp / old.id).mkdir()
    (temp / old.id / "scratch").write_bytes(b"temp")
    (clips / active.id).mkdir()
    (clips / active.id / "live.mp4").write_bytes(b"live")
    source = uploads / "old.mp4"
    source.write_bytes(b"source")

    result = cleanup_for_new_project(store, history)

    assert result["performed"] is True
    assert result["removed_jobs"] == 1
    assert result["protected_active_jobs"] == [active.id]
    assert store.get(old.id) is None
    assert store.get(active.id) is active
    assert persistence.count() == 1
    assert not (clips / old.id).exists()
    assert not (temp / old.id).exists()
    assert (clips / active.id).exists()
    assert source.exists(), "fresh-project cleanup must not delete source uploads"

    current = history.history()
    assert [row["job_id"] for row in current["clips"]] == [active.id]
    assert current["publish_attempts"] == []
    assert history.campaign(campaign.id) is not None


def test_cleanup_refuses_symlinked_job_storage(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    clips = root / "clips"
    temp = root / "temp"
    uploads = root / "uploads"
    for directory in (clips, temp, uploads):
        directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_bytes(b"keep")
    (clips / "unsafe").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr("worker.project_cleanup.settings.clips_dir", clips)
    monkeypatch.setattr("worker.project_cleanup.settings.temp_dir", temp)
    monkeypatch.setattr("worker.project_cleanup.settings.uploads_dir", uploads)

    store = JobStore(persistence=False)
    job = _job(str(uploads / "source.mp4"), status=JobStatus.COMPLETED)
    job.id = "unsafe"
    assert store.add(job)
    history = HistoryStore(root / "history.db")

    cleanup_for_new_project(store, history)

    assert sentinel.exists()
    assert (clips / "unsafe").is_symlink()
