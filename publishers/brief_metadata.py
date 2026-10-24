"""Map campaign-brief copy into platform-neutral publishing requests."""

from __future__ import annotations

from typing import Any


def _tags(values: Any) -> list[str]:
    if isinstance(values, str):
        values = values.replace(",", " ").split()
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        text = text if text.startswith("#") else f"#{text}"
        if text not in result:
            result.append(text)
    return result


def metadata_for_platform(
    brief: Any,
    platform: str,
    *,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy of ``base`` enriched with brief defaults and platform overrides."""
    data = dict(base or {})
    brief_tags = getattr(brief, "hashtags", []) or []
    request_tags = data.get("hashtags", []) or []
    data["description"] = str(data.get("description") or getattr(brief, "description", "") or "")
    data["cta"] = str(
        data.get("cta") or getattr(brief, "call_to_action", "") or getattr(brief, "cta", "") or ""
    )
    data["hashtags"] = _tags([*brief_tags, *request_tags])
    overrides = getattr(brief, "platform_metadata", {}) or {}
    lookup = platform
    if platform == "instagram":
        lookup = "instagram_reels"
    elif platform == "youtube":
        lookup = "youtube_shorts"
    override = (
        (overrides.get(platform) or overrides.get(lookup) or {})
        if isinstance(overrides, dict)
        else {}
    )
    if hasattr(override, "to_dict"):
        override = override.to_dict()
    if isinstance(override, dict):
        nested = override.get("metadata")
        if isinstance(nested, dict):
            override = {**nested, **override}
        if not data["description"]:
            data["description"] = str(override.get("description") or override.get("caption") or "")
        if not data["cta"]:
            data["cta"] = str(override.get("cta") or "")
        if not data.get("hashtags"):
            data["hashtags"] = _tags(override.get("hashtags", []))
        for key in ("title", "description", "cta", "mentions", "hashtags"):
            if key in override and override[key] not in (None, "", []):
                data[key] = _tags(override[key]) if key == "hashtags" else override[key]
    return data


def apply_brief_to_request(request: Any, brief: Any, platform: str) -> Any:
    """Mutate a PublishRequest-like object with brief metadata and return it."""
    values = metadata_for_platform(
        brief,
        platform,
        base={
            "description": getattr(request, "description", ""),
            "cta": getattr(request, "cta", ""),
            "hashtags": getattr(request, "hashtags", []),
        },
    )
    for key in ("description", "cta", "hashtags", "title", "mentions"):
        if key in values and hasattr(request, key):
            setattr(request, key, values[key])
    return request
