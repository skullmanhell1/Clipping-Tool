"""Separator_Backend / Stem_Set property module for the audio-stem-inpainting spec
(``worker/engines/stems.py``).

Covers the assembly properties of epic 8: **P9** (the Stem_Set is always exactly three
stems, assembled in sorted order, task 8.3) and **P10** (stems decompose additively and
preserve the Audio_Format, task 8.4). Epic 5's **P11** (gain resolution) and epic 9's
**P3**-adjacent backend properties land in this same file when the emitters and adapters
that P11 asserts against exist (tasks 11.3 / 13.4).

Both properties use the injected seams rather than a real model: the Stem_Set comes from
:class:`tests.fakes.Fake_Separator_Backend` and friends, and every ffmpeg invocation goes
through an injected runner — :class:`tests.fakes.Recording_Command_Runner` for the
command-level clauses (no binary needed) and a real ``subprocess.run`` for the sample-level
clauses, which need actual audio. Nothing here imports ``demucs``, reads a model file or
touches the network (Reqs 19.1, 19.5, 19.7).
"""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import requires_ffmpeg
from tests.fakes import (
    Fake_Separator_Backend,
    Recording_Command_Runner,
    Truncating_Separator_Backend,
    write_pcm_wav,
)
from tests.strategies import st_audio_format, st_backend_stem_sets, st_pcm_frames
from worker.engines import stems

_FULL_SCALE = 32767


def _real_runner(cmd, timeout_s):
    """A real ``Command_Runner``: the one place these tests execute ffmpeg."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)


def _int16(sample: float) -> int:
    """One float sample in ``[-1, 1]`` as a clamped 16-bit integer."""
    return max(-_FULL_SCALE, min(_FULL_SCALE, int(round(float(sample) * _FULL_SCALE))))


def _pack(frames) -> bytes:
    """Interleaved float frames as little-endian 16-bit PCM."""
    flat = [_int16(sample) for frame in frames for sample in frame]
    return struct.pack("<%dh" % len(flat), *flat) if flat else b""


def _read_samples(path: Path) -> list[int]:
    """Every interleaved 16-bit sample of a PCM WAV, in order.

    Walks the RIFF chunks by hand rather than using ``wave``, which refuses the
    ``WAVE_FORMAT_EXTENSIBLE`` header ffmpeg writes for more than two channels — the same
    reason ``stems._wav_format`` parses the header itself.
    """
    payload = b""
    with open(path, "rb") as handle:
        handle.read(12)
        while True:
            chunk = handle.read(8)
            if len(chunk) < 8:
                break
            name, size = struct.unpack("<4sI", chunk)
            if name == b"data":
                payload = handle.read(size)
                break
            handle.seek(size + (size & 1), 1)
    count = len(payload) // 2
    return list(struct.unpack("<%dh" % count, payload[: count * 2]))


# --------------------------------------------------------------------------- #
# P9 — the Stem_Set is always three stems in sorted order (task 8.3)          #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 9: The Stem_Set is always exactly three stems,
# assembled in sorted order
@settings(max_examples=100, deadline=None)
@given(case=st_backend_stem_sets(), fmt=st_audio_format(valid_only=True))
def test_p9_stem_set_is_always_three_stems_assembled_in_sorted_order(
    case: dict, fmt: dict, tmp_path_factory
) -> None:
    """Whatever the backend returned, the Stem_Set is ``{music, other, vocals}``.

    The generator draws four-stem, two-stem, unknown-name and omission shapes **in a
    permuted key order**, and its ``expected_contributors`` / ``expected_missing`` are the
    oracle. Asserted here:

    * the assembled keys are exactly :data:`STEM_NAMES`, in sorted order (Req 4.1, 4.9);
    * ``drums`` and ``bass`` are **summed** into ``music`` — the emitted ``amix`` carries
      one ``-i`` per contributor, in sorted Backend_Stem order, with ``normalize=0`` so the
      sum is a sum (Req 4.2);
    * an unknown Backend_Stem contributes to nothing (Req 4.3);
    * each Stem_Name with no contributor gets one ``anullsrc`` silence file of the clip's
      duration and **exactly one** ``stem_missing:<name>`` detail (Req 4.3);
    * the whole emitted command sequence is **identical across permutations** of the
      backend's dict iteration order (Req 4.9) — the clause that makes the ffmpeg argv, and
      therefore the output bytes, order-independent.

    The drawn paths are never created on disk (the generator's contract), so this runs with
    no ffmpeg binary at all: the recording runner replays every call and the format check
    has nothing to read. The sample-level guarantees are P10's job.
    """
    audio = stems.Audio_Format(**fmt)
    duration = 0.5
    dest = tmp_path_factory.mktemp("assembled")

    runner = Recording_Command_Runner()
    stem_set, details = stems.assemble_stem_set(
        case["raw"], dest_dir=dest, fmt=audio, duration=duration,
        runner=runner, timeout_s=10.0,
    )

    assert list(stem_set) == list(stems.STEM_NAMES) == sorted(stems.STEM_NAMES)
    assert details == tuple(f"stem_missing:{name}" for name in case["expected_missing"])
    assert len(details) == len(set(details))

    # One anullsrc call per missing stem, one amix call per summed stem.
    silence_calls = [call for call in runner.calls if "anullsrc" in " ".join(call.argv)]
    assert len(silence_calls) == len(case["expected_missing"])
    for call in silence_calls:
        assert f"{duration:.6f}" in call.argv
        assert str(int(audio.sample_rate)) in call.argv
        assert call.timeout_s is not None and call.timeout_s > 0.0

    for name, contributors in case["expected_contributors"].items():
        if len(contributors) < 2:
            continue
        summed = [
            call for call in runner.calls
            if any(part.endswith(f"{name}.wav") for part in call.argv)
            and "amix" in " ".join(call.argv)
        ]
        assert len(summed) == 1, name
        argv = summed[0].argv
        inputs = [argv[i + 1] for i, part in enumerate(argv) if part == "-i"]
        assert [Path(path).stem for path in inputs] == sorted(contributors)
        assert f"amix=inputs={len(contributors)}:normalize=0" in " ".join(argv)

    # Permutation independence (Req 4.9): the same mapping in any key order emits the same
    # commands and assembles to the same relative Stem_Set.
    permuted = dict(reversed(list(case["raw"].items())))
    other_runner = Recording_Command_Runner()
    other_set, other_details = stems.assemble_stem_set(
        permuted, dest_dir=dest, fmt=audio, duration=duration,
        runner=other_runner, timeout_s=10.0,
    )
    assert other_details == details
    assert other_set == stem_set
    assert other_runner.argvs == runner.argvs


# --------------------------------------------------------------------------- #
# P10 — stems decompose additively and preserve the Audio_Format (task 8.4)   #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 10: Stems decompose additively and preserve the
# Audio_Format
@requires_ffmpeg
@settings(max_examples=100, deadline=None)
@given(
    pcm=st_pcm_frames(),
    fmt=st_audio_format(valid_only=True),
    shape=st.sampled_from([("vocals", "drums", "bass", "other"), ("vocals", "music")]),
)
def test_p10_stems_decompose_additively_and_preserve_the_audio_format(
    pcm: dict, fmt: dict, shape: tuple, tmp_path_factory
) -> None:
    """Summing the Stem_Set at unit gain reproduces the input, sample for sample.

    The clip audio is written from the drawn frames, separated by
    :class:`Fake_Separator_Backend` in its ``sum_to_input`` mode — the whole signal in one
    stem, digital silence in the others, so the decomposition is additive **by
    construction** rather than by luck — and then assembled, which is where ``drums`` and
    ``bass`` are summed into ``music`` by a real ``amix`` pass.

    Asserted on the *assembled* set, which is what the mix filtergraph consumes:

    * every stem's sample rate, channel count and duration equal the probed
      :class:`Audio_Format` values (Req 4.6, 5.5);
    * summing all three stems at unit gain reproduces the incoming audio within
      :data:`AMPLITUDE_TOLERANCE` per sample — one 16-bit LSB (Req 4.7, 13.3).

    ``fmt``'s sample rate and channel count are taken from the drawn PCM buffer, since a
    stem can only preserve a format the source actually has; ``codec`` and ``start_time``
    come from the generator.
    """
    channels = int(pcm["channels"])
    sample_rate = int(pcm["sample_rate"])
    audio = stems.Audio_Format(
        sample_rate=sample_rate,
        channels=channels,
        codec=str(fmt["codec"]),
        start_time=float(fmt["start_time"]),
    )
    frames = [frame for frame in pcm["frames"]]
    root = tmp_path_factory.mktemp("additive")
    source = write_pcm_wav(
        root / "in.wav", _pack(frames), sample_rate=sample_rate, channels=channels
    )
    duration = len(frames) / float(sample_rate)

    backend = Fake_Separator_Backend(sum_to_input=True, stems=shape)
    raw = backend.separate(
        source, root / "stems", fmt=audio, seed=11, timeout_s=10.0
    )
    stem_set, _details = stems.assemble_stem_set(
        raw, dest_dir=root / "assembled", fmt=audio, duration=duration,
        runner=_real_runner, timeout_s=30.0,
    )

    # Format preservation (Req 4.6).
    for name, path in stem_set.items():
        probed = stems._wav_format(path)
        assert probed is not None, name
        assert probed[0] == sample_rate, name
        assert probed[1] == channels, name
        assert abs(probed[2] - len(frames)) <= 1, name

    # Additive decomposition within one 16-bit LSB (Req 4.7).
    expected = _read_samples(source)
    totals = [0] * len(expected)
    for path in stem_set.values():
        for index, sample in enumerate(_read_samples(path)):
            if index < len(totals):
                totals[index] += sample
    for index, value in enumerate(totals):
        residual = abs(value - expected[index]) / 32768.0
        assert residual <= stems.AMPLITUDE_TOLERANCE, (index, value, expected[index])


@requires_ffmpeg
def test_a_wrong_length_stem_is_an_integrity_error(tmp_path: Path) -> None:
    """A backend that returns half the audio fails verification (Req 4.6, 14.2).

    The companion to P10's positive clause, and the reason assembly verifies at all: with
    :class:`Truncating_Separator_Backend` every stem is a readable WAV at the right format
    and only the *length* is wrong, so nothing but the explicit duration check can catch it.
    """
    audio = stems.Audio_Format(48000, 2, "pcm_s16le", 0.0)
    backend = Truncating_Separator_Backend(scale=0.5, duration_s=0.25)
    raw = backend.separate(tmp_path / "missing.wav", tmp_path / "stems",
                           fmt=audio, seed=3, timeout_s=5.0)

    try:
        stems.assemble_stem_set(
            raw, dest_dir=tmp_path / "out", fmt=audio, duration=0.25,
            runner=_real_runner, timeout_s=20.0,
        )
    except stems.Integrity_Error as exc:
        assert "frames" in str(exc)
    else:  # pragma: no cover - the check must fire
        raise AssertionError("a truncated stem must raise Integrity_Error")
