"""Render fidelity measurement (M9): SSIM, PSNR, VMAF.

The instrument has to be trustworthy before anything measured with it means anything, so these
tests attack the instrument rather than the renderer.

**Every expectation is derived independently of the parser** (R7.5). A self-comparison must be
exactly 1.0 because the files are the same bytes, and a CRF-45 render must score worse because of
how it was produced — not because `fidelity.py` says so. A test that computes its expectation with
the code under test proves only that the code is self-consistent, which is the failure mode the
whole `evaluation/` package exists to avoid.

One finding from building this is worth stating up front, because it shaped a test: the
self-comparison identity test **did not** catch the real parsing bug in this module. ffmpeg prints
a summary line for `ssim` containing `All:`, which a naive pattern counts as an extra frame — and
on identical input that summary is also 1.0, so SSIM still came back exactly 1.0. What caught it
was the parsed **frame count** disagreeing with PSNR's on the same file. Frame count is therefore
asserted here as a first-class expectation.
"""

from __future__ import annotations

import math
import shutil
import subprocess

import pytest

from config import settings as app_settings
from evaluation import fidelity
from evaluation.fidelity import (
    FidelityError,
    Metric,
    MisalignedComparison,
)
from worker.engines.capabilities import Capability_Status

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")
FFPROBE = shutil.which(app_settings.ffprobe_binary) or shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="no ffmpeg/ffprobe on PATH; fidelity measurement needs both",
)

#: 2 s at 25 fps. Small enough that a VMAF pass is cheap, long enough that a mean and a minimum
#: can differ.
FIXTURE_FRAMES = 50


def _render(path, *, size="320x180", duration=2, crf=20, source=None, extra=()):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    if source is None:
        cmd += ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={duration}"]
    else:
        cmd += ["-i", str(source)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
    cmd += list(extra)
    cmd += [str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr
    return path


@pytest.fixture
def faithful(tmp_path):
    return _render(tmp_path / "faithful.mp4")


@pytest.fixture
def degraded(tmp_path, faithful):
    """The *same content* re-encoded at CRF 45.

    Re-encoded from `faithful` rather than generated afresh, so the two are frame-aligned and the
    only difference is the quantiser. Generating both from `lavfi` independently would also work
    here but would make the comparison depend on `testsrc2` being deterministic, which is a
    property of ffmpeg rather than of this project.
    """
    return _render(tmp_path / "degraded.mp4", source=faithful, crf=45)


def _prober_without_vmaf():
    def prober(capability_id: str) -> Capability_Status:
        available = capability_id != fidelity.VMAF_CAPABILITY
        return Capability_Status(capability_id, available, "injected: libvmaf removed")

    return prober


def _prober_all_present():
    def prober(capability_id: str) -> Capability_Status:
        return Capability_Status(capability_id, True, "injected")

    return prober


# --- 1.3: capability resolution ------------------------------------------------------------


def test_ssim_and_psnr_are_always_reported_as_available():
    """They need no optional ffmpeg component, so they are the floor.

    Asserted so that a future refactor cannot make the always-available metrics conditional and
    leave a build with no fidelity measurement at all.
    """
    names = {a.metric: a for a in fidelity.available_metrics(_prober_without_vmaf())}
    assert names[Metric.SSIM.value].available is True
    assert names[Metric.PSNR.value].available is True


def test_absent_libvmaf_is_reported_with_a_reason_and_is_not_a_pass():
    """R1.4, R1.5, R7.9. The three states must stay distinguishable.

    "measured and fine", "measured and bad", and "could not measure" are three different
    answers. An omitted key collapses the third into the first for any caller doing
    `report.get("vmaf", ok)`, and a zero collapses it into the second. Both have happened in
    real measurement code.
    """
    entry = {a.metric: a for a in fidelity.available_metrics(_prober_without_vmaf())}[
        Metric.VMAF.value
    ]
    assert entry.available is False
    assert entry.reason, "an unavailable metric must carry a named reason"
    assert "libvmaf" in entry.reason.lower() or "vmaf" in entry.reason.lower()


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_run_without_libvmaf_still_reports_ssim_and_psnr(faithful, tmp_path):
    """R1.4: VMAF's absence degrades the run, it does not fail it."""
    report = fidelity.measure(faithful, faithful, prober=_prober_without_vmaf(), log_dir=tmp_path)
    ssim = report.reading(Metric.SSIM.value)
    vmaf = report.reading(Metric.VMAF.value)
    assert ssim is not None and ssim.available is True
    assert ssim.frames == FIXTURE_FRAMES
    # Present in the report as an explicit state, not missing from it.
    assert vmaf is not None, "VMAF must appear in the report even when unavailable"
    assert vmaf.available is False
    assert vmaf.reason
    assert vmaf.mean == 0.0 and vmaf.frames == 0, (
        "an unavailable metric must carry no numbers; a 0.0 that looks measured is worse than none"
    )


def test_an_unavailable_reading_serialises_without_implying_a_score():
    """The JSON a reader or a diff will actually see."""
    entry = {a.metric: a for a in fidelity.available_metrics(_prober_without_vmaf())}[
        Metric.VMAF.value
    ]
    assert entry.to_dict()["available"] is False
    assert entry.to_dict()["reason"]


# --- 2.5: self-comparison identity ---------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_file_compared_against_itself_is_perfect(faithful, tmp_path):
    """R7.1, R7.5, R1.7. Expectations derived from the inputs being identical bytes.

    SSIM is exactly 1.0 and PSNR is infinite. Any parsing error that scales, offsets or misreads
    a field breaks at least one of these — with the documented exception that produced the
    frame-count assertion below.
    """
    report = fidelity.measure(faithful, faithful, prober=_prober_all_present(), log_dir=tmp_path)

    ssim = report.reading(Metric.SSIM.value)
    assert ssim.mean == 1.0, ssim
    assert ssim.minimum == 1.0, ssim

    psnr = report.reading(Metric.PSNR.value)
    assert math.isinf(psnr.mean), psnr
    assert math.isinf(psnr.minimum), psnr


@requires_ffmpeg
@pytest.mark.real_binary
def test_every_metric_parses_the_same_number_of_frames(faithful, tmp_path):
    """The assertion that caught the real parsing bug in this module.

    ffmpeg prints an `ssim` **summary** line containing `All:`, which a naive per-frame pattern
    counts as an extra frame — 51 readings for a 50-frame file, with the summary folded into the
    mean. On identical input the summary is also 1.0, so the identity test above stayed green.
    Only cross-checking the counts against each other exposed it.

    The frame count is also checked against the fixture's known length, so all three parsers
    agreeing on a wrong number cannot pass either.
    """
    report = fidelity.measure(faithful, faithful, prober=_prober_all_present(), log_dir=tmp_path)
    counts = {r.frames for r in report.readings if r.available}
    assert counts == {FIXTURE_FRAMES}, (
        f"parsers disagree or miscount: {[(r.metric, r.frames) for r in report.readings]}"
    )


@requires_ffmpeg
@pytest.mark.real_binary
def test_vmaf_on_an_identical_pair_is_near_but_not_at_one_hundred(faithful, tmp_path):
    """A property of VMAF, pinned so nobody debugs it as a defect here.

    VMAF includes temporal (motion) features and frame 0 has no predecessor, so its score dips —
    measured at 97.4 against a steady state of 99.956. That makes ~97.4 the instrument's floor
    for a *minimum* on any clip, and it means a minimum moving away from that value is the
    signal rather than the value itself.

    Bounded loosely on purpose: tightening it would turn this into a drift test for somebody
    else's model version, and the report already carries the cross-build caveat.
    """
    report = fidelity.measure(faithful, faithful, prober=_prober_all_present(), log_dir=tmp_path)
    vmaf = report.reading(Metric.VMAF.value)
    assert vmaf.available is True
    assert 99.0 < vmaf.mean <= 100.0, vmaf
    assert vmaf.minimum < vmaf.mean, "frame 0's degenerate motion feature should pull the minimum"
    assert vmaf.minimum > 90.0, vmaf


# --- 2.6: degradation orders correctly -----------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_worse_encode_scores_worse_on_every_available_metric(faithful, degraded, tmp_path):
    """R7.2, R7.5, R7.6. Catches sign and ordering errors a self-comparison cannot.

    The expectation comes from how the fixture was produced — CRF 45 against CRF 20 on identical
    source frames — and not from any reading this module takes. If the direction of any metric
    were inverted, a self-comparison would still be perfect and only this test would notice.
    """
    good = fidelity.measure(
        faithful, faithful, prober=_prober_all_present(), log_dir=tmp_path / "a"
    )
    bad = fidelity.measure(degraded, faithful, prober=_prober_all_present(), log_dir=tmp_path / "b")

    assert bad.reading(Metric.SSIM.value).mean < good.reading(Metric.SSIM.value).mean
    assert bad.reading(Metric.SSIM.value).mean < 1.0
    # PSNR: finite is worse than infinite, which is the ordering that matters.
    assert math.isfinite(bad.reading(Metric.PSNR.value).mean)
    assert bad.reading(Metric.VMAF.value).mean < good.reading(Metric.VMAF.value).mean


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_minimum_is_never_above_the_mean(faithful, degraded, tmp_path):
    """A cheap invariant that catches a mean/minimum swap.

    Swapping them is the single most plausible mistake in this module — the fields are adjacent,
    the same type, and both plausible — and it would make every reading look slightly better
    than it is, which is the direction nobody questions.
    """
    report = fidelity.measure(degraded, faithful, prober=_prober_all_present(), log_dir=tmp_path)
    for reading in report.readings:
        if reading.available:
            assert reading.minimum <= reading.mean, reading


# --- 2.7: minima react where means do not --------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_one_damaged_frame_moves_the_minimum_and_barely_the_mean(tmp_path, faithful):
    """R1.8, and the whole argument for reporting both.

    A single badly-reproduced frame is what a scene-change encode decision produces and what a
    viewer notices. Averaged over 50 frames it is nearly invisible; as a minimum it is obvious.

    The damage is applied to one frame by compositing a hard block over it for a single frame's
    duration, so the other 49 frames are untouched and the contrast between the two statistics
    is attributable.
    """
    damaged = tmp_path / "one_bad_frame.mp4"
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(faithful),
            "-vf",
            "drawbox=x=0:y=0:w=iw:h=ih:color=black@1.0:t=fill:enable='eq(n,10)'",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(damaged),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr

    report = fidelity.measure(damaged, faithful, prober=_prober_all_present(), log_dir=tmp_path)
    ssim = report.reading(Metric.SSIM.value)
    assert ssim.frames == FIXTURE_FRAMES
    # The mean stays high because 49 frames are near-perfect; the minimum collapses.
    assert ssim.mean > 0.9, ssim
    assert ssim.minimum < 0.5, ssim
    assert ssim.mean - ssim.minimum > 0.4, (
        "a single damaged frame must be visible in the minimum and not in the mean; "
        "reporting only a mean would hide it entirely"
    )


# --- 2.8: misalignment is refused ----------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_resolution_mismatch_is_refused(tmp_path, faithful):
    """R1.6. Scaling one to match would measure the scaler instead of the encode."""
    other = _render(tmp_path / "bigger.mp4", size="640x360")
    with pytest.raises(MisalignedComparison, match="resolution mismatch"):
        fidelity.measure(other, faithful, prober=_prober_all_present(), log_dir=tmp_path)


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_frame_count_mismatch_is_refused(tmp_path, faithful):
    """R1.6, and the dangerous half.

    A frame-count mismatch does not error inside ffmpeg — it compares frame N against N+1 for
    the whole remainder and returns a number that is plausible, terrible and about nothing. That
    number is far worse than a refusal, because somebody will act on it.
    """
    shorter = _render(tmp_path / "shorter.mp4", duration=1)
    with pytest.raises(MisalignedComparison, match="frame-count mismatch"):
        fidelity.measure(shorter, faithful, prober=_prober_all_present(), log_dir=tmp_path)


@requires_ffmpeg
@pytest.mark.real_binary
def test_alignment_is_checked_before_any_measurement_is_taken(tmp_path, faithful, monkeypatch):
    """The guard runs first, so a misaligned pair never costs a VMAF pass and never yields data.

    Ordering asserted rather than assumed: a guard that runs after the measurement still returns
    the right exception while having already computed — and possibly logged — the number it
    exists to suppress.
    """
    called: list[str] = []
    monkeypatch.setattr(fidelity, "measure_ssim", lambda *a, **k: called.append("ssim") or None)
    shorter = _render(tmp_path / "shorter2.mp4", duration=1)
    with pytest.raises(MisalignedComparison):
        fidelity.measure(shorter, faithful, prober=_prober_all_present(), log_dir=tmp_path)
    assert called == [], "no metric may run before the alignment guard has passed"


# --- 3.x: reporting and comparison ---------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_report_carries_provenance_and_the_cross_build_caveat(faithful, tmp_path):
    """R2.1, R2.2, R2.6. A report outlives its context, so the caveat travels with the numbers."""
    report = fidelity.measure(faithful, faithful, prober=_prober_all_present(), log_dir=tmp_path)
    prov = report.provenance
    for key in ("ffmpeg_version", "encoder", "x264_crf", "x264_preset", "revision"):
        assert key in prov, key
    assert "not comparable across ffmpeg builds" in report.caveat
    assert report.size_bytes > 0, "cost is recorded beside quality (R2.3)"


def test_compare_refuses_readings_from_different_ffmpeg_builds():
    """R2.7 / task 3.3. The arithmetic works perfectly, which is exactly the problem.

    Differencing two builds produces a number that looks like a finding and is an artefact of a
    filter implementation or a libvmaf model version changing. A refusal is the only safe answer,
    and it has to be enforced in code because the caveat in the report is prose nobody diffs.
    """
    before = {
        "provenance": {"ffmpeg_version": "ffmpeg version 6.1"},
        "readings": [{"metric": "ssim", "mean": 0.98, "minimum": 0.90, "available": True}],
    }
    after = {
        "provenance": {"ffmpeg_version": "ffmpeg version 7.0.2"},
        "readings": [{"metric": "ssim", "mean": 0.99, "minimum": 0.92, "available": True}],
    }
    with pytest.raises(FidelityError, match="different ffmpeg builds"):
        fidelity.compare(before, after)


def test_compare_names_every_metric_that_moved_in_both_directions():
    """R2.7: both directions, and the delta signed."""
    prov = {"ffmpeg_version": "ffmpeg version 7.0.2"}
    before = {
        "provenance": prov,
        "readings": [
            {"metric": "ssim", "mean": 0.980, "minimum": 0.900, "available": True},
            {"metric": "vmaf", "mean": 80.0, "minimum": 60.0, "available": True},
        ],
    }
    after = {
        "provenance": prov,
        "readings": [
            {"metric": "ssim", "mean": 0.990, "minimum": 0.910, "available": True},
            {"metric": "vmaf", "mean": 75.0, "minimum": 55.0, "available": True},
        ],
    }
    result = fidelity.compare(before, after)
    by_metric = {m["metric"]: m for m in result["metrics"]}
    assert by_metric["ssim"]["mean"]["delta"] == pytest.approx(0.01)
    assert by_metric["vmaf"]["mean"]["delta"] == pytest.approx(-5.0)


def test_compare_makes_no_significance_or_pass_fail_claim():
    """R2.8 / task 3.5. There is deliberately no threshold anywhere in this module.

    An absolute SSIM or VMAF gate would either never fire or block unrelated work. Asserted so a
    later "helpful" addition of a threshold cannot pass quietly — the judgement belongs to the
    reader, and a metric that measures reproduction cannot be a quality gate.
    """
    prov = {"ffmpeg_version": "x"}
    result = fidelity.compare(
        {"provenance": prov, "readings": []}, {"provenance": prov, "readings": []}
    )
    note = result["note"].lower()
    assert "reproduction, not quality" in note
    assert "pass" not in note.split("no pass/fail")[0].replace("pass/fail", "")
    for forbidden in ("significant", "p-value", "confidence"):
        assert forbidden not in json_dump(result), forbidden


def json_dump(obj) -> str:
    import json

    return json.dumps(obj).lower()


def test_compare_reports_a_metric_that_is_unavailable_in_only_one_report():
    """Half a comparison is not a comparison, and must not be silently dropped."""
    prov = {"ffmpeg_version": "same"}
    before = {
        "provenance": prov,
        "readings": [{"metric": "vmaf", "available": False, "reason": "no libvmaf"}],
    }
    after = {
        "provenance": prov,
        "readings": [{"metric": "vmaf", "mean": 90.0, "minimum": 80.0, "available": True}],
    }
    entry = fidelity.compare(before, after)["metrics"][0]
    assert "unavailable" in entry["note"]


@requires_ffmpeg
@pytest.mark.real_binary
def test_readings_are_reproducible_on_one_build(faithful, tmp_path):
    """R2.5 / task 3.6. Identical inputs, same build, identical numbers.

    Without this the instrument cannot support a before/after comparison at all: a metric that
    varies run to run makes every delta unattributable.
    """
    first = fidelity.measure(
        faithful, faithful, prober=_prober_all_present(), log_dir=tmp_path / "1"
    )
    second = fidelity.measure(
        faithful, faithful, prober=_prober_all_present(), log_dir=tmp_path / "2"
    )
    for metric in (Metric.SSIM.value, Metric.VMAF.value):
        a, b = first.reading(metric), second.reading(metric)
        assert a.mean == b.mean, metric
        assert a.minimum == b.minimum, metric
        assert a.frames == b.frames, metric


def test_a_fidelity_metric_is_documented_as_measuring_reproduction_not_quality():
    """R1.9. The one sentence most likely to be forgotten when a number is quoted in a PR.

    Pinned against the module docstring so it cannot be quietly dropped: a beautifully framed
    clip that encodes badly scores low, and a flawless reproduction of a badly framed reference
    scores 1.0.
    """
    assert "reproduction, not quality" in (fidelity.__doc__ or "")


# --- the VMAF binary is separately configurable ---------------------------------------------
#
# VMAF is the one metric that cannot be measured with the ffmpeg this project renders with,
# because no mainstream distribution compiles `libvmaf` into it. Pointing the primary binary at
# a build that has it is not a fix either: the third-party builds that carry `libvmaf` signal
# colour differently, leaving `color_transfer` and `color_primaries` unset where the
# distribution build writes them, which the colour-pipeline checks read. So VMAF gets its own
# binary and nothing else moves. These tests hold that separation in place.


def test_the_vmaf_binary_defaults_to_the_primary_ffmpeg(monkeypatch):
    """An unset `vmaf_ffmpeg_binary` must not change single-binary behaviour at all.

    This is the configuration almost every reader runs, so it is asserted rather than assumed:
    the default has to leave the module measuring VMAF exactly where it measured it before the
    setting existed.
    """
    monkeypatch.setattr(app_settings, "vmaf_ffmpeg_binary", "", raising=False)
    assert fidelity._vmaf_ffmpeg() == fidelity._ffmpeg()
    assert fidelity._vmaf_binary_is_separate() is False


def test_a_configured_vmaf_binary_is_used_only_for_vmaf(monkeypatch, tmp_path):
    """SSIM and PSNR keep the primary binary; only the VMAF pass switches.

    Moving all three onto a second build would change SSIM and PSNR for a reason unrelated to
    the encode, and a stored baseline would read that as a regression in the renderer.
    """
    fake = tmp_path / "ffmpeg-with-vmaf"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(app_settings, "vmaf_ffmpeg_binary", str(fake), raising=False)

    assert fidelity._vmaf_ffmpeg() == str(fake)
    assert fidelity._vmaf_binary_is_separate() is True

    seen: list[str] = []

    def record(candidate, reference, lavfi, *, binary=""):
        seen.append(binary or fidelity._ffmpeg())
        raise fidelity.FidelityError("stopped after recording the binary")

    monkeypatch.setattr(fidelity, "_run_filter", record)

    for call in (
        lambda: fidelity.measure_ssim("a.mp4", "b.mp4"),
        lambda: fidelity.measure_psnr("a.mp4", "b.mp4"),
        lambda: fidelity.measure_vmaf("a.mp4", "b.mp4", log_dir=tmp_path),
    ):
        with pytest.raises(fidelity.FidelityError):
            call()

    ssim_binary, psnr_binary, vmaf_binary = seen
    assert ssim_binary == fidelity._ffmpeg(), "SSIM must stay on the primary binary"
    assert psnr_binary == fidelity._ffmpeg(), "PSNR must stay on the primary binary"
    assert vmaf_binary == str(fake), "VMAF must use the configured binary"


def test_a_configured_vmaf_binary_answers_its_own_availability(monkeypatch):
    """Availability must come from the build that will run the filter (R1.2, R1.3).

    The capability report describes `settings.ffmpeg_binary` — which in this configuration is
    precisely the build known *not* to have `libvmaf`. Asking it would report VMAF unavailable
    while the measurement would in fact succeed, which is the same "answer cached where nobody
    is looking" defect the capability module was written to remove.
    """
    monkeypatch.setattr(app_settings, "vmaf_ffmpeg_binary", "/opt/ffmpeg-vmaf", raising=False)

    asked: list[str] = []

    def fake_available(name, *, binary=""):
        asked.append(binary)
        return name == fidelity.VMAF_FILTER

    monkeypatch.setattr("worker.engines.capabilities.ffmpeg_filter_available", fake_available)

    vmaf = next(
        entry for entry in fidelity.available_metrics() if entry.metric == Metric.VMAF.value
    )
    assert vmaf.available is True
    assert asked == ["/opt/ffmpeg-vmaf"], "the VMAF binary must be the one probed"


def test_a_missing_vmaf_binary_is_a_named_unavailability_not_a_silent_fallback(monkeypatch):
    """R1.4, R1.5. Configuring a binary that is not there must not measure with another one.

    The reason for configuring it is that the reading has to come from that build, so quietly
    substituting the primary ffmpeg would produce a number nobody asked for — reported as
    though it were the one they did.
    """
    monkeypatch.setattr(
        app_settings, "vmaf_ffmpeg_binary", "/nonexistent/ffmpeg-vmaf", raising=False
    )
    assert fidelity._vmaf_ffmpeg() == "/nonexistent/ffmpeg-vmaf"
    assert fidelity._vmaf_binary_is_separate() is True

    vmaf = next(
        entry for entry in fidelity.available_metrics() if entry.metric == Metric.VMAF.value
    )
    assert vmaf.available is False
    assert vmaf.reason, "an unavailable metric must say why (R1.2)"


def test_compare_refuses_readings_from_different_vmaf_builds():
    """The build guard has to cover the second binary too.

    Swap only the VMAF ffmpeg and `ffmpeg_version` still matches, so the existing check passes
    and the VMAF column gets differenced across two `libvmaf` versions — the arithmetic works
    and the result looks like a finding, which is exactly what that guard exists to stop.
    """
    before = {
        "provenance": {
            "ffmpeg_version": "ffmpeg version 7.1",
            "vmaf_ffmpeg_version": "ffmpeg version 7.1 (libvmaf 2.3.1)",
        },
        "readings": [],
    }
    after = {
        "provenance": {
            "ffmpeg_version": "ffmpeg version 7.1",
            "vmaf_ffmpeg_version": "ffmpeg version 8.0 (libvmaf 3.0.0)",
        },
        "readings": [],
    }
    with pytest.raises(FidelityError) as excinfo:
        fidelity.compare(before, after)
    assert "VMAF" in str(excinfo.value)


def test_compare_still_works_against_a_baseline_written_before_the_field_existed():
    """An unrecorded VMAF version is not a mismatch.

    Stored baselines predate this field. Refusing them would make a schema addition look like a
    build mismatch, and a guard that cries wolf gets deleted — costing more than the case it
    would have caught.
    """
    prov_old = {"ffmpeg_version": "ffmpeg version 7.1"}
    prov_new = {
        "ffmpeg_version": "ffmpeg version 7.1",
        "vmaf_ffmpeg_version": "ffmpeg version 7.1 (libvmaf 2.3.1)",
    }
    result = fidelity.compare(
        {"provenance": prov_old, "readings": []},
        {"provenance": prov_new, "readings": []},
    )
    assert isinstance(result, dict)
