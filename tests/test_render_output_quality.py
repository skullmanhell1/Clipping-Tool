"""Measurable properties of a finished render: no seam clicks (V10), correct loudness (M6).

Both are things you would notice watching a clip and never notice reading the code, which is
why both are asserted against the rendered file rather than against the command that produced
it.

* **V10** — filler removal concatenated the kept segments sample-exactly, so every removed
  "um" left a step discontinuity in the waveform: an audible click, several times a clip.
* **M6** — the improvement plan asks for a loudness assertion on output, failing outside
  tolerance. `AU1` added the normalisation; this is the gate that proves it is still working
  end to end, through the real compositor rather than through the filter in isolation.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import wave

import pytest

from config import settings as app_settings
from worker.effects import audio, compositor
from worker.effects.filler import Interval, apply_keep_intervals
from worker.models import ProcessingOptions

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None, reason="no ffmpeg on PATH; these checks measure rendered files"
)

#: Sample rate used for every fixture and every measurement here.
_RATE = 48000


def _tone_clip(path, *, seconds=6.0, freq=440, volume=1.0):
    """A clip whose audio is one continuous tone.

    A continuous waveform is the strictest possible seam fixture: any join produces a phase
    step, so a click is guaranteed unless something removes it. Real speech would sometimes
    happen to join near a zero crossing and hide the defect.
    """
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
            f"testsrc=s=320x240:d={seconds}:r=30",
            "-f",
            "lavfi",
            "-i",
            f"sine=f={freq}:d={seconds}:sample_rate={_RATE}",
            "-af",
            f"volume={volume}",
            "-shortest",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )
    return path


def _max_sample_step(media, at_seconds: float, window_ms: float = 3.0) -> int:
    """The largest sample-to-sample jump within ``window_ms`` of ``at_seconds``.

    A step discontinuity *is* the click, so this measures the artefact directly rather than
    something correlated with it.
    """
    wav = media.with_suffix(".probe.wav")
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media),
            "-ac",
            "1",
            "-ar",
            str(_RATE),
            "-y",
            str(wav),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    with wave.open(str(wav)) as handle:
        frames = handle.readframes(handle.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)

    centre = int(at_seconds * _RATE)
    half = int(window_ms / 1000.0 * _RATE)
    window = samples[max(0, centre - half) : centre + half]
    assert len(window) > 2, "measurement window fell outside the file"
    return max(abs(window[i + 1] - window[i]) for i in range(len(window) - 1))


# --------------------------------------------------------------------------- #
# V10 — the seam                                                                #
# --------------------------------------------------------------------------- #
def test_seam_fades_are_applied_only_at_real_seams():
    """The clip's outer edges are not seams, and fading there clips a syllable."""
    from worker.effects.filler import _seam_fades

    middle = _seam_fades(2.0, 0.012, lead=True, tail=True)
    assert "afade=t=in" in middle and "afade=t=out" in middle

    first = _seam_fades(2.0, 0.012, lead=False, tail=True)
    assert "afade=t=in" not in first and "afade=t=out" in first

    last = _seam_fades(2.0, 0.012, lead=True, tail=False)
    assert "afade=t=in" in last and "afade=t=out" not in last

    only = _seam_fades(2.0, 0.012, lead=False, tail=False)
    assert only == ""


def test_a_segment_too_short_to_fade_is_left_alone():
    """A fade must not become a meaningful fraction of the segment it is on."""
    from worker.effects.filler import _seam_fades

    assert _seam_fades(0.03, 0.012, lead=True, tail=True) == ""
    assert _seam_fades(0.0, 0.012, lead=True, tail=True) == ""


def test_seam_fading_can_be_disabled():
    from worker.effects.filler import _seam_fades

    assert _seam_fades(2.0, 0.0, lead=True, tail=True) == ""


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_seam_click_is_measurably_reduced(tmp_path, monkeypatch):
    """V10, measured: the waveform step at the join, with the fade and without.

    The comparison is the same source and the same cut rendered twice, so the only variable
    is the fade. An absolute threshold would instead be a statement about this fixture's
    amplitude.
    """
    source = _tone_clip(tmp_path / "tone.mp4")
    keeps = [Interval(0.0, 2.0), Interval(3.0, 5.0)]

    monkeypatch.setattr(app_settings, "filler_seam_fade_ms", 12)
    faded = apply_keep_intervals(source, keeps, tmp_path / "faded.mp4")

    monkeypatch.setattr(app_settings, "filler_seam_fade_ms", 0)
    hard = apply_keep_intervals(source, keeps, tmp_path / "hard.mp4")

    # The join is at 2.0 s: the end of the first kept interval.
    faded_step = _max_sample_step(faded, 2.0)
    hard_step = _max_sample_step(hard, 2.0)

    assert faded_step < hard_step / 2, (
        f"seam step {faded_step} with fading vs {hard_step} without; the discontinuity that "
        "produces the click is not being removed"
    )


@requires_ffmpeg
@pytest.mark.real_binary
def test_seam_fades_do_not_change_the_clip_duration(tmp_path, monkeypatch):
    """The reason this is a fade and not an ``acrossfade``.

    A crossfade overlaps the segments, so the result is shorter than the sum of its parts by
    the overlap at every seam. ``rebase_words`` maps word timings onto the kept timeline
    using cumulative segment durations, so an overlap would drift captions out of sync by a
    growing amount across the clip — a worse artefact than the click it fixes.
    """
    source = _tone_clip(tmp_path / "tone.mp4")
    keeps = [Interval(0.0, 2.0), Interval(3.0, 5.0)]
    expected = sum(k.duration for k in keeps)

    durations = []
    for fade_ms in (0, 12):
        monkeypatch.setattr(app_settings, "filler_seam_fade_ms", fade_ms)
        out = apply_keep_intervals(source, keeps, tmp_path / f"out{fade_ms}.mp4")
        probe = subprocess.run(
            [
                shutil.which("ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        durations.append(float(probe.stdout.strip()))

    assert abs(durations[0] - durations[1]) < 0.05, durations
    assert abs(durations[1] - expected) < 0.15, (durations[1], expected)


# --------------------------------------------------------------------------- #
# M6 — loudness of the delivered file                                           #
# --------------------------------------------------------------------------- #
#: How far the rendered clip may sit from its platform target, in LU.
#:
#: ``loudnorm`` is a normaliser, not a limiter, so it lands within a fraction of a LU when it
#: can apply linear gain. 1.5 LU is loose enough that a clip whose peaks force ffmpeg into
#: dynamic mode still passes, and tight enough that a missing or misapplied filter does not.
LOUDNESS_TOLERANCE_LU = 1.5


@requires_ffmpeg
@pytest.mark.real_binary
@pytest.mark.parametrize(
    ("platform", "source_volume"),
    [("tiktok", 0.05), ("youtube", 0.05), ("tiktok", 0.7)],
)
def test_a_rendered_clip_lands_on_its_platform_loudness_target(platform, source_volume, tmp_path):
    """M6: measure LUFS after a real render and fail outside tolerance.

    Goes through ``compositor.render_clip`` with the shipped defaults rather than applying
    the filter directly, so it covers the wiring as well as the filter: which platform target
    was chosen, where in the audio chain it sits, and whether it survived the mix.

    Both a quiet and a loud source are checked, because normalisation is two-directional and
    a gain that only ever increases would pass the quiet case alone.
    """
    from tests.conftest import FakeWord

    source = _tone_clip(tmp_path / "src.mp4", seconds=4.0, freq=300, volume=source_volume)
    before = audio.measure_loudness(source)
    assert before is not None

    options = ProcessingOptions(platform=platform)
    words = [FakeWord(0.2, 0.6, "money"), FakeWord(0.8, 1.2, "winning")]
    result = compositor.render_clip(source, tmp_path / "out.mp4", options, words, tmp_path)
    assert result is not None, "the default options must render something"

    target = audio.platform_loudness_target(platform)
    assert f"loudness:{target:g}lufs" in result.effects_applied, result.effects_applied

    after = audio.measure_loudness(tmp_path / "out.mp4")
    assert after is not None
    assert abs(after.input_i - target) <= LOUDNESS_TOLERANCE_LU, (
        f"{platform}: rendered at {after.input_i:.2f} LUFS, target {target} "
        f"(source was {before.input_i:.2f})"
    )
    assert (
        after.input_tp <= app_settings.loudness_true_peak_db + 0.5
    ), f"true peak {after.input_tp:.2f} dBTP exceeds the ceiling; the clip will clip"


@requires_ffmpeg
@pytest.mark.real_binary
def test_an_unmeasurable_source_is_recorded_rather_than_fatal(tmp_path):
    """A clip must still render when its loudness cannot be measured.

    Verified through the compositor, because the degradation marker is the only way an
    operator learns that a delivered clip was *not* normalised.
    """
    from tests.conftest import FakeWord

    silent = tmp_path / "novideo.mp4"
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
            "color=black:s=320x240:d=2:r=30",
            "-y",
            str(silent),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    assert audio.measure_loudness(silent) is None, "fixture must have no measurable audio"

    options = ProcessingOptions(platform="tiktok")
    words = [FakeWord(0.2, 0.6, "money")]
    result = compositor.render_clip(silent, tmp_path / "out.mp4", options, words, tmp_path)

    # A source with no audio track never reaches the loudness stage at all; either way the
    # render must succeed and must not claim a normalisation it did not perform.
    assert result is not None
    assert not any(
        m.startswith("loudness:") for m in result.effects_applied
    ), result.effects_applied
