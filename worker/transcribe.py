"""Transcription via faster-whisper.

Produces segment- and word-level timestamps used downstream for segmentation
and caption rendering. Runs on CPU by default (model size configurable via
``settings.whisper_model``) and uses a GPU automatically when available and
requested (``settings.whisper_device``).

The whisper model is loaded lazily and cached process-wide, since loading is
expensive relative to inference.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import settings


@dataclass
class Word:
    """A single transcribed word with timing."""

    start: float
    end: float
    text: str
    probability: float = 1.0


@dataclass
class TranscriptSegment:
    """A timestamped transcript segment containing zero or more words."""

    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    """A full transcript: language + ordered segments."""

    language: str
    segments: list[TranscriptSegment] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Return the full transcript text (segments joined by spaces)."""
        return " ".join(s.text.strip() for s in self.segments).strip()

    @property
    def words(self) -> list[Word]:
        """Return a flat list of every word across all segments."""
        return [w for s in self.segments for w in s.words]


# --- lazy model cache -------------------------------------------------------
_model_lock = threading.Lock()
_model_cache: dict[tuple[str, str, str], object] = {}


def _resolve_device() -> tuple[str, str]:
    """Resolve the (device, compute_type) pair from settings.

    ``auto`` attempts CUDA and falls back to CPU. Returns a tuple suitable for
    passing to ``WhisperModel``.
    """
    device = settings.whisper_device
    compute_type = settings.whisper_compute_type

    if device == "auto":
        try:  # pragma: no cover - depends on host hardware
            import torch  # type: ignore

            if torch.cuda.is_available():
                return "cuda", "float16"
        except Exception:
            pass
        return "cpu", "int8"
    return device, compute_type


def _get_model():
    """Load (and cache) the configured faster-whisper model."""
    from faster_whisper import WhisperModel

    device, compute_type = _resolve_device()
    key = (settings.whisper_model, device, compute_type)
    with _model_lock:
        model = _model_cache.get(key)
        if model is None:
            model = WhisperModel(
                settings.whisper_model,
                device=device,
                compute_type=compute_type,
            )
            _model_cache[key] = model
    return model


def transcribe(
    audio_or_video: str | Path,
    language: Optional[str] = None,
    translate: bool = False,
    beam_size: int = 5,
) -> Transcript:
    """Transcribe ``audio_or_video`` and return a :class:`Transcript`.

    Args:
        audio_or_video: Path to a media file. faster-whisper decodes audio
            directly (via its bundled ffmpeg bindings), so a video file works.
        language: ISO code (e.g. ``"en"``) to force, or ``None`` to auto-detect.
        translate: When ``True``, translate speech to English instead of
            transcribing in the source language.
        beam_size: Decoder beam size (higher = slightly better/slower).

    Returns:
        A :class:`Transcript` with segment- and word-level timing.
    """
    model = _get_model()
    task = "translate" if translate else "transcribe"

    segments_iter, info = model.transcribe(
        str(audio_or_video),
        language=language,
        task=task,
        beam_size=beam_size,
        word_timestamps=True,
        vad_filter=True,
    )

    segments: list[TranscriptSegment] = []
    for seg in segments_iter:
        words = [
            Word(
                start=float(w.start),
                end=float(w.end),
                text=w.word,
                probability=float(getattr(w, "probability", 1.0) or 1.0),
            )
            for w in (seg.words or [])
            if w.start is not None and w.end is not None
        ]
        segments.append(
            TranscriptSegment(
                start=float(seg.start),
                end=float(seg.end),
                text=seg.text.strip(),
                words=words,
            )
        )

    return Transcript(language=info.language, segments=segments)
