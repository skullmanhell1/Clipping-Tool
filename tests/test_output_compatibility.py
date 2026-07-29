"""Rendered clips are in a pixel format that players and platforms actually accept.

libx264 preserves the *source* pixel format unless told otherwise, and no encode site
passed ``-pix_fmt``. A 10-bit source — ordinary for phone footage, OBS/NVENC captures and
many screen recorders — therefore produced a ``yuv420p10le`` / ``High 10`` clip, and a
4:2:2 source a ``High 4:2:2`` one.

Neither plays in Windows Media Player or Films & TV, QuickTime, or most browsers. The
symptom is nasty precisely because nothing looks broken: the file exists, has the right
duration and correct dimensions, and simply refuses to open.

These tests use a real 10-bit input, because the defect is invisible with the 8-bit
sources the rest of the suite generates.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from config import settings as app_settings
from tests.conftest import requires_ffmpeg
from worker import ffmpeg_utils as fu

#: Formats that are safe to hand to any player or social platform.
SAFE_PIX_FMT = "yuv420p"

#: 8-bit-only H.264 profile, i.e. the encoder-side guarantee matching SAFE_PIX_FMT.
SAFE_PROFILE = "High"


def _probe_video(path: Path) -> dict[str, str]:
    """Return ``pix_fmt``/``profile``/``level`` for the first video stream."""
    out = subprocess.run(
        [
            app_settings.ffprobe_binary, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=pix_fmt,profile,level",
            "-of", "default=nw=1", str(path),
        ],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return dict(
        line.split("=", 1) for line in out.strip().splitlines() if "=" in line
    )


@pytest.fixture
def ten_bit_source(tmp_path: Path) -> Path:
    """A genuine 10-bit H.264 source, which is what exposes the defect."""
    dest = tmp_path / "src10.mp4"
    subprocess.run(
        [
            app_settings.ffmpeg_binary, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p10le", "-profile:v", "high10",
            "-c:a", "aac", "-shortest", str(dest),
        ],
        capture_output=True, timeout=120, check=True,
    )
    # Guard the fixture itself: if this is not 10-bit, the tests below prove nothing.
    assert _probe_video(dest)["pix_fmt"] == "yuv420p10le"
    return dest


# ---------------------------------------------------------------------------
# The real encode paths
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_cutting_a_10bit_source_yields_a_playable_clip(ten_bit_source, tmp_path):
    """``cut_segment`` is the pipeline's first pass and set the format for everything after."""
    dest = tmp_path / "cut.mp4"
    fu.cut_segment(ten_bit_source, 0.0, 1.0, dest)

    probed = _probe_video(dest)
    assert probed["pix_fmt"] == SAFE_PIX_FMT, (
        f"clip is {probed['pix_fmt']}, which many players refuse to open"
    )
    assert probed["profile"] == SAFE_PROFILE, f"clip profile is {probed['profile']}"


@requires_ffmpeg
def test_aspect_reformatting_also_normalises_the_format(ten_bit_source, tmp_path):
    """Every pass must normalise, not just the first — any one of them is the last one."""
    cut = tmp_path / "cut.mp4"
    fu.cut_segment(ten_bit_source, 0.0, 1.0, cut)
    dest = tmp_path / "vertical.mp4"
    fu.reformat_aspect(cut, dest, "9:16")

    probed = _probe_video(dest)
    assert probed["pix_fmt"] == SAFE_PIX_FMT
    assert probed["profile"] == SAFE_PROFILE


@requires_ffmpeg
def test_an_8bit_source_is_unaffected(tmp_path):
    """The normal case must not change: ordinary 8-bit input still comes out 8-bit."""
    src = tmp_path / "src8.mp4"
    subprocess.run(
        [
            app_settings.ffmpeg_binary, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src),
        ],
        capture_output=True, timeout=120, check=True,
    )
    dest = tmp_path / "cut.mp4"
    fu.cut_segment(src, 0.0, 1.0, dest)
    assert _probe_video(dest)["pix_fmt"] == SAFE_PIX_FMT


# ---------------------------------------------------------------------------
# The shared argument list, and that nothing bypasses it
# ---------------------------------------------------------------------------


def test_the_encode_args_pin_format_profile_and_level():
    """The three flags that make output universally playable are all present."""
    args = fu.video_encode_args()
    assert "-pix_fmt" in args and args[args.index("-pix_fmt") + 1] == SAFE_PIX_FMT
    assert "-profile:v" in args and args[args.index("-profile:v") + 1] == "high"
    assert "-level" in args


def test_quality_settings_are_configurable(monkeypatch):
    """CRF and preset come from settings, not from literals in five modules."""
    monkeypatch.setattr(app_settings, "x264_crf", 17)
    monkeypatch.setattr(app_settings, "x264_preset", "slow")
    args = fu.video_encode_args()
    assert args[args.index("-crf") + 1] == "17"
    assert args[args.index("-preset") + 1] == "slow"


def test_no_module_hardcodes_its_own_encoder_flags():
    """Every encoding pass goes through the shared helper.

    The missing ``-pix_fmt`` survived because these flags were duplicated at eight call
    sites across five modules, so there was no single place where the output contract
    could be reviewed. This keeps it that way.
    """
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in (root / "worker").rglob("*.py"):
        if path.name == "ffmpeg_utils.py":
            continue  # the helper itself legitimately names the encoder
        if '"-c:v", "libx264"' in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))

    assert not offenders, (
        f"these modules set encoder flags directly instead of calling "
        f"video_encode_args(): {offenders}"
    )
