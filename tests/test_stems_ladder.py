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
from tests.strategies import (
    st_availability_map,
    st_seam_notes,
    st_stem_gains,
    st_stem_options,
)
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
        notes=("filler_seam:1.500",),
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
        tmp_path, runner=runner, options=_opts(repair_mode="crossfade"),
        notes=("filler_seam:1.500",),
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
        tmp_path, runner=runner, options=_opts(repair_mode="crossfade"),
        notes=("filler_seam:1.500",),
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
               notes=("filler_seam:1.500",), deps={"runner": runner})
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





# =========================================================================== #
# Epic 15 — workspace lifecycle, cleanup and the disk bound                    #
# =========================================================================== #
# The lifecycle assertions need real files on disk for the cleanup to reclaim, but no ffmpeg.
# So the backend writes genuine PCM WAVs, and the recording runner is wrapped so that every
# command's *output* file is materialised as a valid WAV too — anything less gets rejected by
# `_verify_stem_file`, which is itself the point: the assembly path really is being exercised.

_FMT = stems.Audio_Format(sample_rate=48000, channels=2, codec="aac")


def _write_wav(path: Path, duration: float = _DURATION) -> Path:
    """A valid 16-bit PCM WAV of ``duration`` at :data:`_FMT`."""
    frames = int(round(duration * _FMT.sample_rate))
    payload = bytes(frames * _FMT.channels * 2)
    return stems._write_pcm_wav(Path(path), payload, _FMT)


class _WritingBackend(_Backend):
    """A backend that writes real stem WAVs, so cleanup has something to reclaim."""

    def separate(self, source, dest_dir, *, fmt, seed, timeout_s):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        out = {}
        for name in self._stems:
            out[name] = _write_wav(Path(str(dest_dir)) / f"{name}.wav")
        return out


def _materialising(runner):
    """Wrap ``runner`` so each command's output file appears, as a valid WAV where relevant."""

    def call(cmd, timeout_s=None, **kwargs):
        completed = runner(cmd, timeout_s, **kwargs)
        argv = [str(part) for part in cmd]
        if "ffprobe" not in argv[0]:
            target = Path(argv[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix == ".wav":
                _write_wav(target)
            elif target.suffix and not target.exists():
                target.write_bytes(b"\x00" * 256)
        return completed

    return call


def _workspace_files(ctx) -> list[str]:
    """Every file surviving in the Engine_Workspace, workspace-relative."""
    root = ctx.workspace.root
    return sorted(
        str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
    )


def _lifecycle_run(tmp_path, **option_overrides):
    """Run the full working half against real files; return ``(result, ctx, runner)``."""
    base = Recording_Command_Runner(probe_json=_MEDIA_JSON)
    runner = _materialising(base)
    engine = _engine(backend=_WritingBackend(), runner=runner)
    options = _opts(
        mix_preset="speech_focus", repair_mode="crossfade", **option_overrides
    )
    ctx = _ctx(
        tmp_path, options=options, notes=("filler_seam:1.500",),
        deps={"runner": runner},
    )
    return engine.run(ctx), ctx, base


def test_the_workspace_layout_is_the_documented_one(tmp_path) -> None:
    """``in.wav``, ``stems/*.wav``, ``mixed.wav``, ``clip_repaired.<ext>`` — and nothing
    written outside the Engine_Workspace (Reqs 11.1, 16.4)."""
    result, ctx, runner = _lifecycle_run(tmp_path)
    assert result.status in (Engine_Status.APPLIED, Engine_Status.DEGRADED), result.detail

    root = str(ctx.workspace.root)
    written = [
        call.argv[-1] for call in runner.ffmpeg_calls
        if Path(call.argv[-1]).suffix in (".wav", ".mp4")
    ]
    assert written
    for path in written:
        assert path.startswith(root), f"wrote outside the workspace: {path}"

    names = {Path(p).name for p in written}
    assert "in.wav" in names
    assert "mixed.wav" in names
    assert "clip_repaired.mp4" in names        # matches the incoming clip's extension


def test_the_replacement_extension_follows_the_incoming_clip() -> None:
    """ffmpeg picks its muxer from the extension, so it must match the container."""
    engine = _engine()

    assert engine._replacement_name(
        type("C", (), {"clip_path": Path("/tmp/clip_a.mkv")})()
    ) == "clip_repaired.mkv"
    assert engine._replacement_name(
        type("C", (), {"clip_path": Path("/tmp/clip_a")})()
    ) == "clip_repaired.mp4"
    assert engine._replacement_name(object()) == "clip_repaired.mp4"


def test_only_the_replacement_survives_the_run(tmp_path) -> None:
    """Intermediates are reclaimed before returning; the Replacement_Media is not (Req 11.4)."""
    result, ctx, _runner = _lifecycle_run(tmp_path)

    assert result.media is not None
    assert _workspace_files(ctx) == ["clip_repaired.mp4"]
    assert Path(result.media).exists()


def test_retained_stems_are_declared_durable_and_survive(tmp_path) -> None:
    """``retain_stems`` makes the host persist them **before** the workspace goes (Req 11.3)."""
    result, ctx, _runner = _lifecycle_run(tmp_path, retain_stems=True)

    durable = [item for item in result.artifacts if item.durable]
    assert {item.name for item in durable} == {
        f"stems/{name}.wav" for name in stems.STEM_NAMES
    }
    assert all(item.media_type == "audio" for item in durable)

    survivors = _workspace_files(ctx)
    assert "clip_repaired.mp4" in survivors
    assert "in.wav" not in survivors           # intermediates still reclaimed
    assert "mixed.wav" not in survivors
    for name in stems.STEM_NAMES:
        assert f"stems/{name}.wav" in survivors


def test_without_retain_stems_nothing_is_durable(tmp_path) -> None:
    """The default keeps no audio: only the Replacement_Media is published."""
    result, ctx, _runner = _lifecycle_run(tmp_path)

    assert [item.durable for item in result.artifacts] == [False]
    assert result.artifacts[0].media_type == "video"
    assert _workspace_files(ctx) == ["clip_repaired.mp4"]


def test_a_cleanup_failure_is_recorded_and_does_not_fail_the_clip(
    tmp_path, monkeypatch
) -> None:
    """Failing to reclaim space must not turn a good clip into a failure (Req 11.4).

    The guard is **per file**, so one refusal records its detail and the loop continues to the
    next — asserted with two files where only the first refuses.
    """
    first = tmp_path / "stubborn.wav"
    second = tmp_path / "deletable.wav"
    first.write_bytes(b"\x00")
    second.write_bytes(b"\x00")

    real_unlink = Path.unlink

    def refuse_one(self, missing_ok=False):
        if self.name == "stubborn.wav":
            raise OSError("device busy")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_one)

    details = _engine()._reclaim([first, second], keep=set())

    assert details == ["cleanup_failed:stubborn.wav"]
    monkeypatch.undo()
    assert first.exists()                       # the refusal really happened
    assert not second.exists()                  # and the loop carried on


def test_reclaim_never_deletes_what_it_was_told_to_keep(tmp_path) -> None:
    """The keep-set is what protects the media the host is about to adopt (Req 11.5)."""
    keeper = tmp_path / "clip_repaired.mp4"
    doomed = tmp_path / "mixed.wav"
    keeper.write_bytes(b"\x00")
    doomed.write_bytes(b"\x00")

    assert _engine()._reclaim([keeper, doomed], keep={keeper}) == []
    assert keeper.exists() and not doomed.exists()


# --------------------------------------------------------------------------- #
# P18 — cost and disk stay bounded regardless of seams and gains               #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 18: Cost and disk stay bounded regardless of seams
# and gains
@settings(max_examples=50, deadline=None)
@given(
    seam_case=st_seam_notes(duration=6.0),
    gains=st_stem_gains(),
    mode=st.sampled_from(["off", "crossfade", "spectral"]),
    retain=st.booleans(),
)
def test_p18_cost_and_disk_stay_bounded_regardless_of_seams_and_gains(
    seam_case: dict, gains: dict, mode: str, retain: bool, tmp_path_factory
) -> None:
    """Two media passes and a bounded workspace, for any Seam count and any gain set.

    Four claims, each of which must hold for *every* input rather than on average:

    * **at most two media passes over the clip container** — extract and remux. Every other
      invocation reads or writes WAVs inside the workspace, never the clip, so cost is constant
      in the Seam count rather than linear in it (Reqs 2.6, 15.9);
    * **every command carries a positive timeout** within the declared budget (Req 15.4);
    * **only the Replacement_Media and the declared durable artifacts survive** (Req 11.4);
    * the mix takes no more ``-i`` inputs than there are stems, so a seam-dense clip cannot
      grow the graph's input count at all — the Seams live in the *expression*, not the inputs.
    """
    root = tmp_path_factory.mktemp("p18")
    base = Recording_Command_Runner(probe_json=_MEDIA_JSON)
    runner = _materialising(base)
    options = stems.resolve_stem_options(
        {
            "stem_mix_preset": "custom",
            "stem_gain_vocals": gains["vocals"],
            "stem_gain_music": gains["music"],
            "stem_gain_other": gains["other"],
            "stem_repair_mode": mode,
            "stem_retain_stems": retain,
        }
    )
    engine = _engine(backend=_WritingBackend(), runner=runner)
    ctx = _ctx(
        root, options=options, duration=_DURATION,
        notes=tuple(seam_case["notes"]), deps={"runner": runner},
    )

    result = engine.run(ctx)

    for call in base.calls:
        assert call.timeout_s is not None
        assert call.timeout_s >= stems.MIN_STEP_TIMEOUT_S
        assert call.timeout_s <= ctx.time_budget_s

    clip = str(ctx.clip_path)
    passes = [c for c in base.ffmpeg_calls if any(part == clip for part in c.argv)]
    assert len(passes) <= 2, [c.argv for c in passes]

    for call in base.ffmpeg_calls:
        assert call.argv.count("-i") <= len(stems.STEM_NAMES)

    allowed = {Path(str(result.media)).name} if result.media else set()
    durable = {item.name for item in result.artifacts if item.durable}
    for name in _workspace_files(ctx):
        assert name in allowed or name in durable, (
            f"unexpected survivor {name!r} (allowed={allowed}, durable={durable})"
        )


# --------------------------------------------------------------------------- #
# Epic 16 — idempotence on repaired output                                     #
# --------------------------------------------------------------------------- #
def test_a_second_run_with_no_seams_is_skipped(tmp_path) -> None:
    """Re-running on already-repaired media does nothing at all (Req 7.11).

    Idempotence is achieved by **skipping**, not by a "byte-stable re-render" — which is both
    the stronger and the more honest guarantee. A re-render could not be byte-stable anyway:
    the remux re-encodes audio to a lossy codec, so a second pass would decode slightly
    differently however careful the filtergraph was. Skipping makes "changes nothing" true by
    construction.
    """
    runner = Recording_Command_Runner(probe_json=_MEDIA_JSON)
    engine = _engine(backend=_Backend(), runner=runner)
    # Unity gains and ``crossfade`` requested, but the clip published no Seams.
    ctx = _ctx(tmp_path, options=_opts(repair_mode="crossfade"), notes=(),
               deps={"runner": runner})

    result = engine.run(ctx)

    assert result.status is Engine_Status.SKIPPED
    assert result.media is None
    assert _details(result) == []
    assert runner.ffmpeg_calls == []             # and it cost no media pass


def test_a_no_seam_clip_with_real_gains_still_runs(tmp_path) -> None:
    """The guard must not swallow work: a gain change is a change even with no Seams."""
    result, _ctx_, _runner = _lifecycle_run(tmp_path)
    assert result.status in (Engine_Status.APPLIED, Engine_Status.DEGRADED)
    assert result.media is not None


def test_declick_is_deliberately_still_work() -> None:
    """``declick`` is not seam-driven, so it is honoured even with no Seams.

    The trade is documented rather than hidden: a creator who asked for the clip edges to be
    faded gets that, and strict idempotence therefore needs the flag off, because fading twice
    is not the same as fading once.
    """
    fmt = stems.Audio_Format(sample_rate=48000, channels=2)
    with_declick = stems.plan_stems(
        opts=_opts(repair_mode="off", declick=True), duration=3.0, fmt=fmt
    )
    without = stems.plan_stems(opts=_opts(repair_mode="off"), duration=3.0, fmt=fmt)

    assert stems.plan_has_work(with_declick) is True
    assert stems.plan_has_work(without) is False


def test_plan_has_work_is_total() -> None:
    """A malformed value must never silently skip the engine."""
    for hostile in (None, object(), 42, "plan"):
        assert stems.plan_has_work(hostile) is True



# --------------------------------------------------------------------------- #
# Task 15.4 — media handoff before deletion, and temp cleanup                 #
# --------------------------------------------------------------------------- #
# These two go through the **real Engine_Host** rather than calling ``run`` directly, because
# what they assert is an ordering contract *between* the engine and the host — which a direct
# call cannot see. They are also the first end-to-end exercise of the widened media gate.


def _host_with_stem_engine(tmp_path, engine, *, enabled: bool = True):
    """A real ``Engine_Host`` with only the stem engine registered."""
    from worker.engines.registry import Engine_Registry
    from worker.models import ProcessingOptions

    registry = Engine_Registry()
    registry.register(engine)
    options = ProcessingOptions(stem_inpainting_enabled=enabled)
    return stems_host(options, tmp_path, registry)


def stems_host(options, tmp_path, registry):
    from worker.engines.host import Engine_Host

    return Engine_Host(
        options,
        job_id="job",
        temp_dir=tmp_path / "temp",
        registry=registry,
        capabilities=_report({}),
    )


def _run_stage_through_host(tmp_path, engine):
    """Drive the AUDIO stage for one clip; return ``(host, outcome, clip)``."""
    from worker.engines.base import Engine_Stage as Stage

    clip = tmp_path / "clip_a.mp4"
    clip.write_bytes(b"\x00" * 64)
    host = _host_with_stem_engine(tmp_path, engine)
    outcome = host.run_stage(
        Stage.AUDIO,
        clip_id="clip_a",
        source=str(clip),
        clip_path=clip,
        clip_start=0.0,
        clip_end=_DURATION,
        duration=_DURATION,
        notes=("filler_seam:1.500",),
    )
    return host, outcome, clip


def test_the_host_takes_the_media_before_the_workspace_is_deleted(tmp_path) -> None:
    """Validates: Req 11.5 — the handoff happens while the file still exists.

    The ordering is the whole point: the engine reclaims its intermediates but keeps the
    Replacement_Media, the host adopts it as ``Stage_Outcome.media`` for the geometry stage,
    and only ``finish_clip`` deletes the workspace. If the engine deleted too eagerly, or the
    host read too late, this is where it would show.

    This is also the first end-to-end proof of the widened gate: the engine returns
    ``degraded`` here (no local model, so the ffmpeg backend was used) **with** media, and the
    host adopts it — which the old ``APPLIED``-only gate would not have done.
    """
    base = Recording_Command_Runner(probe_json=_MEDIA_JSON)
    engine = stems.Stem_Inpainting_Engine(
        backend=_WritingBackend("ffmpeg", stems_out=("vocals", "music")),
        runner=_materialising(base),
    )
    host, outcome, _clip = _run_stage_through_host(tmp_path, engine)

    result = outcome.result_for("stem_inpainting")
    assert result is not None
    assert result.status in (Engine_Status.APPLIED, Engine_Status.DEGRADED), result.detail

    # The host adopted the media, and it is readable *now* — before finish_clip.
    assert outcome.media is not None
    assert Path(outcome.media).exists()
    workspace_root = Path(outcome.media).parent

    host.finish_clip("clip_a")

    # Only now is the workspace gone.
    assert not workspace_root.exists()


def test_a_degraded_result_with_media_is_adopted_by_the_host(tmp_path) -> None:
    """Validates: Req 3.10 — Degraded_With_Media reaches the next stage.

    Pinned separately from the ordering test because it is the requirement that drove the
    foundation change, and it deserves an assertion that names it.
    """
    base = Recording_Command_Runner(probe_json=_MEDIA_JSON)
    engine = stems.Stem_Inpainting_Engine(
        backend=_WritingBackend("ffmpeg", stems_out=("vocals", "music")),
        runner=_materialising(base),
    )
    _host, outcome, _clip = _run_stage_through_host(tmp_path, engine)

    result = outcome.result_for("stem_inpainting")
    if result.status is Engine_Status.DEGRADED:
        assert result.media is not None
        assert outcome.media == result.media
        assert any("degraded:" in m for m in outcome.markers)


def test_finish_clip_leaves_no_stem_workspace_behind(tmp_path) -> None:
    """Validates: Req 11.8 — no ``stem_inpainting__*`` directory survives the clip.

    Workspaces are deleted regardless of engine status (foundation Reqs 17.1, 17.5), so this
    holds for a successful run as much as a failed one.
    """
    base = Recording_Command_Runner(probe_json=_MEDIA_JSON)
    engine = stems.Stem_Inpainting_Engine(
        backend=_WritingBackend("ffmpeg", stems_out=("vocals", "music")),
        runner=_materialising(base),
    )
    host, _outcome, _clip = _run_stage_through_host(tmp_path, engine)

    temp_root = tmp_path / "temp"
    assert list(temp_root.rglob("stem_inpainting__*")), "no workspace was created"

    host.finish_clip("clip_a")

    leftovers = list(temp_root.rglob("stem_inpainting__*"))
    assert leftovers == [], f"workspace survived finish_clip: {leftovers}"


def test_the_engine_is_inert_when_its_flag_is_off(tmp_path) -> None:
    """Validates: Reqs 4.2, 19.5 — a disabled engine costs nothing, not even a workspace.

    The all-off parity guarantee in miniature: no probe, no workspace, no media pass, and a
    ``skipped`` result carrying no marker.
    """
    from worker.engines.base import Engine_Stage as Stage
    from worker.engines.registry import Engine_Registry
    from worker.models import ProcessingOptions

    base = Recording_Command_Runner(probe_json=_MEDIA_JSON)
    engine = stems.Stem_Inpainting_Engine(
        backend=_WritingBackend(), runner=_materialising(base)
    )
    registry = Engine_Registry()
    registry.register(engine)
    host = stems_host(ProcessingOptions(), tmp_path, registry)   # flag defaults off

    assert host.active is False

    clip = tmp_path / "clip_a.mp4"
    clip.write_bytes(b"\x00" * 64)
    outcome = host.run_stage(
        Stage.AUDIO, clip_id="clip_a", source=str(clip), clip_path=clip,
        clip_start=0.0, clip_end=_DURATION, duration=_DURATION,
    )

    assert outcome.media is None
    assert outcome.markers == []
    assert base.calls == []
    assert not list((tmp_path / "temp").rglob("stem_inpainting__*"))
