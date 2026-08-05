"""Backward-compatibility parity and dependency gate for the audio-stem-inpainting spec.

Covers epic 18 — the spec's central promise to an upgrading operator, and the one thing in
this plan that is **not** optional: registering a new AUDIO-stage engine must change nothing
for anyone who does not turn it on.

* **P17** (18.1) drives the real ``run_pipeline`` and compares a run with the engine
  *registered but disabled* against one with it *unregistered at all* — byte-identical clips,
  identical ``effects_applied``, identical metadata.
* The **static gate** (18.2) asserts the Pipeline stage order is unchanged, that no new
  mandatory dependency was introduced, and that the sibling specs were not touched.

The parity tests need real rendering, so they carry ``requires_ffmpeg``. Transcription and
selection are stubbed exactly as ``tests/test_pipeline_degradation.py`` does, so the only
variable between the two runs is the engine registration.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import requires_ffmpeg
from worker.models import ProcessingOptions

_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Stubs (mirroring tests/test_pipeline_degradation.py)                        #
# --------------------------------------------------------------------------- #
def _stub_transcribe(monkeypatch, text="hello there my friend today"):
    import worker.pipeline as pl
    from worker.transcribe import Transcript, TranscriptSegment, Word

    def fake_transcribe(source, language=None, translate=False, **_kw):
        words = [
            Word(0.2, 0.6, "hello"),
            Word(0.7, 1.1, "there"),
            Word(1.2, 1.6, "my"),
            Word(1.7, 2.3, "friend"),
            Word(2.4, 3.0, "today"),
        ]
        return Transcript(
            language="en", segments=[TranscriptSegment(0.0, 4.0, text, words)]
        )

    monkeypatch.setattr(pl, "transcribe", fake_transcribe)


def _stub_selection(monkeypatch, text="hello there my friend today"):
    import worker.pipeline as pl
    from worker.selection import ClipCandidate

    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [ClipCandidate(start=0.0, end=4.0, score=50.0, text=text)],
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _snapshot(clips, clips_dir: Path) -> list[dict]:
    """The observable result of a pipeline run, for comparison between two runs."""
    return [
        {
            # The *filename* is deliberately excluded: ``pipeline.run_pipeline`` builds the
            # clip id as ``f"{idx:02d}_{uuid.uuid4().hex[:6]}"``, so it is random per run and
            # could never match between two runs. Its shape is asserted separately; the
            # **digest** is what actually pins the bytes.
            "name_shape": bool(re.fullmatch(r"clip_\d{2}_[0-9a-f]{6}\.mp4", clip.filename)),
            "start": round(clip.start, 6),
            "end": round(clip.end, 6),
            "duration": round(clip.duration, 6),
            "effects_applied": list(clip.effects_applied),
            "title": clip.title,
            "description": clip.description,
            "hashtags": list(clip.hashtags),
            "digest": _digest(clips_dir / clip.filename),
        }
        for clip in clips
    ]


def _run(source, work: Path, monkeypatch, options, *, registered: bool, tag: str):
    """One pipeline run, with the stem engine either registered or absent.

    ``source`` is shared between the two runs on purpose: the output filename carries a hash
    derived from the source path, so running from two different directories would produce
    different filenames for identical content and mask the comparison.
    """
    import worker.pipeline as pl
    from worker.engines.kinetic import Kinetic_Typography_Engine
    from worker.engines.registry import Engine_Registry
    from worker.engines.stems import Stem_Inpainting_Engine

    registry = Engine_Registry()
    registry.register(Kinetic_Typography_Engine())
    if registered:
        registry.register(Stem_Inpainting_Engine())

    real_host = pl.Engine_Host

    def host(opts, **kwargs):
        kwargs["registry"] = registry
        return real_host(opts, **kwargs)

    monkeypatch.setattr(pl, "Engine_Host", host)

    _stub_transcribe(monkeypatch)
    _stub_selection(monkeypatch)

    clips_dir = work / f"clips_{tag}"
    clips = pl.run_pipeline(
        source, options, clips_dir=clips_dir, temp_dir=work / f"tmp_{tag}"
    )
    return _snapshot(clips, clips_dir), clips


# =========================================================================== #
# P17 — the Pipeline is unchanged except when the engine applies              #
# =========================================================================== #
# Feature: audio-stem-inpainting, Property 17: The Pipeline is unchanged except when the
# engine applies
@requires_ffmpeg
@settings(
    max_examples=8, deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    captions=st.booleans(),
    filler_removal=st.booleans(),
    aspect=st.sampled_from(["9:16", "1:1"]),
)
def test_p17_a_registered_but_disabled_engine_changes_nothing(
    make_video, tmp_path, monkeypatch, captions: bool, filler_removal: bool, aspect: str
) -> None:
    """Registered-but-disabled is byte-identical to unregistered (Reqs 20.1, 20.4).

    This is the promise an upgrading operator actually cares about: installing a release that
    ships a new engine must not change a single byte of their output until they enable it.

    Compared: the clip **file digests**, the ``effects_applied`` lists (order included — an
    engine that appended a marker in a different position would be a behaviour change of its
    own), the clip bounds and durations, and the generated metadata.

    Varying `captions` and `filler_removal` matters because both interact with the AUDIO
    stage: filler removal is what *creates* the seams this engine exists to repair, so a run
    with it on is the case where a stray engine effect would be most likely to leak.
    """
    src = make_video("src.mp4", duration=4.0, w=640, h=360)
    options = ProcessingOptions(
        captions=captions, metadata=False, aspect=aspect,
        filler_removal=filler_removal,
    )

    # ``tmp_path`` is function-scoped and therefore shared across Hypothesis examples, so each
    # example needs its own working directory. The *source* is shared between the two runs of
    # a given example, so the output filenames are comparable.
    case = f"{int(captions)}{int(filler_removal)}{aspect.replace(':', '')}"
    work = tmp_path / f"case_{case}"
    work.mkdir(parents=True, exist_ok=True)

    with monkeypatch.context() as m:
        absent, _ = _run(src, work, m, options, registered=False, tag="absent")
    with monkeypatch.context() as m:
        present, _ = _run(src, work, m, options, registered=True, tag="present")

    assert absent == present, "registering the engine changed the output"


@requires_ffmpeg
def test_p17_an_enabled_engine_preserves_clip_count_and_existing_markers(
    make_video, tmp_path, monkeypatch
) -> None:
    """Even when enabled, the engine adds markers — it never removes or reorders others.

    Asserts the weaker-but-essential half of Req 8.7 / 3.8: whatever the engine does, the clip
    count and durations are unchanged and every pre-existing marker is still present in its
    original relative order. Only ``engine:stem_inpainting:*`` entries may be added.
    """
    src = make_video("src.mp4", duration=4.0, w=640, h=360)
    base = ProcessingOptions(
        captions=False, metadata=False, aspect="9:16", filler_removal=True
    )
    enabled = ProcessingOptions(
        captions=False, metadata=False, aspect="9:16", filler_removal=True,
        stem_inpainting_enabled=True, stem_mix_preset="speech_focus",
    )

    with monkeypatch.context() as m:
        off, _ = _run(src, tmp_path, m, base, registered=True, tag="off")
    with monkeypatch.context() as m:
        on, _ = _run(src, tmp_path, m, enabled, registered=True, tag="on")

    assert len(on) == len(off)
    for before, after in zip(off, on):
        assert after["duration"] == before["duration"]
        assert (after["start"], after["end"]) == (before["start"], before["end"])
        # Every pre-existing marker survives, in the same relative order.
        kept = [m_ for m_ in after["effects_applied"] if not m_.startswith("engine:")]
        assert kept == before["effects_applied"]


# =========================================================================== #
# 18.2 — the static gate                                                      #
# =========================================================================== #
def test_the_pipeline_stage_order_is_unchanged() -> None:
    """No new Pipeline stage was added, and the five hook points are still the five.

    The engine attaches to the **existing** AUDIO hook. If this plan had needed a new stage
    that would be a foundation change, and this assertion is what makes the claim checkable
    rather than a comment.
    """
    from worker.engines.base import Engine_Stage

    assert [stage.value for stage in Engine_Stage] == [
        "source", "audio", "geometry", "compose", "post"
    ]

    source = (_ROOT / "worker" / "pipeline.py").read_text(encoding="utf-8")
    hooks = re.findall(r"host\.run_stage\(\s*Engine_Stage\.(\w+)", source)
    assert hooks == ["AUDIO", "GEOMETRY", "COMPOSE", "POST"]
    assert source.count("host.run_source(") == 1
    # Exactly one AUDIO hook: the engine did not add a second invocation point.
    assert hooks.count("AUDIO") == 1


def test_no_new_mandatory_dependency_was_introduced() -> None:
    """``demucs`` and ``torch`` stay **optional** (Reqs 20.3, 20.5).

    The engine's whole degradation story rests on this: a stock install must be able to run it
    (via the ffmpeg approximation) without pulling a multi-hundred-megabyte ML stack, and
    ``requirements.txt`` is where that promise is either kept or quietly broken.
    """
    runtime = (_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for package in ("demucs", "torch"):
        assert package not in runtime, f"{package} must not be a runtime dependency"

    dev = (_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
    for package in ("demucs", "torch", "numpy"):
        assert package not in dev, f"{package} must not be a dev dependency either"
    # hypothesis was already there before this spec; it must not have been re-added.
    assert dev.count("hypothesis") == 1


def test_the_ci_workflow_has_no_stem_specific_steps() -> None:
    """No stem-specific step, and no ML extras installed in CI (Req 20.3).

    The requirement is that this engine costs CI nothing: no separate job, no model
    download, no torch install. It is asserted on the workflow's *content* rather than by
    pinning exact commands — the workflow has since been hardened for unrelated reasons
    (ruff made blocking, frontend lint/tests added), and a test that breaks whenever any
    CI line changes would be a tripwire on the wrong thing.

    Comment lines are excluded. The requirement is about what CI *does*, and a comment is
    not a step: a note explaining why a checkout needs full history is allowed to name the
    file it is talking about. Checking the raw text instead made the guard fail on its own
    documentation, which is a tripwire rather than a test.
    """
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    executable = "\n".join(
        line for line in ci.splitlines() if not line.strip().startswith("#")
    ).lower()

    # Careful with the substring: "system dependencies" contains "stem".
    assert "stem_" not in executable
    assert "stems" not in executable
    assert "demucs" not in executable
    assert "torch" not in executable
    # The optional ML extras must never be installed by CI — that is what would make the
    # engine expensive to test, and it is the concrete form Req 20.3 takes now that
    # requirements-ml.txt exists.
    assert "requirements-ml" not in executable
    # The suite is still run as one undifferentiated invocation, not a stems-only job.
    assert "-k stem" not in executable
    assert "tests/test_stems" not in executable


def test_the_sibling_spec_directories_were_not_modified() -> None:
    """Nothing under the foundation or kinetic spec dirs changed (Req 20.6).

    Checked against git rather than by inspection, because "I did not edit it" is exactly the
    kind of claim that is easy to believe and wrong.

    Note what this does **and does not** cover. The two sibling *spec* directories are
    untouched. Foundation **code** (`worker/engines/host.py`) and two foundation **test**
    files were changed, deliberately and with the user's approval, to widen the media gate and
    add the additive `run_stage(notes=...)` keyword — both recorded in the epic-12 notes in
    this spec's `tasks.md`. Those are contract changes the pins were updated to match, not
    accidental drift, and pretending otherwise would make this gate dishonest.
    """
    import subprocess

    changed = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=60,
    )
    if changed.returncode != 0:
        pytest.skip("no origin/main to compare against in this checkout")

    touched = [line for line in changed.stdout.splitlines() if line.strip()]
    forbidden = [
        path for path in touched
        if path.startswith(".kiro/specs/av-engines-foundation/")
        or path.startswith(".kiro/specs/kinetic-typography/")
    ]
    assert forbidden == [], f"sibling spec files were modified: {forbidden}"


def test_the_stem_engine_adds_no_new_production_file() -> None:
    """The stem engine's production footprint is additive edits only (Req 20.3).

    ``worker/engines/stems.py`` already existed at ``origin/main`` — epics 4-8 landed its
    planner and backend seam — so everything this feature does to production code is an
    *additive edit* to a file that was already there: the loader line, the eleven options
    fields, the API surface, the panel, and the two approved foundation changes.

    Scoped to *stem-related* files rather than to every file on the branch. The branch has
    since accumulated unrelated reliability work that legitimately adds modules
    (``worker/job_persistence.py``, ``pyproject.toml``, an eslint config), and asserting
    "this branch adds nothing" would turn a requirement about the stem feature's blast
    radius into a tripwire on all future work — failing for reasons that say nothing about
    the requirement. Checking that no *new* production file is stem-related keeps the
    original guarantee and stays true as the branch grows.
    """
    import subprocess

    added = subprocess.run(
        ["git", "diff", "--name-status", "--diff-filter=A", "origin/main...HEAD"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=60,
    )
    if added.returncode != 0:
        pytest.skip("no origin/main to compare against in this checkout")

    new_files = [
        line.split("\t", 1)[1] for line in added.stdout.splitlines() if "\t" in line
    ]
    new_production = [
        path for path in new_files
        if not path.startswith("tests/") and not path.startswith(".kiro/")
    ]

    # No new engine module, and nothing named for this feature: the engine lives entirely
    # in the pre-existing worker/engines/stems.py.
    #
    # Matched on a word boundary rather than as a substring. `"stem" in path.lower()` was the
    # first spelling, and it fires on `api/routers/system.py` -- **sy-stem** -- which this very
    # commit adds. A guard that fails for a reason unrelated to what it guards is worse than no
    # guard: the tempting response is to add an exclusion for this one path, and after two rounds
    # of that nobody trusts the assertion. The word boundary fixes the class, not the instance.
    stem_pattern = re.compile(r"(?:^|[^a-z])stems?(?:[^a-z]|$)")
    stem_related = [
        path for path in new_production
        if stem_pattern.search(path.lower()) or path.startswith("worker/engines/")
    ]
    assert stem_related == [], (
        f"the stem engine should not add production files, but got: {stem_related}"
    )
