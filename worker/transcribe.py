"""Transcription via faster-whisper.

Produces word- and segment-level timestamps used downstream for AI moment
selection and caption rendering. Runs on CPU by default and uses a GPU when
available (see ``settings.whisper_device``).

STUB ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import settings


@dataclass
class TranscriptSegment:
    """A single timestamped transcript segment."""

    start: float
    end: float
    text: str


def transcribe(audio_or_video: str | Path) -> list[TranscriptSegment]:
    """Transcribe ``audio_or_video`` and return timestamped segments.

    TODO(phase-transcribe): load a faster-whisper model configured by
    ``settings.whisper_model`` / ``settings.whisper_device`` and run inference.
    """
    raise NotImplementedError
