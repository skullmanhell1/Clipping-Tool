"""Deterministic, editable campaign profiles extracted from campaign documents.

The importer intentionally treats source text as data.  Values are only assigned when a known
label has an explicit value; conflicting or incomplete instructions are retained as candidates and
reported in ``ambiguities``/``uncertainties`` instead of being guessed.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from worker.campaign_preset import _platform, _ratio, _split, _unique
from worker.campaign_policy import canonical_clip_count, canonical_clip_length
from worker.campaign_sources import extract_source_manifest

_TAG_RE = re.compile(r"(?<![\w])#[\w][\w-]*", re.UNICODE)
_MENTION_RE = re.compile(r"(?<![\w])@[\w][\w.-]*", re.UNICODE)
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_RESOLUTION_RE = re.compile(r"(?<!\d)(\d{2,5})\s*[x×]\s*(\d{2,5})(?!\d)", re.I)
_RANGE_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds|min|mins|minutes)?", re.I)


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


def _first(fields: Mapping[str, str], *names: str) -> str:
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


def _choice(value: Any, allowed: set[str]) -> str:
    """Keep explicit engine choices bounded instead of passing prose into an option field."""
    text = _clean(value).casefold().replace(" ", "_").replace("-", "_")
    aliases = {
        "auto_highlights": "ai",
        "ai_highlights": "ai",
        "silence_based": "silence",
        "fixed_length": "fixed",
        "local_only_assets": "local_only",
        "local_then_external_assets": "local_then_external",
        "follow_active_speaker": "follow_active",
        "split_screen_2_up": "split_screen",
        "standard_retention": "balanced",
        "high_retention": "aggressive",
    }
    candidate = aliases.get(text, text)
    return candidate if candidate in allowed else ""


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
    return [{"width": int(left), "height": int(right)} for left, right in _RESOLUTION_RE.findall(value)]


def _review_warnings(
    ambiguities: list[dict[str, Any]], uncertainties: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for index, item in enumerate(ambiguities):
        field_name = str(item.get("field") or "campaign rule")
        warnings.append(
            {
                "id": f"ambiguity:{field_name}:{index}",
                "severity": "warning",
                "kind": "ambiguity",
                "field": field_name,
                "message": f"Multiple values were found for {field_name}; review the candidates.",
                "details": dict(item),
            }
        )
    for index, item in enumerate(uncertainties):
        field_name = str(item.get("field") or "campaign rule")
        reason = str(item.get("reason") or "The brief did not provide an explicit value.")
        warnings.append(
            {
                "id": f"uncertainty:{field_name}:{index}",
                "severity": "required" if field_name in {"platforms", "aspect_resolution", "name"} else "warning",
                "kind": "uncertainty",
                "field": field_name,
                "message": reason,
                "details": dict(item),
            }
        )
    return warnings


@dataclass
class CampaignProfile:
    schema_version: int = 2
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "Untitled campaign"
    length: dict[str, Any] = field(default_factory=dict)
    aspect_resolution: dict[str, Any] = field(default_factory=dict)
    platforms: list[str] = field(default_factory=list)
    captions_language: dict[str, Any] = field(default_factory=dict)
    copy_rules: dict[str, Any] = field(default_factory=dict)
    watermark_end_cards: dict[str, Any] = field(default_factory=dict)
    banned_words_topics: dict[str, Any] = field(default_factory=dict)
    source_rules: dict[str, Any] = field(default_factory=dict)
    source_library: list[dict[str, Any]] = field(default_factory=list)
    audio_rules: dict[str, Any] = field(default_factory=dict)
    meme_policy: dict[str, Any] = field(default_factory=dict)
    payout_deadlines: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    edit_policy: dict[str, Any] = field(default_factory=dict)
    quality_policy: dict[str, Any] = field(default_factory=dict)
    posting: dict[str, Any] = field(default_factory=dict)
    compliance: dict[str, Any] = field(default_factory=dict)
    source_links: list[str] = field(default_factory=list)
    source_revision: str = ""
    source_content_hash: str = ""
    last_synced_at: float = 0.0
    changed: bool = False
    warnings: list[dict[str, Any]] = field(default_factory=list)
    dismissed_warnings: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    ambiguities: list[dict[str, Any]] = field(default_factory=list)
    uncertainties: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    raw_text_hash: str = ""
    parser: str = "deterministic"
    editable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version or 2),
            "id": self.id,
            "name": self.name,
            "length": dict(self.length),
            "aspect_resolution": dict(self.aspect_resolution),
            "platforms": list(self.platforms),
            "captions_language": dict(self.captions_language),
            "copy_rules": dict(self.copy_rules),
            "watermark_end_cards": dict(self.watermark_end_cards),
            "banned_words_topics": dict(self.banned_words_topics),
            "source_rules": dict(self.source_rules),
            "source_library": [dict(item) for item in self.source_library],
            "audio_rules": dict(self.audio_rules),
            "meme_policy": dict(self.meme_policy),
            "payout_deadlines": dict(self.payout_deadlines),
            "settings": dict(self.settings),
            "edit_policy": dict(self.edit_policy),
            "quality_policy": dict(self.quality_policy),
            "posting": dict(self.posting),
            "compliance": dict(self.compliance),
            "source_links": list(self.source_links),
            "source_revision": self.source_revision,
            "source_content_hash": self.source_content_hash,
            "last_synced_at": float(self.last_synced_at or 0.0),
            "changed": bool(self.changed),
            "warnings": [dict(item) for item in self.warnings],
            "dismissed_warnings": list(self.dismissed_warnings),
            "created_at": float(self.created_at or 0.0),
            "updated_at": float(self.updated_at or 0.0),
            "ambiguities": list(self.ambiguities),
            "uncertainties": list(self.uncertainties),
            "provenance": list(self.provenance),
            "raw_text_hash": self.raw_text_hash,
            "parser": self.parser,
            "editable": self.editable,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "CampaignProfile":
        data = dict(value or {})
        known = {field_name for field_name in cls.__dataclass_fields__}
        payload = {key: data[key] for key in known if key in data}
        item = cls(**payload)
        for field_name in (
            "length",
            "aspect_resolution",
            "captions_language",
            "copy_rules",
            "watermark_end_cards",
            "banned_words_topics",
            "source_rules",
            "audio_rules",
            "meme_policy",
            "payout_deadlines",
            "settings",
            "edit_policy",
            "quality_policy",
            "posting",
            "compliance",
        ):
            if not isinstance(getattr(item, field_name), dict):
                setattr(item, field_name, {})
        if not isinstance(item.source_library, list):
            item.source_library = []
        normalized_sources: list[dict[str, Any]] = []
        for entry in item.source_library:
            if not isinstance(entry, Mapping):
                continue
            source = dict(entry)
            status = str(source.get("status") or "").casefold()
            if status not in {"approved", "suggested", "banned"}:
                status = "approved" if source.get("approved") is True else "suggested"
            source["status"] = status
            source["approved"] = status == "approved"
            normalized_sources.append(source)
        item.source_library = normalized_sources
        for field_name in (
            "platforms",
            "source_links",
            "warnings",
            "dismissed_warnings",
            "ambiguities",
            "uncertainties",
            "provenance",
        ):
            if not isinstance(getattr(item, field_name), list):
                setattr(item, field_name, [])
        if isinstance(item.posting.get("platforms"), list) and item.posting.get("platforms"):
            item.platforms = list(item.posting.get("platforms") or [])
        item.warnings = [
            dict(warning)
            if isinstance(warning, Mapping)
            else {
                "id": f"warning:{index}",
                "severity": "warning",
                "message": str(warning),
            }
            for index, warning in enumerate(item.warnings)
        ]
        try:
            item.schema_version = max(2, int(data.get("schema_version") or 1))
        except (TypeError, ValueError):
            item.schema_version = 2
        if not item.source_library:
            item.source_library = extract_source_manifest(
                "\n".join(item.source_links),
                allowed=item.source_rules.get("allowed", []),
                prohibited=item.source_rules.get("prohibited", []),
                origin="legacy_profile",
            )
        if "source_links" not in data:
            item.source_links = [
                str(entry.get("url") or "")
                for entry in item.source_library
                if str(entry.get("url") or "")
            ]
        approved_urls = [
            str(entry.get("url") or "")
            for entry in item.source_library
            if isinstance(entry, Mapping) and entry.get("status") == "approved" and entry.get("url")
        ]
        if approved_urls:
            item.source_rules["allowed"] = _unique(
                [*item.source_rules.get("allowed", []), *approved_urls]
            )
        item.posting.setdefault("platforms", list(item.platforms))
        item.posting.setdefault("manual_only", False)
        item.posting.setdefault("review_only", False)
        item.posting.setdefault("public_visibility_required", True)
        item.posting.setdefault("daily_clip_target", 2)
        item.posting.setdefault("timezone", "")
        item.posting.setdefault("peak_hours", [])
        item.watermark_end_cards.setdefault(
            "public_visibility_required",
            bool(item.posting.get("public_visibility_required", True)),
        )
        item.compliance.setdefault("required_copy", list(item.copy_rules.get("required_copy", [])))
        item.compliance.setdefault("required_hashtags", list(item.copy_rules.get("hashtags", [])))
        item.compliance.setdefault("required_mentions", list(item.copy_rules.get("mentions", [])))
        item.compliance.setdefault("banned_words", list(item.banned_words_topics.get("words", [])))
        item.compliance.setdefault("banned_topics", list(item.banned_words_topics.get("topics", [])))
        required_checks = item.compliance.get("required_checks")
        if isinstance(required_checks, Mapping):
            item.compliance["required_checks"] = [
                {"id": str(key), "label": str(value or key), "required": True}
                for key, value in required_checks.items()
            ]
        elif not isinstance(required_checks, list):
            item.compliance["required_checks"] = []
        if not item.warnings:
            item.warnings = _review_warnings(item.ambiguities, item.uncertainties)
        return item


def _record_conflict(
    ambiguities: list[dict[str, Any]], field_name: str, values: list[Any], source: str = "document"
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
            if len(cells) >= 2 and cells[0] and cells[1]:
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
    name = name_values[0] if name_values and len({item.casefold() for item in name_values}) == 1 else heading_name
    if not name_values and not heading_name:
        uncertainties.append({"field": "name", "reason": "no explicit campaign name found"})

    brand_values = all_values("brand", "brand name", "campaign brand", "company", "company name")
    _record_conflict(ambiguities, "brand", brand_values)

    length_values = all_values("length", "clip length", "duration", "durations", "video length")
    _record_conflict(ambiguities, "length", length_values)
    length = _length(length_values[0]) if length_values and len(length_values) == 1 else {}
    if length_values and not length:
        uncertainties.append({"field": "length", "reason": "duration is not a supported numeric range", "values": length_values})

    aspect_values = all_values("aspect", "aspect ratio", "aspect ratios", "format", "formats")
    ratios = _unique([_ratio(item) for value in aspect_values for item in _split(value) if _ratio(item)])
    resolution_values = all_values("resolution", "resolutions", "output resolution")
    aspect_resolution = {
        "aspect_ratios": ratios,
        "resolutions": _resolutions(" ".join([*aspect_values, *resolution_values])),
    }
    if aspect_values and not ratios:
        uncertainties.append({"field": "aspect_resolution", "reason": "no supported ratio was found", "values": aspect_values})

    platform_values = all_values("platform", "platforms", "allowed platforms", "publish to", "distribution")
    platforms = _unique([_platform(item) for value in platform_values for item in _split(value)])
    captions_values = all_values("captions", "caption", "caption rules", "language", "languages", "caption language")
    captions_language = {
        "required": _bool(_first(fields, "captions", "caption", "caption rules")),
        "languages": _values(_first(fields, "language", "languages", "caption language")),
        "style": _first(fields, "caption style", "caption preset", "caption format"),
    }
    if captions_values and captions_language["required"] is None and not captions_language["languages"]:
        uncertainties.append({"field": "captions_language", "reason": "caption instruction is not explicit", "values": captions_values})

    copy_rules = {
        "hashtags": _tags(" ".join(all_values("hashtags", "hash tags", "tags", "required hashtags"))),
        "mentions": _mentions(" ".join(all_values("mentions", "handles", "required mentions"))),
        "required_copy": all_values("required copy", "copy", "caption copy", "description", "script copy"),
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
        "public_visibility_required": True,
    }
    banned = all_values("banned words", "forbidden words", "prohibited words", "avoid words", "banned topics", "prohibited topics")
    banned_words = all_values("banned words", "forbidden words", "prohibited words", "avoid words")
    banned_topics = all_values("banned topics", "prohibited topics", "avoid topics")
    banned_words_topics = {"words": _values(" ".join(banned_words)), "topics": _values(" ".join(banned_topics))}
    source_rules = {
        "allowed": _values(" ".join(all_values("allowed sources", "source rules", "approved sources", "source"))),
        "prohibited": _values(" ".join(all_values("prohibited sources", "disallowed sources"))),
        "attribution": _first(fields, "source attribution", "credit"),
    }
    source_library = extract_source_manifest(
        raw,
        allowed=source_rules["allowed"],
        prohibited=source_rules["prohibited"],
    )
    audio_rules = {
        "allowed": _values(" ".join(all_values("allowed audio", "audio rules", "music"))),
        "prohibited": _values(" ".join(all_values("prohibited audio", "disallowed audio"))),
        "attribution": _first(fields, "audio attribution", "music attribution"),
    }
    meme_values = all_values("meme policy", "memes", "meme rules", "meme usage")
    meme_policy = {"allowed": _bool(meme_values[0]) if meme_values else None, "rules": meme_values}
    payout_values = all_values("payout", "payout rate", "compensation", "payment")
    deadline_values = all_values("deadline", "deadlines", "due date", "delivery date")
    payout_deadlines = {"payout": payout_values[0] if payout_values else "", "deadlines": deadline_values}

    # Creator-studio strategy is optional metadata, but it is still explicit campaign intent. Keep
    # it in the versioned settings block so the frontend can review it and the existing options
    # surface can carry the fields into the clipping job without inventing defaults.
    strategy_values = {
        "content_type": _first(fields, "content type", "content format", "video type"),
        "clip_goal": _first(fields, "clip goal", "content goal", "goal", "objective"),
        "target_audience": _first(fields, "target audience", "audience", "viewer"),
        "hook_style": _first(fields, "hook style", "hook approach"),
        "selection_prompt": _first(fields, "selection prompt", "clip prompt", "moments to find", "what to find"),
        "retention_mode": _choice(_first(fields, "retention mode", "retention ranking"), {"off", "balanced", "aggressive"}),
        "num_clips": canonical_clip_count(_first(fields, "number of clips", "clip count", "clips per source")),
        "strategy": _choice(_first(fields, "clip strategy", "selection strategy", "strategy"), {"ai", "silence", "fixed"}),
        "vibe": _choice(_first(fields, "vibe", "tone", "tone of voice"), {"energetic", "educational", "funny", "inspirational", "dramatic", "chill"}),
        "caption_position": _choice(_first(fields, "caption position", "caption placement"), {"bottom", "center", "top"}),
        "broll_intensity": _choice(_first(fields, "b-roll intensity", "broll intensity"), {"off", "subtle", "standard", "heavy"}),
        "asset_sourcing_mode": _choice(_first(fields, "asset sourcing", "asset sourcing mode"), {"off", "local_only", "local_then_external"}),
        "reframe_layout": _choice(_first(fields, "speaker layout", "reframe layout"), {"follow_active", "split_screen"}),
        "reframe_intensity": _choice(_first(fields, "speaker motion", "reframe intensity"), {"subtle", "standard", "heavy"}),
    }
    strategy_flags = {
        "storytelling_enabled": _bool(_first(fields, "storytelling", "story-first", "story structure")),
        "speaker_reframe": _bool(_first(fields, "speaker reframe", "active speaker framing")),
        "broll": _bool(_first(fields, "b-roll", "broll", "b roll", "b-roll allowed")),
        "hook_title": _bool(_first(fields, "hook title", "hook title overlay")),
        "subtitle_sidecar": _bool(_first(fields, "subtitle sidecar", "subtitle files")),
        "filler_removal": _bool(_first(fields, "filler removal", "remove filler")),
        "visual_selection": _bool(_first(fields, "visual selection", "visual cues")),
        "caption_keyword_highlight": _bool(_first(fields, "keyword highlight", "caption keyword highlight")),
        "caption_emoji": _bool(_first(fields, "caption emoji", "emoji in captions")),
        "permissibility_mode": _bool(_first(fields, "permissibility", "local-only assets", "local only assets")),
    }

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
        uncertainties.append({"field": "copy_rules", "reason": "no required caption or hashtag copy found"})

    warnings = _review_warnings(ambiguities, uncertainties)
    posting = {
        "platforms": list(platforms),
        "manual_only": False,
        "public_visibility_required": True,
        "daily_clip_target": 2,
        "timezone": "",
        "peak_hours": [],
    }
    compliance = {
        "required_copy": list(copy_rules["required_copy"]),
        "required_hashtags": list(copy_rules["hashtags"]),
        "required_mentions": list(copy_rules["mentions"]),
        "banned_words": list(banned_words_topics["words"]),
        "banned_topics": list(banned_words_topics["topics"]),
        "required_checks": [],
    }
    settings = {
        "clip_length": canonical_clip_length(length.get("raw", "") if length else "auto"),
        "aspect": ratios[0] if ratios else "",
        "platform": platforms[0] if platforms else "generic",
        "captions": captions_language.get("required") is not False,
        "caption_preset": captions_language.get("style", "") or "karaoke",
        "brand_logo": watermark_end_cards.get("watermark", ""),
        "brand_name": brand_values[0] if brand_values and len({item.casefold() for item in brand_values}) == 1 else "",
        "brand_cta": copy_rules.get("cta", ""),
        "storytelling_enabled": True,
    }
    settings.update({key: value for key, value in strategy_values.items() if value})
    settings.update({key: value for key, value in strategy_flags.items() if value is not None})

    return CampaignProfile(
        schema_version=2,
        name=name or "Untitled campaign",
        length=length,
        aspect_resolution=aspect_resolution,
        platforms=platforms,
        captions_language=captions_language,
        copy_rules=copy_rules,
        watermark_end_cards=watermark_end_cards,
        banned_words_topics=banned_words_topics,
        source_rules=source_rules,
        source_library=source_library,
        audio_rules=audio_rules,
        meme_policy=meme_policy,
        payout_deadlines=payout_deadlines,
        settings=settings,
        posting=posting,
        compliance=compliance,
        source_links=[str(item.get("url") or "") for item in source_library if item.get("url")],
        last_synced_at=time.time() if raw else 0.0,
        warnings=warnings,
        ambiguities=ambiguities,
        uncertainties=uncertainties,
        provenance=list(provenance or []),
        raw_text_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else "",
    )


parse_campaign_import = parse_campaign_profile

__all__ = ["CampaignProfile", "parse_campaign_profile", "parse_campaign_import"]
