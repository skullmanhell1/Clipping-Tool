"""Base-contract property module for the av-engines-foundation spec
(``worker/engines/base.py``).

The real base-contract properties have now landed (tasks 3.5-3.12):

* **P1** — ``Engine_Result`` is serialisable with a closed status domain (3.5).
* **P2** — invocation never mutates the caller's options or context (3.6).
* **P16** — options parsing is total and ignores unknown keys (3.7).
* **P17** — options serialisation round-trips (3.8).
* **P18** — resolution is idempotent and order-insensitive (3.9).
* **P19** — the ``Options_Digest`` is deterministic, order-insensitive,
  discriminating, and stable across processes (3.10).
* **P20** — planning is pure, seeded, and reproducible (3.11).

plus the abstract-surface / marker-helper / import-safety unit tests (task 3.12,
deliberately NOT numbered properties) and the original task 1.3 tooling smoke
check, which is kept below.

Generators come from the shared ``tests/strategies.py`` module and the engine
doubles from ``tests/fakes.py`` — neither is redefined here. The foundation
ships no concrete engine, so the options properties exercise the real coercion
layer through a minimal local :class:`_Demo_Options` record and the planning
property through a minimal local :class:`_Demo_Engine`; both are test fixtures,
not production types.

Everything here is pure and offline: no ffmpeg, no probe, no network. The two
places that *do* spawn a process (the fresh-interpreter digest check of P19 and
the import-safety check of task 3.12) do so on purpose — a same-process check
cannot prove either claim.
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Mapping, Optional, Sequence
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes import FakeEngine, RaisingEngine
from tests.strategies import st_engine_outcomes, st_options_mapping, st_word_timeline
from worker.engines.base import (
    DIGEST_LENGTH,
    FLAG_SUFFIX,
    AV_Engine,
    Engine_Context,
    Engine_Result,
    Engine_Stage,
    Engine_Status,
    coerce_bool,
    coerce_choice,
    coerce_float,
    coerce_int,
    coerce_str,
    derive_seed,
    dump_options,
    marker,
    options_digest,
)
from worker.engines.timebase import Time_Base

#: Repository root — the cwd/``PYTHONPATH`` the subprocess checks need.
_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Plan keys whose values are timestamps or durations in seconds (Req 15.7).
_TIMING_KEYS = ("start", "end", "jitter", "duration")

#: Digest shape required by Req 11.5.
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{16}$")

#: Cap on how many key-insertion-order permutations P18/P19 try per example.
#: ``st_options_mapping`` emits up to six keys (720 permutations); 24 covers
#: *every* permutation for mappings of four keys or fewer and a deterministic,
#: reproducible slice beyond that. Documented interpretation of "for every
#: permutation": exhaustive where cheap, bounded where it would dominate runtime.
_MAX_PERMUTATIONS = 24


# --- Tooling smoke check (task 1.3 — NOT a numbered design property) --------
# Feature: av-engines-foundation, Tooling smoke check: hypothesis collects and runs
@settings(max_examples=100)
@given(st.integers())
def test_hypothesis_toolchain_available(value):
    """Validates: Requirements 22.7

    A trivial invariant (``value + 0 == value`` for every integer) exercised
    through ``@given``/``@settings`` so a missing or unimportable ``hypothesis``
    fails collection loudly. Not part of P1-P20 coverage.
    """
    assert value + 0 == value


# --------------------------------------------------------------------------- #
# Local fixtures: a minimal Engine_Options record and two minimal engines       #
#                                                                             #
# The foundation deliberately ships no concrete engine, so the options and      #
# planning properties need their own smallest-possible subjects. Both are built #
# out of the *real* ``coerce_*`` helpers and the *real* ``AV_Engine`` base, so   #
# the properties test production code rather than a mock of it.                 #
# --------------------------------------------------------------------------- #
#: Known values of the enum-like ``layout`` choice field.
_LAYOUTS = ("karaoke", "word", "line")

#: Documented per-field defaults of :class:`_Demo_Options` (Req 10.5).
_DEMO_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "intensity": 3,
    "gain": 1.0,
    "layout": "karaoke",
    "model": "base",
}

#: Bounds the numeric fields are clamped to.
_INTENSITY_RANGE = (0, 10)
_GAIN_RANGE = (0.0, 2.0)
_MODEL_MAX_LEN = 32


@dataclass(frozen=True)
class _Demo_Options:
    """A representative :class:`worker.engines.base.Engine_Options` record.

    One field of each kind the coercion layer supports — a bool, a bounded int, a
    bounded float, an enum-like choice, and a free-text string — with a **total**
    :meth:`parse` assembled from the production ``coerce_*`` helpers. Frozen, so
    an engine cannot write back into a caller's options (Req 9.6).
    """

    enabled: bool = _DEMO_DEFAULTS["enabled"]
    intensity: int = _DEMO_DEFAULTS["intensity"]
    gain: float = _DEMO_DEFAULTS["gain"]
    layout: str = _DEMO_DEFAULTS["layout"]
    model: str = _DEMO_DEFAULTS["model"]

    @classmethod
    def parse(cls, data: Optional[Mapping[str, Any]]) -> "_Demo_Options":
        """Total parser: never raises, ignores unknown keys, defaults per field."""
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            enabled=coerce_bool(data.get("enabled"), _DEMO_DEFAULTS["enabled"]),
            intensity=coerce_int(
                data.get("intensity"),
                _DEMO_DEFAULTS["intensity"],
                lo=_INTENSITY_RANGE[0],
                hi=_INTENSITY_RANGE[1],
            ),
            gain=coerce_float(
                data.get("gain"),
                _DEMO_DEFAULTS["gain"],
                lo=_GAIN_RANGE[0],
                hi=_GAIN_RANGE[1],
            ),
            layout=coerce_choice(data.get("layout"), _LAYOUTS, _DEMO_DEFAULTS["layout"]),
            model=coerce_str(
                data.get("model"), _DEMO_DEFAULTS["model"], max_len=_MODEL_MAX_LEN
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping (Req 10.2)."""
        return {
            "enabled": bool(self.enabled),
            "intensity": int(self.intensity),
            "gain": float(self.gain),
            "layout": str(self.layout),
            "model": str(self.model),
        }


_DEMO_FIELDS = tuple(field.name for field in dataclasses.fields(_Demo_Options))


class _Demo_Engine(AV_Engine):
    """The smallest complete :class:`~worker.engines.base.AV_Engine`.

    ``resolve_options`` projects any options source onto :class:`_Demo_Options`;
    ``plan`` derives per-word cues, taking every random number from
    ``ctx.rng()`` (Req 12.2) and nothing else.
    """

    engine_id: ClassVar[str] = "demo"
    stage: ClassVar[Engine_Stage] = Engine_Stage.AUDIO

    def resolve_options(self, options: Any) -> _Demo_Options:
        source = options if isinstance(options, Mapping) else dump_options(options)
        return _Demo_Options.parse(source)

    def plan(self, ctx: Engine_Context) -> dict[str, Any]:
        rng = ctx.rng()                     # the ONLY randomness source
        cues = []
        for word in ctx.words:
            jitter = rng.random() / 100.0
            cues.append(
                {
                    "start": float(getattr(word, "start", 0.0)) + jitter,
                    "end": float(getattr(word, "end", 0.0)) + jitter,
                    "jitter": float(jitter),
                    "text": str(getattr(word, "text", "")),
                }
            )
        options = ctx.options
        return {
            "cues": cues,
            "duration": float(ctx.duration),
            "layout": str(getattr(options, "layout", _DEMO_DEFAULTS["layout"])),
            "intensity": int(getattr(options, "intensity", _DEMO_DEFAULTS["intensity"])),
            "seed": int(ctx.seed),
        }

    def run(self, ctx: Engine_Context) -> Engine_Result:
        return Engine_Result(
            engine_id=self.engine_id,
            status=Engine_Status.APPLIED,
            markers=(marker(self.engine_id, "applied"),),
            plan=self.plan(ctx),
        )


class _Mutating_Engine(AV_Engine):
    """An engine that *tries* to mutate the caller's options and its context.

    Every attempt is recorded rather than swallowed silently, so P2 can assert
    the attempts were made **and** that all of them raised.
    """

    engine_id: ClassVar[str] = "mutator"
    stage: ClassVar[Engine_Stage] = Engine_Stage.AUDIO

    def __init__(self) -> None:
        self.option_errors: list = []
        self.context_errors: list = []

    def resolve_options(self, options: Any) -> _Demo_Options:
        for name in _DEMO_FIELDS:
            try:
                setattr(options, name, "mutated")
            except Exception as exc:                     # noqa: BLE001 - recorded
                self.option_errors.append((name, exc))
        try:
            options["layout"] = "mutated"                # not a mapping either
        except Exception as exc:                         # noqa: BLE001 - recorded
            self.option_errors.append(("__setitem__", exc))
        return _Demo_Options.parse(dump_options(options))

    def plan(self, ctx: Engine_Context) -> dict[str, Any]:
        return {}

    def run(self, ctx: Engine_Context) -> Engine_Result:
        for field in dataclasses.fields(ctx):
            try:
                setattr(ctx, field.name, None)
            except Exception as exc:                     # noqa: BLE001 - recorded
                self.context_errors.append((field.name, exc))
        return Engine_Result(engine_id=self.engine_id, status=Engine_Status.APPLIED)


# --------------------------------------------------------------------------- #
# Local helpers                                                                 #
# --------------------------------------------------------------------------- #
def _context(
    *,
    engine_id: str = "demo",
    stage: Engine_Stage = Engine_Stage.AUDIO,
    options: Any = None,
    words: Sequence[Any] = (),
    duration: float = 0.0,
    seed: int = 0,
    digest: str = "",
) -> Engine_Context:
    """A complete, frozen :class:`Engine_Context` with clip-relative bounds."""
    return Engine_Context(
        job_id="job-1",
        clip_id="clip-1",
        engine_id=engine_id,
        stage=stage,
        source_path=Path("/tmp/source.mp4"),
        clip_path=Path("/tmp/clip.mp4"),
        time_base=Time_Base(),
        clip_start=0.0,
        clip_end=float(duration),
        duration=float(duration),
        words=tuple(words),
        options=options,
        options_digest=digest,
        seed=int(seed),
    )


def _canonical(payload: Any) -> str:
    """Canonical JSON text of a dumped mapping.

    Used wherever a property says two dumps are "identical" or "differ".
    ``==`` on the dumped mappings themselves is **not** usable as that notion:
    ``float("nan") != float("nan")``, so two structurally identical dumps
    carrying a NaN compare unequal, and ``1 == 1.0 == True``, so structurally
    different dumps can compare equal. Byte-identical canonical text is the
    honest reading, and it is exactly the notion of sameness a digest can honour.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )


def _key_permutations(mapping: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Up to :data:`_MAX_PERMUTATIONS` key orders of ``mapping`` (all when cheap)."""
    return list(itertools.islice(itertools.permutations(tuple(mapping)), _MAX_PERMUTATIONS))


def _assert_json_value_tree(value: Any, path: str = "$") -> None:
    """Assert ``value`` is built only from JSON scalars, lists and str-keyed maps."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_value_tree(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str), f"non-str key at {path}: {key!r}"
            _assert_json_value_tree(item, f"{path}.{key}")
        return
    raise AssertionError(f"non-JSON value at {path}: {type(value).__name__} {value!r}")


def _assert_timing_values_are_floats(value: Any, path: str = "$") -> None:
    """Assert every timing entry in a plan is a ``float`` (Req 15.7)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _TIMING_KEYS or key.endswith("_s"):
                assert isinstance(item, float) and not isinstance(item, bool), (
                    f"timing value at {path}.{key} is {type(item).__name__}, not float"
                )
            _assert_timing_values_are_floats(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_timing_values_are_floats(item, f"{path}[{index}]")


#: Body of the fresh-interpreter digest probe used by P19 (task 3.10). Reads a
#: JSON payload on stdin and prints ``options_digest`` of it.
_DIGEST_PROBE = (
    "import json, sys\n"
    "from worker.engines.base import options_digest\n"
    "sys.stdout.write(options_digest(json.loads(sys.stdin.read())))\n"
)

#: A hash seed that differs from this interpreter's. With ``PYTHONHASHSEED``
#: unset the parent's seed is randomised per process, so ``"0"`` (randomisation
#: disabled) always differs; if the parent pinned ``0``, pick something else.
_CHILD_HASH_SEED = "12345" if os.environ.get("PYTHONHASHSEED") == "0" else "0"


def _run_probe(code: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter rooted at the repository."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = _CHILD_HASH_SEED
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        input=stdin,
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# --------------------------------------------------------------------------- #
# Property 1 (task 3.5)                                                         #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 1: Engine_Result is serialisable with a
# closed status domain — *For any* Engine_Result produced by any engine outcome,
# `Engine_Result.from_dict(r.to_dict())` equals `r`, `to_dict()` is JSON-encodable,
# and `status` is a member of `Engine_Status`. Generator: `st_engine_outcomes`.
@settings(max_examples=100, deadline=None)
@given(outcome=st_engine_outcomes())
def test_p1_engine_result_serialises_with_closed_status_domain(outcome):
    """Validates: Requirements 1.2, 1.6, 18.5

    The result actually returned by an engine (``FakeEngine`` fed the generated
    status × markers × artifacts × plan outcome) round-trips through
    ``to_dict``/``from_dict`` unchanged, its wire form survives real
    ``json.dumps``/``json.loads``, and its status is always one of the four
    closed-domain ``Engine_Status`` members. When the outcome carries an
    exception, the host-side ``Engine_Result.failed`` record built from it is
    held to the same contract.
    """
    exception = outcome["exception"]
    canned = {key: value for key, value in outcome.items() if key != "exception"}

    engine = FakeEngine(**canned)
    ctx = _context(engine_id=outcome["engine_id"], options=_Demo_Options())
    results = [engine.run(ctx)]

    if exception is not None:
        raiser = RaisingEngine(engine_id=outcome["engine_id"], exc=exception)
        with pytest.raises(type(exception)):
            raiser.run(ctx)
        results.append(
            Engine_Result.failed(
                outcome["engine_id"], f"{type(exception).__name__}: {exception}"
            )
        )

    for result in results:
        # --- closed status domain (Req 1.6) -------------------------------
        assert isinstance(result.status, Engine_Status)
        assert result.status in tuple(Engine_Status)

        payload = result.to_dict()
        # --- JSON-encodable wire form (Reqs 1.2, 18.5) --------------------
        text = json.dumps(payload)
        _assert_json_value_tree(payload)
        assert payload["status"] in {member.value for member in Engine_Status}

        # --- exact round-trip (Req 1.2) -----------------------------------
        assert Engine_Result.from_dict(payload) == result
        assert Engine_Result.from_dict(json.loads(text)) == result


# --------------------------------------------------------------------------- #
# Property 2 (task 3.6)                                                         #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 2: Engine invocation never mutates the
# caller's options or context — *For all* generated `ProcessingOptions` and *for any*
# engine — including one that attempts to mutate its context — `dataclasses.asdict(
# options)` is identical before and after the stage runs, and every attempted
# `Engine_Context` field assignment raises. Generators: `st_options_mapping`,
# `st_engine_outcomes`.
@settings(max_examples=100, deadline=None)
@given(mapping=st_options_mapping(), outcome=st_engine_outcomes())
def test_p2_invocation_never_mutates_caller_options_or_context(mapping, outcome):
    """Validates: Requirements 1.3, 9.6

    Three engines are run against one options record and one context: the canned
    ``FakeEngine`` built from the generated outcome, a ``RaisingEngine`` when the
    outcome carries an exception, and a deliberately hostile engine that attempts
    to assign to every options field, to index the options as a mapping, and to
    assign to every ``Engine_Context`` field. ``dataclasses.asdict(options)`` is
    identical before and after, every hostile attempt raises, and a direct
    assignment to each context field raises ``FrozenInstanceError``.

    Interpretation: the foundation ships no concrete engine and its
    ``ProcessingOptions`` projection is the ``Engine_Options`` record, so the
    "generated ProcessingOptions" of the design text is the local
    ``_Demo_Options`` parsed from a generated hostile mapping.
    """
    options = _Demo_Options.parse(mapping)
    before = dataclasses.asdict(options)

    ctx = _context(options=options, duration=1.0, seed=7)
    ctx_before = [(field.name, getattr(ctx, field.name)) for field in dataclasses.fields(ctx)]

    canned = {key: value for key, value in outcome.items() if key != "exception"}
    canned_engine = FakeEngine(**canned)
    canned_engine.resolve_options(options)
    canned_engine.run(ctx)

    if outcome["exception"] is not None:
        raiser = RaisingEngine(engine_id=outcome["engine_id"], exc=outcome["exception"])
        with pytest.raises(type(outcome["exception"])):
            raiser.run(ctx)

    mutator = _Mutating_Engine()
    mutator.resolve_options(options)
    mutator.run(ctx)

    # --- the caller's options are untouched (Reqs 1.3, 9.6) ---------------
    assert dataclasses.asdict(options) == before

    # --- every attempted options mutation raised --------------------------
    assert {name for name, _ in mutator.option_errors} == set(_DEMO_FIELDS) | {"__setitem__"}
    for name, exc in mutator.option_errors:
        assert isinstance(exc, (dataclasses.FrozenInstanceError, TypeError)), (name, exc)

    # --- every attempted context assignment raised (Req 1.3) --------------
    field_names = {field.name for field in dataclasses.fields(ctx)}
    assert {name for name, _ in mutator.context_errors} == field_names
    for name, exc in mutator.context_errors:
        assert isinstance(exc, dataclasses.FrozenInstanceError), (name, exc)

    # --- and the same holds for a direct assignment by any caller ---------
    for field in dataclasses.fields(ctx):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(ctx, field.name, None)

    # --- the context itself still holds its original values ---------------
    assert [(field.name, getattr(ctx, field.name)) for field in dataclasses.fields(ctx)] == (
        ctx_before
    )


# --------------------------------------------------------------------------- #
# Property 16 (task 3.7)                                                        #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 16: Engine_Options parsing is total and
# ignores unknown keys — *For any* mapping of arbitrary JSON-ish values (wrong types,
# `None`, nested structures, NaN-like strings), `parse` returns an Engine_Options
# instance without raising in which every field is either a coerced valid value or the
# documented default; and *for any* valid mapping extended with arbitrary unrecognised
# keys, the parsed value equals the parse of the mapping alone. `coerce_choice` returns
# its input when in the known set and the default otherwise. Generator:
# `st_options_mapping`.
@settings(max_examples=100, deadline=None)
@given(mapping=st_options_mapping())
def test_p16_options_parsing_is_total_and_ignores_unknown_keys(mapping):
    """Validates: Requirements 10.2, 10.4, 10.5, 10.7, 20.5

    ``parse`` never raises on a hostile mapping; every field lands inside its
    documented domain (typed, finite, clamped, choice-restricted, length-capped)
    and a key that is absent gets exactly the documented default; extending an
    already-valid mapping with arbitrary unrecognised keys does not change the
    parse; and ``coerce_choice`` passes a known value through untouched while
    substituting the default for everything else.
    """
    options = _Demo_Options.parse(mapping)           # must not raise (Req 10.4)

    # --- every field is a coerced valid value (Reqs 10.2, 10.4) -----------
    assert isinstance(options.enabled, bool)
    assert isinstance(options.intensity, int) and not isinstance(options.intensity, bool)
    assert _INTENSITY_RANGE[0] <= options.intensity <= _INTENSITY_RANGE[1]
    assert isinstance(options.gain, float) and math.isfinite(options.gain)
    assert _GAIN_RANGE[0] <= options.gain <= _GAIN_RANGE[1]
    assert options.layout in _LAYOUTS
    assert isinstance(options.model, str) and len(options.model) <= _MODEL_MAX_LEN

    # --- an absent key yields the documented default (Req 10.5) -----------
    for name in _DEMO_FIELDS:
        if name not in mapping:
            assert getattr(options, name) == _DEMO_DEFAULTS[name]

    # --- a missing/None mapping yields the all-defaults record ------------
    assert _Demo_Options.parse(None) == _Demo_Options()
    assert _Demo_Options.parse({}) == _Demo_Options()

    # --- unknown keys are ignored (Reqs 10.2, 20.5) -----------------------
    valid = dump_options(options)                    # a valid mapping by construction
    unknown = {key: value for key, value in mapping.items() if key not in _DEMO_FIELDS}
    unknown.update({"unknown_field": mapping, "_private": [1, 2], "": None})
    extended = dict(valid)
    extended.update(unknown)
    assert _Demo_Options.parse(extended) == _Demo_Options.parse(valid) == options

    # --- coerce_choice: input when known, default otherwise (Req 10.7) ----
    for known in _LAYOUTS:
        assert coerce_choice(known, _LAYOUTS, _DEMO_DEFAULTS["layout"]) == known
    for value in list(mapping.values()) + [None, [], {}, 0, 1, True, "Karaoke", "unknown"]:
        if isinstance(value, str) and value in _LAYOUTS:
            continue
        assert (
            coerce_choice(value, _LAYOUTS, _DEMO_DEFAULTS["layout"])
            == _DEMO_DEFAULTS["layout"]
        )


# --------------------------------------------------------------------------- #
# Property 17 (task 3.8)                                                        #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 17: Engine_Options serialisation
# round-trips — *For any* valid Engine_Options value `o`, `dump_options(parse(
# dump_options(o))) == dump_options(o)`, and the dumped mapping contains only
# JSON-serialisable scalars, lists, and mappings. Generator: `st_options_mapping`.
@settings(max_examples=100, deadline=None)
@given(mapping=st_options_mapping())
def test_p17_options_serialisation_round_trips(mapping):
    """Validates: Requirements 10.1, 10.3

    The valid options value under test is ``parse`` of a generated hostile
    mapping (the only way to obtain a valid record without a concrete engine).
    Its dump survives a parse-and-dump cycle byte for byte, holds only JSON
    scalars/lists/mappings, and encodes under strict JSON (``allow_nan=False``),
    so no non-standard token can hide in it.
    """
    options = _Demo_Options.parse(mapping)
    dumped = dump_options(options)

    # --- round-trip (Req 10.3) -------------------------------------------
    assert dump_options(_Demo_Options.parse(dumped)) == dumped
    assert _canonical(dump_options(_Demo_Options.parse(dumped))) == _canonical(dumped)
    # A second cycle drifts no further either.
    assert dump_options(_Demo_Options.parse(dump_options(_Demo_Options.parse(dumped)))) == dumped

    # --- only JSON-serialisable scalars, lists and mappings (Req 10.1) ----
    _assert_json_value_tree(dumped)
    json.dumps(dumped, allow_nan=False)              # strict JSON: must not raise
    assert set(dumped) == set(_DEMO_FIELDS)

    # The same holds for the ``to_dict`` face of the protocol.
    _assert_json_value_tree(options.to_dict())
    json.dumps(options.to_dict(), allow_nan=False)


# --------------------------------------------------------------------------- #
# Property 18 (task 3.9)                                                        #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 18: Options resolution is idempotent and
# order-insensitive — *For any* `ProcessingOptions`, `resolve_options` called twice
# returns equal Engine_Options with equal digests; and *for any* mapping, the dumped
# output is identical for every insertion-order permutation of that mapping
# (sorted-key iteration). Generators: `st_options_mapping`, key permutations.
@settings(max_examples=100, deadline=None)
@given(mapping=st_options_mapping())
def test_p18_options_resolution_is_idempotent_and_order_insensitive(mapping):
    """Validates: Requirements 10.6, 12.6

    Resolving the same source twice yields equal options with equal digests, and
    re-resolving an already-resolved record is a no-op (idempotent projection).
    Re-inserting the mapping's keys in a different order changes neither the
    dumped mapping's canonical text nor its key order, which is always sorted.
    """
    engine = _Demo_Engine()

    # --- called twice: equal options, equal digests (Req 10.6) ------------
    once = engine.resolve_options(mapping)
    twice = engine.resolve_options(mapping)
    assert once == twice
    assert options_digest(once) == options_digest(twice)

    # --- idempotent: resolving the resolved value changes nothing ---------
    again = engine.resolve_options(once)
    assert again == once
    assert options_digest(again) == options_digest(once)

    # --- order-insensitive dumps (Req 12.6) ------------------------------
    baseline = dump_options(mapping)
    baseline_text = _canonical(baseline)
    assert list(baseline) == sorted(baseline)
    for order in _key_permutations(mapping):
        reordered = {key: mapping[key] for key in order}
        dumped = dump_options(reordered)
        assert list(dumped) == sorted(dumped)
        assert _canonical(dumped) == baseline_text
        assert engine.resolve_options(reordered) == once


# --------------------------------------------------------------------------- #
# Property 19 (task 3.10)                                                       #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 19: Options_Digest is deterministic,
# order-insensitive, discriminating, and stable — *For any* Engine_Options value the
# digest is stable across repeated calls; *for any* mapping and any permutation of its
# key insertion order the digests are equal; *for any* two values whose dumps differ
# the digests differ; and every digest matches `^[0-9a-f]{16}$` and equals the digest
# recomputed in a fresh interpreter process. Generator: `st_options_mapping`.
@settings(max_examples=100, deadline=None)
@given(left=st_options_mapping(), right=st_options_mapping())
def test_p19_options_digest_deterministic_order_insensitive_and_stable(left, right):
    """Validates: Requirements 11.1, 11.2, 11.3, 11.4, 11.5

    Four clauses, all on the real ``options_digest``: repeated calls agree;
    permuting key insertion order does not move the digest; two mappings whose
    canonical dumps differ get different digests; and the digest is 16 lowercase
    hex characters that a **fresh interpreter with a different ``PYTHONHASHSEED``**
    recomputes identically — the only check that can actually prove process
    stability.

    Interpretation: "whose dumps differ" is evaluated on the canonical JSON text
    (see ``_canonical``), because ``==`` on dumped mappings is neither reflexive
    for NaN nor discriminating between ``1``, ``1.0`` and ``True``.
    """
    options = _Demo_Options.parse(left)

    # --- stable across repeated calls (Reqs 11.1, 11.2) -------------------
    digest = options_digest(options)
    assert options_digest(options) == digest
    assert options_digest(_Demo_Options.parse(left)) == digest

    # --- fixed-length lowercase hex (Req 11.5) ---------------------------
    for value in (digest, options_digest(left), options_digest(right)):
        assert _DIGEST_PATTERN.match(value), value
        assert len(value) == DIGEST_LENGTH

    # --- order-insensitive (Req 11.3) ------------------------------------
    left_digest = options_digest(left)
    for order in _key_permutations(left):
        reordered = {key: left[key] for key in order}
        assert options_digest(reordered) == left_digest

    # --- discriminating (Req 11.4) ---------------------------------------
    left_dump, right_dump = dump_options(left), dump_options(right)
    if _canonical(left_dump) != _canonical(right_dump):
        assert options_digest(left) != options_digest(right)
    else:
        assert options_digest(left) == options_digest(right)

    # --- stable in a FRESH interpreter process (Req 11.5) ----------------
    payload = json.dumps(left_dump)                  # ASCII-only by default
    probe = _run_probe(_DIGEST_PROBE, stdin=payload)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == left_digest, (
        f"digest differs across processes: {left_digest} vs {probe.stdout.strip()!r}"
    )


#: Module-level ``random`` entry points that must never be consulted by an engine
#: (they all share the one global ``Random`` instance, which is process state, not
#: seeded per invocation). ``random.Random`` itself is deliberately absent:
#: ``Engine_Context.rng`` constructs one, and that is the sanctioned path.
_GLOBAL_RANDOM_ENTRY_POINTS = (
    "random",
    "randint",
    "randrange",
    "choice",
    "choices",
    "sample",
    "shuffle",
    "uniform",
    "gauss",
    "getrandbits",
    "seed",
    "triangular",
    "betavariate",
)


# --------------------------------------------------------------------------- #
# Property 20 (task 3.11)                                                       #
# --------------------------------------------------------------------------- #
# Feature: av-engines-foundation, Property 20: Engine planning is pure, seeded, and
# reproducible — *For any* clip inputs (words, bounds, options) and *for any* seed,
# `plan` returns equal serialised plans on repeated invocations; the seed is the only
# randomness source (a patched global `random` that raises is never touched); `plan`
# runs with `subprocess.run` and `socket.socket` patched to raise;
# `derive_seed(source_identity, digest)` is stable and differs whenever either input
# differs; and every timing value in the plan is a `float`. Generators:
# `st_word_timeline`, `st_options_mapping`.
@settings(max_examples=100, deadline=None)
@given(timeline=st_word_timeline(), mapping=st_options_mapping(), seed=st.integers(min_value=0))
def test_p20_planning_is_pure_seeded_and_reproducible(timeline, mapping, seed):
    """Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.5, 15.7

    ``plan`` is invoked twice inside a context where every module-level
    ``random`` entry point, ``subprocess.run`` and ``socket.socket`` raise on
    contact: the plans come back equal, nothing forbidden was touched (so
    ``ctx.rng()`` really is the only randomness source and planning really is
    offline), every timing entry is a ``float``, and the plan is JSON-encodable.
    ``derive_seed`` is then checked for stability and for discrimination in each
    argument.

    Interpretation of the ``derive_seed`` clause: the ``digest`` argument is
    drawn as a real ``options_digest`` value (fixed-length lowercase hex, as the
    signature documents). ``derive_seed`` hashes ``f"{identity}|{digest}"``, so
    arbitrary strings containing ``"|"`` in *both* positions could alias — with a
    real digest in the second position the split is unambiguous and no aliasing
    is possible.
    """
    words, duration = timeline
    options = _Demo_Options.parse(mapping)
    digest = options_digest(options)
    engine = _Demo_Engine()
    ctx = _context(options=options, words=words, duration=duration, seed=seed, digest=digest)

    touched: list[str] = []

    def _forbidden(*args, **kwargs):
        touched.append("touched")
        raise AssertionError("planning touched a forbidden global")

    with ExitStack() as stack:
        for name in _GLOBAL_RANDOM_ENTRY_POINTS:
            stack.enter_context(mock.patch.object(random, name, _forbidden))
        stack.enter_context(mock.patch.object(subprocess, "run", _forbidden))
        stack.enter_context(mock.patch.object(socket, "socket", _forbidden))
        first = engine.plan(ctx)
        second = engine.plan(ctx)
        # An equal context (same seed) plans identically too.
        third = _Demo_Engine().plan(
            _context(options=options, words=words, duration=duration, seed=seed, digest=digest)
        )

    # --- the global random module, ffmpeg and the network were untouched --
    # (Reqs 12.2, 12.5)
    assert touched == []

    # --- repeated invocations agree (Reqs 12.1, 12.3) --------------------
    assert _canonical(first) == _canonical(second) == _canonical(third)

    # --- serialisable plan, float timings (Reqs 12.5, 15.7) --------------
    json.dumps(first, allow_nan=False)
    _assert_json_value_tree(first)
    _assert_timing_values_are_floats(first)
    assert len(first["cues"]) == len(words)

    # --- the seed is what varies the plan (Req 12.2) ---------------------
    other = engine.plan(
        _context(
            options=options, words=words, duration=duration, seed=seed + 1, digest=digest
        )
    )
    assert _canonical(other) != _canonical(first)

    # --- derive_seed is stable and discriminating (Req 12.4) -------------
    identity = f"source:{len(words)}:{duration}"
    other_identity = identity + ":variant"
    # A genuinely different options value: an extra *public* key. (A leading
    # underscore would not do — ``dump_options`` drops private keys by design,
    # so the digest would be unchanged.)
    other_digest = options_digest({"variant": True, **dump_options(options)})

    assert derive_seed(identity, digest) == derive_seed(identity, digest)
    assert isinstance(derive_seed(identity, digest), int)
    assert derive_seed(identity, digest) >= 0
    assert derive_seed(other_identity, digest) != derive_seed(identity, digest)
    assert other_digest != digest
    assert derive_seed(identity, other_digest) != derive_seed(identity, digest)


# --------------------------------------------------------------------------- #
# Unit tests (task 3.12 — NOT numbered design properties)                       #
# --------------------------------------------------------------------------- #
def test_incomplete_engine_subclass_cannot_be_instantiated():
    """Validates: Requirements 1.1 — the abstract surface is enforced.

    A subclass that leaves ``resolve_options``, ``plan`` or ``run`` unimplemented
    raises ``TypeError`` at construction, and so does one that implements only
    part of the contract.
    """

    class _NoMethods(AV_Engine):
        engine_id: ClassVar[str] = "no_methods"

    class _PartiallyImplemented(AV_Engine):
        engine_id: ClassVar[str] = "partial"

        def resolve_options(self, options):
            return options

    with pytest.raises(TypeError):
        _NoMethods()
    with pytest.raises(TypeError):
        _PartiallyImplemented()

    # The complete local engine, by contrast, constructs fine.
    assert isinstance(_Demo_Engine(), AV_Engine)


def test_classvar_defaults_hold():
    """Validates: Requirements 19.1, 21.1 — the declared budget/capability defaults."""
    for subject in (AV_Engine, _Demo_Engine, _Demo_Engine()):
        assert subject.priority == 100
        assert subject.time_budget_s == 30.0
        assert subject.max_media_passes == 1
        assert subject.requires_network is False
        assert subject.produces_media is False
        assert subject.requires_model_download is False
        assert subject.required_capabilities == ()
        assert subject.optional_capabilities == ()


def test_flag_field_derives_engine_id_enabled():
    """Validates: Requirements 1.1 — ``flag_field`` is ``<engine_id>_enabled``."""

    class _Flagged(_Demo_Engine):
        engine_id: ClassVar[str] = "kinetic_typography"

    assert _Flagged.flag_field() == "kinetic_typography_enabled"
    assert _Flagged().flag_field() == f"kinetic_typography{FLAG_SUFFIX}"
    assert _Demo_Engine.flag_field() == "demo_enabled"

    # The flag is read off the resolved options and defaults to disabled.
    engine = _Flagged()
    assert engine.is_enabled({"kinetic_typography_enabled": "yes"}) is True
    assert engine.is_enabled({"kinetic_typography_enabled": "off"}) is False
    assert engine.is_enabled({}) is False
    assert engine.is_enabled(None) is False


def test_marker_formats_engine_id_detail():
    """Validates: Requirements 3.3 — ``marker()`` is ``engine:<id>:<detail>``."""
    assert marker("demo", "applied") == "engine:demo:applied"
    assert marker("stem_separation", "fallback") == "engine:stem_separation:fallback"
    # Total: non-string arguments are rendered, not rejected.
    assert marker("demo", 3) == "engine:demo:3"
    assert marker("", "") == "engine::"


#: Body of the import-safety probe: blocks the optional heavy packages in
#: ``sys.modules`` resolution, then imports every module named on the command line.
_IMPORT_PROBE = r"""
import importlib
import sys

BLOCKED = {
    "cv2", "torch", "torchaudio", "torchvision", "numpy", "scipy", "PIL",
    "whisper", "faster_whisper", "demucs", "librosa", "soundfile", "pydub",
    "moviepy", "ffmpeg", "requests", "boto3", "botocore", "fastapi", "pydantic",
    "transformers", "openai", "matplotlib", "sklearn",
}


class _Blocker:
    # Meta-path finder that refuses the optional heavy dependencies.

    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in BLOCKED:
            raise ImportError("blocked optional dependency: " + name)
        return None


sys.meta_path.insert(0, _Blocker())

for name in sys.argv[1:]:
    importlib.import_module(name)

leaked = sorted(root for root in BLOCKED if root in sys.modules)
if leaked:
    raise SystemExit("heavy dependency imported: " + ", ".join(leaked))

print("ok:" + ",".join(sys.argv[1:]))
"""


def test_every_engine_module_imports_without_heavy_dependencies():
    """Validates: Requirements 1.4 — the engine package is import-safe.

    Every ``worker/engines/*.py`` module is imported in a **fresh interpreter**
    with ``sys.modules`` blockers installed for the optional heavy packages
    (torch, OpenCV, numpy, whisper, demucs, requests, ...). A subprocess is
    required: this test session has already imported some of those packages, so
    an in-process import could not prove anything.
    """
    package = _REPO_ROOT / "worker" / "engines"
    modules = ["worker.engines"] + sorted(
        f"worker.engines.{path.stem}"
        for path in package.glob("*.py")
        if path.stem != "__init__"
    )
    assert len(modules) >= 3, modules          # __init__ + base + timebase at minimum

    probe = _run_probe(_IMPORT_PROBE, *modules)
    assert probe.returncode == 0, (
        f"import-safety probe failed for {modules}:\n"
        f"stdout={probe.stdout}\nstderr={probe.stderr}"
    )
    assert probe.stdout.strip() == "ok:" + ",".join(modules)
