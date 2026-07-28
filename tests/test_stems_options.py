"""Stem_Options property module for the audio-stem-inpainting spec
(``worker/engines/stems.py``).

Task 4.1 lands the single non-property pin below: ``tests/strategies.py`` mirrors the stem
vocabularies as literals (it cannot import ``worker.engines.stems`` without making every
test collection depend on that module), so the two copies are asserted equal here and
cannot silently drift — the same pin the kinetic tranche uses.

Tasks 4.5-4.7 append the numbered properties **P3** (options round-trip and digest
separation), **P4** (parsing is total under hostile input) and **P5** (resolution is
idempotent and survives the ProcessingOptions round-trip) to this same file, with
generators imported from ``tests/strategies.py`` rather than redefined here.

Everything here is pure and offline: no ffmpeg, no probe, no filesystem, no network.
"""

from __future__ import annotations

import dataclasses
import json
import math

from hypothesis import given, settings

from tests import strategies
from tests.strategies import (
    st_hostile_value,
    st_options_mapping,
    st_repair_window_ms,
    st_stem_options,
)
from worker.engines import stems
from worker.engines.base import options_digest
from worker.models import ProcessingOptions


def test_vocabularies_match_the_strategies_mirror() -> None:
    """Every mirrored stem vocabulary and bound is byte-equal in both modules."""
    assert tuple(stems.STEM_NAMES) == strategies.STEM_NAMES
    assert dict(stems.STEM_MAPPING) == strategies.STEM_MAPPING
    assert stems.MIX_PRESETS == strategies.MIX_PRESETS
    assert tuple(stems.REPAIR_MODES) == strategies.REPAIR_MODES
    assert tuple(stems.BACKEND_IDS) == strategies.BACKEND_IDS
    assert (stems.GAIN_MIN, stems.GAIN_MAX, stems.GAIN_DEFAULT) == (
        strategies.GAIN_MIN,
        strategies.GAIN_MAX,
        strategies.GAIN_DEFAULT,
    )
    assert (stems.WINDOW_MIN_MS, stems.WINDOW_MAX_MS, stems.WINDOW_DEFAULT_MS) == (
        strategies.WINDOW_MIN_MS,
        strategies.WINDOW_MAX_MS,
        strategies.WINDOW_DEFAULT_MS,
    )



# --------------------------------------------------------------------------- #
# P3 — round-trip and digest separation (task 4.5)                            #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 3: Stem_Options round-trips and its digest
# separates exactly the distinct values
@settings(max_examples=100, deadline=None)
@given(first=st_stem_options(), second=st_stem_options())
def test_p3_options_round_trip_and_digest_separates_distinct_values(
    first: dict, second: dict
) -> None:
    """``parse(to_dict(o)).to_dict() == o.to_dict()``, and the digest separates values.

    ``st_stem_options`` emits an already-in-range field *mapping*, so parsing it is the
    identity and the round-trip asserts serialisation completeness rather than coercion:
    if ``to_dict`` dropped or renamed a field, the re-parsed value would fall back to that
    field's default and the two mappings would differ (Req 9.4).

    The digest clause is the foundation ``options_digest`` applied to the resolved value —
    the same function the Engine_Workspace path and the idempotence key are built from.
    Equal values must digest equally *and* distinct values must digest differently, so the
    assertion is the biconditional, not one direction of it (Req 9.7).
    """
    left = stems.Stem_Options.parse(first)
    right = stems.Stem_Options.parse(second)

    # -- round-trip: serialise, re-parse, serialise again ---------------------
    for value in (left, right):
        assert stems.Stem_Options.parse(value.to_dict()).to_dict() == value.to_dict()
        assert stems.Stem_Options.parse(value.to_dict()) == value
        # every field present, sorted keys, JSON-native
        assert sorted(value.to_dict()) == [
            entry.name for entry in sorted(dataclasses.fields(stems.Stem_Options),
                                           key=lambda e: e.name)
        ]
        json.dumps(value.to_dict())

    # -- digest: equal iff the field values are equal -------------------------
    same_values = left.to_dict() == right.to_dict()
    assert (options_digest(left) == options_digest(right)) is same_values
    assert options_digest(left) == options_digest(
        stems.Stem_Options.parse(left.to_dict())
    )


# --------------------------------------------------------------------------- #
# P4 — parsing is total (task 4.6)                                            #
# --------------------------------------------------------------------------- #
# Feature: audio-stem-inpainting, Property 4: Parsing is total — hostile input yields
# documented defaults, never an exception
@settings(max_examples=100, deadline=None)
@given(
    mapping=st_options_mapping(),
    window=st_repair_window_ms(),
    hostile=st_hostile_value(),
)
def test_p4_parsing_is_total_under_hostile_input(
    mapping: dict, window: object, hostile: object
) -> None:
    """No mapping makes ``parse`` raise, and every field lands inside its declared set.

    ``st_options_mapping`` supplies unknown keys, wrong types, ``None``, nested
    containers, NaN-like strings and huge numbers; the drawn ``window`` and ``hostile``
    values are then planted on the *real* field names so the coercion ladder is exercised
    on the fields that have bounds rather than only on keys that are ignored (Req 9.5,
    18.5).

    The post-conditions are the documented ones: the three choice fields are members of
    their declared sets (Req 9.3), every gain is finite and inside
    ``[GAIN_MIN, GAIN_MAX]`` (Req 5.4), and ``repair_window_ms`` is an ``int`` inside
    ``[WINDOW_MIN_MS, WINDOW_MAX_MS]`` (Req 7.6) — and the whole result serialises, so a
    hostile input cannot poison the Engine_Result plan payload downstream.
    """
    payload = dict(mapping)
    payload["repair_window_ms"] = window
    for name in ("mix_preset", "repair_mode", "backend", "model", "declick",
                 "retain_stems", "gain_vocals", "gain_music", "gain_other"):
        if name not in payload:
            payload[name] = hostile

    options = stems.Stem_Options.parse(payload)          # must not raise

    assert options.mix_preset in stems.MIX_PRESET_CHOICES
    assert options.repair_mode in stems.REPAIR_MODES
    assert options.backend in stems.BACKEND_IDS
    for gain in (options.gain_vocals, options.gain_music, options.gain_other):
        assert isinstance(gain, float) and math.isfinite(gain)
        assert stems.GAIN_MIN <= gain <= stems.GAIN_MAX
    assert isinstance(options.repair_window_ms, int)
    assert not isinstance(options.repair_window_ms, bool)
    assert stems.WINDOW_MIN_MS <= options.repair_window_ms <= stems.WINDOW_MAX_MS
    assert isinstance(options.declick, bool) and isinstance(options.retain_stems, bool)
    assert isinstance(options.model, str)
    json.dumps(options.to_dict())

    # Parsing an already-parsed value is the identity, so totality composes.
    assert stems.Stem_Options.parse(options.to_dict()) == options


# --------------------------------------------------------------------------- #
# P5 — resolution is idempotent and survives the ProcessingOptions round-trip #
# --------------------------------------------------------------------------- #
#: The ``stem_*`` spellings :meth:`Stem_Options.from_processing_options` reads. Task 17.1
#: adds them to ``ProcessingOptions`` as declared fields; until then they are attached as
#: instance attributes, which is exactly the ``getattr`` surface the projection reads (the
#: same arrangement ``tests/test_kinetic_plan.py`` uses for the ``kinetic_*`` settings).
_STEM_ATTRS = tuple(
    "stem_" + entry.name for entry in dataclasses.fields(stems.Stem_Options)
)


# Feature: audio-stem-inpainting, Property 5: Option resolution is idempotent and
# survives the ProcessingOptions round-trip
@settings(max_examples=100, deadline=None)
@given(supplied=st_stem_options(), hostile=st_options_mapping())
def test_p5_resolution_is_idempotent_and_survives_the_options_round_trip(
    supplied: dict, hostile: dict
) -> None:
    """Resolving twice is the identity, and ``ProcessingOptions`` still round-trips.

    Two halves, both from the task text:

    * **Idempotence** (Req 9.6) — ``resolve_stem_options`` (the body of the engine's
      ``resolve_options``, task 4.3) applied to Processing_Options and then to its own
      result yields equal values. The second application reads the bare field names off
      the resolved :class:`Stem_Options`, which is why the projection is stable rather
      than collapsing to defaults on the second pass.
    * **Round-trip** (Req 9.8, 20.2) — the supplied Processing_Options is unchanged by
      resolution and ``ProcessingOptions.from_dict(dataclasses.asdict(o)) == o`` still
      holds, i.e. this engine's resolution does not disturb the existing
      ``tests/test_options_roundtrip.py`` guarantee.

    Hostile ``stem_*`` attributes are planted alongside the valid ones so idempotence is
    asserted on the coerced path too, not only where the projection is already the
    identity.
    """
    options = ProcessingOptions()
    for name in _STEM_ATTRS:
        field = name[len("stem_"):]
        value = supplied[field] if field in supplied else None
        setattr(options, name, value)
    # ... plus one hostile payload per plausible key, to exercise the coercion path.
    for key, value in hostile.items():
        if key.startswith("stem_") and key not in _STEM_ATTRS:
            setattr(options, key, value)

    before = dataclasses.asdict(options)

    once = stems.resolve_stem_options(options)
    twice = stems.resolve_stem_options(once)

    assert twice == once
    assert twice.to_dict() == once.to_dict()
    assert stems.resolve_stem_options(twice) == once
    assert options_digest(once) == options_digest(twice)

    # Read-only: resolution never writes to the caller's ProcessingOptions (Req 1.3).
    assert dataclasses.asdict(options) == before

    # The existing ProcessingOptions round-trip is untouched (Req 9.8, 20.2).
    assert ProcessingOptions.from_dict(dataclasses.asdict(options)) == options

    # A ProcessingOptions carrying no stem_* attribute at all resolves to the documented
    # safe defaults — the untouched-upgrade behaviour of Req 20.2.
    assert stems.resolve_stem_options(ProcessingOptions()) == stems.Stem_Options()
