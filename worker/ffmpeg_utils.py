"""Thin FFmpeg/FFprobe helpers.

Wraps common FFmpeg operations (probing, cutting, aspect reformatting) behind
small, well-documented Python functions so the rest of the pipeline never shells
out directly. Binary paths come from :data:`config.settings`.

All functions raise :class:`FFmpegError` on failure with the captured stderr so
callers can surface actionable errors.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import settings


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe invocation fails."""


# --------------------------------------------------------------------------- #
# H.264 output settings (O1, O2, O3)
# --------------------------------------------------------------------------- #
# Every encode in this repository spelled out ``-c:v libx264 -preset veryfast -crf 20``
# and nothing else, in seven places. Three flags that decide whether a platform will
# accept the file at all were missing everywhere:
#
#   O1  -pix_fmt yuv420p    Without it, ffmpeg keeps the source pixel format. A 4:2:2 or
#                           10-bit source therefore produced a 4:2:2/10-bit H.264 file,
#                           which Safari, many Android decoders and several upload
#                           pipelines refuse outright - and the failure appears at upload
#                           time, long after the render looked fine locally.
#   O2  -profile:v high     libx264 otherwise picks a profile from the input, and it can
#       -level 4.0          land above what older hardware decoders implement.
#   O3  -r <fps>            A variable-frame-rate source (every screen recording, most
#                           phone footage) has no single frame duration, so burned captions
#                           drift against speech as the effective rate wanders.
#
#: Container/codec flags safe to apply to any encode, intermediate or final.
H264_COMPAT_ARGS: tuple[str, ...] = (
    "-pix_fmt", "yuv420p",
    "-profile:v", "high",
    "-level", "4.0",
)


def h264_args(*, normalise_fps: bool = False) -> list[str]:
    """The standard libx264 arguments for an encode.

    ``normalise_fps`` adds ``-r`` at :data:`config.settings.output_fps`, forcing constant
    frame rate. It is off by default because an intermediate that is about to be re-encoded
    gains nothing from being resampled twice, and on for anything a user receives.
    """
    args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *H264_COMPAT_ARGS]
    if normalise_fps:
        args += ["-r", str(int(settings.output_fps))]
    return args


# Common target aspect ratios keyed by the UI values, mapped to (w, h) at a
# canonical short-form resolution.
ASPECT_PRESETS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "4:5": (1080, 1350),
}


@dataclass
class MediaInfo:
    """Basic probed metadata for a media file."""

    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def _default_timeout(cmd: list[str]) -> float:
    """The configured ceiling for ``cmd``, chosen by which binary it invokes.

    ffprobe reads metadata and gets the short ceiling; anything else is treated as an
    encode and gets the long one. The comparison is on the *basename* so an absolute
    ``/usr/bin/ffprobe`` is classified the same as a bare ``ffprobe``.
    """
    if not cmd:
        return float(settings.ffmpeg_timeout_seconds)
    probe_name = Path(str(settings.ffprobe_binary)).name
    if Path(str(cmd[0])).name == probe_name:
        return float(settings.ffprobe_timeout_seconds)
    return float(settings.ffmpeg_timeout_seconds)


def _run(
    cmd: list[str], *, timeout: Optional[float] = None
) -> subprocess.CompletedProcess:
    """Run a command, returning the completed process or raising ``FFmpegError``.

    Every invocation is bounded. Jobs are processed by a thread pool with a single
    worker, so an ffmpeg that never exits would block the whole queue forever —
    silently, because a hung process produces neither output nor an exception. On
    expiry ``subprocess.run`` kills the child and reaps it, and the overrun is
    reported as an :class:`FFmpegError` so callers already handling failure need no
    change.

    Args:
        cmd: argv to execute.
        timeout: Ceiling in seconds. ``None`` uses :func:`_default_timeout`; a value
            ``<= 0`` means unbounded, which is the documented opt-out.

    Raises:
        FFmpegError: the binary is missing, the command failed, or it timed out.
    """
    limit = _default_timeout(cmd) if timeout is None else float(timeout)
    try:
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=limit if limit > 0 else None,
        )
    except FileNotFoundError as exc:  # binary missing
        raise FFmpegError(f"Binary not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        # The stderr captured so far is the only clue as to where it stalled, so it
        # is surfaced exactly like a non-zero exit is. It may be bytes or str
        # depending on where the timeout struck, hence the decode dance.
        raw = exc.stderr or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        tail = raw.strip().splitlines()[-15:]
        detail = (": " + "\n".join(tail)) if tail else ""
        raise FFmpegError(
            f"Command timed out after {limit:g}s ({' '.join(cmd[:2])} ...){detail}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "").strip().splitlines()[-15:]
        raise FFmpegError(
            f"Command failed ({' '.join(cmd[:2])} ...): " + "\n".join(tail)
        ) from exc
    return proc


def probe(path: str | Path) -> MediaInfo:
    """Return :class:`MediaInfo` for ``path`` via ffprobe.

    Args:
        path: Path to the media file.

    Raises:
        FFmpegError: if the file cannot be probed.
    """
    cmd = [
        settings.ffprobe_binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = _run(cmd)
    data = json.loads(proc.stdout or "{}")

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise FFmpegError(f"No video stream found in {path}")

    # Duration can live on the format or stream; prefer format.
    duration = float(fmt.get("duration") or video.get("duration") or 0.0)

    # fps is expressed as a fraction like "30000/1001".
    fps = 0.0
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0"
    try:
        num, _, den = rate.partition("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    return MediaInfo(
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=round(fps, 3),
        has_audio=audio is not None,
    )


def cut_segment(
    source: str | Path,
    start: float,
    end: float,
    dest: str | Path,
    reencode: bool = True,
) -> Path:
    """Cut ``[start, end]`` (seconds) from ``source`` into ``dest``.

    Args:
        source: Input media path.
        start: Segment start in seconds.
        end: Segment end in seconds (must be > ``start``).
        dest: Output path (extension determines the container).
        reencode: When ``True`` (default) re-encode for frame-accurate cuts,
            which is what downstream captioning/reformatting needs. When
            ``False`` attempt a fast stream copy (keyframe-aligned, less exact).

    Returns:
        The ``dest`` path as a :class:`~pathlib.Path`.
    """
    if end <= start:
        raise ValueError(f"end ({end}) must be greater than start ({start})")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start

    cmd = [settings.ffmpeg_binary, "-y", "-ss", f"{start:.3f}", "-i", str(source),
           "-t", f"{duration:.3f}"]
    if reencode:
        cmd += [*h264_args(), "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", str(dest)]

    _run(cmd)
    return dest


def reformat_aspect(
    source: str | Path,
    dest: str | Path,
    aspect: str = "9:16",
    mode: str = "crop_blur",
) -> Path:
    """Reformat ``source`` to a target ``aspect`` ratio.

    Two strategies are supported:

    * ``crop_blur`` (default): the source is centre-cropped to fill the target
      frame; where the crop would leave empty bars, a scaled + blurred copy of
      the source is used as the background so the frame is always filled
      (Opus-Clip style). This is the recommended look for vertical clips.
    * ``pad``: the source is scaled to fit and letter/pillar-boxed with black.

    Args:
        source: Input clip path.
        dest: Output path.
        aspect: One of :data:`ASPECT_PRESETS` keys (e.g. ``"9:16"``).
        mode: ``"crop_blur"`` or ``"pad"``.

    Returns:
        The ``dest`` path.
    """
    if aspect not in ASPECT_PRESETS:
        raise ValueError(
            f"Unknown aspect '{aspect}'. Valid: {sorted(ASPECT_PRESETS)}"
        )
    tw, th = ASPECT_PRESETS[aspect]
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if mode == "pad":
        # Scale to fit inside the target, then pad with black.
        vf = (
            f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
    elif mode == "crop_blur":
        # Background: cover the frame with a zoomed, blurred copy.
        # Foreground: scale to fit fully inside, overlaid centred.
        # The two are combined with a filter graph via split.
        vf = (
            f"split=2[bg][fg];"
            f"[bg]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},boxblur=luma_radius=40:luma_power=1,"
            f"eq=brightness=-0.1[bgb];"
            f"[fg]scale={tw}:{th}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    else:
        raise ValueError(f"Unknown mode '{mode}'. Valid: 'crop_blur', 'pad'.")

    cmd = [
        settings.ffmpeg_binary, "-y", "-i", str(source),
        "-vf", vf,
        *h264_args(),
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(dest),
    ]
    _run(cmd)
    return dest


def extract_audio(source: str | Path, dest: str | Path, sample_rate: int = 16000) -> Path:
    """Extract a mono WAV suitable for transcription/silence analysis.

    Args:
        source: Input media.
        dest: Output ``.wav`` path.
        sample_rate: Target sample rate (16 kHz is ideal for whisper).
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_binary, "-y", "-i", str(source),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(dest),
    ]
    _run(cmd)
    return dest


def generate_thumbnail(
    source: str | Path, dest: str | Path, at: float = 0.0, width: int = 640
) -> Path:
    """Write a single JPEG thumbnail from ``source`` at time ``at`` seconds."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_binary, "-y", "-ss", f"{max(at, 0):.3f}", "-i", str(source),
        "-frames:v", "1", "-vf", f"scale={width}:-2", str(dest),
    ]
    _run(cmd)
    return dest
