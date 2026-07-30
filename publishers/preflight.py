"""Validate a clip against a platform's constraints before uploading it (O10).

The only pre-flight before this was ``video_path.exists()``. Nothing checked aspect,
duration, resolution, file size, codec or frame rate, so a clip a platform will refuse was
discovered by uploading it: the request fails somewhere inside a third-party API, the
failure surfaces as whatever that API chose to say, and the user is left reading a rejection
from TikTok rather than a sentence explaining that their clip is nine minutes long.

Two decisions worth stating.

**Errors block, warnings do not.** A clip that is definitely unacceptable - wrong codec, over
a duration limit - should not consume an upload attempt and a rate-limit slot to find out.
Something merely unusual, like a 4:5 clip going to a platform that prefers 9:16, is the
user's call: they may know exactly what they are doing, and refusing to publish it would be
us overruling them on taste.

**The limits are conservative approximations, not a specification.** Platform limits change
without notice, differ by account tier and by upload route, and are not always documented.
They are collected here as data so they can be corrected in one place, and every one is
deliberately looser than the strictest figure a platform advertises, so a *false* rejection -
blocking something that would have worked - is unlikely. Related: the publishers themselves
have never run against a live platform (PB1), so nothing here has been confirmed against a
real rejection. Treat it as a guard against obvious mistakes, not as certification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from worker.ffmpeg_utils import FFmpegError, MediaInfo, probe

#: Codecs every target platform accepts. Anything else is an error rather than a warning:
#: no platform in this list transcodes an unexpected codec on ingest.
_ACCEPTED_VIDEO_CODECS = frozenset({"h264", "hevc"})
_ACCEPTED_AUDIO_CODECS = frozenset({"aac", "mp3"})


@dataclass(frozen=True)
class PlatformLimits:
    """What a platform will accept. See the module docstring on how exact these are."""

    name: str
    min_duration_s: float
    max_duration_s: float
    max_file_mb: float
    #: Aspect ratios the platform is designed around, as ``width / height``. A clip outside
    #: these is a warning: it will publish, and it will be letterboxed or cropped by the
    #: platform's own player.
    preferred_aspects: tuple[float, ...]
    max_fps: float = 60.0
    #: Below this the clip is unwatchable on a modern phone regardless of what the platform
    #: technically accepts.
    min_width: int = 480


#: 9:16 vertical, 1:1 square, 4:5 portrait, 16:9 landscape - the four this tool can produce.
_VERTICAL = 1080 / 1920
_SQUARE = 1.0
_PORTRAIT_4_5 = 1080 / 1350
_LANDSCAPE = 1920 / 1080

PLATFORM_LIMITS: dict[str, PlatformLimits] = {
    # Short-form vertical feeds. Duration ceilings are the short-form product's, not the
    # platform's absolute maximum, because that is what this tool produces.
    "tiktok": PlatformLimits(
        name="tiktok",
        min_duration_s=1.0,
        max_duration_s=600.0,
        max_file_mb=500.0,
        preferred_aspects=(_VERTICAL,),
    ),
    "instagram": PlatformLimits(
        name="instagram",
        min_duration_s=3.0,
        max_duration_s=90.0,
        max_file_mb=1000.0,
        preferred_aspects=(_VERTICAL, _PORTRAIT_4_5, _SQUARE),
    ),
    "youtube": PlatformLimits(
        name="youtube",
        min_duration_s=1.0,
        # Shorts are capped at 60 s; a longer clip is still a valid YouTube upload, so this
        # is the generous reading rather than the Shorts one.
        max_duration_s=900.0,
        max_file_mb=2000.0,
        preferred_aspects=(_VERTICAL, _LANDSCAPE, _SQUARE),
    ),
    "x": PlatformLimits(
        name="x",
        min_duration_s=0.5,
        max_duration_s=140.0,
        max_file_mb=512.0,
        preferred_aspects=(_VERTICAL, _LANDSCAPE, _SQUARE),
    ),
    "whop": PlatformLimits(
        name="whop",
        min_duration_s=0.5,
        max_duration_s=900.0,
        max_file_mb=1000.0,
        preferred_aspects=(_VERTICAL, _LANDSCAPE, _SQUARE),
    ),
}

#: Used for any platform without an entry: permissive on everything except the checks that
#: are about the file being broken rather than about a platform's policy.
_FALLBACK_LIMITS = PlatformLimits(
    name="generic",
    min_duration_s=0.5,
    max_duration_s=3600.0,
    max_file_mb=4000.0,
    preferred_aspects=(_VERTICAL, _PORTRAIT_4_5, _SQUARE, _LANDSCAPE),
)

#: How far an aspect ratio may drift from a preferred one before it is worth mentioning.
#: 2% absorbs rounding in odd resolutions (1078x1920 and the like) without absorbing a real
#: difference: 9:16 and 4:5 are 0.5625 and 0.8, nowhere near each other.
_ASPECT_TOLERANCE = 0.02


def limits_for(platform: str) -> PlatformLimits:
    """The limits for ``platform``, or the permissive fallback."""
    return PLATFORM_LIMITS.get((platform or "").strip().lower(), _FALLBACK_LIMITS)


@dataclass
class PreflightReport:
    """The outcome of validating one clip for one platform."""

    platform: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: Optional[MediaInfo] = None

    @property
    def ok(self) -> bool:
        """Whether the upload should proceed. Warnings do not block."""
        return not self.errors

    def summary(self) -> str:
        """One line naming every problem, for a failure record a human will read."""
        parts = [f"{self.platform}: "]
        if self.errors:
            parts.append("; ".join(self.errors))
        elif self.warnings:
            parts.append("; ".join(self.warnings))
        else:
            parts.append("ok")
        return "".join(parts)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_clip(video_path: str | Path, platform: str) -> PreflightReport:
    """Check ``video_path`` against ``platform``'s constraints (O10).

    Never raises: a clip that cannot be probed is reported as an error, because publishing a
    file ffprobe cannot read is not going to go well either. Nothing here mutates the file or
    talks to a network.
    """
    path = Path(video_path)
    report = PreflightReport(platform=(platform or "generic").strip().lower() or "generic")
    limits = limits_for(platform)

    if not path.exists():
        report.errors.append("clip file does not exist")
        return report
    if path.stat().st_size == 0:
        report.errors.append("clip file is empty")
        return report

    try:
        info = probe(path)
    except (FFmpegError, Exception) as exc:  # noqa: BLE001 - a report, not a raise
        report.errors.append(f"clip could not be probed: {exc}")
        return report
    report.info = info

    # --- duration ----------------------------------------------------------
    if info.duration <= 0:
        report.errors.append("clip has no measurable duration")
    elif info.duration < limits.min_duration_s:
        report.errors.append(
            f"clip is {info.duration:.2f}s, below {limits.name}'s "
            f"{limits.min_duration_s:g}s minimum"
        )
    elif info.duration > limits.max_duration_s:
        report.errors.append(
            f"clip is {info.duration:.1f}s, above {limits.name}'s "
            f"{limits.max_duration_s:g}s maximum"
        )

    # --- file size ---------------------------------------------------------
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > limits.max_file_mb:
        report.errors.append(
            f"clip is {size_mb:.0f} MB, above {limits.name}'s {limits.max_file_mb:g} MB limit"
        )

    # --- codecs ------------------------------------------------------------
    if info.video_codec and info.video_codec not in _ACCEPTED_VIDEO_CODECS:
        report.errors.append(
            f"video codec {info.video_codec!r} is not accepted (expected one of "
            f"{sorted(_ACCEPTED_VIDEO_CODECS)})"
        )
    if not info.has_audio:
        # Not fatal anywhere, but almost always a mistake in a clip built from speech.
        report.warnings.append("clip has no audio track")
    elif info.audio_codec and info.audio_codec not in _ACCEPTED_AUDIO_CODECS:
        report.errors.append(
            f"audio codec {info.audio_codec!r} is not accepted (expected one of "
            f"{sorted(_ACCEPTED_AUDIO_CODECS)})"
        )

    # --- geometry ----------------------------------------------------------
    if info.width <= 0 or info.height <= 0:
        report.errors.append("clip has no usable video dimensions")
    else:
        if info.width < limits.min_width:
            report.warnings.append(
                f"clip is {info.width}x{info.height}, narrower than the {limits.min_width}px "
                "minimum worth publishing"
            )
        aspect = info.width / info.height
        if not any(abs(aspect - want) <= _ASPECT_TOLERANCE for want in limits.preferred_aspects):
            report.warnings.append(
                f"aspect {aspect:.3f} ({info.width}x{info.height}) is not one "
                f"{limits.name} is designed around; it will be cropped or letterboxed"
            )

    # --- frame rate --------------------------------------------------------
    if info.fps > limits.max_fps:
        report.warnings.append(
            f"clip is {info.fps:g} fps, above {limits.name}'s {limits.max_fps:g} fps; it will "
            "be resampled"
        )

    return report
