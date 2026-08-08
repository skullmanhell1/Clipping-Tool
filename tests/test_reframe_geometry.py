"""Property tests for speaker-aware reframe geometry
(``worker/effects/reframe.py``).

Covers tasks 5.6-5.15 (design Properties P12-P21). Property tests use
``hypothesis`` with ``@settings(max_examples=100)``, one property per test,
tagged with the design property text
(``# Feature: speaker-diarization-reframe, Property N: ...``) and a
``Validates: Requirements ...`` docstring.

Everything here is pure / offline / CPU-only — no ffmpeg, no OpenCV, no network.
Synthetic :class:`~worker.diarization.Speaker_Turn` and
:class:`~worker.effects.reframe.Face_Track` data are constructed directly.

Notes on the follow-active path assertions (P12/P15): ``build_follow_active_path``
applies EMA smoothing + a per-command clamp, so the *pre-smoothing* target is
not directly observable. The tests therefore construct tracks whose associated
face sits at a CONSTANT position for the whole turn — the smoothed, converged,
clamped centre then equals that constant — and use tolerance-based (``approx``)
assertions to accommodate the EMA/clamp/interpolation arithmetic.
"""

from __future__ import annotations

from unittest import mock

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from tests.conftest import FakeWord
from worker.diarization import Speaker_Turn, rebase_turns, segment_by_words
from worker.effects import filler
from worker.effects.filler import Interval
from worker.effects.reframe import (
    REFRAME_INTENSITY,
    Association,
    Center,
    Face_Track,
    FaceBox,
    ReframeUnavailable,
    Region,
    apply_speaker_reframe,
    associate_faces,
    build_follow_active_path,
    build_reframe_filter,
    build_split_screen_layout,
    compute_crop_size,
    intensity_params,
)
from worker.ffmpeg_utils import MediaInfo

_INTENSITIES = ["subtle", "standard", "heavy"]


# --------------------------------------------------------------------------- #
# Strategies                                                                    #
# --------------------------------------------------------------------------- #
@st.composite
def _landscape_src(draw):
    """A landscape source ``(src_w, src_h)`` wider than 9:16, so the 9:16 crop
    is strictly narrower (there is something to follow)."""
    src_h = draw(st.integers(min_value=240, max_value=1080))
    src_h -= src_h % 2
    src_w = draw(st.integers(min_value=src_h, max_value=1920))
    src_w -= src_w % 2
    return max(2, src_w), max(2, src_h)


@st.composite
def _geometry_case(draw):
    """A stress case for the master-bounds property: a landscape source, its
    9:16 crop dims, a clip ``duration``, ordered non-overlapping ``turns`` with
    repeating labels, and ``tracks`` whose boxes may sit anywhere (even outside
    the frame) to exercise the clamp."""
    src_w, src_h = draw(_landscape_src())
    crop_w, crop_h = compute_crop_size(src_w, src_h, 9, 16)
    duration = draw(st.floats(min_value=1.0, max_value=8.0))

    # Ordered, non-overlapping clip-relative turns.
    n_turns = draw(st.integers(min_value=0, max_value=5))
    turns = []
    cursor = draw(st.floats(min_value=0.0, max_value=1.0))
    for _ in range(n_turns):
        gap = draw(st.floats(min_value=0.0, max_value=1.0))
        dur = draw(st.floats(min_value=0.2, max_value=2.5))
        start = cursor + gap
        end = start + dur
        if end > duration:
            break
        turns.append(
            Speaker_Turn(draw(st.sampled_from(["S1", "S2", "S3"])), round(start, 3), round(end, 3))
        )
        cursor = end

    # Tracks with boxes anywhere in a padded frame range (stresses clamping).
    n_tracks = draw(st.integers(min_value=0, max_value=3))
    tracks = []
    for k in range(n_tracks):
        x = draw(st.integers(min_value=-100, max_value=src_w + 100))
        y = draw(st.integers(min_value=-100, max_value=src_h + 100))
        w = draw(st.integers(min_value=20, max_value=250))
        h = draw(st.integers(min_value=20, max_value=250))
        ts = draw(st.floats(min_value=0.0, max_value=max(0.0, duration)))
        te = min(duration, ts + draw(st.floats(min_value=0.3, max_value=duration + 1.0)))
        boxes = []
        t = ts
        while t <= te + 1e-9:
            boxes.append(FaceBox(round(t, 3), x, y, w, h))
            t += 0.2
        if boxes:
            tracks.append(Face_Track(f"F{k + 1}", boxes))
    return turns, tracks, src_w, src_h, crop_w, crop_h, duration


# --------------------------------------------------------------------------- #
# 5.6 — Property 12                                                             #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 12: Follow-active crop tracks the active speaker and holds on gaps
@settings(max_examples=100)
@given(
    fx_frac=st.floats(min_value=0.15, max_value=0.85),
    t0=st.floats(min_value=1.0, max_value=3.0),
    t1=st.floats(min_value=1.0, max_value=3.0),
    intensity=st.sampled_from(_INTENSITIES),
)
def test_p12_follow_active_tracks_speaker_and_holds_on_gap(fx_frac, t0, t1, intensity):
    """Validates: Requirements 8.1, 8.4

    Within an associated turn the (smoothed, converged, clamped) crop-centre
    equals the associated track's constant centre; within a trailing
    unassociated turn the centre HOLDS that most-recent valid centre.

    Construction: the associated face is fixed at a constant position for the
    whole first turn, so EMA converges exactly to it; a trailing turn has no
    overlapping track (a >=1s gap guarantees zero presence), so its centre is
    held. Both therefore equal the constant centre — asserted with a small
    pixel tolerance for the EMA/clamp arithmetic.
    """
    src_w, src_h = 1280, 720
    crop_w, crop_h = compute_crop_size(src_w, src_h, 9, 16)  # (404, 720)
    lo_x, hi_x = crop_w / 2.0, src_w - crop_w / 2.0
    fx = lo_x + fx_frac * (hi_x - lo_x)
    fy = src_h / 2.0  # crop_h == src_h -> cy pinned to centre; place face there

    # Turn 0 associated to F1 (constant box); F1 stops well before turn 1.
    boxes = []
    t = 0.0
    while t <= t0 + 1e-9:
        boxes.append(FaceBox(round(t, 3), int(fx - 25), int(fy - 25), 50, 50))
        t += 0.2
    track = Face_Track("F1", boxes)

    turn0 = Speaker_Turn("S1", 0.0, round(t0, 3))
    turn1_start = round(t0 + 1.0, 3)  # >=1s gap -> zero F1 presence
    turn1 = Speaker_Turn("S2", turn1_start, round(turn1_start + t1, 3))
    turns = [turn0, turn1]
    duration = turn1.end

    assoc = associate_faces(turns, [track])
    # F1 is associated to turn 0 only; turn 1 has no usable track.
    assert assoc.by_turn[0] == "F1"
    assert assoc.by_turn[1] is None

    path = build_follow_active_path(
        turns,
        assoc,
        [track],
        src_w=src_w,
        src_h=src_h,
        crop_w=crop_w,
        crop_h=crop_h,
        intensity=intensity,
        duration=duration,
    )

    saw_associated = False
    saw_held = False
    for c in path:
        # Every centre equals the constant face position (tracked or held).
        assert c.cx == pytest.approx(fx, abs=1.5)
        assert c.cy == pytest.approx(fy, abs=1.5)
        if turn0.start <= c.t < turn0.end:
            saw_associated = True
        if turn1.start <= c.t <= turn1.end:
            saw_held = True
    assert saw_associated and saw_held


# --------------------------------------------------------------------------- #
# 5.7 — Property 13 (MASTER BOUNDS)                                             #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 13: Crop windows stay within frame bounds and times within the clip
@settings(max_examples=100)
@given(case=_geometry_case())
def test_p13_crop_windows_within_bounds_and_times_within_clip(case):
    """Validates: Requirements 8.2, 8.5, 10.5, 11.3, 13.5, 20.6

    For any turns / tracks / intensity / duration, every emitted crop-centre
    keeps the crop window fully inside the source frame
    (``crop_w/2 <= cx <= src_w-crop_w/2`` and
    ``crop_h/2 <= cy <= src_h-crop_h/2``) and every command time lies within
    ``[0, duration]`` — across all three intensities.
    """
    turns, tracks, src_w, src_h, crop_w, crop_h, duration = case
    assoc = associate_faces(turns, tracks)

    lo_x, hi_x = crop_w / 2.0, src_w - crop_w / 2.0
    lo_y, hi_y = crop_h / 2.0, src_h - crop_h / 2.0
    eps = 1e-6
    # Command times are rounded to millisecond precision, so allow a rounding
    # tolerance on the [0, duration] time bound.
    eps_t = 1e-3

    for intensity in _INTENSITIES:
        path = build_follow_active_path(
            turns,
            assoc,
            tracks,
            src_w=src_w,
            src_h=src_h,
            crop_w=crop_w,
            crop_h=crop_h,
            intensity=intensity,
            duration=duration,
        )
        for c in path:
            assert -eps_t <= c.t <= duration + eps_t
            assert lo_x - eps <= c.cx <= hi_x + eps
            assert lo_y - eps <= c.cy <= hi_y + eps


# --------------------------------------------------------------------------- #
# 5.8 — Property 14                                                             #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 14: Intensity maps deterministically and monotonically
@settings(max_examples=100)
@given(value=st.text(max_size=12))
def test_p14_intensity_maps_deterministically_and_monotonically(value):
    """Validates: Requirements 10.2, 10.3, 10.4, 11.2

    The mapping is deterministic (same input -> same output), the transition
    duration is derived from the intensity, unknown values fall back to
    ``standard``, and the ordering subtle -> standard -> heavy yields
    monotonically weaker smoothing (increasing alpha) and faster movement
    (decreasing transition seconds).
    """
    # Deterministic: same input -> identical output.
    assert intensity_params(value) == intensity_params(value)

    # Unknown / malformed values fall back to the standard pair.
    if value not in REFRAME_INTENSITY:
        assert intensity_params(value) == REFRAME_INTENSITY["standard"]

    a_sub, tr_sub = intensity_params("subtle")
    a_std, tr_std = intensity_params("standard")
    a_hvy, tr_hvy = intensity_params("heavy")

    # Alpha (weaker smoothing / faster movement) increases with intensity.
    assert a_sub < a_std < a_hvy
    # Transition duration decreases with intensity.
    assert tr_sub > tr_std > tr_hvy
    # Transition is derived from intensity (each known value distinct).
    assert len({tr_sub, tr_std, tr_hvy}) == 3


# --------------------------------------------------------------------------- #
# 5.9 — Property 15                                                             #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 15: Speaker changes transition smoothly and end before the next stable window
@settings(max_examples=100)
@given(
    t0=st.floats(min_value=1.0, max_value=3.0),
    tail=st.floats(min_value=2.0, max_value=4.0),
    intensity=st.sampled_from(_INTENSITIES),
)
def test_p15_speaker_change_transitions_smoothly(t0, tail, intensity):
    """Validates: Requirements 11.1, 11.4

    On a speaker change the centre does NOT jump instantaneously: there exist
    intermediate interpolated centres strictly between the two speakers' x
    positions, the progression is monotone, and by the time the next turn's
    stable window is reached the centre has moved to the new speaker.
    """
    src_w, src_h = 1280, 720
    crop_w, crop_h = compute_crop_size(src_w, src_h, 9, 16)  # (404, 720)
    lo_x, hi_x = crop_w / 2.0, src_w - crop_w / 2.0
    cy = src_h / 2.0
    x1 = lo_x + 6  # near the left bound
    x2 = hi_x - 6  # near the right bound (well separated)

    _alpha, transition = intensity_params(intensity)

    def _boxes(x, start, end):
        out = []
        t = start
        while t <= end + 1e-9:
            out.append(FaceBox(round(t, 3), int(x - 25), int(cy - 25), 50, 50))
            t += 0.2
        return out

    t0 = round(t0, 3)
    t1_end = round(t0 + transition + tail, 3)
    f1 = Face_Track("F1", _boxes(x1, 0.0, t0))
    f2 = Face_Track("F2", _boxes(x2, t0, t1_end))
    turns = [Speaker_Turn("S1", 0.0, t0), Speaker_Turn("S2", t0, t1_end)]
    duration = t1_end

    assoc = associate_faces(turns, [f1, f2])
    assert assoc.by_turn[0] == "F1"
    assert assoc.by_turn[1] == "F2"

    path = build_follow_active_path(
        turns,
        assoc,
        [f1, f2],
        src_w=src_w,
        src_h=src_h,
        crop_w=crop_w,
        crop_h=crop_h,
        intensity=intensity,
        duration=duration,
    )

    xs = [c.cx for c in path]
    # Monotone non-decreasing progression (x1 < x2).
    for a, b in zip(xs, xs[1:]):
        assert b >= a - 1e-6

    # Not an instantaneous jump: at least one strictly-in-between centre exists.
    assert any(x1 + 1.0 < x < x2 - 1.0 for x in xs)

    # Starts near the previous speaker, ends moved onto the new speaker's window.
    assert xs[0] == pytest.approx(x1, abs=2.0)
    assert xs[-1] > (x1 + x2) / 2.0

    # The transition window ends no later than the next stable window (here the
    # clip end): the interpolation span is bounded by the turn/next-start.
    assert t0 + transition <= duration + 1e-6


# --------------------------------------------------------------------------- #
# Split-screen helpers                                                          #
# --------------------------------------------------------------------------- #
def _rects_overlap(a: Region, b: Region) -> bool:
    ix = min(a.dst_x + a.dst_w, b.dst_x + b.dst_w) - max(a.dst_x, b.dst_x)
    iy = min(a.dst_y + a.dst_h, b.dst_y + b.dst_h) - max(a.dst_y, b.dst_y)
    return ix > 0 and iy > 0


@st.composite
def _tracks_with_shown_order(draw, min_n, max_n):
    """A ``(tracks, association)`` pair with a known ``shown_order`` of the
    given size, each track carrying boxes at a distinct position."""
    n = draw(st.integers(min_value=min_n, max_value=max_n))
    tracks = []
    shown = []
    for k in range(n):
        x = draw(st.integers(min_value=50, max_value=1800))
        y = draw(st.integers(min_value=50, max_value=1000))
        tid = f"F{k + 1}"
        tracks.append(Face_Track(tid, [FaceBox(0.0, x, y, 80, 80), FaceBox(0.2, x, y, 80, 80)]))
        shown.append(tid)
    assoc = Association(
        by_turn={i: shown[i] for i in range(n)}, unassociated=[], shown_order=list(shown)
    )
    return tracks, assoc


# --------------------------------------------------------------------------- #
# 5.10 — Property 16                                                            #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 16: Split-screen regions tile the target frame exactly
@settings(max_examples=100)
@given(
    data=_tracks_with_shown_order(2, 4),
    portrait=st.booleans(),
    src=_landscape_src(),
)
def test_p16_split_screen_tiles_target_exactly(data, portrait, src):
    """Validates: Requirements 9.1, 9.2, 9.3

    For >=2 associated tracks and any target aspect the regions are
    non-overlapping, their union EXACTLY covers the full target frame (areas
    sum to ``target_w*target_h`` and the tiles partition the frame), and each
    region's source crop is centred on its track.
    """
    tracks, assoc = data
    src_w, src_h = src
    if portrait:
        target_w, target_h = 1080, 1920
    else:
        target_w, target_h = 1920, 1080

    n = len(assoc.shown_order)
    regions = build_split_screen_layout(
        [],
        assoc,
        tracks,
        target_w=target_w,
        target_h=target_h,
        src_w=src_w,
        src_h=src_h,
        max_regions=n,
    )
    assert len(regions) == n

    # Non-overlapping.
    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            assert not _rects_overlap(regions[i], regions[j])

    # Areas sum to the full frame.
    total = sum(r.dst_w * r.dst_h for r in regions)
    assert total == target_w * target_h

    # Exact partition. Checked layout-agnostically: non-overlapping rectangles that all lie
    # inside the frame and whose areas sum to the frame's area can only be an exact cover, and
    # both facts are already asserted above. Asserting a *specific* arrangement here would pin
    # the tiling pattern rather than the property, which is what V6 had to change - three or
    # four speakers in a portrait frame now go into a 2-column grid instead of a stack of
    # letterbox slivers.
    for r in regions:
        assert r.dst_x >= 0 and r.dst_y >= 0
        assert r.dst_x + r.dst_w <= target_w
        assert r.dst_y + r.dst_h <= target_h
        assert r.dst_w > 0 and r.dst_h > 0

    # Each region's source crop is centred on its track (clamped in-frame).
    track_by_id = {tr.track_id: tr for tr in tracks}
    for r in regions:
        crop_w, crop_h = compute_crop_size(src_w, src_h, r.dst_w, r.dst_h)
        tr = track_by_id[r.track_id]
        mx = sum(b.center[0] for b in tr.boxes) / len(tr.boxes)
        my = sum(b.center[1] for b in tr.boxes) / len(tr.boxes)
        exp_cx = min(max(mx, crop_w / 2.0), src_w - crop_w / 2.0)
        exp_cy = min(max(my, crop_h / 2.0), src_h - crop_h / 2.0)
        assert r.src_cx == pytest.approx(exp_cx, abs=1e-6)
        assert r.src_cy == pytest.approx(exp_cy, abs=1e-6)


# --------------------------------------------------------------------------- #
# 5.11 — Property 17                                                           #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 17: Split-screen shows the most-talkative speakers within capacity
@settings(max_examples=100)
@given(
    d1=st.floats(min_value=3.0, max_value=6.0),
    d2=st.floats(min_value=1.5, max_value=2.9),
    d3=st.floats(min_value=0.3, max_value=1.4),
)
def test_p17_split_screen_shows_most_talkative_within_capacity(d1, d2, d3):
    """Validates: Requirements 9.4

    When associated tracks exceed capacity (``max_regions``), the shown region
    track_ids are exactly the top-capacity tracks by total speaking duration
    (matching ``assoc.shown_order[:max_regions]``).
    """
    # Three speakers with strictly-decreasing speaking durations d1 > d2 > d3,
    # each with a distinct face track spanning its own turn window.
    starts = [0.0, d1 + 1.0, d1 + d2 + 2.0]
    durs = [d1, d2, d3]
    turns = []
    tracks = []
    for k, (s, d) in enumerate(zip(starts, durs)):
        turns.append(Speaker_Turn(f"S{k + 1}", round(s, 3), round(s + d, 3)))
        boxes = []
        t = s
        while t <= s + d + 1e-9:
            boxes.append(FaceBox(round(t, 3), 100 + 300 * k, 200, 80, 80))
            t += 0.2
        tracks.append(Face_Track(f"F{k + 1}", boxes))

    assoc = associate_faces(turns, tracks)
    # Each turn should associate to its own track (distinct labels/positions).
    assert assoc.shown_order[:2]  # at least the two longest are ranked

    regions = build_split_screen_layout(
        turns,
        assoc,
        tracks,
        target_w=1080,
        target_h=1920,
        src_w=1920,
        src_h=1080,
        max_regions=2,
    )
    shown_ids = [r.track_id for r in regions]
    assert shown_ids == assoc.shown_order[:2]
    # The two shown are the two longest-speaking tracks (F1 and F2).
    assert set(shown_ids) == {"F1", "F2"}


# --------------------------------------------------------------------------- #
# 5.12 — Property 18                                                           #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 18: Too few tracks fall back to follow-active
@settings(max_examples=100)
@given(data=_tracks_with_shown_order(0, 1))
def test_p18_too_few_tracks_fall_back(data):
    """Validates: Requirements 9.5

    Fewer than two associated tracks -> ``build_split_screen_layout`` returns
    ``[]`` (so the caller substitutes ``follow_active``).
    """
    tracks, assoc = data
    assert len(assoc.shown_order) < 2
    regions = build_split_screen_layout(
        [],
        assoc,
        tracks,
        target_w=1080,
        target_h=1920,
        src_w=1920,
        src_h=1080,
        max_regions=2,
    )
    assert regions == []


# --------------------------------------------------------------------------- #
# 5.13 — Property 19                                                           #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 19: Unknown layout applies the follow-active default
@settings(max_examples=100)
@given(layout=st.text(max_size=16).filter(lambda s: s != "split_screen"))
def test_p19_unknown_layout_applies_follow_active_default(layout):
    """Validates: Requirements 7.5

    ``build_reframe_filter`` treats any non-``split_screen`` layout as
    ``follow_active``: an arbitrary/garbage layout string yields a
    follow-active-shaped filter (a ``sendcmd`` + ``crop`` + ``scale`` ``-vf``)
    and the ``speaker_reframe:follow_active`` note.
    """
    centers = [Center(0.0, 640.0, 360.0), Center(0.5, 640.0, 360.0)]
    _ia, vf, notes = build_reframe_filter(
        layout,
        centers=centers,
        crop_w=404,
        crop_h=720,
        src_w=1280,
        src_h=720,
        target_w=1080,
        target_h=1920,
        sendcmd_path=None,
    )
    assert "sendcmd" in vf
    assert "crop" in vf
    assert "scale" in vf
    assert "vstack" not in vf and "hstack" not in vf
    assert notes == ["speaker_reframe:follow_active"]


# --------------------------------------------------------------------------- #
# 5.14 — Property 20                                                           #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 20: No geometry action when the target aspect is not narrower
@settings(max_examples=100)
@given(mult=st.integers(min_value=2, max_value=120))
def test_p20_no_geometry_action_when_aspect_not_narrower(mult):
    """Validates: Requirements 12.5

    For a target aspect that is NOT narrower than the source — a source already
    at the 9:16 target aspect, so the 9:16 crop would fill the whole frame
    (``crop_w >= src_w and crop_h >= src_h``) — ``apply_speaker_reframe`` takes
    no geometry action and raises :class:`ReframeUnavailable`. ``probe`` is
    faked so no ffmpeg is required.
    """
    # Source exactly at 9:16 (even dimensions): the 9:16 crop fills the frame.
    src_w = 18 * mult
    src_h = 32 * mult
    crop_w, crop_h = compute_crop_size(src_w, src_h, 9, 16)
    assume(crop_w >= src_w and crop_h >= src_h)

    fake_info = MediaInfo(duration=5.0, width=src_w, height=src_h, fps=30.0, has_audio=False)
    turns = [Speaker_Turn("S1", 0.0, 5.0)]

    with mock.patch("worker.effects.reframe.probe", return_value=fake_info):
        with pytest.raises(ReframeUnavailable):
            apply_speaker_reframe(
                "unused.mp4",
                "out.mp4",
                turns=turns,
                aspect="9:16",
                layout="follow_active",
                sampler=lambda v: [],  # never reached
            )


# --------------------------------------------------------------------------- #
# 5.15 — Property 21                                                           #
# --------------------------------------------------------------------------- #
@st.composite
def _words_and_keeps(draw):
    """A clip-relative Word_Timeline plus a filler keep-plan (non-overlapping,
    increasing kept segments within ``[0, D]``)."""
    n = draw(st.integers(min_value=1, max_value=8))
    words = []
    cursor = draw(st.floats(min_value=0.0, max_value=0.5))
    for _ in range(n):
        gap = draw(st.floats(min_value=0.0, max_value=0.6))
        dur = draw(st.floats(min_value=0.2, max_value=0.8))
        s = cursor + gap
        e = s + dur
        words.append(FakeWord(round(s, 3), round(e, 3), "w"))
        cursor = e
    duration = round(cursor + draw(st.floats(min_value=0.2, max_value=1.0)), 3)

    # Build non-overlapping increasing keep segments covering part of [0, D].
    n_keeps = draw(st.integers(min_value=1, max_value=4))
    keeps = []
    c = 0.0
    for _ in range(n_keeps):
        skip = draw(st.floats(min_value=0.0, max_value=0.5))
        seg = draw(st.floats(min_value=0.2, max_value=1.0))
        s = c + skip
        e = min(duration, s + seg)
        if e <= s:
            break
        keeps.append(Interval(round(s, 3), round(e, 3)))
        c = e
        if c >= duration:
            break
    if not keeps:
        keeps = [Interval(0.0, duration)]
    return words, duration, keeps


# Feature: speaker-diarization-reframe, Property 21: Filler rebasing keeps turns clip-relative and bounded
@settings(max_examples=100)
@given(data=_words_and_keeps())
def test_p21_filler_rebasing_keeps_turns_clip_relative_and_bounded(data):
    """Validates: Requirements 13.4

    Turns rebased onto the tightened post-filler timeline are bounded within the
    final (post-filler) clip duration and stay aligned to the rebased words:
    reusing ``filler.rebase_words`` on the same keeps yields the tightened
    duration, and every rebased turn satisfies
    ``0 <= start <= end <= sum(keep durations)``.
    """
    words, duration, keeps = data
    tightened = sum(k.duration for k in keeps)
    eps = 1e-3

    turns = segment_by_words(words, duration)
    rebased = rebase_turns(turns, keeps)
    rebased_words = filler.rebase_words(words, keeps)

    for t in rebased:
        assert t.start >= -eps
        assert t.end <= tightened + eps
        assert t.start <= t.end + eps

    # Alignment: rebased turns and rebased words share the same tightened
    # timeline — both are bounded by the total kept duration (no turn or word
    # references a removed interval).
    if rebased_words:
        for w in rebased_words:
            assert w.end <= tightened + eps
    # Rebased turns stay ordered by start (aligned onto the concatenated
    # timeline in the same order as their source turns).
    starts = [t.start for t in rebased]
    assert starts == sorted(starts)
