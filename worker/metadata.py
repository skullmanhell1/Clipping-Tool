"""AI-generated, per-platform clip metadata.

For each clip we ask the pluggable LLM client to write:
    * a punchy title plus a few alternatives
    * a description / caption
    * hashtags (configurable count)
    * an on-screen hook text
    * a call-to-action and optional @mentions
    * a thumbnail text idea

Output tone and length are tailored per platform, and hard limits (character
counts, hashtag counts) are enforced defensively after generation so the result
always fits the destination even if the model overshoots. A single field can be
regenerated in isolation via :func:`regenerate_field`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from worker.llm_client import BaseLLMClient, LLMError, get_llm_client, llm_available
from worker.models import ProcessingOptions


@dataclass
class PlatformProfile:
    """Per-platform tone and hard limits."""

    name: str
    title_max: int
    desc_max: int
    hashtag_max: int
    tone: str


# Sensible defaults per platform (limits are conservative, not exhaustive).
PLATFORM_PROFILES: dict[str, PlatformProfile] = {
    "generic": PlatformProfile("generic", 80, 500, 15, "engaging and clear"),
    "youtube": PlatformProfile("youtube", 100, 1000, 15,
                               "curiosity-driven, keyword-rich"),
    "tiktok": PlatformProfile("tiktok", 80, 300, 8, "punchy, casual, trend-aware"),
    "instagram": PlatformProfile("instagram", 80, 400, 12,
                                 "aesthetic, relatable, emoji-friendly"),
    "x": PlatformProfile("x", 70, 260, 4, "concise and witty"),
    "whop": PlatformProfile("whop", 90, 500, 10, "value-driven, community-focused"),
}

# The metadata fields a user (or the API) can regenerate individually.
REGENERATABLE_FIELDS = (
    "title",
    "title_alternatives",
    "description",
    "hashtags",
    "hook_text",
    "cta",
    "thumbnail_text",
)


@dataclass
class ClipMetadata:
    """Generated metadata for a single clip."""

    title: str = ""
    title_alternatives: list[str] = field(default_factory=list)
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    hook_text: str = ""
    cta: str = ""
    mentions: list[str] = field(default_factory=list)
    thumbnail_text: str = ""
    platform: str = "generic"


def get_profile(platform: str) -> PlatformProfile:
    """Return the :class:`PlatformProfile` for ``platform`` (falls back generic)."""
    return PLATFORM_PROFILES.get((platform or "generic").lower(),
                                 PLATFORM_PROFILES["generic"])


# --- normalisation helpers --------------------------------------------------

def _norm_hashtag(tag: str) -> str:
    """Normalise a single hashtag to ``#word`` form (no spaces/punctuation)."""
    tag = str(tag).strip().lstrip("#")
    tag = "".join(ch for ch in tag if ch.isalnum() or ch in ("_",))
    return f"#{tag}" if tag else ""


def _clamp_hashtags(tags: Any, count: int, cap: int) -> list[str]:
    """Normalise, de-duplicate, and limit hashtags to ``min(count, cap)``."""
    if not isinstance(tags, list):
        tags = []
    seen: list[str] = []
    for t in tags:
        h = _norm_hashtag(t)
        if h and h.lower() not in {s.lower() for s in seen}:
            seen.append(h)
    limit = max(0, min(count, cap))
    return seen[:limit]


def _clamp_text(text: Any, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters on a word boundary where possible."""
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    return (cut or s[:limit]).rstrip()


def _norm_mentions(mentions: Any) -> list[str]:
    out: list[str] = []
    if isinstance(mentions, list):
        for m in mentions:
            m = str(m).strip()
            if not m:
                continue
            out.append(m if m.startswith("@") else f"@{m}")
    return out


# --- prompt + generation ----------------------------------------------------

_SYSTEM = (
    "You are a viral social media copywriter. You write scroll-stopping titles, "
    "captions, and hashtags tailored to each platform's culture and limits."
)


def _build_prompt(
    transcript_text: str,
    options: ProcessingOptions,
    profile: PlatformProfile,
    hashtag_count: int,
) -> str:
    topic = f"Content focus / keywords: {options.topic}." if options.topic.strip() else ""
    vibe = f"Desired vibe/tone: {options.vibe}." if options.vibe.strip() else ""
    return f"""Write social copy for a short video clip whose transcript is:
\"\"\"{transcript_text.strip()[:2000]}\"\"\"

Target platform: {profile.name} (tone: {profile.tone}).
{topic} {vibe}

Constraints:
  - Title <= {profile.title_max} characters.
  - Description <= {profile.desc_max} characters.
  - Exactly {min(hashtag_count, profile.hashtag_max)} hashtags.

Return a JSON object with keys:
  - "title": string
  - "title_alternatives": array of 2-3 alternative titles
  - "description": string
  - "hashtags": array of strings (each starting with #)
  - "hook_text": a very short on-screen opening hook (<= 40 chars)
  - "cta": a short call to action
  - "mentions": array of suggested @handles (may be empty)
  - "thumbnail_text": 2-4 word idea for thumbnail text
Respond with JSON only.""".strip()


def _parse_metadata(
    data: dict,
    options: ProcessingOptions,
    profile: PlatformProfile,
    hashtag_count: int,
) -> ClipMetadata:
    """Convert a raw LLM JSON object into a limit-enforced :class:`ClipMetadata`."""
    alts = data.get("title_alternatives")
    if not isinstance(alts, list):
        alts = []
    return ClipMetadata(
        title=_clamp_text(data.get("title"), profile.title_max),
        title_alternatives=[_clamp_text(a, profile.title_max) for a in alts[:3] if str(a).strip()],
        description=_clamp_text(data.get("description"), profile.desc_max),
        hashtags=_clamp_hashtags(data.get("hashtags"), hashtag_count, profile.hashtag_max),
        hook_text=_clamp_text(data.get("hook_text"), 40),
        cta=_clamp_text(data.get("cta"), 120),
        mentions=_norm_mentions(data.get("mentions")),
        thumbnail_text=_clamp_text(data.get("thumbnail_text"), 30),
        platform=profile.name,
    )


def generate_metadata(
    transcript_text: str,
    options: ProcessingOptions,
    platform: Optional[str] = None,
    hashtag_count: Optional[int] = None,
    client: Optional[BaseLLMClient] = None,
) -> ClipMetadata:
    """Generate per-platform metadata for a clip transcript.

    Falls back to a minimal, deterministic metadata object when the LLM is not
    configured or fails, so the pipeline never breaks.
    """
    platform = platform or options.platform
    hashtag_count = options.hashtag_count if hashtag_count is None else hashtag_count
    profile = get_profile(platform)

    if client is None and not llm_available():
        return _fallback_metadata(transcript_text, profile)

    client = client or get_llm_client()
    prompt = _build_prompt(transcript_text, options, profile, hashtag_count)
    try:
        data = client.complete_json(prompt, system=_SYSTEM, max_tokens=800)
    except LLMError:
        return _fallback_metadata(transcript_text, profile)

    if not isinstance(data, dict):
        return _fallback_metadata(transcript_text, profile)
    return _parse_metadata(data, options, profile, hashtag_count)


def _fallback_metadata(transcript_text: str, profile: PlatformProfile) -> ClipMetadata:
    """Deterministic metadata when no LLM is available."""
    words = (transcript_text or "").split()
    title = _clamp_text(" ".join(words[:10]) or "Untitled clip", profile.title_max)
    return ClipMetadata(
        title=title,
        description=_clamp_text(transcript_text, profile.desc_max),
        hashtags=[],
        platform=profile.name,
    )


def regenerate_field(
    field_name: str,
    transcript_text: str,
    options: ProcessingOptions,
    platform: Optional[str] = None,
    hashtag_count: Optional[int] = None,
    client: Optional[BaseLLMClient] = None,
) -> Any:
    """Regenerate a single metadata field and return just that field's value.

    Raises ``ValueError`` for unknown fields. Delegates to
    :func:`generate_metadata` and extracts the requested field, which keeps the
    prompt cohesive while still letting the UI refresh one field at a time.
    """
    if field_name not in REGENERATABLE_FIELDS:
        raise ValueError(f"Cannot regenerate unknown field: {field_name}")
    meta = generate_metadata(transcript_text, options, platform, hashtag_count, client)
    return getattr(meta, field_name)
