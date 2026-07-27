"""Kinetic typography engine — vocabularies, options, and plan value records.

This module owns the kinetic caption engine: the Kinetic_Style / Reveal_Mode
vocabularies and documented defaults (task 3.1), the :class:`Kinetic_Options`
projection of ``ProcessingOptions`` (tasks 3.2-3.3), and the
:class:`Kinetic_Word` / :class:`Kinetic_Cue` / :class:`Kinetic_Plan` records the
pure planner and the pure ASS emitter exchange (task 3.4).

**Import safety (Reqs 1.4, 1.7, 18.2).** The module imports cleanly with **no
ffmpeg binary, no libass, and no optional font present**: at import time it
touches only the standard library, :mod:`worker.effects.caption_presets`, and
:mod:`worker.engines.*` — all of which are themselves import-safe — and it
executes no probe, no subprocess, no network call, and no filesystem access.
(:mod:`worker.captions` is deliberately *not* imported at module scope — it pulls
in ``config``, hence ``pydantic`` — so the layout helpers, planner and emitter
reach its ``_POSITION_ALIGN`` table, escaping, cue grouping and timestamp helpers
through the lazy :func:`_captions` accessor and *reuse* them rather than restating
them.) Every heavy
dependency (the ffmpeg capability
probe, the libass burn, the system font enumeration, the optional LLM keyword
planner) is reached through a *lazy call* made from ``run``/``plan`` at
invocation time, never from module scope.

**Determinism (Req 11).** No ``time``, ``datetime``, ``os.getpid`` or ``locale``
symbol is imported here; mappings are serialised in sorted key order; the only
randomness source an engine may use is ``Engine_Context.rng()``.

The engine class itself (``Kinetic_Typography_Engine``), the layout helpers, the
planner, and the emitter land in later tasks of this spec; this file deliberately
registers nothing yet.
"""

from __future__ import annotations

import dataclasses
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from worker.effects import caption_presets
from worker.engines.base import (
    coerce_bool,
    coerce_choice,
    coerce_float,
    coerce_int,
    coerce_str,
)
from worker.engines.timebase import Timeline_Segment

__all__ = [
    "ASS_NAME",
    "BOUNCE_OVERSHOOT",
    "CUE_FADE_MS",
    "DEFAULT_REVEAL",
    "DEFAULT_STYLE",
    "FALLBACK_FONT",
    "KINETIC_STYLES",
    "KINETIC_Z_ORDER",
    "MIN_WORD_S",
    "POSITIONS",
    "REVEAL_MODES",
    "SLIDE_UP_PX",
    "SYNTHESISED_RATIO_LIMIT",
    "Kinetic_Cue",
    "Kinetic_Options",
    "Kinetic_Plan",
    "Kinetic_Word",
    "display_width",
    "is_space_free",
    "join_separator",
    "join_width",
    "pack_lines",
    "position_align",
    "resolve_position",
    "safe_area_margins",
]

# ---------------------------------------------------------------------------
# Vocabularies and documented defaults (task 3.1)
# ---------------------------------------------------------------------------

#: Req 4.1 — the Kinetic_Style vocabulary, sorted for deterministic iteration.
#: Pinned against ``tests.strategies.KINETIC_STYLES``, which repeats these seven
#: values literally; the two spellings must stay identical (task 9.4 asserts it).
KINETIC_STYLES: tuple[str, ...] = (
    "bounce",
    "highlight_sweep",
    "karaoke_fill",
    "none",
    "pop",
    "slide_up",
    "typewriter",
)

#: Req 4.8 — the documented substitution for an unrecognised Kinetic_Style; it
#: matches the look of ``caption_presets.FALLBACK_PRESET_NAME`` ("karaoke").
DEFAULT_STYLE = "karaoke_fill"

#: Req 4.9 — the Reveal_Mode vocabulary, sorted. Pinned against
#: ``tests.strategies.REVEAL_MODES``.
REVEAL_MODES: tuple[str, ...] = ("cumulative", "word_by_word")

#: Req 4.9 — the documented default Reveal_Mode.
DEFAULT_REVEAL = "cumulative"

#: Req 7.3 — caption placements; the sorted spelling of
#: ``caption_presets.VALID_POSITIONS``.
POSITIONS: tuple[str, ...] = ("bottom", "center", "top")

#: Req 9.3 — the last rung of the font ladder (``captions._FALLBACK_FONT``).
FALLBACK_FONT = "Arial"

#: Req 2.3 — Caption_Layer z-order band for the compose contribution.
KINETIC_Z_ORDER = 100

#: Req 16.3 — at most one ASS document per invocation, always this file name.
ASS_NAME = "kinetic.ass"

#: Req 6.2 — minimum on-screen duration for a word, in seconds.
MIN_WORD_S = 0.08

#: Req 6.3 — synthesised-timing ratio above which the plan degrades to cue-level.
SYNTHESISED_RATIO_LIMIT = 0.40

#: Req 6.4 — ``\fad(in, out)`` milliseconds used by the cue-level fallback.
CUE_FADE_MS: tuple[int, int] = (120, 120)

#: Req 4.4 — ``bounce`` overshoot scale in percent, before settling at 100.
BOUNCE_OVERSHOOT = 118

#: Req 4.5 — ``slide_up`` entry offset in pixels below the resolved position.
SLIDE_UP_PX = 40

#: Legal ``Kinetic_Options.position`` values: the three placements plus ``""``,
#: which means "inherit the Base_Preset position" (Req 7.4).
_POSITION_CHOICES: tuple[str, ...] = POSITIONS + ("",)

#: Resolution provenance carried on ``Kinetic_Options.notes`` (Req 4.8). Only
#: these spellings are inherited when re-resolving an already-resolved value, so
#: ``from_processing_options`` stays idempotent without importing foreign notes.
_KNOWN_NOTES: tuple[str, ...] = ("position_substituted", "style_substituted")

#: Upper bound for free-text fields, generous enough for an over-long token or a
#: full ASS ``Style:`` line while still bounding the output size (Req 16.4).
_TEXT_LIMIT = 4096


def _write_text_utf8(path: Any, text: Any) -> None:
    """Write ``text`` to ``path`` as UTF-8 with LF line endings.

    The engine's single impure step (Req 12.1). ``newline="\\n"`` pins the line
    endings so the written bytes are byte-identical on every host (Req 11.7);
    ``OSError`` is deliberately left to propagate so ``run`` can report
    ``failed`` with the error summary (Req 12.5).
    """
    target = Path(path)
    parent = target.parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_text(text), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# Small total helpers (no host state, no locale, no clock)
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """Return ``value`` as a ``str``, never raising."""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # pragma: no cover - __str__ that raises
        return ""


def _is_member(value: Any, choices: tuple[str, ...]) -> bool:
    """True when ``value`` is one of ``choices``; total for hostile values."""
    try:
        return value in choices
    except Exception:  # pragma: no cover - uncomparable value
        return False


def _str_tuple(value: Any, *, sort: bool = False) -> tuple[str, ...]:
    """Return ``value`` as a de-duplicated tuple of non-empty strings.

    Order is the first-occurrence order of the input (so a marker sequence keeps
    its emission order) unless ``sort`` is set, which yields the deterministic
    sorted spelling used for resolution provenance.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raw: list[Any] = [value]
    elif isinstance(value, Mapping):
        raw = list(value.keys())
    else:
        try:
            raw = list(value)
        except Exception:  # pragma: no cover - hostile iterable
            return ()
    out: list[str] = []
    for item in raw:
        entry = item if isinstance(item, str) else _text(item)
        if entry and entry not in out:
            out.append(entry)
    return tuple(sorted(out)) if sort else tuple(out)


def _color_map(value: Any) -> dict[str, str]:
    """Return ``value`` as a ``str -> str`` mapping in sorted key order (Req 11.4)."""
    if not isinstance(value, Mapping):
        return {}
    record: dict[str, str] = {}
    try:
        items = list(value.items())
    except Exception:  # pragma: no cover - hostile mapping
        return {}
    for raw_key, raw_value in items:
        key = raw_key if isinstance(raw_key, str) else _text(raw_key)
        record[key] = raw_value if isinstance(raw_value, str) else _text(raw_value)
    return {key: record[key] for key in sorted(record)}


def _get(data: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Mapping lookup that never raises, whatever ``data`` does."""
    try:
        if key in data:
            return data[key]
    except Exception:  # pragma: no cover - hostile mapping
        return default
    return default


def _read(options: Any, *names: str, default: Any = None) -> Any:
    """First non-``None`` attribute of ``options`` among ``names``.

    Attributes only — never a write, never a mutation, so the caller's
    Processing_Options instance is provably unmodified (Reqs 1.3, 10.9). The
    ``ProcessingOptions`` spelling is listed first and the already-resolved
    ``Kinetic_Options`` spelling second, which is what makes re-resolving an
    already-resolved value the identity (Req 10.8).
    """
    for name in names:
        value = getattr(options, name, None)
        if value is not None:
            return value
    return default


# ---------------------------------------------------------------------------
# Kinetic_Options (tasks 3.2, 3.3) — Reqs 10.1-10.10
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Kinetic_Options:
    """Resolved kinetic settings: JSON-serialisable scalars only (Req 10.1).

    Satisfies the foundation ``Engine_Options`` protocol through :meth:`parse`
    and :meth:`to_dict`. ``__post_init__`` coerces **every** field through a
    foundation ``coerce_*`` helper with its documented default and bounds, so
    construction is total (no input raises) and coercing an already-valid value
    is the identity (Reqs 10.5, 10.8).
    """

    # --- motion vocabulary (Req 10.2) ---
    style: str = DEFAULT_STYLE            # one of KINETIC_STYLES
    reveal: str = DEFAULT_REVEAL          # one of REVEAL_MODES
    # --- look, inherited from the Base_Preset (Reqs 10.2, 10.4) ---
    preset_name: str = caption_presets.FALLBACK_PRESET_NAME
    font_override: str = ""               # "" => use preset_font
    preset_font: str = FALLBACK_FONT      # resolved from the Base_Preset
    font_size: int = 84
    position: str = ""                    # "" => Base_Preset.position (Req 7.4)
    # --- layout (Reqs 7.2, 7.5, 7.6) ---
    max_lines: int = 2                    # 1..4
    max_line_width: int = 22              # Display_Width units, 6..80
    safe_area_x_pct: float = 6.0          # 0..25
    safe_area_y_pct: float = 10.0         # 0..40
    # --- motion + emphasis (Reqs 5.9, 6.5, 8.6, 10.2) ---
    motion_duration_ms: int = 120         # 20..1000
    highlight_keywords: bool = False
    keyword_ai: bool = False
    emoji_inline: bool = False
    confidence_floor: float = 0.0         # 0.0..1.0
    # --- carried context (Reqs 3.3, 3.4, 12.2) ---
    captions_enabled: bool = True
    hook_enabled: bool = False
    hook_duration_s: float = 2.5
    hook_font_size: int = 110
    durable_subtitle: bool = False
    permissibility: bool = False
    # --- resolution provenance (Req 4.8) ---
    notes: tuple[str, ...] = ()           # e.g. "style_substituted"

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "style", coerce_choice(self.style, KINETIC_STYLES, DEFAULT_STYLE))
        set_(self, "reveal", coerce_choice(self.reveal, REVEAL_MODES, DEFAULT_REVEAL))
        set_(
            self,
            "preset_name",
            coerce_str(self.preset_name, caption_presets.FALLBACK_PRESET_NAME, 128),
        )
        set_(self, "font_override", coerce_str(self.font_override, "", 128))
        set_(self, "preset_font", coerce_str(self.preset_font, FALLBACK_FONT, 128))
        set_(self, "font_size", coerce_int(self.font_size, 84, lo=8, hi=400))
        set_(self, "position", coerce_choice(self.position, _POSITION_CHOICES, ""))
        set_(self, "max_lines", coerce_int(self.max_lines, 2, lo=1, hi=4))
        set_(self, "max_line_width", coerce_int(self.max_line_width, 22, lo=6, hi=80))
        set_(
            self,
            "safe_area_x_pct",
            coerce_float(self.safe_area_x_pct, 6.0, lo=0.0, hi=25.0),
        )
        set_(
            self,
            "safe_area_y_pct",
            coerce_float(self.safe_area_y_pct, 10.0, lo=0.0, hi=40.0),
        )
        set_(
            self,
            "motion_duration_ms",
            coerce_int(self.motion_duration_ms, 120, lo=20, hi=1000),
        )
        set_(self, "highlight_keywords", coerce_bool(self.highlight_keywords, False))
        set_(self, "keyword_ai", coerce_bool(self.keyword_ai, False))
        set_(self, "emoji_inline", coerce_bool(self.emoji_inline, False))
        set_(
            self,
            "confidence_floor",
            coerce_float(self.confidence_floor, 0.0, lo=0.0, hi=1.0),
        )
        set_(self, "captions_enabled", coerce_bool(self.captions_enabled, True))
        set_(self, "hook_enabled", coerce_bool(self.hook_enabled, False))
        set_(
            self,
            "hook_duration_s",
            coerce_float(self.hook_duration_s, 2.5, lo=0.0, hi=30.0),
        )
        set_(self, "hook_font_size", coerce_int(self.hook_font_size, 110, lo=8, hi=400))
        set_(self, "durable_subtitle", coerce_bool(self.durable_subtitle, False))
        set_(self, "permissibility", coerce_bool(self.permissibility, False))
        set_(self, "notes", _str_tuple(self.notes, sort=True))

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a flat, JSON-native mapping in sorted key order (Req 10.7)."""
        record: dict[str, Any] = {
            "captions_enabled": bool(self.captions_enabled),
            "confidence_floor": float(self.confidence_floor),
            "durable_subtitle": bool(self.durable_subtitle),
            "emoji_inline": bool(self.emoji_inline),
            "font_override": self.font_override,
            "font_size": int(self.font_size),
            "highlight_keywords": bool(self.highlight_keywords),
            "hook_duration_s": float(self.hook_duration_s),
            "hook_enabled": bool(self.hook_enabled),
            "hook_font_size": int(self.hook_font_size),
            "keyword_ai": bool(self.keyword_ai),
            "max_line_width": int(self.max_line_width),
            "max_lines": int(self.max_lines),
            "motion_duration_ms": int(self.motion_duration_ms),
            "notes": list(self.notes),
            "permissibility": bool(self.permissibility),
            "position": self.position,
            "preset_font": self.preset_font,
            "preset_name": self.preset_name,
            "reveal": self.reveal,
            "safe_area_x_pct": float(self.safe_area_x_pct),
            "safe_area_y_pct": float(self.safe_area_y_pct),
            "style": self.style,
        }
        return {key: record[key] for key in sorted(record)}

    @classmethod
    def parse(cls, data: Mapping[str, Any] | None) -> "Kinetic_Options":
        """Total parser: never raises, ignores unknown keys (Reqs 10.5, 10.6).

        Named keys only — a mapping carrying keys that are not fields simply has
        them ignored — and every value present is coerced by ``__post_init__``
        with the field's documented default and bounds, so *any* input yields a
        usable value.
        """
        if not isinstance(data, Mapping):
            return cls()
        kwargs: dict[str, Any] = {}
        for entry in dataclasses.fields(cls):
            try:
                if entry.name in data:
                    kwargs[entry.name] = data[entry.name]
            except Exception:  # pragma: no cover - hostile mapping
                continue
        try:
            return cls(**kwargs)
        except Exception:  # pragma: no cover - defensive: coercion is total
            return cls()

    # -- projection from ProcessingOptions ---------------------------------

    @classmethod
    def from_processing_options(cls, options: Any) -> "Kinetic_Options":
        """Project Processing_Options onto Kinetic_Options (Reqs 10.3, 10.4, 10.8-10.10).

        Reads attributes only (never writes), inherits the Base_Preset look
        through ``caption_presets.resolve_preset`` — which returns
        ``(preset, substituted)`` — and records ``"style_substituted"`` /
        ``"position_substituted"`` in :attr:`notes` when ``coerce_choice`` fell
        back. Enablement (``captions``, ``hook_title``, ``caption_*``) is read
        from options already normalised by ``worker.models.effective_options``
        (Req 10.10).

        Idempotent: each field is read from the Processing_Options spelling first
        and the resolved Kinetic_Options spelling second, coercion of an
        already-valid value is the identity, and existing notes are carried
        forward — so resolving a resolved value returns an equal value.
        """
        preset, _substituted = caption_presets.resolve_preset(
            _read(options, "caption_preset", "preset_name")
        )

        raw_style = _read(options, "kinetic_style", "style", default=DEFAULT_STYLE)
        raw_reveal = _read(options, "kinetic_reveal", "reveal", default=DEFAULT_REVEAL)
        raw_position = _read(options, "caption_position", "position", default="")

        notes = list(_str_tuple(_read(options, "notes", default=()), sort=True))
        if not _is_member(raw_style, KINETIC_STYLES):
            notes.append("style_substituted")
        if not _is_member(raw_position, _POSITION_CHOICES):
            notes.append("position_substituted")

        # The engine never redefines the look: font, size, position, colours and
        # border style all come from the Base_Preset (Req 10.4).
        highlight = bool(
            coerce_bool(
                _read(options, "caption_keyword_highlight", "highlight_keywords"),
                False,
            )
            and preset.highlight_keywords
        )
        emoji = bool(
            coerce_bool(_read(options, "caption_emoji", "emoji_inline"), False)
            and preset.emoji_inline
        )

        return cls(
            style=coerce_choice(raw_style, KINETIC_STYLES, DEFAULT_STYLE),
            reveal=coerce_choice(raw_reveal, REVEAL_MODES, DEFAULT_REVEAL),
            preset_name=preset.name,
            font_override=coerce_str(
                _read(options, "kinetic_font", "font_override", default=""), "", 128
            ),
            preset_font=coerce_str(preset.font, FALLBACK_FONT, 128),
            font_size=coerce_int(preset.font_size, 84, lo=8, hi=400),
            position=coerce_choice(raw_position, _POSITION_CHOICES, ""),
            max_lines=coerce_int(
                _read(options, "kinetic_max_lines", "max_lines"), 2, lo=1, hi=4
            ),
            max_line_width=coerce_int(
                _read(options, "kinetic_max_line_width", "max_line_width"),
                22,
                lo=6,
                hi=80,
            ),
            safe_area_x_pct=coerce_float(
                _read(options, "kinetic_safe_area_x_pct", "safe_area_x_pct"),
                6.0,
                lo=0.0,
                hi=25.0,
            ),
            safe_area_y_pct=coerce_float(
                _read(options, "kinetic_safe_area_y_pct", "safe_area_y_pct"),
                10.0,
                lo=0.0,
                hi=40.0,
            ),
            motion_duration_ms=coerce_int(
                _read(options, "kinetic_motion_ms", "motion_duration_ms"),
                120,
                lo=20,
                hi=1000,
            ),
            highlight_keywords=highlight,
            keyword_ai=coerce_bool(
                _read(options, "caption_keyword_ai", "keyword_ai"), False
            ),
            emoji_inline=emoji,
            confidence_floor=coerce_float(
                _read(options, "kinetic_confidence_floor", "confidence_floor"),
                0.0,
                lo=0.0,
                hi=1.0,
            ),
            captions_enabled=coerce_bool(
                _read(options, "captions", "captions_enabled", default=True), True
            ),
            hook_enabled=coerce_bool(
                _read(options, "hook_title", "hook_enabled"), False
            ),
            hook_duration_s=coerce_float(
                _read(options, "hook_duration", "hook_duration_s"),
                2.5,
                lo=0.0,
                hi=30.0,
            ),
            hook_font_size=coerce_int(
                _read(options, "hook_font_size"), 110, lo=8, hi=400
            ),
            durable_subtitle=coerce_bool(_read(options, "durable_subtitle"), False),
            permissibility=coerce_bool(
                _read(options, "permissibility_mode", "permissibility"), False
            ),
            notes=tuple(notes),
        )


# ---------------------------------------------------------------------------
# Plan value records (task 3.4) — Reqs 11.2, 11.4, 11.10
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Kinetic_Word:
    """One planned word: escaped text, snapped bounds, and motion metadata."""

    text: str = ""                      # already ``captions._escape``-d (Req 4.7)
    start: float = 0.0                  # clip-relative seconds, snapped
    end: float = 0.0                    # >= start
    rel_ms: int = 0                     # motion offset from its cue start (Req 5.3)
    emphasis: bool = False              # Reqs 5.9, 6.5
    timing_synthesised: bool = False    # Req 6.1
    emoji: str = ""                     # inline glyph or "" (Reqs 8.6, 8.7)
    line: int = 0                       # Text_Line index within the cue (Req 7.5)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "text", coerce_str(self.text, "", _TEXT_LIMIT))
        start = coerce_float(self.start, 0.0, lo=0.0)
        set_(self, "start", start)
        set_(self, "end", max(start, coerce_float(self.end, start, lo=0.0)))
        set_(self, "rel_ms", coerce_int(self.rel_ms, 0, lo=0))
        set_(self, "emphasis", coerce_bool(self.emphasis, False))
        set_(self, "timing_synthesised", coerce_bool(self.timing_synthesised, False))
        set_(self, "emoji", coerce_str(self.emoji, "", 64))
        set_(self, "line", coerce_int(self.line, 0, lo=0))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-native mapping in sorted key order."""
        record: dict[str, Any] = {
            "emoji": self.emoji,
            "emphasis": bool(self.emphasis),
            "end": float(self.end),
            "line": int(self.line),
            "rel_ms": int(self.rel_ms),
            "start": float(self.start),
            "text": self.text,
            "timing_synthesised": bool(self.timing_synthesised),
        }
        return {key: record[key] for key in sorted(record)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Kinetic_Word":
        """Rebuild from :meth:`to_dict` output, tolerating missing/hostile fields."""
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            text=_get(data, "text", ""),
            start=_get(data, "start", 0.0),
            end=_get(data, "end", 0.0),
            rel_ms=_get(data, "rel_ms", 0),
            emphasis=_get(data, "emphasis", False),
            timing_synthesised=_get(data, "timing_synthesised", False),
            emoji=_get(data, "emoji", ""),
            line=_get(data, "line", 0),
        )


@dataclass(frozen=True)
class Kinetic_Cue:
    """One on-screen cue: a snapped Timeline_Segment plus its packed Text_Lines."""

    segment: Timeline_Segment = field(
        default_factory=lambda: Timeline_Segment(0.0, 0.0)
    )
    words: tuple[Kinetic_Word, ...] = ()
    lines: tuple[tuple[int, ...], ...] = ()   # word indices per Text_Line (Req 7.5)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        segment = self.segment
        if not isinstance(segment, Timeline_Segment):
            segment = (
                Timeline_Segment.from_dict(segment)
                if isinstance(segment, Mapping)
                else None
            ) or Timeline_Segment(0.0, 0.0)
        set_(self, "segment", segment)

        words: list[Kinetic_Word] = []
        try:
            raw_words = list(self.words)
        except Exception:  # pragma: no cover - hostile iterable
            raw_words = []
        for item in raw_words:
            if isinstance(item, Kinetic_Word):
                words.append(item)
            elif isinstance(item, Mapping):
                words.append(Kinetic_Word.from_dict(item))
        set_(self, "words", tuple(words))

        lines: list[tuple[int, ...]] = []
        try:
            raw_lines = list(self.lines)
        except Exception:  # pragma: no cover - hostile iterable
            raw_lines = []
        for entry in raw_lines:
            if isinstance(entry, (str, bytes, Mapping)):
                continue
            try:
                indices = list(entry)
            except Exception:  # pragma: no cover - hostile iterable
                continue
            lines.append(tuple(coerce_int(index, 0, lo=0) for index in indices))
        set_(self, "lines", tuple(lines))

    @property
    def start(self) -> float:
        """Cue start in clip-relative seconds."""
        return float(self.segment.start)

    @property
    def end(self) -> float:
        """Cue end in clip-relative seconds."""
        return float(self.segment.end)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-native mapping in sorted key order."""
        record: dict[str, Any] = {
            "lines": [list(entry) for entry in self.lines],
            "segment": self.segment.to_dict(),
            "words": [word.to_dict() for word in self.words],
        }
        return {key: record[key] for key in sorted(record)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Kinetic_Cue":
        """Rebuild from :meth:`to_dict` output, tolerating missing/hostile fields."""
        if not isinstance(data, Mapping):
            return cls()
        raw_words = _get(data, "words", ())
        try:
            words = tuple(
                Kinetic_Word.from_dict(item) if isinstance(item, Mapping) else item
                for item in raw_words
            )
        except Exception:  # pragma: no cover - hostile iterable
            words = ()
        return cls(
            segment=_get(data, "segment", None),
            words=words,
            lines=_get(data, "lines", ()),
        )


@dataclass(frozen=True)
class Kinetic_Plan:
    """The complete, JSON-serialisable plan the pure emitter turns into ASS.

    Produced by the planner (task 6) and consumed by ``emit_ass`` (task 8);
    :meth:`to_dict` / :meth:`from_dict` round-trip it so ``plan(ctx)`` can return
    a JSON-serialisable mapping (Reqs 11.2, 11.10).
    """

    style: str
    reveal: str
    font: str                    # the resolved ladder rung (Req 9.7)
    font_size: int
    position: str                # bottom | center | top
    align: int                   # 2 | 5 | 8 (Req 7.3)
    play_res_x: int              # Req 7.1
    play_res_y: int              # Req 7.1
    margin_l: int
    margin_r: int
    margin_v: int                # Safe_Area (Reqs 7.2, 7.10)
    duration: float
    style_line: str              # Style: Default, from the Base_Preset (Req 10.4)
    hook_style: str              # Style: Hook, verbatim shape (Req 3.3)
    hook_text: str = ""
    hook_duration_s: float = 2.5
    cues: tuple[Kinetic_Cue, ...] = ()
    cue_level: bool = False      # Req 6.4
    degraded: bool = False
    markers: tuple[str, ...] = ()
    detail: str = ""
    colors: Mapping[str, str] = field(default_factory=dict)   # primary/highlight
    highlight_scale: int = 118                                # percent, Req 5.9

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "style", coerce_choice(self.style, KINETIC_STYLES, DEFAULT_STYLE))
        set_(self, "reveal", coerce_choice(self.reveal, REVEAL_MODES, DEFAULT_REVEAL))
        set_(self, "font", coerce_str(self.font, FALLBACK_FONT, 128))
        set_(self, "font_size", coerce_int(self.font_size, 84, lo=8, hi=400))
        set_(self, "position", coerce_choice(self.position, POSITIONS, "bottom"))
        align = coerce_int(self.align, 2)
        set_(self, "align", align if align in (2, 5, 8) else 2)
        set_(self, "play_res_x", coerce_int(self.play_res_x, 1080, lo=1))
        set_(self, "play_res_y", coerce_int(self.play_res_y, 1920, lo=1))
        set_(self, "margin_l", coerce_int(self.margin_l, 0, lo=0))
        set_(self, "margin_r", coerce_int(self.margin_r, 0, lo=0))
        set_(self, "margin_v", coerce_int(self.margin_v, 0, lo=0))
        set_(self, "duration", coerce_float(self.duration, 0.0, lo=0.0))
        set_(self, "style_line", coerce_str(self.style_line, "", _TEXT_LIMIT))
        set_(self, "hook_style", coerce_str(self.hook_style, "", _TEXT_LIMIT))
        set_(self, "hook_text", coerce_str(self.hook_text, "", _TEXT_LIMIT))
        set_(
            self,
            "hook_duration_s",
            coerce_float(self.hook_duration_s, 2.5, lo=0.0, hi=30.0),
        )
        cues: list[Kinetic_Cue] = []
        try:
            raw_cues = list(self.cues)
        except Exception:  # pragma: no cover - hostile iterable
            raw_cues = []
        for item in raw_cues:
            if isinstance(item, Kinetic_Cue):
                cues.append(item)
            elif isinstance(item, Mapping):
                cues.append(Kinetic_Cue.from_dict(item))
        set_(self, "cues", tuple(cues))
        set_(self, "cue_level", coerce_bool(self.cue_level, False))
        set_(self, "degraded", coerce_bool(self.degraded, False))
        set_(self, "markers", _str_tuple(self.markers))
        set_(self, "detail", coerce_str(self.detail, "", _TEXT_LIMIT))
        set_(self, "colors", _color_map(self.colors))
        set_(self, "highlight_scale", coerce_int(self.highlight_scale, 118, lo=1, hi=1000))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-native mapping in sorted key order (Req 11.2)."""
        record: dict[str, Any] = {
            "align": int(self.align),
            "colors": dict(sorted(self.colors.items())),
            "cue_level": bool(self.cue_level),
            "cues": [cue.to_dict() for cue in self.cues],
            "degraded": bool(self.degraded),
            "detail": self.detail,
            "duration": float(self.duration),
            "font": self.font,
            "font_size": int(self.font_size),
            "highlight_scale": int(self.highlight_scale),
            "hook_duration_s": float(self.hook_duration_s),
            "hook_style": self.hook_style,
            "hook_text": self.hook_text,
            "margin_l": int(self.margin_l),
            "margin_r": int(self.margin_r),
            "margin_v": int(self.margin_v),
            "play_res_x": int(self.play_res_x),
            "play_res_y": int(self.play_res_y),
            "position": self.position,
            "reveal": self.reveal,
            "style": self.style,
            "style_line": self.style_line,
        }
        record["markers"] = list(self.markers)
        return {key: record[key] for key in sorted(record)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Kinetic_Plan":
        """Rebuild an equivalent plan from :meth:`to_dict` output (Req 11.10)."""
        if not isinstance(data, Mapping):
            data = {}
        raw_cues = _get(data, "cues", ())
        try:
            cues = tuple(
                Kinetic_Cue.from_dict(item) if isinstance(item, Mapping) else item
                for item in raw_cues
            )
        except Exception:  # pragma: no cover - hostile iterable
            cues = ()
        return cls(
            style=_get(data, "style", DEFAULT_STYLE),
            reveal=_get(data, "reveal", DEFAULT_REVEAL),
            font=_get(data, "font", FALLBACK_FONT),
            font_size=_get(data, "font_size", 84),
            position=_get(data, "position", "bottom"),
            align=_get(data, "align", 2),
            play_res_x=_get(data, "play_res_x", 1080),
            play_res_y=_get(data, "play_res_y", 1920),
            margin_l=_get(data, "margin_l", 0),
            margin_r=_get(data, "margin_r", 0),
            margin_v=_get(data, "margin_v", 0),
            duration=_get(data, "duration", 0.0),
            style_line=_get(data, "style_line", ""),
            hook_style=_get(data, "hook_style", ""),
            hook_text=_get(data, "hook_text", ""),
            hook_duration_s=_get(data, "hook_duration_s", 2.5),
            cues=cues,
            cue_level=_get(data, "cue_level", False),
            degraded=_get(data, "degraded", False),
            markers=_get(data, "markers", ()),
            detail=_get(data, "detail", ""),
            colors=_get(data, "colors", {}),
            highlight_scale=_get(data, "highlight_scale", 118),
        )


# ---------------------------------------------------------------------------
# Pure layout helpers and Safe_Area geometry (tasks 5.1-5.3)
# ---------------------------------------------------------------------------
#
# Everything below is a pure function of its arguments: no clock, no locale, no
# filesystem, no probe, no ffmpeg (Reqs 11.5, 18.2). The single external symbol
# they reach for is ``worker.captions._POSITION_ALIGN`` — *reused*, never
# re-spelled, so the alignment/MarginV table can never drift from the v0.8.0
# caption path (Req 7.3). It is reached through the lazy :func:`_captions`
# accessor because :mod:`worker.captions` imports ``config`` — and therefore
# ``pydantic`` — which module-scope import safety forbids (Reqs 1.4, 18.2).


def _captions() -> Any:
    """Lazily import and return :mod:`worker.captions` (Reqs 1.4, 18.2)."""
    from worker import captions

    return captions


#: Code-point ranges of scripts written without inter-word spaces (Reqs 8.2, 8.4):
#: Han (incl. CJK radicals, compatibility and extension B+), Hiragana, Katakana
#: (incl. halfwidth), and Hangul (Jamo, compatibility Jamo, extended, syllables).
_SPACE_FREE_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),    # CJK radicals supplement
    (0x3005, 0x3007),    # ideographic iteration mark, ditto, ideographic zero
    (0x3040, 0x30FF),    # Hiragana + Katakana
    (0x3100, 0x312F),    # Bopomofo
    (0x3130, 0x318F),    # Hangul compatibility Jamo
    (0x31A0, 0x31BF),    # Bopomofo extended
    (0x31F0, 0x31FF),    # Katakana phonetic extensions
    (0x3400, 0x4DBF),    # CJK unified ideographs extension A
    (0x4E00, 0x9FFF),    # CJK unified ideographs
    (0xA960, 0xA97F),    # Hangul Jamo extended-A
    (0xAC00, 0xD7FF),    # Hangul syllables + Jamo extended-B
    (0xF900, 0xFAFF),    # CJK compatibility ideographs
    (0xFF66, 0xFF9D),    # halfwidth Katakana
    (0x1100, 0x11FF),    # Hangul Jamo
    (0x20000, 0x3FFFF),  # CJK unified ideographs extensions B..
)

#: ``unicodedata.category`` values that occupy no advance width (Req 8.9).
_ZERO_WIDTH_CATEGORIES = frozenset({"Mn", "Me", "Cf"})


def display_width(text: Any) -> int:
    """Return the Display_Width of ``text`` in layout units (Reqs 8.1, 8.9).

    East Asian ``F`` (fullwidth) and ``W`` (wide) characters cost 2 units, every
    other visible character costs 1, and combining marks (categories ``Mn`` /
    ``Me``) plus zero-width format characters (``Cf``) cost 0 so a *decomposed*
    grapheme is not counted twice. Total: a non-string argument is rendered with
    ``str`` rather than raising.
    """
    total = 0
    for char in _text(text):
        if unicodedata.category(char) in _ZERO_WIDTH_CATEGORIES:
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
    return total


def is_space_free(text: Any) -> bool:
    """True when ``text`` belongs to a script written without inter-word spaces.

    Decided **per word** from its first non-combining code point (Reqs 8.2, 8.4),
    so a Han/Hiragana/Katakana/Hangul token joins its neighbour with no inserted
    space while Latin, Cyrillic, Arabic and Hebrew tokens take one. Empty or
    combining-only text is not space-free (it takes the default Latin join).
    """
    for char in _text(text):
        if unicodedata.category(char) in _ZERO_WIDTH_CATEGORIES:
            continue
        code = ord(char)
        for lo, hi in _SPACE_FREE_RANGES:
            if lo <= code <= hi:
                return True
        return False
    return False


def join_separator(previous: Any, following: Any) -> str:
    """The separator between two neighbouring words in one Text_Line (Req 8.4).

    ``""`` when **both** neighbours are space-free-script words, otherwise a
    single space. Shared by :func:`pack_lines` (as the join *cost*) and by the
    emitter's Text_Line assembly, so measured width and emitted text can never
    disagree.
    """
    return "" if is_space_free(previous) and is_space_free(following) else " "


def join_width(previous: Any, following: Any) -> int:
    """Display_Width cost of :func:`join_separator` — 1 for a space, else 0."""
    return display_width(join_separator(previous, following))


def _word_text(word: Any) -> str:
    """The text of a :class:`Kinetic_Word`, or of a bare string. Total."""
    if isinstance(word, str):
        return word
    return _text(getattr(word, "text", ""))


def pack_lines(
    words: Any,
    max_lines: Any = 2,
    max_width: Any = 22,
) -> tuple[list[list[int]], list[int]]:
    """Greedily pack ``words`` into Text_Lines of word indices (Reqs 7.5-7.8, 8.5).

    Returns ``(lines, overflow)`` where ``lines`` holds at most ``max_lines``
    lists of indices into ``words`` and ``overflow`` is the tail of indices that
    did not fit — which the planner re-splits into a further Kinetic_Cue with a
    proportionally divided interval (Req 7.7).

    Packing is left-to-right and never reorders, so right-to-left text stays in
    Word_Timeline order (Req 8.3). Every word stays intact inside exactly one
    Text_Line — no word is ever split across a ``\\N`` break (Req 7.8) — and a
    word whose own Display_Width exceeds ``max_width`` is placed alone on its
    line rather than broken (Req 8.5).
    """
    try:
        items = list(words)
    except Exception:  # pragma: no cover - hostile iterable
        return [], []
    limit_lines = coerce_int(max_lines, 2, lo=1, hi=4)
    limit_width = coerce_int(max_width, 22, lo=1)

    lines: list[list[int]] = []
    current: list[int] = []
    current_width = 0
    previous_text = ""

    for index, item in enumerate(items):
        text = _word_text(item)
        width = display_width(text)
        if not current:
            current = [index]
            current_width = width
            previous_text = text
            continue
        gap = join_width(previous_text, text)
        if current_width + gap + width <= limit_width:
            current.append(index)
            current_width += gap + width
            previous_text = text
            continue
        # The word does not fit on the current Text_Line: start a new one, or
        # hand the remaining tail back for re-splitting (Req 7.7).
        lines.append(current)
        if len(lines) >= limit_lines:
            return lines, list(range(index, len(items)))
        current = [index]
        current_width = width
        previous_text = text

    if current:
        lines.append(current)
    return lines, []


def resolve_position(position: Any, preset_position: Any = "") -> str:
    """Resolve a caption position, inheriting the Base_Preset value (Req 7.4).

    An **empty** (or unrecognised) ``Kinetic_Options.position`` means "use the
    Base_Preset ``position``", so a preset such as ``hormozi`` — which declares
    ``center`` — is not silently rendered at the ``bottom`` default. When neither
    value is a member of :data:`POSITIONS`, ``"bottom"`` is used.
    """
    resolved = coerce_choice(position, POSITIONS, "")
    if resolved:
        return resolved
    return coerce_choice(preset_position, POSITIONS, "bottom")


def position_align(position: Any, preset_position: Any = "") -> tuple[int, int]:
    """Return ``(alignment, default_margin_v)`` for a caption position (Req 7.3).

    Reads :data:`worker.captions._POSITION_ALIGN` — the v0.8.0 table mapping
    ``bottom``/``center``/``top`` to ASS alignments ``2``/``5``/``8`` with their
    default ``MarginV`` values — rather than restating it, so the kinetic engine
    and the existing caption path can never disagree. The position is resolved
    through :func:`resolve_position` first, so ``""`` inherits the preset.
    """
    table = _captions()._POSITION_ALIGN
    resolved = resolve_position(position, preset_position)
    align, margin_v = table.get(resolved, table["bottom"])
    return int(align), int(margin_v)


def safe_area_margins(
    play_res_x: Any = 1080,
    play_res_y: Any = 1920,
    *,
    safe_area_x_pct: Any = 6.0,
    safe_area_y_pct: Any = 10.0,
    position: Any = "",
    preset_position: Any = "",
) -> tuple[int, int, int, int]:
    """Return ``(align, margin_l, margin_r, margin_v)`` for the emitted style.

    Reqs 7.2, 7.3, 7.4, 7.10 — the Safe_Area insets are percentages of the
    probed clip size::

        margin_l = margin_r = round(play_res_x * safe_area_x_pct / 100)
        margin_v = max(default_margin_v, round(play_res_y * safe_area_y_pct / 100))

    ``max(...)`` keeps the text box inside the Safe_Area rectangle while
    preserving the v0.8.0 vertical placement whenever the inset is smaller than
    the preset default (``bottom`` -> 220, ``top`` -> 200); ``center`` has a
    default of ``0``, so it keeps its "libass centres vertically" semantics and
    its safe-area obligation is met by the horizontal insets alone.

    Both results are finally clamped so ``margin_l + margin_r < play_res_x`` and
    ``2 * margin_v < play_res_y`` even for a degenerately small probed size —
    the caption box always has room to exist.
    """
    width = coerce_int(play_res_x, 1080, lo=1)
    height = coerce_int(play_res_y, 1920, lo=1)
    pct_x = coerce_float(safe_area_x_pct, 0.0, lo=0.0, hi=100.0)
    pct_y = coerce_float(safe_area_y_pct, 0.0, lo=0.0, hi=100.0)

    align, default_margin_v = position_align(position, preset_position)

    inset_x = int(round(width * pct_x / 100.0))
    inset_y = int(round(height * pct_y / 100.0))

    margin_x = min(inset_x, max(0, (width - 1) // 2))
    margin_v = min(max(default_margin_v, inset_y), max(0, (height - 1) // 2))

    return align, margin_x, margin_x, margin_v
