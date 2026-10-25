"""Compliance checks for the editable campaign-profile model."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from worker.campaign_profile import CampaignProfile


@dataclass(frozen=True)
class ProfileComplianceReport:
    profile_id: str
    profile_name: str
    status: str
    results: tuple[dict[str, Any], ...]
    generated_caption: str = ""
    generated_hashtags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "status": self.status,
            "results": list(self.results),
            "generated_caption": self.generated_caption,
            "generated_hashtags": list(self.generated_hashtags),
        }


def _text(clip: Any) -> str:
    if isinstance(clip, Mapping):
        values = clip.values()
    else:
        values = [getattr(clip, name, "") for name in ("title", "description", "caption", "cta", "transcript_text", "hook_text")]
    return " ".join(str(value or "") for value in values)


def _value(clip: Any, name: str, default: Any = None) -> Any:
    if isinstance(clip, Mapping):
        return clip.get(name, default)
    return getattr(clip, name, default)


def _items(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value] if value not in (None, "") else []


def _check(results: list[dict[str, Any]], rule: str, passed: bool | None, message: str) -> None:
    results.append({"rule": rule, "status": "pass" if passed is True else "fail" if passed is False else "unknown", "message": message})


def generate_campaign_copy(profile: CampaignProfile, clip: Any = None) -> dict[str, Any]:
    clip = clip or {}
    base = str(_value(clip, "caption", "") or _value(clip, "description", "") or _value(clip, "title", "") or "").strip()
    required = [str(item).strip() for item in _items(profile.copy_rules.get("required_copy", [])) if str(item).strip()]
    cta = str(profile.copy_rules.get("cta", "") or "").strip()
    parts = [base, *required, cta]
    caption = " ".join(dict.fromkeys(part for part in parts if part))
    hashtags = [str(item) for item in _items(profile.copy_rules.get("hashtags", []))]
    existing = _value(clip, "hashtags", []) or []
    for tag in existing:
        tag = str(tag).strip()
        if tag and tag.casefold() not in {item.casefold() for item in hashtags}:
            hashtags.append(tag if tag.startswith("#") else f"#{tag}")
    mentions = [str(item) for item in _items(profile.copy_rules.get("mentions", []))]
    for mention in mentions:
        if mention.casefold() not in caption.casefold():
            caption = f"{caption} {mention}".strip()
    return {"caption": caption, "hashtags": hashtags, "mentions": mentions}


def check_profile_compliance(profile: CampaignProfile, clip: Any, *, visibility: str | None = None) -> ProfileComplianceReport:
    results: list[dict[str, Any]] = []
    text = _text(clip)
    forbidden = [str(item).casefold() for item in _items(profile.banned_words_topics.get("words", [])) if str(item).strip()]
    hits = [term for term in forbidden if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text.casefold())]
    _check(results, "banned_words", not hits, f"banned words found: {', '.join(hits)}" if hits else "no banned words found")
    topics = _items(profile.banned_words_topics.get("topics", []))
    topic_hits = [str(item) for item in topics if str(item).casefold() in text.casefold()]
    _check(results, "banned_topics", not topic_hits, f"banned topics found: {', '.join(topic_hits)}" if topic_hits else "no banned topics found")
    required_copy = [str(item) for item in _items(profile.copy_rules.get("required_copy", [])) if str(item).strip()]
    missing_copy = [item for item in required_copy if item.casefold() not in text.casefold()]
    _check(results, "required_copy", not missing_copy, f"missing required copy: {', '.join(missing_copy)}" if missing_copy else "required copy is present")
    cta = str(profile.copy_rules.get("cta", "") or "").strip()
    if cta:
        _check(results, "cta", cta.casefold() in text.casefold(), "CTA is present" if cta.casefold() in text.casefold() else "CTA is missing")
    required_tags = [str(item).casefold() for item in _items(profile.copy_rules.get("hashtags", [])) if str(item).strip()]
    actual_tags = {str(item).casefold() for item in _items(_value(clip, "hashtags", []))}
    missing_tags = [item for item in required_tags if item not in actual_tags]
    _check(results, "hashtags", not missing_tags, f"missing hashtags: {', '.join(missing_tags)}" if missing_tags else "required hashtags are present")
    required_mentions = [str(item).casefold() for item in _items(profile.copy_rules.get("mentions", [])) if str(item).strip()]
    missing_mentions = [item for item in required_mentions if item not in text.casefold()]
    _check(results, "mentions", not missing_mentions, f"missing mentions: {', '.join(missing_mentions)}" if missing_mentions else "required mentions are present")

    platforms = [str(item).casefold() for item in _items(profile.platforms)]
    actual_platform = str(_value(clip, "platform", "") or "").casefold()
    _check(results, "platform", True if not platforms else (actual_platform in platforms if actual_platform else None), "platform is allowed" if actual_platform in platforms or not platforms else "platform is not allowed")
    ratio = str(_value(clip, "aspect_ratio", "") or _value(clip, "aspect", "") or "")
    ratios = [str(item) for item in _items(profile.aspect_resolution.get("aspect_ratios", []))]
    _check(results, "aspect_ratio", True if not ratios else (ratio in ratios if ratio else None), "aspect ratio is allowed" if ratio in ratios or not ratios else "aspect ratio is not allowed")
    duration = _value(clip, "duration", None)
    length = profile.length
    if length.get("min_seconds") is not None or length.get("max_seconds") is not None:
        try:
            seconds = float(duration)
        except (TypeError, ValueError):
            seconds = None
        if seconds is None:
            _check(results, "length", None, "clip duration was not provided")
        else:
            minimum = length.get("min_seconds", float("-inf"))
            maximum = length.get("max_seconds", float("inf"))
            _check(results, "length", minimum <= seconds <= maximum, "clip duration is within the campaign range" if minimum <= seconds <= maximum else "clip duration is outside the campaign range")
    caption_required = profile.captions_language.get("required")
    if caption_required is True:
        captions = _value(clip, "captions", _value(clip, "captions_present", None))
        _check(results, "captions", bool(captions) if captions is not None else None, "captions are present" if captions else "captions are required")
    languages = [str(item).casefold() for item in _items(profile.captions_language.get("languages", [])) if str(item).strip()]
    if languages:
        actual_language = str(_value(clip, "language", "") or _value(clip, "caption_language", "") or "").casefold()
        _check(results, "caption_language", True if actual_language in languages else None if not actual_language else False, "caption language is allowed" if actual_language in languages else "caption language is not allowed" if actual_language else "caption language was not provided")
    watermark_required = profile.watermark_end_cards.get("watermark_required")
    if watermark_required is True:
        watermark = _value(clip, "watermark", _value(clip, "logo", None))
        _check(results, "watermark", bool(watermark) if watermark is not None else None, "watermark is present" if watermark else "watermark is required")
    end_card_required = profile.watermark_end_cards.get("end_card_required")
    if end_card_required is True:
        end_card = _value(clip, "end_card", _value(clip, "end_card_text", None))
        _check(results, "end_card", bool(end_card) if end_card is not None else None, "end card is present" if end_card else "end card is required")
    source_rules = profile.source_rules
    source_url = str(_value(clip, "source_url", "") or "")
    allowed_sources = [str(item).casefold() for item in source_rules.get("allowed", []) if str(item).strip()]
    prohibited_sources = [str(item).casefold() for item in source_rules.get("prohibited", []) if str(item).strip()]
    if allowed_sources or prohibited_sources:
        if not source_url:
            _check(results, "source", None, "source provenance was not provided")
        else:
            folded_source = source_url.casefold()
            allowed = not allowed_sources or any(item in folded_source for item in allowed_sources)
            prohibited = any(item in folded_source for item in prohibited_sources)
            _check(results, "source", allowed and not prohibited, "source is allowed" if allowed and not prohibited else "source violates campaign source rules")
    audio_rules = profile.audio_rules
    allowed_audio = [str(item).casefold() for item in _items(audio_rules.get("allowed", [])) if str(item).strip()]
    prohibited_audio = [str(item).casefold() for item in _items(audio_rules.get("prohibited", [])) if str(item).strip()]
    if allowed_audio or prohibited_audio:
        audio_source = str(_value(clip, "audio_source", "") or _value(clip, "music", "") or "").casefold()
        if not audio_source:
            _check(results, "audio", None, "audio provenance was not provided")
        else:
            allowed = not allowed_audio or any(item in audio_source for item in allowed_audio)
            prohibited = any(item in audio_source for item in prohibited_audio)
            _check(results, "audio", allowed and not prohibited, "audio is allowed" if allowed and not prohibited else "audio violates campaign audio rules")
    meme_allowed = profile.meme_policy.get("allowed")
    if meme_allowed is False:
        meme = _value(clip, "meme", _value(clip, "contains_meme", None))
        _check(results, "meme_policy", not bool(meme) if meme is not None else None, "meme content is not present" if not meme else "meme content is prohibited")
    if visibility is not None:
        required_public = profile.watermark_end_cards.get("public_visibility_required")
        _check(results, "visibility", True if required_public is not True else visibility.casefold() == "public", "visibility is public" if visibility.casefold() == "public" else "public visibility is required")
    status = "fail" if any(item["status"] == "fail" for item in results) else "unknown" if any(item["status"] == "unknown" for item in results) else "pass"
    generated = generate_campaign_copy(profile, clip)
    return ProfileComplianceReport(profile.id, profile.name, status, tuple(results), generated["caption"], tuple(generated["hashtags"]))


def check_clip_against_profiles(profiles: list[CampaignProfile], clip: Any, *, visibility: str | None = None) -> dict[str, Any]:
    reports = [check_profile_compliance(profile, clip, visibility=visibility) for profile in profiles]
    return {
        "status": "fail" if any(report.status == "fail" for report in reports) else "unknown" if any(report.status == "unknown" for report in reports) else "pass",
        "profiles": [report.to_dict() for report in reports],
        "profile_count": len(reports),
    }


__all__ = ["ProfileComplianceReport", "check_profile_compliance", "check_clip_against_profiles", "generate_campaign_copy"]
