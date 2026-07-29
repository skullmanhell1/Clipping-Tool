"""Stem planner property module for the audio-stem-inpainting spec
(``worker/engines/stems.py``).

Covers the pure-planner properties of epic 5: **P1** (planning is pure and never mutates
the caller, task 5.6), **P2** (equal inputs produce equal plans that name their
environment, task 5.7) and **P7** (seam intake is robust and windows are always
normalised, task 5.8). Task 7.3 appends **P6** (seam publication is exactly the interior
joins) to this same file.

Note on ``plan(ctx)``
--------------------
The design states these properties in terms of the ``AV_Engine.plan`` hook. That method
does not exist yet — ``Stem_Inpainting_Engine`` lands in spec **task 13.1**, and its
``plan`` is specified to be a one-line delegation to the module-level
:func:`worker.engines.stems.plan_stems_from_context`. The clauses below therefore exercise
that function, which is the body the future hook will call; when task 13.1 lands, the hook
inherits these guarantees unchanged.

Everything here is pure and offline: no ffmpeg, no probe, no ``demucs``, no model file, no
network. The Capability_Report is always the foundation report driven by
:class:`tests.fakes.StaticProber`, so no real capability is ever probed.
"""

from __future__ import annotations

import dataclasses
import math
import socket
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes import Recording_Command_Runner, StaticProber
from tests.strategies import (
    st_keep_plan,
    st_repair_window_ms,
    st_seam_notes,
    st_stem_options,
    st_time_base,
    st_word_timeline,
)
from worker.engines import stems
from worker.engines.base import Engine_Context, Engine_Stage, options_digest
from worker.engines.capabilities import MODEL_LOCATORS, Capability_Report
from worker.engines.host import filler_seam_notes

ENGINE_ID = "stem_inpainting"
JOB_ID = "job-stems"

#: The two optional Capability_Ids the ``ml`` backend needs (Req 12.1). Anything else the
#: planner might ask about answers unavailable through :class:`StaticProber`'s default.
_ML_CAPABILITIES = ("python_pkg:demucs", "model:htdemucs")


def _report(*, ml: bool = False, mapping: dict | None = None) -> Capability_Report:
    """A foundation Capability_Report over a static, offline prober."""
    answers = {name: bool(ml) for name in _ML_CAPABILITIES}
    answers["binary:ffmpeg"] = True
    if mapping:
        answers.update(mapping)
    return Capability_Report(StaticProber(answers))


def _context(
    *,
    options: stems.Stem_Options,
    notes: tuple[str, ...] = (),
    duration: float = 6.0,
    time_base=None,
    words: tuple = (),
    capabilities: Capability_Report | None = None,
    runner: Recording_Command_Runner | None = None,
    seed: int = 7,
) -> Engine_Context:
    """An AUDIO-stage Engine_Context carrying exactly what the planner reads.

    ``source_path`` is a path that does not exist on purpose: the planner must never read
    it (Req 2.3), and a non-existent path makes an accidental read fail loudly rather than
    quietly succeed. The command runner is injected through ``deps`` — the seam task 8.1
    formalises — so P1 can assert it was never called.
    """
    return Engine_Context(
        job_id=JOB_ID,
        clip_id="clip-000",
        engine_id=ENGINE_ID,
        stage=Engine_Stage.AUDIO,
        source_path=Path("/nonexistent/source.mp4"),
        clip_path=Path("/nonexistent/clip.mp4"),
        time_base=time_base if time_base is not None else stems.Time_Base(),
        clip_start=0.0,
        clip_end=float(duration),
        duration=float(duration),
        words=tuple(words),
        options=options,
        options_digest=options_digest(options),
        seed=int(seed),
        capabilities=capabilities if capabilities is not None else _report(),
        notes=tuple(notes),
        deps={"runner": runner} if runner is not None else {},
    )


# --------------------------------------------------------------------------- #
# P1 — planning is pure and never mutates the caller (task 5.6)               #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 1: Planning is pure and never mutates the
# caller
@settings(max_examples=100, deadline=None)
@given(
    supplied=st_stem_options(),
    seam_case=st_seam_notes(),
    words=st_word_timeline(),
    time_base=st_time_base(),
    ml=st.booleans(),
)
def test_p1_planning_is_pure_and_never_mutates_the_caller(
    supplied: dict, seam_case: dict, words: tuple, time_base, ml: bool
) -> None:
    """Planning spends no subprocess, no import, no socket and no model read.

    Each clause is asserted against a real observer rather than by inspection:

    * **zero command-runner invocations** — an injected
      :class:`Recording_Command_Runner` records every call it receives; the planner must
      leave it at zero (Req 1.9, 19.1).
    * **imports no separation package** — ``sys.modules`` is snapshotted around the call
      and must be unchanged for ``demucs``/``torch``, which is what keeps a minimal
      install able to plan (Req 12.5).
    * **opens no socket** — ``socket.socket`` is swapped for a tripwire that raises, so any
      network attempt fails the test instead of silently succeeding (Req 12.5, 16.1).
    * **reads no model file** — a tripwire is registered in the foundation
      ``MODEL_LOCATORS`` for the drawn model name; the injected Capability_Report answers
      from a static mapping, so the locator (the only thing that would touch the model
      directory) must never be invoked (Req 12.4).
    * **leaves the caller's options identical** and **every context field assignment
      raises** — the Engine_Context is frozen and the planner reads by ``getattr`` only
      (Req 1.3).
    """
    options = stems.Stem_Options.parse(supplied)
    runner = Recording_Command_Runner()
    ctx = _context(
        options=options,
        notes=seam_case["notes"],
        duration=seam_case["duration"],
        time_base=time_base,
        words=words,
        capabilities=_report(
            ml=ml, mapping={"model:" + stems.resolve_model(options): ml}
        ),
        runner=runner,
    )
    before = dataclasses.asdict(ctx.options)

    located: list[str] = []
    model_name = stems.resolve_model(options)
    previous = MODEL_LOCATORS.get(model_name)

    def _tripwire():
        located.append(model_name)
        return None

    MODEL_LOCATORS[model_name] = _tripwire
    real_socket = socket.socket

    class _NoSockets:
        def __init__(self, *args, **kwargs):
            raise AssertionError("planning opened a socket")

    socket.socket = _NoSockets  # type: ignore[assignment]
    modules_before = set(sys.modules)
    try:
        plan = stems.plan_stems_from_context(ctx)
    finally:
        socket.socket = real_socket  # type: ignore[assignment]
        if previous is None:
            MODEL_LOCATORS.pop(model_name, None)
        else:
            MODEL_LOCATORS[model_name] = previous

    assert runner.calls == []
    assert located == []
    for package in ("demucs", "torch", "numpy", "soundfile"):
        assert (package in sys.modules) == (package in modules_before)
    assert dataclasses.asdict(ctx.options) == before

    # The context is frozen: every field assignment raises (Req 1.3).
    for entry in dataclasses.fields(Engine_Context):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(ctx, entry.name, getattr(ctx, entry.name))

    # And the plan is a value, not a view onto the context.
    assert isinstance(plan, stems.Stem_Plan)
    assert plan.to_dict() == stems.plan_stems_from_context(ctx).to_dict()


# --------------------------------------------------------------------------- #
# P2 — equal inputs, equal plans, and the plan names its environment (5.7)    #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 2: Equal inputs produce equal plans, and the
# plan names its environment
@settings(max_examples=100, deadline=None)
@given(
    supplied=st_stem_options(),
    seam_case=st_seam_notes(),
    words=st_word_timeline(),
    ml=st.booleans(),
)
def test_p2_equal_inputs_produce_equal_plans_that_name_their_environment(
    supplied: dict, seam_case: dict, words: tuple, ml: bool
) -> None:
    """Two invocations with equal inputs produce equal plans, in bounds, fully named.

    Determinism is asserted on ``to_dict()`` rather than on the dataclass, because the
    serialised form is what a reproduced run is actually compared against (Req 10.1, 10.6)
    — and it is asserted across two *separately built* contexts, so nothing can be carried
    over in a cached object. Every plan timestamp lies inside ``[0, duration]`` (Req 2.3,
    2.8), and ``backend``/``model`` are non-empty so the plan names the environment that
    produced it (Req 10.7).
    """
    options = stems.Stem_Options.parse(supplied)
    duration = seam_case["duration"]
    caps = {"model:" + stems.resolve_model(options): ml}

    first = stems.plan_stems_from_context(
        _context(options=options, notes=seam_case["notes"], duration=duration,
                 capabilities=_report(ml=ml, mapping=caps))
    )
    second = stems.plan_stems_from_context(
        _context(options=stems.Stem_Options.parse(supplied),
                 notes=tuple(seam_case["notes"]), duration=duration, words=words,
                 capabilities=_report(ml=ml, mapping=caps))
    )

    assert first.to_dict() == second.to_dict()
    assert first == second

    # The plan names its environment (Req 10.7).
    assert first.backend in ("ml", "ffmpeg")
    assert first.model
    assert first.repair_mode in stems.REPAIR_MODES
    assert first.downgraded_from in ("", "spectral")

    # Every timestamp is clip-relative and in bounds (Req 2.3, 2.8).
    assert 0.0 <= first.duration <= duration
    for seam in first.seams:
        assert 0.0 <= seam <= duration
    for window in first.windows:
        assert 0.0 <= window.start < window.end <= duration
        for seam in window.seams:
            assert 0.0 <= seam <= duration

    # Windows are accounted for exactly once by the two repair treatments.
    assert first.bridged_windows + first.notched_windows == len(first.windows)
    assert first.bridged_windows <= stems.MAX_BRIDGE_WINDOWS


# --------------------------------------------------------------------------- #
# P7 — seam intake is robust and windows are always normalised (task 5.8)     #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 7: Seam intake is robust and windows are
# always normalised
@settings(max_examples=100, deadline=None)
@given(
    seam_case=st_seam_notes(),
    window_ms=st_repair_window_ms(valid_only=True),
    repair_mode=st.sampled_from(["crossfade", "spectral"]),
    time_base=st_time_base(),
)
def test_p7_seam_intake_is_robust_and_windows_are_normalised(
    seam_case: dict, window_ms: int, repair_mode: str, time_base
) -> None:
    """The Seam list is exactly the valid notes, and the windows are always canonical.

    The generator's ``expected_seams`` is the oracle: the finite, in-bounds
    ``filler_seam:`` values and nothing else. The planner sorts and de-duplicates
    (Req 6.6), so the comparison is against ``sorted(set(...))`` — and it is an equality,
    which is what rules out an *inferred* extra Seam from the waveform or a Word_Timeline
    gap (Req 6.5).

    The window list is then asserted to be exactly what ``normalize_segments`` promises:
    sorted, non-degenerate, pairwise non-overlapping and contained in ``[0, duration]``
    (Req 6.7, 6.8) — with every planned Seam attributed to the window that absorbed it, so
    a merged cluster is repaired once and no Seam is silently dropped (Req 7.7).
    """
    duration = seam_case["duration"]
    options = stems.Stem_Options.parse(
        {"repair_mode": repair_mode, "repair_window_ms": window_ms, "backend": "ffmpeg"}
    )
    plan = stems.plan_stems_from_context(
        _context(options=options, notes=seam_case["notes"], duration=duration,
                 time_base=time_base)
    )

    assert list(plan.seams) == sorted(set(seam_case["expected_seams"]))

    previous_end = 0.0
    covered: list[float] = []
    for window in plan.windows:
        assert 0.0 <= window.start < window.end <= duration
        assert window.start >= previous_end          # sorted and pairwise disjoint
        previous_end = window.end
        assert list(window.seams) == sorted(window.seams)
        covered.extend(window.seams)

    # Every Seam that could produce a window is attributed to exactly one window.
    assert len(covered) == len(set(covered))
    assert set(covered) <= set(plan.seams)
    if plan.windows:
        assert covered, "a planned window must name the Seam(s) it repairs"

    # Re-planning the same context reproduces the same windows (idempotent intake).
    assert plan.to_dict()["windows"] == stems.plan_stems_from_context(
        _context(options=options, notes=seam_case["notes"], duration=duration,
                 time_base=time_base)
    ).to_dict()["windows"]

    # Repair_Mode "off" plans no window at all, whatever the notes say (Req 7.10).
    quiet = stems.plan_stems_from_context(
        _context(
            options=stems.Stem_Options.parse({"repair_mode": "off"}),
            notes=seam_case["notes"],
            duration=duration,
            time_base=time_base,
        )
    )
    assert quiet.windows == ()
    assert list(quiet.seams) == list(plan.seams)
    assert math.isfinite(quiet.duration)



# --------------------------------------------------------------------------- #
# P6 — seam publication is exactly the interior joins (task 7.3)              #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 6: Seam publication is exactly the interior
# joins, with `rebase_words` rounding
@settings(max_examples=100, deadline=None)
@given(plan=st_keep_plan(), solid=st_keep_plan(allow_zero_length=False, min_keeps=2))
def test_p6_seam_publication_is_exactly_the_interior_joins(plan, solid) -> None:
    """``N`` keeps publish exactly ``N - 1`` interior Seam notes, rounded like the words.

    The oracle is recomputed from the drawn ``FillerPlan.keeps`` rather than read back from
    the implementation: the *i*-th note must be the running sum of the preceding keep
    durations, rounded to three decimals — the same rounding
    ``filler.rebase_words`` applies to the word times, which is what makes a Seam land
    exactly between the two words it joins (Req 6.2, 6.3).

    The two boundary clauses are the ones that matter for repair: no note equals the clip
    start ``0.0`` and none equals the total tightened duration (Req 6.9), because neither is
    a *join* — repairing them would fade the clip's own head or tail. They are asserted on
    the ``solid`` draw, whose keeps all have non-zero length: a **zero-length keep** is
    degenerate (it contributes no output audio, so its "join" coincides with a neighbour's
    boundary) and ``plan_keep_intervals`` never produces one — the generator can, so the
    boundary clauses are stated where they are meaningful rather than weakened for it. The
    count and value clauses hold for *every* draw, degenerate keeps included.

    Publication reads ``keeps`` and nothing else: no call into ``worker.effects.filler``,
    no re-planning (Req 8.2).
    """
    keeps = list(plan.keeps)
    notes = filler_seam_notes(keeps)

    assert len(notes) == max(0, len(keeps) - 1)
    assert all(note.startswith("filler_seam:") for note in notes)

    cursor = 0.0
    expected: list[str] = []
    for keep in keeps[:-1]:
        cursor += float(keep.duration)
        expected.append(f"filler_seam:{round(cursor, 3):.3f}")
    assert list(notes) == expected

    values = [float(note.split(":", 1)[1]) for note in notes]
    assert values == sorted(values)

    # Boundary clauses on the non-degenerate draw (Req 6.9).
    solid_keeps = list(solid.keeps)
    solid_notes = filler_seam_notes(solid_keeps)
    solid_total = round(sum(float(keep.duration) for keep in solid_keeps), 3)
    solid_values = [float(note.split(":", 1)[1]) for note in solid_notes]
    assert solid_values, "two or more non-degenerate keeps must publish a join"
    assert all(0.0 < value < solid_total for value in solid_values)

    # The engine reads back exactly what was published, in bounds (Req 6.4-6.6).
    parsed = stems.parse_seam_notes(solid_notes, solid_total)
    assert parsed == sorted(set(solid_values))

    # A single keep (or none) is not a join, so nothing is published at all.
    assert filler_seam_notes(keeps[:1]) == ()
    assert filler_seam_notes([]) == ()
    assert filler_seam_notes(None) == ()


def test_seam_notes_reach_the_audio_stage_context_and_nothing_else_changes(
    tmp_path: Path,
) -> None:
    """The host publishes the notes on the AUDIO-stage context, additively (Req 8.1, 8.5).

    A unit pin rather than a property: it wires a recording engine into a real
    :class:`Engine_Host`, runs the AUDIO stage with and without ``filler_plan``, and asserts
    that the notes appear only when a plan with an interior join is supplied — so a caller
    that omits the keyword builds contexts identical to the pre-Seam ones (Req 20.6).
    """
    from tests.fakes import FakeEngine

    from worker.effects.filler import Interval
    from worker.engines.host import Engine_Host
    from worker.engines.registry import Engine_Registry

    recorded: list[tuple[str, ...]] = []

    class _Recorder(FakeEngine):
        def run(self, ctx):  # type: ignore[override]
            recorded.append(tuple(ctx.notes))
            return super().run(ctx)

    registry = Engine_Registry()
    registry.register(_Recorder(engine_id="notes_probe", stage=Engine_Stage.AUDIO))
    host = Engine_Host(
        type("_O", (), {"notes_probe_enabled": True})(),
        job_id=JOB_ID,
        temp_dir=tmp_path,
        registry=registry,
        capabilities=_report(),
    )
    keeps = [Interval(0.0, 1.0), Interval(2.0, 3.5), Interval(4.0, 4.25)]

    host.run_stage(
        Engine_Stage.AUDIO, clip_id="c1", source="s.mp4", clip_path=None,
        clip_start=0.0, clip_end=2.75, duration=2.75, filler_plan=keeps,
    )
    host.run_stage(
        Engine_Stage.AUDIO, clip_id="c2", source="s.mp4", clip_path=None,
        clip_start=0.0, clip_end=2.75, duration=2.75,
    )

    # The Seam notes are *appended*: the host's own ``fps_fallback:`` note (this host has
    # no probed fps, so it substitutes) keeps its position and spelling (Req 8.5, 20.6).
    seam_only = tuple(n for n in recorded[0] if n.startswith("filler_seam:"))
    assert seam_only == ("filler_seam:1.000", "filler_seam:2.500")
    assert recorded[0][: len(recorded[0]) - 2] == recorded[1]
    assert not any(n.startswith("filler_seam:") for n in recorded[1])



# =========================================================================== #
# P8 — the no-op configuration costs nothing                                   #
# =========================================================================== #
# Deferred from epic 5 until ladder rungs 0 and 3 existed (13.2), because that is what this
# property asserts against. "Costs nothing" is checked as **observable absence of work** —
# zero runner invocations, zero backend calls, no file in the workspace — rather than as a
# fast return, because only the former distinguishes "skipped before doing anything" from
# "did the work and threw it away".

import time as _time                                            # noqa: E402
from pathlib import Path as _Path                                # noqa: E402

from tests.fakes import (                                        # noqa: E402
    Recording_Command_Runner as _Runner,
    StaticProber as _Prober,
)
from worker.engines.artifacts import allocate_workspace as _alloc  # noqa: E402
from worker.engines.base import (                                # noqa: E402
    Engine_Context as _Ctx,
    Engine_Stage as _Stage,
    Engine_Status as _Status,
)
from worker.engines.capabilities import Capability_Report as _Report  # noqa: E402
from worker.engines.timebase import Time_Base as _TB             # noqa: E402


class _CountingBackend:
    """A separator that records whether it was ever asked to do anything."""

    backend_id = "ml"
    requires_network = False

    def __init__(self) -> None:
        self.calls = 0

    def separate(self, source, dest_dir, *, fmt, seed, timeout_s):
        self.calls += 1
        return {}


def _p8_context(root: _Path, options, runner, *, enabled_options=None):
    clip = root / "clip.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    if not clip.exists():
        clip.write_bytes(b"\x00" * 64)
    return _Ctx(
        job_id="job", clip_id="clip_a", engine_id=stems.ENGINE_ID, stage=_Stage.AUDIO,
        source_path=clip, clip_path=clip, time_base=_TB(sample_rate=48000),
        clip_start=0.0, clip_end=3.0, duration=3.0,
        options=options if enabled_options is None else enabled_options,
        options_digest="p8", seed=1,
        workspace=_alloc(root / "ws", "job", "clip_a", stems.ENGINE_ID, "p8"),
        capabilities=_Report(prober=_Prober({}, default=True)),
        permissibility=False, deadline=_time.monotonic() + 120.0, time_budget_s=120.0,
        notes=("filler_seam:1.500",), deps={"runner": runner},
    )


# Feature: audio-stem-inpainting, Property 8: The no-op configuration costs nothing
@settings(max_examples=100, deadline=None)
@given(option_map=st_stem_options())
def test_p8_the_noop_configuration_costs_nothing(option_map: dict, tmp_path_factory) -> None:
    """Rung 3: unity gains plus ``repair_mode="off"`` is skipped before any work happens.

    The generated options are forced onto the no-op configuration, so every example exercises
    the rung rather than only the occasional one that happens to land there. Asserted: status
    ``skipped``, **no marker** (a no-op is not a degradation and reporting one would be noise),
    no media, zero command-runner invocations, zero backend calls, and **not a single file in
    the Engine_Workspace** — the last being what proves no probe and no extraction happened.
    """
    root = tmp_path_factory.mktemp("p8")
    options = stems.resolve_stem_options(
        type("O", (), {
            **{f"stem_{k}": v for k, v in option_map.items()},
            # Force the no-op shape: neutral gains, no repair.
            "stem_mix_preset": "custom",
            "stem_gain_vocals": 1.0,
            "stem_gain_music": 1.0,
            "stem_gain_other": 1.0,
            "stem_repair_mode": "off",
        })()
    )
    assert stems.plan_is_noop(
        stems.plan_stems(opts=options, duration=3.0)
    ), "the generated options were not the no-op configuration"

    runner = _Runner()
    backend = _CountingBackend()
    engine = stems.Stem_Inpainting_Engine(backend=backend, runner=runner)
    ctx = _p8_context(root, options, runner)

    result = engine.run(ctx)

    assert result.status is _Status.SKIPPED
    assert result.markers == ()
    assert result.media is None
    assert runner.calls == []
    assert backend.calls == 0
    assert [p for p in ctx.workspace.root.rglob("*") if p.is_file()] == []


@settings(max_examples=100, deadline=None)
@given(option_map=st_stem_options())
def test_p8_a_disabled_flag_costs_nothing_for_any_options(
    option_map: dict, tmp_path_factory
) -> None:
    """Rung 0: with the Feature_Flag off the engine is never invoked at all.

    Asserted through the **real Engine_Host**, because rung 0 is the host's gate rather than
    the engine's: what has to be true is that no workspace is allocated, no exclusive
    capability is probed and no media pass is spent — none of which the engine could observe
    about itself. Holds for *any* option mapping, including ones that would otherwise be
    expensive.
    """
    from worker.engines.host import Engine_Host
    from worker.engines.registry import Engine_Registry
    from worker.models import ProcessingOptions

    root = tmp_path_factory.mktemp("p8_off")
    runner = _Runner()
    backend = _CountingBackend()
    engine = stems.Stem_Inpainting_Engine(backend=backend, runner=runner)

    registry = Engine_Registry()
    registry.register(engine)
    options = ProcessingOptions.from_dict(
        {f"stem_{k}": v for k, v in option_map.items()}
    )                                          # stem_inpainting_enabled stays False
    assert options.stem_inpainting_enabled is False

    temp_dir = root / "temp"
    host = Engine_Host(
        options, job_id="job", temp_dir=temp_dir, registry=registry,
        capabilities=_Report(prober=_Prober({}, default=True)),
    )

    assert host.active is False                # no probe, no allocation, nothing

    clip = root / "clip.mp4"
    clip.write_bytes(b"\x00" * 64)
    outcome = host.run_stage(
        _Stage.AUDIO, clip_id="clip_a", source=str(clip), clip_path=clip,
        clip_start=0.0, clip_end=3.0, duration=3.0, notes=("filler_seam:1.500",),
    )

    assert outcome.media is None
    assert outcome.markers == []               # skipped contributes no marker
    assert runner.calls == []
    assert backend.calls == 0
    assert not temp_dir.exists() or not list(temp_dir.rglob("stem_inpainting__*"))
