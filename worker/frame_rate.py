"""Frame-rate policy for delivery (O18) and keyframe interval (O19).

**The existing rule is not wrong; its scope is.** `config.py` says variable-frame-rate sources are
resampled "which is what keeps burned captions in sync", and that reasoning is correct. VFR really
is "every screen recording and most phone footage", and a wandering frame duration really does drift
burned captions against speech. What the rule got wrong is applying to *everything*: a source that
is already constant 24 fps is resampled to 30, which inserts a 3:2 pulldown judder pattern into
footage that had none. The fix is to narrow the rule, not to remove it.

So the policy is:

* **VFR** -> normalise. Exactly as today, for exactly the documented reason.
* **CFR at a platform rate** (24, 25, 30, 50, 60) -> deliver at the source's own rate. No
  resampling, no judder, no invented frames.
* **CFR at any other rate** -> normalise. 15 fps timelapse, 12 fps animation and 29.97 drop-frame
  are all better served by one resample than by asking a platform to handle them.
* **Undeterminable** -> normalise. The conservative branch, because the cost of resampling
  something that did not need it is judder, and the cost of *not* resampling something that did is
  every caption drifting.

**Gated on measurement, not on argument** (R8.9). Frame-rate handling is the most likely place in
this pipeline to introduce A/V drift, and drift desynchronises every burned caption -- the exact
harm the original blanket rule prevented. `evaluation/sync.py` (M11) is what makes narrowing it
defensible: sync is verified at every rate this module can deliver, and the verification lives in
`tests/test_frame_rate_policy.py` rather than in a claim here.

O19's keyframe interval is in the same module because it **depends on this decision**. Nothing set
`-g`, so x264's default of 250 frames applied -- about 8 seconds at 30 fps, which is a long time to
wait for a seek or a thumbnail. The interval is expressed in *seconds* and converted using the
**delivered** rate, and that is the whole reason the two belong together: a hard-coded `-g 60` would
silently mean 2 s at 30 fps and 1 s at 60 fps, and O18 is what makes the delivered rate vary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

#: Frame rates a platform handles natively, so a CFR source at one of these needs no resampling.
#:
#: 24 (film), 25 (PAL), 30, 50, 60. Deliberately excludes 29.97 and 23.976: those are drop-frame
#: rates whose non-integer duration is what makes long-form sync hard, and normalising them is the
#: kinder outcome for a short clip.
PLATFORM_FRAME_RATES: tuple[int, ...] = (24, 25, 30, 50, 60)

#: How close a measured rate must be to a platform rate to count as that rate.
#:
#: Containers report rates as fractions and rounding leaves small errors, so an exact comparison
#: would reject a genuine 25 fps file reporting 24.99998. Tight enough that 29.97 does **not**
#: match 30 -- which is intentional, since 29.97 is a drop-frame rate and normalising it is the
#: documented choice.
RATE_TOLERANCE = 0.01

#: How far `avg_frame_rate` may sit from `r_frame_rate` before a source is called variable.
#:
#: For a true CFR file the two agree. For VFR, `r_frame_rate` reports the *base* (usually the
#: highest) rate while `avg_frame_rate` reports the actual average, so they diverge. 2% is loose
#: enough to absorb container rounding and a few duplicate frames, tight enough to catch the
#: screen-recording case this exists for.
VFR_DIVERGENCE = 0.02

#: Default keyframe interval in seconds (O19, R6.3).
#:
#: Two seconds. x264's unset default is 250 *frames* -- about 8 s at 30 fps -- which makes scrubbing
#: coarse and gives a platform's thumbnail picker very little to choose from. Two seconds is the
#: common streaming convention and costs a few percent of bitrate.
DEFAULT_KEYFRAME_SECONDS = 2.0


class Rate_Kind(str, Enum):
    """What we were able to determine about a source's frame-rate behaviour."""

    CONSTANT = "cfr"
    VARIABLE = "vfr"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Rate_Plan:
    """What frame rate to deliver, and whether that involved resampling."""

    kind: Rate_Kind
    source_fps: float
    delivered_fps: int
    normalised: bool
    marker: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["source_fps"] = round(self.source_fps, 4)
        return data


def classify(avg_fps: float, base_fps: float) -> Rate_Kind:
    """Decide whether a source is CFR, VFR, or undeterminable (R8.1).

    Uses the two rates ffprobe already reports, so this adds no probe. For a constant-rate file
    `avg_frame_rate` and `r_frame_rate` agree; for a variable-rate one the base rate is the highest
    the container allows while the average is what the file actually contains.

    A missing or zero rate is ``UNKNOWN`` rather than a guess, which routes to normalisation -- the
    conservative branch.
    """
    if avg_fps <= 0 or base_fps <= 0:
        return Rate_Kind.UNKNOWN
    divergence = abs(base_fps - avg_fps) / max(base_fps, avg_fps)
    return Rate_Kind.VARIABLE if divergence > VFR_DIVERGENCE else Rate_Kind.CONSTANT


def matching_platform_rate(fps: float) -> int | None:
    """The platform rate ``fps`` is (within tolerance), or ``None``.

    Returns ``None`` for 29.97 on purpose. It is within 0.1% of 30 and is *not* 30: the
    non-integer duration is the thing that makes drop-frame sync awkward, so it takes the
    normalising branch.
    """
    for rate in PLATFORM_FRAME_RATES:
        if abs(fps - rate) <= RATE_TOLERANCE:
            return rate
    return None


def plan_frame_rate(
    *,
    avg_fps: float,
    base_fps: float,
    configured_fps: int,
    always_normalise: bool = False,
    ceiling_fps: int | None = None,
) -> Rate_Plan:
    """Decide the delivered frame rate (R8.2-R8.6).

    ``always_normalise`` restores the previous blanket guarantee for anyone who wants it (R8.8).
    It is a real option rather than a courtesy: an operator delivering to one platform with one
    known requirement may reasonably prefer the certainty.

    ``ceiling_fps`` caps the result at the active platform profile's maximum (R8.6). A source at
    60 fps delivered to a profile that accepts 30 must be resampled even though 60 is a platform
    rate, because the constraint that matters is the destination's.
    """
    kind = classify(avg_fps, base_fps)
    target = int(configured_fps)

    if always_normalise:
        return Rate_Plan(
            kind=kind,
            source_fps=avg_fps,
            delivered_fps=target,
            normalised=True,
            marker=f"frame_rate_normalised:{target}:forced",
        )

    if kind is Rate_Kind.CONSTANT:
        native = matching_platform_rate(avg_fps)
        if native is not None:
            if ceiling_fps and native > int(ceiling_fps):
                # The destination's limit wins over the source's convenience.
                capped = int(ceiling_fps)
                return Rate_Plan(
                    kind=kind,
                    source_fps=avg_fps,
                    delivered_fps=capped,
                    normalised=True,
                    marker=f"frame_rate_normalised:{capped}:profile_ceiling",
                )
            return Rate_Plan(
                kind=kind,
                source_fps=avg_fps,
                delivered_fps=native,
                normalised=False,
                marker=f"frame_rate_preserved:{native}",
            )
        # CFR at an unusual rate: 15 fps timelapse, 12 fps animation, 29.97 drop-frame.
        return Rate_Plan(
            kind=kind,
            source_fps=avg_fps,
            delivered_fps=target,
            normalised=True,
            marker=f"frame_rate_normalised:{target}:non_platform_rate",
        )

    # VFR and UNKNOWN both normalise, and the marker distinguishes them: one is a positive finding
    # about the source and the other is an admission that we could not tell.
    reason = "vfr" if kind is Rate_Kind.VARIABLE else "undetermined"
    return Rate_Plan(
        kind=kind,
        source_fps=avg_fps,
        delivered_fps=target,
        normalised=True,
        marker=f"frame_rate_normalised:{target}:{reason}",
    )


def keyframe_interval_frames(delivered_fps: int, seconds: float) -> int:
    """``-g`` in frames, derived from the **delivered** rate (O19, R6.2).

    Derived rather than fixed, and that is the requirement rather than a preference: a hard-coded
    `-g 60` means 2 s at 30 fps and 1 s at 60 fps, and O18 is precisely what makes the delivered
    rate vary. Expressing the setting in seconds (R6.3) is what keeps the *intent* stable when the
    rate changes.

    Floored at 1 so a nonsensical setting cannot produce `-g 0`, which x264 reads as "every frame
    is a keyframe" -- a very large file, delivered without complaint.
    """
    return max(1, int(round(max(0.1, float(seconds)) * max(1, int(delivered_fps)))))
