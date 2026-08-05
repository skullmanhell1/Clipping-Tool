"""Engine discovery: registration, per-stage lookup, deterministic ordering (Req 2).

One concern lives here, and it is deliberately small: an ``Engine_Id -> AV_Engine``
mapping that yields engines in an order which depends **only** on the engines
themselves, never on the order they happened to be registered in (Req 2.5).

* :class:`Engine_Record` — the frozen ``(engine, engine_id, stage, priority)``
  registration record, whose :attr:`~Engine_Record.sort_key` is the
  ``(priority, engine_id)`` total order every listing operation uses.
* :class:`Engine_Registry` — the mapping itself: :meth:`~Engine_Registry.register`,
  :meth:`~Engine_Registry.get` / :meth:`~Engine_Registry.find`,
  :meth:`~Engine_Registry.for_stage`, :meth:`~Engine_Registry.all`,
  :meth:`~Engine_Registry.ids`, :meth:`~Engine_Registry.records`,
  :meth:`~Engine_Registry.reset`, ``len()`` and ``in``.
* :class:`Engine_Registration_Error` — raised when an Engine_Id is registered twice,
  naming the conflict and leaving the registry untouched (Req 2.3).
* the module-level default registry with :func:`get_registry`, :func:`register` and
  :func:`reset_registry`.

Instances are **fully independent** (Req 22.2): a test can build its own
``Engine_Registry()``, register into it, and neither another instance nor the
module-level default sees a thing.

Import-safe with zero optional heavy dependencies present (Req 1.4): module scope
imports only the standard library plus :mod:`worker.engines.base`. Nothing here
touches ffmpeg, OpenCV, torch, the network, the clock, or the filesystem.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from worker.engines.base import AV_Engine, Engine_Stage

__all__ = [
    "Engine_Registration_Error",
    "Engine_Record",
    "Engine_Registry",
    "get_registry",
    "register",
    "reset_registry",
]

#: Priority used when neither the caller nor the engine declares a usable one.
#: Identical to ``AV_Engine.priority``'s own default, so an engine that never
#: overrides the ClassVar keeps its documented ordering weight.
DEFAULT_PRIORITY = 100


class Engine_Registration_Error(ValueError):
    """Raised when an Engine_Id is registered twice (Req 2.3).

    A ``ValueError`` subclass, so callers that only care that the registration was
    rejected can keep catching ``ValueError``. The message always contains the
    conflicting Engine_Id.
    """


# ---------------------------------------------------------------------------
# Internal, total helpers
# ---------------------------------------------------------------------------


def _as_text(value: Any) -> str:
    """Return ``value`` as a ``str``, never raising."""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # pragma: no cover - __str__ that raises
        return repr(type(value))


def _coerce_priority(value: Any, default: int = DEFAULT_PRIORITY) -> int:
    """Return ``value`` as an ``int`` priority, falling back to ``default``.

    ``bool`` is rejected the way :mod:`worker.engines.base` rejects it in numeric
    fields (a flag landing in a priority slot is type confusion), as are ``None``,
    non-finite floats, text and containers. A finite float truncates toward zero.
    """
    if isinstance(value, bool) or value is None:
        return int(default)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        # ``int()`` on inf/nan raises, so screen them out first.
        if value == value and value not in (float("inf"), float("-inf")):
            return int(value)
        return int(default)
    return int(default)


def _coerce_stage(value: Any) -> Engine_Stage | None:
    """Return ``value`` as an :class:`Engine_Stage`, or ``None`` when unrecognised.

    ``None`` means "no such stage": a *registration* substitutes
    ``Engine_Stage.POST`` (``AV_Engine.stage``'s own default) while a *lookup*
    returns an empty list, because no engine can declare a stage that does not
    exist.
    """
    if isinstance(value, Engine_Stage):
        return value
    if isinstance(value, str):
        try:
            return Engine_Stage(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# The registration record (Req 2.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Engine_Record:
    """One registration: the engine plus the identity/ordering it was filed under.

    Frozen, so a caller holding a record cannot re-file an engine behind the
    registry's back. The declared ``engine_id``/``stage``/``priority`` are captured
    at registration time, which is what makes the ordering stable even if an engine
    object is mutated afterwards.
    """

    engine: AV_Engine
    engine_id: str
    stage: Engine_Stage
    priority: int

    @property
    def sort_key(self) -> tuple[int, str]:
        """``(priority, engine_id)`` — the total order of every listing (Req 2.5).

        Total because ``engine_id`` is unique within a registry: ties on
        ``priority`` are broken by the id, so two different registration orders can
        never produce two different sequences.
        """
        return (self.priority, self.engine_id)


# ---------------------------------------------------------------------------
# The registry (Reqs 2.1-2.7, 22.2)
# ---------------------------------------------------------------------------


class Engine_Registry:
    """Engine_Id -> AV_Engine, yielded in a registration-order-independent order.

    Construct one per test to stay isolated from the process-wide default
    (Req 22.2); the constructor takes no arguments and no global state.
    """

    def __init__(self) -> None:
        self._records: dict[str, Engine_Record] = {}
        # Registration happens at import time in production and inside tests
        # otherwise; the lock keeps the duplicate check and the insert atomic so a
        # concurrent registration cannot slip between them.
        self._lock = threading.RLock()

    # --- registration -----------------------------------------------------

    def register(self, engine: AV_Engine, *, priority: int | None = None) -> AV_Engine:
        """Register ``engine`` under its declared Engine_Id (Req 2.1).

        ``engine_id``, ``stage`` and ``priority`` are read **off the object**, so a
        real engine's ClassVars and a test double's per-instance attributes both
        work (``tests.fakes.FakeEngine`` sets all three on ``self``). An explicit
        ``priority=`` overrides the engine's own declaration.

        Returns the engine, so the call can wrap a construction expression.

        Raises:
            Engine_Registration_Error: naming the conflicting Engine_Id (Req 2.3).
                The registry is left unchanged — the duplicate check happens before
                any mutation, so a rejected registration cannot leave a partial
                entry behind.
        """
        engine_id = _as_text(getattr(engine, "engine_id", ""))
        stage = _coerce_stage(getattr(engine, "stage", None)) or Engine_Stage.POST
        weight = _coerce_priority(
            getattr(engine, "priority", DEFAULT_PRIORITY) if priority is None else priority
        )
        record = Engine_Record(engine=engine, engine_id=engine_id, stage=stage, priority=weight)
        with self._lock:
            existing = self._records.get(engine_id)
            if existing is not None:
                raise Engine_Registration_Error(
                    f"Engine_Id {engine_id!r} is already registered "
                    f"(existing engine: {type(existing.engine).__name__}); "
                    f"Engine_Ids must be unique"
                )
            self._records[engine_id] = record
        return engine

    # --- lookup -----------------------------------------------------------

    def get(self, engine_id: str) -> AV_Engine:
        """Return the engine registered for ``engine_id`` (Req 2.2).

        Raises:
            KeyError: when nothing is registered under that id.
        """
        key = _as_text(engine_id)
        record = self._records.get(key)
        if record is None:
            raise KeyError(f"no engine registered for Engine_Id {key!r}")
        return record.engine

    def find(self, engine_id: str) -> AV_Engine | None:
        """Non-raising variant of :meth:`get`: ``None`` when the id is unknown."""
        record = self._records.get(_as_text(engine_id))
        return record.engine if record is not None else None

    def for_stage(self, stage: Engine_Stage) -> list[AV_Engine]:
        """Engines declaring ``stage``, ordered by ``(priority, engine_id)``.

        Returns ``[]`` for an empty registry, an unused stage, or a value that is no
        stage at all (Reqs 2.4, 2.6). The sequence depends only on the registered
        engines, never on the order they were registered in (Req 2.5).
        """
        wanted = _coerce_stage(stage)
        if wanted is None:
            return []
        return [record.engine for record in self._sorted_records() if record.stage is wanted]

    def all(self) -> list[AV_Engine]:
        """Every registered engine in the same deterministic order (Req 2.5)."""
        return [record.engine for record in self._sorted_records()]

    def ids(self) -> list[str]:
        """Every registered Engine_Id, sorted alphabetically."""
        return sorted(self._records)

    def records(self) -> list[Engine_Record]:
        """Every :class:`Engine_Record`, in ``(priority, engine_id)`` order."""
        return self._sorted_records()

    def stage_of(self, engine_id: str) -> Engine_Stage | None:
        """The Engine_Stage ``engine_id`` was registered under, or ``None``."""
        record = self._records.get(_as_text(engine_id))
        return record.stage if record is not None else None

    # --- lifecycle --------------------------------------------------------

    def reset(self) -> None:
        """Clear every registration (Reqs 2.7, 22.2).

        Affects this instance only: sibling registries and the module-level default
        keep their contents.
        """
        with self._lock:
            self._records.clear()

    # --- dunders ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, engine_id: object) -> bool:
        return _as_text(engine_id) in self._records

    def __iter__(self):
        """Iterate the engines in deterministic order (same as :meth:`all`)."""
        return iter(self.all())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Engine_Registry({self.ids()!r})"

    # --- internals --------------------------------------------------------

    def _sorted_records(self) -> list[Engine_Record]:
        """Snapshot of the records in ``(priority, engine_id)`` order."""
        return sorted(self._records.values(), key=lambda record: record.sort_key)


# ---------------------------------------------------------------------------
# Module-level default registry (Req 22.2)
# ---------------------------------------------------------------------------

_DEFAULT = Engine_Registry()


def get_registry() -> Engine_Registry:
    """Process-wide default registry (tests may build isolated ones — Req 22.2)."""
    return _DEFAULT


def register(engine: AV_Engine, *, priority: int | None = None) -> AV_Engine:
    """Register ``engine`` in the module-level default registry (Req 2.1)."""
    return _DEFAULT.register(engine, priority=priority)


def reset_registry() -> None:
    """Clear the module-level default registry (Reqs 2.7, 22.2)."""
    _DEFAULT.reset()
