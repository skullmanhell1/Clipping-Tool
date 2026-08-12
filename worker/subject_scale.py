"""Subject-scale normalisation across shots (V23).

A clip cut together from a close-up and a wide shot delivers a speaker who changes size at every
cut. `reframe.py` follows the face's *position* and says nothing about its *size*, so the jump
survives reframing intact.

WHAT THIS DOES NOT DO, AND WHY THE SPEC'S OWN MECHANISM IS UNAVAILABLE
---------------------------------------------------------------------
R2.2 asks for the **crop size** to be adjusted per shot. That is not expressible in this pipeline,
and the reason is measured rather than assumed. `crop`'s `w` and `h` are marked commandable (`T` in
`ffmpeg -h filter=crop`), so the obvious implementation is a `sendcmd` script that changes them at
each cut, exactly as the existing script changes `x` and `y`. On the ffmpeg this project ships
(7.0.2-static) that **crashes the CLI**::

    Assertion best_input >= 0 failed at src/fftools/ffmpeg_filter.c:1923

Verified three ways on a synthetic source: `crop x`/`crop y` commands alone render fine, a `crop w`/
`crop h` command fails, and adding `scale=...:eval=frame` does not rescue it. Changing a crop's
output dimensions reconfigures the filter link, and the command-line tool cannot follow that
mid-stream. So per-shot crop *size* is off the table until that changes.

The mechanism used instead is a **magnification step**: `zoompan` with a `z` expression that is
constant within a shot and changes only at a cut. `zoompan` evaluates `z` per output frame and
never reconfigures the link, which is precisely why the existing `zoom_cut` style can already be a
step (`overlays.zoom_filter`). Measured on the same synthetic source, a step from 1.0 to 1.3
moves mean luma 23.49 -> 28.69 while a flat `z=1.0` control stays at 23.49 -> 23.50, so the
magnification is real and the measurement is not an artefact of the graph.

**This is the same mechanism as the zoom feature, which is why R2.10 exists.** Two magnifications
composed on one shot multiply, and neither design intended the combined curve. The caller is
therefore expected to decline when zoom or ken-burns is active, and to say so.

WE CAN ONLY EVER MAGNIFY
------------------------
`compute_crop_size` already returns the *largest* window of the target aspect that fits the source,
and for 9:16 out of 16:9 it is full height. There is no room to widen, and R2.6 forbids reaching
outside the source. So a shot whose subject is already larger than the target cannot be pulled
back - it is left alone and recorded, rather than being approximated by something that would crop
into the frame further.

That fixes the target: the **median** shot scale, not the maximum. Normalising to the maximum would
magnify almost every shot to match the tightest one, softening most of the clip to fix a minority of
it. With the median, at most half the shots move, and the ones that move are the wide ones - which
is also the direction where magnification costs least, because a wide shot has the subject occupying
few pixels and gains the most from being brought closer.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: Largest magnification a single shot may receive.
#:
#: R2.3's bound. 1.35 is the point where a 1080-wide delivery is being built from roughly 800 source
#: pixels; past that the softening is more visible than the size mismatch it corrects. It also caps
#: what one mis-detected shot can do: a spurious tiny face box would otherwise ask for an enormous
#: magnification, and the clip would open on a blown-up fragment.
MAX_MAGNIFICATION = 1.35

#: Scale differences below this are left alone, as a fraction.
#:
#: Two shots within 8% of each other do not read as a size change, and magnifying by 1.05 spends
#: resolution to fix something no viewer would notice. It also keeps the feature quiet on footage
#: that never had the problem, which is what makes the marker meaningful when it does appear.
NEGLIGIBLE_DIFFERENCE = 0.08

#: Minimum detections in a shot before its scale is trusted.
#:
#: One detection could be a false positive on a background object, and a single frame decides the
#: magnification for a whole shot. Two is not robustness, but it is the difference between "measured"
#: and "guessed", and R2.7 already requires leaving an unmeasurable shot alone.
MIN_SAMPLES_PER_SHOT = 2


@dataclass(frozen=True)
class Shot_Scale:
    """One shot's measured subject scale.

    ``scale`` is the face height as a fraction of the crop height, which is the quantity a viewer
    actually perceives: the same face box is a close-up in a tight crop and a mid-shot in a loose
    one, so an absolute pixel height would compare the wrong thing across differing crops.

    ``None`` means unmeasurable - no detection, or too few to trust - and is deliberately distinct
    from a measured small value.
    """

    start: float
    end: float
    scale: float | None
    samples: int

    @property
    def measured(self) -> bool:
        return self.scale is not None and self.scale > 0.0


@dataclass(frozen=True)
class Scale_Plan:
    """The per-shot magnifications to apply, and what to record about them."""

    expression: str = ""
    magnifications: tuple[float, ...] = ()
    shots: tuple[Shot_Scale, ...] = ()
    marker: str = ""
    detail: str = ""

    @property
    def enabled(self) -> bool:
        """Whether anything should actually be inserted into the filter chain."""
        return bool(self.expression)

    @property
    def altered(self) -> int:
        """How many shots this plan magnifies (R2.9 records *that a size was altered*)."""
        return sum(1 for m in self.magnifications if m > 1.0)


def shot_bounds(sample_times: Sequence[float], cut_indices: Sequence[int]) -> list[tuple[int, int]]:
    """``[(start_index, end_index), ...]`` half-open shot ranges over the sample series.

    Built from the indices `reframe.cut_indices` already derives for V4's EMA reset, so V23 and the
    smoother agree on where the shots are by construction (R2.5). A second shot-boundary mechanism
    would drift from the first, and the two disagreeing would put a size step one shot away from the
    cut it belongs to - visibly worse than no normalisation.
    """
    count = len(sample_times)
    if count <= 0:
        return []
    breaks = sorted({int(i) for i in cut_indices if 0 < int(i) < count})
    edges = [0, *breaks, count]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def measure_shots(
    samples: Sequence[tuple[float, Sequence[Any]]],
    cut_indices: Sequence[int],
    *,
    crop_h: int,
) -> list[Shot_Scale]:
    """Subject scale per shot (R2.1), from the detections the reframe pass already has.

    ``samples`` is `Sample_Report.samples`: ``(time, [detection, ...])`` per sampled frame, including
    frames with no detection. Taken from the report rather than from the smoothed centre series
    because `pick_main_face` reduces a detection to ``(cx, cy)`` and discards its size - the very
    quantity being measured here.

    Detections are duck-typed on ``.h`` (and ``.w`` only to break ties by area), so both `Detection`
    and `FaceBox` work. The **largest** box in a frame is the subject, matching `pick_main_face`'s
    own choice, so V23 measures the same face the crop is following.

    The per-shot statistic is the **median**, not the mean: a detector that briefly latches onto a
    background face produces one wildly different box, and a mean would carry that into the
    magnification for the whole shot.
    """
    height = float(max(1, int(crop_h)))
    out: list[Shot_Scale] = []
    for lo, hi in shot_bounds([t for t, _ in samples], cut_indices):
        window = samples[lo:hi]
        if not window:
            continue
        heights: list[float] = []
        for _t, detections in window:
            best = _largest(detections)
            if best is not None:
                heights.append(best / height)
        start = float(window[0][0])
        end = float(window[-1][0])
        if len(heights) < MIN_SAMPLES_PER_SHOT:
            out.append(Shot_Scale(start, end, None, len(heights)))
            continue
        out.append(Shot_Scale(start, end, float(statistics.median(heights)), len(heights)))
    return out


def _largest(detections: Sequence[Any]) -> float | None:
    """The height of the largest detection in one frame, or ``None`` when there is none."""
    best_area = -1.0
    best_h: float | None = None
    for det in detections or ():
        try:
            w = float(getattr(det, "w", 0.0) or 0.0)
            h = float(getattr(det, "h", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if h <= 0.0:
            continue
        area = w * h if w > 0.0 else h
        if area > best_area:
            best_area = area
            best_h = h
    return best_h


def plan_magnifications(shots: Sequence[Shot_Scale]) -> list[float]:
    """One magnification per shot, bounded, ``1.0`` where nothing should change.

    The target is the median of the *measured* shots (see the module note on why not the maximum).
    A shot at or above the target keeps ``1.0``: it would need to be **widened**, and the crop is
    already the largest window of this aspect that fits the source, so widening would mean reaching
    outside it (R2.6). That case is common and is not a failure - it is recorded, not corrected.

    An unmeasurable shot keeps ``1.0`` (R2.7): guessing a magnification from no detection would put a
    size step at a cut for no reason at all.
    """
    measured = [s.scale for s in shots if s.measured and s.scale is not None]
    if len(measured) < 2:
        # One shot cannot be inconsistent with itself, and zero cannot be measured.
        return [1.0] * len(shots)

    target = float(statistics.median(measured))
    if target <= 0.0:
        return [1.0] * len(shots)

    out: list[float] = []
    for shot in shots:
        if not shot.measured or shot.scale is None:
            out.append(1.0)
            continue
        ratio = target / shot.scale
        if ratio <= 1.0 + NEGLIGIBLE_DIFFERENCE:
            # Already at or above the target, or close enough that magnifying would spend
            # resolution on a difference nobody would see.
            out.append(1.0)
            continue
        out.append(round(min(ratio, MAX_MAGNIFICATION), 4))
    return out


def build_expression(
    shots: Sequence[Shot_Scale],
    magnifications: Sequence[float],
    *,
    fps: float,
) -> str:
    """A `zoompan` ``z`` expression that steps at cut times, or ``""`` when nothing changes.

    Written as nested ``if(lt(on,<frame>),<z>,...)`` over the **output frame index**, which is how
    `overlays.zoom_filter` already expresses `zoom_cut`. `on` rather than `t` because `zoompan`
    counts output frames and the two disagree whenever the output frame rate is not the input's.

    Constant within each shot by construction: the expression can only change value at a boundary
    frame, so R2.4 ("never adjust crop size within a shot") is a property of the shape rather than
    something to be checked. An *interpolated* size would be a zoom, which is the whole point of the
    requirement.

    Returns ``""`` when every magnification is 1.0, so the caller adds no filter at all and the
    rendered graph is character-for-character what it was.
    """
    if not shots or len(magnifications) != len(shots):
        return ""
    if all(m <= 1.0 for m in magnifications):
        return ""

    rate = float(fps) if fps and fps > 0 else 25.0
    # Boundary frames, derived from each shot's own start time so the step lands on the cut the
    # measurement came from.
    expr = f"{magnifications[-1]:.4f}"
    for index in range(len(shots) - 1, 0, -1):
        frame = max(1, int(round(float(shots[index].start) * rate)))
        expr = f"if(lt(on,{frame}),{magnifications[index - 1]:.4f},{expr})"
    return expr


def build_filter(expression: str, *, crop_w: int, crop_h: int, fps: float) -> str:
    """The `zoompan` fragment applying ``expression``, or ``""``.

    ``s=`` is the **crop** size, not the delivery size: this sits between `crop` and `scale`, so its
    job is to magnify within the cropped window and hand the same dimensions onward. Setting it to
    the delivery size here would scale twice and the later `scale` would be a no-op on an
    already-resampled frame, costing a generation of sharpness for nothing.

    ``d=1`` because each output frame is its own step - `zoompan`'s default of 90 would hold one
    frame for ninety and turn a step into a freeze.
    """
    if not expression:
        return ""
    rate = float(fps) if fps and fps > 0 else 25.0
    return (
        f"zoompan=z='{expression}':d=1:fps={rate:g}:s={int(crop_w)}x{int(crop_h)}"
        # Centred: the crop has already placed the subject, so magnifying about any other point
        # would undo the framing the tracker just chose.
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    )


def marker(plan: Scale_Plan) -> str:
    """The ``Effects_Applied`` entry, or ``""`` when no crop size was altered (R2.9).

    Names how many shots moved and the largest magnification applied, at fixed precision so a golden
    comparison cannot become platform-dependent. Empty when nothing changed: a marker on every clip
    is noise, and noise is what stops a marker being read.
    """
    if not plan.enabled or plan.altered <= 0:
        return ""
    largest = max(plan.magnifications) if plan.magnifications else 1.0
    return f"subject_scale:{plan.altered}:{largest:.3f}"


def plan(
    samples: Sequence[tuple[float, Sequence[Any]]],
    cut_indices: Sequence[int],
    *,
    crop_w: int,
    crop_h: int,
    fps: float,
    enabled: bool = False,
) -> Scale_Plan:
    """Measure, decide and build - the whole of V23 in one call.

    Returns an inert plan when disabled (R2.8), so the default path allocates no filter and renders
    byte-identically. Total: any malformed input produces an inert plan rather than an exception,
    because a framing refinement must never be the reason a clip fails.
    """
    if not enabled:
        return Scale_Plan(detail="subject-scale normalisation disabled")

    shots = measure_shots(samples, cut_indices, crop_h=crop_h)
    if len(shots) < 2:
        return Scale_Plan(shots=tuple(shots), detail="single shot; nothing to normalise against")

    magnifications = plan_magnifications(shots)
    expression = build_expression(shots, magnifications, fps=fps)
    built = Scale_Plan(
        expression=expression,
        magnifications=tuple(magnifications),
        shots=tuple(shots),
        detail=_detail(shots, magnifications),
    )
    return Scale_Plan(
        expression=built.expression,
        magnifications=built.magnifications,
        shots=built.shots,
        marker=marker(built),
        detail=built.detail,
    )


def _detail(shots: Sequence[Shot_Scale], magnifications: Sequence[float]) -> str:
    moved = sum(1 for m in magnifications if m > 1.0)
    unmeasured = sum(1 for s in shots if not s.measured)
    if moved == 0:
        return f"{len(shots)} shot(s) already consistent ({unmeasured} unmeasurable)"
    return (
        f"magnified {moved} of {len(shots)} shot(s) towards the median ({unmeasured} unmeasurable)"
    )
