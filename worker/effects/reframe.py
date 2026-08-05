"""Face-aware vertical reframing.

When enabled (and the target aspect is narrower than the source), this replaces
the static centre-crop / blurred-background reformat with a crop window that
*follows the main speaker*:

1. Sample frames across the clip and detect faces (MediaPipe if available,
   otherwise OpenCV's Haar cascade).
2. Pick the dominant face per sample (largest / most persistent).
3. Smooth the crop-centre path (EMA + linear resample) so the "camera" glides
   instead of jumping.
4. Apply the moving crop in a single ffmpeg pass via the ``sendcmd`` +
   ``crop`` filters, then scale to the target resolution.

The heavy vision dependency (``cv2``) is imported lazily, and every failure
mode (no cv2, no faces, ffmpeg error) degrades gracefully so the caller can
fall back to the static reformat.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional, Sequence

from config import settings

# ``Speaker_Turn`` is only needed for type hints on the multi-speaker
# association path; importing it here keeps the module self-describing without
# creating a hard runtime dependency cycle (diarization imports nothing from
# this module).
from worker import scene_detect
from worker.diarization import Speaker_Turn
from worker.ffmpeg_utils import (
    ASPECT_PRESETS,
    FFmpegError,
    _run,
    detect_letterbox,
    escape_filter_path,
    h264_args,
    probe,
)


class ReframeUnavailable(RuntimeError):
    """Raised when face-tracking reframe cannot be produced (use fallback)."""


@dataclass
class Center:
    """A crop-centre sample: time ``t`` (s) and centre ``(cx, cy)`` in px."""

    t: float
    cx: float
    cy: float


# --------------------------------------------------------------------------- #
# Pure geometry / smoothing helpers (no ffmpeg or cv2 needed — unit-testable)
# --------------------------------------------------------------------------- #
def compute_crop_size(
    src_w: int, src_h: int, aspect_w: int, aspect_h: int
) -> tuple[int, int]:
    """Return the largest ``(w, h)`` of the target aspect that fits the source.

    Dimensions are rounded to even numbers (encoder-friendly).
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError("source dimensions must be positive")
    target = aspect_w / aspect_h
    source = src_w / src_h
    if target <= source:
        # Target is narrower (or equal): full height, reduced width.
        ch = src_h
        cw = int(round(ch * target))
    else:
        # Target is wider: full width, reduced height.
        cw = src_w
        ch = int(round(cw / target))
    cw = min(src_w, cw - (cw % 2))
    ch = min(src_h, ch - (ch % 2))
    return max(2, cw), max(2, ch)


def pick_main_face(faces: list[tuple[int, int, int, int]]) -> Optional[tuple[float, float]]:
    """Return the centre ``(cx, cy)`` of the largest face box, or ``None``.

    ``faces`` is a list of ``(x, y, w, h)`` rectangles.
    """
    if not faces:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return (x + w / 2.0, y + h / 2.0)


def ema_smooth(
    values: list[float],
    alpha: float = 0.35,
    *,
    reset_at: Sequence[int] = (),
) -> list[float]:
    """Exponential moving-average smoothing of a 1-D sequence.

    ``reset_at`` holds indices at which the average restarts from the incoming value instead of
    blending with the previous one - V4's shot-change reset. Without it the EMA carries the
    previous shot's framing across a cut and then converges on the new one over the following
    second or so, which on screen is a slow drift immediately after every cut: the crop appears
    to be searching for the subject. The subject did not move; the camera changed, and the right
    response to a discontinuity in the input is a discontinuity in the output.
    """
    if not values:
        return []
    breaks = {int(i) for i in reset_at}
    out = [values[0]]
    for index, v in enumerate(values[1:], start=1):
        if index in breaks:
            out.append(v)
        else:
            out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def ema_smooth_zero_phase(
    values: list[float],
    alpha: float = 0.35,
    *,
    reset_at: Sequence[int] = (),
) -> list[float]:
    """Zero-phase (lag-free) smoothing of a 1-D sequence, segment by segment.

    :func:`ema_smooth` is *causal*: every output depends only on samples at or before it, so
    the smoothed path necessarily trails the real one. That is unavoidable when filtering a
    live stream and entirely avoidable here — the whole face path is collected before ffmpeg
    is invoked, so the filter is allowed to look ahead.

    The measured cost of not looking ahead, against a ground-truth path where the subject
    pans 500 px/s (``scripts/bench_reframe.py``): a **mean centre error of 40 px and a p95 of
    159 px** at the full 5 fps sampling rate. The 9:16 crop of a 1080p frame is 608 px wide,
    so a p95 of 159 px puts the subject a quarter of the way to the edge of shot while the
    crop is still catching up. On screen that is the crop lagging behind a moving presenter
    and overshooting when they stop.

    The fix runs the same causal filter forwards and backwards and **averages** the two
    results. Their phase shifts are equal and opposite, so the average is centred on the
    input rather than delayed behind it: for a symmetric input the output is exactly
    symmetric, which is the definition of zero phase and is what
    ``test_zero_phase_smoothing_is_symmetric_and_the_causal_filter_is_not`` asserts.

    Averaging rather than *cascading* the two passes, for two reasons found by measurement:

    * cascading does not actually achieve zero phase here. :func:`ema_smooth` seeds itself
      with ``values[0]``, and that initialisation is asymmetric, so it survives the round
      trip — ``[0, 100, 0]`` cascades to ``[18.24, 30.4, 24]``, still lopsided.
    * cascading applies the filter twice, squaring its attenuation. That over-smooths, and
      over-smoothing is not free: at a starved sample rate the wider kernel erases real
      movement, which showed up in the benchmark as mean error getting *worse*. Averaging
      keeps the nominal bandwidth of a single pass.

    ``reset_at`` is honoured as a hard boundary in **both** directions, which is the part that
    matters for correctness rather than smoothness. A cut is a discontinuity in the input, so
    the response to it must be a discontinuity in the output (V4) — and a backward pass that
    ran across a cut would drag the *next* shot's framing into the end of the previous one,
    which is the same defect V4 fixed, reflected in time. Each segment between resets is
    therefore filtered independently, and a single-sample segment passes through untouched.
    """
    if not values:
        return []
    breaks = sorted({int(i) for i in reset_at if 0 < int(i) < len(values)})
    bounds = [0, *breaks, len(values)]

    out: list[float] = []
    for start, end in zip(bounds, bounds[1:]):
        segment = values[start:end]
        if len(segment) <= 1:
            out.extend(segment)
            continue
        forward = ema_smooth(segment, alpha)
        backward = ema_smooth(segment[::-1], alpha)[::-1]
        out.extend((f + b) / 2.0 for f, b in zip(forward, backward))
    return out


def cut_indices(samples: Sequence[Center], cuts: Sequence[float]) -> list[int]:
    """Sample indices that are the first on the far side of a cut (V4).

    Pure, so the mapping can be tested without ffmpeg. A cut before the first sample or after
    the last produces no index: there is no boundary *within* the series to reset at, and index
    0 already starts the average fresh.
    """
    if not samples or not cuts:
        return []
    indices: list[int] = []
    for cut in sorted(float(c) for c in cuts):
        for index in range(1, len(samples)):
            if samples[index - 1].t < cut <= samples[index].t:
                indices.append(index)
                break
    return sorted(set(indices))


def smooth_centers(
    samples: list[Center],
    alpha: float = 0.35,
    *,
    cuts: Sequence[float] = (),
    zero_phase: Optional[bool] = None,
) -> list[Center]:
    """Smooth the x/y paths of ``samples`` independently.

    ``cuts`` are absolute shot-change times; smoothing restarts at each (V4).

    ``zero_phase`` selects :func:`ema_smooth_zero_phase` over the causal :func:`ema_smooth`,
    defaulting to ``settings.reframe_zero_phase``. It is a setting rather than a constant
    only so the previous behaviour is recoverable on a specific clip; the default is on
    because the causal filter's lag is measurable and visible (see
    :func:`ema_smooth_zero_phase`).
    """
    if not samples:
        return []
    if zero_phase is None:
        zero_phase = bool(getattr(settings, "reframe_zero_phase", True))
    smoother = ema_smooth_zero_phase if zero_phase else ema_smooth
    breaks = cut_indices(samples, cuts)
    xs = smoother([s.cx for s in samples], alpha, reset_at=breaks)
    ys = smoother([s.cy for s in samples], alpha, reset_at=breaks)
    return [Center(s.t, x, y) for s, x, y in zip(samples, xs, ys)]


def resample_centers(
    samples: list[Center], fps: float, duration: float
) -> list[Center]:
    """Linearly resample ``samples`` onto a uniform ``fps`` grid over ``duration``.

    Produces the dense, evenly-spaced centres used to emit smooth crop
    commands. Values are held/clamped at the ends.
    """
    if not samples:
        return []
    if len(samples) == 1:
        s = samples[0]
        return [Center(0.0, s.cx, s.cy)]

    fps = max(1.0, float(fps))
    n = max(1, int(round(duration * fps)))
    out: list[Center] = []
    j = 0
    for i in range(n + 1):
        t = i / fps
        while j < len(samples) - 2 and samples[j + 1].t < t:
            j += 1
        a, b = samples[j], samples[j + 1]
        span = (b.t - a.t) or 1e-6
        frac = max(0.0, min(1.0, (t - a.t) / span))
        cx = a.cx + (b.cx - a.cx) * frac
        cy = a.cy + (b.cy - a.cy) * frac
        out.append(Center(round(t, 3), cx, cy))
    return out


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def build_sendcmd(
    centers: list[Center],
    crop_w: int,
    crop_h: int,
    src_w: int,
    src_h: int,
    *,
    origin_x: int = 0,
    origin_y: int = 0,
    target: str = "crop",
) -> str:
    """Return ``sendcmd`` script text setting the crop x/y over time.

    Each line: ``<t> crop x <X>, crop y <Y>;`` where X/Y are the top-left of the
    crop window, derived from the (clamped) centre so the window stays inside
    the frame.

    ``target`` is the filter these commands address. It is ``crop`` for the single-crop
    follow-active pass, and a ``crop@tN`` *instance* name for split-screen tiles, where several
    crops share one filtergraph and a bare ``crop`` would broadcast every tile's commands to all
    of them (V5).

    ``origin_x``/``origin_y`` and ``src_w``/``src_h`` describe the region the crop is allowed to
    move within, which for a letterboxed source is the content rectangle rather than the whole
    frame (V16). Confining the *existing* crop is how de-letterboxing is done here: prepending a
    second ``crop`` filter would be simpler to read but ``sendcmd`` addresses filters by name, so
    both crops would receive these x/y commands and the bars would be panned around the output.
    The defaults (origin 0 with full frame dimensions) reproduce the previous script exactly.
    """
    max_x = max(0, src_w - crop_w)
    max_y = max(0, src_h - crop_h)
    lines: list[str] = []
    for c in centers:
        x = origin_x + int(round(_clamp(c.cx - origin_x - crop_w / 2.0, 0, max_x)))
        y = origin_y + int(round(_clamp(c.cy - origin_y - crop_h / 2.0, 0, max_y)))
        lines.append(f"{c.t:.3f} {target} x {x}, {target} y {y};")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Multi-face detection data model (speaker-aware reframe)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FaceBox:
    """One detected face in a sampled frame.

    ``t`` is the sample time (seconds); ``x, y, w, h`` is the pixel rectangle.
    """

    t: float
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[float, float]:
        """The box centre ``(cx, cy)`` in pixels."""
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)


@dataclass(frozen=True)
class Face_Track:
    """A face path across sampled frames with a stable ``track_id`` (e.g. ``"F1"``)."""

    track_id: str
    boxes: list[FaceBox] = field(default_factory=list)

    def center_at(self, t: float) -> Optional[tuple[float, float]]:
        """Return the centre of the box nearest in time to ``t``.

        Returns ``None`` when the track has no boxes. Pure.
        """
        if not self.boxes:
            return None
        nearest = min(self.boxes, key=lambda b: abs(b.t - float(t)))
        return nearest.center

    def presence(self, start: float, end: float) -> float:
        """Fraction of ``[start, end]`` covered by detected boxes (0..1). Pure.

        Each sampled box is treated as covering half a sampling interval on
        either side of its timestamp; the covered sub-intervals are unioned,
        clipped to the window, and divided by the window length. The sampling
        interval is estimated from the track's own consecutive box spacing,
        falling back to ``1 / settings.reframe_sample_fps`` when it cannot be
        inferred.
        """
        start = float(start)
        end = float(end)
        window = end - start
        if window <= 0 or not self.boxes:
            return 0.0

        times = sorted(b.t for b in self.boxes)
        dt = _estimate_sample_interval(times)
        half = dt / 2.0

        intervals: list[tuple[float, float]] = []
        for t in times:
            a = max(start, t - half)
            b = min(end, t + half)
            if b > a:
                intervals.append((a, b))
        if not intervals:
            return 0.0

        intervals.sort()
        covered = 0.0
        cur_s, cur_e = intervals[0]
        for a, b in intervals[1:]:
            if a <= cur_e:
                cur_e = max(cur_e, b)
            else:
                covered += cur_e - cur_s
                cur_s, cur_e = a, b
        covered += cur_e - cur_s
        return max(0.0, min(1.0, covered / window))


def _default_sample_interval() -> float:
    """Fallback face-sampling interval (seconds) derived from config."""
    fps = float(settings.reframe_sample_fps) or 5.0
    return 1.0 / fps if fps > 0 else 0.2


def _estimate_sample_interval(times: list[float]) -> float:
    """Estimate the sampling interval from sorted sample ``times`` (median of
    positive consecutive gaps), falling back to the configured default."""
    if len(times) >= 2:
        gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
        gaps = [g for g in gaps if g > 0]
        if gaps:
            gaps.sort()
            mid = len(gaps) // 2
            if len(gaps) % 2:
                return gaps[mid]
            return (gaps[mid - 1] + gaps[mid]) / 2.0
    return _default_sample_interval()


# --------------------------------------------------------------------------- #
# Association result + face<->speaker association (pure)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Association:
    """Result of associating :class:`Speaker_Turn`s with :class:`Face_Track`s.

    ``by_turn`` maps a turn index to its associated ``track_id`` (or ``None``);
    ``unassociated`` lists the indices of turns with no usable track;
    ``shown_order`` ranks associated ``track_id``s by total speaking duration.
    """

    by_turn: dict[int, Optional[str]] = field(default_factory=dict)
    unassociated: list[int] = field(default_factory=list)
    shown_order: list[str] = field(default_factory=list)


def associate_faces(
    turns: list[Speaker_Turn],
    tracks: list[Face_Track],
) -> Association:
    """PURE association of speaker turns to face tracks.

    Rules (Reqs 6.1-6.5):
      - assign at most one track per turn, the one maximising ``presence`` over
        the turn window (6.1, 6.2);
      - a turn with no overlapping track -> ``None`` + listed in
        ``unassociated`` (6.3);
      - keep the number of distinct associated tracks <= the number of distinct
        speaker labels (6.4);
      - turns sharing a ``speaker_label`` map to the SAME track: a single
        per-label track is decided from the aggregate presence across that
        label's turns, then applied to each of its turns (6.5).
    """
    by_turn: dict[int, Optional[str]] = {}
    unassociated: list[int] = []

    if not turns:
        return Association(by_turn={}, unassociated=[], shown_order=[])
    if not tracks:
        for i in range(len(turns)):
            by_turn[i] = None
            unassociated.append(i)
        return Association(by_turn=by_turn, unassociated=unassociated, shown_order=[])

    track_by_id = {tr.track_id: tr for tr in tracks}
    track_order = [tr.track_id for tr in tracks]

    # Distinct speaker labels in first-appearance order.
    labels_in_order: list[str] = []
    for t in turns:
        if t.speaker_label not in labels_in_order:
            labels_in_order.append(t.speaker_label)

    # Aggregate presence per (label, track) across all of the label's turns.
    agg: dict[tuple[str, str], float] = {}
    for t in turns:
        for tr in tracks:
            key = (t.speaker_label, tr.track_id)
            agg[key] = agg.get(key, 0.0) + tr.presence(t.start, t.end)

    # Greedy one-to-one assignment of tracks to labels by descending aggregate
    # presence, so distinct speakers map to distinct faces and the count of
    # associated tracks never exceeds the count of labels (Req 6.4).
    candidates = sorted(
        (
            (agg[(label, tid)], labels_in_order.index(label), track_order.index(tid), label, tid)
            for label in labels_in_order
            for tid in track_order
            if agg[(label, tid)] > 0.0
        ),
        key=lambda c: (-c[0], c[1], c[2]),
    )
    label_track: dict[str, str] = {}
    used_labels: set[str] = set()
    used_tracks: set[str] = set()
    for _score, _lo, _to, label, tid in candidates:
        if label in used_labels or tid in used_tracks:
            continue
        label_track[label] = tid
        used_labels.add(label)
        used_tracks.add(tid)

    # Apply the per-label track to each turn; a turn whose assigned track has no
    # presence over its window (or whose label got no track) is unassociated.
    for i, t in enumerate(turns):
        tid = label_track.get(t.speaker_label)
        if tid is not None and track_by_id[tid].presence(t.start, t.end) > 0.0:
            by_turn[i] = tid
        else:
            by_turn[i] = None
            unassociated.append(i)

    # Rank shown tracks by total speaking duration of their associated turns.
    duration_by_track: dict[str, float] = {}
    for i, t in enumerate(turns):
        tid = by_turn[i]
        if tid is not None:
            duration_by_track[tid] = duration_by_track.get(tid, 0.0) + max(0.0, t.end - t.start)
    shown_order = sorted(
        duration_by_track,
        key=lambda tid: (-duration_by_track[tid], track_order.index(tid)),
    )

    return Association(by_turn=by_turn, unassociated=unassociated, shown_order=shown_order)


# --------------------------------------------------------------------------- #
# Face-track grouping (pure)
# --------------------------------------------------------------------------- #
def _iou(a: FaceBox, b: FaceBox) -> float:
    """Intersection-over-union of two face boxes."""
    ax2, ay2 = a.x + a.w, a.y + a.h
    bx2, by2 = b.x + b.w, b.y + b.h
    ix1, iy1 = max(a.x, b.x), max(a.y, b.y)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def _centroid_dist(a: FaceBox, b: FaceBox) -> float:
    """Euclidean distance between two face-box centres."""
    acx, acy = a.center
    bcx, bcy = b.center
    return math.hypot(acx - bcx, acy - bcy)


def build_face_tracks(
    per_frame: list[list[FaceBox]], *, iou_thresh: float = 0.3
) -> list[Face_Track]:
    """PURE: group per-frame face boxes into :class:`Face_Track`s.

    Boxes are matched to existing tracks across consecutive sampled frames by
    IoU continuity (with a conservative nearest-centroid fallback for faces
    that drift enough between samples to stop overlapping). Each track receives
    a stable ``track_id`` (``"F1"``, ``"F2"`` ...) in creation order. When no
    frame contains any face box the result is ``[]`` (Req 5.5). Testable
    without cv2.
    """
    open_tracks: list[dict] = []
    for frame_boxes in per_frame:
        assigned: set[int] = set()
        for box in frame_boxes:
            # Best IoU match among unclaimed tracks in this frame.
            best_tr: Optional[dict] = None
            best_iou = 0.0
            for tr in open_tracks:
                if id(tr) in assigned:
                    continue
                score = _iou(box, tr["boxes"][-1])
                if score > best_iou:
                    best_iou = score
                    best_tr = tr
            if best_tr is not None and best_iou >= iou_thresh:
                best_tr["boxes"].append(box)
                assigned.add(id(best_tr))
                continue

            # Conservative centroid fallback (within half a face width).
            best_tr = None
            best_d: Optional[float] = None
            for tr in open_tracks:
                if id(tr) in assigned:
                    continue
                last = tr["boxes"][-1]
                d = _centroid_dist(box, last)
                tol = 0.5 * min(box.w, box.h, last.w, last.h)
                if d <= tol and (best_d is None or d < best_d):
                    best_d = d
                    best_tr = tr
            if best_tr is not None:
                best_tr["boxes"].append(box)
                assigned.add(id(best_tr))
            else:
                new = {"id": f"F{len(open_tracks) + 1}", "boxes": [box]}
                open_tracks.append(new)

    return [Face_Track(tr["id"], tr["boxes"]) for tr in open_tracks]


# --------------------------------------------------------------------------- #
# Frame sampling + face detection (lazy cv2) + ffmpeg application
# --------------------------------------------------------------------------- #
#: Smallest face the cascade is asked to find, in **native frame** pixels.
#:
#: Deliberately still the 60 px this has always used, rather than the fraction-of-frame it
#: arguably should be. Whether a 60 px face on 4K is the subject or a bystander is a
#: behavioural question, and answering it inside a change about sampling cost would alter
#: which faces are found while claiming only to find them faster.
_MIN_FACE_PX_NATIVE = 60

#: Floor on the minimum in *working* coordinates. Below this the cascade is asked for faces
#: a handful of pixels across, where it is both unreliable and slower (more scales to
#: search). Only binds when the frame is downscaled by more than ~3.3x, i.e. 4K and up.
_MIN_FACE_PX_FLOOR = 18


def _default_haar_detector(
    cv2, *, detect_width: Optional[int] = None
) -> Optional[Callable[[object], list[tuple[int, int, int, int]]]]:
    """Build the default OpenCV Haar-cascade detector callable.

    Returns a function ``frame -> list[(x, y, w, h)]`` in **native frame coordinates**, or
    ``None`` when the cascade cannot be loaded. This is the cascade used by the v0.7.0
    single-speaker path, extracted so the single-face and multi-face code share one
    implementation.

    ``detect_width`` (default ``settings.reframe_detect_width``) is the width the frame is
    scaled to *before* detection, with boxes scaled back afterwards. Detection dominated the
    sampling cost — measured on a 60 s 1080p clip, 300 sampled frames cost 5.5 s of detection
    against 2.2 s of decode — and that cost is what forced the sample cap down to a rate that
    could not follow a moving subject. Detecting on a smaller frame buys the cost back.

    It is nearly free in accuracy because a crop window is far larger than the error: on a
    real photograph, detecting at 320 px wide instead of 512 moved the resolved face centre
    by **1.4 px** in native coordinates, against a 608 px-wide 9:16 crop of 1080p.
    ``INTER_AREA`` is the right filter here specifically because it averages rather than
    samples, so it does not alias away the eye/brow contrast the cascade keys on.

    The scaling happens inside the detector rather than in :func:`_sample_face_boxes` so that
    an *injected* detector still receives native frames, exactly as its contract says.
    """
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(cascade_path)
    if detector.empty():
        return None

    if detect_width is None:
        detect_width = int(getattr(settings, "reframe_detect_width", 0) or 0)

    def _detect(frame) -> list[tuple[int, int, int, int]]:
        height, width = frame.shape[:2]
        scale = 1.0
        if detect_width and 0 < detect_width < width:
            scale = detect_width / float(width)
            frame = cv2.resize(
                frame,
                (detect_width, max(1, int(round(height * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # The same real-world face size as before: the native-pixel minimum carried into
        # working coordinates, so downscaling changes the cost and not the admission rule.
        min_side = max(_MIN_FACE_PX_FLOOR, int(round(_MIN_FACE_PX_NATIVE * scale)))
        faces = detector.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_side, min_side)
        )
        if scale == 1.0:
            return [tuple(int(v) for v in f) for f in faces]
        inverse = 1.0 / scale
        return [tuple(int(round(v * inverse)) for v in f) for f in faces]

    return _detect


def _sample_face_boxes(
    video: str | Path,
    *,
    sample_fps: float,
    max_samples: Optional[int] = None,
    detector: Optional[Callable] = None,
) -> list[tuple[float, list[tuple[int, int, int, int]]]]:
    """Sample frames across ``video`` and run a face detector on each.

    Returns a list of ``(t, boxes)`` for every sampled frame (including frames
    with no detections), where ``boxes`` is a list of ``(x, y, w, h)`` tuples.
    The heavy vision dependency (``cv2``) is imported lazily; on a missing
    ``cv2``, an unopenable video, or an unloadable default cascade this returns
    ``[]`` and NEVER raises. When ``max_samples`` is given the sampling step is
    widened so at most that many frames are returned. CPU-only.

    ``detector`` is an injected callable ``frame -> list[(x, y, w, h)]``; when
    ``None`` the default lazy-cv2 Haar cascade is used.
    """
    try:
        import cv2  # type: ignore
    except Exception:
        return []

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        if not fps or fps <= 0:
            fps = 30.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        step = max(1, int(round(fps / max(0.5, sample_fps))))

        # Widen the step so we never emit more than ``max_samples`` frames.
        if max_samples and max_samples > 0 and frame_count:
            approx = int(frame_count // step) + 1
            if approx > max_samples:
                step = max(step, int(math.ceil(frame_count / max_samples)))

        if detector is None:
            detector = _default_haar_detector(cv2)
            if detector is None:
                return []

        out: list[tuple[float, list[tuple[int, int, int, int]]]] = []
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                boxes = list(detector(frame))
                out.append((idx / fps, boxes))
                if max_samples and max_samples > 0 and len(out) >= max_samples:
                    break
            idx += 1
    finally:
        cap.release()

    return out


def _as_rect(box: object) -> Optional[tuple[int, int, int, int]]:
    """Coerce one detection to ``(x, y, w, h)``, or ``None`` if it is not a rectangle.

    Accepts a 4-element sequence *and* anything exposing ``x``/``y``/``w``/``h``, which
    :class:`FaceBox` does. Unpacking positionally - as this used to - silently discarded a
    ``FaceBox``, because it carries a leading ``t`` and so has five fields: the
    ``except (TypeError, ValueError): continue`` swallowed it and the frame came back empty.
    Inert with the built-in cascade, which yields plain 4-tuples, and a trap for exactly the
    person plugging in a better detector - they would get a working render, a ``reframe``
    marker, and a crop that never moved.
    """
    for attrs in (("x", "y", "w", "h"),):
        if all(hasattr(box, name) for name in attrs):
            try:
                return tuple(int(getattr(box, name)) for name in attrs)  # type: ignore[return-value]
            except (TypeError, ValueError):
                return None
    try:
        x, y, w, h = box  # type: ignore[misc]
        return int(x), int(y), int(w), int(h)
    except (TypeError, ValueError):
        return None


def detect_faces(
    video: str | Path,
    *,
    sample_fps: Optional[float] = None,
    max_samples: Optional[int] = None,
    detector: Optional[Callable] = None,
) -> list[list[FaceBox]]:
    """Sample <= ``max_samples`` frames and return ALL face boxes per frame.

    Unlike the v0.7.0 single-speaker path (which keeps only the largest face),
    this returns every detected box in each sampled frame as a
    :class:`FaceBox` (Req 5.1). ``sample_fps`` defaults to
    ``settings.reframe_sample_fps`` and ``max_samples`` to
    ``settings.reframe_sample_cap`` (Req 15.2). The vision import is lazy; on a
    missing ``cv2`` or an unopenable video this returns ``[]`` and never raises
    (Reqs 5.3, 5.4). CPU-only. ``detector`` is an injected callable
    ``frame -> list[(x, y, w, h)]`` (defaults to the lazy Haar cascade).
    """
    if sample_fps is None:
        sample_fps = settings.reframe_sample_fps
    if max_samples is None:
        max_samples = settings.reframe_sample_cap

    per_frame = _sample_face_boxes(
        video, sample_fps=sample_fps, max_samples=max_samples, detector=detector
    )

    result: list[list[FaceBox]] = []
    for t, boxes in per_frame:
        frame_boxes: list[FaceBox] = []
        for b in boxes:
            rect = _as_rect(b)
            if rect is None:
                continue
            x, y, w, h = rect
            frame_boxes.append(FaceBox(round(float(t), 3), x, y, w, h))
        result.append(frame_boxes)
    return result


@dataclass(frozen=True)
class Track_Report:
    """The main-face path plus enough to say how well tracking actually went.

    ``sampled`` is how many frames were examined, ``detected`` how many held any face at
    all, and ``tracked`` how many contributed a box to the chosen subject's track.
    ``coverage`` is the last of those over the first, because it is the one that decides
    what the crop does: a frame the subject was not found in is a frame the crop is
    guessing on, whether or not some other face was visible.
    """

    centers: list[Center]
    sampled: int = 0
    detected: int = 0
    tracked: int = 0

    @property
    def coverage(self) -> float:
        """Fraction of sampled frames in which the tracked subject was found (0..1)."""
        return (self.tracked / self.sampled) if self.sampled else 0.0


def choose_main_track(tracks: list[Face_Track]) -> Optional[Face_Track]:
    """PURE: the track most likely to be the subject - present longest, size as tie-break.

    Replaces "the largest box in this frame, decided again from scratch every frame", which
    is what the single-face path used to do and which has no defence against a false
    positive: a cascade phantom that happens to be bigger than the real face wins outright,
    and the crop jumps to it for exactly as long as it persists. PR #92 measured the shipped
    cascade at **1.32 faces per detecting frame** on a two-shot, so phantoms are a documented
    reality rather than a hypothetical, and a benchmark against an intermittent phantom
    larger than the real face had ``pick_main_face`` choosing the phantom on **every frame it
    appeared**.

    Presence is the discriminator because it is the thing a phantom does not have. A
    spurious detection fires on a particular alignment of background texture and stops when
    the shot moves; a person stays in frame. So the boxes are grouped into tracks first (by
    :func:`build_face_tracks`, which already does IoU continuity for the speaker-aware path)
    and the track with the most boxes wins.

    Size only breaks ties, and enters as ``sqrt`` of the median area rather than the area
    itself - the same reasoning PR #92 applied to its confidence weighting. Plain area lets
    scale dominate: a phantom twice the width of the real face has four times its area, so it
    would only need a quarter of the presence to win. Under ``sqrt`` it needs half, and the
    median rather than the mean keeps one enormous frame from carrying a whole track.

    Deterministic: ``max`` returns the first maximal element, so an exact tie resolves to
    creation order, which is frame order.
    """
    if not tracks:
        return None

    def score(track: Face_Track) -> float:
        if not track.boxes:
            return 0.0
        areas = sorted(float(b.w) * float(b.h) for b in track.boxes)
        mid = len(areas) // 2
        median = areas[mid] if len(areas) % 2 else (areas[mid - 1] + areas[mid]) / 2.0
        return len(track.boxes) * math.sqrt(max(1.0, median))

    best = max(tracks, key=score)
    return best if best.boxes else None


def track_faces_report(
    video: str | Path,
    *,
    sample_fps: Optional[float] = None,
    max_samples: Optional[int] = None,
    detector: Optional[Callable] = None,
) -> Track_Report:
    """Sample frames, follow one subject across them, and report how well that went.

    ``sample_fps`` and ``max_samples`` default to ``settings.reframe_sample_fps`` and
    ``settings.reframe_sample_cap``. They used to be ignored on this path entirely - see
    :func:`apply_reframe`.

    Returns an empty report (rather than raising) when cv2 is unavailable, the video cannot
    be opened, or no face is found anywhere; the caller turns that into
    :class:`ReframeUnavailable` and the pipeline falls back down its ladder.

    The subject's centre is held through frames where it was not detected, which is the
    previous behaviour and the right one - freezing the crop is better than snapping it to
    the frame centre - but the *number* of frames that happened on is now reported instead
    of being invisible.
    """
    if sample_fps is None:
        sample_fps = float(settings.reframe_sample_fps)
    if max_samples is None:
        max_samples = int(settings.reframe_sample_cap)

    per_frame = _sample_face_boxes(
        video, sample_fps=sample_fps, max_samples=max_samples, detector=detector
    )
    if not per_frame:
        return Track_Report([])

    frames: list[list[FaceBox]] = []
    times: list[float] = []
    for t, boxes in per_frame:
        stamp = round(float(t), 3)
        times.append(stamp)
        frames.append([FaceBox(stamp, int(b[0]), int(b[1]), int(b[2]), int(b[3])) for b in boxes])

    detected = sum(1 for boxes in frames if boxes)
    main = choose_main_track(build_face_tracks(frames))
    if main is None:
        return Track_Report([], sampled=len(times), detected=detected)

    by_time = {box.t: box.center for box in main.boxes}
    samples: list[Center] = []
    last: Optional[tuple[float, float]] = None
    for stamp in times:
        center = by_time.get(stamp, last)
        if center is None:
            # Before the subject first appears there is nothing to hold, so these frames
            # contribute no command and the crop simply starts where the subject does.
            continue
        last = center
        samples.append(Center(stamp, center[0], center[1]))

    return Track_Report(
        samples, sampled=len(times), detected=detected, tracked=len(by_time)
    )


def track_faces(video: str | Path, sample_fps: float = 5.0) -> list[Center]:
    """The main-face centre path. Thin wrapper over :func:`track_faces_report`.

    Kept because it is this module's long-standing public entry point for the single-face
    path; callers that need to know how well tracking went want the report instead.
    """
    return track_faces_report(video, sample_fps=sample_fps).centers


#: Shared with every other filter-string builder; see :func:`ffmpeg_utils.escape_filter_path`.
_escape_filter_path = escape_filter_path


def _content_rect(video: str | Path, info) -> tuple[int, int, int, int]:
    """``(width, height, x, y)`` of the real picture inside ``video``'s frame (V16).

    Returns the full frame when de-letterboxing is disabled or when no bars are found, so the
    non-letterboxed case - which is most sources - is unchanged and costs no extra work beyond
    the detection probe.
    """
    if not getattr(settings, "auto_deletterbox", True):
        return (info.width, info.height, 0, 0)
    found = detect_letterbox(video)
    if not found:
        return (info.width, info.height, 0, 0)
    width, height, x, y = found
    # Never return a rectangle that is not fully inside the frame: a bogus detection would
    # otherwise produce a crop ffmpeg rejects, turning a cosmetic improvement into a failed clip.
    if x < 0 or y < 0 or x + width > info.width or y + height > info.height:
        return (info.width, info.height, 0, 0)
    return (width, height, x, y)


def apply_reframe(
    video: str | Path,
    dest: str | Path,
    aspect: str = "9:16",
    sample_fps: Optional[float] = None,
    command_fps: Optional[float] = None,
    smoothing: float = 0.35,
) -> Path:
    """Reframe ``video`` to ``aspect`` following the main face; write ``dest``.

    Raises :class:`ReframeUnavailable` when no usable face path is found or the
    aspect is not narrower than the source (nothing to track) so the caller can
    fall back to the static reformat.

    ``sample_fps`` now defaults to ``settings.reframe_sample_fps`` instead of a hard-coded
    ``5.0``. It was a literal default, and ``pipeline.py`` calls this with the aspect only,
    so **REFRAME_SAMPLE_FPS had no effect on the default reframe path at all** - an operator
    lowering it to make renders cheaper, or raising it to follow a livelier subject, changed
    nothing and got no warning. The same was true of REFRAME_SAMPLE_CAP, which
    :func:`track_faces` never passed on, leaving this path's sampling cost unbounded and
    growing linearly with clip length. Both settings are now honoured, which also puts a
    ceiling on the work: at the shipped values a clip up to 180s - the longest the
    ``90s-3min`` preset produces - still gets the full rate.
    """
    if aspect not in ASPECT_PRESETS:
        raise ReframeUnavailable(f"Unknown aspect '{aspect}'")

    dest = Path(dest)
    info = probe(video)
    tw, th = ASPECT_PRESETS[aspect]
    aw, ah = _aspect_ratio_parts(aspect)

    # V8: the crop-update rate is a setting rather than a fixed 12/s, which was visible as
    # stepping whenever the subject moved quickly. Only the sendcmd script gets longer.
    if command_fps is None:
        command_fps = float(getattr(settings, "reframe_command_fps", 24.0) or 24.0)

    # V16: measure against the content rectangle, not the padded frame. On an already-boxed
    # source the two differ, and every number below - crop size, clamps, the "is this even a
    # tighter crop" test - is wrong if it is taken from the frame.
    content = _content_rect(video, info)
    src_w, src_h, origin_x, origin_y = content

    crop_w, crop_h = compute_crop_size(src_w, src_h, aw, ah)
    if crop_w >= src_w and crop_h >= src_h:
        # Target isn't a tighter crop than the source; nothing to follow.
        raise ReframeUnavailable("target aspect is not narrower than source")

    report = track_faces_report(video, sample_fps=sample_fps)
    samples = report.centers
    if not samples:
        raise ReframeUnavailable("no faces detected")

    # V4: reset the smoother at every shot change, so the crop does not drift across a cut.
    # One extra video-only decode of a clip-length file, and only when reframing is already
    # running - which has itself just decoded the clip to find faces, so this is not the pass
    # that makes reframe expensive.
    cuts: list[float] = []
    if getattr(settings, "reframe_reset_on_cut", True):
        cuts = scene_detect.scan_cuts(video)

    smoothed = smooth_centers(samples, alpha=smoothing, cuts=cuts)
    dense = resample_centers(smoothed, command_fps, info.duration)
    script = build_sendcmd(
        dense, crop_w, crop_h, src_w, src_h, origin_x=origin_x, origin_y=origin_y
    )

    cmd_file = dest.with_suffix(".reframe.cmd")
    cmd_file.parent.mkdir(parents=True, exist_ok=True)
    cmd_file.write_text(script, encoding="utf-8")

    # Initial crop position (first command); sendcmd updates x/y over time.
    first = dense[0]
    x0 = origin_x + int(round(_clamp(first.cx - origin_x - crop_w / 2.0, 0, src_w - crop_w)))
    y0 = origin_y + int(round(_clamp(first.cy - origin_y - crop_h / 2.0, 0, src_h - crop_h)))

    escaped = str(cmd_file.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    vf = (
        f"sendcmd=f='{escaped}',"
        f"crop={crop_w}:{crop_h}:{x0}:{y0},"
        f"scale={tw}:{th},setsar=1"
    )
    cmd = [
        settings.ffmpeg_binary, "-y", "-i", str(video),
        "-vf", vf,
        *h264_args(),
        "-c:a", "copy", "-movflags", "+faststart",
        str(dest),
    ]
    try:
        _run(cmd)
    except FFmpegError as exc:
        raise ReframeUnavailable(f"ffmpeg reframe failed: {exc}") from exc
    finally:
        cmd_file.unlink(missing_ok=True)
    return dest


def _aspect_ratio_parts(aspect: str) -> tuple[int, int]:
    """Parse ``"9:16"`` -> ``(9, 16)``."""
    a, _, b = aspect.partition(":")
    return int(a), int(b)



# --------------------------------------------------------------------------- #
# Speaker-aware reframe geometry (pure) + single-pass orchestration
# --------------------------------------------------------------------------- #
#
# The functions below extend the single-speaker reframe with the speaker-aware
# geometry described in the design. Everything except ``apply_speaker_reframe``
# is a PURE function (no ffmpeg, no cv2) so the crop-path / split-screen
# geometry is unit- and property-testable offline. They reuse the existing
# ``Center``, ``ema_smooth``, ``_clamp``, ``compute_crop_size``,
# ``build_sendcmd``, ``_run``, ``probe``, ``ASPECT_PRESETS`` and
# ``_aspect_ratio_parts`` machinery so the v0.7.0 ``apply_reframe`` path is
# untouched.


# Deterministic mapping intensity -> (ema_alpha, transition_seconds).
# Lower alpha = stronger smoothing = slower movement; longer transition.
# Monotonic across subtle < standard < heavy in alpha (and faster movement /
# shorter transition as intensity increases).
REFRAME_INTENSITY: dict[str, tuple[float, float]] = {
    "subtle":   (0.15, 0.60),   # strongest smoothing, slowest, longest xfade
    "standard": (0.35, 0.35),
    "heavy":    (0.60, 0.18),   # weakest smoothing, fastest, shortest xfade
}


def intensity_params(intensity: str) -> tuple[float, float]:
    """Return ``(smoothing_alpha, transition_seconds)`` for ``intensity``.

    Unknown or malformed values map to the ``standard`` pair (Reqs 10.2, 11.2).
    The mapping is deterministic and monotonic: ``subtle < standard < heavy`` in
    ``alpha`` (weaker smoothing / faster movement as intensity rises) and the
    transition duration decreases ``subtle > standard > heavy`` (Reqs 10.3,
    10.4).
    """
    if not isinstance(intensity, str):
        return REFRAME_INTENSITY["standard"]
    return REFRAME_INTENSITY.get(intensity, REFRAME_INTENSITY["standard"])


def _turn_index_at(turns: list[Speaker_Turn], t: float) -> Optional[int]:
    """Return the index of the turn containing time ``t`` (``start <= t < end``),
    accepting ``t == end`` for the final turn so the endpoint is covered."""
    for i, tn in enumerate(turns):
        if tn.start <= t < tn.end:
            return i
    # Endpoint / boundary inclusive fallback (e.g. t == duration == last end).
    for i, tn in enumerate(turns):
        if tn.start <= t <= tn.end:
            return i
    return None


def build_follow_active_path(
    turns: list[Speaker_Turn],
    assoc: Association,
    tracks: list[Face_Track],
    *,
    src_w: int,
    src_h: int,
    crop_w: int,
    crop_h: int,
    intensity: str = "standard",
    command_fps: Optional[float] = None,
    duration: float,
) -> list[Center]:
    """PURE: build the dense follow-active crop-centre path.

    Produces an evenly-spaced (``command_fps`` over ``duration``) list of
    :class:`Center`s driving the ``sendcmd``+``crop`` pass:

      - within a turn associated with a track, target that track's
        ``center_at(t)`` (Req 8.1);
      - within an unassociated turn (or a turn whose track has no centre), HOLD
        the most recent valid centre; when no prior centre exists yet, use the
        frame centre ``(src_w/2, src_h/2)`` as a safe default (Req 8.4);
      - on a speaker change (the associated track differs between consecutive
        turns), interpolate the centre over the intensity-derived
        ``transition_seconds`` instead of jumping, ending no later than the
        start of the next turn's stable window (Reqs 11.1, 11.2, 11.4);
      - smooth the x/y series with the intensity EMA ``alpha`` (Req 10.2);
      - CLAMP every centre so the crop window stays fully inside the source
        frame at every time, including during transitions (Reqs 8.2, 10.5,
        11.3);
      - every emitted ``Center.t`` lies in ``[0, duration]`` (Req 8.5).

    No ffmpeg / cv2. Feeds :func:`build_sendcmd`.
    """
    alpha, transition = intensity_params(intensity)
    duration = max(0.0, float(duration))
    # V8: from settings, was a fixed 12/s.
    if command_fps is None:
        command_fps = float(getattr(settings, "reframe_command_fps", 24.0) or 24.0)
    command_fps = max(1.0, float(command_fps))

    frame_center = (src_w / 2.0, src_h / 2.0)
    track_by_id = {tr.track_id: tr for tr in tracks}

    # Dense, evenly-spaced time grid over [0, duration] (endpoint included).
    n = max(1, int(round(duration * command_fps)))
    times: list[float] = [min(duration, i / command_fps) for i in range(n + 1)]
    if times[-1] < duration:
        times.append(duration)

    # Instantaneous target centre with hold-on-gap logic.
    base: list[tuple[float, float]] = []
    last_valid: Optional[tuple[float, float]] = None
    for t in times:
        idx = _turn_index_at(turns, t) if turns else None
        center: Optional[tuple[float, float]] = None
        if idx is not None:
            tid = assoc.by_turn.get(idx)
            if tid is not None:
                tr = track_by_id.get(tid)
                if tr is not None:
                    center = tr.center_at(t)
        if center is None:
            center = last_valid if last_valid is not None else frame_center
        else:
            last_valid = center
        base.append(center)

    # Keep a pristine copy so a transition's "from" centre is the previous
    # speaker's held position, not an already-blended value.
    base_orig = list(base)

    def _center_before(ts: float) -> tuple[float, float]:
        prev = base_orig[0]
        for j, t in enumerate(times):
            if t < ts:
                prev = base_orig[j]
            else:
                break
        return prev

    # Interpolate the crop centre over the intensity transition on each speaker
    # change (associated track differs between consecutive turns).
    for i in range(1, len(turns)):
        if assoc.by_turn.get(i) == assoc.by_turn.get(i - 1):
            continue
        ts = float(turns[i].start)
        next_start = float(turns[i + 1].start) if i + 1 < len(turns) else duration
        te = min(transition, float(turns[i].end) - ts, next_start - ts)
        if te <= 0:
            continue
        fx, fy = _center_before(ts)
        for j, t in enumerate(times):
            if ts <= t <= ts + te:
                frac = max(0.0, min(1.0, (t - ts) / te))
                bx, by = base_orig[j]
                base[j] = (fx + (bx - fx) * frac, fy + (by - fy) * frac)

    # Smooth the x/y series with the intensity alpha. Zero-phase by default for the same
    # reason as the single-speaker path: this grid is fully known before ffmpeg runs, so
    # there is no reason to accept a causal filter's lag. The deliberate speaker-change
    # ramps above survive it — symmetric smoothing widens a transition by roughly
    # ``1/alpha`` grid steps either side (~125 ms at alpha 0.35 on a 24 fps grid), which
    # softens the start and end of a move rather than displacing it.
    smoother = (
        ema_smooth_zero_phase
        if bool(getattr(settings, "reframe_zero_phase", True))
        else ema_smooth
    )
    xs = smoother([c[0] for c in base], alpha)
    ys = smoother([c[1] for c in base], alpha)

    # Clamp every centre so the crop window stays fully in-frame throughout.
    lo_x, hi_x = crop_w / 2.0, src_w - crop_w / 2.0
    lo_y, hi_y = crop_h / 2.0, src_h - crop_h / 2.0
    out: list[Center] = []
    for t, x, y in zip(times, xs, ys):
        cx = _clamp(x, lo_x, hi_x)
        cy = _clamp(y, lo_y, hi_y)
        out.append(Center(round(float(t), 3), cx, cy))
    return out


# --------------------------------------------------------------------------- #
# Split-screen geometry (pure)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Region:
    """A destination tile + the source crop feeding it.

    ``dst_*`` is the target-frame tile; ``src_cx``/``src_cy`` is the centre of
    the source crop (sized to the tile aspect) that feeds the tile;
    ``track_id`` names the shown :class:`Face_Track`.
    """

    dst_x: int
    dst_y: int
    dst_w: int
    dst_h: int
    src_cx: float
    src_cy: float
    track_id: str
    #: V5: the tile's crop centre over time, or ``()`` for a fixed crop.
    #:
    #: Defaulted and last so every existing positional construction is unaffected. When empty
    #: the tile renders exactly as it did before V5: one static crop at ``src_cx``/``src_cy``.
    centers: tuple[Center, ...] = ()


def _track_mean_center(track: Optional[Face_Track]) -> Optional[tuple[float, float]]:
    """Average centre of a track's boxes (or ``None`` when it has no boxes)."""
    if track is None or not track.boxes:
        return None
    xs = [b.center[0] for b in track.boxes]
    ys = [b.center[1] for b in track.boxes]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def build_region_centers(
    track: Optional[Face_Track],
    *,
    src_w: int,
    src_h: int,
    dst_w: int,
    dst_h: int,
    duration: float,
    command_fps: Optional[float] = None,
    intensity: str = "standard",
) -> tuple[Center, ...]:
    """PURE: the crop-centre path for one split-screen tile (V5).

    Split-screen previously froze each tile on the *mean* of its track's boxes for the whole
    clip. On a still, seated interview that is fine. On anything else the mean is a position the
    subject occupied only on average: a speaker who leans in and back sits off-centre for most of
    the clip, and one who moves across frame is cropped out of their own tile entirely - while
    the single-speaker path has followed faces since v0.7.0. This gives each tile the same
    treatment: its own smoothed, clamped centre path.

    Uses the same EMA smoothing and clamping as the follow-active path, so a tile cannot drift
    outside the source frame and does not jitter per detection. Returns ``()`` when there is
    nothing to follow (no track, no boxes, no duration), which the caller renders as the previous
    static crop.
    """
    duration = max(0.0, float(duration))
    if track is None or not track.boxes or duration <= 0:
        return ()
    if command_fps is None:
        command_fps = float(getattr(settings, "reframe_command_fps", 24.0) or 24.0)
    command_fps = max(1.0, float(command_fps))

    crop_w, crop_h = compute_crop_size(src_w, src_h, dst_w, dst_h)
    alpha, _ = intensity_params(intensity)

    n = max(1, int(round(duration * command_fps)))
    times = [min(duration, i / command_fps) for i in range(n + 1)]

    xs: list[float] = []
    ys: list[float] = []
    last: Optional[tuple[float, float]] = None
    for t in times:
        center = track.center_at(t) or last or (src_w / 2.0, src_h / 2.0)
        last = center
        xs.append(center[0])
        ys.append(center[1])

    xs = ema_smooth(xs, alpha=alpha)
    ys = ema_smooth(ys, alpha=alpha)
    return tuple(
        Center(
            round(t, 3),
            _clamp(x, crop_w / 2.0, max(crop_w / 2.0, src_w - crop_w / 2.0)),
            _clamp(y, crop_h / 2.0, max(crop_h / 2.0, src_h - crop_h / 2.0)),
        )
        for t, x, y in zip(times, xs, ys)
    )


def _region_source_center(
    track: Optional[Face_Track], src_w: int, src_h: int, dst_w: int, dst_h: int
) -> tuple[float, float]:
    """Return the (clamped) source-crop centre for a tile of aspect
    ``dst_w:dst_h``, centred on ``track``'s face and kept fully in-frame."""
    crop_w, crop_h = compute_crop_size(src_w, src_h, dst_w, dst_h)
    center = _track_mean_center(track)
    if center is None:
        cx, cy = src_w / 2.0, src_h / 2.0
    else:
        cx, cy = center
    cx = _clamp(cx, crop_w / 2.0, src_w - crop_w / 2.0)
    cy = _clamp(cy, crop_h / 2.0, src_h - crop_h / 2.0)
    return (cx, cy)


def _grid_regions(
    shown: list[str],
    track_by_id: dict[str, Face_Track],
    target_w: int,
    target_h: int,
    src_w: int,
    src_h: int,
) -> list[Region]:
    """Lay ``shown`` out as a 2-column grid filling the target exactly (V6).

    Tiles are emitted in reading order and the geometry is exact rather than nearly exact: the
    last column absorbs any horizontal rounding remainder and the last row any vertical one, so
    the tiles tile the frame with no seam and no overlap. ``hstack``/``vstack`` reject mismatched
    dimensions outright, so "close enough" here is a failed render rather than a soft edge.

    An odd final tile spans the full width. Leaving a black half-cell instead would read as a
    missing participant.
    """
    rows = (len(shown) + 1) // 2
    base_h = target_h // rows
    regions: list[Region] = []
    y = 0
    for row in range(rows):
        h = base_h if row < rows - 1 else target_h - base_h * (rows - 1)
        in_row = shown[row * 2:row * 2 + 2]
        x = 0
        for col, tid in enumerate(in_row):
            w = target_w if len(in_row) == 1 else (
                target_w // 2 if col == 0 else target_w - target_w // 2
            )
            src_cx, src_cy = _region_source_center(
                track_by_id.get(tid), src_w, src_h, w, h
            )
            regions.append(Region(x, y, w, h, src_cx, src_cy, tid))
            x += w
        y += h
    return regions


def build_split_screen_layout(
    turns: list[Speaker_Turn],
    assoc: Association,
    tracks: list[Face_Track],
    *,
    target_w: int,
    target_h: int,
    src_w: int,
    src_h: int,
    max_regions: Optional[int] = None,
    duration: float = 0.0,
    intensity: str = "standard",
) -> list[Region]:
    """PURE: build the default 2-up split-screen layout.

    Shown tracks come from ``assoc.shown_order`` (ranked by speaking duration);
    when more than ``max_regions`` tracks are associated the top ``max_regions``
    by speaking duration are shown (Req 9.4). Fewer than two associated tracks
    -> ``[]`` so the caller falls back to ``follow_active`` (Req 9.5).

    The target frame is partitioned into non-overlapping tiles that EXACTLY
    cover it (the last tile absorbs any integer-rounding remainder): stacked
    vertically for a portrait target (``target_h >= target_w``, full width) and
    side-by-side for a landscape target (full height) (Reqs 9.1, 9.3). Each
    tile's source crop is centred on its track (Req 9.2). ``max_regions``
    defaults to ``settings.split_screen_max_regions`` (2). No ffmpeg / cv2.
    """
    if max_regions is None:
        max_regions = settings.split_screen_max_regions
    max_regions = max(1, int(max_regions))

    shown = list(assoc.shown_order)
    if len(shown) < 2:
        return []
    shown = shown[:max_regions]
    n = len(shown)
    if n < 2:
        return []

    track_by_id = {tr.track_id: tr for tr in tracks}
    regions: list[Region] = []

    def with_motion(region: Region) -> Region:
        """Attach the V5 per-tile centre path. ``duration=0`` leaves the tile static."""
        if duration <= 0:
            return region
        centers = build_region_centers(
            track_by_id.get(region.track_id),
            src_w=src_w, src_h=src_h,
            dst_w=region.dst_w, dst_h=region.dst_h,
            duration=duration, intensity=intensity,
        )
        return replace(region, centers=centers) if centers else region

    if target_h >= target_w and n >= 3:
        # V6: three or four speakers in a portrait frame go into a 2-column grid, not a stack.
        #
        # Four stacked tiles across 1920 px are 1080x480 each - a 2.25:1 letterbox slot holding a
        # crop of a face, which is the worst possible use of the space. Two columns give
        # 540x960 tiles: portrait slots that match the shape of a head and shoulders, so each
        # speaker is actually recognisable. The last row absorbs the remainder, and with an odd
        # count the final tile spans the full width rather than leaving a hole.
        return [
            with_motion(r)
            for r in _grid_regions(shown, track_by_id, target_w, target_h, src_w, src_h)
        ]

    if target_h >= target_w:
        # Portrait target: stack tiles vertically, full width.
        base_h = target_h // n
        y = 0
        for k, tid in enumerate(shown):
            h = base_h if k < n - 1 else target_h - base_h * (n - 1)
            src_cx, src_cy = _region_source_center(
                track_by_id.get(tid), src_w, src_h, target_w, h
            )
            regions.append(with_motion(Region(0, y, target_w, h, src_cx, src_cy, tid)))
            y += h
    else:
        # Landscape target: place tiles side-by-side, full height.
        base_w = target_w // n
        x = 0
        for k, tid in enumerate(shown):
            w = base_w if k < n - 1 else target_w - base_w * (n - 1)
            src_cx, src_cy = _region_source_center(
                track_by_id.get(tid), src_w, src_h, w, target_h
            )
            regions.append(with_motion(Region(x, 0, w, target_h, src_cx, src_cy, tid)))
            x += w

    return regions


# --------------------------------------------------------------------------- #
# ffmpeg geometry builder (pure) + single-pass orchestration
# --------------------------------------------------------------------------- #
def build_reframe_filter(
    layout: str,
    *,
    centers: Optional[list[Center]] = None,
    regions: Optional[list[Region]] = None,
    crop_w: int,
    crop_h: int,
    src_w: int,
    src_h: int,
    target_w: int,
    target_h: int,
    sendcmd_path: Optional[str] = None,
    intensity: str = "standard",
    tile_sendcmd_paths: Optional[Sequence[str]] = None,
    origin_x: int = 0,
    origin_y: int = 0,
) -> tuple[list[str], str, list[str]]:
    """PURE: return ``(input_args, filter_string_or_filtergraph, applied_notes)``
    for a SINGLE ffmpeg pass. Does NOT run ffmpeg.

    ``follow_active`` -> writes the ``sendcmd`` script referenced by
    ``sendcmd_path`` and returns the ``sendcmd`` + ``crop`` + ``scale`` +
    ``setsar`` ``-vf`` string exactly like :func:`apply_reframe` (Req 8.3).

    ``split_screen`` -> returns one ``-filter_complex`` graph: per region
    ``crop`` (source crop sized to the tile aspect and centred on the region's
    ``src_cx``/``src_cy``) -> ``scale`` to the tile -> ``vstack`` (portrait) /
    ``hstack`` (landscape) -> ``setsar=1`` labelled ``[vout]`` (Reqs 9.6, 13.2).

    ``applied_notes`` returns the layout marker
    (``speaker_reframe:follow_active`` / ``speaker_reframe:split_screen``).
    """
    if layout == "split_screen":
        regions = regions or []
        portrait = target_h >= target_w
        parts: list[str] = []
        labels: list[str] = []
        for k, rg in enumerate(regions):
            rcw, rch = compute_crop_size(src_w, src_h, rg.dst_w, rg.dst_h)
            x = int(round(_clamp(rg.src_cx - rcw / 2.0, 0, max(0, src_w - rcw))))
            y = int(round(_clamp(rg.src_cy - rch / 2.0, 0, max(0, src_h - rch))))
            label = f"r{k}"

            # V5: a tile that has a centre path gets its own sendcmd driving its own crop.
            #
            # The crop is given the *instance name* `crop@tN`. sendcmd dispatches by target name
            # across the whole filtergraph, so with several plain `crop` filters in one graph
            # every tile's commands would be applied to every tile - each crop would jump between
            # all the speakers' positions. Instance names make each target unambiguous.
            prefix = ""
            crop_name = "crop"
            if rg.centers and tile_sendcmd_paths and k < len(tile_sendcmd_paths):
                crop_name = f"crop@t{k}"
                script = build_sendcmd(
                    list(rg.centers), rcw, rch, src_w, src_h,
                    origin_x=origin_x, origin_y=origin_y, target=crop_name,
                )
                tile_path = Path(tile_sendcmd_paths[k])
                tile_path.parent.mkdir(parents=True, exist_ok=True)
                tile_path.write_text(script, encoding="utf-8")
                prefix = f"sendcmd=f='{_escape_filter_path(tile_path)}',"
                first = rg.centers[0]
                x = origin_x + int(
                    round(_clamp(first.cx - origin_x - rcw / 2.0, 0, max(0, src_w - rcw)))
                )
                y = origin_y + int(
                    round(_clamp(first.cy - origin_y - rch / 2.0, 0, max(0, src_h - rch)))
                )

            parts.append(
                f"[0:v]{prefix}{crop_name}={rcw}:{rch}:{x}:{y},"
                f"scale={rg.dst_w}:{rg.dst_h}[{label}]"
            )
            labels.append(f"[{label}]")

        # V6: rows first, then stack the rows. For a single-column or single-row layout - which
        # is every 2-up, and so every graph this produced before V6 - there is exactly one
        # grouping and the emitted string is unchanged.
        rows: list[list[int]] = []
        for k, rg in enumerate(regions):
            if rows and regions[rows[-1][0]].dst_y == rg.dst_y:
                rows[-1].append(k)
            else:
                rows.append([k])

        graph = ";".join(parts)
        single_row = len(rows) == 1
        single_column = all(len(row) == 1 for row in rows)

        if single_row or single_column:
            # One stack, exactly as before V6.
            stack = "hstack" if single_row and not single_column else "vstack"
            if single_row and single_column:
                stack = "vstack" if portrait else "hstack"
            graph += (
                f";{''.join(labels)}{stack}=inputs={len(labels)},setsar=1[vout]"
            )
        else:
            row_labels: list[str] = []
            for r, row in enumerate(rows):
                joined = "".join(labels[k] for k in row)
                row_label = f"[row{r}]"
                if len(row) == 1:
                    # A lone tile in a grid row is already full width; copy it into the row
                    # label rather than hstacking one input, which ffmpeg rejects.
                    graph += f";{joined}null{row_label}"
                else:
                    graph += f";{joined}hstack=inputs={len(row)}{row_label}"
                row_labels.append(row_label)
            graph += (
                f";{''.join(row_labels)}vstack=inputs={len(row_labels)},setsar=1[vout]"
            )
        return ([], graph, ["speaker_reframe:split_screen"])

    # follow_active (default) — mirror apply_reframe's single -vf pass.
    centers = centers or []
    tw, th = target_w, target_h
    script = build_sendcmd(centers, crop_w, crop_h, src_w, src_h)
    if sendcmd_path is not None:
        p = Path(sendcmd_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(script, encoding="utf-8")

    first = centers[0] if centers else Center(0.0, src_w / 2.0, src_h / 2.0)
    x0 = int(round(_clamp(first.cx - crop_w / 2.0, 0, max(0, src_w - crop_w))))
    y0 = int(round(_clamp(first.cy - crop_h / 2.0, 0, max(0, src_h - crop_h))))

    escaped = _escape_filter_path(sendcmd_path) if sendcmd_path is not None else ""
    vf = (
        f"sendcmd=f='{escaped}',"
        f"crop={crop_w}:{crop_h}:{x0}:{y0},"
        f"scale={tw}:{th},setsar=1"
    )
    return ([], vf, ["speaker_reframe:follow_active"])


def apply_speaker_reframe(
    video: str | Path,
    dest: str | Path,
    *,
    turns: list[Speaker_Turn],
    aspect: str = "9:16",
    layout: str = "follow_active",
    intensity: str = "standard",
    detector: Optional[Callable] = None,
    sampler: Optional[Callable] = None,
) -> Path:
    """Orchestrate speaker-aware reframe in a single ffmpeg pass; write ``dest``.

    Pipeline: ``compute_crop_size`` -> ``detect_faces`` (or the injected
    ``sampler``) -> ``build_face_tracks`` -> ``associate_faces`` -> build the
    follow-active path or split-screen regions -> :func:`build_reframe_filter`
    -> single :func:`_run`.

    Raises :class:`ReframeUnavailable` (so the pipeline falls back along its
    degradation chain) when the target aspect is not narrower than the source
    (Req 12.5), there are no turns, no face tracks, no usable geometry, or the
    ffmpeg command fails (Req 14.4). An unknown ``layout`` substitutes
    ``follow_active`` (Req 7.5); ``split_screen`` with fewer than two associated
    tracks substitutes ``follow_active`` (Req 9.5).

    ``detector`` and ``sampler`` are DI seams: when ``sampler`` is supplied it
    produces the per-frame face boxes (``list[list[FaceBox]]``); otherwise
    :func:`detect_faces` is used with the injected ``detector``.
    """
    if aspect not in ASPECT_PRESETS:
        raise ReframeUnavailable(f"Unknown aspect '{aspect}'")

    dest = Path(dest)
    info = probe(video)
    tw, th = ASPECT_PRESETS[aspect]
    aw, ah = _aspect_ratio_parts(aspect)

    crop_w, crop_h = compute_crop_size(info.width, info.height, aw, ah)
    if crop_w >= info.width and crop_h >= info.height:
        raise ReframeUnavailable("target aspect is not narrower than source")

    if not turns:
        raise ReframeUnavailable("no speaker turns")

    # Sample + detect faces (injected sampler overrides frame->box production).
    if sampler is not None:
        per_frame = sampler(video)
    else:
        per_frame = detect_faces(video, detector=detector)

    tracks = build_face_tracks(per_frame)
    if not tracks:
        raise ReframeUnavailable("no face tracks detected")

    assoc = associate_faces(turns, tracks)

    # Normalise the requested layout (unknown -> follow_active).
    if layout not in ("follow_active", "split_screen"):
        layout = "follow_active"

    regions: Optional[list[Region]] = None
    if layout == "split_screen":
        regions = build_split_screen_layout(
            turns, assoc, tracks,
            target_w=tw, target_h=th, src_w=info.width, src_h=info.height,
            # V5: give each tile a centre path over the clip instead of one fixed crop.
            duration=info.duration, intensity=intensity,
        )
        if not regions:
            # Fewer than two associated tracks -> follow_active substitution.
            layout = "follow_active"

    if layout == "split_screen":
        tile_files = [
            dest.with_suffix(f".tile{k}.cmd") for k in range(len(regions or []))
        ]
        _ia, graph, _notes = build_reframe_filter(
            "split_screen",
            regions=regions,
            crop_w=crop_w, crop_h=crop_h,
            src_w=info.width, src_h=info.height,
            target_w=tw, target_h=th,
            intensity=intensity,
            tile_sendcmd_paths=[str(p) for p in tile_files],
        )
        cmd = [
            settings.ffmpeg_binary, "-y", "-i", str(video),
            "-filter_complex", graph,
            "-map", "[vout]", "-map", "0:a?",
            *h264_args(),
            "-c:a", "copy", "-movflags", "+faststart",
            str(dest),
        ]
        try:
            _run(cmd)
        except FFmpegError as exc:
            raise ReframeUnavailable(f"ffmpeg reframe failed: {exc}") from exc
        finally:
            # The scripts are read during the render only; leaving them behind would litter the
            # output directory with one file per speaker per clip.
            for tile in tile_files:
                tile.unlink(missing_ok=True)
        return dest

    # follow_active
    path = build_follow_active_path(
        turns, assoc, tracks,
        src_w=info.width, src_h=info.height, crop_w=crop_w, crop_h=crop_h,
        intensity=intensity, duration=info.duration,
    )
    if not path:
        raise ReframeUnavailable("no usable crop path")

    cmd_file = dest.with_suffix(".reframe.cmd")
    _ia, vf, _notes = build_reframe_filter(
        "follow_active",
        centers=path,
        crop_w=crop_w, crop_h=crop_h,
        src_w=info.width, src_h=info.height,
        target_w=tw, target_h=th,
        sendcmd_path=str(cmd_file),
        intensity=intensity,
    )
    cmd = [
        settings.ffmpeg_binary, "-y", "-i", str(video),
        "-vf", vf,
        *h264_args(),
        "-c:a", "copy", "-movflags", "+faststart",
        str(dest),
    ]
    try:
        _run(cmd)
    except FFmpegError as exc:
        raise ReframeUnavailable(f"ffmpeg reframe failed: {exc}") from exc
    finally:
        cmd_file.unlink(missing_ok=True)
    return dest
