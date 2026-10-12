"""BOXABL campaign defaults, source allow-list and conservative publish preflight.

The source list is copied from the campaign's ``BOXABL Content`` Google Doc. It is an
allow-list of source links, not a claim that every moment in those videos is safe to post:
visual framing, spoken content, and platform-native account tags still need human review.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from worker.google_drive import extract_file_id as drive_file_id

PROFILE_NAME = "boxabl_campaign"

APPROVED_YOUTUBE_VIDEO_IDS = frozenset(
    {
        "8JMrfSFQdpo",  # Enes Yilmazer
        "eS-fnedN2yI",  # Orkun Isitmak
        "uu5aORByttg",  # Faze Rug
        "0Cdqs2Q1Vis",  # Graham Stephan
        "7rX5cPQrx_s",  # Airrack
        "5Lo54Lx7MCU",  # 808
        "KrahQlLAz44",  # Ellie in Space
        "3plLxp_7Jlo",  # Irina Dee
        "JbS_VhA0rRo",  # BOXABL Casita Tour
        "IBV7eIH3Bdw",  # BOXABL Factory Update
        "b9erzF6LHKI",  # BOXABL Factory Update
        "9ThVUNGj3To",  # BOXABL Factory Update
        "rUArr8SWyf8",  # BOXABL Factory Update
        "Cz9cwL_fqTI",  # NYSE interview
    }
)
APPROVED_DRIVE_FILE_IDS = frozenset({"1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry"})

REQUIRED_REVIEW_CHECKS = {
    "source_approved": "Source is one of the official Drive / YouTube links in the campaign brief.",
    "whole_home_2_5": "The complete Casita is clearly visible by 0:02–0:05.",
    "brand_by_8": "Exact BOXABL branding is spoken or visible by 0:05–0:08.",
    "hook_and_pacing": "The first two seconds have a strong hook and the edit stays brisk.",
    "captions_verified": "Captions are legible and BOXABL is spelled exactly BOXABL.",
    "no_prohibited_content": "I checked the whole clip: no Kanye West, stock/equity/ticker talk, political or negative bait, or raw rip.",
}

_BLOCKED_TEXT = re.compile(
    r"\b(?:kanye(?:\s+west)?|ticker|equity|stocks?|shares?\s+(?:of\s+)?stock|politic(?:al|s|ian))\b",
    re.IGNORECASE,
)
_BOXABL_VARIANTS = re.compile(r"\bbox[\s-]*(?:able|abl)\b", re.IGNORECASE)


def normalize_boxabl_text(value: str | None) -> str:
    """Correct common ASR/copy variants of the campaign's proper name."""
    return _BOXABL_VARIANTS.sub("BOXABL", str(value or ""))


def normalize_boxabl_words(words: list[Any]) -> None:
    """Correct ASR word tokens in-place so burned captions and subtitle files agree."""
    index = 0
    while index < len(words):
        word = words[index]
        text = str(getattr(word, "text", "") or "")
        if re.fullmatch(r"box", text.strip(), re.IGNORECASE) and index + 1 < len(words):
            following = str(getattr(words[index + 1], "text", "") or "")
            if re.fullmatch(r"able|abl", following.strip(), re.IGNORECASE):
                word.text = "BOXABL"
                word.end = max(
                    float(getattr(word, "end", 0.0)), float(getattr(words[index + 1], "end", 0.0))
                )
                words[index + 1].text = ""
                index += 2
                continue
        word.text = normalize_boxabl_text(text)
        index += 1


def youtube_video_id(url: str) -> str | None:
    """Extract a video ID only from recognised YouTube video URL shapes."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        return parts.path.strip("/").split("/", 1)[0] or None
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        return None
    query_id = parse_qs(parts.query).get("v", [None])[0]
    if query_id:
        return query_id
    path_parts = [part for part in parts.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
        return path_parts[1]
    return None


def is_approved_source_url(url: str) -> bool:
    """True only for a direct video/file link explicitly listed in the campaign source doc."""
    return (
        youtube_video_id(url) in APPROVED_YOUTUBE_VIDEO_IDS
        or drive_file_id(url) in APPROVED_DRIVE_FILE_IDS
    )


def enrich_campaign_metadata(metadata: Any) -> Any:
    """Apply deterministic brand and caption copy after LLM/fallback metadata generation."""
    metadata.title = normalize_boxabl_text(metadata.title).strip()
    if "BOXABL" not in metadata.title:
        metadata.title = f"BOXABL: {metadata.title}".strip()
    metadata.title = metadata.title[:100].strip()

    metadata.title_alternatives = [
        (lambda title: title if "BOXABL" in title else f"BOXABL: {title}")(
            normalize_boxabl_text(str(value)).strip()
        )[:100].strip()
        for value in (metadata.title_alternatives or [])
    ]

    # This is a short, high-contrast title card at 0:00–0:02; a separate BOXABL card is
    # rendered at 0:05–0:08 by the caption renderer.
    metadata.hook_text = "BOXABL: A 20×20 home that folds?"
    metadata.description = normalize_boxabl_text(metadata.description).strip()
    campaign_context = "Explore the BOXABL Casita, a 20×20 ft foldable, factory-built home."
    if campaign_context.casefold() not in metadata.description.casefold():
        metadata.description = "\n\n".join(
            part for part in (metadata.description, campaign_context) if part
        )
    if "@boxabl" not in metadata.description.casefold():
        metadata.description = f"{metadata.description}\n\n@boxabl".strip()
    metadata.hashtags = [
        "#BOXABL",
        *[tag for tag in (metadata.hashtags or []) if str(tag).strip().casefold() != "#boxabl"],
    ]
    metadata.mentions = ["@boxabl"]
    metadata.thumbnail_text = "BOXABL CASITA"
    return metadata


def _valid_https_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or "").strip())
        return (
            parsed.scheme.lower() == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not any(character.isspace() for character in str(url))
        )
    except ValueError:
        return False


def campaign_publish_blockers(
    *,
    job: Any,
    clip: Any,
    platforms: list[str],
    mode: str,
    checks: dict[str, Any],
    affiliate_url: str = "",
) -> list[str]:
    """Return actionable reasons publishing is not permitted for a BOXABL campaign clip.

    The tool can inspect text and direct source URLs. It cannot reliably prove visual framing,
    spoken timing, account tagging, context, or whether a cut is an unedited rip; those are
    explicit human-review checks, not fabricated computer-vision guarantees.
    """
    blockers: list[str] = []
    if mode != "review":
        blockers.append(
            "BOXABL campaign clips must be uploaded in review mode, not auto-published."
        )
    if getattr(clip, "review_state", "pending") != "approved":
        blockers.append("Approve the clip after watching it before publishing.")

    missing = [
        label for key, label in REQUIRED_REVIEW_CHECKS.items() if checks.get(key) is not True
    ]
    if missing:
        blockers.append("Complete the BOXABL review checklist: " + " ".join(missing))

    source = str(getattr(job, "source", "") or "")
    input_type = str(getattr(job, "input_type", "") or "")
    if input_type == "url" and not is_approved_source_url(source):
        blockers.append("This URL is not an individual source link in the BOXABL campaign brief.")

    copy = " ".join(
        str(getattr(clip, field, "") or "")
        for field in (
            "title",
            "description",
            "hook_text",
            "cta",
            "thumbnail_text",
            "transcript_text",
        )
    )
    if _BLOCKED_TEXT.search(copy):
        blockers.append(
            "Clip text contains a prohibited campaign topic; review and remove it before publishing."
        )
    if "BOXABL" not in copy:
        blockers.append("Clip metadata must include the exact uppercase brand spelling BOXABL.")
    if "@boxabl" not in copy.casefold() and "@boxabl" not in [
        str(mention).casefold() for mention in (getattr(clip, "mentions", []) or [])
    ]:
        blockers.append("Add @boxabl to the campaign caption before publishing.")

    selected = {str(platform).strip().lower() for platform in platforms}
    if "x" in selected:
        if not _valid_https_url(affiliate_url):
            blockers.append("Add your personal trackable affiliate URL (https://…) for X.")
        elif len(affiliate_url) > 220:
            blockers.append("The X affiliate URL is too long to fit safely in the campaign post.")
        else:
            hashtags = [str(tag).strip() for tag in (getattr(clip, "hashtags", []) or [])[:4]]
            tail = [affiliate_url, "@boxabl", *[tag for tag in hashtags if tag]]
            reserved = sum(len(part) for part in tail) + max(0, len(tail) - 1)
            title_cost = min(len(str(getattr(clip, "title", "") or "")), 70)
            if title_cost + 2 + reserved + 2 > 280:
                blockers.append(
                    "The X title, affiliate URL and required campaign tags do not fit the 280-character post limit."
                )
    if "instagram" in selected:
        blockers.append(
            "The Instagram publisher cannot apply the required native @boxabl account tag. "
            "Download the clip and publish/tag it manually in Instagram instead of using this API route."
        )
    return blockers
