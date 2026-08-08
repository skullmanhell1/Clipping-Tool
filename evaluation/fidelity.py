"""Full-reference render fidelity: SSIM, PSNR and VMAF (M9).

**An instrument, not a stage.** Nothing in `worker/` imports this, and that boundary is the
point: a measurement that can reach into the render path can be made to agree with it. When a
reading here reveals a defect, the fix belongs in whichever sibling spec owns the behaviour.

The distinction this module exists to draw is one `golden_render.py` cannot: it answers *"did it
change?"* by hashing frames, which is exactly right for catching an accidental change and tells
you nothing about whether a deliberate one was an improvement. Before this, there was no `vmaf`,
`psnr` or `ssim` anywhere in the repository, so every encoder and scaler decision was taste
asserted against taste.

Four things are load-bearing.

**A fidelity metric measures reproduction, not quality** (R1.9). A beautifully framed clip that
encodes badly scores low; a flawless reproduction of a badly framed reference scores 1.0. It can
tell you whether an encoder setting threw away detail. It cannot tell you whether a clip is worth
watching, and nothing in this module should ever be quoted as though it could.

**Mean *and* minimum, always** (R1.8). A mean SSIM of 0.98 is entirely compatible with one frame
at 0.4 — which is precisely what a scene-change encode decision produces, and precisely what a
viewer notices. Reporting only the mean hides the single worst frame, which is the one that gets
screenshotted.

**Misaligned comparisons are refused, not reported** (R1.6). ffmpeg's `ssim` will happily compare
frame *N* of one file against frame *N+1* of another for the whole remainder of a clip, and the
number it produces is plausible, catastrophic and completely misleading. A refusal that names the
mismatch is worth more than a reading nobody can trust.

**VMAF's absence is a reported state, never a failure and never a pass** (R1.4, R1.5). `libvmaf`
needs an ffmpeg built with it, Debian's routinely lacks it, and the `Dockerfile` deliberately does
not pin a version. So the module is shaped around resolving that rather than assuming it. SSIM and
PSNR are the floor and are always present.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from worker.engines.capabilities import Capability_Status

Prober = Callable[[str], "Capability_Status"]


#: Metric ids. `str, Enum` matching the project's existing style.
class Metric(str, Enum):
    SSIM = "ssim"
    PSNR = "psnr"
    VMAF = "vmaf"


#: The metrics that need no optional ffmpeg component. These are the floor: a run always
#: produces both, so a fidelity comparison is never impossible, only ever less complete.
ALWAYS_AVAILABLE: tuple[str, ...] = (Metric.SSIM.value, Metric.PSNR.value)

#: The capability id `libvmaf` is resolved through. Deliberately the same id vocabulary
#: `worker/engines/capabilities.py` uses, so one probe answers for the whole process.
VMAF_CAPABILITY = "ffmpeg_filter:libvmaf"

#: The filter name inside that id, derived rather than repeated so the two cannot drift.
VMAF_FILTER = VMAF_CAPABILITY.split(":", 1)[1]

#: Reference-render settings: the *same filter graph* at much higher fidelity (R1.1, task 2.3).
#:
#: The reference is not the raw source. Comparing a delivered clip against its source would
#: measure the reframe, the captions and the crop — all deliberate — and report them as fidelity
#: loss. Holding the graph fixed and changing only CRF and preset is what isolates the encode.
REFERENCE_CRF = 12
REFERENCE_PRESET = "slow"

#: PSNR is unbounded above; identical input yields infinity. Reported as `math.inf` rather than a
#: sentinel number, so arithmetic on it stays honest and a caller cannot mistake 999 for a
#: measurement.
PSNR_IDENTICAL = math.inf

#: What VMAF actually reports for an **identical** pair, measured on this build rather than
#: assumed. It is not 100, and the gap is not an error.
#:
#: Measured: mean **99.945**, minimum **97.428**, and the minimum is always **frame 0**. VMAF
#: includes temporal (motion) features, and the first frame has no predecessor, so its motion
#: feature is degenerate and its score dips. Steady-state identical frames score 99.956, not 100,
#: because the model saturates near but not at its ceiling.
#:
#: Two consequences worth stating where they will be read. A test asserting `min == 100` on an
#: identical pair fails for a reason that has nothing to do with this code. And a reader comparing
#: **minima** across runs will see a ~2.5-point dip on every clip that is purely frame 0 — so a
#: minimum that moves *from* 97.4 is a signal, and 97.4 itself is the floor of the instrument.
#:
#: SSIM and PSNR have no such artefact: both are exactly 1.0 and infinite respectively, on every
#: frame including the first. Which is the practical argument for keeping all three rather than
#: only the newest.
VMAF_IDENTICAL_MEAN = 99.945
VMAF_IDENTICAL_MINIMUM = 97.428
VMAF_FIRST_FRAME_IS_DEGENERATE = True


class FidelityError(RuntimeError):
    """A measurement could not be taken. Raised rather than returning a plausible number."""


class MisalignedComparison(FidelityError):
    """The two files are not frame-for-frame comparable (R1.6).

    Its own class because this is the failure a caller most needs to distinguish: it means the
    inputs were wrong, not that the encoder was bad.
    """


@dataclass(frozen=True)
class Metric_Availability:
    """Whether one metric can be measured here, and if not, why not."""

    metric: str
    available: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Metric_Reading:
    """One metric's mean, minimum and frame count — or an explicit unavailable state.

    ``available=False`` carries a ``reason`` and **no numbers**. That shape is deliberate: an
    omitted key reads as "not measured" to one caller and "nothing wrong" to another, and a
    zero would read as a catastrophic score. Neither is what an absent `libvmaf` means.
    """

    metric: str
    mean: float = 0.0
    minimum: float = 0.0
    frames: int = 0
    available: bool = True
    reason: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        # JSON has no infinity. Emitted as a string so a report stays loadable by anything,
        # rather than as `Infinity`, which `json.dump` writes and strict parsers reject.
        for key in ("mean", "minimum"):
            value = data[key]
            if isinstance(value, float) and math.isinf(value):
                data[key] = "inf"
        return data


@dataclass(frozen=True)
class Fidelity_Report:
    """A full reading plus the provenance needed to know what it can be compared against."""

    readings: tuple[Metric_Reading, ...] = ()
    provenance: dict = field(default_factory=dict)
    encode_seconds: float = 0.0
    size_bytes: int = 0
    #: R2.6. Stated in the report rather than in documentation, because a report outlives the
    #: context it was produced in and the caveat has to travel with the numbers.
    caveat: str = (
        "Readings are not comparable across ffmpeg builds: filter implementations and libvmaf "
        "model versions change between releases, and this project deliberately does not pin "
        "ffmpeg. Two reports from different builds are two different experiments."
    )

    def reading(self, metric: str) -> Metric_Reading | None:
        for item in self.readings:
            if item.metric == metric:
                return item
        return None

    def to_dict(self) -> dict:
        return {
            "readings": [r.to_dict() for r in self.readings],
            "provenance": dict(self.provenance),
            "encode_seconds": round(self.encode_seconds, 3),
            "size_bytes": self.size_bytes,
            "caveat": self.caveat,
        }


def _ffmpeg() -> str:
    from config import settings

    return shutil.which(str(settings.ffmpeg_binary)) or "ffmpeg"


def _ffprobe() -> str:
    from config import settings

    return shutil.which(str(settings.ffprobe_binary)) or "ffprobe"


def _vmaf_ffmpeg() -> str:
    """The binary VMAF is measured with — ``settings.vmaf_ffmpeg_binary`` if set.

    Separate from :func:`_ffmpeg` because no single build serves both jobs: ``libvmaf`` is
    absent from the ffmpeg mainstream distributions ship, while the third-party builds that
    have it signal colour differently and break the colour-pipeline readings. So the primary
    binary stays the distribution one and only this measurement moves.

    Empty setting means "the primary binary", which keeps the single-binary case exactly as it
    was: VMAF works if that build has ``libvmaf`` and is reported unavailable by name if not.
    """
    from config import settings

    configured = str(getattr(settings, "vmaf_ffmpeg_binary", "") or "").strip()
    if not configured:
        return _ffmpeg()
    # `which` so a bare name on PATH resolves, matching `_ffmpeg`; the configured value is
    # returned unresolved rather than silently falling back to the primary binary, because a
    # VMAF binary that was asked for and is missing must surface as a named failure, not as a
    # measurement quietly taken with the wrong build.
    return shutil.which(configured) or configured


def _vmaf_binary_is_separate() -> bool:
    """Whether VMAF will run on a different binary than everything else."""
    return _vmaf_ffmpeg() != _ffmpeg()


def available_metrics(prober: Prober | None = None) -> tuple[Metric_Availability, ...]:
    """Which metrics this ffmpeg can measure (R1.3, R6.6).

    Resolved through ``worker.engines.capabilities`` and **not** by a second probe of our own.
    That module exists because a hand-rolled probe once misparsed ``ffmpeg -filters`` and hid
    **124 of 486 filters** — an answer cached where nobody was looking, which is the same shape
    of defect as reporting VMAF unavailable on a build that has it.

    ``prober`` builds a fresh report rather than passing through to ``get_report``, which honours
    an injected prober *only on first construction* and returns the process-wide singleton
    otherwise. Passing one through would mean a test simulating an absent `libvmaf` silently got
    the real answer and passed for the wrong reason.
    """
    entries = [Metric_Availability(name, True) for name in ALWAYS_AVAILABLE]

    detail = ""
    ok = False
    if prober is None and _vmaf_binary_is_separate():
        # VMAF is measured with its own build, so the capability report cannot answer this:
        # that report describes `settings.ffmpeg_binary`, which is precisely the build known
        # not to have `libvmaf`. Asking it would report VMAF unavailable while the measurement
        # would in fact succeed — availability has to be answered by whichever binary is going
        # to run the filter. Still resolved through the shared listing parser rather than a
        # `-filters` grep of our own, for the reason in this function's docstring.
        binary = _vmaf_ffmpeg()
        try:
            from worker.engines.capabilities import ffmpeg_filter_available

            ok = ffmpeg_filter_available(VMAF_FILTER, binary=binary)
            detail = (
                "" if ok else f"{VMAF_FILTER} not built into the configured VMAF ffmpeg: {binary}"
            )
        except Exception as exc:
            # A configured-but-unusable VMAF binary is a named unavailability, not a crash and
            # not a silent fall back to the primary build: the whole point of configuring one is
            # that a reading taken with a different binary is not the reading that was asked for.
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
    else:
        try:
            from worker.engines.capabilities import Capability_Report, get_report

            report = Capability_Report(prober) if prober is not None else get_report()
            status = report.status(VMAF_CAPABILITY)
            ok = bool(status.available)
            detail = status.detail or ""
        except Exception as exc:  # pragma: no cover - defensive; capabilities never raises
            ok = False
            detail = f"{type(exc).__name__}: {exc}"

    entries.append(
        Metric_Availability(
            Metric.VMAF.value,
            ok,
            # Named reason (R1.2). "libvmaf not present in this ffmpeg build" is actionable;
            # a bare False is not, and this is the state a reader will meet most often.
            "" if ok else (detail or f"{VMAF_CAPABILITY} unavailable in this ffmpeg build"),
        )
    )
    return tuple(entries)


def _probe_geometry(path: str | Path) -> tuple[int, int, int]:
    """``(width, height, frame_count)`` by decoding, not by trusting the container.

    ``nb_frames`` is a header field and is routinely absent or wrong — which matters here more
    than usual, because an inaccurate count would make the alignment guard in
    :func:`measure` pass on files that are not comparable, defeating the guard entirely.
    ``-count_frames`` decodes, so it is slow and correct; these are short clips.
    """
    proc = subprocess.run(
        [
            _ffprobe(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=width,height,nb_read_frames",
            "-of",
            "default=nw=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise FidelityError(f"could not probe {path}: {proc.stderr.strip()}")
    values: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    try:
        return (
            int(values.get("width") or 0),
            int(values.get("height") or 0),
            int(values.get("nb_read_frames") or 0),
        )
    except ValueError as exc:
        raise FidelityError(f"unreadable geometry for {path}: {values}") from exc


def _require_aligned(reference: str | Path, candidate: str | Path) -> int:
    """Refuse a comparison that is not frame-for-frame (R1.6). Returns the frame count.

    Both halves matter and they fail differently. A **resolution** mismatch makes ffmpeg's
    `ssim` error out or silently scale, depending on build. A **frame-count** mismatch is the
    dangerous one: it compares frame N against N+1 for the entire remainder and produces a
    number that is plausible, terrible, and about nothing.
    """
    ref_w, ref_h, ref_n = _probe_geometry(reference)
    can_w, can_h, can_n = _probe_geometry(candidate)
    if (ref_w, ref_h) != (can_w, can_h):
        raise MisalignedComparison(
            f"resolution mismatch: reference {ref_w}x{ref_h}, candidate {can_w}x{can_h}. "
            "Scaling one to match would measure the scaler, not the encode."
        )
    if ref_n != can_n or ref_n == 0:
        raise MisalignedComparison(
            f"frame-count mismatch: reference {ref_n} frames, candidate {can_n}. "
            "Comparing these would align frame N against N+1 for the remainder and report a "
            "plausible, meaningless number."
        )
    return ref_n


#: Per-frame SSIM lines, anchored on the frame number.
#:
#: The `n:` prefix is not decoration. ffmpeg also prints a **summary** line for `ssim`
#: (`[Parsed_ssim_0 @ ...] SSIM Y:... U:... V:... All:0.99 (30.0)`) which contains `All:` too, so
#: a bare `All:` pattern silently counts the summary as one extra frame. Measured: 51 readings
#: for a 50-frame file, with the summary's value folded into the mean.
#:
#: Worth recording how that was found, because it says something about the test design: the
#: self-comparison identity test (task 2.5) **did not catch it** — on identical input the summary
#: is also 1.0, so SSIM still came back exactly 1.0. What caught it was comparing the frame count
#: against PSNR's and VMAF's on the same file. A parsed frame count is therefore an assertion in
#: its own right, not a diagnostic.
_SSIM_FRAME = re.compile(r"^n:\d+.*?\bAll:\s*([0-9.]+)", re.MULTILINE)
_PSNR_FRAME = re.compile(r"^n:\d+.*?\bpsnr_avg:\s*([0-9.]+|inf)", re.MULTILINE)


def _run_filter(
    candidate: str | Path, reference: str | Path, lavfi: str, *, binary: str = ""
) -> str:
    """Run a two-input comparison filter, returning combined output.

    Input 0 is the candidate and input 1 the reference, matching every ffmpeg example and the
    argument order of `ssim`/`psnr`/`libvmaf` themselves. Getting this backwards is not
    detectable from the numbers for SSIM and PSNR, which are symmetric — but it is for VMAF,
    which is not, so the order is fixed here once rather than at each call.

    ``binary`` defaults to the primary ffmpeg. Only the VMAF path overrides it, so SSIM and
    PSNR are always measured with the build this project actually renders with — moving them
    onto a second binary would change the numbers a stored baseline is compared against for a
    reason unrelated to the encode.
    """
    proc = subprocess.run(
        [
            binary or _ffmpeg(),
            "-hide_banner",
            "-nostats",
            "-i",
            str(candidate),
            "-i",
            str(reference),
            "-lavfi",
            lavfi,
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        tail = "\n".join(combined.strip().splitlines()[-10:])
        raise FidelityError(f"comparison failed ({lavfi}): {tail}")
    return combined


def _reduce(values: Sequence[float], metric: str) -> Metric_Reading:
    if not values:
        raise FidelityError(f"{metric}: no per-frame readings parsed")
    finite = [v for v in values if not math.isinf(v)]
    # Mean over infinities is infinity, which is correct for an identical pair and is what a
    # reader should see rather than a large finite number implying a measurement.
    mean = (
        (sum(finite) / len(finite))
        if finite and len(finite) == len(values)
        else (math.inf if not finite else sum(finite) / len(finite))
    )
    return Metric_Reading(
        metric=metric,
        mean=mean,
        minimum=min(values),
        frames=len(values),
    )


def measure_ssim(candidate: str | Path, reference: str | Path) -> Metric_Reading:
    """Per-frame SSIM. 1.0 is identical."""
    out = _run_filter(candidate, reference, "[0:v][1:v]ssim=stats_file=-")
    values = [float(m) for m in _SSIM_FRAME.findall(out)]
    return _reduce(values, Metric.SSIM.value)


def measure_psnr(candidate: str | Path, reference: str | Path) -> Metric_Reading:
    """Per-frame PSNR in dB. Infinite for an identical pair."""
    out = _run_filter(candidate, reference, "[0:v][1:v]psnr=stats_file=-")
    values = [(PSNR_IDENTICAL if m == "inf" else float(m)) for m in _PSNR_FRAME.findall(out)]
    return _reduce(values, Metric.PSNR.value)


def measure_vmaf(
    candidate: str | Path, reference: str | Path, *, log_dir: str | Path
) -> Metric_Reading:
    """Per-frame VMAF, 0-100. Requires an ffmpeg built with ``libvmaf``."""
    # Created rather than required. libvmaf's failure mode for an unwritable `log_path` is to
    # complete the pass and write nothing, so the caller sees "no readable log" and goes looking
    # for a broken filter instead of a missing directory. Cheap to prevent, confusing to debug.
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "vmaf.json"
    lavfi = f"[0:v][1:v]libvmaf=log_path={log_path.as_posix()}:log_fmt=json"
    _run_filter(candidate, reference, lavfi, binary=_vmaf_ffmpeg())
    try:
        data = json.loads(log_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FidelityError(f"libvmaf produced no readable log: {exc}") from exc
    values = [
        float(frame["metrics"]["vmaf"])
        for frame in data.get("frames", [])
        if "metrics" in frame and "vmaf" in frame["metrics"]
    ]
    return _reduce(values, Metric.VMAF.value)


def provenance(prober: Prober | None = None) -> dict:
    """What a reading depends on, so two reports can be known to be comparable (R2.2).

    Recorded rather than assumed because every field here has changed the numbers at least
    once in some project: the ffmpeg build, the encoder actually resolved (which O8's probe can
    change per machine), CRF, preset, and the code revision.
    """
    from config import settings

    def _version_line(binary: str) -> str:
        try:
            proc = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=60)
            return (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        except Exception:
            return "unknown"

    version = _version_line(_ffmpeg())
    # VMAF may be measured with a different build than SSIM and PSNR, and this report's own
    # caveat says readings are not comparable across builds. Recording only the primary version
    # would let two reports look comparable while their VMAF columns came from different
    # `libvmaf` versions — which is the exact mistake `compare` refuses to make for ffmpeg.
    vmaf_version = _version_line(_vmaf_ffmpeg()) if _vmaf_binary_is_separate() else version

    encoder = ""
    try:
        from worker import video_encoders

        encoder = video_encoders.resolve_encoder().encoder.name
    except Exception:
        encoder = "unknown"

    revision = ""
    try:
        # S607: `git` is resolved from PATH, exactly as ffmpeg and ffprobe are throughout this
        # project. Hard-coding an absolute path would break on every platform that installs it
        # somewhere else, and the value is used only as a provenance label.
        git_argv = ["git", "rev-parse", "--short", "HEAD"]
        proc = subprocess.run(git_argv, capture_output=True, text=True, timeout=30)
        revision = (proc.stdout or "").strip() or "unknown"
    except Exception:
        revision = "unknown"

    return {
        "ffmpeg_version": version,
        "vmaf_ffmpeg_version": vmaf_version,
        "encoder": encoder,
        "x264_crf": int(settings.x264_crf),
        "x264_preset": str(settings.x264_preset),
        "output_fps": int(settings.output_fps),
        "reference_crf": REFERENCE_CRF,
        "reference_preset": REFERENCE_PRESET,
        "revision": revision,
        "metrics_available": [a.to_dict() for a in available_metrics(prober)],
    }


def measure(
    candidate: str | Path,
    reference: str | Path,
    *,
    metrics: Sequence[str] | None = None,
    prober: Prober | None = None,
    log_dir: str | Path | None = None,
    encode_seconds: float = 0.0,
) -> Fidelity_Report:
    """Compare ``candidate`` against ``reference`` on every requested, available metric.

    Alignment is checked **first** (R1.6), so a misaligned pair costs one probe rather than a
    full VMAF pass and, more importantly, never yields a number.
    """
    _require_aligned(reference, candidate)

    availability = {a.metric: a for a in available_metrics(prober)}
    wanted = list(metrics) if metrics else [m.value for m in Metric]
    log_root = Path(log_dir) if log_dir else Path(candidate).resolve().parent

    readings: list[Metric_Reading] = []
    for name in wanted:
        entry = availability.get(name)
        if entry is None:
            readings.append(Metric_Reading(metric=name, available=False, reason="unknown metric"))
            continue
        if not entry.available:
            # R1.5: an explicit unavailable state carrying the reason. Not an omitted key, and
            # emphatically not a pass — "we could not measure this" and "this measured fine"
            # are the two answers a reader must never see conflated.
            readings.append(Metric_Reading(metric=name, available=False, reason=entry.reason))
            continue
        if name == Metric.SSIM.value:
            readings.append(measure_ssim(candidate, reference))
        elif name == Metric.PSNR.value:
            readings.append(measure_psnr(candidate, reference))
        elif name == Metric.VMAF.value:
            readings.append(measure_vmaf(candidate, reference, log_dir=log_root))

    size = 0
    try:
        size = Path(candidate).stat().st_size
    except OSError:
        size = 0

    return Fidelity_Report(
        readings=tuple(readings),
        provenance=provenance(prober),
        encode_seconds=encode_seconds,
        size_bytes=size,
    )


def timed_encode(argv: Sequence[str]) -> float:
    """Run an encode, returning wall-clock seconds (R2.3, task 2.4).

    Cost is recorded beside quality because a fidelity gain with a 2x time cost is a trade to
    discuss, not a win to announce — and `x264_preset` is paid three times per clip, so the
    multiplier is not hypothetical.
    """
    started = time.monotonic()
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=3600)
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        raise FidelityError(f"encode failed: {tail}")
    return elapsed


def compare(before: dict, after: dict) -> dict:
    """Difference two Fidelity_Reports, naming every metric that moved (R2.7).

    **Refuses to subtract readings from different ffmpeg builds.** Silently differencing them
    is the precise error the report's own caveat exists to prevent, and it is an easy one to
    make because the arithmetic works perfectly and the result looks like a finding.
    """
    b_prov = (before or {}).get("provenance", {})
    a_prov = (after or {}).get("provenance", {})
    b_ff = b_prov.get("ffmpeg_version", "")
    a_ff = a_prov.get("ffmpeg_version", "")
    if b_ff != a_ff:
        raise FidelityError(
            "refusing to compare readings from different ffmpeg builds:\n"
            f"  before: {b_ff or '(unrecorded)'}\n"
            f"  after:  {a_ff or '(unrecorded)'}\n"
            "These are two different experiments, not two measurements."
        )

    # The same refusal for the VMAF binary, which since VMAF moved onto its own build is a
    # second thing the readings depend on. Without this the guard has a hole exactly where it
    # is least visible: swap only the VMAF ffmpeg and `ffmpeg_version` still matches, so the
    # check above passes and the VMAF column gets differenced across two `libvmaf` versions.
    #
    # Only enforced when *both* reports recorded it. Baselines written before this field existed
    # have no value to compare, and refusing those would make an unrelated schema addition look
    # like a build mismatch — the guard would then be discarded for crying wolf, which costs more
    # than the case it would have caught.
    b_vmaf = b_prov.get("vmaf_ffmpeg_version", "")
    a_vmaf = a_prov.get("vmaf_ffmpeg_version", "")
    if b_vmaf and a_vmaf and b_vmaf != a_vmaf:
        raise FidelityError(
            "refusing to compare readings from different VMAF ffmpeg builds:\n"
            f"  before: {b_vmaf}\n"
            f"  after:  {a_vmaf}\n"
            "SSIM and PSNR came from the same build, but VMAF did not."
        )

    def index(report: dict) -> dict[str, dict]:
        return {r["metric"]: r for r in (report or {}).get("readings", [])}

    b_read, a_read = index(before), index(after)
    moved: list[dict] = []
    for metric in sorted(set(b_read) | set(a_read)):
        b, a = b_read.get(metric), a_read.get(metric)
        if not b or not a:
            moved.append({"metric": metric, "note": "present in only one report"})
            continue
        if not (b.get("available", True) and a.get("available", True)):
            moved.append(
                {
                    "metric": metric,
                    "note": "unavailable in at least one report",
                    "before_reason": b.get("reason", ""),
                    "after_reason": a.get("reason", ""),
                }
            )
            continue

        def num(value) -> float:
            return math.inf if value == "inf" else float(value)

        entry: dict[str, object] = {"metric": metric}
        for field_name in ("mean", "minimum"):
            delta = num(a[field_name]) - num(b[field_name])
            entry[field_name] = {
                "before": b[field_name],
                "after": a[field_name],
                "delta": "inf" if math.isinf(delta) else round(delta, 6),
            }
        moved.append(entry)

    return {
        "ffmpeg_version": a_ff,
        "metrics": moved,
        "cost": {
            "encode_seconds": {
                "before": (before or {}).get("encode_seconds", 0.0),
                "after": (after or {}).get("encode_seconds", 0.0),
            },
            "size_bytes": {
                "before": (before or {}).get("size_bytes", 0),
                "after": (after or {}).get("size_bytes", 0),
            },
        },
        # R2.8 / task 3.5: no verdict. There is deliberately no threshold anywhere in this
        # module, because an absolute SSIM or VMAF gate would either never fire or block
        # unrelated work. These are recorded baselines and relative comparisons, and the
        # judgement stays with the reader.
        "note": (
            "Relative comparison only. No pass/fail threshold is applied: a fidelity metric "
            "measures reproduction, not quality, and whether a gain justifies its cost is a "
            "decision rather than a measurement."
        ),
    }
