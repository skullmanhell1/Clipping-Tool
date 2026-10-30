"""Source-library helpers for versioned campaign presets.

The campaign brief is untrusted input.  These helpers extract explicit links and keep the
approval decision separate from discovery.  A channel or video that was merely mentioned in a
brief is therefore ``suggested`` until the operator accepts it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

from worker.google_drive import extract_file_id, extract_folder_id

_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SHORT_YOUTUBE_HOSTS = {"youtu.be", "www.youtu.be"}
_VOD_HOST_HINTS = ("twitch.tv", "kick.com", "rumble.com", "vimeo.com")
_CHANNEL_NAME_RE = re.compile(
    r"^\s*(?:youtube\s+channel|channel|creator)\s*[:=\-]\s*(?P<name>[^\n|]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def clean_url(value: Any) -> str:
    return str(value or "").strip().rstrip(".,;:!?)]}")


def youtube_video_id(url: str) -> str | None:
    try:
        parts = urlsplit(clean_url(url))
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host in _SHORT_YOUTUBE_HOSTS:
        return parts.path.strip("/").split("/", 1)[0] or None
    if host not in _YOUTUBE_HOSTS:
        return None
    query_id = parse_qs(parts.query).get("v", [None])[0]
    if query_id:
        return query_id
    path_parts = [part for part in parts.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
        return path_parts[1]
    return None


def source_type(url: str) -> str:
    value = clean_url(url)
    try:
        parts = urlsplit(value)
    except ValueError:
        return "url"
    host = (parts.hostname or "").lower()
    path = parts.path.casefold()
    if youtube_video_id(value):
        return "youtube_video"
    if host in _YOUTUBE_HOSTS and (
        path.startswith(("/@", "/channel/", "/c/", "/user/")) or path == "/"
    ):
        return "youtube_channel"
    if host in {"drive.google.com", "docs.google.com"}:
        if extract_folder_id(value) and not extract_file_id(value):
            return "google_drive_folder"
        return "google_drive_file"
    if any(hint in host for hint in _VOD_HOST_HINTS):
        return "vod"
    return "url"


def source_id(url: str) -> str:
    return hashlib.sha256(clean_url(url).encode("utf-8")).hexdigest()[:16]


def _urls(values: Iterable[Any]) -> list[str]:
    if isinstance(values, (str, bytes)):
        values = [values]
    result: list[str] = []
    for value in values:
        text = clean_url(value)
        candidates = _URL_RE.findall(text) or ([text] if text.startswith("http") else [])
        for candidate in candidates:
            if candidate not in result:
                result.append(candidate)
    return result


def _source_matches(left: Any, right: Any) -> bool:
    """Match equivalent direct-source URL forms without weakening generic URL checks."""

    first = clean_url(left)
    second = clean_url(right)
    if not first or not second:
        return False
    if first == second:
        return True
    first_video = youtube_video_id(first)
    second_video = youtube_video_id(second)
    if first_video and second_video:
        return first_video == second_video
    first_file = extract_file_id(first)
    second_file = extract_file_id(second)
    if first_file and second_file:
        return first_file == second_file
    first_folder = extract_folder_id(first)
    second_folder = extract_folder_id(second)
    return bool(first_folder and second_folder and first_folder == second_folder)


def make_source_entry(
    url: str,
    *,
    status: str = "suggested",
    title: str = "",
    origin: str = "brief",
    reason: str = "",
    approved: bool | None = None,
) -> dict[str, Any]:
    normalized = clean_url(url)
    state = status if status in {"approved", "suggested", "banned"} else "suggested"
    if approved is not None:
        state = "approved" if approved else state
    return {
        "id": source_id(normalized),
        "url": normalized,
        "title": str(title or "").strip(),
        "type": source_type(normalized),
        "status": state,
        "approved": state == "approved",
        "origin": str(origin or "brief"),
        "reason": str(reason or "").strip(),
    }


def extract_source_manifest(
    text: str,
    *,
    allowed: Iterable[Any] = (),
    prohibited: Iterable[Any] = (),
    origin: str = "brief",
) -> list[dict[str, Any]]:
    """Extract links and assign explicit allow/deny labels without guessing."""

    allowed_list = _urls(allowed)
    prohibited_list = _urls(prohibited)
    candidates = _urls([text, *allowed_list, *prohibited_list])
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        if any(_source_matches(url, blocked) for blocked in prohibited_list):
            entries.append(
                make_source_entry(
                    url,
                    status="banned",
                    origin=origin,
                    reason="Explicitly prohibited by the campaign brief.",
                )
            )
        elif any(_source_matches(url, approved) for approved in allowed_list):
            entries.append(
                make_source_entry(
                    url,
                    status="approved",
                    origin=origin,
                    reason="Explicitly approved by the campaign brief.",
                )
            )
        else:
            entries.append(
                make_source_entry(
                    url,
                    status="suggested",
                    origin=origin,
                    reason="Mentioned in the brief; accept it before clipping.",
                )
            )
    for match in _CHANNEL_NAME_RE.finditer(str(text or "")):
        name = str(match.group("name") or "").strip().strip("*-•")
        if not name or name.casefold().startswith("http"):
            continue
        entry = make_source_entry(
            "",
            status="suggested",
            title=name,
            origin=origin,
            reason="Channel or creator name was mentioned without an individual approved video link.",
        )
        entry["id"] = source_id(f"youtube-channel:{name.casefold()}")
        entry["type"] = "youtube_channel"
        if not any(item.get("id") == entry["id"] for item in entries):
            entries.append(entry)
    return entries


def source_status(preset: Mapping[str, Any] | None, url: str) -> str:
    """Return the active preset's decision for a source URL."""

    value = clean_url(url)
    data = preset or {}
    has_library = "source_library" in data
    library = list(data.get("source_library") or [])
    if not has_library:
        return "unrestricted"
    matched: list[str] = []
    for item in library:
        if not isinstance(item, Mapping):
            continue
        candidate = clean_url(item.get("url"))
        if candidate and _source_matches(candidate, value):
            status = str(item.get("status") or "suggested").casefold()
            matched.append(status if status in {"approved", "suggested", "banned"} else "suggested")
    if "banned" in matched:
        return "banned"
    if "approved" in matched:
        return "approved"
    if matched:
        return "suggested"
    # Once a preset has a source library, an unlisted source is never implicitly approved. This
    # also keeps a brief containing only suggested links from silently allowing local uploads.
    return "unlisted"


def source_blocker(preset: Mapping[str, Any] | None, url: str) -> str:
    status = source_status(preset, url)
    if status == "approved" or status == "unrestricted":
        return ""
    if status == "banned":
        return "This source is banned by the active campaign preset."
    if status == "suggested":
        return "This source is suggested but not approved by the active campaign preset."
    return "This source is not in the active campaign preset's approved source library."


__all__ = [
    "clean_url",
    "extract_source_manifest",
    "make_source_entry",
    "source_blocker",
    "source_id",
    "source_status",
    "source_type",
    "youtube_video_id",
]
