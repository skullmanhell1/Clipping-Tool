"""Opt-in facial tracking planner built on the existing reframe primitives.

This module deliberately stops before invoking ffmpeg.  It gives the pipeline owner a stable hook
for confidence filtering, multi-face/active-speaker selection, smoothing and safe fallback.  The
existing ``worker.effects.reframe`` functions remain the compatibility implementation when this
planner is not requested.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from config import settings
from worker.effects import reframe
from worker.ffmpeg_utils import escape_filter_path


@dataclass(frozen=True)
class Facial_Tracking_Config:
    enabled: bool = False
    confidence_threshold: float = 0.5
    smoothing: float = 0.35
    multiple_faces: bool = False
    active_speaker: bool = False
    fallback: str = "static_crop_blur"
    coverage_floor: float = 0.35
    command_fps: float = 24.0

    @classmethod
    def from_settings(cls) -> Facial_Tracking_Config:
        return cls(
            enabled=bool(getattr(settings, "facial_tracking_upgrade", False)),
            confidence_threshold=_clamp01(
                getattr(settings, "facial_tracking_confidence_threshold", 0.5)
            ),
            smoothing=_clamp01(getattr(settings, "facial_tracking_smoothing", 0.35)),
            multiple_faces=bool(getattr(settings, "facial_tracking_multiple_faces", False)),
            active_speaker=bool(getattr(settings, "facial_tracking_active_speaker", False)),
            fallback=_normalise_fallback(getattr(settings, "facial_tracking_fallback", "")),
            coverage_floor=_clamp01(getattr(settings, "reframe_coverage_floor", 0.35)),
            command_fps=max(1.0, float(getattr(settings, "reframe_command_fps", 24.0) or 24.0)),
        )


@dataclass(frozen=True)
class Facial_Tracking_Plan:
    enabled: bool
    strategy: str
    tracks: tuple[reframe.Face_Track, ...] = ()
    selected_track_id: str | None = None
    selected_track_ids: tuple[str, ...] = ()
    centers: tuple[reframe.Center, ...] = ()
    coverage: float = 0.0
    markers: tuple[str, ...] = ()
    fallback_reason: str = ""
    config: Facial_Tracking_Config = Facial_Tracking_Config()

    @property
    def uses_fallback(self) -> bool:
        return self.strategy in {"static_crop_blur", "static_crop"}

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.centers) and not self.uses_fallback

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "strategy": self.strategy,
            "selected_track_id": self.selected_track_id,
            "selected_track_ids": list(self.selected_track_ids),
            "coverage": round(self.coverage, 4),
            "markers": list(self.markers),
            "fallback_reason": self.fallback_reason,
            "config": asdict(self.config),
        }


def _clamp01(value: object) -> float:
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return max(0.0, min(1.0, parsed))


def _normalise_fallback(value: object) -> str:
    choice = str(value or "static_crop_blur").strip().lower()
    return choice if choice in {"static_crop_blur", "static_crop", "none"} else "static_crop_blur"


def _coerce_face_box(item: object, *, time: float = 0.0) -> reframe.FaceBox | None:
    if isinstance(item, reframe.FaceBox):
        return item
    if isinstance(item, reframe.Detection):
        return reframe.FaceBox(time, item.x, item.y, item.w, item.h, item.score)
    try:
        if all(hasattr(item, attr) for attr in ("x", "y", "w", "h")):
            face = cast(Any, item)
            return reframe.FaceBox(
                float(getattr(face, "t", time)),
                int(face.x),
                int(face.y),
                int(face.w),
                int(face.h),
                getattr(face, "score", None),
            )
        x, y, w, h = cast(Any, item)
        return reframe.FaceBox(time, int(x), int(y), int(w), int(h), None)
    except (TypeError, ValueError):
        return None


def filter_face_boxes(
    boxes: Sequence[object], *, confidence_threshold: float = 0.5, time: float = 0.0
) -> list[reframe.FaceBox]:
    """Keep valid detections and discard only known low-confidence scores.

    Scoreless Haar boxes remain eligible because ``None`` means the backend has no confidence
    signal, not that the face is uncertain.
    """
    threshold = _clamp01(confidence_threshold)
    out: list[reframe.FaceBox] = []
    for item in boxes or ():
        box = _coerce_face_box(item, time=time)
        if box is None or box.w <= 0 or box.h <= 0:
            continue
        if box.score is not None and float(box.score) < threshold:
            continue
        out.append(box)
    return out


def prepare_face_frames(
    per_frame: Sequence[Sequence[object]],
    *,
    confidence_threshold: float | None = None,
    times: Sequence[float] | None = None,
) -> list[list[reframe.FaceBox]]:
    """Normalise arbitrary detector output into confidence-filtered ``FaceBox`` frames."""
    threshold = (
        Facial_Tracking_Config.from_settings().confidence_threshold
        if confidence_threshold is None
        else _clamp01(confidence_threshold)
    )
    output: list[list[reframe.FaceBox]] = []
    for index, boxes in enumerate(per_frame):
        time = float(times[index]) if times is not None and index < len(times) else float(index)
        output.append(filter_face_boxes(boxes, confidence_threshold=threshold, time=time))
    return output


def _track_score(track: reframe.Face_Track, start: float, end: float) -> float:
    presence = track.presence(start, end)
    if not track.boxes:
        return presence
    # A small confidence contribution breaks ties without allowing confidence to override time
    # coverage. Scoreless backends receive the neutral 0.5 term.
    known = [float(box.score) for box in track.boxes if box.score is not None]
    confidence = sum(known) / len(known) if known else 0.5
    return presence * (0.75 + 0.25 * _clamp01(confidence))


def select_active_face_track(
    tracks: Sequence[reframe.Face_Track],
    *,
    active_track_id: str | None = None,
    start: float = 0.0,
    end: float | None = None,
    speaker_turns: Sequence[Any] = (),
) -> reframe.Face_Track | None:
    """Select one face using an explicit active track, speaker overlap, then coverage/score."""
    usable = [track for track in tracks if track.boxes]
    if not usable:
        return None
    if active_track_id:
        for track in usable:
            if track.track_id == active_track_id:
                return track
    if end is None:
        end = max((box.t for track in usable for box in track.boxes), default=start) + 0.001
    if speaker_turns:
        # Aggregate overlap across the supplied active turns. This reuses the same presence
        # semantics as the existing speaker-aware reframe without requiring a diarizer here.
        scored = [
            (
                sum(
                    track.presence(
                        float(getattr(turn, "start", 0.0)), float(getattr(turn, "end", 0.0))
                    )
                    for turn in speaker_turns
                ),
                index,
                track,
            )
            for index, track in enumerate(usable)
        ]
    else:
        scored = [
            (_track_score(track, float(start), float(end)), index, track)
            for index, track in enumerate(usable)
        ]
    return max(scored, key=lambda item: (item[0], -item[1]))[2]


def rank_face_tracks(
    tracks: Sequence[reframe.Face_Track],
    *,
    start: float = 0.0,
    end: float | None = None,
    limit: int = 2,
) -> list[reframe.Face_Track]:
    """Return stable multi-face ranking for split-screen or active-speaker callers."""
    if end is None:
        end = max((box.t for track in tracks for box in track.boxes), default=start) + 0.001
    ranked = sorted(
        enumerate(tracks),
        key=lambda item: (-_track_score(item[1], float(start), float(end)), item[0]),
    )
    return [track for _index, track in ranked[: max(1, int(limit))] if track.boxes]


def track_centers(
    track: reframe.Face_Track,
    *,
    duration: float,
    command_fps: float,
    smoothing: float = 0.35,
) -> list[reframe.Center]:
    """Smooth and densify one track using the existing zero-phase reframe primitives."""
    boxes = reframe.smooth_bounding_boxes(track.boxes, alpha=_clamp01(smoothing))
    samples = [reframe.Center(float(box.t), *box.center) for box in boxes]
    if not samples:
        return []
    smoothed = reframe.smooth_centers(samples, alpha=_clamp01(smoothing), zero_phase=True)
    return reframe.resample_centers(
        smoothed, max(1.0, float(command_fps)), max(0.0, float(duration))
    )


def _fallback_plan(
    config: Facial_Tracking_Config,
    reason: str,
    *,
    enabled: bool,
    coverage: float = 0.0,
    tracks: Sequence[reframe.Face_Track] = (),
) -> Facial_Tracking_Plan:
    strategy = config.fallback
    marker = f"face_tracking_fallback:{reason}"
    markers = (marker,) if strategy != "none" else (marker, "face_tracking_disabled")
    return Facial_Tracking_Plan(
        enabled=enabled,
        strategy=strategy,
        tracks=tuple(tracks),
        coverage=max(0.0, min(1.0, float(coverage))),
        markers=markers,
        fallback_reason=reason,
        config=config,
    )


def plan_face_tracking(
    per_frame: Sequence[Sequence[object]],
    *,
    duration: float,
    source_width: int,
    source_height: int,
    crop_width: int,
    crop_height: int,
    config: Facial_Tracking_Config | None = None,
    active_track_id: str | None = None,
    speaker_turns: Sequence[Any] = (),
    sample_times: Sequence[float] | None = None,
) -> Facial_Tracking_Plan:
    """Build a tracking plan or an honest static-crop fallback."""
    cfg = config or Facial_Tracking_Config.from_settings()
    if not cfg.enabled:
        return Facial_Tracking_Plan(
            enabled=False,
            strategy="disabled",
            markers=(),
            fallback_reason="disabled",
            config=cfg,
        )
    if source_width <= 0 or source_height <= 0 or crop_width <= 0 or crop_height <= 0:
        return _fallback_plan(cfg, "invalid_geometry", enabled=True)

    frames = prepare_face_frames(
        per_frame,
        confidence_threshold=cfg.confidence_threshold,
        times=sample_times,
    )
    coverage = reframe.detection_coverage(
        [(float(index), boxes) for index, boxes in enumerate(frames)]
    )
    tracks = reframe.build_face_tracks(frames)
    if not tracks:
        return _fallback_plan(cfg, "no_detections", enabled=True, coverage=coverage)
    if coverage < cfg.coverage_floor:
        return _fallback_plan(cfg, "low_confidence", enabled=True, coverage=coverage, tracks=tracks)

    selected = select_active_face_track(
        tracks,
        active_track_id=active_track_id if cfg.active_speaker else None,
        end=duration,
        speaker_turns=speaker_turns if cfg.active_speaker else (),
    )
    if selected is None:
        return _fallback_plan(cfg, "no_active_face", enabled=True, coverage=coverage, tracks=tracks)
    centers = track_centers(
        selected,
        duration=duration,
        command_fps=cfg.command_fps,
        smoothing=cfg.smoothing,
    )
    if not centers:
        return _fallback_plan(
            cfg, "unsmoothed_path_empty", enabled=True, coverage=coverage, tracks=tracks
        )

    ranked = rank_face_tracks(tracks, end=duration, limit=2 if cfg.multiple_faces else 1)
    markers = [
        "face_tracking:enabled",
        f"face_tracking:selected:{selected.track_id}",
        f"face_tracking:coverage:{coverage:.2f}",
        f"face_tracking:smoothing:{cfg.smoothing:.2f}",
    ]
    if cfg.multiple_faces and len(ranked) > 1:
        markers.append(f"face_tracking:multiple_faces:{len(ranked)}")
    if cfg.active_speaker and speaker_turns:
        markers.append("face_tracking:active_speaker")
    return Facial_Tracking_Plan(
        enabled=True,
        strategy="tracked_crop",
        tracks=tuple(tracks),
        selected_track_id=selected.track_id,
        selected_track_ids=tuple(track.track_id for track in ranked),
        centers=tuple(centers),
        coverage=coverage,
        markers=tuple(markers),
        config=cfg,
    )


def write_tracking_commands(
    plan: Facial_Tracking_Plan,
    path: str | Path,
    *,
    crop_width: int,
    crop_height: int,
    source_width: int,
    source_height: int,
    origin_x: int = 0,
    origin_y: int = 0,
) -> Path | None:
    """Write a sendcmd script for a usable plan; return ``None`` for safe fallbacks."""
    if not plan.usable:
        return None
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    script = reframe.build_sendcmd(
        list(plan.centers),
        int(crop_width),
        int(crop_height),
        int(source_width),
        int(source_height),
        origin_x=int(origin_x),
        origin_y=int(origin_y),
    )
    destination.write_text(script, encoding="utf-8")
    return destination


def tracking_filter_fragment(
    plan: Facial_Tracking_Plan,
    command_path: str | Path,
    *,
    crop_width: int,
    crop_height: int,
    target_width: int,
    target_height: int,
    source_width: int | None = None,
    source_height: int | None = None,
    first_center: reframe.Center | None = None,
) -> str:
    """Return the video filter fragment for a plan, or ``""`` for static fallback."""
    if not plan.usable:
        return ""
    first = first_center or plan.centers[0]
    max_x = max(0, int(source_width or target_width) - int(crop_width))
    max_y = max(0, int(source_height or target_height) - int(crop_height))
    x0 = max(0, min(max_x, int(round(first.cx - crop_width / 2.0))))
    y0 = max(0, min(max_y, int(round(first.cy - crop_height / 2.0))))
    escaped = escape_filter_path(command_path)
    return (
        f"sendcmd=f='{escaped}',crop={int(crop_width)}:{int(crop_height)}:{x0}:{y0},"
        f"scale={int(target_width)}:{int(target_height)},setsar=1"
    )


def fallback_reformat_kwargs(plan: Facial_Tracking_Plan) -> dict[str, str]:
    """Return arguments for the existing static reformat fallback."""
    if plan.strategy == "static_crop":
        return {"mode": "pad", "background": "black"}
    if plan.strategy == "none":
        return {"mode": "pad", "background": "black"}
    return {"mode": "crop_blur", "background": "blur"}


def tracking_notes(plan: Facial_Tracking_Plan) -> list[str]:
    """Return de-duplicated markers suitable for ``effects_applied``."""
    return list(dict.fromkeys(plan.markers))


__all__ = [
    "Facial_Tracking_Config",
    "Facial_Tracking_Plan",
    "fallback_reformat_kwargs",
    "filter_face_boxes",
    "plan_face_tracking",
    "prepare_face_frames",
    "rank_face_tracks",
    "select_active_face_track",
    "tracking_filter_fragment",
    "tracking_notes",
    "track_centers",
    "write_tracking_commands",
]
