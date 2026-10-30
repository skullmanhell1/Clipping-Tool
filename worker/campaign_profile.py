"""Deterministic, editable campaign profiles extracted from campaign documents.

The importer intentionally treats source text as data.  Values are only assigned when a known
label has an explicit value; conflicting or incomplete instructions are retained as candidates and
reported in ``ambiguities``/``uncertainties`` instead of being guessed.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from worker.campaign_preset import _platform, _ratio, _split, _unique

_TAG_RE = re.compile(r"(?<![\w])#[\w][\w-]*", re.UNICODE)
_MENTION_RE = re.compile(r"(?<![\w])@[\w][\w.-]*", re.UNICODE)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_RESOLUTION_RE = re.compile(r"(?<!\d)(\d{2,5})\s*[x×]\s*(\d{2,5})(?!\d)", re.I)
_RANGE_RE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds|min|mins|minutes)?",
    re.I,
)
_VIDEO_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "drive.google.com",
    "www.drive.google.com",
    "vimeo.com",
    "www.vimeo.com",
    "tiktok.com",
    "www.tiktok.com",
    "instagram.com",
    "www.instagram.com",
    "facebook.com",
    "www.facebook.com",
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().strip("*_`\"'"))


def _values(value: Any) -> list[str]:
    return _unique([_clean(item).lstrip("-•") for item in _split(value)])


def _tags(value: Any) -> list[str]:
    text = " ".join(value) if isinstance(value, (list, tuple, set)) else str(value or "")
    found = _TAG_RE.findall(text)
    if found:
        return _unique(found)
    return _unique([f"#{item.lstrip('#')}" for item in _values(text)])


def _mentions(value: Any) -> list[str]:
    text = " ".join(value) if isinstance(value, (list, tuple, set)) else str(value or "")
    found = _MENTION_RE.findall(text)
    if found:
        return _unique(found)
    return _unique([item for item in _values(text) if item.startswith("@")])


def _video_links(text: str) -> list[str]:
    """Keep source links that can identify an approved video, not the brief itself."""

    result: list[str] = []
    for match in _URL_RE.finditer(str(text or "")):
        url = match.group(0).rstrip(".,;:)]}")
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        host = (parts.hostname or "").casefold()
        path = parts.path.casefold()
        is_drive_file = host in {"drive.google.com", "www.drive.google.com"} and "/file/" in path
        is_direct_video = bool(re.search(r"\.(?:mp4|mov|m4v|webm|mkv)(?:$|[?#])", path))
        if (
            host in _VIDEO_HOSTS
            and (host not in {"drive.google.com", "www.drive.google.com"} or is_drive_file)
        ) or is_direct_video:
            if url not in result:
                result.append(url)
    return result


def _label_line(line: str) -> tuple[str, str] | None:
    text = str(line or "").strip()
    while text and text[0] in "#-*•":
        text = text[1:]
        if text and text[0].isspace():
            text = text.lstrip()
    text = text.strip()
    if not text:
        return None
    cut = min((text.find(separator) for separator in ":=–-" if separator in text), default=-1)
    if cut < 0:
        label, value = text, ""
    else:
        label, value = text[:cut], text[cut + 1 :]
    label = _clean(label)
    if not label or len(label) > 100:
        return None
    return label.casefold(), _clean(value)


def _first(fields: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = fields.get(name, "")
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value:
            return value
    return ""


def _bool(value: str, default: bool | None = None) -> bool | None:
    text = value.casefold().strip()
    if text in {"yes", "true", "required", "mandatory", "on", "enabled", "allowed"}:
        return True
    if text in {"no", "false", "optional", "off", "disabled", "not allowed", "prohibited"}:
        return False
    return default


def _length(value: str) -> dict[str, Any]:
    matches = list(_RANGE_RE.finditer(value))
    if matches:
        match = matches[0]
        unit = (match.group(3) or "s").casefold()
        multiplier = 60 if unit.startswith("min") else 1
        return {
            "min_seconds": float(match.group(1)) * multiplier,
            "max_seconds": float(match.group(2)) * multiplier,
            "raw": value,
        }
    numbers = [float(item) for item in _NUMBER_RE.findall(value)]
    if len(numbers) == 1:
        unit = "min" if re.search(r"\bmin", value, re.I) else "s"
        seconds = numbers[0] * (60 if unit == "min" else 1)
        return {"min_seconds": seconds, "max_seconds": seconds, "raw": value}
    return {"raw": value} if value else {}


def _resolutions(value: str) -> list[dict[str, int]]:
    return [
        {"width": int(left), "height": int(right)} for left, right in _RESOLUTION_RE.findall(value)
    ]


@dataclass
class CampaignProfile:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "Untitled campaign"
    length: dict[str, Any] = field(default_factory=dict)
    aspect_resolution: dict[str, Any] = field(default_factory=dict)
    platforms: list[str] = field(default_factory=list)
    branding: dict[str, Any] = field(default_factory=dict)
    video_links: list[str] = field(default_factory=list)
    captions_language: dict[str, Any] = field(default_factory=dict)
    copy_rules: dict[str, Any] = field(default_factory=dict)
    watermark_end_cards: dict[str, Any] = field(default_factory=dict)
    banned_words_topics: dict[str, Any] = field(default_factory=dict)
    source_rules: dict[str, Any] = field(default_factory=dict)
    audio_rules: dict[str, Any] = field(default_factory=dict)
    meme_policy: dict[str, Any] = field(default_factory=dict)
    payout_deadlines: dict[str, Any] = field(default_factory=dict)
    ambiguities: list[dict[str, Any]] = field(default_factory=list)
    uncertainties: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    raw_text_hash: str = ""
    parser: str = "deterministic"
    editable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "length": dict(self.length),
            "aspect_resolution": dict(self.aspect_resolution),
            "platforms": list(self.platforms),
            "branding": dict(self.branding),
            "video_links": list(self.video_links),
            "captions_language": dict(self.captions_language),
            "copy_rules": dict(self.copy_rules),
            "watermark_end_cards": dict(self.watermark_end_cards),
            "banned_words_topics": dict(self.banned_words_topics),
            "source_rules": dict(self.source_rules),
            "audio_rules": dict(self.audio_rules),
            "meme_policy": dict(self.meme_policy),
            "payout_deadlines": dict(self.payout_deadlines),
            "ambiguities": list(self.ambiguities),
            "uncertainties": list(self.uncertainties),
            "provenance": list(self.provenance),
            "raw_text_hash": self.raw_text_hash,
            "parser": self.parser,
            "editable": self.editable,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> CampaignProfile:
        data = dict(value or {})
        known = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: data[key] for key in known if key in data})


def _record_conflict(
    ambiguities: list[dict[str, Any]],
    field_name: str,
    values: Sequence[Any],
    source: str = "document",
) -> None:
    normalised = [_clean(item) if isinstance(item, str) else item for item in values]
    if len({str(item).casefold() for item in normalised}) > 1:
        ambiguities.append({"field": field_name, "candidates": normalised, "source": source})


def parse_campaign_profile(
    text: str,
    *,
    structured_blocks: list[dict[str, Any]] | None = None,
    provenance: list[dict[str, Any]] | None = None,
) -> CampaignProfile:
    """Extract explicit campaign rules from text, headings, bullets, and table rows."""

    raw = str(text or "")
    fields: dict[str, list[str]] = {}
    heading_name = ""
    for raw_line in raw.splitlines():
        line = str(raw_line).strip()
        if line.startswith("#") and ":" not in line and "=" not in line:
            title = _clean(line.lstrip("#"))
            if title and not heading_name:
                heading_name = title
        parts = None
        if "|" in line or "\t" in line:
            separator = "|" if "|" in line else "\t"
            cells = [_clean(item) for item in line.strip("|").split(separator)]
            if (
                len(cells) >= 2
                and cells[0]
                and cells[1]
                and ":" not in cells[0]
                and "=" not in cells[0]
            ):
                parts = (cells[0].casefold(), " | ".join(cells[1:]))
        if parts is None:
            parts = _label_line(line)
        if not parts:
            continue
        label, value = parts
        fields.setdefault(label, []).append(value)

    # Google Docs table flatteners use `Label | value` and preserve row text. Treat those rows as
    # ordinary labelled fields without allowing a free-form table cell to become a guessed rule.
    for block in structured_blocks or []:
        if str(block.get("type", "")).casefold() == "table":
            for row in block.get("rows", []) or []:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    label = _clean(row[0]).casefold()
                    value = _clean(" | ".join(str(item) for item in row[1:]))
                    if label:
                        fields.setdefault(label, []).append(value)

    def all_values(*names: str) -> list[str]:
        return [value for name in names for value in fields.get(name, []) if value]

    ambiguities: list[dict[str, Any]] = []
    uncertainties: list[dict[str, Any]] = []
    name_values = all_values("campaign", "campaign name", "name", "title")
    _record_conflict(ambiguities, "name", name_values)
    name = (
        name_values[0]
        if name_values and len({item.casefold() for item in name_values}) == 1
        else heading_name
    )

    length_values = all_values("length", "clip length", "duration", "durations", "video length")
    _record_conflict(ambiguities, "length", length_values)
    length = _length(length_values[0]) if length_values and len(length_values) == 1 else {}
    if length_values and not length:
        uncertainties.append(
            {
                "field": "length",
                "reason": "duration is not a supported numeric range",
                "values": length_values,
            }
        )

    aspect_values = all_values("aspect", "aspect ratio", "aspect ratios", "format", "formats")
    ratios = _unique(
        [_ratio(item) for value in aspect_values for item in _split(value) if _ratio(item)]
    )
    resolution_values = all_values("resolution", "resolutions", "output resolution")
    aspect_resolution = {
        "aspect_ratios": ratios,
        "resolutions": _resolutions(" ".join([*aspect_values, *resolution_values])),
    }
    if aspect_values and not ratios:
        uncertainties.append(
            {
                "field": "aspect_resolution",
                "reason": "no supported ratio was found",
                "values": aspect_values,
            }
        )

    platform_values = all_values(
        "platform", "platforms", "allowed platforms", "publish to", "distribution"
    )
    platforms = _unique([_platform(item) for value in platform_values for item in _split(value)])
    branding_values = all_values("branding", "brand", "brand kit", "brand assets", "assets")
    primary_color = _first(fields, "primary color", "primary colour", "brand primary color")
    accent_color = _first(
        fields, "accent color", "accent colour", "highlight color", "brand highlight color"
    )
    branding = {
        "logo": _first(fields, "logo", "logo asset", "brand logo", "watermark"),
        "font": _first(fields, "font", "font family", "font asset", "brand font"),
        "colors": {
            key: value
            for key, value in (("primary", primary_color), ("accent", accent_color))
            if value
        },
        "assets": _values(" ".join(branding_values)),
    }
    captions_values = all_values(
        "captions", "caption", "caption rules", "language", "languages", "caption language"
    )
    captions_language = {
        "required": _bool(_first(fields, "captions", "caption", "caption rules")),
        "languages": _values(_first(fields, "language", "languages", "caption language")),
        "style": _first(fields, "caption style", "caption preset", "caption format"),
    }
    if (
        captions_values
        and captions_language["required"] is None
        and not captions_language["languages"]
    ):
        uncertainties.append(
            {
                "field": "captions_language",
                "reason": "caption instruction is not explicit",
                "values": captions_values,
            }
        )

    copy_rules = {
        "hashtags": _tags(
            " ".join(all_values("hashtags", "hash tags", "tags", "required hashtags"))
        ),
        "mentions": _mentions(" ".join(all_values("mentions", "handles", "required mentions"))),
        "required_copy": all_values(
            "required copy", "copy", "caption copy", "description", "script copy"
        ),
        "cta": _first(fields, "cta", "call to action"),
        "language": _first(fields, "copy language", "caption language"),
    }
    watermark_values = all_values("watermark", "logo", "watermark rules")
    end_card_values = all_values("end card", "end-card", "outro", "end card rules")
    watermark_end_cards = {
        "watermark_required": _bool(watermark_values[0]) if watermark_values else None,
        "watermark": watermark_values[0] if watermark_values else "",
        "end_card_required": _bool(end_card_values[0]) if end_card_values else None,
        "end_card": end_card_values[0] if end_card_values else "",
    }
    banned = all_values(
        "banned words",
        "forbidden words",
        "prohibited words",
        "avoid words",
        "banned topics",
        "prohibited topics",
    )
    banned_words = all_values("banned words", "forbidden words", "prohibited words", "avoid words")
    banned_topics = all_values("banned topics", "prohibited topics", "avoid topics")
    banned_words_topics = {
        "words": _values(" ".join(banned_words)),
        "topics": _values(" ".join(banned_topics)),
    }
    source_rules = {
        "allowed": _values(
            " ".join(all_values("allowed sources", "source rules", "approved sources", "source"))
        ),
        "prohibited": _values(" ".join(all_values("prohibited sources", "disallowed sources"))),
        "attribution": _first(fields, "source attribution", "credit"),
        "video_links": _video_links(raw),
    }
    audio_rules = {
        "allowed": _values(" ".join(all_values("allowed audio", "audio rules", "music"))),
        "prohibited": _values(" ".join(all_values("prohibited audio", "disallowed audio"))),
        "attribution": _first(fields, "audio attribution", "music attribution"),
    }
    meme_values = all_values("meme policy", "memes", "meme rules", "meme usage")
    meme_policy = {"allowed": _bool(meme_values[0]) if meme_values else None, "rules": meme_values}
    payout_values = all_values("payout", "payout rate", "compensation", "payment")
    deadline_values = all_values("deadline", "deadlines", "due date", "delivery date")
    payout_deadlines = {
        "payout": payout_values[0] if payout_values else "",
        "deadlines": deadline_values,
    }
    visibility_values = all_values("public visibility", "visibility", "public posting")
    if visibility_values:
        watermark_end_cards["public_visibility_required"] = _bool(visibility_values[0])

    for field_name, values in (
        ("platforms", platform_values),
        ("captions_language", captions_values),
        ("watermark_end_cards", watermark_values + end_card_values),
        ("banned_words_topics", banned),
        ("source_rules", source_rules["allowed"]),
        ("audio_rules", audio_rules["allowed"]),
        ("meme_policy", meme_values),
        ("payout_deadlines", payout_values + deadline_values),
    ):
        if len(values) > 1:
            _record_conflict(ambiguities, field_name, values)

    if not platforms:
        uncertainties.append({"field": "platforms", "reason": "no explicit platform rule found"})
    if not copy_rules["hashtags"] and not copy_rules["required_copy"]:
        uncertainties.append(
            {"field": "copy_rules", "reason": "no required caption or hashtag copy found"}
        )

    return CampaignProfile(
        name=name or "Untitled campaign",
        length=length,
        aspect_resolution=aspect_resolution,
        platforms=platforms,
        branding=branding,
        video_links=list(source_rules["video_links"]),
        captions_language=captions_language,
        copy_rules=copy_rules,
        watermark_end_cards=watermark_end_cards,
        banned_words_topics=banned_words_topics,
        source_rules=source_rules,
        audio_rules=audio_rules,
        meme_policy=meme_policy,
        payout_deadlines=payout_deadlines,
        ambiguities=ambiguities,
        uncertainties=uncertainties,
        provenance=list(provenance or []),
        raw_text_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else "",
    )


parse_campaign_import = parse_campaign_profile

__all__ = ["CampaignProfile", "parse_campaign_profile", "parse_campaign_import"]
