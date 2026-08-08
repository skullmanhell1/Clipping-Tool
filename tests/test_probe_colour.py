"""Source colour is read from the probe we already run, and classified conservatively (O13, R1).

The fields these tests cover were **already in the JSON `probe()` parsed** and were being
discarded, which is why `worker/ffmpeg_utils.py` could carry a `probe()` that fetched
`color_transfer` while the pipeline had no notion of HDR at all.

Every source here is produced by the real ffmpeg with explicit colour signalling rather than
faked, because the whole failure mode being guarded against is a *disagreement between what
ffprobe reports and what we believe it reports*. A fake that returns `"smpte2084"` proves only
that the classifier reads a string; it cannot catch ffprobe spelling HLG `arib-std-b67` while the
code looks for `hlg`.

The two cases that matter most are the negative ones. **10-bit Rec.709 and 4K SDR must not
classify as HDR** (R1.6): both are ordinary footage, both are the obvious things to mistake for
an HDR signal, and tone-mapping either of them destroys it far more visibly than failing to
tone-map real HDR.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from config import settings as app_settings
from worker import colour
from worker.colour import Dynamic_Range
from worker.ffmpeg_utils import probe

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")
FFPROBE = shutil.which(app_settings.ffprobe_binary) or shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="no ffmpeg/ffprobe on PATH; colour probing needs both",
)


def _make(
    path,
    *,
    pix_fmt: str = "yuv420p",
    profile: str = "high",
    size: str = "320x180",
    trc: str = "",
    primaries: str = "",
    matrix: str = "",
    crange: str = "",
):
    """Render a real one-second source with exactly the colour signalling asked for.

    Any of the four tags may be omitted, which is the point: "the stream did not say" is a
    distinct input from every value it could have said, and it is the one that must not be
    guessed (R1.4).
    """
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate=25:duration=1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        pix_fmt,
        "-profile:v",
        profile,
    ]
    if trc:
        cmd += ["-color_trc", trc]
    if primaries:
        cmd += ["-color_primaries", primaries]
    if matrix:
        cmd += ["-colorspace", matrix]
    if crange:
        cmd += ["-color_range", crange]
    cmd += [str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return path


@requires_ffmpeg
@pytest.mark.real_binary
def test_pq_and_hlg_sources_classify_as_hdr(tmp_path):
    """The two transfer functions that actually mean HDR (R1.6, R2.1)."""
    pq = _make(
        tmp_path / "pq.mp4",
        pix_fmt="yuv420p10le",
        profile="high10",
        trc="smpte2084",
        primaries="bt2020",
        matrix="bt2020nc",
    )
    hlg = _make(
        tmp_path / "hlg.mp4",
        pix_fmt="yuv420p10le",
        profile="high10",
        trc="arib-std-b67",
        primaries="bt2020",
        matrix="bt2020nc",
    )

    pq_info = probe(pq)
    assert pq_info.color_transfer == "smpte2084", "ffprobe's PQ spelling changed"
    assert colour.classify_transfer(pq_info.color_transfer) is Dynamic_Range.HDR

    hlg_info = probe(hlg)
    # Asserted explicitly because this is the spelling most likely to be got wrong: the
    # transfer is universally *called* HLG and ffprobe never uses that word.
    assert hlg_info.color_transfer == "arib-std-b67", "ffprobe's HLG spelling changed"
    assert colour.classify_transfer(hlg_info.color_transfer) is Dynamic_Range.HDR


@requires_ffmpeg
@pytest.mark.real_binary
def test_ten_bit_rec709_is_not_hdr(tmp_path):
    """R1.6: bit depth is not evidence of dynamic range.

    This is the inference that would misfire on a large class of ordinary footage. 10-bit
    Rec.709 is common in anything graded, and tone-mapping it would crush a perfectly correct
    picture.
    """
    src = _make(
        tmp_path / "ten_bit_709.mp4",
        pix_fmt="yuv420p10le",
        profile="high10",
        trc="bt709",
        primaries="bt709",
        matrix="bt709",
    )
    info = probe(src)
    assert info.color_transfer == "bt709"
    assert colour.classify_transfer(info.color_transfer) is Dynamic_Range.SDR

    plan = colour.plan_colour(
        transfer=info.color_transfer,
        primaries=info.color_primaries,
        matrix=info.color_space,
        source_range=info.color_range,
    )
    assert plan.tone_mapped is False
    assert plan.filters == (), "a 10-bit Rec.709 source must be left alone"


@requires_ffmpeg
@pytest.mark.real_binary
def test_four_k_sdr_is_not_hdr(tmp_path):
    """R1.6: resolution is not evidence either. 4K SDR is the norm, not the exception."""
    src = _make(
        tmp_path / "uhd_sdr.mp4",
        size="3840x2160",
        trc="bt709",
        primaries="bt709",
        matrix="bt709",
    )
    info = probe(src)
    assert (info.width, info.height) == (3840, 2160)
    assert colour.classify_transfer(info.color_transfer) is Dynamic_Range.SDR

    plan = colour.plan_colour(
        transfer=info.color_transfer,
        primaries=info.color_primaries,
        matrix=info.color_space,
        source_range=info.color_range,
    )
    assert plan.tone_mapped is False


@requires_ffmpeg
@pytest.mark.real_binary
def test_an_untagged_source_is_unknown_not_sdr(tmp_path):
    """R1.4, R1.7: absent is unknown, and unknown is not a synonym for SDR.

    Untagged 1080p content very often *is* Rec.709. "Probably Rec.709" and "reported Rec.709"
    are still different facts, and only one of them is evidence — so the classification says
    unknown and the pipeline declines to convert.
    """
    src = _make(tmp_path / "untagged.mp4")
    info = probe(src)
    # ffprobe reports absent tags as "unknown" or omits them; both must normalise to "no answer".
    assert info.color_transfer in ("", "unknown"), info.color_transfer
    assert colour.classify_transfer(info.color_transfer) is Dynamic_Range.UNKNOWN

    plan = colour.plan_colour(
        transfer=info.color_transfer,
        primaries=info.color_primaries,
        matrix=info.color_space,
        source_range=info.color_range,
    )
    assert plan.tone_mapped is False, "an unknown transfer must never be tone-mapped (R2.7)"


def test_an_unrecognised_transfer_is_unknown_rather_than_a_guess():
    """R1.7. A transfer function nobody has heard of is not HDR and is not SDR.

    No ffmpeg needed: the input is a string ffprobe could emit from a future container, and the
    requirement is about how we treat a value we do not recognise rather than about ffprobe.
    """
    assert colour.classify_transfer("smpte428") is Dynamic_Range.UNKNOWN
    assert colour.classify_transfer("some-curve-from-2031") is Dynamic_Range.UNKNOWN
    plan = colour.plan_colour(
        transfer="some-curve-from-2031", primaries="", matrix="", source_range="tv"
    )
    assert plan.tone_mapped is False
    assert plan.filters == ()


def test_wide_gamut_sdr_is_sdr_not_hdr():
    """`bt2020-10` is wide *gamut*, not high dynamic *range*.

    Worth its own test because the two are constantly conflated and the value contains "2020",
    which is the string a reader scanning for HDR would flag. Tone-mapping wide-gamut SDR is the
    mislabelled-SDR failure this module exists to avoid.
    """
    for transfer in ("bt2020-10", "bt2020-12"):
        assert colour.classify_transfer(transfer) is Dynamic_Range.SDR, transfer


@requires_ffmpeg
@pytest.mark.real_binary
def test_probe_adds_no_second_ffprobe_invocation(monkeypatch, tmp_path):
    """R1.3: the colour fields are read from the probe we already ran.

    Pinned by counting `_run` calls rather than by reading the code, because "do not add a
    probe" is exactly the kind of constraint that decays the first time someone needs a field
    that is easier to fetch than to thread through.
    """
    from worker import ffmpeg_utils as fu

    calls: list[list[str]] = []
    real_run = fu._run

    def counting_run(cmd, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(fu, "_run", counting_run)

    src = _make(tmp_path / "one_probe.mp4", trc="bt709", primaries="bt709", matrix="bt709")
    info = fu.probe(src)

    assert info.color_transfer == "bt709"
    assert len(calls) == 1, f"probe() ran {len(calls)} subprocesses; it must run exactly one"


def test_positional_construction_of_mediainfo_still_works():
    """R1.5. The colour fields are appended last and defaulted.

    `MediaInfo`'s own comment records why: several tests construct it positionally, so a field
    inserted anywhere but the end silently shifts every one of their arguments along by one —
    and the result still constructs, which is what makes it dangerous.
    """
    from worker.ffmpeg_utils import MediaInfo

    info = MediaInfo(12.0, 1920, 1080, 30.0, True)
    assert info.color_transfer == ""
    assert info.color_primaries == ""
    assert info.color_space == ""
    assert info.color_range == ""
    # And the O10 fields that were appended for the same reason are still where they were.
    assert MediaInfo(1.0, 2, 3, 4.0, False, "h264", "aac", 99).video_codec == "h264"
