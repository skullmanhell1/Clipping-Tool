"""Shared policy helpers for the versioned campaign preset contract."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from worker.campaign_sources import source_blocker


def canonical_clip_length(value: Any) -> str:
    """Map human brief duration text onto the ProcessingOptions enum."""

    raw = str(value or "").strip()
    compact = re.sub(r"\s+", "", raw.casefold()).replace("–", "-").replace("—", "-")
    if compact in {"auto", ""}:
        return "auto"
    if re.fullmatch(r"<30(?:s|sec|secs|second|seconds)?", compact):
        return "<30s"
    if re.fullmatch(r"30-60(?:s|sec|secs|second|seconds)?", compact):
        return "30-60s"
    if re.fullmatch(r"60-90(?:s|sec|secs|second|seconds)?", compact):
        return "60-90s"
    if re.fullmatch(r"90(?:s|sec|secs|second|seconds)?-3(?:m|min|mins|minute|minutes)", compact):
        return "90s-3min"
    if re.fullmatch(r"90-180(?:s|sec|secs|second|seconds)?", compact):
        return "90s-3min"
    return raw


def preset_settings(preset: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return ProcessingOptions-shaped defaults without mutating the saved preset."""

    data = dict(preset or {})
    settings = dict(data.get("settings") or {})
    aspect = dict(data.get("aspect_resolution") or {})
    copy_rules = dict(data.get("copy_rules") or {})
    captions = dict(data.get("captions_language") or {})
    branding = dict(data.get("branding") or {})
    watermark = dict(data.get("watermark_end_cards") or {})
    posting = dict(data.get("posting") or {})
    ratios = list(aspect.get("aspect_ratios") or data.get("aspect_ratios") or [])
    platforms = list(posting.get("platforms") or data.get("platforms") or data.get("allowed_platforms") or [])
    colors = dict(branding.get("colors") or {})
    settings.setdefault("aspect", ratios[0] if ratios else "")
    settings.setdefault("platform", platforms[0] if platforms else "generic")
    settings.setdefault("captions", captions.get("required") is not False)
    settings.setdefault("caption_preset", captions.get("style") or data.get("caption_style") or "karaoke")
    settings.setdefault("brand_logo", watermark.get("watermark") or branding.get("logo") or "")
    settings.setdefault("brand_font", branding.get("font") or "")
    settings.setdefault("brand_primary_color", colors.get("primary") or "")
    settings.setdefault("brand_highlight_color", colors.get("accent") or "")
    settings.setdefault("brand_cta", copy_rules.get("cta") or data.get("cta") or "")
    if "clip_length" in settings:
        settings["clip_length"] = canonical_clip_length(settings.get("clip_length"))
    settings.setdefault("publish_mode", "review" if posting.get("manual_only") else "review")
    settings.setdefault("public_visibility", "public")
    if platforms:
        settings.setdefault("publish_to", platforms)
    return settings


def is_versioned_preset(preset: Mapping[str, Any] | None) -> bool:
    data = dict(preset or {})
    return bool(
        data.get("schema_version")
        or "source_library" in data
        or "settings" in data
        or "posting" in data
    )


def campaign_validation_preset(preset: Mapping[str, Any] | None):
    """Project either preset schema onto the legacy pure validation model."""

    from worker.campaign_preset import CampaignPreset

    data = dict(preset or {})
    if not is_versioned_preset(data):
        return CampaignPreset.from_dict(data)

    settings = dict(data.get("settings") or {})
    aspect = dict(data.get("aspect_resolution") or {})
    captions = dict(data.get("captions_language") or {})
    copy_rules = dict(data.get("copy_rules") or {})
    posting = dict(data.get("posting") or {})
    watermark = dict(data.get("watermark_end_cards") or {})
    branding = dict(data.get("branding") or {})
    if not branding:
        branding = {
            "logo": watermark.get("watermark") or settings.get("brand_logo") or "",
            "font": settings.get("brand_font") or "",
            "colors": {
                "primary": settings.get("brand_primary_color") or "",
                "accent": settings.get("brand_highlight_color") or "",
            },
        }
    return CampaignPreset.from_dict(
        {
            "name": data.get("name", ""),
            "branding": branding,
            "caption_style": captions.get("style") or settings.get("caption_preset") or "",
            "aspect_ratios": aspect.get("aspect_ratios") or [],
            "hashtags": copy_rules.get("hashtags")
            or (data.get("compliance") or {}).get("required_hashtags", []),
            "cta": copy_rules.get("cta") or settings.get("brand_cta") or "",
            "allowed_platforms": posting.get("platforms") or data.get("platforms") or [],
            "daily_clip_target": posting.get("daily_clip_target", 2),
            "public_visibility_required": posting.get("public_visibility_required", True),
            "pattern_interrupter_interval": settings.get("pattern_interrupter_interval", 0),
        }
    )


def preset_required_fields(preset: Mapping[str, Any] | None) -> list[str]:
    data = dict(preset or {})
    aspect = dict(data.get("aspect_resolution") or {})
    posting = dict(data.get("posting") or {})
    required: list[str] = []
    if not str(data.get("name") or "").strip():
        required.append("name")
    if not list(aspect.get("aspect_ratios") or data.get("aspect_ratios") or []):
        required.append("aspect_ratio")
    if not list(posting.get("platforms") or data.get("platforms") or data.get("allowed_platforms") or []):
        required.append("platforms")
    return required


def preset_apply_blockers(preset: Mapping[str, Any] | None) -> list[str]:
    data = dict(preset or {})
    dismissed = {str(item) for item in data.get("dismissed_warnings") or []}
    blockers = [f"Resolve required field: {field}" for field in preset_required_fields(data)]
    for warning in data.get("warnings") or []:
        if not isinstance(warning, Mapping):
            continue
        if str(warning.get("severity") or "").casefold() != "required":
            continue
        warning_id = str(warning.get("id") or "")
        if warning_id and warning_id not in dismissed:
            blockers.append(str(warning.get("message") or "Resolve the required campaign warning."))
    return list(dict.fromkeys(blockers))


def preset_is_manual_only(preset: Mapping[str, Any] | None) -> bool:
    posting = dict((preset or {}).get("posting") or {})
    return bool(posting.get("manual_only") or posting.get("review_only"))


def preset_source_blocker(preset: Mapping[str, Any] | None, url: str) -> str:
    return source_blocker(preset, url)


def campaign_brand_text(preset: Mapping[str, Any] | None) -> str:
    data = dict(preset or {})
    settings = dict(data.get("settings") or {})
    branding = dict(data.get("branding") or {})
    return str(
        settings.get("brand_name")
        or branding.get("campaign_brand")
        or data.get("campaign_brand")
        or data.get("brand_name")
        or ""
    ).strip()


def campaign_brand_config(preset: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(preset or {})
    settings = dict(data.get("settings") or {})
    brand = campaign_brand_text(data)
    if not brand:
        return {}
    variants = list(settings.get("brand_variants") or [])
    patterns = [str(item).strip() for item in variants if str(item).strip()]
    return {
        "name": brand,
        "variants": patterns,
        "hook_text": str(settings.get("brand_hook_text") or "").strip(),
        "card_text": str(settings.get("brand_card_text") or brand).strip(),
        "thumbnail_text": str(settings.get("brand_thumbnail_text") or brand).strip(),
        "mention": str(settings.get("brand_mention") or "").strip(),
        "hashtag": str(settings.get("brand_hashtag") or f"#{brand}").strip(),
    }


def normalize_campaign_text(value: Any, preset: Mapping[str, Any] | None) -> str:
    text = str(value or "")
    config = campaign_brand_config(preset)
    if not config:
        return text
    name = config["name"]
    variants = [name, *config["variants"]]
    for variant in sorted(set(variants), key=len, reverse=True):
        if variant:
            text = re.sub(rf"(?<!\w){re.escape(variant)}(?!\w)", name, text, flags=re.IGNORECASE)
    return text


def normalize_campaign_words(words: list[Any], preset: Mapping[str, Any] | None) -> None:
    config = campaign_brand_config(preset)
    if not config:
        return
    variants = {str(item).casefold() for item in config["variants"]}
    name = config["name"]
    index = 0
    while index < len(words):
        current = str(getattr(words[index], "text", "") or "").strip().casefold()
        following = (
            str(getattr(words[index + 1], "text", "") or "").strip().casefold()
            if index + 1 < len(words)
            else ""
        )
        if f"{current} {following}" in variants:
            words[index].text = name
            words[index].end = max(
                float(getattr(words[index], "end", 0.0)),
                float(getattr(words[index + 1], "end", 0.0)),
            )
            words[index + 1].text = ""
            index += 2
            continue
        if current in variants:
            words[index].text = name
        elif current == "" and index + 1 < len(words):
            index += 1
        else:
            words[index].text = normalize_campaign_text(getattr(words[index], "text", ""), preset)
        index += 1


def enrich_campaign_metadata_for_preset(metadata: Any, preset: Mapping[str, Any] | None) -> Any:
    config = campaign_brand_config(preset)
    data = dict(preset or {})
    copy_rules = dict(data.get("copy_rules") or {})
    required_copy = [str(item).strip() for item in copy_rules.get("required_copy", []) if str(item).strip()]
    required_hashtags = [str(item).strip() for item in copy_rules.get("hashtags", []) if str(item).strip()]
    required_mentions = [str(item).strip() for item in copy_rules.get("mentions", []) if str(item).strip()]
    if not config and not (required_copy or required_hashtags or required_mentions):
        return metadata
    if config:
        name = config["name"]
        metadata.title = normalize_campaign_text(getattr(metadata, "title", ""), preset).strip()
        if name.casefold() not in metadata.title.casefold():
            metadata.title = f"{name}: {metadata.title}".strip()
        metadata.title = metadata.title[:100].strip()
        metadata.title_alternatives = [
            (value if name.casefold() in str(value).casefold() else f"{name}: {value}")[:100].strip()
            for value in (getattr(metadata, "title_alternatives", []) or [])
        ]
        metadata.hook_text = config["hook_text"] or getattr(metadata, "hook_text", "")
    metadata.description = normalize_campaign_text(getattr(metadata, "description", ""), preset).strip()
    for item in required_copy:
        if item.casefold() not in metadata.description.casefold():
            metadata.description = f"{metadata.description}\n\n{item}".strip()
    mentions = [*required_mentions]
    if config and config["mention"]:
        mentions.append(config["mention"])
    for mention in mentions:
        if mention.casefold() not in metadata.description.casefold():
            metadata.description = f"{metadata.description}\n\n{mention}".strip()
    existing_tags = list(getattr(metadata, "hashtags", []) or [])
    tags = [*required_hashtags]
    if config:
        tags.insert(0, config["hashtag"])
    metadata.hashtags = [
        *dict.fromkeys(
            [*tags, *[tag for tag in existing_tags if str(tag).strip()]]
        ),
    ]
    if mentions:
        metadata.mentions = list(dict.fromkeys(mentions))
    if config:
        metadata.thumbnail_text = config["thumbnail_text"]
    return metadata


__all__ = [
    "preset_apply_blockers",
    "canonical_clip_length",
    "preset_is_manual_only",
    "preset_required_fields",
    "preset_settings",
    "is_versioned_preset",
    "campaign_validation_preset",
    "preset_source_blocker",
    "campaign_brand_config",
    "campaign_brand_text",
    "enrich_campaign_metadata_for_preset",
    "normalize_campaign_text",
    "normalize_campaign_words",
]
