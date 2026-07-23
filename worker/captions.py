"""Caption rendering.

Turns word-level transcript timing into burned-in, styled captions (karaoke
style highlighting, positioning, fonts). Rendering is performed via FFmpeg
filters / ASS subtitles.

STUB ONLY.
"""

from __future__ import annotations

from pathlib import Path

from worker.transcribe import TranscriptSegment


def build_subtitle_file(
    segments: list[TranscriptSegment], dest: str | Path
) -> Path:
    """Write an ASS/SRT subtitle file for ``segments`` to ``dest``.

    TODO(phase-captions): emit styled ASS with word-level timing.
    """
    raise NotImplementedError


def burn_captions(video: str | Path, subtitles: str | Path, dest: str | Path) -> Path:
    """Burn ``subtitles`` into ``video`` and write the result to ``dest``.

    TODO(phase-captions): run the ffmpeg subtitles filter.
    """
    raise NotImplementedError
