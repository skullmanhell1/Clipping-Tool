"""Engine_Host property module for the av-engines-foundation spec
(``worker/engines/host.py``).

Covers the design's numbered properties for gating, isolation, timing and lifecycle:

* **P7**  — marker merge is namespaced, ordered, deduplicated, silent for skips (9.6).
* **P8**  — source-stage engines run once per source and are reused (9.7).
* **P9**  — disabled engines cost nothing (9.8).
* **P12** — missing capabilities degrade with exact, single markers (9.9).
* **P14** — one engine's failure is isolated (9.10).
* **P15** — time budgets are enforced and abandoned cleanly (9.11).
* **P23** — every engine of a clip shares one Time_Base and adds no probe (9.12).
* **P27** — the rebased Word_Timeline reaches every subsequent engine (9.13).
* **P28** — independent engines are confluent (9.14).
* **P33** — permissibility blocks network engines and keeps runs offline (9.15).

plus the failure-logging and media-fallback unit examples (9.16) and two lifecycle
regressions covering the SOURCE-stage finalisation and unconditional per-clip
workspace deletion.

Generators come from the shared ``tests/strategies.py`` module (``st_registrations``,
``st_engine_outcomes``, ``st_availability_map``, ``st_word_timeline``, ``st_time_base``)
and the doubles from ``tests/fakes.py`` (``FakeEngine``, ``RaisingEngine``,
``SlowEngine``, ``FakeClock``, ``StaticProber``, ``CountingProber``,
``RecordingStorage``) — never redefined here — so the queued stem-separation and
kinetic-typography specs exercise the same input space and the same doubles.

**Global state.** ``worker.engines`` owns three process-wide singletons that would
otherwise leak between hypothesis examples inside a *single* test function: the default
Engine_Registry, the process-wide Capability_Report, and the ``MODEL_LOCATORS`` mapping.
:func:`reset_engine_globals` is therefore called **inside every property body**, not only
from the autouse fixture (a fixture runs once per *test*, not once per *example*). Every
property additionally builds its own ``Engine_Registry`` and injects its own
Capability_Report, so nothing depends on the defaults at all.

**Offline and ffmpeg-free.** No property here runs ffmpeg or ffprobe: media is described
by a hand-built ``MediaInfo``, engines are doubles, time comes from ``FakeClock``, and
P23/P33 actively patch ``worker.ffmpeg_utils.probe`` / ``socket.socket`` to raise. Each
example gets its own ``tempfile.TemporaryDirectory`` as the Pipeline ``temp_dir`` (rather
than the function-scoped ``tmp_path`` fixture, which hypothesis would share across every
example of one test), and every filesystem-touching property uses
``@settings(deadline=None)``.
"""
from __future__ import annotations

import dataclasses
import logging
import socket
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import worker.ffmpeg_utils as fu
from tests.fakes import (
    CountingProber,
    FakeClock,
    FakeEngine,
    RaisingEngine,
    RecordingStorage,
    SlowEngine,
    StaticProber,
)
from tests.strategies import (
    st_availability_map,
    st_engine_outcomes,
    st_registrations,
    st_time_base,
    st_word_timeline,
)
from worker.effects import filler
from worker.engines.artifacts import ENGINE_TEMP_ROOT, artifact_key, sanitize_component
from worker.engines.base import (
    Compose_Contribution,
    Engine_Artifact,
    Engine_Stage,
    Engine_Status,
)
from worker.engines.capabilities import MODEL_LOCATORS, Capability_Report, reset_report
from worker.engines.host import SOURCE_CLIP_ID, Engine_Host
from worker.engines.registry import Engine_Registry, reset_registry

#: Every stage a host can be asked about, so "for every stage" assertions are
#: exhaustive rather than sampled.
ALL_STAGES: Tuple[Engine_Stage, ...] = tuple(Engine_Stage)

#: The job id every host in this module uses (already a safe path component).
JOB_ID = "job_engine_host"

#: Filesystem-touching properties: each example allocates real workspace directories,
#: so the example count matches ``tests/test_engine_artifacts.py`` rather than the
#: cheap-and-pure 100 used by the timebase/base modules.
FS_SETTINGS = settings(max_examples=50, deadline=None)


# --------------------------------------------------------------------------- #
# Global-state isolation                                                       #
# --------------------------------------------------------------------------- #
def reset_engine_globals() -> None:
    """Clear every ``worker.engines`` process-wide singleton.

    Called from the autouse fixture *and* from inside each property body: hypothesis
    runs many examples inside one test function, so a fixture alone would let the
    default registry, the cached Capability_Report and ``MODEL_LOCATORS`` leak from
    one example into the next.
    """
    reset_registry()
    reset_report()
    MODEL_LOCATORS.clear()


@pytest.fixture(autouse=True)
def clean_engine_globals():
    """Known-empty singletons during each test, **restored afterwards**.

    The teardown restores rather than merely re-clearing, which matters beyond this module:
    ``worker/engines/loader.py`` populates the default registry and ``MODEL_LOCATORS`` by
    **import side effect**, and an already-imported module cannot re-fire it. So leaving them
    empty on the way out silently breaks every later test file that expects a populated
    registry — ``/api/info`` legitimately advertises no engine, and whether a test passes
    starts depending on pytest's file ordering.

    Mirrors what ``tests/test_engine_capabilities.py`` already does for ``MODEL_LOCATORS``.
    """
    from worker.engines.registry import get_registry, register

    engines_before = [record.engine for record in get_registry().records()]
    locators_before = dict(MODEL_LOCATORS)

    reset_engine_globals()
    yield
    reset_engine_globals()

    for engine in engines_before:
        register(engine)
    MODEL_LOCATORS.update(locators_before)
    reset_report()


# --------------------------------------------------------------------------- #
# Local helpers (host wiring only — no doubles, no generators, are defined here) #
# --------------------------------------------------------------------------- #
def media_info(
    *,
    duration: float = 12.0,
    fps: float = 30.0,
    width: int = 1920,
    height: int = 1080,
    has_audio: bool = True,
) -> fu.MediaInfo:
    """A hand-built ``MediaInfo``, so no ffprobe pass is ever needed."""
    return fu.MediaInfo(
        duration=duration, width=width, height=height, fps=fps, has_audio=has_audio
    )


def options_for(flags: Mapping[str, bool], *, permissibility: bool = False) -> Dict[str, Any]:
    """Processing_Options stand-in: ``{"<engine_id>_enabled": bool, ...}``.

    A plain mapping is used deliberately. ``AV_Engine.is_enabled`` reads its
    Feature_Flag by attribute *then* by mapping key, and ``ProcessingOptions`` carries
    no engine flags yet (this spec registers no engines), so a mapping is the only way
    to drive arbitrary generated Engine_Ids through the real gating path.
    """
    options: Dict[str, Any] = {"permissibility_mode": bool(permissibility)}
    for engine_id, enabled in dict(flags).items():
        options[f"{engine_id}_enabled"] = bool(enabled)
    return options


def all_enabled(engine_ids: Iterable[str], *, permissibility: bool = False) -> Dict[str, Any]:
    """Options enabling every one of ``engine_ids``."""
    return options_for({engine_id: True for engine_id in engine_ids},
                       permissibility=permissibility)


def registry_of(engines: Iterable[Any]) -> Engine_Registry:
    """An isolated :class:`Engine_Registry` holding ``engines`` (Req 22.2)."""
    registry = Engine_Registry()
    for engine in engines:
        registry.register(engine)
    return registry


def build_host(temp_dir: Path, registry: Engine_Registry, options: Any, **kwargs: Any) -> Engine_Host:
    """An :class:`Engine_Host` on ``temp_dir`` with every collaborator injected."""
    return Engine_Host(
        options, job_id=JOB_ID, temp_dir=temp_dir, registry=registry, **kwargs
    )


def run_every_stage(
    host: Engine_Host,
    *,
    clip_id: str = "clip_a",
    source: str = "/media/source.mp4",
    clip_path: Path | None = None,
    duration: float = 6.0,
    words: Tuple[Any, ...] = (),
) -> Dict[Engine_Stage, Any]:
    """Invoke every stage once for one clip, returning the Stage_Outcomes by stage."""
    return {
        stage: host.run_stage(
            stage,
            clip_id=clip_id,
            source=source,
            clip_path=clip_path,
            clip_start=0.0,
            clip_end=duration,
            duration=duration,
            words=words,
        )
        for stage in ALL_STAGES
    }


def workspace_leaf_names(temp_dir: Path, *, job_id: str = JOB_ID) -> List[str]:
    """The ``<engine>__<digest>`` directory names that exist beneath the job root."""
    root = Path(temp_dir) / ENGINE_TEMP_ROOT / sanitize_component(job_id, fallback="job")
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.glob("*/*") if path.is_dir())


def result_for(outcomes: Mapping[Engine_Stage, Any], engine_id: str):
    """The single Engine_Result recorded for ``engine_id`` across every stage."""
    found = [
        result
        for outcome in outcomes.values()
        for result in outcome.results
        if result.engine_id == engine_id
    ]
    assert len(found) == 1, f"{engine_id} was recorded {len(found)} times, expected once"
    return found[0]


def namespaced(engine_id: str, markers: Iterable[str]) -> List[str]:
    """Independent restatement of the host's namespacing rule (Req 3.3)."""
    prefix = f"engine:{engine_id}:"
    return [entry if entry.startswith(prefix) else prefix + entry for entry in markers]


# --------------------------------------------------------------------------- #
# Task 9.6 — Property 7                                                        #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 7: Marker merge is namespaced, ordered,
# deduplicated, and silent for skips — the merged list contains every non-skipped
# engine's markers exactly once, in registry invocation order, each matching
# `^engine:<engine_id>:`, with `skipped` results contributing nothing.
@FS_SETTINGS
@given(registrations=st_registrations(min_size=1, max_size=4), data=st.data())
def test_p7_marker_merge_is_namespaced_ordered_deduplicated(registrations, data):
    """Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.6"""
    reset_engine_globals()

    outcomes_by_id = {
        engine_id: data.draw(
            st_engine_outcomes(engine_id=engine_id, allow_exception=False),
            label=f"outcome:{engine_id}",
        )
        for engine_id, _stage, _priority in registrations
    }
    engines = {
        engine_id: FakeEngine(
            engine_id,
            stage,
            priority=priority,
            status=outcomes_by_id[engine_id]["status"],
            markers=outcomes_by_id[engine_id]["markers"],
        )
        for engine_id, stage, priority in registrations
    }

    with tempfile.TemporaryDirectory(prefix="engine-host-") as raw_temp:
        temp_dir = Path(raw_temp)
        registry = registry_of(engines.values())
        host = build_host(temp_dir, registry, all_enabled(engines))
        stage_outcomes = run_every_stage(host, clip_path=temp_dir / "clip_a.mp4")

    for stage, outcome in stage_outcomes.items():
        order = [engine.engine_id for engine in registry.for_stage(stage)]

        # Independent expectation: registry order, namespaced, skips silent, deduped.
        expected: List[str] = []
        seen: set = set()
        for engine_id in order:
            drawn = outcomes_by_id[engine_id]
            if drawn["status"] is Engine_Status.SKIPPED:
                continue
            for entry in namespaced(engine_id, drawn["markers"]):
                if entry in seen:
                    continue
                seen.add(entry)
                expected.append(entry)

        assert outcome.markers == expected                       # 3.2, 3.6
        assert len(outcome.markers) == len(set(outcome.markers))  # 3.6
        # Every marker is attributable to exactly one invoked engine (3.3).
        for entry in outcome.markers:
            owners = [eid for eid in order if entry.startswith(f"engine:{eid}:")]
            assert owners, f"{entry!r} is not namespaced by any engine of {stage}"
        # A skipped engine contributes nothing at all (3.4).
        for engine_id in order:
            if outcomes_by_id[engine_id]["status"] is Engine_Status.SKIPPED:
                assert not any(
                    entry.startswith(f"engine:{engine_id}:") for entry in outcome.markers
                )
        # Results are still recorded for every registered engine, in registry order (3.1).
        assert [result.engine_id for result in outcome.results] == order


# --------------------------------------------------------------------------- #
# Task 9.7 — Property 8                                                        #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 8: Source-stage engines run once per source
# and are reused — for any clip count n >= 1 a counting SOURCE-stage engine records
# exactly one invocation and every clip observes the same cached Engine_Result.
@FS_SETTINGS
@given(
    clip_count=st.integers(min_value=1, max_value=5),
    outcome=st_engine_outcomes(engine_id="source_probe", allow_exception=False),
)
def test_p8_source_engines_run_once_per_source_and_are_reused(clip_count, outcome):
    """Validates: Requirements 3.5, 19.3"""
    reset_engine_globals()

    engine = FakeEngine(
        "source_probe",
        Engine_Stage.SOURCE,
        status=outcome["status"],
        markers=outcome["markers"],
    )

    with tempfile.TemporaryDirectory(prefix="engine-host-") as raw_temp:
        temp_dir = Path(raw_temp)
        host = build_host(temp_dir, registry_of([engine]), all_enabled(["source_probe"]))
        info = media_info()

        # The Pipeline calls run_source once, but every clip may ask again: the
        # outcome is cached per source, so nothing is re-invoked.
        outcomes = [host.run_source("/media/source.mp4", info) for _ in range(clip_count)]
        cached = [host.source_result("source_probe") for _ in range(clip_count)]

    assert engine.run_count == 1                                  # 3.5, 19.3
    assert all(item is outcomes[0] for item in outcomes)          # same Stage_Outcome
    assert all(item is cached[0] for item in cached)              # same Engine_Result
    assert cached[0] is outcomes[0].result_for("source_probe")
    assert cached[0].engine_id == "source_probe"


# --------------------------------------------------------------------------- #
# Task 9.8 — Property 9                                                        #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 9: Disabled engines cost nothing — for any
# subset of enabled flags exactly that subset is invoked; for every disabled engine the
# CountingProber records zero probes of its exclusive capabilities, no workspace
# directory exists on disk, and no additional media pass occurs; with the empty subset
# the prober call count is zero overall.
@FS_SETTINGS
@given(registrations=st_registrations(min_size=1, max_size=4), data=st.data())
def test_p9_disabled_engines_cost_nothing(registrations, data):
    """Validates: Requirements 4.1, 4.2, 19.5"""
    reset_engine_globals()

    flags = {
        engine_id: data.draw(st.booleans(), label=f"flag:{engine_id}")
        for engine_id, _stage, _priority in registrations
    }
    # Every engine declares its OWN capability, so a probe can be attributed to it.
    capability_of = {engine_id: f"python_pkg:{engine_id}_only" for engine_id in flags}
    prober = CountingProber(StaticProber(default=True))
    report = Capability_Report(prober=prober)

    engines = {
        engine_id: FakeEngine(
            engine_id,
            stage,
            priority=priority,
            required_capabilities=(capability_of[engine_id],),
        )
        for engine_id, stage, priority in registrations
    }

    with tempfile.TemporaryDirectory(prefix="engine-host-") as raw_temp:
        temp_dir = Path(raw_temp)
        host = build_host(
            temp_dir, registry_of(engines.values()), options_for(flags), capabilities=report
        )
        stage_outcomes = run_every_stage(host, clip_path=temp_dir / "clip_a.mp4")
        leaves = workspace_leaf_names(temp_dir)

    invoked = {engine_id for engine_id, engine in engines.items() if engine.run_count}
    assert invoked == {engine_id for engine_id, on in flags.items() if on}    # 4.1

    for engine_id, on in flags.items():
        capability = capability_of[engine_id]
        has_workspace = any(name.startswith(f"{engine_id}__") for name in leaves)
        if on:
            assert prober.count_for(capability) == 1
            assert has_workspace
        else:
            assert prober.count_for(capability) == 0             # 4.2 — no probe
            assert not has_workspace                             # 4.2 — no workspace
            assert result_for(stage_outcomes, engine_id).status is Engine_Status.SKIPPED

    if not any(flags.values()):
        assert prober.total == 0                                 # 4.2, 19.5
        assert leaves == []
        # No media replacement, so no additional media pass is ever needed (19.5).
        assert all(outcome.media is None for outcome in stage_outcomes.values())
        assert all(
            result.status is Engine_Status.SKIPPED
            for outcome in stage_outcomes.values()
            for result in outcome.results
        )


# --------------------------------------------------------------------------- #
# Task 9.9 — Property 12                                                       #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 12: Missing capabilities degrade with exact,
# single markers — an unavailable required capability yields `degraded` with exactly
# `engine:<id>:unavailable:<first missing required id>` and a `run` body that never
# executed; each missing optional capability yields exactly
# `engine:<id>:degraded:<capability_id>`; at most one degradation marker per engine per
# clip.
@FS_SETTINGS
@given(
    registrations=st_registrations(min_size=1, max_size=3),
    availability=st_availability_map(max_size=6),
    data=st.data(),
)
def test_p12_missing_capabilities_degrade_with_exact_single_markers(
    registrations, availability, data
):
    """Validates: Requirements 7.1, 7.2, 7.4"""
    reset_engine_globals()

    known = sorted(availability)
    declared: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {}
    for engine_id, _stage, _priority in registrations:
        if known:
            required = tuple(
                data.draw(st.lists(st.sampled_from(known), max_size=3),
                          label=f"required:{engine_id}")
            )
            optional = tuple(
                data.draw(st.lists(st.sampled_from(known), max_size=2),
                          label=f"optional:{engine_id}")
            )
        else:
            required, optional = (), ()
        declared[engine_id] = (required, optional)

    report = Capability_Report(prober=StaticProber(availability, default=False))
    engines = {
        engine_id: FakeEngine(
            engine_id,
            stage,
            priority=priority,
            required_capabilities=declared[engine_id][0],
            optional_capabilities=declared[engine_id][1],
        )
        for engine_id, stage, priority in registrations
    }

    with tempfile.TemporaryDirectory(prefix="engine-host-") as raw_temp:
        temp_dir = Path(raw_temp)
        host = build_host(
            temp_dir,
            registry_of(engines.values()),
            all_enabled(engines),
            capabilities=report,
        )
        stage_outcomes = run_every_stage(host, clip_path=temp_dir / "clip_a.mp4")

    for engine_id, (required, optional) in declared.items():
        result = result_for(stage_outcomes, engine_id)
        first_missing = next(
            (cap for cap in required if not availability.get(cap, False)), None
        )
        degradation = [
            entry for entry in result.markers
            if entry.startswith(f"engine:{engine_id}:degraded:")
        ]

        if first_missing is not None:
            assert result.status is Engine_Status.DEGRADED                       # 7.1
            assert result.markers == (
                f"engine:{engine_id}:unavailable:{first_missing}",
            )
            assert engines[engine_id].run_count == 0        # body never entered (7.1)
            assert degradation == []
        else:
            assert engines[engine_id].run_count == 1
            missing_optional = [
                cap for cap in optional if not availability.get(cap, False)
            ]
            if missing_optional:
                assert degradation == [
                    f"engine:{engine_id}:degraded:{missing_optional[0]}"
                ]                                                               # 7.2
            else:
                assert degradation == []
            assert len(degradation) <= 1               # one per engine per clip (7.4)


# --------------------------------------------------------------------------- #
# Task 9.10 — Property 14                                                      #
# --------------------------------------------------------------------------- #
#: Exception types an engine realistically raises, including the ffmpeg wrapper's own.
_ENGINE_EXCEPTION_TYPES: Tuple[type, ...] = (
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    OSError,
    TimeoutError,
    MemoryError,
    fu.FFmpegError,
)


# Feature: av-engines-foundation, Property 14: One engine's failure is isolated — for any
# subset of RaisingEngines (any exception type, including fu.FFmpegError), each yields
# `failed` with exactly one `engine:<id>:failed` marker and every remaining engine of
# that stage is still invoked in registry order.
@FS_SETTINGS
@given(registrations=st_registrations(min_size=2, max_size=5), data=st.data())
def test_p14_one_engine_failure_is_isolated(registrations, data):
    """Validates: Requirements 8.1, 8.2, 8.4"""
    reset_engine_globals()

    raising: Dict[str, BaseException] = {}
    engines: Dict[str, Any] = {}
    for engine_id, stage, priority in registrations:
        if data.draw(st.booleans(), label=f"raises:{engine_id}"):
            exc_type = data.draw(
                st.sampled_from(list(_ENGINE_EXCEPTION_TYPES)), label=f"exc:{engine_id}"
            )
            raising[engine_id] = exc_type("engine exploded")
            engines[engine_id] = RaisingEngine(
                engine_id, stage, raising[engine_id], priority=priority
            )
        else:
            engines[engine_id] = FakeEngine(engine_id, stage, priority=priority)

    with tempfile.TemporaryDirectory(prefix="engine-host-") as raw_temp:
        temp_dir = Path(raw_temp)
        registry = registry_of(engines.values())
        host = build_host(temp_dir, registry, all_enabled(engines))
        stage_outcomes = run_every_stage(host, clip_path=temp_dir / "clip_a.mp4")

    for stage, outcome in stage_outcomes.items():
        order = [engine.engine_id for engine in registry.for_stage(stage)]
        # Every engine of the stage was still invoked, in registry order (8.2).
        assert [result.engine_id for result in outcome.results] == order
        for engine_id in order:
            assert engines[engine_id].run_count == 1
            result = outcome.result_for(engine_id)
            if engine_id in raising:
                assert result.status is Engine_Status.FAILED                     # 8.1
                assert result.markers == (f"engine:{engine_id}:failed",)         # 8.4
                assert result.contribution is None and result.artifacts == ()
            else:
                assert result.status is Engine_Status.APPLIED
        # The failed engines contribute exactly one marker each, no more.
        failed_markers = [
            entry for entry in outcome.markers if entry.endswith(":failed")
        ]
        assert sorted(failed_markers) == sorted(
            f"engine:{engine_id}:failed" for engine_id in order if engine_id in raising
        )


# --------------------------------------------------------------------------- #
# Task 9.11 — Property 15                                                      #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 15: Time budgets are enforced and abandoned
# cleanly — for any declared time_budget_s and any SlowEngine overrunning it under
# FakeClock, the result carries exactly one `engine:<id>:timeout` marker, no contribution
# or artifact from that engine is applied or persisted, and the clip still completes.
@FS_SETTINGS
@given(
    budget=st.floats(min_value=0.05, max_value=5.0, allow_nan=False, allow_infinity=False),
    overrun=st.floats(min_value=0.01, max_value=10.0, allow_nan=False, allow_infinity=False),
)
def test_p15_time_budgets_are_enforced_and_abandoned_cleanly(budget, overrun):
    """Validates: Requirements 8.6, 19.1"""
    reset_engine_globals()

    with tempfile.TemporaryDirectory(prefix="engine-host-") as raw_temp:
        temp_dir = Path(raw_temp)
        clock = FakeClock()

        # The overrunning engine would have reported work, media and a marker.
        slow_media = temp_dir / "slow_replacement.mp4"
        slow_media.write_bytes(b"slow-media")
        slow = SlowEngine(
            "slow_engine",
            Engine_Stage.AUDIO,
            overrun,
            time_budget_s=budget,
            markers=("did_work",),
            priority=1,
        )
        slow.produces_media = True

        survivor_path = temp_dir / "survivor.bin"
        survivor_path.write_bytes(b"survivor-bytes")
        survivor = Engine_Artifact(
            name="survivor.bin", path=survivor_path, media_type="data", durable=True
        )
        fast = FakeEngine(
            "fast_engine",
            Engine_Stage.AUDIO,
            artifacts=(survivor,),
            contribution=True,
            priority=2,
        )

        storage = RecordingStorage()
        host = build_host(
            temp_dir,
            registry_of([slow, fast]),
            all_enabled(["slow_engine", "fast_engine"]),
            clock=clock,
            storage=storage,
        )
        outcome = host.run_stage(
            Engine_Stage.AUDIO,
            clip_id="clip_a",
            source="/media/source.mp4",
            clip_path=temp_dir / "clip_a.mp4",
            clip_start=0.0,
            clip_end=6.0,
            duration=6.0,
        )
        extra = host.finish_clip("clip_a")

        timed_out = outcome.result_for("slow_engine")
        assert timed_out is not None
        assert timed_out.status is Engine_Status.FAILED                          # 8.6
        assert timed_out.markers == ("engine:slow_engine:timeout",)              # 8.6
        # Contribution, artifacts and media are all abandoned (8.6, 19.1).
        assert timed_out.contribution is None
        assert timed_out.artifacts == ()
        assert timed_out.media is None
        assert outcome.media is None
        assert "engine:slow_engine:did_work" not in outcome.markers

        # The clip still completes: the next engine ran, contributed and persisted.
        assert fast.run_count == 1
        assert [item.engine_id for item in outcome.contributions] == ["fast_engine"]
        assert extra == []
        assert storage.saved_keys == [
            artifact_key(JOB_ID, "clip_a", "fast_engine", "survivor.bin")
        ]
        assert not any("slow_engine" in key for key in storage.saved_keys)


# --------------------------------------------------------------------------- #
# Task 9.12 — Property 23                                                      #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 23: Every engine of a clip shares one
# Time_Base and adds no probe — all recorded ctx.time_base values for a clip are equal
# (and the same object) and the ffprobe spy count added by the host is zero.
@FS_SETTINGS
@given(
    registrations=st_registrations(min_size=1, max_size=4),
    clip_count=st.integers(min_value=1, max_value=4),
    base=st_time_base(),
)
def test_p23_every_engine_shares_one_time_base_and_adds_no_probe(
    registrations, clip_count, base
):
    """Validates: Requirements 13.7, 19.4"""
    reset_engine_globals()

    probes: List[Any] = []

    def refuse_probe(path, *args, **kwargs):
        probes.append(path)
        raise AssertionError("the host must not add an ffprobe pass")

    engines = {
        engine_id: FakeEngine(engine_id, stage, priority=priority)
        for engine_id, stage, priority in registrations
    }

    with mock.patch.object(fu, "probe", refuse_probe), tempfile.TemporaryDirectory(
        prefix="engine-host-"
    ) as raw_temp:
        temp_dir = Path(raw_temp)
        host = build_host(temp_dir, registry_of(engines.values()), all_enabled(engines))

        # The Pipeline has already probed; the host only reads the result (19.4).
        host.run_source("/media/source.mp4", media_info(fps=base.fps))
        shared = host.time_base()
        for index in range(clip_count):
            run_every_stage(
                host,
                clip_id=f"clip_{index}",
                clip_path=temp_dir / f"clip_{index}.mp4",
            )

    recorded = [ctx.time_base for engine in engines.values() for ctx in engine.contexts]
    assert recorded, "at least one enabled engine must have been invoked"
    assert all(item is shared for item in recorded)                  # 13.7 — one object
    assert all(item == shared for item in recorded)
    assert probes == []                                              # 19.4 — no probe
    assert shared.fps == pytest.approx(base.fps)
    assert shared.fps_substituted is False


# --------------------------------------------------------------------------- #
# Task 9.13 — Property 27                                                      #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 27: The rebased Word_Timeline reaches every
# subsequent engine — for any Word_Timeline and any filler keep-plan, the words recorded
# by every engine invoked after filler removal equal filler.rebase_words(words, keeps)
# and every word bound lies within [0, ctx.duration].
@FS_SETTINGS
@given(timeline=st_word_timeline(), data=st.data())
def test_p27_rebased_word_timeline_reaches_every_engine(timeline, data):
    """Validates: Requirements 15.1, 15.2"""
    reset_engine_globals()

    words, duration = timeline
    # A keep-plan carves a removed region out of the middle of the clip, exactly as
    # ``filler.plan_keep_intervals`` does; the bounds are drawn, then sorted.
    cuts = sorted(
        data.draw(
            st.lists(
                st.floats(min_value=0.0, max_value=duration,
                          allow_nan=False, allow_infinity=False),
                min_size=2,
                max_size=2,
            ),
            label="cuts",
        )
    )
    keeps = [
        interval
        for interval in (
            filler.Interval(0.0, cuts[0]),
            filler.Interval(cuts[1], duration),
        )
        if interval.duration > 0.0
    ]
    rebased = filler.rebase_words(words, keeps)
    new_duration = sum(interval.duration for interval in keeps)

    engines = {
        "audio_engine": FakeEngine("audio_engine", Engine_Stage.AUDIO),
        "geometry_engine": FakeEngine("geometry_engine", Engine_Stage.GEOMETRY),
        "compose_engine": FakeEngine("compose_engine", Engine_Stage.COMPOSE),
    }

    with tempfile.TemporaryDirectory(prefix="engine-host-") as raw_temp:
        temp_dir = Path(raw_temp)
        host = build_host(temp_dir, registry_of(engines.values()), all_enabled(engines))
        run_every_stage(
            host,
            clip_path=temp_dir / "clip_a.mp4",
            duration=new_duration,
            words=tuple(rebased),
        )

    for engine in engines.values():
        assert engine.run_count == 1
        ctx = engine.last_context
        # The very objects the Pipeline rebased reach every engine (15.2).
        assert len(ctx.words) == len(rebased)
        assert all(seen is expected for seen, expected in zip(ctx.words, rebased))
        # Bounds are clip-relative, inside [0, ctx.duration] (15.1). ``rebase_words``
        # rounds to milliseconds, so the upper bound carries that tolerance.
        for word in ctx.words:
            assert word.start >= 0.0
            assert word.start <= word.end
            assert word.end <= ctx.duration + 0.005
        assert ctx.duration == pytest.approx(new_duration)


# --------------------------------------------------------------------------- #
# Task 9.14 — Property 28                                                      #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 28: Independent engines are confluent — for
# any two engines whose contributions occupy disjoint time ranges, running them in either
# relative priority order yields equal merged marker sets and equal produced-artifact key
# sets.
@FS_SETTINGS
@given(
    bounds=st.lists(
        st.floats(min_value=0.0, max_value=30.0, allow_nan=False, allow_infinity=False),
        min_size=4,
        max_size=4,
    )
)
def test_p28_independent_engines_are_confluent(bounds):
    """Validates: Requirements 15.6"""
    first_start, first_end, second_start, second_end = sorted(bounds)

    def drive(priority_one: int, priority_two: int) -> Tuple[set, set]:
        """Run both engines at the given relative priorities; return markers and keys."""
        reset_engine_globals()
        with tempfile.TemporaryDirectory(prefix="engine-host-") as raw_temp:
            temp_dir = Path(raw_temp)
            engines = []
            for engine_id, (start, end), priority in (
                ("engine_one", (first_start, first_end), priority_one),
                ("engine_two", (second_start, second_end), priority_two),
            ):
                path = temp_dir / f"{engine_id}.bin"
                path.write_bytes(engine_id.encode())
                engines.append(
                    FakeEngine(
                        engine_id,
                        Engine_Stage.COMPOSE,
                        priority=priority,
                        markers=("planned",),
                        artifacts=(
                            Engine_Artifact(
                                name=f"{engine_id}.bin",
                                path=path,
                                media_type="data",
                                durable=True,
                            ),
                        ),
                        contribution=Compose_Contribution(
                            engine_id=engine_id,
                            video_filters=(f"trim=start={start}:end={end}",),
                            z_order=priority,
                        ),
                    )
                )
            storage = RecordingStorage()
            host = build_host(
                temp_dir,
                registry_of(engines),
                all_enabled(engine.engine_id for engine in engines),
                storage=storage,
            )
            outcome = host.run_stage(
                Engine_Stage.COMPOSE,
                clip_id="clip_a",
                source="/media/source.mp4",
                clip_path=temp_dir / "clip_a.mp4",
                clip_start=0.0,
                clip_end=30.0,
                duration=30.0,
            )
            markers = set(outcome.markers) | set(host.finish_clip("clip_a"))
            return markers, set(storage.saved_keys)

    forward_markers, forward_keys = drive(1, 2)
    reversed_markers, reversed_keys = drive(2, 1)

    assert forward_markers == reversed_markers                       # 15.6
    assert forward_keys == reversed_keys                             # 15.6
    assert forward_keys == {
        artifact_key(JOB_ID, "clip_a", "engine_one", "engine_one.bin"),
        artifact_key(JOB_ID, "clip_a", "engine_two", "engine_two.bin"),
    }


# --------------------------------------------------------------------------- #
# Task 9.15 — Property 33                                                      #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 33: Permissibility blocks network engines and
# keeps runs offline — with permissibility_mode on, no engine declaring requires_network
# executes its run body, each yields `degraded` with exactly one
# `engine:<id>:permissibility_blocked` marker, resolved options equal the documented safe
# values, and a clip of purely local engines completes with socket.socket patched to
# raise.
@FS_SETTINGS
@given(registrations=st_registrations(min_size=1, max_size=4), data=st.data())
def test_p33_permissibility_blocks_network_engines_and_keeps_runs_offline(
    registrations, data
):
    """Validates: Requirements 9.5, 21.2, 21.3, 21.4"""
    reset_engine_globals()

    network = {
        engine_id: data.draw(st.booleans(), label=f"network:{engine_id}")
        for engine_id, _stage, _priority in registrations
    }
    engines = {
        engine_id: FakeEngine(
            engine_id, stage, priority=priority, requires_network=network[engine_id]
        )
        for engine_id, stage, priority in registrations
    }

    def refuse_socket(*args: Any, **kwargs: Any):
        raise AssertionError("a permissibility run must perform no network access")

    options = all_enabled(engines, permissibility=True)

    with mock.patch.object(socket, "socket", refuse_socket), tempfile.TemporaryDirectory(
        prefix="engine-host-"
    ) as raw_temp:
        temp_dir = Path(raw_temp)
        host = build_host(temp_dir, registry_of(engines.values()), options)
        stage_outcomes = run_every_stage(host, clip_path=temp_dir / "clip_a.mp4")
        leaves = workspace_leaf_names(temp_dir)

    for engine_id, needs_network in network.items():
        engine = engines[engine_id]
        result = result_for(stage_outcomes, engine_id)
        if needs_network:
            assert engine.run_count == 0                     # body never entered (21.3)
            assert result.status is Engine_Status.DEGRADED               # 21.2
            assert result.markers == (
                f"engine:{engine_id}:permissibility_blocked",
            )
            # Blocked before any workspace or capability probe exists (21.3).
            assert not any(name.startswith(f"{engine_id}__") for name in leaves)
        else:
            # A purely local engine completes with sockets refused (21.4).
            assert engine.run_count == 1
            assert result.status is Engine_Status.APPLIED
            ctx = engine.last_context
            assert ctx.permissibility is True
            # ``resolve_options`` is a pure pass-through for the double, so the
            # resolved options are exactly the already effective_options-normalised
            # (permissibility-downgraded) mapping the host was handed — the
            # documented safe values (Reqs 9.5, 4.4).
            assert ctx.options is options
            assert engine.resolve_calls == [options]


# --------------------------------------------------------------------------- #
# Task 9.16 — unit examples: failure logging, media fallback, lifecycle        #
# --------------------------------------------------------------------------- #
class Stub_Runtime_Config:
    """Minimal ``RuntimeConfig`` stand-in exposing just ``auto_delete_temp``."""

    def __init__(self, auto_delete_temp: bool) -> None:
        self.auto_delete_temp = auto_delete_temp


def test_failed_engine_logs_exception_class_and_message(tmp_path, caplog):
    """Validates: Requirements 8.5 — the caught exception's class and message are logged."""
    engine = RaisingEngine("boom_engine", Engine_Stage.POST, ValueError("kaboom detail"))
    host = build_host(tmp_path, registry_of([engine]), all_enabled(["boom_engine"]))

    with caplog.at_level(logging.WARNING, logger="worker.engines.host"):
        outcome = host.run_stage(
            Engine_Stage.POST,
            clip_id="clip_a",
            source="/media/source.mp4",
            clip_path=tmp_path / "clip_a.mp4",
            clip_start=0.0,
            clip_end=3.0,
            duration=3.0,
        )

    assert outcome.result_for("boom_engine").status is Engine_Status.FAILED
    assert "boom_engine" in caplog.text
    assert "ValueError" in caplog.text                                   # 8.5 — class
    assert "kaboom detail" in caplog.text                                # 8.5 — message


@pytest.mark.parametrize(
    ("status", "has_media", "expects_media"),
    [
        # Media-bearing statuses: the file is adopted.
        (Engine_Status.APPLIED, True, True),
        (Engine_Status.DEGRADED, True, True),      # Degraded_With_Media
        # No file to adopt, whatever the status.
        (Engine_Status.APPLIED, False, False),
        (Engine_Status.DEGRADED, False, False),    # Degraded_Without_Media
        # Not media-bearing: the file is discarded even though it exists.
        (Engine_Status.FAILED, True, False),
        (Engine_Status.SKIPPED, True, False),
    ],
)
def test_stage_media_is_adopted_only_from_a_media_bearing_produces_media_engine(
    tmp_path, status, has_media, expects_media
):
    """Validates: Requirements 8.3 — and the ``Degraded_With_Media`` widening.

    The gate admits ``applied`` **and** ``degraded``, because degradation describes
    fidelity rather than usability: an engine that fell back to a cheaper path and still
    produced a usable file has produced usable output, and discarding it would throw the
    work away while still charging the clip for the passes that made it.

    Req 8.3 is unchanged and still holds, because it is carried by ``media is None``
    rather than by the status — a rung with nothing to hand back returns no media, and the
    Pipeline's ``out.media or raw`` then keeps the pre-stage file. ``failed`` stays
    excluded: an engine that failed cannot vouch for what it produced.
    """
    replacement = tmp_path / "replacement.wav"
    replacement.write_bytes(b"replacement-audio")
    engine = FakeEngine(
        "media_engine",
        Engine_Stage.AUDIO,
        status=status,
        media=replacement if has_media else None,
    )
    # ``FakeEngine`` derives ``produces_media`` from whether it was given a file, but the
    # declaration and the file are independent in the contract: an engine may declare it and
    # still return nothing. Pin it on so the no-file rows exercise the gate rather than the
    # declaration.
    engine.produces_media = True

    host = build_host(tmp_path, registry_of([engine]), all_enabled(["media_engine"]))
    outcome = host.run_stage(
        Engine_Stage.AUDIO,
        clip_id="clip_a",
        source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=3.0,
        duration=3.0,
    )

    if expects_media:
        assert outcome.media == replacement
    else:
        # ``out.media or raw`` in the Pipeline therefore keeps the pre-stage media.
        assert outcome.media is None


def test_caller_notes_are_appended_after_the_hosts_own(tmp_path):
    """Validates: the additive ``run_stage(notes=...)`` keyword.

    The host can only synthesise notes it can derive (``fps_fallback:`` from the probe,
    ``filler_seam:`` from the keep plan); ``notes`` is how a caller publishes one it cannot.
    Order is asserted because it is part of the contract — an engine reading a prefix by
    position must keep working when a caller starts passing notes.
    """
    engine = FakeEngine("note_reader", Engine_Stage.AUDIO)
    host = build_host(tmp_path, registry_of([engine]), all_enabled(["note_reader"]))

    host.run_stage(
        Engine_Stage.AUDIO,
        clip_id="clip_a",
        source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=3.0,
        duration=3.0,
        notes=("diarization:model", 17),
    )

    seen = engine.contexts[-1].notes
    assert seen[-2:] == ("diarization:model", "17")     # coerced to str
    assert all(isinstance(note, str) for note in seen)


def test_omitting_caller_notes_changes_no_context(tmp_path):
    """Validates: Requirements 23.1 — the ``notes`` keyword is strictly additive.

    The parity guarantee for the new keyword: a call that omits it must build exactly the
    context it built before the keyword existed.
    """
    without = FakeEngine("a", Engine_Stage.AUDIO)
    host_a = build_host(tmp_path, registry_of([without]), all_enabled(["a"]))
    host_a.run_stage(
        Engine_Stage.AUDIO, clip_id="c", source="/s.mp4",
        clip_path=tmp_path / "c.mp4", clip_start=0.0, clip_end=1.0, duration=1.0,
    )

    explicit = FakeEngine("a", Engine_Stage.AUDIO)
    host_b = build_host(tmp_path, registry_of([explicit]), all_enabled(["a"]))
    host_b.run_stage(
        Engine_Stage.AUDIO, clip_id="c", source="/s.mp4",
        clip_path=tmp_path / "c.mp4", clip_start=0.0, clip_end=1.0, duration=1.0,
        notes=(),
    )

    # Equal to each other, and carrying nothing the caller contributed — the host's own
    # synthesised notes (here ``fps_fallback:``) are unchanged in content and position.
    assert without.contexts[-1].notes == explicit.contexts[-1].notes
    assert not any(note.startswith("diarization:") for note in without.contexts[-1].notes)


def test_a_degraded_engines_artifacts_and_media_are_both_kept(tmp_path):
    """Validates: Requirements 3.10 — ``degraded`` is a first-class outcome throughout.

    Before the gate was widened, a degraded engine's artifacts and Compose_Contribution
    were collected but its **media** was silently dropped — an asymmetry with no
    justification, and the reason the audio-stem engine's ``Degraded_With_Media`` rungs
    could not work. This pins that the three now travel together.
    """
    replacement = tmp_path / "degraded.wav"
    replacement.write_bytes(b"usable-but-degraded")
    engine = FakeEngine(
        "partial",
        Engine_Stage.AUDIO,
        status=Engine_Status.DEGRADED,
        media=replacement,
        markers=("engine:partial:degraded:model:htdemucs",),
    )

    host = build_host(tmp_path, registry_of([engine]), all_enabled(["partial"]))
    outcome = host.run_stage(
        Engine_Stage.AUDIO, clip_id="clip_a", source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4", clip_start=0.0, clip_end=3.0, duration=3.0,
    )

    assert outcome.media == replacement
    assert "engine:partial:degraded:model:htdemucs" in outcome.markers


def test_finish_job_persists_source_stage_durable_artifacts(tmp_path, monkeypatch):
    """Validates: Requirements 17.7, 18.1 — the SOURCE pseudo-clip is finalised too.

    SOURCE-stage engines run once per source under the ``SOURCE_CLIP_ID`` pseudo-clip
    that no per-clip ``finish_clip(clip_id)`` call ever names, so ``finish_job`` is the
    only place their durable artifacts can be persisted before the job scratch space
    goes away.
    """
    monkeypatch.setattr(
        "runtime_config.get_runtime_config", lambda: Stub_Runtime_Config(False)
    )
    artifact_path = tmp_path / "source_analysis.json"
    artifact_path.write_bytes(b"{}")
    engine = FakeEngine(
        "source_analyser",
        Engine_Stage.SOURCE,
        artifacts=(
            Engine_Artifact(
                name="source_analysis.json",
                path=artifact_path,
                media_type="data",
                durable=True,
            ),
        ),
    )
    storage = RecordingStorage()
    host = build_host(
        tmp_path,
        registry_of([engine]),
        all_enabled(["source_analyser"]),
        storage=storage,
    )

    host.run_source("/media/source.mp4", media_info())
    source_workspaces = workspace_leaf_names(tmp_path)
    assert any(name.startswith("source_analyser__") for name in source_workspaces)

    markers = host.finish_job()

    assert markers == []
    assert storage.saved_keys == [
        artifact_key(JOB_ID, SOURCE_CLIP_ID, "source_analyser", "source_analysis.json")
    ]
    # The source workspace is gone even though job-level retention is disabled.
    assert workspace_leaf_names(tmp_path) == []


def test_finish_clip_deletes_workspaces_regardless_of_auto_delete_temp(
    tmp_path, monkeypatch
):
    """Validates: Requirements 17.1, 17.5 — per-clip deletion carries no condition.

    ``auto_delete_temp`` governs the *job-level* scratch space (Reqs 17.2, 17.3, 17.6),
    which is why ``artifacts.cleanup_workspace`` is unconditional and
    ``artifacts.cleanup_job_artifacts`` is gated; ``finish_clip`` must follow the former.
    """
    monkeypatch.setattr(
        "runtime_config.get_runtime_config", lambda: Stub_Runtime_Config(False)
    )
    engines = [
        FakeEngine("applied_engine", Engine_Stage.POST, status=Engine_Status.APPLIED),
        FakeEngine("degraded_engine", Engine_Stage.POST, status=Engine_Status.DEGRADED),
        RaisingEngine("failed_engine", Engine_Stage.POST, RuntimeError("nope")),
    ]
    host = build_host(
        tmp_path,
        registry_of(engines),
        all_enabled(engine.engine_id for engine in engines),
    )
    host.run_stage(
        Engine_Stage.POST,
        clip_id="clip_a",
        source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=3.0,
        duration=3.0,
    )
    assert len(workspace_leaf_names(tmp_path)) == 3

    assert host.finish_clip("clip_a") == []

    assert workspace_leaf_names(tmp_path) == []



def test_compose_engines_receive_a_reserved_ffmpeg_input_block(tmp_path):
    """Validates: Requirements 1.5, 10.3 — ``first_input_index`` reservation rules.

    The compositor lays the extra ffmpeg inputs out as one contiguous block
    starting immediately after the primary clip (index 0), so for the enabled
    COMPOSE engines of a stage run, in registry ``(priority, engine_id)`` order,
    ``first_input_index == 1 + sum(max_inputs of the preceding engines)``. An
    engine declaring ``max_inputs == 0`` consumes no index space and is given the
    documented meaningless ``0``, and no other stage reserves anything.
    """
    contributing = FakeEngine("b_two_inputs", Engine_Stage.COMPOSE, priority=10,
                              max_inputs=2)
    quiet = FakeEngine("c_no_inputs", Engine_Stage.COMPOSE, priority=20)
    trailing = FakeEngine("d_one_input", Engine_Stage.COMPOSE, priority=30,
                          max_inputs=1)
    disabled = FakeEngine("a_disabled", Engine_Stage.COMPOSE, priority=5,
                          max_inputs=5)
    post = FakeEngine("e_post", Engine_Stage.POST, max_inputs=3)
    engines = [contributing, quiet, trailing, disabled, post]

    options = options_for({
        "b_two_inputs": True, "c_no_inputs": True, "d_one_input": True,
        "e_post": True, "a_disabled": False,
    })
    host = build_host(
        tmp_path, registry_of(engines), options,
        capabilities=Capability_Report(StaticProber({})), clock=FakeClock(),
    )
    for stage in (Engine_Stage.COMPOSE, Engine_Stage.POST):
        host.run_stage(
            stage, clip_id="clip_a", source="/media/source.mp4",
            clip_path=tmp_path / "clip_a.mp4", clip_start=0.0, clip_end=6.0,
            duration=6.0,
        )

    # The block starts: 1 for the first contributing engine, then + its size. The
    # engine declaring no input neither consumes space nor gets a real index.
    assert contributing.last_context.first_input_index == 1
    assert quiet.last_context.first_input_index == 0
    assert trailing.last_context.first_input_index == 3
    # A disabled engine is skipped before its body is entered, so it reserves
    # nothing and never sees a context at all.
    assert disabled.run_count == 0
    # Only COMPOSE contributes inputs; every other stage keeps the default 0.
    assert post.last_context.first_input_index == 0

    host.finish_clip("clip_a")


# --------------------------------------------------------------------------- #
# Task 15.3 — Clip_Metadata reaches every engine unchanged (Req 15.8)          #
# --------------------------------------------------------------------------- #
#: Hostile-ish Clip_Metadata: the two documented keys plus unknown keys carrying
#: values a filtering/coercing implementation would visibly damage (a tuple that
#: must not become a list, a numeric *string* that must not become an int, ``None``
#: that must not be defaulted away, and a nested structure).
CLIP_METADATA_PAYLOAD: Dict[str, Any] = {
    "hook_text": "Wait for it...",
    "clip_size": (1080, 1920),
    "unknown_key": {"nested": [1, None, "2"]},
    "numeric_string": "12",
    "empty": None,
    "": "blank key survives",
}


def run_compose_stage(
    host: Engine_Host,
    tmp_path: Path,
    *,
    clip_id: str = "clip_a",
    **kwargs: Any,
):
    """Invoke the COMPOSE stage once for one clip, forwarding ``**kwargs`` verbatim.

    ``clip_metadata`` is deliberately *not* given a default here: a call that omits
    it must reach ``run_stage`` without the keyword at all, which is exactly what
    the "omitted yields the empty default" assertion below needs.
    """
    return host.run_stage(
        Engine_Stage.COMPOSE,
        clip_id=clip_id,
        source="/media/source.mp4",
        clip_path=tmp_path / f"{clip_id}.mp4",
        clip_start=0.0,
        clip_end=6.0,
        duration=6.0,
        **kwargs,
    )


def compose_host(tmp_path: Path, engines: List[Any]) -> Engine_Host:
    """A host over ``engines`` with every collaborator injected and a frozen clock.

    The clock never advances on its own, so two stage runs of the same host build
    contexts with identical ``deadline`` values — which is what makes the
    field-by-field "otherwise unchanged" comparison below meaningful.
    """
    return build_host(
        tmp_path,
        registry_of(engines),
        all_enabled(engine.engine_id for engine in engines),
        capabilities=Capability_Report(StaticProber({})),
        clock=FakeClock(),
    )


def test_clip_metadata_reaches_every_engine_of_the_stage_run(tmp_path):
    """Validates: Requirements 15.8 — *every* engine sees exactly the supplied mapping.

    The regression this guards is "merged into the first context only": three
    engines are registered on the same stage, so a per-stage-run mapping that is
    threaded through only the first ``Engine_Context`` fails here.
    """
    engines = [
        FakeEngine("a_first", Engine_Stage.COMPOSE, priority=10),
        FakeEngine("b_second", Engine_Stage.COMPOSE, priority=20),
        FakeEngine("c_third", Engine_Stage.COMPOSE, priority=30),
    ]
    host = compose_host(tmp_path, engines)

    run_compose_stage(host, tmp_path, clip_metadata=CLIP_METADATA_PAYLOAD)

    for engine in engines:
        assert engine.run_count == 1, engine.engine_id
        seen = engine.last_context.clip_metadata
        assert dict(seen) == CLIP_METADATA_PAYLOAD, engine.engine_id
        # Same content, not the caller's object: no engine can reach the mapping
        # the Pipeline still holds.
        assert seen is not CLIP_METADATA_PAYLOAD

    host.finish_clip("clip_a")


def test_omitted_clip_metadata_is_empty_and_leaves_the_context_otherwise_unchanged(
    tmp_path,
):
    """Validates: Requirements 15.8, 23.1 — the default is inert.

    Omitting the keyword must yield the documented empty mapping *and* a context
    that is field-by-field identical to the one built when Clip_Metadata is
    supplied — i.e. the seam adds a field and changes nothing else, which is the
    pre-change context the all-off parity gate (13.1/13.2) still measures.
    """
    engine = FakeEngine("metadata_probe", Engine_Stage.COMPOSE)
    host = compose_host(tmp_path, [engine])

    run_compose_stage(host, tmp_path)                                    # omitted
    run_compose_stage(host, tmp_path, clip_metadata=CLIP_METADATA_PAYLOAD)
    run_compose_stage(host, tmp_path, clip_metadata=None)                # explicit None

    omitted, supplied, explicit_none = engine.contexts
    assert omitted.clip_metadata == {}
    assert explicit_none.clip_metadata == {}                             # None == empty
    assert dict(supplied.clip_metadata) == CLIP_METADATA_PAYLOAD

    # Every OTHER field is untouched by the presence or absence of Clip_Metadata.
    for field_ in dataclasses.fields(omitted):
        if field_.name == "clip_metadata":
            continue
        assert getattr(omitted, field_.name) == getattr(supplied, field_.name), field_.name
        assert getattr(omitted, field_.name) == getattr(explicit_none, field_.name), (
            field_.name
        )

    host.finish_clip("clip_a")


def test_clip_metadata_unknown_keys_pass_through_untouched(tmp_path):
    """Validates: Requirements 15.8 — no filtering, coercion, renaming or defaulting.

    Value *identity* is asserted, not just equality: a tuple stays the same tuple
    (never a list), a numeric string stays a string, ``None`` survives, and a key
    the host has never heard of arrives under its own name.
    """
    engine = FakeEngine("passthrough_probe", Engine_Stage.COMPOSE)
    host = compose_host(tmp_path, [engine])

    run_compose_stage(host, tmp_path, clip_metadata=CLIP_METADATA_PAYLOAD)

    seen = engine.last_context.clip_metadata
    assert set(seen) == set(CLIP_METADATA_PAYLOAD)                       # no key dropped
    for key, value in CLIP_METADATA_PAYLOAD.items():
        assert seen[key] is value, key                                   # no coercion
    assert isinstance(seen["clip_size"], tuple)
    assert seen["numeric_string"] == "12" and isinstance(seen["numeric_string"], str)
    assert seen["empty"] is None

    host.finish_clip("clip_a")


def test_clip_metadata_is_read_only_from_the_engine_point_of_view(tmp_path):
    """Validates: Requirements 15.8 — an engine cannot rebind or reach out through it.

    Three separate boundaries: the field cannot be rebound (frozen dataclass); the
    caller's mapping is not the engine's mapping; and no two engines of one stage
    run share a mapping, so one engine writing into its own copy cannot be observed
    by the caller or by a sibling engine. The copy is shallow by design — a nested
    mutable value is still shared — which is the documented cost of a read-only
    planning channel and is why the assertions below use top-level keys.
    """
    first = FakeEngine("a_reader", Engine_Stage.COMPOSE, priority=10)
    second = FakeEngine("b_reader", Engine_Stage.COMPOSE, priority=20)
    host = compose_host(tmp_path, [first, second])
    supplied = dict(CLIP_METADATA_PAYLOAD)

    run_compose_stage(host, tmp_path, clip_metadata=supplied)

    first_ctx, second_ctx = first.last_context, second.last_context

    # 1 — the field cannot be rebound (Req 1.3: the context is frozen).
    with pytest.raises(dataclasses.FrozenInstanceError):
        first_ctx.clip_metadata = {"hook_text": "hijacked"}

    # 2/3 — three distinct mappings: caller, engine A, engine B.
    assert first_ctx.clip_metadata is not supplied
    assert second_ctx.clip_metadata is not supplied
    assert first_ctx.clip_metadata is not second_ctx.clip_metadata

    # Whatever an engine writes into its own copy stays there.
    first_ctx.clip_metadata["hook_text"] = "hijacked"
    first_ctx.clip_metadata["injected"] = True
    assert supplied == CLIP_METADATA_PAYLOAD
    assert dict(second_ctx.clip_metadata) == CLIP_METADATA_PAYLOAD

    host.finish_clip("clip_a")
