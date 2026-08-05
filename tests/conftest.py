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
from hypothesis import settings as hypothesis_settings

# Redirect all storage + history to a throwaway directory before config loads.
_TMP = Path(tempfile.mkdtemp(prefix="clipper-tests-"))
os.environ.update(
    {
        "STORAGE_ROOT": str(_TMP / "storage"),
        "UPLOADS_DIR": str(_TMP / "storage" / "uploads"),
        "TEMP_DIR": str(_TMP / "storage" / "temp"),
        "CLIPS_DIR": str(_TMP / "storage" / "clips"),
        "HISTORY_DB": str(_TMP / "storage" / "history.db"),
        # Durable job records, for the same reason as HISTORY_DB: without this the
        # shared JobManager singleton writes into the developer's real storage
        # directory and jobs accumulate across runs.
        "JOBS_DB": str(_TMP / "storage" / "jobs.db"),
        # U12: accounts and sessions, for the same reason as JOBS_DB. Redirected even
        # though AUTH_ENABLED defaults off and the store is then never opened - the point
        # of this block is that no test *can* touch real storage, not that none happens to.
        "USERS_DB": str(_TMP / "storage" / "users.db"),
        "PUBLISH_POLL_SECONDS": "0.05",
        "PUBLISH_MIN_INTERVAL_FLOOR_SECONDS": "0",
    }
)

from config import settings  # noqa: E402

settings.ensure_local_dirs()


# --------------------------------------------------------------------------- #
# Hypothesis: no per-example deadline                                           #
# --------------------------------------------------------------------------- #
# Hypothesis defaults to a 200 ms per-example deadline and raises DeadlineExceeded
# above it. 43 of this suite's property tests carried a bare
# `@settings(max_examples=100)` and so inherited that deadline, including the ones
# that do real work — `test_speaker_reframe`, `test_visual_selection`,
# `test_reframe_geometry`, `test_broll_*`, `test_engines_base` all touch the
# filesystem or shell out. 200 ms is easy to exceed on a shared CI runner under
# load, which made those tests fail intermittently: the same commit could pass on
# one run and fail on another, with nothing in the diff to explain it.
#
# A deadline is a *latency* assertion, not a correctness one. These properties
# assert behaviour, and how long one example takes is a property of the host, not
# of the code. So it is switched off suite-wide rather than sprinkled onto
# individual tests — and setting it on the profile means the 43 bare `@settings`
# decorators inherit `deadline=None` without needing to be edited, because
# `settings(...)` fills unspecified fields from the active profile.
#
# Timing that genuinely matters is asserted explicitly and generously elsewhere
# (see tests/test_ffmpeg_utils.py, which bounds real subprocesses).
hypothesis_settings.register_profile("clipper", deadline=None)
hypothesis_settings.load_profile("clipper")


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



# --------------------------------------------------------------------------- #
# "Every effect off" options                                                    #
# --------------------------------------------------------------------------- #
# Many tests describe behaviour when nothing is enabled — the compositor returns None, the
# pipeline reproduces its pre-feature output, a filtergraph stays empty. They expressed that
# as ``ProcessingOptions(captions=False)`` and relied on every effect *defaulting* off.
#
# U1 changed those defaults: a default run now enables reframe, zoom, transitions, fades,
# the hook title, the progress bar, emoji, filler removal, keyword highlighting, in-caption
# emoji and visual selection, because shipping them off meant the tool looked worse than it
# is capable of. That makes "the defaults happen to be off" the wrong way to say "all off" —
# so the intent is stated explicitly here instead, once.
#
#: Every optional effect, at its disabled value.
EFFECTS_OFF: dict = {
    "reframe": False,
    "zoom": False,
    "transitions": False,
    "hook_title": False,
    "music": "",
    "fades": False,
    "color": "",
    "progress_bar": False,
    "emoji": "off",
    "filler_removal": False,
    "caption_keyword_highlight": False,
    "caption_keyword_ai": False,
    "caption_emoji": False,
    "visual_selection": False,
    "broll": False,
    "asset_sourcing_mode": "off",
    "diarization": False,
    "speaker_reframe": False,
    "kinetic_typography_enabled": False,
    "stem_inpainting_enabled": False,
    # AU1/AU2. Loudness normalisation only engages when something else already changed the
    # audio, but "every effect off" has to include it or the all-off graph gains a filter.
    "loudness_normalise": False,
    "music_duck": False,
    # AU7. Only moves clip boundaries, but an "all off" render must still cut exactly the
    # window it was given.
    "trim_silence": False,
}

#: Boolean fields that are not effects, so :data:`EFFECTS_OFF` does not cover them.
#:
#: ``captions`` and ``metadata`` are the product's core function rather than an effect, and
#: tests that want them off pass them explicitly. ``emoji_animate`` is a modifier that does
#: nothing while ``emoji`` is ``"off"``. The rest are policy switches, not output effects.
_NON_EFFECT_BOOLS = frozenset(
    {
        "captions",
        "metadata",
        "emoji_animate",
        "permissibility_mode",
        "translate",
        "stem_declick",
        "stem_retain_stems",
    }
)


def options_all_off(**overrides):
    """:class:`ProcessingOptions` with every optional effect explicitly disabled.

    ``overrides`` are applied afterwards, so a test can enable exactly the one feature it is
    about — which is what makes a single-feature assertion trustworthy.
    """
    from worker.models import ProcessingOptions

    return ProcessingOptions(**{**EFFECTS_OFF, **overrides})


def assert_effects_off_is_exhaustive() -> None:
    """Fail if a boolean effect exists that :data:`EFFECTS_OFF` does not disable.

    Without this the helper rots silently: a new default-on effect would leak into every
    "all off" test, and those tests would quietly stop testing what they claim to.
    """
    import dataclasses

    from worker.models import ProcessingOptions

    leaked = [
        field.name
        for field in dataclasses.fields(ProcessingOptions)
        if field.type in ("bool", bool)
        and field.name not in EFFECTS_OFF
        and field.name not in _NON_EFFECT_BOOLS
        and getattr(options_all_off(), field.name) is True
    ]
    assert not leaked, (
        f"{leaked} default(s) are on and not covered by EFFECTS_OFF; add them there so "
        '"all effects off" tests keep meaning what they say'
    )
