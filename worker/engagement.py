"""Engagement scoring and retention-first edit planning.

This module is the deterministic bridge between *why* a moment is interesting and
*how* it should be edited.  It deliberately consumes already-measured signals from
the selector instead of opening media files or making a second model request.

The signals are complementary:

* hook promptness, speech-rate shifts, energy and pitch variation describe delivery;
* discourse features describe curiosity, emotion and standalone completeness;
* optional audience reaction events provide an external corroborating signal; and
* timestamped energy/pitch/reaction events become bounded zoom and sound cues.

Audience reactions are source-relative JSON supplied by the caller.  Accepted input
is either a JSON list of objects or ``{"reactions": [...]}``; each event may use
``time``/``timestamp``/``at`` and ``count``/``weight``/``value``.  Invalid events
are ignored rather than allowed to fail a render.

The planner is pure and JSON-serialisable.  It is safe to use in tests, in a review
UI, and during a rerender without making the output depend on a clock, network or
ffmpeg implementation.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from worker import audio_features
from worker.effects import caption_presets

VALID_MODES = ("off", "balanced", "aggressive")
_HOOK_WINDOW_S = 3.0
_MIN_CUT_S = 0.08
_MAX_CUES = 12


@dataclass(frozen=True)
class ReactionEvent:
    """One source-relative audience reaction event."""

    time: float
    weight: float = 1.0


@dataclass(frozen=True)
class RetentionPlan:
    """Concrete, bounded edit cues for one rendered clip."""

    mode: str = "off"
    hook_score: float = 0.0
    hook_window_s: float = 0.0
    emphasis_indices: tuple[int, ...] = ()
    zoom_beats: tuple[float, ...] = ()
    sfx_times: tuple[float, ...] = ()
    pacing_cuts: tuple[tuple[float, float], ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-friendly representation for the API and ClipResult."""
        return {
            "mode": self.mode,
            "hook_score": round(self.hook_score, 3),
            "hook_window_s": round(self.hook_window_s, 3),
            "emphasis_indices": list(self.emphasis_indices),
            "zoom_beats": [round(value, 3) for value in self.zoom_beats],
            "sfx_times": [round(value, 3) for value in self.sfx_times],
            "pacing_cuts": [[round(start, 3), round(end, 3)] for start, end in self.pacing_cuts],
            "reasons": list(self.reasons),
        }


def _finite(value: Any) -> float | None:
    """Coerce a finite number, rejecting booleans, NaN and infinities."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_reactions(payload: str | Sequence[Any] | None) -> tuple[ReactionEvent, ...]:
    """Parse optional audience reaction JSON into sorted finite events.

    The function accepts a list directly for Python callers and a JSON string for
    API/UI callers.  Reactions with a negative timestamp or non-positive weight are
    ignored.  Duplicate events are retained because a burst of messages at the same
    second is meaningful; cue de-duplication happens later at the render boundary.
    """
    if payload is None or payload == "":
        return ()
    data: Any = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return ()
    if isinstance(data, dict):
        data = data.get("reactions", data.get("events", []))
    if not isinstance(data, (list, tuple)):
        return ()

    events: list[ReactionEvent] = []
    for item in data:
        if isinstance(item, dict):
            raw_time = item.get("time", item.get("timestamp", item.get("at")))
            raw_weight = item.get("count", item.get("weight", item.get("value", 1.0)))
        else:
            raw_time, raw_weight = item, 1.0
        timestamp = _finite(raw_time)
        weight = _finite(raw_weight)
        if timestamp is None or weight is None or timestamp < 0.0 or weight <= 0.0:
            continue
        events.append(ReactionEvent(round(timestamp, 3), min(weight, 1_000_000.0)))
    return tuple(sorted(events, key=lambda event: (event.time, event.weight)))


def _number(features: dict[str, Any], key: str, default: float = 0.5) -> float:
    value = _finite(features.get(key, default))
    if value is None:
        return default
    return value


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _reaction_score(
    reactions: Sequence[ReactionEvent], start: float, end: float
) -> tuple[float, float, int]:
    """Return ``(normalised score, peak, count)`` for a source-relative window."""
    inside = [event for event in reactions if start <= event.time < end]
    if not inside:
        return 0.5, 0.0, 0
    all_values = [event.weight for event in reactions if event.weight > 0]
    baseline = sorted(all_values)[len(all_values) // 2] if all_values else 1.0
    baseline = max(1.0, baseline)
    total = sum(event.weight for event in inside)
    peak = max(event.weight for event in inside)
    density = total / max(1.0, end - start)
    score = _clamp(0.35 + 0.35 * _clamp(peak / (baseline * 3.0)) + 0.30 * _clamp(density / 3.0))
    return score, peak, len(inside)


def _energy_events(
    envelope: Sequence[tuple[float, float]], start: float, end: float
) -> list[float]:
    if not envelope:
        return []
    local = audio_features.detect_onsets(
        [(float(time), float(db)) for time, db in envelope],
        rise_db=4.0,
        min_gap=0.45,
    )
    return [round(time - start, 3) for time in local if start <= time < end]


def _pitch_events(
    track: Sequence[tuple[float, float]], start: float, end: float
) -> list[float]:
    """Find expressive pitch jumps without claiming to perform emotion recognition."""
    timeline: list[tuple[float, float]] = []
    for raw_time, raw_hz in track:
        time = _finite(raw_time)
        hz = _finite(raw_hz)
        if time is not None and hz is not None and hz > 0:
            timeline.append((time, hz))
    events: list[float] = []
    for (previous_time, previous_hz), (time, hz) in zip(timeline, timeline[1:]):
        semitones = abs(12.0 * math.log2(hz / previous_hz)) if previous_hz > 0 else 0.0
        if start <= time < end and semitones >= 2.0 and time - previous_time <= 0.5:
            events.append(round(time - start, 3))
    return events


def annotate_candidates(
    candidates: Iterable[Any],
    *,
    reactions: Sequence[ReactionEvent] = (),
    envelope: Sequence[tuple[float, float]] = (),
    pitch_track: Sequence[tuple[float, float]] = (),
) -> None:
    """Attach engagement features and timestamped event evidence in place."""
    for candidate in candidates:
        features = getattr(candidate, "features", None)
        if features is None:
            continue
        try:
            start = float(candidate.start)
            end = float(candidate.end)
        except (AttributeError, TypeError, ValueError):
            continue
        reaction, peak, count = _reaction_score(reactions, start, end)
        energy_events = _energy_events(envelope, start, end)
        pitch_events = _pitch_events(pitch_track, start, end)
        features["reaction_score"] = round(reaction, 4)
        features["reaction_peak"] = round(peak, 3)
        features["reaction_count"] = float(count)
        features["energy_onsets"] = energy_events
        features["pitch_events"] = pitch_events
        # Event arrays are local to the candidate window. Keep the origin beside
        # them so a later silence trim can map them onto the delivered timeline.
        features["engagement_event_start"] = round(start, 3)
        features["engagement_score"] = round(score_features(features), 4)


def score_features(features: dict[str, Any] | None) -> float:
    """Fuse measured signals into a bounded engagement score.

    Missing or unreliable features are neutral (0.5), never zero.  This is
    important for silent videos and for optional pitch/reaction inputs: absence of
    evidence must not be treated as evidence that a moment is boring.
    """
    features = features or {}
    hook = _clamp(_number(features, "hook_score"))
    if "relative_speech_rate" in features and _number(features, "reliable", 0.0) >= 1.0:
        pace = _clamp(abs(_number(features, "relative_speech_rate", 1.0) - 1.0) / 0.6)
    else:
        pace = 0.5
    energy = _clamp(0.5 + _number(features, "relative_energy_db", 0.0) / 12.0)
    pitch = _clamp(_number(features, "pitch_variation", 0.5))
    reaction = _clamp(_number(features, "reaction_score"))
    structure = _clamp(_number(features, "structure_score"))
    standalone = _clamp(_number(features, "standalone_score"))
    intensity = _clamp(_number(features, "intensity_score"))
    values = (hook, pace, energy, pitch, reaction, structure, standalone, intensity)
    weights = (0.24, 0.12, 0.14, 0.10, 0.14, 0.08, 0.12, 0.06)
    return _clamp(sum(value * weight for value, weight in zip(values, weights)))


def rerank(candidates: Sequence[Any], mode: str) -> list[Any]:
    """Blend model/base scores with engagement evidence when a retention mode is enabled."""
    resolved = mode if mode in VALID_MODES else "off"
    items = list(candidates)
    if resolved == "off":
        return items
    blend = 0.25 if resolved == "balanced" else 0.40
    for candidate in items:
        base = _clamp(_number(getattr(candidate, "features", {}) or {}, "base_score", 0.0) / 100.0)
        if base == 0.0:
            base = _clamp(float(getattr(candidate, "score", 0.0) or 0.0) / 100.0)
        engagement = _clamp(_number(getattr(candidate, "features", {}) or {}, "engagement_score"))
        candidate.score = round(100.0 * ((1.0 - blend) * base + blend * engagement), 1)
        if getattr(candidate, "features", None) is not None:
            candidate.features["retention_rank_score"] = candidate.score
    return sorted(items, key=lambda item: (-float(getattr(item, "score", 0.0) or 0.0), item.start))


def _word_bounds(word: Any) -> tuple[float, float] | None:
    start = _finite(getattr(word, "start", None))
    end = _finite(getattr(word, "end", None))
    if start is None or end is None or end <= start:
        return None
    return start, end


def _reaction_times_in_window(
    reactions: Sequence[ReactionEvent], source_start: float, duration: float
) -> list[float]:
    source_end = source_start + max(0.0, duration)
    return [
        round(event.time - source_start, 3)
        for event in reactions
        if source_start <= event.time < source_end
    ]


def _event_times(features: dict[str, Any], key: str, duration: float) -> list[float]:
    values = features.get(key, ()) if isinstance(features, dict) else ()
    if not isinstance(values, (list, tuple)):
        return []
    out: list[float] = []
    for value in values:
        time = _finite(value)
        if time is not None and 0.0 <= time < duration:
            out.append(round(time, 3))
    return out


def _unique_times(values: Iterable[float], duration: float, min_gap: float = 0.28) -> tuple[float, ...]:
    ordered = sorted(
        {round(float(value), 3) for value in values if 0.0 <= float(value) < max(0.0, duration)}
    )
    kept: list[float] = []
    for value in ordered:
        if not kept or value - kept[-1] >= min_gap:
            kept.append(value)
    return tuple(kept[:_MAX_CUES])


def _pacing_cuts(
    words: Sequence[Any],
    duration: float,
    *,
    aggressive: bool,
    protected_times: Sequence[float],
) -> tuple[tuple[float, float], ...]:
    """Return only long inter-word gaps, never a partial word."""
    bounds = [item for word in words if (item := _word_bounds(word)) is not None]
    if len(bounds) < 2:
        return ()
    threshold = 0.65 if aggressive else 0.95
    cuts: list[tuple[float, float]] = []
    for previous, following in zip(bounds, bounds[1:]):
        gap_start, gap_end = previous[1], following[0]
        if gap_end - gap_start < threshold:
            continue
        if any(gap_start <= protected < gap_end for protected in protected_times):
            continue
        start = max(0.0, min(duration, gap_start))
        end = max(0.0, min(duration, gap_end))
        if end - start >= _MIN_CUT_S:
            cuts.append((round(start, 3), round(end, 3)))
    return tuple(cuts[:_MAX_CUES])


def rebase_times(values: Sequence[float], keeps: Sequence[Any]) -> list[float]:
    """Map clip-relative event times through a concatenated keep list.

    A source range can occur more than once in a cold-open assembly, so every
    matching keep emits an occurrence. The result is intentionally a list; the
    normal cue de-duplication pass runs afterwards.
    """
    mapped: list[float] = []
    offset = 0.0
    for keep in keeps:
        start = _finite(getattr(keep, "start", None))
        end = _finite(getattr(keep, "end", None))
        if start is None or end is None or end <= start:
            continue
        for value in values:
            time = _finite(value)
            if time is not None and start <= time < end:
                mapped.append(round(offset + time - start, 3))
        offset += end - start
    return mapped


def rebase_reactions(
    reactions: Sequence[ReactionEvent], source_start: float, keeps: Sequence[Any]
) -> tuple[ReactionEvent, ...]:
    """Map source-relative reactions onto a clip's post-cut timeline."""
    local_times = [event.time - source_start for event in reactions]
    rebased: list[ReactionEvent] = []
    for event, time in zip(reactions, local_times):
        for mapped in rebase_times([time], keeps):
            rebased.append(ReactionEvent(mapped, event.weight))
    return tuple(sorted(rebased, key=lambda item: (item.time, item.weight)))


def plan_edit(
    words: Sequence[Any],
    features: dict[str, Any] | None,
    duration: float,
    *,
    mode: str = "off",
    source_start: float = 0.0,
    reactions: Sequence[ReactionEvent] = (),
) -> RetentionPlan:
    """Build a deterministic hook/caption/zoom/SFX/pacing plan for a clip.

    ``words`` are clip-relative.  Event features and ``reactions`` are
    source-relative; ``source_start`` maps them onto the delivered clip.  This
    explicit coordinate conversion prevents the common double-offset bug when a
    selected window is trimmed before rendering.
    """
    resolved = mode if mode in VALID_MODES else "off"
    duration = max(0.0, float(duration or 0.0))
    if resolved == "off" or duration <= 0.0:
        return RetentionPlan(mode="off")

    data = dict(features or {})
    hook_score = _clamp(_number(data, "hook_score", 0.5))
    hook_window = min(duration, _HOOK_WINDOW_S)
    local_reactions = _reaction_times_in_window(reactions, source_start, duration)
    event_origin = _number(data, "engagement_event_start", source_start)
    event_shift = event_origin - float(source_start)
    energy_events = [
        round(value + event_shift, 3)
        for value in _event_times(data, "energy_onsets", duration + abs(event_shift))
    ]
    pitch_events = [
        round(value + event_shift, 3)
        for value in _event_times(data, "pitch_events", duration + abs(event_shift))
    ]
    event_times = _unique_times(
        [*energy_events, *pitch_events, *local_reactions], duration, min_gap=0.32
    )

    # A strong opening receives one restrained punch even when no transient was
    # detected. The 0.35s anchor is inside the first caption phrase and avoids a
    # zoom that completes before the viewer can perceive it.
    beats: list[float] = list(event_times)
    if hook_score >= 0.58 and duration > 0.45:
        beats.insert(0, 0.35)
    beats = list(_unique_times(beats, duration, min_gap=0.32))

    protected = [0.35, *local_reactions]
    cuts = _pacing_cuts(
        words,
        duration,
        aggressive=resolved == "aggressive",
        protected_times=protected,
    )

    try:
        keyword_indices = tuple(
            int(index)
            for index in caption_presets.plan_keywords(words, use_ai=False)
            if 0 <= int(index) < len(words)
        )
    except (AttributeError, TypeError, ValueError):
        keyword_indices = ()
    keyword_indices = tuple(dict.fromkeys(keyword_indices))[:12]

    reasons: list[str] = []
    if hook_score >= 0.58:
        reasons.append("front_loaded_hook")
    if energy_events:
        reasons.append("energy_accents")
    if pitch_events:
        reasons.append("tone_variation")
    if local_reactions:
        reasons.append("audience_reactions")
    if cuts:
        reasons.append("dead_air_pacing")

    # Sound effects are deliberately more restrained than zooms. Balanced mode
    # gets visual accents only; aggressive mode gets at most four low-level pops.
    sfx_times = _unique_times(
        [*local_reactions, *pitch_events, *energy_events], duration, min_gap=0.6
    )
    if resolved != "aggressive":
        sfx_times = ()
    return RetentionPlan(
        mode=resolved,
        hook_score=round(hook_score, 4),
        hook_window_s=round(hook_window, 3),
        emphasis_indices=keyword_indices,
        zoom_beats=tuple(beats),
        sfx_times=sfx_times[:4],
        pacing_cuts=cuts if resolved == "aggressive" else (),
        reasons=tuple(dict.fromkeys(reasons)),
    )


def reaction_note(
    start: float, end: float, reactions: Sequence[ReactionEvent]
) -> str:
    """Prompt-safe qualitative annotation for a transcript segment."""
    score, peak, count = _reaction_score(reactions, start, end)
    if count == 0:
        return ""
    if peak >= 5 or score >= 0.8:
        return "audience reaction spike"
    return "audience reactions"
