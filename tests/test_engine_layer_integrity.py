"""Integrity pins for ``worker/engines/`` — the engine layer's honesty contract.

Every test here pins a defect where the engine layer *reported* something other than
what it did: media adopted without existing, a durable artifact persisted empty, a
workspace deleted underneath a thread still writing into it, a stem retained at a
path never written, a marker claiming ownership of captions it had just given up, an
exit status that was never checked, and a fallback note naming no rejected value.

They are deliberately grouped by *claim* rather than by module, because that is what
they have in common: in each case the clip record, the artifact list or the
Engine_Result said something a reader would reasonably believe and that was not true.

Doubles come from ``tests/fakes.py``. The only real subprocess-free collaborators
built locally are the two engines that need behaviour ``FakeEngine`` cannot express:
one that blocks a real OS thread (to reach the wall-clock watchdog rather than the
injected-clock deadline) and one that reports media it never wrote.
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import CountingProber, FakeEngine, RecordingStorage, StaticProber
from worker.engines import kinetic as kinetic_mod
from worker.engines import stems as stems_mod
from worker.engines.artifacts import allocate_workspace, persist_artifact
from worker.engines.base import (
    AV_Engine,
    Engine_Artifact,
    Engine_Result,
    Engine_Stage,
    Engine_Status,
)
from worker.engines.capabilities import Capability_Report, reset_report
from worker.engines.host import ORPHAN_GRACE_S, Engine_Host
from worker.engines.registry import Engine_Registry, reset_registry
from worker.engines.timebase import DEFAULT_FPS, Time_Base

JOB_ID = "job_engine_integrity"
DIGEST = "d" * 16


@pytest.fixture(autouse=True)
def _isolate_engine_globals():
    """Clear the process-wide registry and Capability_Report around every test."""
    reset_registry()
    reset_report()
    yield
    reset_registry()
    reset_report()


@dataclasses.dataclass
class _FakeMediaInfo:
    """Stand-in for ``worker.ffmpeg_utils.MediaInfo``; only ``fps`` is read."""

    duration: float = 6.0
    width: int = 1080
    height: int = 1920
    fps: Any = 30.0
    has_audio: bool = True


def _options(*engine_ids: str, **extra: Any) -> dict[str, Any]:
    """A Processing_Options mapping enabling ``engine_ids``."""
    options: dict[str, Any] = {"permissibility_mode": False}
    for engine_id in engine_ids:
        options[f"{engine_id}_enabled"] = True
    options.update(extra)
    return options


def _host(temp_dir: Path, engines: list[Any], options: Any, **kwargs: Any) -> Engine_Host:
    registry = Engine_Registry()
    for engine in engines:
        registry.register(engine)
    kwargs.setdefault("capabilities", Capability_Report(StaticProber({}, default=True)))
    return Engine_Host(options, job_id=JOB_ID, temp_dir=temp_dir, registry=registry, **kwargs)


def _workspace_dirs(temp_dir: Path) -> list[str]:
    root = Path(temp_dir) / "engines" / JOB_ID
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.glob("*/*") if path.is_dir())


# --------------------------------------------------------------------------- #
# Claim 1: "here is the replacement media"                                     #
# --------------------------------------------------------------------------- #
class _GhostMediaEngine(AV_Engine):
    """An engine that reports media at a path it never wrote.

    This is what an ffmpeg call that exits 0 and produces nothing looks like from the
    host's side: a well-formed ``applied`` result naming a file that is not there.

    A real ``AV_Engine`` subclass so the host's gating ladder (``is_enabled`` →
    ``flag_field``) is the real one; ``flag_field`` is overridden as an instance
    method because the id is per-instance here, not a ClassVar.
    """

    def __init__(self, engine_id: str, media: Path, *, stage=Engine_Stage.AUDIO):
        self.engine_id = engine_id
        self.stage = stage
        self.priority = 100
        self.produces_media = True
        self.media = media
        self.required_capabilities = ()
        self.optional_capabilities = ()
        self.requires_network = False
        self.time_budget_s = 30.0

    def flag_field(self) -> str:
        return f"{self.engine_id}_enabled"

    def resolve_options(self, options):
        return options

    def plan(self, ctx):
        return {}

    def run(self, ctx) -> Engine_Result:
        return Engine_Result(
            engine_id=self.engine_id, status=Engine_Status.APPLIED, media=self.media
        )


@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_media_that_does_not_exist_is_refused_and_the_engine_is_named(tmp_path, kind):
    """An engine cannot replace the clip with a file it did not write.

    ``Stage_Outcome.media`` is fed straight into the next stage by the Pipeline
    (``raw = out.media or raw``). Adopting a missing or zero-byte path means the
    failure surfaces stages later, inside whatever tries to decode it, and is
    attributed to the wrong engine — with no marker naming the one that caused it.

    Both halves are asserted: the media is refused (the clip keeps the preceding
    stage's file) *and* the responsible engine is named on the clip record.
    """
    ghost = tmp_path / "ghost.mp4"
    if kind == "empty":
        ghost.write_bytes(b"")  # exists, but carries nothing

    engine = _GhostMediaEngine("ghost_engine", ghost)
    host = _host(tmp_path, [engine], _options("ghost_engine"))
    outcome = host.run_stage(
        Engine_Stage.AUDIO,
        clip_id="clip_a",
        source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=6.0,
        duration=6.0,
    )

    assert outcome.media is None
    result = outcome.result_for("ghost_engine")
    assert result is not None
    assert result.media is None
    assert "engine:ghost_engine:media_missing" in outcome.markers
    host.finish_clip("clip_a")


def test_real_media_is_still_adopted(tmp_path):
    """The guard is a existence/size check, not a blanket refusal.

    Without this, "media is never adopted" would pass the test above and silently
    disable every media-producing engine in the project.
    """
    real = tmp_path / "real.mp4"
    real.write_bytes(b"real-media-bytes")

    engine = _GhostMediaEngine("real_engine", real)
    host = _host(tmp_path, [engine], _options("real_engine"))
    outcome = host.run_stage(
        Engine_Stage.AUDIO,
        clip_id="clip_a",
        source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=6.0,
        duration=6.0,
    )

    assert outcome.media == real
    assert not [m for m in outcome.markers if m.endswith(":media_missing")]
    host.finish_clip("clip_a")


# --------------------------------------------------------------------------- #
# Claim 2: "this durable artifact is persisted"                                #
# --------------------------------------------------------------------------- #
def test_an_empty_durable_artifact_is_not_reported_as_persisted(tmp_path):
    """A 0-byte file stores cleanly and would hand back a key naming nothing.

    A *missing* artifact already fails honestly. An empty one did not: ``save_file``
    accepted it and ``persist_artifact`` returned a ``storage_key``, so the
    Engine_Result claimed a durable artifact that could not be read back.
    """
    workspace = allocate_workspace(tmp_path, JOB_ID, "clip_a", "some_engine", DIGEST)
    empty = workspace.path("empty.bin")
    empty.write_bytes(b"")
    artifact = workspace.artifact("empty.bin", media_type="data", durable=True)
    storage = RecordingStorage()

    with pytest.raises(ValueError, match="empty"):
        persist_artifact(
            artifact, job_id=JOB_ID, clip_id="clip_a", engine_id="some_engine", storage=storage
        )
    assert storage.saved_keys == []  # nothing was stored


def test_an_empty_durable_artifact_reaches_the_clip_as_artifact_failed(tmp_path):
    """End-to-end: the host turns the refusal into the documented marker (Req 18.6)."""
    empty_dir = tmp_path / "staging"
    empty_dir.mkdir()
    empty_file = empty_dir / "empty.bin"
    empty_file.write_bytes(b"")
    artifact = Engine_Artifact(name="empty.bin", path=empty_file, media_type="data", durable=True)

    engine = FakeEngine("empty_artifact_engine", Engine_Stage.AUDIO, artifacts=(artifact,))
    storage = RecordingStorage()
    host = _host(tmp_path, [engine], _options("empty_artifact_engine"), storage=storage)
    host.run_stage(
        Engine_Stage.AUDIO,
        clip_id="clip_a",
        source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=6.0,
        duration=6.0,
    )
    markers = host.finish_clip("clip_a")

    assert markers == ["engine:empty_artifact_engine:artifact_failed"]
    assert storage.saved_keys == []


# --------------------------------------------------------------------------- #
# Claim 3: "this engine's workspace is finished with"                          #
# --------------------------------------------------------------------------- #
class _WedgedEngine(AV_Engine):
    """An engine that blocks a **real** OS thread until released.

    ``SlowEngine`` overruns against the injected clock, which exercises the
    deadline check. Reaching the *wall-clock watchdog* — the path that abandons a
    thread it cannot stop — needs a real block, so this engine waits on an
    ``Event`` and then writes into its workspace, which is precisely the write that
    used to race ``rmtree``.
    """

    def __init__(self, engine_id: str, release: threading.Event):
        self.engine_id = engine_id
        self.stage = Engine_Stage.AUDIO
        self.priority = 100
        self.produces_media = False
        self.required_capabilities = ()
        self.optional_capabilities = ()
        self.requires_network = False
        # Below MIN_WALL_TIMEOUT_S, so the watchdog waits the floor (1s) and fires.
        self.time_budget_s = 0.01
        self._release = release
        self.finished = threading.Event()
        self.late_write: Path | None = None
        self.write_error: BaseException | None = None

    def flag_field(self) -> str:
        return f"{self.engine_id}_enabled"

    def resolve_options(self, options):
        return options

    def plan(self, ctx):
        return {}

    def run(self, ctx) -> Engine_Result:
        try:
            self._release.wait(timeout=30.0)
            target = ctx.workspace.path("written_after_abandonment.txt")
            target.write_text("the orphan was still working", encoding="utf-8")
            self.late_write = target
        except BaseException as exc:  # recorded, then surfaced by the test
            self.write_error = exc
        finally:
            self.finished.set()
        return Engine_Result(engine_id=self.engine_id, status=Engine_Status.APPLIED)


def test_an_abandoned_engines_workspace_is_not_deleted_underneath_it(tmp_path):
    """The watchdog abandons a thread it cannot stop; cleanup must not race it.

    ``Future.cancel()`` is a no-op once the task has started and the host holds no
    handle on whatever subprocess the engine is blocked in, so a timed-out engine
    keeps running with its workspace open. ``finish_clip`` used to ``rmtree`` that
    directory immediately, and the resulting ``FileNotFoundError`` was swallowed as
    a routine cleanup failure — so the corruption was invisible.

    With ``orphan_grace_s=0`` the thread is still running when cleanup runs, so the
    workspace must survive, the clip must say so, and the engine's later write must
    land successfully rather than into a deleted tree.
    """
    release = threading.Event()
    engine = _WedgedEngine("wedged_engine", release)
    host = _host(tmp_path, [engine], _options("wedged_engine"), orphan_grace_s=0.0)
    outcome = host.run_stage(
        Engine_Stage.AUDIO,
        clip_id="clip_a",
        source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=6.0,
        duration=6.0,
    )

    result = outcome.result_for("wedged_engine")
    assert result is not None
    assert result.status is Engine_Status.FAILED
    assert result.markers == ("engine:wedged_engine:timeout",)

    # The thread is still blocked, so its workspace must be left alone and the
    # retention recorded rather than silently leaked.
    markers = host.finish_clip("clip_a")
    assert markers == ["engine:wedged_engine:workspace_retained"]
    assert _workspace_dirs(tmp_path) != []

    # Now let the orphan finish. Its write must succeed: the directory is still there.
    release.set()
    assert engine.finished.wait(timeout=30.0)
    assert engine.write_error is None, engine.write_error
    assert engine.late_write is not None
    assert engine.late_write.is_file()
    assert engine.late_write.read_text(encoding="utf-8") == "the orphan was still working"


def test_an_abandoned_engine_that_finishes_within_the_grace_is_cleaned_up_normally(tmp_path):
    """The grace period is a wait, not a leak: a thread that returns loses its workspace.

    Retaining every abandoned engine's workspace unconditionally would turn a race
    into a disk leak. The common case — an engine a fraction of a second past its
    budget — must still be reclaimed on the normal schedule (Req 17.5), with no
    ``workspace_retained`` marker.
    """
    release = threading.Event()
    engine = _WedgedEngine("prompt_engine", release)
    host = _host(
        tmp_path, [engine], _options("prompt_engine"), orphan_grace_s=max(ORPHAN_GRACE_S, 10.0)
    )
    host.run_stage(
        Engine_Stage.AUDIO,
        clip_id="clip_a",
        source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=6.0,
        duration=6.0,
    )

    # Released *before* cleanup, so the thread returns inside the grace window.
    release.set()
    markers = host.finish_clip("clip_a")

    assert engine.finished.wait(timeout=30.0)
    assert markers == []
    assert _workspace_dirs(tmp_path) == []


# --------------------------------------------------------------------------- #
# Claim 4: "this stem was retained"                                            #
# --------------------------------------------------------------------------- #
def _stem_engine():
    return stems_mod.Stem_Inpainting_Engine()


def test_a_retained_stem_is_declared_at_the_path_that_was_written(tmp_path):
    """Under ``spectral`` the retained ``music`` stem must be the **bridged** one.

    ``stem_set["music"]`` is ``stems/music_bridged.wav`` after bridging — the file
    that was actually mixed into the delivered audio. Declaring ``stems/music.wav``
    retained the pre-bridge stem (which does not correspond to the delivered clip)
    and, because ``_reclaim`` keeps only what was declared, deleted the bridged file
    that did.
    """
    workspace = allocate_workspace(tmp_path, JOB_ID, "clip_a", stems_mod.ENGINE_ID, DIGEST)
    bridged = workspace.path("stems", "music_bridged.wav")
    plain = workspace.path("stems", "music.wav")
    vocals = workspace.path("stems", "vocals.wav")
    candidate = workspace.path("clip_repaired.mp4")
    for path in (bridged, plain, vocals, candidate):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")

    stem_set = {"music": bridged, "vocals": vocals}
    artifacts = _stem_engine()._declare_artifacts(
        workspace,
        candidate,
        stem_set,
        stems_mod.Stem_Options(retain_stems=True),
        separated=True,
    )

    durable = sorted(Path(item.path) for item in artifacts if item.durable)
    assert durable == sorted([bridged, vocals])
    assert plain not in durable

    # And the reclaim pass therefore keeps the bridged stem, not the plain one.
    keep = {Path(str(candidate)), *(Path(str(item.path)) for item in artifacts if item.durable)}
    _stem_engine()._reclaim([bridged, plain, vocals, candidate], keep=keep)
    assert bridged.is_file()
    assert vocals.is_file()
    assert not plain.exists()


def test_retain_stems_declares_nothing_when_no_separation_ran(tmp_path):
    """On the repair-only path there are no stems, and the engine must say so.

    ``stem_set`` there maps ``vocals`` to ``in.wav`` — the *unseparated* extraction.
    Declaring a durable ``stems/vocals.wav`` named a file that is never written, so
    ``_reclaim`` kept the nonexistent path, deleted ``in.wav``, and every such clip
    earned an ``artifact_failed`` marker: a path bug misreported as a
    storage-backend fault. Nothing is declared now, and the caller records
    ``retain_stems_unavailable`` so the unhonoured request is still visible.
    """
    workspace = allocate_workspace(tmp_path, JOB_ID, "clip_a", stems_mod.ENGINE_ID, DIGEST)
    extracted = workspace.path("in.wav")
    candidate = workspace.path("clip_repaired.mp4")
    for path in (extracted, candidate):
        path.write_bytes(b"audio")

    artifacts = _stem_engine()._declare_artifacts(
        workspace,
        candidate,
        {"vocals": extracted},
        stems_mod.Stem_Options(retain_stems=True),
        separated=False,
    )

    assert [item for item in artifacts if item.durable] == []
    # The one media artifact is still declared, and it exists.
    assert len(artifacts) == 1
    assert Path(artifacts[0].path).is_file()
    # Nothing declared at a path that was never written.
    for item in artifacts:
        assert Path(item.path).exists()


def test_a_stem_outside_the_workspace_is_skipped_rather_than_mis_declared(tmp_path):
    """A path outside the workspace cannot be declared as a workspace-relative name.

    ``Engine_Workspace.artifact`` resolves its argument against the workspace root,
    so handing it an absolute outside path would declare — and persist — a different
    file than the one in ``stem_set``.
    """
    workspace = allocate_workspace(tmp_path, JOB_ID, "clip_a", stems_mod.ENGINE_ID, DIGEST)
    outside = tmp_path / "elsewhere" / "vocals.wav"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"audio")
    candidate = workspace.path("clip_repaired.mp4")
    candidate.write_bytes(b"video")

    artifacts = _stem_engine()._declare_artifacts(
        workspace,
        candidate,
        {"vocals": outside},
        stems_mod.Stem_Options(retain_stems=True),
        separated=True,
    )
    assert [item for item in artifacts if item.durable] == []


# --------------------------------------------------------------------------- #
# Claim 5: "the command succeeded"                                             #
# --------------------------------------------------------------------------- #
class _Completed:
    def __init__(self, **fields: Any):
        for key, value in fields.items():
            setattr(self, key, value)


@pytest.mark.parametrize(
    "completed, label",
    [
        (_Completed(stderr="boom"), "no returncode attribute at all"),
        (_Completed(returncode=None, stderr="boom"), "returncode None (Popen: still running)"),
    ],
)
def test_stems_run_refuses_an_unreported_exit_status(completed, label):
    """``Command_Runner`` is a documented seam, so the exit status must be reported.

    Defaulting a missing ``returncode`` to ``0`` read "this object has no exit
    status" as "the process succeeded", and ``None`` — which on a ``Popen`` means
    *still running* — was explicitly allowed through. Either way the engine went on
    to use output that no process had vouched for.
    """
    from worker.ffmpeg_utils import FFmpegError

    with pytest.raises(FFmpegError):
        stems_mod._run(lambda argv, budget: completed, ["ffmpeg", "-i", "in.wav"], 5.0)


def test_stems_run_still_accepts_a_real_success():
    """Exit code 0 is success — the guard must not reject every command."""
    completed = _Completed(returncode=0, stderr="")
    assert (
        stems_mod._run(lambda argv, budget: completed, ["ffmpeg", "-i", "in.wav"], 5.0) is completed
    )


# --------------------------------------------------------------------------- #
# Claim 6: "this engine owns the captions"                                     #
# --------------------------------------------------------------------------- #
class _KineticOptions:
    """Processing_Options stand-in driving the kinetic engine (see its ``parse``)."""

    def __init__(self, *, font: str = "", durable: bool = False):
        self.kinetic_typography_enabled = True
        self.captions = True
        self.hook_title = False
        self.permissibility_mode = False
        self.caption_preset = "karaoke"
        self.durable_subtitle = durable
        self.kinetic_font = font


def _kinetic_words():
    from worker.transcribe import Word

    return (
        Word(start=0.0, end=0.4, text="hello", probability=0.9),
        Word(start=0.45, end=0.9, text="there", probability=0.9),
    )


def _run_kinetic(tmp_path, *, font: str, available_fonts: bool, durable: bool = True):
    engine = kinetic_mod.Kinetic_Typography_Engine()
    registry = Engine_Registry()
    registry.register(engine)
    # Every non-font capability is granted; fonts are granted or refused wholesale.
    prober = CountingProber(
        StaticProber({f"font:{font}": available_fonts} if font else {}, default=True)
    )
    host = Engine_Host(
        _KineticOptions(font=font, durable=durable),
        job_id=JOB_ID,
        temp_dir=tmp_path,
        registry=registry,
        capabilities=Capability_Report(prober),
        storage=RecordingStorage(),
    )
    outcome = host.run_stage(
        Engine_Stage.COMPOSE,
        clip_id="clip_a",
        source=tmp_path / "source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=2.0,
        duration=2.0,
        words=_kinetic_words(),
    )
    return host, outcome.results[0]


def test_kinetic_does_not_claim_to_supersede_captions_when_it_gave_up_its_slot(tmp_path):
    """A degraded kinetic run hands the clip back to the v0.8.0 caption path.

    The compositor decides caption ownership from the *contribution*, not the
    marker, so the fallback was already correct — but the clip record carried
    ``engine:kinetic_typography:supersedes_captions`` anyway, stating the opposite
    of what rendered. Font substitution alone degrades, which makes this the common
    case rather than an edge one.
    """
    _host_obj, result = _run_kinetic(tmp_path, font="No Such Font", available_fonts=False)

    assert result.status is Engine_Status.DEGRADED
    assert result.contribution is None  # the slot was given up
    assert f"engine:{kinetic_mod.ENGINE_ID}:supersedes_captions" not in result.markers
    # The degradation itself is still recorded, so the clip is not silently changed.
    assert [m for m in result.markers if ":degraded:font:" in m]


def test_kinetic_does_not_persist_a_subtitle_document_nothing_rendered(tmp_path):
    """A discarded ASS document must not become a durable artifact.

    Persisting it would put a file in the Storage_Backend that no delivered clip
    corresponds to, and bill the job for storing it. The file is still *declared*
    (it exists in the workspace), so ``Engine_Result.artifacts`` stays an accurate
    description of the disk — it is only the ``durable`` flag that changes.
    """
    _host_obj, result = _run_kinetic(
        tmp_path, font="No Such Font", available_fonts=False, durable=True
    )

    assert result.status is Engine_Status.DEGRADED
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.durable is False
    assert Path(artifact.path).is_file()  # still declared, still on disk


def test_kinetic_still_claims_and_persists_when_it_really_does_supersede(tmp_path):
    """The applied path is unchanged: marker emitted, document durable.

    Without this, "never claim, never persist" would satisfy the two tests above
    and quietly disable the feature.
    """
    host, result = _run_kinetic(tmp_path, font="", available_fonts=True, durable=True)

    assert result.status is Engine_Status.APPLIED
    assert result.contribution is not None
    assert f"engine:{kinetic_mod.ENGINE_ID}:supersedes_captions" in result.markers
    assert result.artifacts[0].durable is True
    assert host.finish_clip("clip_a") == []  # it persisted cleanly


# --------------------------------------------------------------------------- #
# Claim 7: "the probed frame rate was unusable"                                #
# --------------------------------------------------------------------------- #
def test_the_time_base_is_not_pinned_to_the_fallback_by_call_order(tmp_path):
    """A ``time_base()`` call before ``run_source`` must not fix the job's fps.

    The provisional fallback used to be *cached*, so any call reaching the host
    before the probe — a stage run in a test, a future caller, a reordering of the
    Pipeline — pinned the whole job to ``DEFAULT_FPS`` with ``fps_substituted=True``,
    permanently and invisibly, because the real probe was then never consulted.
    """
    host = _host(tmp_path, [], _options())

    provisional = host.time_base()
    assert provisional.fps == DEFAULT_FPS
    assert provisional.fps_substituted is True

    # The real probe still wins.
    real = host.time_base(_FakeMediaInfo(fps=24.0))
    assert real.fps == 24.0
    assert real.fps_substituted is False
    # ...and is now the cached one for the rest of the job.
    assert host.time_base() is real


def test_an_unprobed_fps_fallback_note_does_not_name_a_value_of_none(tmp_path):
    """``fps_fallback:`` names the *rejected* fps; with no probe it says ``unprobed``.

    The note used to be built with ``_as_text(None)``, producing the literal
    ``fps_fallback:None`` — which reads as a probed frame rate whose value was the
    string "None", and gives an engine no way to distinguish an out-of-range probe
    from no probe at all.
    """
    engine = FakeEngine("note_reader", Engine_Stage.AUDIO)
    host = _host(tmp_path, [engine], _options("note_reader"))
    # No run_source, so nothing was probed.
    host.run_stage(
        Engine_Stage.AUDIO,
        clip_id="clip_a",
        source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=6.0,
        duration=6.0,
    )

    notes = list(engine.last_context.notes)
    fallback = [note for note in notes if note.startswith("fps_fallback:")]
    assert fallback == ["fps_fallback:unprobed"]
    assert "fps_fallback:None" not in notes
    host.finish_clip("clip_a")


def test_a_rejected_fps_is_still_named_in_the_note(tmp_path):
    """An out-of-range probed value is reported verbatim, not as ``unprobed``."""
    engine = FakeEngine("note_reader", Engine_Stage.AUDIO)
    host = _host(tmp_path, [engine], _options("note_reader"))
    host.run_source("/media/source.mp4", _FakeMediaInfo(fps=0.0))
    host.run_stage(
        Engine_Stage.AUDIO,
        clip_id="clip_a",
        source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=6.0,
        duration=6.0,
    )

    fallback = [n for n in engine.last_context.notes if n.startswith("fps_fallback:")]
    assert fallback == ["fps_fallback:0.0"]
    host.finish_clip("clip_a")


def test_a_usable_probe_produces_no_fallback_note(tmp_path):
    """No substitution, no note — the guard must not annotate every clip."""
    engine = FakeEngine("note_reader", Engine_Stage.AUDIO)
    host = _host(tmp_path, [engine], _options("note_reader"))
    host.run_source("/media/source.mp4", _FakeMediaInfo(fps=30.0))
    host.run_stage(
        Engine_Stage.AUDIO,
        clip_id="clip_a",
        source="/media/source.mp4",
        clip_path=tmp_path / "clip_a.mp4",
        clip_start=0.0,
        clip_end=6.0,
        duration=6.0,
    )

    assert [n for n in engine.last_context.notes if n.startswith("fps_fallback:")] == []
    assert Time_Base.from_media_info(_FakeMediaInfo(fps=30.0)).fps_substituted is False
    host.finish_clip("clip_a")
