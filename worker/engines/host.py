"""Engine host: gating, isolation, timeouts, and lifecycle (Reqs 3, 4, 7, 8, 17-19, 21).

The host is the Pipeline-side coordinator. It owns **every** cross-cutting
concern so no engine can forget one of them:

* **gating** — Feature_Flag resolution (Reqs 4.1, 4.2), Permissibility_Mode
  blocking (Reqs 21.2, 21.3), required/optional capability gating (Reqs 7.1,
  7.2, 7.4);
* **timing** — one :class:`~worker.engines.timebase.Time_Base` per job built from
  the *already performed* source probe, shared by every engine of every clip, so
  no extra ffprobe pass is added (Reqs 13.7, 19.4);
* **isolation** — every exception (``worker.ffmpeg_utils.FFmpegError`` included)
  becomes one ``engine:<id>:failed`` marker, a budget overrun becomes one
  ``engine:<id>:timeout`` marker, and the remaining engines still run (Req 8);
* **lifecycle** — one workspace per (job, clip, engine, Options_Digest), durable
  artifacts persisted *before* the workspaces are deleted (Reqs 16, 17, 18).

Every collaborator is dependency-injected (Req 22.1) and every default is
resolved **lazily**, so a disabled engine costs nothing: the Capability_Report is
first touched when an enabled engine declares a capability, and the
Storage_Backend when a durable artifact actually exists (Reqs 4.2, 19.5).

**Import safety (Req 1.4).** Module scope imports the standard library plus the
five stdlib-only ``worker.engines`` siblings. ``config``, ``runtime_config``,
``storage_backends`` and ``worker.ffmpeg_utils`` are never imported here at
module scope: the first three are reached lazily through
``worker.engines.artifacts`` (which owns the ``auto_delete_temp`` read) or
inside :meth:`Engine_Host._backend`,
and ``FFmpegError`` needs no import at all because it is caught as an ordinary
``Exception``. ``MediaInfo`` and ``BaseStorage`` are annotations only, kept
unevaluated by ``from __future__ import annotations``.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _Future_Timeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from worker.engines.artifacts import (
    Engine_Workspace,
    allocate_workspace,
    cleanup_job_artifacts,
    cleanup_workspace,
    persist_artifact,
)
from worker.engines.base import (
    MARKER_PREFIX,
    AV_Engine,
    Compose_Contribution,
    Engine_Artifact,
    Engine_Context,
    Engine_Result,
    Engine_Stage,
    Engine_Status,
    coerce_bool,
    coerce_float,
    coerce_int,
    derive_seed,
    marker,
    merge_markers,
    options_digest,
)
from worker.engines.capabilities import Capability_Report, get_report
from worker.engines.registry import Engine_Registry, get_registry
from worker.engines.timebase import DEFAULT_FPS, DEFAULT_SAMPLE_RATE, Time_Base

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from storage_backends.base import BaseStorage
    from worker.ffmpeg_utils import MediaInfo

__all__ = [
    "SOURCE_CLIP_ID",
    "MIN_WALL_TIMEOUT_S",
    "DEGRADED_DETAIL_PREFIX",
    "SEAM_NOTE_PREFIX",
    "Stage_Outcome",
    "Engine_Host",
    "filler_seam_notes",
]

#: The statuses whose :attr:`Engine_Result.media` the host will adopt as the
#: stage's replacement media, for an engine that declares ``produces_media``.
#:
#: ``applied`` and ``degraded`` only. ``failed`` means the engine could not
#: vouch for what it produced, and ``skipped`` short-circuits before collection,
#: so neither can hand media forward — the clip then falls back to the preceding
#: stage's file (Req 8.3).
_MEDIA_BEARING_STATUSES: frozenset[Engine_Status] = frozenset(
    {Engine_Status.APPLIED, Engine_Status.DEGRADED}
)

SOURCE_CLIP_ID = "source"
"""Clip identifier used for SOURCE-stage workspaces (there is no clip yet)."""

MIN_WALL_TIMEOUT_S = 1.0
"""Floor on the *wall-clock* watchdog wait (see :meth:`Engine_Host._execute`).

The authoritative budget check is the injected-clock comparison against
``ctx.deadline``; the thread wait is only a watchdog that stops a wedged engine
from blocking the job forever. Flooring it keeps a sub-second budget from being
mistaken for a timeout on a slow machine, and keeps the check meaningful when
the clock is a fake whose units are not seconds.
"""

SEAM_NOTE_PREFIX = "filler_seam:"
"""Prefix of the Seam notes :func:`filler_seam_notes` publishes on
:attr:`Engine_Context.notes`.

An *additive* note kind, not a new contract: :attr:`Engine_Context.notes` already exists
with its documented free-form convention, so this contributes a note **value**. Engines
that do not understand the prefix ignore it exactly as they ignore ``fps_fallback:``
(audio-stem-inpainting Reqs 6.1-6.3, 8.5, 20.6).
"""

DEGRADED_DETAIL_PREFIX = "degraded:"
"""Marker detail prefix capped at one occurrence per engine per clip (Req 7.4)."""

_LOGGER = logging.getLogger(__name__)


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


def _coerce_stage(value: Any) -> Optional[Engine_Stage]:
    """Return ``value`` as an :class:`Engine_Stage`, or ``None`` when unrecognised."""
    if isinstance(value, Engine_Stage):
        return value
    if isinstance(value, str):
        try:
            return Engine_Stage(value)
        except ValueError:
            return None
    return None


def _engine_id_of(engine: Any) -> str:
    """The Engine_Id an engine object declares (instance attribute or ClassVar)."""
    return _as_text(getattr(engine, "engine_id", ""))


def _declared_ids(value: Any) -> tuple[str, ...]:
    """Normalise a declared capability list, treating a bare string as one id."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(_as_text(key) for key in value)
    if isinstance(value, Iterable):
        try:
            return tuple(_as_text(item) for item in value)
        except Exception:  # pragma: no cover - hostile iterable
            return ()
    return ()


def _namespace_markers(engine_id: str, markers: Iterable[Any]) -> list[str]:
    """Prefix every marker with ``engine:<engine_id>:`` unless it already is (Req 3.3).

    An engine may return either a bare detail (``"fallback"``) or an
    already-namespaced marker (``"engine:me:fallback"``); both end up as
    ``engine:<engine_id>:...``, so every merged marker is attributable to exactly
    one engine — including a marker an engine mislabelled with a *different* id,
    which is re-namespaced under its real owner rather than silently trusted.
    """
    prefix = f"{MARKER_PREFIX}:{engine_id}:"
    namespaced: list[str] = []
    for entry in markers:
        text = _as_text(entry)
        namespaced.append(text if text.startswith(prefix) else marker(engine_id, text))
    return namespaced


def _capture(engine: Any, ctx: Engine_Context) -> tuple[bool, Any]:
    """Run ``engine.run(ctx)`` on the worker thread, returning its outcome as a value.

    ``(True, result)`` for a normal return, ``(False, exception)`` for any
    ``Exception``. Bringing the exception back as a *value* is what keeps the two
    failure modes of :meth:`Engine_Host._execute` distinguishable: since Python
    3.11 ``concurrent.futures.TimeoutError`` **is** the builtin ``TimeoutError``,
    so an engine that raises ``TimeoutError`` itself would otherwise be caught by
    the watchdog's ``except`` branch and misreported as a budget overrun
    (``engine:<id>:timeout``, Req 8.6) instead of a failure (``engine:<id>:failed``,
    Reqs 8.1, 8.4). With this indirection only the ``future.result(timeout=...)``
    wait can raise that exception.

    ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) is deliberately not
    captured: an interpreter-level signal must not be downgraded to one engine's
    failure marker.
    """
    try:
        return True, engine.run(ctx)
    except Exception as exc:  # noqa: BLE001 - Reqs 8.1, 8.4: isolate, never propagate
        return False, exc


def _cap_degradation(engine_id: str, markers: Sequence[str]) -> list[str]:
    """Keep at most one ``engine:<id>:degraded:<cap>`` marker, the first (Req 7.4)."""
    prefix = f"{MARKER_PREFIX}:{engine_id}:{DEGRADED_DETAIL_PREFIX}"
    capped: list[str] = []
    seen = False
    for entry in markers:
        if entry.startswith(prefix):
            if seen:
                continue
            seen = True
        capped.append(entry)
    return capped


def filler_seam_notes(keeps: Any) -> tuple[str, ...]:
    """Publish one ``filler_seam:<seconds>`` note per **interior** filler-removal join.

    The audio-stem-inpainting spec's one cross-spec touch point (its Reqs 6.1-6.3, 6.9,
    8.2), and deliberately **additive**: no dataclass, enum, protocol, signature or field
    changes here — :attr:`Engine_Context.notes` already carries free-form host annotations,
    so this contributes note *values*. Nothing in ``worker/effects/filler.py`` is called,
    re-planned or modified: ``FillerPlan.keeps`` is *read*, never recomputed (Req 8.2).

    A Seam is a join in the tightened output, i.e. the boundary between two kept intervals.
    The cursor accumulates ``keep.duration`` over ``keeps[:-1]``, so ``N`` keeps yield
    exactly ``N - 1`` notes: no note for the clip start (the loop emits *after* adding a
    duration, and the first addition already lands past ``0.0`` for any non-degenerate
    keep) and none for the clip end (the last keep's duration is never added) — Req 6.9.

    The value is ``round(cursor, 3)`` rendered with three decimals, mirroring
    ``filler.rebase_words``'s own ``round(..., 3)`` exactly, so a Seam time and the rebased
    word times it sits between agree to the millisecond rather than drifting apart (Req
    6.2, 6.3).

    Pure and total: attributes only, no clock, no filesystem; a ``None`` plan list, a
    non-iterable, a single keep or a keep whose ``duration`` is unusable yields ``()`` or a
    correspondingly shorter tuple rather than an exception — publication must never be able
    to break the Pipeline.

    Args:
        keeps: The ``FillerPlan.keeps`` sequence (anything exposing ``duration`` per item).

    Returns:
        The Seam notes in ascending time order, ``()`` when there is no interior join.
    """
    if keeps is None or isinstance(keeps, (str, bytes)) or not isinstance(keeps, Iterable):
        return ()
    try:
        items = list(keeps)
    except Exception:  # noqa: BLE001 - a hostile plan must not break publication
        return ()
    if len(items) < 2:
        return ()

    notes: list[str] = []
    cursor = 0.0
    for keep in items[:-1]:
        length = coerce_float(getattr(keep, "duration", None), 0.0)
        cursor += length
        notes.append(f"{SEAM_NOTE_PREFIX}{round(cursor, 3):.3f}")
    return tuple(notes)


# ---------------------------------------------------------------------------
# Stage aggregate
# ---------------------------------------------------------------------------


@dataclass
class Stage_Outcome:
    """Aggregate of one stage's invocations (one per :meth:`Engine_Host.run_stage`).

    :attr:`results` holds one :class:`~worker.engines.base.Engine_Result` per
    engine *registered* for the stage, in registry order — including the
    ``skipped`` results of disabled engines, which contribute no markers
    (Req 3.4) and are the audit trail proving they were gated, not run.

    :attr:`media` is ``None`` unless an engine declaring ``produces_media``
    returned a replacement file with a **media-bearing status** —
    :data:`_MEDIA_BEARING_STATUSES`, i.e. ``applied`` or ``degraded``. A
    ``failed`` or ``skipped`` engine, and any engine that returned no file,
    leaves the preceding stage's media in place (Req 8.3).

    ``degraded`` is media-bearing because degradation describes *fidelity*, not
    usability: an engine that fell back to a cheaper path and still produced a
    usable file has produced usable output, and discarding it would throw away
    real work while still charging the clip for the passes that made it. This is
    the ``Degraded_With_Media`` outcome the audio-stem-inpainting spec depends on
    (its Req 3.10): every rung that genuinely has nothing to hand back returns
    no media, so the distinction is carried by ``media is None``, not by status.
    """

    stage: Engine_Stage
    results: list[Engine_Result] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    artifacts: list[Engine_Artifact] = field(default_factory=list)
    contributions: list[Compose_Contribution] = field(default_factory=list)
    media: Optional[Path] = None

    def result_for(self, engine_id: str) -> Optional[Engine_Result]:
        """The result recorded for ``engine_id``, or ``None``."""
        key = _as_text(engine_id)
        for result in self.results:
            if result.engine_id == key:
                return result
        return None


# ---------------------------------------------------------------------------
# The host
# ---------------------------------------------------------------------------


class Engine_Host:
    """Pipeline-side engine coordinator. Fully dependency-injected (Req 22.1)."""

    def __init__(
        self,
        options: Any,
        *,
        job_id: str,
        temp_dir: str | Path,
        registry: Engine_Registry | None = None,
        capabilities: Capability_Report | None = None,
        storage: "BaseStorage | None" = None,
        clock: Callable[[], float] = time.monotonic,
        logger: Any | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        """Build a host for one ``run_pipeline`` call.

        Args:
            options: The Processing_Options for the job. It MUST already be
                ``worker.models.effective_options(...)``-normalised, so
                Permissibility_Mode downgrades are applied before any engine
                runs (Req 4.4); the host never mutates it (Req 1.3).
            job_id: Job identifier, used for workspace paths and artifact keys.
            temp_dir: The scratch directory handed to ``run_pipeline`` (Req 16.1).
            registry: Engine_Registry override; the module-level default is
                resolved lazily on first use (Req 22.2).
            capabilities: Capability_Report override; the process-wide report is
                resolved lazily and only when an enabled engine declares a
                capability, so a disabled engine triggers no probe (Req 4.2).
            storage: Storage_Backend override; the configured backend is resolved
                lazily and only when a durable artifact exists (Req 19.5).
            clock: Monotonic clock used for budgets and deadlines (injectable).
            logger: Logger-like object; defaults to this module's logger.
            sample_rate: Audio sample rate recorded in the shared Time_Base.
        """
        self._options = options
        self.job_id = _as_text(job_id)
        self.temp_dir = Path(_as_text(temp_dir))
        self._registry = registry
        self._capabilities = capabilities
        self._storage = storage
        self._clock = clock if callable(clock) else time.monotonic
        self._logger = logger if logger is not None and hasattr(logger, "warning") else _LOGGER
        self._sample_rate = sample_rate

        self._time_base: Optional[Time_Base] = None
        self._probed_fps: Any = None
        self._source_outcomes: dict[str, Stage_Outcome] = {}
        self._source_results: dict[str, Engine_Result] = {}
        self._workspaces: dict[str, list[Engine_Workspace]] = {}
        # clip_id -> [(engine_id, artifact, results_list, result_index)] so a
        # persisted key can be written back into the very Engine_Result the
        # caller is holding (Req 18.5).
        self._durables: dict[str, list[tuple[str, Engine_Artifact, list, int]]] = {}

    # --- collaborators ----------------------------------------------------

    @property
    def options(self) -> Any:
        """The Processing_Options this host was given (never mutated — Req 1.3)."""
        return self._options

    @property
    def registry(self) -> Engine_Registry:
        """The Engine_Registry in use; the module default is resolved lazily."""
        if self._registry is None:
            self._registry = get_registry()
        return self._registry

    @property
    def capabilities(self) -> Capability_Report:
        """The Capability_Report in use; resolved lazily on first probe (Req 4.2)."""
        if self._capabilities is None:
            self._capabilities = get_report()
        return self._capabilities

    @property
    def permissibility(self) -> bool:
        """``ProcessingOptions.permissibility_mode`` (Reqs 21.2, 21.3)."""
        value: Any = getattr(self._options, "permissibility_mode", None)
        if value is None and isinstance(self._options, Mapping):
            try:
                value = self._options.get("permissibility_mode")
            except Exception:  # pragma: no cover - hostile mapping
                value = None
        return coerce_bool(value, False)

    # --- gating -----------------------------------------------------------

    @property
    def active(self) -> bool:
        """True when at least one *registered* engine is enabled (Reqs 19.5, 23.1).

        The Pipeline guards every hook with this, so a run with no registered or
        no enabled engine performs no probe, no workspace allocation and no extra
        media pass — and therefore reproduces v0.8.0 exactly.
        """
        return any(self._is_enabled(engine) for engine in self._all_engines())

    def enabled_for(self, stage: Engine_Stage) -> list[AV_Engine]:
        """Enabled engines for ``stage`` in registry order (Reqs 3.1, 4.1)."""
        return [engine for engine in self._registered_for(stage) if self._is_enabled(engine)]

    # --- timing -----------------------------------------------------------

    def time_base(self, info: "MediaInfo | None" = None) -> Time_Base:
        """Build (once) and cache the job's Time_Base from the source probe.

        Only ``info.fps`` is read, and only on the first call: every engine of
        every clip receives this identical object, so no additional ffprobe pass
        is performed (Reqs 13.2, 13.7, 19.4). When the probed fps is unusable the
        documented fallback is substituted and the rejected value is remembered so
        each Engine_Context carries an ``fps_fallback:<value>`` note (Req 13.3).

        Called without ``info`` (or before any probe is available) it yields the
        documented fallback frame rate, flagged as substituted.
        """
        if self._time_base is None:
            probed = getattr(info, "fps", None) if info is not None else None
            if info is None:
                base = Time_Base(
                    fps=DEFAULT_FPS, sample_rate=self._sample_rate, fps_substituted=True
                )
            else:
                base = Time_Base.from_media_info(info, sample_rate=self._sample_rate)
            self._time_base = base
            self._probed_fps = probed if base.fps_substituted else None
        return self._time_base

    # --- invocation -------------------------------------------------------

    def run_source(self, source: str | Path, info: "MediaInfo") -> Stage_Outcome:
        """Invoke SOURCE-stage engines at most once per source (Reqs 3.5, 19.3).

        The job's Time_Base is established here from the probe the Pipeline has
        already performed. The outcome is cached per source: a second call with
        the same source returns the identical :class:`Stage_Outcome` without
        re-invoking anything, and :meth:`source_result` serves every clip.
        """
        self.time_base(info)
        key = _as_text(source)
        cached = self._source_outcomes.get(key)
        if cached is not None:
            return cached

        duration = coerce_float(getattr(info, "duration", 0.0), 0.0, lo=0.0)
        outcome = self._run(
            Engine_Stage.SOURCE,
            clip_id=SOURCE_CLIP_ID,
            source=source,
            clip_path=None,
            clip_start=0.0,
            clip_end=duration,
            duration=duration,
            words=(),
        )
        self._source_outcomes[key] = outcome
        for result in outcome.results:
            self._source_results[result.engine_id] = result
        return outcome

    def source_result(self, engine_id: str) -> Optional[Engine_Result]:
        """The cached SOURCE-stage result for ``engine_id``, reused by every clip."""
        return self._source_results.get(_as_text(engine_id))

    def run_stage(
        self,
        stage: Engine_Stage,
        *,
        clip_id: str,
        source: str | Path,
        clip_path: Optional[Path],
        clip_start: float,
        clip_end: float,
        duration: float,
        words: Sequence[Any] = (),
        clip_metadata: Optional[Mapping[str, Any]] = None,
        filler_plan: Any = None,
        notes: Sequence[str] = (),
    ) -> Stage_Outcome:
        """Invoke every enabled engine of ``stage`` for one clip.

        Applies the gating ladder per engine (:meth:`_invoke`), isolates failures
        and timeouts (Req 8), merges markers in registry order without duplicates
        (Reqs 3.2, 3.3, 3.6), and returns replacement :attr:`Stage_Outcome.media`
        only when an engine declaring ``produces_media`` succeeded (Req 8.3).

        ``words`` is passed through untouched, so the Pipeline's already-rebased
        Word_Timeline reaches every engine of every later stage (Req 15.2), and
        the bounds handed to engines are clip-relative ``[0, duration]``
        (Reqs 15.1, 15.7).

        ``clip_metadata`` is the keyword-only Clip_Metadata pass-through (Req
        15.8): per-clip values produced *upstream* of this stage run — today
        ``hook_text`` and ``clip_size`` — merged into **every** Engine_Context
        this stage run builds, one per invoked engine and not just the first.
        ``None`` means the empty mapping, so a call that omits it builds contexts
        identical to the pre-Clip_Metadata ones and the all-off parity gate is
        untouched: Clip_Metadata is read-only planning input that never reaches
        the ffmpeg argv (Reqs 15.8, 23.1). It is a separate channel from
        :attr:`Engine_Context.deps`, which remains the host's own injected
        clock/logger/storage seam.

        ``filler_plan`` is the optional ``FillerPlan`` for this clip — or, equivalently,
        its bare ``keeps`` sequence, which is what the Pipeline keeps in scope — read
        **only** to
        publish :func:`filler_seam_notes` on every context this stage run builds
        (audio-stem-inpainting Reqs 6.1, 8.1, 8.5). It adds no Pipeline stage and changes no
        stage order; omitting it — or passing a plan with a single keep, or one whose
        removal did nothing — publishes zero notes, so contexts are identical to the
        pre-Seam ones and the all-off parity gate is untouched.

        ``notes`` is the caller's own free-form Engine_Context notes, appended after
        the host's synthesised ones (``fps_fallback:``, then ``filler_seam:``). It
        exists so a Pipeline stage can publish a note the host has no way to derive:
        the host can only synthesise what it can see, and the caller knows things it
        cannot.

        Strictly additive: the default is empty, so every pre-existing call site
        builds byte-identical Engine_Contexts and the all-off parity gate is
        untouched. Ordering is fixed rather than merged, so an engine reading a
        prefix by position keeps working, and values are coerced to ``str`` so a
        hostile sequence cannot put a non-string into ``ctx.notes``.
        """
        return self._run(
            stage,
            clip_id=clip_id,
            source=source,
            clip_path=clip_path,
            clip_start=clip_start,
            clip_end=clip_end,
            duration=duration,
            words=words,
            clip_metadata=clip_metadata,
            filler_plan=filler_plan,
            notes=notes,
        )

    # --- lifecycle --------------------------------------------------------

    def finish_clip(self, clip_id: str) -> list[str]:
        """Persist durable artifacts, then delete this clip's workspaces.

        Durable artifacts are stored **before** any deletion (Req 17.7) through
        ``persist_artifact``, and the resulting key is written back into the
        Engine_Result the caller holds (Req 18.5). A persistence error yields
        exactly one ``engine:<id>:artifact_failed`` marker per engine and never
        propagates, so the clip is still produced (Req 18.6).

        Workspaces are then removed **unconditionally** and regardless of the
        engine's status (Reqs 17.1, 17.5): Req 17.1 puts no condition on the
        per-clip deletion, and the ``auto_delete_temp`` toggle governs the
        *job-level* scratch space only (Reqs 17.2, 17.3, 17.6) — which is where
        :meth:`finish_job` honours it, exactly like
        ``artifacts.cleanup_workspace`` (unconditional) versus
        ``artifacts.cleanup_job_artifacts`` (gated). ``OSError`` is logged and
        swallowed (Req 17.4).

        Returns:
            The extra markers produced by finalisation (possibly empty).
        """
        key = _as_text(clip_id)
        failed: dict[str, None] = {}
        for engine_id, artifact, results, index in self._durables.pop(key, []):
            try:
                stored = persist_artifact(
                    artifact,
                    job_id=self.job_id,
                    clip_id=key,
                    engine_id=engine_id,
                    storage=self._backend(),
                )
            except Exception as exc:  # noqa: BLE001 - Req 18.6: degrade, never fail
                self._warn(
                    "engine %s durable artifact %s failed to persist: %s: %s",
                    engine_id,
                    artifact.name,
                    type(exc).__name__,
                    exc,
                )
                failed[engine_id] = None
            else:
                self._record_storage_key(results, index, stored)

        markers = [marker(engine_id, "artifact_failed") for engine_id in failed]

        for workspace in self._workspaces.pop(key, []):
            cleanup_workspace(workspace, logger=self._logger)
        return markers

    def finish_job(self) -> list[str]:
        """Finalise the SOURCE stage, then release the job's engine scratch space.

        SOURCE-stage engines run once per *source*, not once per clip, so their
        workspaces live under the :data:`SOURCE_CLIP_ID` pseudo-clip that no
        ``finish_clip(clip_id)`` call from the Pipeline ever names. Finalising it
        here is what makes a SOURCE-stage engine's durable artifacts reach the
        Storage_Backend at all (Reqs 17.7, 18.1) instead of being deleted with the
        job directory.

        ``<temp_dir>/engines/<job_id>`` is then removed when ``auto_delete_temp``
        is enabled (Req 17.6), delegating to ``artifacts.cleanup_job_artifacts``,
        which reads the runtime setting, routes job-level removal through the
        existing ``storage_backends.retention.cleanup_temp`` path (Req 17.2) and
        logs and swallows ``OSError`` (Req 17.4).

        Returns:
            Any markers produced by finalising the source stage — in practice
            ``engine:<id>:artifact_failed`` (Req 18.6). They are also logged,
            because a job-level marker has no ``ClipResult.effects_applied`` list
            of its own to land in.
        """
        markers = self.finish_clip(SOURCE_CLIP_ID)
        for entry in markers:
            self._warn("engine source-stage finalisation recorded %s", entry)
        cleanup_job_artifacts(self.job_id, temp_dir=self.temp_dir, logger=self._logger)
        self._workspaces.clear()
        return markers

    # --- internals: invocation -------------------------------------------

    def _run(
        self,
        stage: Any,
        *,
        clip_id: Any,
        source: str | Path,
        clip_path: Optional[Path],
        clip_start: float,
        clip_end: float,
        duration: float,
        words: Sequence[Any] = (),
        clip_metadata: Optional[Mapping[str, Any]] = None,
        filler_plan: Any = None,
        notes: Sequence[str] = (),
    ) -> Stage_Outcome:
        """Shared body of :meth:`run_source` and :meth:`run_stage`."""
        coerced = _coerce_stage(stage)
        outcome = Stage_Outcome(stage=coerced if coerced is not None else stage)
        if coerced is None:
            return outcome

        key = _as_text(clip_id)
        # Reserved ffmpeg input indices for this stage run, computed BEFORE any
        # engine runs so every Engine_Context can carry its own block start.
        offsets = self._input_offsets(coerced)
        # Clip_Metadata is snapshotted once per stage run: ``None`` becomes the
        # documented empty mapping, and every engine's context gets its own copy,
        # so no engine can reach the caller's mapping or another engine's context
        # through it (Req 15.8). Keys and values are copied verbatim.
        metadata = dict(clip_metadata) if isinstance(clip_metadata, Mapping) else {}
        # The caller's own notes, coerced once per stage run rather than per engine, and
        # appended last so the host's synthesised prefixes keep their positions.
        caller_notes = tuple(_as_text(note) for note in (notes or ()))
        # Seam notes are computed once per stage run, from the already-planned keeps, and
        # merged into every context this run builds — one publication, N readers.
        # A ``FillerPlan`` or, equivalently, its bare ``keeps`` sequence — the Pipeline
        # keeps the latter in scope for the whole clip, so both spellings are accepted.
        seams = filler_seam_notes(getattr(filler_plan, "keeps", filler_plan))
        for engine in self._registered_for(coerced):
            result = self._invoke(
                engine,
                lambda bound=engine: self._build_context(
                    bound,
                    coerced,
                    clip_id=key,
                    source=source,
                    clip_path=clip_path,
                    clip_start=clip_start,
                    clip_end=clip_end,
                    duration=duration,
                    words=words,
                    first_input_index=offsets.get(_engine_id_of(bound), 0),
                    clip_metadata=metadata,
                    seam_notes=seams,
                    caller_notes=caller_notes,
                ),
            )
            outcome.results.append(result)
            if result.status is Engine_Status.SKIPPED:
                continue
            outcome.artifacts.extend(result.artifacts)
            if result.contribution is not None:
                outcome.contributions.append(result.contribution)
            if (
                result.media is not None
                and result.status in _MEDIA_BEARING_STATUSES
                and bool(getattr(engine, "produces_media", False))
            ):
                outcome.media = result.media
            self._register_durables(key, result, outcome.results, len(outcome.results) - 1)

        outcome.markers = merge_markers(outcome.results)
        return outcome

    def _input_offsets(self, stage: Engine_Stage) -> dict[str, int]:
        """Reserved ``Engine_Context.first_input_index`` per engine of ``stage``.

        The compositor lays the extra ffmpeg inputs out as one contiguous block
        immediately after the primary clip (index 0), so for the engines of a
        stage run, taken in registry ``(priority, engine_id)`` order::

            first_input_index(engine_k) = 1 + sum(max_inputs of preceding engines)

        Only COMPOSE-stage engines contribute inputs, so every other stage gets an
        empty mapping and the documented meaningless ``0``. An engine declaring
        ``max_inputs == 0`` consumes no index space and is likewise given ``0``,
        which is why the block start of the *first* contributing engine is always
        exactly ``1`` no matter how many non-contributing engines precede it.

        The mapping is built from the **enabled** engines of the stage
        (:meth:`enabled_for`), because a disabled engine is skipped before its body
        is entered (Req 4.2) and therefore contributes nothing to the compositor's
        input list. Enablement is pure, so the mapping is deterministic and fixed
        before any ``run()`` executes.

        Contract for a contributing engine: when it is invoked it must emit exactly
        ``max_inputs`` inputs, since the compositor appends the inputs it actually
        receives contiguously. Nothing here can repair a short contribution — an
        engine that declares two inputs and returns one would shift every later
        engine's real index. No engine declares an input today (``max_inputs``
        defaults to ``0``), so the reservation is inert until the first one does.
        """
        if stage is not Engine_Stage.COMPOSE:
            return {}
        offsets: dict[str, int] = {}
        cursor = 1
        for engine in self.enabled_for(stage):
            declared = coerce_int(getattr(engine, "max_inputs", 0), 0, lo=0)
            engine_id = _engine_id_of(engine)
            if declared <= 0:
                offsets[engine_id] = 0
                continue
            offsets[engine_id] = cursor
            cursor += declared
        return offsets

    def _invoke(
        self, engine: AV_Engine, ctx_factory: Callable[[], Engine_Context]
    ) -> Engine_Result:
        """Gating ladder plus failure isolation for one engine.

        1. disabled -> ``skipped``, no probe, no workspace, no marker (4.2, 3.4);
        2. Permissibility_Mode and ``requires_network`` -> ``degraded`` +
           ``permissibility_blocked``, the engine body never entered (21.2, 21.3);
        3. first missing required capability, in declaration order -> ``degraded``
           + ``unavailable:<capability_id>``, the engine body never entered (7.1);
        4. ``ctx_factory()`` allocates the workspace and builds the context, then
           the engine runs under its declared budget;
        5. any ``Exception`` (``FFmpegError`` included) -> ``failed`` + exactly one
           ``failed`` marker, logged with type and message (8.1, 8.4, 8.5);
        6. budget overrun -> ``failed`` + exactly one ``timeout`` marker, the
           contribution and artifacts abandoned (8.6);
        7. surviving markers are namespaced and the degradation marker is capped
           at one per engine per clip (3.3, 7.4).
        """
        engine_id = _engine_id_of(engine)

        # 1 — disabled: no capability probe, no workspace, no marker.
        if not self._is_enabled(engine):
            return Engine_Result.skipped(engine_id)

        # 2 — permissibility.
        if self.permissibility and coerce_bool(getattr(engine, "requires_network", False), False):
            detail = "permissibility_blocked"
            return Engine_Result.degraded(
                engine_id, detail, markers=(marker(engine_id, detail),)
            )

        # 3 — required capabilities, in declaration order.
        report = self.capabilities
        required = _declared_ids(getattr(engine, "required_capabilities", ()))
        missing = report.first_missing(required) if required else None
        if missing is not None:
            detail = f"unavailable:{missing}"
            return Engine_Result.degraded(
                engine_id, detail, markers=(marker(engine_id, detail),)
            )

        # Optional capabilities: the engine still runs at reduced fidelity, and
        # the host guarantees the degradation is recorded even if the engine
        # forgets to (Req 7.2), capped at one marker per engine per clip (7.4).
        optional_missing = report.missing(_declared_ids(getattr(engine, "optional_capabilities", ())))
        degradation = (
            marker(engine_id, f"{DEGRADED_DETAIL_PREFIX}{optional_missing[0]}")
            if optional_missing
            else None
        )

        # 4 — workspace + context, then execution under the declared budget.
        started = self._now()
        try:
            ctx = ctx_factory()
        except Exception as exc:  # noqa: BLE001 - Reqs 8.1, 8.5
            self._log_failure(engine_id, exc, "could not be prepared")
            return self._failure(engine_id, "failed", exc, self._now() - started)

        return self._execute(engine, ctx, degradation=degradation, started=started)

    def _execute(
        self,
        engine: AV_Engine,
        ctx: Engine_Context,
        *,
        degradation: Optional[str],
        started: float,
    ) -> Engine_Result:
        """Run ``engine`` on a single-worker thread and normalise its outcome.

        Two independent budget checks apply (Req 8.6):

        * the **wall-clock watchdog** — ``future.result(timeout=...)`` stops a
          wedged engine from blocking the job forever. The thread may outlive the
          wait, so the host abandons the engine's *contribution* rather than
          claiming a hard kill;
        * the **deadline check** — after a normal return, the injected clock is
          compared against ``ctx.deadline``. This is the authoritative check: it
          catches an engine that consumed its budget through real work (or, under
          an injected clock, through simulated time) and returned anyway.
        """
        engine_id = ctx.engine_id or _engine_id_of(engine)
        budget = coerce_float(ctx.time_budget_s, 0.0, lo=0.0)
        wait = max(budget, MIN_WALL_TIMEOUT_S) if budget > 0.0 else None

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_capture, engine, ctx)
            try:
                returned, payload = future.result(timeout=wait)
            except _Future_Timeout:
                future.cancel()
                self._warn(
                    "engine %s exceeded its %.3fs time budget; abandoning its contribution",
                    engine_id,
                    budget,
                )
                return self._failure(engine_id, "timeout", None, self._now() - started)
            except Exception as exc:  # noqa: BLE001 - Reqs 8.1, 8.4, 8.5
                self._log_failure(engine_id, exc, "failed")
                return self._failure(engine_id, "failed", exc, self._now() - started)
        finally:
            executor.shutdown(wait=False)

        if not returned:
            # The engine raised. Because :func:`_capture` brings the exception back
            # as a value, an engine raising ``TimeoutError`` is reported as a
            # failure and not as a budget overrun (Reqs 8.1, 8.4).
            self._log_failure(engine_id, payload, "failed")
            return self._failure(engine_id, "failed", payload, self._now() - started)
        raw = payload

        now = self._now()
        if math.isfinite(ctx.deadline) and now > ctx.deadline:
            self._warn(
                "engine %s exceeded its %.3fs time budget; abandoning its contribution",
                engine_id,
                budget,
            )
            return self._failure(engine_id, "timeout", None, now - started)

        return self._normalise(engine_id, raw, degradation=degradation, elapsed=now - started)

    def _normalise(
        self,
        engine_id: str,
        raw: Any,
        *,
        degradation: Optional[str],
        elapsed: float,
    ) -> Engine_Result:
        """Namespace, cap and attribute the markers of a returned result.

        A result that is not an :class:`Engine_Result` (a mapping, or nothing at
        all) is coerced rather than trusted, and ``engine_id`` is always the
        *invoked* engine's, so every marker is attributable (Req 3.3).
        """
        if isinstance(raw, Engine_Result):
            result = raw
        elif isinstance(raw, Mapping):
            result = Engine_Result.from_dict(raw)
        elif raw is None:
            result = Engine_Result(engine_id=engine_id, status=Engine_Status.SKIPPED)
        else:
            result = Engine_Result(engine_id=engine_id, status=Engine_Status.FAILED,
                                   detail=f"engine returned {type(raw).__name__}")

        markers = _namespace_markers(engine_id, result.markers)
        if degradation is not None:
            markers.append(degradation)
        markers = _cap_degradation(engine_id, markers)

        return dataclasses.replace(
            result,
            engine_id=engine_id,
            markers=tuple(markers),
            elapsed_s=result.elapsed_s or max(0.0, elapsed),
        )

    def _failure(
        self, engine_id: str, detail: str, exc: Optional[BaseException], elapsed: float
    ) -> Engine_Result:
        """A ``failed`` result carrying exactly one ``engine:<id>:<detail>`` marker.

        Used for both the exception path and the budget-overrun path, so neither
        keeps a contribution, an artifact or a media replacement (Reqs 8.1, 8.6).
        """
        message = detail if exc is None else f"{type(exc).__name__}: {exc}"
        return Engine_Result(
            engine_id=engine_id,
            status=Engine_Status.FAILED,
            markers=(marker(engine_id, detail),),
            detail=message,
            elapsed_s=max(0.0, elapsed),
        )

    # --- internals: context ----------------------------------------------

    def _build_context(
        self,
        engine: AV_Engine,
        stage: Engine_Stage,
        *,
        clip_id: str,
        source: str | Path,
        clip_path: Optional[Path],
        clip_start: float,
        clip_end: float,
        duration: float,
        words: Sequence[Any],
        first_input_index: int = 0,
        clip_metadata: Optional[Mapping[str, Any]] = None,
        seam_notes: Sequence[str] = (),
        caller_notes: Sequence[str] = (),
    ) -> Engine_Context:
        """Allocate the workspace and build the frozen Engine_Context (step 4).

        Called only for an engine that passed the whole gating ladder, so a
        disabled or blocked engine never causes a workspace to exist (Req 4.2).
        Raising here (an unusable ``resolve_options``, an ``OSError`` creating the
        directory) is caught by :meth:`_invoke` and reported as ``failed``.

        ``first_input_index`` is the ffmpeg input block :meth:`_input_offsets`
        reserved for this engine — ``0`` for every non-contributing engine and for
        every stage other than COMPOSE.

        ``clip_metadata`` is this stage run's Clip_Metadata snapshot; it is copied
        into the context so each engine holds its own mapping (Req 15.8), and
        ``None`` yields the documented empty default.

        ``seam_notes`` is this stage run's already-computed
        :func:`filler_seam_notes` tuple. It is **appended** to the host's own notes,
        so ``fps_fallback:`` keeps its position and spelling and an engine that reads
        neither prefix is unaffected (audio-stem-inpainting Reqs 8.5, 20.6). Empty by
        default, so every existing caller builds byte-identical contexts.

        ``caller_notes`` is :meth:`run_stage`'s ``notes`` keyword, appended **after**
        the seam notes so the note order is fully determined and stable:
        ``fps_fallback:`` first, then ``filler_seam:``, then the caller's. Also empty
        by default, for the same parity reason.
        """
        engine_id = _engine_id_of(engine)
        resolved = engine.resolve_options(self._options)
        digest = options_digest(resolved)
        workspace = allocate_workspace(
            self.temp_dir, self.job_id, clip_id, engine_id, digest
        )
        self._workspaces.setdefault(clip_id, []).append(workspace)

        base = self.time_base()
        notes = (f"fps_fallback:{_as_text(self._probed_fps)}",) if base.fps_substituted else ()
        notes = notes + tuple(_as_text(note) for note in (seam_notes or ()))
        notes = notes + tuple(_as_text(note) for note in (caller_notes or ()))
        budget = coerce_float(getattr(engine, "time_budget_s", 0.0), 0.0, lo=0.0)
        deadline = self._now() + budget if budget > 0.0 else math.inf

        deps: dict[str, Any] = {"clock": self._clock, "logger": self._logger}
        if self._storage is not None:
            deps["storage"] = self._storage

        return Engine_Context(
            job_id=self.job_id,
            clip_id=clip_id,
            engine_id=engine_id,
            stage=stage,
            source_path=Path(_as_text(source)),
            clip_path=Path(_as_text(clip_path)) if clip_path is not None else None,
            time_base=base,
            clip_start=clip_start,
            clip_end=clip_end,
            duration=duration,
            words=tuple(words or ()),
            options=resolved,
            options_digest=digest,
            seed=derive_seed(_as_text(source), digest),
            workspace=workspace,
            capabilities=self.capabilities,
            permissibility=self.permissibility,
            deadline=deadline,
            time_budget_s=budget,
            first_input_index=first_input_index,
            notes=notes,
            deps=deps,
            clip_metadata=dict(clip_metadata) if isinstance(clip_metadata, Mapping) else {},
        )

    # --- internals: bookkeeping ------------------------------------------

    def _all_engines(self) -> list[AV_Engine]:
        """Every registered engine in registry order, never raising."""
        try:
            return list(self.registry.all())
        except Exception as exc:  # noqa: BLE001 - a hostile registry must not break gating
            self._warn("engine registry could not be listed: %s", exc)
            return []

    def _registered_for(self, stage: Engine_Stage) -> list[AV_Engine]:
        """Every engine registered for ``stage`` in registry order (Req 2.5)."""
        try:
            return list(self.registry.for_stage(stage))
        except Exception as exc:  # noqa: BLE001 - defensive, as above
            self._warn("engine registry could not be listed for %s: %s", stage, exc)
            return []

    def _is_enabled(self, engine: Any) -> bool:
        """Whether ``engine``'s Feature_Flag is set on the resolved options (Req 4.1)."""
        checker = getattr(engine, "is_enabled", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(self._options))
        except Exception as exc:  # noqa: BLE001 - an unreadable flag reads as disabled
            self._warn("engine %s enablement check failed: %s", _engine_id_of(engine), exc)
            return False

    def _register_durables(
        self, clip_id: str, result: Engine_Result, results: list, index: int
    ) -> None:
        """Remember this result's durable artifacts for :meth:`finish_clip` (Req 17.7).

        A ``failed`` result's artifacts are abandoned — that covers both the
        exception path and the budget overrun (Reqs 8.1, 8.6) — while an
        ``applied`` or ``degraded`` engine's durable artifacts are persisted.
        """
        if result.status in (Engine_Status.FAILED, Engine_Status.SKIPPED):
            return
        pending = [artifact for artifact in result.artifacts if artifact.durable]
        if not pending:
            return
        bucket = self._durables.setdefault(clip_id, [])
        for artifact in pending:
            bucket.append((result.engine_id, artifact, results, index))

    def _record_storage_key(self, results: list, index: int, stored: Engine_Artifact) -> None:
        """Write a persisted artifact's storage key back into its result (Req 18.5)."""
        try:
            result = results[index]
        except (IndexError, TypeError):  # pragma: no cover - defensive
            return
        if not isinstance(result, Engine_Result):  # pragma: no cover - defensive
            return
        artifacts = tuple(
            stored if item.path == stored.path and item.name == stored.name else item
            for item in result.artifacts
        )
        results[index] = dataclasses.replace(result, artifacts=artifacts)

    def _backend(self) -> "BaseStorage":
        """The Storage_Backend, resolved lazily on first durable artifact (Req 1.4)."""
        if self._storage is None:
            from storage_backends import get_storage  # lazy (Req 1.4)

            self._storage = get_storage()
        return self._storage

    def _now(self) -> float:
        """The injected clock's current value, falling back to ``time.monotonic``."""
        try:
            value = self._clock()
        except Exception:  # pragma: no cover - a hostile clock
            return time.monotonic()
        return float(value) if isinstance(value, (int, float)) else time.monotonic()

    def _warn(self, message: str, *args: Any) -> None:
        """Log a warning, never raising (a logger that fails must not break a clip)."""
        try:
            self._logger.warning(message, *args)
        except Exception:  # pragma: no cover - a logger that raises
            pass

    def _log_failure(self, engine_id: str, exc: BaseException, what: str) -> None:
        """Log the caught exception's type and message (Req 8.5)."""
        self._warn("engine %s %s: %s: %s", engine_id, what, type(exc).__name__, exc)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Engine_Host(job_id={self.job_id!r}, temp_dir={str(self.temp_dir)!r})"
