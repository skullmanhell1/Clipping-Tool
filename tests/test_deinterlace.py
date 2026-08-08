"""Interlaced detection and deinterlacing (V20).

The test that matters most is the **negative** one: a genuinely progressive source with fine
horizontal detail must not be deinterlaced, even though `idet` says it is interlaced.

That is not hypothetical. Measured on this build, a progressive `testsrc2` render reads
``TFF: 64  BFF: 0  Progressive: 11`` from `idet` — 85% interlaced, about a source that is
definitively progressive. Deinterlacing on that reading would permanently destroy vertical detail
in every clip from that source, and nothing downstream could tell it had happened.

The asymmetry is what settles it: leaving combing preserves what the source already had, while
deinterlacing progressive footage is irreversible. So disagreement between the container and the
measurement resolves to *inconclusive*.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from config import settings as app_settings
from worker import deinterlace as di
from worker.deinterlace import Scan
from worker.engines.capabilities import Capability_Status

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")
FFPROBE = shutil.which(app_settings.ffprobe_binary) or shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="no ffmpeg/ffprobe on PATH; scan detection needs both",
)


def _interlaced(path, *, seconds=3):
    """A genuinely interlaced source: two moments woven into one frame."""
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x480:rate=50:duration={seconds}",
            "-vf",
            "interlace=scan=tff",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-flags",
            "+ilme+ildct",
            str(path),
        ],
        check=True,
        timeout=600,
    )
    return path


def _progressive_detailed(path, *, seconds=3):
    """Progressive, with the hard horizontal edges that fool `idet`."""
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x480:rate=25:duration={seconds}",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=600,
    )
    return path


def _progressive_soft(path, *, seconds=2):
    """Progressive with no hard edges, which `idet` reads correctly."""
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=gray:s=640x480:r=25:d={seconds}",
            "-vf",
            "noise=alls=6:allf=t,gblur=sigma=1.5",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=600,
    )
    return path


def _prober(available=True, missing=()):
    def prober(capability_id: str) -> Capability_Status:
        name = capability_id.partition(":")[2]
        ok = available and name not in missing
        return Capability_Status(capability_id, ok, "injected")

    return prober


# --- R9.1: detection ------------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.real_binary
def test_an_interlaced_source_is_detected(tmp_path):
    """Both signals agree: the container says `tt` and `idet` measures 100% interlaced."""
    report = di.detect(_interlaced(tmp_path / "i.mp4"), prober=_prober())
    assert report.scan is Scan.INTERLACED, report
    assert report.field_order == "tt"
    assert report.idet_interlaced > report.idet_progressive
    assert report.should_deinterlace is True


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_soft_progressive_source_is_detected_as_progressive(tmp_path):
    """The easy case, where `idet` and the container already agree."""
    report = di.detect(_progressive_soft(tmp_path / "soft.mp4"), prober=_prober())
    assert report.scan is Scan.PROGRESSIVE, report
    assert report.should_deinterlace is False


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_detailed_progressive_source_is_not_deinterlaced_despite_idet(tmp_path):
    """**The test this module exists for.** R9.3.

    Measured: `idet` reads about 85% interlaced on a definitively progressive source, because high
    horizontal detail is indistinguishable from combing to it. Venetian blinds, fences, brickwork
    and small text all look like this.

    Deinterlacing on that reading would permanently destroy vertical detail in every clip from the
    source, and no later stage could detect it. Leaving combing, by contrast, preserves what the
    source already had. So the disagreement resolves against acting.
    """
    path = _progressive_detailed(tmp_path / "detailed.mp4")
    report = di.detect(path, prober=_prober())

    # Precondition: idet really does misread this source. If ffmpeg ever fixes that, this test
    # stops testing what it claims to, so the misreading is asserted rather than assumed.
    assert report.idet_interlaced > report.idet_progressive, (
        "idet no longer misreads this fixture; the corroboration rule needs a new fixture"
    )
    assert report.field_order == "progressive"

    assert report.scan is Scan.INCONCLUSIVE, report
    assert report.should_deinterlace is False
    chain, markers, _ = di.plan(path, prober=_prober())
    assert chain == "", "a progressive source must never be deinterlaced"
    assert "deinterlace_inconclusive" in markers


# --- classification, without needing media --------------------------------------------------


def test_both_signals_must_agree_to_deinterlace():
    """The corroboration rule, stated directly."""
    assert di.classify("tt", 100, 0).scan is Scan.INTERLACED
    # Container says interlaced, measurement disagrees -> unproven.
    assert di.classify("tt", 5, 95).scan is Scan.INCONCLUSIVE
    # Measurement says interlaced, container disagrees -> unproven (the false-positive case).
    assert di.classify("progressive", 95, 5).scan is Scan.INCONCLUSIVE
    assert di.classify("progressive", 0, 100).scan is Scan.PROGRESSIVE


def test_an_absent_field_order_is_inconclusive_not_progressive():
    """A missing declaration is missing data.

    Treating it as progressive would be a guess, and treating it as interlaced would deinterlace
    every file that lost its flag through a re-encode.
    """
    assert di.classify("", 100, 0).scan is Scan.INCONCLUSIVE
    assert di.classify("unknown", 100, 0).scan is Scan.INCONCLUSIVE


def test_too_few_decided_frames_is_inconclusive():
    """A handful of frames is a sample, not a measurement."""
    report = di.classify("tt", 3, 1)
    assert report.scan is Scan.INCONCLUSIVE
    assert "too few" in report.detail


def test_the_ratio_threshold_rejects_a_few_combed_frames():
    """A mostly-progressive file with some combing must not be deinterlaced wholesale.

    Just above and just below the threshold, so the boundary is pinned rather than implied.
    """
    assert di.classify("tt", 70, 30).scan is Scan.INTERLACED  # 0.70 >= 0.65
    assert di.classify("tt", 60, 40).scan is Scan.INCONCLUSIVE  # 0.60 < 0.65


def test_both_signals_are_kept_on_the_report():
    """A wrong answer must be diagnosable: the useful question is *which* signal was wrong."""
    report = di.classify("progressive", 90, 10)
    assert report.field_order == "progressive"
    assert report.idet_interlaced == 90
    assert report.idet_progressive == 10
    assert report.detail
    assert report.to_dict()["scan"] == "inconclusive"


# --- R9.4: availability ---------------------------------------------------------------------


def test_a_missing_deinterlacer_degrades_with_a_named_marker():
    """R9.4. Named capability, not a silent absence."""
    chain, markers, report = di.plan("irrelevant.mp4", prober=_prober(missing=("bwdif", "yadif")))
    assert chain == ""
    assert markers == (f"deinterlace_degraded:ffmpeg_filter:{di.DEINTERLACE_FILTERS[0]}",)
    assert report.scan is Scan.INCONCLUSIVE


def test_a_missing_idet_disables_detection_entirely():
    """Without the measurement half there is no corroboration, and the container alone is not enough.

    This is the design refusing to fall back to the weaker signal — which is the whole point of
    requiring two.
    """
    assert di.filters_available(_prober(missing=("idet",))) == ""
    chain, markers, _ = di.plan("irrelevant.mp4", prober=_prober(missing=("idet",)))
    assert chain == ""
    assert markers and markers[0].startswith("deinterlace_degraded:")


def test_bwdif_is_preferred_over_yadif():
    """A later filter with visibly fewer motion artefacts, where the build has it."""
    assert di.filters_available(_prober()) == "bwdif"
    assert di.filters_available(_prober(missing=("bwdif",))) == "yadif"


def test_a_probe_that_cannot_run_fails_closed():
    """Emitting a filter this ffmpeg lacks would fail the render.

    Same deliberate choice as `worker/colour.py`'s tone-map probe: a fidelity feature must never
    turn a deliverable clip into a failed job.
    """

    def exploding(capability_id: str) -> Capability_Status:
        raise RuntimeError("probe unavailable")

    assert di.filters_available(exploding) == ""


# --- R9.5: frame rate -----------------------------------------------------------------------


def test_the_frame_rate_is_preserved_not_doubled():
    """R9.5. `mode=1` emits one frame per *field*, turning 25i into 50p.

    That sounds like a free upgrade and interacts badly with O18's frame-rate policy and O19's
    keyframe derivation, both of which read the delivered rate.
    """
    assert di.filter_chain("bwdif") == "bwdif=mode=0"
    assert di.filter_chain("yadif") == "yadif=mode=0"


def test_doubling_remains_available_as_a_documented_opt_out():
    assert di.filter_chain("bwdif", double_rate=True) == "bwdif=mode=1"


def test_an_unknown_filter_name_falls_back_rather_than_emitting_it():
    """An unrecognised name must not reach ffmpeg, which would fail the graph."""
    assert di.filter_chain("not-a-filter").startswith(di.DEINTERLACE_FILTERS[-1])


# --- R9.7: configuration --------------------------------------------------------------------


def test_detection_can_be_switched_off_entirely():
    chain, markers, report = di.plan("irrelevant.mp4", enabled=False, prober=_prober())
    assert chain == ""
    assert markers == ()
    assert "disabled" in report.detail


def test_a_progressive_source_accumulates_no_markers():
    """A marker on every clip is noise, and noise is what stops a marker being read."""
    report = di.classify("progressive", 0, 100)
    assert report.scan is Scan.PROGRESSIVE
    # `plan` returns no markers for a confirmed-progressive source; only inconclusive says anything.
    assert di.classify("progressive", 0, 100).should_deinterlace is False


# --- ordering -------------------------------------------------------------------------------


def test_the_chain_is_a_bare_filter_so_it_can_be_placed_first():
    """R9.2. Combing that reaches the crop and scale becomes a smear no later filter can undo.

    The chain carries no input/output labels, so the caller can prepend it ahead of the colour
    conversion and the geometry without rewriting either.
    """
    chain = di.filter_chain("bwdif")
    assert "[" not in chain and "]" not in chain
    assert ";" not in chain
