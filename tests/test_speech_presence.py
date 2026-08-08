"""Speech presence and dynamic control (AU11).

`loudnorm` sets **level**; before this the only spectral shaping anywhere in the audio path was a
`lowpass` inside the music synthesiser. So a clip normalised to exactly the right LUFS could still
be muddy and hard to follow on a phone speaker — which is where nearly all of this footage is
watched.

The tests that earn their keep here are R6.9 and R6.10, measured **from the delivered file**. They
caught a real defect in this feature's first implementation: two-pass `loudnorm` applies a single
gain computed from measuring the source, and the presence chain *removes energy* after that
measurement, so the delivered clip landed **−17.5 LUFS against a −14 target**. Every platform would
then turn it up, lifting the noise floor — the exact harm AU1 exists to prevent.

Nothing in the filter strings could have revealed that. Only rendering and measuring did.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from config import settings as app_settings
from worker.effects import audio
from worker.engines.capabilities import Capability_Status

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None, reason="no ffmpeg on PATH; loudness verification needs it"
)

TARGET_LUFS = -14.0
#: Platforms normalise against integrated loudness with roughly this much slack.
LOUDNESS_TOLERANCE_LU = 1.0


def _source(path, *, seconds=6):
    """Speech-like audio: a voiced tone with level variation, low rumble and a little noise."""
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
            (
                "aevalsrc='0.35*sin(2*PI*180*t)*(0.6+0.4*sin(2*PI*3*t))"
                f"+0.12*sin(2*PI*45*t)+0.05*random(0)':d={seconds}:s=48000"
            ),
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(path.with_suffix(".wav")),
        ],
        check=True,
        timeout=600,
    )
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
            f"color=c=black:s=320x180:r=25:d={seconds}",
            "-i",
            str(path.with_suffix(".wav")),
            "-c:v",
            "libx264",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        timeout=600,
    )
    return path


def _delivered_loudness(path) -> tuple[float, float]:
    """``(integrated_lufs, peak_dbfs)`` of a rendered file, via `ebur128`.

    Deliberately a *different* measurement mechanism from `loudnorm`'s own analysis pass, which is
    what the implementation uses. Verifying `loudnorm` with `loudnorm` would only prove it agrees
    with itself.
    """
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    tail = proc.stderr[-2500:]
    lufs = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", tail)
    peak = re.findall(r"Peak:\s*(-?[\d.]+)\s*dBFS", tail)
    assert lufs and peak, f"ebur128 produced no summary:\n{tail[-600:]}"
    return float(lufs[-1]), float(peak[-1])


def _render(source, dest, strength):
    """The real chain in the compositor's order: presence -> loudnorm -> limiter."""
    chain = audio.presence_chain(strength)
    stats = audio.measure_loudness(source, prefilters=chain)
    parts = [*chain, audio.loudnorm_filter(stats, TARGET_LUFS), audio.true_peak_limit_filter()]
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            ",".join(p for p in parts if p),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(dest),
        ],
        check=True,
        timeout=900,
    )
    return dest


# --- R6.6: the default is off and byte-identical --------------------------------------------


def test_the_chain_is_empty_by_default():
    """R6.6, R6.7. Audible processing needs a preference trial, not an opinion."""
    assert float(app_settings.speech_presence) == 0.0
    assert audio.presence_chain(0.0) == []
    assert audio.presence_marker(0.0) == ""


def test_an_unusable_strength_disables_rather_than_maximising():
    """A mistyped setting should do nothing, not process every clip as hard as possible."""
    for value in (float("nan"), "not-a-number", None, -1.0):
        assert audio.clamp_presence(value) == 0.0
        assert audio.presence_chain(value) == []


def test_an_over_large_strength_is_clamped():
    assert audio.clamp_presence(5.0) == 1.0
    assert audio.presence_chain(5.0) == audio.presence_chain(1.0)


# --- R6.4: one continuous control -----------------------------------------------------------


def test_every_gain_scales_with_strength():
    """R6.4. A single control, and genuinely continuous rather than a three-position switch."""
    half = ",".join(audio.presence_chain(0.5))
    full = ",".join(audio.presence_chain(1.0))
    assert "g=-1.5" in half and "g=1.5" in half
    assert "g=-3.0" in full and "g=3.0" in full
    assert "ratio=1.5" in half and "ratio=2.0" in full


def test_the_high_pass_does_not_scale_because_a_corner_is_not_a_gain():
    """80 Hz is below speech at any strength; sliding the corner would change *what* is removed.

    Scaling it would mean a low setting removes rumble the high setting keeps, or vice versa —
    which is not "less of the same effect".
    """
    for strength in (0.25, 0.5, 1.0):
        assert f"highpass=f={audio.PRESENCE_HIGHPASS_HZ}" in audio.presence_chain(strength)[0]


def test_no_makeup_gain_is_applied():
    """R6.3. Level belongs to AU1's two-pass loudnorm.

    Making up gain here would move the measurement the second pass depends on, which is exactly
    the class of defect R6.10 catches.
    """
    assert "makeup=1" in ",".join(audio.presence_chain(1.0))


def test_the_chain_contains_no_limiter():
    """R6.3 again: true-peak limiting is AU3's, and duplicating it would double-limit."""
    assert "alimiter" not in ",".join(audio.presence_chain(1.0))


# --- R6.9 / R6.10: measured from the delivered file -----------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
@pytest.mark.parametrize("strength", [0.0, 0.5, 1.0])
def test_the_delivered_clip_still_meets_the_loudness_target(tmp_path, strength):
    """R6.10, and the requirement that found a real defect.

    Two-pass `loudnorm` applies one gain derived from measuring the signal. The presence chain
    removes energy — high-pass, 250 Hz cut, compression — so a measurement taken from the *unshaped*
    source produces a gain that is wrong by exactly that amount.

    Measured with the naive ordering: **−17.5 LUFS against a −14 target**, 3.6 LU quiet. Platforms
    turn that *up*, lifting the noise floor, which is the harm AU1 exists to prevent. Nothing in the
    filter strings shows it; only rendering and measuring does.
    """
    source = _source(tmp_path / "src.mp4")
    delivered = _render(source, tmp_path / f"out_{strength}.mp4", strength)
    lufs, _peak = _delivered_loudness(delivered)
    assert abs(lufs - TARGET_LUFS) <= LOUDNESS_TOLERANCE_LU, (
        f"presence={strength} delivered {lufs:.1f} LUFS against a {TARGET_LUFS} target"
    )


@requires_ffmpeg
@pytest.mark.real_binary
@pytest.mark.parametrize("strength", [0.0, 0.5, 1.0])
def test_the_delivered_clip_stays_under_its_true_peak_ceiling(tmp_path, strength):
    """R6.9. A compressor and a presence lift both raise peaks; the ceiling must still hold."""
    source = _source(tmp_path / "src.mp4")
    delivered = _render(source, tmp_path / f"peak_{strength}.mp4", strength)
    _lufs, peak = _delivered_loudness(delivered)
    assert peak <= -1.0, f"presence={strength} peaked at {peak:.1f} dBFS"


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_measurement_pass_sees_the_presence_chain(tmp_path):
    """The fix, asserted directly rather than only through its effect.

    Measuring the same source with and without the prefilters must produce *different* statistics —
    if they matched, the prefilters were not applied and the loudness test above would only be
    passing by luck.
    """
    source = _source(tmp_path / "src.mp4", seconds=4)
    chain = audio.presence_chain(1.0)
    assert chain

    plain = audio.measure_loudness(source)
    shaped = audio.measure_loudness(source, prefilters=chain)
    assert plain is not None and shaped is not None
    assert shaped.input_i != plain.input_i, (
        "the prefilters did not reach the measurement; the second pass would apply a gain "
        "computed for a signal it is not given"
    )


@requires_ffmpeg
@pytest.mark.real_binary
def test_no_prefilters_reproduces_the_previous_measurement_exactly(tmp_path):
    """The pre-AU11 behaviour verbatim, so an unconfigured job is unaffected."""
    source = _source(tmp_path / "src.mp4", seconds=4)
    assert audio.measure_loudness(source) == audio.measure_loudness(source, prefilters=())


# --- R6.11: no extra pass -------------------------------------------------------------------


def test_the_chain_is_filters_only_and_adds_no_encode():
    """R6.11. These are fragments for a graph that already exists.

    Asserted structurally: anything that shelled out would be a second pass, and the whole point of
    composing onto the existing speech branch is that it is free.
    """
    joined = ",".join(audio.presence_chain(1.0))
    for forbidden in ("-i ", "ffmpeg", "-c:a", "-f null"):
        assert forbidden not in joined, forbidden


# --- availability ---------------------------------------------------------------------------


def test_a_missing_filter_disables_the_chain_rather_than_emitting_it():
    def prober(capability_id: str) -> Capability_Status:
        missing = capability_id.endswith(":acompressor")
        return Capability_Status(capability_id, not missing, "injected")

    assert audio.presence_chain(1.0, prober=prober) == []


def test_a_raising_prober_declines_the_chain_rather_than_crashing():
    """The two failure routes resolve differently, and the distinction is easy to get wrong.

    An earlier version of this test asserted "fails open", on the assumption that the `except` in
    `presence_available` would catch a raising prober. It does not: `Capability_Report._probe`
    catches everything a prober throws and returns an *unavailable* status, so the exception never
    reaches that handler — the same mechanism already documented in `worker/colour.py`.

    So a raising prober disables the chain. That is acceptable here where it would not be for the
    tone-map probe: this is an optional, default-off enhancement, and declining it costs nothing
    anyone will notice.
    """

    def exploding(capability_id: str) -> Capability_Status:
        raise RuntimeError("probe unavailable")

    assert audio.presence_available(exploding) is False
    assert audio.presence_chain(1.0, prober=exploding) == []


def test_an_unimportable_capability_module_does_fail_open():
    """The route the `except` actually guards: an optional enhancement should survive a broken tree."""
    import sys

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "worker.engines.capabilities", None)
        assert audio.presence_available() is True


# --- R6.8: the marker -----------------------------------------------------------------------


def test_the_marker_names_the_resolved_strength():
    """R6.8, and the project's standing rule: report what ran, not what was asked for."""
    assert audio.presence_marker(0.6) == "speech_presence:0.60"
    assert audio.presence_marker(5.0) == "speech_presence:1.00"
    assert audio.presence_marker(0.0) == ""
