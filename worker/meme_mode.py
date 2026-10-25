"""Meme-mode timeline events, presets, and best-effort auto suggestions.

The public representation is JSON-friendly so it can live in ``ProcessingOptions`` and in clip
metadata.  Every event has a stable id; deleting one event and undoing that deletion never changes
the ids of its neighbours.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    if value is None:
        return default
    return bool(value)


def _stable_id(*parts: Any) -> str:
    # Stable identifiers only; this digest is not used for security or content integrity.
    digest = hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()  # noqa: S324
    return f"meme_{digest[:12]}"


@dataclass
class MemeEvent:
    """One clip-relative meme edit."""

    id: str
    kind: str
    effect: str
    start: float
    end: float
    volume: float = 0.35
    duck: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "manual"
    asset_id: str = ""
    enabled: bool = True

    @property
    def at(self) -> float:
        return self.start

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "effect": self.effect,
            "start": self.start,
            "end": self.end,
            "at": self.start,
            "volume": self.volume,
            "duck": self.duck,
            "payload": dict(self.payload),
            "source": self.source,
            "asset_id": self.asset_id,
            "enabled": self.enabled,
        }


def event_from_dict(
    value: MemeEvent | Mapping[str, Any], *, default_source: str = "manual"
) -> MemeEvent | None:
    if isinstance(value, MemeEvent):
        return value
    if not isinstance(value, Mapping):
        return None
    kind = str(value.get("kind") or value.get("type") or "").strip().lower()
    effect = (
        str(
            value.get("effect")
            or value.get("name")
            or value.get("action")
            or value.get("asset")
            or ""
        )
        .strip()
        .lower()
    )
    # The browser editor uses ``effect`` for visual events while the persisted worker model uses
    # ``visual`` as the kind. Accept both spellings so hand-authored timeline JSON and older saved
    # profiles cannot silently disappear at render time.
    if kind == "effect":
        kind = "visual"
    if not kind or not effect:
        return None
    start = _number(value.get("start", value.get("at", value.get("time", 0.0))))
    end_value = value.get("end")
    if end_value is None:
        end = start + max(0.05, _number(value.get("duration"), 0.25))
    else:
        end = _number(end_value, start + 0.25)
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    event_id = str(value.get("id") or "").strip() or _stable_id(
        kind, effect, start, end, sorted(payload.items())
    )
    return MemeEvent(
        id=event_id,
        kind=kind,
        effect=effect,
        start=start,
        end=end,
        volume=max(0.0, min(1.0, _number(value.get("volume"), 0.35))),
        duck=max(0.0, min(1.0, _number(value.get("duck", value.get("ducking")), 0.0))),
        payload=dict(payload),
        source=str(value.get("source") or default_source),
        asset_id=str(value.get("asset_id") or value.get("asset") or ""),
        enabled=_as_bool(value.get("enabled", True)),
    )


def normalize_events(
    events: Iterable[MemeEvent | Mapping[str, Any]] | None, duration: float
) -> list[MemeEvent]:
    """Clamp events to a clip while retaining exact caller-supplied times when valid."""
    try:
        raw_length = float(duration)
    except (TypeError, ValueError):
        raw_length = 0.0
    length = (
        math.inf if math.isinf(raw_length) and raw_length > 0 else max(0.0, _number(raw_length))
    )
    out: list[MemeEvent] = []
    for value in events or ():
        event = event_from_dict(value)
        if event is None or not event.enabled or length <= 0:
            continue
        start = max(0.0, min(length, event.start))
        end = max(start, min(length, event.end))
        if end <= start and event.kind not in {"sfx", "audio"}:
            continue
        event.start, event.end = start, end
        out.append(event)
    return sorted(out, key=lambda item: (item.start, item.end, item.id))


def rebase_events(
    events: Iterable[MemeEvent | Mapping[str, Any]], keeps: Sequence[Any], duration: float
) -> list[MemeEvent]:
    """Map clip-relative event times through a destructive keep-interval plan."""
    parsed = normalize_events(events, float("inf"))
    mapped: list[MemeEvent] = []
    output_cursor = 0.0
    for keep in keeps or ():
        if isinstance(keep, (tuple, list)):
            raw_start = keep[0] if keep else 0.0
            raw_end = keep[1] if len(keep) > 1 else raw_start
        else:
            raw_start = getattr(keep, "start", 0.0)
            raw_end = getattr(keep, "end", raw_start)
        keep_start = _number(raw_start)
        keep_end = _number(raw_end, keep_start)
        if keep_end <= keep_start:
            continue
        for event in parsed:
            overlap_start = max(event.start, keep_start)
            overlap_end = min(event.end, keep_end)
            if overlap_end <= overlap_start:
                continue
            clone = MemeEvent(
                id=event.id,
                kind=event.kind,
                effect=event.effect,
                start=output_cursor + overlap_start - keep_start,
                end=output_cursor + overlap_end - keep_start,
                volume=event.volume,
                duck=event.duck,
                payload=dict(event.payload),
                source=event.source,
                asset_id=event.asset_id,
                enabled=event.enabled,
            )
            mapped.append(clone)
        output_cursor += keep_end - keep_start
    # Keep one stable id per source event when an edit spans two kept intervals.
    unique: dict[str, MemeEvent] = {}
    for event in mapped:
        previous = unique.get(event.id)
        if previous is None:
            unique[event.id] = event
        else:
            previous.end = max(previous.end, event.end)
    return normalize_events(unique.values(), duration)


class MemeTimeline:
    """Mutable timeline helper with stable-id remove and undo operations."""

    def __init__(self, events: Iterable[MemeEvent | Mapping[str, Any]] | None = None) -> None:
        self.events: list[MemeEvent] = normalize_events(events, float("inf"))
        self._removed: dict[str, MemeEvent] = {}

    def add(self, event: MemeEvent | Mapping[str, Any]) -> MemeEvent | None:
        parsed = event_from_dict(event)
        if parsed is None:
            return None
        self.events = [item for item in self.events if item.id != parsed.id]
        self.events.append(parsed)
        self.events.sort(key=lambda item: (item.start, item.end, item.id))
        return parsed

    def remove(self, event_id: str) -> MemeEvent | None:
        key = str(event_id or "")
        for index, event in enumerate(self.events):
            if event.id == key:
                removed = self.events.pop(index)
                self._removed[key] = removed
                return removed
        return None

    def undo(self, event_id: str) -> MemeEvent | None:
        restored = self._removed.pop(str(event_id or ""), None)
        if restored is None:
            return None
        self.add(restored)
        return restored

    def to_dict(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]


def add_event(
    events: Iterable[MemeEvent | Mapping[str, Any]], event: MemeEvent | Mapping[str, Any]
) -> list[dict[str, Any]]:
    timeline = MemeTimeline(events)
    timeline.add(event)
    return timeline.to_dict()


def remove_event(
    events: Iterable[MemeEvent | Mapping[str, Any]], event_id: str
) -> list[dict[str, Any]]:
    timeline = MemeTimeline(events)
    timeline.remove(event_id)
    return timeline.to_dict()


def undo_event(
    events: Iterable[MemeEvent | Mapping[str, Any]], event: MemeEvent | Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Restore one previously removed event when the caller retains its stable-id record."""
    timeline = MemeTimeline(events)
    parsed = event_from_dict(event)
    if parsed is not None:
        timeline.add(parsed)
    return timeline.to_dict()


PRESET_PACKS: dict[str, dict[str, Any]] = {
    "chaotic": {
        "label": "Chaotic",
        "events": [
            {"kind": "visual", "effect": "zoom_punch", "start": 0.0, "duration": 0.35},
            {"kind": "visual", "effect": "shake", "start": 0.45, "duration": 0.45},
            {"kind": "visual", "effect": "flash", "start": 0.9, "duration": 0.12},
            {
                "kind": "sfx",
                "effect": "pop",
                "start": 0.9,
                "duration": 0.1,
                "volume": 0.35,
                "duck": 0.25,
            },
            {
                "kind": "overlay",
                "effect": "text",
                "start": 1.0,
                "duration": 0.9,
                "payload": {"text": "WHAT?!", "position": "center", "scale": 0.11},
            },
        ],
    },
    "subtle": {
        "label": "Subtle",
        "events": [
            {"kind": "visual", "effect": "zoom_punch", "start": 0.0, "duration": 0.25},
            {
                "kind": "sfx",
                "effect": "click",
                "start": 0.0,
                "duration": 0.05,
                "volume": 0.18,
                "duck": 0.12,
            },
        ],
    },
    "gaming_rage": {
        "label": "Gaming Rage",
        "events": [
            {"kind": "visual", "effect": "shake", "start": 0.0, "duration": 0.8},
            {"kind": "visual", "effect": "glitch_vhs", "start": 0.8, "duration": 0.5},
            {"kind": "visual", "effect": "chromatic_aberration", "start": 1.3, "duration": 0.4},
            {
                "kind": "sfx",
                "effect": "bass_drop",
                "start": 1.3,
                "duration": 0.45,
                "volume": 0.3,
                "duck": 0.35,
            },
            {
                "kind": "overlay",
                "effect": "text",
                "start": 1.35,
                "duration": 1.2,
                "payload": {
                    "text": "RAGE QUIT",
                    "position": "center",
                    "scale": 0.1,
                    "animate": "pop",
                },
            },
        ],
    },
}


def preset_events(name: str, duration: float) -> list[MemeEvent]:
    key = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    pack = PRESET_PACKS.get(key)
    if not pack:
        return []
    return [
        event
        for index, item in enumerate(pack["events"])
        if (
            event := event_from_dict(
                {
                    **item,
                    "id": _stable_id("preset", key, index, item.get("start")),
                    "source": "preset",
                }
            )
        )
    ][:]


def _word_value(word: Any) -> tuple[float, float, str]:
    if isinstance(word, Mapping):
        return (
            _number(word.get("start")),
            _number(word.get("end"), _number(word.get("start")) + 0.2),
            str(word.get("text") or ""),
        )
    return (
        _number(getattr(word, "start", 0.0)),
        _number(getattr(word, "end", 0.2), _number(getattr(word, "start", 0.0)) + 0.2),
        str(getattr(word, "text", "") or ""),
    )


KEYWORD_EFFECTS: dict[str, str] = {
    "wow": "zoom_punch",
    "amazing": "zoom_punch",
    "incredible": "flash",
    "insane": "shake",
    "crazy": "shake",
    "rage": "shake",
    "angry": "shake",
    "fail": "glitch_vhs",
    "failure": "glitch_vhs",
    "wrong": "chromatic_aberration",
    "no": "flash",
    "money": "zoom_punch",
    "win": "flash",
    "winner": "flash",
}


def _energy_peaks(energy: Sequence[tuple[float, float]]) -> list[float]:
    if not energy:
        return []
    values = [float(value) for _, value in energy if math.isfinite(float(value))]
    if not values:
        return []
    baseline = sorted(values)[len(values) // 2]
    return [round(float(time), 3) for time, value in energy if float(value) >= baseline + 6.0]


def suggest_events(
    words: Sequence[Any] = (),
    *,
    duration: float,
    transcript_text: str = "",
    energy: Sequence[tuple[float, float]] = (),
    silences: Sequence[tuple[float, float]] = (),
    scene_cuts: Sequence[float] = (),
    max_events: int = 24,
) -> list[MemeEvent]:
    """Suggest deterministic events from words plus optional acoustic/scene cues."""
    length = max(0.0, _number(duration))
    candidates: list[MemeEvent] = []
    for word in words:
        start, end, raw_text = _word_value(word)
        token = re.sub(r"[^a-z0-9']", "", raw_text.lower())
        effect = KEYWORD_EFFECTS.get(token)
        if not effect or start >= length:
            continue
        end = min(length, max(start + 0.12, end))
        candidates.append(
            MemeEvent(
                _stable_id("auto", "keyword", token, start),
                "visual",
                effect,
                start,
                min(end + 0.18, length),
                source="auto",
            )
        )
        candidates.append(
            MemeEvent(
                _stable_id("auto", "caption", token, start),
                "caption",
                "emphasis",
                start,
                min(end + 0.35, length),
                payload={"text": raw_text.strip()},
                source="auto",
            )
        )
        if len(candidates) >= max_events:
            break
    for at in _energy_peaks(energy):
        if 0.0 <= at < length:
            candidates.append(
                MemeEvent(
                    _stable_id("auto", "loud", at),
                    "visual",
                    "shake",
                    at,
                    min(length, at + 0.25),
                    source="auto",
                )
            )
    for _start, end in silences:
        end_time = _number(end)
        if end_time > 0.0 and end_time < length:
            candidates.append(
                MemeEvent(
                    _stable_id("auto", "silence", end_time),
                    "visual",
                    "flash",
                    end_time,
                    min(length, end_time + 0.12),
                    source="auto",
                )
            )
    for at in scene_cuts:
        cut = _number(at)
        if 0.0 < cut < length:
            candidates.append(
                MemeEvent(
                    _stable_id("auto", "scene", cut),
                    "visual",
                    "zoom_punch",
                    cut,
                    min(length, cut + 0.22),
                    source="auto",
                )
            )
    # Stable de-duplication keeps a loud keyword from receiving three identical effects.
    unique: dict[tuple[str, str, int], MemeEvent] = {}
    for event in candidates:
        key = (event.kind, event.effect, round(event.start * 20))
        unique.setdefault(key, event)
    return normalize_events(list(unique.values())[:max_events], length)


__all__ = [
    "KEYWORD_EFFECTS",
    "MemeEvent",
    "MemeTimeline",
    "PRESET_PACKS",
    "add_event",
    "event_from_dict",
    "normalize_events",
    "preset_events",
    "rebase_events",
    "remove_event",
    "suggest_events",
    "undo_event",
]
