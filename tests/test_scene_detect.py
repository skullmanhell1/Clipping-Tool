"""Snapping clip starts to shot boundaries (S9).

A clip that opens two seconds into a shot begins on a fragment - half a gesture, the tail of a
camera move - and reads as careless before the viewer has heard anything. The S1 harness showed
the scale: at IoU 0.7, the threshold that asks whether *boundaries* are right rather than whether
the right moment was found, the selector scored zero across the board.

Two things these tests are built around.

**The detector has a real blind spot, and the fixtures have to respect it.** ffmpeg scores scene
change on the luma plane, so a cut between two shots of similar brightness scores near zero. My
first fixture cut between ffmpeg's ``red`` and ``green`` and detected *nothing*, because those
differ by one unit of luma (76 vs 75). The fixtures here cut between black, grey and white, and
one test pins the blind spot deliberately so nobody later mistakes it for a regression.

**Every snap must be capped and reversible.** A boundary that was merely inelegant is better than
one that is wrong, so roughly half of these assert that a snap is *refused*.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from config import settings
from worker import scene_detect

FFMPEG = shutil.which(settings.ffmpeg_binary) or shutil.which("ffmpeg")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None, reason="no ffmpeg on PATH; scene detection needs it"
)


def _clip(tmp_path, colours, *, seconds=4.0, fps=25):
    """A video that cuts between ``colours``, one shot each of ``seconds``."""
    parts = []
    for index, colour in enumerate(colours):
        part = tmp_path / f"part{index}.mp4"
        subprocess.run(
            [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"color={colour}:s=320x240:d={seconds}:r={fps}",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(part)],
            check=True, capture_output=True, timeout=120,
        )
        parts.append(part)

    listing = tmp_path / f"list_{'_'.join(colours)}.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    out = tmp_path / f"{'_'.join(colours)}.mp4"
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps), "-y", str(out)],
        check=True, capture_output=True, timeout=180,
    )
    return out


class Candidate:
    """A ``start``/``end`` carrier, standing in for a ClipCandidate."""

    def __init__(self, start, end):
        self.start = start
        self.end = end


# --------------------------------------------------------------------------- #
# Pure snapping logic                                                          #
# --------------------------------------------------------------------------- #
def test_the_start_moves_to_the_nearest_cut():
    assert scene_detect.snap_start(4.6, 12.0, [4.0], max_shift=1.0) == (4.0, 12.0)
    assert scene_detect.snap_start(7.6, 12.0, [8.0], max_shift=1.0) == (8.0, 12.0)


def test_the_nearest_cut_wins_when_several_are_in_range():
    assert scene_detect.snap_start(5.0, 20.0, [4.2, 5.3, 8.0], max_shift=2.0)[0] == 5.3


def test_a_cut_beyond_the_cap_is_refused():
    """The cap is what keeps this a boundary tidy-up rather than a re-selection.

    Moving a start by four seconds to reach a shot change is not snapping - it is choosing a
    different moment, which is the selector's job and is measured by the S1 benchmark.
    """
    assert scene_detect.snap_start(10.0, 20.0, [4.0], max_shift=1.0) == (10.0, 20.0)


def test_the_end_is_never_moved():
    """A shot change near the end is not a reason to truncate a clip.

    The ending is chosen for content reasons - a punchline, a completed thought - so only the
    opening frame is treated as a presentation problem.
    """
    start, end = scene_detect.snap_start(4.6, 12.0, [4.0, 11.8], max_shift=1.0)
    assert (start, end) == (4.0, 12.0)


def test_a_snap_that_would_collapse_the_clip_is_refused():
    """A cut just before the end must not shorten a clip to nothing."""
    assert scene_detect.snap_start(9.5, 10.0, [9.6], max_shift=1.0) == (9.5, 10.0)


def test_degenerate_input_is_returned_untouched():
    assert scene_detect.snap_start(5.0, 5.0, [4.0]) == (5.0, 5.0)
    assert scene_detect.snap_start(8.0, 4.0, [4.0]) == (8.0, 4.0)
    assert scene_detect.snap_start(5.0, 20.0, []) == (5.0, 20.0)
    assert scene_detect.snap_start(5.0, 20.0, [4.5], max_shift=0.0) == (5.0, 20.0)


def test_a_negative_cut_time_is_ignored():
    assert scene_detect.snap_start(0.3, 20.0, [-0.5]) == (0.3, 20.0)


# --------------------------------------------------------------------------- #
# Detection against real video                                                 #
# --------------------------------------------------------------------------- #
@requires_ffmpeg
@pytest.mark.real_binary
def test_cuts_are_found_at_the_right_absolute_times(tmp_path):
    """The seek-offset bug this test exists to prevent.

    With ``-ss``, ffmpeg reports ``pts_time`` *relative to the seek point*: a cut at 4.0 s scanned
    from 3.0 s comes back as 1.0. Failing to add the offset would snap every boundary towards the
    start of the video, and the numbers would still look plausible.
    """
    video = _clip(tmp_path, ["black", "gray", "white"])   # cuts at 4 s and 8 s

    assert scene_detect.detect_cuts(video, 4.5) == [4.0]
    assert scene_detect.detect_cuts(video, 7.5) == [8.0]
    # Nothing near the middle of a shot.
    assert scene_detect.detect_cuts(video, 2.0, window=1.0) == []


@requires_ffmpeg
@pytest.mark.real_binary
def test_equiluminant_cuts_are_missed_and_that_is_documented(tmp_path):
    """Pinned deliberately, so it is not mistaken for a regression later.

    ffmpeg scores scene change on luma. ffmpeg's ``red`` and ``green`` are (255,0,0) and (0,128,0)
    - luma 76 and 75 - so a cut between them is invisible to the detector at any threshold. This
    is why every snap is capped and optional: a missed cut leaves the boundary exactly where the
    selector put it, which is the previous behaviour.
    """
    video = _clip(tmp_path, ["red", "green"])
    assert scene_detect.detect_cuts(video, 4.0) == [], (
        "the detector now finds equiluminant cuts; if that is intended, this test should be "
        "replaced rather than relaxed"
    )


@requires_ffmpeg
@pytest.mark.real_binary
def test_detection_failures_return_no_cuts(tmp_path):
    """A boundary that cannot be improved should stay where it is, not raise."""
    assert scene_detect.detect_cuts(tmp_path / "missing.mp4", 5.0) == []

    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video at all")
    assert scene_detect.detect_cuts(junk, 5.0) == []


# --------------------------------------------------------------------------- #
# End to end                                                                   #
# --------------------------------------------------------------------------- #
@requires_ffmpeg
@pytest.mark.real_binary
def test_candidates_are_snapped_in_place(tmp_path):
    video = _clip(tmp_path, ["black", "gray", "white"])
    mid_shot = Candidate(4.6, 11.0)     # 0.6 s into the second shot
    clean = Candidate(2.0, 3.5)         # nowhere near a cut

    moved = scene_detect.snap_candidates(video, [mid_shot, clean])

    assert moved == 1
    assert mid_shot.start == 4.0 and mid_shot.end == 11.0
    assert (clean.start, clean.end) == (2.0, 3.5)


@requires_ffmpeg
@pytest.mark.real_binary
def test_snapping_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "scene_snap_enabled", False)
    video = _clip(tmp_path, ["black", "gray", "white"])
    candidate = Candidate(4.6, 11.0)
    assert scene_detect.snap_candidates(video, [candidate]) == 0
    assert candidate.start == 4.6


def test_snapping_an_empty_list_is_safe(tmp_path):
    assert scene_detect.snap_candidates(tmp_path / "whatever.mp4", []) == 0


def test_hostile_candidates_are_skipped_not_fatal(tmp_path):
    class Hostile:
        start = "soon"
        end = None

    assert scene_detect.snap_candidates(tmp_path / "missing.mp4", [Hostile()]) == 0


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_selector_snaps_what_it_returns(tmp_path):
    """Through ``select_moments``, so the wiring is covered as well as the helper."""
    from worker.models import ProcessingOptions
    from worker.selection import select_moments
    from worker.transcribe import Transcript, TranscriptSegment

    video = _clip(tmp_path, ["black", "gray", "white"])
    transcript = Transcript(
        language="en",
        segments=[TranscriptSegment(0.0, 12.0, "some words", words=[])],
    )
    found = select_moments(
        transcript,
        ProcessingOptions(strategy="fixed", clip_length="<30s"),
        video,
        12.0,
    )
    assert found, "the fallback produced no candidates"
    # Every start is either untouched or exactly on a detected cut - never somewhere new.
    for candidate in found:
        cuts = scene_detect.detect_cuts(video, candidate.start)
        assert (not cuts) or candidate.start in cuts or all(
            abs(candidate.start - cut) > settings.scene_snap_max_shift_s for cut in cuts
        ), (candidate.start, cuts)
