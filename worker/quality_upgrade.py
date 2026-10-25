"""Opt-in quality and delivery planning hooks.

The module provides small, isolated plans that the pipeline consumes only when a job explicitly opts
in to a quality preset or per-job delivery control. Every optional filter is capability-checked and
every refusal is represented by a stable marker rather than by a failed render.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, TypeAlias

from config import settings
from worker import frame_rate, output_profiles, video_encoders
from worker.ffmpeg_utils import aspect_size

ProgressCallback: TypeAlias = Callable[["Progress_Metadata"], None]
CapabilityProber: TypeAlias = Callable[[str], Any]


@dataclass(frozen=True)
class QualityPreset:
    """One opt-in output quality target."""

    name: str
    short_side: int
    target_fps: int
    max_bitrate_kbps: int
    crf: int

    @property
    def enabled(self) -> bool:
        return self.short_side > 0

    def size(self, aspect: str) -> tuple[int, int] | None:
        if not self.enabled:
            return None
        try:
            return aspect_size(aspect, self.short_side)
        except (KeyError, ValueError, TypeError):
            return None


QUALITY_PRESETS: dict[str, QualityPreset] = {
    "off": QualityPreset("off", 0, 0, 0, 0),
    "720p": QualityPreset("720p", 720, 30, 5_000, 22),
    "1080p": QualityPreset("1080p", 1080, 30, 10_000, 20),
    "1440p": QualityPreset("1440p", 1440, 60, 16_000, 19),
    "4k": QualityPreset("4k", 2160, 60, 35_000, 18),
}


def _normalise_preset_name(value: object) -> str:
    text = str(value or "off").strip().lower().replace(" ", "")
    aliases = {
        "720": "720p",
        "1080": "1080p",
        "1440": "1440p",
        "2160": "4k",
        "4k": "4k",
        "uhd": "4k",
    }
    return aliases.get(text, text)


def resolve_quality_preset(requested: str | None = None) -> QualityPreset:
    """Resolve a preset without raising; unknown values safely become ``off``."""
    value = requested if requested is not None else getattr(settings, "quality_preset", "off")
    return QUALITY_PRESETS.get(_normalise_preset_name(value), QUALITY_PRESETS["off"])


def available_quality_presets() -> list[dict[str, object]]:
    """Return preset metadata suitable for a settings surface or job record."""
    return [
        {
            "name": preset.name,
            "short_side": preset.short_side or None,
            "target_fps": preset.target_fps or None,
            "max_bitrate_kbps": preset.max_bitrate_kbps or None,
            "crf": preset.crf or None,
        }
        for preset in QUALITY_PRESETS.values()
    ]


@dataclass(frozen=True)
class Progress_Metadata:
    """Structured progress for an expensive optional step."""

    stage: str
    fraction: float
    message: str
    slow: bool = False
    expected_seconds: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fraction", max(0.0, min(1.0, float(self.fraction))))
        if self.expected_seconds is not None:
            object.__setattr__(self, "expected_seconds", max(0.0, float(self.expected_seconds)))

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def emit_progress(
    callback: ProgressCallback | None,
    stage: str,
    fraction: float,
    message: str,
    *,
    slow: bool = False,
    expected_seconds: float | None = None,
) -> Progress_Metadata:
    """Create and optionally deliver one progress event."""
    event = Progress_Metadata(stage, fraction, message, slow, expected_seconds)
    if callback is not None:
        try:
            callback(event)
        except Exception:
            # Progress is observability, never a reason to fail a render.
            pass
    return event


def _filter_available(name: str, prober: CapabilityProber | None = None) -> bool:
    """Ask the shared capability report whether an ffmpeg filter is usable."""
    try:
        from worker.engines.capabilities import Capability_Report, get_report

        report = Capability_Report(prober) if prober is not None else get_report()
        return bool(report.status(f"ffmpeg_filter:{name}").available)
    except Exception:
        # Capability discovery is optional. Existing ffmpeg builds have historically been allowed
        # to decide at render time, so an unavailable capability report must not disable a feature
        # pre-emptively; the compositor will surface a real ffmpeg failure/degradation marker.
        return True


@dataclass(frozen=True)
class Filter_Plan:
    filters: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()

    @property
    def chain(self) -> str:
        return ",".join(self.filters)


_DENOISE_FILTERS: dict[str, str] = {
    "light": "hqdn3d=1.2:1.2:3:3",
    "standard": "hqdn3d=2:2:5:5",
    "strong": "hqdn3d=3:3:7:7",
}


def video_enhancement_plan(
    *,
    denoise: str | None = None,
    sharpen: float | None = None,
    colour_correction: bool | None = None,
    prober: CapabilityProber | None = None,
) -> Filter_Plan:
    """Build conservative, opt-in video cleanup filters.

    Unknown strengths are treated as ``standard`` only when a caller explicitly supplied a
    non-off value. A missing filter produces a ``quality_degraded:*`` marker and an empty step.
    """
    requested_denoise = (
        str(getattr(settings, "quality_denoise", "off") if denoise is None else denoise)
        .strip()
        .lower()
    )
    try:
        requested_sharpen = float(
            getattr(settings, "quality_sharpen", 0.0) if sharpen is None else sharpen
        )
    except (TypeError, ValueError):
        requested_sharpen = 0.0
    requested_colour = (
        bool(getattr(settings, "quality_colour_correction", False))
        if colour_correction is None
        else bool(colour_correction)
    )
    filters: list[str] = []
    markers: list[str] = []

    if requested_denoise not in ("", "off", "none"):
        denoise_filter = _DENOISE_FILTERS.get(requested_denoise, _DENOISE_FILTERS["standard"])
        if _filter_available("hqdn3d", prober):
            filters.append(denoise_filter)
            markers.append(f"quality_denoise:{requested_denoise}")
        else:
            markers.append("quality_degraded:ffmpeg_filter:hqdn3d")

    try:
        amount = max(0.0, min(1.0, requested_sharpen))
    except (TypeError, ValueError):
        amount = 0.0
    if amount > 0.0:
        if _filter_available("unsharp", prober):
            filters.append(f"unsharp=5:5:{amount:.2f}:5:5:0")
            markers.append(f"quality_sharpen:{amount:.2f}")
        else:
            markers.append("quality_degraded:ffmpeg_filter:unsharp")

    if requested_colour:
        if _filter_available("eq", prober):
            # Small values only: this is correction for delivery, not a creative grade.
            filters.append("eq=contrast=1.02:saturation=1.04:brightness=0")
            markers.append("quality_colour_correction:mild")
        else:
            markers.append("quality_degraded:ffmpeg_filter:eq")

    return Filter_Plan(tuple(filters), tuple(markers))


def smart_scale_crop_filter(
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
    *,
    mode: str = "smart",
    prevent_upscale: bool = False,
    scaler_flags: str = "bicubic",
) -> str:
    """Return an aspect-safe ffmpeg scale/crop or fit/pad fragment.

    ``smart`` fits when the source is already close to the target aspect and crops when the target
    is materially narrower/wider. The output dimensions stay fixed and even. ``prevent_upscale``
    is opt-in because a fixed-size social export normally needs to fill its canvas.
    """
    tw, th = max(2, int(target_width)), max(2, int(target_height))
    tw -= tw % 2
    th -= th % 2
    sw, sh = max(1, int(source_width)), max(1, int(source_height))
    selected = str(mode or "smart").strip().lower()
    if selected not in {"crop", "fit", "smart"}:
        selected = "crop"
    flags = str(scaler_flags or "bicubic").strip().lower()
    if flags not in {"bilinear", "bicubic", "lanczos", "area", "neighbor", "spline"}:
        flags = "bicubic"
    if selected == "smart":
        source_ratio = sw / float(sh)
        target_ratio = tw / float(th)
        # A 3% aspect mismatch is usually container rounding or a camera pixel-aspect quirk.
        selected = (
            "fit"
            if abs(source_ratio - target_ratio) / max(source_ratio, target_ratio) <= 0.03
            else "crop"
        )

    if selected == "fit":
        scale = f"scale={tw}:{th}:flags={flags}:force_original_aspect_ratio=decrease"
        if prevent_upscale and sw <= tw and sh <= th:
            scale = f"scale={sw - (sw % 2)}:{sh - (sh % 2)}:flags={flags}:force_original_aspect_ratio=decrease"
        return f"{scale},pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"

    scale = f"scale={tw}:{th}:flags={flags}:force_original_aspect_ratio=increase"
    if prevent_upscale and sw <= tw and sh <= th:
        # Keep the source pixels and letterbox rather than inventing detail.
        return f"scale={sw - (sw % 2)}:{sh - (sh % 2)}:flags={flags}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    return f"{scale},crop={tw}:{th},setsar=1"


@dataclass(frozen=True)
class Frame_Delivery_Plan:
    rate: frame_rate.Rate_Plan
    interpolation_filter: str = ""
    markers: tuple[str, ...] = ()

    @property
    def filters(self) -> tuple[str, ...]:
        return (self.interpolation_filter,) if self.interpolation_filter else ()


def build_frame_delivery_plan(
    *,
    avg_fps: float,
    base_fps: float,
    target_fps: int | None = None,
    interpolation: bool | None = None,
    ceiling_fps: int | None = None,
    frame_rate_policy: str | None = None,
    prober: CapabilityProber | None = None,
) -> Frame_Delivery_Plan:
    """Combine the existing CFR/VFR policy with opt-in interpolation."""
    configured = int(
        target_fps or getattr(settings, "quality_target_fps", 0) or settings.output_fps
    )
    if configured not in (30, 60):
        configured = int(settings.output_fps)
    if ceiling_fps is None:
        try:
            profile = output_profiles.active_profile()
            ceiling_fps = profile.max_fps if profile is not None else None
        except Exception:
            ceiling_fps = None
    always = (
        str(
            getattr(settings, "frame_rate_policy", "auto")
            if frame_rate_policy is None
            else frame_rate_policy
        )
        .strip()
        .lower()
        == "always"
    )
    rate = frame_rate.plan_frame_rate(
        avg_fps=float(avg_fps),
        base_fps=float(base_fps),
        configured_fps=configured,
        always_normalise=always,
        ceiling_fps=ceiling_fps,
    )
    want_interpolation = (
        bool(getattr(settings, "quality_interpolation", False))
        if interpolation is None
        else bool(interpolation)
    )
    markers = [rate.marker] if rate.marker else []
    interpolation_filter = ""
    if want_interpolation:
        if avg_fps <= 0:
            markers.append("frame_interpolation_degraded:source_rate_unknown")
        elif rate.delivered_fps <= math.ceil(avg_fps + 0.01):
            markers.append("frame_interpolation_skipped:target_not_higher")
        elif not _filter_available("minterpolate", prober):
            markers.append("frame_interpolation_degraded:ffmpeg_filter:minterpolate")
        else:
            interpolation_filter = (
                f"minterpolate=fps={int(rate.delivered_fps)}:mi_mode=mci:"
                "mc_mode=aobmc:me_mode=bidir:vsbmc=1"
            )
            markers.append(f"frame_interpolation:{int(rate.delivered_fps)}")
    return Frame_Delivery_Plan(rate, interpolation_filter, tuple(markers))


@dataclass(frozen=True)
class Audio_Upgrade_Plan:
    filters: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    requires_measurement: bool = False

    @property
    def chain(self) -> str:
        return ",".join(self.filters)


def build_audio_upgrade_plan(
    *,
    platform: str = "",
    denoise: str | None = None,
    loudness: bool | None = None,
    voice_clarity: bool | None = None,
    loudness_stats: Any | None = None,
    prober: CapabilityProber | None = None,
) -> Audio_Upgrade_Plan:
    """Build optional denoise, measured loudness and voice-clarity audio steps."""
    from worker.effects import audio

    denoise_value = (
        getattr(settings, "quality_audio_denoise", "off") if denoise is None else denoise
    )
    want_loudness = (
        bool(getattr(settings, "quality_audio_loudness", False))
        if loudness is None
        else bool(loudness)
    )
    want_clarity = (
        bool(getattr(settings, "quality_voice_clarity", False))
        if voice_clarity is None
        else bool(voice_clarity)
    )
    filters: list[str] = []
    markers: list[str] = []
    denoise_filter = audio.denoise_filter(str(denoise_value))
    if denoise_filter:
        if _filter_available("arnndn" if "arnndn" in denoise_filter else "afftdn", prober):
            filters.append(denoise_filter)
            markers.append(f"quality_audio_denoise:{str(denoise_value).strip().lower()}")
        else:
            markers.append("quality_degraded:audio_denoise_filter")
    if want_clarity:
        presence = audio.presence_chain(1.0, prober=prober)
        if presence:
            filters.extend(presence)
            markers.append("quality_voice_clarity:standard")
        else:
            markers.append("quality_degraded:voice_clarity_filters")
    if want_loudness:
        if loudness_stats is None:
            markers.append("quality_audio_loudness_degraded:measurement_unavailable")
        else:
            target = audio.platform_loudness_target(platform)
            filters.append(audio.loudnorm_filter(loudness_stats, target))
            markers.append(f"quality_audio_loudness:{target:g}lufs")
        if getattr(settings, "true_peak_limit_enabled", True):
            if _filter_available("alimiter", prober):
                filters.append(audio.true_peak_limit_filter())
            else:
                markers.append("quality_degraded:ffmpeg_filter:alimiter")
    return Audio_Upgrade_Plan(
        tuple(filters), tuple(markers), want_loudness and loudness_stats is None
    )


@dataclass(frozen=True)
class Platform_Encoding_Plan:
    platform: str
    video_codec: str
    audio_codec: str
    video_bitrate_kbps: int
    audio_bitrate_kbps: int
    max_fps: int
    encoder: str
    encoder_kind: str
    markers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_platform_encoding_plan(
    platform: str | None = None,
    *,
    preset: QualityPreset | None = None,
    requested_encoder: str | None = None,
) -> Platform_Encoding_Plan:
    """Resolve platform bitrate/codec policy and record CPU fallback honestly."""
    name = str(platform if platform is not None else getattr(settings, "output_platform", "") or "")
    profile = output_profiles.profile_for(name)
    chosen = video_encoders.resolve_encoder(requested_encoder)
    target = preset or resolve_quality_preset()
    video_bitrate = (
        profile.max_bitrate_kbps
        if profile is not None
        else target.max_bitrate_kbps or int(getattr(settings, "output_max_bitrate_kbps", 12000))
    )
    configured_audio = int(getattr(settings, "audio_bitrate_kbps", 128) or 128)
    audio_bitrate = (
        min(configured_audio, int(profile.max_audio_bitrate_kbps))
        if profile is not None
        else configured_audio
    )
    max_fps = profile.max_fps if profile is not None else 60
    requested = (
        str(
            requested_encoder
            if requested_encoder is not None
            else getattr(settings, "video_encoder", "libx264") or "libx264"
        )
        .strip()
        .lower()
    )
    markers: list[str] = []
    if chosen.marker:
        markers.append(chosen.marker)
    elif requested == "auto" and not chosen.encoder.hardware:
        markers.append("encoder_fallback:cpu")
    return Platform_Encoding_Plan(
        platform=name.lower(),
        video_codec=profile.video_codec if profile is not None else "h264",
        audio_codec=profile.audio_codec if profile is not None else "aac",
        video_bitrate_kbps=int(video_bitrate),
        audio_bitrate_kbps=int(audio_bitrate),
        max_fps=int(max_fps),
        encoder=chosen.encoder.name,
        encoder_kind=chosen.encoder.kind,
        markers=tuple(markers),
    )


def _media_value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _safe_number(value: Any, *, integer: bool = False) -> int | float:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError):
        parsed = 0.0
    if not math.isfinite(parsed):
        parsed = 0.0
    return int(parsed) if integer else round(parsed, 4)


def _media_snapshot(source: Any) -> dict[str, object]:
    width = int(_safe_number(_media_value(source, "width", 0), integer=True))
    height = int(_safe_number(_media_value(source, "height", 0), integer=True))
    return {
        "width": width,
        "height": height,
        "short_side": min(width, height) if width and height else 0,
        "pixels": width * height,
        "fps": _safe_number(_media_value(source, "fps", 0.0)),
        "duration": _safe_number(_media_value(source, "duration", 0.0)),
        "video_codec": str(_media_value(source, "video_codec", "") or ""),
        "audio_codec": str(_media_value(source, "audio_codec", "") or ""),
        "has_audio": bool(_media_value(source, "has_audio", False)),
        "size_bytes": int(_safe_number(_media_value(source, "size_bytes", 0), integer=True)),
    }


def before_after_quality_metadata(
    before: Any,
    after: Any,
    *,
    preset: str | None = None,
    markers: Sequence[str] = (),
) -> dict[str, object]:
    """Create serialisable before/after media metadata for a clip record."""
    before_snapshot = _media_snapshot(before)
    after_snapshot = _media_snapshot(after)
    delta: dict[str, object] = {}
    for key in ("width", "height", "short_side", "pixels", "fps", "duration", "size_bytes"):
        old, new = before_snapshot[key], after_snapshot[key]
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            delta[key] = new - old
    return {
        "preset": _normalise_preset_name(preset if preset is not None else settings.quality_preset),
        "before": before_snapshot,
        "after": after_snapshot,
        "delta": delta,
        "markers": list(dict.fromkeys(str(marker) for marker in markers if marker)),
    }


# Short alias for callers that prefer the noun used in job records.
quality_metadata = before_after_quality_metadata


@dataclass(frozen=True)
class Quality_Plan:
    preset: QualityPreset
    target_size: tuple[int, int] | None = None
    scale_filter: str = ""
    video_filters: tuple[str, ...] = ()
    audio_filters: tuple[str, ...] = ()
    frame_plan: Frame_Delivery_Plan | None = None
    encoding: Platform_Encoding_Plan | None = None
    markers: tuple[str, ...] = ()
    progress: tuple[Progress_Metadata, ...] = ()
    detail: str = ""

    @property
    def enabled(self) -> bool:
        return self.preset.enabled or bool(
            self.video_filters or self.audio_filters or self.scale_filter
        )

    @property
    def video_chain(self) -> str:
        parts = [self.scale_filter, *self.video_filters]
        if self.frame_plan is not None:
            parts.extend(self.frame_plan.filters)
        return ",".join(part for part in parts if part)

    @property
    def audio_chain(self) -> str:
        return ",".join(self.audio_filters)

    def to_dict(self) -> dict[str, object]:
        return {
            "preset": self.preset.name,
            "target_size": self.target_size,
            "video_chain": self.video_chain,
            "audio_chain": self.audio_chain,
            "markers": list(self.markers),
            "progress": [event.to_dict() for event in self.progress],
            "frame_plan": (
                {
                    "rate": self.frame_plan.rate.to_dict(),
                    "interpolation_filter": self.frame_plan.interpolation_filter,
                    "markers": list(self.frame_plan.markers),
                }
                if self.frame_plan
                else None
            ),
            "encoding": self.encoding.to_dict() if self.encoding else None,
            "detail": self.detail,
        }


def quality_video_args(
    plan: Quality_Plan,
    *,
    normalise_fps: bool = False,
    delivered_fps: int | None = None,
    keyframe_seconds: float | None = None,
    colour_tags: Sequence[str] = (),
) -> list[str]:
    """Adapt a quality plan to the shared ``ffmpeg_utils.h264_args`` seam.

    The helper is additive: an inert plan returns the legacy settings-driven arguments. A caller
    that has selected a quality preset gets the preset's CRF, resolved encoder and platform VBV
    ceiling, while hardware unavailability still falls through to the encoder resolver's CPU
    fallback.
    """
    from worker.ffmpeg_utils import h264_args

    encoding = plan.encoding
    if encoding is None or not plan.preset.enabled:
        return h264_args(
            normalise_fps=normalise_fps,
            delivered_fps=delivered_fps,
            keyframe_seconds=keyframe_seconds,
            colour_tags=colour_tags,
        )
    return h264_args(
        normalise_fps=normalise_fps,
        delivered_fps=delivered_fps,
        keyframe_seconds=keyframe_seconds,
        colour_tags=colour_tags,
        encoder_name=encoding.encoder,
        crf=plan.preset.crf if plan.preset.enabled else None,
        max_bitrate_kbps=encoding.video_bitrate_kbps,
        vbv_cap=plan.preset.enabled,
    )


def build_quality_plan(
    *,
    aspect: str = "9:16",
    source_width: int = 0,
    source_height: int = 0,
    avg_fps: float = 0.0,
    base_fps: float = 0.0,
    platform: str | None = None,
    preset: str | None = None,
    smart_scale: bool | None = None,
    crop_mode: str | None = None,
    denoise: str | None = None,
    sharpen: float | None = None,
    colour_correction: bool | None = None,
    target_fps: int | None = None,
    interpolation: bool | None = None,
    audio_denoise: str | None = None,
    audio_loudness: bool | None = None,
    voice_clarity: bool | None = None,
    requested_encoder: str | None = None,
    frame_rate_policy: str | None = None,
    scaler_flags: str | None = None,
    progress: ProgressCallback | None = None,
    prober: CapabilityProber | None = None,
) -> Quality_Plan:
    """Build the complete opt-in quality plan without invoking ffmpeg."""
    selected = resolve_quality_preset(preset)
    events: list[Progress_Metadata] = []
    markers: list[str] = []
    requested_name = _normalise_preset_name(
        preset if preset is not None else getattr(settings, "quality_preset", "off")
    )
    if requested_name not in QUALITY_PRESETS:
        markers.append(f"quality_preset_degraded:unknown:{requested_name}")
    progress_sink = progress
    if progress_sink is None and not bool(getattr(settings, "quality_progress_metadata", False)):
        progress_sink = None
    try:
        sharpen_enabled = (
            float(getattr(settings, "quality_sharpen", 0.0) if sharpen is None else sharpen) > 0
        )
    except (TypeError, ValueError):
        sharpen_enabled = False
    requested_denoise = getattr(settings, "quality_denoise", "off") if denoise is None else denoise
    requested_audio_denoise = (
        getattr(settings, "quality_audio_denoise", "off")
        if audio_denoise is None
        else audio_denoise
    )
    requested_loudness = (
        bool(getattr(settings, "quality_audio_loudness", False))
        if audio_loudness is None
        else bool(audio_loudness)
    )
    requested_clarity = (
        bool(getattr(settings, "quality_voice_clarity", False))
        if voice_clarity is None
        else bool(voice_clarity)
    )
    requested_smart_scale = (
        bool(getattr(settings, "quality_smart_scale", False))
        if smart_scale is None
        else bool(smart_scale)
    )
    requested_target_fps = (
        int(getattr(settings, "quality_target_fps", 0) or 0)
        if target_fps is None
        else int(target_fps or 0)
    )
    requested_interpolation = (
        bool(getattr(settings, "quality_interpolation", False))
        if interpolation is None
        else bool(interpolation)
    )
    requested_frame_policy = (
        str(
            getattr(settings, "frame_rate_policy", "auto")
            if frame_rate_policy is None
            else frame_rate_policy
        )
        .strip()
        .lower()
    )
    requested_scaler_flags = (
        str(getattr(settings, "scaler_flags", "bicubic") if scaler_flags is None else scaler_flags)
        .strip()
        .lower()
    )
    requested_encoder_value = (
        str(
            getattr(settings, "video_encoder", "libx264")
            if requested_encoder is None
            else requested_encoder
        )
        .strip()
        .lower()
    )
    encoder_requested = requested_encoder_value not in {"", "libx264"}
    if not selected.enabled and not any(
        (
            requested_denoise not in ("", "off", "none"),
            sharpen_enabled,
            bool(
                getattr(settings, "quality_colour_correction", False)
                if colour_correction is None
                else colour_correction
            ),
            requested_audio_denoise not in ("", "off", "none"),
            requested_loudness,
            requested_clarity,
            requested_smart_scale,
            bool(requested_target_fps),
            requested_interpolation,
            requested_frame_policy == "always",
            encoder_requested,
        )
    ):
        return Quality_Plan(selected, markers=tuple(markers), detail="quality upgrade disabled")

    if progress_sink is not None or bool(getattr(settings, "quality_progress_metadata", False)):
        events.append(
            emit_progress(
                progress_sink, "quality_plan", 0.0, "Preparing quality upgrade", slow=False
            )
        )
    target_size = selected.size(aspect) if selected.enabled else None
    if selected.enabled and target_size is None:
        markers.append("quality_resolution_degraded:invalid_aspect")
    use_smart_scale = requested_smart_scale
    scale_filter = ""
    if target_size is not None:
        markers.append(f"quality_resolution:{target_size[0]}x{target_size[1]}")
    if target_size is not None:
        requested_mode = (
            "smart"
            if use_smart_scale
            else str(crop_mode or getattr(settings, "quality_crop_mode", "crop"))
        )
        if requested_mode.strip().lower() not in {"crop", "fit", "smart"}:
            markers.append("quality_scale_degraded:invalid_mode")
        if source_width <= 0 or source_height <= 0:
            markers.append("quality_scale_degraded:source_dimensions_unknown")
        else:
            scale_filter = smart_scale_crop_filter(
                source_width,
                source_height,
                target_size[0],
                target_size[1],
                mode=requested_mode,
                scaler_flags=requested_scaler_flags,
            )

    video_plan = video_enhancement_plan(
        denoise=denoise,
        sharpen=sharpen,
        colour_correction=colour_correction,
        prober=prober,
    )
    markers.extend(video_plan.markers)
    if progress_sink is not None or bool(getattr(settings, "quality_progress_metadata", False)):
        events.append(
            emit_progress(progress_sink, "quality_video", 0.35, "Planning video cleanup", slow=True)
        )
    frame_target = (
        requested_target_fps
        if requested_target_fps in (30, 60)
        else selected.target_fps
        if selected.enabled and selected.target_fps
        else None
    )
    frame_plan = build_frame_delivery_plan(
        avg_fps=avg_fps,
        base_fps=base_fps,
        target_fps=frame_target,
        interpolation=interpolation,
        frame_rate_policy=frame_rate_policy,
        prober=prober,
    )
    markers.extend(frame_plan.markers)
    if progress_sink is not None or bool(getattr(settings, "quality_progress_metadata", False)):
        events.append(
            emit_progress(
                progress_sink, "quality_frame_rate", 0.55, "Planning frame rate", slow=True
            )
        )
    audio_plan = build_audio_upgrade_plan(
        platform=platform or "",
        denoise=audio_denoise,
        loudness=audio_loudness,
        voice_clarity=voice_clarity,
        prober=prober,
    )
    markers.extend(audio_plan.markers)
    encoding = build_platform_encoding_plan(
        platform,
        preset=selected,
        requested_encoder=requested_encoder,
    )
    markers.extend(encoding.markers)
    if progress_sink is not None or bool(getattr(settings, "quality_progress_metadata", False)):
        events.append(
            emit_progress(progress_sink, "quality_plan", 1.0, "Quality upgrade planned", slow=False)
        )
    return Quality_Plan(
        preset=selected,
        target_size=target_size,
        scale_filter=scale_filter,
        video_filters=video_plan.filters,
        audio_filters=audio_plan.filters,
        frame_plan=frame_plan,
        encoding=encoding,
        markers=tuple(dict.fromkeys(marker for marker in markers if marker)),
        progress=tuple(events),
        detail="quality upgrade enabled",
    )


__all__ = [
    "Audio_Upgrade_Plan",
    "Filter_Plan",
    "Frame_Delivery_Plan",
    "Platform_Encoding_Plan",
    "Progress_Metadata",
    "QualityPreset",
    "Quality_Plan",
    "QUALITY_PRESETS",
    "available_quality_presets",
    "before_after_quality_metadata",
    "build_audio_upgrade_plan",
    "build_frame_delivery_plan",
    "build_platform_encoding_plan",
    "build_quality_plan",
    "emit_progress",
    "quality_metadata",
    "quality_video_args",
    "resolve_quality_preset",
    "smart_scale_crop_filter",
    "video_enhancement_plan",
]
