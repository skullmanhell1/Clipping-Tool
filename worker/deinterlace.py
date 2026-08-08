"""Interlaced-source detection and deinterlacing (V20).

Combing that reaches the crop and scale becomes a smear no later filter can undo, so this has to
happen first — before the tone-map, before geometry, before anything (R9.2).

**Detection requires corroboration, and that is the whole design.** Two independent signals are
consulted and *both* must agree before anything is deinterlaced:

1. the container's declared ``field_order`` (``tt``/``bb``/``tb``/``bt`` mean interlaced,
   ``progressive`` means it is not), and
2. ffmpeg's ``idet`` filter, measured over a sample of real frames.

Neither is sufficient alone, and the measurement that established this is worth recording because
the naive implementation is very tempting:

* **``idet`` false-positives on detailed footage.** Measured on a *genuinely progressive*
  `testsrc2` render: ``TFF: 40  BFF: 9  Progressive: 11``. It called progressive footage
  interlaced by a wide margin. The same source rendered *soft* (blurred noise, no hard edges)
  measured ``TFF: 0  BFF: 0  Progressive: 50`` — so the false positive is driven by high-frequency
  horizontal detail, which is exactly what venetian blinds, fences, brickwork and small text look
  like. Trusting ``idet`` alone would deinterlace a large class of ordinary progressive video.
* **``field_order`` alone is a declaration, not a measurement.** It is routinely lost or wrongly
  copied through a re-encode, so a genuinely interlaced file can arrive claiming to be progressive.

**The asymmetry decides the tie.** Failing to deinterlace interlaced footage leaves combing that
was already in the source. Deinterlacing progressive footage **permanently destroys vertical
detail** in every clip, and no later stage can tell it happened. So disagreement resolves to
*inconclusive*, which does nothing and says so (R9.8) — the same conservatism `worker/colour.py`
applies to an unrecognised transfer function and `worker/language.py` to Han script.

**Frame rate is preserved, not doubled** (R9.5). ``yadif``/``bwdif`` in their default mode emit one
frame per *field*, turning 25i into 50p. That sounds like a free upgrade and interacts badly with
O18's frame-rate policy and O19's keyframe derivation, both of which read the delivered rate. Mode
``0`` (``send_frame``) keeps one frame per frame.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-checking only. The runtime import stays inside `filters_available`, because
    # `capabilities` shells out to `ffmpeg -filters` on first use and this module is imported at
    # pipeline scope. `worker/colour.py` guards its prober import the same way.
    from worker.engines.capabilities import Capability_Status

#: A capability prober, matching `worker.engines.capabilities.Prober`. Restated rather than imported
#: so this module has no runtime dependency on that one.
Prober = Callable[[str], "Capability_Status"]

#: Container ``field_order`` values that declare interlacing.
#:
#: ``tt``/``bb`` are top/bottom field first with both fields in one frame; ``tb``/``bt`` are the
#: mixed orderings. Anything else — ``progressive``, ``unknown``, absent — is not a declaration of
#: interlacing.
INTERLACED_FIELD_ORDERS: frozenset[str] = frozenset({"tt", "bb", "tb", "bt"})

#: Deinterlacers this will use, in order of preference.
#:
#: ``bwdif`` first: it is a later filter than ``yadif`` and produces visibly fewer artefacts on
#: motion. ``yadif`` is the fallback because it is in essentially every build. Both are probed
#: rather than assumed (R9.4).
DEINTERLACE_FILTERS: tuple[str, ...] = ("bwdif", "yadif")

#: The ``idet`` filter, needed for the measurement half of detection.
DETECT_FILTER = "idet"

#: Frames sampled for detection.
#:
#: 120 at 25 fps is a little under five seconds — long enough to cross a shot boundary or two, short
#: enough that detection is not a second decode of the whole file.
DETECT_FRAMES = 120

#: Fraction of *decided* frames that must read interlaced for ``idet`` to corroborate.
#:
#: 0.65 rather than a bare majority. The progressive false positive measured 49 interlaced against
#: 11 progressive — a 0.82 ratio — so a majority threshold would not have rejected it either. What
#: rejects it is requiring the container to agree as well; this threshold only guards against a
#: handful of combed frames in an otherwise progressive file swinging the result.
IDET_INTERLACED_RATIO = 0.65

#: Minimum decided frames for the ``idet`` reading to mean anything.
MIN_DECIDED_FRAMES = 20


class Scan(str, Enum):
    """What we determined about a source's scan type. Three states, and the third is load-bearing."""

    INTERLACED = "interlaced"
    PROGRESSIVE = "progressive"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Scan_Report:
    """The determination plus both signals, so a wrong answer is diagnosable.

    Both inputs are kept rather than just the verdict. When a source is deinterlaced that should
    not have been — or the reverse — the useful question is *which signal was wrong*, and a bare
    verdict cannot answer it.
    """

    scan: Scan
    field_order: str = ""
    idet_interlaced: int = 0
    idet_progressive: int = 0
    detail: str = ""

    @property
    def should_deinterlace(self) -> bool:
        return self.scan is Scan.INTERLACED

    def to_dict(self) -> dict:
        data = asdict(self)
        data["scan"] = self.scan.value
        return data


def _ffmpeg() -> str:
    from config import settings

    return shutil.which(str(settings.ffmpeg_binary)) or "ffmpeg"


_MULTI_FRAME = re.compile(
    r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)"
)


def idet_counts(source: str | Path, *, frames: int = DETECT_FRAMES) -> tuple[int, int]:
    """``(interlaced_frames, progressive_frames)`` from ffmpeg's ``idet``.

    Reads the **multi-frame** figures rather than the single-frame ones. Single-frame detection
    judges each frame in isolation and is markedly noisier; multi-frame uses temporal context,
    which is what the decision actually depends on.

    Returns ``(0, 0)`` on any failure. A detection that cannot run must not be an error — it routes
    to ``INCONCLUSIVE``, which does nothing.
    """
    try:
        proc = subprocess.run(
            [
                _ffmpeg(),
                "-hide_banner",
                "-nostats",
                "-i",
                str(source),
                "-vf",
                DETECT_FILTER,
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
        return 0, 0

    combined = (proc.stdout or "") + (proc.stderr or "")
    matches = _MULTI_FRAME.findall(combined)
    if not matches:
        return 0, 0
    # The last report is cumulative over every frame seen.
    tff, bff, progressive = (int(v) for v in matches[-1])
    return tff + bff, progressive


def filters_available(prober: Prober | None = None) -> str:
    """The first *available* deinterlacer, or ``""`` if none is (R9.4).

    Routed through ``worker.engines.capabilities``, the project's single filter probe. ``idet`` is
    checked too: without it there is no measurement half of the detection, and detection on the
    container's declaration alone is precisely what this module refuses to do.

    Fails **closed** — no filter reported means no deinterlacing — matching `worker/colour.py`'s
    tone-map probe. Emitting a filter this ffmpeg lacks would fail the render, and a fidelity
    feature must never turn a deliverable clip into a failed job.
    """
    try:
        from worker.engines.capabilities import Capability_Report, get_report

        report = Capability_Report(prober) if prober is not None else get_report()
        if not report.status(f"ffmpeg_filter:{DETECT_FILTER}").available:
            return ""
        for name in DEINTERLACE_FILTERS:
            if report.status(f"ffmpeg_filter:{name}").available:
                return name
    except Exception:
        return ""
    return ""


def classify(field_order: str, interlaced_frames: int, progressive_frames: int) -> Scan_Report:
    """Combine the container's declaration with ``idet``'s measurement (R9.1, R9.8).

    Both must agree. See the module docstring for the measurements that forced this: ``idet`` alone
    called a progressive source interlaced 49 frames to 11, and ``field_order`` alone is a
    declaration that survives a re-encode unreliably.
    """
    declared = (field_order or "").strip().lower()
    declares_interlaced = declared in INTERLACED_FIELD_ORDERS
    declares_progressive = declared == "progressive"

    decided = interlaced_frames + progressive_frames
    if decided < MIN_DECIDED_FRAMES:
        return Scan_Report(
            Scan.INCONCLUSIVE,
            declared,
            interlaced_frames,
            progressive_frames,
            detail=f"idet decided only {decided} frame(s); too few to corroborate",
        )

    ratio = interlaced_frames / decided
    measures_interlaced = ratio >= IDET_INTERLACED_RATIO

    if declares_interlaced and measures_interlaced:
        return Scan_Report(
            Scan.INTERLACED,
            declared,
            interlaced_frames,
            progressive_frames,
            detail=f"container says {declared} and idet agrees ({ratio:.0%} interlaced)",
        )
    if declares_progressive and not measures_interlaced:
        return Scan_Report(
            Scan.PROGRESSIVE,
            declared,
            interlaced_frames,
            progressive_frames,
            detail="container says progressive and idet agrees",
        )
    if declares_progressive and measures_interlaced:
        # The measured false positive. Resolved *against* deinterlacing, because destroying
        # vertical detail on progressive footage is irreversible and invisible downstream, whereas
        # leaving combing preserves what the source already had.
        return Scan_Report(
            Scan.INCONCLUSIVE,
            declared,
            interlaced_frames,
            progressive_frames,
            detail=(
                f"container says progressive but idet reads {ratio:.0%} interlaced; "
                "high horizontal detail reads as combing, so this is treated as unproven"
            ),
        )
    return Scan_Report(
        Scan.INCONCLUSIVE,
        declared,
        interlaced_frames,
        progressive_frames,
        detail=f"signals disagree or are absent (field_order={declared or 'absent'})",
    )


def detect(source: str | Path, *, prober: Prober | None = None) -> Scan_Report:
    """Determine a source's scan type, using the probe we already run for everything else."""
    field_order = ""
    try:
        from worker import ffmpeg_utils as fu

        field_order = getattr(fu.probe(source), "field_order", "") or ""
    except Exception:
        field_order = ""

    if not filters_available(prober):
        return Scan_Report(
            Scan.INCONCLUSIVE,
            field_order,
            detail="no deinterlacer or no idet in this ffmpeg build",
        )

    interlaced, progressive = idet_counts(source)
    return classify(field_order, interlaced, progressive)


def filter_chain(name: str, *, double_rate: bool = False) -> str:
    """The deinterlace filter fragment (R9.5).

    ``mode=0`` (``send_frame``) emits one frame per input frame. The default ``mode=1``
    (``send_field``) emits one per *field*, turning 25i into 50p — which sounds like a free upgrade
    and interacts badly with O18's frame-rate policy and O19's keyframe derivation, both of which
    read the delivered rate. ``double_rate`` is the documented opt-out.
    """
    if name not in DEINTERLACE_FILTERS:
        name = DEINTERLACE_FILTERS[-1]
    return f"{name}=mode={1 if double_rate else 0}"


def plan(
    source: str | Path,
    *,
    enabled: bool = True,
    double_rate: bool = False,
    prober: Prober | None = None,
) -> tuple[str, tuple[str, ...], Scan_Report]:
    """``(filter_chain, markers, report)`` for one source.

    An empty chain means nothing should be done, which is the overwhelmingly common case — so a
    library of progressive clips renders byte-identically to before this existed.
    """
    if not enabled:
        return "", (), Scan_Report(Scan.INCONCLUSIVE, detail="deinterlacing disabled")

    available = filters_available(prober)
    if not available:
        # R9.4: name the capability rather than degrading silently.
        return (
            "",
            (f"deinterlace_degraded:ffmpeg_filter:{DEINTERLACE_FILTERS[0]}",),
            Scan_Report(Scan.INCONCLUSIVE, detail="no deinterlacer available"),
        )

    report = detect(source, prober=prober)
    if report.scan is Scan.INTERLACED:
        return (
            filter_chain(available, double_rate=double_rate),
            (f"deinterlace:{available}",),
            report,
        )
    if report.scan is Scan.INCONCLUSIVE:
        # R9.8: say that the determination failed. An absent feature with no explanation is
        # indistinguishable from a broken one.
        return "", ("deinterlace_inconclusive",), report
    return "", (), report
