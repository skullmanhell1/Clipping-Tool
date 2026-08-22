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

import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from worker.llm_client import (
    UNTRUSTED_NOTICE,
    BaseLLMClient,
    LLMError,
    fence_untrusted,
    get_llm_client,
    llm_available,
)
from worker.models import ProcessingOptions

logger = logging.getLogger(__name__)


@dataclass
class PlatformProfile:
    """Per-platform tone and hard limits."""

    name: str
    title_max: int
    desc_max: int
    hashtag_max: int
    tone: str
    #: Total characters when the platform puts the title and the caption in **one** field.
    #:
    #: ``0`` means they are separate fields, which is the normal case. X is the exception:
    #: ``publishers/x.py`` posts ``f"{title}\n\n{caption}"[:280]``, so ``title_max`` and
    #: ``desc_max`` are two halves of one 280-character budget rather than two independent limits.
    #: They summed to 332, so a request that satisfied both fields was still chopped mid-word by
    #: the publisher — the exact failure ``publishers/tailoring`` exists to prevent, happening one
    #: layer below it because ``fit_caption`` budgeted against ``desc_max`` alone and never
    #: reserved the title it knew would be prepended.
    #:
    #: Appended last with a default so the positional construction below is unaffected.
    combined_max: int = 0


# Sensible defaults per platform (limits are conservative, not exhaustive).
PLATFORM_PROFILES: dict[str, PlatformProfile] = {
    "generic": PlatformProfile("generic", 80, 500, 15, "engaging and clear"),
    "youtube": PlatformProfile("youtube", 100, 1000, 15, "curiosity-driven, keyword-rich"),
    "tiktok": PlatformProfile("tiktok", 80, 300, 8, "punchy, casual, trend-aware"),
    "instagram": PlatformProfile("instagram", 80, 400, 12, "aesthetic, relatable, emoji-friendly"),
    # 280 is the whole tweet, title included. See PlatformProfile.combined_max.
    "x": PlatformProfile("x", 70, 260, 4, "concise and witty", combined_max=280),
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
    #: Why this is template output rather than generated, or ``""`` when the model produced it.
    #:
    #: `_fallback_metadata` returns a title that is literally the first ten words of the
    #: transcript and a description that is its first N raw characters — and it was
    #: **indistinguishable in shape** from a real generation, with nothing on the clip record to
    #: say which had happened. A user comparing "AI titles" across a job could not tell that some
    #: of them were the transcript's opening words, and the causes (no key, a truncated reply, an
    #: ``{"error": ...}`` body, a non-dict payload) all looked identical.
    #:
    #: `run_pipeline` turns a non-empty value into ``metadata_degraded:<reason>``.
    fallback_reason: str = ""


def get_profile(platform: str) -> PlatformProfile:
    """Return the :class:`PlatformProfile` for ``platform`` (falls back generic)."""
    return PLATFORM_PROFILES.get((platform or "generic").lower(), PLATFORM_PROFILES["generic"])


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


def _regional_run_before(text: str, index: int) -> int:
    """How many consecutive regional-indicator characters end at ``index``."""
    count = 0
    position = index - 1
    while position >= 0 and 0x1F1E6 <= ord(text[position]) <= 0x1F1FF:
        count += 1
        position -= 1
    return count


def _splits_a_grapheme(text: str, index: int) -> bool:
    """Whether cutting ``text`` at ``index`` would land inside one visible character."""
    if index <= 0 or index >= len(text):
        return False
    previous, following = text[index - 1], text[index]
    # A combining mark, variation selector or enclosing keycap belongs to the character before it.
    if unicodedata.category(following) in ("Mn", "Me", "Mc"):
        return True
    if following in ("\ufe0f", "\ufe0e", "\u20e3"):
        return True
    # Zero-width joiner sequences: 👨‍👩‍👧 is one glyph made of five code points.
    if previous == "\u200d" or following == "\u200d":
        return True
    # Skin-tone modifiers.
    if 0x1F3FB <= ord(following) <= 0x1F3FF:
        return True
    # Flags are *pairs* of regional indicators, so a cut is only unsafe mid-pair — an odd-length
    # run before the cut means we are between the two halves of one flag.
    if 0x1F1E6 <= ord(following) <= 0x1F1FF and _regional_run_before(text, index) % 2 == 1:
        return True
    return False


def safe_truncate(text: str, limit: int) -> str:
    """Trim ``text`` to at most ``limit`` characters without splitting a visible character.

    Slicing by code point is what `s[:limit]` does, and a code point is not a character. Cutting
    inside a ZWJ sequence (👨‍👩‍👧), a regional-indicator pair (🇺🇸) or a base-plus-combining-mark
    turns one glyph into a fragment — or, worse, into a *different* glyph: half of 🇺🇸 is 🇺, a
    letter U. This happens whenever the cut lands inside a space-free run, which is exactly the
    case a word-boundary fallback cannot help with.

    It matters here rather than in the abstract because these strings are titles, descriptions and
    hashtags that get **posted to the user's accounts** — the mangling is public and permanent.

    Conservative by construction: it only ever moves the cut *earlier*, so the result is always
    within ``limit``.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = limit
    while cut > 0 and _splits_a_grapheme(text, cut):
        cut -= 1
    return text[:cut]


def _clamp_text(text: Any, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters on a word boundary where possible.

    A non-scalar value becomes ``""`` rather than its ``repr``. ``str(text or "")`` accepted
    anything, so a model returning ``{"title": {"a": 1}}`` — or a proxy returning an
    ``{"error": {...}}`` envelope that happened to land in a text field — published the literal
    string ``"{'a': 1}"`` to the user's accounts. A bare ``NaN`` became ``"nan"`` the same way.
    An empty field is visibly wrong and recoverable; a published Python repr is neither.
    """
    if isinstance(text, (dict, list, tuple, set)):
        return ""
    if isinstance(text, float) and text != text:  # NaN would stringify to "nan"
        return ""
    if text is None or isinstance(text, bool):
        return ""
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    # Word boundary first (it reads better), grapheme safety unconditionally. The word-boundary
    # cut can itself be unsafe: `rsplit(" ", 1)` on a run with no spaces returns the whole slice.
    word_cut = safe_truncate(s, limit).rsplit(" ", 1)[0]
    return (word_cut or safe_truncate(s, limit)).rstrip()


#: Most @handles accepted from a model reply.
#:
#: There was no cap, no length limit, no character validation and no de-duplication — and mentions
#: are **published to the user's connected accounts**. So a model reply (which is shaped by an
#: untrusted transcript, see `llm_client.fence_untrusted`) could emit a hundred real handles and
#: they would go out under the user's name. A suggested-mentions field needs single digits; the
#: value of the hundred-and-first is not the reason to leave it unbounded.
MAX_MENTIONS = 5

#: Longest accepted handle. 30 is above every platform's own ceiling (X 15, Instagram 30,
#: TikTok 24, YouTube 30), so this rejects nonsense without second-guessing a real limit.
_MAX_HANDLE_LENGTH = 30


def _norm_mentions(mentions: Any) -> list[str]:
    """Normalise, validate, de-duplicate and cap suggested @handles.

    Validated rather than merely prefixed. ``f"@{m}"`` on arbitrary text produced "handles"
    containing spaces, URLs and punctuation, which are not handles at all — they are text that
    gets posted. Only the character set every one of these platforms actually allows in a handle
    survives.
    """
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(mentions, list):
        return out
    for raw in mentions:
        handle = str(raw).strip().lstrip("@").strip()
        if not handle or len(handle) > _MAX_HANDLE_LENGTH:
            continue
        # The intersection of what these platforms permit: letters, digits, underscore, dot.
        if not all(ch.isalnum() or ch in ("_", ".") for ch in handle):
            continue
        key = handle.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(f"@{handle}")
        if len(out) >= MAX_MENTIONS:
            break
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
    # Fenced, and the instructions come after it. The transcript is untrusted - see
    # `llm_client.fence_untrusted` for why that matters more here than anywhere else in the
    # project: this function's output is rendered into the video *and* published to the user's
    # own accounts.
    return f"""Write social copy for a short video clip whose transcript is:
{fence_untrusted(transcript_text, limit=2000)}

{UNTRUSTED_NOTICE}

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
    platform: str | None = None,
    hashtag_count: int | None = None,
    client: BaseLLMClient | None = None,
) -> ClipMetadata:
    """Generate per-platform metadata for a clip transcript.

    Falls back to a minimal, deterministic metadata object when the LLM is not
    configured or fails, so the pipeline never breaks.
    """
    platform = platform or options.platform
    hashtag_count = options.hashtag_count if hashtag_count is None else hashtag_count
    profile = get_profile(platform)

    if client is None and not llm_available():
        return _fallback_metadata(transcript_text, profile, "no_llm_configured")

    # Construction is inside the `try`, not before it. `get_llm_client()` raises for a missing
    # SDK, an unimportable one, a malformed base URL and absent credentials — and with it outside,
    # every one of those propagated out of this function and **failed the job**, while the
    # docstring above promises a fallback. The client normalises those to LLMError now, and this
    # is the guard that makes the promise true.
    try:
        client = client or get_llm_client()
        prompt = _build_prompt(transcript_text, options, profile, hashtag_count)
        data = client.complete_json(prompt, system=_SYSTEM, max_tokens=800, expect=dict)
        if not isinstance(data, dict):  # pragma: no cover - `expect=dict` already enforces this
            # Belt and braces, and it narrows the type for the checker. `expect=` moved this
            # check into `parse_json` where a mismatch can be reported as the parse failure it
            # is, rather than silently discarding a reply the caller could not use.
            raise LLMError("expected a JSON object from the model")
    except LLMError as exc:
        logger.warning("metadata generation fell back to a template: %s", exc)
        return _fallback_metadata(transcript_text, profile, _reason_code(exc))

    return _parse_metadata(data, options, profile, hashtag_count)


def _reason_code(exc: Exception) -> str:
    """A short, stable marker suffix for why the model was not used.

    Deliberately coarse. The marker lands on a clip record read by a person deciding whether to
    re-run a job, and the distinction they act on is "misconfigured" versus "the model misbehaved"
    versus "the provider was down". The full text is logged.
    """
    message = str(exc).lower()
    if "not set" in message or "could not be imported" in message or "constructed" in message:
        return "not_configured"
    if "parse" in message or "empty llm response" in message or "expected a json" in message:
        return "unusable_reply"
    return "request_failed"


def _fallback_metadata(
    transcript_text: str, profile: PlatformProfile, reason: str = "no_llm_configured"
) -> ClipMetadata:
    """Deterministic metadata when no LLM is available.

    ``reason`` is recorded on the result so the clip record can say this is template output. See
    :attr:`ClipMetadata.fallback_reason`.
    """
    words = (transcript_text or "").split()
    title = _clamp_text(" ".join(words[:10]) or "Untitled clip", profile.title_max)
    return ClipMetadata(
        title=title,
        description=_clamp_text(transcript_text, profile.desc_max),
        hashtags=[],
        platform=profile.name,
        fallback_reason=reason,
    )


def regenerate_field(
    field_name: str,
    transcript_text: str,
    options: ProcessingOptions,
    platform: str | None = None,
    hashtag_count: int | None = None,
    client: BaseLLMClient | None = None,
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
