"""Frame-rate policy (O18) and keyframe interval (O19).

**The gate for this whole item is R8.9: A/V sync verified at every rate the policy can deliver.**
That is why O18 was blocked on M11 rather than on effort. Frame-rate handling is the likeliest place
in this pipeline to introduce drift, drift desynchronises every burned caption, and the blanket
`-r 30` this narrows was preventing exactly that harm. Narrowing it is only defensible with a
measurement, and `evaluation/sync.py` is the measurement.

The policy tests probe the **delivered file** rather than the argument list, per the spec's standing
rule: `-r 24` in argv proves a flag was passed, not that the muxed file runs at 24.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from config import settings as app_settings
from evaluation import sync
from worker import frame_rate as fr
from worker.frame_rate import Rate_Kind

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")
FFPROBE = shutil.which(app_settings.ffprobe_binary) or shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="no ffmpeg/ffprobe on PATH; frame-rate policy needs both",
)


def _delivered_rate(path) -> float:
    proc = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    num, _, den = (proc.stdout or "0/0").strip().partition("/")
    return float(num) / float(den) if float(den) else 0.0


def _keyframe_count(path) -> int:
    """How many I-frames the delivered file actually contains.

    Uses ``-skip_frame nokey``, which decodes only keyframes and emits one row per keyframe. The
    obvious alternative -- reading ``frame=key_frame`` for every frame and counting the ``1``s --
    undercounted here (2 where the file demonstrably contains 3, at indices 0, 60 and 120), so this
    asks ffprobe the question directly rather than filtering its answer to everything.
    """
    proc = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return sum(1 for line in proc.stdout.splitlines() if line.strip())


# --- 5.1/5.2: the policy itself ------------------------------------------------------------


@pytest.mark.parametrize("rate", fr.PLATFORM_FRAME_RATES)
def test_a_cfr_source_at_a_platform_rate_is_preserved(rate):
    """R8.3. The defect this fixes: CFR 24 resampled to 30 gains 3:2 judder it never had."""
    plan = fr.plan_frame_rate(avg_fps=float(rate), base_fps=float(rate), configured_fps=30)
    assert plan.kind is Rate_Kind.CONSTANT
    assert plan.delivered_fps == rate
    assert plan.normalised is False
    assert plan.marker == f"frame_rate_preserved:{rate}"


def test_a_vfr_source_is_still_normalised():
    """R8.2, and the half of the old rule that was correct.

    `config.py` called VFR "every screen recording and most phone footage" and said resampling is
    "what keeps burned captions in sync". That reasoning stands unchanged; only its scope moved.
    """
    plan = fr.plan_frame_rate(avg_fps=22.5, base_fps=30.0, configured_fps=30)
    assert plan.kind is Rate_Kind.VARIABLE
    assert plan.normalised is True
    assert plan.marker.endswith(":vfr")


def test_an_undeterminable_rate_normalises_and_says_which_it_was():
    """R8.5. The conservative branch, and distinguishable from a positive VFR finding.

    Both normalise, so the behaviour is identical — but "the source is variable" and "we could not
    tell" are different facts, and only the marker preserves the difference.
    """
    plan = fr.plan_frame_rate(avg_fps=0.0, base_fps=0.0, configured_fps=30)
    assert plan.kind is Rate_Kind.UNKNOWN
    assert plan.normalised is True
    assert plan.marker.endswith(":undetermined")


@pytest.mark.parametrize("rate", [15.0, 12.0, 29.97, 23.976, 48.0])
def test_a_cfr_source_at_an_unusual_rate_is_normalised(rate):
    """R8.4. Timelapse, animation and drop-frame are better served by one resample.

    29.97 is the interesting member: it is within 0.1% of 30 and deliberately does **not** match,
    because its non-integer frame duration is precisely what makes drop-frame sync awkward.
    """
    plan = fr.plan_frame_rate(avg_fps=rate, base_fps=rate, configured_fps=30)
    assert plan.normalised is True, rate
    assert plan.delivered_fps == 30
    assert plan.marker.endswith(":non_platform_rate")


def test_2997_is_not_treated_as_30():
    """Called out separately because rounding it to 30 is the tempting shortcut."""
    assert fr.matching_platform_rate(29.97) is None
    assert fr.matching_platform_rate(30.0) == 30
    # Container rounding must still be absorbed, or a genuine 25 fps file gets resampled.
    assert fr.matching_platform_rate(24.999998) == 25


def test_the_profile_ceiling_wins_over_the_source(monkeypatch):
    """R8.6. A 60 fps source into a 30 fps profile is resampled even though 60 is a platform rate.

    The constraint that matters is the destination's, not the source's convenience.
    """
    plan = fr.plan_frame_rate(avg_fps=60.0, base_fps=60.0, configured_fps=30, ceiling_fps=30)
    assert plan.delivered_fps == 30
    assert plan.normalised is True
    assert plan.marker.endswith(":profile_ceiling")


def test_unconditional_normalisation_is_still_available():
    """R8.8. The old blanket guarantee, for anyone who wants the certainty."""
    plan = fr.plan_frame_rate(avg_fps=24.0, base_fps=24.0, configured_fps=30, always_normalise=True)
    assert plan.delivered_fps == 30
    assert plan.normalised is True
    assert plan.marker.endswith(":forced")


# --- 5.5: probe the delivered file ---------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
@pytest.mark.parametrize("rate", [24, 25, 30, 50, 60])
def test_a_cfr_source_is_delivered_at_its_own_rate(tmp_path, rate):
    """R8.11, R13.1, R13.3. Probed, not asserted on argv."""
    from worker.ffmpeg_utils import h264_args

    src = tmp_path / f"src{rate}.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x180:rate={rate}:duration=2",
            "-c:v",
            "libx264",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        check=True,
        timeout=600,
    )
    from worker import ffmpeg_utils as fu

    info = fu.probe(src)
    plan = fr.plan_frame_rate(
        avg_fps=info.fps, base_fps=info.base_fps or info.fps, configured_fps=30
    )
    assert plan.delivered_fps == rate, plan

    dest = tmp_path / f"out{rate}.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            *h264_args(normalise_fps=True, delivered_fps=plan.delivered_fps, keyframe_seconds=2.0),
            "-an",
            str(dest),
        ],
        check=True,
        timeout=600,
    )
    assert _delivered_rate(dest) == pytest.approx(float(rate), abs=0.05)


# --- 5.6 / R8.9: the gate. Sync at every deliverable rate ----------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
@pytest.mark.parametrize("rate", [24, 25, 30, 50, 60])
def test_gate_av_sync_holds_at_every_platform_frame_rate(tmp_path, rate):
    """**R8.9. This is the gate that unblocked O18.**

    The old blanket `-r 30` prevented a real harm: burned captions drifting against speech.
    Narrowing it is only defensible if sync survives at every rate the narrowed policy can now
    deliver, and that is measurable rather than arguable — M11 exists for this.

    Each fixture carries a white flash and an audio burst at the same instant; the delivered file
    is decoded and the two are located independently.
    """
    from worker.ffmpeg_utils import h264_args

    src = sync.make_sync_fixture(tmp_path / f"sync{rate}.mp4", event_at=1.0, duration=3.0, fps=rate)
    dest = tmp_path / f"delivered{rate}.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            *h264_args(normalise_fps=True, delivered_fps=rate, keyframe_seconds=2.0),
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(dest),
        ],
        check=True,
        timeout=900,
    )

    report = sync.measure_sync(dest, label=f"delivered at {rate}fps")
    assert abs(report.offset_ms) <= sync.TOLERANCE_MS, (
        f"{rate}fps drifted by {report.offset_ms:.2f}ms — the policy must not be relaxed "
        f"until this passes at every rate (R8.9)"
    )


@requires_ffmpeg
@pytest.mark.real_binary
def test_gate_av_sync_holds_when_a_source_is_resampled(tmp_path):
    """The other half of the gate: the normalising branch must not drift either.

    Resampling video while leaving audio alone is the textbook way to introduce drift, and it is
    the branch VFR and undeterminable sources both take.
    """
    from worker.ffmpeg_utils import h264_args

    src = sync.make_sync_fixture(tmp_path / "vfr_src.mp4", event_at=1.0, duration=3.0, vfr=True)
    dest = tmp_path / "resampled.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            *h264_args(normalise_fps=True, delivered_fps=30, keyframe_seconds=2.0),
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(dest),
        ],
        check=True,
        timeout=900,
    )
    report = sync.measure_sync(dest, label="vfr resampled to 30")
    assert abs(report.offset_ms) <= sync.TOLERANCE_MS, report


# --- O19: keyframe interval ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("fps", "seconds", "expected"),
    [
        (24, 2.0, 48),
        (25, 2.0, 50),
        (30, 2.0, 60),
        (50, 2.0, 100),
        (60, 2.0, 120),
        (30, 1.0, 30),
        (30, 4.0, 120),
    ],
)
def test_the_keyframe_interval_is_derived_from_the_delivered_rate(fps, seconds, expected):
    """R6.2, and precisely why O19 belongs with O18.

    A hard-coded `-g 60` would silently mean 2 s at 30 fps and 1 s at 60 fps — and O18 is what
    makes the delivered rate vary in the first place. Expressing the setting in seconds is what
    keeps the *intent* stable when the rate changes.
    """
    assert fr.keyframe_interval_frames(fps, seconds) == expected


def test_a_nonsense_interval_cannot_produce_g_zero():
    """`-g 0` means every frame is a keyframe: a very large file, delivered without complaint."""
    assert fr.keyframe_interval_frames(30, 0.0) >= 1
    assert fr.keyframe_interval_frames(30, -5.0) >= 1
    assert fr.keyframe_interval_frames(0, 2.0) >= 1


def test_intermediates_are_not_keyframe_constrained():
    """R6.4. Constraining an encoder whose output is about to be re-encoded costs quality.

    The same reasoning `vbv_cap` and `normalise_fps` already apply, which is why this is an
    explicit parameter rather than something inferred.
    """
    from worker.ffmpeg_utils import h264_args

    assert "-g" not in h264_args()
    assert "-g" not in h264_args(normalise_fps=True)
    assert "-g" in h264_args(normalise_fps=True, keyframe_seconds=2.0)


def test_scene_change_keyframes_are_not_disabled():
    """R6.5. `-sc_threshold 0` would force a fixed GOP and put an I-frame in the wrong place.

    That is worse for both quality and seeking than the uneven spacing scene detection produces,
    so its absence is asserted rather than left to reviewer memory.
    """
    from worker.ffmpeg_utils import h264_args

    args = h264_args(normalise_fps=True, keyframe_seconds=2.0)
    assert "-sc_threshold" not in args


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_delivered_file_actually_contains_more_keyframes_than_before(tmp_path):
    """Probed, because the point of O19 is the file rather than the flag.

    x264's unset default is 250 frames — about 8 s at 30 fps — so a 4 s clip previously contained
    essentially one keyframe. At 2 s intervals it should contain several.
    """
    from worker.ffmpeg_utils import h264_args

    src = tmp_path / "src.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30:duration=6",
            "-c:v",
            "libx264",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        check=True,
        timeout=600,
    )
    dest = tmp_path / "keyed.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            *h264_args(normalise_fps=True, delivered_fps=30, keyframe_seconds=2.0),
            "-an",
            str(dest),
        ],
        check=True,
        timeout=600,
    )
    # 6 s at 2 s intervals: at least three, allowing for scene-change keyframes on top.
    assert _keyframe_count(dest) >= 3, "the interval did not reach the delivered file"


# --- classification edges -----------------------------------------------------------------


def test_container_rounding_does_not_make_a_cfr_file_look_variable():
    """A 2% band absorbs rounding and a few duplicate frames without missing real VFR."""
    assert fr.classify(29.999, 30.0) is Rate_Kind.CONSTANT
    assert fr.classify(30.0, 30.0) is Rate_Kind.CONSTANT
    assert fr.classify(20.0, 30.0) is Rate_Kind.VARIABLE


def test_a_zero_rate_is_unknown_rather_than_constant():
    """Zero is missing data. Calling it constant would skip the conservative branch."""
    assert fr.classify(0.0, 30.0) is Rate_Kind.UNKNOWN
    assert fr.classify(30.0, 0.0) is Rate_Kind.UNKNOWN


def test_the_two_rate_fields_are_parsed_by_one_function():
    """The CFR/VFR decision compares them, so parsing them differently would fake a divergence.

    Exactly the duplicated-fact defect mutation testing keeps finding in this repository.
    """
    import inspect

    from worker import ffmpeg_utils

    source = inspect.getsource(ffmpeg_utils.probe)
    assert source.count("_fraction(") >= 1
    assert "avg_frame_rate" in source and "r_frame_rate" in source
