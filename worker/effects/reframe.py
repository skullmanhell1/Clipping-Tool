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

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from config import settings

# ``Speaker_Turn`` is only needed for type hints on the multi-speaker
# association path; importing it here keeps the module self-describing without
# creating a hard runtime dependency cycle (diarization imports nothing from
# this module).
from worker import headroom, scene_detect
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

logger = logging.getLogger(__name__)


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
def compute_crop_size(src_w: int, src_h: int, aspect_w: int, aspect_h: int) -> tuple[int, int]:
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


@dataclass(frozen=True)
class Detection:
    """One detected face: an absolute-pixel box with an optional confidence.

    ``score`` is ``Optional`` rather than defaulted to a number because Haar supplies no
    confidence at all. ``detectMultiScale`` can be asked for reject levels, but they are not
    comparable across scales and are not probabilities; synthesising a score from them would
    be the false precision this codebase declines elsewhere (see :mod:`worker.language`
    refusing to guess a language from Han script). ``None`` means "this backend does not
    know", which is a different statement from "confidence zero", and
    :func:`pick_main_face` branches on the difference.
    """

    x: int
    y: int
    w: int
    h: int
    score: float | None = None

    @property
    def area(self) -> int:
        return self.w * self.h

    def as_tuple(self) -> tuple[int, int, int, int]:
        """The ``(x, y, w, h)`` form every pre-existing caller expects."""
        return (self.x, self.y, self.w, self.h)


def relative_box_to_pixels(
    rel_x: float,
    rel_y: float,
    rel_w: float,
    rel_h: float,
    *,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    """Convert a detector's bounding box to an absolute-pixel box, or ``None``.

    Pure, and deliberately **not inlined at the call site**. Every other detector in this
    module reports pixels; MediaPipe's legacy ``solutions`` API reported values normalised to
    ``[0, 1]``. A box that silently stays normalised becomes a 1-pixel face at the frame
    origin, and *nothing downstream objects*: ``pick_main_face`` returns a centre,
    ``FaceBox`` validates, ``build_face_tracks`` builds tracks, ``build_sendcmd`` clamps to a
    valid window, and ffmpeg encodes successfully. The only symptom is every clip cropped to
    the frame's left edge, visible solely in the pixels. That is the same shape as the
    ``font_substituted:Arial`` defect, and it is why this is a named function with its own
    tests rather than four multiplications at the call site.

    **Accepts either coordinate system, on purpose.** When all four values are ``<= 1.0``
    they are treated as normalised and scaled by the frame dimensions; otherwise they are
    treated as already absolute and this becomes a clamp-and-validate step. That makes the
    function correct whichever form the installed library hands over, rather than correct
    only against the version it was written for — and the tasks API's actual answer is
    recorded in the spec's design document (measured: **absolute pixels**, see
    ``.kiro/specs/face-detection-upgrade/design.md``). The ambiguity is real but bounded: a
    genuinely absolute box with every value ``<= 1`` is at most one pixel, which the
    degeneracy check below rejects either way.

    **Order is fixed: convert, then clamp, then test for degeneracy.** Testing degeneracy
    first would admit a box lying entirely off-frame (it is non-degenerate until it is
    clipped); clamping before converting is meaningless because the bounds are in pixels.
    A partially visible face legitimately produces a box extending past the frame edge, so
    clamping is what can *create* degeneracy, which is why the test has to follow it.
    """
    if width <= 0 or height <= 0:
        return None
    try:
        values = (float(rel_x), float(rel_y), float(rel_w), float(rel_h))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in values):
        # NaN survives float() and then poisons every comparison and every clamp below,
        # reaching an ffmpeg crop argument as a literal "nan".
        return None

    normalised = all(abs(v) <= 1.0 for v in values)
    if normalised:
        x0 = values[0] * width
        y0 = values[1] * height
        x1 = x0 + values[2] * width
        y1 = y0 + values[3] * height
    else:
        x0, y0 = values[0], values[1]
        x1, y1 = x0 + values[2], y0 + values[3]

    # Clamp in float space, then measure what survived, then coerce to int. Measuring before
    # the int coercion is what makes the degeneracy test meaningful: rounding a 0.1-pixel box
    # outward would manufacture a 1-pixel "detection" out of nothing, and a box narrower than
    # a single pixel is not a face however it arrived.
    fx0 = max(0.0, min(float(width), min(x0, x1)))
    fy0 = max(0.0, min(float(height), min(y0, y1)))
    fx1 = max(0.0, min(float(width), max(x0, x1)))
    fy1 = max(0.0, min(float(height), max(y0, y1)))
    if (fx1 - fx0) < 1.0 or (fy1 - fy0) < 1.0:
        return None

    # No second degeneracy check after the int coercion: it would be unreachable. The float
    # test above guarantees ``fx1 - fx0 >= 1``, and both are already inside ``[0, width]``, so
    # ``ceil(fx1) > floor(fx0)`` necessarily. A dead guard here was not harmless -- it silently
    # rescued a mutation that moved the degeneracy test to the wrong side of the clamp, which
    # made the documented convert/clamp/test order unverifiable.
    px0 = int(math.floor(fx0))
    py0 = int(math.floor(fy0))
    px1 = min(width, int(math.ceil(fx1)))
    py1 = min(height, int(math.ceil(fy1)))
    return (px0, py0, px1 - px0, py1 - py0)


def detection_coverage(samples: Sequence[tuple[float, Sequence[object]]]) -> float:
    """The fraction of sampled frames containing at least one detection.

    ``0.0`` for an empty sample list rather than a ``ZeroDivisionError``: zero frames sampled
    and zero frames with a face are different causes with the same honest answer, and the
    caller distinguishes them by whether it got any samples at all.
    """
    if not samples:
        return 0.0
    hit = sum(1 for _t, boxes in samples if boxes)
    return max(0.0, min(1.0, hit / len(samples)))


# Marker builders. Kept as functions rather than f-strings at the call sites so the spelling
# has exactly one definition -- these strings are the only channel a caller sees, and two
# call sites formatting "the same" marker slightly differently is the duplicated-fact defect
# mutation testing has caught twice in this repository.
def face_detector_marker(resolved: str) -> str:
    """``face_detector:{resolved}`` -- names the backend that actually ran."""
    return f"face_detector:{resolved}"


def face_detector_substituted_marker(requested: str, resolved: str) -> str:
    """``face_detector_substituted:{requested}:{resolved}`` -- names **both** sides.

    Following ``caption_font_substituted:{script}:{family}``: a marker naming only the
    outcome cannot tell you what was lost, which is the whole reason the operator is reading
    it.
    """
    return f"face_detector_substituted:{requested}:{resolved}"


def low_confidence_marker(coverage: float) -> str:
    """``reframe_low_confidence:{coverage:.2f}`` -- fixed precision, never ``str(float)``.

    A marker whose text varied with float repr would make golden comparison
    platform-dependent, and the golden renders are how the byte-parity requirement is
    verified.
    """
    return f"reframe_low_confidence:{coverage:.2f}"


def sample_rate_marker(effective_fps: float) -> str:
    """``reframe_sample_rate:{fps:.1f}`` -- the rate that actually ran, one decimal."""
    return f"reframe_sample_rate:{effective_fps:.1f}"


#: Prefix :func:`resolve_detector` uses internally to encode "a substitution happened", carrying
#: both sides so the marker can name them. Internal because the wire format callers see is the
#: marker, and that translation happens in exactly one place -- :func:`detector_marker_for`.
_SUBSTITUTED_PREFIX = "substituted:"


def detector_marker_for(resolved_label: str) -> str:
    """Turn a :func:`resolve_detector` label into the marker recorded on the clip.

    One function so the two marker spellings have one decision point between them. A caller
    doing this inline would have to remember that a substitution is spelled differently from a
    plain resolution, and the failure mode of forgetting is a marker that says Haar ran and
    nothing that says MediaPipe was asked for.
    """
    if resolved_label.startswith(_SUBSTITUTED_PREFIX):
        requested, _, resolved = resolved_label[len(_SUBSTITUTED_PREFIX) :].partition(":")
        return face_detector_substituted_marker(requested, resolved)
    return face_detector_marker(resolved_label)


def _as_detection(item: object) -> Detection | None:
    """Coerce a ``Detection`` or a bare ``(x, y, w, h)`` tuple into a ``Detection``.

    Both shapes are live: the Haar path and every existing test pass 4-tuples, the MediaPipe
    path passes ``Detection``. Returning ``None`` for anything unusable keeps
    :func:`pick_main_face` non-raising for callers that hand it partial data.
    """
    if isinstance(item, Detection):
        return item
    # Anything carrying x/y/w/h attributes -- chiefly :class:`FaceBox`, which the
    # speaker-aware path deals in. FaceBox is *not* a 4-tuple: it carries a leading ``t``, so
    # tuple unpacking below silently rejects it. Without this branch every FaceBox is dropped
    # and the speaker path's coverage is always 0.0 -- reported as "this footage has no faces"
    # for footage the same run just tracked successfully.
    if all(hasattr(item, attr) for attr in ("x", "y", "w", "h")):
        try:
            return Detection(
                int(item.x),  # type: ignore[attr-defined]
                int(item.y),  # type: ignore[attr-defined]
                int(item.w),  # type: ignore[attr-defined]
                int(item.h),  # type: ignore[attr-defined]
                score=getattr(item, "score", None),
            )
        except (TypeError, ValueError):
            return None
    # `cast` rather than a bare `x, y, w, h = item`: unpacking an `object` requires a
    # `type: ignore[misc]`, and that ignore then leaves x/y/w/h with no inferred type at all, so
    # the `Detection(...)` call below reports `has-type` four times over. The cast is a no-op at
    # runtime, which is the point -- every iterable the tuple path accepted before still unpacks
    # here, including the numpy rows an injected detector may hand back, and the `except` below
    # remains the real shape check.
    try:
        x, y, w, h = cast("tuple[Any, Any, Any, Any]", item)
    except (TypeError, ValueError):
        return None
    try:
        return Detection(int(x), int(y), int(w), int(h))
    except (TypeError, ValueError):
        return None


def pick_main_face(faces: Sequence[object]) -> tuple[float, float] | None:
    """Return the centre ``(cx, cy)`` of the main face box, or ``None``.

    ``faces`` is a list of ``(x, y, w, h)`` rectangles **or** :class:`Detection` records; the
    tuple form is preserved because 48 existing tests and the Haar path pass it.

    Selection depends on whether confidences are present, and the two branches are kept
    genuinely separate rather than unified behind a synthesised score:

    * **No scores anywhere** -- largest area, which is the v0.11.0 behaviour *verbatim*,
      including that ``max`` keeps the first of equal-area boxes. This is what makes the
      byte-identical-default requirement achievable rather than merely likely.
    * **Any score present** -- ranked on ``score * sqrt(area)``, so a large low-confidence
      box loses to a smaller confident face. That is the point of the requirement: the crop
      should follow a face rather than a bookshelf, and a bookshelf is usually bigger.

      ``sqrt(area)`` -- the box's linear extent -- rather than ``area``, and the difference
      decides real cases. Weighting by area makes confidence almost irrelevant: a 400x400
      false positive at 0.10 has 62x the area of an 80x80 face at 0.95 and wins on
      ``score * area`` by more than two to one, which is exactly the outcome this requirement
      exists to prevent. Linear extent is also the better measure of what "dominant face"
      means for framing: a face twice as wide occupies twice the width of the crop, not four
      times. Since ``sqrt`` is monotonic in area, this changes no ordering when the scores
      are equal.

    A lone detection always wins regardless of its score -- with nothing to compare against,
    a confidence is not evidence for rejecting the only thing found, and dropping it would
    turn a low-confidence frame into a zero-detection frame, which is a different report.
    """
    items = [d for d in (_as_detection(f) for f in faces) if d is not None]
    if not items:
        return None
    # No special case for a single detection: ``max`` over one item returns it under either
    # key, so an explicit branch would be a second statement of the same behaviour -- and one
    # that no test could distinguish from its absence. The property is still asserted (a lone
    # detection wins whatever its score); it is simply a consequence of the code rather than a
    # separate clause that could drift away from it.
    if any(d.score is not None for d in items):
        # A missing score among scored peers ranks on area alone (neutral multiplier) rather
        # than as zero, which would silently discard it.
        best = max(
            items,
            key=lambda d: (1.0 if d.score is None else max(0.0, d.score)) * math.sqrt(d.area),
        )
    else:
        best = max(items, key=lambda d: d.area)
    return (best.x + best.w / 2.0, best.y + best.h / 2.0)


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
    zero_phase: bool | None = None,
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


def resample_centers(samples: list[Center], fps: float, duration: float) -> list[Center]:
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
    headroom_bias: float = 0.0,
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
        # V22: headroom applied *here*, downstream of `smooth_centers`, which is R1.5 rather than a
        # convenience. `smooth_centers` resets its EMA at every detected cut, so a bias folded into
        # the samples upstream would ramp in again after each shot boundary -- a slow vertical drift
        # per cut that reads as bad tracking rather than as a setting. Zero by default, so the
        # arithmetic below is byte-identical to v0.11.0 unless someone opts in.
        biased_cy = headroom.biased_center_y(
            c.cy, crop_h, src_h, bias=headroom_bias, origin_y=origin_y
        )
        x = origin_x + int(round(_clamp(c.cx - origin_x - crop_w / 2.0, 0, max_x)))
        y = origin_y + int(round(_clamp(biased_cy - origin_y - crop_h / 2.0, 0, max_y)))
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

    def center_at(self, t: float) -> tuple[float, float] | None:
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

    by_turn: dict[int, str | None] = field(default_factory=dict)
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
    by_turn: dict[int, str | None] = {}
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
    # `turn_track` rather than reusing `tid` from the loop above: that one is a definite `str`
    # taken from the candidate tuples, whereas these two are lookups that can miss. Sharing the
    # name made the optional case invisible to a reader and to the type checker alike.
    for i, t in enumerate(turns):
        turn_track = label_track.get(t.speaker_label)
        if turn_track is not None and track_by_id[turn_track].presence(t.start, t.end) > 0.0:
            by_turn[i] = turn_track
        else:
            by_turn[i] = None
            unassociated.append(i)

    # Rank shown tracks by total speaking duration of their associated turns.
    duration_by_track: dict[str, float] = {}
    for i, t in enumerate(turns):
        turn_track = by_turn[i]
        if turn_track is not None:
            duration_by_track[turn_track] = duration_by_track.get(turn_track, 0.0) + max(
                0.0, t.end - t.start
            )
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
            best_tr: dict | None = None
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
            best_d: float | None = None
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
    cv2, *, detect_width: int | None = None
) -> Callable[[object], list[tuple[int, int, int, int]]] | None:
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
        # Spelled out as fixed 4-tuples rather than `tuple(int(v) for v in f)`, which produces
        # `tuple[int, ...]` — a type that does not match the declared return and would let a
        # detector returning 3- or 5-element rects through unnoticed. Haar rects are always
        # (x, y, w, h), so indexing is also the shape check. Kept through the rescale: the
        # generator form this commit originally used is the one that comment rules out.
        if scale == 1.0:
            return [(int(f[0]), int(f[1]), int(f[2]), int(f[3])) for f in faces]
        inverse = 1.0 / scale
        return [
            (
                int(round(f[0] * inverse)),
                int(round(f[1] * inverse)),
                int(round(f[2] * inverse)),
                int(round(f[3] * inverse)),
            )
            for f in faces
        ]

    return _detect


#: The Face_Detector_Backend values this build understands.
FACE_DETECTOR_BACKENDS: tuple[str, ...] = ("haar", "mediapipe")

#: ``haar`` is the default because every new setting must default to previously shipped
#: behaviour. That is not caution for its own sake: the golden and parity renders only detect
#: an *accidental* change while they are not re-frozen each release, and switching the default
#: detector would change the crop path -- and therefore the pixels -- in every one of them.
DEFAULT_FACE_DETECTOR_BACKEND = "haar"


def _mediapipe_detector(
    min_score: float, model_path: Path
) -> tuple[Callable[[object], list[Detection]], Callable[[], None]] | None:
    """Build the BlazeFace detector, returning ``(detect, close)`` or ``None``.

    ``mediapipe`` is imported **here** rather than at module scope, matching every other heavy
    dependency in this package: this module must stay importable on a host with no vision
    stack, because the capability probe and the options round-trip tests import it.

    Uses ``mediapipe.tasks.python.vision.FaceDetector``. The legacy
    ``mediapipe.solutions.face_detection`` namespace was **removed** in 0.10.x and must not be
    reintroduced -- on the installed 0.10.35, ``dir(mediapipe)`` is exactly
    ``['Image', 'ImageFormat', 'tasks']``. There is consequently no ``model_selection``
    argument: near versus far range is decided by *which vendored model file is loaded*, not by
    a constructor flag. ``tests/test_face_detection_real_binary.py`` pins the API surface so a
    resolver upgrade that moves it fails loudly here rather than silently at render time.

    Returns a ``close`` alongside the detector because MediaPipe holds a native graph that must
    be released; the sampler calls it in a ``finally``.

    Never raises and never fetches: a missing model or a construction failure returns ``None``
    and the caller degrades to Haar with a substitution marker.
    """
    try:
        # Lazy on purpose, see the docstring: this module must import on a host with no
        # vision stack.
        import mediapipe as mp
        import numpy as np
        from mediapipe.tasks.python import BaseOptions, vision
    except Exception:
        return None

    if not model_path or not Path(model_path).is_file():
        return None

    try:
        options = vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            min_detection_confidence=float(min_score),
        )
        detector = vision.FaceDetector.create_from_options(options)
    except Exception:
        return None

    def _detect(frame) -> list[Detection]:
        """Detect faces in one BGR frame, returning absolute-pixel boxes.

        The frame arrives from OpenCV as **BGR**; MediaPipe is told the buffer is ``SRGB``, so
        the channels are reversed first. Passing BGR through unswapped is not a crash: the
        model sees a blue-skinned face, detects fewer of them, and the only symptom is a
        coverage figure lower than it should be -- which would then read as "this footage is
        hard" rather than "the channels are backwards". ``ascontiguousarray`` because the
        reversed view is a stride trick and MediaPipe needs a real contiguous buffer.
        """
        rgb = np.ascontiguousarray(frame[:, :, ::-1])
        height, width = rgb.shape[0], rgb.shape[1]
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = detector.detect(image)

        out: list[Detection] = []
        for detection in getattr(result, "detections", None) or []:
            box = getattr(detection, "bounding_box", None)
            if box is None:
                continue
            # Measured on 0.10.35: these are ABSOLUTE PIXELS as int (see the spec's design
            # doc). Routed through the conversion anyway, which then clamps and validates
            # rather than scaling -- and would still be correct if the library moved back to a
            # normalised box, which it has already changed once.
            converted = relative_box_to_pixels(
                box.origin_x,
                box.origin_y,
                box.width,
                box.height,
                width=width,
                height=height,
            )
            if converted is None:
                continue
            categories = getattr(detection, "categories", None) or []
            score = None
            if categories:
                raw_score = getattr(categories[0], "score", None)
                if raw_score is not None:
                    score = float(raw_score)
            # No absolute minimum-size floor (Requirement 2.7): a distant face is small and is
            # exactly what this backend was adopted to find.
            if score is not None and score < float(min_score):
                continue
            out.append(Detection(*converted, score=score))
        return out

    def _close() -> None:
        try:
            detector.close()
        except Exception:
            # Releasing a native graph must not be able to fail a render that already
            # succeeded; the process exiting reclaims it regardless.
            pass

    return _detect, _close


def resolve_detector(
    backend: str,
    *,
    injected: Callable | None = None,
    cv2_module=None,
    min_score: float | None = None,
    model_dir: Path | None = None,
) -> tuple[Callable | None, str]:
    """Return ``(detector, resolved_label)`` -- the label names what **ran**.

    The return type is the design decision worth defending: handing back a bare callable would
    force the caller to infer which backend produced the detections, and inference is how
    ``font_substituted:Arial`` got frozen into a golden file as correct. The label is returned
    *by the branch that actually succeeded*.

    Never raises. A detector that cannot be built returns ``(None, label)`` and the caller
    degrades along the existing geometry ladder to a static reformat.

    The ladder, in order:

    1. an injected detector resolves to ``"injected"`` -- not to a backend name, because a test
       double is not evidence that a backend works;
    2. ``mediapipe`` requested and constructible resolves to ``"mediapipe"``;
    3. ``mediapipe`` requested but unimportable, unconstructible, or with its vendored model
       absent or the wrong size resolves to ``"substituted:mediapipe:haar"`` -- all four causes
       share one label because the operator's remedy is identical in every case, while the log
       line names the specific cause;
    4. anything else, including an unrecognised value, resolves to ``"haar"``.
    """
    if injected is not None:
        return injected, "injected"

    requested = (backend or "").strip().lower()
    if requested not in FACE_DETECTOR_BACKENDS:
        requested = DEFAULT_FACE_DETECTOR_BACKEND

    if cv2_module is None:
        try:
            import cv2 as cv2_module
        except Exception:
            cv2_module = None

    def _haar() -> Callable | None:
        if cv2_module is None:
            return None
        try:
            return _default_haar_detector(cv2_module)
        except Exception:
            return None

    if requested == "mediapipe":
        from worker import face_models

        model_path = face_models.resolve_model("mediapipe", model_dir)
        built = None
        if model_path is None:
            logger.warning(
                "face detector: mediapipe requested but its vendored model is absent or the "
                "wrong size under %s; falling back to haar. Run "
                "`python scripts/fetch_models.py --check`.",
                model_dir if model_dir is not None else face_models.models_dir(),
            )
        else:
            score = settings.face_detector_min_score if min_score is None else min_score
            built = _mediapipe_detector(float(score), model_path)
            if built is None:
                logger.warning(
                    "face detector: mediapipe requested but could not be imported or "
                    "constructed; falling back to haar",
                )
        if built is not None:
            detect, close = built
            # Carried as an attribute rather than a third return value so the documented
            # two-tuple signature holds for every backend; the sampler releases it in a
            # `finally`. Haar has nothing to release, so the attribute is simply absent.
            detect.close = close  # type: ignore[attr-defined]
            return detect, "mediapipe"
        haar = _haar()
        return haar, "substituted:mediapipe:haar"

    return _haar(), "haar"


@dataclass(frozen=True)
class Sample_Report:
    """The samples, plus what was learned while producing them.

    ``coverage`` is computed **here**, from the very sample set the crop path is derived from.
    A second sampling pass could disagree with the first -- different frames, a different
    detector state -- and the disagreement would be invisible: the reported confidence would
    describe one set of frames while the framing was built from another.
    """

    samples: list[tuple[float, list[Detection]]]
    resolved_backend: str
    effective_fps: float
    requested_fps: float

    @property
    def coverage(self) -> float:
        """Fraction of sampled frames containing at least one detection."""
        return detection_coverage(self.samples)

    @property
    def capped(self) -> bool:
        """Whether the sample cap actually reduced the rate below what was asked for.

        Compared with a small tolerance rather than ``<``: the effective rate is a division of
        two measured quantities, so an uncapped clip lands a hair either side of the requested
        value and a bare comparison would report the cap as binding on roughly half of them.
        """
        return self.effective_fps < (self.requested_fps - 0.05)

    def as_tuples(self) -> list[tuple[float, list[tuple[int, int, int, int]]]]:
        """The samples in the ``(t, [(x, y, w, h), ...])`` form pre-existing callers expect."""
        return [(t, [d.as_tuple() for d in boxes]) for t, boxes in self.samples]


def sample_face_report(
    video: str | Path,
    *,
    sample_fps: float,
    max_samples: int | None = None,
    detector: Callable | None = None,
    backend: str | None = None,
    min_score: float | None = None,
    model_dir: Path | None = None,
) -> Sample_Report:
    """Sample frames across ``video``, detect faces, and report what happened.

    The additive sibling of :func:`_sample_face_boxes`, which is now a thin wrapper over this.
    Split this way round -- report as the implementation, tuple list as the wrapper -- because
    the wrapper's signature and return type are load-bearing: ``FRAME_SAMPLER`` in
    ``worker/pipeline.py`` is patched *by name*, and the existing reframe tests call it
    directly. An additive sibling changes neither.

    Never raises. A missing ``cv2``, an unopenable video, or no constructible detector yields
    an empty sample list, which the caller already treats as "degrade to the static reformat".
    """
    requested = float(sample_fps)
    empty = Sample_Report(
        samples=[], resolved_backend="", effective_fps=0.0, requested_fps=requested
    )

    try:
        import cv2
    except Exception:
        return replace(empty, resolved_backend="none")

    resolved_detector, label = resolve_detector(
        backend if backend is not None else DEFAULT_FACE_DETECTOR_BACKEND,
        injected=detector,
        cv2_module=cv2,
        min_score=min_score,
        model_dir=model_dir,
    )
    if resolved_detector is None:
        return replace(empty, resolved_backend=label)

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        # Still report which backend was resolved: "no samples because the video would not
        # open" and "no samples because no detector could be built" are different faults and
        # the marker is the only place a caller can tell them apart.
        _release_detector(resolved_detector)
        return replace(empty, resolved_backend=label)

    out: list[tuple[float, list[Detection]]] = []
    frames_read = 0
    fps = 30.0
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

        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frames_read = idx + 1
            if idx % step == 0:
                out.append((idx / fps, _detect_frame(resolved_detector, idx)(frame)))
                if max_samples and max_samples > 0 and len(out) >= max_samples:
                    break
            idx += 1
    finally:
        cap.release()
        # Requirement 2.9: MediaPipe holds a native graph. Released here rather than by the
        # caller, and in a `finally` so it happens even when sampling raises.
        _release_detector(resolved_detector)

    # Effective rate from the sample count and the span actually scanned, not from the
    # requested rate -- the point of the marker is to say what really happened.
    scanned_seconds = frames_read / fps if fps > 0 else 0.0
    effective = (len(out) / scanned_seconds) if scanned_seconds > 0 else 0.0
    return Sample_Report(
        samples=out,
        resolved_backend=label,
        effective_fps=effective,
        requested_fps=requested,
    )


def _release_detector(detector: Callable | None) -> None:
    """Call a backend's ``close`` if it has one. Haar has nothing to release."""
    close = getattr(detector, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        logger.debug("face detector close() failed", exc_info=True)


def _detect_frame(detector: Callable, idx: int) -> Callable[[object], list[Detection]]:
    """Wrap one detector call so a frame that raises becomes a zero-detection frame.

    Requirement 4.3, and deliberately **not** a rung on the degradation ladder: one bad frame
    is not a broken backend, and aborting would discard every frame that worked. The zero also
    lowers reported coverage, which is the honest signal -- a backend that throws on a third of
    the frames really did find faces in fewer of them.
    """

    def _run(frame) -> list[Detection]:
        try:
            raw = detector(frame)
        except Exception:
            logger.debug("face detector raised on sampled frame %d", idx, exc_info=True)
            return []
        out: list[Detection] = []
        for box in raw or []:
            coerced = _as_detection(box)
            if coerced is not None:
                out.append(coerced)
        return out

    return _run


def _sample_face_boxes(
    video: str | Path,
    *,
    sample_fps: float,
    max_samples: int | None = None,
    detector: Callable | None = None,
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

    A thin wrapper over :func:`sample_face_report` since the detection-confidence work. The
    signature and return type are unchanged on purpose: ``FRAME_SAMPLER`` in
    ``worker/pipeline.py`` is patched by name and the existing reframe tests call this
    directly, so the report is an additive sibling rather than a signature change.
    """
    return sample_face_report(
        video, sample_fps=sample_fps, max_samples=max_samples, detector=detector
    ).as_tuples()


def detect_faces_report(
    video: str | Path,
    *,
    sample_fps: float | None = None,
    max_samples: int | None = None,
    detector: Callable | None = None,
    backend: str | None = None,
) -> tuple[list[list[FaceBox]], Sample_Report]:
    """All face boxes per sampled frame, **and** what was learned finding them.

    The additive sibling of :func:`detect_faces`, in the same direction as the other two pairs
    in this module: the reporting form is the implementation and the original is a wrapper, so
    the original's signature cannot drift out from under its callers.
    """
    if sample_fps is None:
        sample_fps = settings.reframe_sample_fps
    if max_samples is None:
        max_samples = settings.reframe_sample_cap

    report = sample_face_report(
        video,
        sample_fps=sample_fps,
        max_samples=max_samples,
        detector=detector,
        backend=backend,
    )
    result: list[list[FaceBox]] = []
    for t, boxes in report.samples:
        result.append([FaceBox(round(float(t), 3), d.x, d.y, d.w, d.h) for d in boxes])
    return result, report


def synthetic_report(
    per_frame: Sequence[Sequence[object]], label: str, requested_fps: float
) -> Sample_Report:
    """A :class:`Sample_Report` describing samples that came from an injected sampler.

    An injected sampler bypasses detection entirely, so there is no backend to name and no
    measured rate. Rather than reporting nothing -- which would leave the speaker-aware path
    silent where the single-speaker path speaks, and the requirement is that both report
    identically -- this records ``injected`` and computes coverage from the boxes the sampler
    actually produced. ``effective_fps`` is set to the requested rate so the cap never reads as
    having bound: nothing was sampled, so nothing was capped.
    """
    samples = [
        (float(index), [d for d in (_as_detection(b) for b in boxes) if d is not None])
        for index, boxes in enumerate(per_frame)
    ]
    return Sample_Report(
        samples=samples,
        resolved_backend=label,
        effective_fps=float(requested_fps),
        requested_fps=float(requested_fps),
    )


def detect_faces(
    video: str | Path,
    *,
    sample_fps: float | None = None,
    max_samples: int | None = None,
    detector: Callable | None = None,
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
    return detect_faces_report(
        video, sample_fps=sample_fps, max_samples=max_samples, detector=detector
    )[0]


def detector_notes(report: Sample_Report) -> list[str]:
    """The Effects_Applied markers a :class:`Sample_Report` earns, in a fixed order.

    One function so both geometry paths report identically -- the requirement is that
    single-speaker reframe and speaker-aware reframe say the same things, and the way to
    guarantee that is for there to be one implementation rather than two that agree today.

    Three rules are encoded here rather than at the call sites:

    * the resolved-backend marker is always present, because a caller cannot otherwise tell a
      requested backend from the one that ran;
    * the sampling-rate marker appears **only when the cap actually bound**, since its purpose
      is to explain a *reduced* rate and emitting it always would make it noise;
    * the low-confidence marker appears only when coverage is below the floor **and at least
      one detection was found**. Zero coverage is already reported by the existing no-faces
      degradation, and emitting ``reframe_low_confidence:0.00`` beside it would be a second
      name for one condition -- the duplicated-fact pattern mutation testing has caught twice
      in this repository.
    """
    notes = [detector_marker_for(report.resolved_backend)]
    if report.capped:
        notes.append(sample_rate_marker(report.effective_fps))
    coverage = report.coverage
    if coverage > 0.0 and coverage < float(settings.reframe_coverage_floor):
        notes.append(low_confidence_marker(coverage))
    return notes


def track_faces_report(
    video: str | Path,
    sample_fps: float = 5.0,
    *,
    backend: str | None = None,
    detector: Callable | None = None,
    max_samples: int | None = None,
) -> tuple[list[Center], Sample_Report]:
    """The main-face centre path **and** what was learned finding it.

    The additive sibling of :func:`track_faces`, in the same direction as
    :func:`sample_face_report` is of :func:`_sample_face_boxes`: the reporting version is the
    implementation and the original is a thin wrapper, so the original's signature -- which
    tests and the pipeline both depend on -- cannot drift.
    """
    report = sample_face_report(
        video,
        sample_fps=sample_fps,
        max_samples=max_samples,
        detector=detector,
        backend=backend,
    )
    samples: list[Center] = []
    last_center: tuple[float, float] | None = None
    for t, boxes in report.samples:
        center = pick_main_face(boxes)
        if center is None:
            center = last_center
        if center is not None:
            samples.append(Center(round(t, 3), center[0], center[1]))
            last_center = center
    return samples, report


def track_faces(video: str | Path, sample_fps: float = 5.0) -> list[Center]:
    """Sample frames and return the main-face centre path (``Center`` samples).

    Uses OpenCV's Haar cascade (via the shared :func:`_sample_face_boxes`
    machinery). Returns ``[]`` when cv2 is unavailable, the video cannot be
    opened, or no faces are found anywhere (caller falls back). This is the
    unchanged v0.7.0 single-speaker behaviour: it keeps only the dominant face
    per sampled frame and holds the last known centre through frames with no
    detection.

    A thin wrapper over :func:`track_faces_report` since the detection-confidence work; the
    signature and return type are unchanged because existing callers and tests depend on them.
    """
    return track_faces_report(video, sample_fps=sample_fps)[0]


#: Shared with every other filter-string builder; see :func:`ffmpeg_utils.escape_filter_path`.
_escape_filter_path = escape_filter_path


def _intersect_margin(
    rect: tuple[int, int, int, int], margin_x: int, margin_y: int
) -> tuple[int, int, int, int]:
    """Inset a ``(width, height, x, y)`` content rectangle by a stabilisation margin (V21/R10.5).

    The two facts compose rather than compete: `V16` letterbox detection says which pixels are
    *content*, and `V21` says which pixels `vidstab` may have vacated. Intersecting them is what
    stops the crop consuming the same margin twice -- the defect R10.5 names, where a crop reaching
    into the vacated band delivers black edges no later stage can detect.

    Dimensions stay even: libx264's 4:2:0 subsampling requires it and an odd crop fails the encode.
    """
    width, height, x, y = rect
    inset_x = max(0, int(margin_x))
    inset_y = max(0, int(margin_y))
    new_w = max(2, width - 2 * inset_x)
    new_h = max(2, height - 2 * inset_y)
    return (new_w - (new_w % 2), new_h - (new_h % 2), x + inset_x, y + inset_y)


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
    sample_fps: float = 5.0,
    command_fps: float | None = None,
    smoothing: float = 0.35,
    *,
    backend: str | None = None,
    detector: Callable | None = None,
    notes: list[str] | None = None,
    colour_tags: Sequence[str] = (),
    stabilise_margin: tuple[int, int] = (0, 0),
) -> Path:
    """Reframe ``video`` to ``aspect`` following the main face; write ``dest``.

    Raises :class:`ReframeUnavailable` when no usable face path is found or the
    aspect is not narrower than the source (nothing to track) so the caller can
    fall back to the static reformat.

    ``backend`` selects the Face_Detector_Backend (``None`` means the configured default, which
    is ``haar``); ``detector`` injects one outright, for tests.

    ``notes`` is an **out-parameter**: when a list is passed, the detector and confidence
    markers are appended to it. An out-parameter rather than a changed return type because the
    return is ``Path`` and every caller and test relies on that; and appended only **after the
    render has succeeded**, so a clip that falls back to the static reformat does not carry a
    marker claiming a detector framed it.
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
    # V21/R10.5: hand the stabilisation margin to the crop geometry, so reframing and vidstab do not
    # each consume the same pixels. (0, 0) when stabilisation is off, leaving this untouched.
    if stabilise_margin != (0, 0):
        content = _intersect_margin(content, *stabilise_margin)
    src_w, src_h, origin_x, origin_y = content

    crop_w, crop_h = compute_crop_size(src_w, src_h, aw, ah)
    if crop_w >= src_w and crop_h >= src_h:
        # Target isn't a tighter crop than the source; nothing to follow.
        raise ReframeUnavailable("target aspect is not narrower than source")

    samples, sample_report = track_faces_report(
        video, sample_fps=sample_fps, backend=backend, detector=detector
    )
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
    # V22/R1.6: only ever biased when a face was actually detected. Reaching here means `samples`
    # came from detections -- `apply_reframe` raises `ReframeUnavailable` otherwise -- so the bias is
    # read from settings here rather than being threaded from a caller that cannot know.
    headroom_bias_value = headroom.clamp_bias(getattr(settings, "reframe_headroom_bias", 0.0))
    script = build_sendcmd(
        dense,
        crop_w,
        crop_h,
        src_w,
        src_h,
        origin_x=origin_x,
        origin_y=origin_y,
        headroom_bias=headroom_bias_value,
    )
    if notes is not None:
        _marker = headroom.marker(headroom_bias_value)
        if _marker and _marker not in notes:
            notes.append(_marker)

    cmd_file = dest.with_suffix(".reframe.cmd")
    cmd_file.parent.mkdir(parents=True, exist_ok=True)
    cmd_file.write_text(script, encoding="utf-8")

    # Initial crop position (first command); sendcmd updates x/y over time.
    first = dense[0]
    # The same bias as the sendcmd script above. If these two disagreed, frame 0 would be framed
    # differently from frame 1 and the clip would open with a visible jump -- so the biased centre is
    # computed by the same function rather than by a second copy of the arithmetic.
    first_cy = headroom.biased_center_y(
        first.cy, crop_h, src_h, bias=headroom_bias_value, origin_y=origin_y
    )
    x0 = origin_x + int(round(_clamp(first.cx - origin_x - crop_w / 2.0, 0, src_w - crop_w)))
    y0 = origin_y + int(round(_clamp(first_cy - origin_y - crop_h / 2.0, 0, src_h - crop_h)))

    escaped = str(cmd_file.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    vf = f"sendcmd=f='{escaped}',crop={crop_w}:{crop_h}:{x0}:{y0},scale={tw}:{th},setsar=1"
    cmd = [
        settings.ffmpeg_binary,
        "-y",
        "-i",
        str(video),
        "-vf",
        vf,
        *h264_args(colour_tags=colour_tags),
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    try:
        _run(cmd)
    except FFmpegError as exc:
        raise ReframeUnavailable(f"ffmpeg reframe failed: {exc}") from exc
    finally:
        cmd_file.unlink(missing_ok=True)
    # Only now: the render succeeded, so the markers describe a clip that exists. Appending
    # them earlier would leave a `face_detector:*` marker on a clip that went on to fail and
    # fall back to the static reformat, which is the marker naming a backend that framed
    # nothing.
    if notes is not None:
        notes.extend(detector_notes(sample_report))
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
    "subtle": (0.15, 0.60),  # strongest smoothing, slowest, longest xfade
    "standard": (0.35, 0.35),
    "heavy": (0.60, 0.18),  # weakest smoothing, fastest, shortest xfade
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


def _turn_index_at(turns: list[Speaker_Turn], t: float) -> int | None:
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
    command_fps: float | None = None,
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
    last_valid: tuple[float, float] | None = None
    for t in times:
        idx = _turn_index_at(turns, t) if turns else None
        center: tuple[float, float] | None = None
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
    #
    # Deliberately still the CAUSAL filter, unlike the single-speaker path, which was moved to
    # zero-phase because its lag was measurable and visible. Two reasons this path is
    # different, and they were established by trying it rather than assumed:
    #
    #   * The motion here is not a subject being followed, it is a set of *deliberately
    #     constructed* transition ramps (above), timed to start when a speaker starts. A
    #     symmetric filter looks ahead, so it begins the move ~1/alpha grid steps early
    #     - about 125 ms at alpha 0.35 - and the crop drifts toward the next speaker before
    #     they have said anything. `test_p15_speaker_change_transitions_smoothly` pins that
    #     the path starts on the previous speaker's position and caught this immediately.
    #   * There is no ground-truth harness for this path, so "better" is not measurable here
    #     the way it is for a single tracked face. Changing intentional timing on the
    #     strength of an argument that was only verified elsewhere is how a plausible
    #     regression gets shipped.
    #
    # Wiring it up belongs with a follow-active benchmark, not with this change.
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
    #: V5: the tile's crop centre over time, or ``()`` for a fixed crop.
    #:
    #: Defaulted and last so every existing positional construction is unaffected. When empty
    #: the tile renders exactly as it did before V5: one static crop at ``src_cx``/``src_cy``.
    centers: tuple[Center, ...] = ()


def _track_mean_center(track: Face_Track | None) -> tuple[float, float] | None:
    """Average centre of a track's boxes (or ``None`` when it has no boxes)."""
    if track is None or not track.boxes:
        return None
    xs = [b.center[0] for b in track.boxes]
    ys = [b.center[1] for b in track.boxes]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def build_region_centers(
    track: Face_Track | None,
    *,
    src_w: int,
    src_h: int,
    dst_w: int,
    dst_h: int,
    duration: float,
    command_fps: float | None = None,
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
    last: tuple[float, float] | None = None
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
    track: Face_Track | None, src_w: int, src_h: int, dst_w: int, dst_h: int
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
        in_row = shown[row * 2 : row * 2 + 2]
        x = 0
        for col, tid in enumerate(in_row):
            w = (
                target_w
                if len(in_row) == 1
                else (target_w // 2 if col == 0 else target_w - target_w // 2)
            )
            src_cx, src_cy = _region_source_center(track_by_id.get(tid), src_w, src_h, w, h)
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
    max_regions: int | None = None,
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
            src_w=src_w,
            src_h=src_h,
            dst_w=region.dst_w,
            dst_h=region.dst_h,
            duration=duration,
            intensity=intensity,
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
            src_cx, src_cy = _region_source_center(track_by_id.get(tid), src_w, src_h, target_w, h)
            regions.append(with_motion(Region(0, y, target_w, h, src_cx, src_cy, tid)))
            y += h
    else:
        # Landscape target: place tiles side-by-side, full height.
        base_w = target_w // n
        x = 0
        for k, tid in enumerate(shown):
            w = base_w if k < n - 1 else target_w - base_w * (n - 1)
            src_cx, src_cy = _region_source_center(track_by_id.get(tid), src_w, src_h, w, target_h)
            regions.append(with_motion(Region(x, 0, w, target_h, src_cx, src_cy, tid)))
            x += w

    return regions


# --------------------------------------------------------------------------- #
# ffmpeg geometry builder (pure) + single-pass orchestration
# --------------------------------------------------------------------------- #
def build_reframe_filter(
    layout: str,
    *,
    centers: list[Center] | None = None,
    regions: list[Region] | None = None,
    crop_w: int,
    crop_h: int,
    src_w: int,
    src_h: int,
    target_w: int,
    target_h: int,
    sendcmd_path: str | None = None,
    intensity: str = "standard",
    tile_sendcmd_paths: Sequence[str] | None = None,
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
                    list(rg.centers),
                    rcw,
                    rch,
                    src_w,
                    src_h,
                    origin_x=origin_x,
                    origin_y=origin_y,
                    target=crop_name,
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
                f"[0:v]{prefix}{crop_name}={rcw}:{rch}:{x}:{y},scale={rg.dst_w}:{rg.dst_h}[{label}]"
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
            graph += f";{''.join(labels)}{stack}=inputs={len(labels)},setsar=1[vout]"
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
            graph += f";{''.join(row_labels)}vstack=inputs={len(row_labels)},setsar=1[vout]"
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
    vf = f"sendcmd=f='{escaped}',crop={crop_w}:{crop_h}:{x0}:{y0},scale={tw}:{th},setsar=1"
    return ([], vf, ["speaker_reframe:follow_active"])


def apply_speaker_reframe(
    video: str | Path,
    dest: str | Path,
    *,
    turns: list[Speaker_Turn],
    aspect: str = "9:16",
    layout: str = "follow_active",
    intensity: str = "standard",
    detector: Callable | None = None,
    sampler: Callable | None = None,
    backend: str | None = None,
    notes: list[str] | None = None,
    colour_tags: Sequence[str] = (),
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
        # An injected sampler bypassed detection, so there is no backend to name and no
        # measured rate; the report records `injected` and computes coverage from what the
        # sampler produced, so this path reports in the same vocabulary as the other.
        sample_report = synthetic_report(per_frame, "injected", float(settings.reframe_sample_fps))
    else:
        per_frame, sample_report = detect_faces_report(video, detector=detector, backend=backend)

    tracks = build_face_tracks(per_frame)
    if not tracks:
        raise ReframeUnavailable("no face tracks detected")

    assoc = associate_faces(turns, tracks)

    # Normalise the requested layout (unknown -> follow_active).
    if layout not in ("follow_active", "split_screen"):
        layout = "follow_active"

    regions: list[Region] | None = None
    if layout == "split_screen":
        regions = build_split_screen_layout(
            turns,
            assoc,
            tracks,
            target_w=tw,
            target_h=th,
            src_w=info.width,
            src_h=info.height,
            # V5: give each tile a centre path over the clip instead of one fixed crop.
            duration=info.duration,
            intensity=intensity,
        )
        if not regions:
            # Fewer than two associated tracks -> follow_active substitution.
            layout = "follow_active"

    if layout == "split_screen":
        tile_files = [dest.with_suffix(f".tile{k}.cmd") for k in range(len(regions or []))]
        _ia, graph, _notes = build_reframe_filter(
            "split_screen",
            regions=regions,
            crop_w=crop_w,
            crop_h=crop_h,
            src_w=info.width,
            src_h=info.height,
            target_w=tw,
            target_h=th,
            intensity=intensity,
            tile_sendcmd_paths=[str(p) for p in tile_files],
        )
        cmd = [
            settings.ffmpeg_binary,
            "-y",
            "-i",
            str(video),
            "-filter_complex",
            graph,
            "-map",
            "[vout]",
            "-map",
            "0:a?",
            *h264_args(colour_tags=colour_tags),
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
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
        if notes is not None:
            notes.extend(detector_notes(sample_report))
        return dest

    # follow_active
    path = build_follow_active_path(
        turns,
        assoc,
        tracks,
        src_w=info.width,
        src_h=info.height,
        crop_w=crop_w,
        crop_h=crop_h,
        intensity=intensity,
        duration=info.duration,
    )
    if not path:
        raise ReframeUnavailable("no usable crop path")

    cmd_file = dest.with_suffix(".reframe.cmd")
    _ia, vf, _notes = build_reframe_filter(
        "follow_active",
        centers=path,
        crop_w=crop_w,
        crop_h=crop_h,
        src_w=info.width,
        src_h=info.height,
        target_w=tw,
        target_h=th,
        sendcmd_path=str(cmd_file),
        intensity=intensity,
    )
    cmd = [
        settings.ffmpeg_binary,
        "-y",
        "-i",
        str(video),
        "-vf",
        vf,
        *h264_args(colour_tags=colour_tags),
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    try:
        _run(cmd)
    except FFmpegError as exc:
        raise ReframeUnavailable(f"ffmpeg reframe failed: {exc}") from exc
    finally:
        cmd_file.unlink(missing_ok=True)
    if notes is not None:
        notes.extend(detector_notes(sample_report))
    return dest
