"""Engine workspaces and durable engine artifacts.

Three concerns live here, all of them thin and all of them injectable:

* **workspace allocation** — :func:`sanitize_component`, the frozen
  :class:`Engine_Workspace` and :func:`allocate_workspace` carve one scratch
  directory per (job, clip, engine, options digest) beneath the ``temp_dir``
  handed to ``worker.pipeline.run_pipeline``, with every path component
  sanitised and containment asserted (Reqs 11.6, 16.1-16.7);
* **cleanup** — :func:`cleanup_workspace`, :func:`cleanup_job_workspaces` and
  :func:`cleanup_job_artifacts` delete engine scratch space on the same
  schedule as every other temp file, routing job-level removal through the
  existing ``storage_backends.retention.cleanup_temp`` path when the
  ``auto_delete_temp`` runtime toggle is on (Reqs 17.1-17.6);
* **durable persistence** — :func:`artifact_key` and :func:`persist_artifact`
  address and store artifacts through the active ``BaseStorage``, so the key and
  the code path are identical on ``local`` and on ``s3`` (Reqs 18.1-18.5).

**Import safety (Req 1.4).** Module scope imports the standard library plus
``worker.engines.base`` (itself stdlib-only). ``storage_backends`` (which pulls
in ``config``/``pydantic-settings`` and, for S3, ``boto3``) and
``runtime_config`` are imported **lazily inside the functions that need them**,
so importing this module costs nothing and drags in no optional dependency.

**Errors.** Path helpers raise ``ValueError`` only for a genuine containment
violation. Cleanup logs and swallows ``OSError`` (Req 17.4). Persistence
deliberately lets storage errors propagate: the Engine_Host turns them into a
single ``engine:<engine_id>:artifact_failed`` marker and still produces the clip
(Req 18.6).
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

from worker.engines.base import Engine_Artifact

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from storage_backends.base import BaseStorage

__all__ = [
    "ENGINE_TEMP_ROOT",
    "ENGINE_KEY_ROOT",
    "MAX_COMPONENT_LEN",
    "sanitize_component",
    "Engine_Workspace",
    "allocate_workspace",
    "cleanup_workspace",
    "cleanup_job_workspaces",
    "cleanup_job_artifacts",
    "artifact_key",
    "persist_artifact",
]

ENGINE_TEMP_ROOT = "engines"
"""First component of a workspace path: ``<temp_dir>/engines/<job>/<clip>/<engine>__<digest>``."""

ENGINE_KEY_ROOT = "engines"
"""First segment of a durable artifact key: ``engines/<job>/<clip>/<engine>/<name>``."""

MAX_COMPONENT_LEN = 48
"""Ceiling on one sanitised path component, so deep paths stay well inside every OS limit."""

#: Length of the hex disambiguator appended to a *lossy* sanitisation (see
#: :func:`sanitize_component`).
DISAMBIGUATOR_LEN = 6

#: Everything outside this class is replaced by ``"_"``: the result contains no
#: path separator, no NUL, no whitespace, no shell metacharacter, and no
#: upper-case character (so two components can never differ only by case on a
#: case-insensitive filesystem).
_UNSAFE = re.compile(r"[^a-z0-9._-]")

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


def _as_path(value: Any) -> Path:
    """Return ``value`` as a :class:`~pathlib.Path`, never raising."""
    if isinstance(value, Path):
        return value
    try:
        return Path(_as_text(value))
    except Exception:  # pragma: no cover - hostile path input
        return Path("")


def _digest(value: str) -> str:
    """Short stable hex digest of ``value`` (lower-case, :data:`DISAMBIGUATOR_LEN` chars)."""
    encoded = value.encode("utf-8", "surrogatepass")
    return hashlib.sha256(encoded).hexdigest()[:DISAMBIGUATOR_LEN]


def _log(logger: Any, message: str, *args: Any) -> None:
    """Log ``message`` exactly once through ``logger`` or the module logger (Req 17.4)."""
    target = logger if logger is not None and hasattr(logger, "warning") else _LOGGER
    try:
        target.warning(message, *args)
    except Exception:  # pragma: no cover - a logger that raises must not break cleanup
        pass


def sanitize_component(value: Any, *, fallback: str = "x") -> str:
    """Return a safe single path component for ``value`` (Reqs 16.6, 18.4).

    The transform lower-cases, replaces every character outside ``[a-z0-9._-]``
    (path separators, NUL, whitespace, unicode, shell metacharacters) with
    ``"_"``, strips leading dots, and substitutes ``fallback`` for a result that
    would be ``""``, ``"."`` or ``".."``. The output is therefore a single
    component that can neither traverse (``..``) nor absolutise (``/``) nor
    vanish (``""``) — the containment guarantee of Reqs 16.5/16.6 and the
    key-safety guarantee of Req 18.4.

    Because that transform is *lossy*, a plain truncation would map distinct
    inputs onto the same directory and break the distinctness Reqs 16.7/11.6
    require. So whenever the sanitised form differs from the input, or the input
    exceeds :data:`MAX_COMPONENT_LEN`, a ``-<6 hex>`` digest of the **raw** input
    is appended and the readable part truncated to fit
    :data:`MAX_COMPONENT_LEN`. Already-safe short values (ordinary job ids, clip
    ids, Engine_Ids, options digests) pass through untouched, which also makes
    the function idempotent on its own output.

    Args:
        value: Any value; non-strings are rendered with ``str()``.
        fallback: Readable stem used when nothing survives sanitisation.

    Returns:
        A non-empty component of at most :data:`MAX_COMPONENT_LEN` characters.
    """
    raw = _as_text(value)
    base = _UNSAFE.sub("_", raw.lower()).lstrip(".")
    if base in ("", ".", ".."):
        stem = _UNSAFE.sub("_", _as_text(fallback).lower()).lstrip(".")
        base = stem if stem not in ("", ".", "..") else "x"
    if base == raw and len(base) <= MAX_COMPONENT_LEN:
        return base
    keep = MAX_COMPONENT_LEN - DISAMBIGUATOR_LEN - 1
    return f"{base[:keep]}-{_digest(raw)}"


def _split_segments(value: Any) -> list[str]:
    """Split a (possibly hostile) relative name into its raw, non-empty segments."""
    text = _as_text(value).replace("\\", "/")
    return [segment for segment in text.split("/") if segment]


def _contains(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` resolves to ``root`` or beneath it (Req 16.5).

    Both sides are fully resolved, so symlinked temp roots compare equal while a
    symlink *inside* the workspace pointing out of it is correctly rejected.
    """
    try:
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return False
    if resolved_candidate == resolved_root:
        return True
    return resolved_root in resolved_candidate.parents


@dataclass(frozen=True)
class Engine_Workspace:
    """Per-job, per-clip, per-engine scratch directory (Req 16).

    The identity fields are stored **sanitised** (:func:`sanitize_component` is
    idempotent, so passing them on to :func:`artifact_key` re-derives exactly the
    same components), and both paths are stored as :class:`~pathlib.Path`.
    """

    root: Path
    temp_dir: Path
    job_id: str
    clip_id: str
    engine_id: str
    options_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _as_path(self.root))
        object.__setattr__(self, "temp_dir", _as_path(self.temp_dir))
        for field_name in ("job_id", "clip_id", "engine_id", "options_digest"):
            object.__setattr__(self, field_name, _as_text(getattr(self, field_name)))

    def path(self, *parts: Any) -> Path:
        """Return a sanitised path inside :attr:`root` (Reqs 16.4, 16.5).

        Each part is split on ``/`` and ``\\`` and every segment is passed
        through :func:`sanitize_component`, so traversal payloads (``..``,
        ``/etc/passwd``, ``C:\\``, NUL bytes) become ordinary file names instead
        of escapes. The resolved result is then verified to live inside
        :attr:`root` and a ``ValueError`` is raised if it does not — the case
        sanitisation cannot cover, a symlink inside the workspace pointing out of
        it.

        With no parts (or no surviving segment) the workspace root is returned.
        """
        segments = [
            sanitize_component(segment, fallback="part")
            for part in parts
            for segment in _split_segments(part)
        ]
        candidate = self.root.joinpath(*segments) if segments else self.root
        if not _contains(self.root, candidate):
            raise ValueError(
                f"engine workspace path escapes its root {self.root}: {candidate}"
            )
        return candidate

    def artifact(
        self, name: Any, *, media_type: str = "data", durable: bool = False
    ) -> Engine_Artifact:
        """Declare an :class:`~worker.engines.base.Engine_Artifact` at ``self.path(name)``.

        The artifact's ``name`` is the sanitised workspace-relative POSIX path,
        so it matches the file actually written and is safe to feed to
        :func:`artifact_key` (Reqs 17.7, 18.1).
        """
        target = self.path(name)
        if target == self.root:
            # ``name`` carried no usable segment at all (empty, ``.``, ``..``).
            target = self.root / sanitize_component(name, fallback="artifact")
        try:
            relative = target.relative_to(self.root).as_posix()
        except ValueError:  # pragma: no cover - _contains already guarantees this
            relative = target.name
        return Engine_Artifact(
            name=relative,
            path=target,
            media_type=media_type,
            durable=durable,
        )

    def exists(self) -> bool:
        """True when the workspace directory is present on disk."""
        try:
            return self.root.is_dir()
        except OSError:  # pragma: no cover - unstattable path
            return False


def allocate_workspace(
    temp_dir: str | Path,
    job_id: Any,
    clip_id: Any,
    engine_id: Any,
    options_digest: Any,
    *,
    create: bool = True,
) -> Engine_Workspace:
    """Allocate ``<temp_dir>/engines/<job>/<clip>/<engine>__<digest>`` (Reqs 16.1-16.3).

    Every component is sanitised (16.6), the result is verified to resolve inside
    ``temp_dir`` (16.5), and the directory plus any missing parents are created
    when ``create`` (16.3). Because the leaf carries the Engine_Id *and* the
    Options_Digest, two engines on one clip — and two runs of one engine with
    different options — get different directories (16.7, 11.6).

    Args:
        temp_dir: The scratch directory ``run_pipeline`` was given.
        job_id: Job identifier (sanitised).
        clip_id: Clip identifier (sanitised).
        engine_id: Engine_Id (sanitised).
        options_digest: Options_Digest of the invocation (sanitised).
        create: Create the directory tree (default) or compute the path only.

    Returns:
        The :class:`Engine_Workspace` for this invocation.

    Raises:
        ValueError: The computed root would not resolve inside ``temp_dir``.
        OSError: The directory could not be created.
    """
    base = _as_path(temp_dir)
    job = sanitize_component(job_id, fallback="job")
    clip = sanitize_component(clip_id, fallback="clip")
    engine = sanitize_component(engine_id, fallback="engine")
    digest = sanitize_component(options_digest, fallback="nodigest")

    root = base / ENGINE_TEMP_ROOT / job / clip / f"{engine}__{digest}"
    if not _contains(base, root):  # pragma: no cover - sanitisation makes this unreachable
        raise ValueError(f"engine workspace {root} escapes temp_dir {base}")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return Engine_Workspace(
        root=root,
        temp_dir=base,
        job_id=job,
        clip_id=clip,
        engine_id=engine,
        options_digest=digest,
    )


def _default_remover(path: Path) -> None:
    """Remove a workspace directory tree, letting ``OSError`` surface to the caller."""
    shutil.rmtree(path)


def cleanup_workspace(
    ws: Engine_Workspace,
    *,
    remover: Optional[Callable[[Path], None]] = None,
    logger: Any | None = None,
) -> bool:
    """Delete ``ws.root``; log and swallow ``OSError`` (Reqs 17.1, 17.4, 17.5).

    The engine's status is irrelevant: an ``applied``, ``degraded``, ``failed``
    or timed-out engine's workspace is removed on exactly the same schedule
    (17.5). ``remover`` is the seam tests use to inject an ``OSError``.

    Args:
        ws: The workspace to delete (or any object exposing ``root``).
        remover: Callable removing the directory tree; defaults to ``rmtree``.
        logger: Logger-like object; defaults to this module's logger.

    Returns:
        ``True`` when the workspace is gone, ``False`` when removal failed (one
        warning has then been logged and processing continues).
    """
    root = _as_path(getattr(ws, "root", ws))
    remove = remover if remover is not None else _default_remover
    try:
        if root.exists() or root.is_symlink():
            remove(root)
    except OSError as exc:
        _log(logger, "engine workspace cleanup failed for %s: %s", root, exc)
        return False
    return True


def cleanup_job_workspaces(
    temp_dir: str | Path, job_id: Any, *, logger: Any | None = None
) -> int:
    """Delete ``<temp_dir>/engines/<job>`` (Reqs 17.1, 17.6).

    Returns the number of top-level entries (clip directories) removed beneath
    the job directory — ``0`` when the job directory is absent or already empty.
    The job directory itself is removed too. ``OSError`` is logged and swallowed
    (17.4).
    """
    target = _as_path(temp_dir) / ENGINE_TEMP_ROOT / sanitize_component(
        job_id, fallback="job"
    )
    if not target.exists():
        return 0

    removed = 0
    try:
        children = sorted(target.iterdir())
    except OSError as exc:
        _log(logger, "engine workspace listing failed for %s: %s", target, exc)
        children = []
    for child in children:
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except OSError as exc:
            _log(logger, "engine workspace cleanup failed for %s: %s", child, exc)
    try:
        shutil.rmtree(target, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - ignore_errors makes this unreachable
        _log(logger, "engine workspace cleanup failed for %s: %s", target, exc)
    return removed


def cleanup_job_artifacts(
    job_id: Any,
    *,
    temp_dir: str | Path | None = None,
    logger: Any | None = None,
) -> int:
    """Job-level engine cleanup honouring ``auto_delete_temp`` (Reqs 17.2, 17.3, 17.6).

    With the ``auto_delete_temp`` runtime setting **enabled**, the job's scratch
    space — engine workspaces included — is removed through the existing
    ``storage_backends.retention.cleanup_temp`` path, exactly like every other
    temp file (17.2); any workspace still left beneath an explicitly supplied
    ``temp_dir`` (a pipeline scratch directory outside ``settings.temp_dir``) is
    then removed directly, so no ``engines/<job_id>`` directory survives a
    completed job (17.6). With the setting **disabled** nothing is removed and
    the content waits for the ``RetentionSweeper`` sweep (17.3).

    ``runtime_config`` and ``storage_backends.retention`` are imported lazily
    (Req 1.4) and looked up as module attributes, so both are patchable.

    Args:
        job_id: The job whose scratch space should be released.
        temp_dir: The ``run_pipeline`` scratch directory, when it is known.
        logger: Logger-like object for swallowed ``OSError``s.

    Returns:
        The number of entries removed (``0`` when retention is disabled).
    """
    import runtime_config  # lazy (Req 1.4)

    try:
        auto_delete = bool(runtime_config.get_runtime_config().auto_delete_temp)
    except Exception as exc:  # noqa: BLE001 - a config read must never fail cleanup
        _log(logger, "runtime config unavailable, keeping engine workspaces: %s", exc)
        return 0
    if not auto_delete:
        return 0

    from storage_backends import retention  # lazy (Req 1.4)

    removed = 0
    try:
        removed += int(retention.cleanup_temp(_as_text(job_id)))
    except OSError as exc:
        _log(logger, "retention cleanup failed for job %s: %s", job_id, exc)
    if temp_dir is not None:
        removed += cleanup_job_workspaces(temp_dir, job_id, logger=logger)
    return removed


def artifact_key(job_id: Any, clip_id: Any, engine_id: Any, name: Any) -> str:
    """Return the durable storage key ``engines/<job>/<clip>/<engine>/<name>``.

    Every component is sanitised and the joined key is passed through
    ``storage_backends.base.normalize_key``, so the key has no leading slash and
    no ``""``/``.``/``..`` segment (Req 18.4), is a fixed point of
    ``normalize_key``, and is identical whichever Storage_Backend is active —
    the key depends on nothing but its arguments (Reqs 18.2, 18.3).
    """
    from storage_backends.base import normalize_key  # lazy (Req 1.4)

    segments = [
        ENGINE_KEY_ROOT,
        sanitize_component(job_id, fallback="job"),
        sanitize_component(clip_id, fallback="clip"),
        sanitize_component(engine_id, fallback="engine"),
        sanitize_component(name, fallback="artifact"),
    ]
    return normalize_key("/".join(segments))


def _engine_id_from_path(path: Path) -> str:
    """Recover the Engine_Id from a workspace path ``.../<engine>__<digest>/...``.

    The workspace leaf is identified by its position — it is the directory whose
    great-grandparent is the ``engines`` root (``engines/<job>/<clip>/<leaf>``) —
    so a nested artifact whose own directory name happens to contain ``"__"``
    cannot be mistaken for it. Positional identification failing (an artifact
    outside any workspace), the nearest ``<engine>__<digest>``-shaped ancestor is
    used, then the containing directory name.
    """
    for parent in path.parents:
        if parent.parent.parent.parent.name == ENGINE_TEMP_ROOT and "__" in parent.name:
            return parent.name.split("__", 1)[0]
    for parent in path.parents:
        if "__" in parent.name:
            return parent.name.split("__", 1)[0]
    return path.parent.name or "engine"


def persist_artifact(
    artifact: Engine_Artifact,
    *,
    job_id: Any,
    clip_id: Any,
    engine_id: Any = None,
    storage: "BaseStorage | None" = None,
) -> Engine_Artifact:
    """Persist one artifact through the active Storage_Backend (Reqs 18.1, 18.5).

    The file is stored with ``BaseStorage.save_file`` under
    :func:`artifact_key`, using ``storage`` when given and
    ``storage_backends.get_storage()`` otherwise (imported lazily, so a run with
    no durable artifact never touches the backend). The returned copy carries the
    key in ``storage_key`` (18.5).

    Errors are **not** caught: the Engine_Host turns a failure into a single
    ``engine:<engine_id>:artifact_failed`` marker and still produces the clip
    (Req 18.6).

    Args:
        artifact: The artifact to store; its ``path`` must exist.
        job_id: Job identifier for the key.
        clip_id: Clip identifier for the key.
        engine_id: Engine_Id for the key, used verbatim; recovered from the
            workspace path (``<engine>__<digest>``) when left as ``None``.
        storage: Backend override; defaults to the configured backend.

    Returns:
        A copy of ``artifact`` with ``storage_key`` set.
    """
    record = (
        artifact
        if isinstance(artifact, Engine_Artifact)
        else Engine_Artifact.from_dict(artifact)
    )
    # A supplied id is passed through verbatim (not even stripped):
    # :func:`artifact_key` owns every normalisation, so an explicit id always
    # produces exactly the key ``artifact_key(job_id, clip_id, engine_id, name)``
    # predicts. Only ``None`` triggers recovery from the workspace path.
    identifier = (
        _engine_id_from_path(record.path) if engine_id is None else _as_text(engine_id)
    )
    key = artifact_key(job_id, clip_id, identifier, record.name or record.path.name)

    backend = storage
    if backend is None:
        from storage_backends import get_storage  # lazy (Req 1.4)

        backend = get_storage()
    backend.save_file(key, record.path)
    return dataclasses.replace(record, storage_key=key)
