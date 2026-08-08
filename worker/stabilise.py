"""Optional stabilisation for shaky sources (V21).

Handheld footage under a *moving* crop compounds: the camera shakes, and the reframe crop chases the
shake, so the delivered clip moves more than the source did. Stabilising first means the crop tracks
a subject that is already steady.

**The margin problem is the substance of this item**, and it is the reason R10.5 exists.
``vidstab`` corrects shake by *translating the frame*, which needs somewhere to translate into. There
are two ways to provide it and only one composes with the rest of this pipeline:

* ``optzoom``/``zoom`` — let ``vidstab`` scale the picture up so the shifted edges are always
  covered. **Rejected.** It silently changes subject scale, which fights `V23`'s scale
  normalisation, and it changes it by an amount that varies with how shaky the footage was — so two
  clips from one source would be framed differently for reasons nobody chose.
* **Leave the edges and declare a smaller valid rectangle.** Taken. ``vidstab`` shifts within a
  bounded range, the outer band may contain invalid pixels, and reframing is told to keep its crop
  inside the remainder.

That second option needs no new concept, which is the point: ``build_sendcmd`` already accepts
``origin_x``/``origin_y``/``src_w``/``src_h`` to confine the crop to a content rectangle, because
that is how `V16` letterbox handling works and how `V22`'s headroom clamp stays in bounds. The
stabilisation margin is the same kind of fact, so it travels the same way.

**Bounded by configuration, not discovered from the transforms file.** ``maxshift`` caps the
translation in pixels, so the invalid band is known *before* the analysis runs and the content
rectangle is deterministic. Reading the actual shifts back out of the transforms file would give a
tighter rectangle and make the geometry depend on the footage — so two renders of the same clip at
different settings would crop differently, and a golden could never be frozen.

**Never applied to synthetic content** (R10.9). Screen recordings have no camera shake, and
``vidstab`` finds spurious motion in scrolling text and introduces a wobble that was not there. The
decision is delegated to `worker.content_class`, which owns it — a second copy of that rule would be
a second thing to get wrong when its thresholds move.

**Two passes over the video, and that is unavoidable** (R10.8). ``vidstabdetect`` must see the whole
clip before ``vidstabtransform`` can act, so this is the one feature here that genuinely costs an
extra decode. On a long source the analysis looks like a stalled job, so it reports progress rather
than going quiet.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:  # pragma: no cover - typing only
    from worker.engines.capabilities import Capability_Status

Prober: TypeAlias = Callable[[str], "Capability_Status"]

#: Filters a two-pass stabilisation needs. Both come from ``libvidstab``, which is a build option --
#: present here, absent from several distribution builds -- so it is probed rather than assumed.
REQUIRED_FILTERS: tuple[str, ...] = ("vidstabdetect", "vidstabtransform")

#: ``vidstabdetect`` shakiness, 1-10. 5 is the filter's own default and suits handheld footage.
DETECT_SHAKINESS = 5

#: ``vidstabdetect`` accuracy, 1-15. 15 is the maximum and the analysis is already the expensive
#: pass, so paying for the best available measurement is the right trade.
DETECT_ACCURACY = 15

#: Smoothing window in frames for ``vidstabtransform``.
#:
#: 10 frames either side, so about two thirds of a second at 30 fps. Longer windows produce a
#: smoother "tripod" look and lag genuine camera moves; shorter ones leave residual jitter.
TRANSFORM_SMOOTHING = 10

#: Maximum translation, as a fraction of each dimension, at full strength.
#:
#: This is the *margin* the content rectangle gives up, so it is deliberately small. 4% of a 1080p
#: height is 43 px of correction -- enough for handheld walking shake, not enough to fix a source
#: that needed a tripod. Larger values buy more correction and cost frame, and the cost is paid on
#: every clip whether or not the shake was there.
MAX_SHIFT_FRACTION = 0.04


@dataclass(frozen=True)
class Stabilise_Plan:
    """What stabilisation will do, and the margin it consumes.

    ``margin_x``/``margin_y`` are the inset a consumer must apply to the content rectangle. They are
    part of the plan rather than something reframing recomputes, because the two must agree exactly:
    if reframing assumed a smaller margin than ``vidstab`` used, the crop can include invalid pixels
    and the clip gets black edges (R10.5).
    """

    enabled: bool = False
    strength: float = 0.0
    margin_x: int = 0
    margin_y: int = 0
    markers: tuple[str, ...] = ()
    detail: str = ""

    def content_rect(self, src_w: int, src_h: int) -> tuple[int, int, int, int]:
        """``(origin_x, origin_y, width, height)`` reframing must keep its crop inside (R10.5).

        The same shape `V16` letterbox detection returns, so a caller can hand either to
        ``build_sendcmd`` without knowing which produced it.
        """
        if not self.enabled:
            return 0, 0, int(src_w), int(src_h)
        inset_x = min(self.margin_x, max(0, int(src_w) // 4))
        inset_y = min(self.margin_y, max(0, int(src_h) // 4))
        # Even dimensions: libx264's 4:2:0 subsampling requires them, and an odd crop fails the
        # encode outright rather than degrading.
        width = max(2, int(src_w) - 2 * inset_x)
        height = max(2, int(src_h) - 2 * inset_y)
        return inset_x, inset_y, width - (width % 2), height - (height % 2)

    def to_dict(self) -> dict:
        return asdict(self)


def clamp_strength(strength: float) -> float:
    """Bring a stabilisation strength into ``[0.0, 1.0]``. Unusable values disable it."""
    try:
        value = float(strength)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return max(0.0, min(1.0, value))


def _ffmpeg() -> str:
    from config import settings

    return shutil.which(str(settings.ffmpeg_binary)) or "ffmpeg"


def filters_available(prober: Prober | None = None) -> bool:
    """Whether this ffmpeg has ``libvidstab``.

    Fails **closed**: emitting ``vidstabdetect`` on a build without it is a filter-graph
    configuration error, which fails the render. Stabilisation is optional, so declining it costs
    nothing — the same reasoning the tone-map and deinterlace probes use.
    """
    try:
        from worker.engines.capabilities import Capability_Report, get_report

        report = Capability_Report(prober) if prober is not None else get_report()
        return all(report.status(f"ffmpeg_filter:{name}").available for name in REQUIRED_FILTERS)
    except Exception:
        return False


def margin_pixels(src_w: int, src_h: int, strength: float) -> tuple[int, int]:
    """The invalid band ``vidstab`` may leave, in pixels, for a given strength.

    Derived from the configured cap rather than from the transforms file, so the content rectangle is
    known before the analysis runs and does not vary with how shaky a particular clip happened to be
    (see the module docstring).
    """
    amount = clamp_strength(strength)
    return (
        int(round(int(src_w) * MAX_SHIFT_FRACTION * amount)),
        int(round(int(src_h) * MAX_SHIFT_FRACTION * amount)),
    )


def detect_filter(transforms_path: str | Path, strength: float) -> str:
    """The first-pass analysis filter. Writes transforms, produces no picture worth keeping."""
    amount = clamp_strength(strength)
    # Shakiness scales with strength so a low setting is not paying for a search it will not use.
    shakiness = max(1, min(10, int(round(1 + (DETECT_SHAKINESS - 1) * amount))))
    escaped = str(Path(transforms_path)).replace("\\", "\\\\").replace(":", "\\:")
    return f"vidstabdetect=shakiness={shakiness}:accuracy={DETECT_ACCURACY}:result={escaped}"


def transform_filter(
    transforms_path: str | Path, strength: float, *, src_w: int, src_h: int
) -> str:
    """The second-pass correction filter.

    ``optzoom=0`` is the important argument, and it is deliberate. With optimal zoom enabled
    ``vidstab`` scales the picture to hide the shifted edges, which changes subject scale by an
    amount that depends on how shaky the footage was — fighting `V23` and making two clips from one
    source frame differently for reasons nobody chose. Disabled, the edges may be invalid and
    :meth:`Stabilise_Plan.content_rect` is what keeps the crop out of them.
    """
    amount = clamp_strength(strength)
    shift_x, shift_y = margin_pixels(src_w, src_h, strength)
    smoothing = max(1, int(round(TRANSFORM_SMOOTHING * amount)) or 1)
    escaped = str(Path(transforms_path)).replace("\\", "\\\\").replace(":", "\\:")
    return (
        f"vidstabtransform=input={escaped}:smoothing={smoothing}"
        f":maxshift={max(shift_x, shift_y)}:optzoom=0:crop=black:interpol=bilinear"
    )


def plan(
    *,
    src_w: int,
    src_h: int,
    strength: float = 0.0,
    is_synthetic: bool = False,
    prober: Prober | None = None,
) -> Stabilise_Plan:
    """Decide whether and how to stabilise (R10.1, R10.3, R10.9).

    ``is_synthetic`` comes from `worker.content_class`, which owns that determination. Passed in
    rather than looked up here so this module has no opinion about how synthetic content is
    identified — when V24's thresholds move, only one place changes.
    """
    amount = clamp_strength(strength)
    if amount <= 0:
        return Stabilise_Plan(detail="stabilisation disabled")

    if is_synthetic:
        # R10.9. Screen recordings have no camera shake, and vidstab finds spurious motion in
        # scrolling text and introduces a wobble that was not in the source -- so this is a refusal
        # to make things worse, not a missing capability.
        return Stabilise_Plan(
            strength=amount,
            markers=("stabilise_skipped:synthetic_content",),
            detail="source classified as a screen recording or graphics; nothing to stabilise",
        )

    if not filters_available(prober):
        return Stabilise_Plan(
            strength=amount,
            markers=(f"stabilise_degraded:ffmpeg_filter:{REQUIRED_FILTERS[0]}",),
            detail="this ffmpeg has no libvidstab",
        )

    margin_x, margin_y = margin_pixels(src_w, src_h, amount)
    return Stabilise_Plan(
        enabled=True,
        strength=amount,
        margin_x=margin_x,
        margin_y=margin_y,
        # R10.6: the marker names the strength that ran, not the one requested.
        markers=(f"stabilise:{amount:.2f}",),
        detail=(
            f"stabilising at {amount:.2f}; content rectangle inset by {margin_x}x{margin_y}px so "
            "reframing does not consume the same margin"
        ),
    )


def run_analysis(
    source: str | Path,
    transforms_path: str | Path,
    strength: float,
    *,
    progress: Callable[[float, str], None] | None = None,
) -> bool:
    """Run the first pass. Returns whether it produced a usable transforms file.

    ``progress`` exists because of R10.8: this is the one genuinely expensive addition in the spec —
    ``vidstabdetect`` must see every frame before ``vidstabtransform`` can act — and on a long source
    a silent two-pass analysis is indistinguishable from a hung job.

    Returns ``False`` rather than raising on any failure, so a stabilisation that cannot run degrades
    to an unstabilised clip instead of failing it.
    """
    if progress is not None:
        progress(0.0, "Analysing camera motion")
    try:
        proc = subprocess.run(
            [
                _ffmpeg(),
                "-hide_banner",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-vf",
                detect_filter(transforms_path, strength),
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except Exception:
        return False
    if progress is not None:
        progress(1.0, "Camera motion analysed")
    return proc.returncode == 0 and Path(transforms_path).is_file()
