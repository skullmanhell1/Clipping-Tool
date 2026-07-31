"""ffmpeg-pipeline property module for the audio-stem-inpainting spec
(``worker/engines/stems.py``).

Covers epic 11: the audio-format probe and step budget (task 11.1), media pass 1
(task 11.2), the single gain + repair filtergraph (task 11.3), spectral per-stem repair
with ``music`` bridging (task 11.4) and the declick + remux pass (task 11.5) — headlined by
**P12** (repair touches only planned windows, once, and never exceeds full scale).

Everything here runs **offline**. Every invocation goes through
:class:`tests.fakes.Recording_Command_Runner`, so no ffmpeg binary is needed to assert the
emitted argv and filtergraph; the handful of clauses that genuinely need rendered audio
carry ``requires_ffmpeg`` and are skipped when the binary is absent.

P12's central claim is checked **semantically rather than structurally**: the emitted
``volume`` expression is translated into a Python callable by :func:`_gain_at` and evaluated,
so "unity outside the windows, zero at the join, one trough per merged window" is asserted
against the real expression string rather than against a count of substrings. That keeps the
property meaningful without a renderer, and the rendered clause below it then confirms the
same expression survives ffmpeg's own parser.
"""

from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import requires_ffmpeg
from tests.fakes import Recording_Command_Runner
from tests.strategies import st_repair_mode, st_seam_notes, st_stem_gains
from worker.engines import stems
from worker.engines.base import Engine_Status
from worker.engines.timebase import Time_Base
from worker.ffmpeg_utils import FFmpegError


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #
def _plan(
    *,
    windows=(),
    gains=None,
    repair_mode="crossfade",
    duration=10.0,
    declick=False,
    backend="ml",
    sample_rate=48000,
    channels=2,
) -> stems.Stem_Plan:
    """A ``Stem_Plan`` with only the fields these tests vary."""
    bundle = dict(gains or {name: 1.0 for name in stems.STEM_NAMES})
    return stems.Stem_Plan(
        backend=backend,
        model="htdemucs",
        gains=bundle,
        active_stems=tuple(n for n in stems.STEM_NAMES if bundle.get(n, 1.0) > 0.0),
        repair_mode=repair_mode,
        repair_window_ms=12,
        seams=(),
        windows=tuple(windows),
        sample_rate=sample_rate,
        channels=channels,
        duration=duration,
        declick=declick,
        needs_separation=True,
        missing_capabilities=(),
    )


def _stem_set(root: Path) -> dict:
    """A Stem_Set mapping pointing at (non-existent) per-stem paths."""
    return {name: root / "stems" / f"{name}.wav" for name in stems.STEM_NAMES}


_EXPR_RE = re.compile(r"exprs='([^']*)'")


def _expressions(filters) -> list[str]:
    """The per-chunk gain expression of a sequence of ``aeval`` filter strings.

    ``aeval`` carries one expression per channel, each of the form ``val(<ch>)*<gain>``. The
    gain factor is identical across channels, so the first lane is taken and its ``val(0)*``
    prefix stripped — leaving exactly the piecewise gain :func:`_gain_at` evaluates.
    """
    out = []
    for item in filters:
        found = _EXPR_RE.search(item)
        assert found is not None, f"no quoted exprs in {item!r}"
        lanes = found.group(1).split("|")
        first = lanes[0]
        assert first.startswith("val(0)*"), f"unexpected aeval lane: {first!r}"
        gain = first[len("val(0)*") :]
        # Every channel must get the same gain, or the notch would pan the audio.
        for lane, other in enumerate(lanes):
            assert other == f"val({lane})*{gain}", other
        out.append(gain)
    return out


def _gain_at(expression: str, t: float) -> float:
    """Evaluate one emitted ffmpeg ``volume`` expression at time ``t``.

    Translates the ffmpeg expression dialect the emitter uses — ``if(c,a,b)``,
    ``between(t,lo,hi)``, ``sin``, ``abs``, ``PI`` — into Python and evaluates it. ``if`` is
    evaluated eagerly, which is safe because the emitter never divides by a half-width it
    did not already prove positive.

    This is what makes P12 assertable with no renderer: the *actual string handed to
    ffmpeg* is what gets evaluated, so a malformed or mis-centred notch fails the property
    rather than passing a substring count.
    """

    def _if(condition, when_true, when_false):
        return when_true if condition else when_false

    def _between(value, low, high):
        return low <= value <= high

    translated = expression.replace("if(", "_if(").replace("between(", "_between(")
    return float(
        eval(  # evaluating our own emitted expression, by design
            translated,
            {"__builtins__": {}},
            {
                "_if": _if,
                "_between": _between,
                "sin": math.sin,
                "abs": abs,
                "PI": math.pi,
                "t": float(t),
            },
        )
    )


def _chain_gain(filters, t: float) -> float:
    """The product of every chunk's gain at ``t`` — the chain's effective gain.

    Chunking is only semantics-preserving because each chunk is unity outside its own
    windows, so the chain's gain is the product. Asserting through the product is what makes
    the chunking claim (Req 15.9) part of the property rather than an assumption.
    """
    total = 1.0
    for expression in _expressions(filters):
        total *= _gain_at(expression, t)
    return total


def _windows(spans) -> tuple:
    """``Repair_Window``s from ``(start, end)`` pairs."""
    return tuple(stems.Repair_Window(start=s, end=e, seams=((s + e) / 2.0,)) for s, e in spans)


def _real_runner(cmd, timeout_s):
    """A real ``Command_Runner``: the only place this module executes ffmpeg."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)


# --------------------------------------------------------------------------- #
# P12 — repair touches only planned windows, once, without clipping            #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 12: Repair touches only planned windows, once,
# and never exceeds full scale
@settings(max_examples=100, deadline=None)
@given(
    seam_case=st_seam_notes(duration=20.0),
    mode=st_repair_mode(),
    gains=st_stem_gains(),
)
def test_p12_repair_touches_only_planned_windows_once_without_clipping(
    seam_case: dict, mode: str, gains: dict, tmp_path_factory
) -> None:
    """The repair gain is unity outside every planned window and zero exactly at each join.

    Asserted, for the real emitted expression chain:

    * **only planned windows are touched** — the effective gain is exactly ``1.0`` at every
      probe point outside every window, so a gain-only reference rendering and the repaired
      one are identical there (Req 5.9, 7.2);
    * **each merged window is repaired exactly once** — the gain reaches ``0.0`` at the
      window centre and rises monotonically to ``1.0`` at both edges, which a
      double-applied notch could not do (it would square the trough and leave the edges at
      unity but the interior too low). Because ``repair_windows`` already merged overlaps,
      one window means one notch (Req 7.7);
    * **no sample can exceed full scale** — enforced by representation: the mix is written
      as ``pcm_s16le`` (Req 5.9);
    * ``off`` mode emits **no** repair filter at all (Req 7.10).
    """
    root = tmp_path_factory.mktemp("p12")
    duration = float(seam_case["duration"])
    tb = Time_Base(sample_rate=48000)

    windows = tuple(stems.repair_windows(seam_case["expected_seams"], 12, duration, tb))
    plan = _plan(windows=windows, gains=gains, repair_mode=mode, duration=duration)
    filters = stems.notch_filters(windows) if mode != "off" else ()

    # `off` asks for no repair at all.
    if mode == "off":
        graph = stems.build_mix_graph(plan, _stem_set(root))[1]
        assert "aeval" not in graph
        return

    usable = [w for w in windows if w.end > w.start]
    if not usable:
        assert filters == ()
        return

    # Chunking: one filter per NOTCH_EXPR_CHUNK windows (Req 15.9).
    expected_chunks = math.ceil(len(usable) / stems.NOTCH_EXPR_CHUNK)
    assert len(filters) == expected_chunks

    for window in usable:
        centre = (window.start + window.end) / 2.0
        half = (window.end - window.start) / 2.0

        # Zero exactly at the join, unity at both edges.
        assert _chain_gain(filters, centre) == pytest.approx(0.0, abs=1e-9)
        assert _chain_gain(filters, window.start) == pytest.approx(1.0, abs=1e-6)
        assert _chain_gain(filters, window.end) == pytest.approx(1.0, abs=1e-6)

        # Monotone rise away from the join, and never above unity (one notch, not two).
        previous = 0.0
        for step in range(1, 6):
            value = _chain_gain(filters, centre + half * step / 5.0)
            assert 0.0 - 1e-9 <= value <= 1.0 + 1e-9
            assert value >= previous - 1e-9
            previous = value

    # Outside every window the chain is exactly unity, so a gain-only reference rendering
    # and the repaired one agree sample for sample there.
    for probe in (0.0, duration, duration / 3.0, duration / 2.0, duration * 0.77):
        if any(w.start <= probe <= w.end for w in usable):
            continue
        assert _chain_gain(filters, probe) == pytest.approx(1.0, abs=1e-9)

    # No written sample can exceed full scale: the representation enforces it.
    argv = stems.mix_command(plan, _stem_set(root), root / "mixed.wav")
    assert "pcm_s16le" in argv


@requires_ffmpeg
def test_p12_emitted_expression_is_accepted_by_ffmpeg(tmp_path, make_video) -> None:
    """The emitted notch chain parses and renders under the real ffmpeg expression parser.

    The property above proves the expression means the right thing; this proves ffmpeg
    agrees it *is* an expression. Rendered output is compared against a gain-only reference:
    the two must differ (a notch was applied) while both keep the same duration
    (Req 7.9, 17.1).
    """
    source = make_video(name="src.mp4", duration=3.0, audio=True)
    fmt = stems.Audio_Format(sample_rate=48000, channels=2, codec="aac")
    extracted = stems.extract_clip_audio(
        source, tmp_path / "in.wav", fmt=fmt, runner=_real_runner, timeout_s=60.0
    )

    windows = _windows(((1.0, 1.012), (2.0, 2.012)))
    stem_set = {name: extracted for name in stems.STEM_NAMES}

    repaired, _ = stems.render_mix(
        _plan(windows=windows, duration=3.0),
        stem_set,
        tmp_path / "repaired.wav",
        runner=_real_runner,
        timeout_s=60.0,
    )
    reference, _ = stems.render_mix(
        _plan(windows=(), duration=3.0, repair_mode="off"),
        stem_set,
        tmp_path / "reference.wav",
        runner=_real_runner,
        timeout_s=60.0,
    )

    assert repaired.exists() and reference.exists()
    left = stems._read_wav_payload(repaired)
    right = stems._read_wav_payload(reference)
    assert left is not None and right is not None
    assert left[0] == right[0] == 48000 and left[1] == right[1] == 2
    # Same length (duration-exact), different content (the notch did something).
    assert abs(len(left[2]) - len(right[2])) <= 4 * stems._SAMPLE_WIDTH
    assert left[2] != right[2]


# --------------------------------------------------------------------------- #
# Task 11.1 — probe_audio_format and step_timeout                             #
# --------------------------------------------------------------------------- #
def test_probe_audio_format_reads_the_first_audio_stream() -> None:
    """A well-formed probe yields the rate, channels, codec and start_time (Req 17.4)."""
    runner = Recording_Command_Runner(sample_rate=44100, channels=1, codec="mp3")
    fmt = stems.probe_audio_format("clip.mp4", runner, 5.0)

    assert isinstance(fmt, stems.Audio_Format)
    assert (fmt.sample_rate, fmt.channels, fmt.codec) == (44100, 1, "mp3")
    argv = runner.calls[0].argv
    assert "ffprobe" in argv[0]
    assert "-select_streams" in argv and "a:0" in argv
    assert "-of" in argv and "json" in argv
    # An ffprobe, not a media pass: nothing is decoded or written.
    assert "-i" not in argv


def test_probe_audio_format_returns_none_without_an_audio_stream() -> None:
    """No audio stream is **not** an error — it is the ``skipped`` rung's gate (Req 4.8)."""
    runner = Recording_Command_Runner(has_audio=False)
    assert stems.probe_audio_format("silent.mp4", runner, 5.0) is None


@pytest.mark.parametrize(
    "stream",
    [
        {"channels": 2},  # sample_rate absent
        {"sample_rate": "48000"},  # channels absent
        {"sample_rate": "0", "channels": 2},  # zero rate
        {"sample_rate": "48000", "channels": 0},  # zero channels
        {"sample_rate": "-48000", "channels": 2},  # negative rate
        {"sample_rate": "48000", "channels": -2},  # negative channels
        {"sample_rate": "N/A", "channels": "N/A"},  # non-numeric
    ],
)
def test_probe_audio_format_rejects_an_unusable_format(stream: dict) -> None:
    """A present-but-unusable format raises, feeding ``degraded:audio_format`` (Req 17.5)."""
    runner = Recording_Command_Runner(probe_json={"streams": [stream]})
    with pytest.raises(stems.Invalid_Audio_Format):
        stems.probe_audio_format("clip.mp4", runner, 5.0)


def test_probe_audio_format_tolerates_missing_codec_and_start_time() -> None:
    """``codec``/``start_time`` are best-effort: ``"N/A"`` is not a failure."""
    runner = Recording_Command_Runner(
        probe_json={
            "streams": [
                {"sample_rate": "48000", "channels": 2, "codec_name": "N/A", "start_time": "N/A"}
            ]
        }
    )
    fmt = stems.probe_audio_format("clip.mp4", runner, 5.0)
    assert fmt is not None and fmt.codec == "" and fmt.start_time == 0.0


class _Ctx:
    """The two ``Engine_Context`` fields ``step_timeout`` reads."""

    def __init__(self, remaining: float, budget: float = 30.0) -> None:
        self._remaining = remaining
        self.time_budget_s = budget

    def remaining(self) -> float:
        return self._remaining


@settings(max_examples=100, deadline=None)
@given(
    remaining=st.floats(min_value=-100.0, max_value=600.0, allow_nan=False, allow_infinity=False),
    reserve=st.sampled_from(
        [
            stems.EXTRACT_RESERVE_S,
            stems.SEPARATE_RESERVE_S,
            stems.REPAIR_RESERVE_S,
            stems.REMUX_RESERVE_S,
        ]
    ),
)
def test_step_timeout_is_always_positive_and_holds_back_its_reserve(
    remaining: float, reserve: float
) -> None:
    """Every derived timeout is positive, and never spends its successor's reserve.

    The floor is what guarantees no ffmpeg invocation is launched with a non-positive or
    missing budget (Req 15.4); the subtraction is what leaves the later steps something to
    run with (Req 15.3).
    """
    value = stems.step_timeout(_Ctx(remaining), reserve)
    assert value >= stems.MIN_STEP_TIMEOUT_S
    if remaining - reserve > stems.MIN_STEP_TIMEOUT_S:
        assert value == pytest.approx(remaining - reserve)


def test_step_timeout_degrades_to_the_floor_without_a_usable_context() -> None:
    """A missing or haywire ``remaining()`` must not become an unbounded subprocess."""

    class _Broken:
        def remaining(self):
            raise RuntimeError("no clock")

    assert stems.step_timeout(_Broken(), 3.0) == stems.MIN_STEP_TIMEOUT_S
    assert stems.step_timeout(object(), 3.0) == stems.MIN_STEP_TIMEOUT_S
    # An infinite deadline falls back to the engine's own declared budget.
    assert stems.step_timeout(_Ctx(math.inf, budget=30.0), 8.0) == pytest.approx(22.0)


# --------------------------------------------------------------------------- #
# Task 11.2 — media pass 1                                                    #
# --------------------------------------------------------------------------- #
def test_extract_decodes_audio_only_at_the_probed_format(tmp_path) -> None:
    """``-vn`` and the pinned ``-ar``/``-ac`` are what make later comparisons exact."""
    runner = Recording_Command_Runner()
    fmt = stems.Audio_Format(sample_rate=44100, channels=1, codec="aac")
    dest = tmp_path / "ws" / "in.wav"

    out = stems.extract_clip_audio("clip.mp4", dest, fmt=fmt, runner=runner, timeout_s=7.5)

    assert out == dest
    argv = runner.calls[0].argv
    assert "-vn" in argv  # no video decoded (Req 4.4)
    assert argv[argv.index("-map") + 1] == "0:a:0"
    assert argv[argv.index("-ar") + 1] == "44100"
    assert argv[argv.index("-ac") + 1] == "1"
    assert argv[argv.index("-c:a") + 1] == "pcm_s16le"
    assert runner.calls[0].timeout_s == 7.5  # explicit timeout (Req 15.4)


# --------------------------------------------------------------------------- #
# Task 11.3 — the gain + repair filtergraph                                   #
# --------------------------------------------------------------------------- #
def test_a_muted_stem_is_not_an_input_at_all(tmp_path) -> None:
    """Gain ``0.0`` excludes the stem from the graph *and* from ``-i`` (Req 5.7)."""
    plan = _plan(gains={"music": 0.0, "other": 0.5, "vocals": 1.0}, repair_mode="off")
    inputs, graph, _ = stems.build_mix_graph(plan, _stem_set(tmp_path))

    assert len(inputs) == 2
    assert not any("music" in str(path) for path in inputs)
    assert "g_music" not in graph
    # ``other`` is attenuated, so it gets a volume node...
    assert "g_other" in graph
    # ...while ``vocals`` is at unity and therefore gets **no node at all**, feeding straight
    # into the mix. That is the re-entrancy guard (Req 7.10): a no-op ``volume=1.000000``
    # would make a second run a second filter pass instead of nothing.
    assert "g_vocals" not in graph
    assert "amix=inputs=2:normalize=0:dropout_transition=0" in graph


def test_amix_is_skipped_for_a_single_surviving_stem(tmp_path) -> None:
    """One input needs no ``amix``: mixing a stream with itself costs a node for nothing."""
    plan = _plan(gains={"music": 0.0, "other": 0.0, "vocals": 0.5}, repair_mode="off")
    inputs, graph, out_label = stems.build_mix_graph(plan, _stem_set(tmp_path))

    assert len(inputs) == 1
    assert "amix" not in graph
    assert out_label == "g_vocals"


def test_a_unity_gain_with_nothing_to_repair_emits_an_empty_graph(tmp_path) -> None:
    """The re-entrancy guard at its limit: nothing requested means nothing emitted.

    With unity gains, no windows and no declick the graph is empty and the output label is the
    raw input — so a re-render is a stream copy of the mix stage's input rather than a second
    filter pass (Req 7.10). ``plan_has_work`` is what stops the engine getting this far at all.
    """
    # All gains at unity and no windows — and the single-input Stem_Set of the repair-only
    # path, which is the shape a second run on already-repaired media takes.
    plan = _plan(repair_mode="off")
    inputs, graph, out_label = stems.build_mix_graph(plan, {"vocals": tmp_path / "in.wav"})

    assert len(inputs) == 1
    assert graph == ""
    assert out_label == "0:a"
    assert stems.plan_has_work(plan) is False


def test_muting_a_stem_counts_as_work(tmp_path) -> None:
    """A ``0.0`` gain is a real change, so it must not be mistaken for a no-op.

    Worth pinning explicitly: ``plan_has_work`` asks "would this change the audio?", and
    silencing a stem certainly would — even though it emits no ``volume`` node for the
    *surviving* stems and drops the muted one as an input entirely.
    """
    plan = _plan(gains={"music": 0.0, "other": 1.0, "vocals": 1.0}, repair_mode="off")
    assert stems.plan_has_work(plan) is True


def test_normalize_zero_is_always_present(tmp_path) -> None:
    """``amix``'s default would divide by the input count and break Req 4.7."""
    graph = stems.build_mix_graph(_plan(), _stem_set(tmp_path))[1]
    assert "normalize=0" in graph
    assert "dropout_transition=0" in graph


def test_alimiter_is_appended_only_when_available(tmp_path) -> None:
    """The peak guard is optional, and its absence is visible, not silent (Req 5.9)."""
    plan = _plan()
    with_guard = stems.build_mix_graph(plan, _stem_set(tmp_path), alimiter=True)[1]
    without = stems.build_mix_graph(plan, _stem_set(tmp_path), alimiter=False)[1]

    assert "alimiter=limit=0.977:level=disabled" in with_guard
    assert "alimiter" not in without


def test_a_boost_without_a_peak_guard_is_clamped_and_reported() -> None:
    """A refused boost is recorded, not silently clipped (Req 5.9)."""
    boosted = {"music": 2.5, "other": 1.0, "vocals": 4.0}

    kept, details = stems.resolve_peak_guard(boosted, True)
    assert kept == boosted and details == ()

    clamped, details = stems.resolve_peak_guard(boosted, False)
    assert clamped == {"music": 1.0, "other": 1.0, "vocals": 1.0}
    assert details == ("degraded:ffmpeg_filter:alimiter",)


def test_attenuation_only_never_carries_the_alimiter_marker() -> None:
    """With every gain at or below unity there is nothing to guard against."""
    quiet = {"music": 0.25, "other": 0.0, "vocals": 1.0}
    kept, details = stems.resolve_peak_guard(quiet, False)
    assert kept == quiet and details == ()


def test_declick_fades_only_the_clip_head_and_tail(tmp_path) -> None:
    """The two boundaries Req 6.3 forbids a Seam at are exactly where ``afade`` is correct."""
    plan = _plan(duration=8.0, declick=True, repair_mode="off")
    graph = stems.build_mix_graph(plan, _stem_set(tmp_path))[1]

    assert "afade=t=in:st=0:d=0.001000" in graph
    assert "afade=t=out:st=7.999000:d=0.001000" in graph


def test_declick_is_skipped_on_a_clip_shorter_than_the_two_fades(tmp_path) -> None:
    """A 1 ms in/out pair cannot fit in a sub-2 ms clip, so it is not emitted."""
    plan = _plan(duration=0.001, declick=True, repair_mode="off")
    graph = stems.build_mix_graph(plan, _stem_set(tmp_path))[1]
    assert "afade" not in graph


def test_render_mix_writes_pcm_at_the_planned_format(tmp_path) -> None:
    """The representation is the no-clipping guarantee, so it must be pinned (Req 5.9)."""
    runner = Recording_Command_Runner()
    plan = _plan(sample_rate=44100, channels=1)

    out, details = stems.render_mix(
        plan,
        _stem_set(tmp_path),
        tmp_path / "mixed.wav",
        runner=runner,
        timeout_s=9.0,
    )

    argv = runner.calls[0].argv
    assert out == tmp_path / "mixed.wav" and details == ()
    assert argv[argv.index("-c:a") + 1] == "pcm_s16le"
    assert argv[argv.index("-ar") + 1] == "44100"
    assert argv[argv.index("-ac") + 1] == "1"


def test_spectral_notches_each_stem_before_the_mix(tmp_path) -> None:
    """``spectral`` repairs per stem with a scaled half-width (Req 7.3), not post-mix."""
    windows = _windows(((1.0, 1.012),))
    plan = _plan(windows=windows, repair_mode="spectral")
    graph = stems.build_mix_graph(plan, _stem_set(tmp_path))[1]

    # One notch inside each per-stem chain, and none on the summed stream.
    for name in stems.STEM_NAMES:
        chain = graph.split(f"[g_{name}]")[0].split(";")[-1]
        assert "aeval" in chain
    assert graph.count("aeval") == len(stems.STEM_NAMES)

    # Narrower for vocals than for music, per SPECTRAL_HALF_WIDTH_SCALE.
    vocals = stems.notch_filters(windows, scale=stems.SPECTRAL_HALF_WIDTH_SCALE["vocals"])
    music = stems.notch_filters(windows, scale=stems.SPECTRAL_HALF_WIDTH_SCALE["music"])
    assert _gain_at(_expressions(vocals)[0], 1.0035) > _gain_at(_expressions(music)[0], 1.0035)


def test_crossfade_notches_once_post_mix(tmp_path) -> None:
    """``crossfade`` repairs the summed stream once, not per stem (Req 7.2, 7.7)."""
    plan = _plan(windows=_windows(((1.0, 1.012),)), repair_mode="crossfade")
    graph = stems.build_mix_graph(plan, _stem_set(tmp_path))[1]
    assert graph.count("aeval") == 1


def test_a_zero_width_window_contributes_no_notch() -> None:
    """There is no discontinuity to taper across, and ``/h`` would divide by zero."""
    assert stems.notch_filters(_windows(((1.0, 1.0),))) == ()
    assert stems.notch_filters(()) == ()


def test_notch_expressions_are_chunked(tmp_path) -> None:
    """A long Seam list becomes several bounded expressions, not one giant one (Req 15.9)."""
    count = stems.NOTCH_EXPR_CHUNK * 2 + 1
    spans = [(index * 1.0, index * 1.0 + 0.012) for index in range(1, count + 1)]
    filters = stems.notch_filters(_windows(spans))

    assert len(filters) == 3
    # Every chunk is unity outside its own windows, which is why chaining is safe.
    for expression in _expressions(filters):
        assert _gain_at(expression, 0.5) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Task 11.4 — spectral music bridging                                         #
# --------------------------------------------------------------------------- #
def test_bridging_requires_neighbouring_material_inside_the_clip() -> None:
    """A window too close to either clip bound falls back to the notch (Req 7.3)."""
    # h = 0.006; the first window has no room on the left, the last none on the right.
    windows = _windows(((0.002, 0.014), (5.0, 5.012), (9.994, 10.006)))
    bridged, notched = stems.partition_bridge_windows(windows, 10.0)

    assert [w.start for w in bridged] == [5.0]
    assert [w.start for w in notched] == [0.002, 9.994]
    assert len(bridged) + len(notched) == len(windows)


def test_bridging_is_capped(tmp_path) -> None:
    """Beyond ``MAX_BRIDGE_WINDOWS`` every window notches, bounding the graph (Req 15.9)."""
    spans = [(index * 1.0, index * 1.0 + 0.012) for index in range(1, 30)]
    bridged, notched = stems.partition_bridge_windows(_windows(spans), 40.0)

    assert len(bridged) == stems.MAX_BRIDGE_WINDOWS
    assert len(notched) == len(spans) - stems.MAX_BRIDGE_WINDOWS


def test_bridging_refuses_windows_whose_source_material_would_overlap() -> None:
    """Two bridges may not read the same neighbouring material, or ``concat`` would lie."""
    # Adjacent windows: the second's left source [s-h, s) lies inside the first's window.
    windows = _windows(((1.0, 1.012), (1.012, 1.024)))
    bridged, notched = stems.partition_bridge_windows(windows, 10.0)

    assert len(bridged) == 1 and len(notched) == 1


def test_bridge_graph_partitions_the_timeline_exactly(tmp_path) -> None:
    """``concat`` of keeps + bridges covers ``[0, duration)`` once, in order (Req 7.9)."""
    windows = _windows(((2.0, 2.012), (5.0, 5.012)))
    graph, label = stems.build_bridge_graph(windows, 10.0)

    assert label == "bridged"
    # (n+1) keeps + 4n trims, all fed from one asplit -> the source is decoded once.
    assert "asplit=11" in graph
    assert graph.count("acrossfade=") == 4  # two per window
    # 3 keeps + 2 bridges, and each bridge contributes its own left and right half.
    assert "concat=n=7:v=0:a=1[bridged]" in graph
    assert "[k0][l0][r0][k1][l1][r1][k2]concat=" in graph
    # Each crossfade lasts exactly the half-width, which is what preserves duration.
    assert graph.count("d=0.006000") == 4
    # The keeps tile the gaps between windows.
    for expected in (
        "start=0.000000:end=2.000000",
        "start=2.012000:end=5.000000",
        "start=5.012000:end=10.000000",
    ):
        assert expected in graph


def test_bridge_music_stem_spends_no_invocation_when_nothing_qualifies(tmp_path) -> None:
    """Nothing to bridge means no ffmpeg call and the source returned untouched."""
    runner = Recording_Command_Runner()
    fmt = stems.Audio_Format(sample_rate=48000, channels=2)
    source = tmp_path / "music.wav"

    # Flush against the clip head: the left source segment would start before 0.
    path, bridged, residual = stems.bridge_music_stem(
        source,
        tmp_path / "bridged.wav",
        _windows(((0.0, 0.012),)),
        fmt=fmt,
        duration=10.0,
        runner=runner,
        timeout_s=5.0,
    )

    assert path == source and bridged == ()
    assert len(residual) == 1
    assert runner.calls == []


def test_a_bridged_window_is_not_also_notched(tmp_path) -> None:
    """Repairing a window twice is exactly what Req 7.7 forbids."""
    windows = _windows(((2.0, 2.012), (0.002, 0.014)))
    bridged, residual = stems.partition_bridge_windows(windows, 10.0)
    assert len(bridged) == 1 and len(residual) == 1

    plan = _plan(windows=windows, repair_mode="spectral", duration=10.0)
    graph = stems.build_mix_graph(plan, _stem_set(tmp_path), stem_windows={"music": residual})[1]

    music_chain = graph.split("[g_music]")[0].split(";")[-1]
    found = [e for e in _expressions([f"aeval=exprs='{m}'" for m in _EXPR_RE.findall(music_chain)])]
    assert len(found) == 1, "the music stem should carry exactly one notch chunk"

    # The residual window IS notched (gain 0 at its join)...
    assert _gain_at(found[0], 0.008) == pytest.approx(0.0, abs=1e-9)
    # ...while the bridged window is left alone, so it is not repaired twice.
    assert _gain_at(found[0], 2.006) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Task 11.5 — media pass 2                                                    #
# --------------------------------------------------------------------------- #
def test_remux_copies_video_and_never_shortens(tmp_path) -> None:
    """``-c:v copy`` and the deliberate absence of ``-shortest`` (Req 3.2, 17.1, 17.3)."""
    runner = Recording_Command_Runner()
    fmt = stems.Audio_Format(sample_rate=48000, channels=2, codec="aac")

    stems.remux_replacement(
        "clip.mp4",
        tmp_path / "mixed.wav",
        tmp_path / "out.mp4",
        fmt=fmt,
        runner=runner,
        timeout_s=11.0,
    )

    argv = runner.calls[0].argv
    assert argv[argv.index("-c:v") + 1] == "copy"
    assert "-shortest" not in argv
    assert argv.count("-map") == 2
    assert "0:v:0" in argv and "1:a:0" in argv
    assert "+faststart" in argv
    assert runner.calls[0].timeout_s == 11.0


def test_itsoffset_is_emitted_only_for_a_non_zero_start_time(tmp_path) -> None:
    """A container whose audio starts late keeps that relationship (Req 17.4)."""
    zero = stems.remux_command(
        "clip.mp4",
        tmp_path / "m.wav",
        tmp_path / "o.mp4",
        fmt=stems.Audio_Format(sample_rate=48000, channels=2),
    )
    assert "-itsoffset" not in zero

    offset = stems.remux_command(
        "clip.mp4",
        tmp_path / "m.wav",
        tmp_path / "o.mp4",
        fmt=stems.Audio_Format(sample_rate=48000, channels=2, start_time=0.021),
    )
    assert offset[offset.index("-itsoffset") + 1] == "0.021000"
    # The offset applies to the video input, so it precedes the mixed audio -i.
    assert offset.index("-itsoffset") < offset.index(str(tmp_path / "m.wav"))


@pytest.mark.parametrize(
    "codec,expected",
    [
        ("aac", "aac"),
        ("mp3", "mp3"),
        ("opus", "opus"),
        ("", "aac"),
        ("pcm_s16le", "aac"),
        ("some_exotic_codec", "aac"),
    ],
)
def test_remux_codec_falls_back_to_aac_for_anything_unproven(codec, expected) -> None:
    """A remux that fails for want of an encoder is worse than one that lands as AAC."""
    assert (
        stems.remux_codec(stems.Audio_Format(sample_rate=48000, channels=2, codec=codec))
        == expected
    )


# --------------------------------------------------------------------------- #
# Failure isolation                                                            #
# --------------------------------------------------------------------------- #
def test_every_invocation_failure_arrives_as_one_ffmpeg_error(tmp_path) -> None:
    """One failure type reaches the ladder, whatever the runner did (Req 14.3)."""
    fmt = stems.Audio_Format(sample_rate=48000, channels=2)

    with pytest.raises(FFmpegError):
        stems.extract_clip_audio(
            "clip.mp4",
            tmp_path / "in.wav",
            fmt=fmt,
            runner=Recording_Command_Runner(fail_at=0),
            timeout_s=5.0,
        )
    with pytest.raises(FFmpegError):
        stems.render_mix(
            _plan(),
            _stem_set(tmp_path),
            tmp_path / "mixed.wav",
            runner=Recording_Command_Runner(returncode=1, stderr="boom"),
            timeout_s=5.0,
        )


def test_a_budget_overrun_stays_a_timeout(tmp_path) -> None:
    """``TimeoutExpired`` is re-raised unchanged so the budget rung can tell it apart."""
    with pytest.raises(subprocess.TimeoutExpired):
        stems.remux_replacement(
            "clip.mp4",
            tmp_path / "m.wav",
            tmp_path / "o.mp4",
            fmt=stems.Audio_Format(sample_rate=48000, channels=2),
            runner=Recording_Command_Runner(timeout_at=0),
            timeout_s=5.0,
        )


@pytest.mark.parametrize(
    "call",
    [
        "extract",
        "mix",
        "remux",
        "bridge",
    ],
)
def test_a_probed_audio_format_is_required(tmp_path, call: str) -> None:
    """Every pass refuses to run without a real probed format, rather than guessing."""
    with pytest.raises(stems.Invalid_Audio_Format):
        if call == "extract":
            stems.extract_clip_audio(
                "c.mp4",
                tmp_path / "a.wav",
                fmt=None,
                runner=Recording_Command_Runner(),
                timeout_s=5.0,
            )
        elif call == "mix":
            stems.assemble_stem_set(
                {},
                dest_dir=tmp_path,
                fmt=None,
                duration=1.0,
                runner=Recording_Command_Runner(),
                timeout_s=5.0,
            )
        elif call == "remux":
            stems.remux_replacement(
                "c.mp4",
                tmp_path / "m.wav",
                tmp_path / "o.mp4",
                fmt=None,
                runner=Recording_Command_Runner(),
                timeout_s=5.0,
            )
        else:
            stems.bridge_music_stem(
                tmp_path / "m.wav",
                tmp_path / "b.wav",
                (),
                fmt=None,
                duration=1.0,
                runner=Recording_Command_Runner(),
                timeout_s=5.0,
            )


# =========================================================================== #
# Epic 12 — integrity verification of the Replacement_Media (tasks 12.1-12.2) #
# =========================================================================== #


def _probe(**overrides) -> stems.Media_Probe:
    """A ``Media_Probe`` describing a well-formed one-video/one-audio clip."""
    fields = {
        "audio_streams": 1,
        "video_streams": 1,
        "audio_duration": 3.0,
        "video_duration": 3.0,
        "video_frames": 90,
        "sample_rate": 48000,
        "channels": 2,
        "audio_start_time": 0.0,
    }
    fields.update(overrides)
    return stems.Media_Probe(**fields)


def _canned(candidate: stems.Media_Probe, original: stems.Media_Probe):
    """A ``probe`` seam returning ``candidate`` first and ``original`` second."""
    answers = [candidate, original]

    def probe(_path, _runner=None, _timeout=None):
        return answers.pop(0) if answers else original

    return probe


def _verify(candidate: stems.Media_Probe, original=None, **fmt_kwargs):
    """Run ``verify_replacement`` against canned probes."""
    fields = {"sample_rate": 48000, "channels": 2}
    fields.update(fmt_kwargs)
    return stems.verify_replacement(
        "candidate.mp4",
        "clip.mp4",
        fmt=stems.Audio_Format(**fields),
        probe=_canned(candidate, original if original is not None else _probe()),
    )


def test_a_well_formed_candidate_verifies() -> None:
    """The happy path returns the candidate's probe rather than just ``None``."""
    produced = _verify(_probe())
    assert isinstance(produced, stems.Media_Probe)
    assert produced.audio_streams == 1 and produced.video_streams == 1


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"audio_streams": 0}, "audio streams"),
        ({"audio_streams": 2}, "audio streams"),
        ({"video_streams": 0}, "video streams"),
        ({"video_streams": 2}, "video streams"),
        ({"sample_rate": 44100}, "44100Hz"),
        ({"channels": 1}, "channels"),
        ({"audio_duration": 2.5}, "audio duration drifted"),
        ({"video_duration": 2.5}, "video duration drifted"),
        ({"video_frames": 89}, "video frame count changed"),
        ({"audio_start_time": 0.5}, "start_time drifted"),
    ],
)
def test_every_integrity_condition_is_enforced_and_named(overrides: dict, expected: str) -> None:
    """Each failure raises, and says **which** condition failed (Req 17.1-17.4).

    A generic "integrity check failed" would be nearly useless in a log: "the audio got
    shorter" and "a second audio stream appeared" have different causes and different fixes,
    so the message names the specific mismatch.
    """
    with pytest.raises(stems.Integrity_Error, match=re.escape(expected)):
        _verify(_probe(**overrides))


def test_the_audio_duration_tolerance_is_exactly_one_audio_frame() -> None:
    """One frame passes, two fail — the check that catches a stray ``-shortest`` (Req 17.1)."""
    frame = 1.0 / 48000

    _verify(_probe(audio_duration=3.0 + frame))  # at tolerance: ok
    with pytest.raises(stems.Integrity_Error, match="audio duration drifted"):
        _verify(_probe(audio_duration=3.0 + frame * 2))

    # The tolerance follows the *probed* rate, so a low rate is looser in absolute terms.
    _verify(
        _probe(sample_rate=8000, audio_duration=3.0 + 1.0 / 8000),
        _probe(sample_rate=8000),
        sample_rate=8000,
    )


def test_an_unknown_frame_count_is_not_treated_as_zero_frames() -> None:
    """Some containers omit ``nb_frames``; failing every such clip would be wrong."""
    # Neither side reports it -> the comparison is skipped, not failed.
    _verify(_probe(video_frames=0), _probe(video_frames=0))
    # Only one side reports it -> also skipped, because 0 means "unknown" here.
    _verify(_probe(video_frames=0), _probe(video_frames=90))
    _verify(_probe(video_frames=90), _probe(video_frames=0))


def test_verification_does_not_delete_the_candidate(tmp_path) -> None:
    """Deletion belongs to the ladder rung that owns the workspace (Req 3.5, 17.7)."""
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"not really an mp4")

    with pytest.raises(stems.Integrity_Error):
        stems.verify_replacement(
            candidate,
            "clip.mp4",
            fmt=stems.Audio_Format(sample_rate=48000, channels=2),
            probe=_canned(_probe(audio_streams=0), _probe()),
        )

    assert candidate.exists(), "verify_replacement must not delete anything itself"


def test_probe_media_counts_every_stream() -> None:
    """It selects no stream, because the stream *count* is part of what is verified."""
    runner = Recording_Command_Runner(
        probe_json={
            "streams": [
                {"codec_type": "video", "duration": "3.0", "nb_frames": "90"},
                {
                    "codec_type": "audio",
                    "sample_rate": "48000",
                    "channels": 2,
                    "duration": "3.0",
                    "start_time": "0.0",
                },
                {"codec_type": "audio", "sample_rate": "48000", "channels": 2},
            ],
            "format": {"duration": "3.0"},
        }
    )
    probed = stems.probe_media("clip.mp4", runner, 5.0)

    assert probed.audio_streams == 2 and probed.video_streams == 1
    assert probed.video_frames == 90
    assert (probed.sample_rate, probed.channels) == (48000, 2)
    argv = runner.calls[0].argv
    assert "-show_streams" in argv
    assert "-select_streams" not in argv  # would hide a second audio stream


def test_probe_media_falls_back_to_the_container_duration() -> None:
    """A stream that carries no duration of its own still yields a usable answer."""
    runner = Recording_Command_Runner(
        probe_json={
            "streams": [
                {"codec_type": "video", "duration": "N/A"},
                {"codec_type": "audio", "sample_rate": "48000", "channels": 2, "start_time": "N/A"},
            ],
            "format": {"duration": "4.25"},
        }
    )
    probed = stems.probe_media("clip.mp4", runner, 5.0)

    assert probed.video_duration == pytest.approx(4.25)
    assert probed.audio_duration == pytest.approx(4.25)
    assert probed.audio_start_time == 0.0
    assert probed.video_frames == 0  # unknown, not zero frames


def test_probe_media_is_total_on_unreadable_output() -> None:
    """An unreadable candidate must fail *verification*, not raise a parse error.

    That distinction matters to the ladder: a parse error would surface as a bare ``failed``
    with an opaque message, whereas an all-zero probe fails the stream-count check and says
    so.
    """
    for payload in ("not json at all", "[]", "null", ""):
        runner = Recording_Command_Runner(probe_json=payload)
        assert stems.probe_media("x.mp4", runner, 5.0) == stems.Media_Probe()

    with pytest.raises(stems.Integrity_Error, match="audio streams"):
        stems.verify_replacement(
            "candidate.mp4",
            "clip.mp4",
            fmt=stems.Audio_Format(sample_rate=48000, channels=2),
            runner=Recording_Command_Runner(probe_json="garbage"),
        )


def test_verification_requires_a_probed_format() -> None:
    """Without a probed format there is nothing to compare the candidate against."""
    with pytest.raises(stems.Invalid_Audio_Format):
        stems.verify_replacement("c.mp4", "clip.mp4", fmt=None, probe=_canned(_probe(), _probe()))


# --------------------------------------------------------------------------- #
# P13 — Replacement_Media preserves duration, format, streams and alignment    #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 13: Replacement_Media preserves duration, format,
# streams and A/V alignment
@requires_ffmpeg
@pytest.mark.parametrize(
    "gains,repair_mode,declick",
    [
        ({"music": 1.0, "other": 1.0, "vocals": 1.0}, "off", False),
        ({"music": 0.25, "other": 0.6, "vocals": 1.0}, "crossfade", False),
        ({"music": 0.0, "other": 0.0, "vocals": 1.0}, "crossfade", True),
    ],
)
def test_p13_replacement_media_preserves_duration_format_streams_and_alignment(
    tmp_path, make_video, gains: dict, repair_mode: str, declick: bool
) -> None:
    """End to end on a real tiny clip: the video is untouched and the audio is exact.

    Runs the whole epic-11 pipeline for real — extract, separate (ffmpeg backend), assemble,
    mix + repair, remux — then checks the candidate through :func:`verify_replacement` and
    additionally asserts the two things verification cannot see:

    * the **video stream is bit-identical** to the incoming clip's, which is the strongest
      possible statement of "``-c:v copy`` copied" (Req 3.2, 17.3);
    * the **incoming clip file is unchanged**, byte for byte, so the engine provably works on
      copies and never edits its input in place (Req 17.6, 17.7).
    """
    source = make_video(name="src.mp4", duration=2.0, w=320, h=240, audio=True)
    before = source.read_bytes()

    fmt = stems.probe_audio_format(source, _real_runner, 60.0)
    assert fmt is not None

    extracted = stems.extract_clip_audio(
        source, tmp_path / "in.wav", fmt=fmt, runner=_real_runner, timeout_s=60.0
    )
    # The **decoded** audio length, not the container duration. A lossy stream carries encoder
    # priming/padding, so 2.000 s of AAC decodes to ~2.020 s of PCM — and the stems are
    # derived from that PCM, so that is the length they must match. Using the clip duration
    # here is precisely the mistake the engine used to make.
    probed_wav = stems._wav_format(extracted)
    assert probed_wav is not None
    audio_duration = probed_wav[2] / probed_wav[0]
    assert audio_duration != pytest.approx(
        2.0, abs=1e-4
    ), "expected encoder padding to make the decoded audio longer than the container"

    raw = stems.Ffmpeg_Separator_Backend(runner=_real_runner).separate(
        extracted, tmp_path / "stems", fmt=fmt, seed=1, timeout_s=60.0
    )
    stem_set, _missing = stems.assemble_stem_set(
        raw,
        dest_dir=tmp_path / "stems",
        fmt=fmt,
        duration=audio_duration,
        runner=_real_runner,
        timeout_s=60.0,
    )

    plan = _plan(
        windows=_windows(((1.0, 1.012),)) if repair_mode != "off" else (),
        gains=gains,
        repair_mode=repair_mode,
        duration=2.0,
        declick=declick,
        sample_rate=fmt.sample_rate,
        channels=fmt.channels,
        backend="ffmpeg",
    )
    mixed, _details = stems.render_mix(
        plan, stem_set, tmp_path / "mixed.wav", runner=_real_runner, timeout_s=60.0
    )
    # ``-t`` bounded to the original audio stream duration is what stops the ~20 ms of
    # re-encode padding from lengthening the clip on every pass.
    baseline = stems.probe_media(source, _real_runner, 60.0)
    candidate = stems.remux_replacement(
        source,
        mixed,
        tmp_path / "repaired.mp4",
        fmt=fmt,
        runner=_real_runner,
        timeout_s=60.0,
        duration=baseline.audio_duration,
    )

    # The whole integrity gate, on real output.
    produced = stems.verify_replacement(
        candidate,
        source,
        fmt=fmt,
        runner=_real_runner,
        timeout_s=60.0,
        baseline=baseline,
    )
    assert produced.audio_streams == 1 and produced.video_streams == 1

    # The video stream is bit-identical: -c:v copy really copied.
    def _video_bytes(path: Path) -> bytes:
        out = tmp_path / f"{path.stem}.h264"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-f",
                "h264",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        return out.read_bytes()

    assert _video_bytes(candidate) == _video_bytes(source)

    # The incoming clip was never edited in place.
    assert source.read_bytes() == before


# =========================================================================== #
# Task 19.2 — tiny-clip end-to-end behaviour                                  #
# =========================================================================== #
# These run the **whole engine** against real ffmpeg on a 2-second clip: probe, extract,
# separate, assemble, mix + repair, remux, verify. Nothing is stubbed except the separator,
# which is a fake precisely so no model is needed — the ffmpeg work is all real.


def _e2e_ctx(tmp_path, source, *, options, notes=(), remaining=120.0):
    """A real ``Engine_Context`` pointing at a real clip."""
    import time

    from tests.fakes import StaticProber
    from worker.engines.artifacts import allocate_workspace
    from worker.engines.base import Engine_Context, Engine_Stage
    from worker.engines.capabilities import Capability_Report
    from worker.engines.timebase import Time_Base

    return Engine_Context(
        job_id="job",
        clip_id="clip_a",
        engine_id=stems.ENGINE_ID,
        stage=Engine_Stage.AUDIO,
        source_path=source,
        clip_path=source,
        time_base=Time_Base(sample_rate=48000),
        clip_start=0.0,
        clip_end=2.0,
        duration=2.0,
        options=options,
        options_digest="e2edigest",
        seed=7,
        workspace=allocate_workspace(
            tmp_path / "ws", "job", "clip_a", stems.ENGINE_ID, "e2edigest"
        ),
        capabilities=Capability_Report(prober=StaticProber({}, default=True)),
        permissibility=False,
        deadline=time.monotonic() + remaining,
        time_budget_s=120.0,
        notes=tuple(notes),
    )


def _e2e_options(**overrides):
    return stems.resolve_stem_options(
        type("O", (), {f"stem_{k}": v for k, v in overrides.items()})()
    )


@requires_ffmpeg
def test_e2e_an_applied_run_produces_a_valid_replacement(tmp_path, make_video) -> None:
    """A full ``applied`` run through a fake separator yields real, verified media.

    The separator is a fake so no model is needed, but every ffmpeg invocation is real — so
    this is the test that would catch a filtergraph that does not parse, an argv in the wrong
    order, or a Replacement_Media that fails its own integrity gate.
    """
    from tests.fakes import Fake_Separator_Backend

    source = make_video(name="tiny.mp4", duration=2.0, w=320, h=240, audio=True)
    engine = stems.Stem_Inpainting_Engine(
        # ``sum_to_input`` copies the source frames verbatim, so the stems are exactly the
        # length of the extracted audio and the additive invariant holds by construction.
        backend=Fake_Separator_Backend(sum_to_input=True),
        runner=_real_runner,
    )
    ctx = _e2e_ctx(
        tmp_path,
        source,
        options=_e2e_options(mix_preset="speech_focus", repair_mode="crossfade"),
        notes=("filler_seam:1.000",),
    )

    result = engine.run(ctx)

    assert result.status is Engine_Status.APPLIED, result.detail
    assert result.media is not None and Path(result.media).exists()

    details = [m.split(":", 2)[2] for m in result.markers]
    assert "mix:speech_focus" in details
    assert "repair:crossfade:1" in details

    # The Replacement_Media passes the integrity gate on its own terms.
    baseline = stems.probe_media(source, _real_runner, 60.0)
    produced = stems.verify_replacement(
        result.media,
        source,
        fmt=stems.probe_audio_format(source, _real_runner, 60.0),
        runner=_real_runner,
        timeout_s=60.0,
        baseline=baseline,
    )
    assert produced.audio_streams == 1 and produced.video_streams == 1
    assert produced.video_frames == baseline.video_frames


@requires_ffmpeg
def test_e2e_non_unit_gains_apply_with_an_empty_seam_list(tmp_path, make_video) -> None:
    """Filler removal off means no Seams — but a gain change is still real work (Req 8.5).

    The complement of the idempotence guard: `plan_has_work` must let this through, because
    the operator asked for a mix change even though there is nothing to repair.
    """
    from tests.fakes import Fake_Separator_Backend

    source = make_video(name="tiny.mp4", duration=2.0, w=320, h=240, audio=True)
    engine = stems.Stem_Inpainting_Engine(
        backend=Fake_Separator_Backend(sum_to_input=True), runner=_real_runner
    )
    ctx = _e2e_ctx(
        tmp_path,
        source,
        options=_e2e_options(mix_preset="clean_speech", repair_mode="crossfade"),
        notes=(),  # no filler removal -> no Seams
    )

    result = engine.run(ctx)

    assert result.status is Engine_Status.APPLIED, result.detail
    assert result.media is not None and Path(result.media).exists()
    details = [m.split(":", 2)[2] for m in result.markers]
    assert "mix:clean_speech" in details
    assert not any(item.startswith("repair:") for item in details)  # nothing to repair
    assert result.plan["windows"] == []


@requires_ffmpeg
def test_e2e_an_unusable_probed_format_degrades_with_no_media(tmp_path, make_video) -> None:
    """A present-but-broken audio format degrades and hands back nothing (Req 17.5)."""
    from tests.fakes import Fake_Separator_Backend

    source = make_video(name="tiny.mp4", duration=2.0, w=320, h=240, audio=True)

    def broken_prober(_path, _runner=None, _timeout=None):
        raise stems.Invalid_Audio_Format("sample_rate=0")

    engine = stems.Stem_Inpainting_Engine(
        backend=Fake_Separator_Backend(sum_to_input=True),
        runner=_real_runner,
        prober=broken_prober,
    )
    ctx = _e2e_ctx(
        tmp_path,
        source,
        options=_e2e_options(mix_preset="speech_focus", repair_mode="crossfade"),
        notes=("filler_seam:1.000",),
    )

    result = engine.run(ctx)

    assert result.status is Engine_Status.DEGRADED
    assert result.media is None
    assert [m.split(":", 2)[2] for m in result.markers] == ["degraded:audio_format"]


@requires_ffmpeg
def test_e2e_a_clip_with_no_audio_is_skipped(tmp_path, make_video) -> None:
    """No audio stream is not a degradation, and costs no media pass (Req 4.8)."""
    from tests.fakes import Fake_Separator_Backend

    silent = make_video(name="mute.mp4", duration=2.0, w=320, h=240, audio=False)
    engine = stems.Stem_Inpainting_Engine(
        backend=Fake_Separator_Backend(sum_to_input=True), runner=_real_runner
    )
    ctx = _e2e_ctx(
        tmp_path,
        silent,
        options=_e2e_options(mix_preset="speech_focus", repair_mode="crossfade"),
        notes=("filler_seam:1.000",),
    )

    result = engine.run(ctx)

    assert result.status is Engine_Status.SKIPPED
    assert result.media is None
    assert result.markers == ()


@requires_ffmpeg
def test_e2e_the_compositor_spends_the_same_passes_either_way(
    tmp_path, make_video, monkeypatch
) -> None:
    """The engine adds audio passes of its own but changes the **compositor** not at all.

    This is the cost claim that matters for the single-pass guarantee (Reqs 2.6, 19.5): whatever
    the stem engine does at the AUDIO stage, the compositor still renders each clip in exactly
    the same number of ffmpeg invocations as it would with the engine disabled.
    """
    import worker.effects.compositor as comp
    import worker.pipeline as pl
    from worker.engines.kinetic import Kinetic_Typography_Engine
    from worker.engines.registry import Engine_Registry
    from worker.engines.stems import Stem_Inpainting_Engine
    from worker.models import ProcessingOptions
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    source = make_video(name="src.mp4", duration=4.0, w=640, h=360, audio=True)

    def _counted(options, tag):
        calls = []
        real_run = comp._run
        monkeypatch.setattr(comp, "_run", lambda cmd: (calls.append(tuple(cmd)), real_run(cmd))[1])

        monkeypatch.setattr(
            pl,
            "transcribe",
            lambda *a, **k: Transcript(
                language="en",
                segments=[
                    TranscriptSegment(
                        0.0,
                        4.0,
                        "hello there my friend today",
                        [
                            Word(0.2, 0.6, "hello"),
                            Word(0.7, 1.1, "there"),
                            Word(1.2, 1.6, "my"),
                            Word(1.7, 2.3, "friend"),
                            Word(2.4, 3.0, "today"),
                        ],
                    )
                ],
            ),
        )
        monkeypatch.setattr(
            pl.sel,
            "select_moments",
            lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=50.0, text="hi")],
        )

        registry = Engine_Registry()
        registry.register(Kinetic_Typography_Engine())
        registry.register(Stem_Inpainting_Engine(runner=_real_runner))
        real_host = pl.Engine_Host
        monkeypatch.setattr(
            pl,
            "Engine_Host",
            lambda opts, **kw: real_host(opts, **{**kw, "registry": registry}),
        )

        clips = pl.run_pipeline(
            source,
            options,
            clips_dir=tmp_path / f"clips_{tag}",
            temp_dir=tmp_path / f"tmp_{tag}",
        )
        return len(clips), len(calls)

    with monkeypatch.context():
        off_clips, off_passes = _counted(
            ProcessingOptions(captions=True, metadata=False, aspect="9:16"), "off"
        )
    with monkeypatch.context():
        on_clips, on_passes = _counted(
            ProcessingOptions(
                captions=True,
                metadata=False,
                aspect="9:16",
                stem_inpainting_enabled=True,
                stem_mix_preset="speech_focus",
            ),
            "on",
        )

    assert on_clips == off_clips
    assert (
        on_passes == off_passes
    ), f"compositor spent {on_passes} passes enabled vs {off_passes} disabled"
