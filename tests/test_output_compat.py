"""Delivered files decode on the platforms they are uploaded to (O1, O2, O3).

Every encode in this repository spelled out ``-c:v libx264 -preset veryfast -crf 20`` and
nothing else, in seven places. Three flags that decide whether a file is *accepted* at all
were missing everywhere, and their absence is invisible locally:

* **O1** — without ``-pix_fmt yuv420p``, ffmpeg keeps the source pixel format. A 4:2:2 or
  10-bit source (any ProRes, most capture cards, some phones) therefore produced a
  4:2:2/10-bit H.264 file. It plays perfectly in VLC and ffplay, and is refused by Safari,
  many Android decoders and several upload pipelines — so the failure surfaces at upload
  time, long after the render looked fine.
* **O2** — without ``-profile:v high -level 4.0``, libx264 derives both from the input and
  can land above what older hardware decoders implement.
* **O3** — a variable-frame-rate source has no single frame duration, so burned captions
  drift against speech as the effective rate wanders.

The tests assert the *probed output*, not the argument list. An argument-list test passes
whenever the flag is spelled correctly, including when something later in the command
overrides it; ffprobe reports what the file actually is.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from config import settings as app_settings
from worker import captions as cap
from worker.ffmpeg_utils import H264_COMPAT_ARGS, h264_args

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")
FFPROBE = shutil.which(app_settings.ffprobe_binary) or shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="no ffmpeg/ffprobe on PATH; output-compatibility checks need both",
)


def _probe(path) -> dict[str, str]:
    """``pix_fmt``, ``profile``, ``level`` and ``r_frame_rate`` of a file's video stream."""
    proc = subprocess.run(
        [
            FFPROBE, "-v", "error", "-select_streams", "v",
            "-show_entries", "stream=pix_fmt,profile,level,r_frame_rate",
            "-of", "default=nw=1", str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


@pytest.fixture
def source_422(tmp_path):
    """A 4:2:2, 15 fps source — the shape that exposed O1 and O3.

    Deliberately *not* the 4:2:0 30 fps that every other fixture uses: a yuv420p source
    would be converted to yuv420p by accident, so the test would pass with the flag absent.
    """
    path = tmp_path / "src422.mp4"
    subprocess.run(
        [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=s=320x240:d=1:r=15",
            "-f", "lavfi", "-i", "sine=d=1",
            "-shortest", "-pix_fmt", "yuv422p", "-c:v", "libx264", "-y", str(path),
        ],
        check=True, capture_output=True, timeout=120,
    )
    probed = _probe(path)
    assert probed["pix_fmt"] == "yuv422p", "the fixture must really be 4:2:2"
    assert probed["r_frame_rate"] == "15/1", "the fixture must really not be 30 fps"
    return path


# --------------------------------------------------------------------------- #
# The argument builder                                                          #
# --------------------------------------------------------------------------- #
def test_compat_flags_are_present_in_every_encode():
    args = h264_args()
    for flag in H264_COMPAT_ARGS:
        assert flag in args
    assert "-pix_fmt" in args and args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert "-profile:v" in args and args[args.index("-profile:v") + 1] == "high"
    assert "-level" in args and args[args.index("-level") + 1] == "4.0"


def test_frame_rate_normalisation_is_opt_in():
    """An intermediate that will be re-encoded gains nothing from being resampled twice."""
    assert "-r" not in h264_args()
    delivered = h264_args(normalise_fps=True)
    assert delivered[delivered.index("-r") + 1] == str(app_settings.output_fps)


def test_every_encode_site_uses_the_shared_builder():
    """The flags are only a guarantee if no encode bypasses them.

    Seven call sites each spelled the libx264 arguments out by hand, which is how three
    flags came to be missing from all seven. A new site that hand-rolls them would silently
    reintroduce that.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    # The two modules that *are* the builder. `video_encoders` was split out for O8 because
    # choosing an encoder needs a probe, a per-encoder quality mapping and a fallback - too much
    # to sit inside `h264_args` - so "libx264" legitimately appears there as the software default
    # and the fallback target. Exempted by name rather than by pattern, so a third module cannot
    # join the list by accident.
    builder_modules = {"ffmpeg_utils.py", "video_encoders.py"}
    offenders: list[str] = []
    for path in sorted((root / "worker").rglob("*.py")):
        if path.name in builder_modules:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'["\']libx264["\']', line) and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    assert not offenders, (
        "these encodes name libx264 directly instead of calling h264_args(), so they do "
        "not get the compatibility flags:\n" + "\n".join(offenders)
    )


# --------------------------------------------------------------------------- #
# What the file actually is                                                     #
# --------------------------------------------------------------------------- #
@requires_ffmpeg
@pytest.mark.real_binary
def test_a_422_source_is_delivered_as_420_high_at_a_constant_rate(source_422, tmp_path):
    """O1, O2, O3 end to end, through a real deliverable path.

    ``burn_captions`` is a path a user's file really comes out of, so this covers the
    argument builder *and* its wiring.
    """
    ass = tmp_path / "cap.ass"
    cap.build_ass([cap.Cue(0.0, 1.0, [])], ass, hook_text="hello")
    out = tmp_path / "burned.mp4"
    cap.burn_captions(source_422, ass, out)

    probed = _probe(out)
    assert probed["pix_fmt"] == "yuv420p", "a 4:2:2 file some platforms refuse to decode"
    assert probed["profile"] == "High"
    assert probed["level"] == "40", "level 4.0 is reported by ffprobe as 40"
    assert probed["r_frame_rate"] == f"{app_settings.output_fps}/1"


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_missing_flags_really_were_the_difference(source_422, tmp_path):
    """Without the flags the same source yields a file with the old problems.

    This is the control. It runs the previous argument list against the same input, so the
    test above is demonstrably testing the flags rather than a property the encoder would
    have given us anyway.
    """
    out = tmp_path / "legacy.mp4"
    subprocess.run(
        [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source_422),
            # The pre-O1/O2/O3 argument list, verbatim.
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy", "-movflags", "+faststart", str(out),
        ],
        check=True, capture_output=True, timeout=120,
    )
    probed = _probe(out)
    assert probed["pix_fmt"] == "yuv422p", (
        "if this is already 4:2:0 the fixture no longer reproduces O1 and the test above "
        "proves nothing"
    )
    assert probed["r_frame_rate"] != f"{app_settings.output_fps}/1"



def test_o8_the_quality_flag_is_never_hand_rolled_as_crf_outside_the_builder():
    """`-crf` is libx264's spelling and nobody else's.

    Three of the five supported encoders use a different flag *and* a different scale, so a call
    site that appends `-crf` itself would be silently ignored by VideoToolbox (which then uses its
    own default bitrate) and would ask NVENC for a target it does not read. The compatibility guard
    above catches a hand-rolled `libx264`; this catches a hand-rolled rate control, which is the
    other half of the same mistake and the half O8 introduced the possibility of.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    builder_modules = {"ffmpeg_utils.py", "video_encoders.py"}
    offenders: list[str] = []
    for path in sorted((root / "worker").rglob("*.py")):
        if path.name in builder_modules:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'["\']-crf["\']', line) and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    assert not offenders, (
        "these sites spell the rate control themselves; -crf only means anything to libx264:\n"
        + "\n".join(offenders)
    )



# --------------------------------------------------------------------------- #
# -threads: a lever that must be invisible until it is set                      #
# --------------------------------------------------------------------------- #
def test_the_default_emits_no_threads_flag_at_all(monkeypatch):
    """``0`` must leave the argv byte-identical to what it was before the setting existed.

    ``-threads 0`` and no ``-threads`` are equivalent to ffmpeg, so this is not a
    behavioural claim - it is the one form of "unchanged" a test can actually verify, and
    the reason the flag is omitted rather than passed as zero.
    """
    monkeypatch.setattr(app_settings, "ffmpeg_threads", 0)
    monkeypatch.setattr(app_settings, "video_encoder", "libx264", raising=False)

    args = h264_args()

    assert "-threads" not in args
    assert args == [
        "-c:v", "libx264", "-preset", app_settings.x264_preset,
        "-crf", str(app_settings.x264_crf), *H264_COMPAT_ARGS,
    ]


def test_a_configured_thread_count_appears_exactly_once(monkeypatch):
    """Once, not twice: a duplicated ``-threads`` lets the last one silently win."""
    monkeypatch.setattr(app_settings, "ffmpeg_threads", 6)
    monkeypatch.setattr(app_settings, "video_encoder", "libx264", raising=False)

    args = h264_args(normalise_fps=True, vbv_cap=True)

    assert args.count("-threads") == 1
    assert args[args.index("-threads") + 1] == "6"


def test_a_nonsensical_thread_count_is_treated_as_unset(monkeypatch):
    """A negative value has no meaning to ffmpeg, so it takes the ``0`` path."""
    monkeypatch.setattr(app_settings, "ffmpeg_threads", -4)

    assert "-threads" not in h264_args()


def test_threads_is_not_spelled_out_anywhere_but_the_builders():
    """The same boundary the libx264 and -crf guards enforce, for the same reason.

    Those two flags were once duplicated across seven call sites, which is how three of them
    came to be missing from all seven. A ``-threads`` appended at a call site would be a
    fourth flag with no single home and would contradict the setting on whichever paths it
    reached.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    builder_modules = {"ffmpeg_utils.py", "video_encoders.py"}
    offenders: list[str] = []
    for path in sorted((root / "worker").rglob("*.py")):
        if path.name in builder_modules:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'["\']-threads["\']', line) and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    assert not offenders, (
        "these sites set -threads themselves, so FFMPEG_THREADS would not control them:\n"
        + "\n".join(offenders)
    )


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_configured_thread_count_is_accepted_by_the_real_encoder(tmp_path):
    """The flag ffmpeg is actually given must not be rejected by it.

    An argv assertion proves the string is spelled the way the code intends and nothing
    more. ``-threads`` sits in the output options and ffmpeg is unforgiving about where a
    codec option may appear, so this runs a real encode with it in place - which is the
    working agreement's rule about testing against the real program rather than a model of
    the argument list.
    """
    from config import settings

    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=25:duration=1", "-y", str(src)],
        check=True, capture_output=True, timeout=120,
    )

    out = tmp_path / "out.mp4"
    original = settings.ffmpeg_threads
    settings.ffmpeg_threads = 2
    try:
        args = h264_args()
    finally:
        settings.ffmpeg_threads = original
    assert "-threads" in args

    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(src),
         *args, "-y", str(out)],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.exists() and out.stat().st_size > 0
    # And the output is still a compliant deliverable - the new flag must not have
    # displaced the compatibility ones.
    probed = _probe(out)
    assert probed["pix_fmt"] == "yuv420p"
