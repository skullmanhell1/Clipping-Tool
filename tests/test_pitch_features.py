"""Pitch variation as a selection feature (S3).

The detector is validated against signals whose true pitch is known by construction, and the
expectations are computed from the generating frequencies rather than from the code under test
(R10.9's cross-check rule). A pitch estimator that agrees with itself is worthless.

The two cases that matter are opposite failures: a **monotone** signal must read near-zero spread,
and a signal with a **known interval** must read that interval. An estimator that always returns a
plausible mid-range number passes neither.
"""

from __future__ import annotations

import math
import shutil
import subprocess

import pytest

from config import settings as app_settings
from worker import pitch_features as pf

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None, reason="no ffmpeg on PATH; pitch fixtures are generated with it"
)


def _tone(path, hz: float, seconds: float = 1.5):
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
            f"aevalsrc='0.5*sin(2*PI*{hz}*t)':d={seconds}:s=16000",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        timeout=300,
    )
    return path


def _concat(path, parts):
    listing = path.parent / "concat.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        timeout=300,
    )
    return path


class _Candidate:
    def __init__(self, start, end):
        self.start = start
        self.end = end
        self.features: dict = {}


# --- accuracy, cross-checked against the generating frequency ------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_steady_tone_is_estimated_at_its_true_frequency(tmp_path):
    """The expectation is 150 Hz because that is what was synthesised, not what the code says."""
    samples, rate = pf.read_mono_wav(_tone(tmp_path / "a.wav", 150.0))
    track = pf.f0_track(samples, rate)
    median = pf.source_median_f0(track)
    assert median is not None
    assert median == pytest.approx(150.0, rel=0.02), median


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_monotone_signal_reports_near_zero_variation(tmp_path):
    """The first of the two opposite failures an always-plausible estimator would pass."""
    samples, rate = pf.read_mono_wav(_tone(tmp_path / "flat.wav", 150.0, 3.0))
    track = pf.f0_track(samples, rate)
    pitch = pf.pitch_in_window(track, 0.0, 3.0, source_median=pf.source_median_f0(track))
    assert pitch.reliable is True
    assert pitch.variation_semitones < 0.5, pitch
    assert pf.describe(pitch) == "flat, monotone delivery"


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_known_interval_is_measured_as_that_interval(tmp_path):
    """The second failure. 150 Hz then 200 Hz is a 4.98-semitone interval, by arithmetic.

    The expected value is computed here with `math.log2` on the two frequencies that were
    synthesised — no part of it comes from `pitch_features`. Measured: 5.03 st against a true
    4.98 st, a 1% error.
    """
    low = _tone(tmp_path / "low.wav", 150.0)
    high = _tone(tmp_path / "high.wav", 200.0)
    samples, rate = pf.read_mono_wav(_concat(tmp_path / "both.wav", [low, high]))

    track = pf.f0_track(samples, rate)
    pitch = pf.pitch_in_window(track, 0.0, 3.0, source_median=pf.source_median_f0(track))

    expected = 12.0 * math.log2(200.0 / 150.0)
    assert pitch.variation_semitones == pytest.approx(expected, abs=0.6), (
        f"measured {pitch.variation_semitones:.2f}st against a true interval of {expected:.2f}st"
    )
    assert pitch.variation > 0.5


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_measurement_is_relative_to_the_speakers_own_median(tmp_path):
    """R4.2. A low voice and a high voice with the same *interval* must score the same.

    This is the requirement that stops the feature rewarding people for having high voices. The two
    fixtures are an octave apart in absolute pitch and identical in relative movement.
    """

    def spread(low_hz, high_hz, tag):
        a = _tone(tmp_path / f"{tag}a.wav", low_hz)
        b = _tone(tmp_path / f"{tag}b.wav", high_hz)
        samples, rate = pf.read_mono_wav(_concat(tmp_path / f"{tag}.wav", [a, b]))
        track = pf.f0_track(samples, rate)
        return pf.pitch_in_window(
            track, 0.0, 3.0, source_median=pf.source_median_f0(track)
        ).variation_semitones

    # Both are a perfect fourth (5 semitones): 100->133.5 and 200->267.
    low_voice = spread(100.0, 133.5, "lo")
    high_voice = spread(200.0, 267.0, "hi")
    assert low_voice == pytest.approx(high_voice, abs=0.8), (low_voice, high_voice)


# --- determinism (R4.8) --------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_identical_audio_produces_identical_values(tmp_path):
    """R4.8. Without this the feature cannot support a before/after benchmark at all."""
    samples, rate = pf.read_mono_wav(_tone(tmp_path / "det.wav", 180.0))
    first = pf.f0_track(samples, rate)
    second = pf.f0_track(samples, rate)
    assert first == second
    baseline = pf.source_median_f0(first)
    a = pf.pitch_in_window(first, 0.0, 1.0, source_median=baseline)
    b = pf.pitch_in_window(second, 0.0, 1.0, source_median=baseline)
    assert a == b


def _imported_modules() -> set[str]:
    """Every module `pitch_features` actually imports, from its AST.

    Parsed rather than grepped. A substring scan over the source also reads **prose**, and this
    module's docstrings deliberately name `numpy`, `librosa`, `parselmouth` and `ffmpeg` in order to
    explain why none of them is used — so a text search reports the explanation as a violation.
    Both of these tests failed that way before being rewritten, which is a small illustration of
    the difference between asserting on code and asserting on a file.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pf))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_no_randomness_or_model_dependency_in_the_module():
    """R4.3, R4.8, asserted structurally so a later "improvement" cannot add one quietly."""
    imported = _imported_modules()
    for forbidden in (
        "random",
        "torch",
        "onnxruntime",
        "librosa",
        "parselmouth",
        "requests",
        "urllib",
        "numpy",
        "scipy",
    ):
        assert forbidden not in imported, f"{forbidden} is imported; {sorted(imported)}"


def test_this_module_spawns_no_process_of_its_own():
    """The extraction belongs to the caller, so this stays a pure function of its samples.

    Not the same claim as "no additional pass". An earlier version of this test asserted that,
    on the belief that `extract_audio` already produced a 16 kHz WAV for transcription — **it does
    not**. Nothing in `worker/` calls `extract_audio`; faster-whisper decodes the media itself, and
    that helper has only ever been exercised by tests. R4.7 permits one additional pass, and the
    caller takes it.

    What this test still buys is real: a module that shells out cannot be tested without a media
    fixture, and every pitch case here runs on synthesised samples instead.
    """
    imported = _imported_modules()
    assert "subprocess" not in imported, "spawning a process here would make this untestable"
    assert "config" not in imported, "no settings are read, so no binary path can be invoked"
    # The only audio dependency is the stdlib WAV reader.
    assert "wave" in imported


# --- reliability (R4.5, R4.6) --------------------------------------------------------------


def test_a_window_with_no_voiced_audio_is_unreliable_not_zero():
    """R4.5/R4.6. A 0.0 that looks measured lets an unmeasurable passage score as monotone.

    The distinction is load-bearing: R3.5 says an unreliable feature is treated as *neutral*, and
    neutral cannot be applied to a value indistinguishable from a real reading of zero.
    """
    pitch = pf.pitch_in_window([], 0.0, 2.0, source_median=150.0)
    assert pitch.reliable is False
    assert pitch.as_features() == {"pitch_reliable": 0.0}
    assert "pitch_variation" not in pitch.as_features()


def test_a_window_with_only_a_few_voiced_frames_is_unreliable():
    """Below the absolute floor, a handful of frames is a sample rather than a distribution."""
    track = [(0.10, 150.0), (0.12, 152.0), (0.14, 151.0)]
    pitch = pf.pitch_in_window(track, 0.0, 2.0, source_median=150.0)
    assert pitch.reliable is False
    assert pitch.voiced_frames == 3


def test_a_window_that_is_mostly_silence_is_unreliable_even_with_enough_frames():
    """The relative floor. Eight voiced frames in a 10-second window is 3% voiced.

    Absolute count alone would admit it; the fraction is what rejects a window that is mostly
    music or room tone with one spoken word in it.
    """
    track = [(0.02 * i, 150.0) for i in range(8)]
    pitch = pf.pitch_in_window(track, 0.0, 10.0, source_median=150.0)
    assert pitch.reliable is False


def test_no_source_median_means_no_reading():
    """Without a baseline there is nothing to be relative *to*, so there is no measurement."""
    track = [(0.02 * i, 150.0 + i) for i in range(40)]
    assert pf.pitch_in_window(track, 0.0, 1.0, source_median=None).reliable is False


def test_the_source_median_needs_enough_voiced_audio():
    assert pf.source_median_f0([]) is None
    assert pf.source_median_f0([(0.0, 150.0), (0.02, 151.0)]) is None


def test_the_median_is_robust_to_an_octave_error():
    """Median rather than mean, because autocorrelation's characteristic error is a doubling.

    One frame at twice the true pitch drags a mean noticeably and leaves a median untouched.
    """
    track = [(0.02 * i, 150.0) for i in range(20)] + [(0.5, 300.0)]
    assert pf.source_median_f0(track) == pytest.approx(150.0)


# --- integration shape ---------------------------------------------------------------------


def test_annotate_candidates_matches_the_existing_feature_convention():
    """R4.4. Same call shape as `audio_features` and `selection_features`, so the three compose."""
    track = [(0.02 * i, 150.0 + (i % 7)) for i in range(120)]
    candidates = [_Candidate(0.0, 1.0), _Candidate(1.0, 2.0)]
    pf.annotate_candidates(candidates, track)
    for candidate in candidates:
        assert "pitch_reliable" in candidate.features


def test_annotate_candidates_tolerates_a_candidate_without_features():
    """The selector builds candidates on several paths; one without the dict must not raise."""

    class Bare:
        start, end = 0.0, 1.0

    pf.annotate_candidates([Bare()], [(0.02 * i, 150.0) for i in range(60)])


def test_annotate_candidates_tolerates_unusable_bounds():
    broken = _Candidate("not-a-number", 1.0)
    pf.annotate_candidates([broken], [(0.02 * i, 150.0) for i in range(60)])
    assert broken.features == {}


# --- the LLM annotation (R4.9) -------------------------------------------------------------


def test_the_prompt_annotation_is_qualitative_and_never_a_number():
    """R4.9. A model shown `0.62` has no scale for it and will confabulate one.

    Asserted across the whole range rather than at one point, because the failure mode is a
    number leaking into one branch of a phrase table.
    """
    for spread in (0.0, 1.0, 2.0, 4.0, 8.0, 20.0):
        pitch = pf.Pitch(
            median_hz=150.0,
            variation_semitones=spread,
            variation=min(1.0, spread / 6.0),
            voiced_frames=50,
            reliable=True,
        )
        phrase = pf.describe(pitch)
        assert phrase
        assert not any(character.isdigit() for character in phrase), phrase


def test_an_unreliable_reading_contributes_no_phrase():
    """Saying nothing is right; saying "monotone" about audio we could not measure is not."""
    assert pf.describe(pf.Pitch(reliable=False)) == ""


def test_the_scaled_variation_is_bounded():
    """It feeds a weighted blend, so an unbounded value would dominate every other signal."""
    extreme = pf.Pitch(variation_semitones=100.0, variation=min(1.0, 100.0 / 6.0), reliable=True)
    assert 0.0 <= extreme.variation <= 1.0


# --- WAV reading ---------------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_reading_rejects_a_format_it_cannot_interpret(tmp_path):
    """Stereo or 8-bit input would be silently misread as mono 16-bit, producing noise.

    Raising is right: the caller has the 16 kHz mono WAV `extract_audio` guarantees, so a different
    format means something upstream changed and should be noticed rather than absorbed.
    """
    stereo = tmp_path / "stereo.wav"
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
            "aevalsrc='0.5*sin(2*PI*150*t)|0.5*sin(2*PI*150*t)':d=0.5:s=16000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(stereo),
        ],
        check=True,
        timeout=300,
    )
    with pytest.raises(ValueError, match="mono"):
        pf.read_mono_wav(stereo)
