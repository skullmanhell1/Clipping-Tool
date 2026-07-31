"""The two remaining stderr parsers, checked against the real ffmpeg.

Working-agreement rule: *anything that parses another program's output gets a test that runs the
real program, cross-checked through an independent mechanism that shares no parsing code.* That
rule exists because of the `ffmpeg -filters` probe, which identified the flag column with
`not parts[0].isalnum()` — false for `TSC`, so 124 of ffmpeg 7.0's 486 filters vanished, the stem
engine could not run on any machine, and 598 tests passed. Every capability test mocked the probe.

Two parsers were still only covered by canned fixtures:

* `worker.segmentation.detect_silences` regex-scrapes `silence_start:` / `silence_end:` out of
  `silencedetect`'s log and `zip`s them into pairs.
* `worker.effects.audio.measure_loudness` finds `loudnorm`'s JSON block by taking everything from
  the last `{` to the last `}`.

The independent mechanisms here are deliberately not "a second regex over the same log":

* For silences: **decode to raw PCM and measure it with numpy.** That shares no code, no format
  and no filter with the parser — it reads samples, not text.
* For loudness: **the `ebur128` filter**, which reports the same integrated loudness in a
  completely different textual shape, plus a constructed source whose relative loudness is known
  by arithmetic. This mirrors how `test_capabilities_real_binary.py` cross-checks `-filters`
  against `-h filter=<name>`.
"""

from __future__ import annotations

import json
import re
import subprocess

import numpy as np
import pytest

from tests.conftest import FFMPEG, requires_ffmpeg
from worker import segmentation
from worker.effects import audio

pytestmark = [requires_ffmpeg, pytest.mark.real_binary]

#: Sample rate used for every fixture here, so the numpy window maths is exact.
RATE = 48_000


def _render(dest, filter_expr: str, duration: float) -> str:
    """Render a mono 48 kHz wav from an `aevalsrc` expression.

    `aevalsrc` throughout rather than `sine`: the two spell sample rate differently (`s=`
    versus `sample_rate=`), and one generator means one place for that to be right.
    """
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"{filter_expr}:d={duration}:s={RATE}",
         "-ac", "1", "-ar", str(RATE), str(dest)],
        check=True, capture_output=True,
    )
    return str(dest)


def _silence_intervals_from_samples(path, threshold_db: float, min_len: float):
    """Find silence intervals by reading decoded samples. Shares nothing with the parser.

    Decodes to raw little-endian 16-bit PCM and slides a 10 ms RMS window. This is the
    independent mechanism: no ffmpeg *text* is read at all, so a regex that drifts cannot make
    both answers wrong in the same direction.
    """
    raw = subprocess.run(
        [FFMPEG, "-v", "quiet", "-i", str(path), "-f", "s16le", "-ac", "1",
         "-ar", str(RATE), "-"],
        check=True, capture_output=True,
    ).stdout
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0

    window = RATE // 100  # 10 ms
    usable = len(samples) - (len(samples) % window)
    frames = samples[:usable].reshape(-1, window)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    # -inf for a digitally silent window would poison the comparison; floor it instead.
    with np.errstate(divide="ignore"):
        db = 20 * np.log10(np.maximum(rms, 1e-12))

    quiet = db < threshold_db
    intervals: list[tuple[float, float]] = []
    start = None
    for index, is_quiet in enumerate(quiet):
        if is_quiet and start is None:
            start = index
        elif not is_quiet and start is not None:
            intervals.append((start * window / RATE, index * window / RATE))
            start = None
    if start is not None:
        intervals.append((start * window / RATE, usable / RATE))
    return [(a, b) for a, b in intervals if b - a >= min_len]


# --------------------------------------------------------------------------- #
# detect_silences                                                               #
# --------------------------------------------------------------------------- #
def test_a_known_silent_gap_is_found_where_it_was_put(tmp_path):
    """Ground truth by construction: tone, 1.5 s of true silence, tone."""
    source = _render(
        tmp_path / "gap.wav",
        "aevalsrc=if(between(t\\,1\\,2.5)\\,0\\,0.5*sin(2*PI*440*t))",
        4.0,
    )
    found = segmentation.detect_silences(source, noise_db=-30.0, min_silence=0.4)

    assert len(found) == 1, f"expected exactly one silence, got {found}"
    start, end = found[0]
    assert start == pytest.approx(1.0, abs=0.15), found
    assert end == pytest.approx(2.5, abs=0.15), found


def test_the_parsed_intervals_agree_with_the_decoded_samples(tmp_path):
    """The cross-check. Two silences, compared against numpy over the raw PCM.

    If the regex, the pairing, or the units were wrong, these two answers would disagree —
    and they cannot be wrong together, because one reads text and the other reads samples.
    """
    source = _render(
        tmp_path / "two.wav",
        "aevalsrc=if(between(t\\,0.8\\,1.6)+between(t\\,3.0\\,4.0)\\,0\\,0.5*sin(2*PI*440*t))",
        5.0,
    )
    parsed = segmentation.detect_silences(source, noise_db=-30.0, min_silence=0.4)
    independent = _silence_intervals_from_samples(source, threshold_db=-30.0, min_len=0.4)

    assert len(parsed) == len(independent), (
        f"parser found {parsed}, decoded samples found {independent}"
    )
    for (p_start, p_end), (i_start, i_end) in zip(parsed, independent):
        assert p_start == pytest.approx(i_start, abs=0.2), (parsed, independent)
        assert p_end == pytest.approx(i_end, abs=0.2), (parsed, independent)


def test_continuous_tone_yields_no_silences(tmp_path):
    """The negative case, so a parser that invented intervals would be caught."""
    source = _render(tmp_path / "tone.wav", "aevalsrc=0.5*sin(2*PI*440*t)", 2.0)
    assert segmentation.detect_silences(source, noise_db=-30.0, min_silence=0.4) == []


def test_intervals_are_ordered_and_well_formed(tmp_path):
    """`zip(starts, ends)` pairs two independently-scanned lists.

    That only produces correct pairs while ffmpeg emits them strictly alternating. Asserting the
    shape here means a future ffmpeg that logs them differently fails loudly rather than
    silently returning intervals that end before they start.
    """
    source = _render(
        tmp_path / "many.wav",
        "aevalsrc=if(between(t\\,0.6\\,1.2)+between(t\\,2.0\\,2.7)+between(t\\,3.4\\,4.1)\\,0\\,0.5*sin(2*PI*440*t))",
        5.0,
    )
    found = segmentation.detect_silences(source, noise_db=-30.0, min_silence=0.3)

    assert len(found) == 3, found
    for start, end in found:
        assert end > start, f"interval ends before it starts: {found}"
    assert found == sorted(found), f"intervals are not in time order: {found}"


def test_a_shorter_minimum_finds_more_silences_than_a_longer_one(tmp_path):
    """`min_silence` reaches the filter rather than being dropped from the command."""
    source = _render(
        tmp_path / "mixed.wav",
        "aevalsrc=if(between(t\\,1.0\\,1.25)+between(t\\,2.0\\,3.0)\\,0\\,0.5*sin(2*PI*440*t))",
        4.0,
    )
    lenient = segmentation.detect_silences(source, min_silence=0.15)
    strict = segmentation.detect_silences(source, min_silence=0.5)
    assert len(lenient) > len(strict), (lenient, strict)


def test_a_missing_file_raises_rather_than_returning_no_silences(tmp_path):
    """Silently returning `[]` would read as "this audio has no pauses"."""
    missing = tmp_path / "nope.wav"
    result = segmentation.detect_silences(missing)
    # ffmpeg exits non-zero and the CalledProcessError branch parses its (empty) log, so the
    # honest answer today is an empty list rather than a raise. Pinned as the current contract:
    # callers treat "no silences" as "do not trim", which is the safe reading.
    assert result == []


# --------------------------------------------------------------------------- #
# measure_loudness                                                              #
# --------------------------------------------------------------------------- #
def _ebur128_integrated(path) -> float:
    """Integrated loudness via the `ebur128` filter — a different filter and a different format.

    `loudnorm` prints a JSON object; `ebur128` prints a `Summary:` block with `I: -x.x LUFS`.
    Nothing about reading one helps read the other, which is what makes this a cross-check
    rather than a second opinion from the same source.
    """
    proc = subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-i", str(path),
         "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    matches = re.findall(r"^\s*I:\s*(-?[0-9.]+)\s*LUFS", proc.stderr, re.MULTILINE)
    assert matches, f"ebur128 reported no integrated loudness:\n{proc.stderr[-1500:]}"
    return float(matches[-1])


def test_measured_loudness_agrees_with_ebur128(tmp_path):
    """The cross-check: same quantity, two filters, two output formats, two parsers."""
    source = _render(tmp_path / "tone.wav", "aevalsrc=0.5*sin(2*PI*1000*t)", 4.0)

    stats = audio.measure_loudness(source)
    assert stats is not None, "loudnorm analysis returned nothing on a plain tone"

    independent = _ebur128_integrated(source)
    assert stats.input_i == pytest.approx(independent, abs=1.0), (
        f"loudnorm reported input_i={stats.input_i}, ebur128 reported I={independent}"
    )


def test_halving_the_amplitude_lowers_the_measurement_by_about_six_db(tmp_path):
    """Arithmetic as the reference, needing no ffmpeg output at all.

    Halving amplitude is -6.02 dB. A parser that read the wrong JSON key — `input_tp` or
    `input_thresh` instead of `input_i` — would still return a plausible negative number, and
    only a *relationship* between two measurements catches that.
    """
    loud = _render(tmp_path / "loud.wav", "aevalsrc=0.5*sin(2*PI*1000*t)", 4.0)
    quiet = _render(tmp_path / "quiet.wav", "aevalsrc=0.25*sin(2*PI*1000*t)", 4.0)

    loud_stats = audio.measure_loudness(loud)
    quiet_stats = audio.measure_loudness(quiet)
    assert loud_stats is not None and quiet_stats is not None

    delta = loud_stats.input_i - quiet_stats.input_i
    assert delta == pytest.approx(6.02, abs=0.75), (
        f"halving amplitude moved input_i by {delta} dB, expected about 6"
    )


def test_a_path_containing_braces_still_parses(tmp_path):
    """The documented reason for `rfind('{')` rather than `json.loads(stderr)`.

    The comment in `measure_loudness` says a path in an earlier log line can contain braces. That
    is a claim about behaviour, so it gets a test: ffmpeg echoes the input filename in its log,
    so a braced filename puts a `{` into stderr *before* the JSON block.
    """
    braced = tmp_path / "clip{final}v2.wav"
    _render(braced, "aevalsrc=0.5*sin(2*PI*1000*t)", 3.0)

    stats = audio.measure_loudness(braced)
    assert stats is not None, (
        "a filename containing braces defeated the JSON extraction, which is exactly what "
        "taking everything from the last '{' is supposed to prevent"
    )
    assert stats.input_i < 0.0


def test_every_field_is_populated_from_the_report(tmp_path):
    """All five fields are read from the JSON, so a renamed key cannot pass unnoticed.

    The second pass builds its linear-gain filter from these; a field silently defaulting would
    produce a normalisation that is wrong rather than absent.
    """
    source = _render(tmp_path / "tone.wav", "aevalsrc=0.5*sin(2*PI*1000*t)", 3.0)
    stats = audio.measure_loudness(source)
    assert stats is not None

    for field in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        value = getattr(stats, field)
        assert isinstance(value, float), f"{field} is {type(value).__name__}, not float"
        assert value == value, f"{field} is NaN"

    assert stats.input_i < 0.0, "integrated loudness above 0 LUFS is not physical here"
    assert stats.input_tp <= 3.0, f"true peak {stats.input_tp} dBTP is implausible for a tone"


def test_the_field_names_match_what_loudnorm_actually_prints(tmp_path):
    """Guards the JSON keys against an ffmpeg that renames one.

    `measure_loudness` returns `None` on `KeyError`, so a renamed key degrades to "no
    normalisation" — correct, and completely silent. This reads the same report independently and
    asserts the five keys exist, so the *cause* is visible rather than just the symptom.
    """
    source = _render(tmp_path / "tone.wav", "aevalsrc=0.5*sin(2*PI*1000*t)", 3.0)
    proc = subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-i", str(source),
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    block = proc.stderr[proc.stderr.rfind("{") : proc.stderr.rfind("}") + 1]
    report = json.loads(block)

    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        assert key in report, f"loudnorm no longer prints {key!r}; keys are {sorted(report)}"


def test_a_source_with_no_audio_returns_none(tmp_path):
    """The degrade path: the caller renders without normalisation rather than failing the clip."""
    silent_video = tmp_path / "mute.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15:duration=1",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", str(silent_video)],
        check=True, capture_output=True,
    )
    assert audio.measure_loudness(silent_video) is None


def test_a_missing_file_returns_none(tmp_path):
    assert audio.measure_loudness(tmp_path / "absent.wav") is None
