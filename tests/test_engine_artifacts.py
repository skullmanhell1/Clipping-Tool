"""Engine workspace / durable-artifact module for the av-engines-foundation spec
(``worker/engines/artifacts.py``).

Covers the design's numbered properties for the artifact layer:

* **P29** — workspace paths are contained, sanitised, and unique (task 7.4).
* **P30** — workspaces are always cleaned up, durable artifacts first (task 7.5).
* **P31** — durable artifact keys are safe and backend-neutral (task 7.6).
* **P32** — artifact persistence failure degrades, it does not fail the clip (task 7.7).

plus the retention-wiring unit tests (task 7.8), which spy
``storage_backends.retention.cleanup_temp`` and drive both settings of
``runtime_config.get_runtime_config().auto_delete_temp``.

Generators come from the shared ``tests/strategies.py`` module
(``st_hostile_component``, ``st_engine_outcomes``, ``st_engine_id``) and the doubles from
``tests/fakes.py`` (``RecordingStorage``, ``FakeS3Client``) — never redefined here — so
the sibling engine specs exercise the same input space and the same doubles.

``Engine_Host`` does not exist yet (task 9): the clip-lifecycle properties P30/P32 are
driven through :func:`drive_clip`, a deliberately dumb local stand-in that performs
exactly the sequence the host owes this layer — write artifacts, persist the durable
ones, *then* delete the workspace — so the contract host.py will implement is pinned
here at the artifacts layer.

Temp directories are created with :func:`tempfile.TemporaryDirectory` inside each
property body rather than through the function-scoped ``tmp_path`` fixture, because
hypothesis runs many examples inside a single test function and each example needs its
own clean Pipeline ``temp_dir``. ``@settings(deadline=None)`` is used for the same
reason: these properties touch the filesystem.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from config import settings as app_settings
from storage_backends.base import normalize_key
from storage_backends.local import LocalStorage
from storage_backends.s3 import S3Storage
from tests.fakes import FakeS3Client, RecordingStorage
from tests.strategies import st_engine_id, st_engine_outcomes, st_hostile_component
from worker.engines.artifacts import (
    ENGINE_KEY_ROOT,
    ENGINE_TEMP_ROOT,
    MAX_COMPONENT_LEN,
    Engine_Workspace,
    allocate_workspace,
    artifact_key,
    cleanup_job_artifacts,
    cleanup_job_workspaces,
    cleanup_workspace,
    persist_artifact,
    sanitize_component,
)
from worker.engines.base import Engine_Status

#: A well-formed Options_Digest (16 lowercase hex characters, as ``options_digest``
#: produces) used wherever the digest itself is not the input under test.
DIGEST = "0123456789abcdef"


# --------------------------------------------------------------------------- #
# Local doubles and helpers                                                    #
# --------------------------------------------------------------------------- #
class Recording_Logger:
    """Captures ``warning`` calls so "logs exactly once" can be asserted (Req 17.4)."""

    def __init__(self) -> None:
        self.warnings: List[Tuple[Any, ...]] = []

    def warning(self, message: Any, *args: Any) -> None:
        self.warnings.append((message, *args))


class Refusing_Remover:
    """A workspace remover that always raises ``OSError`` (the Req 17.4 injection)."""

    def __init__(self, exc: OSError | None = None) -> None:
        self.exc = exc or OSError("cannot remove workspace")
        self.calls: List[Path] = []

    def __call__(self, path: Path) -> None:
        self.calls.append(Path(path))
        raise self.exc


class Witness_Storage(RecordingStorage):
    """A :class:`~tests.fakes.RecordingStorage` that also witnesses *when* a save happened.

    Each ``save_file`` records ``(key, source_still_on_disk)``. A durable artifact
    persisted **before** its workspace was removed is necessarily still on disk at save
    time — the ordering P30 demands (Req 17.7).
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.witnessed: List[Tuple[str, bool]] = []

    def save_file(self, key, path):  # type: ignore[override]
        self.witnessed.append((key, Path(path).exists()))
        return super().save_file(key, path)


def write_artifact(artifact) -> Path:
    """Materialise ``artifact.path`` on disk with a little content."""
    artifact.path.parent.mkdir(parents=True, exist_ok=True)
    artifact.path.write_bytes(b"engine-artifact-bytes")
    return artifact.path


def drive_clip(
    temp_dir: Path,
    job_id: str,
    clip_id: str,
    outcomes,
    *,
    storage,
    remover=None,
    logger=None,
):
    """Stand in for ``Engine_Host.finish_clip`` at the artifacts layer.

    For every engine outcome: allocate a workspace, write the engine's artifacts into
    it, persist the durable ones (Req 17.7 — *before* any deletion), then delete the
    workspace whatever the engine's status was (Req 17.5). A persistence error becomes
    exactly one ``engine:<engine_id>:artifact_failed`` marker and never propagates
    (Req 18.6).

    Returns ``(markers, workspaces, persisted)``.
    """
    markers: List[str] = []
    workspaces: List[Engine_Workspace] = []
    persisted: List[Any] = []

    for outcome in outcomes:
        engine_id = outcome["engine_id"]
        ws = allocate_workspace(temp_dir, job_id, clip_id, engine_id, DIGEST)
        workspaces.append(ws)

        declared = [
            ws.artifact(item.name, media_type=item.media_type, durable=item.durable)
            for item in outcome["artifacts"]
        ]
        for artifact in declared:
            write_artifact(artifact)

        # A timed-out or crashed engine's artifacts are abandoned (Req 8.6); every
        # other engine's durable artifacts are persisted first (Req 17.7).
        abandoned = outcome["exception"] is not None or outcome["status"] in (
            Engine_Status.FAILED,
            Engine_Status.SKIPPED,
        )
        if not abandoned:
            failed = False
            for artifact in declared:
                if not artifact.durable:
                    continue
                try:
                    persisted.append(
                        persist_artifact(
                            artifact,
                            job_id=job_id,
                            clip_id=clip_id,
                            engine_id=ws.engine_id,
                            storage=storage,
                        )
                    )
                except Exception:  # noqa: BLE001 - Req 18.6: degrade, never fail
                    failed = True
            if failed:
                markers.append(f"engine:{ws.engine_id}:artifact_failed")

        cleanup_workspace(ws, remover=remover, logger=logger)

    return markers, workspaces, persisted


def assert_contained(root: Path, candidate: Path) -> None:
    """Assert ``candidate`` resolves to ``root`` or beneath it (Reqs 16.5, 16.6)."""
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    assert resolved == resolved_root or resolved_root in resolved.parents, (
        f"{candidate} escaped {root}"
    )


def assert_safe_component(component: str) -> None:
    """Assert a sanitised component can neither traverse nor absolutise nor vanish."""
    assert isinstance(component, str)
    assert component not in ("", ".", "..")
    assert "/" not in component and "\\" not in component
    assert "\x00" not in component
    assert 0 < len(component) <= MAX_COMPONENT_LEN
    assert component == component.lower()


# Feature: av-engines-foundation, Property 29: Workspace paths are contained, sanitised,
# and unique — *For any* job id, clip id, engine id, digest, and relative artifact name
# (including traversal payloads) the allocated workspace and every `ws.path(...)` result
# resolve inside the Pipeline `temp_dir`, the directory exists and is writable after
# allocation, the sanitised components all appear in the path, and distinct tuples map to
# distinct directories.
@settings(max_examples=100, deadline=None)
@given(
    job_id=st_hostile_component(),
    clip_id=st_hostile_component(),
    engine_id=st_hostile_component(),
    digest=st_hostile_component(),
    name=st_hostile_component(),
)
def test_p29_workspace_paths_are_contained_sanitised_and_unique(
    job_id, clip_id, engine_id, digest, name
):
    """Validates: Requirements 11.6, 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.7"""
    with tempfile.TemporaryDirectory(prefix="engine-ws-") as raw_temp:
        temp_dir = Path(raw_temp)
        ws = allocate_workspace(temp_dir, job_id, clip_id, engine_id, digest)

        # Sanitisation is total, safe, and idempotent on its own output.
        job = sanitize_component(job_id, fallback="job")
        clip = sanitize_component(clip_id, fallback="clip")
        engine = sanitize_component(engine_id, fallback="engine")
        options = sanitize_component(digest, fallback="nodigest")
        for component in (job, clip, engine, options):
            assert_safe_component(component)
            assert sanitize_component(component) == component

        # Containment: the workspace lives inside the Pipeline temp_dir (16.1, 16.5).
        assert_contained(temp_dir, ws.root)
        assert ws.temp_dir == temp_dir

        # The sanitised components all appear, in order, in the path (16.2, 16.6).
        assert ws.root.parts[-4:] == (ENGINE_TEMP_ROOT, job, clip, f"{engine}__{options}")
        assert (ws.job_id, ws.clip_id, ws.engine_id, ws.options_digest) == (
            job,
            clip,
            engine,
            options,
        )

        # The directory exists and is writable after allocation (16.3).
        assert ws.exists() and ws.root.is_dir()
        assert os.access(ws.root, os.W_OK)
        probe = ws.path("probe.bin")
        probe.write_bytes(b"ok")
        assert probe.read_bytes() == b"ok"

        # Every ws.path(...) stays inside the workspace — hostile names included (16.4).
        for parts in ((name,), (name, name), ("..", name), (f"../../{name}",), ()):
            target = ws.path(*parts)
            assert_contained(ws.root, target)
            assert_contained(temp_dir, target)
        artifact = ws.artifact(name, media_type="video", durable=True)
        assert_contained(ws.root, artifact.path)
        assert artifact.name and artifact.durable is True
        write_artifact(artifact)
        assert artifact.path.is_file()

        # Determinism, then distinctness: changing any one component of the tuple
        # yields a different directory (16.7, 11.6).
        again = allocate_workspace(temp_dir, job_id, clip_id, engine_id, digest)
        assert again.root == ws.root

        tuples = [
            (f"{job_id}_alt", clip_id, engine_id, digest),
            (job_id, f"{clip_id}_alt", engine_id, digest),
            (job_id, clip_id, f"{engine_id}_alt", digest),
            (job_id, clip_id, engine_id, f"{digest}_alt"),
        ]
        roots = {ws.root} | {
            allocate_workspace(temp_dir, *values, create=False).root for values in tuples
        }
        assert len(roots) == len(tuples) + 1


# Feature: av-engines-foundation, Property 30: Workspaces are always cleaned up, durable
# artifacts first — *For any* set of engines with any statuses (`applied`, `degraded`,
# `failed`, timeout) no workspace for that clip remains after cleanup and no
# `engines/<job_id>` directory remains after job cleanup; a `RecordingStorage` shows every
# durable artifact saved *before* its workspace was removed; and an injected `OSError`
# remover returns normally, logs once, and later clips still process.
@settings(max_examples=50, deadline=None)
@given(
    outcomes=st.lists(st_engine_outcomes(max_artifacts=2), min_size=1, max_size=3),
    job_id=st_engine_id(max_words=1),
)
def test_p30_workspaces_are_always_cleaned_up_durable_artifacts_first(outcomes, job_id):
    """Validates: Requirements 17.1, 17.4, 17.5, 17.6, 17.7"""
    with tempfile.TemporaryDirectory(prefix="engine-ws-") as raw_temp:
        temp_dir = Path(raw_temp)
        storage = Witness_Storage()

        markers, workspaces, persisted = drive_clip(
            temp_dir, job_id, "clip_a", outcomes, storage=storage
        )

        # Every workspace of the clip is gone, whatever the engine's status was
        # (Reqs 17.1, 17.5) — and a healthy storage never degrades (Req 18.6).
        for ws in workspaces:
            assert not ws.exists()
            assert not ws.root.exists()
        assert markers == []

        # Every durable artifact was saved while its workspace still existed (17.7).
        expected_saves = sum(
            1
            for outcome in outcomes
            for artifact in outcome["artifacts"]
            if artifact.durable
            and outcome["exception"] is None
            and outcome["status"] not in (Engine_Status.FAILED, Engine_Status.SKIPPED)
        )
        assert len(storage.witnessed) == expected_saves == len(persisted)
        assert all(present for _key, present in storage.witnessed)
        assert [record.storage_key for record in persisted] == [
            key for key, _present in storage.witnessed
        ]

        # Job-level cleanup leaves no engines/<job_id> directory behind (17.1, 17.6).
        job_root = temp_dir / ENGINE_TEMP_ROOT / sanitize_component(job_id)
        cleanup_job_workspaces(temp_dir, job_id)
        assert not job_root.exists()

        # An OSError from the remover is logged once, returns normally, and later
        # clips still process (17.4).
        remover = Refusing_Remover()
        logger = Recording_Logger()
        _markers, blocked, _persisted = drive_clip(
            temp_dir,
            job_id,
            "clip_b",
            outcomes,
            storage=storage,
            remover=remover,
            logger=logger,
        )
        assert len(logger.warnings) == len(blocked)
        assert all(ws.exists() for ws in blocked)

        _markers, later, _persisted = drive_clip(
            temp_dir, job_id, "clip_c", outcomes, storage=storage, logger=logger
        )
        assert all(not ws.exists() for ws in later)
        assert len(logger.warnings) == len(blocked)      # no new warning

        # And the whole job still cleans up, blocked workspaces included (17.6).
        cleanup_job_workspaces(temp_dir, job_id)
        assert not job_root.exists()


# Feature: av-engines-foundation, Property 31: Durable artifact keys are safe and
# backend-neutral — *For any* job id, clip id, engine id, and artifact name (including
# hostile values), `artifact_key` output is a fixed point of `normalize_key`, has no
# empty/`.`/`..` segment and no leading slash, is identical for a local backend and a
# fake S3 backend, and is recorded on the returned artifact record.
@settings(max_examples=100, deadline=None)
@given(
    job_id=st_hostile_component(),
    clip_id=st_hostile_component(),
    engine_id=st_hostile_component(),
    name=st_hostile_component(),
)
def test_p31_durable_artifact_keys_are_safe_and_backend_neutral(
    job_id, clip_id, engine_id, name
):
    """Validates: Requirements 18.1, 18.2, 18.3, 18.4, 18.5"""
    key = artifact_key(job_id, clip_id, engine_id, name)

    # Key safety: a fixed point of normalize_key with five safe segments (18.2, 18.4).
    assert normalize_key(key) == key
    assert not key.startswith("/")
    segments = key.split("/")
    assert len(segments) == 5
    assert segments[0] == ENGINE_KEY_ROOT
    for segment in segments:
        assert_safe_component(segment)

    with tempfile.TemporaryDirectory(prefix="engine-ws-") as raw_temp:
        temp_dir = Path(raw_temp)
        ws = allocate_workspace(temp_dir, job_id, clip_id, engine_id, DIGEST)
        artifact = ws.artifact(name, media_type="video", durable=True)
        write_artifact(artifact)

        # Backend neutrality: the local backend and a fake S3 backend produce the
        # same key, and it is recorded on the returned record (18.1, 18.3, 18.5).
        local_root = temp_dir / "local-storage"
        local = LocalStorage(root=local_root)
        s3 = S3Storage(client=FakeS3Client(), bucket="bkt")

        stored_local = persist_artifact(
            artifact, job_id=job_id, clip_id=clip_id, engine_id=engine_id, storage=local
        )
        stored_s3 = persist_artifact(
            artifact, job_id=job_id, clip_id=clip_id, engine_id=engine_id, storage=s3
        )

        expected = artifact_key(job_id, clip_id, engine_id, artifact.name)
        assert stored_local.storage_key == stored_s3.storage_key == expected
        assert normalize_key(expected) == expected
        # Both backends really hold the bytes under that key.
        assert (local_root / expected).is_file()
        assert s3.client.objects[("bkt", expected)] == artifact.path.read_bytes()
        # The original record is untouched; only the copy carries the key (18.5).
        assert artifact.storage_key == ""


# Feature: av-engines-foundation, Property 32: Artifact persistence failure degrades, it
# does not fail the clip — *For any* storage raising on `save_file`, exactly one
# `engine:<id>:artifact_failed` marker is recorded, the clip is still produced, and the
# workspace is still cleaned up.
@settings(max_examples=50, deadline=None)
@given(
    engine_id=st_engine_id(max_words=1),
    name=st_engine_id(max_words=1),
    exc_type=st.sampled_from([OSError, PermissionError, RuntimeError, ValueError]),
    durable_count=st.integers(min_value=1, max_value=3),
)
def test_p32_artifact_persistence_failure_degrades_not_fails(
    engine_id, name, exc_type, durable_count
):
    """Validates: Requirements 18.6"""
    with tempfile.TemporaryDirectory(prefix="engine-ws-") as raw_temp:
        temp_dir = Path(raw_temp)
        clip_path = temp_dir / "clip_a.mp4"
        clip_path.write_bytes(b"finished-clip")          # the clip is already produced

        names = [f"{name}_{index}.bin" for index in range(durable_count)]
        keys = {artifact_key("job_x", "clip_a", engine_id, item) for item in names}
        storage = RecordingStorage(fail_on=keys, exc=exc_type("storage refused"))

        outcome = {
            "engine_id": engine_id,
            "status": Engine_Status.APPLIED,
            "artifacts": tuple(
                type(
                    "Declared",
                    (),
                    {"name": item, "media_type": "data", "durable": True},
                )()
                for item in names
            ),
            "exception": None,
        }

        markers, workspaces, persisted = drive_clip(
            temp_dir, "job_x", "clip_a", [outcome], storage=storage
        )

        # Exactly one marker for the engine, however many artifacts failed (18.6).
        assert markers == [f"engine:{engine_id}:artifact_failed"]
        assert persisted == []
        # Every failing key was actually attempted.
        assert sorted(storage.saved_keys) == sorted(keys)
        # The clip survives and the workspace is still cleaned up (17.1, 18.6).
        assert clip_path.is_file()
        assert all(not ws.exists() for ws in workspaces)


# --------------------------------------------------------------------------- #
# Task 7.8 — retention wiring under ``auto_delete_temp`` (Reqs 17.2, 17.3)     #
# --------------------------------------------------------------------------- #
class Stub_Runtime_Config:
    """Minimal ``RuntimeConfig`` stand-in exposing just ``auto_delete_temp``."""

    def __init__(self, auto_delete_temp: bool) -> None:
        self.auto_delete_temp = auto_delete_temp


@pytest.fixture
def job_workspaces(tmp_path):
    """A job scratch dir beneath ``settings.temp_dir`` holding two engine workspaces."""
    job_id = f"job_{tmp_path.name}"
    job_temp = Path(app_settings.temp_dir) / job_id
    for engine_id in ("stem_separation", "kinetic_typography"):
        ws = allocate_workspace(job_temp, job_id, "clip_a", engine_id, DIGEST)
        write_artifact(ws.artifact("scratch.bin"))
    yield job_id, job_temp
    if job_temp.exists():
        import shutil

        shutil.rmtree(job_temp, ignore_errors=True)


def test_job_cleanup_routes_through_retention_when_auto_delete_enabled(
    monkeypatch, job_workspaces
):
    """Validates: Requirements 17.2, 17.6 — job cleanup goes through ``cleanup_temp``."""
    from storage_backends import retention

    job_id, job_temp = job_workspaces
    engines_root = job_temp / ENGINE_TEMP_ROOT / job_id
    assert engines_root.is_dir()

    calls: List[str] = []
    real_cleanup_temp = retention.cleanup_temp

    def spy(target=None):
        calls.append(target)
        return real_cleanup_temp(target)

    monkeypatch.setattr(retention, "cleanup_temp", spy)
    monkeypatch.setattr(
        "runtime_config.get_runtime_config", lambda: Stub_Runtime_Config(True)
    )

    removed = cleanup_job_artifacts(job_id, temp_dir=job_temp)

    assert calls == [job_id]                     # routed through the retention path
    assert removed >= 1
    assert not engines_root.exists()             # no workspace survives (17.6)
    assert not job_temp.exists()


def test_job_workspaces_survive_when_auto_delete_disabled(monkeypatch, job_workspaces):
    """Validates: Requirements 17.3 — disabled retention waits for the sweeper."""
    from storage_backends import retention

    job_id, job_temp = job_workspaces
    engines_root = job_temp / ENGINE_TEMP_ROOT / job_id

    calls: List[str] = []
    monkeypatch.setattr(retention, "cleanup_temp", lambda target=None: calls.append(target))
    monkeypatch.setattr(
        "runtime_config.get_runtime_config", lambda: Stub_Runtime_Config(False)
    )

    removed = cleanup_job_artifacts(job_id, temp_dir=job_temp)

    assert removed == 0
    assert calls == []                           # nothing removed, nothing routed
    assert engines_root.is_dir()
    assert sorted(path.name for path in engines_root.rglob("scratch.bin")) == [
        "scratch.bin",
        "scratch.bin",
    ]


# --------------------------------------------------------------------------- #
# Focused unit tests                                                           #
# --------------------------------------------------------------------------- #
def test_sanitize_component_rules():
    """Validates: Requirements 16.6, 18.4 — the documented sanitisation rules."""
    # Already-safe short values pass through untouched.
    assert sanitize_component("stem_separation") == "stem_separation"
    assert sanitize_component("0123456789abcdef") == "0123456789abcdef"
    assert sanitize_component("clip-a.mp4") == "clip-a.mp4"

    # Lossy inputs are made safe and disambiguated, never dropped or merged.
    for hostile in ("", ".", "..", "/", "../../etc/passwd", "a" * 512, "🎬", "UPPER"):
        component = sanitize_component(hostile)
        assert_safe_component(component)
        assert sanitize_component(component) == component
    assert sanitize_component("", fallback="artifact").startswith("artifact-")
    assert sanitize_component("..") != sanitize_component(".")
    assert sanitize_component("a" * 512) != sanitize_component("a" * 513)


def test_workspace_path_rejects_a_symlink_escape(tmp_path):
    """Validates: Requirements 16.4, 16.5 — an escaping resolution raises ``ValueError``."""
    ws = allocate_workspace(tmp_path / "temp", "job_x", "clip_a", "demo", DIGEST)
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws.root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        ws.path("escape", "secret.txt")


def test_persist_artifact_recovers_the_engine_id_from_the_workspace_path(tmp_path):
    """Validates: Requirements 18.2, 18.5 — the key names the engine that produced it."""
    ws = allocate_workspace(tmp_path, "job_x", "clip_a", "stem_separation", DIGEST)
    artifact = ws.artifact("vocals/stem.wav", media_type="audio", durable=True)
    write_artifact(artifact)
    storage = RecordingStorage()

    stored = persist_artifact(artifact, job_id="job_x", clip_id="clip_a", storage=storage)

    assert stored.storage_key == artifact_key(
        "job_x", "clip_a", "stem_separation", "vocals/stem.wav"
    )
    assert stored.storage_key.startswith("engines/job_x/clip_a/stem_separation/")
    assert stored.storage_key.split("/")[3] == "stem_separation"
    assert storage.saved_keys == [stored.storage_key]


def test_cleanup_helpers_are_total_on_missing_paths(tmp_path):
    """Validates: Requirements 17.1, 17.4 — cleanup of absent paths is a quiet no-op."""
    ws = allocate_workspace(tmp_path, "job_x", "clip_a", "demo", DIGEST, create=False)
    assert not ws.exists()
    assert cleanup_workspace(ws) is True
    assert cleanup_job_workspaces(tmp_path, "job_x") == 0

    # Two clips, then one job-level sweep removes both.
    for clip_id in ("clip_a", "clip_b"):
        allocate_workspace(tmp_path, "job_x", clip_id, "demo", DIGEST)
    assert cleanup_job_workspaces(tmp_path, "job_x") == 2
    assert not (tmp_path / ENGINE_TEMP_ROOT / "job_x").exists()
