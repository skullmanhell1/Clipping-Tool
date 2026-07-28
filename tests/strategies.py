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
``st_i18n_word_timeline``, ``st_broken_word_timeline`` and ``st_font_availability``. The
fourth tranche (audio-stem-inpainting task 2.1) adds the thirteen stem generators every
stem property test imports: ``st_stem_options``, ``st_stem_gains``, ``st_mix_preset``,
``st_repair_mode``, ``st_repair_window_ms``, ``st_keep_plan``, ``st_seam_notes``,
``st_audio_format``, ``st_pcm_frames``, ``st_backend_stem_sets``, ``st_gate_scenarios``,
``st_failure_points`` and ``st_tiny_clip``.
"""
from __future__ import annotations

import math
import string
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from hypothesis import strategies as st

from tests.conftest import FakeWord
from worker.effects.filler import FillerPlan, Interval
from worker.engines.base import Engine_Artifact, Engine_Stage, Engine_Status
from worker.engines.timebase import (
    DEFAULT_FPS,
    DEFAULT_SAMPLE_RATE,
    MAX_FPS,
    MIN_FPS,
    Rounding,
    Time_Base,
)
from worker.ffmpeg_utils import FFmpegError

__all__ = [
    "BACKEND_IDS",
    "CAPABILITY_KINDS",
    "DEFAULT_SEGMENT_DURATION",
    "GAIN_DEFAULT",
    "GAIN_MAX",
    "GAIN_MIN",
    "KINETIC_STYLES",
    "LLM_CAPABILITY",
    "MIX_PRESETS",
    "MIX_PRESET_CHOICES",
    "REPAIR_MODES",
    "REVEAL_MODES",
    "SAMPLE_RATES",
    "STEM_MAPPING",
    "STEM_NAMES",
    "WINDOW_DEFAULT_MS",
    "WINDOW_MAX_MS",
    "WINDOW_MIN_MS",
    "st_audio_format",
    "st_availability_map",
    "st_backend_stem_sets",
    "st_broken_word_timeline",
    "st_capability_id",
    "st_engine_id",
    "st_engine_outcomes",
    "st_failure_points",
    "st_font_availability",
    "st_gate_scenarios",
    "st_hostile_component",
    "st_hostile_value",
    "st_i18n_word_timeline",
    "st_invalid_fps",
    "st_keep_plan",
    "st_kinetic_options",
    "st_kinetic_style",
    "st_malformed_capability_id",
    "st_mix_preset",
    "st_options_mapping",
    "st_pcm_frames",
    "st_priority",
    "st_registrations",
    "st_repair_mode",
    "st_repair_window_ms",
    "st_reveal_mode",
    "st_seam_notes",
    "st_segment_records",
    "st_stage",
    "st_stem_gains",
    "st_stem_options",
    "st_time_base",
    "st_tiny_clip",
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

        One documented blind spot (found by kinetic task 9.7, Property 17): when
        **no** ladder rung is available (``available_families == ()``) the engine
        uses the documented last rung ``"Arial"`` anyway *and still records the
        substitution marker*, because the resolved family is not actually
        installed. If ``ladder[0]`` happens to be ``"Arial"`` itself (an empty
        ``font_override`` with ``preset_font == "Arial"``), ``expected_font ==
        ladder[0]`` and this flag reads ``False`` while the engine records one
        marker. Consumers should therefore read the marker verdict as
        ``expected_marked or not available_families``. The flag is left as-is
        because it is a stability contract for the sibling specs.

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



# --------------------------------------------------------------------------- #
# Tranche 4 (audio-stem-inpainting task 2.1): the thirteen stem generators      #
# --------------------------------------------------------------------------- #
# NOTE ON DUPLICATED VOCABULARIES
# -------------------------------
# ``worker/engines/stems.py`` does not exist yet — audio-stem-inpainting task 4.1 creates
# it — so ``STEM_NAMES`` / ``STEM_MAPPING`` / ``MIX_PRESETS`` / ``REPAIR_MODES`` /
# ``BACKEND_IDS`` and the numeric bounds cannot be imported here without making every test
# collection depend on an unwritten module. They are therefore repeated below as literal
# constants, exactly like :data:`CAPABILITY_KINDS` mirrors ``Capability_Kind`` and
# :data:`KINETIC_STYLES` mirrors the kinetic vocabulary. **Keep them in sync.**
#
#   *** Task 4.1 MUST define exactly these values in ``worker/engines/stems.py``, and
#   *** task 4.x's first test MUST assert
#   ***     tuple(stems.STEM_NAMES)   == STEM_NAMES
#   ***     dict(stems.STEM_MAPPING)  == STEM_MAPPING
#   ***     stems.MIX_PRESETS         == MIX_PRESETS
#   ***     tuple(stems.REPAIR_MODES) == REPAIR_MODES
#   ***     tuple(stems.BACKEND_IDS)  == BACKEND_IDS
#   ***     (stems.GAIN_MIN, stems.GAIN_MAX, stems.GAIN_DEFAULT)
#   ***         == (GAIN_MIN, GAIN_MAX, GAIN_DEFAULT)
#   ***     (stems.WINDOW_MIN_MS, stems.WINDOW_MAX_MS, stems.WINDOW_DEFAULT_MS)
#   ***         == (WINDOW_MIN_MS, WINDOW_MAX_MS, WINDOW_DEFAULT_MS)
#   *** so this duplication cannot silently drift (the same pin the kinetic tranche uses).
#
# NOTE ON EMITTED SHAPES
# ----------------------
# ``st_stem_options`` and ``st_audio_format`` emit **plain dicts** (field mappings), never
# ``Stem_Options`` / ``Audio_Format`` instances: those dataclasses land in tasks 4.2/4.4,
# and a mapping is exactly what ``Stem_Options.parse`` / the ffprobe reader consume. Once
# 4.2 lands, callers write ``Stem_Options.parse(draw(st_stem_options()))``.
# ``st_keep_plan`` is the exception — it emits real ``worker.effects.filler.FillerPlan``
# objects built from real ``Interval``s, because both already exist in production.
#
# Everything in this tranche is pure and offline: no ffmpeg, no temp file, no network.
# ``st_tiny_clip`` yields only the *kwargs* for the ``make_video`` fixture, never a file.

#: The three Stem_Names, sorted — duplicated from stem task 4.1 (see note above).
STEM_NAMES: Tuple[str, ...] = ("music", "other", "vocals")

#: Backend_Stem name -> Stem_Name; ``drums`` and ``bass`` both collapse into ``music``.
STEM_MAPPING: Dict[str, str] = {
    "vocals": "vocals",
    "drums": "music",
    "bass": "music",
    "other": "other",
}

#: The three non-``custom`` Mix_Presets and their documented gain bundles.
MIX_PRESETS: Dict[str, Dict[str, float]] = {
    "speech_focus": {"vocals": 1.0, "music": 0.25, "other": 0.6},
    "music_focus": {"vocals": 0.25, "music": 1.0, "other": 0.8},
    "clean_speech": {"vocals": 1.0, "music": 0.0, "other": 0.0},
}

#: Every legal ``mix_preset`` option value, sorted: the three bundles plus ``custom``,
#: which means "use the individual gain fields".
MIX_PRESET_CHOICES: Tuple[str, ...] = tuple(sorted(("custom", *MIX_PRESETS)))

#: The three Repair_Modes, in the design's declared order.
REPAIR_MODES: Tuple[str, ...] = ("off", "crossfade", "spectral")

#: The three ``backend`` option values; a *resolved* backend is only ``ml`` or ``ffmpeg``.
BACKEND_IDS: Tuple[str, ...] = ("auto", "ml", "ffmpeg")

#: Gain bounds, inclusive, and the value a rejected gain falls back to.
GAIN_MIN, GAIN_MAX, GAIN_DEFAULT = 0.0, 4.0, 1.0

#: Repair-window bounds in milliseconds, inclusive, and the documented default.
WINDOW_MIN_MS, WINDOW_MAX_MS, WINDOW_DEFAULT_MS = 2, 120, 12

#: The Capability_Ids the stem engine declares (one required, the rest optional) plus
#: ``ffmpeg_filter:volume``, which the gain chain of every resolved path needs.
_STEM_CAPABILITIES: Tuple[str, ...] = (
    "binary:ffmpeg",
    "ffmpeg_filter:acrossfade",
    "ffmpeg_filter:afade",
    "ffmpeg_filter:alimiter",
    "ffmpeg_filter:highpass",
    "ffmpeg_filter:lowpass",
    "ffmpeg_filter:pan",
    "ffmpeg_filter:volume",
    "model:htdemucs",
    "python_pkg:demucs",
)

#: Budget gate thresholds (stem task 4.1's step reserves/minimums). Mirrored here only so
#: :func:`st_gate_scenarios` can straddle every one of them.
_REPAIR_MIN_S, _REMUX_MIN_S = 3.0, 2.0
_SEPARATION_MIN_S: Dict[str, float] = {"ml": 20.0, "ffmpeg": 4.0}

#: Remaining-budget values that sit just below / on / just above each gate threshold, so
#: no rung of the ladder is reachable only by luck.
_BUDGET_BREAKPOINTS: Tuple[float, ...] = (
    0.0,
    0.5,
    1.0,
    _REPAIR_MIN_S + _REMUX_MIN_S - 0.001,        # 4.999 -> rung 6 (abandon)
    _REPAIR_MIN_S + _REMUX_MIN_S,                # 5.0   -> repair-only is affordable
    _REPAIR_MIN_S + _REMUX_MIN_S + 0.001,
    _SEPARATION_MIN_S["ffmpeg"] + _REPAIR_MIN_S + _REMUX_MIN_S - 0.001,   # 8.999
    _SEPARATION_MIN_S["ffmpeg"] + _REPAIR_MIN_S + _REMUX_MIN_S,           # 9.0
    _SEPARATION_MIN_S["ml"] + _REPAIR_MIN_S + _REMUX_MIN_S - 0.001,       # 24.999
    _SEPARATION_MIN_S["ml"] + _REPAIR_MIN_S + _REMUX_MIN_S,               # 25.0
    45.0,
    90.0,                                        # the declared time_budget_s
)

#: Model names worth drawing: the documented default, a plausible sibling, and the empty
#: string (a legal JSON scalar that must not crash resolution).
_STEM_MODELS: Tuple[str, ...] = ("htdemucs", "htdemucs_ft", "mdx_extra", "")

#: The Seam_Note prefix the engine reads. No other prefix is ever parsed.
_SEAM_PREFIX = "filler_seam:"



# --------------------------------------------------------------------------- #
# Stem_Options field mappings, gains, presets, modes, windows                    #
# --------------------------------------------------------------------------- #
def _st_gain(*, allow_zero: bool = True) -> st.SearchStrategy[float]:
    """One in-range gain over ``[GAIN_MIN, GAIN_MAX]``.

    Both bounds and the neutral value are sampled explicitly (a pure ``floats`` draw
    almost never hits ``0.0``, ``1.0`` or ``4.0``, and all three are semantically
    special: ``0.0`` excludes the stem entirely, ``1.0`` is the no-op, ``4.0`` is the
    maximum boost).
    """
    landmarks = [0.25, 0.5, 0.6, 0.8, GAIN_DEFAULT, 1.5, 2.0, 3.0, GAIN_MAX]
    lowest = GAIN_MIN if allow_zero else 0.001
    if allow_zero:
        landmarks.insert(0, GAIN_MIN)
    return st.one_of(
        st.sampled_from(landmarks),
        st.floats(min_value=lowest, max_value=GAIN_MAX,
                  allow_nan=False, allow_infinity=False),
    )


@st.composite
def st_stem_options(
    draw,
    *,
    mix_presets: Sequence[str] = MIX_PRESET_CHOICES,
    repair_modes: Sequence[str] = REPAIR_MODES,
    backends: Sequence[str] = BACKEND_IDS,
    models: Sequence[str] = _STEM_MODELS,
    include_enabled: bool = False,
):
    """Valid ``Stem_Options`` **field mappings** spanning every declared bound.

    Returns a plain ``dict[str, Any]`` of JSON-native scalars carrying exactly the ten
    ``Stem_Options`` fields — ``mix_preset``, ``gain_vocals``, ``gain_music``,
    ``gain_other``, ``repair_mode``, ``repair_window_ms``, ``declick``, ``backend``,
    ``model``, ``retain_stems`` — and *not* a ``Stem_Options`` instance (see the tranche
    note above). Every value is already in range, so ``Stem_Options.parse`` is the
    identity on it, which is what makes the round-trip half of Property 3 and the
    idempotence half of Property 5 meaningful.

    Bounds covered, inclusive: gains over ``[0.0, 4.0]`` (``0.0``, ``1.0`` and ``4.0``
    all reachable), ``repair_window_ms`` over ``[2, 120]``; ``mix_preset`` /
    ``repair_mode`` / ``backend`` / ``model`` from their closed vocabularies.

    The eleventh field of the ProcessingOptions surface, ``stem_inpainting_enabled``, is
    the Feature_Flag rather than a ``Stem_Options`` field, so it is absent by default;
    pass ``include_enabled=True`` to have a drawn boolean added under that key (the shape
    ladder rung 0 and Property 8's flag-disabled half need).

    Consumed by stem Properties 3, 5, 8, 13, 14, 15, 16, 19, 20.
    """
    options: Dict[str, Any] = {
        "mix_preset": draw(st.sampled_from(list(mix_presets))),
        "gain_vocals": draw(_st_gain()),
        "gain_music": draw(_st_gain()),
        "gain_other": draw(_st_gain()),
        "repair_mode": draw(st.sampled_from(list(repair_modes))),
        "repair_window_ms": draw(
            st.one_of(
                st.integers(min_value=WINDOW_MIN_MS, max_value=WINDOW_MAX_MS),
                st.sampled_from([WINDOW_MIN_MS, WINDOW_DEFAULT_MS, WINDOW_MAX_MS]),
            )
        ),
        "declick": draw(st.booleans()),
        "backend": draw(st.sampled_from(list(backends))),
        "model": draw(st.sampled_from(list(models))),
        "retain_stems": draw(st.booleans()),
    }
    if include_enabled:
        options["stem_inpainting_enabled"] = draw(st.booleans())
    return options


@st.composite
def st_stem_gains(
    draw,
    *,
    stems: Sequence[str] = STEM_NAMES,
    allow_zero: bool = True,
    allow_boost: bool = True,
):
    """Per-Stem_Name resolved gains: ``dict[str, float]`` keyed by exactly ``stems``.

    Every value is finite and inside ``[0.0, 4.0]``. A zero and a boost (``> 1.0``) are
    each *forced* onto a random stem about half the time, so the two semantically
    interesting branches — "gain ``0.0`` excludes the stem from ``active_stems`` and from
    the filtergraph" (Req 5.7) and "boost must not clip" — are reached far more often
    than a uniform draw would reach them. An all-``1.0`` draw (the no-op configuration of
    Property 8) is also reachable.

    Pass ``allow_zero=False`` for a mapping with no excluded stem, or
    ``allow_boost=False`` to stay at or below unity.

    Consumed by stem Properties 11, 12, 18.
    """
    names = list(stems)
    gains: Dict[str, float] = {
        name: draw(_st_gain(allow_zero=allow_zero)) for name in names
    }
    if names and allow_zero and draw(st.booleans()):
        gains[draw(st.sampled_from(names))] = GAIN_MIN
    if names and allow_boost and draw(st.booleans()):
        gains[draw(st.sampled_from(names))] = draw(
            st.one_of(
                st.sampled_from([1.5, 2.0, GAIN_MAX]),
                st.floats(min_value=1.001, max_value=GAIN_MAX,
                          allow_nan=False, allow_infinity=False),
            )
        )
    return gains


def st_mix_preset() -> st.SearchStrategy[str]:
    """One of the four ``mix_preset`` values — ``clean_speech``, ``custom``,
    ``music_focus``, ``speech_focus`` (:data:`MIX_PRESET_CHOICES`); consumed by stem
    Property 11, which asserts a non-``custom`` preset yields exactly its
    :data:`MIX_PRESETS` bundle and ignores the individual gain fields."""
    return st.sampled_from(list(MIX_PRESET_CHOICES))


def st_repair_mode() -> st.SearchStrategy[str]:
    """``off``, ``crossfade`` or ``spectral`` (:data:`REPAIR_MODES`); consumed by stem
    Properties 7 and 12, and by the ``spectral``-without-``ml`` downgrade of rung 9."""
    return st.sampled_from(list(REPAIR_MODES))


#: ``repair_window_ms`` payloads that are *not* in-range integers: out of range on both
#: sides, non-integral, non-numeric, non-finite and wrong-typed. ``coerce_int`` + clamp
#: must turn every one of these into an integer inside ``[2, 120]`` without raising.
_HOSTILE_WINDOW_MS: Tuple[Any, ...] = (
    WINDOW_MIN_MS - 1,            # 1  -> clamps up
    0,
    -1,
    -1000,
    WINDOW_MAX_MS + 1,            # 121 -> clamps down
    1000,
    10 ** 9,
    -(10 ** 9),
    True,                         # bool is an int subclass: a classic coercion trap
    False,
    2.4,
    119.6,
    float("nan"),
    float("inf"),
    float("-inf"),
    None,
    "",
    " ",
    "12",
    "12.0",
    "abc",
    "nan",
    "inf",
    "1e400",
    "١٢",                         # Arabic-Indic digits
    [12],
    (12,),
    {"repair_window_ms": 12},
)


def st_repair_window_ms(*, valid_only: bool = False) -> st.SearchStrategy[Any]:
    """``repair_window_ms`` values: in-range integers **and** out-of-range integers and
    non-numerics.

    In-range draws span ``[2, 120]`` inclusive (both bounds sampled explicitly), where
    parsing is the identity; the hostile pool covers below-range, above-range, huge,
    non-integral, ``bool``, ``None``, empty/whitespace strings, numeric-looking strings,
    non-finite floats, non-ASCII digits and container values, so
    ``Stem_Options.parse``'s totality claim is actually tested. Pass
    ``valid_only=True`` for the in-range half alone (what Property 7 wants when it needs
    windows that are guaranteed to be planned).

    Consumed by stem Properties 4 and 7.
    """
    in_range = st.one_of(
        st.integers(min_value=WINDOW_MIN_MS, max_value=WINDOW_MAX_MS),
        st.sampled_from([WINDOW_MIN_MS, WINDOW_DEFAULT_MS, WINDOW_MAX_MS]),
    )
    if valid_only:
        return in_range
    hostile = st.sampled_from(list(_HOSTILE_WINDOW_MS)).map(_copied)
    return st.one_of(in_range, hostile)



# --------------------------------------------------------------------------- #
# Filler keep plans (the Seam source) and Seam_Notes (the Seam intake)           #
# --------------------------------------------------------------------------- #
@st.composite
def st_keep_plan(
    draw,
    *,
    min_keeps: int = 1,
    max_keeps: int = 8,
    allow_zero_length: bool = True,
    allow_adjacent: bool = True,
):
    """A real :class:`worker.effects.filler.FillerPlan` whose ``keeps`` are real
    :class:`worker.effects.filler.Interval` objects.

    ``keeps`` is always sorted, non-overlapping and non-negative — the shape
    ``plan_keep_intervals`` produces and the shape ``filler_seam_notes`` consumes, so
    Property 6 (``N`` keeps publish exactly ``N − 1`` interior Seam notes, the *i*-th
    being ``round(Σ_{j≤i} keeps[j].duration, 3)``) can be asserted directly on the drawn
    plan. Every bound is rounded to 3 decimals, matching ``rebase_words``' rounding.

    Cases guaranteed reachable: **single keep** (``N == 1`` ⇒ zero notes), **adjacent**
    keeps (a zero gap, so one keep's ``end`` is the next one's ``start``), **zero-length**
    keeps (``start == end``, contributing ``0.0`` to the running sum, which is what makes
    "no note equals the tightened duration" non-trivial) and **many keeps** (up to
    ``max_keeps``). Pass ``min_keeps=0`` for the empty plan, or
    ``allow_zero_length=False`` / ``allow_adjacent=False`` to suppress those cases.

    ``removed_fillers`` is a drawn count and ``removed_seconds`` is the total removed gap
    (rounded to 3), so ``FillerPlan.changed`` is meaningful: a plan built from all-zero
    gaps reports no change.

    Consumed by stem Property 6.
    """
    n = draw(st.integers(min_value=min_keeps, max_value=max_keeps))
    gap_pool = [st.floats(min_value=0.01, max_value=0.8,
                          allow_nan=False, allow_infinity=False)]
    if allow_adjacent:
        gap_pool.append(st.just(0.0))
    length_pool = [st.floats(min_value=0.02, max_value=2.0,
                             allow_nan=False, allow_infinity=False)]
    if allow_zero_length:
        length_pool.append(st.just(0.0))

    keeps: List[Interval] = []
    cursor = draw(st.floats(min_value=0.0, max_value=0.5,
                            allow_nan=False, allow_infinity=False))
    removed = 0.0
    for _ in range(n):
        gap = draw(st.one_of(gap_pool))
        length = draw(st.one_of(length_pool))
        start = round(cursor + gap, 3)
        end = round(start + length, 3)
        keeps.append(Interval(start, end))
        removed += gap
        cursor = end
    return FillerPlan(
        keeps=keeps,
        removed_fillers=draw(st.integers(min_value=0, max_value=max(n, 1) * 2)),
        removed_seconds=round(removed, 3),
    )


#: Notes emitted by *other* producers: the host's own markers, sibling engines' markers
#: and the existing pipeline's effect names. The stem engine must read none of them.
_FOREIGN_NOTES: Tuple[str, ...] = (
    "",
    " ",
    "filler_removal",
    "music:calm",
    "captions",
    "engine:kinetic_typography:applied",
    "engine:kinetic_typography:degraded:font:Inter",
    "engine:stem_inpainting:applied:ml",
    "engine:stem_inpainting:repair:crossfade:2",
    "unavailable:binary:ffmpeg",
    "seam:1.500",
    "🎬",
)

#: Notes whose *prefix* is malformed: wrong separator, wrong case, wrong spelling,
#: leading/trailing whitespace, missing or unparseable payload, doubled payload.
_MALFORMED_SEAM_NOTES: Tuple[str, ...] = (
    "filler_seam",
    "filler_seam:",
    "filler_seam::1.000",
    "filler_seam=1.000",
    "filler_seam 1.000",
    "fillerseam:1.000",
    "filler_seams:1.000",
    "FILLER_SEAM:1.000",
    "Filler_Seam:1.000",
    " filler_seam:1.000",
    "filler_seam:1.000 ",
    "filler_seam:1.000:2.000",
    "filler_seam:abc",
    "filler_seam:1,000",
    "filler_seam:1.0.0",
    "filler_seam:٣",
    "filler_seam:0x10",
    "filler_seam:1_000",
    "filler_seam:+1.000",
    "prefix_filler_seam:1.000",
)

#: Notes with the *right* prefix and a non-finite or negative payload — each one must be
#: discarded individually, leaving its well-formed neighbours alone.
_NON_FINITE_SEAM_NOTES: Tuple[str, ...] = (
    "filler_seam:nan",
    "filler_seam:NaN",
    "filler_seam:-nan",
    "filler_seam:inf",
    "filler_seam:-inf",
    "filler_seam:Infinity",
    "filler_seam:1e400",
    "filler_seam:-0.001",
    "filler_seam:-1.000",
    "filler_seam:-1000.000",
)


def _seam_value(note: Any, duration: float) -> float | None:
    """The value a well-formed, in-bounds ``filler_seam:<float>`` note carries, else
    ``None``.

    A deliberately minimal *oracle* for :func:`st_seam_notes`, mirroring the documented
    intake rule (exact ``filler_seam:`` prefix, ``float()`` parses the whole remainder,
    the value is finite and ``0 <= value <= duration``) — not a copy of
    ``parse_seam_notes``, which the property under test is what actually validates.
    """
    if not isinstance(note, str) or not note.startswith(_SEAM_PREFIX):
        return None
    try:
        value = float(note[len(_SEAM_PREFIX):])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0.0 or value > duration:
        return None
    return value


@st.composite
def st_seam_notes(
    draw,
    *,
    duration: float = None,
    min_valid: int = 0,
    max_valid: int = 5,
    max_hostile: int = 5,
    include_hostile: bool = True,
):
    """Seam_Note tuples: valid ``filler_seam:<float>`` notes mixed with hostile ones.

    Returns a plain ``dict`` whose keys are a stability contract:

    ``notes``
        ``tuple[str, ...]`` in **arbitrary order** — exactly what
        ``Engine_Context.notes`` looks like. Contains valid notes (including the ``0.0``
        and ``duration`` boundary values, and sometimes an exact duplicate of one of
        them), plus — unless ``include_hostile=False`` — malformed prefixes
        (:data:`_MALFORMED_SEAM_NOTES`), non-finite and negative payloads
        (:data:`_NON_FINITE_SEAM_NOTES`), out-of-bounds values (``> duration``), other
        producers' notes (:data:`_FOREIGN_NOTES`) and free-form text.
    ``duration``
        the clip duration the bounds check is against; pass ``duration=`` to pin it.
    ``expected_seams``
        ``tuple[float, ...]``: the values a total, order-preserving,
        duplicate-preserving intake must keep, in ``notes`` order — the oracle for
        Property 7's "exactly the finite, in-bounds values, no inferred extras" claim.
        Computed by :func:`_seam_value` over the emitted tuple, so a note from the
        hostile pool that happens to be well formed is classified correctly. An
        implementation that sorts or de-duplicates should compare
        ``sorted(set(...))`` on both sides.
    ``valid_count``
        ``len(expected_seams)``, handy for the ``repair:<mode>:<n>`` marker count.

    Consumed by stem Properties 7, 12, 18.
    """
    span = (
        round(draw(st.floats(min_value=0.5, max_value=60.0,
                             allow_nan=False, allow_infinity=False)), 3)
        if duration is None
        else float(duration)
    )

    def _note(value: float) -> str:
        return f"{_SEAM_PREFIX}{value:.3f}"

    valid: List[str] = [
        _note(value)
        for value in draw(
            st.lists(
                st.one_of(
                    st.floats(min_value=0.0, max_value=span,
                              allow_nan=False, allow_infinity=False),
                    st.sampled_from([0.0, span, round(span / 2.0, 3)]),
                ),
                min_size=min_valid,
                max_size=max_valid,
            )
        )
    ]
    notes: List[str] = list(valid)
    if valid and draw(st.booleans()):
        notes.append(draw(st.sampled_from(valid)))      # duplicate seam

    if include_hostile:
        out_of_bounds = st.sampled_from(
            [
                _note(span + 0.001),
                _note(span + 1.0),
                _note(span * 10.0 + 1.0),
                f"{_SEAM_PREFIX}1000000000.000",
            ]
        )
        notes.extend(
            draw(
                st.lists(
                    st.one_of(
                        st.sampled_from(list(_MALFORMED_SEAM_NOTES)),
                        st.sampled_from(list(_NON_FINITE_SEAM_NOTES)),
                        out_of_bounds,
                        st.sampled_from(list(_FOREIGN_NOTES)),
                        st.text(max_size=24),
                    ),
                    max_size=max_hostile,
                )
            )
        )

    ordered = tuple(draw(st.permutations(notes)))
    expected = tuple(
        value
        for value in (_seam_value(note, span) for note in ordered)
        if value is not None
    )
    return {
        "notes": ordered,
        "duration": span,
        "expected_seams": expected,
        "valid_count": len(expected),
    }



# --------------------------------------------------------------------------- #
# Audio format mappings and tiny PCM buffers                                    #
# --------------------------------------------------------------------------- #
#: Audio codec names ``ffprobe`` realistically reports for a short-form clip.
_AUDIO_CODECS: Tuple[str, ...] = (
    "aac",
    "pcm_s16le",
    "mp3",
    "opus",
    "vorbis",
    "flac",
    "ac3",
)

#: Channel counts worth exercising: mono, stereo and one surround layout.
_AUDIO_CHANNELS: Tuple[int, ...] = (1, 2, 6)

#: The four ``Audio_Format`` field names, in the order ``ffprobe`` is asked for them.
_AUDIO_FORMAT_FIELDS: Tuple[str, ...] = ("sample_rate", "channels", "codec", "start_time")

#: Per-field hostile payloads. ``ffprobe`` hands every value back as a JSON *string*, so
#: ``"44100"`` is realistic rather than exotic; the rest are the missing / zero /
#: negative / non-finite / wrong-type traps Req 17.5 has to survive.
_HOSTILE_AUDIO_VALUES: Dict[str, Tuple[Any, ...]] = {
    "sample_rate": (0, -1, -44100, 0.0, 44100.5, "44100", "0", "-48000", "", "N/A",
                    "abc", None, True, float("nan"), float("inf"), 10 ** 12, [44100]),
    "channels": (0, -1, -2, 0.0, 2.5, "2", "0", "-1", "", "N/A", "abc", None, True,
                 float("nan"), float("inf"), 10 ** 6, {"channels": 2}),
    "codec": ("", " ", "N/A", "unknown", None, 0, 1, True, [], {}, "🎬", "a" * 200),
    "start_time": (-1.0, -0.5, "0.000000", "N/A", "", None, True, float("nan"),
                   float("inf"), float("-inf"), 10 ** 9, [0.0]),
}


@st.composite
def st_audio_format(
    draw,
    *,
    valid_only: bool = False,
    allow_missing: bool = True,
    sample_rates: Sequence[int] = SAMPLE_RATES,
    channels: Sequence[int] = _AUDIO_CHANNELS,
    codecs: Sequence[str] = _AUDIO_CODECS,
):
    """``Audio_Format`` **field mappings**: valid combinations plus missing, zero,
    negative, non-finite and wrong-typed values.

    Returns a plain ``dict`` (see the tranche note above — the frozen ``Audio_Format``
    dataclass lands in task 4.4) keyed by ``sample_rate``, ``channels``, ``codec`` and
    ``start_time``. A key may be **absent** entirely, which is how a missing ``ffprobe``
    entry reaches the reader.

    A drawn mapping is *valid* exactly when ``sample_rate`` and ``channels`` are present
    integers ``>= 1``; everything else must be rejected as ``Invalid_Audio_Format``
    (rung 5, ``degraded:audio_format``). Sample rates come from the foundation's
    :data:`SAMPLE_RATES`, so the format lines up with :func:`st_time_base`.
    ``start_time`` covers ``0.0`` and small positive offsets, the value that decides
    whether the remux emits ``-itsoffset`` at all.

    Pass ``valid_only=True`` for the always-valid half (what Property 10 needs when it
    asserts stems preserve the format), or ``allow_missing=False`` to keep all four keys
    present while still corrupting values.

    Consumed by stem Properties 10, 15, 18.
    """
    fmt: Dict[str, Any] = {
        "sample_rate": draw(st.sampled_from(list(sample_rates))),
        "channels": draw(st.sampled_from(list(channels))),
        "codec": draw(st.sampled_from(list(codecs))),
        "start_time": draw(
            st.one_of(
                st.just(0.0),
                st.sampled_from([0.0, 0.001, 0.023, 0.5]),
                st.floats(min_value=0.0, max_value=2.0,
                          allow_nan=False, allow_infinity=False),
            )
        ),
    }
    if valid_only:
        return fmt

    kinds = ["keep", "keep", "keep", "hostile"]
    if allow_missing:
        kinds.append("missing")
    for field_name in _AUDIO_FORMAT_FIELDS:
        kind = draw(st.sampled_from(kinds))
        if kind == "missing":
            fmt.pop(field_name, None)
        elif kind == "hostile":
            fmt[field_name] = _copied(
                draw(st.sampled_from(list(_HOSTILE_AUDIO_VALUES[field_name])))
            )
    return fmt


#: The PCM content shapes worth drawing. ``anti_phase`` forces 2 channels (it is defined
#: by ``c1 == -c0``, which is exactly the content the ffmpeg backend's ``pan`` mid
#: extraction cancels to silence); ``silence`` is the Req 16.7 "silence in ⇒ silence out"
#: case; ``full_scale`` sits on ±1.0 so any gain > 1.0 must be shown not to wrap.
_PCM_KINDS: Tuple[str, ...] = (
    "silence",
    "dc_offset",
    "full_scale",
    "anti_phase",
    "impulse",
    "ramp",
    "alternating",
    "drawn",
)


@st.composite
def st_pcm_frames(
    draw,
    *,
    kinds: Sequence[str] = _PCM_KINDS,
    channels: int = None,
    max_frames: int = 32,
    sample_rates: Sequence[int] = SAMPLE_RATES,
):
    """Tiny float frame buffers, including silence, anti-phase and full-scale content.

    Returns a plain ``dict`` whose keys are a stability contract:

    ``kind``
        which shape was drawn, one of :data:`_PCM_KINDS`.
    ``channels``
        ``1``, ``2`` or ``6`` — forced to ``2`` for ``kind == "anti_phase"``.
    ``sample_rate``
        a :data:`SAMPLE_RATES` member, so the buffer can be paired with a
        :func:`st_audio_format` mapping or written as a WAV header by a test double.
    ``frames``
        ``tuple[tuple[float, ...], ...]``: one interleaved frame per element, each of
        length ``channels``, every sample a finite float in ``[-1.0, 1.0]``. Never empty
        and never longer than ``max_frames`` — these are deliberately *tiny* buffers so a
        property can compare them sample-by-sample against ``AMPLITUDE_TOLERANCE``.
    ``peak``
        ``max(abs(sample))`` over the buffer, i.e. the full-scale headroom oracle for
        Property 12's "no sample exceeds full scale".

    Pure Python floats only — no numpy, no audio file, no ffmpeg (Req 19.5).

    Consumed by stem Properties 10, 12, 19, 20.
    """
    kind = draw(st.sampled_from(list(kinds)))
    count = 2 if kind == "anti_phase" else (
        channels if channels is not None else draw(st.sampled_from(list(_AUDIO_CHANNELS)))
    )
    length = draw(st.integers(min_value=1, max_value=max_frames))
    sample = st.floats(min_value=-1.0, max_value=1.0,
                       allow_nan=False, allow_infinity=False)

    frames: List[Tuple[float, ...]] = []
    if kind == "silence":
        frames = [(0.0,) * count for _ in range(length)]
    elif kind == "dc_offset":
        level = draw(sample)
        frames = [(level,) * count for _ in range(length)]
    elif kind == "full_scale":
        frames = [
            ((1.0,) * count if index % 2 == 0 else (-1.0,) * count)
            for index in range(length)
        ]
    elif kind == "anti_phase":
        values = draw(st.lists(sample, min_size=length, max_size=length))
        frames = [(value, -value) for value in values]
    elif kind == "impulse":
        hit = draw(st.integers(min_value=0, max_value=length - 1))
        frames = [
            ((1.0,) * count if index == hit else (0.0,) * count)
            for index in range(length)
        ]
    elif kind == "ramp":
        frames = [
            ((-1.0 + 2.0 * index / max(length - 1, 1)),) * count
            for index in range(length)
        ]
    elif kind == "alternating":
        level = draw(sample)
        frames = [
            ((level,) * count if index % 2 == 0 else (-level,) * count)
            for index in range(length)
        ]
    else:                                   # "drawn": arbitrary in-range content
        frames = [
            tuple(draw(st.lists(sample, min_size=count, max_size=count)))
            for _ in range(length)
        ]

    buffer = tuple(frames)
    peak = max((abs(value) for frame in buffer for value in frame), default=0.0)
    return {
        "kind": kind,
        "channels": count,
        "sample_rate": draw(st.sampled_from(list(sample_rates))),
        "frames": buffer,
        "peak": peak,
    }


# --------------------------------------------------------------------------- #
# Backend stem mappings                                                         #
# --------------------------------------------------------------------------- #
#: The four Backend_Stems ``htdemucs`` emits, sorted.
_FOUR_STEM_NAMES: Tuple[str, ...] = ("bass", "drums", "other", "vocals")

#: The two-Backend_Stem shapes, both spellings (see the DIVERGENCE note in
#: :func:`st_backend_stem_sets`): the design's ffmpeg adapter emits ``vocals`` + ``music``
#: and omits ``other``, while ``tests.fakes.Missing_Stem_Backend(missing=("bass",
#: "drums"))`` — documented there as "the ffmpeg adapter's two-stem shape" — leaves
#: ``vocals`` + ``other``. Both are drawn, so a property is blind to neither.
_TWO_STEM_SHAPES: Tuple[Tuple[str, ...], ...] = (
    ("music", "vocals"),
    ("other", "vocals"),
)

#: Backend_Stem names outside :data:`STEM_MAPPING`: other separators' vocabularies, case
#: and whitespace variants, and outright junk. None of them may reach the Stem_Set.
_UNKNOWN_STEM_NAMES: Tuple[str, ...] = (
    "guitar",
    "piano",
    "accompaniment",
    "no_vocals",
    "Vocals",
    "VOCALS",
    " vocals",
    "vocals ",
    "vocals2",
    "",
    "..",
    "🎬",
)


def _stem_target(name: Any) -> str | None:
    """The Stem_Name a Backend_Stem contributes to, or ``None`` for an unknown name.

    :data:`STEM_MAPPING` first, then **identity for a name that is already a Stem_Name**.
    The identity fallback is not decoration: the ffmpeg adapter emits Backend_Stems called
    ``vocals`` and ``music``, and ``music`` has no :data:`STEM_MAPPING` key of its own
    (see the DIVERGENCE note in :func:`st_backend_stem_sets`).
    """
    if not isinstance(name, str):
        return None
    target = STEM_MAPPING.get(name)
    if target is not None:
        return target
    return name if name in STEM_NAMES else None


@st.composite
def st_backend_stem_sets(
    draw,
    *,
    dest: Path = Path("stems_raw"),
    include_unknown: bool = True,
    allow_empty: bool = True,
):
    """Backend stem mappings: four-stem, two-stem, unknown-name and omission cases, in
    **arbitrary dict order**.

    Returns a plain ``dict`` whose keys are a stability contract:

    ``kind``
        ``"four_stem"``, ``"two_stem"``, ``"unknown_names"`` or ``"omission"``.
    ``raw``
        ``dict[str, Path]`` — the ``{Backend_Stem name: wav path}`` mapping a
        ``Separator_Backend.separate`` returns. Built in a **permuted** key order, which
        is the whole point: Req 4.9 says the assembled Stem_Set and the emitted
        filtergraph must be identical across permutations. Paths are workspace-relative
        and are never created on disk.
    ``expected_contributors``
        ``dict[str, tuple[str, ...]]`` — for each covered Stem_Name, the sorted
        Backend_Stem names that :data:`STEM_MAPPING` routes into it, so
        ``drums`` + ``bass`` → ``music`` (the summing case) is checkable directly.
        Unknown names contribute to nothing and never appear here.
    ``expected_missing``
        ``tuple[str, ...]``, sorted — the Stem_Names with no contributor, each of which
        owes exactly one silent file of the clip's duration and one
        ``stem_missing:<name>`` marker.
    ``expected_keys``
        always ``STEM_NAMES``: the assembled Stem_Set is exactly three stems, whatever
        the backend returned.

    DIVERGENCE from the design, deliberately encoded here: the ffmpeg adapter emits the
    Backend_Stems ``vocals`` and ``music`` (``music := clip − vocals``), but the designed
    :data:`STEM_MAPPING` has keys ``vocals``/``drums``/``bass``/``other`` only — there is
    no ``music`` key. The oracle therefore resolves a name through
    :func:`_stem_target`: ``STEM_MAPPING`` first, then identity when the name is already a
    Stem_Name. Task 4.1 must either add ``"music": "music"`` to ``STEM_MAPPING`` or
    document that identity fallback in ``assemble_stem_set``; otherwise the ffmpeg
    backend's ``music`` stem would be silently discarded and replaced with silence.

    Consumed by stem Property 9.
    """
    kind = draw(st.sampled_from(["four_stem", "two_stem", "unknown_names", "omission"]))
    if kind == "four_stem":
        names = list(_FOUR_STEM_NAMES)
    elif kind == "two_stem":
        names = list(draw(st.sampled_from([list(shape) for shape in _TWO_STEM_SHAPES])))
    elif kind == "unknown_names":
        names = draw(
            st.lists(
                st.sampled_from(list(_FOUR_STEM_NAMES)),
                min_size=0,
                max_size=4,
                unique=True,
            )
        )
        names += draw(
            st.lists(
                st.sampled_from(list(_UNKNOWN_STEM_NAMES) if include_unknown
                                else list(_FOUR_STEM_NAMES)),
                min_size=1,
                max_size=3,
                unique=True,
            )
        )
    else:                                   # "omission": an arbitrary subset, maybe empty
        names = draw(
            st.lists(
                st.sampled_from(list(_FOUR_STEM_NAMES)),
                min_size=0 if allow_empty else 1,
                max_size=4,
                unique=True,
            )
        )

    ordered = list(draw(st.permutations(list(dict.fromkeys(names)))))
    raw: Dict[str, Path] = {name: dest / f"{name}.wav" for name in ordered}

    contributors: Dict[str, List[str]] = {}
    for name in ordered:
        target = _stem_target(name)
        if target is None:
            continue                        # unknown Backend_Stem: contributes nothing
        contributors.setdefault(target, []).append(name)

    return {
        "kind": kind,
        "raw": raw,
        "expected_contributors": {
            stem: tuple(sorted(sources)) for stem, sources in sorted(contributors.items())
        },
        "expected_missing": tuple(
            stem for stem in STEM_NAMES if stem not in contributors
        ),
        "expected_keys": STEM_NAMES,
    }



# --------------------------------------------------------------------------- #
# Gate scenarios and forced failure points                                      #
# --------------------------------------------------------------------------- #
#: Every documented forced failure point, mapped to the rung it must land on. ``oserror``
#: is the one row of the error table that does **not** change the status: the engine
#: records it in ``Engine_Result.detail`` and keeps going (Req 11.6).
_FAILURE_KINDS: Tuple[str, ...] = (
    "backend_raises",
    "backend_truncates",
    "backend_non_audio",
    "ffmpeg_error",
    "timeout",
    "integrity_failure",
    "oserror",
)


def _failure_exception(kind: str) -> BaseException | None:
    """The exception instance a forced failure point raises, or ``None`` when the failure
    is expressed as bad *output* rather than as a raise."""
    if kind == "backend_raises":
        return RuntimeError("separation failed")
    if kind == "ffmpeg_error":
        return FFmpegError("ffmpeg exited 1")
    if kind == "timeout":
        return subprocess.TimeoutExpired(cmd=["ffmpeg", "-i", "in.wav"], timeout=1.0)
    if kind == "oserror":
        return OSError(28, "No space left on device")
    return None


@st.composite
def st_failure_points(
    draw,
    *,
    kinds: Sequence[str] = _FAILURE_KINDS,
    max_call_index: int = 3,
):
    """Forced failure points across the whole documented taxonomy.

    Returns a plain ``dict`` whose keys are a stability contract:

    ``kind``
        one of :data:`_FAILURE_KINDS` — backend raising, backend truncating (wrong
        duration), backend returning a non-audio file, ``FFmpegError`` from any
        invocation, subprocess timeout, integrity-verification failure, or ``OSError`` on
        a workspace write/delete.
    ``step``
        where it strikes: ``"separate"``, ``"extract"``, ``"repair"``, ``"remux"`` or
        ``"verify"``. Drawn for the ffmpeg/timeout/OSError kinds, fixed for the
        backend-output kinds.
    ``double``
        the name of the ``tests.fakes`` double (stem task 2.2) the property should
        install: ``Raising_Separator_Backend``, ``Truncating_Separator_Backend``,
        ``Fake_Separator_Backend`` or ``Recording_Command_Runner``. A *name*, not a class,
        so this tranche neither imports nor pins the doubles. Note that
        ``Fake_Separator_Backend`` has **no** non-audio switch, so
        ``kind == "backend_non_audio"`` means "install the plain fake and overwrite the
        stem file it returned with a non-audio payload" — the double writes real WAVs by
        design, and corrupting the output is the test's job.
    ``call_index``
        which recorded invocation the ``Recording_Command_Runner`` should fail at
        (``0 .. max_call_index``) — feed it to that double's ``fail_at`` /
        ``timeout_at`` keyword. Meaningless for the backend-output kinds and always ``0``
        there.
    ``exception``
        a real exception **instance** where the failure is a raise —
        ``RuntimeError``, ``worker.ffmpeg_utils.FFmpegError``,
        ``subprocess.TimeoutExpired``, ``OSError(ENOSPC)`` — else ``None``
        (``backend_truncates`` / ``backend_non_audio`` / ``integrity_failure`` are bad
        *output*, detected by verification rather than raised).
    ``expected_status``
        ``"failed"``, ``"degraded"``, or ``None`` meaning "unchanged — the run continues"
        (the ``oserror`` row).
    ``expected_marker``
        ``"failed"``, ``"timeout"`` or ``None``; the marker detail the rung owes, before
        ``base.marker`` namespaces it.
    ``expects_media``
        ``False`` for every raising/verification kind (no Replacement_Media may be
        returned and nothing partial may survive); ``None`` for ``oserror``, whose media
        outcome is deliberately unconstrained because the engine keeps going.

    Consumed by stem Property 16 (and by Property 15's forced-failure axis through
    :func:`st_gate_scenarios`).
    """
    kind = draw(st.sampled_from(list(kinds)))
    if kind in ("backend_raises", "backend_truncates", "backend_non_audio"):
        step = "separate"
        call_index = 0
        double = {
            "backend_raises": "Raising_Separator_Backend",
            "backend_truncates": "Truncating_Separator_Backend",
            "backend_non_audio": "Fake_Separator_Backend",
        }[kind]
    elif kind == "integrity_failure":
        step = "verify"
        call_index = 0
        double = "Recording_Command_Runner"
    else:
        step = draw(st.sampled_from(["extract", "separate", "repair", "remux"]))
        call_index = draw(st.integers(min_value=0, max_value=max_call_index))
        double = "Recording_Command_Runner"

    expected_status = "degraded" if kind == "timeout" else (
        None if kind == "oserror" else "failed"
    )
    expected_marker = {"timeout": "timeout", "oserror": None}.get(kind, "failed")
    return {
        "kind": kind,
        "step": step,
        "double": double,
        "call_index": call_index,
        "exception": _failure_exception(kind),
        "expected_status": expected_status,
        "expected_marker": expected_marker,
        "expects_media": None if kind == "oserror" else False,
    }


@st.composite
def st_gate_scenarios(
    draw,
    *,
    capabilities: Sequence[str] = _STEM_CAPABILITIES,
    noise: st.SearchStrategy = None,
    enabled: bool = None,
    allow_failure: bool = True,
    default: bool = None,
):
    """Capability availability × remaining budget × forced failure — one row of the
    degradation ladder's input space.

    Returns a plain ``dict`` whose keys are a stability contract:

    ``enabled``
        the ``stem_inpainting_enabled`` Feature_Flag. ``False`` is rung 0: the engine body
        is never invoked, no workspace, no probe, no pass.
    ``availability``
        ``dict[str, bool]`` keyed by **capability id**, ready to hand straight to
        ``tests.fakes.StaticProber(mapping, default=...)``. Every id in ``capabilities``
        (``binary:ffmpeg``, ``python_pkg:demucs``, ``model:htdemucs`` and the
        ``ffmpeg_filter:*`` set) gets an explicit answer, and the foundation
        :func:`st_availability_map` is composed in as **noise**: unrelated ids are merged
        first, so the stem ids always win.
    ``default``
        the ``StaticProber`` answer for any id absent from ``availability``.
    ``remaining_s``
        what ``ctx.remaining()`` reports, drawn from :data:`_BUDGET_BREAKPOINTS` (which
        straddles ``REPAIR_MIN_S + REMUX_MIN_S = 5.0``, the ffmpeg separation gate at
        ``9.0`` and the ml gate at ``25.0``, each from just below to just above) or from a
        continuous ``[0, 120]`` draw, so rungs 6 and 7 are both reachable.
    ``permissibility``
        ``ctx.permissibility``; combined with ``requires_network`` it is rung 2.
    ``requires_network``
        what the injected backend declares. ``True`` + ``permissibility`` ⇒
        ``permissibility_blocked`` and no media, whatever else holds.
    ``has_audio``
        ``False`` is rung 4: skip with **no** marker at all.
    ``audio_format``
        a :func:`st_audio_format` mapping, or ``None`` exactly when ``has_audio`` is
        ``False``. An invalid mapping (missing/zero/negative sample rate or channels) is
        rung 5, ``degraded:audio_format``.
    ``options``
        a valid :func:`st_stem_options` mapping, so the gains/repair-mode no-op of rung 3
        and the ``spectral``-downgrade of rung 9 are both reachable.
    ``failure``
        a :func:`st_failure_points` mapping, or ``None`` for a clean run. Pass
        ``allow_failure=False`` to keep every scenario failure-free.

    Consumed by stem Properties 15 and 17.
    """
    availability: Dict[str, bool] = dict(
        draw(st_availability_map(max_size=4) if noise is None else noise)
    )
    availability.update(
        {
            capability: draw(st.booleans())
            for capability in capabilities
        }
    )
    has_audio = draw(st.booleans())
    return {
        "enabled": draw(st.booleans()) if enabled is None else bool(enabled),
        "availability": availability,
        "default": draw(st.booleans()) if default is None else bool(default),
        "remaining_s": draw(
            st.one_of(
                st.sampled_from(list(_BUDGET_BREAKPOINTS)),
                st.floats(min_value=0.0, max_value=120.0,
                          allow_nan=False, allow_infinity=False),
            )
        ),
        "permissibility": draw(st.booleans()),
        "requires_network": draw(st.booleans()),
        "has_audio": has_audio,
        "audio_format": draw(st_audio_format()) if has_audio else None,
        "options": draw(st_stem_options()),
        "failure": (
            draw(st.one_of(st.none(), st_failure_points()))
            if allow_failure
            else None
        ),
    }


# --------------------------------------------------------------------------- #
# Tiny clip parameters for the ``make_video`` fixture                            #
# --------------------------------------------------------------------------- #
#: File names the ``make_video`` fixture can write under ``tmp_path``: plain, portable,
#: always ``.mp4`` (the fixture encodes H.264 + AAC).
_TINY_CLIP_NAMES: Tuple[str, ...] = ("src.mp4", "clip.mp4", "tiny.mp4", "in.mp4")

#: ``(width, height)`` pairs — every value even, as ``yuv420p`` requires: tiny landscape,
#: square, 16:9 and 9:16 portrait (the short-form target).
_TINY_CLIP_SIZES: Tuple[Tuple[int, int], ...] = (
    (128, 128),
    (160, 120),
    (192, 108),
    (256, 144),
    (320, 240),
    (320, 568),
    (360, 640),
)


@st.composite
def st_tiny_clip(
    draw,
    *,
    names: Sequence[str] = _TINY_CLIP_NAMES,
    sizes: Sequence[Tuple[int, int]] = _TINY_CLIP_SIZES,
    audio: bool = None,
    min_duration: float = 0.3,
    max_duration: float = 2.0,
):
    """Tiny-clip **parameters** for the ``make_video`` fixture — never a file.

    Returns a plain ``dict`` of exactly the fixture's keyword arguments —
    ``name``, ``duration``, ``w``, ``h``, ``audio`` — so a test writes::

        clip = make_video(**draw(st_tiny_clip()))

    ``make_video`` is a *fixture* (a factory), not an importable function, and it needs
    ffmpeg; this generator stays pure and offline by emitting only the kwargs, which is
    why it can be drawn inside a ``@given`` body under ``requires_ffmpeg`` without the
    module ever touching a subprocess. Keep the drawn clip tiny: the fixture encodes at
    30 fps, so ``duration`` stays in ``[0.3, 2.0]`` seconds and the frame sizes are all
    even (``yuv420p``) and small.

    ``audio`` is drawn by default and ``False`` is meaningful — an audio-less clip is
    ladder rung 4, the no-audio ``skipped`` with no marker. Pass ``audio=True`` for the
    integrity properties that need a soundtrack.

    Consumed by stem Properties 13 and 14 (both ``requires_ffmpeg``).
    """
    width, height = draw(st.sampled_from(list(sizes)))
    duration = draw(
        st.one_of(
            st.sampled_from([value for value in (0.3, 0.5, 1.0, 1.5, 2.0)
                             if min_duration <= value <= max_duration]
                            or [min_duration]),
            st.floats(min_value=min_duration, max_value=max_duration,
                      allow_nan=False, allow_infinity=False),
        )
    )
    return {
        "name": draw(st.sampled_from(list(names))),
        "duration": round(float(duration), 2),
        "w": width,
        "h": height,
        "audio": draw(st.booleans()) if audio is None else bool(audio),
    }
