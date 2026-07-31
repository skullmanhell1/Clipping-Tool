"""Tests for disk usage, sidecar metadata, and the retention sweep."""

from __future__ import annotations

import os
import time

from storage_backends import retention


def test_disk_usage_keys():
    usage = retention.disk_usage()
    for key in (
        "total_bytes",
        "used_bytes",
        "free_bytes",
        "used_percent",
        "free_gb",
        "areas",
        "low_space",
    ):
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


# --------------------------------------------------------------------------- #
# Directory sizing: correctness, and not re-walking on every poll               #
# --------------------------------------------------------------------------- #
def test_dir_size_totals_files_recursively(tmp_path):
    """The scandir-based walk sums nested files correctly.

    ``_dir_size`` was rewritten from ``rglob`` + ``stat`` to ``os.scandir`` for speed, so
    its arithmetic is worth pinning independently of the caching.
    """
    from storage_backends.retention import _dir_size

    (tmp_path / "nested" / "deeper").mkdir(parents=True)
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "nested" / "b.bin").write_bytes(b"y" * 250)
    (tmp_path / "nested" / "deeper" / "c.bin").write_bytes(b"z" * 5)

    assert _dir_size(tmp_path) == 355


def test_dir_size_of_a_missing_directory_is_zero(tmp_path):
    """A storage area that does not exist yet reads as empty, not as an error."""
    from storage_backends.retention import _dir_size

    assert _dir_size(tmp_path / "not-created") == 0


def test_dir_size_does_not_follow_directory_symlinks(tmp_path):
    """A symlinked directory is not descended into.

    Otherwise a link pointing at a large tree — or at an ancestor — would make the walk
    unbounded or send it round a loop.
    """
    import os

    from storage_backends.retention import _dir_size

    real = tmp_path / "real"
    real.mkdir()
    (real / "f.bin").write_bytes(b"x" * 10)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "big.bin").write_bytes(b"y" * 9999)

    os.symlink(elsewhere, real / "link")

    assert _dir_size(real) == 10


def test_area_sizes_are_cached_between_calls(tmp_path, monkeypatch):
    """Repeated reads do not re-walk the storage tree.

    The storage panel polls ``/api/storage``, and each call previously walked clips/,
    uploads/ and temp/ in full. Directory sizes are a rough gauge, so a short cache
    removes a whole filesystem traversal from a request path.
    """
    from storage_backends import retention

    retention.invalidate_disk_usage_cache()
    monkeypatch.setattr(retention.settings, "disk_usage_cache_seconds", 60.0)

    walks = []
    real_dir_size = retention._dir_size

    def counting_dir_size(path):
        walks.append(str(path))
        return real_dir_size(path)

    monkeypatch.setattr(retention, "_dir_size", counting_dir_size)

    retention.disk_usage()
    first = len(walks)
    assert first == 3, "expected one walk per storage area"

    retention.disk_usage()
    retention.disk_usage()
    assert len(walks) == first, "the cached sizes were recomputed"


def test_refresh_forces_a_recompute(tmp_path, monkeypatch):
    """``refresh=True`` bypasses the cache, which the cleanup endpoint relies on.

    After deleting files, reporting the cached totals would show the pre-cleanup sizes and
    make cleanup look like it did nothing.
    """
    from storage_backends import retention

    retention.invalidate_disk_usage_cache()
    monkeypatch.setattr(retention.settings, "disk_usage_cache_seconds", 60.0)

    walks = []
    real_dir_size = retention._dir_size
    monkeypatch.setattr(
        retention,
        "_dir_size",
        lambda path: (walks.append(str(path)), real_dir_size(path))[1],
    )

    retention.disk_usage()
    assert len(walks) == 3

    retention.disk_usage(refresh=True)
    assert len(walks) == 6, "refresh=True did not recompute the area sizes"


def test_a_zero_ttl_disables_caching(monkeypatch):
    """0 is the documented opt-out for operators who want live numbers."""
    from storage_backends import retention

    retention.invalidate_disk_usage_cache()
    monkeypatch.setattr(retention.settings, "disk_usage_cache_seconds", 0.0)

    walks = []
    real_dir_size = retention._dir_size
    monkeypatch.setattr(
        retention,
        "_dir_size",
        lambda path: (walks.append(str(path)), real_dir_size(path))[1],
    )

    retention.disk_usage()
    retention.disk_usage()
    assert len(walks) == 6


def test_the_volume_figures_are_never_cached(monkeypatch):
    """Free/total space is a single cheap syscall and must always be current.

    Caching it would let a "low space" warning stay hidden for the TTL, which is the one
    number that must not lag.
    """
    from storage_backends import retention

    retention.invalidate_disk_usage_cache()
    monkeypatch.setattr(retention.settings, "disk_usage_cache_seconds", 60.0)

    calls = []
    real_usage = retention.shutil.disk_usage
    monkeypatch.setattr(
        retention.shutil,
        "disk_usage",
        lambda p: (calls.append(str(p)), real_usage(p))[1],
    )

    retention.disk_usage()
    retention.disk_usage()
    assert len(calls) == 2
