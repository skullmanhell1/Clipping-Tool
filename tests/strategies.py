"""Shared ``hypothesis`` generators for the av-engines-foundation spec and its two
sibling engine specs (**audio stem separation** and **kinetic typography**).

This is the single shared generator module named in the av-engines-foundation design
("Named generators ... defined in ``tests/strategies.py`` and shared by sibling specs").
The generator **names are a stability contract**: the queued stem-separation and
kinetic-typography specs import them verbatim, so *extend this module, never fork it* —
add new generators, add keyword arguments with defaults, but do not rename, remove, or
narrow an existing generator's shape.

Conventions
-----------
* Every generator is a **function returning a strategy**, so it composes with
  ``st.lists``, ``st.tuples``, ``@st.composite`` and ``@given`` alike.
* Generators are deliberately **adversarial** where the property under test claims
  totality: :func:`st_options_mapping`, :func:`st_segment_records` and
  :func:`st_hostile_component` all emit values the production code must survive
  without raising.
* Everything here is pure and offline — no ffmpeg, no OpenCV, no network, no temp files.
* Shapes handed to the sibling specs (:func:`st_word_timeline`,
  :func:`st_segment_records`) are plain builtins / ``tests.conftest.FakeWord``
  instances, never spec-private types.

The first tranche (task 2.3) holds the generators that need no engine contract; the second
tranche (task 3.4) adds ``st_stage``, ``st_registrations`` and ``st_engine_outcomes``, which
depend on the ``worker/engines/base.py`` contract. The third tranche (kinetic-typography
task 2.1) adds the six engine generators every kinetic property test imports:
``st_kinetic_options``, ``st_kinetic_style``, ``st_reveal_mode``,
``st_i18n_word_timeline``, ``st_broken_word_timeline`` and ``st_font_availability``.
"""
from __future__ import annotations

import string
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from hypothesis import strategies as st

from tests.conftest import FakeWord
from worker.engines.base import Engine_Artifact, Engine_Stage, Engine_Status
from worker.engines.timebase import (
    DEFAULT_FPS,
    DEFAULT_SAMPLE_RATE,
    MAX_FPS,
    MIN_FPS,
    Rounding,
    Time_Base,
)

__all__ = [
    "CAPABILITY_KINDS",
    "DEFAULT_SEGMENT_DURATION",
    "KINETIC_STYLES",
    "LLM_CAPABILITY",
    "REVEAL_MODES",
    "SAMPLE_RATES",
    "st_availability_map",
    "st_broken_word_timeline",
    "st_capability_id",
    "st_engine_id",
    "st_engine_outcomes",
    "st_font_availability",
    "st_hostile_component",
    "st_hostile_value",
    "st_i18n_word_timeline",
    "st_invalid_fps",
    "st_kinetic_options",
    "st_kinetic_style",
    "st_malformed_capability_id",
    "st_options_mapping",
    "st_priority",
    "st_registrations",
    "st_reveal_mode",
    "st_segment_records",
    "st_stage",
    "st_time_base",
    "st_well_formed_capability_id",
    "st_word_timeline",
]

#: The capability kinds of ``worker.engines.capabilities.Capability_Kind``, kept as plain
#: strings (not imported) so this tranche stays importable before that module lands. Keep
#: this tuple in sync with the enum when a kind is added.
CAPABILITY_KINDS: Tuple[str, ...] = (
    "python_pkg",
    "binary",
    "ffmpeg_filter",
    "font",
    "provider_key",
    "model",
)

#: The bare capability id that needs no ``<kind>:<name>`` prefix.
LLM_CAPABILITY = "llm"

#: Sample rates worth exercising, including the engines' documented default.
SAMPLE_RATES: Tuple[int, ...] = (8000, 16000, 22050, 44100, DEFAULT_SAMPLE_RATE, 96000)

#: Clip duration :func:`st_segment_records` generates *valid* records against. Callers
#: that pass a different ``duration=`` must hand the same value to ``normalize_segments``.
DEFAULT_SEGMENT_DURATION = 30.0

_SNAKE_ALPHABET = string.ascii_lowercase
_CAPABILITY_NAME_ALPHABET = string.ascii_lowercase + string.digits + "_-."


# --------------------------------------------------------------------------- #
# Engine identity and ordering                                                  #
# --------------------------------------------------------------------------- #
def st_engine_id(*, max_words: int = 3) -> st.SearchStrategy[str]:
    """Valid lowercase snake_case Engine_Ids; consumed by P5, P34, P35 and both sibling
    specs' registration tests."""
    word = st.text(alphabet=_SNAKE_ALPHABET, min_size=1, max_size=8)
    return st.lists(word, min_size=1, max_size=max_words).map("_".join)


def st_priority(*, min_value: int = 0, max_value: int = 5) -> st.SearchStrategy[int]:
    """Small engine priorities drawn from a deliberately narrow band so ties are common;
    consumed by the ordering properties P3 and P4 (and P28's relative-order swap)."""
    return st.integers(min_value=min_value, max_value=max_value)


# --------------------------------------------------------------------------- #
# Capabilities                                                                  #
# --------------------------------------------------------------------------- #
def _st_capability_name() -> st.SearchStrategy[str]:
    """A plausible capability name payload (``demucs``, ``atempo``, ``Inter-Bold`` ...)."""
    return st.text(alphabet=_CAPABILITY_NAME_ALPHABET, min_size=1, max_size=16)


def st_well_formed_capability_id() -> st.SearchStrategy[str]:
    """Only the well-formed ``<kind>:<name>`` ids (plus bare ``llm``); used where a test
    needs a parseable id rather than a totality stress case."""
    kinded = st.tuples(st.sampled_from(CAPABILITY_KINDS), _st_capability_name()).map(
        lambda pair: f"{pair[0]}:{pair[1]}"
    )
    return st.one_of(kinded, st.just(LLM_CAPABILITY))


def st_malformed_capability_id() -> st.SearchStrategy[str]:
    """Unknown-kind / structurally broken capability ids; the probe must be *total* over
    arbitrary strings (P10), so these are mixed into :func:`st_capability_id`."""
    fixtures = st.sampled_from(
        [
            "",
            ":",
            "::",
            ":demucs",
            "python_pkg:",
            "python_pkg::demucs",
            "PYTHON_PKG:demucs",  # wrong case -> unknown kind
            "unknown_kind:demucs",
            "binary",  # kind with no separator
            "binary:ff mpeg",  # embedded space
            "ffmpeg_filter:-atempo",
            "font:../../etc/passwd",
            "provider_key:OPENAI KEY",
            "model:\x00null",
            "llm:extra",
            "  llm  ",
            "🎬:emoji",
            "x" * 300,
        ]
    )
    return st.one_of(fixtures, st.text(max_size=24))


def st_capability_id() -> st.SearchStrategy[str]:
    """Capability ids spanning every well-formed kind *and* malformed/unknown-kind
    strings; consumed by P10 (probing is total) and P11 (report caching)."""
    return st.one_of(st_well_formed_capability_id(), st_malformed_capability_id())


def st_availability_map(*, max_size: int = 8) -> st.SearchStrategy[Dict[str, bool]]:
    """Capability id -> availability mappings for ``StaticProber``; consumed by P11, P12
    and P13."""
    return st.dictionaries(st_capability_id(), st.booleans(), max_size=max_size)


# --------------------------------------------------------------------------- #
# Hostile options mappings                                                      #
# --------------------------------------------------------------------------- #
#: Option keys engines plausibly read, so hostile mappings sometimes hit a real field
#: instead of always landing on an unknown key.
_PLAUSIBLE_OPTION_KEYS: Tuple[str, ...] = (
    "enabled",
    "engine_enabled",
    "stem_separation_enabled",
    "kinetic_typography_enabled",
    "intensity",
    "layout",
    "aspect",
    "model",
    "fps",
    "sample_rate",
    "rounding",
    "seed",
    "priority",
    "max_duration",
    "min_duration",
    "z_order",
)

#: String payloads that *look* numeric/boolean/null but are not, plus empty and
#: whitespace-only values — the classic coercion traps.
_NASTY_STRINGS: Tuple[str, ...] = (
    "",
    " ",
    "\t\n",
    "nan",
    "NaN",
    "-nan",
    "inf",
    "-inf",
    "Infinity",
    "None",
    "null",
    "nil",
    "true",
    "TRUE",
    "True ",
    "false",
    "yes",
    "no",
    "on",
    "off",
    "0",
    "1",
    "-0",
    "1e400",
    "0x10",
    "1_000",
    "1,5",
    "12.5.7",
    "٣",  # non-ASCII digit
    "9" * 40,
    "standard\x00",
    "../../etc/passwd",
    "🎬",
)


def _st_hostile_scalar() -> st.SearchStrategy[Any]:
    """A single hostile JSON-ish scalar: wrong type, ``None``, NaN-like text, huge number."""
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(10 ** 30), max_value=10 ** 30),
        st.floats(allow_nan=True, allow_infinity=True),
        st.sampled_from(_NASTY_STRINGS),
        st.text(max_size=24),
    )


def st_hostile_value() -> st.SearchStrategy[Any]:
    """A hostile option *value*: any hostile scalar, or a nested list/mapping of them."""
    return st.recursive(
        _st_hostile_scalar(),
        lambda children: st.one_of(
            st.lists(children, max_size=3),
            st.dictionaries(st.text(max_size=8), children, max_size=3),
        ),
        max_leaves=6,
    )


def st_options_mapping(*, max_size: int = 6) -> st.SearchStrategy[Dict[str, Any]]:
    """Adversarial JSON-ish option mappings (wrong types, ``None``, nested structures,
    NaN-like strings, empty strings, huge numbers, unknown keys); consumed by P2, P16,
    P17, P18, P19, P20, P34 and P35 — this is the generator that proves parsing is
    total."""
    keys = st.one_of(
        st.sampled_from(_PLAUSIBLE_OPTION_KEYS),
        st.text(max_size=12),
        st.sampled_from(["", " ", "🎬", "a" * 80]),
    )
    return st.dictionaries(keys, st_hostile_value(), max_size=max_size)


# --------------------------------------------------------------------------- #
# Segment records                                                               #
# --------------------------------------------------------------------------- #
def _copied(record: Any) -> Any:
    """Copy sampled container records so a consumer that mutates one cannot poison the
    strategy's shared fixture object."""
    if isinstance(record, dict):
        return dict(record)
    if isinstance(record, list):
        return list(record)
    return record


@st.composite
def _st_valid_segment_record(draw, duration: float):
    """One well-formed, in-bounds, non-degenerate ``{"start", "end"}`` record."""
    start = draw(st.floats(min_value=0.0, max_value=max(duration - 0.05, 0.0),
                           allow_nan=False, allow_infinity=False))
    length = draw(st.floats(min_value=0.05, max_value=max(duration / 2.0, 0.1),
                            allow_nan=False, allow_infinity=False))
    return {"start": round(start, 3), "end": round(min(start + length, duration), 3)}


@st.composite
def _st_inverted_segment_record(draw, duration: float):
    """One inverted record (``end < start``) — must be discarded by normalisation."""
    valid = draw(_st_valid_segment_record(duration))
    return {"start": valid["end"], "end": valid["start"]}


def _st_degenerate_segment_record(duration: float) -> st.SearchStrategy[Any]:
    """Zero-length, out-of-range, NaN/inf, non-numeric, missing-key and wrong-type
    records — everything ``normalize_segments`` has to reject or clamp."""
    zero_length = st.floats(min_value=0.0, max_value=duration,
                            allow_nan=False, allow_infinity=False).map(
        lambda t: {"start": round(t, 3), "end": round(t, 3)}
    )
    out_of_range = st.sampled_from(
        [
            {"start": -5.0, "end": 1.0},
            {"start": -5.0, "end": -1.0},
            {"start": duration + 1.0, "end": duration + 5.0},
            {"start": 0.0, "end": duration * 10.0},
            {"start": -1e9, "end": 1e9},
        ]
    )
    non_finite = st.sampled_from(
        [
            {"start": float("nan"), "end": 1.0},
            {"start": 0.0, "end": float("nan")},
            {"start": float("-inf"), "end": float("inf")},
            {"start": 0.0, "end": float("inf")},
        ]
    )
    non_numeric = st.sampled_from(
        [
            {"start": "0.0", "end": "1.0"},
            {"start": "abc", "end": "def"},
            {"start": None, "end": 1.0},
            {"start": 0.0, "end": None},
            {"start": True, "end": False},
            {"start": [0.0], "end": {"v": 1.0}},
        ]
    )
    missing_or_wrong_shape = st.sampled_from(
        [
            {},
            {"start": 0.0},
            {"end": 1.0},
            {"begin": 0.0, "finish": 1.0},
            {"start": 0.0, "end": 1.0, "unknown": "ignored"},
            None,
            "not-a-record",
            0.0,
            [0.0, 1.0],
            (0.0, 1.0),
        ]
    )
    return st.one_of(
        zero_length, out_of_range, non_finite, non_numeric, missing_or_wrong_shape
    ).map(_copied)


@st.composite
def st_segment_records(
    draw,
    *,
    duration: float = DEFAULT_SEGMENT_DURATION,
    min_size: int = 0,
    max_size: int = 8,
):
    """Segment records mixing valid dicts with malformed ones (inverted, NaN/inf,
    non-numeric, missing keys, wrong types, out-of-range, zero-length) plus deliberately
    touching and overlapping pairs; consumed by P24, P25, P26 and by both sibling specs'
    segment-normalisation tests.

    Returns a plain ``list`` of arbitrary objects in arbitrary order. Pass the same
    ``duration`` to ``normalize_segments`` that you passed here.
    """
    records: List[Any] = draw(
        st.lists(
            st.one_of(
                _st_valid_segment_record(duration),
                _st_inverted_segment_record(duration),
                _st_degenerate_segment_record(duration),
            ),
            min_size=min_size,
            max_size=max_size,
        )
    )

    # Guarantee touching / overlapping pairs show up often enough to matter for the
    # merge branch of normalisation, rather than relying on random collisions.
    if draw(st.booleans()):
        a = draw(_st_valid_segment_record(duration))
        span = max(a["end"] - a["start"], 0.05)
        touching = {"start": a["end"], "end": round(min(a["end"] + span, duration), 3)}
        overlapping = {
            "start": round(max(a["start"] + span / 2.0, 0.0), 3),
            "end": round(min(a["end"] + span / 2.0, duration), 3),
        }
        records.extend([a, touching, overlapping])

    # Order is arbitrary on input: normalisation is responsible for sorting.
    return list(draw(st.permutations(records)))


# --------------------------------------------------------------------------- #
# Word timelines                                                                #
# --------------------------------------------------------------------------- #
@st.composite
def st_word_timeline(
    draw,
    *,
    min_words: int = 1,
    max_words: int = 8,
):
    """An ordered, non-overlapping ``tests.conftest.FakeWord`` timeline paired with the
    clip ``duration`` that contains it; consumed by P20 and P27, and by the kinetic
    typography spec's layout properties.

    Returns ``(words, duration)`` so the bounds are consistent by construction: every
    word satisfies ``0 <= word.start <= word.end <= duration`` and the words are sorted
    by ``start``.
    """
    n = draw(st.integers(min_value=min_words, max_value=max_words))
    words: List[FakeWord] = []
    cursor = draw(st.floats(min_value=0.0, max_value=0.5,
                            allow_nan=False, allow_infinity=False))
    for _ in range(n):
        gap = draw(st.floats(min_value=0.0, max_value=0.6,
                             allow_nan=False, allow_infinity=False))
        length = draw(st.floats(min_value=0.05, max_value=0.9,
                                allow_nan=False, allow_infinity=False))
        start = cursor + gap
        end = start + length
        words.append(FakeWord(round(start, 3), round(end, 3),
                              draw(st.sampled_from(["so", "like", "hello", "um", "word"]))))
        cursor = end
    tail = draw(st.floats(min_value=0.0, max_value=1.0,
                          allow_nan=False, allow_infinity=False))
    duration = round(cursor + tail, 3)
    return words, duration


# --------------------------------------------------------------------------- #
# Time bases                                                                    #
# --------------------------------------------------------------------------- #
@st.composite
def st_time_base(draw, *, sample_rates: Sequence[int] = SAMPLE_RATES):
    """Valid :class:`worker.engines.timebase.Time_Base` combinations — ``fps`` inside
    ``[MIN_FPS, MAX_FPS]``, a realistic ``sample_rate``, both ``Rounding`` members, and
    both ``fps_substituted`` states; consumed by P21, P22 and P23."""
    fps = draw(
        st.one_of(
            st.floats(min_value=MIN_FPS, max_value=MAX_FPS,
                      allow_nan=False, allow_infinity=False),
            st.sampled_from([MIN_FPS, 23.976, 24.0, 25.0, DEFAULT_FPS, 50.0, 59.94,
                             60.0, MAX_FPS]),
        )
    )
    return Time_Base(
        fps=float(fps),
        sample_rate=draw(st.sampled_from(list(sample_rates))),
        rounding=draw(st.sampled_from(list(Rounding))),
        fps_substituted=draw(st.booleans()),
    )


def st_invalid_fps() -> st.SearchStrategy[Any]:
    """Probed fps values that must trigger the ``DEFAULT_FPS`` substitution (missing,
    zero, negative, non-finite, out of range); consumed by P21."""
    return st.one_of(
        st.none(),
        st.just(0.0),
        st.floats(min_value=-1000.0, max_value=0.0, allow_nan=False,
                  allow_infinity=False),
        st.sampled_from([float("nan"), float("inf"), float("-inf"),
                         MIN_FPS - 0.001, MAX_FPS + 0.001, 1e9, -1e9]),
    )


# --------------------------------------------------------------------------- #
# Hostile path components                                                       #
# --------------------------------------------------------------------------- #
def st_hostile_component() -> st.SearchStrategy[str]:
    """Path-component payloads (``..``, ``.``, ``/``, ``\\``, empty, NUL byte, unicode,
    very long strings, leading dots, reserved-ish names); consumed by P29 and P31, and by
    both sibling specs' workspace/artifact-key tests."""
    fixtures = st.sampled_from(
        [
            "",
            " ",
            ".",
            "..",
            "...",
            "./.",
            "../..",
            "../../etc/passwd",
            "/",
            "//",
            "/absolute",
            "\\",
            "\\\\server\\share",
            "..\\..\\windows",
            "\x00",
            "clip\x00id",
            "\n",
            "\t",
            ".hidden",
            "..leading-dots",
            "CON",
            "PRN",
            "NUL",
            "AUX",
            "COM1",
            "LPT1",
            "con.mp4",
            "a" * 512,
            "x/" * 128,
            "🎬🎬🎬",
            "naïve-café",
            "日本語",
            "мир",
            "UPPER_CASE",
            "spaces in name",
            "semi;colon&amp|pipe",
            "quote'\"quote",
            "*glob?[]",
            "trailing.",
            "trailing ",
            "-leading-dash",
        ]
    )
    return st.one_of(fixtures, st.text(max_size=64), st.text(min_size=200, max_size=300))



# --------------------------------------------------------------------------- #
# Tranche 2 (task 3.4): generators that depend on the engine contract           #
# --------------------------------------------------------------------------- #
#: Media types an ``Engine_Artifact`` declares (mirrors the ``base`` docstring).
_ARTIFACT_MEDIA_TYPES: Tuple[str, ...] = (
    "video",
    "audio",
    "image",
    "subtitle",
    "data",
)

#: Exception classes engines realistically raise; the host must isolate all of them.
_ENGINE_EXCEPTIONS: Tuple[type, ...] = (
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    OSError,
    TimeoutError,
    MemoryError,
    ZeroDivisionError,
)


def st_stage() -> st.SearchStrategy[Engine_Stage]:
    """Any :class:`worker.engines.base.Engine_Stage` member; consumed by P4 (stage
    lookup partitions the registry) and by both sibling specs' stage-hook tests."""
    return st.sampled_from(list(Engine_Stage))


@st.composite
def st_registrations(
    draw,
    *,
    min_size: int = 0,
    max_size: int = 6,
    allow_duplicate_ids: bool = False,
):
    """``(engine_id, stage, priority)`` registration sets, ties and conflicts included.

    Returns a ``list`` of triples in *arbitrary registration order* — the order is
    meaningful input, because P3 asserts ``for_stage`` is invariant under permutation.
    Priorities come from the deliberately narrow :func:`st_priority` band, so
    ``(priority, engine_id)`` **ties are common** (P3/P4 need them), and one drawn
    priority is often copied onto another entry to force an exact tie.

    Ids are unique by default, so the set can be registered wholesale. Pass
    ``allow_duplicate_ids=True`` to have a duplicate id appended (with a possibly
    different stage/priority) — the shape P5 needs for the duplicate-registration
    error.
    """
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    records: List[Tuple[str, Engine_Stage, int]] = []
    seen: set = set()
    for _ in range(n):
        engine_id = draw(st_engine_id(max_words=2))
        if engine_id in seen:
            # Keep ids unique without discarding the draw: suffix until distinct.
            engine_id = f"{engine_id}_{len(records)}"
        seen.add(engine_id)
        records.append((engine_id, draw(st_stage()), draw(st_priority())))

    # Force an exact (priority, engine_id) tie often enough to matter for ordering.
    if len(records) >= 2 and draw(st.booleans()):
        donor = draw(st.integers(min_value=0, max_value=len(records) - 1))
        target = draw(st.integers(min_value=0, max_value=len(records) - 1))
        engine_id, stage, _ = records[target]
        records[target] = (engine_id, stage, records[donor][2])

    if allow_duplicate_ids and records:
        index = draw(st.integers(min_value=0, max_value=len(records) - 1))
        duplicate_id = records[index][0]
        records.append((duplicate_id, draw(st_stage()), draw(st_priority())))
        return records

    return list(draw(st.permutations(records)))


@st.composite
def _st_engine_artifact(draw, *, durable: bool = None):
    """One :class:`worker.engines.base.Engine_Artifact` with a workspace-relative path."""
    name = draw(st_engine_id(max_words=1))
    suffix = draw(st.sampled_from([".mp4", ".wav", ".png", ".ass", ".json", ""]))
    file_name = f"{name}{suffix}"
    return Engine_Artifact(
        name=file_name,
        path=Path("engines") / file_name,
        media_type=draw(st.sampled_from(list(_ARTIFACT_MEDIA_TYPES))),
        durable=draw(st.booleans()) if durable is None else bool(durable),
    )


@st.composite
def st_engine_outcomes(
    draw,
    *,
    engine_id: str = None,
    max_markers: int = 3,
    max_artifacts: int = 2,
    allow_exception: bool = True,
):
    """The status × markers × artifacts × exception cross product one engine can produce.

    Returns a plain ``dict`` whose keys are a stability contract (the sibling specs
    splat it into :class:`tests.fakes.FakeEngine` and into ``Engine_Result``):

    ``engine_id``
        the engine the outcome belongs to (a valid Engine_Id).
    ``status``
        an :class:`worker.engines.base.Engine_Status` member — every member is reachable.
    ``markers``
        ``tuple[str, ...]``, a mix of already ``engine:<id>:<detail>``-namespaced and bare
        details, sometimes with a repeat so de-duplication is exercised.
    ``artifacts``
        ``tuple[Engine_Artifact, ...]`` with mixed ``durable`` flags and media types.
    ``plan``
        a small JSON-safe planning mapping.
    ``detail``
        a short human-readable string.
    ``exception``
        an ``Exception`` instance the engine should raise instead of returning, or
        ``None`` for a normal return. Independent of ``status`` on purpose: the host
        must cope with a failing engine whatever status it *would* have reported.
        Pass ``allow_exception=False`` for a returns-only outcome.

    Consumed by P1, P2, P7, P13 and P30, and by both sibling specs' host tests.
    """
    eid = engine_id if engine_id is not None else draw(st_engine_id(max_words=2))
    details = st.one_of(
        st.sampled_from(["applied", "skipped", "degraded", "timeout", "fallback", "cached"]),
        st.text(alphabet=_SNAKE_ALPHABET + "_", min_size=1, max_size=10),
    )
    markers: List[str] = draw(
        st.lists(
            st.one_of(details.map(lambda d: f"engine:{eid}:{d}"), details),
            max_size=max_markers,
        )
    )
    if markers and draw(st.booleans()):
        markers.append(markers[0])          # duplicate -> exercises merge_markers dedup

    artifacts = draw(st.lists(_st_engine_artifact(), max_size=max_artifacts))

    exception = None
    if allow_exception and draw(st.booleans()):
        exc_type = draw(st.sampled_from(list(_ENGINE_EXCEPTIONS)))
        exception = exc_type("engine failed")

    return {
        "engine_id": eid,
        "status": draw(st.sampled_from(list(Engine_Status))),
        "markers": tuple(markers),
        "artifacts": tuple(artifacts),
        "plan": draw(
            st.dictionaries(
                st.sampled_from(["segments", "cues", "intensity", "model", "seed"]),
                st.one_of(
                    st.integers(min_value=-1000, max_value=1000),
                    st.floats(min_value=-100.0, max_value=100.0,
                              allow_nan=False, allow_infinity=False),
                    st.booleans(),
                    st.text(max_size=8),
                ),
                max_size=3,
            )
        ),
        "detail": draw(st.text(max_size=24)),
        "exception": exception,
    }



# --------------------------------------------------------------------------- #
# Tranche 3 (kinetic-typography task 2.1): the six engine generators            #
# --------------------------------------------------------------------------- #
# NOTE ON DUPLICATED VOCABULARIES
# -------------------------------
# ``worker/engines/kinetic.py`` does not exist yet — kinetic-typography task 3.1 creates
# it — so ``KINETIC_STYLES`` / ``REVEAL_MODES`` cannot be imported here without making
# every foundation test collection depend on an unwritten module. They are therefore
# repeated below as literal constants.
#
#   *** Task 3.1 MUST define exactly these values in ``worker/engines/kinetic.py``, and a
#   *** later test MUST assert
#   ***     tuple(kinetic.KINETIC_STYLES) == KINETIC_STYLES
#   ***     tuple(kinetic.REVEAL_MODES)   == REVEAL_MODES
#   *** so this duplication cannot silently drift.
#
# DISCHARGED (kinetic task 3.6): that pin now lives in
# ``tests/test_kinetic_engine.py::test_kinetic_vocabularies_match_the_shared_generators``,
# which asserts both equalities (and that both spellings are sorted and de-duplicated) on
# every run. The two spellings are therefore pinned to each other; keep them in sync.
#
# NOTE ON WORD CONFIDENCE
# -----------------------
# ``tests.conftest.FakeWord`` *does* carry ``.probability``, but it is hard-coded to
# ``1.0`` and is not a constructor parameter. Rather than widen that shared double, the
# generators below construct a ``FakeWord`` and then set ``.probability`` on the instance
# (see :func:`_word`), so the emitted values stay exactly ``FakeWord`` instances and
# compose with :func:`st_word_timeline`. Consequence for kinetic task 6.8 (Property 13,
# the Word_Confidence floor): timelines drawn straight from :func:`st_word_timeline`
# carry ``probability == 1.0`` for every word, so no word can ever sit below a legal
# ``confidence_floor`` (0.0–1.0). That property must either draw its timeline from
# :func:`st_i18n_word_timeline` / :func:`st_broken_word_timeline` (both of which draw real
# probabilities) or set ``.probability`` on the words itself.

#: The 7 Kinetic_Styles, sorted — duplicated from kinetic task 3.1 (see note above).
KINETIC_STYLES: Tuple[str, ...] = (
    "bounce",
    "highlight_sweep",
    "karaoke_fill",
    "none",
    "pop",
    "slide_up",
    "typewriter",
)

#: The 2 Reveal_Modes, sorted — duplicated from kinetic task 3.1 (see note above).
REVEAL_MODES: Tuple[str, ...] = ("cumulative", "word_by_word")

#: Caption positions (``worker.effects.caption_presets.VALID_POSITIONS``); ``""`` means
#: "inherit the Base_Preset position" (Req 7.4), so it is a legal option value too.
_KINETIC_POSITIONS: Tuple[str, ...] = ("bottom", "center", "top")

#: The documented last rung of the font ladder.
_FALLBACK_FONT = "Arial"

#: Built-in Caption_Preset names (``caption_presets.BUILTIN_PRESETS``), kept as plain
#: strings so this module imports without the preset registry.
_PRESET_NAMES: Tuple[str, ...] = (
    "karaoke",
    "boxed",
    "minimal",
    "pop",
    "typewriter",
    "hormozi",
)

#: Font families worth putting on the ladder: realistic families, families that are never
#: installed in CI, and hostile-ish spellings the probe must survive.
_FONT_FAMILIES: Tuple[str, ...] = (
    "Arial",
    "Impact",
    "Inter",
    "Inter-Bold",
    "Montserrat",
    "Anton",
    "DejaVu Sans",
    "Noto Sans JP",
    "Definitely Not Installed",
    "font;with:punctuation",
    "  ",
    "🎬 Display",
)


def _word(start: Any, end: Any, text: str, probability: float = 1.0) -> FakeWord:
    """A ``tests.conftest.FakeWord`` carrying an explicit Word_Confidence.

    ``FakeWord.__init__`` does not accept ``probability`` (it pins ``1.0``), so it is set
    on the instance here — the shared double is left untouched and the emitted value is
    still exactly a ``FakeWord``.
    """
    word = FakeWord(start, end, text)
    word.probability = probability
    return word


# --------------------------------------------------------------------------- #
# Kinetic_Options field mappings                                                #
# --------------------------------------------------------------------------- #
@st.composite
def st_kinetic_options(
    draw,
    *,
    styles: Sequence[str] = KINETIC_STYLES,
    reveals: Sequence[str] = REVEAL_MODES,
    positions: Sequence[str] = _KINETIC_POSITIONS + ("",),
    captions_enabled: bool = None,
    hook_enabled: bool = None,
):
    """Valid ``Kinetic_Options`` **field mappings** spanning every declared bound.

    Returns a plain ``dict[str, Any]`` of JSON-native scalars, *not* a
    ``Kinetic_Options`` instance: that dataclass does not exist until kinetic task 3.2,
    and a mapping is exactly what ``Kinetic_Options.parse`` consumes. Once 3.2 lands,
    callers write ``Kinetic_Options.parse(draw(st_kinetic_options()))`` — every value is
    already in range, so ``parse`` is the identity on it (which is what makes the
    idempotence half of Property 18 meaningful).

    Bounds covered, inclusive, exactly as declared in the design:
    ``max_lines`` 1–4, ``max_line_width`` 6–80, ``safe_area_x_pct`` 0–25,
    ``safe_area_y_pct`` 0–40, ``motion_duration_ms`` 20–1000, ``confidence_floor``
    0.0–1.0. ``style`` / ``reveal`` / ``position`` / the font fields are drawn from their
    closed vocabularies (``position=""`` means "inherit the Base_Preset position", and
    ``font_override=""`` means "use ``preset_font``", both legal values).

    ``notes`` is deliberately absent: it is resolution *provenance* written by
    ``from_processing_options``, never an input field.

    Consumed by kinetic Properties 2, 4, 5, 6, 8, 10–19.
    """
    pick = st.sampled_from  # local alias keeps the draws below readable
    return {
        # --- motion vocabulary ---
        "style": draw(pick(list(styles))),
        "reveal": draw(pick(list(reveals))),
        # --- look, inherited from the Base_Preset ---
        "preset_name": draw(pick(list(_PRESET_NAMES))),
        "font_override": draw(
            st.one_of(st.just(""), pick(list(_FONT_FAMILIES)))
        ),
        "preset_font": draw(pick(list(_FONT_FAMILIES))),
        "font_size": draw(st.integers(min_value=12, max_value=200)),
        "position": draw(pick(list(positions))),
        # --- layout (bounds are inclusive on both ends) ---
        "max_lines": draw(st.integers(min_value=1, max_value=4)),
        "max_line_width": draw(st.integers(min_value=6, max_value=80)),
        "safe_area_x_pct": draw(
            st.floats(min_value=0.0, max_value=25.0,
                      allow_nan=False, allow_infinity=False)
        ),
        "safe_area_y_pct": draw(
            st.floats(min_value=0.0, max_value=40.0,
                      allow_nan=False, allow_infinity=False)
        ),
        # --- motion + emphasis ---
        "motion_duration_ms": draw(st.integers(min_value=20, max_value=1000)),
        "highlight_keywords": draw(st.booleans()),
        "keyword_ai": draw(st.booleans()),
        "emoji_inline": draw(st.booleans()),
        "confidence_floor": draw(
            st.floats(min_value=0.0, max_value=1.0,
                      allow_nan=False, allow_infinity=False)
        ),
        # --- carried context ---
        "captions_enabled": (
            draw(st.booleans()) if captions_enabled is None else bool(captions_enabled)
        ),
        "hook_enabled": (
            draw(st.booleans()) if hook_enabled is None else bool(hook_enabled)
        ),
        "hook_duration_s": draw(
            st.floats(min_value=0.0, max_value=6.0,
                      allow_nan=False, allow_infinity=False)
        ),
        "hook_font_size": draw(st.integers(min_value=12, max_value=240)),
        "durable_subtitle": draw(st.booleans()),
        "permissibility": draw(st.booleans()),
    }


def st_kinetic_style() -> st.SearchStrategy[str]:
    """One of the 7 :data:`KINETIC_STYLES`; consumed by kinetic Properties 6, 7, 8, 9."""
    return st.sampled_from(list(KINETIC_STYLES))


def st_reveal_mode() -> st.SearchStrategy[str]:
    """``cumulative`` or ``word_by_word``; consumed by kinetic Properties 6, 7, 9."""
    return st.sampled_from(list(REVEAL_MODES))


# --------------------------------------------------------------------------- #
# Internationalised word timelines                                              #
# --------------------------------------------------------------------------- #
#: Space-free wide scripts: Han, Hiragana, Katakana, Hangul. Every code point here is
#: East_Asian_Width ``W``/``F``, i.e. 2 Display_Width units per character.
_WIDE_TOKENS: Tuple[str, ...] = (
    "漢字",            # Han
    "日本語",          # Han
    "中文字幕",        # Han
    "ひらがな",        # Hiragana
    "こんにちは",      # Hiragana
    "カタカナ",        # Katakana
    "テスト",          # Katakana
    "한국어",          # Hangul
    "안녕하세요",      # Hangul
    "ｆｕｌｌｗｉｄｔｈ",  # fullwidth Latin (East_Asian_Width F)
)

#: Right-to-left scripts: Arabic and Hebrew, with and without vowel points.
_RTL_TOKENS: Tuple[str, ...] = (
    "مرحبا",
    "العربية",
    "كَلِمَة",          # Arabic + combining harakat
    "שלום",
    "עברית",
    "בְּרֵאשִׁית",       # Hebrew + combining niqqud
)

#: Tokens carrying combining marks (Unicode categories ``Mn``/``Me``) — decomposed
#: sequences that must count 0 Display_Width units for the mark itself.
_COMBINING_TOKENS: Tuple[str, ...] = (
    "e\u0301",                 # e + COMBINING ACUTE
    "cafe\u0301",              # café, decomposed
    "nai\u0308ve",             # naïve, decomposed
    "a\u0301\u0300\u0302",     # stacked marks
    "\u0915\u094d\u0937",      # Devanagari conjunct
    "o\u20dd",                 # COMBINING ENCLOSING CIRCLE (Me)
)

#: Emoji tokens: plain, skin-tone modified, ZWJ sequences, and flags.
_EMOJI_TOKENS: Tuple[str, ...] = (
    "🎬",
    "🔥",
    "👍🏽",
    "👨‍👩‍👧",
    "🇯🇵",
    "word🔥",
)

#: Single tokens whose Display_Width provably exceeds *any* legal ``max_line_width``
#: (the declared maximum is 80 units), so the "one over-long word sits alone on its line
#: and is never split" branch of layout is always reachable.
_OVER_LONG_TOKENS: Tuple[str, ...] = (
    "日" * 45,                 # 90 units (wide, 2 each)
    "ｗ" * 50,                 # 100 units (fullwidth)
    "A" * 90,                  # 90 units (narrow)
    "supercalifragilisticexpialidocious" * 3,   # 102 units
    "🔥" * 41,                 # 82 units (emoji, 2 each)
    "한글" * 25,               # 100 units
)

#: Ordinary Latin tokens, so an i18n timeline is a realistic *mixture* rather than
#: uniformly exotic.
_LATIN_TOKENS: Tuple[str, ...] = ("this", "changed", "everything", "ok", "I")


def _st_i18n_token(*, include_over_long: bool = True) -> st.SearchStrategy[str]:
    """One internationalised token from the wide / RTL / combining / emoji / Latin pools
    (plus the over-long pool unless suppressed)."""
    pools = [
        st.sampled_from(list(_WIDE_TOKENS)),
        st.sampled_from(list(_RTL_TOKENS)),
        st.sampled_from(list(_COMBINING_TOKENS)),
        st.sampled_from(list(_EMOJI_TOKENS)),
        st.sampled_from(list(_LATIN_TOKENS)),
    ]
    if include_over_long:
        pools.append(st.sampled_from(list(_OVER_LONG_TOKENS)))
    return st.one_of(pools)


@st.composite
def st_i18n_word_timeline(
    draw,
    *,
    min_words: int = 1,
    max_words: int = 8,
    include_over_long: bool = True,
):
    """An internationalised Word_Timeline built **on top of** :func:`st_word_timeline`.

    The timing skeleton is drawn from :func:`st_word_timeline`, so the bounds invariant
    is identical by construction (``0 <= start <= end <= duration``, sorted by ``start``);
    only the word ``text`` is replaced, from the wide-script (Han / Hiragana / Katakana /
    Hangul), right-to-left (Arabic / Hebrew), combining-mark, emoji, Latin and over-long
    pools. When ``include_over_long`` is set, at least one word is guaranteed to be a
    single token whose Display_Width exceeds any legal ``max_line_width`` (> 80 units).

    Every word also carries a drawn ``probability`` (see the tranche-3 note above), so
    the Word_Confidence floor is exercisable from this generator.

    Returns ``(words, duration)`` — the same shape :func:`st_word_timeline` returns, so
    the two are drop-in interchangeable in a property.

    Consumed by kinetic Properties 7 and 14.
    """
    skeleton, duration = draw(
        st_word_timeline(min_words=min_words, max_words=max_words)
    )
    token = _st_i18n_token(include_over_long=include_over_long)
    words: List[FakeWord] = [
        _word(
            w.start,
            w.end,
            draw(token),
            draw(st.floats(min_value=0.0, max_value=1.0,
                           allow_nan=False, allow_infinity=False)),
        )
        for w in skeleton
    ]
    if include_over_long and words:
        index = draw(st.integers(min_value=0, max_value=len(words) - 1))
        words[index] = _word(
            words[index].start,
            words[index].end,
            draw(st.sampled_from(list(_OVER_LONG_TOKENS))),
            words[index].probability,
        )
    return words, duration


# --------------------------------------------------------------------------- #
# Broken word timelines                                                         #
# --------------------------------------------------------------------------- #
#: Non-numeric bound payloads: ``captions._word_bounds`` must coerce every one of these
#: to ``0.0`` rather than raise.
_NON_NUMERIC_BOUNDS: Tuple[Any, ...] = (
    None,
    "",
    " ",
    "abc",
    "1.0",
    "nan",
    "inf",
    float("nan"),
    float("inf"),
    float("-inf"),
    [0.0],
    {"start": 0.0},
    (),
    True,
)

#: Empty / whitespace-only word texts, which sanitisation must drop entirely.
_BLANK_TEXTS: Tuple[str, ...] = ("", " ", "   ", "\t", "\n", "\r\n", "\u00a0", "\u3000")


def _break_missing_end(word: FakeWord) -> FakeWord:
    """Drop the ``end`` attribute entirely, so ``getattr(w, "end", None)`` is ``None``."""
    broken = _word(word.start, word.end, word.text, word.probability)
    del broken.end
    return broken


@st.composite
def st_broken_word_timeline(
    draw,
    *,
    min_words: int = 1,
    max_words: int = 8,
):
    """A Word_Timeline whose words are malformed in every documented way.

    Built by drawing a well-formed skeleton from :func:`st_word_timeline` and corrupting
    a random, possibly empty, subset — so a drawn timeline can be anywhere between
    entirely valid and entirely broken, which is exactly what the
    ``SYNTHESISED_RATIO_LIMIT`` branch of Property 12 needs to straddle.

    Corruptions applied, one per word, covering the whole documented set:

    * **missing ``end``** — the attribute is deleted, not set to ``None``;
    * **non-numeric bounds** — ``None``, ``""``, ``"abc"``, ``"1.0"``, NaN / ±inf, a
      list, a dict, a tuple, ``True``;
    * **inverted** — ``end < start``;
    * **zero-length** — ``end == start``;
    * **empty / whitespace-only text** — including NBSP and ideographic space.

    Words carry drawn ``probability`` values (see the tranche-3 note above).

    Returns ``(words, duration)``, the :func:`st_word_timeline` shape. ``duration`` is
    always finite and positive even when every word is broken.

    Consumed by kinetic Property 12.
    """
    skeleton, duration = draw(
        st_word_timeline(min_words=min_words, max_words=max_words)
    )
    words: List[FakeWord] = []
    for source in skeleton:
        probability = draw(
            st.floats(min_value=0.0, max_value=1.0,
                      allow_nan=False, allow_infinity=False)
        )
        base = _word(source.start, source.end, source.text, probability)
        kind = draw(
            st.sampled_from(
                [
                    "valid",
                    "missing_end",
                    "non_numeric_start",
                    "non_numeric_end",
                    "inverted",
                    "zero_length",
                    "blank_text",
                ]
            )
        )
        if kind == "missing_end":
            base = _break_missing_end(base)
        elif kind == "non_numeric_start":
            base = _word(
                draw(st.sampled_from(list(_NON_NUMERIC_BOUNDS))),
                source.end,
                source.text,
                probability,
            )
        elif kind == "non_numeric_end":
            base = _word(
                source.start,
                draw(st.sampled_from(list(_NON_NUMERIC_BOUNDS))),
                source.text,
                probability,
            )
        elif kind == "inverted":
            base = _word(source.end, source.start, source.text, probability)
        elif kind == "zero_length":
            base = _word(source.start, source.start, source.text, probability)
        elif kind == "blank_text":
            base = _word(
                source.start,
                source.end,
                draw(st.sampled_from(list(_BLANK_TEXTS))),
                probability,
            )
        words.append(base)
    return words, duration


# --------------------------------------------------------------------------- #
# Font ladder availability                                                      #
# --------------------------------------------------------------------------- #
@st.composite
def st_font_availability(
    draw,
    *,
    fonts: Sequence[str] = _FONT_FAMILIES,
    noise: st.SearchStrategy = None,
    allow_none_available: bool = True,
):
    """Availability combinations over the ``(font_override, preset_font, "Arial")`` ladder.

    Returns a plain ``dict`` whose keys are a stability contract:

    ``font_override`` / ``preset_font``
        the two option fields feeding the ladder; ``font_override`` is often ``""``.
    ``fallback_font``
        always ``"Arial"`` — the documented last rung.
    ``ladder``
        ``tuple[str, ...]``: the non-empty rungs in probe order, duplicates removed,
        ``"Arial"`` last. This is the *only* set of families the engine may emit.
    ``availability``
        ``dict[str, bool]`` keyed by **capability id** (``font:<family>``), ready to hand
        straight to ``tests.fakes.StaticProber(mapping, default=...)``. Composed with the
        foundation :func:`st_availability_map`, whose unrelated ids are merged in as
        noise; the ladder's own entries always win over the noise.
    ``default``
        the ``StaticProber`` answer for any id absent from ``availability``.
    ``available_families``
        ``tuple[str, ...]``: the ladder members whose id maps to ``True``.
    ``expected_font``
        the family the ladder must resolve to: the first available rung, else
        ``"Arial"``.
    ``expected_marked``
        ``True`` when ``expected_font`` differs from the requested family
        (``ladder[0]``), i.e. exactly one ``degraded:font:<requested>`` marker is owed.

    ``allow_none_available=False`` forces at least one rung available, for tests that
    need the non-degraded path.

    Consumed by kinetic Property 17.
    """
    font_override = draw(st.one_of(st.just(""), st.sampled_from(list(fonts))))
    preset_font = draw(st.sampled_from(list(fonts)))

    ladder: List[str] = []
    for family in (font_override, preset_font, _FALLBACK_FONT):
        if family and family not in ladder:
            ladder.append(family)

    flags = draw(
        st.lists(st.booleans(), min_size=len(ladder), max_size=len(ladder))
    )
    if not allow_none_available and not any(flags):
        flags[draw(st.integers(min_value=0, max_value=len(ladder) - 1))] = True

    noise_strategy = st_availability_map(max_size=4) if noise is None else noise
    availability: Dict[str, bool] = dict(draw(noise_strategy))
    # The ladder's own answers are authoritative; noise must not shadow them.
    availability.update(
        {f"font:{family}": bool(flag) for family, flag in zip(ladder, flags)}
    )

    available_families = tuple(
        family for family, flag in zip(ladder, flags) if flag
    )
    expected_font = available_families[0] if available_families else _FALLBACK_FONT

    return {
        "font_override": font_override,
        "preset_font": preset_font,
        "fallback_font": _FALLBACK_FONT,
        "ladder": tuple(ladder),
        "availability": availability,
        "default": draw(st.booleans()),
        "available_families": available_families,
        "expected_font": expected_font,
        "expected_marked": expected_font != ladder[0],
    }
