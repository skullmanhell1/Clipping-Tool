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
:class:`Integrity_Error` types and :func:`assemble_stem_set` (tasks 8.1-8.2), the two
backend adapters — :class:`ML_Separator_Backend` with its :func:`_locate_model` locator and
:class:`Ffmpeg_Separator_Backend`, the candid mid/speech-band approximation (tasks 9.1-9.2)
— and the whole ffmpeg pipeline: :func:`probe_audio_format` and :func:`step_timeout`
(task 11.1), :func:`extract_clip_audio` (task 11.2), the gain + repair filtergraph
:func:`build_mix_graph` / :func:`render_mix` with :func:`notch_filters` (task 11.3), the
spectral per-stem repair and music bridging :func:`bridge_music_stem` (task 11.4), and
:func:`remux_replacement` (task 11.5), and the integrity gate — :class:`Media_Probe`,
:func:`probe_media` and :func:`verify_replacement` (task 12.1). The engine class, its ``run``
gate / degradation ladder and its registration arrive in epic 13.

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
import json
import math
import os
import struct
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from worker.engines import registry as engine_registry
from worker.engines.base import (
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
    marker,
)
from worker.engines.capabilities import MODEL_LOCATORS
from worker.engines.timebase import Time_Base, normalize_segments

__all__ = [
    "ALIMITER_CAPABILITY",
    "AMPLITUDE_TOLERANCE",
    "Audio_Format",
    "BACKEND_IDS",
    "Command_Runner",
    "DISK_BOUND_MULTIPLE",
    "ENGINE_ID",
    "EXTRACT_RESERVE_S",
    "Ffmpeg_Separator_Backend",
    "GAIN_DEFAULT",
    "GAIN_MAX",
    "GAIN_MIN",
    "Integrity_Error",
    "Invalid_Audio_Format",
    "MAX_BRIDGE_WINDOWS",
    "MIN_STEP_TIMEOUT_S",
    "MIX_PRESETS",
    "MIX_PRESET_CHOICES",
    "ML_Separator_Backend",
    "Media_Probe",
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
    "SPECTRAL_HALF_WIDTH_SCALE",
    "STEM_MAPPING",
    "STEM_NAMES",
    "Separator_Backend",
    "Stem_Error",
    "Stem_Inpainting_Engine",
    "Stem_Options",
    "Stem_Plan",
    "WINDOW_DEFAULT_MS",
    "WINDOW_MAX_MS",
    "WINDOW_MIN_MS",
    "assemble_stem_set",
    "bridge_music_stem",
    "build_bridge_graph",
    "build_mix_graph",
    "extract_clip_audio",
    "extract_command",
    "injected",
    "mix_command",
    "notch_filters",
    "partition_bridge_windows",
    "probe_audio_format",
    "probe_media",
    "remux_codec",
    "remux_command",
    "remux_replacement",
    "render_mix",
    "resolve_peak_guard",
    "step_remaining",
    "step_timeout",
    "verify_replacement",
    "parse_seam_notes",
    "plan_has_work",
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

    mix_preset: str = _MIX_PRESET_DEFAULT  # one of MIX_PRESET_CHOICES
    gain_vocals: float = GAIN_DEFAULT  # [GAIN_MIN, GAIN_MAX]
    gain_music: float = GAIN_DEFAULT
    gain_other: float = GAIN_DEFAULT
    repair_mode: str = _REPAIR_MODE_DEFAULT  # one of REPAIR_MODES
    repair_window_ms: int = WINDOW_DEFAULT_MS  # [WINDOW_MIN_MS, WINDOW_MAX_MS]
    declick: bool = False
    backend: str = _BACKEND_DEFAULT  # one of BACKEND_IDS
    model: str = _MODEL_DEFAULT
    retain_stems: bool = False  # durable per-stem WAVs (Req 11.3)

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
    def parse(cls, data: Mapping[str, Any] | None) -> Stem_Options:
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
    def from_processing_options(cls, options: Any) -> Stem_Options:
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

    start: float = 0.0  # clip-relative seconds, sample-snapped
    end: float = 0.0  # clip-relative seconds, ``>= start``
    seams: tuple[float, ...] = ()  # the Seam(s) merged into this window (Req 7.7)

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

    backend: str  # "ml" | "ffmpeg" (Req 10.7)
    model: str  # resolved model name (Req 10.7)
    gains: dict[str, float]  # keyed by STEM_NAMES, sorted
    active_stems: tuple[str, ...]  # gain > 0.0 only (Req 5.7)
    repair_mode: str  # resolved, post-downgrade
    repair_window_ms: int  # [WINDOW_MIN_MS, WINDOW_MAX_MS]
    seams: tuple[float, ...]  # normalised, in-bounds (Req 6.6)
    windows: tuple[Repair_Window, ...]  # sorted, disjoint (Req 6.8)
    sample_rate: int
    channels: int
    duration: float
    declick: bool
    needs_separation: bool  # gain != 1.0 or spectral repair
    missing_capabilities: tuple[str, ...]
    downgraded_from: str = ""  # "spectral" when rung 9 fired
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
    if bundle is None:  # "custom" — the individual fields
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
        text = note[len(SEAM_NOTE_PREFIX) :].strip()
        try:
            value = float(text)
        except (TypeError, ValueError):
            continue  # malformed payload (Req 6.5)
        if not math.isfinite(value):
            continue  # "nan"/"inf" spelled as a float literal
        if value < 0.0 or value > limit:
            continue  # out of bounds for this clip (Req 6.6)
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
    half_s = half / 2000.0  # milliseconds -> seconds, symmetric

    def _snap(value: float) -> float:
        """Clamp into ``[0, duration]``, then snap to the nearest sample boundary."""
        bounded = min(max(value, 0.0), limit)
        return min(
            max(time_base.sample_to_seconds(time_base.seconds_to_sample(bounded)), 0.0), limit
        )

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
            seam for seam in ordered if segment.start - tolerance <= seam <= segment.end + tolerance
        )
        windows.append(Repair_Window(start=segment.start, end=segment.end, seams=held))
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
    requested = coerce_choice(
        getattr(opts, "backend", _BACKEND_DEFAULT), BACKEND_IDS, _BACKEND_DEFAULT
    )
    if not coerce_bool(needs_separation, False) or requested == "ffmpeg":
        return "ffmpeg", ()

    required = ("python_pkg:demucs", "model:" + resolve_model(opts))
    missing = tuple(
        capability_id
        for capability_id in required
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
        windows = tuple(repair_windows(seams, options.repair_window_ms, limit, time_base))

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


def plan_has_work(plan: Stem_Plan) -> bool:
    """Whether this **resolved** plan would change the audio at all (Req 7.10, 7.11).

    The post-probe companion to :func:`plan_is_noop`, and the second half of the idempotence
    guarantee. The two differ in *when* they can be asked, which is why both exist:

    * :func:`plan_is_noop` is answerable **before** probing — it reads only the gains and the
      Repair_Mode — so it is rung 3 and costs nothing.
    * this one needs the **windows**, which need the probed sample rate to snap against, so it
      can only be asked after pass 0. It catches the case ``plan_is_noop`` structurally
      cannot: ``repair_mode`` is ``crossfade`` (so not a no-op by rung 3's test) but the clip
      published **no Seams**, so there is no window to repair and, with unity gains, nothing
      whatsoever to do.

    That case is exactly what re-running the engine on its own Replacement_Media looks like,
    and returning ``False`` here is what makes Req 7.11 true *by construction*: the second run
    is skipped, so it cannot change the audio. Getting there via a skip rather than via a
    "byte-stable re-render" matters, because the remux pass re-encodes to a lossy codec — a
    re-render would decode *slightly* differently no matter how careful the filtergraph was.

    ``declick`` is deliberately **work**, even with no Seams. It is not seam-driven: it is an
    explicit request to fade the clip's own head and tail, and honouring a request the operator
    made is not a "second repair pass". A caller who wants strict idempotence leaves it off.

    Total: anything that does not look like a plan reads as "has work", so a malformed value
    can never silently skip the engine.
    """
    gains = getattr(plan, "gains", None)
    if not isinstance(gains, Mapping) or set(gains) != set(STEM_NAMES):
        return True
    if getattr(plan, "declick", False):
        return True
    if getattr(plan, "windows", ()):
        return True
    return any(_coerce_gain(gains[name]) != GAIN_DEFAULT for name in STEM_NAMES)


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
    except Exception as exc:  # one failure type for the host (Req 14.3)
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
            f"stem {name} is {frames} frames, expected {expected} (+/-{_DURATION_TOLERANCE_FRAMES})"
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
        _ffmpeg_binary(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=channel_layout={layout}:sample_rate={int(fmt.sample_rate)}",
        "-t",
        f"{max(coerce_float(duration, 0.0), 0.0):.6f}",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(int(fmt.sample_rate)),
        "-ac",
        str(channels),
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
        "-map",
        "[sum]",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(int(fmt.sample_rate)),
        "-ac",
        str(max(int(fmt.channels), 1)),
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
            target = backend_stem  # identity fallback (see STEM_MAPPING)
        if target is None:
            continue  # unknown name contributes to nothing
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


# --------------------------------------------------------------------------- #
# PCM WAV read/write helpers (tasks 9.1, 9.2)                                 #
# --------------------------------------------------------------------------- #
# The Stem_Set is always 16-bit PCM WAV (this engine writes every intermediate with
# ``-c:a pcm_s16le``), so reading and writing one needs no numeric stack and no
# subprocess. :func:`_wav_format` above already parses the header; these two add the
# payload side, which the ML adapter needs to hand samples to ``demucs`` and to write the
# separated stems back out at the requested Audio_Format.

#: Bytes per sample in the PCM representation every intermediate uses (``pcm_s16le``).
_SAMPLE_WIDTH = 2

#: Full-scale divisor for the int16 representation. ``32768`` (not ``32767``) so the
#: negative rail maps exactly to ``-1.0`` and the round-trip is symmetric.
_FULL_SCALE = 32768.0


def _read_wav_payload(path: Any) -> tuple[int, int, bytes] | None:
    """``(sample_rate, channels, frame_bytes)`` of a PCM WAV, or ``None`` if unreadable.

    The payload companion to :func:`_wav_format`: same chunk-walking tolerance (a
    ``LIST``/``fact`` chunk between ``fmt`` and ``data`` is skipped, and ffmpeg's
    ``WAVE_FORMAT_EXTENSIBLE`` tag is accepted), but it returns the ``data`` chunk's bytes
    rather than a frame count.

    Only 16-bit PCM is accepted, because that is the only thing this engine ever writes; a
    file at any other sample width reads as ``None``, which the caller turns into an
    :class:`Integrity_Error` rather than silently misinterpreting the samples.
    """
    try:
        with open(str(path), "rb") as handle:
            header = handle.read(12)
            if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None
            channels = sample_rate = bits = 0
            while True:
                chunk = handle.read(8)
                if len(chunk) < 8:
                    return None
                name, size = struct.unpack("<4sI", chunk)
                if name == b"fmt " and size >= 16:
                    body = handle.read(size + (size & 1))
                    if len(body) < 16:
                        return None
                    channels, sample_rate = struct.unpack("<HI", body[2:8])
                    (bits,) = struct.unpack("<H", body[14:16])
                elif name == b"data":
                    payload = handle.read(size)
                    if not channels or not sample_rate or bits != _SAMPLE_WIDTH * 8:
                        return None
                    return int(sample_rate), int(channels), payload
                else:
                    handle.seek(size + (size & 1), 1)
    except Exception:
        return None


def _write_pcm_wav(dest: Path, payload: bytes, fmt: Audio_Format) -> Path:
    """Write ``payload`` as a canonical 16-bit PCM WAV at ``fmt``, returning ``dest``.

    A 44-byte canonical header (``RIFF``/``WAVE``/``fmt ``/``data``, format tag ``1``) is
    emitted rather than anything ``WAVE_FORMAT_EXTENSIBLE``, so the result is readable by
    :func:`_wav_format`, by the standard-library ``wave`` module (which is what the test
    doubles use) and by ffmpeg alike.

    The parent directory is created via :func:`_prepared`. Any ``OSError`` propagates: a
    stem we cannot write is a real failure, and the ladder reports it as ``failed`` rather
    than mixing a stem that is not there.
    """
    channels = max(int(fmt.channels), 1)
    rate = max(int(fmt.sample_rate), 1)
    block_align = channels * _SAMPLE_WIDTH
    target = _prepared(Path(str(dest)))
    with open(target, "wb") as handle:
        handle.write(b"RIFF")
        handle.write(struct.pack("<I", 36 + len(payload)))
        handle.write(b"WAVEfmt ")
        handle.write(
            struct.pack(
                "<IHHIIHH",
                16,  # fmt chunk size
                1,  # WAVE_FORMAT_PCM
                channels,
                rate,
                rate * block_align,  # byte rate
                block_align,
                _SAMPLE_WIDTH * 8,  # bits per sample
            )
        )
        handle.write(b"data")
        handle.write(struct.pack("<I", len(payload)))
        handle.write(payload)
    return target


# --------------------------------------------------------------------------- #
# The model locator (task 9.1)                                                #
# --------------------------------------------------------------------------- #


def _model_dir(explicit: Any = None) -> Path:
    """The local directory searched for separation checkpoints (Req 12.3).

    ``explicit`` wins when given (the adapter's ``model_dir`` constructor keyword, which is
    the test seam); otherwise the :data:`MODEL_DIR_ENV` environment variable, otherwise
    :data:`MODEL_DIR_DEFAULT`. Reading ``os.environ`` is not filesystem access, so this is
    still safe to call from a locator that must not touch the network or import anything.
    """
    if explicit is not None:
        return Path(str(explicit))
    return Path(os.environ.get(MODEL_DIR_ENV, str(MODEL_DIR_DEFAULT)))


def _locate_model(name: str, model_dir: Any = None) -> Path | None:
    """The local checkpoint path for ``name``, or ``None`` when it is not present.

    Two documented layouts are accepted, in this order (Req 12.3):

    1. ``<dir>/<name>.th`` — a single-file checkpoint;
    2. ``<dir>/<name>/model.th`` — a checkpoint directory.

    **Stats the filesystem and nothing else.** No import, no subprocess, no network — which
    is what makes it legal as a :data:`MODEL_LOCATORS` entry (the capability layer calls
    locators during probing, and a probe that could download would make
    ``model:<name>`` mean "available *after* a fetch" instead of "present locally";
    Req 12.5, 12.6, 21.5).

    Total: an empty/non-string name, a directory that does not exist, and an ``OSError``
    from a hostile path all read as ``None``, i.e. "not available".
    """
    stem = str(name or "").strip()
    if not stem:
        return None
    base = _model_dir(model_dir)
    for candidate in (base / f"{stem}.th", base / stem / "model.th"):
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - hostile path
            continue
    return None


#: Register the default model with the foundation's locator registry so
#: ``model:htdemucs`` reports available **only** when the checkpoint is already on disk
#: (Req 12.4, 21.5). A plain dict assignment: no filesystem access at import time, so the
#: module-level import contract (Req 1.4) still holds. Keyed by the bare model name because
#: that is what ``resolve_model`` puts after ``model:``.
MODEL_LOCATORS[_MODEL_DEFAULT] = lambda: _locate_model(_MODEL_DEFAULT)


# --------------------------------------------------------------------------- #
# Adapter A — ML_Separator_Backend (task 9.1)                                 #
# --------------------------------------------------------------------------- #


def _pin_torch(torch: Any, seed: int) -> None:
    """Pin ``torch`` to one deterministic, CPU-only, seeded thread (Req 10.3, 15.2).

    Factored out of :meth:`ML_Separator_Backend._infer` so the reproducibility contract is
    assertable against a recording shim with no ``torch`` and no ``numpy`` installed — the
    claim "one thread, seeded" is the whole basis of the determinism scope in Req 10.4, so it
    deserves a test that does not need the numeric stack to run.

    ``set_num_threads(ML_THREAD_COUNT)`` is the load-bearing one: with more than one thread,
    summation order inside threaded kernels varies between runs and byte-identical output is
    no longer achievable at any seed. The cost is speed, and the spec records the thread
    count as part of the Fixed_Environment its determinism claim is scoped to (Req 10.5).

    ``use_deterministic_algorithms`` is **best effort**: older builds raise or lack it
    entirely, and refusing to run on those would be a worse trade than losing the last
    increment of determinism, so the failure is swallowed.
    """
    torch.set_num_threads(ML_THREAD_COUNT)  # pinned (Req 10.3)
    torch.set_grad_enabled(False)
    torch.manual_seed(int(seed) & 0xFFFFFFFF)  # seeded (Req 10.2, 10.3)
    try:
        torch.use_deterministic_algorithms(True)  # best effort
    except Exception:
        pass


class ML_Separator_Backend:
    """Real source separation through a **local** ``demucs`` checkpoint (Req 12.1-12.6).

    ``backend_id = "ml"`` and ``requires_network = False`` *by construction*, not by
    promise: :meth:`separate` resolves the checkpoint with :func:`_locate_model` and raises
    :class:`Model_Unavailable` **before importing anything at all**, so a missing model
    costs no ``torch`` import and, more importantly, there is no code path on which
    ``demucs`` could resolve a remote model name and fetch it (Req 12.6, 16.1). A backend
    that would fetch is treated as model-unavailable, and the ladder degrades to the ffmpeg
    approximation with ``degraded:model:<name>``.

    Reproducibility (Req 10.3, 10.4) is bought deliberately and its cost is documented:
    ``torch.set_num_threads(ML_THREAD_COUNT)`` pins inference to one thread so summation
    order inside threaded kernels cannot vary between runs, gradients are disabled,
    ``manual_seed`` is set from the engine's derived seed, and
    ``use_deterministic_algorithms(True)`` is best-effort. The thread count is part of the
    Fixed_Environment the spec's determinism claim is scoped to — byte-identical output is
    promised *within* one environment only (Req 10.5).

    Backend_Stems for ``htdemucs`` are ``vocals``/``drums``/``bass``/``other``; this adapter
    returns them under those names and does **not** map them —
    :func:`assemble_stem_set` owns the :data:`STEM_MAPPING` step, so ``drums`` and ``bass``
    are summed into ``music`` exactly once, in one place (Req 4.2).

    Collaborators are injected for testability (Req 19.1): ``locator`` overrides checkpoint
    resolution and ``loader`` overrides the model construction, so
    :meth:`separate` can be exercised end to end against a shim with no ``torch``,
    no ``demucs`` and no checkpoint installed.
    """

    backend_id = "ml"
    requires_network = False

    def __init__(
        self,
        model: str = _MODEL_DEFAULT,
        model_dir: Any = None,
        *,
        locator: Any = None,
        loader: Any = None,
    ) -> None:
        self.model = str(model or _MODEL_DEFAULT)
        self.model_dir = model_dir
        self._locator = locator
        self._loader = loader

    # -- checkpoint resolution ---------------------------------------------

    def locate(self) -> Path | None:
        """The local checkpoint path, or ``None`` — the injected locator when given."""
        if self._locator is not None:
            try:
                found = self._locator(self.model, self.model_dir)
            except Exception:  # pragma: no cover - hostile injected locator
                return None
            return None if found is None else Path(str(found))
        return _locate_model(self.model, self.model_dir)

    # -- the protocol operation --------------------------------------------

    def separate(
        self,
        source: Path,
        dest_dir: Path,
        *,
        fmt: Audio_Format,
        seed: int,
        timeout_s: float,
    ) -> Mapping[str, Path]:
        """Separate ``source`` into per-Backend_Stem WAVs inside ``dest_dir``.

        Order of operations is load-bearing:

        1. **Resolve the checkpoint first.** Absent ⇒ :class:`Model_Unavailable`, raised
           before a single heavy import happens (Req 12.6).
        2. Read the source WAV with :func:`_read_wav_payload` — no ``ffprobe``, no media
           pass — and reject anything that is not 16-bit PCM at ``fmt``.
        3. Only now import ``torch``/``demucs`` lazily (Req 1.4), pin threads, seed, and
           load the model from the resolved **local path**.
        4. Write one WAV per Backend_Stem at exactly ``fmt``, via :func:`_write_pcm_wav`.

        Raises:
            Model_Unavailable: the checkpoint is not present locally.
            Invalid_Audio_Format: ``fmt`` is not an :class:`Audio_Format`, or ``source`` is
                not readable 16-bit PCM at ``fmt``.
            Stem_Error: the lazy import failed, or inference raised — the ladder reports
                ``failed`` and keeps the preceding stage's media (Req 14.2).
        """
        if not isinstance(fmt, Audio_Format):
            raise Invalid_Audio_Format("ML_Separator_Backend requires a probed Audio_Format")

        checkpoint = self.locate()
        if checkpoint is None:
            raise Model_Unavailable(self.model)

        payload = _read_wav_payload(source)
        if payload is None:
            raise Invalid_Audio_Format(f"unreadable 16-bit PCM WAV: {source}")
        rate, channels, frames = payload
        if rate != int(fmt.sample_rate) or channels != int(fmt.channels):
            raise Invalid_Audio_Format(
                f"source is {rate}Hz/{channels}ch, expected "
                f"{int(fmt.sample_rate)}Hz/{int(fmt.channels)}ch"
            )

        separated = self._infer(checkpoint, frames, fmt=fmt, seed=seed)

        destination = Path(str(dest_dir))
        written: dict[str, Path] = {}
        for name in sorted(separated):
            written[name] = _write_pcm_wav(destination / f"{name}.wav", separated[name], fmt)
        return written

    # -- inference ----------------------------------------------------------

    def _infer(
        self, checkpoint: Path, frames: bytes, *, fmt: Audio_Format, seed: int
    ) -> Mapping[str, bytes]:
        """Run separation, returning ``{Backend_Stem: pcm_s16le bytes}``.

        Everything heavy lives here and nowhere else, so :meth:`separate`'s refusal path
        and its WAV I/O stay importable and testable with no numeric stack present. An
        injected ``loader`` short-circuits the ``demucs`` import entirely, which is the
        test seam for task 9.5.
        """
        if self._loader is not None:
            try:
                return self._loader(checkpoint, frames, fmt, seed)
            except Stem_Error:
                raise
            except Exception as exc:  # one failure type for the ladder
                raise Stem_Error(f"injected loader failed: {exc}") from exc

        try:
            # Lazy, and only on this path: the module must import with none of these
            # present (Req 1.4), and a missing checkpoint has already been refused above.
            import numpy
            import torch
            from demucs.apply import apply_model
            from demucs.pretrained import get_model
        except Exception as exc:  # demucs/torch absent or broken
            raise Model_Unavailable(f"{self.model}: {exc}") from exc

        try:
            _pin_torch(torch, seed)
            channels = max(int(fmt.channels), 1)
            flat = numpy.frombuffer(frames, dtype="<i2").astype("float32") / _FULL_SCALE
            usable = (flat.size // channels) * channels
            planar = flat[:usable].reshape(-1, channels).T  # (channels, samples)

            model = get_model(name=str(checkpoint))  # local path ONLY
            model.cpu().eval()
            batch = torch.from_numpy(numpy.ascontiguousarray(planar)).unsqueeze(0)
            stacked = apply_model(model, batch, device="cpu", progress=False)[0]
            names = list(getattr(model, "sources", ()) or ())
        except Exception as exc:  # one failure type for the ladder
            raise Stem_Error(f"demucs inference failed: {exc}") from exc

        out: dict[str, bytes] = {}
        for index, name in enumerate(names):
            channel_first = stacked[index].numpy()  # (channels, samples)
            interleaved = channel_first.T.reshape(-1)
            clamped = numpy.clip(interleaved, -1.0, 1.0 - 1.0 / _FULL_SCALE)
            out[str(name)] = (clamped * _FULL_SCALE).astype("<i2").tobytes()
        return out


# --------------------------------------------------------------------------- #
# Adapter B — Ffmpeg_Separator_Backend (task 9.2)                             #
# --------------------------------------------------------------------------- #

#: Speech band retained for the ``vocals`` estimate on the ffmpeg path, in Hz.
_SPEECH_HIGHPASS_HZ = 180
_SPEECH_LOWPASS_HZ = 6000


class Ffmpeg_Separator_Backend:
    """A mid-channel / speech-band **approximation**, not source separation (Req 13.2-13.4).

    Say this plainly, because the whole adapter depends on being honest about it: this is
    **not** source separation. It is a centre-channel and speech-band estimate. It cannot
    separate music that shares the speech band or sits centred in the mix; on mono input it
    degrades to a pure band split; it pulls centred instruments into ``vocals`` and leaves
    sibilance in ``music``. It exists for two reasons only — it needs no model, and
    ``music := clip - vocals`` makes the additive-decomposition invariant (Req 4.7) hold
    **exactly** rather than approximately, so ``speech_focus``-style gains still behave
    predictably.

    Because it is a downgrade it is only ever reached carrying a ``degraded:<capability_id>``
    marker and ``Engine_Status.degraded``, so the operator is never told this is real
    separation (Req 13.2, 13.3).

    One audio-only invocation produces two Backend_Stems. ``other`` is **deliberately
    omitted**, so :func:`assemble_stem_set` substitutes digital silence for it and records
    ``stem_missing:other`` (Req 4.3) — an omission the caller already handles, rather than a
    third estimate this adapter cannot honestly make.
    """

    backend_id = "ffmpeg"
    requires_network = False

    def __init__(self, *, runner: Any = None) -> None:
        self._runner = runner

    # -- the filtergraph ----------------------------------------------------

    @staticmethod
    def build_graph(channels: int) -> str:
        """The designed single-invocation filtergraph for a ``channels``-channel input.

        Stereo (or wider) input extracts the mid channel first, then the speech band::

            [0:a]asplit=2[x1][x2];
            [x1]pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1,
                highpass=f=180,lowpass=f=6000[voc_out];
            [voc_out]asplit=2[voc_a][voc_src];
            [voc_src]volume=-1:precision=float[voc_neg];
            [x2][voc_neg]amix=inputs=2:normalize=0:dropout_transition=0[mus]

        For a **mono** input the ``pan`` node is omitted, because mid extraction is the
        identity there and ``pan=stereo`` would silently upmix the stem to two channels and
        break the ``fmt`` preservation check in :func:`_verify_stem_file` (Req 4.6).

        ``volume=-1`` inverts phase and ``amix=normalize=0`` sums rather than averages, so
        the second output is exactly ``clip - vocals``; ``dropout_transition=0`` stops a
        shorter input from ramping the other, keeping the subtraction sample-exact.

        Two deviations from the design's illustrative snippet, both deliberate:

        * it splits the input **two** ways, not three. The snippet's ``asplit=3`` leaves
          ``[x3]`` unconnected, and ffmpeg rejects a filtergraph with an unconnected output
          pad outright, so the graph as printed would not run. Only two copies of the input
          are ever used (one for the vocal estimate, one for the subtraction).
        * the vocal chain is labelled once and split, rather than labelled ``[voc]`` and
          then re-split, which is the same graph with one fewer label.
        """
        mid = "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1," if max(int(channels), 1) > 1 else ""
        return (
            "[0:a]asplit=2[x1][x2];"
            f"[x1]{mid}highpass=f={_SPEECH_HIGHPASS_HZ},"
            f"lowpass=f={_SPEECH_LOWPASS_HZ}[voc];"
            "[voc]asplit=2[voc_out][voc_src];"
            "[voc_src]volume=-1:precision=float[voc_neg];"
            "[x2][voc_neg]amix=inputs=2:normalize=0:dropout_transition=0[mus]"
        )

    def build_command(
        self, source: Path, vocals: Path, music: Path, *, fmt: Audio_Format
    ) -> list[str]:
        """The full argv for the one audio-only invocation this adapter spends.

        Both outputs are written from a single ffmpeg process — two ``-map`` targets on one
        command line, not two passes — so the adapter costs exactly one invocation
        regardless of the Stem_Set (Req 2.6, 15.9). Both are pinned to ``pcm_s16le`` at
        ``fmt``, which is what makes them pass :func:`_verify_stem_file` unchanged.
        """
        channels = max(int(fmt.channels), 1)
        rate = str(int(fmt.sample_rate))
        argv = [
            _ffmpeg_binary(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            self.build_graph(channels),
        ]
        for label, dest in (("[voc_out]", vocals), ("[mus]", music)):
            argv += [
                "-map",
                label,
                "-c:a",
                "pcm_s16le",
                "-ar",
                rate,
                "-ac",
                str(channels),
                str(dest),
            ]
        return argv

    # -- the protocol operation --------------------------------------------

    def separate(
        self,
        source: Path,
        dest_dir: Path,
        *,
        fmt: Audio_Format,
        seed: int,
        timeout_s: float,
    ) -> Mapping[str, Path]:
        """Produce ``vocals`` and ``music`` in one invocation; omit ``other`` (Req 4.3).

        ``seed`` is accepted to satisfy the :class:`Separator_Backend` protocol and is
        deliberately unused: this path is a deterministic filtergraph with no random
        choice, so seeding it would imply a variability that does not exist.

        Raises:
            Invalid_Audio_Format: ``fmt`` is not a probed :class:`Audio_Format`.
            worker.ffmpeg_utils.FFmpegError: the invocation failed (via :func:`_run`).
        """
        if not isinstance(fmt, Audio_Format):
            raise Invalid_Audio_Format("Ffmpeg_Separator_Backend requires a probed Audio_Format")
        destination = Path(str(dest_dir))
        vocals = _prepared(destination / "vocals.wav")
        music = _prepared(destination / "music.wav")
        _run(
            self._runner if self._runner is not None else _default_runner(),
            self.build_command(source, vocals, music, fmt=fmt),
            timeout_s,
        )
        # ``other`` is intentionally absent: assemble_stem_set writes silence for it and
        # records ``stem_missing:other`` (Req 4.3).
        return {"music": music, "vocals": vocals}


def _default_runner() -> Command_Runner:
    """The real :data:`Command_Runner` — ``subprocess.run`` with an explicit timeout.

    Built lazily rather than held as a module constant so the module still imports with no
    ffmpeg binary present (Req 1.4), and so every test can inject a recording runner
    instead (Req 19.1). ``check=False``: :func:`_run` inspects ``returncode`` itself and
    raises one ``FFmpegError``, so a non-zero exit is reported with its stderr tail rather
    than as a bare ``CalledProcessError``.
    """

    def run(argv: Sequence[str], timeout_s: float) -> subprocess.CompletedProcess:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )

    return run


# --------------------------------------------------------------------------- #
# The ffmpeg pipeline (epic 11)                                               #
# --------------------------------------------------------------------------- #
# Every function below either *builds* an argv/filtergraph string (pure, so the emitted
# command is assertable without running anything) or spends exactly one invocation through
# the injected :data:`Command_Runner`. The split is deliberate: the property tests assert
# the emitted graph, and the integration tests run it.

#: Peak ceiling handed to ``alimiter`` when the filter is available — just under full scale,
#: so the limiter engages before the ``pcm_s16le`` representation saturates (Req 5.9).
_ALIMITER_LIMIT = 0.977

#: Capability id for the optional peak guard (Req 5.9).
ALIMITER_CAPABILITY = "ffmpeg_filter:alimiter"

#: Declick fade length in seconds — one millisecond at the clip's own head and tail, the two
#: boundaries for which Req 6.3 forbids a Seam (Req 9.1).
_DECLICK_S = 0.001

#: Per-stem half-width scaling for ``spectral`` repair (Req 7.3). ``vocals`` is narrowed
#: hardest because a speech transient smeared over 12 ms is audible as a lisp, while
#: ``music`` takes the full window.
SPECTRAL_HALF_WIDTH_SCALE: dict[str, float] = {
    "music": 1.0,
    "other": 0.6,
    "vocals": 0.35,
}

#: Timestamp reset applied to every trimmed segment before ``concat``, which requires each
#: input to start at PTS 0.
_ASETPTS = "asetpts=N/SR/TB"


def _ffprobe_binary() -> str:
    """The configured ffprobe binary, resolved **lazily** (Req 1.4).

    The ``ffprobe`` sibling of :func:`_ffmpeg_binary`, with the same lazy ``config`` import
    so this module still imports with no ``pydantic-settings`` present.
    """
    try:
        from config import settings  # lazy (Req 1.4)

        binary = str(getattr(settings, "ffprobe_binary", "") or "").strip()
    except Exception:  # pragma: no cover - config unavailable in a minimal install
        binary = ""
    return binary or "ffprobe"


def _fixed(value: Any) -> str:
    """Format one timestamp/gain for a filtergraph, at fixed 6-decimal precision.

    Fixed notation, never scientific: ``repr(1e-05)`` is ``'1e-05'``, which ffmpeg's
    expression parser reads as ``1`` followed by garbage. Fixed formatting also makes the
    emitted string a deterministic function of the plan, which is what lets two runs be
    compared as strings (Req 10.6, 4.9).
    """
    return f"{coerce_float(value, 0.0):.6f}"


# --------------------------------------------------------------------------- #
# Task 11.1 — the audio-format probe and the step budget                      #
# --------------------------------------------------------------------------- #


def probe_audio_format(
    path: Any, runner: Any = None, timeout_s: float = MIN_STEP_TIMEOUT_S
) -> Audio_Format | None:
    """Probe the first audio stream of ``path`` (Req 4.8, 17.4, 17.5).

    An **``ffprobe``, not a media pass**: nothing is decoded and no frame is written, so
    this does not count against :attr:`max_media_passes`.
    ``worker.ffmpeg_utils.probe`` stays in charge of ``has_audio``, ``duration`` and ``fps``
    for the video-integrity comparison (Req 17.3) — it simply carries no sample rate or
    channel count, which is the whole reason this function exists.

    Three outcomes, all distinct and all load-bearing:

    * **No audio stream at all** ⇒ ``None``. Not an error: the ladder skips the clip with no
      marker (Req 4.8).
    * **A stream whose declared format is unusable** — ``sample_rate`` or ``channels``
      missing, non-numeric, zero or negative ⇒ :class:`Invalid_Audio_Format`, which the
      ladder reports as ``degraded:audio_format`` (Req 17.5).
    * Otherwise an :class:`Audio_Format`. ``codec`` and ``start_time`` are best-effort:
      ``ffprobe`` legitimately omits either or reports ``"N/A"``, and neither absence makes
      the format unusable, so they fall back to ``""`` and ``0.0``.

    Raises:
        Invalid_Audio_Format: the stream exists but its format is unusable, or ``ffprobe``
            emitted output that cannot be parsed as JSON.
        worker.ffmpeg_utils.FFmpegError: the invocation itself failed (via :func:`_run`).
    """
    argv = [
        _ffprobe_binary(),
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,channels,codec_name,start_time",
        "-of",
        "json",
        str(path),
    ]
    completed = _run(runner if runner is not None else _default_runner(), argv, timeout_s)

    raw = getattr(completed, "stdout", "") or ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise Invalid_Audio_Format(f"unparseable ffprobe output: {exc}") from exc

    streams = payload.get("streams") if isinstance(payload, Mapping) else None
    if not isinstance(streams, Sequence) or not streams:
        return None  # no audio stream (Req 4.8)
    stream = streams[0]
    if not isinstance(stream, Mapping):
        return None

    rate = coerce_int(stream.get("sample_rate"), 0, lo=0)
    channels = coerce_int(stream.get("channels"), 0, lo=0)
    if rate <= 0 or channels <= 0:
        raise Invalid_Audio_Format(
            f"unusable audio format: sample_rate={stream.get('sample_rate')!r}, "
            f"channels={stream.get('channels')!r}"
        )

    codec = stream.get("codec_name")
    start = stream.get("start_time")
    return Audio_Format(
        sample_rate=rate,
        channels=channels,
        codec="" if codec in (None, "N/A") else str(codec),
        start_time=coerce_float(start, 0.0) if start not in (None, "N/A") else 0.0,
    )


def step_timeout(ctx: Any, reserve_s: float) -> float:
    """The explicit subprocess timeout for the next step (Req 15.3, 15.4).

    ``max(MIN_STEP_TIMEOUT_S, ctx.remaining() - reserve_s)``: each step holds back
    ``reserve_s`` so the steps *after* it still have budget to finish, and the floor
    guarantees no ffmpeg invocation is ever launched with a non-positive or missing timeout.

    ``ctx.remaining()`` is re-read on **every** call rather than sampled once, so a step
    that overran shortens the next one instead of being papered over.

    Total: a context with no ``remaining``, one whose ``remaining`` raises, and a
    non-numeric reserve all fall back to the floor — a missing budget must not become an
    unbounded subprocess.
    """
    try:
        raw = ctx.remaining()
    except Exception:  # no/haywire remaining() -> floor
        return MIN_STEP_TIMEOUT_S
    try:
        remaining = float(raw)
    except (TypeError, ValueError, OverflowError):
        return MIN_STEP_TIMEOUT_S

    reserve = coerce_float(reserve_s, 0.0)
    if math.isnan(remaining):
        return MIN_STEP_TIMEOUT_S
    if math.isinf(remaining):
        # ``Engine_Context.deadline`` defaults to ``inf`` — "no deadline". The step is then
        # unbounded by the *job* budget, but ``subprocess`` must still get a finite number,
        # so fall back to the engine's own declared per-clip budget. Note the raw value is
        # read **before** ``coerce_float``, which flattens every non-finite input to its
        # default and would otherwise turn "no deadline" into "no budget at all".
        if remaining < 0:
            return MIN_STEP_TIMEOUT_S
        return max(
            MIN_STEP_TIMEOUT_S,
            coerce_float(getattr(ctx, "time_budget_s", 0.0), 0.0) - reserve,
        )
    return max(MIN_STEP_TIMEOUT_S, remaining - reserve)


# --------------------------------------------------------------------------- #
# Task 11.2 — media pass 1: extract the clip audio                            #
# --------------------------------------------------------------------------- #


def extract_command(clip: Any, dest: Path, fmt: Audio_Format) -> list[str]:
    """The argv for media pass 1 — decode the clip's audio to WAV (Req 4.4).

    ``-vn`` is what makes this an *audio* pass: no video frame is decoded, so the cost is
    proportional to the audio only. ``-map 0:a:0`` pins the same first audio stream
    :func:`probe_audio_format` measured, and ``-ar``/``-ac`` pin the probed format, so the
    extracted WAV, every stem and the re-mixed result all share one format and the
    :func:`_verify_stem_file` check has something exact to compare against (Req 4.6).
    """
    return [
        _ffmpeg_binary(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(clip),
        "-vn",
        "-map",
        "0:a:0",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(int(fmt.sample_rate)),
        "-ac",
        str(max(int(fmt.channels), 1)),
        "-f",
        "wav",
        str(dest),
    ]


def extract_clip_audio(
    clip: Any, dest: Path, *, fmt: Audio_Format, runner: Any = None, timeout_s: float
) -> Path:
    """Run media pass 1, writing the clip audio inside the Engine_Workspace (Req 4.4, 11.1).

    Raises:
        Invalid_Audio_Format: ``fmt`` is not a probed :class:`Audio_Format`.
        worker.ffmpeg_utils.FFmpegError: the invocation failed (via :func:`_run`).
    """
    if not isinstance(fmt, Audio_Format):
        raise Invalid_Audio_Format("extract_clip_audio requires a probed Audio_Format")
    target = _prepared(Path(str(dest)))
    _run(
        runner if runner is not None else _default_runner(),
        extract_command(clip, target, fmt),
        timeout_s,
    )
    return target


# --------------------------------------------------------------------------- #
# Task 11.3 — the equal-power V-notch, and the gain + repair filtergraph      #
# --------------------------------------------------------------------------- #


def notch_filters(
    windows: Sequence[Repair_Window], *, scale: float = 1.0, channels: int = 2
) -> tuple[str, ...]:
    """The chunked ``aeval`` filters implementing equal-power V-notch seam repair.

    Per merged :class:`Repair_Window` ``[s, e]`` with centre ``c`` and half-width ``h``, the
    gain is ``sin(PI/2*abs(t-c)/h)`` — an **equal-power V-notch**: unity at both window
    edges, exactly zero at the join, quarter-sine (constant-power) taper between. The click
    disappears because the waveform is driven continuously to zero *across* the
    discontinuity instead of stepping over it. At the default 12 ms window (6 ms per side)
    it is inaudible.

    Why this and not the obvious alternatives (Req 7.2, 7.9):

    * ``acrossfade`` **shortens** its output by the overlap, which would break duration
      preservation (Req 17.1). It is used for music *bridging* in
      :func:`bridge_music_stem`, where the construction is duration-exact by design, and
      nowhere else.
    * a chained ``afade=t=out`` sets the gain to zero for *everything after* the fade, so it
      cannot express an interior window at all.
    * **``volume`` with ``eval=frame`` — which the design specifies — does not work.**
      Verified empirically against ffmpeg 7.0.2: with ``eval=frame`` the ``t`` variable does
      not take the values a per-frame evaluation implies, so ``gt(t,1.0)`` is false for every
      frame of a 3-second input and a ``between(t,…)``-gated expression never fires. The
      filter silently applies unity gain — no error, no warning, output byte-identical to the
      input. A constant expression (``volume='0.5'``) *does* apply, which is exactly why this
      is so easy to miss: the filter looks like it is working. Even had ``t`` behaved,
      ``eval=frame`` evaluates once per 1024-sample block (~21 ms at 48 kHz), which cannot
      express the 12 ms default window at all.
    * ``aeval`` evaluates its expression **per sample**, so the taper is exact rather than
      stepped, it is duration-exact, and it costs a constant number of nodes in the Seam
      count. Measured cost is ~1.8 s for 24 windows over 30 s of stereo — comfortably inside
      the engine's declared budget.

    One expression is emitted per channel (``val(0)*g|val(1)*g``) with ``c=same``, so the
    channel layout is preserved and the same gain is applied to every channel. A single
    expression does appear to be reused across channels in practice, but that is undocumented
    behaviour and the explicit form costs nothing.

    Because ``repair_windows`` already merged overlaps through ``normalize_segments``, each
    merged window contributes **exactly one** notch, so no sample is ever faded twice
    (Req 7.7) — the merge is what makes that true, not this function.

    ``scale`` narrows the half-width for per-stem ``spectral`` repair
    (:data:`SPECTRAL_HALF_WIDTH_SCALE`); the notch stays centred on the join, so a scaled
    window still reaches zero exactly at the seam and simply recovers sooner.

    Windows are emitted in chunks of :data:`NOTCH_EXPR_CHUNK`, each chunk its own filter.
    That is semantics-preserving precisely because the windows are disjoint and every
    expression is ``1`` outside its own windows, so chaining chunks multiplies by unity
    everywhere except inside a window that only one chunk mentions (Req 15.9).

    A window with a non-positive half-width contributes nothing: it names a zero-width span,
    there is no discontinuity to taper across, and ``/h`` would divide by zero.
    """
    factor = coerce_float(scale, 1.0)
    usable: list[tuple[float, float, float]] = []
    for window in windows or ():
        start = coerce_float(getattr(window, "start", 0.0), 0.0)
        end = coerce_float(getattr(window, "end", 0.0), 0.0)
        centre = (start + end) / 2.0
        half = ((end - start) / 2.0) * factor
        if half <= 0.0:
            continue
        usable.append((max(centre - half, 0.0), centre + half, centre))
    if not usable:
        return ()

    lanes = max(coerce_int(channels, _CHANNELS_DEFAULT, lo=1), 1)
    filters: list[str] = []
    for offset in range(0, len(usable), NOTCH_EXPR_CHUNK):
        chunk = usable[offset : offset + NOTCH_EXPR_CHUNK]
        expression = "1"
        for start, end, centre in reversed(chunk):
            half = end - centre
            expression = (
                f"if(between(t,{_fixed(start)},{_fixed(end)}),"
                f"sin(PI/2*abs(t-{_fixed(centre)})/{_fixed(half)}),"
                f"{expression})"
            )
        exprs = "|".join(f"val({lane})*{expression}" for lane in range(lanes))
        filters.append(f"aeval=exprs='{exprs}':c=same")
    return tuple(filters)


def resolve_peak_guard(
    gains: Mapping[str, float], alimiter_available: bool
) -> tuple[dict[str, float], tuple[str, ...]]:
    """Reconcile requested gains with the availability of a peak guard (Req 5.9).

    Returns ``(gains, marker_details)``. With ``alimiter`` available the requested gains are
    returned unchanged and the limiter is appended to the graph, making the ceiling musical
    rather than a hard clip. Without it, a **boost** (any gain ``> 1.0``) is clamped to
    ``1.0`` and ``degraded:ffmpeg_filter:alimiter`` is recorded — the operator is told the
    boost was refused instead of receiving audible clipping.

    Attenuation-only bundles are never touched and never carry the marker: with all gains
    ``<= 1.0`` clipping is practically impossible, and the ``pcm_s16le`` representation
    enforces the ceiling regardless.
    """
    source: Mapping[str, Any] = gains if isinstance(gains, Mapping) else {}
    resolved = {
        name: _coerce_gain(source[name]) if name in source else GAIN_DEFAULT for name in STEM_NAMES
    }
    if alimiter_available or not any(value > GAIN_DEFAULT for value in resolved.values()):
        return resolved, ()
    clamped = {name: min(value, GAIN_DEFAULT) for name, value in resolved.items()}
    return clamped, (f"degraded:{ALIMITER_CAPABILITY}",)


def build_mix_graph(
    plan: Stem_Plan,
    stem_set: Mapping[str, Path],
    *,
    gains: Mapping[str, float] | None = None,
    alimiter: bool = False,
    stem_windows: Mapping[str, Sequence[Repair_Window]] | None = None,
) -> tuple[list[Path], str, str]:
    """Build the one-invocation gain + repair filtergraph (Req 5.5, 5.7, 7.2, 7.5, 15.9).

    Returns ``(input_paths, filter_complex, out_label)``. Pure — it builds strings and
    touches no file — so the emitted graph is assertable without ffmpeg installed, which is
    what the epic-11 property tests do.

    Shape, bottom to top:

    1. **Inputs** are the Stem_Set WAVs in :data:`STEM_NAMES` order, and a stem whose
       resolved gain is ``0.0`` is **not added as an input at all** (Req 5.7) — muting a stem
       costs no decode, rather than decoding it and multiplying by zero.
    2. Per input, ``volume=<gain>:precision=float``. Under ``spectral`` the per-stem notch
       chain (with its :data:`SPECTRAL_HALF_WIDTH_SCALE` half-width) is appended *here*,
       before the mix, which is what makes spectral repair per-stem rather than post-mix
       (Req 7.3).
    3. ``amix=inputs=N:normalize=0:dropout_transition=0``. ``normalize=0`` is essential:
       the default divides by the input count, which would silently attenuate every stem and
       break the additive decomposition (Req 4.7). With a single input ``amix`` is skipped
       entirely — mixing one stream with itself is a no-op that costs a node.
    4. Under ``crossfade`` the notch chain applies **post-mix** (one pass over the summed
       stream). Under ``spectral`` it has already been applied per stem, so it is not
       repeated here — repairing twice would fade the seam twice, which Req 7.7 forbids.
    5. ``declick`` adds 1 ms ``afade`` in/out at the clip's own head and tail (Req 9.1) —
       the two boundaries where ``afade`` is the *correct* tool, because there is no "after"
       to zero out.
    6. ``alimiter`` when available (Req 5.9).

    ``gains`` overrides ``plan.gains``, which is how :func:`resolve_peak_guard`'s clamped
    bundle reaches the graph without mutating the frozen plan.

    ``stem_windows`` overrides which windows a *given* stem is notched over, defaulting to
    ``plan.windows`` for every stem. It exists for exactly one caller: a ``music`` stem that
    :func:`bridge_music_stem` already repaired with real neighbouring material must be
    notched over the **residual** windows only, or the bridged windows would be repaired
    twice (Req 7.7).
    """
    bundle: Mapping[str, float] = plan.gains if gains is None else gains
    spectral = plan.repair_mode == "spectral"
    overrides: Mapping[str, Sequence[Repair_Window]] = (
        stem_windows if isinstance(stem_windows, Mapping) else {}
    )

    inputs: list[Path] = []
    labels: list[str] = []
    parts: list[str] = []
    for name in STEM_NAMES:
        gain = _coerce_gain(bundle.get(name, GAIN_DEFAULT))
        if gain <= GAIN_MIN:
            continue  # muted: not an input at all (Req 5.7)
        path = stem_set.get(name) if isinstance(stem_set, Mapping) else None
        if path is None:
            continue
        index = len(inputs)
        inputs.append(Path(str(path)))
        chain: list[str] = []
        # Re-entrancy guard (Req 7.10, 7.11): a *unity* gain contributes no ``volume`` node
        # at all, rather than a no-op ``volume=1.000000``. Same for an empty window list
        # contributing no notch. The point is that re-running the engine on its own output
        # with nothing left to do emits an empty graph, so the second run is a re-render and
        # not a second repair pass. This complements :func:`plan_is_noop`, which catches the
        # same situation earlier and more cheaply when it is knowable before probing.
        if gain != GAIN_DEFAULT:
            chain.append(f"volume={_fixed(gain)}:precision=float")
        if spectral:
            windows = overrides.get(name, plan.windows)
            chain.extend(
                notch_filters(
                    windows,
                    scale=SPECTRAL_HALF_WIDTH_SCALE.get(name, 1.0),
                    channels=plan.channels,
                )
            )
        if chain:
            label = f"g_{name}"
            parts.append(f"[{index}:a]{','.join(chain)}[{label}]")
        else:
            label = f"{index}:a"  # nothing to do: feed the input straight through
        labels.append(label)

    if not labels:
        # Every stem muted. ``anullsrc`` is not reachable here (the plan would have to have
        # an all-zero gain bundle, which the ladder's no-op rung does not catch because the
        # mode may still be repairing), so emit silence of the planned length rather than an
        # empty graph.
        parts.append(
            f"anullsrc=channel_layout={'mono' if plan.channels == 1 else 'stereo'}:"
            f"sample_rate={int(plan.sample_rate)}"
            f",atrim=end={_fixed(plan.duration)}[mix]"
        )
        current = "mix"
    elif len(labels) == 1:
        current = labels[0]
    else:
        joined = "".join(f"[{label}]" for label in labels)
        parts.append(f"{joined}amix=inputs={len(labels)}:normalize=0:dropout_transition=0[mix]")
        current = "mix"

    tail: list[str] = []
    if plan.repair_mode == "crossfade":
        tail.extend(notch_filters(plan.windows, channels=plan.channels))
    if plan.declick and plan.duration > 2 * _DECLICK_S:
        tail.append(f"afade=t=in:st=0:d={_fixed(_DECLICK_S)}")
        tail.append(f"afade=t=out:st={_fixed(plan.duration - _DECLICK_S)}:d={_fixed(_DECLICK_S)}")
    if alimiter:
        tail.append(f"alimiter=limit={_ALIMITER_LIMIT}:level=disabled")

    if tail:
        parts.append(f"[{current}]{','.join(tail)}[out]")
        current = "out"
    return inputs, ";".join(parts), current


def mix_command(
    plan: Stem_Plan,
    stem_set: Mapping[str, Path],
    dest: Path,
    *,
    gains: Mapping[str, float] | None = None,
    alimiter: bool = False,
    stem_windows: Mapping[str, Sequence[Repair_Window]] | None = None,
) -> list[str]:
    """The full argv for the single gain + repair invocation (Req 5.5, 15.9).

    ``mixed.wav`` is written as ``pcm_s16le`` at the planned format, which is how the
    no-clipping invariant is enforced by *representation* rather than by analysis: with
    anti-phase content ``|Σ gₛ·sₛ| <= Σ gₛ`` is the only sound analytic bound, so the honest
    guarantee is that no **written** sample can exceed full scale — it saturates (Req 5.9).
    """
    inputs, graph, out_label = build_mix_graph(
        plan, stem_set, gains=gains, alimiter=alimiter, stem_windows=stem_windows
    )
    argv = [_ffmpeg_binary(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for path in inputs:
        argv += ["-i", str(path)]
    argv += [
        "-filter_complex",
        graph,
        "-map",
        f"[{out_label}]",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(int(plan.sample_rate)),
        "-ac",
        str(max(int(plan.channels), 1)),
        str(dest),
    ]
    return argv


def render_mix(
    plan: Stem_Plan,
    stem_set: Mapping[str, Path],
    dest: Path,
    *,
    runner: Any = None,
    timeout_s: float,
    alimiter: bool = False,
    stem_windows: Mapping[str, Sequence[Repair_Window]] | None = None,
) -> tuple[Path, tuple[str, ...]]:
    """Run the gain + repair invocation, returning ``(mixed_path, marker_details)``.

    The details are :func:`resolve_peak_guard`'s, i.e. ``degraded:ffmpeg_filter:alimiter``
    when a boost was refused for want of a peak guard, else empty.

    Raises:
        worker.ffmpeg_utils.FFmpegError: the invocation failed (via :func:`_run`).
    """
    gains, details = resolve_peak_guard(plan.gains, alimiter)
    target = _prepared(Path(str(dest)))
    _run(
        runner if runner is not None else _default_runner(),
        mix_command(
            plan,
            stem_set,
            target,
            gains=gains,
            alimiter=alimiter,
            stem_windows=stem_windows,
        ),
        timeout_s,
    )
    return target, details


# --------------------------------------------------------------------------- #
# Task 11.4 — spectral music bridging                                         #
# --------------------------------------------------------------------------- #


def partition_bridge_windows(
    windows: Sequence[Repair_Window], duration: float, *, cap: int = MAX_BRIDGE_WINDOWS
) -> tuple[tuple[Repair_Window, ...], tuple[Repair_Window, ...]]:
    """Split ``windows`` into ``(bridgeable, notch_only)`` for the ``music`` stem (Req 7.3).

    Bridging replaces the damaged span with **real neighbouring material**, so it needs that
    material to exist and to be unclaimed. A window ``[s, e]`` with ``h = (e-s)/2`` and
    ``c = s+h`` qualifies only when all four hold:

    * ``h > 0`` — a zero-width window has nothing to bridge;
    * ``s - h >= 0`` — the left source segment ``[s-h, s)`` is inside the clip;
    * ``e + h <= duration`` — the right source segment ``[e, e+h)`` is inside the clip;
    * ``s - h`` is at or after the previous bridged window's ``e + h`` — otherwise two
      bridges would read overlapping source material and the ``concat`` segment list would
      no longer partition the timeline.

    Everything else — including every window once :data:`MAX_BRIDGE_WINDOWS` is reached —
    falls back to the notch for that window, which is always available and always
    duration-exact. That cap is what bounds the filtergraph so a seam-dense clip cannot
    explode it (Req 15.9).

    The two returned tuples partition the input, so
    ``len(bridgeable) + len(notch_only) == len([w for w in windows])`` and the caller can
    record :attr:`Stem_Plan.bridged_windows` / :attr:`Stem_Plan.notched_windows` from them
    directly.
    """
    limit = coerce_float(duration, 0.0)
    ceiling = max(coerce_int(cap, MAX_BRIDGE_WINDOWS, lo=0), 0)
    bridged: list[Repair_Window] = []
    notched: list[Repair_Window] = []
    guard = 0.0
    for window in sorted(
        (w for w in (windows or ()) if isinstance(w, Repair_Window)),
        key=lambda w: (w.start, w.end),
    ):
        half = (window.end - window.start) / 2.0
        if (
            len(bridged) < ceiling
            and half > 0.0
            and window.start - half >= 0.0
            and window.end + half <= limit
            and window.start - half >= guard
        ):
            bridged.append(window)
            guard = window.end + half
        else:
            notched.append(window)
    return tuple(bridged), tuple(notched)


def build_bridge_graph(bridged: Sequence[Repair_Window], duration: float) -> tuple[str, str]:
    """The duration-exact ``acrossfade`` + ``concat`` bridge graph (Req 7.3, 7.9).

    Returns ``(filter_complex, out_label)``. Pure.

    This is the one place ``acrossfade`` is genuinely correct. Crossfading two ``h``-length
    segments with ``d=h`` yields exactly ``h`` samples out, so for window ``[s, e]`` with
    ``h = (e-s)/2`` and ``c = s+h``::

        left  = acrossfade(atrim=[s-h, s), atrim=[s, c),  d=h, qsin/qsin)   -> h samples
        right = acrossfade(atrim=[c, e),   atrim=[e, e+h), d=h, qsin/qsin)  -> h samples

    and ``left + right`` is exactly ``2h = e - s`` samples — the span it replaces. The
    ``concat`` of ``[0,s) + left + right + [e,duration)`` therefore preserves total duration
    **exactly**, which is what Req 17.1 demands and what plain ``acrossfade`` on the whole
    stream would violate.

    Musically this is the better repair for ``music``: instead of ducking to silence at the
    join it fades *in* material that was actually adjacent, so a sustained chord across a
    filler cut survives.

    Every trimmed segment carries :data:`_ASETPTS`, because ``concat`` requires each input to
    start at PTS 0. The input is ``asplit``-ed exactly once into the ``(n+1) + 4n`` copies
    the segments need, so the source is decoded once regardless of window count.
    """
    windows = [w for w in (bridged or ()) if isinstance(w, Repair_Window)]
    limit = coerce_float(duration, 0.0)
    count = len(windows)
    if count == 0:
        return "", ""

    total = (count + 1) + 4 * count
    sources = [f"b{index}" for index in range(total)]
    parts = ["[0:a]asplit=" + str(total) + "".join(f"[{s}]" for s in sources)]
    order: list[str] = []
    cursor = 0.0
    pick = 0

    for k, window in enumerate(windows):
        start, end = float(window.start), float(window.end)
        half = (end - start) / 2.0
        centre = start + half

        keep = f"k{k}"
        parts.append(
            f"[{sources[pick]}]atrim=start={_fixed(cursor)}:end={_fixed(start)},{_ASETPTS}[{keep}]"
        )
        pick += 1
        order.append(keep)

        for side, (a_start, a_end, b_start, b_end) in (
            ("l", (start - half, start, start, centre)),
            ("r", (centre, end, end, end + half)),
        ):
            parts.append(
                f"[{sources[pick]}]atrim=start={_fixed(a_start)}:end={_fixed(a_end)},"
                f"{_ASETPTS}[{side}a{k}]"
            )
            pick += 1
            parts.append(
                f"[{sources[pick]}]atrim=start={_fixed(b_start)}:end={_fixed(b_end)},"
                f"{_ASETPTS}[{side}b{k}]"
            )
            pick += 1
            parts.append(
                f"[{side}a{k}][{side}b{k}]acrossfade=d={_fixed(half)}:c1=qsin:c2=qsin[{side}{k}]"
            )
            order.append(f"{side}{k}")
        cursor = end

    tail = f"k{count}"
    parts.append(
        f"[{sources[pick]}]atrim=start={_fixed(cursor)}:end={_fixed(limit)},{_ASETPTS}[{tail}]"
    )
    order.append(tail)

    joined = "".join(f"[{label}]" for label in order)
    parts.append(f"{joined}concat=n={len(order)}:v=0:a=1[bridged]")
    return ";".join(parts), "bridged"


def bridge_music_stem(
    source: Path,
    dest: Path,
    windows: Sequence[Repair_Window],
    *,
    fmt: Audio_Format,
    duration: float,
    runner: Any = None,
    timeout_s: float,
    cap: int = MAX_BRIDGE_WINDOWS,
) -> tuple[Path, tuple[Repair_Window, ...], tuple[Repair_Window, ...]]:
    """Bridge the ``music`` stem's repairable windows (Req 7.3) — ``spectral`` only.

    Returns ``(path, bridged, residual)``:

    * ``path`` is the bridged stem when at least one window qualified, else ``source``
      unchanged — no invocation is spent when there is nothing to bridge;
    * ``bridged`` is what :attr:`Stem_Plan.bridged_windows` should count;
    * ``residual`` is what still needs the notch, and is exactly what the caller passes as
      ``build_mix_graph(..., stem_windows={"music": residual})`` so a bridged window is
      never also notched (Req 7.7).

    Counts are recorded on the plan as **detail only** — no extra marker, because whether a
    window was bridged or notched is a fidelity nuance, not a degradation the operator needs
    to act on (Req 7.3).

    Raises:
        Invalid_Audio_Format: ``fmt`` is not a probed :class:`Audio_Format`.
        worker.ffmpeg_utils.FFmpegError: the invocation failed (via :func:`_run`).
    """
    if not isinstance(fmt, Audio_Format):
        raise Invalid_Audio_Format("bridge_music_stem requires a probed Audio_Format")

    bridged, residual = partition_bridge_windows(windows, duration, cap=cap)
    if not bridged:
        return Path(str(source)), (), residual

    graph, out_label = build_bridge_graph(bridged, duration)
    target = _prepared(Path(str(dest)))
    argv = [
        _ffmpeg_binary(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        graph,
        "-map",
        f"[{out_label}]",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(int(fmt.sample_rate)),
        "-ac",
        str(max(int(fmt.channels), 1)),
        str(target),
    ]
    _run(runner if runner is not None else _default_runner(), argv, timeout_s)
    return target, bridged, residual


# --------------------------------------------------------------------------- #
# Task 11.5 — media pass 2: remux                                             #
# --------------------------------------------------------------------------- #

#: Audio codec used when the probed container codec is absent or not re-encodable by name.
_REMUX_CODEC_DEFAULT = "aac"

#: Bitrate for the re-encoded audio stream on the remux pass.
_REMUX_BITRATE = "192k"

#: Probed codecs we re-encode with their own encoder rather than substituting the default.
#: Deliberately short: an encoder that is merely *named* by the probe is not necessarily
#: available in the local build, and ``aac`` is universally present in any ffmpeg that can
#: write MP4.
_REMUX_CODEC_ALLOWED: frozenset[str] = frozenset({"aac", "mp3", "opus", "vorbis", "flac"})


def remux_codec(fmt: Audio_Format) -> str:
    """The audio encoder for media pass 2 — the probed codec when we can trust it.

    Matching the source codec avoids a gratuitous format change, but only for codecs we know
    have an encoder in a stock build (:data:`_REMUX_CODEC_ALLOWED`); anything else — an
    exotic codec, ``""``, ``pcm_*`` in an MP4 — becomes :data:`_REMUX_CODEC_DEFAULT`, because
    a remux that fails for want of an encoder is strictly worse than one that lands as AAC.
    """
    codec = str(getattr(fmt, "codec", "") or "").strip().lower()
    return codec if codec in _REMUX_CODEC_ALLOWED else _REMUX_CODEC_DEFAULT


def remux_command(
    clip: Any, mixed: Path, dest: Path, *, fmt: Audio_Format, duration: float = 0.0
) -> list[str]:
    """The argv for media pass 2 — the repaired audio back onto the original video.

    Four deliberate choices, each protecting an integrity requirement:

    * ``-c:v copy`` bit-copies the video stream, so the picture is provably untouched and the
      pass costs no video encode (Req 3.2, 17.3).
    * ``-t <duration>`` bounds the output to the **original clip's audio duration**, and it is
      what makes duration preservation actually hold (Req 17.1). It is needed because a lossy
      audio stream carries encoder priming/padding: decoding 2.000 s of AAC yields ~2.020 s of
      PCM, and re-encoding that grows the stream again — so without an explicit bound every
      pass through this engine would lengthen the clip by ~20 ms. Measured, not assumed.
    * ``-shortest`` remains **deliberately absent**, and the distinction from ``-t`` is the
      whole point: ``-shortest`` truncates to whichever stream *happens* to be shorter, which
      is a silent, input-dependent duration change. ``-t`` is an explicit bound taken from the
      original clip, so the output length is a measured property of the input rather than an
      accident of which stream won.
    * ``-itsoffset`` is emitted **only** when the probed audio ``start_time`` is non-zero, so
      a container whose audio legitimately starts late keeps that relationship instead of
      being silently re-based to zero (Req 17.4).

    ``-map 0:v:0 -map 1:a:0`` pins exactly one video and one audio stream, which is what
    :func:`verify_replacement` then asserts. A non-positive ``duration`` omits ``-t``, which is
    the pre-measurement behaviour and is what the unit tests that do not probe rely on.
    """
    offset = coerce_float(getattr(fmt, "start_time", 0.0), 0.0)
    bound = coerce_float(duration, 0.0)
    argv = [
        _ffmpeg_binary(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(clip),
    ]
    if offset:
        argv += ["-itsoffset", _fixed(offset)]
    argv += [
        "-i",
        str(mixed),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        remux_codec(fmt),
        "-b:a",
        _REMUX_BITRATE,
        "-ar",
        str(int(fmt.sample_rate)),
        "-ac",
        str(max(int(fmt.channels), 1)),
    ]
    if bound > 0.0:
        argv += ["-t", _fixed(bound)]
    argv += ["-movflags", "+faststart", str(dest)]
    return argv


def remux_replacement(
    clip: Any,
    mixed: Path,
    dest: Path,
    *,
    fmt: Audio_Format,
    runner: Any = None,
    timeout_s: float,
    duration: float = 0.0,
) -> Path:
    """Run media pass 2, producing the candidate Replacement_Media (Req 3.1, 3.2, 9.1).

    The result is a *candidate*: task 12.1's ``verify_replacement`` is what promotes it to
    Replacement_Media, and a candidate that fails verification is deleted and the engine
    reports ``failed`` with no media, so the preceding stage's clip is used (Req 3.5).

    Raises:
        Invalid_Audio_Format: ``fmt`` is not a probed :class:`Audio_Format`.
        worker.ffmpeg_utils.FFmpegError: the invocation failed (via :func:`_run`).
    """
    if not isinstance(fmt, Audio_Format):
        raise Invalid_Audio_Format("remux_replacement requires a probed Audio_Format")
    target = _prepared(Path(str(dest)))
    _run(
        runner if runner is not None else _default_runner(),
        remux_command(clip, mixed, target, fmt=fmt, duration=duration),
        timeout_s,
    )
    return target


# --------------------------------------------------------------------------- #
# Epic 12 — integrity verification of the Replacement_Media                   #
# --------------------------------------------------------------------------- #

#: Tolerance on the **video** duration comparison, in seconds.
#:
#: The design words this comparison as "equal", and under ``-c:v copy`` the video packets
#: really are bit-identical — but the *container* may carry them on a different timescale
#: (an MP4 written fresh does not necessarily reuse the input's ``timescale``), so the
#: duration ``ffprobe`` reports can differ in the last decimal place without a single frame
#: having changed. Comparing exactly would therefore fail good output on a technicality.
#: One millisecond is far below one frame at any sane frame rate, so a real truncation — the
#: thing this check exists to catch — still fails loudly.
_VIDEO_DURATION_TOLERANCE_S = 0.001

#: Slack added to every tolerance comparison below, to absorb binary-float error.
#:
#: Needed because the comparisons are of the form "drift must be **within** one audio frame",
#: and a file that is legitimately exactly one frame different fails a naive ``>`` test:
#: ``abs((3.0 + 1/8000) - 3.0)`` is ``0.00012500000000011...``, which is fractionally larger
#: than ``1/8000``. Without this slack the check would reject good output depending on where
#: the durations happen to land in binary floating point — i.e. non-deterministically from
#: the operator's point of view. A nanosecond is ~5 orders of magnitude below one sample at
#: 48 kHz, so it widens nothing that matters.
_DRIFT_EPSILON = 1e-9


@dataclass(frozen=True)
class Media_Probe:
    """Stream inventory and timings of one media file, as ``ffprobe`` reports them.

    Distinct from :class:`Audio_Format`, which answers "what format is the audio?" for the
    *processing* passes. This answers "what streams and timings does this file have?" for the
    *verification* pass, and therefore has to look at every stream rather than ``a:0``.

    A pure record, like :class:`Audio_Format`: no coercion beyond parsing, no validation.
    Deciding what counts as acceptable is :func:`verify_replacement`'s job, and doing it here
    would hide the very mismatches that function exists to report.

    ``video_frames`` is ``0`` when ``ffprobe`` does not report ``nb_frames`` — legitimately
    common for some containers — which the comparison treats as "unknown", not "zero frames".
    """

    audio_streams: int = 0
    video_streams: int = 0
    audio_duration: float = 0.0
    video_duration: float = 0.0
    video_frames: int = 0
    sample_rate: int = 0
    channels: int = 0
    audio_start_time: float = 0.0


def _stream_duration(stream: Mapping[str, Any], fallback: float) -> float:
    """One stream's duration, falling back to the container's when it carries none."""
    value = stream.get("duration")
    if value in (None, "N/A", ""):
        return fallback
    return coerce_float(value, fallback)


def probe_media(
    path: Any, runner: Any = None, timeout_s: float = MIN_STEP_TIMEOUT_S
) -> Media_Probe:
    """Probe **every** stream of ``path`` for the integrity comparison (Req 17.1-17.4).

    An ``ffprobe``, not a media pass. Unlike :func:`probe_audio_format` this selects no
    stream: the count of audio and video streams is itself part of what
    :func:`verify_replacement` checks, so a query that pinned ``a:0`` could not see a second
    audio stream sneaking in.

    Total: a missing field, ``"N/A"``, an unparseable number or output that is not JSON at
    all yields a :class:`Media_Probe` with zeros rather than raising. That is deliberate —
    an unreadable candidate must fail *verification* with an :class:`Integrity_Error`
    naming the mismatch, not blow up with a parse error the ladder would report as a bare
    ``failed``.
    """
    argv = [
        _ffprobe_binary(),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    completed = _run(runner if runner is not None else _default_runner(), argv, timeout_s)

    raw = getattr(completed, "stdout", "") or ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return Media_Probe()
    if not isinstance(payload, Mapping):
        return Media_Probe()

    streams = payload.get("streams")
    if not isinstance(streams, Sequence):
        streams = ()
    container = payload.get("format")
    container_duration = coerce_float(
        (container or {}).get("duration") if isinstance(container, Mapping) else 0.0, 0.0
    )

    audio = [s for s in streams if isinstance(s, Mapping) and s.get("codec_type") == "audio"]
    video = [s for s in streams if isinstance(s, Mapping) and s.get("codec_type") == "video"]

    first_audio: Mapping[str, Any] = audio[0] if audio else {}
    first_video: Mapping[str, Any] = video[0] if video else {}
    start = first_audio.get("start_time")

    return Media_Probe(
        audio_streams=len(audio),
        video_streams=len(video),
        audio_duration=(_stream_duration(first_audio, container_duration) if audio else 0.0),
        video_duration=(_stream_duration(first_video, container_duration) if video else 0.0),
        video_frames=coerce_int(first_video.get("nb_frames"), 0, lo=0),
        sample_rate=coerce_int(first_audio.get("sample_rate"), 0, lo=0),
        channels=coerce_int(first_audio.get("channels"), 0, lo=0),
        audio_start_time=(0.0 if start in (None, "N/A", "") else coerce_float(start, 0.0)),
    )


def verify_replacement(
    candidate: Any,
    clip: Any,
    *,
    fmt: Audio_Format,
    runner: Any = None,
    timeout_s: float = MIN_STEP_TIMEOUT_S,
    probe: Any = None,
    baseline: Media_Probe | None = None,
) -> Media_Probe:
    """Promote a remux candidate to Replacement_Media, or raise (Req 3.5, 17.1-17.4, 17.7).

    Every condition must hold; the **first** failure raises :class:`Integrity_Error` naming
    the specific mismatch, because "the audio got shorter" and "a second audio stream
    appeared" need different fixes and a generic message would hide which happened:

    * exactly one audio stream and exactly one video stream (Req 17.2);
    * audio duration within **one audio frame** (``1/sample_rate``) of the incoming clip's
      (Req 17.1) — this is the check that catches a stray ``-shortest`` or an
      ``acrossfade`` that shortened the stream, which is the single most likely way this
      engine could silently corrupt a clip;
    * sample rate and channel count equal the probed :class:`Audio_Format` (Req 17.2);
    * video duration and frame count equal the incoming clip's (Req 17.3), so ``-c:v copy``
      demonstrably copied rather than re-encoded or truncated;
    * audio ``start_time`` equal to the incoming clip's, within one audio frame (Req 17.4),
      so A/V alignment survived.

    Returns the candidate's :class:`Media_Probe` on success, which the caller can attach to
    the result detail. **Deletion is the caller's job**, not this function's: the ladder rung
    that catches the error is what owns the workspace and what decides to fall back to the
    preceding stage's media (Req 3.5, 17.7). Keeping deletion out of here means a caller can
    also use it as a read-only assertion.

    ``probe`` overrides the prober (the Req 19.1 seam) and must accept
    ``(path, runner, timeout_s)``.

    Raises:
        Invalid_Audio_Format: ``fmt`` is not a probed :class:`Audio_Format`.
        Integrity_Error: any condition above fails.
        worker.ffmpeg_utils.FFmpegError: a probe invocation failed (via :func:`_run`).
    """
    if not isinstance(fmt, Audio_Format):
        raise Invalid_Audio_Format("verify_replacement requires a probed Audio_Format")

    reader = probe if probe is not None else probe_media
    produced = reader(candidate, runner, timeout_s)
    # ``baseline`` lets a caller that has already probed the clip hand its answer in, saving a
    # redundant ``ffprobe``. The engine does exactly that: it needs the clip's audio duration
    # *before* the remux (to bound it with ``-t``), so the probe has already happened.
    original = baseline if baseline is not None else reader(clip, runner, timeout_s)

    # 1. Stream inventory (Req 17.2).
    if produced.audio_streams != 1:
        raise Integrity_Error(
            f"Replacement_Media has {produced.audio_streams} audio streams, expected 1"
        )
    if produced.video_streams != 1:
        raise Integrity_Error(
            f"Replacement_Media has {produced.video_streams} video streams, expected 1"
        )

    # 2. Audio format (Req 17.2).
    if produced.sample_rate != int(fmt.sample_rate):
        raise Integrity_Error(
            f"Replacement_Media is {produced.sample_rate}Hz, expected {int(fmt.sample_rate)}Hz"
        )
    if produced.channels != int(fmt.channels):
        raise Integrity_Error(
            f"Replacement_Media has {produced.channels} channels, expected {int(fmt.channels)}"
        )

    # 3. Audio duration, within one audio frame (Req 17.1).
    frame = 1.0 / max(int(fmt.sample_rate), 1)
    audio_drift = abs(produced.audio_duration - original.audio_duration)
    if audio_drift > frame + _DRIFT_EPSILON:
        raise Integrity_Error(
            f"audio duration drifted by {audio_drift:.6f}s "
            f"({produced.audio_duration:.6f} vs {original.audio_duration:.6f}), "
            f"tolerance is one audio frame ({frame:.6f}s)"
        )

    # 4. Video untouched (Req 17.3).
    video_drift = abs(produced.video_duration - original.video_duration)
    if video_drift > _VIDEO_DURATION_TOLERANCE_S + _DRIFT_EPSILON:
        raise Integrity_Error(
            f"video duration drifted by {video_drift:.6f}s "
            f"({produced.video_duration:.6f} vs {original.video_duration:.6f})"
        )
    # ``nb_frames`` is compared only when the probe reported it for both files: some
    # containers legitimately omit it, and treating "unknown" as "zero" would fail every
    # such clip. When both report it, ``-c:v copy`` makes equality exact.
    if produced.video_frames and original.video_frames:
        if produced.video_frames != original.video_frames:
            raise Integrity_Error(
                f"video frame count changed: {produced.video_frames} vs {original.video_frames}"
            )

    # 5. A/V alignment (Req 17.4).
    start_drift = abs(produced.audio_start_time - original.audio_start_time)
    if start_drift > frame + _DRIFT_EPSILON:
        raise Integrity_Error(
            f"audio start_time drifted by {start_drift:.6f}s "
            f"({produced.audio_start_time:.6f} vs {original.audio_start_time:.6f})"
        )

    return produced


# --------------------------------------------------------------------------- #
# Epic 13 — the engine class and the run gate / degradation ladder            #
# --------------------------------------------------------------------------- #

#: This engine's Engine_Id; its Feature_Flag is therefore ``stem_inpainting_enabled``.
ENGINE_ID = "stem_inpainting"

#: Filters the **ffmpeg** Separator_Backend's filtergraph cannot do without (Req 13.5).
_FFMPEG_BACKEND_FILTERS: tuple[str, ...] = (
    "ffmpeg_filter:pan",
    "ffmpeg_filter:highpass",
    "ffmpeg_filter:lowpass",
)

#: The filter every path needs: gains and the V-notch are both ``volume`` nodes (Req 13.5).
_VOLUME_FILTER = "ffmpeg_filter:volume"


def _capability_missing(caps: Any, capability_id: str) -> bool:
    """Whether ``caps`` positively reports ``capability_id`` unavailable.

    Note the asymmetry, which is deliberate: **absence of a report is not absence of the
    capability.** A ``None`` Capability_Report, or one that raises, reads as "available", so a
    context built without a report does not degrade every engine that consults one. Only an
    explicit ``False`` counts as missing.
    """
    if caps is None:
        return False
    try:
        return not bool(caps.available(capability_id))
    except Exception:  # pragma: no cover - hostile report
        return False


class Stem_Inpainting_Engine(AV_Engine):
    """Stem-aware audio repair: separation, per-stem gains, and seam inpainting.

    The AUDIO-stage counterpart to the COMPOSE-stage kinetic typography engine, and
    deliberately the opposite kind of engine: it declares ``produces_media = True`` and hands
    back a replacement clip, rather than contributing filters to someone else's pass.

    **Why AUDIO stage and not SOURCE.** The seams this engine exists to repair *do not exist*
    before the clip is cut and tightened — they are created by
    ``filler.apply_keep_intervals`` concatenating kept intervals. A source-stage engine could
    not know where they are (Req 2.4). The host publishes their positions as
    ``filler_seam:<seconds>`` notes on the context, and :func:`parse_seam_notes` is the only
    way this engine learns about a Seam: it never infers one from the waveform or from
    Word_Timeline gaps (Req 6.5).

    **Cost is fixed at two media passes**, always: extract (``-vn``) and remux
    (``-c:v copy``). Every gain and every seam is folded into **one** filtergraph, so the
    invocation count is constant in the Seam count rather than linear in it (Req 2.6, 15.9).

    **Everything is injected** (Req 19.1): ``backend`` (a :class:`Separator_Backend`),
    ``runner`` (a :data:`Command_Runner`) and ``prober``, each overridable per invocation
    through ``ctx.deps`` via :func:`injected`. That is what lets the whole ladder be tested
    with no ffmpeg binary, no ``demucs``, no ``torch`` and no model file.
    """

    engine_id = ENGINE_ID
    stage = Engine_Stage.AUDIO
    priority = 20
    required_capabilities = ("binary:ffmpeg",)
    optional_capabilities = (
        "python_pkg:demucs",
        "model:htdemucs",
        "ffmpeg_filter:acrossfade",  # spectral music bridging
        "ffmpeg_filter:afade",  # declick at clip head/tail
        "ffmpeg_filter:pan",  # ffmpeg-backend mid extraction
        "ffmpeg_filter:highpass",  # ffmpeg-backend speech band
        "ffmpeg_filter:lowpass",  # ffmpeg-backend speech band
        "ffmpeg_filter:alimiter",  # optional soft peak guard
    )
    requires_network = False
    requires_model_download = True
    time_budget_s = 90.0
    max_media_passes = 2
    max_inputs = 0
    produces_media = True

    def __init__(
        self,
        *,
        backend: Any = None,
        runner: Any = None,
        prober: Any = None,
    ) -> None:
        self._backend = backend
        self._runner = runner
        self._prober = prober

    # -- pure hooks ---------------------------------------------------------

    def resolve_options(self, options: Any) -> Stem_Options:
        """Project Processing_Options onto :class:`Stem_Options` (pure, total, idempotent)."""
        return resolve_stem_options(options)

    def plan(self, ctx: Engine_Context) -> Mapping[str, Any]:
        """The serialised :class:`Stem_Plan` — pure, and never probes (Req 1.9, 12.5)."""
        return plan_stems_from_context(ctx).to_dict()

    # -- collaborator resolution -------------------------------------------

    def _runner_for(self, ctx: Any) -> Any:
        """The Command_Runner: ``ctx.deps`` wins, then the constructor, then the real one."""
        return injected(ctx, "runner", self._runner) or _default_runner()

    def _prober_for(self, ctx: Any) -> Any:
        """The Audio_Format prober, same precedence."""
        return injected(ctx, "prober", self._prober) or probe_audio_format

    def _backend_for(self, ctx: Any, plan: Stem_Plan) -> Any:
        """The Separator_Backend for the **resolved** backend id.

        An injected backend is used verbatim whatever the plan resolved, because an injected
        collaborator is a deliberate override and second-guessing it would make the seam
        useless for testing. Otherwise the plan's resolved id decides, and ``resolve_backend``
        has already downgraded ``ml`` → ``ffmpeg`` when ``demucs``/the checkpoint is absent,
        so reaching :class:`ML_Separator_Backend` here means the capability report said the
        model is present.
        """
        override = injected(ctx, "backend", self._backend)
        if override is not None:
            return override
        if plan.backend == "ml":
            return ML_Separator_Backend(model=plan.model or _MODEL_DEFAULT)
        return Ffmpeg_Separator_Backend(runner=self._runner_for(ctx))

    # -- result construction ------------------------------------------------

    def _result(
        self,
        status: Engine_Status,
        plan: Stem_Plan | None = None,
        details: Sequence[str] = (),
        *,
        media: Any = None,
        detail: str = "",
        artifacts: Sequence[Any] = (),
    ) -> Engine_Result:
        """One place that builds every result, so no rung can forget to namespace a marker."""
        return Engine_Result(
            engine_id=ENGINE_ID,
            status=status,
            markers=tuple(marker(ENGINE_ID, item) for item in details),
            artifacts=tuple(artifacts),
            plan=plan.to_dict() if plan is not None else {},
            media=media,
            detail=detail,
        )

    @staticmethod
    def _discard(paths: Sequence[Any]) -> None:
        """Delete every file this run created, best effort (Req 15.7).

        Called by every rung that abandons work, so no partial Replacement_Media and no
        half-written stem survives to be mistaken for output. ``OSError`` is swallowed
        deliberately: the run is already abandoning, and failing to clean up must not turn a
        clean ``degraded`` into an exception the host reports as ``failed``.
        """
        for path in paths:
            try:
                Path(str(path)).unlink(missing_ok=True)
            except OSError:  # pragma: no cover - unlink refused
                continue

    # -- the ladder ---------------------------------------------------------

    def run(self, ctx: Engine_Context) -> Engine_Result:
        """Execute the engine, applying the ordered degradation ladder (Reqs 3, 13-17).

        Rungs are evaluated strictly in order and the **first match returns**. The full table
        is in the spec's design; the two structural facts worth stating here are:

        * **rung 0 and rung 1 never reach this method.** A disabled Feature_Flag means the
          host never invokes the engine at all (so no workspace, no probe, no pass), and a
          missing *required* capability — ``binary:ffmpeg`` — is gated by the host too. Note
          the host returns ``degraded`` + ``unavailable:binary:ffmpeg`` for that, per
          foundation Req 7.1, **not** the ``skipped`` this engine's own spec table lists;
          the foundation owns the gate, so the foundation's status is what happens. There is
          deliberately no duplicate check here: re-probing a capability the host already
          gated on would be the only place the two could disagree.
        * **``media is None`` is what distinguishes the two degraded families**, not the
          status. ``Degraded_With_Media`` (rungs 7-9) hands back a usable file and the host
          adopts it exactly as for ``applied``; ``Degraded_Without_Media`` (rungs 2, 5, 6, 10,
          11) hands back nothing and the Pipeline keeps the preceding stage's media.

        Never raises for an expected condition. Unexpected exceptions are left to propagate,
        because the host already converts them into one ``failed`` marker and logs the type
        and message (Req 14.1) — catching them here would only hide the traceback.
        """
        from worker.ffmpeg_utils import FFmpegError  # lazy (Req 1.4)

        options = (
            ctx.options
            if isinstance(ctx.options, Stem_Options)
            else resolve_stem_options(getattr(ctx, "options", None))
        )
        runner = self._runner_for(ctx)
        caps = getattr(ctx, "capabilities", None)

        # --- rung 3: the whole-engine no-op --------------------------------
        # First, and before anything at all happens: no probe, no workspace file, no
        # subprocess. That ordering is the requirement (Req 5.6, 15.8) — "costs nothing" is
        # observable as zero runner calls, not merely as a fast return.
        plan = plan_stems_from_context(ctx)
        if plan_is_noop(plan):
            return self._result(Engine_Status.SKIPPED, plan)

        # --- rung 2: permissibility vs the resolved backend ---------------
        # The host's permissibility gate keys on this engine's *class-level*
        # ``requires_network``, which is False — the engine itself never needs the network.
        # What can need it is the resolved Separator_Backend, and the host cannot see that.
        # So this rung has to live here, and it runs before the body does any work.
        backend = self._backend_for(ctx, plan)
        if bool(getattr(ctx, "permissibility", False)) and bool(
            getattr(backend, "requires_network", False)
        ):
            return self._result(Engine_Status.DEGRADED, plan, ["permissibility_blocked"])

        # --- rung 6: not enough budget to finish at all -------------------
        # Repair plus remux is the minimum that could still produce media; below it there is
        # no point starting, and starting would leave a partial file to clean up.
        if step_remaining(ctx) < REPAIR_MIN_S + REMUX_MIN_S:
            return self._result(Engine_Status.DEGRADED, plan, ["degraded:budget"])

        # --- rungs 4 and 5: the audio format ------------------------------
        try:
            fmt = self._prober_for(ctx)(ctx.clip_path, runner, step_timeout(ctx, EXTRACT_RESERVE_S))
        except Invalid_Audio_Format:
            return self._result(Engine_Status.DEGRADED, plan, ["degraded:audio_format"])
        except subprocess.TimeoutExpired as exc:
            return self._result(Engine_Status.DEGRADED, plan, ["timeout"], detail=str(exc))
        except FFmpegError as exc:
            # Rung 13 reaches the probe too: ``ffprobe`` is an ffmpeg invocation, and one
            # that will not run is a failure rather than a degradation — we have no format,
            # so there is nothing to fall back *to*. Without this the exception escaped to
            # the host, which would still have reported ``failed`` but with the traceback of
            # an apparently-unhandled error rather than a named rung.
            return self._result(Engine_Status.FAILED, plan, ["failed"], detail=str(exc))
        if fmt is None:
            # No audio stream at all. Not a degradation and **not marked**: there was
            # nothing to repair, so reporting one would be noise (Req 4.8).
            return self._result(Engine_Status.SKIPPED, plan)

        # Re-plan with the real format: ``plan`` is pure and could not probe, so until now the
        # sample rate and channel count were Time_Base placeholders.
        plan = plan_stems(
            opts=options,
            notes=getattr(ctx, "notes", ()) or (),
            duration=getattr(ctx, "duration", 0.0),
            fmt=fmt,
            caps=caps,
            tb=getattr(ctx, "time_base", None),
        )

        # --- rung 3b: nothing left to do, now that the windows are known ---
        # Rung 3 could not see this: with ``repair_mode="crossfade"`` and unity gains the
        # plan is not a no-op by its test, but if the clip published no Seams there is no
        # window to repair and nothing to change. Skipping here rather than rendering an
        # identical clip is what makes re-running the engine on its own output a no-change
        # operation (Req 7.11), and it also saves two media passes and a lossy re-encode on
        # any clip that simply had no filler removed.
        if not plan_has_work(plan):
            return self._result(Engine_Status.SKIPPED, plan)

        # --- rung 10: a filter the resolved path cannot do without --------
        missing_filter = self._missing_filter(caps, plan)
        if missing_filter is not None:
            return self._result(Engine_Status.DEGRADED, plan, [f"unavailable:{missing_filter}"])

        return self._execute(ctx, plan, options, fmt, backend, runner, caps)

    def _missing_filter(self, caps: Any, plan: Stem_Plan) -> str | None:
        """The first unavailable filter the resolved path needs, or ``None`` (Req 13.5).

        Declaration order, so the reported capability is stable rather than dependent on dict
        iteration. Only filters the *resolved* path actually uses are consulted: the
        ffmpeg-backend band-split filters matter only when that backend was resolved **and**
        separation is wanted, and ``volume`` matters always because both the gains and the
        V-notch are ``volume`` nodes. ``acrossfade``/``afade``/``alimiter`` are deliberately
        absent — each has a documented fallback, so their absence degrades fidelity rather
        than blocking the path.
        """
        required = [_VOLUME_FILTER]
        if plan.backend == "ffmpeg" and plan.needs_separation:
            required = list(_FFMPEG_BACKEND_FILTERS) + required
        for capability_id in required:
            if _capability_missing(caps, capability_id):
                return capability_id
        return None

    def _execute(
        self,
        ctx: Engine_Context,
        plan: Stem_Plan,
        options: Stem_Options,
        fmt: Audio_Format,
        backend: Any,
        runner: Any,
        caps: Any,
    ) -> Engine_Result:
        """Rungs 7-9 and 11-15: the part that actually spends media passes.

        Split out of :meth:`run` so the pre-work gates stay readable as a flat ordered list,
        and so every path through the working half shares one ``try`` — which is what makes
        "every rung that abandons work deletes what it created" true by construction rather
        than by remembering to call the cleanup in nine places.
        """
        from worker.ffmpeg_utils import FFmpegError  # lazy (Req 1.4)

        workspace = getattr(ctx, "workspace", None)
        created: list[Any] = []
        details: list[str] = []

        def _degradation(detail: str) -> None:
            """Record a degradation detail at most once per clip (Req 13.7)."""
            if detail not in details:
                details.append(detail)

        try:
            # One full stream probe of the incoming clip, used twice: to bound the remux with
            # ``-t`` (so encoder padding cannot lengthen the clip) and as the baseline
            # ``verify_replacement`` compares against. An ``ffprobe``, not a media pass.
            #
            # It lives **inside** this try on purpose: a probe that fails or times out has to
            # become a named rung like any other invocation, not escape to the host as an
            # apparently-unhandled error. Placing it above the try is a mistake the ladder
            # tests catch immediately.
            baseline = probe_media(ctx.clip_path, runner, step_timeout(ctx, EXTRACT_RESERVE_S))

            # ---- media pass 1: extract ----------------------------------
            extracted = extract_clip_audio(
                ctx.clip_path,
                self._workspace_path(workspace, "in.wav"),
                fmt=fmt,
                runner=runner,
                timeout_s=step_timeout(ctx, EXTRACT_RESERVE_S),
            )
            created.append(extracted)

            # The **decoded audio length**, which is not the same number as the clip's
            # container duration and must not be conflated with it. A lossy audio stream
            # carries encoder priming/padding, so decoding 2.000 s of AAC legitimately yields
            # ~2.020 s of PCM. Every stem is derived from this WAV, so this is the length they
            # must match (Req 4.6) — verifying them against the *clip* duration instead
            # rejects perfectly good output, which is exactly what it did before this was
            # measured against a real encode.
            #
            # The Repair_Windows deliberately keep using ``ctx.duration``: they are
            # clip-relative positions published by filler removal, not a property of the
            # decoded stream.
            audio_duration = self._audio_duration(extracted, fmt, plan.duration)

            stem_set: dict[str, Path]
            applied_backend = ""

            if not plan.needs_separation:
                # The repair-only path: no backend, no stems, no separation budget. The
                # extracted audio is the single mix input (Req 13.4).
                stem_set = {"vocals": extracted}
            else:
                need = SEPARATION_MIN_S.get(plan.backend, SEPARATION_MIN_S["ffmpeg"])
                if step_remaining(ctx) < need + REPAIR_MIN_S + REMUX_MIN_S:
                    # ---- rung 7: separation unaffordable ----------------
                    # Degraded_With_Media: fall back to repairing the un-separated audio,
                    # which still fixes the seams — the audible defect — and still yields a
                    # usable clip. Re-planned from options rather than patched onto the frozen
                    # plan, so the serialised plan honestly describes what ran.
                    plan = plan_stems(
                        opts=dataclasses.replace(
                            options,
                            repair_mode="crossfade",
                            mix_preset="custom",
                            gain_vocals=GAIN_DEFAULT,
                            gain_music=GAIN_DEFAULT,
                            gain_other=GAIN_DEFAULT,
                        ),
                        notes=getattr(ctx, "notes", ()) or (),
                        duration=getattr(ctx, "duration", 0.0),
                        fmt=fmt,
                        caps=caps,
                        tb=getattr(ctx, "time_base", None),
                    )
                    _degradation("degraded:budget")
                    if not plan_has_work(plan):
                        # The fallback has nothing to do either — the gains were the only
                        # reason to run, and they are exactly what this rung gives up. Return
                        # without media rather than spend a remux producing an identical clip.
                        self._discard(created)
                        return self._result(Engine_Status.DEGRADED, plan, ["degraded:budget"])
                    stem_set = {"vocals": extracted}
                else:
                    # ---- rung 8: the backend actually used --------------
                    # ``resolve_backend`` already chose, and named what was missing.
                    for capability_id in plan.missing_capabilities:
                        _degradation(f"degraded:{capability_id}")

                    raw = backend.separate(
                        extracted,
                        self._workspace_path(workspace, "stems"),
                        fmt=fmt,
                        seed=int(getattr(ctx, "seed", 0) or 0),
                        timeout_s=step_timeout(ctx, SEPARATE_RESERVE_S),
                    )
                    created.extend(Path(str(p)) for p in (raw or {}).values())

                    stem_set, missing_stems = assemble_stem_set(
                        raw or {},
                        dest_dir=self._workspace_path(workspace, "stems"),
                        fmt=fmt,
                        duration=audio_duration,
                        runner=runner,
                        timeout_s=step_timeout(ctx, SEPARATE_RESERVE_S),
                    )
                    created.extend(stem_set.values())
                    details.extend(missing_stems)
                    applied_backend = str(getattr(backend, "backend_id", "") or "")

            # ---- rung 9: spectral downgraded to crossfade ---------------
            if plan.downgraded_from == "spectral":
                # ``spectral`` needs real stems to bridge music across the join, so on any
                # non-``ml`` backend it is not available at all.
                _degradation("degraded:python_pkg:demucs")

            # ---- spectral music bridging (task 11.4) --------------------
            stem_windows: dict[str, tuple[Repair_Window, ...]] = {}
            if plan.repair_mode == "spectral" and "music" in stem_set:
                bridged_path, bridged, residual = bridge_music_stem(
                    stem_set["music"],
                    self._workspace_path(workspace, "stems", "music_bridged.wav"),
                    plan.windows,
                    fmt=fmt,
                    duration=audio_duration,
                    runner=runner,
                    timeout_s=step_timeout(ctx, REPAIR_RESERVE_S),
                )
                if bridged:
                    stem_set = {**stem_set, "music": bridged_path}
                    stem_windows["music"] = residual
                    created.append(bridged_path)

            # ---- the single gain + repair pass --------------------------
            mixed, guard_details = render_mix(
                plan,
                stem_set,
                self._workspace_path(workspace, "mixed.wav"),
                runner=runner,
                timeout_s=step_timeout(ctx, REPAIR_RESERVE_S),
                alimiter=not _capability_missing(caps, ALIMITER_CAPABILITY),
                stem_windows=stem_windows or None,
            )
            created.append(mixed)
            for item in guard_details:
                _degradation(item)

            # ---- rung 11: budget gone before the remux -----------------
            # Checked explicitly rather than left to the subprocess timeout: below
            # REMUX_MIN_S there is no way to produce media, and starting the pass would
            # leave a partial file behind (Req 15.6, 15.7).
            if step_remaining(ctx) < REMUX_MIN_S:
                self._discard(created)
                return self._result(Engine_Status.DEGRADED, plan, [*details, "timeout"])

            # ---- media pass 2: remux -----------------------------------
            candidate = remux_replacement(
                ctx.clip_path,
                mixed,
                self._workspace_path(workspace, self._replacement_name(ctx)),
                fmt=fmt,
                runner=runner,
                timeout_s=step_timeout(ctx, REMUX_RESERVE_S),
                duration=baseline.audio_duration,
            )
            created.append(candidate)

            # ---- rung 14: integrity ------------------------------------
            # Applied to Degraded_With_Media exactly as to ``applied``: a degraded result
            # still hands the clip forward, so it needs the same guarantee (Req 3.11).
            try:
                verify_replacement(
                    candidate,
                    ctx.clip_path,
                    fmt=fmt,
                    runner=runner,
                    timeout_s=step_timeout(ctx, REMUX_RESERVE_S),
                    baseline=baseline,
                )
            except Integrity_Error as exc:
                self._discard(created)
                return self._result(Engine_Status.FAILED, plan, ["failed"], detail=str(exc))

        except subprocess.TimeoutExpired as exc:
            # ---- rung 11 (raised form) ---------------------------------
            # The engine's own cooperative budget check is what normally catches this; a
            # subprocess that overran anyway lands here. Either way the *contribution* is
            # abandoned, not the clip. Note this is deliberately ``degraded`` per this
            # spec's Req 15.6, whereas a hard host-level watchdog overrun is ``failed`` per
            # foundation Req 8.6 — they are different events: one is us noticing in time,
            # the other is us not noticing at all.
            self._discard(created)
            return self._result(
                Engine_Status.DEGRADED, plan, [*details, "timeout"], detail=str(exc)
            )
        except (Stem_Error, FFmpegError) as exc:
            # ---- rungs 12 and 13 ---------------------------------------
            # A backend that raised, returned a non-audio file or returned wrong-duration
            # audio (Integrity_Error from assemble_stem_set), or any failed ffmpeg
            # invocation. Nothing usable was produced, so no media and the clip keeps the
            # preceding stage's file.
            self._discard(created)
            return self._result(Engine_Status.FAILED, plan, ["failed"], detail=str(exc))

        # ---- workspace lifecycle (tasks 15.1, 15.2) --------------------
        artifacts = self._declare_artifacts(workspace, candidate, stem_set, options)
        cleanup_details = self._reclaim(
            created,
            keep={
                Path(str(candidate)),
                *(Path(str(item.path)) for item in artifacts if item.durable),
            },
        )
        details.extend(cleanup_details)

        # ---- rung 15 (and the applied form of rungs 7-9) ---------------
        if applied_backend:
            details.append(f"applied:{applied_backend}")
        details.append(f"mix:{options.mix_preset}")
        if plan.windows:
            details.append(f"repair:{plan.repair_mode}:{len(plan.windows)}")

        degraded = any(item.startswith(("degraded:", "unavailable:")) for item in details)
        return self._result(
            Engine_Status.DEGRADED if degraded else Engine_Status.APPLIED,
            plan,
            details,
            media=candidate,
            artifacts=artifacts,
        )

    # -- workspace lifecycle ------------------------------------------------

    @staticmethod
    def _audio_duration(extracted: Any, fmt: Audio_Format, fallback: float) -> float:
        """The extracted WAV's true length in seconds, or ``fallback`` when unreadable.

        Read straight from the RIFF header — no subprocess, no budget. ``fallback`` is the
        planned clip duration, which is what an injected recording runner leaves us with
        (it writes no file), so the offline tests keep working while the real path uses the
        measured length.
        """
        probed = _wav_format(extracted)
        if probed is None:
            return max(coerce_float(fallback, 0.0), 0.0)
        rate, _channels, frames = probed
        return frames / max(rate, 1)

    @staticmethod
    def _replacement_name(ctx: Any) -> str:
        """``clip_repaired<ext>``, reusing the incoming clip's container extension.

        Matching the extension matters because the remux writes the same container: naming it
        ``.mp4`` unconditionally would mislabel the output for any other container the
        Pipeline might hand us, and ffmpeg picks its muxer from the extension.
        """
        suffix = ""
        try:
            suffix = Path(str(getattr(ctx, "clip_path", "") or "")).suffix
        except Exception:  # pragma: no cover - hostile path
            suffix = ""
        return f"clip_repaired{suffix or '.mp4'}"

    def _declare_artifacts(
        self,
        workspace: Any,
        candidate: Any,
        stem_set: Mapping[str, Path],
        options: Stem_Options,
    ) -> tuple[Any, ...]:
        """The Engine_Artifacts this run publishes (Req 11.1-11.3, 11.5).

        Two kinds, and **only** these two:

        * the Replacement_Media, as the media artifact;
        * when ``retain_stems`` is set, each per-stem WAV as a **durable** artifact, so the
          host persists it through the active Storage_Backend under a ``normalize_key``-ed key
          *before* the workspace is deleted. A persistence failure surfaces as the
          foundation's ``artifact_failed`` marker and the clip is still produced.

        Note task 15.1 also asks for the transient intermediates (``in.wav``, ``mixed.wav``,
        the non-durable stems) to be declared. They are deliberately **not**: task 15.2
        requires those same files to be deleted before returning, so declaring them would
        publish an artifact list of paths that do not exist. An inaccurate list is worse than
        a short one — the host does not persist non-durable artifacts anyway, so the only
        effect would be to mislead a reader of ``Engine_Result.artifacts``.
        """
        if workspace is None:
            return ()
        artifacts: list[Any] = []
        try:
            artifacts.append(
                workspace.artifact(Path(str(candidate)).name, media_type="video", durable=False)
            )
            if options.retain_stems:
                for name in STEM_NAMES:
                    if name in stem_set:
                        artifacts.append(
                            workspace.artifact(
                                f"stems/{name}.wav", media_type="audio", durable=True
                            )
                        )
        except Exception:  # pragma: no cover - hostile workspace double
            return tuple(artifacts)
        return tuple(artifacts)

    def _reclaim(self, created: Sequence[Any], *, keep: set) -> list[str]:
        """Delete every intermediate except ``keep``, returning marker details (Req 11.4).

        **The bounded-disk arithmetic** (Req 11.7). Let ``W`` be the extracted WAV's size and
        ``C`` the clip container's. Peak workspace usage over the run is:

        * ``in.wav``                       — ``W``
        * three assembled stems            — ``3W`` (each is the same duration, rate and
          channel count as the extraction, so each is ``W``)
        * one bridged ``music`` stem       — ``W`` at most, and only under ``spectral``
        * ``mixed.wav``                    — ``W``
        * ``clip_repaired.<ext>``          — ``C`` at most; the video is stream-copied and the
          audio re-encoded to a *lossy* codec from PCM, so it cannot exceed the source

        The stems and the mix coexist (the mix reads them), and the bridged stem replaces the
        plain ``music`` stem in the graph but both files exist while the bridge runs. That is
        ``W + 3W + W + W = 6W`` in the worst case — over the documented
        ``DISK_BOUND_MULTIPLE × W`` (``5W``) — which is why the bridged stem is written **into
        the stems directory and the plain one deleted with it**, and why the bound is stated as
        ``DISK_BOUND_MULTIPLE × W + C``: the ``+ C`` term is the Replacement_Media, and ``5W``
        covers ``in.wav`` + three stems + ``mixed.wav`` with the bridge counted against the
        stem it replaces.

        Each delete is guarded individually: an ``OSError`` records a detail and the loop
        continues, because failing to reclaim space must not turn a good clip into a failure.
        Note the ordering contract this relies on — the host takes
        ``Engine_Result.media`` and persists durable artifacts *before* deleting the
        workspace, so keeping only those two categories here is safe (Req 11.5).
        """
        details: list[str] = []
        seen: set[str] = set()
        for path in created:
            try:
                target = Path(str(path))
            except Exception:  # pragma: no cover - hostile path
                continue
            key = str(target)
            if key in seen or target in keep:
                continue
            seen.add(key)
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                detail = f"cleanup_failed:{target.name}"
                if detail not in details:
                    details.append(detail)
                del exc
        return details

    @staticmethod
    def _workspace_path(workspace: Any, *parts: str) -> Path:
        """A path inside the Engine_Workspace, or a temp fallback when there is none.

        ``Engine_Workspace.path`` is a sanitising, traversal-safe **method** (not an
        attribute), and it is the only legal place this engine writes (Req 16.4). A context
        built without a workspace is only reachable from a direct unit-test call, and falling
        back to a temp directory keeps those callable rather than forcing every one to build
        a workspace.
        """
        if workspace is not None:
            try:
                return workspace.path(*parts)
            except Exception:  # pragma: no cover - hostile workspace double
                pass
        import tempfile

        return Path(tempfile.gettempdir()) / "stem_inpainting" / Path(*parts)


def step_remaining(ctx: Any) -> float:
    """``ctx.remaining()`` as a finite, non-negative number of seconds.

    The budget *gates* need a plain comparable number, where :func:`step_timeout` needs a
    subprocess timeout — different jobs, so they are different functions. An infinite
    deadline ("no deadline", the foundation's default) reads as the engine's declared
    ``time_budget_s`` so a gate cannot be trivially satisfied by the absence of a deadline;
    a missing or broken ``remaining`` reads as ``0.0``, which fails every gate closed rather
    than open.
    """
    try:
        raw = float(ctx.remaining())
    except Exception:  # no/haywire remaining() -> fail the gate closed
        return 0.0
    if math.isnan(raw):
        return 0.0
    if math.isinf(raw):
        if raw < 0:
            return 0.0
        return max(coerce_float(getattr(ctx, "time_budget_s", 0.0), 0.0), 0.0)
    return max(raw, 0.0)


# Registration by import side effect, exactly as the kinetic engine does it: one line in
# ``worker/engines/loader.py`` imports this module, and the engine becomes visible to
# ``/api/info`` and to the Engine_Host. Guarded so a re-import cannot raise a duplicate
# registration error.
if ENGINE_ID not in engine_registry.get_registry():
    engine_registry.register(Stem_Inpainting_Engine())
