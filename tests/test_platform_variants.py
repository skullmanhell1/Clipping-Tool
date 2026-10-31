"""Platform variant delivery: one selected moment, several destination files."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config import settings
from publishers.history import HistoryStore
from publishers.manager import PublishManager
from tests.fakes import FakePublisher
from worker import rerender
from worker.jobs import get_manager
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions


@pytest.fixture
def client():
    return TestClient(app)


def _job_and_clip():
    job = Job(
        input_type="file",
        source="source.mp4",
        options=ProcessingOptions(aspect="9:16", output_platform="tiktok"),
    )
    clip = ClipResult(
        id="variant-clip",
        filename="clip_variant.mp4",
        start=1.0,
        end=7.0,
        duration=6.0,
        title="A selected moment",
        video_url=f"clips/{job.id}/clip_variant.mp4",
    )
    job.clips = [clip]
    job.status = JobStatus.COMPLETED
    get_manager().store.add(job)

    clip_dir = Path(settings.clips_dir) / job.id
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / clip.filename).write_bytes(b"BASE-VIDEO")
    return job, clip, clip_dir


def _variant(filename="clip_variant_youtube.mp4", platform="youtube"):
    return {
        "platform": platform,
        "label": "YouTube landscape",
        "filename": filename,
        "video_url": f"clips/job/{filename}",
        "thumbnail_url": "",
        "aspect": "16:9",
        "width": 1920,
        "height": 1080,
        "short_side": 1080,
        "max_duration_s": 43200.0,
        "duration": 6.0,
        "start": 1.0,
        "end": 7.0,
    }


def test_variant_filename_is_stable_and_filesystem_safe():
    assert rerender.variant_filename("clip_01.mp4", "youtube_shorts") == (
        "clip_01_youtube_shorts.mp4"
    )
    assert rerender.variant_filename("clip_01.mp4", "../../TikTok") == "clip_01_tiktok.mp4"
    assert rerender.variant_filename("clip_01", "x") == "clip_01_x.mp4"


def test_platform_variant_forces_the_destination_aspect_and_resolution(monkeypatch):
    job, clip, _ = _job_and_clip()
    captured = {}

    def fake_rerender(job_arg, clip_arg, **kwargs):
        captured.update(kwargs)
        filename = kwargs["output_filename"]
        return ClipResult(
            id=clip_arg.id,
            filename=filename,
            start=clip_arg.start,
            end=clip_arg.end,
            duration=6.0,
            video_url=f"clips/{job_arg.id}/{filename}",
            thumbnail_url=f"clips/{job_arg.id}/clip_variant_youtube.jpg",
        )

    monkeypatch.setattr(rerender, "rerender_clip", fake_rerender)
    record = rerender.render_platform_variant(
        job,
        clip,
        "youtube",
        option_overrides={"color": "vivid"},
    )

    overrides = captured["option_overrides"]
    assert overrides["color"] == "vivid"
    assert overrides["aspect"] == "16:9"
    assert overrides["output_platform"] == "youtube"
    assert overrides["output_short_side"] == 1080
    assert captured["output_filename"] == "clip_variant_youtube.mp4"
    assert record["label"] == "YouTube landscape"
    assert (record["width"], record["height"]) == (1920, 1080)
    assert record["filename"] == "clip_variant_youtube.mp4"


def test_variant_endpoint_stores_the_record_without_replacing_the_clip(client, monkeypatch):
    job, clip, _ = _job_and_clip()
    record = _variant()
    captured = {}

    def fake_variant(job_arg, clip_arg, platform, **kwargs):
        captured.update(job=job_arg.id, clip=clip_arg.id, platform=platform, **kwargs)
        return record

    monkeypatch.setattr(rerender, "render_platform_variant", fake_variant)
    response = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/variants",
        json={"platform": "youtube", "settings": {"color": "vivid"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == clip.filename
    assert body["variants"] == [record]
    assert captured["platform"] == "youtube"
    assert captured["option_overrides"] == {"color": "vivid"}
    assert get_manager().store.get_clip(job.id, clip.id).variants == [record]


def test_variant_endpoint_rejects_an_unknown_destination(client):
    job, clip, _ = _job_and_clip()
    response = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/variants",
        json={"platform": "myspace"},
    )
    assert response.status_code == 400
    assert "unknown platform variant" in response.json()["detail"].lower()


def test_a_canonical_rerender_clears_stale_platform_variants(client, monkeypatch):
    """A changed base render invalidates every old destination file."""
    job, clip, _ = _job_and_clip()
    record = _variant()
    get_manager().store.update_clip(job.id, clip.id, {"variants": [record]})
    fresh = ClipResult.from_dict({**clip.to_dict(), "variants": []})

    monkeypatch.setattr(rerender, "rerender_clip", lambda *_args, **_kwargs: fresh)
    response = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/rerender",
        json={"settings": {"color": "cool"}},
    )

    assert response.status_code == 200
    assert response.json()["variants"] == []


def test_variant_video_and_package_routes_are_owned_by_the_clip(client):
    job, clip, clip_dir = _job_and_clip()
    record = _variant(filename="clip_variant_youtube.mp4")
    (clip_dir / record["filename"]).write_bytes(b"VARIANT-VIDEO")
    get_manager().store.update_clip(job.id, clip.id, {"variants": [record]})

    video = client.get(f"/api/clips/{job.id}/{record['filename']}/video")
    assert video.status_code == 200
    assert video.content == b"VARIANT-VIDEO"

    package = client.get(f"/api/clips/{job.id}/{record['filename']}/download")
    assert package.status_code == 200
    assert package.headers["content-type"] == "application/zip"


def _publish_manager(tmp_path, *publishers):
    store = HistoryStore(tmp_path / "publish.db")
    manager = PublishManager(
        publishers={publisher.name: publisher for publisher in publishers},
        history=store,
        autostart=False,
    )
    return manager, store


def test_publish_manager_uses_the_matching_platform_variant(tmp_path):
    clip_dir = tmp_path / "job"
    clip_dir.mkdir()
    canonical = clip_dir / "clip_variant.mp4"
    canonical.write_bytes(b"BASE")
    variant_file = clip_dir / "clip_variant_youtube_shorts.mp4"
    variant_file.write_bytes(b"SHORTS")
    clip = ClipResult(
        id="variant-clip",
        filename=canonical.name,
        start=1.0,
        end=7.0,
        duration=6.0,
        variants=[
            {
                "platform": "youtube_shorts",
                "filename": variant_file.name,
                "aspect": "9:16",
                "width": 1080,
                "height": 1920,
            }
        ],
    )
    manager, store = _publish_manager(tmp_path, FakePublisher("youtube"))

    ids = manager.submit(
        job_id="job",
        clip=clip,
        video_path=canonical,
        platforms=["youtube_shorts"],
        mode="review",
    )

    request = store.get_attempt(ids[0])["request_json"]
    assert request["video_path"] == str(variant_file)
    assert request["delivery"] == {
        "kind": "variant",
        "platform": "youtube_shorts",
        "requested_platform": "youtube_shorts",
        "filename": variant_file.name,
        "aspect": "9:16",
        "width": 1080,
        "height": 1920,
    }


def test_publish_manager_does_not_borrow_a_different_platform_variant(tmp_path):
    clip_dir = tmp_path / "job"
    clip_dir.mkdir()
    canonical = clip_dir / "clip_variant.mp4"
    canonical.write_bytes(b"BASE")
    (clip_dir / "clip_variant_shorts.mp4").write_bytes(b"SHORTS")
    clip = ClipResult(
        id="variant-clip",
        filename=canonical.name,
        start=1.0,
        end=7.0,
        duration=6.0,
        variants=[{"platform": "youtube_shorts", "filename": "clip_variant_shorts.mp4"}],
    )
    manager, store = _publish_manager(tmp_path, FakePublisher("youtube"))

    ids = manager.submit(
        job_id="job",
        clip=clip,
        video_path=canonical,
        platforms=["youtube"],
        mode="review",
    )

    request = store.get_attempt(ids[0])["request_json"]
    assert request["video_path"] == str(canonical)
    assert request["delivery"]["kind"] == "canonical"


def test_publish_manager_falls_back_when_a_variant_file_is_gone(tmp_path):
    clip_dir = tmp_path / "job"
    clip_dir.mkdir()
    canonical = clip_dir / "clip_variant.mp4"
    canonical.write_bytes(b"BASE")
    clip = ClipResult(
        id="variant-clip",
        filename=canonical.name,
        start=1.0,
        end=7.0,
        duration=6.0,
        variants=[{"platform": "tiktok", "filename": "clip_variant_tiktok.mp4"}],
    )
    manager, store = _publish_manager(tmp_path, FakePublisher("tiktok"))

    ids = manager.submit(
        job_id="job",
        clip=clip,
        video_path=canonical,
        platforms=["tiktok"],
        mode="review",
    )

    request = store.get_attempt(ids[0])["request_json"]
    assert request["video_path"] == str(canonical)
    assert request["delivery"] == {
        "kind": "canonical",
        "platform": "tiktok",
        "filename": canonical.name,
        "fallback": "variant_file_missing",
        "variant_filename": "clip_variant_tiktok.mp4",
    }


def test_clip_result_round_trips_variants():
    record = _variant()
    restored = ClipResult.from_dict(
        {
            "id": "c1",
            "filename": "clip.mp4",
            "start": 0,
            "end": 1,
            "duration": 1,
            "variants": [record],
        }
    )
    assert restored.variants == [record]
