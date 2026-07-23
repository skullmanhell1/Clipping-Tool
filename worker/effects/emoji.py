"""Emoji overlays.

Selects contextually relevant emoji (via the LLM client) and composites the
corresponding Twemoji PNGs (from ``settings.emoji_assets_dir``) onto clips.

STUB ONLY.
"""

from __future__ import annotations

from pathlib import Path

from config import settings


def suggest_emoji(text: str, limit: int = 3) -> list[str]:
    """Return up to ``limit`` emoji suggestions relevant to ``text``.

    TODO(phase-emoji): prompt the LLM client for emoji suggestions.
    """
    raise NotImplementedError


def overlay_emoji(video: str | Path, emoji: list[str], dest: str | Path) -> Path:
    """Composite Twemoji PNGs for ``emoji`` onto ``video``.

    TODO(phase-emoji): resolve codepoints to PNG assets and overlay via ffmpeg.
    """
    raise NotImplementedError
