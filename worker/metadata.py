"""Auto-generated clip metadata.

Uses the pluggable LLM client to generate titles, descriptions, and hashtags
tailored per destination platform.

STUB ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ClipMetadata:
    """Generated metadata for a single clip."""

    title: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)


def generate_metadata(transcript_text: str, platform: str = "generic") -> ClipMetadata:
    """Generate title/description/hashtags for ``transcript_text``.

    TODO(phase-metadata): prompt the LLM client and adapt output length/tone to
    the target ``platform``.
    """
    raise NotImplementedError
