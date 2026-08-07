"""A/V sync verification (M11).

The instrument is falsified first: a **deliberate 200 ms offset must measure as 200 ms** before any
synchronised reading is worth anything. A detector that always answers zero passes every
"synchronised" test ever written, which is why that test comes second here rather than first.

Then the three drift-prone paths the spec names, one test each because each exercises a different
mechanism:

* a clip cut from a **non-zero start** — the seek path, plus audio priming that varies between
  ffmpeg versions;
* a **VFR source** — `output_fps=30` resamples VFR to CFR, and resampling video without touching
  audio is a classic drift source;
* a **keep-interval concat** — `filler.apply_keep_intervals` uses `afade` rather than `acrossfade`
  *specifically because* a crossfade shifts the timeline. That reasoning is sound and is verified
  here rather than trusted.

**No defect is alleged.** These record what the pipeline currently does.
"""

from __future__ import annotations

import shutil

import pytest

from config import settings as app_settings
from evaluation import sync
from evaluation.sync import SyncError

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")
FFPROBE = shutil.which(app_settings.ffprobe_binary) or shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="no ffmpeg/ffprobe on PATH; sync measurement needs both",
)


# --- 5.4: the instrument is falsifiable ----------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_deliberate_offset_is_measured_as_that_offset(tmp_path):
    """R7.4, R7.5. The test that makes every other reading in this module meaningful.

    A 200 ms audio delay must read as approximately +200 ms. Tolerance is one frame at 25 fps,
    because the visual event can only be located to the frame it occupies — that granularity is a
    property of video, not a weakness of the detector.
    """
    path = sync.make_sync_fixture(tmp_path / "offset.mp4", audio_offset=0.200)
    report = sync.measure_sync(path)
    assert report.offset_ms == pytest.approx(200.0, abs=40.0), report
    assert report.within_tolerance is False
    assert report.audio_onset_s > report.video_onset_s


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_negative_offset_is_measured_with_its_sign(tmp_path):
    """Audio arriving *early* is a different defect and must not be reported as the same one."""
    path = sync.make_sync_fixture(
        tmp_path / "early.mp4", event_at=1.5, audio_offset=-0.200
    )
    report = sync.measure_sync(path)
    assert report.offset_ms == pytest.approx(-200.0, abs=40.0), report


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_synchronised_fixture_reads_near_zero(tmp_path):
    """R7.4. Only meaningful because the two tests above prove the detector can say otherwise."""
    path = sync.make_sync_fixture(tmp_path / "sync.mp4")
    report = sync.measure_sync(path)
    assert abs(report.offset_ms) <= sync.TOLERANCE_MS, report
    assert report.within_tolerance is True


@requires_ffmpeg
@pytest.mark.real_binary
def test_both_onsets_are_reported_not_only_the_difference(tmp_path):
    """When a reading looks wrong it is usually one detector failing, not genuine drift.

    The difference alone cannot distinguish those, so both onsets are part of the report.
    """
    path = sync.make_sync_fixture(tmp_path / "onsets.mp4", event_at=1.0)
    report = sync.measure_sync(path)
    assert report.video_onset_s == pytest.approx(1.0, abs=0.05)
    assert report.audio_onset_s == pytest.approx(1.0, abs=0.05)


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_measurement_is_reported_even_when_it_fails_tolerance(tmp_path):
    """R4.6. A run reading 8 ms differs meaningfully from one reading 0 ms.

    A bare pass/fail discards the trend, which is the only thing that would reveal slow drift
    across releases.
    """
    path = sync.make_sync_fixture(tmp_path / "big.mp4", audio_offset=0.400)
    report = sync.measure_sync(path)
    assert report.within_tolerance is False
    assert report.offset_ms > 300.0, "the number must survive failing the check"
    assert "reported whether or not" in report.note


# --- refusals rather than plausible zeros ---------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_silent_file_is_refused_not_reported_as_synchronised(tmp_path):
    """The failure that would otherwise read as perfect sync.

    With no audio event to locate, an onset of 0.0 differences against the video onset to produce
    a large offset — or, if the video onset were also missing, exactly 0.0, which is
    indistinguishable from success. Refusing is the only safe answer.
    """
    silent = tmp_path / "silent.mp4"
    import subprocess

    proc = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=2",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-shortest",
            "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=white@1.0:t=fill:enable='between(t,1.0,1.04)'",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(silent),
        ],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    with pytest.raises(SyncError, match="silent|onset"):
        sync.measure_sync(silent)


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_file_with_no_visual_event_is_refused(tmp_path):
    """Symmetrically: a missing flash must not be located at frame 0."""
    import subprocess

    dark = tmp_path / "dark.mp4"
    proc = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=2",
            "-f", "lavfi", "-i",
            "aevalsrc='if(between(t,1.0,1.05), 0.8*sin(2*PI*1000*t), 0)':d=2:s=48000",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(dark),
        ],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    with pytest.raises(SyncError, match="no visual event"):
        sync.measure_sync(dark)


# --- 5.5: the three drift-prone paths -------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_path_a_a_clip_cut_from_a_non_zero_start_stays_in_sync(tmp_path):
    """(a) The seek path. `cut_segment` places `-ss` before `-i`.

    That is frame-accurate under re-encoding in modern ffmpeg and **this test alleges no defect**
    — it exists because nothing measured it, and audio priming behaviour around a seek has changed
    between ffmpeg versions before.

    The event is placed 1.0 s after the cut point, so a seek that landed early or late shows up as
    a shifted event rather than as a missing one.
    """
    from worker import ffmpeg_utils as fu

    source = sync.make_sync_fixture(tmp_path / "src_a.mp4", event_at=2.0, duration=4.0)
    cut = tmp_path / "cut_a.mp4"
    fu.cut_segment(source, 1.0, 3.5, cut)

    report = sync.measure_sync(cut, label="cut from non-zero start")
    assert abs(report.offset_ms) <= sync.TOLERANCE_MS, report
    # The event was at 2.0 s in the source and the cut began at 1.0 s, so it should now sit at
    # ~1.0 s. Asserted so a *seek* error is distinguishable from a sync error: the two produce
    # different symptoms and only one of them is what this test is named for.
    assert report.video_onset_s == pytest.approx(1.0, abs=0.15), report


@requires_ffmpeg
@pytest.mark.real_binary
def test_path_b_a_vfr_source_survives_normalisation_to_cfr(tmp_path):
    """(b) VFR -> CFR. `config.py` calls VFR "every screen recording and most phone footage".

    `output_fps=30` resamples the video and does nothing to the audio, which is the textbook way
    to introduce drift. Measured through a real re-encode with `-r`.
    """
    import subprocess

    source = sync.make_sync_fixture(tmp_path / "src_b.mp4", event_at=1.0, duration=3.0, vfr=True)
    normalised = tmp_path / "cfr_b.mp4"
    proc = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-r", "30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", str(normalised),
        ],
        capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, proc.stderr

    report = sync.measure_sync(normalised, label="vfr normalised to 30fps")
    assert abs(report.offset_ms) <= sync.TOLERANCE_MS, report


@requires_ffmpeg
@pytest.mark.real_binary
def test_path_c_a_keep_interval_concat_does_not_shift_the_timeline(tmp_path):
    """(c) The concat seam. `filler.apply_keep_intervals` uses `afade`, not `acrossfade`.

    The reasoning recorded in that module is that a crossfade *shifts the timeline*, since it
    consumes overlap from both sides. That is correct, and it is exactly the kind of correct
    reasoning worth verifying rather than trusting — especially as interior-silence work will
    create many more seams than exist today.

    The event is kept inside the **second** interval, so anything the first seam did to the
    timeline shows up as a shifted onset.
    """
    from worker.effects import filler
    from worker.effects.filler import Interval

    source = sync.make_sync_fixture(tmp_path / "src_c.mp4", event_at=2.5, duration=4.0)
    joined = tmp_path / "concat_c.mp4"
    # Drop 0.5 s from the middle, keeping the event well inside the second kept region.
    filler.apply_keep_intervals(
        source, [Interval(0.0, 1.5), Interval(2.0, 4.0)], joined
    )

    report = sync.measure_sync(joined, label="keep-interval concat")
    assert abs(report.offset_ms) <= sync.TOLERANCE_MS, report


# --- 5.6: recording the finding -------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_many_readings_collect_into_a_committable_record(tmp_path):
    """R4.8 / task 5.6: write the finding, whatever it is, and allege nothing."""
    reports = [
        sync.measure_sync(sync.make_sync_fixture(tmp_path / "r1.mp4"), label="baseline"),
        sync.measure_sync(
            sync.make_sync_fixture(tmp_path / "r2.mp4", event_at=1.5), label="later event"
        ),
    ]
    record = sync.report_many(reports)
    assert record["all_within_tolerance"] is True, record
    assert len(record["measurements"]) == 2
    assert record["worst_ms"] < sync.TOLERANCE_MS
    assert "No defect is alleged" in record["note"]


def test_the_tolerance_is_documented_as_a_check_not_as_the_measurement():
    """The distinction the report depends on: tolerance gates a check, it never edits a number."""
    assert sync.TOLERANCE_MS == 20.0
    assert "reported whether or not it is within tolerance" in sync.Sync_Report.note


def test_the_module_does_not_allege_a_defect_in_the_seek_path():
    """Pinned, because the temptation to write "fixes A/V drift" in a changelog is real.

    `-ss` before `-i` is frame-accurate under re-encoding. This spec observes that nothing
    measured it; that is a different claim and the module should keep saying so.
    """
    doc = sync.__doc__ or ""
    assert "No defect is alleged" in doc
    assert "frame-accurate" in doc
