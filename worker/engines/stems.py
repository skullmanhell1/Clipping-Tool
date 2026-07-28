"""Stem-aware audio repair for clips (engine_id ``stem_inpainting``) — vocabularies.

This module is the home of the Stem_Engine. Landed so far: its vocabularies and
documented constants (task 4.1), the :class:`Stem_Options` value object (task 4.2),
option resolution — :meth:`Stem_Options.from_processing_options` plus the module-level
:func:`resolve_stem_options` the engine's ``resolve_options`` delegates to (task 4.3) —
the three plan records :class:`Audio_Format`, :class:`Repair_Window` and
:class:`Stem_Plan` (task 4.4), and the whole pure planner — :func:`resolve_gains`,
:func:`parse_seam_notes`, :func:`repair_windows`, :func:`resolve_backend`,
:func:`resolve_repair_mode`, :func:`plan_stems`, :func:`plan_stems_from_context` and
:func:`plan_is_noop` (tasks 5.1-5.5), and the Separator_Backend seam — the
:class:`Separator_Backend` protocol, the :data:`Command_Runner` alias with its
:func:`_run` wrapper, the :class:`Model_Unavailable` / :class:`Invalid_Audio_Format` /
:class:`Integrity_Error` types and :func:`assemble_stem_set` (tasks 8.1-8.2). The two
backend adapters, the filtergraph emitters, the engine class and its registration arrive
in epics 9, 11 and 13.

Import contract (Req 1.4)
-------------------------
This module imports cleanly with **no ffmpeg binary on ``PATH``, no ``demucs``, no
``torch``, no ``numpy`` and no model file present**. Module scope is restricted to the
standard library plus ``worker.engines.*`` siblings (themselves stdlib-only); nothing
here shells out, opens a socket, reads the clock or touches the filesystem. Every heavy
dependency the later tasks need — ``demucs``/``torch`` for the ML Separator_Backend, the
ffmpeg/ffprobe subprocesses, model checkpoint reads — is reached through a
**function-local lazy import** inside the one function that needs it, so a minimal
install can still import this module, probe capabilities and plan (Req 12.5).

``MODEL_DIR_DEFAULT`` is a :class:`pathlib.Path` value only; constructing it performs no
filesystem access, and the model locator registered in task 8.1 is what actually looks
for the checkpoint (Req 12.3).

Vocabulary mirrors
------------------
``STEM_NAMES``, ``STEM_MAPPING``, ``MIX_PRESETS``, ``REPAIR_MODES``, ``BACKEND_IDS`` and
the gain/window bounds below are mirrored as literals in ``tests/strategies.py`` (the
shared generator module cannot import this module without making every test collection
depend on it). ``tests/test_stems_options.py`` pins the two copies against each other, so
the duplication cannot silently drift. **Keep them in sync.**
"""

from __future__ import annotations

import dataclasses
import math
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from worker.engines.base import (
    coerce_bool,
    coerce_choice,
    coerce_float,
    coerce_int,
    coerce_str,
)
from worker.engines.timebase import Time_Base, normalize_segments

__all__ = [
    "AMPLITUDE_TOLERANCE",
    "Audio_Format",
    "BACKEND_IDS",
    "Command_Runner",
    "DISK_BOUND_MULTIPLE",
    "EXTRACT_RESERVE_S",
    "GAIN_DEFAULT",
    "GAIN_MAX",
    "GAIN_MIN",
    "Integrity_Error",
    "Invalid_Audio_Format",
    "MAX_BRIDGE_WINDOWS",
    "MIN_STEP_TIMEOUT_S",
    "MIX_PRESETS",
    "MIX_PRESET_CHOICES",
    "ML_THREAD_COUNT",
    "MODEL_DIR_DEFAULT",
    "MODEL_DIR_ENV",
    "Model_Unavailable",
    "NOTCH_EXPR_CHUNK",
    "REMUX_MIN_S",
    "REMUX_RESERVE_S",
    "REPAIR_MIN_S",
    "REPAIR_MODES",
    "REPAIR_RESERVE_S",
    "Repair_Window",
    "SEAM_NOTE_PREFIX",
    "SEPARATE_RESERVE_S",
    "SEPARATION_MIN_S",
    "STEM_MAPPING",
    "STEM_NAMES",
    "Separator_Backend",
    "Stem_Error",
    "Stem_Options",
    "Stem_Plan",
    "WINDOW_DEFAULT_MS",
    "WINDOW_MAX_MS",
    "WINDOW_MIN_MS",
    "assemble_stem_set",
    "injected",
    "parse_seam_notes",
    "plan_is_noop",
    "plan_stems",
    "plan_stems_from_context",
    "repair_windows",
    "resolve_backend",
    "resolve_gains",
    "resolve_model",
    "resolve_repair_mode",
    "resolve_stem_options",
    "separation_needed",
]

# --------------------------------------------------------------------------- #
# Stem vocabulary                                                             #
# --------------------------------------------------------------------------- #

#: The exact Stem_Set the engine decomposes clip audio into, sorted so iteration order is
#: canonical and permutation-independent (Req 4.1, 4.9).
STEM_NAMES: tuple[str, ...] = ("music", "other", "vocals")

#: Backend_Stem name -> Stem_Name; ``drums`` and ``bass`` both collapse (sum) into
#: ``music`` (Req 4.2).
#:
#: NOTE — the deliberate gap: the ffmpeg Separator_Backend (task 8.3) emits the
#: Backend_Stems ``vocals`` and ``music`` (``music := clip - vocals``), and ``music`` has
#: **no key of its own here**. That is settled and intentional: rather than adding a
#: ``"music": "music"`` self-entry (which would make this table no longer the fixed
#: Req 4.2 mapping), ``assemble_stem_set`` (task 8.2) resolves a Backend_Stem name by
#: consulting ``STEM_MAPPING`` first and then **falling back to identity when the name is
#: already a Stem_Name** — so ``music`` routes to ``music`` and is never discarded and
#: replaced with silence. Names in neither place contribute to nothing (Req 4.3).
STEM_MAPPING: dict[str, str] = {
    "vocals": "vocals",
    "drums": "music",
    "bass": "music",
    "other": "other",
}

#: The three non-``custom`` Mix_Presets and their documented Stem_Gain bundles; a resolved
#: non-``custom`` preset wins over the individual gain fields (Req 5.2). No shipped preset
#: boosts a stem, so the saturating path needs a deliberate ``custom`` configuration.
MIX_PRESETS: dict[str, dict[str, float]] = {
    "speech_focus": {"vocals": 1.0, "music": 0.25, "other": 0.6},
    "music_focus": {"vocals": 0.25, "music": 1.0, "other": 0.8},
    "clean_speech": {"vocals": 1.0, "music": 0.0, "other": 0.0},
}

#: Every legal ``mix_preset`` option value, sorted: the three bundles plus ``custom``,
#: which means "use the individual Stem_Gain fields" (Req 5.2, 5.3).
MIX_PRESET_CHOICES: tuple[str, ...] = tuple(sorted(("custom", *MIX_PRESETS)))

#: The three allowed Repair_Mode values, in the design's declared order (Req 7.1).
REPAIR_MODES: tuple[str, ...] = ("off", "crossfade", "spectral")

#: The three ``backend`` option values; a *resolved* backend is only ``ml`` or ``ffmpeg``
#: (Req 12.1, 13.1).
BACKEND_IDS: tuple[str, ...] = ("auto", "ml", "ffmpeg")

# --------------------------------------------------------------------------- #
# Numeric bounds                                                              #
# --------------------------------------------------------------------------- #

#: Inclusive Stem_Gain bounds and the documented value a rejected gain falls back to
#: (Req 5.1, 5.4).
GAIN_MIN, GAIN_MAX, GAIN_DEFAULT = 0.0, 4.0, 1.0

#: Inclusive Repair_Window bounds in milliseconds and the documented default; an
#: out-of-range value is clamped, never rejected (Req 7.1, 7.6).
WINDOW_MIN_MS, WINDOW_MAX_MS, WINDOW_DEFAULT_MS = 2, 120, 12

#: Documented per-sample amplitude tolerance (one 16-bit LSB) for the additive
#: decomposition and the cross-environment re-mix guarantee (Req 4.7, 10.6).
AMPLITUDE_TOLERANCE = 1.0 / 32768.0

#: Documented bound on total Engine_Workspace bytes, as a multiple of the extracted clip
#: audio size (Req 11.7).
DISK_BOUND_MULTIPLE = 5

#: Cap on spectral music-bridging windows per clip, bounding filtergraph size so a
#: seam-dense clip cannot explode the graph (Req 7.3, 15.9).
MAX_BRIDGE_WINDOWS = 24

#: Seams per ``volume`` expression chunk, so a long Seam list is emitted as several
#: bounded expressions instead of one unbounded command line (Req 7.5, 15.9).
NOTCH_EXPR_CHUNK = 32

#: Thread count pinned on the ML Separator_Backend for CPU-only, reproducible inference
#: (Req 10.3, 15.2).
ML_THREAD_COUNT = 1

#: Environment variable naming the local directory searched for separation model files
#: (Req 12.3).
MODEL_DIR_ENV = "CLIPPER_STEM_MODEL_DIR"

#: Fallback local model directory when ``MODEL_DIR_ENV`` is unset; a plain path value, no
#: filesystem access at import (Req 12.3, 12.4).
MODEL_DIR_DEFAULT = Path("models/stems")

# --------------------------------------------------------------------------- #
# Step reserves and budget gate thresholds                                    #
# --------------------------------------------------------------------------- #
# ``step_timeout(ctx, reserve_s) = max(MIN_STEP_TIMEOUT_S, ctx.remaining() - reserve_s)``
# (task 11.1): each step re-reads ``ctx.remaining()`` and holds back its reserve so the
# steps that follow it still have budget, and every ffmpeg invocation carries an explicit
# positive timeout (Req 15.3, 15.4).

#: Budget held back from the audio-extraction pass for the steps after it (Req 15.3/15.4).
EXTRACT_RESERVE_S = 3.0

#: Budget held back from separation for repair plus remux (Req 15.3, 15.4).
SEPARATE_RESERVE_S = 8.0

#: Budget held back from seam repair for the remux pass (Req 15.3, 15.4).
REPAIR_RESERVE_S = 5.0

#: Budget held back from the final remux pass — the last step, so a thin reserve
#: (Req 15.3, 15.4).
REMUX_RESERVE_S = 0.5

#: Floor on any derived step timeout: never zero or negative, so no ffmpeg invocation is
#: launched with a non-positive timeout (Req 15.4).
MIN_STEP_TIMEOUT_S = 1.0

#: Remaining budget separation needs, per *resolved* backend, before it is attempted at
#: all; below it the ladder takes the repair-only degraded rung (Req 15.5, 13.3).
SEPARATION_MIN_S: dict[str, float] = {"ml": 20.0, "ffmpeg": 4.0}

#: Remaining budget seam repair needs before it is attempted (Req 15.3, 15.5).
REPAIR_MIN_S = 3.0

#: Remaining budget the remux pass needs; below it there is no way to produce
#: Replacement_Media, so the run abandons rather than leaving a partial file (Req 15.6,
#: 15.7).
REMUX_MIN_S = 2.0


# --------------------------------------------------------------------------- #
# Documented option defaults                                                  #
# --------------------------------------------------------------------------- #
# Private, because they exist only to keep each :class:`Stem_Options` field default and
# the coercion fallback used for it literally the same value.

#: Documented default Mix_Preset: "use the individual Stem_Gain fields" (Req 5.3).
_MIX_PRESET_DEFAULT = "custom"

#: Documented default Repair_Mode (Req 7.1).
_REPAIR_MODE_DEFAULT = "crossfade"

#: Documented default backend selection: resolve ``ml`` vs ``ffmpeg`` from capabilities
#: (Req 12.1, 13.1) rather than pinning one here.
_BACKEND_DEFAULT = "auto"

#: Documented default separation model name; the capability id it implies is
#: ``model:<model>`` (Req 12.3, 12.4).
_MODEL_DEFAULT = "htdemucs"

#: Cap on the stored ``model`` string, mirroring the other engines' name fields; a longer
#: value is truncated rather than rejected, so parsing stays total.
_MODEL_MAX_LEN = 128

#: Prefix every :class:`Stem_Options` field carries on Processing_Options — ``mix_preset``
#: is read as ``stem_mix_preset``, and so on for all ten (task 17.1, Req 18.1). Mirrors the
#: ``kinetic_*`` convention, so the projection needs no per-field name table.
_OPTION_PREFIX = "stem_"


def _read(options: Any, *names: str) -> Any:
    """First non-``None`` attribute of ``options`` among ``names``, else ``None``.

    Attributes only — never a write, never a mutation — so the caller's Processing_Options
    instance is provably unmodified (Req 1.3). The Processing_Options spelling
    (``stem_<field>``) is listed first and the already-resolved :class:`Stem_Options`
    spelling (``<field>``) second, which is what makes re-resolving an already-resolved
    value the identity (Req 9.6).

    Total: ``getattr`` is given a default *and* guarded, so a missing attribute, a
    ``property`` that raises, or an exotic ``__getattr__`` all read as "not supplied"
    rather than propagating.
    """
    for name in names:
        try:
            value = getattr(options, name, None)
        except Exception:  # pragma: no cover - hostile descriptor
            continue
        if value is not None:
            return value
    return None


def _coerce_gain(value: Any) -> float:
    """Coerce one Stem_Gain to a finite float inside ``[GAIN_MIN, GAIN_MAX]`` (Req 5.4).

    ``coerce_float`` first (so ``bool``, ``None``, containers, non-numeric text, ``NaN``
    and ``±inf`` all become :data:`GAIN_DEFAULT`), then an explicit finite check and an
    explicit range check: a value that is negative or above :data:`GAIN_MAX` is
    **substituted** by :data:`GAIN_DEFAULT`, exactly as Req 5.4 words it, rather than
    saturated at the bound — so ``resolve_gains`` (task 5.1) sees the same value whether
    it reads a parsed field or a raw one.

    Total: an integer too large to convert to a float makes ``float()`` raise
    ``OverflowError``, so the call is guarded and that input yields the default too.
    """
    try:
        number = coerce_float(value, GAIN_DEFAULT)
    except (TypeError, ValueError, OverflowError):  # pragma: no cover - hostile huge int
        return GAIN_DEFAULT
    if not math.isfinite(number) or number < GAIN_MIN or number > GAIN_MAX:
        return GAIN_DEFAULT
    return float(number)


@dataclass(frozen=True)
class Stem_Options:
    """Resolved stem settings: JSON-serialisable scalars only (Req 9.1).

    Satisfies the foundation ``Engine_Options`` protocol through the :meth:`parse`
    classmethod and :meth:`to_dict` (Req 9.2). ``__post_init__`` routes **every** field
    through a foundation ``coerce_*`` helper with its documented default and bounds, so
    construction is total — no input raises — and coercing an already-valid value is the
    identity, which is what makes the round-trip (Req 9.4) and idempotence (Req 9.6)
    properties hold.

    Ten fields, one per option the design lists. The eleventh member of the
    Processing_Options surface, ``stem_inpainting_enabled``, is the engine's Feature_Flag
    and lives on Processing_Options, read through ``AV_Engine.flag_field()`` — it is
    deliberately **not** a field here.

    Field rules:

    * ``mix_preset`` / ``repair_mode`` / ``backend`` — ``coerce_choice`` against
      :data:`MIX_PRESET_CHOICES` / :data:`REPAIR_MODES` / :data:`BACKEND_IDS`, with the
      documented default substituted for any unrecognised value (Req 9.3).
    * ``gain_vocals`` / ``gain_music`` / ``gain_other`` — finite floats inside
      ``[GAIN_MIN, GAIN_MAX]``; anything else becomes :data:`GAIN_DEFAULT` (Req 5.4).
    * ``repair_window_ms`` — ``coerce_int`` **clamped** into
      ``[WINDOW_MIN_MS, WINDOW_MAX_MS]``, so an out-of-range window is narrowed rather
      than rejected (Req 7.6).
    * ``declick`` / ``retain_stems`` — ``coerce_bool``.
    * ``model`` — ``coerce_str``; the empty string is a legal value meaning "the resolver
      picks", and it is preserved verbatim through the round-trip.
    """

    mix_preset: str = _MIX_PRESET_DEFAULT       # one of MIX_PRESET_CHOICES
    gain_vocals: float = GAIN_DEFAULT           # [GAIN_MIN, GAIN_MAX]
    gain_music: float = GAIN_DEFAULT
    gain_other: float = GAIN_DEFAULT
    repair_mode: str = _REPAIR_MODE_DEFAULT     # one of REPAIR_MODES
    repair_window_ms: int = WINDOW_DEFAULT_MS   # [WINDOW_MIN_MS, WINDOW_MAX_MS]
    declick: bool = False
    backend: str = _BACKEND_DEFAULT             # one of BACKEND_IDS
    model: str = _MODEL_DEFAULT
    retain_stems: bool = False                  # durable per-stem WAVs (Req 11.3)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(
            self,
            "mix_preset",
            coerce_choice(self.mix_preset, MIX_PRESET_CHOICES, _MIX_PRESET_DEFAULT),
        )
        set_(self, "gain_vocals", _coerce_gain(self.gain_vocals))
        set_(self, "gain_music", _coerce_gain(self.gain_music))
        set_(self, "gain_other", _coerce_gain(self.gain_other))
        set_(
            self,
            "repair_mode",
            coerce_choice(self.repair_mode, REPAIR_MODES, _REPAIR_MODE_DEFAULT),
        )
        set_(
            self,
            "repair_window_ms",
            coerce_int(
                self.repair_window_ms,
                WINDOW_DEFAULT_MS,
                lo=WINDOW_MIN_MS,
                hi=WINDOW_MAX_MS,
            ),
        )
        set_(self, "declick", coerce_bool(self.declick, False))
        set_(self, "backend", coerce_choice(self.backend, BACKEND_IDS, _BACKEND_DEFAULT))
        set_(self, "model", coerce_str(self.model, _MODEL_DEFAULT, _MODEL_MAX_LEN))
        set_(self, "retain_stems", coerce_bool(self.retain_stems, False))

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a flat, JSON-native mapping in sorted key order (Req 9.2, 9.4).

        Every value is a ``str``, ``bool``, ``int`` or finite ``float``, so
        ``json.dumps`` on the result never raises; every field is present, so the mapping
        is a complete description and ``parse(to_dict(o)).to_dict() == o.to_dict()``.
        """
        record: dict[str, Any] = {
            "backend": str(self.backend),
            "declick": bool(self.declick),
            "gain_music": float(self.gain_music),
            "gain_other": float(self.gain_other),
            "gain_vocals": float(self.gain_vocals),
            "mix_preset": str(self.mix_preset),
            "model": str(self.model),
            "repair_mode": str(self.repair_mode),
            "repair_window_ms": int(self.repair_window_ms),
            "retain_stems": bool(self.retain_stems),
        }
        return {key: record[key] for key in sorted(record)}

    @classmethod
    def parse(cls, data: Mapping[str, Any] | None) -> "Stem_Options":
        """Total parser: never raises, ignores unknown keys (Req 9.5, 18.5).

        Named keys only — the mapping is read field by field, so any key that is not a
        field (including unhashable-looking junk and the ``stem_inpainting_enabled``
        Feature_Flag) is simply ignored — and each value present is coerced by
        :meth:`__post_init__` with that field's documented default and bounds. ``None``
        and any non-mapping argument yield the all-defaults value.
        """
        if not isinstance(data, Mapping):
            return cls()
        kwargs: dict[str, Any] = {}
        for entry in dataclasses.fields(cls):
            try:
                if entry.name in data:
                    kwargs[entry.name] = data[entry.name]
            except Exception:  # pragma: no cover - hostile mapping
                continue
        try:
            return cls(**kwargs)
        except Exception:  # pragma: no cover - defensive: coercion is total
            return cls()

    # -- projection from ProcessingOptions (task 4.3) -----------------------

    @classmethod
    def from_processing_options(cls, options: Any) -> "Stem_Options":
        """Project Processing_Options onto Stem_Options (Req 1.3, 9.6, 20.2).

        Reads the ``stem_*`` attributes off ``options`` — already normalised by
        ``worker.models.effective_options`` — collects them into a mapping and hands that
        mapping to :meth:`parse`, so **every** field travels the single coercion ladder in
        :meth:`__post_init__`. There is deliberately no second ladder here: the choice
        sets, the gain range, the window clamp and the string cap are defined once.

        *Pure and read-only* (Req 1.3): the only contact with ``options`` is
        :func:`getattr`, so nothing is assigned, no container reached through it is
        mutated, and the host observes ``dataclasses.asdict(options)`` unchanged after
        every invocation.

        *Total*: a missing attribute, ``None``, or any hostile value yields the documented
        default rather than an exception — ``getattr`` is guarded, and :meth:`parse` is
        already total (Req 9.5). ``options`` may be any object at all, including ``None``.

        *Idempotent* (Req 9.6): each field is read from the Processing_Options spelling
        (``stem_`` + field name, the surface epic 17 adds in task 17.1) first and from the
        already-resolved :class:`Stem_Options` spelling (the bare field name) second, and
        coercing an already-valid value is the identity — so
        ``from_processing_options(from_processing_options(o))`` returns an equal value, and
        resolving the same Processing_Options twice yields equal results.

        **Current state of the Processing_Options surface (verified against
        ``worker/models.py``):** ``ProcessingOptions`` carries **no** ``stem_*`` field yet
        — task 17.1 adds the eleven of them (the ten fields here plus the
        ``stem_inpainting_enabled`` Feature_Flag, which is *not* a field here and is read
        through ``AV_Engine.flag_field()``). Until then every read misses and the result is
        the all-defaults :class:`Stem_Options`, which is exactly the documented safe
        configuration (Req 20.2). No bare :class:`Stem_Options` field name collides with
        an existing ``ProcessingOptions`` field, so the second spelling cannot capture an
        unrelated value.

        A value of ``None`` is treated as "not supplied" and the key is omitted, so
        :meth:`parse` applies that field's default. ``False``, ``0``/``0.0`` and ``""``
        are *supplied* values and are preserved: a muted stem stays ``0.0`` and an empty
        ``model`` (meaning "the resolver picks") stays ``""``.
        """
        raw: dict[str, Any] = {}
        for entry in dataclasses.fields(cls):
            value = _read(options, _OPTION_PREFIX + entry.name, entry.name)
            if value is not None:
                raw[entry.name] = value
        return cls.parse(raw)


# --------------------------------------------------------------------------- #
# Option resolution (task 4.3)                                                #
# --------------------------------------------------------------------------- #


def resolve_stem_options(options: Any) -> Stem_Options:
    """Resolve Processing_Options to :class:`Stem_Options` — the engine's
    ``resolve_options`` body (Req 1.2, 1.3, 9.6, 20.2).

    Module-level on purpose. ``Stem_Inpainting_Engine`` does not exist yet — it arrives in
    **task 13.1**, and its ``resolve_options(self, options)`` is to be a one-line
    delegation to this function::

        def resolve_options(self, options: Any) -> Stem_Options:
            return resolve_stem_options(options)

    Keeping the projection here means the planner tasks (epics 5-7), the property tests
    (task 4.7, which resolves without an engine instance) and epic 13's method all share
    one implementation, and none of them needs the engine class to be importable.

    Pure, total and idempotent, and it never writes to ``options``: the whole projection is
    :meth:`Stem_Options.from_processing_options`, which reads attributes only.
    """
    return Stem_Options.from_processing_options(options)


# --------------------------------------------------------------------------- #
# Plan records (task 4.4)                                                     #
# --------------------------------------------------------------------------- #
# Three frozen records: the probed :class:`Audio_Format`, one merged
# :class:`Repair_Window`, and the :class:`Stem_Plan` the planner (epic 5) returns and the
# engine hands back as ``Engine_Result.plan``. They are *records*, not logic: nothing here
# decides anything. The planner is task 5.x; these only guarantee that whatever it decides
# is stored in a canonical shape and serialises to JSON.
#
# The two invariants every field below is arranged to keep:
#
# * **JSON-native output.** ``json.dumps(plan.to_dict())`` succeeds with **no custom
#   encoder**: every leaf is a ``str``, ``bool``, ``int`` or *finite* ``float``, every
#   tuple becomes a ``list``, every nested :class:`Repair_Window` becomes its own
#   ``to_dict()``, and any stray path-like value is stringified. ``coerce_float`` rejects
#   ``NaN``/``±inf`` outright, so a non-finite value cannot even be *stored*, let alone
#   emitted (Req 10.7).
# * **Deterministic ordering.** Sorted keys at every level, ``gains`` rebuilt in
#   :data:`STEM_NAMES` order, ``active_stems`` filtered back into that order, ``seams``
#   and ``windows`` sorted. Two runs that decide the same things therefore produce equal
#   dicts, which is how planning determinism (Req 10.1) and the cross-environment
#   guarantee (Req 10.6) are actually asserted — field by field, on the serialised form.
#
# There is deliberately **no** ``from_dict`` on any of the three: the design asks only for
# ``to_dict``, the plan is an output (it is compared and returned, never parsed back), and
# a parser nobody calls is a second source of truth waiting to drift. ``Stem_Options`` is
# the value object that round-trips (Req 9.4); the plan is not.


def _plan_float(value: Any) -> float:
    """One plan timing/level as a **finite** float, defaulting to ``0.0``.

    Thin alias for ``coerce_float(value, 0.0)``, named for its single purpose: it is the
    only way a float enters :class:`Repair_Window` or :class:`Stem_Plan`, so ``NaN``,
    ``±inf``, ``None``, strings and containers are all normalised at construction and
    :meth:`Stem_Plan.to_dict` can never emit a value ``json.dumps`` would render as the
    invalid literal ``NaN``/``Infinity``.
    """
    return coerce_float(value, 0.0)


def _plan_floats(values: Any) -> tuple[float, ...]:
    """Coerce an iterable of Seam times to a sorted tuple of finite floats.

    Total: a non-iterable (or a ``str``/``bytes``, which is iterable but never a Seam
    list) yields ``()``, and each element travels :func:`_plan_float`. Sorted, because
    Seam order is not something a caller should be able to vary — two planners that found
    the same Seams must produce the same tuple (Req 6.6, 10.1). Duplicates are **kept**:
    de-duplication is ``normalize_segments``'/the planner's job (task 5.2, 5.3), and
    silently dropping a Seam here would make ``Repair_Window.seams`` an incomplete record
    of what was merged (Req 7.7).
    """
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        return ()
    return tuple(sorted(_plan_float(value) for value in values))


def _plan_strings(values: Any) -> tuple[str, ...]:
    """Coerce an iterable of identifiers to a tuple of ``str``, order preserved.

    Order is preserved rather than sorted on purpose: the one tuple this builds from an
    external source is ``missing_capabilities``, whose incoming order is
    ``Capability_Report.missing`` — declaration order, which is what makes
    ``first_missing`` (and therefore the ``degraded:``/``unavailable:`` marker the ladder
    emits) meaningful. That order is already deterministic, so sorting would buy nothing
    and lose the "which one fired" information. Total: a non-iterable yields ``()``.
    """
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        return ()
    return tuple(str(value) for value in values)


@dataclass(frozen=True)
class Audio_Format:
    """The probed audio format of one clip — ``ffprobe``'s answer, as a value (Req 17.4).

    ``MediaInfo`` carries ``has_audio``, ``duration`` and ``fps`` but **no** sample rate or
    channel count, so ``probe_audio_format`` (task 8.x) reads
    ``stream=sample_rate,channels,codec_name,start_time`` off the first audio stream and
    returns one of these. It is the value that must be equal across environments for the
    cross-environment guarantee to hold (Req 10.6), and ``sample_rate``/``channels`` are
    what the extract and re-mix passes pin (``-ar``/``-ac``), which is why they are also
    copied onto :class:`Stem_Plan`.

    A pure record: **no coercion, no validation, no clamping.** Validity is the prober's
    responsibility and its rules are sharper than a dataclass default could be — no audio
    stream at all yields ``None`` rather than a value (Req 4.8), and an invalid, zero or
    negative ``sample_rate``/``channels`` is an ``Invalid_Audio_Format`` that raises the
    ``degraded:audio_format`` rung (Req 17.5). Silently repairing those here would hide
    exactly the condition that rung exists to report.

    ``start_time`` is the stream's first presentation timestamp in seconds. It defaults to
    ``0.0`` and ``codec`` to ``""`` because ``ffprobe`` legitimately omits both (or reports
    ``"N/A"``) on some containers, and neither absence makes the format unusable — unlike
    the two required fields, which have no meaningful default.

    No ``to_dict``: the design does not give it one. The two fields any consumer needs on
    the serialised side already appear as :class:`Stem_Plan` fields.
    """

    sample_rate: int
    channels: int
    codec: str = ""
    start_time: float = 0.0


@dataclass(frozen=True)
class Repair_Window:
    """One clip-relative span of audio to repair, plus the Seam(s) merged into it.

    Produced by ``repair_windows`` (task 5.3): a symmetric ``repair_window_ms`` window is
    built around every Seam, snapped to sample boundaries through ``Time_Base``, clamped
    to ``[0, duration]`` and run through the foundation ``normalize_segments`` — so the
    windows in a :class:`Stem_Plan` are sorted, pairwise disjoint and in bounds, and a
    cluster of overlapping windows arrives here as **one** window that is repaired exactly
    once while :attr:`seams` still names every Seam it absorbed (Req 6.8, 7.7).

    :attr:`seams` is what the filtergraph emitter needs: the V-notch expression is built
    per *window*, but the ``repair:<mode>:<count>`` marker counts merged windows and the
    Seam list is what makes a merge auditable after the fact.

    Construction is total — every field travels :func:`_plan_float` / :func:`_plan_floats`,
    so no non-finite value can be stored — and :attr:`seams` is normalised to a sorted
    tuple, which is what makes two equal plans serialise identically.
    """

    start: float = 0.0                    # clip-relative seconds, sample-snapped
    end: float = 0.0                      # clip-relative seconds, ``>= start``
    seams: tuple[float, ...] = ()         # the Seam(s) merged into this window (Req 7.7)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "start", _plan_float(self.start))
        set_(self, "end", _plan_float(self.end))
        set_(self, "seams", _plan_floats(self.seams))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-native mapping in sorted key order.

        ``seams`` becomes a ``list`` (a tuple is not JSON), the two bounds are finite
        floats by construction, and the keys are sorted — so this nests directly inside
        :meth:`Stem_Plan.to_dict` and the whole plan is one ``json.dumps`` call with no
        custom encoder.
        """
        record: dict[str, Any] = {
            "end": float(self.end),
            "seams": [float(seam) for seam in self.seams],
            "start": float(self.start),
        }
        return {key: record[key] for key in sorted(record)}


@dataclass(frozen=True)
class Stem_Plan:
    """Everything the planner decided, as a comparable, serialisable value (Req 10.1).

    Returned by the engine's ``plan`` operation and carried verbatim into
    ``Engine_Result.plan``. Exactly the seventeen designed fields — no more, so the
    serialised form is a complete description of the decision and two runs can be compared
    **field by field** rather than by inspecting media, which is how planning determinism
    (Req 10.1) and the cross-environment guarantee (Req 10.6) are asserted. :attr:`backend`
    and :attr:`model` are in the plan precisely so a reproduced run can be checked against
    the environment that produced it (Req 10.7).

    Field rules:

    * ``backend`` — the **resolved** backend, so ``"ml"`` or ``"ffmpeg"`` only; never
      ``"auto"``, which is a request, not a decision (Req 12.1, 13.1).
    * ``model`` — the resolved separation model name; ``""`` when the resolved backend
      needs none (the ffmpeg approximation).
    * ``gains`` — the resolved Stem_Gain per Stem_Name, rebuilt in :data:`STEM_NAMES`
      order from a **copy** of the mapping given, with every value a finite float. The
      copy matters: the plan is frozen, and a plan that aliased the planner's dict would
      not be.
    * ``active_stems`` — the stems with a non-zero gain, i.e. the ffmpeg inputs actually
      added; a ``0.0`` stem is not an input at all (Req 5.7). Filtered back into
      :data:`STEM_NAMES` order, so it is a canonical subset.
    * ``repair_mode`` — resolved and **post-downgrade**: when rung 9 turned ``spectral``
      into ``crossfade``, this reads ``"crossfade"`` and :attr:`downgraded_from` reads
      ``"spectral"`` (Req 7.4).
    * ``seams`` / ``windows`` — the normalised in-bounds Seams (Req 6.6) and the sorted,
      disjoint :class:`Repair_Window` list built from them (Req 6.8). Both sorted here
      too, so ordering is a property of the value and not of the caller.
    * ``sample_rate`` / ``channels`` — copied off the probed :class:`Audio_Format`; the
      ``-ar``/``-ac`` the extract and re-mix passes pin.
    * ``duration`` — the clip duration the windows were clamped against; preserved exactly
      by the pipeline (Req 17.1).
    * ``declick`` / ``needs_separation`` — the declick flag as resolved, and whether
      separation is required at all (any gain ``!= 1.0``, or ``repair_mode ==
      "spectral"``); a plan with ``needs_separation`` false is the repair-only path.
    * ``missing_capabilities`` — the capability ids that were unavailable, in
      ``Capability_Report.missing`` order (see :func:`_plan_strings`).
    * ``downgraded_from`` — ``"spectral"`` when the spectral rung fired, else ``""``.
    * ``bridged_windows`` / ``notched_windows`` — how many windows got the spectral
      music-bridge treatment (bounded by :data:`MAX_BRIDGE_WINDOWS`) versus the plain
      equal-power V-notch. They sum to ``len(windows)`` on a well-formed plan; that is the
      planner's invariant to keep, not something this record enforces.

    Construction is total: every field travels a ``coerce_*``-backed normaliser, so a
    hostile or half-built value yields a usable plan instead of an exception, and no
    non-finite float, stray tuple type or aliased container can get in. Only the three
    fields the design defaults are optional; the rest are required, because a plan missing
    one of them is not a decision.
    """

    backend: str                                     # "ml" | "ffmpeg" (Req 10.7)
    model: str                                       # resolved model name (Req 10.7)
    gains: dict[str, float]                          # keyed by STEM_NAMES, sorted
    active_stems: tuple[str, ...]                     # gain > 0.0 only (Req 5.7)
    repair_mode: str                                 # resolved, post-downgrade
    repair_window_ms: int                            # [WINDOW_MIN_MS, WINDOW_MAX_MS]
    seams: tuple[float, ...]                         # normalised, in-bounds (Req 6.6)
    windows: tuple[Repair_Window, ...]               # sorted, disjoint (Req 6.8)
    sample_rate: int
    channels: int
    duration: float
    declick: bool
    needs_separation: bool                           # gain != 1.0 or spectral repair
    missing_capabilities: tuple[str, ...]
    downgraded_from: str = ""                        # "spectral" when rung 9 fired
    bridged_windows: int = 0
    notched_windows: int = 0

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "backend", str(self.backend))
        set_(self, "model", str(self.model))
        set_(self, "gains", self._canonical_gains(self.gains))
        set_(self, "active_stems", self._canonical_active(self.active_stems))
        set_(self, "repair_mode", str(self.repair_mode))
        set_(
            self,
            "repair_window_ms",
            coerce_int(
                self.repair_window_ms,
                WINDOW_DEFAULT_MS,
                lo=WINDOW_MIN_MS,
                hi=WINDOW_MAX_MS,
            ),
        )
        set_(self, "seams", _plan_floats(self.seams))
        set_(self, "windows", self._canonical_windows(self.windows))
        set_(self, "sample_rate", coerce_int(self.sample_rate, 0, lo=0))
        set_(self, "channels", coerce_int(self.channels, 0, lo=0))
        set_(self, "duration", _plan_float(self.duration))
        set_(self, "declick", coerce_bool(self.declick, False))
        set_(self, "needs_separation", coerce_bool(self.needs_separation, False))
        set_(self, "missing_capabilities", _plan_strings(self.missing_capabilities))
        set_(self, "downgraded_from", str(self.downgraded_from))
        set_(self, "bridged_windows", coerce_int(self.bridged_windows, 0, lo=0))
        set_(self, "notched_windows", coerce_int(self.notched_windows, 0, lo=0))

    # -- canonical forms ----------------------------------------------------

    @staticmethod
    def _canonical_gains(gains: Any) -> dict[str, float]:
        """A fresh gain mapping with one finite entry per :data:`STEM_NAMES`, in order.

        Insertion order is :data:`STEM_NAMES` — already sorted (Req 4.1) — so iterating
        ``plan.gains`` is canonical and the filtergraph's input order follows from the
        plan rather than from whatever order the planner happened to build its dict in
        (Req 5.7). A stem the caller omitted reads :data:`GAIN_DEFAULT`; a key that is not
        a Stem_Name is dropped, because a gain for a stem that does not exist cannot reach
        the mix. Total: a non-mapping yields the all-default bundle.
        """
        source: Mapping[str, Any] = gains if isinstance(gains, Mapping) else {}
        record: dict[str, float] = {}
        for name in STEM_NAMES:
            try:
                present = name in source
            except Exception:  # pragma: no cover - hostile mapping
                present = False
            record[name] = _coerce_gain(source[name]) if present else GAIN_DEFAULT
        return record

    @staticmethod
    def _canonical_active(names: Any) -> tuple[str, ...]:
        """The given stem names as a canonical subset of :data:`STEM_NAMES`.

        Filtered *through* :data:`STEM_NAMES` rather than sorted, which de-duplicates and
        orders in one pass and drops anything that is not a Stem_Name — so
        ``active_stems`` is always a subset of the stems that exist, in the same order the
        gains iterate. Total: a non-iterable yields ``()``.
        """
        supplied = set(_plan_strings(names))
        return tuple(name for name in STEM_NAMES if name in supplied)

    @staticmethod
    def _canonical_windows(windows: Any) -> tuple[Repair_Window, ...]:
        """The given windows as a tuple of :class:`Repair_Window`, sorted by bounds.

        Sorted by ``(start, end)``, matching what ``normalize_segments`` already
        guarantees (task 5.3) — asserting it here as well means the serialised plan is
        order-independent of the caller. Anything that is not a :class:`Repair_Window` is
        dropped: mappings are *not* rebuilt into windows, because that would be a
        ``from_dict`` in disguise and the design asks for none. Total: a non-iterable
        yields ``()``.
        """
        if isinstance(windows, (str, bytes)) or not isinstance(windows, Iterable):
            return ()
        kept = [window for window in windows if isinstance(window, Repair_Window)]
        return tuple(sorted(kept, key=lambda window: (window.start, window.end)))

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-native mapping in sorted key order (Req 10.1, 10.7).

        One key per field and no others, so the mapping is a complete, comparable
        description of the plan: two plans are equal exactly when their dicts are, and any
        single differing field changes the dict. ``json.dumps`` accepts the result with
        **no custom encoder** — tuples become lists, each :class:`Repair_Window` becomes
        its own :meth:`Repair_Window.to_dict`, ``str()`` flattens any path-like string
        field, and every float is finite by construction. ``gains`` nests as a mapping in
        :data:`STEM_NAMES` order; every other ordering is sorted.
        """
        record: dict[str, Any] = {
            "active_stems": [str(name) for name in self.active_stems],
            "backend": str(self.backend),
            "bridged_windows": int(self.bridged_windows),
            "channels": int(self.channels),
            "declick": bool(self.declick),
            "downgraded_from": str(self.downgraded_from),
            "duration": float(self.duration),
            "gains": {name: float(self.gains[name]) for name in STEM_NAMES},
            "missing_capabilities": [str(item) for item in self.missing_capabilities],
            "model": str(self.model),
            "needs_separation": bool(self.needs_separation),
            "notched_windows": int(self.notched_windows),
            "repair_mode": str(self.repair_mode),
            "repair_window_ms": int(self.repair_window_ms),
            "sample_rate": int(self.sample_rate),
            "seams": [float(seam) for seam in self.seams],
            "windows": [window.to_dict() for window in self.windows],
        }
        return {key: record[key] for key in sorted(record)}



# --------------------------------------------------------------------------- #
# The pure planner (tasks 5.1-5.5)                                            #
# --------------------------------------------------------------------------- #
# Every function below is **pure**: no ffmpeg, no ``demucs`` import, no network, no model
# read, no clock, no filesystem, no randomness (Req 1.9, 2.7, 12.5, 19.2). They compose
# into :func:`plan_stems`, and :func:`plan_stems_from_context` is the body the engine's
# ``plan(ctx)`` delegates to in task 13.1 — the same arrangement
# :func:`resolve_stem_options` uses for ``resolve_options``, so the planner is testable
# with no engine instance and no heavy dependency installed.
#
# The one collaborator that *could* touch the outside world is the Capability_Report:
# :func:`resolve_backend` asks it whether ``python_pkg:demucs`` and ``model:<model>`` are
# available. The foundation's report is memoised and its probes are ``find_spec`` (an
# import-free spec lookup) and a registered model locator, and every test injects a fake
# report — so consulting it imports no separation package and reads no model file, which is
# what Property 1 asserts.

#: The one Seam_Note prefix the engine reads (Req 6.4). No other prefix is parsed, and no
#: Seam is ever inferred from the waveform or from Word_Timeline gaps (Req 6.5) — a Seam
#: exists only because filler removal published it.
SEAM_NOTE_PREFIX = "filler_seam:"

#: Channel count assumed while planning, before ``probe_audio_format`` has run. ``plan`` is
#: pure, so it cannot probe: the plan it returns carries this and ``Time_Base.sample_rate``
#: as placeholders, and ``run`` re-plans with the real :class:`Audio_Format` once the probe
#: has happened (which is why :func:`plan_stems` takes ``fmt`` at all).
_CHANNELS_DEFAULT = 2


def resolve_gains(opts: Stem_Options) -> dict[str, float]:
    """Resolve the Stem_Gain bundle: preset wins, ``custom`` uses the fields (Req 5.1-5.4).

    A non-``custom`` Mix_Preset returns exactly its :data:`MIX_PRESETS` bundle and the
    individual ``gain_*`` fields are **ignored entirely** — not merged, not used as
    fallbacks for a stem the bundle omits (no shipped bundle omits one) — which is Req 5.2
    read literally. ``custom`` returns the three validated fields (Req 5.3).

    The result is a fresh mapping keyed by :data:`STEM_NAMES`, so iteration order is sorted
    and canonical (Req 4.1) and the filtergraph's input order is a property of the plan
    rather than of the preset table's key order. Every value goes through
    :func:`_coerce_gain`, so a hostile preset table entry or an unvalidated field is
    replaced by :data:`GAIN_DEFAULT` rather than escaping into the filtergraph (Req 5.4) —
    belt and braces, since :class:`Stem_Options` has already coerced the fields.

    Total: ``opts`` may be any object; a missing attribute reads as :data:`GAIN_DEFAULT`.
    """
    preset = coerce_choice(
        getattr(opts, "mix_preset", _MIX_PRESET_DEFAULT),
        MIX_PRESET_CHOICES,
        _MIX_PRESET_DEFAULT,
    )
    bundle = MIX_PRESETS.get(preset)
    if bundle is None:                       # "custom" — the individual fields
        bundle = {
            "music": getattr(opts, "gain_music", GAIN_DEFAULT),
            "other": getattr(opts, "gain_other", GAIN_DEFAULT),
            "vocals": getattr(opts, "gain_vocals", GAIN_DEFAULT),
        }
    return {name: _coerce_gain(bundle.get(name, GAIN_DEFAULT)) for name in STEM_NAMES}


def parse_seam_notes(notes: Sequence[str], duration: float) -> list[float]:
    """Extract the Seam times from the host's Engine_Context notes (Req 6.4-6.6).

    Keeps only well-formed ``filler_seam:<float>`` notes whose value is finite and inside
    ``[0, duration]``. Every rejection is **individual**: a malformed, non-finite, negative
    or out-of-bounds note is dropped on its own and the remaining valid notes in the same
    tuple survive (Req 6.5), because a single bad note must not cost the clip its repair.

    Reads no other prefix and infers nothing: a note that is not a ``filler_seam:`` note is
    invisible here, and no Seam is derived from the waveform or from Word_Timeline gaps
    (Req 6.5) — which is what keeps this engine's Seam list exactly what filler removal
    published (Req 6.1-6.3).

    Returns a **sorted, de-duplicated** list, so a duplicated note cannot produce two
    windows over the same join and the planned order is canonical (Req 6.6). The
    de-duplication is on the parsed float, so ``filler_seam:1.500`` and ``filler_seam:1.5``
    are one Seam.

    Total: ``notes`` may be any iterable of anything (or not an iterable at all), and
    ``duration`` may be any value — a non-finite or non-positive duration admits no Seam,
    since there is no in-bounds time to repair.
    """
    limit = coerce_float(duration, 0.0)
    if limit <= 0.0:
        return []
    if isinstance(notes, (str, bytes)) or not isinstance(notes, Iterable):
        return []

    seams: set[float] = set()
    for note in notes:
        if not isinstance(note, str) or not note.startswith(SEAM_NOTE_PREFIX):
            continue
        text = note[len(SEAM_NOTE_PREFIX):].strip()
        try:
            value = float(text)
        except (TypeError, ValueError):
            continue                          # malformed payload (Req 6.5)
        if not math.isfinite(value):
            continue                          # "nan"/"inf" spelled as a float literal
        if value < 0.0 or value > limit:
            continue                          # out of bounds for this clip (Req 6.6)
        seams.add(value)
    return sorted(seams)


def repair_windows(
    seams: Sequence[float],
    window_ms: int,
    duration: float,
    tb: Any,
) -> list[Repair_Window]:
    """Build the sorted, disjoint :class:`Repair_Window` list for ``seams`` (Req 6.7-6.8).

    Per Seam: a **symmetric** ``window_ms`` window centred on the Seam, its bounds snapped
    to **sample** boundaries through ``Time_Base`` and clamped to ``[0, duration]``, so a
    Seam near either edge yields a shortened window instead of an out-of-bounds one
    (Req 6.7). The list then goes through the foundation ``normalize_segments``, which
    sorts, drops the degenerate, clamps again and merges overlapping *or* touching spans —
    so a Seam cluster tighter than the window width arrives as **one** window that is
    repaired exactly once (Req 6.8, 7.7).

    ``normalize_segments`` is called **without** ``time_base`` on purpose. Its optional
    snapping is to the *frame* grid, and a frame is one to two orders of magnitude coarser
    than these windows (a 12 ms window inside a 33 ms frame would snap to zero length and
    be dropped entirely). Sample snapping — the grid Req 6.7 actually names, and the grid
    the audio filtergraph works on — is therefore applied here, before normalisation, via
    ``seconds_to_sample``/``sample_to_seconds``.

    Each merged window carries every Seam it absorbed in :attr:`Repair_Window.seams`
    (Req 7.7), matched by containment with a one-sample tolerance so a Seam exactly on a
    snapped bound is not lost. A Seam whose window collapsed (an empty clip, a degenerate
    ``window_ms``) simply has no window; it stays in ``Stem_Plan.seams``, which is the
    record of what was published.

    Total: any hostile ``seams``, ``window_ms``, ``duration`` or ``tb`` yields ``[]`` or a
    correspondingly shortened list rather than an exception.
    """
    limit = coerce_float(duration, 0.0)
    if limit <= 0.0:
        return []
    ordered = _plan_floats(seams)
    if not ordered:
        return []

    time_base = tb if isinstance(tb, Time_Base) else Time_Base()
    half = coerce_int(window_ms, WINDOW_DEFAULT_MS, lo=WINDOW_MIN_MS, hi=WINDOW_MAX_MS)
    half_s = half / 2000.0                     # milliseconds -> seconds, symmetric

    def _snap(value: float) -> float:
        """Clamp into ``[0, duration]``, then snap to the nearest sample boundary."""
        bounded = min(max(value, 0.0), limit)
        return min(max(time_base.sample_to_seconds(
            time_base.seconds_to_sample(bounded)), 0.0), limit)

    records = [
        {"start": _snap(seam - half_s), "end": _snap(seam + half_s)}
        for seam in ordered
        if 0.0 <= seam <= limit
    ]
    merged = normalize_segments(records, limit)

    tolerance = 1.0 / float(max(time_base.sample_rate, 1))
    windows: list[Repair_Window] = []
    for segment in merged:
        held = tuple(
            seam for seam in ordered
            if segment.start - tolerance <= seam <= segment.end + tolerance
        )
        windows.append(
            Repair_Window(start=segment.start, end=segment.end, seams=held)
        )
    return windows


def _capability_available(caps: Any, capability_id: str) -> bool:
    """Ask an injected Capability_Report about one id; unknown answers read unavailable.

    Conservative and total: no report (``None``), a report that raises, or an answer that
    is not a clean ``True`` all read as **unavailable**, so a missing report can only push
    the ladder onto the dependency-free path — never claim a model that is not there
    (Req 12.4).
    """
    probe = getattr(caps, "available", None)
    if not callable(probe):
        return False
    try:
        return bool(probe(capability_id))
    except Exception:  # pragma: no cover - hostile injected report
        return False


def resolve_model(opts: Stem_Options) -> str:
    """The resolved separation model name — never empty (Req 10.7, 12.3).

    ``model=""`` means "the resolver picks", and this is the resolver: it picks
    :data:`_MODEL_DEFAULT`. A non-empty name is used verbatim, so the ``model:<name>``
    Capability_Id, the plan field and the marker all agree on one spelling. Non-empty
    matters because the plan is the record a reproduced run is compared against — an empty
    model name would describe no environment at all.
    """
    name = coerce_str(getattr(opts, "model", _MODEL_DEFAULT), _MODEL_DEFAULT, _MODEL_MAX_LEN)
    return name.strip() or _MODEL_DEFAULT


def resolve_backend(
    opts: Stem_Options, caps: Any, needs_separation: bool
) -> tuple[str, tuple[str, ...]]:
    """Resolve the Separator_Backend and report what was missing (Req 12.4-12.6, 13.1-13.2).

    Returns ``(backend_id, missing_capability_ids)`` where ``backend_id`` is always a
    *resolved* backend — ``"ml"`` or ``"ffmpeg"``, never ``"auto"`` — and the missing ids
    are what the ladder turns into one ``degraded:<capability_id>`` marker each (Req 13.2,
    13.7).

    The rules, in order:

    * **No separation wanted** → ``("ffmpeg", ())``. When every resolved gain is ``1.0``
      and the Repair_Mode is not ``spectral``, no Separator_Backend runs at all: the work
      is gains-free seam repair in one ffmpeg filtergraph (Req 13.4). Nothing is probed and
      **nothing is reported missing**, because a capability nobody wants is not a
      degradation — reporting it would emit a ``degraded:`` marker for a run that lost no
      fidelity.
    * **``backend="ffmpeg"`` requested** → ``("ffmpeg", ())``. The operator chose the
      approximation; there is no degradation to report.
    * **``auto`` or ``ml``** → ``"ml"`` only when **both** ``python_pkg:demucs`` and
      ``model:<resolved model>`` are available, otherwise ``"ffmpeg"`` with the unavailable
      ids reported. This is deliberately the same for ``auto`` and for an explicit ``ml``:
      Req 13.2 keys the ``degraded:`` marker on the *capability* being unavailable while
      separation was wanted, not on how the backend was requested.

    A backend that would fetch a checkpoint over the network is treated as
    **model-unavailable** here (Req 12.6) — which needs no code: the ``model:`` probe is
    the foundation's locator registry, and a locator reports available only for a model
    file already present in the local model directory (Req 12.3), so "would download" and
    "absent" are the same answer.

    Pure: the only outside contact is the injected report (see the epic note above).
    """
    requested = coerce_choice(getattr(opts, "backend", _BACKEND_DEFAULT), BACKEND_IDS,
                              _BACKEND_DEFAULT)
    if not coerce_bool(needs_separation, False) or requested == "ffmpeg":
        return "ffmpeg", ()

    required = ("python_pkg:demucs", "model:" + resolve_model(opts))
    missing = tuple(
        capability_id for capability_id in required
        if not _capability_available(caps, capability_id)
    )
    if missing:
        return "ffmpeg", missing
    return "ml", ()


def resolve_repair_mode(requested: str, backend: str) -> tuple[str, bool]:
    """Resolve the Repair_Mode against the resolved backend (Req 7.3, 7.4).

    Returns ``(mode, downgraded)``. ``spectral`` needs real stems to bridge music across
    the join, so on any non-``ml`` backend it becomes ``crossfade`` and ``downgraded`` is
    ``True`` — the caller records ``"spectral"`` in ``Stem_Plan.downgraded_from`` and emits
    ``degraded:python_pkg:demucs`` (Req 7.4). ``off`` and ``crossfade`` are never
    downgraded, on any backend.

    Total: an unrecognised ``requested`` value falls back to the documented default before
    the rule is applied.
    """
    mode = coerce_choice(requested, REPAIR_MODES, _REPAIR_MODE_DEFAULT)
    if mode == "spectral" and str(backend) != "ml":
        return "crossfade", True
    return mode, False


def separation_needed(gains: Mapping[str, float], repair_mode: Any) -> bool:
    """Whether this configuration needs the Stem_Set at all (Req 5.6, 13.4).

    ``True`` when any resolved gain differs from ``1.0`` — i.e. some stem is attenuated,
    muted or boosted, which is impossible without separating it — or when the *requested*
    Repair_Mode is ``spectral``, whose music bridging is defined per stem. The requested
    mode is what counts here, before :func:`resolve_repair_mode` may downgrade it:
    resolving the backend is what decides whether ``spectral`` is affordable, so it cannot
    also be the input to that decision.

    ``False`` is the repair-only path: all gains neutral, so no backend, no stems, no
    separation budget (Req 13.4) — and with ``repair_mode == "off"`` as well it is the
    whole-engine no-op :func:`plan_is_noop` detects.
    """
    mode = coerce_choice(repair_mode, REPAIR_MODES, _REPAIR_MODE_DEFAULT)
    if mode == "spectral":
        return True
    values = gains.values() if isinstance(gains, Mapping) else ()
    return any(_coerce_gain(gain) != GAIN_DEFAULT for gain in values)


def plan_stems(
    *,
    opts: Stem_Options,
    notes: Sequence[str] = (),
    duration: float = 0.0,
    fmt: Audio_Format | None = None,
    caps: Any = None,
    tb: Any = None,
) -> Stem_Plan:
    """Compose the resolvers above into one serialisable :class:`Stem_Plan` (Req 10.1).

    Keyword-only, exactly as designed, and a **pure function of its arguments**: no clock,
    no filesystem, no subprocess, no randomness (the plan needs none — nothing here is a
    random choice, so ``ctx.rng()`` is never drawn; Req 10.2). Equal arguments therefore
    give equal plans, which is the whole of Req 10.1, and the plan records ``backend`` and
    ``model`` so a reproduced run can name the environment it came from (Req 10.7).

    Composition order matters and is forced by the dependencies:

    1. :func:`resolve_gains` — the preset/field rules.
    2. :func:`separation_needed` on those gains plus the **requested** repair mode.
    3. :func:`resolve_backend` — needs (2) to know whether a missing capability is a
       degradation at all.
    4. :func:`resolve_repair_mode` — needs (3), since ``spectral`` survives only on ``ml``.
    5. :func:`parse_seam_notes`, then :func:`repair_windows` — the Seam intake and the
       merged window list.

    Seams are parsed even when the resolved mode is ``off``, so the plan is an honest
    record of what filler removal published; the **windows** are what ``off`` empties, and
    an empty window list is what the emitters read as "no repair to do" (Req 7.10).

    ``fmt`` is the probed :class:`Audio_Format` when there is one. There is none while
    ``plan`` runs — planning is pure and cannot probe — so the plan then carries the
    ``Time_Base`` sample rate and :data:`_CHANNELS_DEFAULT`, and ``run`` re-plans with the
    real format after pass 0. ``duration`` is the clip-relative upper bound, so every
    timestamp in the plan lies inside ``[0, duration]`` (Req 2.3, 2.8).
    """
    options = opts if isinstance(opts, Stem_Options) else resolve_stem_options(opts)
    time_base = tb if isinstance(tb, Time_Base) else Time_Base()
    limit = coerce_float(duration, 0.0)

    gains = resolve_gains(options)
    wants_stems = separation_needed(gains, options.repair_mode)
    backend, missing = resolve_backend(options, caps, wants_stems)
    repair_mode, downgraded = resolve_repair_mode(options.repair_mode, backend)

    seams = parse_seam_notes(notes, limit)
    windows: tuple[Repair_Window, ...] = ()
    if repair_mode != "off":
        windows = tuple(
            repair_windows(seams, options.repair_window_ms, limit, time_base)
        )

    if repair_mode == "spectral":
        bridged = min(len(windows), MAX_BRIDGE_WINDOWS)
    else:
        bridged = 0
    notched = len(windows) - bridged

    sample_rate = getattr(fmt, "sample_rate", None)
    channels = getattr(fmt, "channels", None)
    return Stem_Plan(
        backend=backend,
        model=resolve_model(options),
        gains=gains,
        active_stems=tuple(name for name in STEM_NAMES if gains[name] > GAIN_MIN),
        repair_mode=repair_mode,
        repair_window_ms=options.repair_window_ms,
        seams=tuple(seams),
        windows=windows,
        sample_rate=time_base.sample_rate if sample_rate is None else sample_rate,
        channels=_CHANNELS_DEFAULT if channels is None else channels,
        duration=limit,
        declick=options.declick,
        needs_separation=wants_stems,
        missing_capabilities=missing,
        downgraded_from="spectral" if downgraded else "",
        bridged_windows=bridged,
        notched_windows=notched,
    )


def plan_stems_from_context(ctx: Any) -> Stem_Plan:
    """Plan from an Engine_Context — the body of the engine's ``plan(ctx)`` (Req 1.9).

    Module-level for the same reason :func:`resolve_stem_options` is:
    ``Stem_Inpainting_Engine`` arrives in **task 13.1**, and its hook is to be a one-line
    delegation::

        def plan(self, ctx: Engine_Context) -> Stem_Plan:
            return plan_stems_from_context(ctx)

    Reads exactly six fields — ``options``, ``notes``, ``duration``, ``time_base``,
    ``capabilities`` and nothing else — and in particular **never reads
    ``ctx.source_path``** and never touches ``ctx.clip_path``: every timestamp it produces
    comes from ``[0, ctx.duration]`` and from the rebased, clip-relative notes, so no
    source-relative time can reach the audio processing (Req 2.3).

    ``ctx.options`` is already this engine's resolved :class:`Stem_Options` (the host calls
    ``resolve_options`` before building the context); anything else — a raw
    Processing_Options, ``None``, a hostile object — is resolved here, so the function is
    total and idempotent either way. ``ctx`` itself is never written to (Req 1.3): the
    contact is ``getattr`` only.

    ``fmt`` is deliberately omitted: probing is a subprocess and ``plan`` is pure
    (Req 1.9, 12.5, 19.2).
    """
    return plan_stems(
        opts=getattr(ctx, "options", None),
        notes=getattr(ctx, "notes", ()) or (),
        duration=getattr(ctx, "duration", 0.0),
        fmt=None,
        caps=getattr(ctx, "capabilities", None),
        tb=getattr(ctx, "time_base", None),
    )


def plan_is_noop(plan: Stem_Plan) -> bool:
    """Whether this plan asks for nothing at all — ladder rung 3's gate (Req 5.6, 7.10).

    ``True`` when every resolved gain is exactly :data:`GAIN_DEFAULT` **and** the resolved
    Repair_Mode is ``off``: the output would be a bit-for-bit copy of the input, so rung 3
    returns ``skipped`` **before** any probe, any workspace file and any subprocess — which
    is what makes the no-op configuration cost nothing (Req 5.6, 15.8).

    Note what is *not* consulted: an empty Seam list is **not** a no-op. A clip with no
    Seams and a non-neutral gain bundle still has real mixing work to do; only the gains
    and the mode decide.

    Total: any object is accepted, and anything that does not look like a plan reads as
    "not a no-op", so a malformed value can never silently skip the engine.
    """
    gains = getattr(plan, "gains", None)
    if not isinstance(gains, Mapping) or set(gains) != set(STEM_NAMES):
        return False
    if getattr(plan, "repair_mode", "") != "off":
        return False
    return all(_coerce_gain(gains[name]) == GAIN_DEFAULT for name in STEM_NAMES)



# --------------------------------------------------------------------------- #
# The Separator_Backend seam (task 8.1)                                       #
# --------------------------------------------------------------------------- #
# One audio file in, per-Backend_Stem files out — nothing else. The protocol is
# **file-based on purpose** (Req 4.5): it makes the ffmpeg adapter a first-class
# implementation rather than a special case, and it lets a test double be a few lines of
# ``wave`` with no numeric stack installed (Req 19.1).

#: The injectable subprocess seam every ffmpeg/ffprobe invocation goes through, so tests
#: record commands instead of executing them (Req 19.1) and every call carries an
#: **explicit** timeout (Req 15.4). ``Callable[[Sequence[str], float],
#: subprocess.CompletedProcess]``.
Command_Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess"]


class Stem_Error(RuntimeError):
    """Base class for this engine's own failures, so a caller can catch the family.

    Deliberately a ``RuntimeError`` subclass: the Engine_Host already isolates *every*
    exception into one ``engine:<id>:failed`` marker (foundation Req 8.1), so these types
    exist to let the ladder tell its rungs apart, not to escape.
    """


class Model_Unavailable(Stem_Error):
    """The separation checkpoint is not present locally (Req 12.4, 12.6).

    Raised by the ML Separator_Backend **before importing anything**, so a missing model
    costs no ``torch`` import — and raised rather than downloaded, because a backend that
    would fetch a checkpoint over the network is treated as model-unavailable (Req 12.6,
    16.1). The ladder turns it into ``degraded:model:<name>`` plus the ffmpeg fallback.
    """


class Invalid_Audio_Format(Stem_Error):
    """The probed audio format is unusable — zero/negative/absent rate or channels (17.5).

    Distinct from "no audio stream at all", which is not an error: that case yields ``None``
    from the prober and the engine skips (Req 4.8). This one means there *is* a stream and
    its declared format cannot be worked with, which the ladder reports as
    ``degraded:audio_format``.
    """


class Integrity_Error(Stem_Error):
    """Produced audio failed verification — wrong format, wrong duration, or unreadable.

    Raised by :func:`assemble_stem_set` for a backend file that does not match the probed
    :class:`Audio_Format` (Req 4.6, 14.2) and, in task 12.1, by ``verify_replacement`` for a
    candidate whose duration or stream layout drifted (Req 3.5, 17.1). Either way the
    partial output is discarded and the engine reports ``failed`` — never a silently wrong
    clip.
    """


class Separator_Backend(Protocol):
    """One audio file in, per-Backend_Stem files out (Req 4.5, 19.1).

    Implemented by ``ML_Separator_Backend`` (task 9.1), ``Ffmpeg_Separator_Backend``
    (task 9.2) and the ``tests.fakes`` doubles. Structural, so no double needs to import
    this module.

    ``separate`` returns ``{Backend_Stem name: wav path}`` — *backend* names such as
    ``drums``/``bass``, not Stem_Names: :func:`assemble_stem_set` is what maps them through
    :data:`STEM_MAPPING`, sums the collisions and substitutes silence for an omission
    (Req 4.2, 4.3). A backend **raises** on failure; the engine converts that into
    ``Engine_Status.failed`` (Req 14.2) rather than guessing at partial output.

    :attr:`requires_network` is what the permissibility rung consults *before* calling
    ``separate`` (Req 16.3), which is why it is a declared attribute and not a return value.
    """

    #: ``"ml"`` | ``"ffmpeg"`` | a fake id in tests.
    backend_id: str
    #: Declared, not discovered: consulted before any work happens (Req 16.3).
    requires_network: bool

    def separate(
        self,
        source: Path,
        dest_dir: Path,
        *,
        fmt: Audio_Format,
        seed: int,
        timeout_s: float,
    ) -> Mapping[str, Path]:
        """Separate ``source`` into ``dest_dir``, preserving ``fmt`` exactly (Req 4.6)."""
        ...  # pragma: no cover - protocol declaration


def _ffmpeg_binary() -> str:
    """The configured ffmpeg binary, resolved **lazily** (Req 1.4).

    ``config`` is imported inside the function, never at module scope, so this module still
    imports with no ``pydantic-settings`` installed; the configured binary is always used
    rather than a hard-coded ``"ffmpeg"`` — the same rule
    ``worker.engines.capabilities`` follows.
    """
    try:
        from config import settings  # lazy (Req 1.4)

        binary = str(getattr(settings, "ffmpeg_binary", "") or "").strip()
    except Exception:  # pragma: no cover - config unavailable in a minimal install
        binary = ""
    return binary or "ffmpeg"


def _run(runner: Any, cmd: Sequence[str], timeout_s: float) -> Any:
    """Invoke ``runner`` with an explicit timeout, re-raising failures as ``FFmpegError``.

    Two guarantees, both required:

    * **an explicit, positive timeout on every invocation** (Req 15.4) — the value is
      floored at :data:`MIN_STEP_TIMEOUT_S`, so no subprocess is ever launched with a
      non-positive or missing budget;
    * **one failure type** (Req 14.3) — a non-zero return code, a
      ``subprocess.TimeoutExpired``, an ``OSError`` or anything else the runner raises
      leaves this function as ``worker.ffmpeg_utils.FFmpegError``, which the host isolates
      into a single ``failed`` marker. ``FFmpegError`` is imported lazily so module import
      stays free of ``worker.ffmpeg_utils`` (Req 1.4), and a ``TimeoutExpired`` is
      re-raised unchanged so the budget rung can tell a timeout from a failure (Req 15.6).

    Returns the runner's ``CompletedProcess`` on success.
    """
    from worker.ffmpeg_utils import FFmpegError  # lazy (Req 1.4)

    argv = [str(part) for part in cmd]
    budget = max(coerce_float(timeout_s, MIN_STEP_TIMEOUT_S), MIN_STEP_TIMEOUT_S)
    try:
        completed = runner(argv, budget)
    except subprocess.TimeoutExpired:
        raise
    except FFmpegError:
        raise
    except Exception as exc:  # noqa: BLE001 - one failure type for the host (Req 14.3)
        raise FFmpegError(f"{argv[0]} failed: {exc}") from exc

    code = getattr(completed, "returncode", 0)
    if code not in (0, None):
        detail = str(getattr(completed, "stderr", "") or "").strip()[:400]
        raise FFmpegError(f"{argv[0]} exited {code}: {detail}")
    return completed


def injected(ctx: Any, key: str, fallback: Any = None) -> Any:
    """Read one injected collaborator from ``Engine_Context.deps`` (Req 19.1, 12.7).

    The engine's constructor keywords (``backend``, ``runner``, ``prober``) are the primary
    seam; ``ctx.deps`` is the per-invocation override the host and the tests use, and it
    wins when both are present because it is the more specific one. Known keys for this
    engine: ``"backend"`` (a :class:`Separator_Backend`), ``"runner"`` (a
    :data:`Command_Runner`), ``"prober"`` (the ``Audio_Format`` reader) and ``"capabilities"``
    (a Capability_Report) — plus the host's own ``"clock"``, ``"logger"`` and ``"storage"``.

    Total and read-only: a missing key, a ``deps`` that is not a mapping or a hostile
    ``__getitem__`` all yield ``fallback``.
    """
    deps = getattr(ctx, "deps", None)
    if not isinstance(deps, Mapping):
        return fallback
    try:
        value = deps.get(key, None)
    except Exception:  # pragma: no cover - hostile mapping
        return fallback
    return fallback if value is None else value


# --------------------------------------------------------------------------- #
# Stem-set assembly (task 8.2)                                                #
# --------------------------------------------------------------------------- #

#: Tolerance on a stem's length, in **audio frames**, when it is checked against the
#: planned duration (Req 4.6, 17.1's "one audio frame"). One frame, not a percentage: a
#: backend that rounds the last sample differently is fine, a backend that returns half the
#: clip is an :class:`Integrity_Error`.
_DURATION_TOLERANCE_FRAMES = 1


def _wav_format(path: Any) -> tuple[int, int, int] | None:
    """``(sample_rate, channels, frames)`` of a PCM WAV, or ``None`` when unreadable.

    Reads the RIFF header directly rather than shelling out to ``ffprobe``: the Stem_Set
    files are always PCM WAVs written by this engine or by a backend under our own
    ``-c:a pcm_s16le``, so the header is authoritative, and reading it costs no subprocess
    and no budget (Req 15.4's timeout discipline applies to processes we do launch — the
    cheapest check is the one that launches none).

    The header is parsed with :mod:`struct` instead of the standard library ``wave`` module
    on purpose: ffmpeg writes ``WAVE_FORMAT_EXTENSIBLE`` (format tag ``0xFFFE``) as soon as
    there are more than two channels, and ``wave`` refuses that tag — a 5.1 clip would read
    as "not a readable WAV" and be rejected as an :class:`Integrity_Error` even though the
    file is perfectly good. Chunks are walked so a ``LIST``/``fact`` chunk between ``fmt``
    and ``data`` is skipped rather than misread.

    A missing file, a non-RIFF file, a truncated header, an unsupported layout or an OS
    error all read as ``None``, which the caller turns into an :class:`Integrity_Error`.
    """
    try:
        with open(str(path), "rb") as handle:
            header = handle.read(12)
            if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None
            channels = sample_rate = block_align = 0
            frames = None
            while True:
                chunk = handle.read(8)
                if len(chunk) < 8:
                    break
                name, size = struct.unpack("<4sI", chunk)
                if name == b"fmt " and size >= 16:
                    body = handle.read(size + (size & 1))
                    if len(body) < 16:
                        return None
                    # fmt body: format tag (0:2), channels (2:4), sample rate (4:8),
                    # byte rate (8:12), block align (12:14), bits per sample (14:16).
                    channels, sample_rate, _byte_rate, block_align = struct.unpack(
                        "<HIIH", body[2:14]
                    )
                elif name == b"data":
                    frames = size // block_align if block_align else None
                    break
                else:
                    handle.seek(size + (size & 1), 1)
            if not channels or not sample_rate or frames is None:
                return None
            return int(sample_rate), int(channels), int(frames)
    except Exception:
        return None


def _verify_stem_file(path: Any, name: str, fmt: Audio_Format, duration: float) -> None:
    """Raise :class:`Integrity_Error` unless ``path`` matches ``fmt`` and ``duration``.

    The Req 4.6 check, applied to every file that reaches the Stem_Set: same sample rate,
    same channel count, and a frame count within :data:`_DURATION_TOLERANCE_FRAMES` of
    ``duration``. Anything else means the backend did not preserve the Audio_Format, which
    the engine reports as ``failed`` with no media rather than mixing stems of different
    lengths together (Req 14.2).

    A file that does not exist is **not** checked here: the only way one is missing is that
    an injected recording runner never executed the command that would have written it
    (the Req 19.1 test seam), and a *real* runner failure has already raised ``FFmpegError``
    inside :func:`_run`. This keeps assembly fully testable offline without weakening the
    check on backend-produced files, which always exist.
    """
    try:
        exists = Path(str(path)).exists()
    except Exception:  # pragma: no cover - hostile path
        exists = False
    if not exists:
        return

    probed = _wav_format(path)
    if probed is None:
        raise Integrity_Error(f"stem {name} is not a readable WAV: {path}")
    rate, channels, frames = probed
    if rate != int(fmt.sample_rate) or channels != int(fmt.channels):
        raise Integrity_Error(
            f"stem {name} format {rate}Hz/{channels}ch != "
            f"{int(fmt.sample_rate)}Hz/{int(fmt.channels)}ch"
        )
    expected = int(round(max(coerce_float(duration, 0.0), 0.0) * max(rate, 1)))
    if abs(frames - expected) > _DURATION_TOLERANCE_FRAMES:
        raise Integrity_Error(
            f"stem {name} is {frames} frames, expected {expected} (+/-"
            f"{_DURATION_TOLERANCE_FRAMES})"
        )


def _prepared(dest: Path) -> Path:
    """Ensure ``dest``'s parent directory exists, returning ``dest`` unchanged.

    ffmpeg does not create directories, and the Engine_Workspace sub-directory a stem is
    written into (``stems/``) may not exist yet. A failure to create it is swallowed: the
    ffmpeg invocation that follows will fail with its own ``FFmpegError``, which is the one
    error the host should see (Req 11.6, 14.3).
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - reported by the ffmpeg failure that follows
        pass
    return dest


def _silence_command(dest: Path, fmt: Audio_Format, duration: float) -> list[str]:
    """The ``anullsrc`` argv writing digital silence of ``duration`` at ``fmt`` (Req 4.3).

    Deterministic by construction: the layout is derived from ``fmt.channels``, the length
    is formatted to microsecond precision, and the codec/rate/channels are pinned — so the
    same missing stem produces the same command on every run and on every machine, which is
    what keeps the emitted filtergraph strings comparable (Req 4.9, 10.6).
    """
    channels = max(int(fmt.channels), 1)
    layout = "mono" if channels == 1 else "stereo"
    return [
        _ffmpeg_binary(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout={layout}:sample_rate={int(fmt.sample_rate)}",
        "-t", f"{max(coerce_float(duration, 0.0), 0.0):.6f}",
        "-c:a", "pcm_s16le",
        "-ar", str(int(fmt.sample_rate)),
        "-ac", str(channels),
        str(dest),
    ]


def _sum_command(sources: Sequence[Path], dest: Path, fmt: Audio_Format) -> list[str]:
    """The ``amix`` argv summing ``sources`` into ``dest`` without normalisation.

    ``normalize=0`` is the whole point: ``amix``'s default divides by the input count, which
    would silently halve ``drums + bass`` instead of summing them and would break the
    additive decomposition (Req 4.7). ``dropout_transition=0`` keeps a shorter input from
    ramping the others, so the sum stays sample-exact.

    Inputs are added in the order given — the caller supplies them in ``sorted`` Backend_Stem
    order — so the emitted string is independent of the backend's dict iteration order
    (Req 4.9).
    """
    argv = [_ffmpeg_binary(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for source in sources:
        argv += ["-i", str(source)]
    labels = "".join(f"[{index}:a]" for index in range(len(sources)))
    argv += [
        "-filter_complex",
        f"{labels}amix=inputs={len(sources)}:normalize=0:dropout_transition=0[sum]",
        "-map", "[sum]",
        "-c:a", "pcm_s16le",
        "-ar", str(int(fmt.sample_rate)),
        "-ac", str(max(int(fmt.channels), 1)),
        str(dest),
    ]
    return argv


def assemble_stem_set(
    raw: Mapping[str, Path],
    *,
    dest_dir: Path,
    fmt: Audio_Format,
    duration: float,
    runner: Any,
    timeout_s: float,
) -> tuple[dict[str, Path], tuple[str, ...]]:
    """Map Backend_Stems onto the Stem_Set, summing collisions (Req 4.1-4.3, 4.6, 4.9).

    Returns ``({stem_name: path}, marker_details)`` where the mapping has **exactly** the
    three :data:`STEM_NAMES` keys, in that (sorted) order, and the details are the
    ``stem_missing:<stem_name>`` entries the caller namespaces into markers (Req 4.3).

    How the mapping works:

    * Contributors are collected by iterating ``sorted(raw)`` and routing each Backend_Stem
      through :data:`STEM_MAPPING`, **falling back to identity when the name is already a
      Stem_Name** — which is how the ffmpeg backend's ``music`` output survives without
      adding a self-entry to the fixed Req 4.2 table (see the note on
      :data:`STEM_MAPPING`). A name in neither place contributes to nothing (Req 4.3).
    * ``drums`` and ``bass`` therefore both land on ``music`` and are **summed** in one
      ``amix=normalize=0`` pass, not averaged and not dropped (Req 4.2).
    * A Stem_Name with a single contributor uses that file **as-is** — no re-encode, so no
      resampling artefact and no wasted media pass.
    * A Stem_Name with no contributor is written as digital silence of ``duration`` at
      ``fmt`` via ``anullsrc`` and reported as ``stem_missing:<name>`` (Req 4.3), so the mix
      always has three inputs and a backend omission can never mute the whole clip.

    Determinism (Req 4.9): both loops are over sorted sequences — ``sorted(raw)`` for the
    grouping and :data:`STEM_NAMES` for the output — so the returned mapping, the file
    names, the order of ``-i`` inputs and every emitted argv are identical across
    permutations of the backend's dict iteration order.

    Verification (Req 4.6, 14.2): every contributor file is checked against ``fmt`` and
    ``duration`` *before* it is used, and every file this function writes is checked after —
    a mismatch raises :class:`Integrity_Error`, which the engine reports as ``failed``.

    Raises:
        Integrity_Error: a stem file is unreadable, at the wrong format, or the wrong
            length.
        worker.ffmpeg_utils.FFmpegError: an ffmpeg invocation failed (via :func:`_run`).
    """
    if not isinstance(fmt, Audio_Format):
        raise Invalid_Audio_Format("assemble_stem_set requires a probed Audio_Format")
    length = max(coerce_float(duration, 0.0), 0.0)
    destination = Path(str(dest_dir))
    source = raw if isinstance(raw, Mapping) else {}

    # 1. Group Backend_Stems onto Stem_Names, in sorted Backend_Stem order (Req 4.9).
    contributors: dict[str, list[Path]] = {name: [] for name in STEM_NAMES}
    for backend_stem in sorted(str(key) for key in source):
        target = STEM_MAPPING.get(backend_stem)
        if target is None and backend_stem in STEM_NAMES:
            target = backend_stem              # identity fallback (see STEM_MAPPING)
        if target is None:
            continue                           # unknown name contributes to nothing
        path = source.get(backend_stem)
        if path is None:
            continue
        contributors[target].append(Path(str(path)))

    # 2. Resolve each Stem_Name, in sorted Stem_Name order (Req 4.1, 4.9).
    stem_set: dict[str, Path] = {}
    details: list[str] = []
    for name in STEM_NAMES:
        paths = contributors[name]
        for path in paths:
            _verify_stem_file(path, name, fmt, length)

        if not paths:
            target = _prepared(destination / f"{name}.wav")
            _run(runner, _silence_command(target, fmt, length), timeout_s)
            details.append(f"stem_missing:{name}")
        elif len(paths) == 1:
            target = paths[0]
        else:
            target = _prepared(destination / f"{name}.wav")
            _run(runner, _sum_command(paths, target, fmt), timeout_s)

        _verify_stem_file(target, name, fmt, length)
        stem_set[name] = target

    return stem_set, tuple(details)
