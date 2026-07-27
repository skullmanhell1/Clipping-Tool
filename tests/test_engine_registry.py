"""Engine_Registry property module for the av-engines-foundation spec
(``worker/engines/registry.py``).

Covers the design's numbered properties for discovery and deterministic ordering:

* **P3** — registry order is independent of registration order (task 5.2).
* **P4** — stage lookup partitions the registry, and lookup round-trips (task 5.3).
* **P5** — duplicate Engine_Id registration is a named error (task 5.4).
* **P6** — reset empties a registry and instances stay isolated (task 5.5).

Generators come from the shared ``tests/strategies.py`` module and the registered
engines are ``tests.fakes.FakeEngine`` doubles — never redefined here — so the
sibling engine specs exercise the same input space and the same doubles.

Global state: every property builds its own ``Engine_Registry()`` (Req 22.2), and
the module-level default registry is cleared by the autouse ``clean_default_registry``
fixture before *and* after each test, so test order can never matter.

Everything here is pure and offline: no ffmpeg, no probe, no filesystem.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes import FakeEngine
from tests.strategies import st_engine_id, st_registrations, st_stage
from worker.engines.base import Engine_Stage
from worker.engines.registry import (
    Engine_Registration_Error,
    Engine_Registry,
    get_registry,
    register,
    reset_registry,
)

#: Every stage a registry can be asked about, so "for every stage" assertions are
#: exhaustive rather than sampled.
ALL_STAGES: Tuple[Engine_Stage, ...] = tuple(Engine_Stage)


@pytest.fixture(autouse=True)
def clean_default_registry():
    """Leave the module-level default registry empty before and after each test.

    P5 and P6 touch the process-wide default deliberately; this fixture makes that
    safe by guaranteeing a known-empty starting state and restoring it afterwards.
    """
    reset_registry()
    yield
    reset_registry()


def _engines_for(
    registrations: List[Tuple[str, Engine_Stage, int]]
) -> Dict[str, FakeEngine]:
    """One :class:`FakeEngine` per registration triple, keyed by Engine_Id.

    A single instance per id is shared by every registry in a test, so ordering
    assertions can compare engine *identity* rather than just ids.
    """
    return {
        engine_id: FakeEngine(engine_id, stage, priority=priority)
        for engine_id, stage, priority in registrations
    }


def _expected_ids(
    registrations: List[Tuple[str, Engine_Stage, int]], stage: Engine_Stage
) -> List[str]:
    """The Engine_Ids of ``stage``, in the design's ``(priority, engine_id)`` order.

    Independent restatement of Req 2.5 — it sorts the *generated* triples rather
    than asking the registry, so it cannot inherit a bug from the implementation.
    """
    matching = [
        (priority, engine_id)
        for engine_id, declared_stage, priority in registrations
        if declared_stage is stage
    ]
    return [engine_id for _, engine_id in sorted(matching)]


# Feature: av-engines-foundation, Property 3: Registry order is independent of
# registration order — *For any* set of registrations and *for any* permutation of
# it, `for_stage(stage)` returns the same sequence, equal to the registrations
# sorted by `(priority, engine_id)`.
@settings(max_examples=100, deadline=None)
@given(registrations=st_registrations(), data=st.data())
def test_p3_registry_order_is_independent_of_registration_order(registrations, data):
    """Validates: Requirements 2.5

    Two isolated registries hold the *same* engine instances registered in two
    different orders (the generated order and a drawn permutation of it). For every
    stage the two ``for_stage`` sequences must be identical, and both must equal the
    independently computed ``(priority, engine_id)`` ordering.
    """
    permuted = data.draw(st.permutations(registrations))
    engines = _engines_for(registrations)

    first = Engine_Registry()
    for engine_id, _, _ in registrations:
        first.register(engines[engine_id])

    second = Engine_Registry()
    for engine_id, _, _ in permuted:
        second.register(engines[engine_id])

    for stage in ALL_STAGES:
        listed = first.for_stage(stage)
        # Permutation invariance: identical sequences, engine instance by instance.
        assert listed == second.for_stage(stage)
        # ... and the sequence is the declared (priority, engine_id) order.
        expected = _expected_ids(registrations, stage)
        assert [engine.engine_id for engine in listed] == expected
        assert listed == [engines[engine_id] for engine_id in expected]

    # The whole-registry listing carries the same guarantee.
    assert first.all() == second.all()
    assert first.records() == second.records()


# Feature: av-engines-foundation, Property 4: Stage lookup partitions the registry,
# and lookup round-trips — *For any* set of registrations (including the empty set),
# every engine returned by `for_stage(s)` declares stage `s`, the union over all
# stages equals the registration set with no duplicates, and `get(engine_id)`
# returns the exact instance registered for that id.
@settings(max_examples=100, deadline=None)
@given(registrations=st_registrations(), stage=st_stage())
def test_p4_stage_lookup_partitions_registry_and_lookup_round_trips(registrations, stage):
    """Validates: Requirements 2.1, 2.2, 2.4, 2.6

    The empty registration set is inside ``st_registrations``' range, so the
    "empty registry yields ``[]`` for every stage" clause (Req 2.6) is covered by
    the same property.
    """
    engines = _engines_for(registrations)
    registry = Engine_Registry()
    for engine_id, _, _ in registrations:
        registry.register(engines[engine_id])

    # Only engines declaring the drawn stage come back (Req 2.4).
    for engine in registry.for_stage(stage):
        assert engine.stage is stage

    # The per-stage lists partition the registry: every engine appears exactly once
    # across all stages, and the union is precisely what was registered (Req 2.1).
    listed: List[FakeEngine] = []
    for candidate in ALL_STAGES:
        for engine in registry.for_stage(candidate):
            assert engine.stage is candidate
            listed.append(engine)
    assert len(listed) == len(registrations) == len(registry)
    assert len({id(engine) for engine in listed}) == len(listed)
    assert {engine.engine_id for engine in listed} == set(engines)

    # Lookup round-trips to the exact instance (Req 2.2).
    for engine_id in engines:
        assert engine_id in registry
        assert registry.get(engine_id) is engines[engine_id]
        assert registry.find(engine_id) is engines[engine_id]

    # An empty registry answers every stage with [] (Req 2.6).
    registry.reset()
    for candidate in ALL_STAGES:
        assert registry.for_stage(candidate) == []


# Feature: av-engines-foundation, Property 5: Duplicate Engine_Id registration is a
# named error — *For any* Engine_Id, registering it twice raises
# `Engine_Registration_Error` whose message contains that id, and the registry is
# unchanged (same length, same instance for the id).
@settings(max_examples=100, deadline=None)
@given(
    engine_id=st_engine_id(),
    stage=st_stage(),
    other_stage=st_stage(),
    registrations=st_registrations(allow_duplicate_ids=True),
)
def test_p5_duplicate_engine_id_registration_is_a_named_error(
    engine_id, stage, other_stage, registrations
):
    """Validates: Requirements 2.3

    Checked three ways for the same property: a bare double registration on an
    isolated registry, a registration set that already contains a duplicate id, and
    the module-level default registry (whose ``register`` shares the code path).
    """
    # The autouse fixture runs once per test *function*, while hypothesis runs many
    # examples inside it — so each example clears the shared default itself.
    reset_registry()
    registry = Engine_Registry()
    first = FakeEngine(engine_id, stage)
    registry.register(first)
    before = len(registry)

    duplicate = FakeEngine(engine_id, other_stage, priority=7)
    with pytest.raises(Engine_Registration_Error) as excinfo:
        registry.register(duplicate)
    assert engine_id in str(excinfo.value)
    # No partial mutation: same size, same instance, original stage retained.
    assert len(registry) == before
    assert registry.get(engine_id) is first
    assert registry.stage_of(engine_id) is stage
    assert duplicate not in registry.all()

    # Same guarantee while replaying a generated set that carries a duplicate id.
    replay = Engine_Registry()
    accepted: Dict[str, FakeEngine] = {}
    for candidate_id, candidate_stage, priority in registrations:
        engine = FakeEngine(candidate_id, candidate_stage, priority=priority)
        if candidate_id in accepted:
            size = len(replay)
            with pytest.raises(Engine_Registration_Error) as replay_error:
                replay.register(engine)
            assert candidate_id in str(replay_error.value)
            assert len(replay) == size
            assert replay.get(candidate_id) is accepted[candidate_id]
        else:
            replay.register(engine)
            accepted[candidate_id] = engine
    assert len(replay) == len(accepted)

    # And through the module-level default registry (cleared by the fixture).
    default_engine = FakeEngine(engine_id, stage)
    register(default_engine)
    with pytest.raises(Engine_Registration_Error) as default_error:
        register(FakeEngine(engine_id, other_stage))
    assert engine_id in str(default_error.value)
    assert len(get_registry()) == 1
    assert get_registry().get(engine_id) is default_engine


# Feature: av-engines-foundation, Property 6: Reset empties a registry and instances
# stay isolated — *For any* set of registrations, after `reset()` the registry length
# is zero and every stage list is empty; and *for any* two `Engine_Registry`
# instances, registering into one never changes the contents of the other or of the
# module-level default.
@settings(max_examples=100, deadline=None)
@given(registrations=st_registrations())
def test_p6_reset_empties_registry_and_instances_stay_isolated(registrations):
    """Validates: Requirements 2.7, 22.2"""
    # Per-example isolation of the shared default (see P5's note above).
    reset_registry()
    engines = _engines_for(registrations)

    populated = Engine_Registry()
    observer = Engine_Registry()
    default = get_registry()

    for engine_id, _, _ in registrations:
        populated.register(engines[engine_id])
        # Isolation holds after *every* registration, not just at the end.
        assert len(observer) == 0
        assert len(default) == 0

    assert len(populated) == len(registrations)

    # The sibling instance and the module-level default saw nothing.
    for stage in ALL_STAGES:
        assert observer.for_stage(stage) == []
        assert default.for_stage(stage) == []
    assert observer.all() == []
    assert default.all() == []
    assert observer.ids() == []
    assert default.ids() == []

    # Reset empties this registry completely (Req 2.7).
    populated.reset()
    assert len(populated) == 0
    assert populated.all() == []
    assert populated.ids() == []
    assert populated.records() == []
    for stage in ALL_STAGES:
        assert populated.for_stage(stage) == []
    for engine_id in engines:
        assert engine_id not in populated
        assert populated.find(engine_id) is None

    # Resetting one instance is safe to repeat and still touches nothing else.
    populated.reset()
    assert len(observer) == 0
    assert len(default) == 0

    # The reverse direction: registering into the default leaves instances alone.
    for engine_id, _, _ in registrations:
        register(engines[engine_id])
    assert len(default) == len(registrations)
    assert len(populated) == 0
    assert len(observer) == 0
    reset_registry()
    assert len(default) == 0
