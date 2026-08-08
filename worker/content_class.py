"""Screen-recording and graphics detection (V24).

A 16:9 slide cropped to 9:16 delivers an unreadable middle third. The pipeline has no notion of
content *type*, so it treats a spreadsheet exactly as it treats a face — tracks it, crops into it,
and throws away the two thirds that carried the information.

**High precision, deliberately low recall.** This classifier answers ``SCREEN`` only when it is
nearly certain, and ``UNKNOWN`` for everything else — and ``UNKNOWN`` means the existing behaviour
runs unchanged (R5). That asymmetry is the design, because the two errors are not comparable:
mistaking a slide for camera footage leaves things exactly as they are today, while mistaking a
talking head for a slide letterboxes a face into the middle of the frame and wastes half the screen.

**The thresholds come from measurement, and the measurement shows what this cannot do** (R9). Two
signal features over sampled frames, no model and no network (R2):

===========================  =========  ======  ======
source                       entropy Y    YDIF  truth
===========================  =========  ======  ======
flat UI, static                  0.150   0.000  screen
slide deck, changing             0.116   0.000  screen
flat-colour animation            0.434   2.317  screen
screen recording of a video      0.512   2.870  screen
camera, moving                   0.633   2.855  camera
camera, nearly still             0.232   0.899  camera
===========================  =========  ======  ======

**Neither feature separates the classes on its own.** Nearly-still camera footage has entropy of
0.232, inside the range flat UI occupies; animation and a screen recording of a video sit at
0.43-0.51, overlapping moving camera footage at 0.633. Any single-threshold classifier built on
either one misclassifies real footage.

What *is* reliable is the **conjunction**: near-zero temporal difference together with low histogram
entropy. Sensor noise means camera footage cannot produce ``YDIF`` of zero even pointed at a blank
wall — the nearly-still shot above still measured 0.899 — while synthetic content repeats pixels
exactly. So that pair identifies static synthetic content with no false positives on either camera
fixture.

The cost is stated rather than hidden: **animation and screen recordings of moving video are not
detected**, and fall through to ``UNKNOWN``. Catching them needs a real content model, which R2
forbids. The case this does catch — a static slide or document being cropped into — is both the most
common and the most damaging.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:  # pragma: no cover - typing only
    from worker.engines.capabilities import Capability_Status

Prober: TypeAlias = Callable[[str], "Capability_Status"]

#: Filters the classifier needs. ``entropy`` is not in every build, so it is probed.
REQUIRED_FILTERS: tuple[str, ...] = ("entropy", "signalstats")

#: Frames sampled per clip. R6 requires per-clip classification, so this is paid per clip and has to
#: stay cheap: 75 frames is three seconds at 25 fps, enough to cross a slide transition.
SAMPLE_FRAMES = 75

#: Upper bound on mean temporal difference for content to count as synthetic.
#:
#: Measured: static synthetic content reads exactly **0.000**, and the nearly-still camera fixture --
#: a blurred noise field, about as static as camera footage gets -- reads **0.899**. 0.15 sits in
#: that gap with room on both sides, and is above zero so that a lightly re-compressed screen
#: recording, whose codec artefacts perturb otherwise identical pixels, still qualifies.
MAX_SYNTHETIC_YDIF = 0.15

#: Upper bound on normalised luma-histogram entropy for content to count as synthetic.
#:
#: Measured: flat UI 0.116-0.150, nearly-still camera 0.232, moving camera 0.633. 0.20 separates the
#: UI fixtures from the nearest camera reading. Tight on purpose -- this threshold is the one that
#: protects camera footage, and R10 makes automatic classification conditional on not degrading it.
MAX_SYNTHETIC_ENTROPY = 0.20


class Content(str, Enum):
    """What a clip's content was determined to be. ``UNKNOWN`` is a first-class answer."""

    SCREEN = "screen"
    CAMERA = "camera"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Content_Report:
    """The classification plus the features behind it, so a wrong answer is diagnosable."""

    content: Content
    ydif: float = 0.0
    entropy: float = 0.0
    frames: int = 0
    detail: str = ""
    forced: bool = False

    @property
    def is_synthetic(self) -> bool:
        """Whether a consumer should treat this as synthetic content (R11).

        The property other components ask, rather than each re-deriving the comparison -- `V21`
        stabilisation refuses synthetic content, and a second copy of the rule would be a second
        thing to get wrong.
        """
        return self.content is Content.SCREEN

    @property
    def marker(self) -> str:
        """The ``Effects_Applied`` entry naming the class detected (R7)."""
        if self.forced:
            return f"content_class:{self.content.value}:forced"
        if self.content is Content.UNKNOWN:
            return "content_class:unknown"
        return f"content_class:{self.content.value}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["content"] = self.content.value
        return data


def _ffmpeg() -> str:
    from config import settings

    return shutil.which(str(settings.ffmpeg_binary)) or "ffmpeg"


def filters_available(prober: Prober | None = None) -> bool:
    """Whether this ffmpeg can measure both features.

    Fails **closed** — no measurement means ``UNKNOWN``, which runs the existing behaviour. That is
    the safe direction here: the alternative would be classifying on one feature, and the
    measurements in this module's docstring show that neither feature separates the classes alone.
    """
    try:
        from worker.engines.capabilities import Capability_Report, get_report

        report = Capability_Report(prober) if prober is not None else get_report()
        return all(report.status(f"ffmpeg_filter:{name}").available for name in REQUIRED_FILTERS)
    except Exception:
        return False


_ENTROPY = re.compile(r"normalized_entropy\.normal\.Y=([0-9.]+)")
_YDIF = re.compile(r"signalstats\.YDIF=([0-9.]+)")


def measure(
    source: str | Path,
    *,
    start: float = 0.0,
    frames: int = SAMPLE_FRAMES,
) -> tuple[float, float, int]:
    """``(mean_ydif, mean_entropy, frames_read)`` over a sample of ``source``.

    ``start`` is what makes this per-clip rather than per-source (R6): a recording that alternates
    between a camera and a shared screen must be classified where the clip actually is, not where
    the file begins.

    Returns zeros on any failure, which the caller reads as ``UNKNOWN``.
    """
    try:
        proc = subprocess.run(
            [
                _ffmpeg(),
                "-hide_banner",
                "-nostats",
                "-ss",
                f"{max(0.0, float(start)):.3f}",
                "-i",
                str(source),
                "-vf",
                "entropy,signalstats,metadata=print:file=-",
                "-frames:v",
                str(int(frames)),
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except Exception:
        return 0.0, 0.0, 0

    text = (proc.stdout or "") + (proc.stderr or "")
    entropies = [float(v) for v in _ENTROPY.findall(text)]
    diffs = [float(v) for v in _YDIF.findall(text)]
    if not entropies or not diffs:
        return 0.0, 0.0, 0

    # The frame count is the smaller of the two, so a partial read cannot make one feature's mean
    # describe more frames than the other's.
    count = min(len(entropies), len(diffs))
    mean_ydif = sum(diffs[:count]) / count
    mean_entropy = sum(entropies[:count]) / count
    return mean_ydif, mean_entropy, count


def classify_features(ydif: float, entropy: float, frames: int) -> Content_Report:
    """Classify from measured features. Pure, so the thresholds are directly testable.

    The rule is a **conjunction**, and the module docstring records why: neither feature separates
    the classes on its own, and a single-threshold classifier on either one misclassifies real
    camera footage.
    """
    if frames <= 0:
        return Content_Report(
            Content.UNKNOWN,
            ydif,
            entropy,
            frames,
            detail="no frames measured; existing behaviour applies",
        )

    still = ydif <= MAX_SYNTHETIC_YDIF
    flat = entropy <= MAX_SYNTHETIC_ENTROPY

    if still and flat:
        return Content_Report(
            Content.SCREEN,
            ydif,
            entropy,
            frames,
            detail=(
                f"temporally static (YDIF {ydif:.3f} <= {MAX_SYNTHETIC_YDIF}) and flat "
                f"(entropy {entropy:.3f} <= {MAX_SYNTHETIC_ENTROPY}); camera footage cannot be "
                "both, because sensor noise prevents a zero temporal difference"
            ),
        )

    # Everything else is UNKNOWN rather than CAMERA. Asserting "camera" would be a claim this
    # measurement cannot support -- animation and a screen recording of moving video both land here
    # -- and R5 makes UNKNOWN behave exactly as today, so the distinction costs nothing.
    reason = []
    if not still:
        reason.append(f"YDIF {ydif:.3f} above {MAX_SYNTHETIC_YDIF}")
    if not flat:
        reason.append(f"entropy {entropy:.3f} above {MAX_SYNTHETIC_ENTROPY}")
    return Content_Report(
        Content.UNKNOWN,
        ydif,
        entropy,
        frames,
        detail=(
            f"not identifiable as synthetic ({'; '.join(reason)}). Animation and screen "
            "recordings of moving video are known to land here; catching those needs a content "
            "model, which this deliberately does not use."
        ),
    )


def classify(
    source: str | Path,
    *,
    start: float = 0.0,
    override: str = "auto",
    enabled: bool = True,
    prober: Prober | None = None,
) -> Content_Report:
    """Classify one clip's content (R1, R6).

    ``override`` accepts ``camera`` or ``screen`` to force the answer (R8), because an operator who
    knows what they uploaded should not have to argue with a classifier. An unrecognised value falls
    back to ``auto`` rather than raising.
    """
    forced = (override or "auto").strip().lower()
    if forced in {Content.CAMERA.value, Content.SCREEN.value}:
        return Content_Report(
            Content(forced), detail=f"forced by configuration to {forced}", forced=True
        )

    if not enabled:
        return Content_Report(
            Content.UNKNOWN, detail="automatic classification disabled; existing behaviour applies"
        )
    if not filters_available(prober):
        return Content_Report(
            Content.UNKNOWN,
            detail=f"this ffmpeg lacks {' or '.join(REQUIRED_FILTERS)}; cannot classify",
        )

    ydif, entropy, frames = measure(source, start=start)
    return classify_features(ydif, entropy, frames)


def fit_instead_of_crop(report: Content_Report) -> bool:
    """Whether this clip should be *fitted* into the frame rather than cropped into (R3).

    Reuses `ffmpeg_utils.reformat_aspect`'s existing ``pad``/``crop_blur`` modes rather than
    introducing a third geometry path, and reuses `detect_letterbox` for bar geometry (R12) — the
    caller passes the content rectangle it already computes.
    """
    return report.is_synthetic


def skip_face_tracking(report: Content_Report) -> bool:
    """Whether face-tracking reframe should be skipped for this clip (R4).

    Separate from :func:`fit_instead_of_crop` because they are separate requirements and a consumer
    may honour one without the other -- and because a face detector run against a slide finds
    nothing anyway, so this saves a decode rather than changing a look.
    """
    return report.is_synthetic


def measured_behaviour() -> dict:
    """The measurements the thresholds were set from, and what they show this cannot do (R9).

    Committed as data rather than prose so it can be quoted in a report and diffed when the
    thresholds move. R9 asks for measured misclassification behaviour rather than an accuracy claim,
    and the honest answer includes the two classes this misses.
    """
    return {
        "features": ["signalstats YDIF (mean)", "entropy normalized_entropy.normal.Y (mean)"],
        "thresholds": {
            "max_synthetic_ydif": MAX_SYNTHETIC_YDIF,
            "max_synthetic_entropy": MAX_SYNTHETIC_ENTROPY,
            "rule": "both must hold; either alone misclassifies real footage",
        },
        "measured": [
            {
                "source": "flat UI, static",
                "ydif": 0.000,
                "entropy": 0.150,
                "truth": "screen",
                "classified": "screen",
            },
            {
                "source": "slide deck, changing",
                "ydif": 0.000,
                "entropy": 0.116,
                "truth": "screen",
                "classified": "screen",
            },
            {
                "source": "flat-colour animation",
                "ydif": 2.317,
                "entropy": 0.434,
                "truth": "screen",
                "classified": "unknown",
            },
            {
                "source": "screen recording of a video",
                "ydif": 2.870,
                "entropy": 0.512,
                "truth": "screen",
                "classified": "unknown",
            },
            {
                "source": "camera, moving",
                "ydif": 2.855,
                "entropy": 0.633,
                "truth": "camera",
                "classified": "unknown",
            },
            {
                "source": "camera, nearly still",
                "ydif": 0.899,
                "entropy": 0.232,
                "truth": "camera",
                "classified": "unknown",
            },
        ],
        "false_positives_on_camera": 0,
        "missed_screen_content": ["flat-colour animation", "screen recording of moving video"],
        "why_neither_feature_alone": (
            "Nearly-still camera footage has entropy 0.232, inside the range flat UI occupies. "
            "Animation and a screen recording of a video sit at 0.43-0.51, overlapping moving "
            "camera footage at 0.633. Only the conjunction of near-zero temporal difference and "
            "low entropy separates the classes without false positives on camera footage."
        ),
        "honest_limit": (
            "High precision, low recall. UNKNOWN runs the existing behaviour unchanged, so a missed "
            "detection costs nothing new; a false positive would letterbox a face, which is why the "
            "thresholds are set to avoid that instead of to maximise detections."
        ),
    }
