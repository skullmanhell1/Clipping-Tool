"""Infrastructure hardening: I3 intermediate cache, I5 job resume, M1 golden renders.

I8 (frontend coverage) is by definition covered in ``frontend/src/components/*.test.jsx``.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from evaluation import golden_render as gr
from tests.conftest import FFMPEG, requires_ffmpeg
from worker import intermediate_cache as ic
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions

# --------------------------------------------------------------------------- #
# I3 - the intermediate cache
# --------------------------------------------------------------------------- #


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    from config import settings

    directory = tmp_path / "intermediates"
    monkeypatch.setattr(settings, "intermediate_cache_dir", directory, raising=False)
    monkeypatch.setattr(settings, "intermediate_cache_enabled", True, raising=False)
    return directory


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.bin"
    path.write_bytes(b"some source bytes" * 100)
    return path


def test_i3_a_second_call_does_not_recompute(cache_dir, source):
    calls = []

    def compute():
        calls.append(True)
        return [[1.0, 2.0]]

    first = ic.memoise("silences", source, compute)
    second = ic.memoise("silences", source, compute)
    assert first == second == [[1.0, 2.0]]
    assert len(calls) == 1, "the cached value was not reused"


def test_i3_an_empty_result_is_cached_too(cache_dir, source):
    """ "This file has no detectable silence" is a real and expensive answer.

    Treating it as a miss would re-decode the whole file on every run of exactly the sources where
    the measurement costs most and yields least.
    """
    calls = []

    def compute():
        calls.append(True)
        return []

    assert ic.memoise("silences", source, compute) == []
    assert ic.memoise("silences", source, compute) == []
    assert len(calls) == 1


def test_i3_editing_the_source_is_a_miss(cache_dir, source):
    """Keyed on content, not path and mtime.

    The usual shortcut is wrong in exactly the case that matters: footage re-exported over the same
    filename, which keeps its name and often its size.
    """
    calls = []

    def compute():
        calls.append(True)
        return [len(source.read_bytes())]

    ic.memoise("silences", source, compute)
    source.write_bytes(b"different bytes entirely" * 100)
    ic.memoise("silences", source, compute)
    assert len(calls) == 2


def test_i3_moving_the_source_is_a_hit(cache_dir, source, tmp_path):
    """The same bytes under a new name must not pay again."""
    calls = []

    def compute():
        calls.append(True)
        return [1]

    ic.memoise("silences", source, compute)
    moved = tmp_path / "renamed.bin"
    moved.write_bytes(source.read_bytes())
    ic.memoise("silences", moved, compute)
    assert len(calls) == 1


def test_i3_a_different_parameter_is_a_different_entry(cache_dir, source):
    """A silence map measured at -30 dB is not interchangeable with one measured at -25.

    Keying on the file alone would serve the wrong measurement silently and permanently, which is
    worse than having no cache.
    """
    calls = []

    def compute():
        calls.append(True)
        return [len(calls)]

    ic.memoise("silences", source, compute, {"noise_db": -30})
    ic.memoise("silences", source, compute, {"noise_db": -25})
    assert len(calls) == 2


def test_i3_parameter_order_does_not_change_the_key(cache_dir, source):
    """Otherwise two entries hold identical data under different keys."""
    calls = []

    def compute():
        calls.append(True)
        return [1]

    ic.memoise("x", source, compute, {"a": 1, "b": 2})
    ic.memoise("x", source, compute, {"b": 2, "a": 1})
    assert len(calls) == 1


def test_i3_different_measurements_do_not_collide(cache_dir, source):
    assert ic.memoise("silences", source, lambda: ["s"]) == ["s"]
    assert ic.memoise("envelope", source, lambda: ["e"]) == ["e"]


def test_i3_disabling_the_cache_always_computes(cache_dir, source, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "intermediate_cache_enabled", False, raising=False)
    calls = []
    for _ in range(3):
        ic.memoise("silences", source, lambda: calls.append(True) or [])
    assert len(calls) == 3


def test_i3_an_unreadable_source_computes_rather_than_failing(cache_dir, tmp_path):
    """A cache must never be the reason a job fails."""
    calls = []
    result = ic.memoise("silences", tmp_path / "absent.bin", lambda: calls.append(True) or [7])
    assert result == [7]
    assert len(calls) == 1


def test_i3_a_corrupt_entry_is_a_miss(cache_dir, source):
    calls = []

    def compute():
        calls.append(True)
        return [1]

    ic.memoise("silences", source, compute)
    key = ic.key_for("silences", source, {})
    ic.path_for(key).write_text("{ not json", encoding="utf-8")
    ic.memoise("silences", source, compute)
    assert len(calls) == 2


def test_i3_an_old_schema_is_a_miss(cache_dir, source):
    """An entry written by another build is discarded rather than mis-parsed."""
    key = ic.key_for("silences", source, {})
    ic.path_for(key).parent.mkdir(parents=True, exist_ok=True)
    ic.path_for(key).write_text(json.dumps({"schema": 999, "value": ["stale"]}), encoding="utf-8")
    assert ic.load(key) is None


def test_i3_writes_leave_no_temporary_files(cache_dir, source):
    ic.memoise("silences", source, lambda: [1])
    assert not list(cache_dir.glob("*.tmp"))


def test_i3_pruning_keeps_the_newest(cache_dir, source, tmp_path):
    """An unbounded cache of whole-file measurements is a slow disk leak."""
    for index in range(8):
        other = tmp_path / f"src{index}.bin"
        other.write_bytes(f"content {index}".encode() * 50)
        ic.memoise("silences", other, lambda i=index: [i])
        # Distinct mtimes, so "oldest" is well defined.
        time.sleep(0.01)

    assert len(list(cache_dir.glob("*.json"))) == 8
    removed = ic.prune(max_entries=3)
    assert removed == 5
    assert len(list(cache_dir.glob("*.json"))) == 3


def test_i3_pruning_can_be_disabled(cache_dir, source):
    ic.memoise("silences", source, lambda: [1])
    assert ic.prune(max_entries=0) == 0
    assert len(list(cache_dir.glob("*.json"))) == 1


def test_i3_frames_dir_is_per_source_and_per_parameter(cache_dir, source, tmp_path):
    """Two sources sharing a frames directory would mix one video's frames into another's
    selection - a wrong answer that looks entirely plausible."""
    other = tmp_path / "other.bin"
    other.write_bytes(b"totally different" * 60)
    a = ic.frames_dir_for(source, {"limit": 12})
    b = ic.frames_dir_for(other, {"limit": 12})
    c = ic.frames_dir_for(source, {"limit": 48})
    assert a != b
    assert a != c
    assert a == ic.frames_dir_for(source, {"limit": 12})


def test_i3_frames_dir_is_none_when_disabled(cache_dir, source, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "intermediate_cache_enabled", False, raising=False)
    assert ic.frames_dir_for(source, {}) is None


@requires_ffmpeg
def test_i3_an_existing_keyframe_is_not_extracted_again(tmp_path, make_video):
    """The line that makes the frames cache worth having.

    Without it the cache hands back a directory of correct frames and then overwrites every one of
    them, paying the seeks it was supposed to save.
    """
    from worker.visual_selection import sample_keyframes

    video = make_video("src.mp4", duration=4.0, w=320, h=240)
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()

    first = sample_keyframes(video, 4.0, limit=3, frames_dir=str(frames_dir))
    assert first
    stamps = {Path(f.path): Path(f.path).stat().st_mtime_ns for f in first}

    second = sample_keyframes(video, 4.0, limit=3, frames_dir=str(frames_dir))
    assert [f.path for f in second] == [f.path for f in first]
    for path, mtime in stamps.items():
        assert path.stat().st_mtime_ns == mtime, f"{path.name} was re-extracted"


@requires_ffmpeg
def test_i3_a_zero_byte_frame_is_treated_as_missing(tmp_path, make_video):
    """A run killed mid-write leaves an empty file, and reusing it would corrupt the signal."""
    from worker.visual_selection import sample_keyframes

    video = make_video("src.mp4", duration=4.0, w=320, h=240)
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    frames = sample_keyframes(video, 4.0, limit=2, frames_dir=str(frames_dir))
    truncated = Path(frames[0].path)
    truncated.write_bytes(b"")

    again = sample_keyframes(video, 4.0, limit=2, frames_dir=str(frames_dir))
    assert Path(again[0].path).stat().st_size > 0


# --------------------------------------------------------------------------- #
# I5 - resuming a partially-completed job
# --------------------------------------------------------------------------- #


def _clip(index: int, start: float, end: float) -> ClipResult:
    return ClipResult(
        id=f"{index:02d}_x",
        filename=f"clip_{index:02d}.mp4",
        start=start,
        end=end,
        duration=end - start,
    )


def _planned(*windows) -> list[dict]:
    return [{"start": s, "end": e, "reason": "", "score": 0.0} for s, e in windows]


def test_i5_the_plan_survives_serialisation():
    job = Job(
        input_type="file",
        source="a.mp4",
        options=ProcessingOptions(),
        planned_clips=_planned((0.0, 5.0), (10.0, 15.0)),
    )
    assert len(Job.from_dict(job.to_dict()).planned_clips) == 2


def test_i5_missing_windows_are_the_ones_without_a_clip():
    from worker.jobs import JobManager

    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.planned_clips = _planned((0.0, 5.0), (10.0, 15.0), (20.0, 25.0))
    job.clips = [_clip(1, 0.0, 5.0)]

    missing = JobManager._missing_windows(JobManager.__new__(JobManager), job)
    assert [(c.start, c.end) for c in missing] == [(10.0, 15.0), (20.0, 25.0)]


def test_i5_a_trimmed_clip_still_matches_its_window():
    """AU7 silence trimming and S9 cut snapping both move a clip's boundaries after the plan is
    recorded, so exact equality would re-render clips that already exist."""
    from worker.jobs import JobManager

    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.planned_clips = _planned((10.0, 20.0))
    job.clips = [_clip(1, 10.4, 19.7)]
    assert JobManager._missing_windows(JobManager.__new__(JobManager), job) is None


def test_i5_a_genuinely_different_moment_is_not_confused_with_a_trim():
    from worker.jobs import JobManager

    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.planned_clips = _planned((10.0, 20.0))
    job.clips = [_clip(1, 40.0, 50.0)]
    missing = JobManager._missing_windows(JobManager.__new__(JobManager), job)
    assert [(c.start, c.end) for c in missing] == [(10.0, 20.0)]


def test_i5_no_plan_means_nothing_to_resume():
    """A job interrupted before selection finished genuinely has to start over."""
    from worker.jobs import JobManager

    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    assert JobManager._missing_windows(JobManager.__new__(JobManager), job) is None


def test_i5_a_complete_job_has_nothing_missing():
    from worker.jobs import JobManager

    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.planned_clips = _planned((0.0, 5.0), (10.0, 15.0))
    job.clips = [_clip(1, 0.0, 5.0), _clip(2, 10.0, 15.0)]
    assert JobManager._missing_windows(JobManager.__new__(JobManager), job) is None


def test_i5_a_malformed_plan_entry_is_skipped_not_fatal():
    from worker.jobs import JobManager

    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.planned_clips = [{"start": "nonsense"}, {"start": 5.0, "end": 9.0}]
    missing = JobManager._missing_windows(JobManager.__new__(JobManager), job)
    assert [(c.start, c.end) for c in missing] == [(5.0, 9.0)]


def test_i5_an_interrupted_job_says_it_can_be_resumed(tmp_path):
    """The message has to distinguish the two cases: telling someone to re-submit a job whose
    finished clips are on disk costs them the whole render a second time."""
    from worker.job_persistence import Job_Persistence

    store = Job_Persistence(tmp_path / "jobs.db")
    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.status = JobStatus.PROCESSING
    job.planned_clips = _planned((0.0, 5.0), (10.0, 15.0), (20.0, 25.0))
    job.clips = [_clip(1, 0.0, 5.0)]
    store.save(job)

    restored = next(j for j in store.load_all() if j.id == job.id)
    assert restored.status is JobStatus.FAILED
    assert "1 of 3" in restored.error
    assert "resume" in restored.error.lower()


def test_i5_an_interrupted_job_with_no_plan_says_to_resubmit(tmp_path):
    from worker.job_persistence import Job_Persistence

    store = Job_Persistence(tmp_path / "jobs.db")
    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.status = JobStatus.PROCESSING
    store.save(job)

    restored = next(j for j in store.load_all() if j.id == job.id)
    assert "Re-submit" in restored.error
    assert "resume" not in restored.error.lower()


def test_i5_resume_refuses_a_running_job():
    from worker.jobs import JobManager, JobStore

    manager = JobManager.__new__(JobManager)
    manager.store = JobStore()
    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.status = JobStatus.PROCESSING
    job.planned_clips = _planned((0.0, 5.0))
    manager.store.add(job)
    assert manager.resume(job.id) is False


def test_i5_resume_refuses_an_unknown_job():
    from worker.jobs import JobManager, JobStore

    manager = JobManager.__new__(JobManager)
    manager.store = JobStore()
    assert manager.resume("no-such-job") is False


@pytest.fixture
def resume_client(monkeypatch):
    from fastapi.testclient import TestClient

    import api.main as main
    from worker.jobs import JobStore

    store = JobStore()

    class _Manager:
        def __init__(self):
            self.store = store
            self.resumed: list[str] = []

        def resume(self, job_id):
            self.resumed.append(job_id)
            return True

    manager = _Manager()
    monkeypatch.setattr(main, "get_manager", lambda: manager)
    return TestClient(main.app), store, manager


def _failed_job(store, *, planned, clips=()):
    job = Job(input_type="file", source="a.mp4", options=ProcessingOptions())
    job.status = JobStatus.FAILED
    job.planned_clips = planned
    job.clips = list(clips)
    store.add(job)
    return job


def test_i5_the_resume_endpoint_queues_the_job(resume_client):
    client, store, manager = resume_client
    job = _failed_job(store, planned=_planned((0.0, 5.0), (10.0, 15.0)), clips=[_clip(1, 0.0, 5.0)])
    response = client.post(f"/api/jobs/{job.id}/resume")
    assert response.status_code == 200
    assert manager.resumed == [job.id]


def test_i5_resuming_an_unknown_job_is_a_404(resume_client):
    client, _store, _manager = resume_client
    assert client.post("/api/jobs/nope/resume").status_code == 404


def test_i5_resuming_a_completed_job_is_refused(resume_client):
    client, store, _manager = resume_client
    job = _failed_job(store, planned=_planned((0.0, 5.0)))
    job.status = JobStatus.COMPLETED
    response = client.post(f"/api/jobs/{job.id}/resume")
    assert response.status_code == 409
    assert "completed" in response.json()["detail"]


def test_i5_resuming_a_job_with_no_plan_explains_why_not(resume_client):
    """Rather than silently starting the full re-run the caller was trying to avoid."""
    client, store, _manager = resume_client
    job = _failed_job(store, planned=[])
    response = client.post(f"/api/jobs/{job.id}/resume")
    assert response.status_code == 409
    assert "before it chose its clips" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# M1 - golden-output rendering
# --------------------------------------------------------------------------- #


@pytest.fixture
def golden_clips(tmp_path):
    """A base clip plus three variants: re-encoded, caption bar burned in, and graded."""
    base = tmp_path / "base.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x568:rate=25:duration=2",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-crf",
            "20",
            str(base),
        ],
        check=True,
        capture_output=True,
    )
    variants = {"base": base}
    for name, args in (
        ("recrf", ["-crf", "34"]),
        ("captioned", ["-vf", "drawbox=x=0:y=h-120:w=iw:h=90:color=black@0.75:t=fill"]),
        ("graded", ["-vf", "eq=contrast=1.7:saturation=1.8"]),
        # A purely *structural* change: a mirrored frame has an identical luma mean and spread, so
        # only the bit distance can see it.
        ("flipped", ["-vf", "hflip"]),
    ):
        out = tmp_path / f"{name}.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-y",
                "-i",
                str(base),
                *args,
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        variants[name] = out
    return variants


@requires_ffmpeg
def test_m1_an_identical_render_matches_its_golden(golden_clips):
    frames = gr.hash_frames(golden_clips["base"], 2.0, count=4)
    assert frames, "no frames were hashed"
    assert gr.compare(frames, [f.to_dict() for f in frames]).ok


@requires_ffmpeg
def test_m1_a_re_encode_still_matches(golden_clips):
    """The reason this is perceptual rather than an exact frame hash.

    An exact hash is reproducible for one ffmpeg build only, so a golden of exact hashes fails on
    every upgrade, gets re-frozen without inspection, and stops meaning anything - the failure mode
    a golden exists to prevent.
    """
    golden = [f.to_dict() for f in gr.hash_frames(golden_clips["base"], 2.0, count=4)]
    result = gr.compare(gr.hash_frames(golden_clips["recrf"], 2.0, count=4), golden)
    assert result.ok, result.summary()


@requires_ffmpeg
def test_m1_a_burned_in_caption_bar_is_caught(golden_clips):
    """Captions vanishing is the regression this exists to catch, and it must be caught **twice**.

    The bit distance has to exceed the tolerance on its own, not merely in combination with the
    luma shift the bar also causes. Asserting only ``not result.ok`` leaves the bit tolerance free
    to drift wide - checked by widening it to 40, which the weaker assertion did not notice.
    """
    golden = [f.to_dict() for f in gr.hash_frames(golden_clips["base"], 2.0, count=4)]
    result = gr.compare(gr.hash_frames(golden_clips["captioned"], 2.0, count=4), golden)
    assert not result.ok
    assert result.worst > gr.DEFAULT_TOLERANCE, (
        f"a caption bar moved only {result.worst} bits, which the tolerance "
        f"{gr.DEFAULT_TOLERANCE} would absorb"
    )


@requires_ffmpeg
def test_m1_a_colour_grade_change_is_caught(golden_clips):
    """The case that needed a second signal.

    An average hash compares each cell to the frame's own mean, so it is invariant to contrast by
    construction: the graded clip moved 2 bits, inside the tolerance needed for encoder noise. The
    mean did not separate them either (0.47 versus 0.06 for a re-encode). The luma *spread* does:
    49.5 unchanged versus 75.5 graded.
    """
    golden = [f.to_dict() for f in gr.hash_frames(golden_clips["base"], 2.0, count=4)]
    result = gr.compare(gr.hash_frames(golden_clips["graded"], 2.0, count=4), golden)
    assert not result.ok
    assert max(entry["spread_shift"] for entry in result.detail) > gr.DEFAULT_MEAN_TOLERANCE


@requires_ffmpeg
def test_m1_a_purely_structural_change_is_caught_by_the_bit_distance(golden_clips):
    """Isolates the structural signal, which nothing else here pins.

    The caption-bar test above passes even with an absurd bit tolerance, because the caption also
    shifts the luma mean - so it is really testing the luma check. A mirrored frame has an
    *identical* mean and spread (measured: 0.03 and 0.04 apart) and differs by all 64 bits, so it
    fails only if the bit distance is doing its job.
    """
    golden = [f.to_dict() for f in gr.hash_frames(golden_clips["base"], 2.0, count=4)]
    frames = gr.hash_frames(golden_clips["flipped"], 2.0, count=4)
    result = gr.compare(frames, golden)
    assert not result.ok, result.summary()
    assert result.worst > gr.DEFAULT_TOLERANCE
    # And the luma signals really are blind to it, which is what makes this test isolating.
    assert max(entry["mean_shift"] for entry in result.detail) < gr.DEFAULT_MEAN_TOLERANCE
    assert max(entry["spread_shift"] for entry in result.detail) < gr.DEFAULT_MEAN_TOLERANCE


@requires_ffmpeg
def test_m1_a_truncated_render_is_caught(golden_clips):
    """Comparing only the overlap would report a render that produced fewer frames as a pass."""
    frames = gr.hash_frames(golden_clips["base"], 2.0, count=4)
    assert not gr.compare(frames[:2], [f.to_dict() for f in frames]).ok


@requires_ffmpeg
def test_m1_the_golden_file_round_trips(tmp_path, golden_clips):
    frames = gr.hash_frames(golden_clips["base"], 2.0, count=3)
    path = gr.write_golden(tmp_path / "g.json", "base", frames, notes="a note")
    loaded = gr.load_golden(path)
    assert loaded is not None
    assert gr.compare(frames, loaded).ok
    payload = json.loads(path.read_text(encoding="utf-8"))
    # The grid size is recorded, so changing it invalidates the golden rather than silently
    # comparing hashes of different shapes.
    assert payload["grid"] == gr.HASH_GRID
    assert payload["notes"] == "a note"


def test_m1_a_missing_golden_reads_as_none(tmp_path):
    assert gr.load_golden(tmp_path / "absent.json") is None


def test_m1_a_corrupt_golden_reads_as_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not json", encoding="utf-8")
    assert gr.load_golden(path) is None


def test_m1_distance_counts_differing_bits():
    assert gr.distance("0", "0") == 0
    assert gr.distance("1", "0") == 1
    assert gr.distance("f", "0") == 4


@requires_ffmpeg
def test_m1_an_unreadable_frame_is_skipped_not_fatal(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    assert gr.hash_frames(broken, 2.0, count=3) == []
    assert gr.average_hash(broken, 1.0) is None
