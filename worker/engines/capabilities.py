"""Capability probing and per-process caching for AV engines.

Two layers live here:

* the probe layer — :class:`Capability_Kind`, :func:`parse_capability_id`,
  the frozen :class:`Capability_Status`, the injectable :data:`Prober` alias,
  the :data:`MODEL_LOCATORS` registry and :func:`default_prober`
  (Reqs 5.1-5.7, 21.5);
* the cache layer — :class:`Capability_Report` plus the process-wide
  :func:`get_report` / :func:`reset_report` (Reqs 6.1-6.5, 20.2).

**Import safety (Req 1.4).** Module scope imports the standard library only.
The three collaborators this module needs — ``config.settings`` (which pulls in
``pydantic-settings``), ``worker.captions`` and ``worker.llm_client`` (which pull
in the caption/LLM stacks) — are imported **lazily inside the individual probe
functions**. That resolves the tension between Req 5.4/5.5 ("use
``settings.ffmpeg_binary``", "reuse the existing availability helpers") and
Req 1.4 ("imports successfully with every optional heavy dependency absent"):
importing ``worker.engines.capabilities`` costs nothing and leaks nothing, while
actually *probing* pays for the collaborator it needs — and pays for it inside
the total error wrapper, so even a collaborator that fails to import merely
reports the capability unavailable (Req 5.3).

**Totality (Req 5.3).** :func:`default_prober` accepts *any* string — malformed,
empty, unicode, NUL-bearing, arbitrarily long — and never raises: every
underlying error becomes ``available=False`` with the exception class name at the
front of ``detail``.

**Offline (Req 5.6).** No probe opens a socket. The only external process is the
local ``<ffmpeg_binary> -filters`` invocation used for filter probes.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import shutil
import string
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "Capability_Kind",
    "LLM_CAPABILITY",
    "CAPABILITY_SEPARATOR",
    "MAX_DETAIL_LENGTH",
    "FFMPEG_FILTER_TIMEOUT",
    "parse_capability_id",
    "Capability_Status",
    "Prober",
    "MODEL_LOCATORS",
    "default_prober",
    "Capability_Report",
    "get_report",
    "reset_report",
]


class Capability_Kind(str, Enum):
    """Probeable capability kinds (Req 5.1); mirrors the str-Enum style of ``Engine_Stage``."""

    PYTHON_PKG = "python_pkg"  # python_pkg:demucs      -> importlib.util.find_spec
    BINARY = "binary"  # binary:ffprobe         -> shutil.which
    FFMPEG_FILTER = "ffmpeg_filter"  # ffmpeg_filter:atempo   -> settings.ffmpeg_binary -filters
    FONT = "font"  # font:Impact            -> captions.font_available
    PROVIDER_KEY = "provider_key"  # provider_key:broll     -> settings.<name>_api_key
    MODEL = "model"  # model:htdemucs         -> registered locator
    LLM = "llm"  # bare "llm"             -> llm_client.llm_available


LLM_CAPABILITY = "llm"
"""The one Capability_Id that carries no name (Req 5.5)."""

CAPABILITY_SEPARATOR = ":"
"""Separator between kind and name in a Capability_Id."""

MAX_DETAIL_LENGTH = 160
"""Detail strings stay short and single-line (Req 5.2)."""

FFMPEG_FILTER_TIMEOUT = 20.0
"""Wall-clock ceiling for the local ``ffmpeg -filters`` listing."""

FFMPEG_FILTER_FLAG_WIDTHS = frozenset({2, 3})
"""Widths of the flags column in ``ffmpeg -filters`` output.

Three through ffmpeg 7 (``T..``, ``TSC`` — timeline, slice threading, command), and two
on builds that dropped the command flag from the listing (``.S``, ``TS``), which is what
ffmpeg does from 8.x. A *set* of accepted widths rather than one number for the same
reason :data:`FFMPEG_FILTER_FLAG_CHARS` is the whole uppercase alphabet: this column is
not a stable interface, and the failure mode when an assumption about it breaks is silent.

That is not hypothetical. The alphabet was generalised for exactly this reason while the
width was left hardcoded at 3, and the width is what changed: against a build printing two
flags, every row was rejected, ``_ffmpeg_filter_names`` returned an empty set, and so every
``ffmpeg_filter:`` probe reported unavailable. Nothing raised — each engine simply
degraded, which is the same "one fact stated in two places" shape as the defects this
module was written to catch.
"""

FFMPEG_FILTER_FLAG_CHARS = frozenset(string.ascii_uppercase + ".")
"""Alphabet of the flags column: a flag letter, or ``.`` where the flag is unset.

Deliberately every uppercase letter rather than just today's ``T``/``S``/``C``, so a
flag letter added by a future ffmpeg cannot silently make filters unparseable — the
failure mode this alphabet exists to prevent.
"""

FFMPEG_FILTER_PAD_SEPARATOR = "->"
"""Marker inside the pad-spec column (``A->A``), used to identify real filter rows."""

_KIND_VALUES = frozenset(kind.value for kind in Capability_Kind)


# ---------------------------------------------------------------------------
# Internal, total helpers
# ---------------------------------------------------------------------------


def _as_text(value: Any) -> str:
    """Return ``value`` as a ``str``, never raising (same helper as ``base.py``)."""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # pragma: no cover - __str__ that raises
        return repr(type(value))


def _short(text: Any) -> str:
    """Collapse ``text`` to one short, printable line (Req 5.2)."""
    raw = _as_text(text)
    cleaned = " ".join(raw.replace("\x00", " ").split())
    if len(cleaned) > MAX_DETAIL_LENGTH:
        return cleaned[: MAX_DETAIL_LENGTH - 1] + "\u2026"
    return cleaned


def _error_detail(exc: BaseException) -> str:
    """``"<ExceptionClass>: <message>"`` — the class name always survives truncation.

    Req 5.3: an underlying error is reported, not raised, and the caller can see
    *what* failed.
    """
    name = type(exc).__name__
    message = ""
    try:
        message = _as_text(exc)
    except Exception:  # pragma: no cover - hostile __str__
        message = ""
    if not message:
        return _short(name)
    return _short(f"{name}: {message}")


def _unavailable(capability_id: str, detail: str) -> Capability_Status:
    return Capability_Status(capability_id=capability_id, available=False, detail=detail)


def _available(capability_id: str, detail: str) -> Capability_Status:
    return Capability_Status(capability_id=capability_id, available=True, detail=detail)


def parse_capability_id(capability_id: str) -> tuple[str, str]:
    """Split ``"<kind>:<name>"``; ``"llm"`` parses as ``("llm", "")``.

    The kind is matched case-insensitively after stripping surrounding
    whitespace; the name is returned with surrounding whitespace stripped but is
    otherwise untouched (font families and model names are case-sensitive).
    Unknown kinds return ``("", capability_id)`` and probe as unavailable
    (Req 5.2). Never raises for any input.
    """
    raw = _as_text(capability_id)
    head, separator, tail = raw.partition(CAPABILITY_SEPARATOR)
    kind = head.strip().lower()
    if not separator:
        # Bare token: only ``"llm"`` is a valid nameless capability.
        if kind == LLM_CAPABILITY:
            return (Capability_Kind.LLM.value, "")
        return ("", raw)
    if kind in _KIND_VALUES:
        return (kind, tail.strip())
    return ("", raw)


@dataclass(frozen=True)
class Capability_Status:
    """One probe outcome: availability plus a short human-readable detail (Req 5.2)."""

    capability_id: str
    available: bool
    detail: str = ""

    def __post_init__(self) -> None:
        # Normalise by construction so every status is shaped even when built
        # from hostile input (Req 5.2): a real ``bool`` and a short ``str``.
        object.__setattr__(self, "capability_id", _as_text(self.capability_id))
        object.__setattr__(self, "available", bool(self.available))
        object.__setattr__(self, "detail", _short(self.detail))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-encodable mapping (Reqs 6.4, 20.2)."""
        return {
            "capability_id": self.capability_id,
            "available": bool(self.available),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Capability_Status:
        """Rebuild from :meth:`to_dict` output, tolerating missing/hostile fields."""
        if not isinstance(data, Mapping):
            return cls(capability_id="", available=False, detail="")
        return cls(
            capability_id=_as_text(data.get("capability_id", "")),
            available=bool(data.get("available", False)),
            detail=_short(data.get("detail", "")),
        )


Prober = Callable[[str], Capability_Status]  # injectable (Req 5.7)

#: ``model:<name>`` locators registered by engines; empty by default so an absent
#: model with downloading disabled reports unavailable (Req 21.5). A locator is a
#: zero-argument callable returning the local path of the model, or ``None`` when
#: it is not present on disk. Locators must never download (Req 5.6).
MODEL_LOCATORS: dict[str, Callable[[], Path | None]] = {}


# ---------------------------------------------------------------------------
# Per-kind probes — each one lazily imports its collaborator (Req 1.4)
# ---------------------------------------------------------------------------


def _probe_python_pkg(capability_id: str, name: str) -> Capability_Status:
    """``python_pkg:<module>`` -> :func:`importlib.util.find_spec`.

    Note: ``find_spec`` on a dotted name imports the *parent* packages, so
    engines should declare top-level modules (``python_pkg:demucs``) to keep the
    probe cheap.
    """
    if not name:
        return _unavailable(capability_id, "no module name")
    spec = importlib.util.find_spec(name)
    if spec is None:
        return _unavailable(capability_id, f"module not importable: {name}")
    return _available(capability_id, f"module found: {name}")


def _probe_binary(capability_id: str, name: str) -> Capability_Status:
    """``binary:<exe>`` -> :func:`shutil.which` on the configured PATH."""
    if not name:
        return _unavailable(capability_id, "no binary name")
    resolved = shutil.which(name)
    if not resolved:
        return _unavailable(capability_id, f"binary not on PATH: {name}")
    return _available(capability_id, f"binary found: {resolved}")


def _ffmpeg_filter_names(binary: str = "") -> set[str]:
    """Filter names reported by ``<binary> -filters`` (Req 5.4).

    ``config`` is imported here, not at module scope, so this module stays
    import-safe without ``pydantic-settings`` (Req 1.4). The configured binary is
    always used — never a hard-coded ``"ffmpeg"``.

    ``binary`` overrides ``settings.ffmpeg_binary`` for callers that must ask a
    *different* build what it can do. The fidelity gate needs that: it measures VMAF
    with its own binary, because the distribution ffmpeg this project otherwise runs on
    is not built with ``libvmaf``. It is a parameter rather than a second copy of the
    loop below for the reason that loop documents — a hand-rolled listing parser once
    hid 124 of 486 filters, and two parsers are two chances to do it again.
    """
    from config import settings  # lazy (Req 1.4)

    binary = _as_text(binary or getattr(settings, "ffmpeg_binary", "")).strip()
    if not binary:
        raise ValueError("settings.ffmpeg_binary is not configured")
    proc = subprocess.run(
        [binary, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        timeout=FFMPEG_FILTER_TIMEOUT,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    listing = _as_text(proc.stdout or "") + "\n" + _as_text(proc.stderr or "")
    names: set[str] = set()
    for line in listing.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        # ffmpeg prints "<flags> <name> <pads> <description>", e.g.
        #     "T.. aeval     A->A  Filter audio signal ..."
        #     "TSC highpass  A->A  Apply a high-pass filter ..."
        # The flags column is exactly three characters drawn from
        # :data:`FFMPEG_FILTER_FLAG_CHARS` (a '.' marks an unset flag). It must be
        # recognised by that *alphabet* and not by "contains a non-alphanumeric
        # character": a filter with every flag set prints a dot-free group like
        # "TSC", so an ``isalnum()`` test misreads the flags as the filter name and
        # loses the real one. That silently hid 124 of 486 filters on ffmpeg 7.0
        # (``highpass``/``lowpass``/``bass``/``equalizer``/... all TSC), which made
        # every engine requiring one of them permanently unavailable.
        #
        # The pad-spec column ("A->A", "N->A", "|->V") is what distinguishes a real
        # filter row from prose such as the "Filters:" banner or the flag legend, so
        # rows are only accepted when it is present.
        if (
            len(parts) >= 3
            and len(parts[0]) in FFMPEG_FILTER_FLAG_WIDTHS
            and set(parts[0]) <= FFMPEG_FILTER_FLAG_CHARS
            and FFMPEG_FILTER_PAD_SEPARATOR in parts[2]
        ):
            names.add(parts[1])
    return names


def ffmpeg_filter_available(name: str, *, binary: str = "") -> bool:
    """Whether ``binary`` (default: the configured ffmpeg) lists the filter ``name``.

    The listing parser behind the ``ffmpeg_filter:`` capability id, exposed for the one
    caller that has to ask a build other than ``settings.ffmpeg_binary``: the fidelity
    gate's VMAF binary.

    Returns a bool rather than a :class:`Capability_Status` deliberately. The capability
    report describes *the configured build* — what this project will actually render with —
    and a second binary's filters do not belong in it. Recording "libvmaf available" there
    would be read by every other caller as "the primary ffmpeg can do this", which is the
    kind of one-fact-in-two-places drift the report exists to prevent.
    """
    if not name:
        return False
    return name in _ffmpeg_filter_names(binary)


def _probe_ffmpeg_filter(capability_id: str, name: str) -> Capability_Status:
    """``ffmpeg_filter:<filter>`` -> the configured ffmpeg's own filter listing."""
    if not name:
        return _unavailable(capability_id, "no filter name")
    names = _ffmpeg_filter_names()
    if not names:
        return _unavailable(capability_id, "ffmpeg reported no filters")
    if name in names:
        return _available(capability_id, f"filter available: {name}")
    return _unavailable(capability_id, f"filter not built in: {name}")


def _probe_font(capability_id: str, name: str) -> Capability_Status:
    """``font:<family>`` -> ``worker.captions.font_available`` (Req 5.5)."""
    if not name:
        return _unavailable(capability_id, "no font name")
    from worker import captions  # lazy (Req 1.4)

    if bool(captions.font_available(name)):
        return _available(capability_id, f"font available: {name}")
    return _unavailable(capability_id, f"font not installed: {name}")


def _probe_provider_key(capability_id: str, name: str) -> Capability_Status:
    """``provider_key:<provider>`` -> ``settings.<provider>_api_key``.

    ``<provider>_provider_api_key`` is accepted as a documented second spelling
    so the repo's existing ``settings.broll_provider_api_key`` is reachable as
    ``provider_key:broll``.
    """
    if not name:
        return _unavailable(capability_id, "no provider name")
    from config import settings  # lazy (Req 1.4)

    slug = name.strip().lower().replace("-", "_").replace(" ", "_")
    if not slug:
        return _unavailable(capability_id, "no provider name")
    missing = object()
    for attribute in (f"{slug}_api_key", f"{slug}_provider_api_key"):
        value = getattr(settings, attribute, missing)
        if value is missing:
            # The setting does not exist at all — try the second spelling.
            continue
        # The setting exists: an unset (``None``) or blank key is unavailable,
        # and it is reported as *unset* rather than as *absent from config*.
        if value is not None and _as_text(value).strip():
            return _available(capability_id, f"key configured: {attribute}")
        return _unavailable(capability_id, f"key not configured: {attribute}")
    return _unavailable(capability_id, f"no setting {slug}_api_key")


def _probe_model(capability_id: str, name: str) -> Capability_Status:
    """``model:<name>`` -> a registered :data:`MODEL_LOCATORS` entry.

    With no locator registered, or a locator reporting no local path, the
    capability is unavailable: absent model + downloading disabled must never
    read as available (Req 21.5).
    """
    if not name:
        return _unavailable(capability_id, "no model name")
    locator = MODEL_LOCATORS.get(name)
    if locator is None:
        return _unavailable(capability_id, f"no locator registered: {name}")
    located = locator()
    if located is None:
        return _unavailable(capability_id, f"model absent locally: {name}")
    path = Path(_as_text(located))
    if not path.exists():
        return _unavailable(capability_id, f"model path missing: {path}")
    return _available(capability_id, f"model present: {path}")


def _probe_llm(capability_id: str) -> Capability_Status:
    """``llm`` -> ``worker.llm_client.llm_available`` (Req 5.5).

    Credential inspection only: ``llm_available`` reads configured keys and
    performs no network call (Req 5.6).
    """
    from worker import llm_client  # lazy (Req 1.4)

    if bool(llm_client.llm_available()):
        return _available(capability_id, "llm credentials configured")
    return _unavailable(capability_id, "no llm credentials configured")


def default_prober(capability_id: str) -> Capability_Status:
    """Probe one Capability_Id locally, never raising and never touching the network.

    Dispatches on :class:`Capability_Kind` (Req 5.1); wraps every underlying error as
    ``available=False`` with the error summary as ``detail`` (Req 5.3); uses
    ``settings.ffmpeg_binary`` for filter probes (Req 5.4); delegates to
    ``worker.llm_client.llm_available`` and ``worker.captions.font_available`` (Req 5.5);
    performs no network access (Req 5.6).
    """
    identifier = _as_text(capability_id)
    try:
        kind, name = parse_capability_id(identifier)
        if kind == Capability_Kind.PYTHON_PKG.value:
            return _probe_python_pkg(identifier, name)
        if kind == Capability_Kind.BINARY.value:
            return _probe_binary(identifier, name)
        if kind == Capability_Kind.FFMPEG_FILTER.value:
            return _probe_ffmpeg_filter(identifier, name)
        if kind == Capability_Kind.FONT.value:
            return _probe_font(identifier, name)
        if kind == Capability_Kind.PROVIDER_KEY.value:
            return _probe_provider_key(identifier, name)
        if kind == Capability_Kind.MODEL.value:
            return _probe_model(identifier, name)
        if kind == Capability_Kind.LLM.value:
            return _probe_llm(identifier)
        return _unavailable(identifier, f"unknown capability kind: {_short(identifier)}")
    except BaseException as exc:  # totality is the contract (Req 5.3)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return _unavailable(identifier, _error_detail(exc))


class Capability_Report:
    """Per-process cache of Capability_Id -> Capability_Status (Req 6)."""

    def __init__(self, prober: Prober | None = None) -> None:
        self._prober = prober or default_prober  # Req 5.7
        self._cache: dict[str, Capability_Status] = {}

    def status(self, capability_id: str) -> Capability_Status:
        """Cached status; the underlying prober runs at most once per id (Reqs 6.1, 6.2)."""
        key = _as_text(capability_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        status = self._probe(key)
        self._cache[key] = status
        return status

    def _probe(self, capability_id: str) -> Capability_Status:
        """Call the injected prober once, normalising and never propagating errors.

        An injected prober that raises yields an unavailable status carrying the
        exception class name (Req 5.3); a prober returning something other than a
        :class:`Capability_Status` (a bare ``bool``, say) is coerced, and a status
        answering for a different id is re-labelled with the requested one so the
        cache never lies about its keys.
        """
        try:
            result = self._prober(capability_id)
        except BaseException as exc:  # Req 5.3
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return _unavailable(capability_id, _error_detail(exc))
        if isinstance(result, Capability_Status):
            if result.capability_id == capability_id:
                return result
            return dataclasses.replace(result, capability_id=capability_id)
        if isinstance(result, Mapping):
            return dataclasses.replace(
                Capability_Status.from_dict(result), capability_id=capability_id
            )
        return Capability_Status(
            capability_id=capability_id,
            available=bool(result),
            detail="" if result is None else _short(result),
        )

    def available(self, capability_id: str) -> bool:
        """Whether ``capability_id`` is available (cached — Reqs 6.2, 6.3)."""
        return bool(self.status(capability_id).available)

    def first_missing(self, capability_ids: Iterable[str]) -> str | None:
        """First unavailable id in declaration order, else ``None`` (Req 7.1)."""
        for capability_id in self._iter_ids(capability_ids):
            if not self.available(capability_id):
                return capability_id
        return None

    def missing(self, capability_ids: Iterable[str]) -> list[str]:
        """All unavailable ids, in declaration order (Req 7.2)."""
        return [
            capability_id
            for capability_id in self._iter_ids(capability_ids)
            if not self.available(capability_id)
        ]

    @staticmethod
    def _iter_ids(capability_ids: Iterable[str]) -> list[str]:
        """Normalise a declaration list to ``str`` ids, tolerating hostile input.

        A bare string is treated as one id rather than as a sequence of
        characters, and a non-iterable argument yields no ids at all.
        """
        if capability_ids is None:
            return []
        if isinstance(capability_ids, str):
            return [capability_ids]
        try:
            items = list(capability_ids)
        except Exception:  # pragma: no cover - hostile iterable
            return []
        return [_as_text(item) for item in items]

    def to_dict(self) -> dict[str, dict[str, Any]]:
        """Serialisable mapping in sorted key order, for /api/info (Reqs 6.4, 20.2)."""
        return {key: self._cache[key].to_dict() for key in sorted(self._cache)}

    def invalidate(self, capability_id: str | None = None) -> None:
        """Drop one or all cached entries so a new prober can be injected (Req 6.5)."""
        if capability_id is None:
            self._cache.clear()
            return
        self._cache.pop(_as_text(capability_id), None)


_REPORT: Capability_Report | None = None


def get_report(prober: Prober | None = None) -> Capability_Report:
    """Process-wide report (Req 6.1); ``prober`` is honoured on first construction."""
    global _REPORT
    if _REPORT is None:
        _REPORT = Capability_Report(prober)
    return _REPORT


def reset_report() -> None:
    """Drop the process-wide report (test isolation — Reqs 6.5, 22.1)."""
    global _REPORT
    _REPORT = None
