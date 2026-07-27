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

This first tranche (task 2.3) holds the generators that need no engine contract; the
second tranche (task 3.4) adds ``st_stage``, ``st_registrations`` and
``st_engine_outcomes`` once ``worker/engines/base.py`` exists.
"""
from __future__ import annotations

import string
from typing import Any, Dict, List, Sequence, Tuple

from hypothesis import strategies as st

from tests.conftest import FakeWord
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
    "LLM_CAPABILITY",
    "SAMPLE_RATES",
    "st_availability_map",
    "st_capability_id",
    "st_engine_id",
    "st_hostile_component",
    "st_hostile_value",
    "st_invalid_fps",
    "st_malformed_capability_id",
    "st_options_mapping",
    "st_priority",
    "st_segment_records",
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
