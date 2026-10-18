"""Pure campaign presets, compliance policy, and daily publishing slots.

The module is intentionally independent from the API, job queue, and publisher manager. A
Google-Doc-style brief can be parsed and checked before rendering or publishing has side effects.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, time, tzinfo
from enum import Enum
from typing import Any
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


def _unique(values: Sequence[str]) -> list[str]:
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
_DURATION_RE = re.compile(r"[-+]?\d+(?:\.\d+)?\s*(?:seconds?|secs?|s)\b", re.IGNORECASE)


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


def _strip_durations(text: str) -> str:
    """Remove ``5s`` / ``1.5 seconds`` tokens, as ``re.sub`` of :data:`_DURATION_RE` did.

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
            match = _DURATION_RE.match(text, index)
            if match:
                pieces.append(text[kept_from:index])
                kept_from = index = match.end()
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


def parse_campaign_preset(text: str) -> CampaignPreset:
    """Parse a human-authored Google-Doc-style brief deterministically."""

    name = ""
    brand = Branding()
    caption = ""
    ratios: list[str] = []
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
) -> tuple[int, int]:
    """Return exactly two distinct UTC epoch slots for a local calendar day.

    The first two distinct peak values are used in caller order. ``visibility`` is optional for
    callers that already checked a preset, but if supplied it must be public.
    """

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
    if len(parsed) < 2:
        raise ValueError("at least two distinct peak hours are required")
    zone = _zone(timezone)
    slots = [
        int(datetime.combine(calendar_day, item, tzinfo=zone).timestamp()) for item in parsed[:2]
    ]
    if slots[0] == slots[1]:
        raise ValueError("peak hours resolved to duplicate UTC slots")
    return slots[0], slots[1]


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
