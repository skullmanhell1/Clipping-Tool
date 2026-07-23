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


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command, returning the completed process or raising ``FFmpegError``."""
    try:
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:  # binary missing
        raise FFmpegError(f"Binary not found: {cmd[0]}") from exc
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
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k"]
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
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
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
