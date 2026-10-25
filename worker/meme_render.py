"""FFmpeg graph builders for meme-mode events.

This module only builds graph fragments and resolves local assets.  The compositor still owns the
single ffmpeg invocation, input-index ordering, output codecs, and aspect-ratio profile behavior.
Missing local assets and unavailable optional filters become markers and no-ops.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import settings
from worker import meme_assets, meme_mode
from worker.effects import audio
from worker.ffmpeg_utils import _run

logger = logging.getLogger(__name__)


@dataclass
class MemeVideoPlan:
    filters: list[str] = field(default_factory=list)
    graph: str = ""
    output_label: str = "0:v"
    markers: list[str] = field(default_factory=list)


@dataclass
class MemeOverlayPlan:
    input_args: list[str] = field(default_factory=list)
    graph: str = ""
    output_label: str = ""
    markers: list[str] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MemeAudioPlan:
    input_args: list[str] = field(default_factory=list)
    graph: str = ""
    output_label: str = ""
    changed: bool = False
    markers: list[str] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)


def _available(filter_name: str) -> bool:
    """Probe a filter when possible; inability to probe should not disable an optional effect."""
    try:
        from worker.engines.capabilities import get_report

        return bool(get_report().status(f"ffmpeg_filter:{filter_name}").available)
    except Exception:
        return True


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _enable(event: meme_mode.MemeEvent) -> str:
    return f"enable='between(t,{event.start:.3f},{event.end:.3f})'"


def _safe_drawtext(value: Any) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _position(payload: dict[str, Any]) -> tuple[str, str]:
    position = str(payload.get("position") or "center").lower()
    named = {
        "top_left": ("W*0.06", "H*0.08"),
        "top_right": ("W-w-W*0.06", "H*0.08"),
        "bottom_left": ("W*0.06", "H-h-H*0.12"),
        "bottom_right": ("W-w-W*0.06", "H-h-H*0.12"),
        "top": ("(W-w)/2", "H*0.08"),
        "bottom": ("(W-w)/2", "H-h-H*0.12"),
        "left": ("W*0.06", "(H-h)/2"),
        "right": ("W-w-W*0.06", "(H-h)/2"),
        "center": ("(W-w)/2", "(H-h)/2"),
    }
    if position in named:
        return named[position]
    x = _finite(payload.get("x"), 0.5)
    y = _finite(payload.get("y"), 0.5)
    return (f"(W-w)*{max(0.0, min(1.0, x)):.4f}", f"(H-h)*{max(0.0, min(1.0, y)):.4f}")


def build_video_plan(
    events: Sequence[meme_mode.MemeEvent | dict[str, Any]],
    *,
    duration: float,
    fps: float,
    width: int,
    height: int,
    input_label: str = "0:v",
) -> MemeVideoPlan:
    """Build linear visual filters and an exact-duration freeze-frame graph."""
    parsed = meme_mode.normalize_events(events, duration)
    plan = MemeVideoPlan(output_label=input_label)
    freeze = next(
        (
            event
            for event in parsed
            if event.kind == "visual" and event.effect in {"freeze", "freeze_frame"}
        ),
        None,
    )
    if freeze is not None and _available("tpad"):
        # Replace the selected interval with its first frame while preserving total duration. This
        # avoids changing audio/caption timing, unlike a naive stop_mode=clone tail pad.
        span = max(0.05, freeze.end - freeze.start)
        plan.graph = (
            f"[{input_label}]split=3[meme_pre][meme_hold][meme_post];"
            f"[meme_pre]trim=start=0:end={freeze.start:.3f},setpts=PTS-STARTPTS[meme_pre_t];"
            f"[meme_hold]trim=start={freeze.start:.3f}:end={freeze.end:.3f},"
            f"setpts=PTS-STARTPTS,select='eq(n,0)',tpad=stop_mode=clone:stop_duration={span:.3f},"
            f"trim=duration={span:.3f},setpts=PTS-STARTPTS[meme_hold_t];"
            f"[meme_post]trim=start={freeze.end:.3f},setpts=PTS-STARTPTS[meme_post_t];"
            f"[meme_pre_t][meme_hold_t][meme_post_t]concat=n=3:v=1:a=0,"
            f"setpts=N/FRAME_RATE/TB[meme_freeze]"
        )
        plan.output_label = "meme_freeze"
        plan.markers.append("meme:freeze_frame")
    elif freeze is not None:
        plan.markers.append("meme_degraded:freeze_frame")

    for event in parsed:
        effect = event.effect
        enabled = _enable(event)
        if event.kind != "visual" or effect in {"freeze", "freeze_frame"}:
            continue
        if effect in {"zoom", "zoom_punch", "punch_in"}:
            if not _available("scale"):
                plan.markers.append("meme_degraded:zoom_punch")
                continue
            scale = max(0.02, min(0.5, _finite(event.payload.get("amount"), 0.16)))
            factor = f"(1+{scale:.3f}*between(t,{event.start:.3f},{event.end:.3f}))"
            plan.filters.append(
                f"scale=w='ceil({width}*{factor}/2)*2':h='ceil({height}*{factor}/2)*2':eval=frame,"
                f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2"
            )
            plan.markers.append("meme:zoom_punch")
        elif effect == "shake":
            if not _available("crop"):
                plan.markers.append("meme_degraded:shake")
                continue
            amplitude = max(1.0, min(width * 0.08, _finite(event.payload.get("amount"), 16.0)))
            plan.filters.append(
                f"scale={int(width * 1.08) // 2 * 2}:{int(height * 1.08) // 2 * 2},"
                f"crop={width}:{height}:x='(iw-ow)/2+sin(t*55)*{amplitude:.1f}*between(t,{event.start:.3f},{event.end:.3f})':"
                f"y='(ih-oh)/2+cos(t*47)*{amplitude:.1f}*between(t,{event.start:.3f},{event.end:.3f})'"
            )
            plan.markers.append("meme:shake")
        elif effect == "flash":
            if not _available("eq"):
                plan.markers.append("meme_degraded:flash")
                continue
            amount = max(0.0, min(1.0, _finite(event.payload.get("amount"), 0.35)))
            plan.filters.append(
                f"eq=brightness='if(between(t,{event.start:.3f},{event.end:.3f}),{amount:.3f},0)':eval=frame"
            )
            plan.markers.append("meme:flash")
        elif effect in {"glitch", "glitch_vhs", "vhs"}:
            if not _available("noise"):
                plan.markers.append("meme_degraded:glitch_vhs")
                continue
            plan.filters.append(f"noise=alls=10:allf=t+u:{enabled}")
            if _available("chromashift"):
                plan.filters.append(f"chromashift=cbh=2:crh=-2:{enabled}")
            plan.markers.append("meme:glitch_vhs")
        elif effect in {"chromatic_aberration", "chromatic"}:
            if not _available("chromashift"):
                plan.markers.append("meme_degraded:chromatic_aberration")
                continue
            plan.filters.append(f"chromashift=cbh=3:crh=-3:{enabled}")
            plan.markers.append("meme:chromatic_aberration")
        elif effect in {"slow_motion", "slowmo"}:
            if not _available("setpts"):
                plan.markers.append("meme_degraded:slow_motion")
                continue
            speed = max(0.25, min(1.0, _finite(event.payload.get("speed"), 0.5)))
            plan.filters.append(
                f"setpts=PTS*(1+({1.0 / speed - 1.0:.4f})*between(T,{event.start:.3f},{event.end:.3f}))"
            )
            plan.markers.append("meme:slow_motion")
    # The graph label is fed into the same look/caption chain by the compositor.
    return plan


def _overlay_asset(
    event: meme_mode.MemeEvent, library: meme_assets.MemeAssetLibrary
) -> meme_assets.MemeAsset | None:
    tags = event.payload.get("tags", ())
    if isinstance(tags, str):
        tags = tuple(part.strip() for part in tags.split(",") if part.strip())
    requested = event.asset_id or event.payload.get("asset") or event.payload.get("path", "")
    direct = Path(str(requested)).expanduser() if requested else Path()
    if direct.is_file() and direct.suffix.lower() in meme_assets.OVERLAY_EXTENSIONS:
        return meme_assets.MemeAsset(
            id=direct.stem,
            name=direct.stem,
            path=direct,
            kind="overlay",
            license="unknown",
            provenance="user_supplied",
        )
    return library.find(requested, tags=tuple(tags or ()))


def build_overlay_plan(
    events: Sequence[meme_mode.MemeEvent | dict[str, Any]],
    *,
    duration: float,
    width: int,
    height: int,
    input_offset: int,
    base_label: str,
    asset_library: str | Path | None = None,
) -> MemeOverlayPlan:
    """Build timed text/image/GIF/video overlays below the logo layer."""
    parsed = meme_mode.normalize_events(events, duration)
    library = meme_assets.MemeAssetLibrary(
        asset_library or getattr(settings, "broll_dir", ""), kind="overlay"
    )
    plan = MemeOverlayPlan(output_label=base_label)
    current = base_label
    input_index = input_offset
    overlay_index = 0
    for event in parsed:
        if event.kind not in {"overlay", "caption", "emoji", "sticker"}:
            continue
        payload = event.payload
        x, y = _position(payload)
        out = f"meme_overlay_{overlay_index}"
        text = (
            payload.get("text")
            if event.kind in {"caption", "emoji"} or event.effect == "text"
            else None
        )
        if text is not None:
            safe = _safe_drawtext(text)
            font_size = max(
                12, int(height * max(0.025, min(0.2, _finite(payload.get("scale"), 0.08))))
            )
            color = _safe_drawtext(payload.get("color") or "white")
            border = max(0, int(_finite(payload.get("border", 4), 4)))
            animate = str(payload.get("animate") or "").lower()
            size_expr = str(font_size)
            if animate == "pop":
                size_expr = f"{font_size}*(0.75+0.25*min(1,max(0,(t-{event.start:.3f})/0.12)))"
            filter_text = (
                f"drawtext=text='{safe}':fontcolor={color}:fontsize='{size_expr}':borderw={border}:"
                f"bordercolor=black@0.75:x={x}:y={y}:{_enable(event)}"
            )
            graph = f"[{current}]{filter_text}[{out}]"
            plan.markers.append(f"meme:{'caption' if event.kind == 'caption' else 'text_overlay'}")
        else:
            asset = _overlay_asset(event, library)
            if asset is None or not asset.path.is_file():
                plan.markers.append(f"meme_asset_missing:{event.asset_id or event.effect}")
                continue
            is_image = asset.path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            if is_image:
                plan.input_args += [
                    "-loop",
                    "1",
                    "-t",
                    f"{max(0.1, duration):.3f}",
                    "-i",
                    str(asset.path),
                ]
            else:
                plan.input_args += [
                    "-stream_loop",
                    "-1",
                    "-t",
                    f"{max(0.1, duration):.3f}",
                    "-i",
                    str(asset.path),
                ]
            scale = max(0.02, min(1.0, _finite(payload.get("scale"), 0.25)))
            graph = (
                f"[{input_index}:v]setpts=PTS-STARTPTS,scale=w='iw*{scale:.4f}':h=-1:eval=init,"
                f"format=rgba[meme_asset_{overlay_index}];"
                f"[{current}][meme_asset_{overlay_index}]overlay=x={x}:y={y}:{_enable(event)}[{out}]"
            )
            plan.assets.append(asset.to_record())
            plan.markers.append(f"meme:overlay:{event.effect}")
            input_index += 1
        plan.graph = f"{plan.graph};{graph}" if plan.graph else graph
        current = out
        overlay_index += 1
    plan.output_label = current
    return plan


def _materialize_synthetic(effect: meme_assets.SyntheticEffect, temp_dir: Path) -> Path | None:
    dest = temp_dir / f"meme_sfx_{effect.name}.wav"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                settings.ffmpeg_binary,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                effect.lavfi,
                "-ar",
                "48000",
                "-ac",
                "1",
                str(dest),
            ]
        )
        return dest if dest.is_file() and dest.stat().st_size > 0 else None
    except Exception:
        logger.warning("meme synthetic effect unavailable: %s", effect.name, exc_info=True)
        dest.unlink(missing_ok=True)
        return None


def build_audio_plan(
    events: Sequence[meme_mode.MemeEvent | dict[str, Any]],
    *,
    duration: float,
    temp_dir: str | Path,
    input_offset: int,
    speech_label: str,
    sfx_library: str | Path | None = None,
    default_volume: float = 0.35,
    default_duck: float = 0.0,
) -> MemeAudioPlan:
    """Build per-event SFX mixing with volume and short ducking windows."""
    parsed = meme_mode.normalize_events(events, duration)
    temp = Path(temp_dir)
    plan = MemeAudioPlan(output_label=speech_label)
    hits: list[tuple[meme_mode.MemeEvent, Path, dict[str, Any]]] = []
    for event in parsed:
        if event.kind not in {"sfx", "audio"}:
            continue
        name = event.asset_id or event.effect
        raw_tags = event.payload.get("tags", ()) or ()
        if isinstance(raw_tags, str):
            raw_tags = tuple(part.strip() for part in raw_tags.split(",") if part.strip())
        resolved = meme_assets.resolve_sfx(name, library_dir=sfx_library, tags=tuple(raw_tags))
        if resolved is None:
            plan.markers.append(f"meme_sfx_missing:{name}")
            continue
        if isinstance(resolved, meme_assets.SyntheticEffect):
            path = _materialize_synthetic(resolved, temp)
            record = resolved.to_record()
            if path is None:
                plan.markers.append(f"meme_sfx_missing:{name}")
                continue
        else:
            path = resolved.path
            record = resolved.to_record()
        if event.volume == 0.35 and default_volume != 0.35:
            event.volume = max(0.0, min(1.0, float(default_volume)))
        if event.duck == 0.0 and default_duck:
            event.duck = max(0.0, min(1.0, float(default_duck)))
        hits.append((event, path, record))
        plan.assets.append(record)
    if not hits:
        return plan

    audio_label = speech_label
    duck_windows = [
        (event.start, max(event.end, event.start + 0.08)) for event, _, _ in hits if event.duck > 0
    ]
    duck_amount = max((event.duck for event, _, _ in hits), default=0.0)
    if duck_windows and duck_amount > 0:
        plan.graph = audio.broll_duck_filter(
            audio_label, "meme_ducked", duck_windows, amount=duck_amount, ramp=0.04
        )
        audio_label = "meme_ducked"

    labels: list[str] = []
    for index, (event, path, _) in enumerate(hits):
        plan.input_args += ["-i", str(path)]
        label = f"meme_sfx_{index}"
        volume = max(0.0, min(1.0, event.volume))
        plan.graph += (";" if plan.graph else "") + (
            f"[{input_offset + index}:a]adelay={int(round(max(0.0, event.start) * 1000))}:all=1,"
            f"volume={volume:.3f}[{label}]"
        )
        labels.append(f"[{label}]")
    plan.graph += (";" if plan.graph else "") + (
        f"[{audio_label}]{''.join(labels)}amix=inputs={len(labels) + 1}:duration=first:"
        f"dropout_transition=0:normalize=0[meme_audio]"
    )
    plan.output_label = "meme_audio"
    plan.changed = True
    plan.markers.append(f"meme:sfx:{len(hits)}")
    if duck_windows:
        plan.markers.append(f"meme:sfx_ducked:{len(duck_windows)}")
    return plan


__all__ = [
    "MemeAudioPlan",
    "MemeOverlayPlan",
    "MemeVideoPlan",
    "build_audio_plan",
    "build_overlay_plan",
    "build_video_plan",
]
