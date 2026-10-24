"""Pure storytelling and interactive-hook planning for short clips.

This module only emits recommendations. It does not download media, call an LLM,
inspect a video, or mutate a pipeline object. Operators can review the plan and
then supply or license any image, screenshot, chart, movie still, or meme asset.

Inputs are duck-typed so the planner accepts ``worker.transcribe`` words,
mapping payloads, and malformed values without making the render path fragile.
All public results have ``to_dict`` methods and contain JSON-compatible values.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_DURATION = 0.0
PATTERN_INTERVAL_SECONDS = 5.0
PATTERN_FIRST_SECONDS = 4.0
PATTERN_DURATION_SECONDS = 0.8
MAX_ENTITY_CARDS = 16
MAX_CONCEPT_OVERLAYS = 20
MAX_INTERACTIVE_OVERLAYS = 12

_WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)?", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_QUESTION_WORDS = frozenset(
    {
        "who",
        "what",
        "when",
        "where",
        "why",
        "how",
        "which",
        "can",
        "could",
        "should",
        "would",
        "is",
        "are",
        "do",
        "does",
        "did",
    }
)
_ENTITY_TYPES = frozenset(
    {
        "book",
        "tweet",
        "post",
        "chart",
        "movie",
        "film",
        "person",
        "company",
        "product",
        "podcast",
        "article",
        "paper",
        "website",
        "place",
        "event",
        "brand",
        "unknown",
    }
)
_ENTITY_TYPE_ALIASES = {
    "books": "book",
    "films": "movie",
    "movies": "movie",
    "people": "person",
    "posts": "post",
    "charts": "chart",
    "articles": "article",
    "papers": "paper",
}


def _finite(value: Any) -> float | None:
    """Return a finite float, excluding booleans and ``nan``/infinity."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _duration(value: Any) -> float:
    number = _finite(value)
    return round(max(0.0, number if number is not None else DEFAULT_DURATION), 3)


def _bound_time(value: Any, duration: float, default: float = 0.0) -> float:
    number = _finite(value)
    if number is None:
        number = default
    return round(max(0.0, min(duration, number)), 3)


def _window(
    start: Any, end: Any, duration: float, fallback_end: float = 0.0
) -> tuple[float, float]:
    bounded_start = _bound_time(start, duration)
    bounded_end = _bound_time(end, duration, fallback_end)
    if bounded_end < bounded_start:
        bounded_end = bounded_start
    return bounded_start, bounded_end


def _text(value: Any) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return ""
    try:
        return _SPACE_RE.sub(" ", str(value)).strip()
    except Exception:
        return ""


def _tokens(value: Any) -> list[str]:
    return [token.casefold() for token in _WORD_RE.findall(_text(value))]


def _phrase_key(value: Any) -> str:
    return " ".join(_tokens(value))


def _round(value: float) -> float:
    return round(float(value), 3)


def _get(item: Any, *keys: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        for key in keys:
            if key in item:
                return item[key]
        return default
    for key in keys:
        try:
            return getattr(item, key)
        except Exception:
            continue
    return default


def _json_value(value: Any) -> Any:
    """Recursively convert dataclasses/tuples into strict JSON-compatible values."""

    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    if hasattr(value, "__dataclass_fields__"):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


@dataclass(frozen=True)
class TranscriptWord:
    """A normalized word used by the planner."""

    text: str
    start: float = 0.0
    end: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class EntityMention:
    """An entity mention with optional transcript timing."""

    text: str
    entity_type: str = "unknown"
    start: float = 0.0
    end: float = 0.0
    source: str = "operator_or_transcript"

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class EntityProofCard:
    """A proof-card recommendation; the media itself is never claimed to exist."""

    entity: str
    entity_type: str
    start: float
    end: float
    label: str
    proof_prompt: str
    is_suggestion: bool = True
    asset_status: str = "operator_provided_or_licensed_media_required"
    asset_required: bool = True
    source: str = "transcript_entity"
    scene_context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class OverlaySuggestion:
    """A deterministic concept or meme overlay recommendation."""

    overlay_kind: str
    concept: str
    start: float
    end: float
    prompt: str
    label: str
    is_suggestion: bool = True
    asset_status: str = "suggestion_only"
    asset_required: bool = False
    scene_context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class PatternInterrupter:
    """A bounded point where an editor can change visual rhythm."""

    start: float
    end: float
    style: str
    prompt: str
    is_suggestion: bool = True
    asset_status: str = "suggestion_only"
    asset_required: bool = False
    scene_context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class InteractiveOverlay:
    """A poll or question prompt for an interactive surface."""

    overlay_kind: str
    prompt: str
    start: float
    end: float
    options: tuple[str, ...] = ()
    is_suggestion: bool = True
    asset_status: str = "suggestion_only"
    asset_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class CuriosityLoop:
    """The opener promise and the evidence that made it worth suggesting."""

    detected: bool = False
    start: float = 0.0
    end: float = 0.0
    opener: str = ""
    promise: str = ""
    reason: str = ""
    is_suggestion: bool = True
    asset_status: str = "suggestion_only"
    asset_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class EndCardPlan:
    """A detected cliffhanger or Part 2 end-card opportunity."""

    detected: bool = False
    kind: str = "none"
    start: float = 0.0
    end: float = 0.0
    text: str = ""
    reason: str = ""
    is_suggestion: bool = True
    asset_status: str = "suggestion_only"
    asset_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class StorytellingPlan:
    """Complete, JSON-serializable storytelling plan for one clip."""

    duration: float
    entity_proof_cards: tuple[EntityProofCard, ...] = ()
    concept_overlays: tuple[OverlaySuggestion, ...] = ()
    meme_overlays: tuple[OverlaySuggestion, ...] = ()
    pattern_interrupters: tuple[PatternInterrupter, ...] = ()
    interactive_overlays: tuple[InteractiveOverlay, ...] = ()
    curiosity_loop: CuriosityLoop = field(default_factory=CuriosityLoop)
    end_card: EndCardPlan = field(default_factory=EndCardPlan)

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


def normalize_words(words: Any, *, duration: Any = None) -> tuple[TranscriptWord, ...]:
    """Normalize word-like input while ignoring malformed entries."""

    clip_duration = _duration(duration)
    if isinstance(words, str):
        values: Iterable[Any] = words.split()
    elif isinstance(words, Mapping):
        if any(key in words for key in ("text", "word", "token")):
            values = (words,)
        else:
            values = words.get("words", words.get("transcript", ()))
            if isinstance(values, str):
                values = values.split()
    else:
        try:
            values = words or ()
            if not isinstance(values, Iterable):
                values = _get(words, "words", "tokens", "transcript", default=())
        except Exception:
            values = ()
    if isinstance(values, (bytes, bytearray)) or not isinstance(values, Iterable):
        return ()

    result: list[TranscriptWord] = []
    for value in values:
        if isinstance(value, str):
            text = _text(value)
            start, end = 0.0, 0.0
        else:
            text = _text(_get(value, "text", "word", "token", default=""))
            start, end = _window(
                _get(value, "start", "begin", "at", default=0.0),
                _get(value, "end", "stop", default=0.0),
                clip_duration,
            )
        if text:
            result.append(TranscriptWord(text=text, start=start, end=end))
    return tuple(result)


def _word_window(
    words: Sequence[TranscriptWord], index: int, duration: float
) -> tuple[float, float]:
    word = words[index]
    start, end = word.start, word.end
    if end <= start:
        end = min(duration, start + 0.8)
    if end <= start:
        end = min(duration, start + 0.1)
    return _window(start, end, duration, fallback_end=start)


def _scene_items(scene_metadata: Any) -> list[Mapping[str, Any]]:
    if isinstance(scene_metadata, Mapping):
        raw = scene_metadata.get("scenes", scene_metadata.get("shots", ()))
    else:
        raw = scene_metadata
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _scene_context(time: float, scene_metadata: Any) -> str:
    """Return a small stable context label from optional scene metadata."""

    for scene in _scene_items(scene_metadata):
        start = _finite(scene.get("start", scene.get("begin", 0.0)))
        end = _finite(scene.get("end", scene.get("stop", 0.0)))
        if start is None or end is None or not start <= time <= end:
            continue
        for key in ("context", "type", "label", "shot_type"):
            label = _text(scene.get(key))
            if label:
                return label.casefold()
        if scene.get("faces") or scene.get("face_count"):
            return "face_present"
        if scene.get("motion") or scene.get("motion_score"):
            return "motion_present"
        return "scene"
    return ""


def _scene_cuts(scene_metadata: Any, duration: float) -> tuple[float, ...]:
    if isinstance(scene_metadata, Mapping):
        raw = scene_metadata.get(
            "shot_boundaries", scene_metadata.get("cuts", scene_metadata.get("scene_changes", ()))
        )
    else:
        raw = ()
    if not isinstance(raw, (list, tuple)):
        return ()
    values = {_bound_time(value, duration) for value in raw if _finite(value) is not None}
    return tuple(sorted(value for value in values if 0.0 < value < duration))


def _normalize_entity(
    item: Any, words: Sequence[TranscriptWord], duration: float
) -> EntityMention | None:
    if isinstance(item, str):
        text = _text(item)
        entity_type = "unknown"
        start = end = 0.0
        source = "operator_or_transcript"
    else:
        text = _text(_get(item, "text", "name", "entity", "title", default=""))
        entity_type = _text(_get(item, "entity_type", "type", "kind", "label", default=""))
        start = _get(item, "start", "begin", "at", default=0.0)
        end = _get(item, "end", "stop", default=0.0)
        source = (
            _text(_get(item, "source", default="operator_or_transcript"))
            or "operator_or_transcript"
        )
    if not text:
        return None
    normalized_type = _phrase_key(entity_type).replace(" ", "_")
    normalized_type = _ENTITY_TYPE_ALIASES.get(normalized_type, normalized_type)
    if normalized_type not in _ENTITY_TYPES:
        normalized_type = "unknown"
    bounded_start, bounded_end = _window(start, end, duration)
    if bounded_end <= bounded_start:
        mention_key = _phrase_key(text)
        phrase = " ".join(_phrase_key(word.text) for word in words)
        if mention_key and mention_key in phrase:
            target = mention_key.split()
            for word_index in range(len(words) - len(target) + 1):
                word_keys = [
                    _phrase_key(word.text) for word in words[word_index : word_index + len(target)]
                ]
                if word_keys == target:
                    bounded_start, bounded_end = _word_window(words, word_index, duration)
                    if len(target) > 1:
                        bounded_end = _word_window(words, word_index + len(target) - 1, duration)[1]
                    break
    return EntityMention(
        text=text,
        entity_type=normalized_type,
        start=bounded_start,
        end=bounded_end,
        source=source,
    )


def infer_entities(
    words: Any, *, entities: Any = None, duration: Any = None
) -> tuple[EntityMention, ...]:
    """Normalize explicit entities and entity annotations embedded in words.

    Capitalisation alone is not treated as reliable NER. Callers with entity
    extraction results should pass those results through ``entities`` or a
    word's ``entity`` field.
    """

    clip_duration = _duration(duration)
    normalized_words = normalize_words(words, duration=clip_duration)
    candidates: list[Any] = []
    if isinstance(entities, Mapping):
        raw_entities = entities.get("entities", ())
        if isinstance(raw_entities, (str, Mapping)):
            raw_entities = (raw_entities,)
        if isinstance(raw_entities, Iterable):
            candidates.extend(raw_entities)
    elif isinstance(entities, (list, tuple, set)):
        candidates.extend(entities)
    elif entities:
        candidates.append(entities)

    if isinstance(words, Mapping):
        raw_words: Any = words.get("words", ()) or ()
    elif isinstance(words, str):
        raw_words = ()
    else:
        try:
            raw_words = words or ()
        except Exception:
            raw_words = ()
    if isinstance(raw_words, Iterable) and not isinstance(
        raw_words, (str, bytes, bytearray, Mapping)
    ):
        for raw_word in raw_words:
            annotation = _get(raw_word, "entity", "entity_name", "named_entity", default=None)
            if annotation:
                if isinstance(annotation, Mapping):
                    merged = dict(annotation)
                    merged.setdefault("start", _get(raw_word, "start", default=0.0))
                    merged.setdefault("end", _get(raw_word, "end", default=0.0))
                    candidates.append(merged)
                else:
                    candidates.append(
                        {
                            "text": annotation,
                            "type": _get(raw_word, "entity_type", "type", default="unknown"),
                            "start": _get(raw_word, "start", default=0.0),
                            "end": _get(raw_word, "end", default=0.0),
                        }
                    )

    result: list[EntityMention] = []
    seen: set[tuple[str, str, float, float]] = set()
    for candidate in candidates:
        mention = _normalize_entity(candidate, normalized_words, clip_duration)
        if mention is None:
            continue
        key = (_phrase_key(mention.text), mention.entity_type, mention.start, mention.end)
        if key not in seen:
            seen.add(key)
            result.append(mention)
    result.sort(key=lambda item: (item.start, item.end, _phrase_key(item.text), item.entity_type))
    return tuple(result)


def plan_entity_proof_cards(
    words: Any = (),
    entities: Any = None,
    duration: Any = 0.0,
    scene_metadata: Any = None,
) -> tuple[EntityProofCard, ...]:
    """Suggest proof cards for explicit or transcript-annotated entities."""

    clip_duration = _duration(duration)
    mentions = infer_entities(words, entities=entities, duration=clip_duration)
    cards: list[EntityProofCard] = []
    seen: set[tuple[str, str]] = set()
    for mention in mentions:
        key = (_phrase_key(mention.text), mention.entity_type)
        if key in seen:
            continue
        seen.add(key)
        start, end = mention.start, mention.end
        if end <= start and clip_duration > start:
            end = min(clip_duration, start + 1.2)
        label = mention.entity_type if mention.entity_type != "unknown" else "entity"
        cards.append(
            EntityProofCard(
                entity=mention.text,
                entity_type=label,
                start=start,
                end=end,
                label=f"Proof: {mention.text}",
                proof_prompt=f"Show a licensed or operator-supplied {label} reference for {mention.text}.",
                source=mention.source,
                scene_context=_scene_context(start, scene_metadata),
            )
        )
        if len(cards) >= MAX_ENTITY_CARDS:
            break
    return tuple(cards)


_CONCEPT_RULES: tuple[tuple[str, frozenset[str], str, str], ...] = (
    (
        "money",
        frozenset({"money", "revenue", "sales", "profit", "price", "cost", "dollar", "dollars"}),
        "currency",
        "Use a simple currency or number graphic.",
    ),
    (
        "growth",
        frozenset({"growth", "grow", "grew", "increase", "increased", "scale", "scaling"}),
        "upward_chart",
        "Use an upward chart or arrow graphic.",
    ),
    (
        "risk",
        frozenset(
            {"risk", "danger", "warning", "careful", "threat", "problem", "failure", "failed"}
        ),
        "warning",
        "Use a warning or red-flag graphic.",
    ),
    (
        "time",
        frozenset({"time", "deadline", "today", "tomorrow", "before", "after", "wait"}),
        "clock",
        "Use a clock, countdown, or timeline graphic.",
    ),
    (
        "idea",
        frozenset({"idea", "strategy", "plan", "insight", "think", "thought"}),
        "lightbulb",
        "Use a lightbulb or brief concept diagram.",
    ),
    (
        "success",
        frozenset({"win", "won", "success", "successful", "result", "results"}),
        "celebration",
        "Use a restrained success or result graphic.",
    ),
)
_MEME_RULES: tuple[tuple[str, frozenset[str], str, str], ...] = (
    (
        "plot_twist",
        frozenset({"actually", "unexpected", "surprise", "surprisingly", "plot", "twist"}),
        "plot-twist reaction",
        "Operator-supplied or licensed reaction/meme template.",
    ),
    (
        "overwhelmed",
        frozenset({"insane", "wild", "chaos", "confusing", "overwhelming"}),
        "overwhelmed reaction",
        "Operator-supplied or licensed reaction/meme template.",
    ),
    (
        "nobody_expected",
        frozenset({"nobody", "never", "literally"}),
        "nobody expected this",
        "Operator-supplied or licensed reaction/meme template.",
    ),
)


def _rule_overlay(
    words: Sequence[TranscriptWord],
    duration: float,
    scene_metadata: Any,
    rules: Sequence[tuple[str, frozenset[str], str, str]],
    kind: str,
    *,
    asset_required: bool,
) -> tuple[OverlaySuggestion, ...]:
    result: list[OverlaySuggestion] = []
    seen: set[tuple[str, float]] = set()
    for index, word in enumerate(words):
        token = _tokens(word.text)
        if not token:
            continue
        for concept, triggers, label, prompt in rules:
            if not triggers.intersection(token):
                continue
            start, end = _word_window(words, index, duration)
            key = (concept, start)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                OverlaySuggestion(
                    overlay_kind=kind,
                    concept=concept,
                    start=start,
                    end=end,
                    prompt=prompt,
                    label=label,
                    asset_status=(
                        "operator_provided_or_licensed_media_required"
                        if asset_required
                        else "suggestion_only"
                    ),
                    asset_required=asset_required,
                    scene_context=_scene_context(start, scene_metadata),
                )
            )
            break
        if len(result) >= MAX_CONCEPT_OVERLAYS:
            break
    return tuple(result)


def suggest_concept_overlays(
    words: Any = (), duration: Any = 0.0, scene_metadata: Any = None
) -> tuple[OverlaySuggestion, ...]:
    """Suggest deterministic, text/shape/emoji-friendly concept overlays."""

    clip_duration = _duration(duration)
    return _rule_overlay(
        normalize_words(words, duration=clip_duration),
        clip_duration,
        scene_metadata,
        _CONCEPT_RULES,
        "concept",
        asset_required=False,
    )


def suggest_meme_overlays(
    words: Any = (), duration: Any = 0.0, scene_metadata: Any = None
) -> tuple[OverlaySuggestion, ...]:
    """Suggest meme concepts without pretending a meme asset has been sourced."""

    clip_duration = _duration(duration)
    return _rule_overlay(
        normalize_words(words, duration=clip_duration),
        clip_duration,
        scene_metadata,
        _MEME_RULES,
        "meme",
        asset_required=True,
    )


def suggest_visual_overlays(
    words: Any = (), duration: Any = 0.0, scene_metadata: Any = None
) -> tuple[OverlaySuggestion, ...]:
    """Return concept and meme suggestions in stable transcript order."""

    concepts = suggest_concept_overlays(words, duration, scene_metadata)
    memes = suggest_meme_overlays(words, duration, scene_metadata)
    return tuple(
        sorted(concepts + memes, key=lambda item: (item.start, item.overlay_kind, item.concept))
    )


def plan_pattern_interrupters(
    duration: Any,
    scene_metadata: Any = None,
    *,
    interval_seconds: Any = PATTERN_INTERVAL_SECONDS,
) -> tuple[PatternInterrupter, ...]:
    """Place deterministic rhythm changes at five-second intervals.

    Five seconds is inside the requested four-to-six-second cadence. A scene
    cut within half a second of a scheduled point is preferred.
    """

    clip_duration = _duration(duration)
    if clip_duration <= PATTERN_FIRST_SECONDS:
        return ()
    interval = _finite(interval_seconds)
    interval = max(4.0, min(6.0, interval if interval is not None else PATTERN_INTERVAL_SECONDS))
    cuts = _scene_cuts(scene_metadata, clip_duration)
    result: list[PatternInterrupter] = []
    scheduled = PATTERN_FIRST_SECONDS
    while scheduled < clip_duration:
        start = scheduled
        nearby = [cut for cut in cuts if scheduled <= cut <= scheduled + 0.5]
        if nearby:
            start = min(nearby, key=lambda cut: (abs(cut - scheduled), cut))
        end = min(clip_duration, start + PATTERN_DURATION_SECONDS)
        if end > start:
            result.append(
                PatternInterrupter(
                    start=_round(start),
                    end=_round(end),
                    style=("shot_change" if nearby else "micro_zoom"),
                    prompt=(
                        "Use the nearby shot change as a visual reset."
                        if nearby
                        else "Add a brief crop, scale, caption treatment, or cutaway reset."
                    ),
                    scene_context=_scene_context(start, scene_metadata),
                )
            )
        scheduled += interval
    return tuple(result)


def _question_text(words: Sequence[TranscriptWord], index: int) -> str:
    start = max(0, index - 3)
    end = min(len(words), index + 9)
    parts: list[str] = []
    for position in range(start, end):
        parts.append(words[position].text)
        if position >= index and any(mark in words[position].text for mark in ".!?;:"):
            break
    return " ".join(parts).strip().rstrip(".,;")


def _question_options(prompt: str) -> tuple[str, ...]:
    if " or " in prompt.casefold():
        left, right = re.split(r"\s+or\s+", prompt, maxsplit=1, flags=re.IGNORECASE)
        left = _text(left).strip(" ?.,!")
        right = _text(right).strip(" ?.,!")
        if left and right:
            return (left, right)
    return ("Yes", "No")


def plan_interactive_overlays(
    words: Any = (), duration: Any = 0.0, scene_metadata: Any = None
) -> tuple[InteractiveOverlay, ...]:
    """Turn explicit transcript questions into poll/question suggestions."""

    clip_duration = _duration(duration)
    normalized = normalize_words(words, duration=clip_duration)
    result: list[InteractiveOverlay] = []
    seen: set[str] = set()
    for index, word in enumerate(normalized):
        tokens = _tokens(word.text)
        if not tokens:
            continue
        at_sentence_start = index == 0 or normalized[index - 1].text.rstrip().endswith(
            (".", "!", "?")
        )
        is_question = "?" in word.text or (
            at_sentence_start and bool(set(tokens).intersection(_QUESTION_WORDS))
        )
        if not is_question:
            continue
        prompt = _question_text(normalized, index)
        if not prompt:
            continue
        start, end = _word_window(normalized, index, clip_duration)
        key = _phrase_key(prompt)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            InteractiveOverlay(
                overlay_kind=("poll" if " or " in prompt.casefold() else "question"),
                prompt=prompt if prompt.endswith("?") else f"{prompt}?",
                start=start,
                end=end,
                options=_question_options(prompt),
            )
        )
        if len(result) >= MAX_INTERACTIVE_OVERLAYS:
            break
    return tuple(result)


def plan_curiosity_loop(
    words: Any = (), duration: Any = 0.0, scene_metadata: Any = None
) -> CuriosityLoop:
    """Detect a question, promise, or contrast in the opening seconds."""

    clip_duration = _duration(duration)
    normalized = normalize_words(words, duration=clip_duration)
    if not normalized or clip_duration <= 0:
        return CuriosityLoop()
    opening = [word for word in normalized if word.start < min(clip_duration, 4.0)]
    if not opening:
        opening = list(normalized[:8])
    opener = " ".join(word.text for word in opening[:12]).strip()
    lowered = opener.casefold()
    tokens = _tokens(opener)
    reasons: list[str] = []
    if "?" in opener or (tokens and tokens[0] in _QUESTION_WORDS):
        reasons.append("opening_question")
    if any(
        marker in lowered for marker in ("but", "until", "actually", "nobody", "secret", "truth")
    ):
        reasons.append("open_loop_language")
    if re.search(r"\b\d+\b", lowered):
        reasons.append("specific_promise")
    if not reasons:
        return CuriosityLoop()
    start = _bound_time(opening[0].start, clip_duration)
    end = _bound_time(opening[-1].end, clip_duration, start + 2.5)
    if end <= start:
        end = min(clip_duration, start + 2.5)
    promise = (
        "Invite the viewer to stay for the answer."
        if "opening_question" in reasons
        else "Tease the missing explanation before revealing the payoff."
    )
    return CuriosityLoop(
        detected=True,
        start=start,
        end=end,
        opener=opener,
        promise=promise,
        reason=", ".join(reasons),
    )


_PART_TWO_PATTERNS: tuple[tuple[str, str], ...] = (
    ("part 2", "part_2"),
    ("part two", "part_2"),
    ("to be continued", "cliffhanger"),
    ("continued in", "cliffhanger"),
    ("next episode", "cliffhanger"),
    ("next part", "cliffhanger"),
    ("there's more", "cliffhanger"),
    ("theres more", "cliffhanger"),
    ("that's not all", "cliffhanger"),
    ("thats not all", "cliffhanger"),
    ("follow for part", "part_2"),
)


def detect_cliffhanger(
    words: Any = (), duration: Any = 0.0, scene_metadata: Any = None
) -> EndCardPlan:
    """Detect explicit Part 2 / continuation language near the clip ending."""

    clip_duration = _duration(duration)
    normalized = normalize_words(words, duration=clip_duration)
    if not normalized or clip_duration <= 0:
        return EndCardPlan()
    final_words = [word for word in normalized if word.start >= max(0.0, clip_duration - 8.0)]
    if not final_words:
        final_words = list(normalized[-12:])
    final_text = " ".join(word.text for word in final_words)
    lowered = final_text.casefold().replace("’", "'")
    match: tuple[str, str] | None = None
    for pattern, kind in _PART_TWO_PATTERNS:
        if pattern in lowered:
            match = (pattern, kind)
            break
    if match is None:
        return EndCardPlan()
    start = _bound_time(final_words[0].start, clip_duration)
    return EndCardPlan(
        detected=True,
        kind=match[1],
        start=start,
        end=clip_duration,
        text="PART 2" if match[1] == "part_2" else "TO BE CONTINUED",
        reason=f"detected continuation phrase: {match[0]}",
    )


def plan_storytelling(
    words: Any = (),
    duration: Any = 0.0,
    entities: Any = None,
    scene_metadata: Any = None,
    *,
    pattern_interval: Any = PATTERN_INTERVAL_SECONDS,
) -> StorytellingPlan:
    """Build the complete pure storytelling plan for one clip.

    The usual call is ``plan_storytelling(words, duration, entities=...)``.
    For callers that naturally order arguments as ``words, entities, duration,
    scene_metadata``, a non-numeric second argument and numeric third argument
    are accepted and normalized here as well.
    """

    if _finite(duration) is None and _finite(entities) is not None:
        duration, entities = entities, duration
    clip_duration = _duration(duration)
    concept_overlays = suggest_concept_overlays(words, clip_duration, scene_metadata)
    meme_overlays = suggest_meme_overlays(words, clip_duration, scene_metadata)
    return StorytellingPlan(
        duration=clip_duration,
        entity_proof_cards=plan_entity_proof_cards(words, entities, clip_duration, scene_metadata),
        concept_overlays=concept_overlays + meme_overlays,
        meme_overlays=meme_overlays,
        pattern_interrupters=plan_pattern_interrupters(
            clip_duration, scene_metadata, interval_seconds=pattern_interval
        ),
        interactive_overlays=plan_interactive_overlays(words, clip_duration, scene_metadata),
        curiosity_loop=plan_curiosity_loop(words, clip_duration, scene_metadata),
        end_card=detect_cliffhanger(words, clip_duration, scene_metadata),
    )


# Friendly aliases for callers that describe the feature as visual depth/hooks.
build_storytelling_plan = plan_storytelling
plan_contextual_visual_depth = plan_storytelling
plan_visual_depth = plan_storytelling
plan_interactive_hooks = plan_interactive_overlays
detect_part_two = detect_cliffhanger
detect_end_card = detect_cliffhanger
suggest_meme_concepts = suggest_meme_overlays


__all__ = [
    "CuriosityLoop",
    "EndCardPlan",
    "EntityMention",
    "EntityProofCard",
    "InteractiveOverlay",
    "OverlaySuggestion",
    "PatternInterrupter",
    "StorytellingPlan",
    "TranscriptWord",
    "build_storytelling_plan",
    "detect_cliffhanger",
    "detect_end_card",
    "detect_part_two",
    "infer_entities",
    "normalize_words",
    "plan_contextual_visual_depth",
    "plan_visual_depth",
    "plan_curiosity_loop",
    "plan_entity_proof_cards",
    "plan_interactive_hooks",
    "plan_interactive_overlays",
    "plan_pattern_interrupters",
    "plan_storytelling",
    "suggest_concept_overlays",
    "suggest_meme_concepts",
    "suggest_meme_overlays",
    "suggest_visual_overlays",
]
