"""Tests for disk usage, sidecar metadata, and the retention sweep."""
from __future__ import annotations

import os
import time

from storage_backends import retention


def test_disk_usage_keys():
    usage = retention.disk_usage()
    for key in ("total_bytes", "used_bytes", "free_bytes", "used_percent",
                "free_gb", "areas", "low_space"):
        assert key in usage
    assert set(usage["areas"]) == {"clips", "uploads", "temp"}


def test_disk_usage_low_space_flag():
    # An absurd free-space threshold forces the warning on.
    assert retention.disk_usage(warn_free_gb=10**9)["low_space"] is True


def test_sidecar_write_and_path(tmp_path):
    clip = tmp_path / "clip_01.mp4"
    clip.write_bytes(b"x")

    class Clip:
        def to_dict(self):
            return {"title": "Hello", "hashtags": ["#a"], "score": 90}

    dest = retention.write_sidecar(clip, Clip())
    assert dest == tmp_path / "clip_01.json"
    assert dest.exists()
    import json
    data = json.loads(dest.read_text())
    assert data["title"] == "Hello"
    assert "saved_at" in data


def test_cleanup_expired_removes_old_clips_not_sources(tmp_path, monkeypatch):
    from config import settings

    root = tmp_path / "storage"
    (root / "clips" / "job1").mkdir(parents=True)
    (root / "uploads").mkdir(parents=True)
    (root / "temp").mkdir(parents=True)

    old_clip = root / "clips" / "job1" / "old.mp4"
    new_clip = root / "clips" / "job1" / "new.mp4"
    source = root / "uploads" / "source.mp4"
    for p in (old_clip, new_clip, source):
        p.write_bytes(b"data")

    # Age the old clip AND the source well beyond the window.
    old_time = time.time() - 40 * 86400
    os.utime(old_clip, (old_time, old_time))
    os.utime(source, (old_time, old_time))

    monkeypatch.setattr(settings, "storage_root", root)
    monkeypatch.setattr(settings, "clips_dir", root / "clips")
    monkeypatch.setattr(settings, "temp_dir", root / "temp")
    monkeypatch.setattr(settings, "uploads_dir", root / "uploads")

    result = retention.cleanup_expired(retention_days=30)
    assert result["removed"] == 1
    assert not old_clip.exists()
    assert new_clip.exists()
    # The source is old but must NEVER be auto-deleted.
    assert source.exists()


def test_cleanup_expired_keep_forever_is_noop(tmp_path, monkeypatch):
    from config import settings

    root = tmp_path / "storage"
    (root / "clips").mkdir(parents=True)
    old = root / "clips" / "old.mp4"
    old.write_bytes(b"data")
    old_time = time.time() - 999 * 86400
    os.utime(old, (old_time, old_time))
    monkeypatch.setattr(settings, "storage_root", root)
    monkeypatch.setattr(settings, "clips_dir", root / "clips")
    monkeypatch.setattr(settings, "temp_dir", root / "temp")

    result = retention.cleanup_expired(retention_days=0)
    assert result["kept_forever"] is True
    assert result["removed"] == 0
    assert old.exists()


def test_cleanup_temp(tmp_path, monkeypatch):
    from config import settings

    temp = tmp_path / "temp"
    (temp / "job1").mkdir(parents=True)
    (temp / "job1" / "scratch.tmp").write_bytes(b"x")
    monkeypatch.setattr(settings, "temp_dir", temp)

    assert retention.cleanup_temp("job1") == 1
    assert not (temp / "job1").exists()
