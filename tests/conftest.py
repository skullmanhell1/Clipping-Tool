"""Shared pytest fixtures and environment isolation for Phase 3 tests.

Storage and the history database are redirected into a per-session temporary
directory *before* the application settings are imported, so tests never touch
real project storage or a developer's SQLite history file.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Redirect all storage + history to a throwaway directory before config loads.
_TMP = Path(tempfile.mkdtemp(prefix="clipper-tests-"))
os.environ.update(
    {
        "STORAGE_ROOT": str(_TMP / "storage"),
        "UPLOADS_DIR": str(_TMP / "storage" / "uploads"),
        "TEMP_DIR": str(_TMP / "storage" / "temp"),
        "CLIPS_DIR": str(_TMP / "storage" / "clips"),
        "HISTORY_DB": str(_TMP / "storage" / "history.db"),
        "PUBLISH_POLL_SECONDS": "0.05",
        "PUBLISH_DEFAULT_INTERVAL_SECONDS": "0",
    }
)

from config import settings  # noqa: E402

settings.ensure_local_dirs()


@pytest.fixture
def history(tmp_path):
    """A fresh, isolated HistoryStore backed by a per-test SQLite file."""
    from publishers.history import HistoryStore

    return HistoryStore(tmp_path / "history.db")


class FakeClip:
    """Minimal clip stand-in matching the attributes publishers/history read."""

    def __init__(self, clip_id="c1", filename="clip_c1.mp4"):
        self.id = clip_id
        self.filename = filename
        self.title = "The one trick that changed everything"
        self.description = "A short, punchy description."
        self.hashtags = ["#viral", "#shorts"]
        self.hook_text = "Wait for it..."
        self.cta = "Follow for more"
        self.mentions = ["@creator"]
        self.thumbnail_text = "MIND BLOWN"
        self.score = 87.5


@pytest.fixture
def fake_clip():
    return FakeClip()


@pytest.fixture
def video_file(tmp_path):
    """A tiny fake video file used for size/stream reads in adapters."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"video-bytes" * 512)
    return path
