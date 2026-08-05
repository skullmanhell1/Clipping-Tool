"""A clip is checked against the platform before it is uploaded (O10), and clips do not
open on dead air (AU7).

O10's starting point: the only pre-flight in the publish path was ``video_path.exists()``.
Nothing checked aspect, duration, resolution, file size, codec or frame rate, so a clip a
platform will refuse was discovered *by uploading it* - the failure surfaced as whatever
that platform's API chose to say, after consuming an upload attempt and a rate-limit slot.

AU7's starting point: nothing trimmed silence at a clip's edges, so a moment selected from
a pause opened on dead air. The first second is where a viewer decides whether to keep
watching.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from config import settings as app_settings
from publishers import preflight
from worker.segmentation import (
    MAX_EDGE_TRIM_S,
    detect_silences,
    trim_edge_silence,
)

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None, reason="no ffmpeg on PATH; these checks probe real media"
)


def _make_clip(path, *, seconds=4.0, w=1080, h=1920, fps=30, audio=True, vcodec="libx264"):
    cmd = [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=s={w}x{h}:d={seconds}:r={fps}",
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=f=300:d={seconds}", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", vcodec, "-pix_fmt", "yuv420p", "-y", str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    return path


# --------------------------------------------------------------------------- #
# O10 — the limits table                                                        #
# --------------------------------------------------------------------------- #
def test_limits_are_defined_for_every_publisher_we_ship():
    """A platform with no entry silently gets the permissive fallback.

    That is the right default, but it must be a choice rather than an oversight, so the
    platforms we actually publish to are named here.
    """
    from worker.metadata import PLATFORM_PROFILES

    for platform in PLATFORM_PROFILES:
        if platform == "generic":
            continue
        assert (
            platform in preflight.PLATFORM_LIMITS
        ), f"{platform} has no pre-flight limits, so nothing about its uploads is checked"


def test_unknown_platforms_get_the_permissive_fallback():
    assert preflight.limits_for("does-not-exist").name == "generic"
    assert preflight.limits_for("").name == "generic"
    assert preflight.limits_for("  TikTok ").name == "tiktok"


def test_limits_are_internally_coherent():
    """A typo in the table would silently reject everything or nothing."""
    for name, limits in preflight.PLATFORM_LIMITS.items():
        assert 0 < limits.min_duration_s < limits.max_duration_s, name
        assert limits.max_file_mb > 0, name
        assert limits.preferred_aspects, name
        assert all(ratio > 0 for ratio in limits.preferred_aspects), name


# --------------------------------------------------------------------------- #
# O10 — validation without touching a network                                   #
# --------------------------------------------------------------------------- #
def test_a_missing_or_empty_file_is_an_error(tmp_path):
    missing = preflight.validate_clip(tmp_path / "nope.mp4", "tiktok")
    assert not missing.ok and "does not exist" in missing.errors[0]

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    report = preflight.validate_clip(empty, "tiktok")
    assert not report.ok and "empty" in report.errors[0]


def test_an_unprobeable_file_is_an_error_rather_than_an_exception(tmp_path):
    """Publishing a file ffprobe cannot read is not going to go well either.

    It must be reported, not raised: the publish worker turns a report into a failed attempt
    with a readable reason, and an exception here would abort the whole queue pass.
    """
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"not really a video" * 64)
    report = preflight.validate_clip(junk, "tiktok")
    assert not report.ok
    assert any("probed" in error for error in report.errors), report.errors


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_well_formed_vertical_clip_passes_every_platform(tmp_path):
    """The shape this tool produces by default must not trip its own pre-flight.

    A validator that rejects the product's normal output is worse than none - it would move
    the failure from upload time to publish time and stop everything.
    """
    clip = _make_clip(tmp_path / "good.mp4", seconds=5.0, w=1080, h=1920, fps=30)
    for platform in preflight.PLATFORM_LIMITS:
        report = preflight.validate_clip(clip, platform)
        assert report.ok, f"{platform}: {report.errors}"
        assert not report.warnings, f"{platform}: {report.warnings}"


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_clip_over_a_duration_limit_is_rejected_for_that_platform_only(tmp_path):
    """Limits are per platform, so the same file can be fine in one place and not another.

    A 95-second clip exceeds Instagram's Reels ceiling while being unremarkable on YouTube;
    a single global limit would either block valid YouTube uploads or let Instagram ones
    through to be refused.
    """
    clip = _make_clip(tmp_path / "long.mp4", seconds=95.0, w=540, h=960, fps=15)

    instagram = preflight.validate_clip(clip, "instagram")
    assert not instagram.ok
    assert any("above" in error for error in instagram.errors), instagram.errors

    youtube = preflight.validate_clip(clip, "youtube")
    assert youtube.ok, youtube.errors


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_clip_below_a_minimum_duration_is_rejected(tmp_path):
    clip = _make_clip(tmp_path / "tiny.mp4", seconds=1.5, w=540, h=960, fps=30)
    report = preflight.validate_clip(clip, "instagram")  # 3 s minimum
    assert not report.ok
    assert any("below" in error for error in report.errors), report.errors


@requires_ffmpeg
@pytest.mark.real_binary
def test_an_unaccepted_video_codec_is_an_error(tmp_path):
    """No target platform transcodes an unexpected codec on ingest, so this blocks."""
    clip = _make_clip(tmp_path / "mpeg4.mp4", seconds=3.0, w=540, h=960, vcodec="mpeg4")
    report = preflight.validate_clip(clip, "tiktok")
    assert not report.ok
    assert any("codec" in error for error in report.errors), report.errors


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_silent_clip_warns_but_still_publishes(tmp_path):
    """Almost always a mistake in a clip built from speech - but the user's to make."""
    clip = _make_clip(tmp_path / "silent.mp4", seconds=4.0, w=1080, h=1920, audio=False)
    report = preflight.validate_clip(clip, "tiktok")
    assert report.ok, report.errors
    assert any("audio" in warning for warning in report.warnings), report.warnings


@requires_ffmpeg
@pytest.mark.real_binary
def test_an_unexpected_aspect_warns_rather_than_blocks(tmp_path):
    """A landscape clip going to TikTok will publish, letterboxed. That is a taste call.

    Blocking it would be us overruling a user who may know exactly what they are doing,
    which is the distinction between an error and a warning here.
    """
    clip = _make_clip(tmp_path / "wide.mp4", seconds=4.0, w=1920, h=1080)
    report = preflight.validate_clip(clip, "tiktok")
    assert report.ok, report.errors
    assert any("aspect" in warning for warning in report.warnings), report.warnings


def test_the_report_summarises_itself_for_a_failure_record():
    """The reason lands in a history row a human reads, so it has to read like a sentence."""
    report = preflight.PreflightReport(platform="tiktok", errors=["clip is 900.0s, above ..."])
    assert report.summary().startswith("tiktok: ")
    assert not report.ok
    assert report.to_dict()["ok"] is False

    clean = preflight.PreflightReport(platform="youtube")
    assert clean.ok and clean.summary() == "youtube: ok"


# --------------------------------------------------------------------------- #
# O10 — the publish worker actually consults it                                 #
# --------------------------------------------------------------------------- #
def test_a_rejected_clip_never_reaches_the_publisher(tmp_path, fake_clip, video_file):
    """The point of the whole item: no upload attempt is spent on a clip that cannot work.

    ``video_file`` is a few hundred bytes of fake MP4, which is unprobeable - exactly the
    case that used to sail past ``exists()`` and into a platform API.
    """
    from publishers.history import HistoryStore
    from publishers.manager import PublishManager
    from tests.fakes import FakePublisher

    publisher = FakePublisher("tiktok")
    store = HistoryStore(tmp_path / "history.db")
    manager = PublishManager(publishers={"tiktok": publisher}, history=store, autostart=False)

    ids = manager.submit(
        job_id="j", clip=fake_clip, video_path=video_file, platforms=["tiktok"], mode="auto"
    )
    assert ids
    manager.run_due_once()

    assert publisher.published == [], "a clip that fails pre-flight was still uploaded"
    attempt = store.get_attempt(ids[0])
    assert attempt["state"] == "failed"
    assert "rejected before upload" in (attempt["error"] or "")


# --------------------------------------------------------------------------- #
# AU7 — edge silence                                                            #
# --------------------------------------------------------------------------- #
def test_leading_and_trailing_silence_are_trimmed():
    silences = [(0.0, 1.2), (5.0, 5.4), (9.6, 12.0)]
    assert trim_edge_silence(0.5, 10.5, silences) == (1.2, 9.6)


def test_a_pause_in_the_middle_is_content_not_dead_air():
    """Only a silence touching a boundary is trimmed. A pause mid-clip is speech rhythm."""
    silences = [(0.0, 1.2), (5.0, 5.4), (9.6, 12.0)]
    assert trim_edge_silence(2.0, 8.0, silences) == (2.0, 8.0)


def test_trimming_is_capped_per_edge():
    """A cap is the difference between tightening a cut and mangling one.

    ``silencedetect`` reports a pause, and a pause at a boundary is often the breath before
    the first word. Uncapped, a moment selected mid-pause could lose seconds and open on a
    syllable.
    """
    start, end = trim_edge_silence(0.0, 10.0, [(0.0, 9.0)])
    assert start == pytest.approx(MAX_EDGE_TRIM_S)
    assert end == 10.0


def test_a_window_that_would_collapse_is_left_alone():
    """Dead air is better than a clip trimmed out of existence."""
    silences = [(9.6, 12.0)]
    assert trim_edge_silence(9.8, 10.4, silences) == (9.8, 10.4)


def test_degenerate_windows_are_returned_untouched():
    for window in ((5.0, 5.0), (7.0, 3.0), (0.0, 0.0)):
        assert trim_edge_silence(*window, [(0.0, 10.0)]) == window


@requires_ffmpeg
@pytest.mark.real_binary
def test_edge_silence_is_detected_and_trimmed_on_real_audio(tmp_path):
    """End to end with ffmpeg: silence, then a tone, then silence.

    Ties the pure policy to real ``silencedetect`` output — the two halves are useless apart,
    and a parser change would otherwise pass every unit test above.
    """
    source = tmp_path / "gappy.wav"
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=f=300:d=6:sample_rate=48000",
            # audible only between 2 s and 4 s
            "-af",
            "volume='if(between(t,2,4),1.0,0.0)':eval=frame",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )

    silences = detect_silences(source)
    assert silences, "silencedetect reported nothing on a file that is mostly silent"

    # A window spanning the whole file tightens onto the tone, within the per-edge cap.
    start, end = trim_edge_silence(0.0, 6.0, silences)
    assert start > 0.0, f"leading silence not trimmed (silences={silences})"
    assert end < 6.0, f"trailing silence not trimmed (silences={silences})"
    assert start <= MAX_EDGE_TRIM_S + 0.01
    assert end >= 6.0 - MAX_EDGE_TRIM_S - 0.01
