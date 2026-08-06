"""M5's stage timings must attribute time to the stage that spent it.

The per-clip loop reported "Writing copy for clip N" at step 2 and then said nothing until
"Adding effects to clip N" at step 5. Everything in between - word slicing, filler removal,
the AUDIO-stage engines, the geometry ladder with its Haar face detection over up to 120
sampled frames and its full ``reformat_aspect`` re-encode, and the GEOMETRY-stage engines -
was therefore filed under "Writing copy".

Measured on a real 40s source: 17.4s of a 26s render, 67% of the total, attributed to a
stage that returns ``_fallback_metadata`` in microseconds when no LLM is configured. M5
exists because nobody knew where the minutes went; misattributing two thirds of them makes
it worse than nothing, because it points confidently at the wrong place.
"""
from __future__ import annotations

import time

from worker import jobs
from worker.jobs import JobManager, JobStore
from worker.models import JobStatus

try:  # module-level helpers (not fixtures) from the shared conftest
    from tests.conftest import options_all_off, requires_ffmpeg
except ImportError:  # pragma: no cover - conftest always importable under pytest
    from conftest import options_all_off, requires_ffmpeg


class _RecordingStore(JobStore):
    """A job store that remembers every progress update it was handed.

    The progress callback is built inside ``JobManager._run`` and is not reachable from
    outside, so the store is the seam: every ``progress()`` call lands here.
    """

    def __init__(self) -> None:
        super().__init__(persistence=False)
        self.progress_updates: list[tuple[float, str, int]] = []

    def update(self, job_id: str, **fields) -> None:
        if "progress" in fields and "stage" in fields:
            self.progress_updates.append(
                (float(fields["progress"]), str(fields["stage"]),
                 int(fields.get("stage_index") or 0))
            )
        super().update(job_id, **fields)


def _stub_asr_and_selection(monkeypatch, duration: float = 4.0):
    """Stub whisper and selection: a test must not download a model."""
    import worker.pipeline as pl
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    def fake_transcribe(source, language=None, translate=False, **_kw):
        words = [Word(0.2, 0.6, "hello"), Word(0.8, 1.4, "world")]
        return Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, duration, "hello world", words)],
        )

    monkeypatch.setattr(pl, "transcribe", fake_transcribe)
    # *Two* candidates, not one. Every per-clip assertion below is about a stage the loop
    # visits once per clip, and with a single clip a per-clip stage is indistinguishable
    # from a once-per-job one - the grouping in ``_stage_label``, the ``count`` in the
    # timings row and the step counter's behaviour at a clip boundary would all pass
    # vacuously.
    half = duration / 2
    monkeypatch.setattr(
        pl.sel, "select_moments",
        lambda *a, **k: [
            ClipCandidate(start=0.0, end=half, score=50.0, text="hello"),
            ClipCandidate(start=half, end=duration, score=40.0, text="world"),
        ],
    )


def _run_a_render(make_video, monkeypatch, **option_overrides):
    """Run one real two-clip render through the job manager and return (job, store)."""
    _stub_asr_and_selection(monkeypatch)
    src = make_video("stages.mp4", duration=4.0, w=320, h=180)

    store = _RecordingStore()
    manager = JobManager(store=store)
    options = options_all_off(captions=False, **option_overrides)
    job = manager.submit("file", str(src), options)

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        current = store.get(job.id)
        if current.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            break
        time.sleep(0.05)
    current = store.get(job.id)
    assert current.status is JobStatus.COMPLETED, current.error
    return current, store


# --------------------------------------------------------------------------- #
# The new stage exists, is recognised, and is measured                          #
# --------------------------------------------------------------------------- #
def test_the_reframing_stage_is_a_known_stage():
    """``stage_position`` must resolve it, or the UI silently degrades.

    An unrecognised stage returns 0, which ``JobCard`` reads as "no step information" and
    falls back to a plain bar. So a new report string that is not in :data:`JOB_STAGES`
    does not fail anything - it quietly removes the step counter for that part of every
    render, which is exactly the kind of regression nobody reports.
    """
    assert "Reframing" in jobs.JOB_STAGES
    assert jobs.stage_position("Reframing clip 1") > 0
    assert jobs._stage_label("Reframing clip 3") == "Reframing"


def test_job_stages_are_in_the_order_the_pipeline_reports_them():
    """The tuple's order is the step number a user sees, so it must match the pipeline.

    ``run_pipeline`` reports, per clip: "Rendering clip", then "Writing copy" (step 2),
    then "Reframing" (step 4), then "Adding effects" (step 5). The tuple previously listed
    "Adding effects" before "Writing copy", so ``stage_index`` counted 6 -> 8 -> 7 and the
    UI's "step N of M" went backwards mid-clip.
    """
    order = jobs.JOB_STAGES
    positions = [order.index(name) for name in
                 ("Rendering clip", "Writing copy", "Reframing", "Adding effects")]
    assert positions == sorted(positions), order
    assert order[0] == "Starting"
    assert order[-1] == "Completed"


@requires_ffmpeg
def test_the_geometry_ladder_is_timed_under_its_own_stage(make_video, monkeypatch):
    """A real render records a ``Reframing`` row in ``stage_timings``.

    Real, not stubbed, below the ASR seam: the reformat pass is the cost being attributed,
    so faking it would leave the attribution untested.
    """
    job, _store = _run_a_render(make_video, monkeypatch, metadata=True)

    stages = {row["stage"]: row for row in job.stage_timings}
    assert "Reframing" in stages, sorted(stages)
    # Two clips, one row, count 2 - the grouping in ``_stage_label`` applies to the new
    # stage as well, so it does not arrive as "Reframing clip 1" and "Reframing clip 2".
    assert stages["Reframing"]["count"] == 2, stages["Reframing"]
    assert stages["Reframing"]["seconds"] > 0.0


@requires_ffmpeg
def test_metadata_generation_no_longer_absorbs_the_geometry_pass(make_video, monkeypatch):
    """With no LLM configured, "Writing copy" must be a rounding error.

    ``generate_metadata`` returns ``_fallback_metadata`` immediately in that case, so any
    substantial time under this label is time that belongs to another stage. Compared
    against the reframing row rather than to an absolute threshold, because an absolute
    one would just be a pin on how fast this machine is.
    """
    job, _store = _run_a_render(make_video, monkeypatch, metadata=True)

    stages = {row["stage"]: row["seconds"] for row in job.stage_timings}
    assert "Writing copy" in stages, sorted(stages)
    assert stages["Writing copy"] < stages["Reframing"], stages


# --------------------------------------------------------------------------- #
# Progress must never go backwards                                              #
# --------------------------------------------------------------------------- #
@requires_ffmpeg
def test_progress_is_monotonically_non_decreasing_across_a_whole_run(
    make_video, monkeypatch
):
    """Inserting a report between two existing ones must not make the bar jump back.

    The new call sits at 0.45 of the per-clip span, between step 2's 0.3 and step 5's 0.6.
    Asserted over every update the job manager received, rather than over the three
    fractions by inspection, because the per-clip base offset is what makes them interact.
    """
    _job, store = _run_a_render(make_video, monkeypatch, metadata=True)

    fractions = [fraction for fraction, _stage, _index in store.progress_updates]
    assert fractions, "no progress was reported at all"
    for earlier, later in zip(fractions, fractions[1:]):
        assert later >= earlier - 1e-9, store.progress_updates


@requires_ffmpeg
def test_every_stage_a_real_render_reports_is_a_known_stage(make_video, monkeypatch):
    """No stage the pipeline reports may resolve to 0.

    ``stage_position`` returns 0 for anything it does not recognise, and 0 is not an error
    anywhere - the UI hides the step counter and ``_stage_label`` falls through to the raw,
    per-clip string, which fragments the timings into one row per clip. So an unlisted
    stage costs coverage silently, in both features, and nothing fails.

    Asserted against a real run rather than against a hand-written list of strings, because
    a hand-written list is a second source of truth that drifts the moment someone adds a
    report call - which is how "Rendered clip N of M", "Done" and "Translating subtitles"
    came to be missing from :data:`JOB_STAGES` in the first place.
    """
    _job, store = _run_a_render(make_video, monkeypatch, metadata=True)

    unknown = sorted({
        stage for _f, stage, index in store.progress_updates
        if index == 0 and jobs.stage_position(stage) == 0
    })
    assert unknown == [], f"stages not listed in JOB_STAGES: {unknown}"


@requires_ffmpeg
def test_the_step_counter_never_counts_backwards_within_a_clip(make_video, monkeypatch):
    """``stage_index`` rises through each clip, now that JOB_STAGES is in pipeline order.

    Scoped to *within* a clip on purpose. The per-clip loop necessarily returns to
    "Rendering clip" for clip 2, so the step number cannot be monotonic across a multi-clip
    job and asserting that it is would be asserting something false. What was wrong, and is
    what this pins, is the jump *inside* one clip: "Adding effects" was listed before
    "Writing copy", so a single clip counted 6 -> 8 -> 7.
    """
    _job, store = _run_a_render(make_video, monkeypatch, metadata=True)

    runs: list[list[tuple[str, int]]] = []
    for _fraction, stage, index in store.progress_updates:
        if index == 0:
            continue
        if stage.startswith("Rendering clip") or not runs:
            runs.append([])
        runs[-1].append((stage, index))

    per_clip = [run for run in runs if run and run[0][0].startswith("Rendering clip")]
    assert len(per_clip) == 2, runs
    for run in per_clip:
        indices = [index for _stage, index in run]
        assert indices == sorted(indices), run
