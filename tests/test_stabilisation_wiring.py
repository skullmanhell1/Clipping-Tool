"""V21 stabilisation reaches the render, and the margin it reserves is load-bearing.

`worker/stabilise.py` shipped in #124 complete and tested, imported by nothing outside its own test
module, with `stabilise_strength` read by nothing. Every gate was green; the feature had no effect on
any frame. `tests/test_stabilisation.py` covers the module's arithmetic — this file covers whether
anything calls it, and whether the result is actually steadier.

The measurement at the bottom is the point of the file. `test_stabilisation_actually_steadies_the_clip`
renders real shaky footage through real ffmpeg and compares inter-frame difference, and
`test_without_the_margin_a_black_band_reaches_the_frame` demonstrates the R10.5 defect by deliberately
ignoring the margin. Measured on the fixture here: inter-frame difference falls from ~24 to ~7, and the
darkest top strip is ~16 with the margin against **0.00** without it — a fully black band delivered.
Without that second test the margin wiring could be deleted and every other assertion would still pass.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from tests.conftest import FFMPEG, requires_ffmpeg
from worker import stabilise
from worker.effects import reframe as rf

# --------------------------------------------------------------------------- #
# The rule about which geometry branch can host the correction                 #
# --------------------------------------------------------------------------- #


def test_only_the_reframe_crop_can_host_stabilisation():
    """`apply_reframe` is the one branch with a content rectangle to inset."""
    assert stabilise.geometry_refusal(reframe=True, speaker_reframe=False) == ""


@pytest.mark.parametrize(
    ("reframe", "speaker_reframe", "reason"),
    [
        (True, True, "speaker_layout"),
        (False, True, "speaker_layout"),
        (False, False, "no_reframe_crop"),
    ],
)
def test_the_other_branches_refuse_and_say_which(reframe, speaker_reframe, reason):
    """A refusal names the branch.

    An operator who sets `STABILISE_STRENGTH` and sees nothing happen needs to know *which* branch
    declined, not merely that something did — that was the whole failure mode of this feature before
    it was wired at all.

    `speaker_layout` wins over `no_reframe_crop` when both are true because the speaker branch is what
    actually runs; reporting the more general reason would name a branch that was never reached.
    """
    assert stabilise.geometry_refusal(reframe=reframe, speaker_reframe=speaker_reframe) == reason


# --------------------------------------------------------------------------- #
# apply_reframe honours the two parameters nothing used to pass               #
# --------------------------------------------------------------------------- #


def _captured_filter(monkeypatch, tmp_path, make_video, **kwargs) -> str:
    """Run `apply_reframe` with a stubbed encoder and return the `-vf` string it built."""
    src = make_video("stab_src.mp4", duration=1.0, w=1280, h=720)

    def fake_track(video, sample_fps=5.0, backend=None, detector=None):
        centres = [rf.Center(0.0, 640, 360), rf.Center(1.0, 640, 360)]
        return centres, rf.synthetic_report([[(0, 0, 10, 10)]] * 2, "injected", sample_fps)

    monkeypatch.setattr(rf, "track_faces_report", fake_track)
    # V4's cut detection is a real decode of the clip and irrelevant to the filter string; patched on
    # the module `reframe` imports rather than on `reframe` itself, which does not re-export it.
    monkeypatch.setattr(rf.scene_detect, "scan_cuts", lambda *a, **k: [])

    seen: dict[str, str] = {}

    def fake_run(cmd, *a, **k):
        seen["vf"] = cmd[cmd.index("-vf") + 1]
        # `apply_reframe` returns `dest` without checking it exists, but the `finally` block unlinks
        # the sendcmd script, so nothing here needs a real encode.
        return None

    monkeypatch.setattr(rf, "_run", fake_run)
    rf.apply_reframe(src, tmp_path / "out.mp4", aspect="9:16", **kwargs)
    return seen["vf"]


@requires_ffmpeg
def test_the_prefilter_goes_at_the_head_of_the_chain(monkeypatch, tmp_path, make_video):
    """Order is the substance: the margin and every crop coordinate are in source pixels.

    `vidstabtransform` has to translate the whole frame *before* anything crops or scales it. Put it
    after the crop and it would shift an already-cropped picture, moving the subject out of the frame
    the tracker chose — which looks like the tracker failing.
    """
    vf = _captured_filter(
        monkeypatch, tmp_path, make_video, prefilter="vidstabtransform=input=x.trf"
    )

    assert vf.startswith("vidstabtransform=input=x.trf,sendcmd=")


@requires_ffmpeg
def test_no_prefilter_leaves_the_filter_string_untouched(monkeypatch, tmp_path, make_video):
    """The default has to be a true no-op or every reframe golden moves."""
    vf = _captured_filter(monkeypatch, tmp_path, make_video)

    assert vf.startswith("sendcmd=")
    assert "vidstab" not in vf


@requires_ffmpeg
def test_the_margin_pulls_the_crop_inside_the_valid_rectangle(monkeypatch, tmp_path, make_video):
    """R10.5. The crop must give up exactly the band `vidstab` may vacate.

    Asserted as a *smaller* crop rather than against a fixed number, because the crop size is derived
    from the aspect and the content rectangle and pinning it here would restate that arithmetic
    instead of checking it.
    """
    plain = _captured_filter(monkeypatch, tmp_path, make_video)
    inset = _captured_filter(monkeypatch, tmp_path, make_video, stabilise_margin=(31, 17))

    def crop_of(vf: str) -> tuple[int, int]:
        match = re.search(r"crop=(\d+):(\d+):", vf)
        assert match, vf
        return int(match.group(1)), int(match.group(2))

    plain_w, plain_h = crop_of(plain)
    inset_w, inset_h = crop_of(inset)

    assert inset_h == plain_h - 2 * 17
    assert inset_w < plain_w
    # Even dimensions: libx264's 4:2:0 subsampling requires them and an odd crop fails the encode
    # outright rather than degrading.
    assert inset_w % 2 == 0 and inset_h % 2 == 0


# --------------------------------------------------------------------------- #
# The measurement: is the clip actually steadier, and is the margin needed?    #
# --------------------------------------------------------------------------- #

#: Crop geometry shared by every variant below, so the only difference is the stabilisation.
#:
#: 386x686 at (447, 17) is the 9:16 crop of a 1280x720 frame inset by the margin a strength of 0.6
#: reserves — `margin_pixels(1280, 720, 0.6)` is (31, 17).
_CROP_INSET = "crop=386:686:447:17"
#: The same crop reaching the top and bottom frame edges, which is the defect.
_CROP_FULL_HEIGHT = "crop=386:720:447:0"


def _shaky_source(tmp_path):
    """Real footage with a known, deliberate per-frame translation.

    Synthesised rather than committed: the shake is generated by a `sin`/`cos` crop offset, so its
    amplitude is a fact about the fixture rather than about whatever footage someone happened to add.
    """
    dest = tmp_path / "shaky.mp4"
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
            "testsrc2=size=1280x720:rate=30:duration=2",
            "-vf",
            "pad=1400:800:60:40,crop=1280:720:'60+40*sin(n*1.7)':'40+30*cos(n*2.3)'",
            "-c:v",
            "libx264",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def _render(source, dest, chain: str) -> None:
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            f"{chain},scale=1080:1920,setsar=1",
            "-c:v",
            "libx264",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )


def _metadata_values(path, chain: str) -> list[float]:
    out = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            f"{chain},signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    ).stdout
    return [float(v) for v in re.findall(r"YAVG=([0-9.]+)", out)]


def _interframe_difference(path) -> float:
    """Mean absolute inter-frame luma difference. Lower means steadier.

    A steadier clip has consecutive frames that look more alike. The measure includes the content's
    own motion, which is identical in both variants here, so the difference between them is the shake.
    """
    values = _metadata_values(path, "tblend=all_mode=difference")
    assert values, "no frames measured"
    return sum(values) / len(values)


def _darkest_top_strip(path) -> float:
    """The darkest mean luma of the top 24 rows across all frames.

    Near zero means a vacated black band was delivered. A minimum rather than a mean, because the
    band appears only on the frames where the correction shifted furthest — which is also why
    `cropdetect` cannot see it: it reports the union over time, and that union is the whole frame.
    """
    values = _metadata_values(path, "crop=iw:24:0:0")
    assert values, "no frames measured"
    return min(values)


@requires_ffmpeg
def test_stabilisation_actually_steadies_the_clip(tmp_path):
    """The claim the feature exists to make, measured rather than asserted.

    Both sides use the identical crop, so the only variable is `vidstabtransform`. Measured on this
    fixture: about 24 without, about 7 with — a two-thirds reduction. The assertion is a wide
    inequality rather than a pinned figure because the exact value depends on the x264 build; what
    must not regress is the direction and the order of magnitude.
    """
    source = _shaky_source(tmp_path)
    transforms = tmp_path / "t.trf"
    assert stabilise.run_analysis(source, transforms, 0.6)

    baseline = tmp_path / "base.mp4"
    stabilised = tmp_path / "stab.mp4"
    _render(source, baseline, _CROP_INSET)
    _render(
        source,
        stabilised,
        f"{stabilise.transform_filter(transforms, 0.6, src_w=1280, src_h=720)},{_CROP_INSET}",
    )

    shaky_score = _interframe_difference(baseline)
    steady_score = _interframe_difference(stabilised)

    assert steady_score < shaky_score * 0.6, (
        f"stabilisation did not steady the clip: {shaky_score:.2f} -> {steady_score:.2f}"
    )


@requires_ffmpeg
def test_the_margin_keeps_the_vacated_band_off_the_frame(tmp_path):
    """R10.5, from both sides — and the second half is what makes the wiring provably necessary.

    `optzoom=0` means `vidstab` does not scale the picture to cover what it shifts, so the vacated
    edge is filled with black. With the margin the crop never reaches it. Without, it does: measured
    at exactly **0.00** on this fixture, a fully black band in the delivered frame.

    Delete `stabilise_margin=` from the pipeline and every other test here still passes. This one
    does not.
    """
    source = _shaky_source(tmp_path)
    transforms = tmp_path / "t.trf"
    assert stabilise.run_analysis(source, transforms, 0.6)
    stab_vf = stabilise.transform_filter(transforms, 0.6, src_w=1280, src_h=720)

    with_margin = tmp_path / "margin.mp4"
    without_margin = tmp_path / "nomargin.mp4"
    _render(source, with_margin, f"{stab_vf},{_CROP_INSET}")
    _render(source, without_margin, f"{stab_vf},{_CROP_FULL_HEIGHT}")

    assert _darkest_top_strip(with_margin) > 5.0, "the margin failed to keep the band off the frame"
    assert _darkest_top_strip(without_margin) < 1.0, (
        "the fixture no longer demonstrates the defect, so the test above proves nothing"
    )


# --------------------------------------------------------------------------- #
# The pipeline call site                                                       #
# --------------------------------------------------------------------------- #
#
# Everything above tests `apply_reframe` and `stabilise` directly. That is exactly the gap that let
# this feature ship dead: deleting `prefilter=stab_prefilter` from the pipeline breaks nothing above.
# These drive `run_pipeline` and capture what the geometry stage was actually handed.


def _run_clip(tmp_path, monkeypatch, make_video, *, strength, **option_overrides):
    """Run one clip through `run_pipeline`, returning `(clip, captured apply_reframe kwargs)`."""
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    src = make_video("stab_pipeline.mp4", duration=4.0, w=1280, h=720)

    words = [Word(0.3, 0.7, "this"), Word(0.8, 1.2, "moves"), Word(1.3, 1.7, "about")]
    monkeypatch.setattr(
        pl,
        "transcribe",
        lambda *a, **k: Transcript(
            language="en", segments=[TranscriptSegment(0.0, 4.0, "this moves about", words)]
        ),
    )
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [
            ClipCandidate(start=0.0, end=4.0, score=90.0, reason="r", title="T", text="t")
        ],
    )
    monkeypatch.setattr(pl.settings, "stabilise_strength", strength)

    captured: dict = {}
    real = pl.reframe.apply_reframe

    def spy(video, dest, **kwargs):
        captured.update(kwargs)
        # Delegate to a static reformat so the pipeline gets a real file without needing a face: the
        # question here is what the geometry stage was *handed*, not whether tracking succeeded.
        pl.fu.reformat_aspect(video, dest, aspect=kwargs.get("aspect", "9:16"), mode="crop_blur")
        return dest

    monkeypatch.setattr(pl.reframe, "apply_reframe", spy)
    assert real is not spy

    clips = pl.run_pipeline(
        src,
        options_all_off(aspect="9:16", **option_overrides),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
    )
    assert len(clips) == 1
    return clips[0], captured


@requires_ffmpeg
def test_the_pipeline_hands_the_correction_and_the_margin_to_the_reframe(
    tmp_path, monkeypatch, make_video
):
    """The whole chain, with the real two-pass analysis running.

    Deleting either keyword argument from `worker/pipeline.py` fails here and nowhere else.
    """
    clip, captured = _run_clip(tmp_path, monkeypatch, make_video, strength=0.6, reframe=True)

    assert "vidstabtransform" in captured.get("prefilter", "")
    assert captured.get("stabilise_margin") == (31, 17)
    assert "stabilise:0.60" in clip.effects_applied


@requires_ffmpeg
def test_stabilisation_is_off_by_default_and_changes_nothing(tmp_path, monkeypatch, make_video):
    """Zero strength reproduces the previous behaviour exactly, argument for argument."""
    clip, captured = _run_clip(tmp_path, monkeypatch, make_video, strength=0.0, reframe=True)

    assert captured.get("prefilter", "") == ""
    assert captured.get("stabilise_margin") == (0, 0)
    assert not any(m.startswith("stabilise") for m in clip.effects_applied)


@requires_ffmpeg
def test_a_speaker_layout_refuses_on_the_clip_record(tmp_path, monkeypatch, make_video):
    """`apply_speaker_reframe` has no content rectangle to inset, so it declines and says so."""
    clip, _ = _run_clip(
        tmp_path, monkeypatch, make_video, strength=0.6, reframe=True, speaker_reframe=True
    )

    assert "stabilise_skipped:speaker_layout" in clip.effects_applied
    assert "stabilise:0.60" not in clip.effects_applied


@requires_ffmpeg
def test_no_reframe_means_no_crop_to_hide_the_band(tmp_path, monkeypatch, make_video):
    """`crop_blur` scales the whole frame, vacated band included."""
    clip, _ = _run_clip(tmp_path, monkeypatch, make_video, strength=0.6, reframe=False)

    assert "stabilise_skipped:no_reframe_crop" in clip.effects_applied


@requires_ffmpeg
def test_a_failed_analysis_degrades_rather_than_failing_the_job(tmp_path, monkeypatch, make_video):
    """`run_analysis` returns False on any failure, and a clip is better than an exception."""
    import worker.pipeline as pl

    monkeypatch.setattr(pl.stabilise, "run_analysis", lambda *a, **k: False)
    clip, captured = _run_clip(tmp_path, monkeypatch, make_video, strength=0.6, reframe=True)

    assert "stabilise_degraded:analysis_failed" in clip.effects_applied
    assert captured.get("prefilter", "") == ""
    assert captured.get("stabilise_margin") == (0, 0), (
        "the margin must not be reserved when no correction is applied -- that would crop away "
        "frame for nothing"
    )
