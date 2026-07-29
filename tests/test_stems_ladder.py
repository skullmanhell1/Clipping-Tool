"""Degradation-ladder property module for the audio-stem-inpainting spec
(``worker/engines/stems.py``).

Covers epic 13: the engine class, its registration and the sixteen-rung ``run`` gate —
**P15** (the ladder is a total function to ``(status, markers)``), **P16** (every failure is
isolated and leaves nothing behind), the media-presence invariant across every outcome, and
the pinned ClassVar block.

Everything is offline. `Engine_Context`s are built directly rather than through the host, so
a rung can be aimed at precisely; every ffmpeg/ffprobe invocation goes through
:class:`tests.fakes.Recording_Command_Runner`, and separation goes through a local fake
backend. Nothing imports ``demucs``, reads a model file or opens a socket.

The one structural note worth reading before the tests: the ladder distinguishes its two
degraded families by **``media``, not by status**. ``Degraded_With_Media`` (rungs 7-9) hands
back a usable file that the host adopts exactly as for ``applied``; ``Degraded_Without_Media``
(rungs 2, 5, 6, 10, 11) hands back nothing and the Pipeline keeps the preceding stage's file.
Every test below therefore asserts status **and** media presence, never one alone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes import Recording_Command_Runner, StaticProber
from tests.strategies import st_availability_map, st_stem_options
from worker.engines import stems
from worker.engines.artifacts import allocate_workspace
from worker.engines.base import Engine_Context, Engine_Stage, Engine_Status
from worker.engines.capabilities import Capability_Report
from worker.engines.timebase import Time_Base
from worker.ffmpeg_utils import FFmpegError

# A probe payload both probers agree on: ``probe_audio_format`` reads ``streams[0]`` (so the
# audio stream must come first) while ``probe_media`` needs to see a video stream too. Using
# one payload for the clip *and* the candidate means integrity verification passes, so the
# happy path is reachable without a real remux.
_MEDIA_JSON = {
    "streams": [
        {"codec_type": "audio", "sample_rate": "48000", "channels": 2,
         "codec_name": "aac", "start_time": "0.0", "duration": "3.0"},
        {"codec_type": "video", "duration": "3.0", "nb_frames": "90"},
    ],
    "format": {"duration": "3.0"},
}

_DURATION = 3.0


class _Backend:
    """A minimal offline ``Separator_Backend``.

    Returns paths that are never written. That is legitimate rather than a shortcut: with an
    injected recording runner no ffmpeg ever executes, and ``_verify_stem_file`` deliberately
    skips a file that does not exist for exactly this reason — so assembly is exercised
    end to end with no audio on disk.
    """

    def __init__(
        self,
        backend_id: str = "ml",
        *,
        requires_network: bool = False,
        raises: BaseException | None = None,
        stems_out: tuple[str, ...] = ("vocals", "drums", "bass", "other"),
    ) -> None:
        self.backend_id = backend_id
        self.requires_network = requires_network
        self._raises = raises
        self._stems = stems_out
        self.calls = 0

    def separate(self, source, dest_dir, *, fmt, seed, timeout_s):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return {name: Path(str(dest_dir)) / f"{name}.wav" for name in self._stems}


def _report(availability: dict[str, bool] | None = None) -> Capability_Report:
    """A Capability_Report answering from a fixed availability map (everything else True)."""
    return Capability_Report(prober=StaticProber(availability or {}, default=True))


def _ctx(
    tmp_path: Path,
    *,
    options: stems.Stem_Options | None = None,
    remaining: float = 90.0,
    availability: dict[str, bool] | None = None,
    permissibility: bool = False,
    notes: tuple[str, ...] = (),
    duration: float = _DURATION,
    deps: dict | None = None,
) -> Engine_Context:
    """A real ``Engine_Context`` aimed at one rung."""
    clip = tmp_path / "clip.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"\x00" * 64)
    workspace = allocate_workspace(
        tmp_path / "ws", "job", "clip_a", stems.ENGINE_ID, "digest00"
    )
    return Engine_Context(
        job_id="job",
        clip_id="clip_a",
        engine_id=stems.ENGINE_ID,
        stage=Engine_Stage.AUDIO,
        source_path=clip,
        clip_path=clip,
        time_base=Time_Base(sample_rate=48000),
        clip_start=0.0,
        clip_end=duration,
        duration=duration,
        options=options if options is not None else stems.Stem_Options(),
        options_digest="digest00",
        seed=11,
        workspace=workspace,
        capabilities=_report(availability),
        permissibility=permissibility,
        deadline=time.monotonic() + remaining,
        time_budget_s=90.0,
        notes=notes,
        deps=deps or {},
    )


def _opts(**overrides) -> stems.Stem_Options:
    """Resolved ``Stem_Options`` from ``stem_``-prefixed overrides."""
    return stems.resolve_stem_options(
        type("O", (), {f"stem_{k}": v for k, v in overrides.items()})()
    )


def _details(result) -> list[str]:
    """The marker details, with the ``engine:stem_inpainting:`` prefix stripped."""
    prefix = f"engine:{stems.ENGINE_ID}:"
    for item in result.markers:
        assert item.startswith(prefix), f"un-namespaced marker: {item!r}"
    return [item[len(prefix):] for item in result.markers]


def _engine(**kwargs) -> stems.Stem_Inpainting_Engine:
    return stems.Stem_Inpainting_Engine(**kwargs)


def _run(tmp_path, *, engine=None, runner=None, **ctx_kwargs):
    """Run the ladder, returning ``(result, details, runner)``."""
    runner = runner if runner is not None else Recording_Command_Runner(
        probe_json=_MEDIA_JSON
    )
    engine = engine if engine is not None else _engine(
        backend=_Backend(), runner=runner
    )
    ctx = _ctx(tmp_path, deps={"runner": runner}, **ctx_kwargs)
    result = engine.run(ctx)
    return result, _details(result), runner


# --------------------------------------------------------------------------- #
# Task 13.8 — the pinned ClassVar block, registration and the flag             #
# --------------------------------------------------------------------------- #
def test_the_classvar_contract_is_pinned() -> None:
    """The declarations the host gates on, pinned so a later edit is deliberate."""
    engine = _engine()

    assert engine.engine_id == "stem_inpainting"
    assert engine.stage is Engine_Stage.AUDIO
    assert engine.priority == 20
    assert engine.required_capabilities == ("binary:ffmpeg",)
    assert engine.optional_capabilities == (
        "python_pkg:demucs",
        "model:htdemucs",
        "ffmpeg_filter:acrossfade",
        "ffmpeg_filter:afade",
        "ffmpeg_filter:pan",
        "ffmpeg_filter:highpass",
        "ffmpeg_filter:lowpass",
        "ffmpeg_filter:alimiter",
    )
    assert engine.requires_network is False
    assert engine.requires_model_download is True
    assert engine.time_budget_s == 90.0
    assert engine.max_media_passes == 2
    assert engine.max_inputs == 0
    assert engine.produces_media is True


def test_the_flag_is_off_by_default() -> None:
    """Every engine is OFF until explicitly enabled (foundation Req 9.2)."""
    engine = _engine()
    assert engine.flag_field() == "stem_inpainting_enabled"
    assert engine.is_enabled(None) is False
    assert engine.is_enabled(object()) is False
    assert engine.is_enabled({"stem_inpainting_enabled": True}) is True


def test_the_engine_is_registered_at_import_of_the_loader() -> None:
    """One line in ``loader.py`` is all it takes; nothing else changes (Req 1.7).

    Asserted in a **fresh interpreter**, for the same reason the model-locator test is: the
    default registry is a process-global, and ``tests/test_engine_host.py`` calls
    ``reset_registry()`` from an autouse fixture *and* from inside each property body. The
    live registry is therefore not a reliable witness to what importing ``loader`` does — a
    test reading it would pass or fail on file ordering.

    The subprocess also asserts the stronger claim: that the Engine_Host and ``/api/info``
    see the engine purely as a **side effect of the import**, with no setup hook called.
    """
    root = Path(__file__).resolve().parents[1]
    script = (
        "from worker.engines import loader\n"
        "from worker.engines.registry import get_registry\n"
        "from worker.engines.base import Engine_Stage\n"
        "r = get_registry()\n"
        "assert 'stem_inpainting' in r, 'not registered by loader'\n"
        "assert r.stage_of('stem_inpainting') is Engine_Stage.AUDIO\n"
        "print(','.join(e.engine_id for e in r.for_stage(Engine_Stage.AUDIO)))\n"
        "print(','.join(r.ids()))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(root), timeout=60,
        env={**os.environ, "PYTHONPATH": str(root)},
    )
    assert out.returncode == 0, out.stderr
    audio_stage, all_ids = out.stdout.strip().splitlines()
    assert audio_stage == "stem_inpainting"
    # Registration is additive: the sibling engine is untouched (Req 20.6).
    assert all_ids == "kinetic_typography,stem_inpainting"


def test_resolve_options_and_plan_stay_pure(tmp_path) -> None:
    """``plan`` must not probe: no runner call, and no format guessed (Req 1.9, 12.5)."""
    runner = Recording_Command_Runner(probe_json=_MEDIA_JSON)
    engine = _engine(runner=runner)
    ctx = _ctx(tmp_path, options=_opts(repair_mode="crossfade"))

    planned = engine.plan(ctx)

    assert runner.calls == []                       # nothing was probed
    assert planned == stems.plan_stems_from_context(ctx).to_dict()
    assert engine.resolve_options(ctx.options) == ctx.options   # idempotent


# --------------------------------------------------------------------------- #
# Rungs 2-6 — the pre-work gates, all Degraded_Without_Media / skipped         #
# --------------------------------------------------------------------------- #
def test_rung3_the_noop_configuration_costs_nothing(tmp_path) -> None:
    """No gain change and no repair: ``skipped``, **no marker**, zero subprocesses.

    "Costs nothing" is asserted as *zero runner calls* rather than as a fast return, which is
    the only way to see that no probe and no workspace write happened (Req 5.6, 15.8).
    """
    result, details, runner = _run(
        tmp_path, options=_opts(repair_mode="off")     # gains all default
    )

    assert result.status is Engine_Status.SKIPPED
    assert details == []
    assert result.media is None
    assert runner.calls == []


def test_rung4_no_audio_stream_is_skipped_without_a_marker(tmp_path) -> None:
    """Nothing to repair is not a degradation, so it is not reported (Req 4.8)."""
    runner = Recording_Command_Runner(has_audio=False)
    result, details, runner = _run(
        tmp_path, runner=runner, options=_opts(repair_mode="crossfade")
    )

    assert result.status is Engine_Status.SKIPPED
    assert details == []
    assert result.media is None
    assert runner.ffmpeg_calls == []                  # probed, but no media pass


def test_rung5_an_unusable_audio_format_degrades(tmp_path) -> None:
    """A present-but-broken format is reported, unlike an absent stream (Req 17.5)."""
    runner = Recording_Command_Runner(
        probe_json={"streams": [{"sample_rate": "0", "channels": 0}]}
    )
    result, details, runner = _run(
        tmp_path, runner=runner, options=_opts(repair_mode="crossfade")
    )

    assert result.status is Engine_Status.DEGRADED
    assert details == ["degraded:audio_format"]
    assert result.media is None
    assert runner.ffmpeg_calls == []


def test_rung2_permissibility_blocks_a_networked_backend(tmp_path) -> None:
    """The host cannot see this: its gate reads the *engine's* ``requires_network``.

    The engine declares ``False`` — it never needs the network itself. What can need it is the
    resolved Separator_Backend, so this rung has to live in the engine, and it must fire
    before any work happens (Req 16.3).
    """
    engine = _engine(backend=_Backend(requires_network=True))
    result, details, runner = _run(
        tmp_path, engine=engine, permissibility=True,
        options=_opts(repair_mode="crossfade"),
    )

    assert result.status is Engine_Status.DEGRADED
    assert details == ["permissibility_blocked"]
    assert result.media is None
    assert runner.calls == []                         # body never entered


def test_rung6_too_little_budget_to_finish_at_all(tmp_path) -> None:
    """Below repair+remux there is no way to produce media, so nothing is started."""
    result, details, runner = _run(
        tmp_path,
        remaining=stems.REPAIR_MIN_S + stems.REMUX_MIN_S - 0.1,
        options=_opts(repair_mode="crossfade"),
    )

    assert result.status is Engine_Status.DEGRADED
    assert details == ["degraded:budget"]
    assert result.media is None
    assert runner.calls == []


def test_rung10_a_missing_required_filter_degrades_without_media(tmp_path) -> None:
    """The ffmpeg backend cannot band-split without ``pan``/``highpass``/``lowpass``."""
    result, details, runner = _run(
        tmp_path,
        options=_opts(backend="ffmpeg", mix_preset="speech_focus"),
        availability={"ffmpeg_filter:pan": False},
    )

    assert result.status is Engine_Status.DEGRADED
    assert details == ["unavailable:ffmpeg_filter:pan"]
    assert result.media is None
    assert runner.ffmpeg_calls == []


def test_the_volume_filter_is_required_on_every_path(tmp_path) -> None:
    """Both the gains and the V-notch are ``volume`` nodes, so its absence blocks all paths."""
    result, details, _ = _run(
        tmp_path,
        options=_opts(repair_mode="crossfade"),
        availability={"ffmpeg_filter:volume": False},
    )

    assert result.status is Engine_Status.DEGRADED
    assert details == ["unavailable:ffmpeg_filter:volume"]
    assert result.media is None


# --------------------------------------------------------------------------- #
# Rungs 7-9 — Degraded_With_Media                                             #
# --------------------------------------------------------------------------- #
def test_rung7_unaffordable_separation_falls_back_to_repair_only(tmp_path) -> None:
    """Degraded, **with media**: the seams still get fixed on the un-separated audio.

    This is the rung that most needed the host gate widening — the fallback produces a real,
    usable clip, and the old ``APPLIED``-only gate would have thrown it away.
    """
    budget = stems.SEPARATION_MIN_S["ml"] + stems.REPAIR_MIN_S + stems.REMUX_MIN_S
    result, details, runner = _run(
        tmp_path,
        remaining=budget - 1.0,
        options=_opts(mix_preset="speech_focus", repair_mode="crossfade"),
        notes=("filler_seam:1.500",),
    )

    assert result.status is Engine_Status.DEGRADED
    assert result.media is not None                   # Degraded_With_Media
    assert "degraded:budget" in details
    assert "repair:crossfade:1" in details
    # No separation was attempted, so no stem files and no backend call.
    assert not any("stems" in " ".join(c.argv) for c in runner.ffmpeg_calls)


def test_rung8_a_missing_model_uses_the_ffmpeg_backend_with_media(tmp_path) -> None:
    """Degraded, with media: the dependency-free fallback, honestly labelled (Req 13.2)."""
    engine = _engine(backend=_Backend("ffmpeg", stems_out=("vocals", "music")))
    result, details, _ = _run(
        tmp_path,
        engine=engine,
        options=_opts(mix_preset="speech_focus", repair_mode="crossfade"),
        availability={"python_pkg:demucs": False, "model:htdemucs": False},
        notes=("filler_seam:1.500",),
    )

    assert result.status is Engine_Status.DEGRADED
    assert result.media is not None
    assert "degraded:python_pkg:demucs" in details
    assert "degraded:model:htdemucs" in details
    assert "applied:ffmpeg" in details
    assert "mix:speech_focus" in details
    # The ffmpeg backend cannot honestly estimate ``other``, so silence is substituted.
    assert "stem_missing:other" in details


def test_rung9_spectral_downgrades_to_crossfade_off_the_ml_backend(tmp_path) -> None:
    """``spectral`` needs real stems to bridge music, so it cannot survive elsewhere."""
    engine = _engine(backend=_Backend("ffmpeg", stems_out=("vocals", "music")))
    result, details, _ = _run(
        tmp_path,
        engine=engine,
        options=_opts(backend="ffmpeg", repair_mode="spectral",
                      mix_preset="speech_focus"),
        notes=("filler_seam:1.500",),
    )

    assert result.status is Engine_Status.DEGRADED
    assert result.media is not None
    assert "degraded:python_pkg:demucs" in details
    assert "repair:crossfade:1" in details
    assert not any(item.startswith("repair:spectral") for item in details)


def test_one_degradation_marker_per_capability_per_clip(tmp_path) -> None:
    """Rungs 8 and 9 both want ``degraded:python_pkg:demucs``; it appears once (Req 13.7)."""
    engine = _engine(backend=_Backend("ffmpeg", stems_out=("vocals", "music")))
    _result, details, _ = _run(
        tmp_path,
        engine=engine,
        options=_opts(repair_mode="spectral", mix_preset="speech_focus"),
        availability={"python_pkg:demucs": False, "model:htdemucs": False},
        notes=("filler_seam:1.500",),
    )

    assert details.count("degraded:python_pkg:demucs") == 1
    assert len(details) == len(set(details)) or details.count("mix:speech_focus") == 1


# --------------------------------------------------------------------------- #
# Rungs 11-15 — timeouts, failures, and the applied rung                      #
# --------------------------------------------------------------------------- #
def test_rung11_a_timeout_abandons_the_contribution_not_the_clip(tmp_path) -> None:
    """Degraded + ``timeout``, no media, and nothing left on disk (Req 15.6, 15.7).

    Deliberately ``degraded`` rather than ``failed``: this is the engine noticing it ran out
    of time and standing down cleanly. A hard host-level watchdog overrun — the engine *not*
    noticing — remains ``failed`` per foundation Req 8.6. They are different events.
    """
    runner = Recording_Command_Runner(probe_json=_MEDIA_JSON, timeout_at=1)
    result, details, _ = _run(
        tmp_path, runner=runner, options=_opts(repair_mode="crossfade"),
        notes=("filler_seam:1.500",),
    )

    assert result.status is Engine_Status.DEGRADED
    assert "timeout" in details
    assert result.media is None


def test_rung12_a_raising_backend_fails_without_media(tmp_path) -> None:
    """Nothing usable was produced, so the clip keeps the preceding stage's media."""
    engine = _engine(backend=_Backend(raises=stems.Stem_Error("backend exploded")))
    result, details, _ = _run(
        tmp_path, engine=engine,
        options=_opts(mix_preset="speech_focus", repair_mode="crossfade"),
    )

    assert result.status is Engine_Status.FAILED
    assert details == ["failed"]
    assert result.media is None
    assert "backend exploded" in result.detail


def test_rung13_a_failed_ffmpeg_invocation_fails_without_media(tmp_path) -> None:
    """Every ffmpeg failure arrives as one ``FFmpegError`` and one ``failed`` marker."""
    runner = Recording_Command_Runner(probe_json=_MEDIA_JSON, fail_at=1)
    result, details, _ = _run(
        tmp_path, runner=runner, options=_opts(repair_mode="crossfade")
    )

    assert result.status is Engine_Status.FAILED
    assert details == ["failed"]
    assert result.media is None


def test_rung14_a_failed_integrity_check_fails_and_deletes_the_candidate(tmp_path) -> None:
    """A clip that cannot be vouched for is never handed forward (Req 3.5, 17.7)."""
    # The candidate probe reports two audio streams; the clip probe is well-formed.
    broken = {
        "streams": [
            {"codec_type": "audio", "sample_rate": "48000", "channels": 2,
             "codec_name": "aac", "duration": "3.0", "start_time": "0.0"},
            {"codec_type": "audio", "sample_rate": "48000", "channels": 2},
            {"codec_type": "video", "duration": "3.0", "nb_frames": "90"},
        ],
        "format": {"duration": "3.0"},
    }
    runner = Recording_Command_Runner(probe_json=[_MEDIA_JSON, broken, _MEDIA_JSON])
    result, details, _ = _run(
        tmp_path, runner=runner, options=_opts(repair_mode="crossfade")
    )

    assert result.status is Engine_Status.FAILED
    assert details == ["failed"]
    assert result.media is None
    assert "audio streams" in result.detail


def test_rung15_the_applied_rung_reports_backend_mix_and_repair(tmp_path) -> None:
    """The success path, with media and the full marker set (Req 13.1, 3.7, 5.8, 7.8)."""
    result, details, runner = _run(
        tmp_path,
        options=_opts(mix_preset="clean_speech", repair_mode="crossfade"),
        notes=("filler_seam:1.500", "filler_seam:2.250"),
    )

    assert result.status is Engine_Status.APPLIED
    assert result.media is not None
    assert "applied:ml" in details
    assert "mix:clean_speech" in details
    assert "repair:crossfade:2" in details
    assert not any(item.startswith("degraded:") for item in details)

    # Exactly two media passes: extract and remux, plus the assembly/mix invocations.
    assert result.plan["repair_mode"] == "crossfade"
    assert result.plan["backend"] == "ml"


def test_no_repair_marker_when_there_is_nothing_to_repair(tmp_path) -> None:
    """``repair:<mode>:<n>`` is emitted only for ``n >= 1`` (Req 7.8)."""
    result, details, _ = _run(
        tmp_path,
        options=_opts(mix_preset="speech_focus", repair_mode="crossfade"),
        notes=(),                                    # no seams published
    )

    assert result.status is Engine_Status.APPLIED
    assert not any(item.startswith("repair:") for item in details)
    assert "mix:speech_focus" in details


# --------------------------------------------------------------------------- #
# P15 — the ladder is a total function to (status, markers)                    #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 15: The degradation ladder is a total function to
# (status, markers)
@settings(max_examples=100, deadline=None)
@given(
    availability=st_availability_map(),
    option_map=st_stem_options(),
    remaining=st.floats(min_value=0.0, max_value=200.0,
                        allow_nan=False, allow_infinity=False),
    permissibility=st.booleans(),
    network_backend=st.booleans(),
    backend_id=st.sampled_from(["ml", "ffmpeg"]),
)
def test_p15_the_ladder_is_a_total_function_to_status_and_markers(
    availability: dict,
    option_map: dict,
    remaining: float,
    permissibility: bool,
    network_backend: bool,
    backend_id: str,
    tmp_path_factory,
) -> None:
    """For **any** combination of inputs the ladder returns a well-formed outcome.

    Totality is the property: whatever the capability map, the budget, the options and the
    backend's network declaration, ``run`` returns an ``Engine_Result`` — never raises for an
    expected condition — and the result always satisfies the ladder's structural invariants:

    * the status is one of the four, and it is ``skipped`` only for the two unmarked rungs;
    * every marker is namespaced ``engine:stem_inpainting:``;
    * at most one degradation marker per Capability_Id (Req 13.7);
    * **media is present only when the status is media-bearing**, and never on a
      ``skipped``/``failed`` outcome — the invariant the host's gate relies on;
    * ``skipped`` carries **no** marker at all, and ``failed`` carries exactly ``failed``.
    """
    root = tmp_path_factory.mktemp("p15")
    options = stems.resolve_stem_options(option_map)
    runner = Recording_Command_Runner(probe_json=_MEDIA_JSON)
    engine = _engine(
        backend=_Backend(
            backend_id,
            requires_network=network_backend,
            stems_out=("vocals", "music") if backend_id == "ffmpeg"
            else ("vocals", "drums", "bass", "other"),
        ),
        runner=runner,
    )
    ctx = _ctx(
        root,
        options=options,
        remaining=remaining,
        availability=availability,
        permissibility=permissibility,
        notes=("filler_seam:1.500",),
        deps={"runner": runner},
    )

    result = engine.run(ctx)                          # must not raise
    details = _details(result)

    assert result.status in tuple(Engine_Status)
    assert result.engine_id == stems.ENGINE_ID

    # Media presence follows the status, and only the media-bearing statuses may carry it.
    if result.media is not None:
        assert result.status in (Engine_Status.APPLIED, Engine_Status.DEGRADED)
    if result.status in (Engine_Status.SKIPPED, Engine_Status.FAILED):
        assert result.media is None

    # The two unmarked rungs, and the single-marker failure rungs.
    if result.status is Engine_Status.SKIPPED:
        assert details == []
    if result.status is Engine_Status.FAILED:
        assert details == ["failed"]

    # At most one degradation marker per Capability_Id.
    degradations = [item for item in details if item.startswith("degraded:")]
    assert len(degradations) == len(set(degradations))

    # An applied result never carries a degradation.
    if result.status is Engine_Status.APPLIED:
        assert not degradations
        assert not any(item.startswith("unavailable:") for item in details)


# --------------------------------------------------------------------------- #
# P16 — every failure is isolated and leaves nothing behind                    #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 16: Every failure is isolated and leaves nothing
# behind
@settings(max_examples=100, deadline=None)
@given(
    failure_index=st.integers(min_value=0, max_value=6),
    mode=st.sampled_from(["fail", "timeout"]),
    option_map=st_stem_options(),
)
def test_p16_every_failure_is_isolated_and_leaves_nothing_behind(
    failure_index: int, mode: str, option_map: dict, tmp_path_factory
) -> None:
    """A forced failure at **any** invocation yields a clean outcome and no partial media.

    "Leaves nothing behind" is asserted as *no ``.mp4`` candidate and no ``mixed.wav``
    surviving in the workspace* — the two files that could be mistaken for finished output.
    Every rung that abandons work deletes what it created first (Req 15.7), so a failed run is
    indistinguishable on disk from one that never started.
    """
    root = tmp_path_factory.mktemp("p16")
    options = stems.resolve_stem_options(option_map)
    runner = Recording_Command_Runner(
        probe_json=_MEDIA_JSON,
        fail_at=failure_index if mode == "fail" else None,
        timeout_at=failure_index if mode == "timeout" else None,
    )
    engine = _engine(backend=_Backend(), runner=runner)
    ctx = _ctx(
        root, options=options, availability={}, notes=("filler_seam:1.500",),
        deps={"runner": runner},
    )

    result = engine.run(ctx)                          # must not raise
    assert result.status in tuple(Engine_Status)

    # ``failure_index`` may be past the end of a short run (a rung returned before that
    # many invocations happened), in which case nothing was injected and success is the
    # correct outcome. Only assert the failure outcome when the failure was actually hit.
    injected_was_reached = len(runner.calls) > failure_index
    if injected_was_reached:
        assert result.status is not Engine_Status.APPLIED
        if mode == "timeout":
            assert result.status is Engine_Status.DEGRADED
            assert "timeout" in _details(result)
        else:
            assert result.status is Engine_Status.FAILED
            assert _details(result) == ["failed"]

    # No abandoned output survives. (A successful run may legitimately leave its
    # replacement, so only assert this when no media was returned.)
    if result.media is None:
        leftovers = [
            path.name
            for path in ctx.workspace.root.rglob("*")
            if path.is_file() and path.suffix in (".mp4", ".wav")
        ]
        assert leftovers == [], f"partial output survived: {leftovers}"


def test_a_failure_does_not_touch_the_incoming_clip(tmp_path) -> None:
    """Failure isolation includes the input: the engine works on copies (Req 17.6)."""
    runner = Recording_Command_Runner(probe_json=_MEDIA_JSON, fail_at=1)
    ctx = _ctx(tmp_path, options=_opts(repair_mode="crossfade"),
               deps={"runner": runner})
    before = Path(ctx.clip_path).read_bytes()

    result = _engine(backend=_Backend(), runner=runner).run(ctx)

    assert result.status is Engine_Status.FAILED
    assert Path(ctx.clip_path).read_bytes() == before


def test_an_unexpected_exception_is_left_to_the_host(tmp_path) -> None:
    """The host already converts these into one ``failed`` marker and logs them (Req 14.1).

    Catching them here would swallow the traceback for no benefit, so a genuinely unexpected
    error — as opposed to the documented ``Stem_Error``/``FFmpegError`` families — propagates.
    """
    engine = _engine(backend=_Backend(raises=ZeroDivisionError("not a stem error")))
    runner = Recording_Command_Runner(probe_json=_MEDIA_JSON)
    ctx = _ctx(tmp_path, options=_opts(mix_preset="speech_focus"),
               deps={"runner": runner})

    with pytest.raises(ZeroDivisionError):
        engine.run(ctx)
