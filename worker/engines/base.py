"""AV engine contracts: the abstraction every advanced AV engine implements.

Three layers live here, all of them **pure** and all of them import-safe:

* the value layer — :class:`Engine_Stage`, :class:`Engine_Status`,
  :class:`Engine_Artifact`, :class:`Compose_Input`,
  :class:`Compose_Contribution`, :class:`Engine_Context`,
  :class:`Engine_Result`, plus :func:`marker` / :func:`merge_markers`
  (Reqs 1.2, 1.3, 1.5, 1.6, 3.2-3.4, 3.6, 15.1, 15.7);
* the options layer — the :class:`Engine_Options` protocol, the ``coerce_*``
  helpers, :func:`dump_options`, :func:`options_digest`, :func:`derive_seed`
  (Reqs 10, 11, 12.4, 12.6);
* the abstract base — :class:`AV_Engine` (Reqs 1.1, 4.1, 9.1, 9.2, 12.5, 19.1,
  21.1, 22.1).

Import-safe with zero optional heavy dependencies present (Req 1.4): this module
imports only the standard library plus ``worker.engines.timebase``.
``Engine_Workspace`` (``worker.engines.artifacts``) and ``Capability_Report``
(``worker.engines.capabilities``) are referenced in annotations **as strings
only** — ``from __future__ import annotations`` keeps them unevaluated, so this
module never imports those siblings at runtime and stays loadable on its own.

Nothing here touches ffmpeg, OpenCV, torch, the network, the filesystem, or the
global ``random`` module: randomness comes exclusively from
:meth:`Engine_Context.rng` (Req 12.2).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Protocol,
    TypeGuard,
    runtime_checkable,
)

from worker.engines.timebase import Time_Base

if TYPE_CHECKING:
    # Type-checking only, which is what preserves the import discipline stated in the module
    # docstring: at runtime this module still imports nothing but the standard library and
    # `worker.engines.timebase`, and `test_every_engine_module_imports_without_heavy_dependencies`
    # proves it in a fresh interpreter.
    #
    # These two names were previously bare string annotations carrying a `noqa: F821`, which
    # silenced the linter but left them genuinely undefined — so mypy reported them and no tool
    # could check either field. `worker.engines.artifacts` imports `Engine_Artifact` from this
    # module, so the reference is circular; that is fine here because the cycle exists only for
    # the type checker, which resolves it, and never at runtime.
    from worker.engines.artifacts import Engine_Workspace
    from worker.engines.capabilities import Capability_Report

__all__ = [
    "DIGEST_LENGTH",
    "MARKER_PREFIX",
    "FLAG_SUFFIX",
    "Engine_Stage",
    "Engine_Status",
    "Engine_Artifact",
    "Compose_Input",
    "Compose_Contribution",
    "Engine_Context",
    "Engine_Result",
    "marker",
    "merge_markers",
    "Engine_Options",
    "coerce_bool",
    "coerce_int",
    "coerce_float",
    "coerce_choice",
    "coerce_str",
    "dump_options",
    "options_digest",
    "derive_seed",
    "AV_Engine",
]

DIGEST_LENGTH = 16  # Req 11.5 — fixed-length lowercase hex
MARKER_PREFIX = "engine"  # Req 3.3 — engine:<engine_id>:<detail>
FLAG_SUFFIX = "_enabled"  # Req 9.1 — ProcessingOptions.<engine_id>_enabled

#: Guard for pathological (deeply nested or self-referencing) option values: at
#: this nesting depth :func:`_json_safe` stops recursing and stringifies, so
#: neither :func:`dump_options` nor :func:`options_digest` can blow the stack.
_MAX_DEPTH = 32

#: Truthy/falsy spellings recognised for boolean-ish form and JSON values. The
#: truthy set is exactly ``worker.models._as_bool``'s (Req 10.7); the falsy set
#: is its documented complement, so an unrecognised spelling is *malformed* and
#: falls back to the caller's default rather than silently reading as ``False``.
_TRUE_STRINGS = ("1", "true", "yes", "on")
_FALSE_STRINGS = ("0", "false", "no", "off", "", "none", "null")


class Engine_Stage(str, Enum):
    """Pipeline point at which an engine runs (mirrors JobStatus's str-Enum style)."""

    SOURCE = "source"  # once per source, before the clip loop (Req 3.5)
    AUDIO = "audio"  # per clip, after filler removal, before geometry
    GEOMETRY = "geometry"  # per clip, after the geometry ladder
    COMPOSE = "compose"  # per clip, contributes to the single compositor pass
    POST = "post"  # per clip, after the final file + thumbnail exist


class Engine_Status(str, Enum):
    """Outcome of one engine invocation (Req 1.6)."""

    APPLIED = "applied"
    SKIPPED = "skipped"
    DEGRADED = "degraded"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Internal, total helpers
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> TypeGuard[int | float]:
    """True for a real numeric value that is not a bool.

    Typed as a :class:`TypeGuard` rather than plain ``bool`` so the narrowing this performs is
    visible to the type checker. Every caller here follows the check with ``float(value)``, and
    without the guard each of those reads as ``float(<something> | None)`` — which was two of the
    findings in this module and would have needed a suppression apiece to silence.

    Mirrors ``worker.engines.timebase._is_number``: a ``bool`` is deliberately
    **not** a number here even though ``isinstance(True, int)`` is ``True`` in
    Python. A boolean landing in a numeric option field is a type confusion, so
    :func:`coerce_int` / :func:`coerce_float` treat it as malformed and return
    their default. (:func:`coerce_bool` conversely *does* accept ints and floats,
    exactly as ``worker.models._as_bool`` does.)
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_text(value: Any) -> str:
    """Return ``value`` as a ``str``, never raising."""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # pragma: no cover - __str__ that raises
        return repr(type(value))


def _sha256_hex(payload: str) -> str:
    """Hex digest of ``payload``.

    ``errors="surrogatepass"`` keeps the encode total for lone surrogates (which
    plain UTF-8 rejects) without changing the bytes for well-formed text, so the
    hash is defined for every possible string.
    """
    return hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Return a JSON-encodable, deterministically ordered copy of ``value``.

    Total by construction and idempotent (applying it twice is a no-op), which is
    what makes the dump/parse round-trip and the digest stable:

    * dataclass instances become mappings (private keys dropped);
    * mappings become ``dict``s with ``str`` keys in sorted key order (Req 12.6);
    * lists/tuples become lists; sets become lists sorted by their rendered form;
    * ``bool``/``int``/``float``/``str``/``None`` pass through unchanged
      (``NaN``/``inf`` included: ``json.dumps`` renders them deterministically);
    * ``Enum`` yields its value, ``Path`` and everything else its ``str``;
    * recursion beyond :data:`_MAX_DEPTH` stringifies, so a cyclic or absurdly
      nested structure degrades instead of raising ``RecursionError``.
    """
    if depth >= _MAX_DEPTH:
        return _as_text(value)

    if value is None or isinstance(value, (bool, int, float, str)):
        # ``bool`` first: it is an ``int`` subclass and must stay a JSON boolean.
        if isinstance(value, bool):
            return bool(value)
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            return float(value)
        if isinstance(value, str):
            return value
        return None

    if isinstance(value, Enum):
        return _json_safe(value.value, depth + 1)

    if isinstance(value, Path):
        return str(value)

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            fields = dataclasses.fields(value)
        except Exception:  # pragma: no cover - defensive
            return _as_text(value)
        record: dict[str, Any] = {}
        for entry in fields:
            if entry.name.startswith("_"):
                continue
            record[entry.name] = _json_safe(getattr(value, entry.name, None), depth + 1)
        return {key: record[key] for key in sorted(record)}

    if isinstance(value, Mapping):
        record = {}
        try:
            items = list(value.items())
        except Exception:  # pragma: no cover - hostile mapping
            return _as_text(value)
        for raw_key, raw_value in items:
            key = raw_key if isinstance(raw_key, str) else _as_text(raw_key)
            if key.startswith("_"):
                continue
            record[key] = _json_safe(raw_value, depth + 1)
        return {key: record[key] for key in sorted(record)}

    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")

    if isinstance(value, (set, frozenset)):
        rendered = [_json_safe(item, depth + 1) for item in value]
        return sorted(rendered, key=lambda item: json.dumps(item, sort_keys=True, default=str))

    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in value]

    if isinstance(value, Iterable):
        try:
            items = list(value)
        except Exception:  # pragma: no cover - hostile iterable
            return _as_text(value)
        return [_json_safe(item, depth + 1) for item in items]

    return _as_text(value)


def _as_tuple(value: Any) -> tuple[Any, ...]:
    """Return ``value`` as a tuple, treating scalars and junk as single/empty items."""
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        return (value,)
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, Iterable):
        try:
            return tuple(value)
        except Exception:  # pragma: no cover - hostile iterable
            return ()
    return (value,)


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    """Return ``value`` as a tuple of strings, never raising."""
    return tuple(_as_text(item) for item in _as_tuple(value))


def _as_path(value: Any) -> Path | None:
    """Return ``value`` as a :class:`~pathlib.Path`, or ``None`` when unusable."""
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        text = (
            value.decode("utf-8", errors="replace")
            if isinstance(value, (bytes, bytearray))
            else value
        )
        if not text:
            return None
        try:
            return Path(text)
        except Exception:  # pragma: no cover - defensive
            return None
    return None


# ---------------------------------------------------------------------------
# Frozen value records (Reqs 1.2, 1.3, 1.5, 1.6, 15.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Engine_Artifact:
    """A file produced by an engine (Reqs 17.7, 18.5)."""

    name: str  # workspace-relative file name
    path: Path  # absolute path inside the Engine_Workspace
    media_type: str = "data"  # video | audio | image | subtitle | data
    durable: bool = False  # persist through the Storage_Backend
    storage_key: str = ""  # set by the host after persistence (Req 18.5)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _as_text(self.name))
        object.__setattr__(self, "path", _as_path(self.path) or Path(""))
        object.__setattr__(self, "media_type", _as_text(self.media_type))
        object.__setattr__(self, "durable", bool(self.durable))
        object.__setattr__(self, "storage_key", _as_text(self.storage_key))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-encodable mapping (Req 18.5)."""
        return {
            "name": self.name,
            "path": str(self.path),
            "media_type": self.media_type,
            "durable": bool(self.durable),
            "storage_key": self.storage_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Engine_Artifact:
        """Rebuild from :meth:`to_dict` output, tolerating missing/hostile fields."""
        if not isinstance(data, Mapping):
            return cls(name="", path=Path(""))
        return cls(
            name=_as_text(data.get("name", "")),
            path=_as_path(data.get("path")) or Path(""),
            media_type=_as_text(data.get("media_type", "data")),
            durable=coerce_bool(data.get("durable", False), False),
            storage_key=_as_text(data.get("storage_key", "")),
        )


@dataclass(frozen=True)
class Compose_Input:
    """An extra ffmpeg input a compose-stage engine needs (Req 1.5)."""

    path: Path
    loop: bool = False  # still images: ``-loop 1``
    duration: float = 0.0  # 0 => natural duration

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _as_path(self.path) or Path(""))
        object.__setattr__(self, "loop", bool(self.loop))
        object.__setattr__(self, "duration", coerce_float(self.duration, 0.0, lo=0.0))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-encodable mapping."""
        return {
            "path": str(self.path),
            "loop": bool(self.loop),
            "duration": float(self.duration),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Compose_Input:
        """Rebuild from :meth:`to_dict` output, tolerating missing/hostile fields."""
        if not isinstance(data, Mapping):
            return cls(path=Path(""))
        return cls(
            path=_as_path(data.get("path")) or Path(""),
            loop=coerce_bool(data.get("loop", False), False),
            duration=coerce_float(data.get("duration", 0.0), 0.0, lo=0.0),
        )


@dataclass(frozen=True)
class Compose_Contribution:
    """Filter-graph fragments an engine contributes to the ONE compositor pass.

    The engine never invokes ffmpeg itself (Req 1.5). ``video_filters`` /
    ``audio_filters`` are appended to the compositor's existing
    ``-filter_complex`` chain; ``subtitle_path`` is handed to the existing libass
    ``subtitles`` filter slot.
    """

    engine_id: str
    inputs: tuple[Compose_Input, ...] = ()
    video_filters: tuple[str, ...] = ()
    audio_filters: tuple[str, ...] = ()
    subtitle_path: Path | None = None
    z_order: int = 0  # lower renders first; captions stay on top

    def __post_init__(self) -> None:
        object.__setattr__(self, "engine_id", _as_text(self.engine_id))
        object.__setattr__(
            self,
            "inputs",
            tuple(
                item
                if isinstance(item, Compose_Input)
                else Compose_Input.from_dict(item)
                if isinstance(item, Mapping)
                else Compose_Input(path=_as_path(item) or Path(""))
                for item in _as_tuple(self.inputs)
            ),
        )
        object.__setattr__(self, "video_filters", _as_str_tuple(self.video_filters))
        object.__setattr__(self, "audio_filters", _as_str_tuple(self.audio_filters))
        object.__setattr__(self, "subtitle_path", _as_path(self.subtitle_path))
        object.__setattr__(self, "z_order", coerce_int(self.z_order, 0))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-encodable mapping."""
        return {
            "engine_id": self.engine_id,
            "inputs": [item.to_dict() for item in self.inputs],
            "video_filters": list(self.video_filters),
            "audio_filters": list(self.audio_filters),
            "subtitle_path": str(self.subtitle_path) if self.subtitle_path else None,
            "z_order": int(self.z_order),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Compose_Contribution:
        """Rebuild from :meth:`to_dict` output, tolerating missing/hostile fields."""
        if not isinstance(data, Mapping):
            return cls(engine_id="")
        return cls(
            engine_id=_as_text(data.get("engine_id", "")),
            inputs=tuple(
                Compose_Input.from_dict(item) if isinstance(item, Mapping) else item
                for item in _as_tuple(data.get("inputs", ()))
            ),
            video_filters=_as_str_tuple(data.get("video_filters", ())),
            audio_filters=_as_str_tuple(data.get("audio_filters", ())),
            subtitle_path=_as_path(data.get("subtitle_path")),
            z_order=coerce_int(data.get("z_order", 0), 0),
        )


@dataclass(frozen=True)
class Engine_Context:
    """Immutable per-invocation record handed to an engine (Reqs 1.2, 1.3, 15.1).

    Every timestamp is a float in **clip-relative seconds** (Req 15.7). The record
    is frozen and its collection fields are tuples/mappings, so an engine cannot
    write back into the host's state: every attempted field assignment raises
    ``dataclasses.FrozenInstanceError`` (Req 1.3).
    """

    job_id: str
    clip_id: str
    engine_id: str
    stage: Engine_Stage
    source_path: Path  # original source media
    clip_path: Path | None  # current clip media (None at SOURCE stage)
    time_base: Time_Base  # identical for every engine of this clip (13.7)
    clip_start: float  # source-relative provenance only
    clip_end: float
    duration: float  # clip-relative upper bound == end - start (15.1)
    words: tuple[Any, ...] = ()  # rebased clip-relative Word_Timeline (15.2)
    options: Any = None  # this engine's resolved Engine_Options
    options_digest: str = ""  # Req 11.1
    seed: int = 0  # Req 12.2 — only randomness source
    workspace: Engine_Workspace | None = None  # worker.engines.artifacts
    capabilities: Capability_Report | None = None  # worker.engines.capabilities
    permissibility: bool = False  # ProcessingOptions.permissibility_mode
    deadline: float = math.inf  # time.monotonic() budget end (Req 8.6)
    time_budget_s: float = 0.0
    #: Absolute ffmpeg input index of this engine's **first** reserved input, so a
    #: COMPOSE-stage engine can write valid ``[N:v]`` / ``[N:a]`` filter labels
    #: against its own inputs (Req 1.5). The host reserves one contiguous block of
    #: :attr:`AV_Engine.max_inputs` indices per contributing engine, immediately
    #: after the primary clip (index 0), in registry ``(priority, engine_id)``
    #: order — so the value is known *before* :meth:`AV_Engine.run` executes.
    #: Meaningless (``0``) for an engine that contributes no inputs, i.e. whose
    #: ``max_inputs`` is ``0``: such an engine consumes no index space at all.
    first_input_index: int = 0
    #: Free-form host annotations, e.g. ``"fps_fallback:0.0"`` (Req 13.3, design
    #: P21) or ``"filler_seam:<seconds>"``. Deliberately **not** narrowed to an
    #: enum: engines and sibling specs append their own note kinds here.
    notes: tuple[str, ...] = ()
    deps: Mapping[str, Any] = field(default_factory=dict)  # injected fakes (Req 22.1)
    #: Read-only per-clip Clip_Metadata: values produced **upstream** of this stage
    #: run and supplied by the Pipeline at stage invocation (Req 15.8). A separate
    #: channel from :attr:`deps`, which stays the host's injected clock/logger/
    #: storage seam. Known keys today:
    #:
    #:   ``"hook_text"``  -> ``str``, the generated hook title for this clip
    #:   ``"clip_size"``  -> ``tuple[int, int]``, the target ``(width, height)``
    #:
    #: Unknown keys pass through untouched — no filtering, coercion, renaming or
    #: defaulting — so a later consumer needs no further contract change: an engine
    #: reads the keys it understands and treats a missing key as absent. Defaults to
    #: empty, so a context built without it is unchanged and the all-off path is
    #: inert. Appended at the **end** of the field list on purpose: the
    #: contract-surface pin (Req 23.6) asserts the exact field order, so a new field
    #: only ever goes last.
    clip_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _as_text(self.job_id))
        object.__setattr__(self, "clip_id", _as_text(self.clip_id))
        object.__setattr__(self, "engine_id", _as_text(self.engine_id))
        object.__setattr__(self, "words", _as_tuple(self.words))
        object.__setattr__(self, "notes", _as_str_tuple(self.notes))
        object.__setattr__(self, "options_digest", _as_text(self.options_digest))
        object.__setattr__(self, "permissibility", bool(self.permissibility))
        for name in ("clip_start", "clip_end", "duration", "time_budget_s"):
            object.__setattr__(self, name, coerce_float(getattr(self, name), 0.0))
        # ``deadline`` keeps ``math.inf`` (the documented "no budget" value), so it
        # is coerced separately from the finite-only timing fields above.
        deadline = self.deadline
        object.__setattr__(
            self,
            "deadline",
            float(deadline) if _is_number(deadline) else math.inf,
        )
        object.__setattr__(self, "seed", coerce_int(self.seed, 0))
        # An input index is a non-negative ffmpeg ``-i`` position; index 0 is the
        # primary clip, which no engine ever owns, so 0 doubles as "not reserved".
        object.__setattr__(self, "first_input_index", coerce_int(self.first_input_index, 0, lo=0))
        if not isinstance(self.deps, Mapping):
            object.__setattr__(self, "deps", {})
        # Clip_Metadata is normalised exactly like ``deps``: an unusable value
        # becomes the documented empty mapping, and a real mapping's keys and
        # values are left untouched (Req 15.8).
        if not isinstance(self.clip_metadata, Mapping):
            object.__setattr__(self, "clip_metadata", {})

    def rng(self) -> random.Random:
        """Return a seeded RNG; the ONLY permitted randomness source (Req 12.2).

        A fresh :class:`random.Random` seeded from :attr:`seed` — never the global
        ``random`` module — so repeated calls on an equal context yield equal
        sequences (Reqs 12.1-12.3).
        """
        return random.Random(self.seed)  # noqa: S311 - engine determinism is a documented requirement, not a weak secret

    def remaining(self, now: float | None = None) -> float:
        """Seconds left before ``deadline`` (pass to subprocess timeouts).

        Returns ``math.inf`` for the default "no deadline" context and never a
        negative number: an exhausted budget reports ``0.0``.
        """
        if not math.isfinite(self.deadline):
            return math.inf
        current = now if _is_number(now) else time.monotonic()
        if not math.isfinite(float(current)):
            return 0.0
        return max(0.0, self.deadline - float(current))


@dataclass(frozen=True)
class Engine_Result:
    """Immutable, serialisable outcome of one engine invocation (Reqs 1.2, 1.6)."""

    engine_id: str
    status: Engine_Status
    markers: tuple[str, ...] = ()  # namespaced by the host (3.3)
    artifacts: tuple[Engine_Artifact, ...] = ()
    contribution: Compose_Contribution | None = None
    plan: Mapping[str, Any] = field(default_factory=dict)  # serialisable planning output
    media: Path | None = None  # replacement clip media (AUDIO/GEOMETRY only)
    detail: str = ""
    elapsed_s: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "engine_id", _as_text(self.engine_id))
        object.__setattr__(self, "status", _coerce_status(self.status))
        object.__setattr__(self, "markers", _as_str_tuple(self.markers))
        object.__setattr__(
            self,
            "artifacts",
            tuple(
                item if isinstance(item, Engine_Artifact) else Engine_Artifact.from_dict(item)
                for item in _as_tuple(self.artifacts)
            ),
        )
        contribution = self.contribution
        if contribution is not None and not isinstance(contribution, Compose_Contribution):
            contribution = (
                Compose_Contribution.from_dict(contribution)
                if isinstance(contribution, Mapping)
                else None
            )
        object.__setattr__(self, "contribution", contribution)
        # The plan is normalised to a JSON-safe mapping in sorted key order, so
        # ``to_dict`` is always JSON-encodable and the round-trip is exact.
        plan = _json_safe(self.plan) if self.plan else {}
        object.__setattr__(self, "plan", plan if isinstance(plan, dict) else {})
        object.__setattr__(self, "media", _as_path(self.media))
        object.__setattr__(self, "detail", _as_text(self.detail))
        object.__setattr__(self, "elapsed_s", coerce_float(self.elapsed_s, 0.0, lo=0.0))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-encodable mapping (Reqs 1.6, 18.5)."""
        return {
            "engine_id": self.engine_id,
            "status": self.status.value,
            "markers": list(self.markers),
            "artifacts": [item.to_dict() for item in self.artifacts],
            "contribution": self.contribution.to_dict() if self.contribution else None,
            "plan": dict(self.plan),
            "media": str(self.media) if self.media else None,
            "detail": self.detail,
            "elapsed_s": float(self.elapsed_s),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Engine_Result:
        """Rebuild from :meth:`to_dict` output, tolerating missing/hostile fields."""
        if not isinstance(data, Mapping):
            return cls(engine_id="", status=Engine_Status.FAILED)
        contribution = data.get("contribution")
        return cls(
            engine_id=_as_text(data.get("engine_id", "")),
            status=_coerce_status(data.get("status")),
            markers=_as_str_tuple(data.get("markers", ())),
            artifacts=tuple(
                Engine_Artifact.from_dict(item) if isinstance(item, Mapping) else item
                for item in _as_tuple(data.get("artifacts", ()))
            ),
            contribution=(
                Compose_Contribution.from_dict(contribution)
                if isinstance(contribution, Mapping)
                else contribution
                if isinstance(contribution, Compose_Contribution)
                else None
            ),
            plan=data.get("plan") or {},
            media=_as_path(data.get("media")),
            detail=_as_text(data.get("detail", "")),
            elapsed_s=coerce_float(data.get("elapsed_s", 0.0), 0.0, lo=0.0),
        )

    # Convenience constructors used by engines and by the host's gating ladder.

    @classmethod
    def skipped(cls, engine_id: str) -> Engine_Result:
        """A no-op outcome that contributes no marker at all (Reqs 3.4, 4.x)."""
        return cls(engine_id=engine_id, status=Engine_Status.SKIPPED)

    @classmethod
    def degraded(cls, engine_id: str, detail: str, *, markers: Sequence[str] = ()) -> Engine_Result:
        """A partial outcome: the engine fell back but the clip is still usable (Req 7.1)."""
        return cls(
            engine_id=engine_id,
            status=Engine_Status.DEGRADED,
            markers=_as_str_tuple(markers),
            detail=detail,
        )

    @classmethod
    def failed(cls, engine_id: str, detail: str) -> Engine_Result:
        """A failed outcome: the engine produced nothing usable (Req 8.1)."""
        return cls(engine_id=engine_id, status=Engine_Status.FAILED, detail=detail)


def _coerce_status(value: Any) -> Engine_Status:
    """Return an :class:`Engine_Status` member, defaulting to ``FAILED`` (Req 1.6).

    ``failed`` is the safe default: an outcome whose status could not be read is
    never reported as a success.
    """
    if isinstance(value, Engine_Status):
        return value
    if isinstance(value, str):
        try:
            return Engine_Status(value.strip().lower())
        except ValueError:
            return Engine_Status.FAILED
    return Engine_Status.FAILED


def marker(engine_id: str, detail: str) -> str:
    """Return ``engine:<engine_id>:<detail>`` (Req 3.3)."""
    return f"{MARKER_PREFIX}:{_as_text(engine_id)}:{_as_text(detail)}"


def merge_markers(results: Sequence[Engine_Result]) -> list[str]:
    """Concatenate result markers in invocation order, de-duplicated (Reqs 3.2, 3.6).

    ``skipped`` results contribute nothing (Req 3.4). The first occurrence of a
    marker fixes its position, so the merged list preserves the Engine_Registry
    invocation order and holds each marker at most once (Req 3.6).
    """
    merged: list[str] = []
    seen: set[str] = set()
    for result in _as_tuple(results):
        status = _coerce_status(getattr(result, "status", None))
        if status is Engine_Status.SKIPPED:
            continue
        for entry in _as_str_tuple(getattr(result, "markers", ())):
            if entry in seen:
                continue
            seen.add(entry)
            merged.append(entry)
    return merged


# ---------------------------------------------------------------------------
# Options layer: protocol, coercion, dump, digest, seed (Reqs 10, 11, 12.4)
# ---------------------------------------------------------------------------


@runtime_checkable
class Engine_Options(Protocol):
    """Per-engine options record: a dataclass of JSON-serialisable values (Req 10.1)."""

    @classmethod
    def parse(cls, data: Mapping[str, Any] | None) -> Engine_Options:
        """Total parser: never raises, ignores unknown keys, defaults per field (10.2/10.4/10.5)."""
        ...

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe mapping (Req 10.2)."""
        ...


# Coercion helpers mirroring the ``ProcessingOptions.from_dict`` conventions
# (``worker.models._as_bool`` semantics, enum-like known-value sets) — Req 10.7.
#
# Every helper is **total**: for any input whatsoever — ``None``, ``NaN``, ``inf``,
# huge ints, nested containers, lone-surrogate strings, objects whose ``__str__``
# raises — it returns a value of the declared type instead of raising (Req 10.4).


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce a form/JSON value to ``bool`` (``worker.models._as_bool`` semantics).

    ``bool`` passes through; a real number reads as its truthiness (so ``0``/``0.0``
    are ``False``, and ``NaN`` — being truthy in Python — is ``True``); a string is
    matched against the documented truthy (:data:`_TRUE_STRINGS`) and falsy
    (:data:`_FALSE_STRINGS`) spellings case-insensitively. Anything else —
    ``None``, a list, a mapping, an arbitrary object, an unrecognised spelling —
    is *malformed* and yields ``default`` (Req 10.4).
    """
    fallback = bool(default)
    if isinstance(value, bool):
        return value
    if _is_number(value):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
        return fallback
    return fallback


def coerce_int(value: Any, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """Coerce to ``int``, clamped to ``[lo, hi]``, mirroring the ``hashtag_count`` rule.

    Accepted: an ``int``; a finite ``float`` (truncated toward zero, as ``int()``
    does); a numeric string (``"5"``, and ``"5.5"`` via a float fallback).
    Rejected — and therefore replaced by ``default`` — are ``bool`` (see
    :func:`_is_number`), ``None``, ``NaN``/``inf``, non-numeric text, and every
    container or object. The clamp is applied last, so the result always sits in
    range even when ``default`` does not.
    """
    number: int | None = None
    if _is_number(value):
        if isinstance(value, int):
            number = int(value)
        elif math.isfinite(value):
            number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = int(text)
        except (TypeError, ValueError):
            try:
                candidate = float(text)
            except (TypeError, ValueError):
                number = None
            else:
                number = int(candidate) if math.isfinite(candidate) else None
    if number is None:
        number = int(default) if _is_number(default) else 0
    if lo is not None and _is_number(lo):
        number = max(int(lo), number)
    if hi is not None and _is_number(hi):
        number = min(int(hi), number)
    return int(number)


def coerce_float(
    value: Any, default: float, lo: float | None = None, hi: float | None = None
) -> float:
    """Coerce to a **finite** ``float``, clamped to ``[lo, hi]`` (``music_volume`` rule).

    Accepted: a finite ``int``/``float``; a numeric string. Rejected — replaced by
    ``default`` — are ``bool``, ``None``, ``NaN``, ``±inf``, non-numeric text, and
    every container or object; a non-finite ``default`` collapses to ``0.0`` so the
    result is always a usable timing/gain value. The clamp is applied last.
    """
    number: float | None = None
    if _is_number(value):
        candidate = float(value)
        if math.isfinite(candidate):
            number = candidate
    elif isinstance(value, str):
        try:
            candidate = float(value.strip())
        except (TypeError, ValueError):
            number = None
        else:
            if math.isfinite(candidate):
                number = candidate
    if number is None:
        number = float(default) if _is_number(default) and math.isfinite(float(default)) else 0.0
    if lo is not None and _is_number(lo) and math.isfinite(float(lo)):
        number = max(float(lo), number)
    if hi is not None and _is_number(hi) and math.isfinite(float(hi)):
        number = min(float(hi), number)
    return float(number)


def coerce_choice(value: Any, known: Sequence[str], default: str) -> str:
    """Validate an enum-like string against ``known``, else substitute ``default`` (Req 10.7).

    Exactly the ``ProcessingOptions.from_dict`` rule — ``v if v in known else
    default`` — so a known value is returned unchanged (never re-spelled) and
    everything else, including ``None``, wrong types and unknown spellings, yields
    ``default``.
    """
    try:
        members = tuple(known)
    except Exception:  # pragma: no cover - hostile ``known``
        members = ()
    try:
        if value in members:
            return value if isinstance(value, str) else _as_text(value)
    except Exception:  # pragma: no cover - unhashable/uncomparable value
        pass
    return default if isinstance(default, str) else _as_text(default)


def coerce_str(value: Any, default: str = "", max_len: int = 512) -> str:
    """Coerce to a ``str`` of at most ``max_len`` characters.

    A ``str`` passes through truncated; a real number is rendered (``5`` ->
    ``"5"``), including ``NaN``/``inf`` whose ``str`` form is stable. ``None``,
    ``bool``, containers and arbitrary objects are malformed for a text field and
    yield ``default`` (also truncated).
    """
    limit = max_len if isinstance(max_len, int) and not isinstance(max_len, bool) else 512
    limit = max(0, limit)
    if isinstance(value, str):
        return value[:limit]
    if _is_number(value):
        return _as_text(value)[:limit]
    fallback = default if isinstance(default, str) else _as_text(default)
    return fallback[:limit]


def dump_options(options: Any) -> dict[str, Any]:
    """``dataclasses.asdict`` with private keys dropped and mappings emitted in
    sorted key order (Reqs 10.2, 12.6).

    Total and idempotent: dataclasses (including nested ones) become mappings, a
    plain mapping is normalised in place, an object exposing ``to_dict`` is asked
    for it, ``None`` yields ``{}``, and any other value is wrapped as
    ``{"value": ...}``. Every leaf is JSON-encodable, so ``json.dumps`` on the
    result never raises (Req 10.1).
    """
    if options is None:
        return {}

    record: Any
    if dataclasses.is_dataclass(options) and not isinstance(options, type):
        try:
            record = dataclasses.asdict(options)
        except Exception:  # pragma: no cover - exotic dataclass
            record = options
    elif isinstance(options, Mapping):
        record = options
    else:
        to_dict = getattr(options, "to_dict", None)
        if callable(to_dict):
            try:
                record = to_dict()
            except Exception:  # pragma: no cover - hostile ``to_dict``
                record = options
        else:
            record = options

    dumped = _json_safe(record)
    if isinstance(dumped, dict):
        return dumped
    return {"value": dumped}


def options_digest(options: Any) -> str:
    """Return a stable 16-char lowercase hex digest of resolved options (Req 11).

    ``sha256(json.dumps(dump_options(options), sort_keys=True, separators=(",", ":"),
    ensure_ascii=True, default=str)).hexdigest()[:DIGEST_LENGTH]`` — equal for equal
    values (11.2), insensitive to key insertion order (11.3), different for different
    values (11.4), fixed-length lowercase hex and process-stable (11.5).

    Process stability comes from using only ``sha256`` over a canonical JSON
    rendering: no ``hash()``, no ``id()``, no set iteration order, and therefore no
    ``PYTHONHASHSEED`` dependence. ``sort_keys`` plus :func:`dump_options`'s own
    sorted-key normalisation makes key insertion order irrelevant, and
    ``ensure_ascii=True`` keeps the payload pure ASCII (so even lone surrogates
    hash deterministically). ``NaN``/``inf`` are rendered by ``json.dumps`` as the
    literals ``NaN``/``Infinity`` — non-standard JSON but a fixed, deterministic
    spelling, which is all the digest needs.
    """
    payload = json.dumps(
        dump_options(options),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return _sha256_hex(payload)[:DIGEST_LENGTH]


def derive_seed(source_identity: str, digest: str) -> int:
    """Reproducible per-invocation seed (Req 12.4).

    ``int(sha256(f"{source_identity}|{digest}").hexdigest()[:16], 16)`` — never stored,
    always recomputable, and stable across processes for the same inputs.
    """
    payload = f"{_as_text(source_identity)}|{_as_text(digest)}"
    return int(_sha256_hex(payload)[:16], 16)


# ---------------------------------------------------------------------------
# The abstract base (Reqs 1.1, 1.2, 4.1, 9.1, 9.2, 12.5, 19.1, 21.1, 22.1)
# ---------------------------------------------------------------------------


class AV_Engine(ABC):
    """Base class every advanced AV engine implements (Req 1.1).

    Class-level declarations are the engine's contract with the host; instance
    collaborators are dependency-injected through ``__init__`` (Req 22.1), which
    is why this base defines no required constructor arguments: a subclass takes
    its probe, backend, clock or storage as keyword arguments with defaults and
    tests pass fakes.

    A subclass that leaves any of :meth:`resolve_options`, :meth:`plan` or
    :meth:`run` unimplemented cannot be instantiated — Python raises
    ``TypeError`` — so the contract is enforced at construction time.
    """

    engine_id: ClassVar[str] = ""  # snake_case, stable
    stage: ClassVar[Engine_Stage] = Engine_Stage.POST
    priority: ClassVar[int] = 100  # ordering key (Req 2.5)
    required_capabilities: ClassVar[tuple[str, ...]] = ()  # Req 7.1
    optional_capabilities: ClassVar[tuple[str, ...]] = ()  # Req 7.2
    requires_network: ClassVar[bool] = False  # Req 21.1
    requires_model_download: ClassVar[bool] = False  # Req 21.1
    time_budget_s: ClassVar[float] = 30.0  # Req 19.1
    max_media_passes: ClassVar[int] = 1  # Req 19.1
    #: How many ffmpeg ``-i`` inputs this engine may contribute to the ONE
    #: compositor pass (Req 1.5). Declared alongside ``max_media_passes`` because
    #: both bound the cost an engine may add to a clip: ``max_media_passes`` bounds
    #: the *passes*, ``max_inputs`` the *inputs*. The host reserves a contiguous
    #: block of exactly this many indices and publishes its start as
    #: ``Engine_Context.first_input_index``; ``0`` (the default) means the engine
    #: contributes no input and therefore consumes no index space.
    max_inputs: ClassVar[int] = 0
    produces_media: ClassVar[bool] = False  # may return Result.media

    @classmethod
    def flag_field(cls) -> str:
        """ProcessingOptions attribute holding this engine's Feature_Flag.

        Defaults to ``f"{cls.engine_id}{FLAG_SUFFIX}"`` (Reqs 9.1, 9.2). Override
        only when an engine has to reuse an existing v0.8.0 flag name.
        """
        return f"{_as_text(cls.engine_id)}{FLAG_SUFFIX}"

    def is_enabled(self, options: Any) -> bool:
        """True when the Feature_Flag on the *resolved* options is set (Reqs 4.1, 4.4).

        Reads :meth:`flag_field` off ``options`` (attribute first, then mapping
        key) and coerces it with :func:`coerce_bool`. A missing flag reads as
        disabled, so every engine is OFF until explicitly enabled (Req 9.2), and
        the check never raises.
        """
        flag = self.flag_field()
        if options is None:
            return False
        value: Any = getattr(options, flag, None)
        if value is None and isinstance(options, Mapping):
            try:
                value = options.get(flag)
            except Exception:  # pragma: no cover - hostile mapping
                value = None
        if value is None:
            return False
        return coerce_bool(value, False)

    @abstractmethod
    def resolve_options(self, options: Any) -> Any:
        """Project ProcessingOptions onto this engine's Engine_Options (Reqs 1.1, 10.6).

        Pure and idempotent; applies the documented safe values under
        ``options.permissibility_mode`` (Req 9.5). Must not mutate ``options`` (9.6).
        """
        raise NotImplementedError

    @abstractmethod
    def plan(self, ctx: Engine_Context) -> Mapping[str, Any]:
        """Pure planning step: no ffmpeg, no network, no model download (Req 12.5).

        Deterministic for equal inputs and equal ``ctx.seed`` (Reqs 12.1-12.3).
        Returns a JSON-serialisable mapping (segment lists, cue lists, parameters).
        """
        raise NotImplementedError

    @abstractmethod
    def run(self, ctx: Engine_Context) -> Engine_Result:
        """Execute the engine. Exactly one argument, exactly one result (Req 1.2).

        Treats ``ctx`` as read-only (Req 1.3); writes only inside
        ``ctx.workspace`` (Req 16.4); returns compose work as a
        ``Compose_Contribution`` rather than invoking ffmpeg (Req 1.5).
        """
        raise NotImplementedError
