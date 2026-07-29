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

import os
import re
import socket
import struct
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import requires_ffmpeg
from tests.fakes import (
    Fake_Separator_Backend,
    Recording_Command_Runner,
    Truncating_Separator_Backend,
    write_pcm_wav,
)
from tests.strategies import (
    st_audio_format,
    st_backend_stem_sets,
    st_pcm_frames,
    st_stem_options,
)
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



# =========================================================================== #
# Epic 9 — the two backend adapters (tasks 9.1-9.5)                           #
# =========================================================================== #

def _fmt(**overrides) -> stems.Audio_Format:
    """A valid probed ``Audio_Format``."""
    fields = {"sample_rate": 48000, "channels": 2, "codec": "aac", "start_time": 0.0}
    fields.update(overrides)
    return stems.Audio_Format(**fields)


def _identity_loader(_checkpoint, frames: bytes, _fmt_, _seed):
    """A deterministic stand-in for ``demucs``: the whole signal into ``vocals``.

    Pure and byte-exact, so it needs no ``torch``/``numpy`` and the additive decomposition
    holds by construction. This is the "fake model shim" P20's determinism clause is
    asserted behind.
    """
    quiet = bytes(len(frames))
    return {"bass": quiet, "drums": quiet, "other": quiet, "vocals": frames}


def _offset_loader(delta: int):
    """A loader whose output differs from :func:`_identity_loader` by ``delta`` LSB.

    Stands in for "the same run in a *different* environment": the plan, the Stem_Set, the
    format and the duration are all identical, and only the samples differ, by less than
    :data:`AMPLITUDE_TOLERANCE`.
    """

    def loader(_checkpoint, frames: bytes, _fmt_, _seed):
        count = len(frames) // 2
        values = struct.unpack("<%dh" % count, frames[: count * 2])
        nudged = struct.pack(
            "<%dh" % count,
            *[max(-32768, min(32767, value + delta)) for value in values],
        )
        quiet = bytes(len(nudged))
        return {"bass": quiet, "drums": quiet, "other": quiet, "vocals": nudged}

    return loader


# --------------------------------------------------------------------------- #
# Task 9.1 — the model locator                                                #
# --------------------------------------------------------------------------- #
def test_the_locator_reports_nothing_for_an_empty_directory(tmp_path) -> None:
    """``model:<name>`` must mean "present locally", not "fetchable" (Req 12.4, 12.6)."""
    assert stems._locate_model("htdemucs", tmp_path) is None
    assert stems._locate_model("htdemucs", tmp_path / "missing") is None
    assert stems._locate_model("", tmp_path) is None


@pytest.mark.parametrize("layout", ["file", "directory"])
def test_the_locator_finds_both_documented_layouts(tmp_path, layout: str) -> None:
    """``<dir>/<name>.th`` and ``<dir>/<name>/model.th`` are both accepted (Req 12.3)."""
    if layout == "file":
        expected = tmp_path / "htdemucs.th"
        expected.write_bytes(b"checkpoint")
    else:
        expected = tmp_path / "htdemucs" / "model.th"
        expected.parent.mkdir(parents=True)
        expected.write_bytes(b"checkpoint")

    assert stems._locate_model("htdemucs", tmp_path) == expected


def test_the_locator_honours_the_environment_variable(tmp_path, monkeypatch) -> None:
    """The model directory is operator-configurable without touching ``config.py``."""
    (tmp_path / "htdemucs.th").write_bytes(b"checkpoint")
    monkeypatch.setenv(stems.MODEL_DIR_ENV, str(tmp_path))

    assert stems._locate_model("htdemucs") == tmp_path / "htdemucs.th"
    assert stems._model_dir() == tmp_path


def test_the_locator_is_registered_at_import_time(tmp_path) -> None:
    """Importing the module registers ``model:htdemucs`` with the capability layer.

    Asserted in a **fresh interpreter** rather than against the live process, on purpose.
    ``MODEL_LOCATORS`` is a mutable process-global, and the foundation's own host tests clear
    it in their isolation fixture (``tests/test_engine_host.py``) without restoring it — so
    the live dict is not a reliable witness to what module import does, and a test that read
    it would pass or fail depending on file ordering.

    A subprocess also asserts the stronger thing we actually care about: that the
    registration is a **side effect of importing the module**, so ``/api/info`` and the
    capability probe see ``model:htdemucs`` without anyone calling a setup hook (Req 12.3,
    21.5).
    """
    root = Path(__file__).resolve().parents[1]
    script = (
        "from worker.engines import stems\n"
        "from worker.engines.capabilities import MODEL_LOCATORS\n"
        "assert stems._MODEL_DEFAULT in MODEL_LOCATORS, 'not registered at import'\n"
        "print(MODEL_LOCATORS[stems._MODEL_DEFAULT]())\n"
    )
    env = {**os.environ, stems.MODEL_DIR_ENV: str(tmp_path), "PYTHONPATH": str(root)}

    absent = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(root), env=env, timeout=60,
    )
    assert absent.returncode == 0, absent.stderr
    assert absent.stdout.strip() == "None"

    (tmp_path / "htdemucs.th").write_bytes(b"checkpoint")
    present = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(root), env=env, timeout=60,
    )
    assert present.returncode == 0, present.stderr
    assert present.stdout.strip() == str(tmp_path / "htdemucs.th")


def test_the_locator_never_imports_or_downloads(tmp_path, monkeypatch) -> None:
    """A probe that could fetch would make the capability mean the wrong thing (Req 12.5)."""
    monkeypatch.setattr(socket, "socket", _no_socket)
    for module in ("torch", "demucs"):
        monkeypatch.setitem(sys.modules, module, None)

    assert stems._locate_model("htdemucs", tmp_path) is None      # no raise


def _no_socket(*_args, **_kwargs):
    """Stand-in for ``socket.socket`` that fails loudly if anything opens a socket."""
    raise AssertionError("the stem engine must never open a socket (Req 16.7)")


@contextmanager
def _sockets_blocked():
    """Block ``socket.socket`` for the duration of the block.

    A context manager rather than the ``monkeypatch`` fixture because Hypothesis rejects a
    function-scoped fixture inside ``@given`` (the fixture would be set up once and reused
    across every generated example, which is exactly the kind of shared state that makes a
    property test lie).
    """
    original = socket.socket
    socket.socket = _no_socket
    try:
        yield
    finally:
        socket.socket = original


# --------------------------------------------------------------------------- #
# Task 9.1 — the ML adapter's refusal, pinning and format handling            #
# --------------------------------------------------------------------------- #
def test_a_missing_checkpoint_is_refused_before_any_import(tmp_path, monkeypatch) -> None:
    """No download can be triggered from inside ``run`` (Req 12.4, 12.6, 16.1).

    ``torch``/``demucs`` are made **unimportable**, so if the adapter tried to import before
    resolving the checkpoint the failure would surface as an ``ImportError``/``TypeError``
    rather than :class:`Model_Unavailable`. Getting the documented exception is therefore
    evidence about the *ordering*, not just the outcome.
    """
    monkeypatch.setattr(socket, "socket", _no_socket)
    for module in ("torch", "demucs", "demucs.apply", "demucs.pretrained"):
        monkeypatch.setitem(sys.modules, module, None)

    backend = stems.ML_Separator_Backend(model_dir=tmp_path)
    assert backend.requires_network is False
    assert backend.backend_id == "ml"

    with pytest.raises(stems.Model_Unavailable):
        backend.separate(
            tmp_path / "in.wav", tmp_path / "stems",
            fmt=_fmt(), seed=7, timeout_s=30.0,
        )


class _TorchShim:
    """A recording stand-in for the parts of ``torch`` the adapter configures."""

    def __init__(self, *, deterministic_raises: bool = False) -> None:
        self.threads: list[int] = []
        self.seeds: list[int] = []
        self.grad: list[bool] = []
        self.deterministic: list[bool] = []
        self._raises = deterministic_raises

    def set_num_threads(self, count):
        self.threads.append(count)

    def manual_seed(self, seed):
        self.seeds.append(seed)

    def set_grad_enabled(self, flag):
        self.grad.append(flag)

    def use_deterministic_algorithms(self, flag):
        if self._raises:
            raise RuntimeError("unsupported in this build")
        self.deterministic.append(flag)


def test_inference_is_pinned_to_one_seeded_cpu_thread() -> None:
    """One thread and an explicit seed are the whole basis of the Req 10.4 claim."""
    shim = _TorchShim()
    stems._pin_torch(shim, 0x1_0000_0009)

    assert shim.threads == [stems.ML_THREAD_COUNT] == [1]
    assert shim.grad == [False]
    assert shim.seeds == [9]                       # masked into 32 bits
    assert shim.deterministic == [True]


def test_a_build_without_deterministic_algorithms_still_runs() -> None:
    """Best-effort: refusing to run on an older build is the worse trade."""
    shim = _TorchShim(deterministic_raises=True)
    stems._pin_torch(shim, 3)                      # must not raise

    assert shim.threads == [1] and shim.seeds == [3]
    assert shim.deterministic == []


def test_the_ml_adapter_writes_one_wav_per_backend_stem(tmp_path) -> None:
    """Backend_Stem names are returned unmapped — ``assemble_stem_set`` owns the mapping."""
    fmt = _fmt(sample_rate=8000, channels=1)
    source = tmp_path / "in.wav"
    write_pcm_wav(source, _pack([(0.5,), (-0.5,), (0.25,)]), sample_rate=8000, channels=1)
    (tmp_path / "htdemucs.th").write_bytes(b"checkpoint")

    backend = stems.ML_Separator_Backend(
        model_dir=tmp_path, loader=_identity_loader
    )
    written = backend.separate(
        source, tmp_path / "stems", fmt=fmt, seed=1, timeout_s=30.0
    )

    assert sorted(written) == ["bass", "drums", "other", "vocals"]
    for path in written.values():
        probed = stems._wav_format(path)
        assert probed is not None
        assert probed[0] == 8000 and probed[1] == 1          # fmt preserved (Req 4.6)


def test_the_ml_adapter_rejects_a_source_that_is_not_at_the_probed_format(tmp_path) -> None:
    """Separating a mismatched source would silently mix stems of different lengths."""
    source = tmp_path / "in.wav"
    write_pcm_wav(source, _pack([(0.1, 0.1)]), sample_rate=44100, channels=2)
    (tmp_path / "htdemucs.th").write_bytes(b"checkpoint")

    backend = stems.ML_Separator_Backend(model_dir=tmp_path, loader=_identity_loader)
    with pytest.raises(stems.Invalid_Audio_Format):
        backend.separate(
            source, tmp_path / "stems",
            fmt=_fmt(sample_rate=48000, channels=2), seed=1, timeout_s=30.0,
        )


# --------------------------------------------------------------------------- #
# Task 9.2 — the ffmpeg approximation                                         #
# --------------------------------------------------------------------------- #
def test_the_ffmpeg_adapter_needs_nothing_but_ffmpeg(tmp_path, monkeypatch) -> None:
    """The dependency-free fallback: no model, no torch, no network (Req 13.2)."""
    monkeypatch.setattr(socket, "socket", _no_socket)
    for module in ("torch", "demucs", "numpy"):
        monkeypatch.setitem(sys.modules, module, None)

    runner = Recording_Command_Runner()
    backend = stems.Ffmpeg_Separator_Backend(runner=runner)
    assert (backend.backend_id, backend.requires_network) == ("ffmpeg", False)

    written = backend.separate(
        tmp_path / "in.wav", tmp_path / "stems",
        fmt=_fmt(), seed=5, timeout_s=12.0,
    )

    # `other` is deliberately omitted so assemble_stem_set substitutes silence (Req 4.3).
    assert sorted(written) == ["music", "vocals"]
    assert len(runner.calls) == 1                        # ONE invocation (Req 2.6)
    assert runner.calls[0].timeout_s == 12.0


def test_the_ffmpeg_graph_subtracts_vocals_to_get_music() -> None:
    """``music := clip - vocals`` is what makes additive decomposition exact (Req 4.7)."""
    graph = stems.Ffmpeg_Separator_Backend.build_graph(2)

    assert "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1" in graph     # mid channel
    assert "highpass=f=180" in graph and "lowpass=f=6000" in graph     # speech band
    assert "volume=-1:precision=float" in graph                       # phase invert
    assert "amix=inputs=2:normalize=0:dropout_transition=0" in graph   # sum, not average
    # Every declared output pad is consumed: ffmpeg rejects an unconnected one outright.
    produced = set(re.findall(r"\[(\w+)\]", graph))
    assert "x3" not in produced


def test_the_ffmpeg_graph_omits_the_pan_node_for_mono() -> None:
    """Mid extraction is the identity on mono, and ``pan=stereo`` would upmix the stem."""
    mono = stems.Ffmpeg_Separator_Backend.build_graph(1)
    assert "pan=" not in mono
    assert "highpass=f=180" in mono
    # The format-preservation check would fail if the stem came back as two channels.
    assert "asplit=2" in mono


def test_the_ffmpeg_adapter_pins_both_outputs_to_the_probed_format(tmp_path) -> None:
    """Both stems must pass ``_verify_stem_file`` unchanged (Req 4.6)."""
    backend = stems.Ffmpeg_Separator_Backend()
    argv = backend.build_command(
        tmp_path / "in.wav", tmp_path / "vocals.wav", tmp_path / "music.wav",
        fmt=_fmt(sample_rate=44100, channels=1),
    )

    assert argv.count("-map") == 2                       # two outputs, one process
    assert argv.count("-c:a") == 2
    assert [argv[i + 1] for i, part in enumerate(argv) if part == "-ar"] == ["44100", "44100"]
    assert [argv[i + 1] for i, part in enumerate(argv) if part == "-ac"] == ["1", "1"]


# --------------------------------------------------------------------------- #
# P19 — nothing leaves the machine and nothing enters the audio                #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 19: Nothing leaves the machine and nothing
# enters the audio
@settings(max_examples=100, deadline=None)
@given(option_map=st_stem_options(), fmt_map=st_audio_format(valid_only=True))
def test_p19_nothing_leaves_the_machine_and_nothing_enters_the_audio(
    option_map: dict, fmt_map: dict, tmp_path_factory
) -> None:
    """Probing, planning and running complete with sockets disabled, reading only local files.

    Asserted for every enabled configuration:

    * **nothing leaves the machine** — ``socket.socket`` raises for the whole run, and
      probing, planning and both media passes still complete (Req 12.5, 16.4, 16.7);
    * **nothing enters the audio** — every path-like argument of every emitted command is
      either the incoming clip or inside the workspace, so no bed, no external sample and no
      downloaded asset can reach the mix. The only synthetic source the engine may name is
      ``anullsrc``, i.e. digital silence for an omitted stem (Req 4.3);
    * no argument is a URL, on any scheme.
    """
    workspace = tmp_path_factory.mktemp("p19")
    clip = workspace / "clip.mp4"
    clip.write_bytes(b"\x00" * 32)

    fmt = stems.Audio_Format(
        sample_rate=int(fmt_map["sample_rate"]),
        channels=int(fmt_map["channels"]),
        codec=str(fmt_map.get("codec") or ""),
    )
    runner = Recording_Command_Runner(
        sample_rate=fmt.sample_rate, channels=fmt.channels
    )

    with _sockets_blocked():
        probed = stems.probe_audio_format(clip, runner, 5.0)
        assert probed is not None

        options = stems.resolve_stem_options(option_map)
        plan = stems.plan_stems(opts=options, duration=4.0, fmt=probed)

        stems.extract_clip_audio(
            clip, workspace / "in.wav", fmt=probed, runner=runner, timeout_s=5.0
        )
        stems.Ffmpeg_Separator_Backend(runner=runner).separate(
            workspace / "in.wav", workspace / "stems",
            fmt=probed, seed=plan.repair_window_ms, timeout_s=5.0,
        )
        stem_set, _details = stems.assemble_stem_set(
            {"music": workspace / "stems" / "music.wav",
             "vocals": workspace / "stems" / "vocals.wav"},
            dest_dir=workspace / "stems", fmt=probed, duration=4.0,
            runner=runner, timeout_s=5.0,
        )
        stems.render_mix(
            plan, stem_set, workspace / "mixed.wav", runner=runner, timeout_s=5.0
        )
        stems.remux_replacement(
            clip, workspace / "mixed.wav", workspace / "out.mp4",
            fmt=probed, runner=runner, timeout_s=5.0,
        )

    root = str(workspace.resolve())
    for call in runner.calls:
        for argument in call.argv:
            assert "://" not in argument, f"URL reached a command: {argument!r}"
            if argument.startswith("/") or argument.startswith(root):
                assert argument.startswith(root), (
                    f"absolute path outside the workspace: {argument!r}"
                )
        # The only synthetic audio source permitted is silence for an omitted stem.
        for index, argument in enumerate(call.argv):
            if argument == "-i" and index + 1 < len(call.argv):
                target = call.argv[index + 1]
                assert target.startswith(root) or target.startswith("anullsrc="), (
                    f"unexpected input: {target!r}"
                )


@requires_ffmpeg
def test_p19_silent_audio_in_yields_silent_audio_out(tmp_path) -> None:
    """No bed, no sample, no synthesis: silence stays silence through the whole pipeline."""
    fmt = _fmt(sample_rate=8000, channels=1)
    source = tmp_path / "in.wav"
    write_pcm_wav(source, _pack([(0.0,)] * 8000), sample_rate=8000, channels=1)

    written = stems.Ffmpeg_Separator_Backend(runner=_real_runner).separate(
        source, tmp_path / "stems", fmt=fmt, seed=1, timeout_s=60.0
    )
    stem_set, _ = stems.assemble_stem_set(
        written, dest_dir=tmp_path / "stems", fmt=fmt, duration=1.0,
        runner=_real_runner, timeout_s=60.0,
    )
    plan = stems.Stem_Plan(
        backend="ffmpeg", model="", gains={n: 1.0 for n in stems.STEM_NAMES},
        active_stems=stems.STEM_NAMES, repair_mode="off", repair_window_ms=12,
        seams=(), windows=(), sample_rate=8000, channels=1, duration=1.0,
        declick=False, needs_separation=True, missing_capabilities=(),
    )
    mixed, _ = stems.render_mix(
        plan, stem_set, tmp_path / "mixed.wav", runner=_real_runner, timeout_s=60.0
    )

    assert set(_read_samples(mixed)) <= {0}, "silence in must give silence out"


# --------------------------------------------------------------------------- #
# P20 — reproducibility holds where it is claimed and only there               #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 20: Reproducibility holds where it is claimed
# and only there
@settings(max_examples=100, deadline=None)
@given(
    pcm=st_pcm_frames(max_frames=16),
    option_map=st_stem_options(),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
def test_p20_reproducibility_holds_where_it_is_claimed_and_only_there(
    pcm: dict, option_map: dict, seed: int, tmp_path_factory
) -> None:
    """Byte-identical within one environment; within tolerance across two.

    The two halves of the spec's deliberately **scoped** determinism claim (Req 10.4-10.6):

    * *within* a Fixed_Environment — same shim, same seed — two in-process runs produce
      byte-identical stems and byte-identical emitted commands;
    * *across* environments — simulated by two loaders differing by one LSB — the runs still
      agree exactly on the ``Stem_Plan``, the Stem_Set keys, the ``Audio_Format`` and the
      output duration, and differ by at most :data:`AMPLITUDE_TOLERANCE` per sample. The
      spec explicitly does **not** promise bit-exactness here, and this asserts precisely
      that weaker guarantee rather than pretending to the stronger one.
    """
    root = tmp_path_factory.mktemp("p20")
    channels = int(pcm["channels"])
    rate = int(pcm["sample_rate"])
    fmt = _fmt(sample_rate=rate, channels=channels)

    source = root / "in.wav"
    write_pcm_wav(source, _pack(pcm["frames"]), sample_rate=rate, channels=channels)
    (root / "htdemucs.th").write_bytes(b"checkpoint")

    options = stems.resolve_stem_options(option_map)
    duration = len(pcm["frames"]) / rate

    def _run_once(label: str, loader):
        backend = stems.ML_Separator_Backend(model_dir=root, loader=loader)
        written = backend.separate(
            source, root / label, fmt=fmt, seed=seed, timeout_s=30.0
        )
        plan = stems.plan_stems(opts=options, duration=duration, fmt=fmt)
        command = stems.mix_command(plan, written, root / f"{label}.wav")
        return written, plan, command

    first, plan_a, command_a = _run_once("a", _identity_loader)
    second, plan_b, command_b = _run_once("b", _identity_loader)

    # Within one environment: byte-identical stems and byte-identical commands.
    assert sorted(first) == sorted(second)
    for name in first:
        assert first[name].read_bytes() == second[name].read_bytes()
    assert [part.replace("/a", "/X") for part in command_a] == [
        part.replace("/b", "/X") for part in command_b
    ]
    assert plan_a.to_dict() == plan_b.to_dict()

    # Across two environments differing by sub-tolerance noise.
    third, plan_c, _ = _run_once("c", _offset_loader(1))
    assert plan_c.to_dict() == plan_a.to_dict()          # same plan
    assert sorted(third) == sorted(first)               # same Stem_Set
    for name in first:
        left = _read_samples(first[name])
        right = _read_samples(third[name])
        assert len(left) == len(right)                  # same duration
        for a, b in zip(left, right):
            assert abs(a - b) / 32768.0 <= stems.AMPLITUDE_TOLERANCE + 1e-12


def test_p20_the_emitted_graph_is_independent_of_mapping_order(tmp_path) -> None:
    """Two permutations of the same Stem_Set emit the identical command (Req 4.9, 10.6)."""
    plan = stems.Stem_Plan(
        backend="ml", model="htdemucs", gains={n: 0.5 for n in stems.STEM_NAMES},
        active_stems=stems.STEM_NAMES, repair_mode="off", repair_window_ms=12,
        seams=(), windows=(), sample_rate=48000, channels=2, duration=3.0,
        declick=False, needs_separation=True, missing_capabilities=(),
    )
    forward = {name: tmp_path / f"{name}.wav" for name in stems.STEM_NAMES}
    reversed_map = {name: forward[name] for name in reversed(stems.STEM_NAMES)}

    assert stems.mix_command(plan, forward, tmp_path / "m.wav") == stems.mix_command(
        plan, reversed_map, tmp_path / "m.wav"
    )


# --------------------------------------------------------------------------- #
# Task 9.5 — injected collaborator wiring                                     #
# --------------------------------------------------------------------------- #
def test_an_injected_runner_replaces_every_real_invocation(tmp_path) -> None:
    """The Req 19.1 seam: no adapter reaches ``subprocess`` when a runner is injected."""
    runner = Recording_Command_Runner()
    stems.Ffmpeg_Separator_Backend(runner=runner).separate(
        tmp_path / "in.wav", tmp_path / "stems", fmt=_fmt(), seed=1, timeout_s=5.0
    )
    assert len(runner.calls) == 1


def test_an_injected_locator_overrides_checkpoint_resolution(tmp_path) -> None:
    """So a test can exercise the success path with no checkpoint on disk."""
    marker = tmp_path / "pretend.th"
    backend = stems.ML_Separator_Backend(locator=lambda _n, _d: marker)
    assert backend.locate() == marker

    broken = stems.ML_Separator_Backend(locator=lambda _n, _d: 1 / 0)
    assert broken.locate() is None                     # never raises


def test_injected_reads_one_collaborator_out_of_context_deps() -> None:
    """``ctx.deps`` is the per-invocation override, and reading it is total (Req 19.1)."""

    class _Ctx:
        def __init__(self, deps):
            self.deps = deps

    sentinel = object()
    assert stems.injected(_Ctx({"runner": sentinel}), "runner") is sentinel
    assert stems.injected(_Ctx({}), "runner", "fallback") == "fallback"
    assert stems.injected(_Ctx(None), "runner", "fallback") == "fallback"
    assert stems.injected(object(), "runner", "fallback") == "fallback"


def test_the_repair_only_path_completes_with_demucs_absent(tmp_path, monkeypatch) -> None:
    """Seam repair needs no separation at all, so it must survive a bare install (Req 13.4)."""
    monkeypatch.setattr(socket, "socket", _no_socket)
    for module in ("torch", "demucs", "numpy"):
        monkeypatch.setitem(sys.modules, module, None)

    options = stems.resolve_stem_options({"stem_repair_mode": "crossfade"})
    plan = stems.plan_stems(
        opts=options,
        notes=("filler_seam:1.500",),
        duration=5.0,
        fmt=_fmt(),
    )

    assert plan.needs_separation is False              # all gains neutral
    assert plan.repair_mode == "crossfade"
    assert len(plan.windows) == 1

    runner = Recording_Command_Runner()
    stem_set = {name: tmp_path / f"{name}.wav" for name in stems.STEM_NAMES}
    _mixed, details = stems.render_mix(
        plan, stem_set, tmp_path / "mixed.wav", runner=runner, timeout_s=5.0
    )

    assert details == ()
    assert "eval=frame" in runner.calls[0].argv[runner.calls[0].argv.index("-filter_complex") + 1]
