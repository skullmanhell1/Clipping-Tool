"""AI-driven "best moment" selection.

Given a transcript, ask the pluggable LLM client to identify the most
engaging, self-contained moments and return candidate clip time ranges.

STUB ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass

from worker.transcribe import TranscriptSegment


@dataclass
class ClipCandidate:
    """A proposed clip time range with a relevance score and rationale."""

    start: float
    end: float
    score: float
    reason: str


def select_moments(
    segments: list[TranscriptSegment], max_clips: int = 10
) -> list[ClipCandidate]:
    """Return up to ``max_clips`` candidate clip ranges from ``segments``.

    TODO(phase-selection): prompt the LLM client with the transcript and parse
    structured candidate ranges from the response.
    """
    raise NotImplementedError
