"""Shared pytest fixtures and environment isolation for Phase 3 tests.

Storage and the history database are redirected into a per-session temporary
directory *before* the application settings are imported, so tests never touch
real project storage or a developer's SQLite history file.
"""
from __future__ import annotations

import os
import shutil
import subprocess
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


# --------------------------------------------------------------------------- #
# ffmpeg integration helpers (Phase 4 effects)
# --------------------------------------------------------------------------- #
FFMPEG = shutil.which(settings.ffmpeg_binary) or shutil.which("ffmpeg")
FFPROBE = shutil.which(settings.ffprobe_binary) or shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available"
)


def _make_video(dest: Path, duration: float = 3.0, w: int = 1280, h: int = 720,
                audio: bool = True, rate: int = 30) -> Path:
    """Generate a small synthetic test clip with ffmpeg (testsrc + sine)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-f", "lavfi",
           "-i", f"testsrc=size={w}x{h}:rate={rate}:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={duration}", "-shortest"]
    cmd += ["-pix_fmt", "yuv420p", "-c:v", "libx264"]
    if audio:
        cmd += ["-c:a", "aac"]
    cmd += [str(dest)]
    subprocess.run(cmd, check=True, capture_output=True)
    return dest


@pytest.fixture
def make_video(tmp_path):
    """Factory fixture returning ``fn(name, duration, w, h, audio) -> Path``."""
    def _factory(name="src.mp4", duration=3.0, w=1280, h=720, audio=True):
        return _make_video(tmp_path / name, duration, w, h, audio)

    return _factory


def probe_duration(path) -> float:
    """Return the duration (s) of a media file via ffprobe."""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def probe_size(path) -> tuple[int, int]:
    """Return the (width, height) of a video's first video stream."""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip().split(",")
    return int(out[0]), int(out[1])


@pytest.fixture
def png_asset(tmp_path):
    """Create a small opaque PNG (stands in for a Twemoji asset) via ffmpeg."""
    def _factory(name="emoji.png", color="red"):
        path = tmp_path / name
        subprocess.run(
            [FFMPEG, "-y", "-f", "lavfi", "-i", f"color=c={color}:s=72x72:d=1,format=rgba",
             "-frames:v", "1", str(path)],
            check=True, capture_output=True,
        )
        return path

    return _factory


class FakeWord:
    """Lightweight word with ``.start``/``.end``/``.text`` for effect tests."""

    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text
        self.probability = 1.0
