"""Pure campaign presets, compliance policy, and daily publishing slots.

The module is intentionally independent from the API, job queue, and publisher manager. A
Google-Doc-style brief can be parsed and checked before rendering or publishing has side effects.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, time, tzinfo
from enum import Enum
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MISSING = object()
_RATIO_RE = re.compile(r"(?<!\d)(\d{1,4})\s*:\s*(\d{1,4})(?!\d)")
_TAG_RE = re.compile(r"(?<![\w])#([\w][\w-]*)", re.UNICODE)
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_ALIASES = {
    "youtube shorts": "youtube_shorts",
    "youtube short": "youtube_shorts",
    "shorts": "youtube_shorts",
    "instagram reels": "instagram",
    "instagram reel": "instagram",
    "reels": "instagram",
    "twitter": "x",
}


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().strip("*_`#")).casefold()


def _platform(value: Any) -> str:
    text = _label(value)
    return _ALIASES.get(text, text).replace(" ", "_")


def _ratio(value: Any) -> str:
    match = _RATIO_RE.search(str(value or ""))
    if not match:
        return ""
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return ""
    divisor = math.gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def _split(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = [str(item) for item in value]
    else:
        values = re.split(r"[,;|\n•]", str(value or ""))
    return _unique([re.sub(r"^\s*[-*]\s*", "", item).strip().strip("\"'`") for item in values])


def _hashtags(value: Any) -> list[str]:
    values = (
        value if isinstance(value, (list, tuple, set)) else re.split(r"[,;|\n•]", str(value or ""))
    )
    result: list[str] = []
    for raw in values:
        text = str(raw).strip()
        found = _TAG_RE.findall(text)
        if found:
            result.extend(f"#{item}" for item in found)
        else:
            bare = re.sub(r"[^\w-]", "", text, flags=re.UNICODE)
            if bare:
                result.append(f"#{bare}")
    return _unique(result)


def _color(value: Any) -> str:
    text = str(value or "").strip().strip("\"'`")
    if re.fullmatch(r"#[0-9a-fA-F]{3}", text):
        return "#" + "".join(char * 2 for char in text[1:])
    return text


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"1", "true", "yes", "y", "on", "enabled", "required", "public"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "optional", "private"}:
        return False
    return default


def _number(value: Any, default: float) -> float:
    match = _NUMBER_RE.search(str(value or ""))
    return float(match.group(0)) if match else default


def _get(source: Any, *names: str) -> Any:
    if source is None:
        return _MISSING
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return _MISSING


def _first(first: Any, second: Any, *names: str) -> Any:
    value = _get(first, *names)
    if value is not _MISSING and value is not None:
        if not isinstance(value, str) or value.strip():
            return value
    return _get(second, *names)


@dataclass
class Branding:
    """Required logo, font, and colour identity."""

    logo: str = ""
    font: str = ""
    colors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.logo = str(self.logo or "").strip()
        self.font = str(self.font or "").strip()
        self.colors = {
            str(key).strip().casefold(): _color(value)
            for key, value in dict(self.colors or {}).items()
            if str(key).strip() and str(value).strip()
        }

    @property
    def color(self) -> dict[str, str]:
        return self.colors


@dataclass
class HookSettings:
    enabled: bool = False
    duration_seconds: float = 3.0
    style: str = ""

    def __post_init__(self) -> None:
        self.enabled = bool(self.enabled)
        self.duration_seconds = max(0.0, float(self.duration_seconds))
        self.style = str(self.style or "").strip()


@dataclass
class CliffhangerSettings:
    enabled: bool = False
    duration_seconds: float = 2.0
    style: str = ""

    def __post_init__(self) -> None:
        self.enabled = bool(self.enabled)
        self.duration_seconds = max(0.0, float(self.duration_seconds))
        self.style = str(self.style or "").strip()


@dataclass
class CampaignPreset:
    """The complete deterministic policy for one campaign."""

    name: str = ""
    branding: Branding = field(default_factory=Branding)
    caption_style: str = ""
    aspect_ratios: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    cta: str = ""
    allowed_platforms: list[str] = field(default_factory=list)
    daily_clip_target: int = 2
    require_public_visibility: bool = True
    # Alias accepted at construction time because it is the longer, human-facing policy name.
    public_visibility_required: bool | None = None
    pattern_interrupter_interval: int = 0
    hook_settings: HookSettings = field(default_factory=HookSettings)
    cliffhanger_settings: CliffhangerSettings = field(default_factory=CliffhangerSettings)
    # Extra requirements extracted from natural-language briefs. Keeping campaign-specific rules
    # in this namespaced mapping lets new briefs add requirements without dropping them during the
    # ProcessingOptions/API round-trip. Known keys are validated and applied where possible; all
    # other extracted guidance stays visible for human review.
    brief_rules: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()
        if not isinstance(self.branding, Branding):
            self.branding = Branding(**dict(self.branding or {}))
        if not isinstance(self.hook_settings, HookSettings):
            self.hook_settings = HookSettings(**dict(self.hook_settings or {}))
        if not isinstance(self.cliffhanger_settings, CliffhangerSettings):
            self.cliffhanger_settings = CliffhangerSettings(**dict(self.cliffhanger_settings or {}))
        self.caption_style = str(self.caption_style or "").strip()
        aspect_values = _split(self.aspect_ratios)
        self.aspect_ratios = _unique([_ratio(value) for value in aspect_values if _ratio(value)])
        self.hashtags = _hashtags(self.hashtags)
        self.cta = str(self.cta or "").strip()
        platform_values = _split(self.allowed_platforms)
        self.allowed_platforms = _unique(
            [_platform(value) for value in platform_values if str(value).strip()]
        )
        self.daily_clip_target = max(1, int(self.daily_clip_target or 2))
        if self.public_visibility_required is None:
            self.public_visibility_required = bool(self.require_public_visibility)
        self.public_visibility_required = bool(self.public_visibility_required)
        self.require_public_visibility = self.public_visibility_required
        self.pattern_interrupter_interval = max(0, int(self.pattern_interrupter_interval or 0))
        self.brief_rules = (
            deepcopy(dict(self.brief_rules or {})) if isinstance(self.brief_rules, Mapping) else {}
        )

    @property
    def brand(self) -> Branding:
        return self.branding

    @property
    def logo(self) -> str:
        return self.branding.logo

    @property
    def font(self) -> str:
        return self.branding.font

    @property
    def colors(self) -> dict[str, str]:
        return self.branding.colors

    @property
    def caption(self) -> str:
        return self.caption_style

    @property
    def platforms(self) -> list[str]:
        return self.allowed_platforms

    @property
    def public_visibility(self) -> bool:
        return self.require_public_visibility

    @property
    def daily_target(self) -> int:
        return self.daily_clip_target

    @property
    def pattern_interval(self) -> int:
        return self.pattern_interrupter_interval

    @property
    def hook(self) -> HookSettings:
        return self.hook_settings

    @property
    def cliffhanger(self) -> CliffhangerSettings:
        return self.cliffhanger_settings

    def required_mentions_for(self, platform: str) -> list[str]:
        """Return required tags for one destination, with common tags as a fallback."""
        rule = self.brief_rules.get("required_mentions", {})
        if not isinstance(rule, Mapping):
            return []
        by_platform = rule.get("by_platform", {})
        if not isinstance(by_platform, Mapping):
            by_platform = {}
        key = _platform(platform)
        aliases = [key]
        if key == "youtube":
            aliases.append("youtube_shorts")
        elif key == "youtube_shorts":
            aliases.append("youtube")
        elif key == "instagram_reels":
            aliases.append("instagram")
        elif key == "instagram":
            aliases.append("instagram_reels")
        for alias in aliases:
            values = by_platform.get(alias)
            if isinstance(values, str):
                values = _split(values)
            if isinstance(values, (list, tuple, set)) and values:
                return _unique([str(item).strip() for item in values if str(item).strip()])
        common = rule.get("common", [])
        return _unique([str(item).strip() for item in _split(common) if str(item).strip()])

    def required_mentions_by_platform(self) -> dict[str, list[str]]:
        """Return required mentions keyed by all permitted platform spellings."""
        rule = self.brief_rules.get("required_mentions", {})
        by_platform = rule.get("by_platform", {}) if isinstance(rule, Mapping) else {}
        platforms = self.allowed_platforms or (
            list(by_platform) if isinstance(by_platform, Mapping) else []
        )
        return {platform: self.required_mentions_for(platform) for platform in platforms}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "branding": {
                "logo": self.branding.logo,
                "font": self.branding.font,
                "colors": dict(self.branding.colors),
            },
            "caption_style": self.caption_style,
            "aspect_ratios": list(self.aspect_ratios),
            "hashtags": list(self.hashtags),
            "cta": self.cta,
            "allowed_platforms": list(self.allowed_platforms),
            "daily_clip_target": self.daily_clip_target,
            "require_public_visibility": self.require_public_visibility,
            "public_visibility_required": self.public_visibility_required,
            "pattern_interrupter_interval": self.pattern_interrupter_interval,
            "hook_settings": self.hook_settings.__dict__.copy(),
            "cliffhanger_settings": self.cliffhanger_settings.__dict__.copy(),
            "brief_rules": deepcopy(self.brief_rules),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> CampaignPreset:
        data = dict(value or {})
        branding = data.get("branding", data.get("brand", {}))
        if not branding:
            branding = {
                "logo": _split(data.get("logo_assets", data.get("logos", [])))[:1],
                "font": _split(data.get("font_assets", data.get("fonts", [])))[:1],
            }
            branding["logo"] = branding["logo"][0] if branding["logo"] else ""
            branding["font"] = branding["font"][0] if branding["font"] else ""
        hook = data.get("hook_settings", data.get("hook", {}))
        cliffhanger = data.get("cliffhanger_settings", data.get("cliffhanger", {}))
        return cls(
            name=data.get("name", ""),
            branding=branding
            if isinstance(branding, Branding)
            else Branding(**dict(branding or {})),
            caption_style=data.get("caption_style", data.get("caption", "")),
            aspect_ratios=data.get("aspect_ratios", data.get("target_aspect_ratios", [])),
            hashtags=data.get("hashtags", []),
            cta=data.get("cta", data.get("call_to_action", "")),
            allowed_platforms=data.get(
                "allowed_platforms",
                data.get(
                    "platforms",
                    data.get("allowed", list((data.get("platform_metadata") or {}).keys())),
                ),
            ),
            daily_clip_target=data.get("daily_clip_target", data.get("daily_target", 2)),
            require_public_visibility=data.get(
                "require_public_visibility", data.get("public_visibility_required", True)
            ),
            pattern_interrupter_interval=data.get(
                "pattern_interrupter_interval", data.get("pattern_interval", 0)
            ),
            hook_settings=hook
            if isinstance(hook, HookSettings)
            else HookSettings(**dict(hook or {})),
            cliffhanger_settings=(
                cliffhanger
                if isinstance(cliffhanger, CliffhangerSettings)
                else CliffhangerSettings(**dict(cliffhanger or {}))
            ),
            brief_rules=deepcopy(data.get("brief_rules", {}))
            if isinstance(data.get("brief_rules", {}), Mapping)
            else {},
        )


# The brief parser runs on operator-supplied text (`POST /api/campaign-presets/parse`), so every
# pattern below is linear-time. The earlier one-liners placed a lazy run next to `\s*` over
# overlapping character classes and were scanned from every start position by `findall`/`sub`,
# which CodeQL rated py/polynomial-redos (severity 7.5): a long run of spaces or digits made a
# single request quadratic. The helpers keep the exact results of those regexes - pinned by the
# property tests in tests/test_campaign_preset.py - while doing the ambiguous part by hand.
_LINE_SEPARATORS = ":=–-"
_LINE_LABEL_MAX = 80
_COLOR_VALUE_RE = re.compile(r"\s*(#[\da-fA-F]{3,8}|[\w-]+)")
_COLOR_SEPARATOR_RE = re.compile(r"[:=]")
# Tried in the order the regex alternation `(seconds?|secs?|s)` tried them.
_DURATION_UNITS = ("seconds", "second", "secs", "sec", "s")


def _split_label_line(text: str) -> tuple[str, str] | None:
    """Split ``label: value`` exactly as ``^(?:#+\\s*)?([^:=–-]{1,80}?)(?:\\s*[:=–-]\\s*(.*))?$``.

    The label cannot contain a separator, so it ends at the first one (or the end of the line).
    It starts after the ``#`` heading marker; if that would leave it empty, the regex backed off
    the marker by one character, which is reproduced here. ``text`` is a single line.
    """

    hashes = len(text) - len(text.lstrip("#"))
    start = len(text) - len(text[hashes:].lstrip()) if hashes else 0
    cut = min((text.find(sep) for sep in _LINE_SEPARATORS if sep in text), default=-1)
    if (len(text) if cut < 0 else cut) == start:
        if start == 0:
            return None
        start -= 1
    if cut < 0:
        label = text[start:]
    else:
        label = text[start:cut].rstrip() or text[start : start + 1]
    if len(label) > _LINE_LABEL_MAX:
        return None
    return label, ("" if cut < 0 else text[cut + 1 :].strip())


def _line_parts(line: str) -> tuple[str, str] | None:
    text = line.strip().strip("*_`")
    text = re.sub(r"^[-*•]\s*", "", text)
    if not text:
        return None
    parts = _split_label_line(text)
    if parts is None:
        return None
    return _label(parts[0]), parts[1]


def _is_color_key_char(ch: str) -> bool:
    return ch == " " or ch == "-" or ch == "_" or ch.isalnum()


def _color_pairs(value: str) -> list[tuple[str, str]]:
    """``key: value`` pairs, as ``re.findall(r"([\\w -]+?)\\s*[:=]\\s*(#hex|[\\w-]+)")`` gave.

    Each ``:``/``=`` is examined once. Its key is the run of word/space/hyphen characters that
    ends where the whitespace before the separator begins, bounded by the end of the previous
    pair; when there is no such run the regex settled for a single space, if one is present.
    """

    pairs: list[tuple[str, str]] = []
    floor = 0
    for sep in _COLOR_SEPARATOR_RE.finditer(value):
        if sep.start() < floor:
            continue
        ws_start = sep.start()
        while ws_start > floor and value[ws_start - 1].isspace():
            ws_start -= 1
        key_start = ws_start
        while key_start > floor and _is_color_key_char(value[key_start - 1]):
            key_start -= 1
        if key_start < ws_start:
            key = value[key_start:ws_start]
        else:
            key = " " if value.find(" ", ws_start, sep.start()) >= 0 else ""
        item = _COLOR_VALUE_RE.match(value, sep.end())
        if key and item:
            pairs.append((key, item.group(1)))
            floor = item.end()
    return pairs


def _unit_char_matches(ch: str, wanted: str) -> bool:
    # `re.IGNORECASE` on str patterns also folds the long s (U+017F) onto "s".
    return ch.lower() == wanted or (wanted == "s" and ch == "\u017f")


def _duration_end(text: str, index: int) -> int | None:
    """End of a ``[-+]?\\d+(?:\\.\\d+)?\\s*(seconds?|secs?|s)\\b`` match at ``index``, if any.

    Hand-written so no regex quantifier runs over the digits. Backtracking into a digit run could
    never help the regex (a shorter run is followed by a digit, which no unit accepts), so only
    the with-fraction and without-fraction ends are tried, in that order.
    """

    size = len(text)
    start = index + 1 if text[index] in "+-" else index
    digits_end = start
    while digits_end < size and text[digits_end].isdecimal():
        digits_end += 1
    if digits_end == start:
        return None
    ends = []
    if digits_end + 1 < size and text[digits_end] == "." and text[digits_end + 1].isdecimal():
        fraction_end = digits_end + 1
        while fraction_end < size and text[fraction_end].isdecimal():
            fraction_end += 1
        ends.append(fraction_end)
    ends.append(digits_end)
    for number_end in ends:
        unit_start = number_end
        while unit_start < size and text[unit_start].isspace():
            unit_start += 1
        for unit in _DURATION_UNITS:
            unit_end = unit_start + len(unit)
            if unit_end > size or not all(
                _unit_char_matches(text[unit_start + offset], wanted)
                for offset, wanted in enumerate(unit)
            ):
                continue
            # `\b`: the unit must not run on into another word character.
            if unit_end == size or not (text[unit_end].isalnum() or text[unit_end] == "_"):
                return unit_end
    return None


def _strip_durations(text: str) -> str:
    """Remove ``5s`` / ``1.5 seconds`` tokens exactly as the former ``re.sub`` did.

    A match is only attempted where a number begins. Starting later inside the same digit run
    sees the same suffix and fails the same way, so skipping those positions changes nothing
    and keeps the scan linear.
    """

    pieces: list[str] = []
    kept_from = 0
    index = 0
    while index < len(text):
        ch = text[index]
        prev = text[index - 1] if index else ""
        signed = ch in "+-" and text[index + 1 : index + 2].isdecimal()
        number_start = ch.isdecimal() and not (prev and (prev.isdecimal() or prev in "+-"))
        if signed or number_start:
            end = _duration_end(text, index)
            if end is not None:
                pieces.append(text[kept_from:index])
                kept_from = index = end
                continue
        index += 1
    pieces.append(text[kept_from:])
    return "".join(pieces)


def _colors(value: str) -> dict[str, str]:
    pairs = _color_pairs(value)
    if pairs:
        return {_label(key): _color(item) for key, item in pairs}
    hexes = re.findall(r"#[\da-fA-F]{3,8}\b", value)
    values = [_color(item) for item in hexes] or [_color(item) for item in _split(value)]
    names = ("primary", "secondary", "accent", "background")
    return {
        names[index] if index < len(names) else f"color_{index + 1}": item
        for index, item in enumerate(values)
    }


def _setting(value: str, default_duration: float) -> tuple[bool, float, str]:
    text = str(value or "").strip()
    enabled = _bool(text, True)
    if any(word in text.casefold() for word in ("off", "disable", "none", "false", "no")):
        enabled = False
    duration = _number(text, default_duration)
    style = re.sub(r"\b(enabled|disabled|on|off|true|false|yes|no)\b", "", text, flags=re.I)
    style = _strip_durations(style)
    return enabled, duration, style.strip(" ,;:/-")


_BRIEF_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_BARE_SOCIAL_URL_RE = re.compile(
    r"(?<![\w@])(?:www\.)?(?:instagram\.com|youtube\.com|youtu\.be)/[A-Za-z0-9_?=&./%-]+",
    re.IGNORECASE,
)
_BRIEF_MENTION_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,62}[A-Za-z0-9_])?")
_BRIEF_BARE_YT_HANDLE_RE = re.compile(
    r"(?<![\w@])@?([A-Za-z0-9_.]{2,64})\s+on\s+(?:yt|youtube)\b", re.IGNORECASE
)
_BRIEF_DURATION_RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,4}(?:\.\d{1,2})?)\s*(?:-|–|—|to)\s*"
    r"(\d{1,4}(?:\.\d{1,2})?)\s*(seconds?|secs?|s)\b",
    re.IGNORECASE,
)
_BRIEF_CURRENCY_RE = re.compile(r"\$\s*(\d{1,12}(?:,\d{3}){0,3}(?:\.\d{1,2})?)")
_BRIEF_PERCENT_RE = re.compile(r"(?<![\d.])(\d{1,4}(?:\.\d{1,2})?)\s*%")


def _brief_lines(text: str) -> list[str]:
    """Flatten pasted/Docs-exported briefs into safe, non-empty display lines."""
    result: list[str] = []
    for raw in str(text or "").replace("\v", "\n").splitlines():
        line = raw.strip().replace("\u00a0", " ")
        for prefix in ("- ", "* ", "• ", "✓ ", "✗ ", "⚠ "):
            if line.startswith(prefix):
                line = line[len(prefix) :].strip()
                break
        line = re.sub(r"^\d+[.)]\s+", "", line)
        if line:
            result.append(line)
    return result


def _brief_norm(value: str) -> str:
    text = str(value or "").strip()
    while text.startswith("#"):
        text = text[1:].lstrip()
    text = text.strip("*_` ").casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _brief_label_values(lines: list[str], labels: set[str]) -> list[str]:
    """Read both ``Label: value`` and Google-Doc quick-fact ``Label / value`` rows."""
    values: list[str] = []
    for index, line in enumerate(lines):
        parts = _line_parts(line)
        if parts is None or _brief_norm(parts[0]) not in labels:
            continue
        value = str(parts[1] or "").strip()
        if not value:
            for candidate in lines[index + 1 : index + 3]:
                normalized = _brief_norm(candidate)
                if normalized in labels or normalized in {
                    "quick facts",
                    "what to do",
                    "where to get the clips",
                    "what to clip",
                    "where to post",
                    "the rules",
                    "caption required",
                    "on screen text required",
                    "your audience required",
                    "how to submit required",
                }:
                    break
                value = candidate.strip()
                if value:
                    break
        if value:
            values.append(value)
    return values


def _brief_platforms(value: str) -> list[str]:
    text = str(value or "").casefold()
    found: list[str] = []
    for pattern, platform in (
        (r"\byoutube\s+shorts?\b|\bshorts\b", "youtube_shorts"),
        (r"\binstagram\s+reels?\b|\breels\b", "instagram"),
        (r"\btiktok\b", "tiktok"),
        (r"\bfacebook\s+reels?\b", "facebook_reels"),
        (r"\bwhop\b", "whop"),
        (r"\bx\s*\(twitter\)\b|\btwitter\b", "x"),
    ):
        if re.search(pattern, text, re.IGNORECASE):
            found.append(platform)
    return _unique(found)


def _brief_section(lines: list[str], starts: set[str], stops: set[str]) -> list[str]:
    active = False
    result: list[str] = []
    for line in lines:
        normalized = _brief_norm(line)
        if normalized in starts:
            active = True
            continue
        if active and normalized in stops:
            active = False
        if active:
            result.append(line)
    return result


def _extract_campaign_brief_rules(text: str) -> tuple[str, list[str], dict[str, Any]]:
    """Extract common natural-language campaign rules without requiring a fixed template.

    The parser is intentionally deterministic: campaign requirements are reviewable, stored with
    each job, and do not depend on an LLM guessing a rule from private source material.
    """
    lines = _brief_lines(text)
    full_text = "\n".join(lines)
    folded = full_text.casefold()
    title = ""
    for line in lines:
        parts = _line_parts(line)
        if (
            parts
            and _brief_norm(parts[0]) in {"campaign", "campaign name", "name", "title"}
            and parts[1]
        ):
            title = parts[1]
            break
    if not title:
        for line in lines[:8]:
            candidate = line.lstrip("# ").strip("*_` ")
            if not candidate or _brief_norm(candidate) in {
                "quick facts",
                "the goal",
                "what to do",
                "where to get the clips",
                "where to post",
                "what to clip",
                "the rules",
            }:
                continue
            if ":" in candidate and len(candidate.split(":", 1)[0]) < 25:
                continue
            title = re.sub(r"(?i)^clipfarm\s*[×·|:-]\s*", "", candidate).strip()
            if title.isupper():
                title = title.title()
            break

    platform_lines = _brief_label_values(
        lines, {"platform", "platforms", "allowed platforms", "publish to"}
    )
    platform_lines.extend(
        _brief_section(
            lines,
            {"where to post", "platforms", "match the topic to the platform"},
            {
                "caption",
                "caption required",
                "on screen text required",
                "your audience required",
                "how to submit required",
                "the rules",
            },
        )
    )
    platforms = _unique(
        platform for value in platform_lines for platform in _brief_platforms(value)
    )

    # Content-by-platform guidance usually appears as one platform name followed by a short list
    # of topics. Keep only the first line after each platform heading so unrelated later sections
    # cannot accidentally become campaign topics.
    platform_topics: dict[str, list[str]] = {}
    topic_section = _brief_section(
        lines,
        {"match the topic to the platform"},
        {"where to post", "caption", "caption required", "on screen text required", "the rules"},
    )
    current_platform = ""
    for line in topic_section:
        candidates = _brief_platforms(line)
        if candidates:
            current_platform = candidates[0]
            continue
        if current_platform and line.strip():
            platform_topics.setdefault(current_platform, []).append(line.strip())
            current_platform = ""

    aspect_values = _brief_label_values(
        lines,
        {"aspect", "aspect ratio", "aspect ratios", "target aspect ratios", "format", "formats"},
    )
    inferred_ratios = _unique(
        _ratio(value) for item in aspect_values for value in _split(item) if _ratio(value)
    )
    if (
        not inferred_ratios
        and platforms
        and set(platforms).issubset({"youtube_shorts", "instagram", "tiktok", "facebook_reels"})
    ):
        inferred_ratios = ["9:16"]

    rules: dict[str, Any] = {
        "daily_clip_target_explicit": bool(
            _brief_label_values(
                lines, {"daily clip target", "daily clips", "clips per day", "daily target"}
            )
        )
    }
    if inferred_ratios:
        rules["inferred_aspect_ratios"] = inferred_ratios
    duration_match = _BRIEF_DURATION_RANGE_RE.search(full_text)
    if duration_match:
        rules["clip_duration_seconds"] = {
            "min": float(duration_match.group(1)),
            "max": float(duration_match.group(2)),
        }

    required_subtitles = any(
        phrase in folded
        for phrase in (
            "subtitles on the whole clip",
            "captions on the whole clip",
            "subtitles throughout",
            "captions throughout",
            "subtitles for the entire clip",
            "captions for the entire clip",
        )
    ) or ("subtitle" in folded and "required" in folded and "whole clip" in folded)
    if required_subtitles:
        rules["subtitles_required"] = True

    guidance_lines = []
    for line in lines:
        lower = line.casefold()
        if any(
            token in lower
            for token in (
                "write your own",
                "lead with",
                "not a summary",
                "no clickbait",
                "misleading caption",
                "subtitles on",
                "big, readable",
                "accurate",
                "keep the original meaning",
            )
        ):
            if line not in guidance_lines:
                guidance_lines.append(line)
    if guidance_lines:
        rules["caption_guidance"] = " ".join(guidance_lines[:8])[:1000]

    font_match = re.search(
        r"(?:official\s+font\s+is|suggested\s+font\s*:)\s*[‘'\"“]?([A-Za-z0-9 _-]{2,40})",
        full_text,
        re.IGNORECASE,
    )
    if font_match:
        suggested_font = font_match.group(1).strip(" ‘'\"“”’.,;:-")
        if suggested_font:
            rules["suggested_caption_font"] = suggested_font

    mention_lines = [
        line
        for line in lines
        if _BRIEF_MENTION_RE.search(line)
        and any(token in line.casefold() for token in ("tag", "mention", "caption", "tagging"))
    ]
    common_mentions: list[str] = []
    youtube_mentions: list[str] = []
    instagram_mentions: list[str] = []
    for line in mention_lines:
        handles = _BRIEF_MENTION_RE.findall(line)
        if "on yt" in line.casefold() or "on youtube" in line.casefold():
            groups = re.findall(r"\(([^)]{0,200})\)", line)
            special = [handle for group in groups for handle in _BRIEF_MENTION_RE.findall(group)]
            special.extend(
                "@" + match.group(1).lstrip("@")
                for match in _BRIEF_BARE_YT_HANDLE_RE.finditer(line)
            )
            youtube_mentions.extend(special)
            common_mentions.extend(handle for handle in handles if handle not in special)
        elif "on instagram" in line.casefold() or "on reels" in line.casefold():
            instagram_mentions.extend(handles)
        else:
            common_mentions.extend(handles)
    common_mentions = _unique(common_mentions)
    youtube_mentions = _unique(youtube_mentions)
    instagram_mentions = _unique(instagram_mentions)
    if common_mentions or youtube_mentions or instagram_mentions:
        by_platform: dict[str, list[str]] = {}
        if youtube_mentions:
            # A YouTube handle may be an alternate spelling of the Instagram/public handle
            # (e.g. @iammarkmanson vs @markmanson); keep the common campaign account but do not
            # require both variants on the same destination.
            youtube_base = [
                handle
                for handle in common_mentions
                if not any(
                    handle.lstrip("@").casefold() in candidate.lstrip("@").casefold()
                    or candidate.lstrip("@").casefold() in handle.lstrip("@").casefold()
                    for candidate in youtube_mentions
                )
            ]
            by_platform["youtube_shorts"] = _unique([*youtube_base, *youtube_mentions])
        if instagram_mentions:
            by_platform["instagram"] = _unique(instagram_mentions)
        rules["required_mentions"] = {
            "common": common_mentions,
            "by_platform": by_platform,
        }

    # Explicitly labeled source blocks in Docs exports are easier to read than paragraphs scattered
    # through a template. Treat the Instagram reference as styling guidance, not an approved footage source.
    source_lines = _brief_section(
        lines,
        {"where to get the clips", "approved sources", "source rules", "where to get clips"},
        {
            "do not use these parts",
            "what to clip",
            "match the topic to the platform",
            "where to post",
            "caption",
            "caption required",
            "the rules",
        },
    )
    source_lines.extend(
        line
        for line in lines
        if any(
            token in line.casefold()
            for token in (
                "do not use any clip from outside",
                "clip only from",
                "outside clips get rejected",
                "other podcasts from mark",
                "official episodes only",
            )
        )
    )
    approved_sources: list[str] = []
    style_references: list[str] = []
    for line in source_lines:
        urls = [match.group(0).rstrip('.,;:!?)"') for match in _BRIEF_URL_RE.finditer(line)]
        urls.extend(
            match.group(0).rstrip('.,;:!?)"') for match in _BARE_SOCIAL_URL_RE.finditer(line)
        )
        normalized_urls = _unique(
            url if url.casefold().startswith("http") else "https://" + url for url in urls
        )
        if "style reference" in line.casefold() or "own reels" in line.casefold():
            style_references.extend(normalized_urls)
        elif any(
            token in line.casefold()
            for token in ("official", "use only", "only the files", "source")
        ):
            approved_sources.extend(normalized_urls)
    prohibited_sources = [
        line.strip()
        for line in source_lines
        if any(
            token in line.casefold()
            for token in ("do not use", "outside", "other podcasts", "not be accepted")
        )
    ]
    if approved_sources or style_references or prohibited_sources:
        rules["source_policy"] = {
            "approved_sources": _unique(approved_sources),
            "approved_channel_handles": _unique(
                [
                    path.split("/", 2)[1]
                    for url in approved_sources
                    if (path := urlsplit(url).path).startswith("/@")
                ]
            ),
            "style_references": _unique(style_references),
            "allow_only_approved_sources": any(
                token in " ".join(source_lines).casefold() for token in ("only", "outside")
            ),
            "prohibited_sources": _unique(prohibited_sources),
        }

    prohibited_content = [
        line.strip()
        for line in lines
        if any(
            token in line.casefold()
            for token in (
                "ad reads",
                "sponsor segments",
                "misrepresents",
                "mid-sentence",
                "mid-thought",
                "nsfw",
                "violent",
                "off-topic",
                "hidden likes",
                "fake views",
                "bots",
                "paid ads",
                "boost and promote",
                "same video pushed",
                "stealing or reposting",
            )
        )
    ]
    if prohibited_content:
        rules["prohibited_content"] = _unique(prohibited_content)

    face_match = re.search(
        r"face.{0,60}first\s+(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b",
        full_text,
        re.IGNORECASE,
    ) or re.search(
        r"first\s+(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b.{0,60}face",
        full_text,
        re.IGNORECASE,
    )
    if face_match:
        rules["face_visible_within_seconds"] = float(face_match.group(1))
        face_context = next(
            (
                line
                for line in lines
                if "face" in line.casefold() and re.search(r"\bfirst\s+\d", line, re.IGNORECASE)
            ),
            full_text,
        )
        subject_match = (
            re.search(
                r"\b(?:(?i:show|keep|ensure|put|have|make\s+sure)\s+(?:the\s+)?)"
                r"([A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,2})['’]s\s+face",
                face_context,
            )
            or re.search(
                r"\b([A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,2})['’]s\s+face",
                face_context,
            )
            or re.search(
                r"\bface\s+of\s+(?:the\s+)?([A-Z][A-Za-z'’.-]*(?:\s+[A-Z][A-Za-z'’.-]*){0,2})",
                face_context,
            )
        )
        if subject_match:
            rules["face_subject"] = subject_match.group(1).strip()

    audience_values = _brief_label_values(
        lines, {"audience", "your audience", "audience requirement"}
    )
    audience_lines = _brief_section(
        lines,
        {"your audience", "your audience required", "audience"},
        {
            "how to submit",
            "how to submit required",
            "the rules",
            "you must",
            "you will not get paid if",
        },
    )
    audience_text = " ".join([*audience_values, *audience_lines])
    audience_match = _BRIEF_PERCENT_RE.search(audience_text)
    regions = []
    for pattern, region in (
        (r"\b(?:us|u\.s\.|united states)\b", "US"),
        (r"\b(?:uk|u\.k\.|united kingdom)\b", "UK"),
        (r"\b(?:ca|canada)\b", "CA"),
        (r"\b(?:aus|australia)\b", "AU"),
    ):
        if re.search(pattern, audience_text, re.IGNORECASE):
            regions.append(region)
    if audience_match or regions:
        audience_rule: dict[str, Any] = {"regions": _unique(regions)}
        if audience_match:
            audience_rule["minimum_percent"] = float(audience_match.group(1))
        rules["audience_requirement"] = audience_rule

    engagement_line = next(
        (line for line in lines if "engagement" in line.casefold() and "%" in line), ""
    )
    engagement_match = _BRIEF_PERCENT_RE.search(engagement_line)
    if engagement_match:
        rules["minimum_engagement_percent"] = float(engagement_match.group(1))
    post_line = next(
        (line for line in lines if "post" in line.casefold() and "day" in line.casefold()), ""
    )
    if post_line and any(
        token in post_line.casefold() for token in ("keep", "at least", "minimum")
    ):
        days = re.search(r"(\d{1,4})\s+days?\b", post_line, re.IGNORECASE)
        if days:
            rules["minimum_post_days"] = int(days.group(1))
    if "likes are visible" in folded or "hidden likes" in folded:
        rules["likes_must_be_visible"] = True

    quick_fact_values = {
        "cpm": _brief_label_values(lines, {"cpm", "payout rate"}),
        "maximum_per_clip": _brief_label_values(
            lines, {"max per clip", "maximum per clip", "clip maximum"}
        ),
        "minimum_payout": _brief_label_values(lines, {"min payout", "minimum payout"}),
    }
    payout: dict[str, Any] = {}
    cpm_text = " ".join(quick_fact_values["cpm"])
    currency = _BRIEF_CURRENCY_RE.search(cpm_text)
    if currency:
        payout["cpm_usd"] = float(currency.group(1).replace(",", ""))
    elif cpm_text:
        number = re.search(r"(\d+(?:\.\d+)?)", cpm_text)
        if number:
            payout["cpm_usd"] = float(number.group(1))
    for key, label in (
        ("maximum_per_clip_usd", "maximum_per_clip"),
        ("minimum_payout_usd", "minimum_payout"),
    ):
        match = _BRIEF_CURRENCY_RE.search(" ".join(quick_fact_values[label]))
        if match:
            payout[key] = float(match.group(1).replace(",", ""))
    if payout:
        rules["payout"] = payout

    clip_formats = []
    in_clip_section = False
    for line in lines:
        normalized = _brief_norm(line)
        if normalized == "what to clip":
            in_clip_section = True
            continue
        if in_clip_section and normalized in {
            "where to post",
            "the rules",
            "caption",
            "caption required",
        }:
            in_clip_section = False
        if in_clip_section and "|" in line:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if (
                len(cells) >= 2
                and cells[0]
                and cells[1]
                and _brief_norm(cells[0]) not in {"what", "what it looks like"}
            ):
                clip_formats.append({"name": cells[0], "guidance": " | ".join(cells[1:])})
    if clip_formats:
        rules["clip_formats"] = clip_formats

    selection_lines = [
        line.strip()
        for line in lines
        if any(
            token in line.casefold()
            for token in ("focus on the latest episodes", "latest episodes first", "the goal:")
        )
    ]
    if selection_lines:
        rules["selection_guidance"] = " ".join(selection_lines[:3])
    if platform_topics:
        rules["platform_topic_guidance"] = platform_topics

    return title, platforms, rules


def parse_campaign_preset(text: str) -> CampaignPreset:
    """Parse a human-authored Google-Doc-style brief deterministically."""

    name, detected_platforms, brief_rules = _extract_campaign_brief_rules(text)
    brand = Branding()
    caption = ""
    ratios: list[str] = list(brief_rules.get("inferred_aspect_ratios", []))
    tags: list[str] = []
    cta = ""
    platforms: list[str] = []
    daily = 2
    public = True
    interval = 0
    hook = HookSettings()
    cliffhanger = CliffhangerSettings()
    section = ""

    for raw in str(text or "").splitlines():
        parts = _line_parts(raw)
        if parts is None:
            continue
        label, value = parts
        if not value and label in {
            "branding",
            "brand",
            "brand kit",
            "captions",
            "caption",
            "hook",
            "hook settings",
            "cliffhanger",
            "cliffhanger settings",
            "publishing",
            "schedule",
            "daily policy",
        }:
            section = label
            continue
        if label in {"campaign", "campaign name", "name", "title"} and value and not name:
            name = value
        elif label in {"logo", "logo asset", "logo url", "brand logo"}:
            brand.logo = value
        elif label in {"font", "font family", "font asset", "brand font"}:
            brand.font = value
        elif label in {"colors", "colours", "brand colors", "brand colours", "color palette"}:
            brand.colors.update(_colors(value))
        elif label in {
            "primary color",
            "primary colour",
            "secondary color",
            "secondary colour",
            "accent color",
            "accent colour",
            "background color",
            "background colour",
        }:
            key = label.replace("colour", "color").replace(" ", "_")
            brand.colors[key] = _color(value)
        elif label in {"caption style", "caption preset", "caption animation", "style"}:
            caption = value
        elif label in {"aspect ratios", "aspect ratio", "target aspect ratios", "formats"}:
            ratios.extend(_ratio(item) for item in _split(value) if _ratio(item))
        elif label in {"hashtags", "hash tags", "tags"}:
            tags.extend(_hashtags(value))
        elif label in {"cta", "call to action", "call-to-action"}:
            cta = value
        elif label in {"allowed platforms", "platforms", "publish to", "publishing platforms"}:
            platforms.extend(_platform(item) for item in _split(value))
        elif label in {"daily clip target", "daily clips", "clips per day", "daily target"}:
            daily = max(1, int(_number(value, 2)))
        elif label in {
            "public visibility",
            "public visibility required",
            "require public visibility",
            "visibility",
        }:
            public = _bool(value, public)
        elif label in {
            "pattern interrupter interval",
            "pattern interrupter",
            "interrupter interval",
        }:
            interval = max(0, int(_number(value, 0)))
        elif label in {"hook", "hook settings", "hook style", "hook duration"} or (
            section in {"hook", "hook settings"} and label in {"enabled", "duration", "style"}
        ):
            enabled, duration, style = _setting(value, 3.0)
            hook = HookSettings(enabled, duration, value if label == "hook style" else style)
        elif label in {
            "cliffhanger",
            "cliffhanger settings",
            "cliffhanger style",
            "cliffhanger duration",
        } or (
            section in {"cliffhanger", "cliffhanger settings"}
            and label in {"enabled", "duration", "style"}
        ):
            enabled, duration, style = _setting(value, 2.0)
            cliffhanger = CliffhangerSettings(
                enabled, duration, value if label == "cliffhanger style" else style
            )

    platforms.extend(detected_platforms)
    return CampaignPreset(
        name=name,
        branding=brand,
        caption_style=caption,
        aspect_ratios=ratios,
        hashtags=tags,
        cta=cta,
        allowed_platforms=platforms,
        daily_clip_target=daily,
        require_public_visibility=public,
        pattern_interrupter_interval=interval,
        hook_settings=hook,
        cliffhanger_settings=cliffhanger,
        brief_rules=brief_rules,
    )


def _copy(value: CampaignPreset | Mapping[str, Any]) -> CampaignPreset:
    if isinstance(value, CampaignPreset):
        return CampaignPreset.from_dict(value.to_dict())
    return CampaignPreset.from_dict(value)


def merge_presets(
    base: CampaignPreset | Mapping[str, Any], override: CampaignPreset | Mapping[str, Any]
) -> CampaignPreset:
    """Merge an override without mutating either input; populated values win deterministically."""

    first, second = _copy(base), _copy(override)
    return CampaignPreset(
        name=second.name or first.name,
        branding=Branding(
            logo=second.branding.logo or first.branding.logo,
            font=second.branding.font or first.branding.font,
            colors={**first.branding.colors, **second.branding.colors},
        ),
        caption_style=second.caption_style or first.caption_style,
        aspect_ratios=list(second.aspect_ratios or first.aspect_ratios),
        hashtags=list(second.hashtags or first.hashtags),
        cta=second.cta or first.cta,
        allowed_platforms=list(second.allowed_platforms or first.allowed_platforms),
        daily_clip_target=(
            second.daily_clip_target if second.daily_clip_target != 2 else first.daily_clip_target
        ),
        require_public_visibility=second.require_public_visibility,
        pattern_interrupter_interval=second.pattern_interrupter_interval
        or first.pattern_interrupter_interval,
        hook_settings=second.hook_settings
        if second.hook_settings != HookSettings()
        else first.hook_settings,
        cliffhanger_settings=(
            second.cliffhanger_settings
            if second.cliffhanger_settings != CliffhangerSettings()
            else first.cliffhanger_settings
        ),
        brief_rules=deepcopy(second.brief_rules or first.brief_rules),
    )


def swap_presets(
    current: CampaignPreset | Mapping[str, Any], replacement: CampaignPreset | Mapping[str, Any]
) -> CampaignPreset:
    """Return a full copy of ``replacement``; old campaign values cannot leak across a swap."""

    del current
    return _copy(replacement)


swap_preset = swap_presets


class ComplianceStatus(str, Enum):
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ComplianceResult:
    rule: str
    status: str
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in {item.value for item in ComplianceStatus}:
            raise ValueError(f"unknown compliance status: {self.status}")

    @property
    def passed(self) -> bool | None:
        return {"pass": True, "fail": False}.get(self.status)

    @property
    def rule_name(self) -> str:
        return self.rule

    @property
    def reason(self) -> str:
        return self.message

    @property
    def failed(self) -> bool:
        return self.status == "fail"

    @property
    def unknown(self) -> bool:
        return self.status == "unknown"


@dataclass(frozen=True)
class ComplianceReport:
    results: tuple[ComplianceResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe report for clip sidecars and API responses."""

        return {
            "status": self.status,
            "overall": self.overall,
            "results": [
                {"rule": item.rule, "status": item.status, "message": item.message}
                for item in self.results
            ],
        }

    @property
    def status(self) -> str:
        if any(item.failed for item in self.results):
            return "fail"
        if any(item.unknown for item in self.results):
            return "unknown"
        return "pass"

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def overall(self) -> str:
        return self.status

    @property
    def by_rule(self) -> dict[str, ComplianceResult]:
        return {item.rule: item for item in self.results}

    @property
    def compliant(self) -> bool:
        return self.passed

    @property
    def ok(self) -> bool:
        return self.passed

    @property
    def failures(self) -> list[ComplianceResult]:
        return [item for item in self.results if item.failed]

    @property
    def unknowns(self) -> list[ComplianceResult]:
        return [item for item in self.results if item.unknown]

    def for_rule(self, rule: str) -> ComplianceResult | None:
        return next((item for item in self.results if item.rule == rule), None)


def _result(rule: str, status: ComplianceStatus, message: str) -> ComplianceResult:
    return ComplianceResult(rule, status.value, message)


def _visibility(value: Any) -> str | None:
    if isinstance(value, bool):
        return "public" if value else "private"
    text = str(value or "").strip().casefold().replace("-", "_")
    if isinstance(value, (int, float)) and value in {0, 1}:
        return "public" if value else "private"
    if text in {"public", "everyone", "anyone", "open"}:
        return "public"
    if text in {"private", "unlisted", "followers", "draft", "friends", "internal"}:
        return text
    return None


def validate_public_visibility(value: Any, *, required: bool = True) -> ComplianceResult:
    """Return a pass/fail/unknown result for public visibility."""

    if not required:
        return _result("visibility", ComplianceStatus.PASS, "public visibility is not required")
    if value is _MISSING or value is None or str(value).strip() == "":
        return _result("visibility", ComplianceStatus.UNKNOWN, "visibility was not provided")
    normalised = _visibility(value)
    if normalised == "public":
        return _result("visibility", ComplianceStatus.PASS, "visibility is public")
    if normalised is None:
        return _result("visibility", ComplianceStatus.UNKNOWN, "visibility value is not recognised")
    return _result(
        "visibility", ComplianceStatus.FAIL, f"visibility is {normalised}, public is required"
    )


def _actual_tags(value: Any) -> set[str]:
    return {item.casefold() for item in _hashtags(value)}


def _actual_colors(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(key).casefold(): _color(item) for key, item in value.items()}
    return _colors(str(value or ""))


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    text = str(value or "").strip().casefold()
    if text in {"1", "true", "yes", "y", "on", "enabled", "required", "public"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "optional", "private"}:
        return False
    return None


def _url_host(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text if "://" in text else f"//{text}")
        return (parsed.hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return ""


def _same_host(actual: str, approved: str) -> bool:
    actual = actual.casefold().removeprefix("www.")
    approved = approved.casefold().removeprefix("www.")
    if actual == approved or actual.endswith("." + approved) or approved.endswith("." + actual):
        return True
    youtube_hosts = {"youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"}
    return actual in youtube_hosts and approved in youtube_hosts


def _brief_review_results(
    policy: CampaignPreset, facts: Any, metadata: Any
) -> list[ComplianceResult]:
    """Check mechanically provable brief rules and flag the rest for a person to review."""
    rules = policy.brief_rules
    results: list[ComplianceResult] = []

    duration_rule = rules.get("clip_duration_seconds")
    if isinstance(duration_rule, Mapping):
        duration = _first(facts, metadata, "duration_seconds", "clip_duration_seconds", "duration")
        minimum = duration_rule.get("min")
        maximum = duration_rule.get("max")
        range_invalid = False
        try:
            minimum_value = float(minimum) if minimum is not None else None
            maximum_value = float(maximum) if maximum is not None else None
        except (TypeError, ValueError):
            minimum_value = maximum_value = None
            range_invalid = True
            results.append(
                _result(
                    "clip_duration",
                    ComplianceStatus.UNKNOWN,
                    "campaign duration range is not numeric",
                )
            )
        if duration is _MISSING or duration is None or str(duration).strip() == "":
            results.append(
                _result("clip_duration", ComplianceStatus.UNKNOWN, "clip duration was not provided")
            )
        elif not range_invalid:
            actual = _number(duration, -1.0)
            if actual < 0:
                results.append(
                    _result(
                        "clip_duration", ComplianceStatus.UNKNOWN, "clip duration is not numeric"
                    )
                )
            elif (minimum_value is not None and actual < minimum_value) or (
                maximum_value is not None and actual > maximum_value
            ):
                lower_text = f"{minimum_value:g}" if minimum_value is not None else "any"
                upper_text = f"{maximum_value:g}" if maximum_value is not None else "any"
                results.append(
                    _result(
                        "clip_duration",
                        ComplianceStatus.FAIL,
                        f"duration {actual:g}s is outside {lower_text}–{upper_text}s",
                    )
                )
            else:
                results.append(
                    _result(
                        "clip_duration",
                        ComplianceStatus.PASS,
                        f"duration {actual:g}s is within the campaign range",
                    )
                )

    if rules.get("subtitles_required"):
        captions = _first(
            facts, metadata, "subtitles_enabled", "captions_enabled", "captions", "subtitles"
        )
        enabled = _optional_bool(captions) if captions is not _MISSING else None
        results.append(
            _result("subtitles", ComplianceStatus.UNKNOWN, "subtitle setting was not provided")
            if enabled is None
            else _result(
                "subtitles",
                ComplianceStatus.PASS if enabled else ComplianceStatus.FAIL,
                "subtitle setting is enabled; wording accuracy still needs review"
                if enabled
                else "subtitles are required but disabled",
            )
        )

    required_mentions = policy.required_mentions_for(
        str(_first(facts, metadata, "platform", "target_platform") or "")
    )
    if required_mentions:
        mention_values: list[str] = []
        has_evidence = False
        for source in (facts, metadata):
            for key in ("mentions", "caption_mentions", "caption", "description"):
                value = _get(source, key)
                if value is _MISSING or value is None:
                    continue
                if isinstance(value, (list, tuple, set)):
                    if value:
                        has_evidence = True
                        mention_values.extend(str(item) for item in value)
                elif str(value).strip():
                    has_evidence = True
                    mention_values.append(str(value))
        actual_mentions = {
            handle.casefold()
            for text in mention_values
            for handle in _BRIEF_MENTION_RE.findall(text)
        }
        if not has_evidence:
            results.append(
                _result(
                    "required_mentions",
                    ComplianceStatus.UNKNOWN,
                    "caption mentions were not provided",
                )
            )
        else:
            missing_mentions = [
                handle for handle in required_mentions if handle.casefold() not in actual_mentions
            ]
            results.append(
                _result(
                    "required_mentions",
                    ComplianceStatus.FAIL if missing_mentions else ComplianceStatus.PASS,
                    f"missing required mentions: {', '.join(missing_mentions)}"
                    if missing_mentions
                    else "required campaign mentions are present",
                )
            )

    source_rule = rules.get("source_policy")
    if isinstance(source_rule, Mapping) and (
        source_rule.get("allow_only_approved_sources") or source_rule.get("approved_sources")
    ):
        approved_sources = source_rule.get("approved_sources", [])
        approved_hosts = {host for host in (_url_host(item) for item in approved_sources) if host}
        source_host_value = _first(facts, metadata, "source_host")
        source_value = _first(facts, metadata, "source_url", "source_uri", "source")
        actual_host = _url_host(source_host_value) if source_host_value is not _MISSING else ""
        if not actual_host and source_value is not _MISSING:
            actual_host = _url_host(source_value)
        if not actual_host:
            results.append(
                _result(
                    "source_host",
                    ComplianceStatus.UNKNOWN,
                    "source host is not available; verify the official source",
                )
            )
        elif not approved_hosts:
            results.append(
                _result(
                    "source_host",
                    ComplianceStatus.UNKNOWN,
                    "approved source hosts could not be extracted",
                )
            )
        elif any(_same_host(actual_host, host) for host in approved_hosts):
            results.append(
                _result(
                    "source_host", ComplianceStatus.PASS, f"source host {actual_host} is approved"
                )
            )
        else:
            results.append(
                _result(
                    "source_host",
                    ComplianceStatus.FAIL,
                    f"source host {actual_host} is not approved",
                )
            )

        channel_handles = [str(item) for item in source_rule.get("approved_channel_handles", [])]
        if channel_handles:
            channel_value = _first(
                facts, metadata, "source_channel_handle", "source_channel", "channel", "uploader"
            )
            channel_text = "" if channel_value is _MISSING else str(channel_value).strip()
            source_text = "" if source_value is _MISSING else str(source_value).strip()
            evidence = channel_text or source_text
            if not evidence:
                results.append(
                    _result(
                        "source_channel",
                        ComplianceStatus.UNKNOWN,
                        "official channel provenance needs human review",
                    )
                )
            elif any(
                handle.casefold() in evidence.casefold()
                or re.sub(r"[^a-z0-9]", "", handle.casefold().lstrip("@"))
                == re.sub(r"[^a-z0-9]", "", channel_text.casefold())
                for handle in channel_handles
            ):
                results.append(
                    _result(
                        "source_channel",
                        ComplianceStatus.PASS,
                        "source channel matches the approved channel",
                    )
                )
            else:
                results.append(
                    _result(
                        "source_channel",
                        ComplianceStatus.UNKNOWN,
                        "source channel cannot be confirmed from the available metadata",
                    )
                )

    if "face_visible_within_seconds" in rules:
        limit = float(rules["face_visible_within_seconds"])
        subject = str(rules.get("face_subject") or "the featured person's").strip()
        subject_possessive = subject if subject.endswith(("'s", "’s")) else f"{subject}'s"
        first_face = _first(
            facts,
            metadata,
            "face_first_appearance_seconds",
            "face_visible_at_seconds",
            "face_first_seen_seconds",
        )
        if first_face is _MISSING or first_face is None or str(first_face).strip() == "":
            results.append(
                _result(
                    "face_visibility",
                    ComplianceStatus.UNKNOWN,
                    f"human review needed: verify {subject_possessive} face appears within {limit:g}s",
                )
            )
        else:
            actual = _number(first_face, -1)
            results.append(
                _result(
                    "face_visibility",
                    ComplianceStatus.PASS if 0 <= actual <= limit else ComplianceStatus.FAIL,
                    f"first face appears at {actual:g}s (limit {limit:g}s)"
                    if actual >= 0
                    else "face timing is invalid",
                )
            )

    if isinstance(rules.get("audience_requirement"), Mapping):
        requirement = rules["audience_requirement"]
        results.append(
            _result(
                "audience_requirement",
                ComplianceStatus.UNKNOWN,
                f"human review needed: verify at least {requirement.get('minimum_percent', 'the required')}% of views are from {', '.join(requirement.get('regions', []))}",
            )
        )
    if "minimum_post_days" in rules:
        results.append(
            _result(
                "post_lifetime",
                ComplianceStatus.UNKNOWN,
                f"human review needed: keep the post live for at least {rules['minimum_post_days']} days",
            )
        )
    if "minimum_engagement_percent" in rules:
        results.append(
            _result(
                "minimum_engagement",
                ComplianceStatus.UNKNOWN,
                f"human review needed: verify engagement reaches {rules['minimum_engagement_percent']:g}%",
            )
        )
    if rules.get("likes_must_be_visible"):
        results.append(
            _result(
                "likes_visibility",
                ComplianceStatus.UNKNOWN,
                "human review needed: confirm likes are visible",
            )
        )
    if rules.get("prohibited_content"):
        results.append(
            _result(
                "content_safety",
                ComplianceStatus.UNKNOWN,
                "human review needed: review the clip against the brief's exclusions",
            )
        )
    return results


def _branding_result(facts: Any, metadata: Any, preset: CampaignPreset) -> ComplianceResult:
    expected = preset.branding
    if not (expected.logo or expected.font or expected.colors):
        return _result("branding", ComplianceStatus.PASS, "no branding requirement configured")
    nested_facts = _get(facts, "branding", "brand")
    nested_meta = _get(metadata, "branding", "brand")
    nested_facts = None if nested_facts is _MISSING else nested_facts
    nested_meta = None if nested_meta is _MISSING else nested_meta
    actual_logo = _first(nested_facts, nested_meta, "logo", "logo_asset", "logo_url")
    actual_font = _first(nested_facts, nested_meta, "font", "font_family", "font_asset")
    actual_colors = _first(
        nested_facts,
        nested_meta,
        "colors",
        "colours",
        "color_palette",
        "primary_color",
        "brand_colors",
    )
    if actual_logo is _MISSING:
        actual_logo = _first(facts, metadata, "logo", "logo_asset", "logo_url")
    if actual_font is _MISSING:
        actual_font = _first(facts, metadata, "font", "font_family", "font_asset")
    if actual_colors is _MISSING:
        actual_colors = _first(
            facts, metadata, "colors", "colours", "color_palette", "primary_color", "brand_colors"
        )
    missing: list[str] = []
    mismatched: list[str] = []
    if expected.logo:
        if actual_logo is _MISSING or not str(actual_logo or "").strip():
            missing.append("logo")
        elif str(actual_logo).strip().casefold() != expected.logo.casefold():
            mismatched.append("logo")
    if expected.font:
        if actual_font is _MISSING or not str(actual_font or "").strip():
            missing.append("font")
        elif str(actual_font).strip().casefold() != expected.font.casefold():
            mismatched.append("font")
    if expected.colors:
        actual = {} if actual_colors is _MISSING else _actual_colors(actual_colors)
        for key, expected_value in expected.colors.items():
            if key not in actual:
                missing.append(f"color:{key}")
            elif actual[key].casefold() != expected_value.casefold():
                mismatched.append(f"color:{key}")
    if mismatched:
        return _result(
            "branding", ComplianceStatus.FAIL, f"branding mismatch: {', '.join(mismatched)}"
        )
    if missing:
        return _result(
            "branding", ComplianceStatus.UNKNOWN, f"branding missing: {', '.join(missing)}"
        )
    return _result("branding", ComplianceStatus.PASS, "branding matches preset")


def validate_clip_against_preset(
    preset: CampaignPreset | Mapping[str, Any], clip_facts: Any = None, metadata: Any = None
) -> ComplianceReport:
    """Validate facts and metadata; absent evidence is unknown, not an implicit pass."""

    policy = _copy(preset)
    facts, meta = clip_facts, metadata
    results: list[ComplianceResult] = []
    results.append(
        validate_public_visibility(
            _first(facts, meta, "visibility", "visibility_status", "is_public", "public"),
            required=policy.require_public_visibility,
        )
    )

    platform = _first(facts, meta, "platform", "target_platform")
    if not policy.allowed_platforms:
        results.append(
            _result("platform", ComplianceStatus.PASS, "no platform restriction configured")
        )
    elif platform is _MISSING or platform is None or str(platform).strip() == "":
        results.append(_result("platform", ComplianceStatus.UNKNOWN, "platform was not provided"))
    else:
        values = platform if isinstance(platform, (list, tuple, set)) else [platform]
        bad = [
            _platform(value) for value in values if _platform(value) not in policy.allowed_platforms
        ]
        results.append(
            _result("platform", ComplianceStatus.FAIL, f"platform not allowed: {', '.join(bad)}")
            if bad
            else _result("platform", ComplianceStatus.PASS, "platform is allowed")
        )

    aspect = _first(facts, meta, "aspect_ratio", "aspect", "ratio")
    if not policy.aspect_ratios:
        results.append(
            _result("aspect_ratio", ComplianceStatus.PASS, "no aspect-ratio restriction configured")
        )
    elif aspect is _MISSING or aspect is None or not str(aspect).strip():
        results.append(
            _result("aspect_ratio", ComplianceStatus.UNKNOWN, "aspect ratio was not provided")
        )
    elif _ratio(aspect) in policy.aspect_ratios:
        results.append(_result("aspect_ratio", ComplianceStatus.PASS, "aspect ratio is allowed"))
    else:
        results.append(
            _result("aspect_ratio", ComplianceStatus.FAIL, f"aspect ratio is not allowed: {aspect}")
        )

    actual_tags = _first(facts, meta, "hashtags", "tags")
    required_tags = {tag.casefold() for tag in policy.hashtags}
    if not required_tags:
        results.append(
            _result("hashtags", ComplianceStatus.PASS, "no hashtag requirement configured")
        )
    elif actual_tags is _MISSING or actual_tags is None:
        results.append(_result("hashtags", ComplianceStatus.UNKNOWN, "hashtags were not provided"))
    else:
        missing = sorted(required_tags - _actual_tags(actual_tags))
        results.append(
            _result("hashtags", ComplianceStatus.FAIL, f"missing hashtags: {', '.join(missing)}")
            if missing
            else _result("hashtags", ComplianceStatus.PASS, "required hashtags are present")
        )

    actual_cta = _first(facts, meta, "cta", "call_to_action", "caption", "description")
    if not policy.cta:
        results.append(_result("cta", ComplianceStatus.PASS, "no CTA requirement configured"))
    elif actual_cta is _MISSING or not str(actual_cta or "").strip():
        results.append(_result("cta", ComplianceStatus.UNKNOWN, "CTA was not provided"))
    elif policy.cta.casefold() in str(actual_cta).casefold():
        results.append(_result("cta", ComplianceStatus.PASS, "CTA matches preset"))
    else:
        results.append(_result("cta", ComplianceStatus.FAIL, "CTA does not match preset"))

    actual_caption = _first(facts, meta, "caption_style", "caption_preset", "style")
    if not policy.caption_style:
        results.append(
            _result(
                "caption_style", ComplianceStatus.PASS, "no caption-style requirement configured"
            )
        )
    elif actual_caption is _MISSING or not str(actual_caption or "").strip():
        results.append(
            _result("caption_style", ComplianceStatus.UNKNOWN, "caption style was not provided")
        )
    elif str(actual_caption).strip().casefold() == policy.caption_style.casefold():
        results.append(
            _result("caption_style", ComplianceStatus.PASS, "caption style matches preset")
        )
    else:
        results.append(
            _result("caption_style", ComplianceStatus.FAIL, "caption style does not match preset")
        )

    results.append(_branding_result(facts, meta, policy))
    for name, setting, names in (
        ("hook", policy.hook_settings, ("hook", "hook_text", "has_hook")),
        (
            "cliffhanger",
            policy.cliffhanger_settings,
            ("cliffhanger", "cliffhanger_text", "has_cliffhanger"),
        ),
    ):
        if not setting.enabled:
            results.append(_result(name, ComplianceStatus.PASS, f"{name} is not required"))
            continue
        value = _first(facts, meta, *names)
        if value is _MISSING or value is None or str(value).strip() == "":
            results.append(
                _result(name, ComplianceStatus.UNKNOWN, f"{name} evidence was not provided")
            )
        elif isinstance(value, bool) and not value:
            results.append(_result(name, ComplianceStatus.FAIL, f"{name} is missing"))
        else:
            results.append(_result(name, ComplianceStatus.PASS, f"{name} is present"))
    results.extend(_brief_review_results(policy, facts, meta))
    return ComplianceReport(tuple(results))


def validate_clip(
    *args: Any,
    preset: CampaignPreset | Mapping[str, Any] | None = None,
    clip_facts: Any = None,
    metadata: Any = None,
) -> ComplianceReport:
    """Compatibility wrapper for ``(preset, facts, metadata)`` or ``(facts, metadata, preset)``."""

    if args:
        if len(args) > 3:
            raise TypeError("validate_clip accepts at most three positional arguments")
        positions = [index for index, item in enumerate(args) if isinstance(item, CampaignPreset)]
        if preset is None and positions:
            position = positions[0]
            preset = args[position]
            values = [item for index, item in enumerate(args) if index != position]
            if values:
                clip_facts = values[0]
            if len(values) > 1:
                metadata = values[1]
        elif preset is None and len(args) == 3:
            clip_facts, metadata, preset = args
        elif preset is None and len(args) == 2:
            clip_facts, preset = args
        elif preset is not None:
            clip_facts = args[0]
            if len(args) > 1:
                metadata = args[1]
    if preset is None:
        raise TypeError("validate_clip requires a CampaignPreset")
    return validate_clip_against_preset(preset, clip_facts, metadata)


def _zone(value: str | tzinfo) -> tzinfo:
    if isinstance(value, tzinfo):
        return value
    name = str(value or "").strip()
    if not name:
        raise ValueError("timezone must be an IANA name or tzinfo")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {name}") from exc


def _time(value: Any) -> time:
    if isinstance(value, datetime):
        return value.timetz().replace(tzinfo=None)
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return time(hour=int(value[0]), minute=int(value[1]))
    if isinstance(value, (int, float)):
        hour = int(value)
        minute = int(round((float(value) - hour) * 60))
        if minute == 60:
            hour, minute = hour + 1, 0
        return time(hour=hour, minute=minute)
    match = re.fullmatch(
        r"(\d{1,2})(?::(\d{1,2}))?\s*(am|pm)?",
        str(value or "").strip().casefold(),
    )
    if not match:
        raise ValueError(f"invalid peak hour: {value!r}")
    hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"invalid 12-hour peak hour: {value!r}")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    return time(hour=hour, minute=minute)


def plan_daily_slots(
    day: date_type | datetime | str,
    timezone: str | tzinfo,
    peak_hours: Sequence[Any] | Mapping[Any, Any],
    *,
    visibility: Any = None,
    count: int = 2,
) -> tuple[int, ...]:
    """Return distinct UTC epoch slots for a local calendar day.

    The first ``count`` distinct peak values are used in caller order. ``visibility`` is optional
    for callers that already checked a preset, but if supplied it must be public.
    """

    try:
        requested_count = max(1, min(10, int(count)))
    except (TypeError, ValueError):
        raise ValueError("slot count must be an integer between 1 and 10") from None
    if visibility is not None:
        result = validate_public_visibility(visibility)
        if result.status != "pass":
            raise ValueError(result.message)
    if isinstance(day, datetime):
        calendar_day = day.date()
    elif isinstance(day, date_type):
        calendar_day = day
    else:
        try:
            calendar_day = date_type.fromisoformat(str(day).strip())
        except ValueError as exc:
            raise ValueError(f"invalid calendar date: {day!r}") from exc
    if isinstance(peak_hours, Mapping):
        values = list(peak_hours.values())
    elif isinstance(peak_hours, str):
        values = _split(peak_hours)
    elif isinstance(peak_hours, set):
        values = sorted(peak_hours, key=str)
    else:
        values = list(peak_hours)
    parsed: list[time] = []
    for value in values:
        candidate = _time(value)
        if candidate not in parsed:
            parsed.append(candidate)
    if len(parsed) < requested_count:
        if requested_count == 2:
            raise ValueError("two distinct peak hours are required")
        raise ValueError(f"at least {requested_count} distinct peak hours are required")
    zone = _zone(timezone)
    slots = [
        int(datetime.combine(calendar_day, item, tzinfo=zone).timestamp())
        for item in parsed[:requested_count]
    ]
    if len(set(slots)) != len(slots):
        raise ValueError("peak hours resolved to duplicate UTC slots")
    return tuple(slots)


parse_preset = parse_campaign_preset
parse_brief = parse_campaign_preset
parse_google_doc_brief = parse_campaign_preset
check_clip_compliance = validate_clip_against_preset
check_compliance = validate_clip

__all__ = [
    "Branding",
    "CampaignPreset",
    "CliffhangerSettings",
    "ComplianceReport",
    "ComplianceResult",
    "ComplianceStatus",
    "HookSettings",
    "check_clip_compliance",
    "check_compliance",
    "merge_presets",
    "parse_brief",
    "parse_campaign_preset",
    "parse_google_doc_brief",
    "parse_preset",
    "plan_daily_slots",
    "swap_preset",
    "swap_presets",
    "validate_clip",
    "validate_clip_against_preset",
    "validate_public_visibility",
]
