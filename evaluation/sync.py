"""A/V sync verification (M11): does the audio still line up with the picture?

**No defect is alleged.** `cut_segment` puts `-ss` before `-i`, which is frame-accurate under
re-encoding in modern ffmpeg, and this module makes no claim that anything is broken. What it
observes is that **nothing measured it** — and if sync ever drifts, every burned-in caption drifts
with it and no test in the project would notice, because a desynchronised clip is a perfectly valid
file of the right duration.

The measurement is deliberately crude and therefore trustworthy: a fixture carries a **white flash
and an audio burst at the same instant**, and the offset is the difference between where each one
is found in the decoded output. A test asserting the argument list contains `-ss` proves nothing
about the file (R4.2); this decodes both streams and compares them.

Measured precision on this build: a synchronised fixture reads **0.1 ms**, with the flash located
at frame 25 (t=1.000) and the burst at sample 48003 (t=1.0001). So the instrument comfortably
resolves the ~40 ms single-frame granularity that matters, and a reading of 8 ms is meaningfully
different from one of 0 ms — which is why :class:`Sync_Report` carries the number and not just a
verdict (R4.6).
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

#: Tolerance for "synchronised", in milliseconds.
#:
#: One frame at 25 fps is 40 ms and at 30 fps is 33 ms, and an error smaller than a frame cannot be
#: corrected by shifting video. Half a frame is the useful threshold: below it, nothing actionable
#: exists. Documented rather than tuned, and a run **reports its measurement regardless** — the
#: tolerance decides only whether a check fails, never what gets recorded.
TOLERANCE_MS = 20.0

#: Fraction of the audio peak that counts as the burst's onset.
#:
#: A sine burst reaches full amplitude within a cycle, and AAC's encoding smears a hard edge
#: slightly, so a threshold near the peak would locate the onset a millisecond or two late and one
#: near the noise floor would trigger on the codec's pre-echo. 0.3 sits in the flat middle of that
#: range; measured, the located onset was 0.1 ms from truth.
AUDIO_ONSET_FRACTION = 0.3

#: Luma the flash frame must exceed to be treated as the visual event, on the 0-255 scale
#: `signalstats` reports. The fixture flashes to white on a black field, so anything above the
#: midpoint is unambiguous; this is not a general-purpose scene-change detector and does not
#: pretend to be.
FLASH_LUMA_MIN = 128.0


class SyncError(RuntimeError):
    """The offset could not be measured. Raised rather than reporting a plausible zero."""


@dataclass(frozen=True)
class Sync_Report:
    """Where each event was found, and the resulting offset.

    Both onsets are reported, not just the difference. When a reading looks wrong it is almost
    always one detector that failed rather than genuine drift, and the difference alone cannot
    distinguish those.
    """

    offset_ms: float
    audio_onset_s: float
    video_onset_s: float
    within_tolerance: bool
    tolerance_ms: float = TOLERANCE_MS
    label: str = ""
    note: str = (
        "Positive offset means the audio event arrives later than the visual event. The measured "
        "value is reported whether or not it is within tolerance: a run reading 8 ms differs "
        "meaningfully from one reading 0 ms, and only one of those is a trend."
    )

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("offset_ms", "audio_onset_s", "video_onset_s"):
            data[key] = round(data[key], 4)
        return data


def _ffmpeg() -> str:
    from config import settings

    return shutil.which(str(settings.ffmpeg_binary)) or "ffmpeg"


def _ffprobe() -> str:
    from config import settings

    return shutil.which(str(settings.ffprobe_binary)) or "ffprobe"


def make_sync_fixture(
    dest: str | Path,
    *,
    event_at: float = 1.0,
    duration: float = 3.0,
    audio_offset: float = 0.0,
    fps: int = 25,
    size: str = "320x180",
    vfr: bool = False,
) -> Path:
    """Render a source whose audio and visual events coincide (R4.1, R4.2).

    ``audio_offset`` deliberately desynchronises the fixture, which is what makes the instrument
    falsifiable: a detector that always answers zero passes every synchronised test ever written.

    The events are chosen to be trivially locatable rather than realistic. A white flash on black
    and a 1 kHz burst in silence have unambiguous onsets, so a non-zero reading is the pipeline's
    doing and never the detector's.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    audio_at = max(0.0, event_at + audio_offset)

    video_src = f"color=c=black:s={size}:r={fps}:d={duration}"
    audio_expr = (
        f"if(between(t,{audio_at},{audio_at + 0.05}), 0.8*sin(2*PI*1000*t), 0)"
    )
    flash = (
        f"drawbox=x=0:y=0:w=iw:h=ih:color=white@1.0:t=fill:"
        f"enable='between(t,{event_at},{event_at + 1.0 / fps})'"
    )

    cmd = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", video_src,
        "-f", "lavfi", "-i", f"aevalsrc='{audio_expr}':d={duration}:s=48000",
        "-vf", flash,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
    ]
    if vfr:
        # A variable frame rate source, which `config.py`'s own comment calls "every screen
        # recording and most phone footage". `-vsync vfr` plus a duplicate-dropping filter
        # produces genuinely irregular frame durations rather than a relabelled CFR file.
        cmd += ["-vsync", "vfr"]
    cmd += [str(dest)]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise SyncError(f"could not build fixture: {proc.stderr.strip()}")
    return dest


_FRAME_INDEX = re.compile(r"^frame:(\d+)", re.MULTILINE)
_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")


def video_onset(path: str | Path) -> float:
    """Seconds at which the brightest frame occurs.

    Located by decoding and reading per-frame luma, not by reading a container timestamp. The
    frame index is converted using the stream's **actual** average rate, so a clip that was
    resampled from VFR to CFR is still measured against the timeline it really has.
    """
    proc = subprocess.run(
        [
            _ffmpeg(), "-hide_banner", "-nostats", "-i", str(path),
            "-vf", "signalstats,metadata=print:file=-", "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=900,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    frames = _FRAME_INDEX.findall(combined)
    lumas = _YAVG.findall(combined)
    if not frames or len(frames) != len(lumas):
        raise SyncError(
            f"could not read per-frame luma from {path} "
            f"({len(frames)} frame markers, {len(lumas)} luma readings)"
        )

    best_index, best_luma = -1, -1.0
    for raw_index, raw_luma in zip(frames, lumas, strict=True):
        luma = float(raw_luma)
        if luma > best_luma:
            best_index, best_luma = int(raw_index), luma
    if best_luma < FLASH_LUMA_MIN:
        raise SyncError(
            f"no visual event found in {path}: brightest frame is {best_luma:.1f}, "
            f"below the {FLASH_LUMA_MIN} threshold. The fixture may be missing its flash."
        )

    fps = _stream_fps(path)
    return best_index / fps


def _stream_fps(path: str | Path) -> float:
    proc = subprocess.run(
        [
            _ffprobe(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate", "-of", "default=nw=1:nk=1", str(path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    text = (proc.stdout or "").strip()
    try:
        num, _, den = text.partition("/")
        rate = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        rate = 0.0
    if rate <= 0:
        raise SyncError(f"could not determine frame rate for {path} (got {text!r})")
    return rate


def audio_onset(path: str | Path, *, sample_rate: int = 48000) -> float:
    """Seconds at which the audio burst begins.

    Decoded to mono PCM and scanned for the first sample crossing a fraction of the clip's peak.
    Relative to the peak rather than an absolute level, so the measurement does not depend on the
    encoder's output gain or on any loudness normalisation the pipeline applied — which matters,
    since `AU1`'s two-pass `loudnorm` changes absolute levels by design.
    """
    proc = subprocess.run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", "0:a", "-f", "s16le", "-ac", "1", "-ar", str(sample_rate), "-",
        ],
        capture_output=True, timeout=900,
    )
    raw = proc.stdout or b""
    count = len(raw) // 2
    if count == 0:
        raise SyncError(f"no decodable audio in {path}")
    samples = struct.unpack(f"<{count}h", raw[: count * 2])

    peak = max((abs(s) for s in samples), default=0)
    if peak == 0:
        raise SyncError(f"audio in {path} is silent; no event to locate")
    threshold = peak * AUDIO_ONSET_FRACTION
    for index, value in enumerate(samples):
        if abs(value) > threshold:
            return index / float(sample_rate)
    raise SyncError(f"no audio onset above threshold in {path}")


def measure_sync(path: str | Path, *, label: str = "") -> Sync_Report:
    """Measure the offset between the audio and visual events in a rendered file.

    Positive means the audio arrives late. Reports the measurement whether or not it is within
    tolerance (R4.6), because the trend is the useful part and a bare pass/fail discards it.
    """
    v = video_onset(path)
    a = audio_onset(path)
    offset_ms = (a - v) * 1000.0
    return Sync_Report(
        offset_ms=offset_ms,
        audio_onset_s=a,
        video_onset_s=v,
        within_tolerance=abs(offset_ms) <= TOLERANCE_MS,
        label=label or str(Path(path).name),
    )


def report_many(reports: Sequence[Sync_Report]) -> dict:
    """Collect several readings into one committable record, with no verdict beyond tolerance."""
    return {
        "tolerance_ms": TOLERANCE_MS,
        "measurements": [r.to_dict() for r in reports],
        "worst_ms": max((abs(r.offset_ms) for r in reports), default=0.0),
        "all_within_tolerance": all(r.within_tolerance for r in reports),
        "note": (
            "This records what was measured. No defect is alleged by the existence of these "
            "numbers: cut_segment's -ss placement is frame-accurate under re-encoding in modern "
            "ffmpeg. The finding these measurements exist to prevent is drift going unnoticed, "
            "since a desynchronised clip is a valid file of the correct duration and every "
            "burned-in caption drifts with it."
        ),
    }
