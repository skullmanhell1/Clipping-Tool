"""Per-platform caption and hashtag tailoring at publish time (PB6).

A clip's metadata is generated **once**, for one platform - whichever ``options.platform`` said -
and then the same title, description and hashtags are sent to every destination. The per-platform
limits in :data:`worker.metadata.PLATFORM_PROFILES` are real (tiktok 80/300/8, x 70/260/4) but
they are applied at generation time to that single platform, so cross-posting a YouTube-shaped
caption to X means the publishers chop it: ``f"{title}\\n\\n{caption}"[:280]``.

Truncation at a character index is the worst available option. It cuts mid-word, it strips the
call to action and the hashtags off the end - the parts doing the work - and on X it can remove
the entire caption because the title consumed the budget first. What it never does is decide what
to *drop*.

So this module fits text to a platform in the order a person would:

1. drop hashtags beyond the platform's count, keeping the earliest (the generator emits its
   strongest first, and a specific tag beats a generic one);
2. shorten the description at a sentence boundary, then a clause boundary, then a word boundary;
3. keep the call to action attached even when the description has to give up room for it, because
   a caption that ends mid-sentence with no ask is worse than a shorter one that asks.

Optionally - and off by default - the description can be *regenerated* for the destination by the
LLM instead of shortened. That is what the plan item asks for, and it costs one model call per
platform per clip, which is why it is opt-in rather than automatic.
"""

from __future__ import annotations

import re
from typing import Any

from config import settings
from worker.metadata import PlatformProfile, get_profile

#: Sentence-ending punctuation, for finding a graceful cut.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

#: Clause boundaries, tried when no sentence break fits.
_CLAUSE_END = re.compile(r"(?<=[,;:—-])\s+")


def _fit_sentences(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters, preferring a sentence boundary.

    Falls back to a clause boundary, then a word boundary. Returns ``""`` only when even the
    first word does not fit, which the caller handles by dropping the field rather than emitting
    a fragment.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""

    for pattern in (_SENTENCE_END, _CLAUSE_END):
        pieces = pattern.split(text)
        if len(pieces) > 1:
            kept: list[str] = []
            total = 0
            for piece in pieces:
                addition = len(piece) + (1 if kept else 0)
                if total + addition > limit:
                    break
                kept.append(piece)
                total += addition
            if kept:
                return " ".join(kept).strip()

    cut = text[:limit].rsplit(" ", 1)[0].strip()
    return cut


def fit_hashtags(tags: Any, profile: PlatformProfile) -> list[str]:
    """Keep at most ``profile.hashtag_max`` hashtags, earliest first (PB6)."""
    if not isinstance(tags, (list, tuple)):
        return []
    seen: list[str] = []
    for tag in tags:
        text = str(tag).strip()
        if not text:
            continue
        if text.lower() not in {s.lower() for s in seen}:
            seen.append(text)
    return seen[: max(0, int(profile.hashtag_max))]


def fit_caption(
    description: str,
    cta: str,
    hashtags: list[str],
    mentions: list[str],
    profile: PlatformProfile,
) -> tuple[str, str, list[str]]:
    """Fit description/CTA/hashtags into ``profile.desc_max`` as a whole (PB6).

    The platform limit applies to the *rendered caption*, not to the description alone, so the
    budget is shared: the hashtags and the call to action are reserved first because they are the
    parts with a job to do, and the description absorbs what is left. Reserving nothing and
    trimming the description to the full limit - which is what a per-field clamp does - produces a
    caption that overflows the moment the tags are appended, and the publisher then cuts the tags
    back off.

    Returns ``(description, cta, hashtags)``.
    """
    limit = max(0, int(profile.desc_max))
    tags = fit_hashtags(hashtags, profile)
    cta = (cta or "").strip()

    # Reserve the tail: mentions + hashtags, plus the blank line separating it.
    tail = " ".join([*(mentions or []), *tags]).strip()
    reserved = len(tail) + (2 if tail else 0)

    # The CTA is short by construction; if it genuinely cannot fit alongside the tail, the tags
    # win - they are how the post is found, whereas a CTA in a caption nobody reaches is inert.
    if cta and reserved + len(cta) + 2 <= limit:
        reserved += len(cta) + 2
    elif cta:
        cta = ""

    fitted = _fit_sentences(description, max(0, limit - reserved))
    return fitted, cta, tags


def _regenerate_description(transcript_text: str, platform: str, hashtag_count: int) -> str | None:
    """Ask the LLM for a description written *for* ``platform``, or ``None``.

    Returns ``None`` on any failure - no LLM configured, a model error, an empty answer - and the
    caller then falls back to fitting the text it already has. A tailoring feature that could fail
    a publish would be a worse trade than a slightly long caption.
    """
    try:
        from worker.llm_client import llm_available
        from worker.metadata import regenerate_field
        from worker.models import ProcessingOptions

        if not llm_available():
            return None
        options = ProcessingOptions(platform=platform, hashtag_count=hashtag_count)
        value = regenerate_field(
            "description",
            transcript_text,
            options,
            platform=platform,
            hashtag_count=hashtag_count,
        )
        text = str(value or "").strip()
        return text or None
    except Exception:
        return None


def tailor_request(request: dict[str, Any], platform: str) -> dict[str, Any]:
    """Return a copy of a stored publish request fitted to ``platform`` (PB6).

    Pure with respect to its input: the caller keeps the original request so a re-tailoring for a
    different platform starts from the full text rather than from an already-shortened one.
    Tailoring the stored request in place would make every retry a little shorter than the last.
    """
    profile = get_profile(platform)
    tailored = dict(request)

    title = str(tailored.get("title") or "")
    tailored["title"] = _fit_sentences(title, profile.title_max)

    description = str(tailored.get("description") or "")
    if getattr(settings, "publish_tailor_with_llm", False):
        rewritten = _regenerate_description(
            str(tailored.get("transcript_text") or description),
            profile.name,
            profile.hashtag_max,
        )
        if rewritten:
            description = rewritten

    fitted, cta, tags = fit_caption(
        description,
        str(tailored.get("cta") or ""),
        list(tailored.get("hashtags") or []),
        list(tailored.get("mentions") or []),
        profile,
    )
    tailored["description"] = fitted
    tailored["cta"] = cta
    tailored["hashtags"] = tags
    return tailored
