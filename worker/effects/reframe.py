"""Face-aware vertical reframing.

Detects and tracks faces (mediapipe + opencv) across a clip and produces a
smooth crop path so a horizontal clip can be reframed to vertical (9:16) while
keeping the speaker centred.

STUB ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class CropWindow:
    """A crop rectangle for a given timestamp."""

    t: float
    x: int
    y: int
    w: int
    h: int


def track_faces(video: str | Path) -> list[CropWindow]:
    """Return a per-frame/keyframe crop path that follows the main face(s).

    TODO(phase-reframe): run mediapipe face detection with temporal smoothing.
    """
    raise NotImplementedError


def apply_reframe(
    video: str | Path, crop_path: list[CropWindow], dest: str | Path
) -> Path:
    """Apply a computed ``crop_path`` to reframe ``video`` to vertical.

    TODO(phase-reframe): translate crop windows into ffmpeg crop expressions.
    """
    raise NotImplementedError
