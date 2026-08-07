"""U4: transcript-based trimming.

Three layers, because the failure modes are different at each. The geometry
(:mod:`worker.transcript_trim`) is pure and cheap, so it is tested exhaustively. The
pipeline integration is tested against real ffmpeg output, because "the keeps were
computed correctly" and "the media is actually shorter" are separate claims and only
the second one is the feature. The API layer is tested for the refusals, since those
are the paths a UI has to distinguish and the ones that are easy to get wrong quietly.
"""

from __future__ import annotations

import math
import uuid

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config import settings
from tests.conftest import options_all_off, probe_duration, requires_ffmpeg
from worker import transcript_trim as trim
from worker.effects.filler import Interval
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions
from worker.transcribe import Transcript, TranscriptSegment, Word


# --------------------------------------------------------------------------- #
# normalise_cuts
# --------------------------------------------------------------------------- #
def test_no_cuts_normalises_to_nothing():
    assert trim.normalise_cuts([], 10.0) == []
    assert trim.normalise_cuts(None, 10.0) == []


def test_cuts_are_clamped_to_the_clip():
    """A cut past the end is clamped, not dropped: the user struck the last word."""
    out = trim.normalise_cuts([(8.0, 99.0), (-5.0, 1.0)], 10.0)
    assert [(i.start, i.end) for i in out] == [(0.0, 1.0), (8.0, 10.0)]


def test_a_cut_entirely_outside_the_clip_is_dropped():
    assert trim.normalise_cuts([(20.0, 30.0)], 10.0) == []


def test_cuts_are_sorted_and_coalesced_when_they_touch():
    out = trim.normalise_cuts([(4.0, 5.0), (1.0, 2.0), (2.005, 3.0)], 10.0)
    # (1,2) and (2.005,3) are within MERGE_GAP_S, so they become one cut.
    assert [(i.start, i.end) for i in out] == [(1.0, 3.0), (4.0, 5.0)]


def test_cuts_further_apart_than_the_merge_gap_stay_separate():
    out = trim.normalise_cuts([(1.0, 2.0), (2.0 + trim.MERGE_GAP_S * 3, 3.0)], 10.0)
    assert len(out) == 2


def test_overlapping_cuts_become_one():
    out = trim.normalise_cuts([(1.0, 5.0), (2.0, 3.0), (4.0, 6.0)], 10.0)
    assert [(i.start, i.end) for i in out] == [(1.0, 6.0)]


def test_a_reversed_pair_is_read_as_a_range_not_discarded():
    out = trim.normalise_cuts([(5.0, 2.0)], 10.0)
    assert [(i.start, i.end) for i in out] == [(2.0, 5.0)]


def test_zero_length_cuts_are_dropped():
    assert trim.normalise_cuts([(3.0, 3.0)], 10.0) == []


@pytest.mark.parametrize(
    "cut",
    [
        ("nonsense", 2.0),
        (None, 2.0),
        (1.0,),
        (1.0, 2.0, 3.0),
        "not a cut at all",
        42,
    ],
)
def test_malformed_cuts_are_dropped_rather_than_raising(cut):
    """One stale offset from a UI must not cost the user the rest of the edit."""
    assert trim.normalise_cuts([cut, (1.0, 2.0)], 10.0) == [Interval(1.0, 2.0)]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_cuts_are_rejected(bad):
    """NaN survives float() and then poisons sorting and every duration after it."""
    assert trim.normalise_cuts([(bad, 2.0)], 10.0) == []
    assert trim.normalise_cuts([(1.0, bad)], 10.0) == []


def test_cuts_are_accepted_in_all_three_wire_shapes():
    """Objects, mappings and pairs all arrive in practice; all three must work."""
    as_object = trim.normalise_cuts([Interval(1.0, 2.0)], 10.0)
    as_mapping = trim.normalise_cuts([{"start": 1.0, "end": 2.0}], 10.0)
    as_pair = trim.normalise_cuts([(1.0, 2.0)], 10.0)
    assert as_object == as_mapping == as_pair == [Interval(1.0, 2.0)]


def test_a_zero_duration_clip_yields_no_cuts():
    assert trim.normalise_cuts([(0.0, 1.0)], 0.0) == []


# --------------------------------------------------------------------------- #
# plan_cuts
# --------------------------------------------------------------------------- #
def test_an_empty_cut_list_keeps_the_whole_clip_and_changes_nothing():
    plan = trim.plan_cuts([], 10.0)
    assert plan.keeps == [Interval(0.0, 10.0)]
    assert plan.changed is False
    assert plan.refusal == ""
    assert plan.cut_count == 0


def test_a_cut_in_the_middle_splits_the_clip_into_two_keeps():
    plan = trim.plan_cuts([(4.0, 6.0)], 10.0)
    assert [(k.start, k.end) for k in plan.keeps] == [(0.0, 4.0), (6.0, 10.0)]
    assert plan.removed_seconds == pytest.approx(2.0)
    assert plan.changed is True
    assert plan.marker == trim.MARKER


def test_a_cut_at_the_head_leaves_one_keep():
    plan = trim.plan_cuts([(0.0, 3.0)], 10.0)
    assert [(k.start, k.end) for k in plan.keeps] == [(3.0, 10.0)]
    assert plan.removed_seconds == pytest.approx(3.0)


def test_a_cut_at_the_tail_leaves_one_keep():
    plan = trim.plan_cuts([(7.0, 10.0)], 10.0)
    assert [(k.start, k.end) for k in plan.keeps] == [(0.0, 7.0)]


def test_slivers_below_the_minimum_segment_are_dropped():
    """Two cuts that leave 50 ms between them: a click, not a word.

    Asserted against literal keeps rather than against ``trim.MIN_SEGMENT_S``. Comparing the
    result to the constant that produced it is vacuous - lowering the constant makes the
    assertion pass by definition, which is precisely the change that must fail here.
    """
    plan = trim.plan_cuts([(2.0, 4.0), (4.05, 6.0)], 10.0)
    assert [(k.start, k.end) for k in plan.keeps] == [(0.0, 2.0), (6.0, 10.0)]


def test_cutting_the_whole_clip_is_refused_rather_than_rendered_empty():
    plan = trim.plan_cuts([(0.0, 10.0)], 10.0)
    assert plan.refusal == "empty_result"
    assert plan.changed is False
    assert plan.marker == f"{trim.REFUSED_MARKER}:empty_result"
    # Falls back to the untrimmed clip, so a caller can render plan.keeps blindly.
    assert plan.keeps == [Interval(0.0, 10.0)]


def test_too_many_cuts_is_refused_with_its_own_reason():
    cuts = [(i * 0.1, i * 0.1 + 0.01) for i in range(trim.MAX_CUTS * 2)]
    plan = trim.plan_cuts(cuts, 10_000.0)
    assert plan.refusal == "too_many_cuts"
    assert plan.changed is False
    assert plan.marker == f"{trim.REFUSED_MARKER}:too_many_cuts"


def test_the_cut_limit_counts_coalesced_cuts_not_raw_ones():
    """MAX_CUTS guards the filter graph, and the graph is built from merged keeps."""
    # Every cut overlaps its neighbour, so they merge into one.
    cuts = [(i * 0.001, i * 0.001 + 0.5) for i in range(trim.MAX_CUTS * 2)]
    plan = trim.plan_cuts(cuts, 100.0)
    assert plan.refusal == ""
    assert plan.cut_count == 1


def test_cuts_compose_with_an_existing_filler_plan_by_union():
    """Both features want regions gone; the render must honour both."""
    filler_keeps = [Interval(0.0, 3.0), Interval(4.0, 10.0)]  # filler removed 3-4
    plan = trim.plan_cuts([(6.0, 7.0)], 10.0, base_keeps=filler_keeps)
    assert [(k.start, k.end) for k in plan.keeps] == [(0.0, 3.0), (4.0, 6.0), (7.0, 10.0)]
    # Removed relative to what the clip was already going to be (9s), not the full 10s.
    assert plan.removed_seconds == pytest.approx(1.0)
    assert plan.changed is True


def test_a_cut_inside_an_already_removed_region_changes_nothing():
    """No second re-encode to achieve what the first pass already did."""
    filler_keeps = [Interval(0.0, 3.0), Interval(4.0, 10.0)]
    plan = trim.plan_cuts([(3.2, 3.8)], 10.0, base_keeps=filler_keeps)
    assert plan.changed is False
    assert plan.keeps == filler_keeps


def test_a_refusal_falls_back_to_the_existing_filler_plan_not_the_whole_clip():
    filler_keeps = [Interval(0.0, 3.0)]
    plan = trim.plan_cuts([(0.0, 10.0)], 10.0, base_keeps=filler_keeps)
    assert plan.refusal == "empty_result"
    assert plan.keeps == filler_keeps


def test_a_refusal_is_never_changed_even_carrying_a_removed_duration():
    """The guard on `changed`, pinned directly on the dataclass.

    Through :func:`plan_cuts` this is invisible, because every refusal it builds also has
    ``removed_seconds`` of zero - so the refusal check and the duration check happen to agree
    and either alone would do. ``TrimPlan`` is public and the two fields are independent, so
    the invariant is asserted where it lives: a plan that was declined must never cause a
    re-encode, whatever else it says.
    """
    declined = trim.TrimPlan(
        keeps=[Interval(0.0, 10.0)], removed_seconds=5.0, cut_count=1, refusal="empty_result"
    )
    assert declined.changed is False
    assert declined.marker == f"{trim.REFUSED_MARKER}:empty_result"

    usable = trim.TrimPlan(keeps=[Interval(0.0, 5.0)], removed_seconds=5.0, cut_count=1)
    assert usable.changed is True
    assert usable.marker == trim.MARKER


def test_a_plan_that_removes_almost_nothing_is_not_worth_a_re_encode():
    """A whole extra encode pass to remove 5 ms is a loss, not an optimisation."""
    assert trim.TrimPlan(keeps=[Interval(0.0, 10.0)], removed_seconds=0.005).changed is False
    assert trim.TrimPlan(keeps=[Interval(0.0, 10.0)], removed_seconds=0.5).changed is True


def test_keeps_are_disjoint_and_ascending():
    plan = trim.plan_cuts([(2.0, 3.0), (5.0, 6.0), (8.0, 8.5)], 12.0)
    for earlier, later in zip(plan.keeps, plan.keeps[1:]):
        assert earlier.end <= later.start


# --------------------------------------------------------------------------- #
# Pipeline integration
# --------------------------------------------------------------------------- #
def _transcript(duration: float) -> Transcript:
    words = [
        Word(0.3, 0.7, "This"),
        Word(0.9, 1.4, "part"),
        Word(1.6, 2.1, "stays"),
        Word(3.0, 3.5, "remove"),
        Word(3.6, 4.1, "this"),
        Word(5.0, 5.5, "end"),
    ]
    return Transcript(
        language="en",
        segments=[TranscriptSegment(0.0, duration, "This part stays remove this end", words)],
    )


def _wire(monkeypatch, duration, cuts):
    """Point the pipeline at a fixed transcript and a single explicit candidate."""
    import worker.pipeline as pl
    from worker.selection import ClipCandidate

    monkeypatch.setattr(
        pl, "transcribe", lambda s, language=None, translate=False, **_kw: _transcript(duration)
    )
    return [ClipCandidate(start=0.0, end=duration, reason="t", text="x", cuts=cuts)]


@requires_ffmpeg
def test_a_cut_list_shortens_the_rendered_media(make_video, tmp_path, monkeypatch):
    import worker.pipeline as pl

    src = make_video("cut.mp4", duration=6.0, w=640, h=360)
    candidates = _wire(monkeypatch, 6.0, [(2.5, 4.5)])

    clips = pl.run_pipeline(
        src,
        options_all_off(captions=False, metadata=False, aspect="9:16"),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
        explicit_candidates=candidates,
    )
    assert len(clips) == 1
    clip = clips[0]
    assert trim.MARKER in clip.effects_applied

    out = tmp_path / "clips" / clip.filename
    assert probe_duration(out) == pytest.approx(4.0, abs=0.35)
    # The recorded duration describes the rendered media, not the source window; the
    # window itself is still what it was, because that is what a resume matches on.
    assert clip.duration == pytest.approx(4.0, abs=0.35)
    assert (clip.start, clip.end) == (0.0, 6.0)


@requires_ffmpeg
def test_no_cut_list_renders_exactly_the_window(make_video, tmp_path, monkeypatch):
    """The parity case: U4 present but unused must change nothing at all."""
    import worker.pipeline as pl

    src = make_video("nocut.mp4", duration=6.0, w=640, h=360)
    candidates = _wire(monkeypatch, 6.0, [])

    clips = pl.run_pipeline(
        src,
        options_all_off(captions=False, metadata=False, aspect="9:16"),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
        explicit_candidates=candidates,
    )
    clip = clips[0]
    assert trim.MARKER not in clip.effects_applied
    assert not any(m.startswith(trim.REFUSED_MARKER) for m in clip.effects_applied)
    assert clip.duration == pytest.approx(6.0, abs=0.05)
    assert probe_duration(tmp_path / "clips" / clip.filename) == pytest.approx(6.0, abs=0.35)


@requires_ffmpeg
def test_a_cut_list_and_filler_removal_both_apply(make_video, tmp_path, monkeypatch):
    """Two features removing regions must not become one feature winning."""
    import worker.pipeline as pl
    from worker.selection import ClipCandidate

    src = make_video("both.mp4", duration=8.0, w=640, h=360)

    words = [
        Word(0.3, 0.7, "one"),
        Word(0.9, 1.3, "um"),  # filler, removed by filler_removal
        Word(1.5, 1.9, "two"),
        Word(2.1, 2.5, "three"),
        Word(2.7, 3.1, "four"),
        Word(3.3, 3.7, "five"),
    ]
    monkeypatch.setattr(
        pl,
        "transcribe",
        lambda s, language=None, translate=False, **_kw: Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, 8.0, "one um two three four five", words)],
        ),
    )
    candidates = [ClipCandidate(start=0.0, end=8.0, reason="t", text="x", cuts=[(2.0, 2.6)])]

    clips = pl.run_pipeline(
        src,
        options_all_off(captions=False, metadata=False, aspect="9:16", filler_removal=True),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
        explicit_candidates=candidates,
    )
    clip = clips[0]
    assert "filler_removal" in clip.effects_applied
    assert trim.MARKER in clip.effects_applied
    # Trailing dead air after 3.7s is also removed by filler removal, so assert the
    # relationship that matters: both removals happened, so the result is shorter than
    # either alone would give.
    assert clip.duration < 3.7


@requires_ffmpeg
def test_a_cut_list_that_empties_the_clip_is_refused_and_marked(make_video, tmp_path, monkeypatch):
    import worker.pipeline as pl

    src = make_video("empty.mp4", duration=5.0, w=640, h=360)
    candidates = _wire(monkeypatch, 5.0, [(0.0, 5.0)])

    clips = pl.run_pipeline(
        src,
        options_all_off(captions=False, metadata=False, aspect="9:16"),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
        explicit_candidates=candidates,
    )
    clip = clips[0]
    assert f"{trim.REFUSED_MARKER}:empty_result" in clip.effects_applied
    assert trim.MARKER not in clip.effects_applied
    # Refused, so the clip is the untrimmed window - not a zero-length file.
    assert clip.duration == pytest.approx(5.0, abs=0.05)
    assert probe_duration(tmp_path / "clips" / clip.filename) > 4.0


@requires_ffmpeg
def test_captions_follow_the_cut(make_video, tmp_path, monkeypatch):
    """Words must be rebased onto the tightened timeline, or captions desync."""
    import worker.pipeline as pl

    src = make_video("caps.mp4", duration=6.0, w=640, h=360)
    candidates = _wire(monkeypatch, 6.0, [(2.5, 4.5)])
    seen: list = []

    real_rebase = pl.filler.rebase_words

    def spy(words, keeps):
        out = real_rebase(words, keeps)
        seen.append(out)
        return out

    monkeypatch.setattr(pl.filler, "rebase_words", spy)
    pl.run_pipeline(
        src,
        options_all_off(captions=True, metadata=False, aspect="9:16"),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
        explicit_candidates=candidates,
    )
    assert seen, "words were never rebased, so captions would sit on the old timeline"
    # "end" was at 5.0 in the source window; with 2s removed before it, it is now at 3.0.
    rebased = {w.text: w.start for w in seen[0]}
    assert rebased["end"] == pytest.approx(3.0, abs=0.05)
    # The struck words are gone from the caption stream entirely.
    assert "remove" not in rebased


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def clip_job(tmp_path):
    """A completed job whose source file exists, so re-render can resolve it.

    The source bytes are made **unique per test**. The transcript cache is content-addressed
    (``hash_source`` digests the bytes, not the path), so a fixed payload gives every test in
    the session the same cache key — and a test asserting a *miss* then passes or fails
    depending on whether some earlier test happened to store one. That is not a hypothetical:
    it is how this fixture behaved on its first full-suite run, having passed in isolation.
    """
    from worker.jobs import get_manager

    source = tmp_path / "source.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp42" + uuid.uuid4().bytes + b"payload" * 64)

    manager = get_manager()
    job = Job(input_type="file", source=str(source), options=ProcessingOptions())
    job.source_path = str(source)
    clip = ClipResult(id="clipT", filename="clipT.mp4", start=2.0, end=8.0, duration=6.0, title="T")
    job.clips = [clip]
    job.status = JobStatus.COMPLETED
    manager.store.add(job)

    clip_dir = settings.clips_dir / job.id
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / clip.filename).write_bytes(b"FAKEVIDEODATA")
    return job, clip, source


def test_transcript_endpoint_404s_for_an_unknown_clip(client, clip_job):
    job, _clip, _src = clip_job
    assert client.get(f"/api/jobs/{job.id}/clips/nope/transcript").status_code == 404
    assert client.get("/api/jobs/nope/clips/clipT/transcript").status_code == 404


def test_transcript_endpoint_409s_when_nothing_is_cached(client, clip_job):
    """A miss is reported, never repaired by a four-minute synchronous ASR run."""
    job, clip, _src = clip_job
    resp = client.get(f"/api/jobs/{job.id}/clips/{clip.id}/transcript")
    assert resp.status_code == 409
    assert "cache" in resp.json()["detail"].lower()


def test_transcript_endpoint_serves_clip_relative_words_from_the_cache(client, clip_job):
    from worker import transcript_cache
    from worker.transcribe import cache_key_for

    job, clip, source = clip_job
    # Source-relative words; the clip is the window [2, 8].
    words = [Word(1.0, 1.4, "before"), Word(3.0, 3.4, "inside"), Word(9.0, 9.4, "after")]
    transcript = Transcript(
        language="en", segments=[TranscriptSegment(0.0, 12.0, "before inside after", words)]
    )
    key = cache_key_for(source, language=None, translate=False, vocabulary="")
    assert key is not None
    transcript_cache.store(key, transcript)

    resp = client.get(f"/api/jobs/{job.id}/clips/{clip.id}/transcript")
    assert resp.status_code == 200
    body = resp.json()
    assert [w["text"] for w in body["words"]] == ["inside"]
    # Rebased: 3.0 in the source is 1.0 into a clip starting at 2.0.
    assert body["words"][0]["start"] == pytest.approx(1.0)
    assert body["duration"] == pytest.approx(6.0)
    assert body["trimmed"] is False
    assert body["max_cuts"] == trim.MAX_CUTS


def test_transcript_endpoint_reports_a_clip_whose_media_was_already_tightened(client, clip_job):
    """The editor must know its offsets no longer match the media it is playing."""
    from worker import transcript_cache
    from worker.jobs import get_manager
    from worker.transcribe import cache_key_for

    job, clip, source = clip_job
    get_manager().store.update_clip(job.id, clip.id, {"effects_applied": ["filler_removal"]})
    key = cache_key_for(source, language=None, translate=False, vocabulary="")
    transcript_cache.store(
        key,
        Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, 12.0, "x", [Word(3.0, 3.4, "x")])],
        ),
    )
    body = client.get(f"/api/jobs/{job.id}/clips/{clip.id}/transcript").json()
    assert body["trimmed"] is True


def test_a_refused_trim_marker_does_not_report_the_clip_as_trimmed(client, clip_job):
    """`transcript_trim_refused:*` shares a prefix with the applied marker and must not
    be mistaken for it - the media was left alone."""
    from worker import transcript_cache
    from worker.jobs import get_manager
    from worker.transcribe import cache_key_for

    job, clip, source = clip_job
    get_manager().store.update_clip(
        job.id, clip.id, {"effects_applied": [f"{trim.REFUSED_MARKER}:empty_result"]}
    )
    key = cache_key_for(source, language=None, translate=False, vocabulary="")
    transcript_cache.store(
        key,
        Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, 12.0, "x", [Word(3.0, 3.4, "x")])],
        ),
    )
    body = client.get(f"/api/jobs/{job.id}/clips/{clip.id}/transcript").json()
    assert body["trimmed"] is False


def test_rerender_rejects_an_oversized_cut_list(client, clip_job):
    job, clip, _src = clip_job
    cuts = [{"start": i * 0.1, "end": i * 0.1 + 0.01} for i in range(trim.MAX_CUTS + 1)]
    resp = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/rerender", json={"settings": {}, "cuts": cuts}
    )
    assert resp.status_code == 422
    assert str(trim.MAX_CUTS) in resp.json()["detail"]


def test_rerender_rejects_a_negative_cut(client, clip_job):
    job, clip, _src = clip_job
    resp = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/rerender",
        json={"settings": {}, "cuts": [{"start": -1.0, "end": 2.0}]},
    )
    assert resp.status_code == 422


def test_rerender_forwards_the_cut_list_to_the_worker(client, clip_job, monkeypatch):
    """The cut list must not be silently dropped between HTTP and the render."""
    from worker import rerender as rerender_module

    job, clip, _src = clip_job
    captured: dict = {}

    def fake_rerender(job_arg, clip_arg, *, option_overrides=None, cuts=None, **_kw):
        captured["cuts"] = cuts
        captured["overrides"] = option_overrides
        return clip_arg

    monkeypatch.setattr(rerender_module, "rerender_clip", fake_rerender)
    resp = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/rerender",
        json={"settings": {"zoom": True}, "cuts": [{"start": 1.0, "end": 2.0}]},
    )
    assert resp.status_code == 200
    assert captured["cuts"] == [(1.0, 2.0)]


def test_rerender_without_cuts_sends_an_empty_list(client, clip_job, monkeypatch):
    """U7's existing callers send no `cuts` at all; that must stay a no-op."""
    from worker import rerender as rerender_module

    job, clip, _src = clip_job
    captured: dict = {}

    def fake_rerender(job_arg, clip_arg, *, option_overrides=None, cuts=None, **_kw):
        captured["cuts"] = cuts
        return clip_arg

    monkeypatch.setattr(rerender_module, "rerender_clip", fake_rerender)
    resp = client.post(f"/api/jobs/{job.id}/clips/{clip.id}/rerender", json={"settings": {}})
    assert resp.status_code == 200
    assert not captured["cuts"]


def test_rerender_puts_the_cuts_on_the_candidate_it_builds(clip_job, monkeypatch):
    """rerender_clip's only job here is to carry cuts onto the ClipCandidate."""
    from worker import rerender as rerender_module

    job, clip, _src = clip_job
    seen: dict = {}

    def fake_pipeline(source, options, **kwargs):
        seen["candidates"] = kwargs["explicit_candidates"]
        raise rerender_module.RerenderError("stop here")

    monkeypatch.setattr(rerender_module, "run_pipeline", fake_pipeline)
    with pytest.raises(rerender_module.RerenderError):
        rerender_module.rerender_clip(job, clip, cuts=[(1.0, 2.0)])

    candidate = seen["candidates"][0]
    assert candidate.cuts == [(1.0, 2.0)]
    assert (candidate.start, candidate.end) == (2.0, 8.0)


def test_rerender_defaults_to_no_cuts(clip_job, monkeypatch):
    from worker import rerender as rerender_module

    job, clip, _src = clip_job
    seen: dict = {}

    def fake_pipeline(source, options, **kwargs):
        seen["candidates"] = kwargs["explicit_candidates"]
        raise rerender_module.RerenderError("stop here")

    monkeypatch.setattr(rerender_module, "run_pipeline", fake_pipeline)
    with pytest.raises(rerender_module.RerenderError):
        rerender_module.rerender_clip(job, clip)
    assert seen["candidates"][0].cuts == []


# --------------------------------------------------------------------------- #
# The cache key must be derived in exactly one place
# --------------------------------------------------------------------------- #
def test_the_transcript_endpoint_and_transcribe_agree_on_the_cache_key(tmp_path):
    """Two derivations of the same key would quietly disagree; there must be one.

    `transcribe()` writes the entry and `clip_transcript` reads it. If the key were
    computed independently in each, a change to either would have no observable effect
    in the tests that cover it and the reader would miss every entry the writer wrote.
    """
    import worker.transcribe as tr
    from worker import clip_transcript as ct
    from worker import transcript_cache

    source = tmp_path / "agree.mp4"
    source.write_bytes(b"some bytes")

    key = tr.cache_key_for(source, language="en", translate=False, vocabulary="jargon")
    transcript_cache.store(
        key,
        Transcript(
            language="en", segments=[TranscriptSegment(0.0, 1.0, "hi", [Word(0.0, 1.0, "hi")])]
        ),
    )
    # The reader finds it with only the job's options to go on.
    recovered = ct.load_transcript(source, language="en", translate=False, vocabulary="jargon")
    assert recovered.text.strip() == "hi"


def test_a_different_vocabulary_is_a_different_transcript(tmp_path):
    import worker.transcribe as tr
    from worker import clip_transcript as ct
    from worker import transcript_cache

    source = tmp_path / "vocab.mp4"
    source.write_bytes(b"some bytes")
    transcript_cache.store(
        tr.cache_key_for(source, vocabulary="alpha"),
        Transcript(language="en", segments=[]),
    )
    with pytest.raises(ct.TranscriptUnavailable):
        ct.load_transcript(source, vocabulary="beta")


def test_a_missing_source_is_reported_as_unavailable(tmp_path):
    from worker import clip_transcript as ct

    with pytest.raises(ct.TranscriptUnavailable):
        ct.load_transcript(tmp_path / "gone.mp4")
