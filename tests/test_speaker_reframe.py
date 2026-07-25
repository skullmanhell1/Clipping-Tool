"""Property + unit tests for multi-face tracks and face<->speaker association
(``worker/effects/reframe.py``).

Covers tasks 4.4-4.7. Property tests use ``hypothesis`` with
``@settings(max_examples=100)``, one property per test, tagged with the design
property text (``# Feature: speaker-diarization-reframe, Property N: ...``) and
a ``Validates: Requirements ...`` docstring.

All tests are pure/offline/CPU-only — no ffmpeg, no OpenCV, no network. The pure
functions under test (``build_face_tracks``, ``associate_faces``,
``Face_Track.presence``) need no video: ``FaceBox`` / ``Face_Track`` /
``Speaker_Turn`` are constructed directly. The ``FakeFaceDetector`` double from
``tests/fakes.py`` stands in for the injected ``detector`` of ``detect_faces``
so the dependency-injection wiring is exercised without cv2 (``detect_faces``'
own ffmpeg/cv2 sampling behaviour is covered by task 5.17).
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes import FakeFaceDetector
from worker.diarization import Speaker_Turn
from worker.effects.reframe import (
    Association,
    FaceBox,
    Face_Track,
    associate_faces,
    build_face_tracks,
)

_EPS = 1e-9

_LABELS = ["S1", "S2", "S3"]


# --------------------------------------------------------------------------- #
# Strategies                                                                    #
# --------------------------------------------------------------------------- #
@st.composite
def _per_frame_boxes(draw):
    """A list of per-frame :class:`FaceBox` sets.

    A small number of "true faces" sit at stable base positions; each sampled
    frame either shows every face (with a few pixels of jitter, so IoU
    continuity keeps them on one track) or is empty. With zero true faces every
    frame is empty, exercising the "no faces anywhere -> zero tracks" edge.
    """
    n_faces = draw(st.integers(min_value=0, max_value=3))
    bases: list[tuple[int, int, int, int]] = []
    for _ in range(n_faces):
        bx = draw(st.integers(min_value=0, max_value=900))
        by = draw(st.integers(min_value=0, max_value=500))
        bw = draw(st.integers(min_value=60, max_value=200))
        bh = draw(st.integers(min_value=60, max_value=200))
        bases.append((bx, by, bw, bh))

    n_frames = draw(st.integers(min_value=0, max_value=8))
    per_frame: list[list[FaceBox]] = []
    for fi in range(n_frames):
        t = round(fi * 0.2, 3)
        if bases and draw(st.booleans()):
            frame: list[FaceBox] = []
            for (bx, by, bw, bh) in bases:
                jx = draw(st.integers(min_value=-3, max_value=3))
                jy = draw(st.integers(min_value=-3, max_value=3))
                frame.append(FaceBox(t, bx + jx, by + jy, bw, bh))
            per_frame.append(frame)
        else:
            per_frame.append([])
    return per_frame


@st.composite
def _turns_and_tracks(draw):
    """A synthetic (``Speaker_Turn`` list, ``Face_Track`` list) pair.

    Turns are ordered, non-overlapping, clip-relative windows with labels drawn
    from a small set (so labels repeat and exercise the per-label mapping).
    Tracks each span a time range with boxes sampled at ~5 fps at a fixed
    on-screen position, built directly — no ffmpeg/cv2.
    """
    horizon = draw(st.floats(min_value=5.0, max_value=30.0))

    n_turns = draw(st.integers(min_value=0, max_value=6))
    turns: list[Speaker_Turn] = []
    cursor = draw(st.floats(min_value=0.0, max_value=2.0))
    for _ in range(n_turns):
        gap = draw(st.floats(min_value=0.0, max_value=2.0))
        dur = draw(st.floats(min_value=0.3, max_value=3.0))
        start = cursor + gap
        end = start + dur
        if end > horizon:
            break
        turns.append(
            Speaker_Turn(draw(st.sampled_from(_LABELS)), round(start, 3), round(end, 3))
        )
        cursor = end

    n_tracks = draw(st.integers(min_value=0, max_value=3))
    tracks: list[Face_Track] = []
    for k in range(n_tracks):
        ts = draw(st.floats(min_value=0.0, max_value=horizon))
        tdur = draw(st.floats(min_value=0.5, max_value=horizon))
        te = min(horizon, ts + tdur)
        x = draw(st.integers(min_value=0, max_value=800))
        y = draw(st.integers(min_value=0, max_value=400))
        w = draw(st.integers(min_value=40, max_value=200))
        h = draw(st.integers(min_value=40, max_value=200))
        boxes: list[FaceBox] = []
        t = ts
        while t <= te + _EPS:
            boxes.append(FaceBox(round(t, 3), x, y, w, h))
            t += 0.2
        if boxes:
            tracks.append(Face_Track(f"F{k + 1}", boxes))
    return turns, tracks


# --------------------------------------------------------------------------- #
# 4.4 — Property 9                                                              #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 9: Face boxes group into stable tracks
@settings(max_examples=100)
@given(per_frame=_per_frame_boxes())
def test_p9_face_boxes_group_into_stable_tracks(per_frame):
    """Validates: Requirements 5.2, 5.5

    ``build_face_tracks`` returns tracks each with a stable, non-empty
    ``track_id`` (ids unique across the returned list); and when no frame
    contains any face box it returns zero tracks.
    """
    tracks = build_face_tracks(per_frame)
    total_boxes = sum(len(frame) for frame in per_frame)

    if total_boxes == 0:
        # No faces in any frame -> zero tracks (Req 5.5).
        assert tracks == []
        return

    # At least one box somewhere -> at least one track.
    assert tracks

    ids = [tr.track_id for tr in tracks]
    # Stable + unique ids across the returned list.
    assert len(ids) == len(set(ids))
    for tr in tracks:
        assert isinstance(tr.track_id, str) and tr.track_id
        assert tr.boxes  # every returned track carries at least one box


# --------------------------------------------------------------------------- #
# 4.5 — Property 10                                                             #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 10: Association is single-valued and cardinality-bounded
@settings(max_examples=100)
@given(data=_turns_and_tracks())
def test_p10_association_single_valued_and_cardinality_bounded(data):
    """Validates: Requirements 6.1, 6.4, 6.5

    Each turn associates with at most one track (``by_turn`` is a single
    ``track_id`` or ``None``), the number of distinct associated tracks does not
    exceed the number of distinct speaker labels, and turns sharing a
    ``speaker_label`` map to the SAME track whenever both are associated.
    """
    turns, tracks = data
    assoc = associate_faces(turns, tracks)

    assert isinstance(assoc, Association)
    track_ids = {tr.track_id for tr in tracks}

    # Single-valued: each turn maps to one known track id or None.
    for i in range(len(turns)):
        v = assoc.by_turn.get(i)
        assert v is None or v in track_ids

    # Cardinality: distinct associated tracks <= distinct speaker labels.
    associated = {v for v in assoc.by_turn.values() if v is not None}
    distinct_labels = {t.speaker_label for t in turns}
    assert len(associated) <= len(distinct_labels)

    # Per-label consistency: turns sharing a label map to the same track.
    label_track: dict[str, str] = {}
    for i, t in enumerate(turns):
        v = assoc.by_turn.get(i)
        if v is not None:
            if t.speaker_label in label_track:
                assert label_track[t.speaker_label] == v
            else:
                label_track[t.speaker_label] = v


# --------------------------------------------------------------------------- #
# 4.6 — Property 11                                                             #
# --------------------------------------------------------------------------- #
# Feature: speaker-diarization-reframe, Property 11: Association picks the most-present track; gaps are marked
@settings(max_examples=100)
@given(data=_turns_and_tracks())
def test_p11_association_picks_most_present_track_gaps_marked(data):
    """Validates: Requirements 6.2, 6.3

    Each ASSOCIATED turn's chosen track has positive ``presence`` over that
    turn's window (the association maximises presence for the turn's speaker).
    Any turn with NO track having positive presence over its window is left
    ``unassociated`` with ``by_turn`` = ``None``.

    Note on the per-label nuance: association is decided per-label from the
    AGGREGATE presence across a label's turns (Req 6.5), so an associated
    track's presence need not be the single-window maximum. We therefore assert
    the (robust, still-meaningful) invariant that the chosen track is present
    over the window (> 0). For an unassociated turn that DOES overlap some
    track, its label was consistently bound elsewhere: either the label's
    chosen track is not present over this particular window, or the label
    received no track because every locally-present track was taken by other
    labels.
    """
    turns, tracks = data
    assoc = associate_faces(turns, tracks)
    track_by_id = {tr.track_id: tr for tr in tracks}
    associated_tracks = {v for v in assoc.by_turn.values() if v is not None}

    # Reconstruct the per-label chosen track from the associations.
    label_track: dict[str, str] = {}
    for i, t in enumerate(turns):
        v = assoc.by_turn.get(i)
        if v is not None:
            label_track.setdefault(t.speaker_label, v)

    for i, t in enumerate(turns):
        v = assoc.by_turn.get(i)
        if v is not None:
            # The chosen track must actually be present over the turn window.
            assert track_by_id[v].presence(t.start, t.end) > 0.0
        else:
            # Gap: recorded as unassociated with by_turn None.
            assert assoc.by_turn.get(i, "MISSING") is None
            assert i in assoc.unassociated

            present = [tr for tr in tracks if tr.presence(t.start, t.end) > 0.0]
            if present:
                # Per-label nuance (documented above).
                chosen = label_track.get(t.speaker_label)
                if chosen is None:
                    # Label got no track: every locally-present track was taken.
                    assert all(tr.track_id in associated_tracks for tr in present)
                else:
                    # Label's globally-chosen track is not present here.
                    assert track_by_id[chosen].presence(t.start, t.end) == 0.0


# --------------------------------------------------------------------------- #
# 4.7 — Unit tests: DI wiring and unassociated-turn handling                    #
# --------------------------------------------------------------------------- #
def test_fake_face_detector_is_a_valid_injectable_callable():
    """Validates: Requirements 20.1

    ``FakeFaceDetector`` is a valid ``detector(frame) -> list[(x, y, w, h)]``
    callable — the exact shape ``detect_faces`` injects — usable fully offline
    (no cv2). It supports canned static boxes, a scripted per-frame cycle, and
    an empty ("no faces") variant, recording every call.
    """
    # Static boxes returned on every call; calls are recorded.
    det = FakeFaceDetector(boxes=[(10, 20, 30, 40)])
    assert det("frame-a") == [(10, 20, 30, 40)]
    assert det("frame-b") == [(10, 20, 30, 40)]
    assert det.calls == ["frame-a", "frame-b"]

    # Scripted per-frame boxes cycle (wrapping when exhausted).
    scripted = FakeFaceDetector(script=[[(0, 0, 10, 10)], []])
    assert scripted("f0") == [(0, 0, 10, 10)]
    assert scripted("f1") == []
    assert scripted("f2") == [(0, 0, 10, 10)]  # wraps around

    # No-faces variant.
    empty = FakeFaceDetector()
    assert empty("f") == []


def test_pure_di_path_builds_tracks_and_associates_offline():
    """Validates: Requirements 20.1

    The pure DI path — ``build_face_tracks`` + ``associate_faces`` on canned
    ``FaceBox`` data — works offline with no cv2/ffmpeg. Canned per-frame boxes
    for a single steady face group into one track, and a turn spanning that
    track's window associates to it.
    """
    per_frame = [
        [FaceBox(0.0, 100, 100, 50, 50)],
        [FaceBox(0.2, 102, 101, 50, 50)],
        [FaceBox(0.4, 101, 99, 50, 50)],
    ]
    tracks = build_face_tracks(per_frame)
    assert len(tracks) == 1
    assert tracks[0].track_id == "F1"

    turns = [Speaker_Turn("S1", 0.0, 0.4)]
    assoc = associate_faces(turns, tracks)
    assert assoc.by_turn[0] == "F1"
    assert assoc.unassociated == []
    assert assoc.shown_order == ["F1"]


def test_turn_with_no_overlapping_track_is_unassociated():
    """Validates: Requirements 6.3

    A turn whose only track's boxes lie entirely outside the turn window has no
    overlapping track, so it is marked unassociated with ``by_turn`` = ``None``.
    """
    # Track lives at t ~= 10s; the turn is [0, 2] -> no overlap.
    track = Face_Track("F1", [FaceBox(10.0, 0, 0, 50, 50), FaceBox(10.2, 0, 0, 50, 50)])
    turns = [Speaker_Turn("S1", 0.0, 2.0)]

    assoc = associate_faces(turns, [track])
    assert assoc.by_turn[0] is None
    assert 0 in assoc.unassociated
    assert assoc.shown_order == []



# =========================================================================== #
# Task 5.16 — Unit tests: filter shape, tile arithmetic, layout substitution   #
# =========================================================================== #
#
# These exercise the pure ffmpeg-geometry builders (no ffmpeg run) and the
# reframe-level layout substitution inside ``apply_speaker_reframe`` (unknown ->
# follow_active, split_screen with < 2 tracks -> follow_active). The full
# pipeline precedence dispatch (speaker-aware vs legacy vs static) is wired in
# Task 7 and covered by that phase's pipeline tests; here we assert the
# reframe-level decisions offline by faking ``probe`` and ``_run`` and injecting
# a canned sampler, so no ffmpeg/cv2 is needed.

from pathlib import Path

from tests.conftest import FakeWord, probe_size, requires_ffmpeg
from tests.fakes import CannedSampler
from worker.effects import compositor, reframe
from worker.effects.reframe import (
    Center,
    build_reframe_filter,
    build_split_screen_layout,
)
from worker.ffmpeg_utils import ASPECT_PRESETS, MediaInfo
from worker.models import ProcessingOptions


def _landscape_info(w=1280, h=720, duration=2.0):
    """A faked landscape :class:`MediaInfo` whose 9:16 crop is narrower."""
    return MediaInfo(duration=duration, width=w, height=h, fps=30.0, has_audio=True)


def _frames(positions, duration=2.0, step=0.2):
    """Per-frame :class:`FaceBox` lists: each frame shows one box per position
    in ``positions`` (``(x, y, w, h)``), sampled every ``step`` seconds."""
    frames = []
    t = 0.0
    while t <= duration + 1e-9:
        frames.append([FaceBox(round(t, 3), x, y, w, h) for (x, y, w, h) in positions])
        t += step
    return frames


def _recording_run(monkeypatch):
    """Replace ``reframe._run`` with a recorder that creates the output file
    (last cmd arg) and records each command; returns the ``calls`` list."""
    calls: list[list[str]] = []

    def _fake(cmd):
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"\x00")
        return None

    monkeypatch.setattr(reframe, "_run", _fake)
    return calls


def test_build_reframe_filter_follow_active_shape_and_sendcmd(tmp_path):
    """Validates: Requirements 11.5, 12.1

    ``follow_active`` returns a single ``-vf`` string chaining
    ``sendcmd`` + ``crop`` + ``scale`` + ``setsar`` and writes the referenced
    ``sendcmd`` script file when a ``sendcmd_path`` is given.
    """
    cmd_file = tmp_path / "out.reframe.cmd"
    centers = [Center(0.0, 640.0, 360.0), Center(0.5, 700.0, 360.0)]
    input_args, vf, notes = build_reframe_filter(
        "follow_active",
        centers=centers,
        crop_w=404, crop_h=720, src_w=1280, src_h=720,
        target_w=1080, target_h=1920,
        sendcmd_path=str(cmd_file),
    )
    assert input_args == []
    assert "sendcmd=f='" in vf
    assert "crop=404:720:" in vf
    assert "scale=1080:1920" in vf
    assert "setsar=1" in vf
    assert notes == ["speaker_reframe:follow_active"]
    # The sendcmd script was written and carries crop x/y commands.
    assert cmd_file.exists()
    assert "crop x" in cmd_file.read_text()


def test_build_split_screen_layout_2up_vertical_tile_arithmetic():
    """Validates: Requirements 12.2

    A vertical 1080x1920 target with two shown tracks partitions into two
    1080x960 tiles stacked at y=0 and y=960 (exact, non-overlapping cover).
    """
    tracks = [
        Face_Track("F1", [FaceBox(0.0, 100, 200, 80, 80), FaceBox(0.2, 100, 200, 80, 80)]),
        Face_Track("F2", [FaceBox(0.0, 1400, 200, 80, 80), FaceBox(0.2, 1400, 200, 80, 80)]),
    ]
    assoc = Association(by_turn={0: "F1", 1: "F2"}, unassociated=[],
                        shown_order=["F1", "F2"])
    regions = build_split_screen_layout(
        [], assoc, tracks,
        target_w=1080, target_h=1920, src_w=1920, src_h=1080,
        max_regions=2,
    )
    assert len(regions) == 2
    assert (regions[0].dst_x, regions[0].dst_y, regions[0].dst_w, regions[0].dst_h) == (0, 0, 1080, 960)
    assert (regions[1].dst_x, regions[1].dst_y, regions[1].dst_w, regions[1].dst_h) == (0, 960, 1080, 960)
    assert regions[0].track_id == "F1" and regions[1].track_id == "F2"


def test_build_reframe_filter_split_screen_stack_direction():
    """Validates: Requirements 12.2

    The split-screen filtergraph uses ``vstack`` for a portrait target and
    ``hstack`` for a landscape target, in a single ``-filter_complex`` graph.
    """
    tracks = [
        Face_Track("F1", [FaceBox(0.0, 100, 200, 80, 80)]),
        Face_Track("F2", [FaceBox(0.0, 1400, 200, 80, 80)]),
    ]
    assoc = Association(by_turn={0: "F1", 1: "F2"}, unassociated=[],
                        shown_order=["F1", "F2"])

    portrait_regions = build_split_screen_layout(
        [], assoc, tracks, target_w=1080, target_h=1920,
        src_w=1920, src_h=1080, max_regions=2)
    _ia, graph_p, notes_p = build_reframe_filter(
        "split_screen", regions=portrait_regions,
        crop_w=0, crop_h=0, src_w=1920, src_h=1080,
        target_w=1080, target_h=1920)
    assert "vstack" in graph_p and "hstack" not in graph_p
    assert "[vout]" in graph_p
    assert notes_p == ["speaker_reframe:split_screen"]

    landscape_regions = build_split_screen_layout(
        [], assoc, tracks, target_w=1920, target_h=1080,
        src_w=1920, src_h=1080, max_regions=2)
    _ia, graph_l, _n = build_reframe_filter(
        "split_screen", regions=landscape_regions,
        crop_w=0, crop_h=0, src_w=1920, src_h=1080,
        target_w=1920, target_h=1080)
    assert "hstack" in graph_l and "vstack" not in graph_l


def test_apply_speaker_reframe_unknown_layout_substitutes_follow_active(monkeypatch, tmp_path):
    """Validates: Requirements 12.1

    An unknown ``layout`` is substituted with ``follow_active``: the single
    ffmpeg pass uses a ``-vf`` (sendcmd/crop/scale) command, not a
    ``-filter_complex`` split-screen graph. ``probe``/``_run`` are faked and a
    canned sampler injects one face track, so no ffmpeg/cv2 runs.
    """
    monkeypatch.setattr(reframe, "probe", lambda v: _landscape_info())
    calls = _recording_run(monkeypatch)

    sampler = CannedSampler(_frames([(100, 300, 120, 120)]))
    turns = [Speaker_Turn("S1", 0.0, 2.0)]
    dest = tmp_path / "out.mp4"

    reframe.apply_speaker_reframe(
        "src.mp4", dest, turns=turns, aspect="9:16",
        layout="totally-bogus-layout", sampler=sampler,
    )
    assert len(calls) == 1
    cmd = calls[0]
    assert "-vf" in cmd and "-filter_complex" not in cmd


def test_apply_speaker_reframe_split_screen_below_two_tracks_substitutes(monkeypatch, tmp_path):
    """Validates: Requirements 12.2, 12.4

    ``split_screen`` with fewer than two associated tracks falls back to
    ``follow_active`` (a single ``-vf`` pass), whereas two well-separated tracks
    produce the split-screen ``-filter_complex`` graph — both in ONE pass.
    """
    monkeypatch.setattr(reframe, "probe", lambda v: _landscape_info())

    # One face -> one track -> split_screen substituted with follow_active.
    calls = _recording_run(monkeypatch)
    sampler_one = CannedSampler(_frames([(100, 300, 120, 120)]))
    reframe.apply_speaker_reframe(
        "src.mp4", tmp_path / "one.mp4",
        turns=[Speaker_Turn("S1", 0.0, 2.0)], aspect="9:16",
        layout="split_screen", sampler=sampler_one,
    )
    assert len(calls) == 1
    assert "-vf" in calls[0] and "-filter_complex" not in calls[0]

    # Two well-separated faces -> two tracks -> genuine split_screen graph.
    calls2 = _recording_run(monkeypatch)
    sampler_two = CannedSampler(_frames([(100, 300, 120, 120), (1000, 300, 120, 120)]))
    reframe.apply_speaker_reframe(
        "src.mp4", tmp_path / "two.mp4",
        turns=[Speaker_Turn("S1", 0.0, 1.0), Speaker_Turn("S2", 1.0, 2.0)],
        aspect="9:16", layout="split_screen", sampler=sampler_two,
    )
    assert len(calls2) == 1
    assert "-filter_complex" in calls2[0] and "-vf" not in calls2[0]


# =========================================================================== #
# Task 5.17 — ffmpeg integration: single-pass geometry outputs                 #
# =========================================================================== #
#
# These render tiny real clips with ffmpeg (guarded by ``requires_ffmpeg``) but
# still mock the face sampler with canned boxes (no cv2). They assert the
# geometry stage emits a single ffmpeg pass at the target resolution and that
# the geometry-prepared clip flows into the compositor with no additional
# geometry pass.


def _caption_words():
    return [FakeWord(0.2, 0.6, "hello"), FakeWord(0.7, 1.1, "there"),
            FakeWord(1.2, 1.6, "friend")]


@requires_ffmpeg
def test_follow_active_renders_at_target_resolution(make_video, tmp_path):
    """Validates: Requirements 8.3, 13.1

    A follow-active reframe of a landscape clip (canned two-face sampler)
    produces an output at the 9:16 target resolution in a single pass.
    """
    src = make_video("src.mp4", duration=2.0, w=1280, h=720)
    sampler = CannedSampler(_frames([(180, 300, 140, 140), (960, 300, 140, 140)],
                                    duration=2.0))
    turns = [Speaker_Turn("S1", 0.0, 1.0), Speaker_Turn("S2", 1.0, 2.0)]
    dest = tmp_path / "follow.mp4"

    out = reframe.apply_speaker_reframe(
        src, dest, turns=turns, aspect="9:16",
        layout="follow_active", sampler=sampler,
    )
    assert Path(out).exists()
    assert probe_size(out) == ASPECT_PRESETS["9:16"]


@requires_ffmpeg
def test_split_screen_single_ffmpeg_pass_at_target(make_video, tmp_path, monkeypatch):
    """Validates: Requirements 9.6, 13.2, 15.5

    A split-screen reframe (two canned tracks) is produced in a SINGLE ffmpeg
    invocation (spy on ``reframe._run`` while still executing the real command)
    at the 9:16 target resolution.
    """
    src = make_video("src.mp4", duration=2.0, w=1280, h=720)
    sampler = CannedSampler(_frames([(150, 300, 140, 140), (1000, 300, 140, 140)],
                                    duration=2.0))
    turns = [Speaker_Turn("S1", 0.0, 1.0), Speaker_Turn("S2", 1.0, 2.0)]
    dest = tmp_path / "split.mp4"

    calls: list[list[str]] = []
    real_run = reframe._run

    def _spy(cmd):
        calls.append(list(cmd))
        return real_run(cmd)

    monkeypatch.setattr(reframe, "_run", _spy)

    out = reframe.apply_speaker_reframe(
        src, dest, turns=turns, aspect="9:16",
        layout="split_screen", sampler=sampler,
    )
    # Exactly one ffmpeg invocation for the whole geometry stage.
    assert len(calls) == 1
    assert "-filter_complex" in calls[0]
    assert Path(out).exists()
    assert probe_size(out) == ASPECT_PRESETS["9:16"]


@requires_ffmpeg
def test_geometry_prepared_clip_flows_into_compositor(make_video, png_asset, tmp_path):
    """Validates: Requirements 13.3, 20.5

    The geometry-prepared clip (already at the 9:16 target) flows into
    ``compositor.render_clip`` with captions + emoji enabled and the composited
    output stays at the target resolution — i.e. the compositor applies no
    ADDITIONAL geometry pass.
    """
    src = make_video("src.mp4", duration=2.0, w=1280, h=720)
    sampler = CannedSampler(_frames([(180, 300, 140, 140), (960, 300, 140, 140)],
                                    duration=2.0))
    turns = [Speaker_Turn("S1", 0.0, 1.0), Speaker_Turn("S2", 1.0, 2.0)]
    geo = tmp_path / "geo.mp4"
    reframe.apply_speaker_reframe(
        src, geo, turns=turns, aspect="9:16",
        layout="follow_active", sampler=sampler,
    )
    assert probe_size(geo) == ASPECT_PRESETS["9:16"]

    asset = png_asset("e.png")
    opts = ProcessingOptions(captions=True, emoji="heavy")
    result = compositor.render_clip(
        geo, tmp_path / "final.mp4", opts, _caption_words(), tmp_path,
        emoji_resolver=lambda c: asset,
    )
    assert result is not None
    assert result.path.exists()
    # No additional geometry pass: the composited clip keeps the target size.
    assert probe_size(result.path) == ASPECT_PRESETS["9:16"]
