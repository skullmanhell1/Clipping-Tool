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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from config import settings

# ``Speaker_Turn`` is only needed for type hints on the multi-speaker
# association path; importing it here keeps the module self-describing without
# creating a hard runtime dependency cycle (diarization imports nothing from
# this module).
from worker.diarization import Speaker_Turn
from worker.ffmpeg_utils import ASPECT_PRESETS, FFmpegError, _run, probe


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


def ema_smooth(values: list[float], alpha: float = 0.35) -> list[float]:
    """Exponential moving-average smoothing of a 1-D sequence."""
    if not values:
        return []
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def smooth_centers(samples: list[Center], alpha: float = 0.35) -> list[Center]:
    """Smooth the x/y paths of ``samples`` independently with an EMA."""
    if not samples:
        return []
    xs = ema_smooth([s.cx for s in samples], alpha)
    ys = ema_smooth([s.cy for s in samples], alpha)
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
) -> str:
    """Return ``sendcmd`` script text setting the crop x/y over time.

    Each line: ``<t> crop x <X>, crop y <Y>;`` where X/Y are the top-left of the
    crop window, derived from the (clamped) centre so the window stays inside
    the frame.
    """
    max_x = max(0, src_w - crop_w)
    max_y = max(0, src_h - crop_h)
    lines: list[str] = []
    for c in centers:
        x = int(round(_clamp(c.cx - crop_w / 2.0, 0, max_x)))
        y = int(round(_clamp(c.cy - crop_h / 2.0, 0, max_y)))
        lines.append(f"{c.t:.3f} crop x {x}, crop y {y};")
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
def _default_haar_detector(cv2) -> Optional[Callable[[object], list[tuple[int, int, int, int]]]]:
    """Build the default OpenCV Haar-cascade detector callable.

    Returns a function ``frame -> list[(x, y, w, h)]`` or ``None`` when the
    cascade cannot be loaded. This is the exact detection used by the v0.7.0
    single-speaker path, extracted so the single-face and multi-face code
    share one cascade implementation.
    """
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(cascade_path)
    if detector.empty():
        return None

    def _detect(frame) -> list[tuple[int, int, int, int]]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
        )
        return [tuple(int(v) for v in f) for f in faces]

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
            try:
                x, y, w, h = b
            except (TypeError, ValueError):
                continue
            frame_boxes.append(FaceBox(round(float(t), 3), int(x), int(y), int(w), int(h)))
        result.append(frame_boxes)
    return result


def track_faces(video: str | Path, sample_fps: float = 5.0) -> list[Center]:
    """Sample frames and return the main-face centre path (``Center`` samples).

    Uses OpenCV's Haar cascade (via the shared :func:`_sample_face_boxes`
    machinery). Returns ``[]`` when cv2 is unavailable, the video cannot be
    opened, or no faces are found anywhere (caller falls back). This is the
    unchanged v0.7.0 single-speaker behaviour: it keeps only the dominant face
    per sampled frame and holds the last known centre through frames with no
    detection.
    """
    per_frame = _sample_face_boxes(video, sample_fps=sample_fps)
    if not per_frame:
        return []

    samples: list[Center] = []
    last_center: Optional[tuple[float, float]] = None
    for t, boxes in per_frame:
        center = pick_main_face([tuple(f) for f in boxes])
        if center is None:
            center = last_center
        if center is not None:
            samples.append(Center(round(t, 3), center[0], center[1]))
            last_center = center
    return samples


def apply_reframe(
    video: str | Path,
    dest: str | Path,
    aspect: str = "9:16",
    sample_fps: float = 5.0,
    command_fps: float = 12.0,
    smoothing: float = 0.35,
) -> Path:
    """Reframe ``video`` to ``aspect`` following the main face; write ``dest``.

    Raises :class:`ReframeUnavailable` when no usable face path is found or the
    aspect is not narrower than the source (nothing to track) so the caller can
    fall back to the static reformat.
    """
    if aspect not in ASPECT_PRESETS:
        raise ReframeUnavailable(f"Unknown aspect '{aspect}'")

    dest = Path(dest)
    info = probe(video)
    tw, th = ASPECT_PRESETS[aspect]
    aw, ah = _aspect_ratio_parts(aspect)

    crop_w, crop_h = compute_crop_size(info.width, info.height, aw, ah)
    if crop_w >= info.width and crop_h >= info.height:
        # Target isn't a tighter crop than the source; nothing to follow.
        raise ReframeUnavailable("target aspect is not narrower than source")

    samples = track_faces(video, sample_fps=sample_fps)
    if not samples:
        raise ReframeUnavailable("no faces detected")

    smoothed = smooth_centers(samples, alpha=smoothing)
    dense = resample_centers(smoothed, command_fps, info.duration)
    script = build_sendcmd(dense, crop_w, crop_h, info.width, info.height)

    cmd_file = dest.with_suffix(".reframe.cmd")
    cmd_file.parent.mkdir(parents=True, exist_ok=True)
    cmd_file.write_text(script, encoding="utf-8")

    # Initial crop position (first command); sendcmd updates x/y over time.
    first = dense[0]
    x0 = int(round(_clamp(first.cx - crop_w / 2.0, 0, info.width - crop_w)))
    y0 = int(round(_clamp(first.cy - crop_h / 2.0, 0, info.height - crop_h)))

    escaped = str(cmd_file.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    vf = (
        f"sendcmd=f='{escaped}',"
        f"crop={crop_w}:{crop_h}:{x0}:{y0},"
        f"scale={tw}:{th},setsar=1"
    )
    cmd = [
        settings.ffmpeg_binary, "-y", "-i", str(video),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
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
    command_fps: float = 12.0,
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

    # EMA-smooth the x/y series with the intensity alpha.
    xs = ema_smooth([c[0] for c in base], alpha)
    ys = ema_smooth([c[1] for c in base], alpha)

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


def _track_mean_center(track: Optional[Face_Track]) -> Optional[tuple[float, float]]:
    """Average centre of a track's boxes (or ``None`` when it has no boxes)."""
    if track is None or not track.boxes:
        return None
    xs = [b.center[0] for b in track.boxes]
    ys = [b.center[1] for b in track.boxes]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


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

    if target_h >= target_w:
        # Portrait target: stack tiles vertically, full width.
        base_h = target_h // n
        y = 0
        for k, tid in enumerate(shown):
            h = base_h if k < n - 1 else target_h - base_h * (n - 1)
            src_cx, src_cy = _region_source_center(
                track_by_id.get(tid), src_w, src_h, target_w, h
            )
            regions.append(Region(0, y, target_w, h, src_cx, src_cy, tid))
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
            regions.append(Region(x, 0, w, target_h, src_cx, src_cy, tid))
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
            parts.append(
                f"[0:v]crop={rcw}:{rch}:{x}:{y},"
                f"scale={rg.dst_w}:{rg.dst_h}[{label}]"
            )
            labels.append(f"[{label}]")
        stack = "vstack" if portrait else "hstack"
        joined = "".join(labels)
        graph = ";".join(parts)
        graph += f";{joined}{stack}=inputs={len(labels)},setsar=1[vout]"
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

    escaped = (
        str(Path(sendcmd_path).resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        if sendcmd_path is not None
        else ""
    )
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
        )
        if not regions:
            # Fewer than two associated tracks -> follow_active substitution.
            layout = "follow_active"

    if layout == "split_screen":
        _ia, graph, _notes = build_reframe_filter(
            "split_screen",
            regions=regions,
            crop_w=crop_w, crop_h=crop_h,
            src_w=info.width, src_h=info.height,
            target_w=tw, target_h=th,
            intensity=intensity,
        )
        cmd = [
            settings.ffmpeg_binary, "-y", "-i", str(video),
            "-filter_complex", graph,
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy", "-movflags", "+faststart",
            str(dest),
        ]
        try:
            _run(cmd)
        except FFmpegError as exc:
            raise ReframeUnavailable(f"ffmpeg reframe failed: {exc}") from exc
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
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
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
