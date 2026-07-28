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
import math
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from worker.effects import caption_presets
from worker.engines import registry as engine_registry
from worker.engines.base import (
    AV_Engine,
    Compose_Contribution,
    Engine_Context,
    Engine_Result,
    Engine_Stage,
    Engine_Status,
    coerce_bool,
    coerce_choice,
    coerce_float,
    coerce_int,
    coerce_str,
    marker,
)
from worker.engines.timebase import Time_Base, Timeline_Segment, normalize_segments

__all__ = [
    "ASS_NAME",
    "BOUNCE_OVERSHOOT",
    "CUE_FADE_MS",
    "DEFAULT_PLAY_RES",
    "DEFAULT_REVEAL",
    "DEFAULT_STYLE",
    "ENGINE_ID",
    "FALLBACK_FONT",
    "KINETIC_STYLES",
    "KINETIC_Z_ORDER",
    "MIN_WORD_S",
    "POSITIONS",
    "REVEAL_MODES",
    "SLIDE_UP_PX",
    "SUBTITLES_CAPABILITY",
    "SYNTHESISED_RATIO_LIMIT",
    "Kinetic_Cue",
    "Kinetic_Options",
    "Kinetic_Plan",
    "Kinetic_Typography_Engine",
    "Kinetic_Word",
    "display_width",
    "emit_ass",
    "is_space_free",
    "join_separator",
    "join_width",
    "pack_lines",
    "plan_kinetic",
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

#: Req 1.1 — the engine identifier, and therefore the ``engine:<id>:<detail>``
#: marker namespace the *planner* already needs (Reqs 4.8, 6.3, 14.4). The engine
#: class (task 9.1) declares ``engine_id = ENGINE_ID`` rather than a second
#: literal, so the two spellings cannot drift.
ENGINE_ID = "kinetic_typography"

#: Req 2.3 — Caption_Layer z-order band for the compose contribution.
KINETIC_Z_ORDER = 100

#: Req 16.3 — at most one ASS document per invocation, always this file name.
ASS_NAME = "kinetic.ass"

#: Req 1.5 / 13.1 — the single required Capability_Id: the engine hands libass an
#: ASS file through the compositor's existing ``subtitles`` filter slot, so
#: without that filter there is nothing for the contribution to plug into.
SUBTITLES_CAPABILITY = "ffmpeg_filter:subtitles"

#: Req 7.1 — ``PlayResX``/``PlayResY`` fallback: the pipeline's target vertical
#: frame. The engine may not probe the clip (``run`` creates no subprocess), so the
#: target size is read from the Clip_Metadata channel the Pipeline publishes at the
#: COMPOSE hook — ``ctx.clip_metadata["clip_size"]``, a ``(width, height)`` pair —
#: and falls back to this value whenever that key is absent or malformed.
DEFAULT_PLAY_RES: tuple[int, int] = (1080, 1920)

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

#: Slack, in **milliseconds**, allowed when quantising a second-valued timestamp
#: onto the whole-millisecond grid ``\t`` offsets live on (Req 5.3). One
#: nanosecond: far below anything a transcript or a frame grid can express, but
#: comfortably above the binary-float error of a frame-snapped subtraction (a
#: difference such as ``32/30 - 26/30`` evaluates to ``0.19999999999999996``, i.e.
#: ~4e-14 ms short of 200 ms), so a mathematically whole millisecond floors onto
#: itself rather than onto the millisecond below.
_MS_EPSILON = 1e-6


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
    # ``d`` in the design's span table (Reqs 4.4-4.6). Carried on the plan — not
    # threaded into the emitter as a second argument — so ``emit_ass(plan)``
    # depends on nothing but ``plan`` (design determinism rule 4, Req 11.5).
    motion_duration_ms: int = 120
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
        set_(
            self,
            "motion_duration_ms",
            coerce_int(self.motion_duration_ms, 120, lo=20, hi=1000),
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
            "motion_duration_ms": int(self.motion_duration_ms),
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
            motion_duration_ms=_get(data, "motion_duration_ms", 120),
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


# ---------------------------------------------------------------------------
# The pure planner — ``plan_kinetic`` (tasks 6.1-6.5) — Reqs 4.7-4.8, 5, 6, 7, 14.4
# ---------------------------------------------------------------------------
#
# ``plan_kinetic`` is a **pure function of its arguments**: no I/O, no ffmpeg, no
# clock, no subprocess, no network, no randomness (Reqs 11.1, 11.5, 15.6, 18.2).
# The three collaborators it needs from the impure world are *injected* — the
# resolved font family, the keyword planner, and the ``remaining`` budget reader —
# so the planner itself never probes anything. Everything it borrows from the
# v0.8.0 caption path (``_word_bounds``, ``words_to_cues``, ``_escape``,
# ``_preset_style_line``) is reached through the lazy :func:`_captions` accessor,
# which is what keeps module import free of ``pydantic`` (Reqs 1.4, 18.2).


@dataclass(frozen=True)
class _Source_Word:
    """A sanitised Word_Timeline entry (planner-internal, never serialised).

    Carries the **raw** (un-escaped) text and the Word_Confidence so it can be
    handed to ``caption_presets.plan_keywords`` and compared against
    ``Kinetic_Options.confidence_floor`` (Reqs 5.9, 6.5); ``captions.words_to_cues``
    consumes it duck-typed through ``.text`` / ``.start`` / ``.end``.
    """

    text: str = ""
    start: float = 0.0
    end: float = 0.0
    probability: float = 1.0
    synthesised: bool = False   # Req 6.1 — this word's interval was invented


def _is_finite_number(value: Any) -> bool:
    """True when ``value`` is a usable (finite, non-bool) numeric or numeric string."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        try:
            return math.isfinite(float(value))
        except Exception:  # pragma: no cover - exotic numeric type
            return False
    if isinstance(value, str):
        try:
            return math.isfinite(float(value.strip()))
        except Exception:
            return False
    return False


def _word_probability(word: Any) -> float:
    """Word_Confidence of ``word``, defaulting to ``1.0`` (Req 6.5). Total."""
    return coerce_float(getattr(word, "probability", None), 1.0, lo=0.0, hi=1.0)


def _sanitise_words(words: Any) -> list[_Source_Word]:
    """Planner step 1 — drop unusable words and coerce their bounds (Reqs 6.1, 6.6).

    Empty and whitespace-only words are dropped entirely, so the remaining words
    of their cue are retained (Req 6.6). Bounds are coerced exactly the way
    ``captions._word_bounds`` does — a missing or non-numeric value reads as
    ``0.0`` and an inverted pair collapses to ``end = start`` — and any word whose
    ``start``/``end`` was missing, non-numeric, inverted, or zero-length is
    flagged :attr:`_Source_Word.synthesised` so step 4 can invent its interval
    (Req 6.1).
    """
    captions = _captions()
    try:
        items = list(words) if words is not None else []
    except Exception:  # pragma: no cover - hostile iterable
        items = []

    out: list[_Source_Word] = []
    for item in items:
        text = _word_text(item).strip()
        if not text:
            continue                                   # Req 6.6
        raw_start, raw_end = captions._word_bounds(item)
        start = coerce_float(raw_start, 0.0, lo=0.0)   # rejects NaN/inf too
        end = coerce_float(raw_end, start, lo=0.0)
        if end < start:
            end = start
        attr_start = None if isinstance(item, str) else getattr(item, "start", None)
        attr_end = None if isinstance(item, str) else getattr(item, "end", None)
        synthesised = (
            not _is_finite_number(attr_start)
            or not _is_finite_number(attr_end)
            or end <= start
        )
        out.append(
            _Source_Word(
                text=text,
                start=start,
                end=end,
                probability=_word_probability(item),
                synthesised=synthesised,
            )
        )
    return out


def _finalise_lines(
    lines: list[list[int]], count: int, max_lines: int
) -> tuple[tuple[int, ...], ...]:
    """Return ``lines`` as a tuple, guaranteeing every word index appears once.

    ``pack_lines`` only leaves indices out when it reports an overflow tail, and
    the re-splitter consumes every tail — this is the belt-and-braces guard that
    keeps the plan self-consistent (every word in exactly one Text_Line, Req 7.8)
    even for a degenerate input that hit the re-split iteration cap.
    """
    placed = [index for line in lines for index in line]
    missing = [index for index in range(count) if index not in placed]
    packed = [list(line) for line in lines if line]
    if missing:
        if packed and len(packed) >= max(1, max_lines):
            packed[-1].extend(missing)
        else:
            packed.append(missing)
    return tuple(tuple(line) for line in packed)


def _split_drafts(
    cue_start: float,
    cue_end: float,
    items: list[_Source_Word],
    max_lines: int,
    max_width: int,
    time_base: Any,
) -> list[tuple[float, float, list[_Source_Word], tuple[tuple[int, ...], ...]]]:
    """Planner step 3 — lay out one cue, re-splitting it on overflow (Reqs 7.5-7.8).

    ``pack_lines`` packs the cue greedily into at most ``max_lines`` Text_Lines of
    at most ``max_width`` Display_Width. When words are left over the cue is split
    **at that word boundary** and the original interval is divided in proportion
    to the two halves' word-time spans::

        head_span = head[-1].end - head[0].start
        tail_span = tail[-1].end - tail[0].start
        ratio     = 0.5 if head_span + tail_span <= 0 else head_span / total
        boundary  = time_base.snap(start + (end - start) * ratio)

    A degenerate pair of spans (all-zero timings) splits the interval evenly. The
    boundary is snapped to the frame grid and clamped into ``[start, end]``, so the
    parts stay contiguous and their union is exactly the original interval
    (Req 7.7). The tail is re-examined in place, so a cue that overflows twice
    produces three contiguous parts in Word_Timeline order.
    """
    drafts: list[tuple[float, float, list[_Source_Word], tuple[tuple[int, ...], ...]]] = []
    pending: list[tuple[float, float, list[_Source_Word]]] = [
        (cue_start, cue_end, items)
    ]
    # Every split consumes at least one word, so the queue is bounded by the word
    # count; the cap is pure paranoia against a pathological packing.
    budget = 4 * len(items) + 16

    while pending and budget > 0:
        budget -= 1
        start, end, words = pending.pop(0)
        lines, overflow = pack_lines(words, max_lines, max_width)
        if not overflow or not lines or not lines[0]:
            drafts.append((start, end, words, _finalise_lines(lines, len(words), max_lines)))
            continue

        boundary_index = overflow[0]
        head = words[:boundary_index]
        tail = words[boundary_index:]
        if not head or not tail:  # pragma: no cover - pack_lines keeps >=1 word
            drafts.append((start, end, words, _finalise_lines(lines, len(words), max_lines)))
            continue

        head_span = head[-1].end - head[0].start
        tail_span = tail[-1].end - tail[0].start
        total = head_span + tail_span
        ratio = 0.5 if total <= 0.0 else head_span / total
        boundary = _snap(time_base, start + (end - start) * ratio)
        boundary = min(max(boundary, start), end)

        drafts.append(
            (start, boundary, head, _finalise_lines(lines, len(head), max_lines))
        )
        pending.insert(0, (boundary, end, tail))

    for start, end, words in pending:  # pragma: no cover - cap never reached
        lines, _overflow = pack_lines(words, max_lines, max_width)
        drafts.append((start, end, words, _finalise_lines(lines, len(words), max_lines)))
    return drafts


def _fill_timings(
    start: float, end: float, items: list[_Source_Word]
) -> list[_Source_Word]:
    """Planner step 4 — invent the flagged words' intervals (Reqs 6.1, 6.2).

    A word flagged in step 1 takes its share of the **cue span**, distributed
    evenly across the cue's words in Word_Timeline order; a word whose duration is
    still zero is widened to :data:`MIN_WORD_S`. Words with usable timings are
    left exactly as transcribed, so Req 5.9's "spoken timing unchanged" holds.
    """
    count = len(items)
    if not count:
        return []
    span = max(0.0, end - start)
    step = span / count
    filled: list[_Source_Word] = []
    for index, word in enumerate(items):
        word_start = word.start
        word_end = word.end
        if word.synthesised:
            word_start = start + step * index
            word_end = word_start + step
        if word_end - word_start <= 0.0:
            word_end = word_start + MIN_WORD_S       # Req 6.2
        filled.append(dataclasses.replace(word, start=word_start, end=word_end))
    return filled


def _snap(time_base: Any, seconds: float) -> float:
    """``time_base.snap`` guarded for a hostile/absent time base (Reqs 5.4, 16.2)."""
    try:
        return float(time_base.snap(seconds))
    except Exception:  # pragma: no cover - duck-typed time base without snap
        return float(seconds)


def _snap_floor(time_base: Any, seconds: float) -> float:
    """The greatest frame boundary **not after** ``seconds`` (Reqs 5.4, 5.6).

    ``Time_Base.snap`` rounds to the *nearest* boundary, so snapping the clip
    duration can land above it; the foundation's ``normalize_segments`` resolves
    that by clamping after snapping, which leaves the clip end un-snapped. The
    planner instead clamps to this floor, so every emitted bound is both inside
    ``[0, duration]`` **and** exactly on the frame grid — at the cost of at most
    the final fraction of a frame.
    """
    value = _snap(time_base, seconds)
    if value <= seconds:
        return max(0.0, value)
    frame = _frame_duration(time_base)
    lowered = _snap(time_base, seconds - frame / 2.0)
    return max(0.0, lowered if lowered <= seconds else seconds)


def _frame_duration(time_base: Any) -> float:
    """One frame in seconds, guarded for a duck-typed time base."""
    try:
        frame = float(time_base.frame_duration())
    except Exception:  # pragma: no cover - duck-typed time base
        frame = 0.0
    return frame if frame > 0.0 and math.isfinite(frame) else 1.0 / 30.0


def _covering_segment(
    segments: list[Timeline_Segment], start: float, end: float
) -> Timeline_Segment | None:
    """The normalised segment overlapping ``[start, end)``, or ``None`` if dropped.

    ``normalize_segments`` merges touching/overlapping cue intervals, so a cue
    survives normalisation when *some* canonical segment still covers part of its
    interval; a cue whose whole interval was dropped (out of bounds, or shorter
    than ``min_duration`` and not merged into a neighbour) has no covering
    segment and therefore drops its words with it.
    """
    for segment in segments:
        if segment.start < end and start < segment.end:
            return segment
        if segment.start <= start < segment.end:  # pragma: no cover - zero-length cue
            return segment
    return None


def _style_line(
    preset: Any,
    font: str,
    font_size: int,
    align: int,
    margin_l: int,
    margin_r: int,
    margin_v: int,
) -> str:
    """The ``Style: Default`` line: v0.8.0 look, Safe_Area margins (Reqs 7.2, 10.4).

    Every look field comes from ``captions._preset_style_line`` — *reused*, not
    re-spelled, so colours, border style and the karaoke-thickened outline/shadow
    can never drift from the existing caption path — and only the three margin
    columns are replaced with the Safe_Area values computed by
    :func:`safe_area_margins`.
    """
    base = _captions()._preset_style_line(preset, font, font_size, align, margin_v)
    fields = base.split(",")
    # Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,
    # OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX,
    # ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL,
    # MarginR, MarginV, Encoding  -> 23 fields, margins at indices 19..21.
    if len(fields) == 23:
        fields[19] = str(int(margin_l))
        fields[20] = str(int(margin_r))
        fields[21] = str(int(margin_v))
        return ",".join(fields)
    return base  # pragma: no cover - the shape is pinned by task 15.2


def _hook_style_line(font: str, hook_font_size: int) -> str:
    """The ``Style: Hook`` line, identical in shape to ``build_ass``'s (Req 3.3).

    ``captions`` keeps this definition as a literal inside ``build_ass`` /
    ``_preset_header_styles`` rather than a reusable helper, and
    ``_preset_header_styles`` cannot be called from a *pure* planner because it
    probes the host font list (``fc-list``, a subprocess). The numbers are
    therefore repeated verbatim here; task 8.10's property test asserts the two
    spellings stay identical.
    """
    return (
        f"Style: Hook,{font},{int(hook_font_size)},&H0000E5FF,&H0000E5FF,"
        f"&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,8,60,60,160,1"
    )


def plan_kinetic(
    words: Any,
    duration: Any,
    time_base: Any = None,
    opts: Any = None,
    font: Any = "",
    hook_text: Any = "",
    keyword_planner: Any = None,
    remaining: Any = None,
    *,
    play_res_x: Any = 1080,
    play_res_y: Any = 1920,
) -> Kinetic_Plan:
    """Turn a rebased Word_Timeline into a :class:`Kinetic_Plan` — **pure**.

    No I/O, no ffmpeg, no clock, no subprocess, no network, no randomness: the
    result depends only on the arguments (Reqs 11.1, 11.5, 11.7, 15.6, 18.2).

    Pipeline (design "The pure planner"):

    1. **Sanitise** — drop empty/whitespace-only words (Req 6.6) and coerce bounds
       like ``captions._word_bounds``, flagging invented intervals (Req 6.1).
    2. **Group** — ``captions.words_to_cues`` with its existing defaults, so cue
       grouping matches the v0.8.0 caption path exactly (Req 5.2).
    3. **Lay out and re-split** — :func:`pack_lines` per cue; overflow splits the
       cue at a word boundary and divides the interval in proportion to the two
       halves' word-time spans (Reqs 7.5-7.8).
    4. **Budget** — ``remaining()`` is consulted **exactly once**, between layout
       and normalisation; at ``<= 0`` planning stops with ``degraded`` and the
       ``degraded:budget`` marker (Req 14.4).
    5. **Fill, snap, normalise** — invented intervals get their share of the cue
       span, zero-length words widen to :data:`MIN_WORD_S` (Reqs 6.1, 6.2), every
       cue bound is snapped with ``time_base.snap`` and the cue list runs through
       ``normalize_segments(..., min_duration=MIN_WORD_S)``; cues dropped there
       drop their words too, survivors clamp their words into their own snapped
       bounds (Reqs 5.3-5.8, 16.2).
    6. **Emphasis** — the injected ``keyword_planner`` selects flat indices, which
       are only ever *membership-tested* against a positional index, never
       iterated, so ordering stays deterministic (Reqs 5.9, 11.4); a word below
       ``opts.confidence_floor`` loses its emphasis but keeps text and timing
       (Req 6.5).
    7. **Degrade** — over :data:`SYNTHESISED_RATIO_LIMIT` synthesised words the
       plan falls back to cue-level animation (Reqs 6.3, 6.4).

    Args:
        words: The clip-relative rebased Word_Timeline (Req 5.1).
        duration: Clip duration in seconds; every timestamp stays in ``[0, d]``.
        time_base: Foundation ``Time_Base`` used for ``snap`` (Reqs 5.4, 16.2).
        opts: The resolved :class:`Kinetic_Options` (a mapping is parsed).
        font: The already-resolved font ladder rung (Req 9.7) — injected, because
            probing the host font list is impure.
        hook_text: The hook title carried on ``ctx.clip_metadata["hook_text"]``
            (Req 3.3).
        keyword_planner: ``caption_presets.plan_keywords``-shaped callable (Req 18.1).
        remaining: ``Engine_Context.remaining``-shaped callable (Req 14.4).
        play_res_x: Probed clip width for ``PlayResX`` (Req 7.1).
        play_res_y: Probed clip height for ``PlayResY`` (Req 7.1).

    Returns:
        The complete :class:`Kinetic_Plan`; never raises (Req 6.7).
    """
    # -- resolved inputs ----------------------------------------------------
    options = (
        opts
        if isinstance(opts, Kinetic_Options)
        else Kinetic_Options.parse(opts if isinstance(opts, Mapping) else None)
    )
    base = time_base if hasattr(time_base, "snap") else Time_Base()
    limit = coerce_float(duration, 0.0, lo=0.0)
    preset, _substituted = caption_presets.resolve_preset(options.preset_name)
    family = coerce_str(font, "", 128) or options.font_override or options.preset_font

    # -- geometry (task 5.3 helpers; Base_Preset position per Req 7.4) ------
    position = resolve_position(options.position, preset.position)
    align, margin_l, margin_r, margin_v = safe_area_margins(
        play_res_x,
        play_res_y,
        safe_area_x_pct=options.safe_area_x_pct,
        safe_area_y_pct=options.safe_area_y_pct,
        position=options.position,
        preset_position=preset.position,
    )
    width = coerce_int(play_res_x, 1080, lo=1)
    height = coerce_int(play_res_y, 1920, lo=1)

    colors = {
        "box": getattr(preset.colors, "box", ""),
        "highlight": getattr(preset.colors, "highlight", ""),
        "outline": getattr(preset.colors, "outline", ""),
        "primary": getattr(preset.colors, "primary", ""),
    }
    highlight_scale = coerce_int(
        int(round(coerce_float(preset.highlight_scale, 1.18, lo=0.01, hi=10.0) * 100)),
        118,
        lo=1,
        hi=1000,
    )

    # Resolution provenance (``style_substituted`` / ``position_substituted``)
    # becomes a namespaced marker the engine forwards verbatim (Req 4.8).
    markers: list[str] = [marker(ENGINE_ID, note) for note in options.notes]

    def _build(
        cues: tuple[Kinetic_Cue, ...],
        *,
        cue_level: bool,
        degraded: bool,
        extra_markers: tuple[str, ...],
        detail: str,
    ) -> Kinetic_Plan:
        return Kinetic_Plan(
            style=options.style,
            reveal=options.reveal,
            font=family,
            font_size=options.font_size,
            position=position,
            align=align,
            play_res_x=width,
            play_res_y=height,
            margin_l=margin_l,
            margin_r=margin_r,
            margin_v=margin_v,
            duration=limit,
            style_line=_style_line(
                preset,
                family,
                options.font_size,
                align,
                margin_l,
                margin_r,
                margin_v,
            ),
            hook_style=_hook_style_line(family, options.hook_font_size),
            hook_text=_text(hook_text),
            hook_duration_s=options.hook_duration_s,
            motion_duration_ms=options.motion_duration_ms,
            cues=cues,
            cue_level=cue_level,
            degraded=degraded,
            markers=tuple(markers) + extra_markers,
            detail=detail,
            colors=colors,
            highlight_scale=highlight_scale,
        )

    # -- step 1: sanitise ---------------------------------------------------
    source = _sanitise_words(words)
    if not source or limit <= 0.0:
        return _build(
            (),
            cue_level=False,
            degraded=False,
            extra_markers=(),
            detail="0 cues, 0 words",
        )

    # -- step 2: group with the v0.8.0 cue rules ---------------------------
    try:
        grouped = _captions().words_to_cues(source)
    except Exception:  # pragma: no cover - bounds are already sanitised floats
        grouped = []

    # -- step 3: layout + proportional re-splitting -------------------------
    drafts: list[tuple[float, float, list[_Source_Word], tuple[tuple[int, ...], ...]]] = []
    for cue in grouped:
        cue_words = [word for word in cue.words if isinstance(word, _Source_Word)]
        if not cue_words:
            continue
        drafts.extend(
            _split_drafts(
                coerce_float(cue.start, 0.0, lo=0.0),
                coerce_float(cue.end, 0.0, lo=0.0),
                cue_words,
                options.max_lines,
                options.max_line_width,
                base,
            )
        )

    # -- step 4: the single budget consultation (Req 14.4) ------------------
    if callable(remaining):
        try:
            left = coerce_float(remaining(), 1.0)
        except Exception:  # pragma: no cover - hostile budget reader
            left = 1.0
        if left <= 0.0:
            return _build(
                (),
                cue_level=False,
                degraded=True,
                extra_markers=(marker(ENGINE_ID, "degraded:budget"),),
                detail="budget exhausted during planning",
            )

    # -- step 5: snap, widen degenerate cues, fill timings, normalise -------
    # Bounds are clamped to the last frame boundary at or before the clip end, so
    # every emitted timestamp is snapped *and* inside [0, duration] (Reqs 5.4, 5.6).
    grid_limit = _snap_floor(base, limit)
    bounds: list[tuple[float, float]] = []
    for start, end, _cue_words, _lines in drafts:
        cue_start = min(max(_snap(base, start), 0.0), grid_limit)
        cue_end = min(max(_snap(base, end), 0.0), grid_limit)
        if cue_end < cue_start:
            cue_end = cue_start
        bounds.append((cue_start, cue_end))

    # A cue whose snapped interval collapsed to a point has *no span to
    # distribute* (the whole cue's timings were missing), so it would be dropped
    # by ``normalize_segments`` and take its words with it — a fully broken
    # transcript would then emit an empty document instead of degrading. Such
    # cues (and only such cues: a cue with a real interval is left exactly as
    # laid out, so re-split parts stay contiguous per Req 7.7) are given the
    # documented minimum on-screen span, laid out after the previous cue and
    # never past the next cue that does have a real start (Reqs 6.1, 6.2).
    frame = _frame_duration(base)
    filled_bounds: list[tuple[float, float]] = []
    cursor_pre = 0.0
    for index, (cue_start, cue_end) in enumerate(bounds):
        if cue_end <= cue_start:
            cue_start = max(cue_start, cursor_pre)
            ceiling = grid_limit
            for later_start, later_end in bounds[index + 1 :]:
                if later_end > later_start and later_start > cue_start:
                    ceiling = min(ceiling, later_start)
                    break
            # ``+ frame`` absorbs the half-frame snap error, so the snapped span
            # is never *shorter* than MIN_WORD_S (which would drop the cue).
            cue_end = min(
                max(ceiling, cue_start),
                _snap(base, cue_start + MIN_WORD_S + frame),
            )
        cursor_pre = max(cursor_pre, cue_end)
        filled_bounds.append((cue_start, cue_end))

    snapped: list[tuple[float, float, list[_Source_Word], tuple[tuple[int, ...], ...]]] = []
    segments: list[Timeline_Segment] = []
    for (cue_start, cue_end), (_s, _e, cue_words, lines) in zip(filled_bounds, drafts):
        filled = _fill_timings(cue_start, cue_end, cue_words)
        snapped.append((cue_start, cue_end, filled, lines))
        segments.append(Timeline_Segment(start=cue_start, end=cue_end))

    normalised = normalize_segments(
        segments, limit, time_base=base, min_duration=MIN_WORD_S
    )

    kept: list[tuple[float, float, list[_Source_Word], tuple[tuple[int, ...], ...]]] = []
    cursor = 0.0
    for cue_start, cue_end, filled, lines in sorted(
        snapped, key=lambda entry: (entry[0], entry[1])
    ):
        segment = _covering_segment(normalised, cue_start, cue_end)
        if segment is None:
            continue                        # dropped by normalisation: words go too
        start = max(cue_start, float(segment.start), cursor)
        end = min(cue_end, float(segment.end))
        if end <= start:
            continue
        cursor = end
        kept.append((start, end, filled, lines))

    # -- step 6: emphasis (keywords, then the Word_Confidence floor) --------
    flat: list[_Source_Word] = [word for _s, _e, cue_words, _l in kept for word in cue_words]
    selected: Any = None
    if options.highlight_keywords and callable(keyword_planner) and flat:
        try:
            selected = keyword_planner(flat, use_ai=options.keyword_ai, client=None)
        except Exception:  # pragma: no cover - a planner failure loses emphasis only
            selected = None

    emphasis: list[bool] = []
    for index, word in enumerate(flat):
        hit = False
        if selected is not None:
            try:
                # Membership test against a *positional* index only — the set is
                # never iterated, so its iteration order cannot leak (Req 11.4).
                hit = index in selected
            except Exception:  # pragma: no cover - hostile container
                hit = False
        if hit and word.probability < options.confidence_floor:
            hit = False                     # Reqs 5.9, 6.5 — text/timing untouched
        emphasis.append(hit)

    # -- build the cue records ---------------------------------------------
    captions = _captions()
    cues: list[Kinetic_Cue] = []
    synthesised_count = 0
    flat_index = 0
    for start, end, cue_words, lines in kept:
        line_of: dict[int, int] = {}
        for line_index, line in enumerate(lines):
            for word_index in line:
                line_of.setdefault(word_index, line_index)

        planned: list[Kinetic_Word] = []
        for offset, word in enumerate(cue_words):
            word_start = min(max(word.start, start), end)
            word_end = min(max(word.end, word_start), end)
            if word.synthesised and word_end - word_start < MIN_WORD_S:
                word_end = min(end, word_start + MIN_WORD_S)
                word_start = max(start, word_end - MIN_WORD_S)
            # The motion onset lives on the whole-millisecond grid — the only one
            # libass' ``\t`` can express — so the planned word start is quantised
            # *down* onto that grid and ``rel_ms`` is read straight off the same
            # integer. That single derivation is what keeps both halves of the
            # contract true at once: ``rel_ms == round((word.start - cue.start) *
            # 1000)``, which is ``build_word_span``'s own arithmetic and hence the
            # Req 4.3 byte-for-byte parity, *and* ``cue.start + rel_ms / 1000 <=
            # word.end`` (Reqs 5.3, 5.8), because the onset now **is** the word's
            # own start. Quantising down never moves a bound outward, so words
            # stay inside their cue and a synthesised word keeps its MIN_WORD_S
            # span (Req 6.2). ``_MS_EPSILON`` absorbs binary-float error, so an
            # offset that is mathematically a whole millisecond (e.g. 32/30 s -
            # 26/30 s, which evaluates to 0.19999999999999996) floors onto that
            # millisecond instead of the one below it.
            rel_ms = max(
                0, int(math.floor((word_start - start) * 1000.0 + _MS_EPSILON))
            )
            word_start = start + rel_ms / 1000.0
            if word.synthesised:
                synthesised_count += 1
            planned.append(
                Kinetic_Word(
                    text=captions._escape(word.text),      # Req 4.7
                    start=word_start,
                    end=word_end,
                    rel_ms=rel_ms,
                    emphasis=emphasis[flat_index] if flat_index < len(emphasis) else False,
                    timing_synthesised=word.synthesised,
                    emoji=_inline_emoji(word, preset, options),
                    line=line_of.get(offset, 0),
                )
            )
            flat_index += 1

        cues.append(
            Kinetic_Cue(
                segment=Timeline_Segment(start=start, end=end),
                words=tuple(planned),
                lines=lines,
            )
        )

    # -- step 7: the synthesised-timing degradation check (Reqs 6.3, 6.4) ---
    word_count = sum(len(cue.words) for cue in cues)
    ratio = (synthesised_count / word_count) if word_count else 0.0
    extra: tuple[str, ...] = ()
    cue_level = False
    degraded = False
    detail = (
        f"{len(cues)} cues, {word_count} words, "
        f"style={options.style}, reveal={options.reveal}"
    )
    if ratio > SYNTHESISED_RATIO_LIMIT:
        cue_level = True
        degraded = True
        extra = (marker(ENGINE_ID, "degraded:word_timings"),)
        detail = (
            f"{synthesised_count}/{word_count} words had synthesised timings; "
            "cue-level animation"
        )

    return _build(
        tuple(cues),
        cue_level=cue_level,
        degraded=degraded,
        extra_markers=extra,
        detail=detail,
    )


def _inline_emoji(word: Any, preset: Any, options: Kinetic_Options) -> str:
    """The inline emoji glyph for ``word``, or ``""`` (Reqs 8.6, 8.7).

    Delegates to ``captions.caption_emoji_glyph`` — a pure, font-glyph-only,
    download-free helper — and only when in-caption emoji is enabled on **both**
    the Base_Preset and the resolved options (``Kinetic_Options.emoji_inline`` is
    already the conjunction of the two). An unavailable glyph returns ``""``, so
    the glyph is dropped while the surrounding words are retained (Req 8.7).
    """
    if not options.emoji_inline:
        return ""
    try:
        return _text(
            _captions().caption_emoji_glyph(
                word, preset, permissible=options.permissibility
            )
        )
    except Exception:  # pragma: no cover - the helper never raises
        return ""



# ---------------------------------------------------------------------------
# The pure ASS emitter — ``emit_ass`` (tasks 8.1-8.5) — Reqs 3.3, 4, 7.1-7.5,
# 8.3-8.8, 11.5, 11.6
# ---------------------------------------------------------------------------
#
# ``emit_ass(plan)`` is a **pure function of the plan**: no I/O, no clock, no
# subprocess, no locale-sensitive formatting, no randomness (Reqs 11.5, 11.7).
# The only external symbols it reaches for are ``captions._ass_timestamp`` (the
# single timestamp formatter, Req 11.6) and ``captions._escape`` (for the hook
# title only — cue word text was already escaped by the planner, Req 4.7), both
# through the lazy :func:`_captions` accessor so module import stays free of
# ``pydantic`` (Reqs 1.4, 18.2).
#
# Everything the emitter needs about the look is *already on the plan*:
# ``style_line`` / ``hook_style`` were built by the planner from the Base_Preset
# (Reqs 7.2, 10.4, 3.3), ``colors`` / ``highlight_scale`` carry the emphasis
# palette, ``motion_duration_ms`` is the span table's ``d``, and each
# :class:`Kinetic_Word` already carries its escaped text, its ``rel_ms`` onset,
# its emphasis flag and its inline emoji glyph. Nothing is re-derived here.

#: The ``[V4+ Styles]`` ``Format:`` line — byte-identical to ``captions.build_ass``
#: so libass parses the 23 style columns the same way (Req 7.5).
_ASS_STYLE_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
    "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
    "MarginR, MarginV, Encoding"
)

#: The ``[Events]`` ``Format:`` line — byte-identical to ``captions.build_ass``:
#: nine comma-separated fields before the free-form ``Text`` field (Req 4.10).
_ASS_EVENT_FORMAT = (
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
    "Effect, Text"
)

#: ``build_word_span``'s own defaults, used when a plan carries no palette.
_DEFAULT_PRIMARY = "&H00FFFFFF"
_DEFAULT_HIGHLIGHT = "&H0000E5FF"

#: The ASS text-field spelling of a Text_Line break (Req 7.5 — ``WrapStyle: 2``
#: disables libass auto-wrapping, so the engine emits every break itself).
_LINE_BREAK = "\\N"


def _plan_palette(plan: Kinetic_Plan) -> tuple[str, str]:
    """Return ``(primary, highlight)`` from the plan's Base_Preset palette."""
    colors = plan.colors if isinstance(plan.colors, Mapping) else {}
    primary = _text(_get(colors, "primary", "")) or _DEFAULT_PRIMARY
    highlight = _text(_get(colors, "highlight", "")) or _DEFAULT_HIGHLIGHT
    return primary, highlight


def _caption_anchor(plan: Kinetic_Plan) -> tuple[int, int]:
    """The resolved caption position in script coordinates, for ``\\move``.

    All three :data:`POSITIONS` are horizontally centred (ASS alignments 2/5/8),
    so ``x`` is the script mid-point; ``y`` follows the resolved alignment and the
    Safe_Area ``MarginV`` (Reqs 4.5, 7.2, 7.3).
    """
    anchor_x = int(plan.play_res_x) // 2
    if plan.align == 8:
        anchor_y = int(plan.margin_v)
    elif plan.align == 5:
        anchor_y = int(plan.play_res_y) // 2
    else:
        anchor_y = int(plan.play_res_y) - int(plan.margin_v)
    return anchor_x, max(0, anchor_y)


def _style_span(
    style: str,
    word: Kinetic_Word,
    motion_ms: int,
    primary: str,
    highlight: str,
) -> str:
    """The per-word animation span for one Kinetic_Style (Reqs 4.2-4.6).

    The four shared styles reproduce ``captions.build_word_span`` **byte for
    byte** for the same word — including its hard-coded ``+120`` (``pop``) and
    ``+30`` (``typewriter``) ramps, which are deliberately *not* ``motion_ms`` —
    so a Kinetic_Plan at ``reveal="cumulative"`` renders the v0.8.0 look exactly
    (Req 4.3). The three new styles use ``d = motion_ms`` as the design's span
    table specifies. ``rel`` is the word's onset relative to its **cue** start in
    milliseconds, which is the offset libass ``\\t`` expects (Req 5.3).

    Well-formed by construction (Req 4.10): every ``{`` opened below is closed in
    the same f-string, and ``word.text`` was already ``_escape``-d by the planner,
    so no transcript text can unbalance the braces.
    """
    escaped = word.text
    rel = int(word.rel_ms)
    duration_ms = int(motion_ms)
    half = duration_ms // 2

    if style == "karaoke_fill":
        dur_cs = max(1, int(round((word.end - word.start) * 100)))
        return f"{{\\kf{dur_cs}}}{escaped}"
    if style == "pop":
        return (
            f"{{\\fscx60\\fscy60\\t({rel},{rel + 120},"
            f"\\fscx100\\fscy100)}}{escaped}"
        )
    if style in ("typewriter", "slide_up"):
        # ``slide_up`` carries the event-level ``\move`` (added in
        # :func:`_cue_event`) plus this per-word alpha gate, so its words still
        # appear on beat rather than all at once (Req 4.5).
        return f"{{\\alpha&HFF&\\t({rel},{rel + 30},\\alpha&H00&)}}{escaped}"
    if style == "bounce":
        return (
            f"{{\\fscx55\\fscy55"
            f"\\t({rel},{rel + half},"
            f"\\fscx{BOUNCE_OVERSHOOT}\\fscy{BOUNCE_OVERSHOOT})"
            f"\\t({rel + half},{rel + duration_ms},\\fscx100\\fscy100)}}{escaped}"
        )
    if style == "highlight_sweep":
        return (
            f"{{\\c{highlight}&\\t({rel},{rel + duration_ms},"
            f"\\c{primary}&)}}{escaped}"
        )
    return escaped  # "none" — the plain escaped word (Req 4.3)


def _word_span(
    plan: Kinetic_Plan, word: Kinetic_Word, primary: str, highlight: str
) -> str:
    """Compose one word's span: style, inline emoji, Reveal_Mode gate, emphasis.

    The composition is a **product**, so the 7 x 2 style/Reveal_Mode matrix needs
    no special cases (Req 4.9):

    1. the Kinetic_Style span (:func:`_style_span`);
    2. the inline emoji glyph, appended *inside* the word's span so emphasis and
       the reveal gate cover it too — an empty glyph is simply dropped, keeping
       every surrounding word (Reqs 8.6, 8.7);
    3. the ``word_by_word`` alpha gate, wrapped *around* the style span so words
       before their onset are fully transparent. ``typewriter`` is excluded
       because its own tag set already **is** that gate — double-gating would emit
       two ``\\alpha`` overrides for one word (Req 4.9);
    4. emphasis, outermost, in ``build_word_span``'s composition order so both
       tag sets apply and the word's spoken timing is untouched (Reqs 5.9, 6.5).
    """
    span = _style_span(plan.style, word, plan.motion_duration_ms, primary, highlight)

    if word.emoji:
        span = f"{span} {word.emoji}"

    if plan.reveal == "word_by_word" and plan.style != "typewriter":
        rel = int(word.rel_ms)
        span = f"{{\\alpha&HFF&\\t({rel},{rel + 1},\\alpha&H00&)}}{span}"

    if word.emphasis:
        scale = int(plan.highlight_scale)
        span = (
            f"{{\\c{highlight}&\\fscx{scale}\\fscy{scale}}}"
            f"{span}"
            f"{{\\c{primary}&\\fscx100\\fscy100}}"
        )
    return span


def _text_lines(cue: Kinetic_Cue) -> list[list[int]]:
    """The cue's Text_Lines as word-index lists, each index appearing once.

    ``Kinetic_Cue.lines`` is authoritative (the planner's ``_finalise_lines``
    already guarantees full coverage); this guard only drops out-of-range and
    duplicate indices and appends anything unplaced, so no word can be emitted
    twice or lost however the plan was constructed (Reqs 7.5, 7.8, 8.10).
    """
    count = len(cue.words)
    if not count:
        return []
    groups: list[list[int]] = []
    used: set[int] = set()
    for line in cue.lines:
        indices = [
            index for index in line if 0 <= index < count and index not in used
        ]
        used.update(indices)
        if indices:
            groups.append(indices)
    missing = [index for index in range(count) if index not in used]
    if missing:
        if groups:
            groups[-1].extend(missing)
        else:
            groups.append(missing)
    return groups


def _cue_text(
    plan: Kinetic_Plan, cue: Kinetic_Cue, primary: str, highlight: str
) -> str:
    """The ASS text field for one cue: joined spans, ``\\N`` between Text_Lines.

    Within a Text_Line neighbours are joined by :func:`join_separator` — the very
    helper :func:`pack_lines` measured the line with — so the width that was
    packed and the text that is emitted can never disagree (Reqs 8.4, 8.5).
    Text_Lines are joined with the literal ``\\N`` (Req 7.5). Words are emitted in
    Word_Timeline order with **no** directional override characters inserted, so
    right-to-left text is left to libass' own bidi handling (Reqs 4.11, 8.3).

    Under ``plan.cue_level`` all per-word tags are dropped and the plain joined
    text is emitted; the single ``\\fad`` prefix is added by :func:`_cue_event`
    (Req 6.4).
    """
    rendered: list[str] = []
    for group in _text_lines(cue):
        parts: list[str] = []
        previous = ""
        for position, index in enumerate(group):
            word = cue.words[index]
            if position:
                parts.append(join_separator(previous, word.text))
            parts.append(
                word.text
                if plan.cue_level
                else _word_span(plan, word, primary, highlight)
            )
            previous = word.text
        rendered.append("".join(parts))
    return _LINE_BREAK.join(rendered)


def _cue_event(
    plan: Kinetic_Plan, cue: Kinetic_Cue, primary: str, highlight: str
) -> str | None:
    """One ``Dialogue:`` line for a cue, or ``None`` when it has nothing to say.

    Both timestamps are clamped to ``[0, plan.duration]`` and formatted **only**
    through ``captions._ass_timestamp`` (Reqs 5.6, 11.6). The event names the
    ``Default`` style declared in the header (Req 4.10) and carries at most one
    event-level prefix: ``{\\fad(120,120)}`` under cue-level degradation
    (Req 6.4), or the ``slide_up`` entry ``\\move`` — the one tag that cannot be
    expressed per word because it is event-scoped (Req 4.5).
    """
    text = _cue_text(plan, cue, primary, highlight)
    if not text:
        return None

    limit = max(0.0, float(plan.duration))
    start = min(max(0.0, cue.start), limit)
    end = min(max(0.0, cue.end), limit)
    if end < start:
        end = start
    stamp = _captions()._ass_timestamp

    if plan.cue_level:
        prefix = f"{{\\fad({CUE_FADE_MS[0]},{CUE_FADE_MS[1]})}}"
    elif plan.style == "slide_up":
        anchor_x, anchor_y = _caption_anchor(plan)
        prefix = (
            f"{{\\move({anchor_x},{anchor_y + SLIDE_UP_PX},"
            f"{anchor_x},{anchor_y},0,{int(plan.motion_duration_ms)})}}"
        )
    else:
        prefix = ""

    return (
        f"Dialogue: 0,{stamp(start)},{stamp(end)},Default,,0,0,0,,{prefix}{text}"
    )


def _hook_event(plan: Kinetic_Plan) -> str | None:
    """The hook title event, byte-identical to ``build_ass``'s (Reqs 3.3, 3.7).

    Emitted as the **first** event when the plan carries a non-empty hook text, so
    no hook title is lost when the engine owns the Subtitle_Slot. ``run`` passes
    an empty hook text whenever ``opts.hook_enabled`` is off, which is what gates
    this on the option without the emitter needing the options value.
    """
    hook = _text(plan.hook_text).strip()
    if not hook:
        return None
    captions = _captions()
    start = captions._ass_timestamp(0.0)
    end = captions._ass_timestamp(max(0.5, float(plan.hook_duration_s)))
    escaped = captions._escape(hook.upper())
    return f"Dialogue: 1,{start},{end},Hook,,0,0,0,,{{\\fad(250,350)}}{escaped}"


def _fallback_style_line(plan: Kinetic_Plan) -> str:
    """A minimal ``Style: Default`` line for a plan that carries none.

    The planner always builds ``style_line`` from the Base_Preset, so this is
    reached only by a hand-built or truncated plan; it exists so that every
    ``Dialogue:`` line still names a style declared in ``[V4+ Styles]`` (Req 4.10).
    """
    primary, highlight = _plan_palette(plan)
    return (
        f"Style: Default,{plan.font},{int(plan.font_size)},{primary},{highlight},"
        f"&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,2,1,{int(plan.align)},"
        f"{int(plan.margin_l)},{int(plan.margin_r)},{int(plan.margin_v)},1"
    )


def emit_ass(plan: Any) -> str:
    """Render a :class:`Kinetic_Plan` as an ASS document — **pure, locale-free**.

    Deterministic by construction (Req 11.5): the result depends on nothing but
    ``plan``. No filesystem, no clock, no subprocess, no network, no randomness,
    no locale-sensitive formatting; every timestamp is produced by
    ``captions._ass_timestamp`` after clamping to ``[0, plan.duration]``
    (Reqs 5.6, 11.6). Writing the returned text is the engine's separate, single
    impure step (:func:`_write_text_utf8`, Req 12.1).

    Document shape — identical to ``captions.build_ass`` so libass parses it the
    same way (Reqs 7.1, 7.5, 3.3)::

        [Script Info]           ScriptType, PlayResX/Y (probed size), WrapStyle: 2,
                                ScaledBorderAndShadow: yes
        [V4+ Styles]            the 23-column Format: line, then Style: Default
                                (Base_Preset look + Safe_Area margins) and
                                Style: Hook — both already built by the planner
        [Events]                the 10-field Format: line, then the hook event
                                (first, when present) and one event per cue

    Every ``Dialogue:`` line names ``Default`` or ``Hook``, carries the nine
    comma-separated fields the ``Format:`` line declares before its text, and has
    balanced braces: spans come only from the closed style table in
    :func:`_style_span`, each ``{`` is closed inside the same f-string, and word
    text was ``_escape``-d by the planner (Reqs 4.7, 4.10).

    Args:
        plan: A :class:`Kinetic_Plan`, or any mapping :meth:`Kinetic_Plan.from_dict`
            can rebuild one from (so ``plan(ctx)``'s serialised output round-trips
            straight back into the emitter).

    Returns:
        The complete ASS document, joined with ``"\\n"`` and ending in exactly one
        trailing newline (Req 8.8). Never raises.
    """
    record = plan if isinstance(plan, Kinetic_Plan) else Kinetic_Plan.from_dict(plan)
    primary, highlight = _plan_palette(record)

    events: list[str] = []
    hook = _hook_event(record)
    if hook:
        events.append(hook)
    for cue in record.cues:
        event = _cue_event(record, cue, primary, highlight)
        if event:
            events.append(event)

    lines: list[str] = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {int(record.play_res_x)}",
        f"PlayResY: {int(record.play_res_y)}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        _ASS_STYLE_FORMAT,
        record.style_line or _fallback_style_line(record),
        record.hook_style or _hook_style_line(record.font, 110),
        "",
        "[Events]",
        _ASS_EVENT_FORMAT,
    ]
    lines.extend(events)
    return "\n".join(lines) + "\n"



# ---------------------------------------------------------------------------
# The engine class (tasks 9.1-9.3) — Reqs 1, 2, 3, 9, 12, 13.1, 14.4, 16
# ---------------------------------------------------------------------------
#
# Everything above this line is pure. The class below is the only part of the
# module that touches the outside world, and it touches it exactly twice per
# invocation: it *reads* capability answers (cached by the foundation
# ``Capability_Report``) and it *writes* one UTF-8 ASS document inside the
# Engine_Workspace (Req 12.1). It never invokes ffmpeg, never spawns a
# subprocess of its own, never opens a socket, and never downloads a model
# (Reqs 1.5, 15.1, 15.2, 16.1); compose work is handed back declaratively as a
# ``Compose_Contribution`` for the ONE compositor pass (Req 2.1).


class Kinetic_Typography_Engine(AV_Engine):
    """Word-level animated captions as a COMPOSE-stage :class:`AV_Engine` (Req 1.1).

    The ClassVar block *is* the contract the Engine_Host reads: stage, ordering
    priority, required capability, cost declarations, and the inherited
    :meth:`~worker.engines.base.AV_Engine.flag_field` (``kinetic_typography_enabled``,
    default OFF — Reqs 1.8, 15.5).

    Collaborators are dependency-injected keyword-only (Req 18.1) so tests can
    drive the engine with no fonts, no LLM and no filesystem:

    * ``font_probe`` — ``captions.font_available``-shaped ``(family) -> bool``,
      consulted **only** when the context carries no ``Capability_Report``; the
      report is the primary oracle, and it resolves ``font:<family>`` through
      ``captions.font_available`` itself (Reqs 9.1, 9.2);
    * ``keyword_planner`` — ``caption_presets.plan_keywords``-shaped, called by the
      pure planner for keyword emphasis (Reqs 5.9, 18.1);
    * ``ass_writer`` — ``(path, text) -> None``, the single impure step (Req 12.1).

    Import safety (Reqs 1.4, 1.7): ``font_probe`` defaults to ``None`` rather than
    to ``captions.font_available`` **on purpose** — naming that default would
    require importing :mod:`worker.captions` (hence ``config``, hence ``pydantic``)
    at module scope, which the engine package's import-safety gate forbids. The
    default is therefore resolved lazily through :func:`_captions` at the moment a
    font is probed, which is behaviourally identical and keeps
    ``import worker.engines.kinetic`` free of every heavy dependency.
    """

    engine_id: ClassVar[str] = ENGINE_ID                    # Req 1.1
    stage: ClassVar[Engine_Stage] = Engine_Stage.COMPOSE    # Req 1.1
    priority: ClassVar[int] = 50                            # Req 1.1
    required_capabilities: ClassVar[tuple[str, ...]] = (SUBTITLES_CAPABILITY,)  # 1.5
    #: ``font:<family>`` is *not* declared here: the family is only known after
    #: options resolution, so it is probed per clip by :meth:`_resolve_font` and a
    #: missing font degrades rather than gating the engine (Reqs 1.5, 9.4).
    optional_capabilities: ClassVar[tuple[str, ...]] = ()
    requires_network: ClassVar[bool] = False                # Reqs 1.5, 15.1
    requires_model_download: ClassVar[bool] = False         # Reqs 1.5, 15.2
    time_budget_s: ClassVar[float] = 5.0                    # Reqs 1.6, 16.1
    max_media_passes: ClassVar[int] = 0                     # Reqs 1.6, 2.2
    max_inputs: ClassVar[int] = 0                           # Req 2.4 — subtitle only
    produces_media: ClassVar[bool] = False                  # Req 1.6

    def __init__(
        self,
        *,
        font_probe: Callable[[str], bool] | None = None,
        keyword_planner: Callable[..., Any] | None = None,
        ass_writer: Callable[[Any, str], None] | None = None,
    ) -> None:
        self._font_probe = font_probe
        self._keyword_planner = (
            keyword_planner if keyword_planner is not None else caption_presets.plan_keywords
        )
        self._ass_writer = ass_writer if ass_writer is not None else _write_text_utf8

    # -- contract methods ---------------------------------------------------

    def resolve_options(self, options: Any) -> Kinetic_Options:
        """Project Processing_Options onto :class:`Kinetic_Options` (Reqs 10.3, 10.4).

        Pure, total and idempotent, and it never writes to ``options`` — the whole
        projection is :meth:`Kinetic_Options.from_processing_options`, which reads
        attributes only (Reqs 1.3, 10.8, 10.9, 10.10).
        """
        return Kinetic_Options.from_processing_options(options)

    def plan(self, ctx: Engine_Context) -> Mapping[str, Any]:
        """Return the serialised :class:`Kinetic_Plan` — **pure** (Reqs 11.1, 11.2).

        No ffmpeg, no network, no subprocess of its own, no clock and no
        randomness: the result is a function of ``ctx.words``, ``ctx.duration``,
        ``ctx.time_base``, ``ctx.options``, the resolved font, the hook text and
        clip size on ``ctx.clip_metadata``, the injected keyword planner and
        ``ctx.remaining`` (Reqs 11.5, 15.6, 18.2). The mapping round-trips back
        through :meth:`Kinetic_Plan.from_dict`, so ``emit_ass(plan(ctx))`` is
        valid (Req 11.10).
        """
        font, _font_markers = self._resolve_font(ctx)
        return self._plan_for(ctx, font).to_dict()

    def run(self, ctx: Engine_Context) -> Engine_Result:
        """Plan, emit, and write one ASS document inside the Engine_Workspace.

        The gate ladder, in order — each rung returns, and the compositor
        consequently keeps rendering captions through its own v0.8.0 path:

        1. captions disabled -> ``skipped``, no markers (Req 3.4);
        2. no non-whitespace word in the rebased timeline -> ``skipped`` (Req 3.5);
        3. :data:`SUBTITLES_CAPABILITY` unavailable -> ``degraded`` +
           ``unavailable:ffmpeg_filter:subtitles`` (Reqs 13.1, 13.3);
        4. ``ctx.remaining() <= 0`` -> ``degraded`` + ``degraded:budget`` (Req 14.4).

        Otherwise the font ladder is descended (Req 9), the pure planner and pure
        emitter run, and the text is written **once** through
        ``ctx.workspace.path(ASS_NAME)`` — the only impure step, and the only file
        this engine ever creates (Reqs 12.1, 12.3, 16.3, 16.4). An ``OSError`` from
        the writer is reported as ``failed`` with a ``"<Type>: <msg>"`` detail and
        no contribution (Req 12.5).

        Ownership of the Subtitle_Slot is expressed by **one** signal: a
        ``degraded`` result carries ``contribution=None``, so
        ``contribution is not None`` <=> ``applied`` <=> the engine owns the slot
        and the compositor must not render captions itself (Reqs 3.6, 3.9).
        """
        opts = self._resolved_options(ctx)

        # --- rung 1: captions disabled (Req 3.4) --------------------------
        if not opts.captions_enabled:
            return Engine_Result.skipped(ENGINE_ID)

        # --- rung 2: nothing to animate (Reqs 3.5, 6.6) -------------------
        words = tuple(getattr(ctx, "words", ()) or ())
        if not any(_word_text(word).strip() for word in words):
            return Engine_Result.skipped(ENGINE_ID)

        # --- rung 3: the required capability (Reqs 13.1, 13.3) ------------
        if not self._capability_available(ctx, SUBTITLES_CAPABILITY):
            return Engine_Result.degraded(
                ENGINE_ID,
                "subtitles filter unavailable",
                markers=(marker(ENGINE_ID, f"unavailable:{SUBTITLES_CAPABILITY}"),),
            )

        # --- rung 4: the time budget (Req 14.4) ---------------------------
        if self._remaining(ctx) <= 0.0:
            return Engine_Result.degraded(
                ENGINE_ID,
                "budget exhausted before planning",
                markers=(marker(ENGINE_ID, "degraded:budget"),),
            )

        # --- plan and emit: both pure (Reqs 11.1, 11.5) -------------------
        font, font_markers = self._resolve_font(ctx)
        kplan = self._plan_for(ctx, font)
        text = emit_ass(kplan)

        # --- the single impure step (Reqs 12.1, 12.3, 12.5) ---------------
        workspace = getattr(ctx, "workspace", None)
        if workspace is None:
            return Engine_Result.failed(
                ENGINE_ID, "RuntimeError: no Engine_Workspace was allocated"
            )
        try:
            dest = workspace.path(ASS_NAME)
            self._ass_writer(dest, text)
        except OSError as exc:
            return Engine_Result.failed(ENGINE_ID, f"{type(exc).__name__}: {exc}")

        artifact = workspace.artifact(                      # Reqs 12.2, 12.4, 12.7
            ASS_NAME, media_type="subtitle", durable=opts.durable_subtitle
        )

        markers = _str_tuple(
            (
                *font_markers,                              # <=1 degraded:font: (Req 9.8)
                *kplan.markers,                             # style_substituted / degraded:*
                marker(ENGINE_ID, f"style:{kplan.style}"),  # Req 3.7
                marker(ENGINE_ID, "supersedes_captions"),   # Reqs 3.7, 3.9
            )
        )

        # Font substitution degrades too: Req 9.4/9.5 hand such a clip back to the
        # v0.8.0 caption path, which performs the same substitution through
        # ``_preset_header_styles``. The planner cannot see the font ladder (it is
        # given the resolved family), so the two degradation sources are OR-ed here.
        degraded = bool(kplan.degraded or font_markers)
        status = Engine_Status.DEGRADED if degraded else Engine_Status.APPLIED

        result = Engine_Result(
            engine_id=ENGINE_ID,
            status=status,
            markers=markers,
            artifacts=(artifact,),
            plan=kplan.to_dict(),
            contribution=Compose_Contribution(              # Reqs 2.1, 2.3, 2.4
                engine_id=ENGINE_ID,
                inputs=(),
                video_filters=(),
                audio_filters=(),
                subtitle_path=dest,
                z_order=KINETIC_Z_ORDER,
            ),
            detail=kplan.detail,
        )
        if status is Engine_Status.DEGRADED:                # Reqs 3.6, 3.9
            return dataclasses.replace(result, contribution=None)
        return result

    # -- the font ladder (task 9.2) — Reqs 9.1-9.8 -------------------------

    def _resolve_font(self, ctx: Any) -> tuple[str, tuple[str, ...]]:
        """Descend ``font_override`` -> Base_Preset font -> :data:`FALLBACK_FONT`.

        Returns ``(family, markers)`` where ``family`` is **always** a member of
        the ladder (Req 9.7) and ``markers`` holds **at most one**
        ``engine:kinetic_typography:degraded:font:<requested_family>`` — recorded
        exactly when the family used is not the one requested (Reqs 9.4, 9.8).
        Empty rungs are dropped before the descent, so the requested family is the
        first non-empty rung.

        Nothing is downloaded and no network is touched: each rung is a cached
        ``font:<family>`` capability answer (Reqs 9.6, 15.1). The requested
        Kinetic_Style and Reveal_Mode are untouched by substitution — only the
        font name changes (Req 9.5).
        """
        opts = self._resolved_options(ctx)
        ladder = tuple(
            family
            for family in (opts.font_override, opts.preset_font, FALLBACK_FONT)
            if family
        ) or (FALLBACK_FONT,)
        requested = ladder[0]

        for family in ladder:
            if self._font_available(ctx, family):
                if family == requested:
                    return family, ()
                return family, (marker(ENGINE_ID, f"degraded:font:{requested}"),)
        # Nothing probed available: use the documented last rung anyway (Req 9.7).
        return FALLBACK_FONT, (marker(ENGINE_ID, f"degraded:font:{requested}"),)

    # -- internal helpers ---------------------------------------------------

    def _resolved_options(self, ctx: Any) -> Kinetic_Options:
        """The :class:`Kinetic_Options` for this invocation, however ``ctx`` carries it.

        The host resolves options before building the context, so ``ctx.options``
        is normally already a :class:`Kinetic_Options`; a mapping (a serialised
        record) is parsed and anything else is projected, so the engine is total
        even against a hand-built context.
        """
        value = getattr(ctx, "options", None)
        if isinstance(value, Kinetic_Options):
            return value
        if value is None:
            return Kinetic_Options()
        if isinstance(value, Mapping):
            return Kinetic_Options.parse(value)
        return Kinetic_Options.from_processing_options(value)

    def _plan_for(self, ctx: Any, font: str) -> Kinetic_Plan:
        """Run the pure planner for ``ctx`` with an already-resolved ``font``."""
        opts = self._resolved_options(ctx)
        play_res_x, play_res_y = self._play_res(ctx)
        return plan_kinetic(
            words=getattr(ctx, "words", ()),          # rebased, clip-relative (Req 5.1)
            duration=getattr(ctx, "duration", 0.0),   # Reqs 5.6, 5.7
            time_base=getattr(ctx, "time_base", None),  # Reqs 5.4, 16.2
            opts=opts,
            font=font,
            hook_text=self._hook_text(ctx, opts),     # Req 3.3
            keyword_planner=self._keyword_planner,    # Req 18.1
            remaining=getattr(ctx, "remaining", None),  # Req 14.4
            play_res_x=play_res_x,
            play_res_y=play_res_y,
        )

    def _hook_text(self, ctx: Any, opts: Kinetic_Options) -> str:
        """The hook title to plan with — ``""`` when hook titles are off (Req 3.3).

        Read from the Clip_Metadata channel the Pipeline publishes at the COMPOSE
        hook: ``ctx.clip_metadata["hook_text"]``. Total — a non-mapping
        ``clip_metadata``, an absent key or a ``None`` value all read as no hook.

        ``emit_ass`` gates the hook event on a non-empty ``plan.hook_text`` alone
        (the plan carries no ``hook_enabled`` flag), so enablement is enforced
        here: a disabled hook plans as no hook at all.
        """
        if not opts.hook_enabled:
            return ""
        metadata = getattr(ctx, "clip_metadata", None)
        if not isinstance(metadata, Mapping):
            return ""
        value = _get(metadata, "hook_text", "")
        # An explicit ``None`` reads as "no hook", not as the text ``"None"``.
        return "" if value is None else _text(value)

    def _play_res(self, ctx: Any) -> tuple[int, int]:
        """``(PlayResX, PlayResY)`` for the emitted header (Req 7.1).

        The engine may not probe the clip itself (``run`` creates no subprocess),
        so the target size comes from the Clip_Metadata channel the Pipeline
        publishes at the COMPOSE hook: ``ctx.clip_metadata["clip_size"]``, a
        ``(width, height)`` pair. Total by construction — a non-mapping
        ``clip_metadata``, an absent key, a value that is not a two-element
        sequence, and non-numeric or non-positive elements all land on
        :data:`DEFAULT_PLAY_RES`, the pipeline's target vertical frame, rather
        than raise. Both elements still pass through ``coerce_int(..., lo=1)``,
        which is the identity for an already-valid size; a **non-positive**
        element is treated as malformed (so the pair falls back whole) instead of
        being clamped to a degenerate ``1`` — a 1-pixel ``PlayRes`` is not a size
        the Pipeline can ever publish.
        """
        metadata = getattr(ctx, "clip_metadata", None)
        metadata = metadata if isinstance(metadata, Mapping) else {}
        width: Any = None
        height: Any = None
        size = _get(metadata, "clip_size", None)
        if isinstance(size, (list, tuple)) and len(size) == 2:
            # ``0`` is the "malformed" sentinel here: ``coerce_int`` already maps
            # every non-numeric value (including ``bool``) onto the default.
            candidate_w = coerce_int(size[0], 0)
            candidate_h = coerce_int(size[1], 0)
            if candidate_w >= 1 and candidate_h >= 1:
                width, height = candidate_w, candidate_h
        return (
            coerce_int(width, DEFAULT_PLAY_RES[0], lo=1),
            coerce_int(height, DEFAULT_PLAY_RES[1], lo=1),
        )

    def _capability_available(self, ctx: Any, capability_id: str) -> bool:
        """Whether ``capability_id`` is available, per the context's report (Req 13.1).

        A context carrying no ``Capability_Report`` reports *unknown*, not
        *missing*, and unknown reads as available — the same conservative choice
        ``captions.font_available`` makes for an unenumerable host font list, so a
        capability is never falsely reported absent.
        """
        report = getattr(ctx, "capabilities", None)
        if report is None:
            return True
        try:
            return bool(report.available(capability_id))
        except Exception:  # pragma: no cover - Capability_Report never raises
            return True

    def _font_available(self, ctx: Any, family: str) -> bool:
        """Whether ``font:<family>`` is available (Reqs 9.1, 9.2, 9.6).

        The context's ``Capability_Report`` is the oracle — it resolves
        ``font:<family>`` through ``captions.font_available`` and caches the
        answer, so each family is probed at most once per process. The injected
        ``font_probe`` is used only when the context carries no report at all
        (a hand-built context), and ``captions.font_available`` is reached lazily
        so module import needs no font machinery.
        """
        report = getattr(ctx, "capabilities", None)
        if report is not None:
            try:
                return bool(report.available(f"font:{family}"))
            except Exception:  # pragma: no cover - Capability_Report never raises
                return True
        probe = self._font_probe
        if probe is None:
            probe = _captions().font_available
        try:
            return bool(probe(family))
        except Exception:  # pragma: no cover - a probe failure must not gate
            return True

    def _remaining(self, ctx: Any) -> float:
        """Seconds of budget left, ``inf`` when the context declares none (Req 14.4)."""
        reader = getattr(ctx, "remaining", None)
        if not callable(reader):
            return math.inf
        try:
            value = reader()
        except Exception:  # pragma: no cover - Engine_Context.remaining never raises
            return math.inf
        if value is None or isinstance(value, bool):
            return math.inf
        try:
            number = float(value)
        except Exception:  # pragma: no cover - hostile budget reader
            return math.inf
        return number if math.isfinite(number) else math.inf


# Registration happens once, at import (Req 2.1). The foundation ships a
# module-level ``register(engine, *, priority=None)`` *function* taking an engine
# **instance** — there is no registration decorator — so the call site is an
# expression, not a class annotation. The guard keeps a module *reload* (and a
# double import through two names) from raising ``Engine_Registration_Error``:
# registration stays exactly once per registry.
if engine_registry.get_registry().find(ENGINE_ID) is None:
    engine_registry.register(Kinetic_Typography_Engine())
