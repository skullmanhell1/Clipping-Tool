"""The measurement harness cannot report a number it did not measure.

`evaluation/` is the instrument this project judges itself with. Every quality claim about the
tool — including the ones in my own pull requests — is downstream of it, which makes a wrong
reading here more expensive than a wrong reading anywhere else: a defect in the renderer produces
a bad clip somebody notices, while a defect in the instrument produces a *number* nobody
questions.

Each test below pins one place where "did not measure" was emitted as the **ideal** value. That is
the recurring shape in this directory, and it is dangerous precisely because the neutral element
of every one of these metrics is also its best score:

| metric | "nothing measured" | which also means |
| --- | --- | --- |
| A/V sync offset | `0.0 ms`, `all_within_tolerance: True` | perfectly synced |
| caption lag | `0.0 ms` | perfectly aligned |
| WER | `0.00%` | a flawless transcript |
| golden-frame comparison | equal-length empty lists | matches the golden |
| selection F1 | `0.00` | a selector that does not work |

`evaluation/fidelity.py` is the module that already gets this right — `Metric_Reading` carries
`available` + `reason` and *no numbers* when unavailable, and `_reduce` **raises** rather than
returning zero for an empty reading set. It is the pattern the rest of these follow.
"""

from __future__ import annotations

import math

import pytest

from evaluation import caption_timing as ct
from evaluation import golden_render as gr
from evaluation import sync as sync_mod
from evaluation import wer as wer_mod
from evaluation.dataset import LabelledMoment
from evaluation.fidelity import _reduce
from evaluation.metrics import PRIMARY_IOU, AggregateScore, score_source
from evaluation.report import MIN_SOURCES_FOR_VERDICT, Report, render_text
from evaluation.sync import Sync_Report, report_many
from evaluation.wer import WerResult, format_comparison


# --------------------------------------------------------------------------- #
# A/V sync: an empty run is not a passing run                                  #
# --------------------------------------------------------------------------- #
def test_a_sync_record_with_no_measurements_does_not_claim_everything_passed():
    """`all()` of nothing is `True`, and that `True` was being committed to a file.

    `measure_sync` correctly *raises* rather than returning zero, so a run in which every clip
    failed leaves the caller with an empty list. Summarising that produced
    `worst_ms: 0.0, all_within_tolerance: true` — a perfect result — printed directly above a note
    telling the reader "This records what was measured. No defect is alleged."
    """
    record = report_many([])

    assert record["measurements_taken"] == 0
    assert record["worst_ms"] is None
    assert record["all_within_tolerance"] is None


def test_a_sync_record_with_measurements_still_reports_them():
    """The guard must not blanket-`None` a real run."""
    good = Sync_Report(
        offset_ms=12.0, audio_onset_s=1.0, video_onset_s=0.988, within_tolerance=True, label="a"
    )
    bad = Sync_Report(
        offset_ms=-300.0, audio_onset_s=1.0, video_onset_s=1.3, within_tolerance=False, label="b"
    )

    record = report_many([good, bad])
    assert record["measurements_taken"] == 2
    assert record["worst_ms"] == pytest.approx(300.0)
    assert record["all_within_tolerance"] is False

    only_good = report_many([good])
    assert only_good["all_within_tolerance"] is True
    assert only_good["worst_ms"] == pytest.approx(12.0)


# --------------------------------------------------------------------------- #
# A/V sync: the onset is the frame's own timestamp, not index / average fps     #
# --------------------------------------------------------------------------- #
#: `signalstats,metadata=print` output for four frames whose spacing is deliberately irregular —
#: what a variable-frame-rate source produces. The brightest frame is the third, whose true
#: presentation time is 0.9s. Dividing its *index* (2) by a 25fps average would say 0.08s.
_VFR_METADATA = """frame:0    pts:0        pts_time:0
lavfi.signalstats.YAVG=16.0
frame:1    pts:1000     pts_time:0.4
lavfi.signalstats.YAVG=17.0
frame:2    pts:2000     pts_time:0.9
lavfi.signalstats.YAVG=250.0
frame:3    pts:3000     pts_time:1.7
lavfi.signalstats.YAVG=18.0
"""


def test_the_video_onset_uses_the_frames_own_timestamp(monkeypatch, tmp_path):
    """A VFR source's flash must be placed at its `pts_time`, not `index / avg_frame_rate`.

    The old implementation parsed only the frame *index* and divided by the container's average
    rate — valid only when frames are evenly spaced. `make_sync_fixture(vfr=True)` exists
    specifically to produce "genuinely irregular frame durations", so measuring one directly was
    off by the accumulated spacing skew: it either alleged A/V drift that was not there or hid
    drift that was. Since this module exists because nothing else in the project would notice
    sync drift, a wrong reading is worse than no reading.
    """

    class _Proc:
        returncode = 0
        stdout = _VFR_METADATA
        stderr = ""

    monkeypatch.setattr(sync_mod.subprocess, "run", lambda *a, **k: _Proc())
    # Would be consulted only by the fallback; a wrong answer here proves it was not used.
    monkeypatch.setattr(sync_mod, "_stream_fps", lambda _p: 25.0)

    onset = sync_mod.video_onset(tmp_path / "vfr.mp4")

    assert onset == pytest.approx(0.9)
    assert onset != pytest.approx(2 / 25.0)


def test_the_video_onset_falls_back_to_the_average_rate_when_there_is_no_timestamp(
    monkeypatch, tmp_path
):
    """A decoder that emits no `pts_time` still yields the previous answer, not an error."""
    without_pts = "\n".join(
        line for line in _VFR_METADATA.splitlines() if "pts_time" not in line or "YAVG" in line
    )
    indexed = "\n".join(
        f"frame:{i}" if "frame:" in line else line
        for i, line in enumerate(without_pts.splitlines())
    )
    # Rebuild a frame/luma pairing with no pts_time at all.
    payload = "".join(
        f"frame:{i}\nlavfi.signalstats.YAVG={luma}\n"
        for i, luma in enumerate((16.0, 17.0, 250.0, 18.0))
    )
    del indexed

    class _Proc:
        returncode = 0
        stdout = payload
        stderr = ""

    monkeypatch.setattr(sync_mod.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(sync_mod, "_stream_fps", lambda _p: 25.0)

    assert sync_mod.video_onset(tmp_path / "cfr.mp4") == pytest.approx(2 / 25.0)


# --------------------------------------------------------------------------- #
# Caption timing: silence is not speech, and an unmeasurable clip is not 0 ms   #
# --------------------------------------------------------------------------- #
def _fake_decode(monkeypatch, samples: bytes):
    class _Proc:
        returncode = 0
        stdout = samples
        stderr = b""

    monkeypatch.setattr(ct, "_SPEECH_MASK_TEST_HOOK", None, raising=False)
    import subprocess as _sp

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Proc())


def test_a_silent_clip_is_refused_rather_than_read_as_continuous_speech(monkeypatch):
    """The speech threshold is relative to the clip's own peak, so silence reads as all-speech.

    On digital silence every RMS clamps to the epsilon, so every frame sits within
    `SPEECH_FLOOR_DB` of the peak and the mask is **all True** — "sound is happening in every
    frame". `coverage_overlap` then returned the fraction of the clip the captions cover: a
    plausible number about nothing.

    The module's docstring guards the opposite direction (a missing binary yielding an all-False
    mask). That case cannot happen. This one can, for any clip whose audio track survived the mux
    carrying no signal.
    """
    silence = b"\x00\x00" * 16000 * 2  # two seconds of exact digital silence
    _fake_decode(monkeypatch, silence)

    with pytest.raises(RuntimeError, match="no audible signal"):
        ct.speech_mask("silent.mp4")


def test_a_clip_shorter_than_one_analysis_frame_is_refused(monkeypatch):
    """Previously returned `[]`, which every consumer turned into a perfect reading."""
    _fake_decode(monkeypatch, b"\x01\x00" * 8)  # far less than one 20 ms hop

    with pytest.raises(RuntimeError, match="too short"):
        ct.speech_mask("tiny.mp4")


def test_real_audio_still_produces_a_mask(monkeypatch):
    """The two refusals must not reject material that can be measured."""
    import struct

    loud = b"".join(struct.pack("<h", 12000 if (i // 3200) % 2 == 0 else 4) for i in range(32000))
    _fake_decode(monkeypatch, loud)

    mask = ct.speech_mask("real.mp4")
    assert mask, "no envelope produced"
    assert any(mask) and not all(mask), "the mask should distinguish the bursts from the gaps"


def test_an_unmeasurable_clip_does_not_report_a_perfect_lag():
    """`0.0 ms` lag is the *best* reading, and it was returned for an unmeasurable clip.

    `scripts/measure_caption_sync.py` appends each lag to a list it takes the median of and prints
    as a verdict, so enough fabricated zeroes medianed a genuine constant offset out of existence.
    """
    with pytest.raises(RuntimeError, match="non-empty speech mask"):
        ct.best_fit_lag_ms([], [])
    with pytest.raises(RuntimeError, match="non-empty speech mask"):
        ct.coverage_overlap([], [])


def test_a_cue_occupies_the_hops_before_its_end_not_one_extra():
    """`range(first, last + 1)` marked one extra 20 ms hop per event.

    That inflated both the intersection and the union on every measurement — by an amount
    comparable to the 20 ms precision this module claims — and biased the argmax of the ±2 s
    search, because the shift windows are not symmetric.

    A cue covering exactly [0.00, 0.10) at a 0.02 hop occupies hops 0-4, i.e. five of them.
    """
    event = ct.Rendered_Event(start=0.0, end=0.10, text="x")
    mask = [True] * 5 + [False] * 5

    # Exactly the speech: intersection 5, union 5.
    assert ct.coverage_overlap([event], mask, hop=0.02) == pytest.approx(1.0)

    # The old off-by-one lit hop 5 as well, which would make the union 6 and the score 5/6.
    assert ct.coverage_overlap([event], mask, hop=0.02) != pytest.approx(5 / 6)


# --------------------------------------------------------------------------- #
# Fidelity: the mean and the frame count must share a denominator              #
# --------------------------------------------------------------------------- #
def test_a_mixed_reading_reports_how_many_frames_the_mean_covers():
    """PSNR returns `inf` for any bit-identical frame, so mixed readings are routine.

    The mean is taken over the finite frames — the strict arithmetic mean would be infinity, and
    one perfect frame would erase the measurement of every other. But `frames` counted *all* of
    them, so `mean` and `frames` were computed over different denominators with nothing saying so,
    and `compare` would then difference two such means across runs where the number of identical
    frames changed and call it a fidelity regression.
    """
    reading = _reduce([40.0, 42.0, math.inf, 44.0], "psnr")

    assert reading.frames == 4
    assert reading.finite_frames == 3
    assert reading.mean == pytest.approx(42.0)  # over the finite three, and now stated
    assert math.isinf(reading.minimum) is False
    assert reading.to_dict()["finite_frames"] == 3


def test_an_all_identical_reading_is_still_infinite():
    """Every frame identical genuinely is an infinite PSNR, and must not become a finite number."""
    reading = _reduce([math.inf, math.inf], "psnr")
    assert math.isinf(reading.mean)
    assert reading.frames == 2
    assert reading.finite_frames == 0
    assert reading.to_dict()["mean"] == "inf"


def test_an_ordinary_reading_is_unchanged():
    reading = _reduce([0.98, 0.99, 1.0], "ssim")
    assert reading.frames == reading.finite_frames == 3
    assert reading.mean == pytest.approx(0.99)


def test_no_readings_still_raises_rather_than_scoring_zero():
    """The behaviour this whole file is modelled on — do not regress it."""
    with pytest.raises(Exception, match="no per-frame readings"):
        _reduce([], "ssim")


# --------------------------------------------------------------------------- #
# WER: a model with nothing to score is not the best model                     #
# --------------------------------------------------------------------------- #
def test_a_result_with_no_reference_words_is_not_measured():
    assert (
        WerResult(reference_words=0, substitutions=0, deletions=0, insertions=0).measured is False
    )
    assert (
        WerResult(reference_words=10, substitutions=1, deletions=0, insertions=0).measured is True
    )
    # `aggregate` of nothing produces exactly the unmeasured shape.
    assert wer_mod.aggregate([]).measured is False


def test_an_unmeasured_model_is_not_ranked_best():
    """Zero reference words scores 0.00% — the best possible WER — so it sorted to the top.

    Every other model's "vs best" delta, which the module calls the point of the whole exercise,
    was then computed against that fabricated zero.
    """
    real = WerResult(reference_words=1000, substitutions=50, deletions=20, insertions=10)
    better = WerResult(reference_words=1000, substitutions=20, deletions=10, insertions=5)
    empty = WerResult(reference_words=0, substitutions=0, deletions=0, insertions=0)

    table = format_comparison([("small", real), ("skipped", empty), ("medium", better)])
    lines = [line for line in table.splitlines() if line and not line.startswith("-")]

    # The first data row is the best *measured* model, not the empty one.
    data_rows = [line for line in lines[1:] if line[:14].strip() in {"small", "medium", "skipped"}]
    assert data_rows[0].split()[0] == "medium"
    # The unmeasured model is shown, but as n/a rather than as a score.
    skipped_row = next(row for row in data_rows if row.startswith("skipped"))
    assert "n/a" in skipped_row
    assert "0.00%" not in skipped_row
    assert "had no reference text" in table

    # And the real best model's delta is 0, i.e. measured against itself.
    medium_row = next(row for row in data_rows if row.startswith("medium"))
    assert "+0.00%" in medium_row


# --------------------------------------------------------------------------- #
# Selection report: a verdict needs measurements                               #
# --------------------------------------------------------------------------- #
def _report(*, sources: int, failed: int, selector_f1_beats: bool) -> Report:
    # `TimeRange` is attribute-based (`.start`/`.end`), so real moments rather than tuples.
    labels = [LabelledMoment(start=0.0, end=10.0)]
    good = [LabelledMoment(start=0.0, end=10.0)]
    miss = [LabelledMoment(start=50.0, end=60.0)]
    chosen = good if selector_f1_beats else miss
    selector = AggregateScore(
        label="selector",
        k=1,
        sources=[
            score_source(name=f"s{i}", predictions=chosen, labels=labels, k=1)
            for i in range(sources)
        ],
    )
    baseline = AggregateScore(
        label="baseline:uniform",
        k=1,
        sources=[
            score_source(name=f"s{i}", predictions=miss, labels=labels, k=1) for i in range(sources)
        ],
    )
    return Report(
        dataset_size=sources,
        moment_count=sources,
        selector=selector,
        baselines=[baseline],
        errors=[(f"e{i}", "RuntimeError: boom") for i in range(failed)],
    )


def test_a_report_where_every_source_failed_makes_no_claim():
    """Errored sources are scored as zero predictions, so total failure renders as a bad selector.

    Precision and recall both return 0.0 for "no predictions" and "no labels" alike, so a run in
    which every source raised produced a full table of 0.00 and then asserted that the selector
    "is not selecting; it is sampling, and the LLM call is being paid for nothing" — a maximally
    strong quality claim built from zero measurements, on a default exit code of 0.
    """
    report = _report(sources=3, failed=3, selector_f1_beats=True)

    assert report.sources_measured == 0
    assert report.beats_baseline is False
    payload = report.to_dict()
    assert payload["dataset"]["sources_measured"] == 0
    assert payload["dataset"]["sources_failed"] == 3
    assert payload["beats_best_baseline"] is False

    text = render_text(report)
    assert "NOTHING WAS MEASURED" in text
    assert "is not selecting" not in text  # the strong claim is withheld


def test_a_partial_report_says_so():
    report = _report(sources=6, failed=2, selector_f1_beats=True)

    assert report.sources_measured == 4
    text = render_text(report)
    assert "PARTIAL: 4 of 6" in text
    assert report.to_dict()["dataset"]["sources_failed"] == 2


def test_a_small_sample_is_labelled_an_anecdote():
    """A bare `>` on one pair of F1 point estimates reads the same at n=1 and n=20."""
    report = _report(sources=2, failed=0, selector_f1_beats=True)

    assert report.sources_measured == 2 < MIN_SOURCES_FOR_VERDICT
    text = render_text(report)
    assert "anecdote" in text
    # The comparison is still made — it is qualified, not suppressed.
    assert f"At IoU {PRIMARY_IOU}" in text


def test_a_full_report_is_not_qualified():
    """The guards must not stamp a caveat on a complete run."""
    report = _report(sources=MIN_SOURCES_FOR_VERDICT + 1, failed=0, selector_f1_beats=True)

    text = render_text(report)
    assert "NOTHING WAS MEASURED" not in text
    assert "PARTIAL" not in text
    assert "anecdote" not in text
    assert report.beats_baseline is True


# --------------------------------------------------------------------------- #
# Golden render: an unchecked level check is not a passed one                  #
# --------------------------------------------------------------------------- #
def test_a_golden_without_luma_fields_does_not_silently_pass_the_level_check():
    """The structural hash is blind to a grade — that is why mean and spread exist.

    Defaulting the absent gap to 0.0 put it inside every tolerance, so a golden written before
    those fields existed (or hand-edited) passed a *graded* render while reporting "matches the
    golden", with nothing saying the brightness and contrast checks had not run.
    """
    frames = [gr.FrameHash(at=0.5, hash="ff00", mean=120.0, spread=30.0)]
    old_golden = [{"at": 0.5, "hash": "ff00"}]  # no mean, no spread

    result = gr.compare(frames, old_golden)

    assert result.level_unchecked == 1
    assert "NOT checked" in result.summary()
    assert "Re-freeze" in result.summary()


def test_a_complete_golden_reports_no_unchecked_frames():
    frames = [gr.FrameHash(at=0.5, hash="ff00", mean=120.0, spread=30.0)]
    golden = [{"at": 0.5, "hash": "ff00", "mean": 120.0, "spread": 30.0}]

    result = gr.compare(frames, golden)
    assert result.ok is True
    assert result.level_unchecked == 0
    assert "NOT checked" not in result.summary()


def test_a_brightness_change_is_still_caught():
    """The level check must actually fail when it can run."""
    frames = [gr.FrameHash(at=0.5, hash="ff00", mean=200.0, spread=30.0)]
    golden = [{"at": 0.5, "hash": "ff00", "mean": 120.0, "spread": 30.0}]

    assert gr.compare(frames, golden).ok is False
