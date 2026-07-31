"""Per-platform output profiles (O7).

Every clip was encoded identically and shipped to every destination: one resolution, one CRF,
one VBV ceiling, one duration. That is a reasonable default and a poor answer for any specific
platform - a 9:16 1080x1920 file is right for TikTok and Reels, wrong for a YouTube landscape
upload, and larger than X will accept at 140 seconds.

This module holds the *encode-side* profile: the resolution, aspect, bitrate ceiling and
duration limit a platform wants. It deliberately does **not** duplicate two tables that already
exist and are already the authority on their own concerns:

* :data:`publishers.preflight.PLATFORM_LIMITS` - what a platform will *accept* (validation, O10);
* :data:`worker.effects.audio.PLATFORM_LUFS` - the loudness target (AU1).

A profile is what we aim to *produce*; preflight is what is checked before upload. Keeping them
separate means a platform tightening its limits does not silently change how files are encoded,
and the duration ceiling below is read *from* preflight rather than restated, so the two cannot
disagree about the same number.

Scope, stated plainly: selecting a profile changes how the single clip is encoded. It does not
render N variants of every clip for N platforms. That is a job-model change - N outputs per
clip, N publish records, N thumbnails, N times the encode time - not an encoder setting, and
pretending otherwise by looping here would multiply every render silently.

What a profile controls, and what it does not:

* **resolution** and **bitrate ceiling** - controlled, because neither is exposed in the UI, so
  there is no user choice to override;
* **duration** - controlled, as a ceiling on clip length: a clip longer than the destination
  accepts fails at upload having already cost a full render;
* **aspect** - *advisory only*. It is reported by :func:`describe` so the UI can recommend one,
  and deliberately does not override the aspect a user picked. Silently re-shaping an explicit
  9:16 request into 16:9 because a platform was named is a setting that fights the interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import settings
from worker.ffmpeg_utils import OUTPUT_SHORT_SIDES

#: Bitrate ceilings in kbps, by short side.
#:
#: These are VBV caps, not targets: CRF still decides quality and the cap only stops a busy clip
#: (confetti, fast pans, grain, gameplay) ballooning past what a platform will take. Chosen from
#: the platforms' own recommended ranges for H.264 at these sizes, rounded down - a cap that sits
#: at the top of the recommendation is not a cap.
_BITRATE_BY_SHORT_SIDE: dict[int, int] = {
    720: 5_000,
    1080: 10_000,
    1440: 16_000,
    2160: 35_000,
}


@dataclass(frozen=True)
class OutputProfile:
    """How to encode for one destination."""

    name: str
    #: Aspect key into :data:`worker.ffmpeg_utils.ASPECT_PRESETS`.
    aspect: str
    #: Short side in pixels; must be one of :data:`OUTPUT_SHORT_SIDES`.
    short_side: int
    #: VBV ceiling in kbps.
    max_bitrate_kbps: int
    #: Longest clip this destination should be given, in seconds, or ``None`` for no opinion.
    max_duration_s: Optional[float]

    @property
    def size(self) -> tuple[int, int]:
        """``(width, height)`` for this profile's aspect at its short side."""
        from worker.ffmpeg_utils import aspect_size

        return aspect_size(self.aspect, self.short_side)

    def to_dict(self) -> dict:
        width, height = self.size
        return {
            "name": self.name,
            "aspect": self.aspect,
            "short_side": self.short_side,
            "width": width,
            "height": height,
            "max_bitrate_kbps": self.max_bitrate_kbps,
            "max_duration_s": self.max_duration_s,
        }


#: Duration ceilings for profiles whose *product* is stricter than the platform's upload limit.
#:
#: `youtube_shorts` is the case that forces this to exist. Preflight has no Shorts entry - and
#: correctly so, because a Shorts upload *is* a YouTube upload and is validated as one - so
#: reading the table gave the generic 3600 s fallback, i.e. a Shorts profile with a one-hour
#: ceiling. Shorts itself caps at 3 minutes, and that is what a clip aimed at Shorts must respect.
_DURATION_OVERRIDES: dict[str, float] = {
    "youtube_shorts": 180.0,
}


def _duration_ceiling(platform: str) -> Optional[float]:
    """The platform's own maximum duration.

    An override wins where the product is stricter than the upload limit; otherwise this is read
    from the preflight table so the two cannot disagree about the same number.

    Preflight is imported lazily and defensively: this module sits in the render path and the
    publisher package pulls in publisher clients. A missing or unreadable table costs the
    duration opinion, not the profile.
    """
    override = _DURATION_OVERRIDES.get(platform)
    if override is not None:
        return override
    try:
        from publishers.preflight import limits_for

        return float(limits_for(platform).max_duration_s)
    except Exception:  # pragma: no cover - defensive; the table is a plain dict literal
        return None


#: The aspect and resolution each platform is built around.
#:
#: `youtube` is the only landscape entry, and only because YouTube proper is a landscape product;
#: a Shorts upload wants the vertical profile, which is what `youtube_shorts` is for. Guessing
#: between them from a single "youtube" string would be wrong half the time, so both exist.
_PLATFORM_SHAPES: dict[str, tuple[str, int]] = {
    "tiktok": ("9:16", 1080),
    "instagram": ("9:16", 1080),
    "youtube_shorts": ("9:16", 1080),
    "youtube": ("16:9", 1080),
    "x": ("16:9", 720),
    "whop": ("9:16", 1080),
}


def profile_for(platform: str) -> Optional[OutputProfile]:
    """The output profile for ``platform``, or ``None`` when there is no entry.

    ``None`` means "no platform-specific opinion", and every caller treats that as "use the
    configured settings" - which is the behaviour before O7 and remains the default.
    """
    key = (platform or "").strip().lower()
    shape = _PLATFORM_SHAPES.get(key)
    if shape is None:
        return None
    aspect, short_side = shape
    return OutputProfile(
        name=key,
        aspect=aspect,
        short_side=short_side,
        max_bitrate_kbps=_BITRATE_BY_SHORT_SIDE.get(
            short_side, int(settings.output_max_bitrate_kbps)
        ),
        max_duration_s=_duration_ceiling(key),
    )


def active_profile() -> Optional[OutputProfile]:
    """The profile named by ``settings.output_platform``, or ``None`` when unset."""
    return profile_for(str(getattr(settings, "output_platform", "") or ""))


def _is_untouched(field_name: str) -> bool:
    """Whether ``settings.<field_name>`` still holds its declared default.

    This is how a profile and an explicit setting coexist: the profile supplies a value only
    while the operator has expressed no preference. Comparing against the *declared* default
    read from the model - rather than against a literal repeated here - matters, because those
    two drift. Written first with a hardcoded ``10_000`` for the bitrate, which is wrong: the
    field's default is 12000, so every install would have looked "explicitly configured" and the
    profile's ceiling would never have applied.
    """
    try:
        field = type(settings).model_fields[field_name]
    except (AttributeError, KeyError):  # pragma: no cover - defensive
        return False
    return getattr(settings, field_name, None) == field.default


def resolve_short_side() -> int:
    """The output short side: the profile's, unless the operator set one explicitly."""
    configured = int(getattr(settings, "output_short_side", 1080) or 1080)
    profile = active_profile()
    if profile is None or not _is_untouched("output_short_side"):
        return configured
    return profile.short_side if profile.short_side in OUTPUT_SHORT_SIDES else configured


def resolve_max_bitrate_kbps() -> int:
    """The VBV ceiling: the profile's, unless the operator set one explicitly."""
    configured = int(getattr(settings, "output_max_bitrate_kbps", 12_000) or 12_000)
    profile = active_profile()
    if profile is None or not _is_untouched("output_max_bitrate_kbps"):
        return configured
    return profile.max_bitrate_kbps


def duration_ceiling_s() -> Optional[float]:
    """The active profile's duration ceiling, or ``None``."""
    profile = active_profile()
    return None if profile is None else profile.max_duration_s


def describe() -> dict:
    """The active profile as a dict for the API/UI, or ``{}`` when none is active."""
    profile = active_profile()
    if profile is None:
        return {}
    described = profile.to_dict()
    described["effective_short_side"] = resolve_short_side()
    described["effective_max_bitrate_kbps"] = resolve_max_bitrate_kbps()
    return described
