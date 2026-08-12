"""Caption generation and burn-in.

Turns word-level transcript timing into styled, karaoke-highlighted ASS
subtitles and burns them into a clip via FFmpeg's ``subtitles`` (libass) filter.

Design:
    * ``slice_words`` extracts the words that fall inside a clip's time window
      and rebases them to clip-relative time (0 = start of clip).
    * ``words_to_cues`` groups words into short, readable cues.
    * ``build_ass`` renders cues to an ASS file with a bottom-centred style and
      a per-word karaoke fill highlight (Opus-Clip style).
    * ``burn_captions`` composites the ASS onto the video.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from config import settings
from worker import caption_placement, cue_constraints, script_support, text_metrics, word_spans
from worker.effects.caption_presets import CaptionPreset
from worker.ffmpeg_utils import _run, escape_filter_path, h264_args
from worker.transcribe import Transcript, Word

logger = logging.getLogger(__name__)


@dataclass
class Cue:
    """A group of words shown together on screen."""

    start: float
    end: float
    words: list[Word] = field(default_factory=list)


def slice_words(transcript: Transcript, start: float, end: float) -> list[Word]:
    """Return words within ``[start, end]``, rebased to clip-relative time.

    A word is included when its midpoint falls inside the window. Timings are
    shifted so the clip begins at 0 and clamped to ``[0, end-start]``.
    """
    duration = end - start
    out: list[Word] = []
    for w in transcript.words:
        mid = (w.start + w.end) / 2
        if start <= mid <= end:
            out.append(
                Word(
                    start=max(0.0, w.start - start),
                    end=min(duration, w.end - start),
                    text=w.text.strip(),
                    probability=w.probability,
                )
            )
    return out


@dataclass(frozen=True)
class TextFit:
    """How wide a caption line may be, and in which font (C6, C16).

    Bundled as one object rather than five parameters because every caller needs all of them
    together and they must agree: measuring in one font and rendering in another produces
    confident wrong numbers, which is worse than not measuring.
    """

    font: str
    font_size: float
    max_width_px: float
    max_lines: int = 2
    spacing: float = 0.0
    scale_x: float = 100.0

    def fits(self, text: str) -> bool:
        return text_metrics.fits_in_lines(
            text,
            font=self.font,
            font_size=self.font_size,
            max_width_px=self.max_width_px,
            max_lines=self.max_lines,
            spacing=self.spacing,
            scale_x=self.scale_x,
        )

    def wrap(self, text: str) -> list[str]:
        return text_metrics.wrap_text(
            text,
            font=self.font,
            font_size=self.font_size,
            max_width_px=self.max_width_px,
            max_lines=self.max_lines,
            spacing=self.spacing,
            scale_x=self.scale_x,
        )

    @classmethod
    def for_preset(
        cls,
        preset: Any,
        *,
        video_width: int,
        fraction: float = text_metrics.DEFAULT_LINE_WIDTH_FRACTION,
    ) -> TextFit:
        """Build a fit from a :class:`CaptionPreset` and the output width."""
        return cls(
            font=str(getattr(preset, "font", "") or ""),
            font_size=float(getattr(preset, "font_size", 96) or 96),
            max_width_px=text_metrics.line_budget_px(video_width, fraction),
            max_lines=max(1, int(getattr(preset, "max_lines", 2) or 2)),
            spacing=float(getattr(preset, "spacing", 0) or 0),
            scale_x=float(getattr(preset, "scale_x", 100) or 100),
        )


def words_to_cues(
    words: Iterable[Word],
    # C5: three words, not five. Five words at a readable size gives long thin lines that
    # scan left-to-right like a subtitle; short-form captions are near-full-width and meant
    # to be taken in at a glance, which is what allows the larger size that comes with it.
    max_words: int = 3,
    max_gap: float = 0.6,
    max_duration: float = 3.0,
    *,
    fit: TextFit | None = None,
) -> list[Cue]:
    """Group ``words`` into readable cues.

    A new cue is started when the current cue reaches ``max_words``, spans more
    than ``max_duration`` seconds, or when the silent gap before a word exceeds
    ``max_gap`` seconds.

    ``fit`` (C6/C16) additionally breaks a cue when its text would no longer fit the frame in the
    preset's line budget, *measured* in the font that will draw it. A word count alone cannot
    decide this: three words in Anton at 96 px occupy a different width from three words in
    Archivo Black, and the same three words are a comfortable line or an overflowing one depending
    on which letters they contain. Without it the wrap below has to drop words to stay inside the
    frame, which is a caption missing its ending.
    """
    cues: list[Cue] = []
    current: list[Word] = []

    for w in words:
        if not w.text:
            continue
        if current:
            gap = w.start - current[-1].end
            span = w.end - current[0].start
            too_wide = False
            if fit is not None:
                candidate = " ".join([_word_text(word) for word in [*current, w]])
                too_wide = not fit.fits(candidate)
            if len(current) >= max_words or gap > max_gap or span > max_duration or too_wide:
                cues.append(Cue(current[0].start, current[-1].end, current))
                current = []
        current.append(w)

    if current:
        cues.append(Cue(current[0].start, current[-1].end, current))
    return cues


# --- Cue timing passes (C24 then C23) ---------------------------------------
#
# Both passes were written, tested and then never called: `cue_constraints.apply_constraints` and
# `word_spans.apply_hygiene` had no importer outside their own test modules, so three caption
# features shipped with no effect on a single rendered frame. Their tests passed because they
# exercised the functions directly. Wiring them in is what this seam exists for.
#
# The order is C24 then C23, and it is not interchangeable. C24 changes cue *windows* -- extending
# one into free time, or merging two -- and deliberately passes word spans through untouched. C23
# clamps word spans to the cue window they belong to. Run C23 first and a subsequent merge would
# discard the very boundary it clamped to.


def _cue_windows(cues: list[Cue]) -> list[cue_constraints.Cue_Window]:
    """Project cues onto the window type C24 operates on."""
    return [
        cue_constraints.Cue_Window(
            start=cue.start,
            end=cue.end,
            text=" ".join(_word_text(w) for w in cue.words),
            word_spans=tuple(_word_bounds(w) for w in cue.words),
        )
        for cue in cues
    ]


def apply_cue_constraints(
    cues: list[Cue],
    *,
    clip_duration: float | None = None,
    fit: TextFit | None = None,
    min_seconds: float | None = None,
    max_reading_rate: float | None = None,
) -> tuple[list[Cue], cue_constraints.Constraint_Report]:
    """Apply C24's legibility floors to ``cues``, returning cues rather than windows.

    ``Cue_Window`` carries ``word_spans`` precisely so a caller can map the result back: spans are
    never modified and a merge *concatenates* them, so each output window's span count says exactly
    how many of the input words it holds, in order. That is what makes the round trip exact without
    C24 needing to know what a ``Word`` is.

    Returns the input list unchanged when both floors are off, so the default path allocates nothing
    and is bit-identical (R4.10).
    """
    floor = settings.min_cue_seconds if min_seconds is None else min_seconds
    rate = settings.max_reading_rate if max_reading_rate is None else max_reading_rate
    report = cue_constraints.Constraint_Report()
    if (floor or 0) <= 0 and (rate or 0) <= 0:
        return cues, report

    populated = [cue for cue in cues if cue.words]
    if not populated:
        return cues, report

    windows, report = cue_constraints.apply_constraints(
        _cue_windows(populated),
        min_seconds=float(floor or 0.0),
        max_reading_rate=float(rate or 0.0),
        clip_end=clip_duration,
        fit=fit,
    )

    flat = [w for cue in populated for w in cue.words]
    out: list[Cue] = []
    position = 0
    for window in windows:
        count = len(window.word_spans)
        out.append(Cue(window.start, window.end, flat[position : position + count]))
        position += count
    return out, report


def apply_span_hygiene(
    cues: list[Cue], *, min_seconds: float | None = None
) -> tuple[list[Cue], word_spans.Hygiene_Report]:
    """Repair per-word spans inside each cue (C23), summing one report for the whole clip.

    Unconditional, unlike C24's floors. Reordering and de-overlapping are not preferences: a
    ``\\kf`` sweep that runs backwards, or two words lit simultaneously, is a fault. What keeps this
    safe as a default is that :func:`word_spans.apply_hygiene` returns *the caller's own objects*
    when the spans already comply, so a well-formed transcript produces an identical file and the
    parity goldens do not move. Only the floor -- which alters spans that are merely short rather
    than malformed -- is a setting, and it ships at zero.

    One summed report per clip, not one per cue: `word_spans_repaired:N` is meant to say how much of
    this clip's timing needed repair, and a marker per cue would be noise.
    """
    floor = settings.min_word_span_seconds if min_seconds is None else min_seconds
    total = word_spans.Hygiene_Report()
    out: list[Cue] = []
    mutated = False
    for cue in cues:
        if not cue.words:
            out.append(cue)
            continue
        repaired, report = word_spans.hygiene_for_cue(
            cue.words, cue.start, cue.end, min_seconds=float(floor or 0.0)
        )
        total.reordered += report.reordered
        total.deoverlapped += report.deoverlapped
        total.lengthened += report.lengthened
        total.clamped_to_cue += report.clamped_to_cue
        if report.altered:
            mutated = True
            # The cue window is left alone. Widening it to cover a repaired span is C24's job and
            # C24 has already run; doing it here would undo a decision made one pass earlier.
            out.append(Cue(cue.start, cue.end, list(repaired)))
        else:
            out.append(cue)
    return (out if mutated else cues), total


# --- ASS rendering ----------------------------------------------------------


def _ass_timestamp(seconds: float) -> str:
    """Format ``seconds`` as an ASS timestamp ``H:MM:SS.cs`` (centiseconds)."""
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis == 100:  # rounding overflow
        centis = 0
        secs += 1
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _escape(text: str) -> str:
    """Escape characters that are special in ASS dialogue text."""
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


#: The emphasis colour shared by the legacy templates and the preset path (C4).
#:
#: ASS ``&HAABBGGRR``, so this is opaque amber (R=255, G=229, B=0). It is the same value as
#: ``caption_presets.CaptionColors.highlight``; the two are spelled separately because the
#: legacy ``_caption_style`` path takes no preset, and a drift test pins them equal.
HIGHLIGHT_COLOUR = "&H0000E5FF"

# Caption position (UI value) -> ASS numpad alignment + default vertical margin.
# ASS alignments: 1-3 = bottom (left/centre/right), 4-6 = middle, 7-9 = top.
#
# C13: nine positions rather than three. The margins are the C12 safe-area figures below, so
# every position is platform-aware rather than only the three that existed.
_POSITION_ALIGN: dict[str, tuple[int, int]] = {
    "bottom": (2, 220),
    "bottom_left": (1, 220),
    "bottom_right": (3, 220),
    "center": (5, 0),
    "center_left": (4, 0),
    "center_right": (6, 0),
    "top": (8, 200),
    "top_left": (7, 200),
    "top_right": (9, 200),
}

#: Every position name a caller may pass, in a stable order for the UI.
VALID_CAPTION_POSITIONS: tuple[str, ...] = tuple(_POSITION_ALIGN)

# --------------------------------------------------------------------------- #
# C12 - platform safe areas
# --------------------------------------------------------------------------- #
# The vertical margins above were hard-coded at 220/200 and are not TikTok-aware. Every
# short-form platform draws its own chrome over the video - a caption sitting under the
# username, the caption text, the action rail or a progress bar is unreadable, and the
# creator cannot tell from the rendered file because the chrome is not in it.
#
# Expressed as a **fraction of frame height**, not pixels, because the same clip is rendered at
# 720/1080/1440/2160 (O9) and a pixel margin means a different physical inset at each. The
# figures are conservative approximations collected as data rather than a specification:
# platform UI changes without notice and differs by app version, so each is a little larger
# than the strictest measurement, since a caption slightly too high is survivable and one under
# the action rail is not.
#
# Bottom is much larger than top on every platform: that is where the caption, username and
# button rail all live.
SAFE_AREA_INSETS: dict[str, dict[str, float]] = {
    # username + caption + action rail; the tallest bottom chrome of the three.
    "tiktok": {"top": 0.09, "bottom": 0.22, "side": 0.06},
    # Reels: similar bottom stack, slightly shallower.
    "instagram": {"top": 0.08, "bottom": 0.20, "side": 0.055},
    # Shorts: title at the top, a shorter bottom bar.
    "youtube": {"top": 0.10, "bottom": 0.16, "side": 0.05},
    # No known chrome. Chosen to reproduce the pre-C12 literals *exactly* at 1080x1920 -
    # 220/200 vertical and 80 horizontal - so asking for the generic profile is provably a
    # no-op on the default frame size rather than approximately one. A first attempt used
    # rounder numbers and came out a pixel off, which is the kind of difference that shows up
    # later as an unexplained golden-file mismatch.
    "none": {"top": 200 / 1920, "bottom": 220 / 1920, "side": 80 / 1080},
}

#: What ``platform=None`` resolves to.
DEFAULT_SAFE_AREA = "none"


def safe_area_margins(
    video_width: int,
    video_height: int,
    platform: str | None = None,
) -> dict[str, int]:
    """Pixel margins keeping captions clear of a platform's own UI (C12).

    Returns ``{"top", "bottom", "side"}`` in pixels for this frame size. The ``none`` profile
    reproduces the previous hard-coded 220/200 at 1920 tall, so an unconfigured render is
    unchanged - the insets are a *choice* being made available, not a silent reframing of every
    existing clip.
    """
    profile = SAFE_AREA_INSETS.get(
        (platform or DEFAULT_SAFE_AREA).strip().lower(), SAFE_AREA_INSETS[DEFAULT_SAFE_AREA]
    )
    height = max(1, int(video_height or 1))
    width = max(1, int(video_width or 1))
    return {
        "top": int(round(height * profile["top"])),
        "bottom": int(round(height * profile["bottom"])),
        "side": int(round(width * profile["side"])),
    }


def resolve_margins(
    position: str,
    video_width: int,
    video_height: int,
    *,
    platform: str | None = None,
    offset: int = 0,
) -> tuple[int, int, int]:
    """``(margin_l, margin_r, margin_v)`` for a position (C12, C13).

    ``offset`` nudges the caption further from its edge in pixels - positive only, and only
    away from the edge. A negative offset would push text *into* the chrome the safe area
    exists to avoid, which is the one direction no caller should be able to ask for by
    accident; it is clamped rather than rejected so a stray value cannot fail a render.

    A centred caption ignores the vertical inset: ASS reads ``MarginV`` as a distance from the
    edge, and for alignments 4-6 it has no meaning. Adding one would silently do nothing, which
    is worse than not offering it.
    """
    align = _POSITION_ALIGN.get(position, _POSITION_ALIGN["bottom"])[0]
    margins = safe_area_margins(video_width, video_height, platform)
    side = margins["side"]
    offset = max(0, int(offset or 0))

    if align in (4, 5, 6):
        return side, side, 0
    edge = "top" if align in (7, 8, 9) else "bottom"
    return side, side, margins[edge] + offset


def _caption_style(
    template: str,
    position: str,
    font: str,
    font_size: int,
) -> tuple[str, int, bool]:
    """Return ``(style_line, alignment, karaoke)`` for a caption template.

    Templates:
        * ``karaoke`` — white text, amber per-word fill sweep, bold outline.
        * ``boxed``   — white text on a semi-opaque box (BorderStyle 3).
        * ``minimal`` — plain white text, thin outline, no karaoke.
    """
    align, margin_v = _POSITION_ALIGN.get(position, _POSITION_ALIGN["bottom"])

    white = "&H00FFFFFF"
    # C4: the karaoke fill was pure green (&H0000FF00), which reads as dated - it is the
    # default nobody chose. The current idiom is white sweeping to yellow or to a brand
    # colour. This is the same amber as ``CaptionColors.highlight``, so the legacy
    # templates and the preset path now agree on what an emphasised word looks like
    # instead of differing by which code path rendered them.
    highlight = HIGHLIGHT_COLOUR
    black = "&H00000000"
    box = "&H80000000"  # semi-transparent black background/shadow

    if template == "boxed":
        # BorderStyle 3 draws an opaque box behind the text (uses OutlineColour).
        style = (
            f"Style: Default,{font},{font_size},{white},{white},{box},{box},"
            f"-1,0,0,0,100,100,0,0,3,0,0,{align},80,80,{margin_v},1"
        )
        return style, align, False
    if template == "minimal":
        style = (
            f"Style: Default,{font},{font_size},{white},{white},{black},&H64000000,"
            f"-1,0,0,0,100,100,0,0,1,2,1,{align},80,80,{margin_v},1"
        )
        return style, align, False
    # Default: karaoke.
    style = (
        f"Style: Default,{font},{font_size},{white},{highlight},{black},&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,4,2,{align},80,80,{margin_v},1"
    )
    return style, align, True


# --- Preset-driven ASS spans (Feature A) ------------------------------------

# Fonts tried, in order, when a preset's declared font is unavailable (Req 5.3, C1).
#
# This used to be the single name "Arial", which is the bug C1 names: Arial is not
# installed on any Linux host, and every built-in preset *also* declared Arial, so the
# substitution branch replaced a missing font with the same missing font. It reported a
# substitution while changing nothing, and libass then metric-aliased to whatever the host
# offered - Liberation Sans Regular where fonts-liberation is installed, Noto Sans
# elsewhere - with synthesised rather than real bold. That single defect is most of the
# "captions look plain" impression.
#
# The order is preference, not availability: the first four are the bundled heavy display
# faces in ``assets/fonts`` (see ``assets/fonts.json``), each verified to resolve to
# itself under both fontconfig and libass' ``fontsdir`` provider. The last three are
# common system sans faces, so a host with no bundled directory still lands on a real
# file. ``Liberation Sans`` is terminal because it is the one face both the Dockerfile and
# CI install by name.
#
# Deliberately contains no variable font: a request for "Montserrat" through ``fontsdir``
# silently resolves to NotoSans-Bold (verified with libass at -loglevel verbose), because
# libass' directory provider does not select named instances the way fontconfig does.
# Keeping this list to static faces is what makes the terminal rung a real guarantee.
FALLBACK_FONTS: tuple[str, ...] = (
    "Anton",
    "Archivo Black",
    "Bebas Neue",
    "Poppins ExtraBold",
    "Noto Sans",
    "DejaVu Sans",
    "Liberation Sans",
)

#: The documented last rung, kept as a name because callers (and
#: ``worker.engines.kinetic.FALLBACK_FONT``) refer to "the fallback font" singular.
_FALLBACK_FONT = FALLBACK_FONTS[-1]

# Cached, best-effort lower-cased set of locally available font family names.
_FONT_CACHE: dict[str, frozenset[str] | None] = {}


def _enumerate_system_fonts() -> frozenset[str] | None:
    """Best-effort enumeration of local font families (lower-cased).

    Uses ``fc-list`` when available. Returns ``None`` when enumeration is not
    possible so callers can stay conservative and assume a font *is* available
    (we never want to falsely substitute).
    """
    if "fonts" in _FONT_CACHE:
        return _FONT_CACHE["fonts"]

    fonts: frozenset[str] | None = None
    fc = shutil.which("fc-list")
    if fc:
        try:
            proc = subprocess.run(
                [fc, ":", "family"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0 and proc.stdout:
                names: set[str] = set()
                for line in proc.stdout.splitlines():
                    for fam in line.split(","):
                        fam = fam.strip().lower()
                        if fam:
                            names.add(fam)
                fonts = frozenset(names) if names else None
        except Exception:
            fonts = None

    _FONT_CACHE["fonts"] = fonts
    return fonts


def _bundled_font_families() -> frozenset[str]:
    """Lower-cased vendored families libass can load straight from the ``fontsdir``.

    :func:`subtitles_filter` hands ``settings.font_assets_dir`` to libass, so a vendored face
    renders whether or not the host has it installed — the same reasoning
    :func:`discovered_fonts` already records for operator-supplied files. ``fc-list`` cannot
    see that directory, which is why this is a second probe rather than more entries in
    :func:`_enumerate_system_fonts`.

    **Variable faces are excluded**, matching :func:`available_fonts`: libass' directory
    provider cannot select a named instance of one, so a request for such a family silently
    resolves to something else. ``assets/fonts.json`` records ``Montserrat`` becoming
    ``NotoSans-Bold``, and it is why no variable family sits on :data:`FALLBACK_FONTS`.
    Counting them here would substitute *towards* a face that cannot be rendered.

    Scoped to the manifest on purpose. Those are the faces CI verifies are present and the
    only ones a preset may name; operator-supplied files keep their existing behaviour —
    they render through ``fontsdir`` and become visible to fontconfig after
    :func:`refresh_font_cache_if_changed`. Widening this to a directory scan would mean
    parsing font metadata on a per-clip path for no gain.

    ``FONT_MANIFEST`` is defined further down this module; the reference resolves at call
    time, so the ordering is fine.
    """
    if "bundled" in _FONT_CACHE:
        return _FONT_CACHE["bundled"] or frozenset()

    families: set[str] = set()
    fonts_dir = Path(getattr(settings, "font_assets_dir", "") or "")
    if fonts_dir.is_dir():
        try:
            entries = json.loads(FONT_MANIFEST.read_text(encoding="utf-8"))["fonts"]
        except Exception:
            entries = []  # an unreadable manifest degrades to "nothing bundled"
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("variable"):
                continue
            family = str(entry.get("family") or "").strip()
            filename = str(entry.get("file") or "").strip()
            # The manifest is a declaration; the file is what libass will open. A CI step
            # exists precisely because the two once disagreed.
            if family and filename and (fonts_dir / filename).is_file():
                families.add(family.lower())

    resolved = frozenset(families)
    _FONT_CACHE["bundled"] = resolved
    return resolved


def font_available(name: str) -> bool:
    """Return whether ``name`` is a locally available font family (best-effort).

    Conservative by design (Req 5.3): when we cannot enumerate host fonts we
    return ``True`` so a real font is never falsely substituted. When we *can*
    enumerate, an obviously-absent family reports ``False`` (this is also the
    monkeypatch point used by the font-substitution tests).

    The vendored directory is consulted first, because a face this repository ships is
    available by virtue of being shipped. Probing only fontconfig reported every vendored
    face missing wherever the Dockerfile's ``fc-cache`` had not run, and ``resolve_font``
    then substituted a font that would have rendered perfectly well — C1's failure mode one
    layer up. ``scripts/setup_dev_env.sh`` and the Dockerfile both work around it by
    installing the faces system-wide; `.github/workflows/ci.yml` does not, which is why six
    preset assertions failed there. Consulting the directory removes the need for the
    workaround rather than adding a third copy of it.
    """
    if not isinstance(name, str) or not name.strip():
        return False
    wanted = name.strip().lower()
    if wanted in _bundled_font_families():
        return True
    fonts = _enumerate_system_fonts()
    if not fonts:
        return True  # uncertain -> assume available
    return wanted in fonts


def resolve_font(
    requested: str,
    *,
    available: Any | None = None,
) -> tuple[str, bool]:
    """Resolve ``requested`` to a font that is actually installed (C1).

    Returns ``(font_to_use, substituted)``. ``substituted`` is ``True`` only when the
    returned font differs from what was asked for, so a caller can record the event.

    Walks :data:`FALLBACK_FONTS` in preference order and returns the first available
    family. When nothing on the ladder probes available the terminal rung is returned
    anyway: there is no third option at render time, and ``font_available`` is
    deliberately optimistic when it cannot enumerate host fonts, so this branch means
    enumeration *worked* and found none of them - a misconfigured image, which the
    preflight check reports separately.

    ``available`` overrides the probe (the injection point the substitution tests use).
    """
    probe = available if available is not None else font_available
    if isinstance(requested, str) and requested.strip() and probe(requested):
        return requested, False
    for candidate in FALLBACK_FONTS:
        if probe(candidate):
            return candidate, True
    return FALLBACK_FONTS[-1], True


class _Uppercased:
    """A Word-like wrapper whose text is upper-cased (C7).

    A wrapper rather than a mutation: the caller's ``Word`` objects belong to the
    transcript and are read again by the emoji planner, the keyword planner and the
    kinetic engine. Upper-casing in place would leak the caption's presentation choice
    into everything downstream that reads the same words.
    """

    __slots__ = ("_word", "text")

    def __init__(self, word: Any) -> None:
        self._word = word
        self.text = _word_text(word).upper()

    def __getattr__(self, name: str) -> Any:
        # start/end/probability and anything else come from the wrapped word.
        return getattr(self._word, name)


def _uppercased(word: Any) -> Any:
    """``word`` with upper-cased text, or the word unchanged when it has none."""
    if not _word_text(word):
        return word
    return _Uppercased(word)


def _word_text(word: Any) -> str:
    """Best-effort extraction of a Word-like object's text (or a bare string)."""
    if isinstance(word, str):
        return word
    return str(getattr(word, "text", "") or "")


def _word_bounds(word: Any) -> tuple[float, float]:
    """Return ``(start, end)`` for a Word-like object (end defaults to start)."""
    try:
        start = float(getattr(word, "start", 0.0) or 0.0)
    except (TypeError, ValueError):
        start = 0.0
    try:
        end = float(getattr(word, "end", start) or start)
    except (TypeError, ValueError):
        end = start
    if end < start:
        end = start
    return start, end


def build_word_span(
    word: Any,
    preset: CaptionPreset,
    highlighted: bool,
    *,
    cue_start: float = 0.0,
) -> str:
    """Return the ASS-tagged text span for a single ``word`` (pure).

    Emits **libass ASS tags only** (never ``drawtext``, Req 2.3). The animation
    span is chosen from ``preset.animation``:

    * ``pop`` — a scale ramp ``{\\fscx60\\fscy60\\t(rel,rel+120,\\fscx100\\fscy100)}``
      anchored to the word's ``start`` relative to the cue (Req 2.1).
    * ``typewriter`` — a per-word alpha reveal
      ``{\\alpha&HFF&\\t(rel,rel+30,\\alpha&H00&)}`` (Req 2.2).
    * ``karaoke_fill`` — the legacy ``{\\kfNN}`` fill sweep (NN = word duration
      in centiseconds), preserving existing karaoke behaviour (Req 1.1).
    * ``none`` — the plain escaped word.

    When ``highlighted`` the span is wrapped in a distinct colour + scale span
    (``preset.colors.highlight`` / ``preset.highlight_scale``) composed *around*
    the animation span, so both apply while the word's spoken timing is left
    unchanged (Reqs 3.1, 3.5). Word text is escaped via :func:`_escape`.
    """
    text = _word_text(word)
    if getattr(settings, "caption_mask_profanity", False):
        # C22: applied to the word's *text* only. Timings, emphasis selection and emoji lookup
        # all read the original word, so masking changes what is drawn and nothing about when or
        # how - a masked word must not become a different word to the rest of the pipeline.
        text = mask_profanity(text)
    escaped = _escape(text)
    animation = getattr(preset, "animation", "none")
    w_start, w_end = _word_bounds(word)
    rel_ms = max(0, int(round((w_start - cue_start) * 1000)))

    if animation == "pop":
        span = f"{{\\fscx60\\fscy60\\t({rel_ms},{rel_ms + 120},\\fscx100\\fscy100)}}{escaped}"
    elif animation == "typewriter":
        span = f"{{\\alpha&HFF&\\t({rel_ms},{rel_ms + 30},\\alpha&H00&)}}{escaped}"
    elif animation == "karaoke_fill":
        dur_cs = max(1, int(round((w_end - w_start) * 100)))
        span = f"{{\\kf{dur_cs}}}{escaped}"
    else:
        span = escaped

    # C10: a punch on the active word, independent of the animation style. Applied *before* the
    # highlight wrap so the highlight's own scale still wins on an emphasised word - two
    # competing \fscx spans on one word would otherwise fight, and which one applied would
    # depend on tag order rather than on intent.
    if not highlighted and animation != "pop":
        punch = _punch_span(preset, rel_ms)
        if punch:
            span = f"{punch}{span}{{\\fscx100\\fscy100}}"

    # C9: the pill goes on *before* the highlight wrap, so an emphasised word ends up as
    # highlight(pill(animation)) rather than pill(highlight(animation)).
    #
    # Both orders render acceptably - the pill sets `\3c` (border colour) and the highlight sets
    # `\c` (fill), so they do not contest the same attribute. The order matters for a different
    # reason: the documented contract is that a highlight only *wraps* the span a plain word would
    # produce, which the property test checks by substring. With the pill outermost that stops being
    # literally true, and a contract enforced by substring is a contract that has to stay
    # syntactically true, not merely true in spirit.
    pill = _word_pill_span(preset)
    if pill:
        span = f"{pill[0]}{span}{pill[1]}"

    if highlighted:
        colors = getattr(preset, "colors", None)
        highlight = getattr(colors, "highlight", "&H0000E5FF")
        primary = getattr(colors, "primary", "&H00FFFFFF")
        scale = int(round(float(getattr(preset, "highlight_scale", 1.18)) * 100))
        span = (
            f"{{\\c{highlight}&\\fscx{scale}\\fscy{scale}}}"
            f"{span}"
            f"{{\\c{primary}&\\fscx100\\fscy100}}"
        )
    elif _is_doubted(word, preset):
        # T7: a word the model barely guessed at is dimmed rather than asserted. Applied only
        # when the word is *not* highlighted: emphasis and doubt are contradictory claims, and
        # a word that earned emphasis has already been judged worth stating.
        span = f"{{{_dim_alpha_tag(preset)}}}{span}{{\\alpha&H00&}}"
    return span


def _word_pill_span(preset: CaptionPreset) -> tuple[str, str] | None:
    """The ``(open, close)`` override pair drawing a pill behind one word (C9), or ``None``.

    A thick border in a solid colour, not a drawn rectangle. ASS has no per-word background, and a
    real box would need the rendered text width, which is not known where these tags are emitted.
    A heavy border hugs the glyphs instead - which is closer to the reference look than a rectangle
    would be, and is why the parameter is a *scale* of the font size rather than a pixel padding.
    """
    try:
        strength = float(getattr(preset, "word_pill", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if strength <= 0:
        return None

    colors = getattr(preset, "colors", None)
    fill = str(getattr(preset, "word_pill_color", "") or "") or str(
        getattr(colors, "highlight", "&H0000E5FF")
    )
    # Scaled from the font size so one preset works at every output resolution (O9 renders 720 to
    # 2160), and capped: past a certain thickness adjacent words' pills merge into a bar.
    size = float(getattr(preset, "font_size", 96) or 96)
    width = max(1, min(40, int(round(size * min(0.35, strength) * 0.25))))

    restore_border = max(0, int(getattr(preset, "outline", 0) or 0))
    restore_colour = str(getattr(colors, "outline", "&H00000000"))
    return (
        f"{{\\bord{width}\\3c{fill}&}}",
        f"{{\\bord{restore_border}\\3c{restore_colour}&}}",
    )


# --------------------------------------------------------------------------- #
# C22 - profanity masking
# --------------------------------------------------------------------------- #
# Off by default. Burned captions are permanent, so masking is a publishing decision - a
# creator whose whole voice is profane would be censored by their own tool - but a creator
# posting to a brand account or a platform that demotes profanity has no way to comply short of
# re-recording.
#
# The list is deliberately short and covers the words platforms actually act on. A long list
# starts catching the Scunthorpe problem, and the cost of a false positive here is a masked
# word the speaker did say innocently, which reads as a rendering fault rather than a policy.
_PROFANITY: frozenset[str] = frozenset(
    {
        "fuck",
        "fucking",
        "fucked",
        "fucker",
        "motherfucker",
        "shit",
        "shitty",
        "bullshit",
        "cunt",
        "cock",
        "dick",
        "bitch",
        "bastard",
        "asshole",
        "arsehole",
        "whore",
        "slut",
        "nigger",
        "faggot",
        "retard",
        "retarded",
    }
)

#: What a masked character becomes.
MASK_CHARACTER = "*"

_WORD_CHARS = re.compile(r"[^\W\d_]", re.UNICODE)


def is_profane(text: str) -> bool:
    """Whether ``text``, stripped of punctuation, is on the mask list (C22).

    Matches whole words only. Substring matching is what produces "Scunthorpe" and
    "classic" - a masked word inside an innocent one is far more conspicuous than an unmasked
    profanity, because the viewer can see the tool got it wrong.
    """
    stripped = "".join(ch for ch in (text or "").lower() if _WORD_CHARS.match(ch))
    return stripped in _PROFANITY


def mask_profanity(text: str) -> str:
    """Mask a profane word, keeping its first letter and its punctuation (C22).

    ``fucking!`` becomes ``f******!``. The first letter and the length stay, because the point
    is that the viewer can follow the sentence - a fully blanked word makes the caption
    unreadable, which defeats having captions at all.

    Non-profane text is returned unchanged and identically, so this is safe to apply to every
    word rather than only to matches.
    """
    if not is_profane(text):
        return text
    out = []
    seen_letter = False
    for ch in text:
        if _WORD_CHARS.match(ch):
            if seen_letter:
                out.append(MASK_CHARACTER)
            else:
                out.append(ch)
                seen_letter = True
        else:
            out.append(ch)
    return "".join(out)


#: Path to the vendored-font manifest.
FONT_MANIFEST = Path(__file__).resolve().parent.parent / "assets" / "fonts.json"


#: Font-file suffixes libass' ``fontsdir`` provider will load.
_FONT_SUFFIXES: tuple[str, ...] = (".ttf", ".otf", ".ttc")

#: OS/2 ``usWeightClass`` to the fontconfig weight scale, which is what ``assets/fonts.json``
#: records and what libass prints in its ``fontselect:`` line.
#:
#: Two scales for one concept, and mixing them is a silent bug rather than an error: a font file
#: says 700 for bold, fontconfig says 200, and emitting the file's number in the same ``weight``
#: field the manifest populates would make a user-supplied regular face (400) look nearly twice as
#: heavy as a vendored black one (210). Values follow fontconfig's own ``fcweight.c`` table.
_FC_WEIGHTS: tuple[tuple[int, int], ...] = (
    (100, 0),
    (200, 40),
    (300, 50),
    (350, 55),
    (380, 75),
    (400, 80),
    (500, 100),
    (600, 180),
    (700, 200),
    (800, 205),
    (900, 210),
    (1000, 215),
)

#: The fontconfig weight at and above which a face is a heavy display face.
#:
#: 200 is fontconfig BOLD, and the vendored manifest marks Poppins Bold (200) as heavy - so this
#: matches the existing declaration rather than inventing a second threshold.
_HEAVY_FC_WEIGHT = 200


def _fc_weight(os2_weight: int) -> int:
    """``usWeightClass`` on fontconfig's scale, interpolated between the table's rungs."""
    if os2_weight <= 0:
        return 0
    previous_os2, previous_fc = _FC_WEIGHTS[0]
    for os2, fc in _FC_WEIGHTS:
        if os2_weight <= os2:
            if os2 == previous_os2:
                return fc
            span = (os2_weight - previous_os2) / (os2 - previous_os2)
            return int(round(previous_fc + span * (fc - previous_fc)))
        previous_os2, previous_fc = os2, fc
    return _FC_WEIGHTS[-1][1]


def _font_identity(path: Path) -> dict | None:
    """The family name, style and weight recorded *inside* the font file (A5).

    Read from the file rather than derived from its filename, and that distinction is the whole
    reason this function exists. libass and fontconfig both select a face by the family name in
    its ``name`` table; a picker that offered ``MyBrandFont.ttf`` as "MyBrandFont" would be
    offering a name that resolves to nothing, and libass answers an unknown family by silently
    substituting another face. That is exactly the C1 defect - the one this codebase has already
    shipped once - so a font whose real name cannot be read is *not offered*.

    Returns ``None`` for an unreadable file, and for a variable font: ``fontsdir`` cannot select a
    named instance of one, so it would substitute too.
    """
    handle = None
    try:
        from fontTools.ttLib import TTFont

        # Opened here rather than by path so the descriptor is closed even when ``TTFont`` raises
        # part-way through construction - it takes ownership of the file only once it succeeds, so
        # ``with TTFont(path)`` leaks one descriptor per unreadable file. A long-running server
        # scanning a directory containing one bad font would leak on every request.
        handle = open(path, "rb")
        with TTFont(handle, lazy=True, fontNumber=0) as font:
            if "fvar" in font:
                return None
            name_table = font["name"]
            # 16/17 are the typographic family/subfamily and are what a face with more than four
            # weights records; 1/2 are the legacy pair every font has. Preferring the typographic
            # name matters for families like Poppins, where the legacy family collapses nine
            # weights into "Poppins" plus a style nobody can select.
            family = name_table.getDebugName(16) or name_table.getDebugName(1)
            style = name_table.getDebugName(17) or name_table.getDebugName(2) or ""
            weight = 0
            if "OS/2" in font:
                weight = int(getattr(font["OS/2"], "usWeightClass", 0) or 0)
        family = str(family or "").strip()
        if not family:
            return None
        style = str(style).strip()
        # libass matches on the full name for a non-regular style, so a bold-only file has to be
        # offered as "Family Bold" or a request for "Family" lands on a synthesised bold.
        name = family if style.lower() in ("", "regular") else f"{family} {style}"
        return {
            "name": name,
            "family": family,
            "style": style,
            # Converted, not passed through: see ``_FC_WEIGHTS``.
            "weight": _fc_weight(weight),
        }
    except Exception:
        logger.debug("A5: could not read font identity from %s", path, exc_info=True)
        return None
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def discovered_fonts(manifest_files: frozenset[str] = frozenset()) -> list[dict]:
    """Caption faces the operator dropped into ``font_assets_dir`` themselves (A5).

    A **server-side directory**, not an upload endpoint. The same reasoning U6 recorded for the
    brand logo: an upload needs a storage location, a cleanup policy and a retention rule, none
    of which exist for assets, and inventing three of them to accept a TTF is a larger decision
    than the feature. Copying a file into a directory the operator already controls needs none.

    No ``fc-cache`` run is required for these to *render*: the renderer passes ``font_assets_dir``
    to libass as ``fontsdir``, which reads the directory directly. :func:`refresh_font_cache`
    exists for the fontconfig-mediated paths (ffmpeg's ``drawtext``, and anything outside libass),
    and is best-effort because a host without ``fc-cache`` is not a broken host.

    Fonts already named in the manifest are skipped, so a vendored face keeps its verified
    licence and ``use`` metadata rather than being re-derived.
    """
    directory = Path(getattr(settings, "font_assets_dir", "") or "")
    if not directory.is_dir():
        return []

    found: list[dict] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _FONT_SUFFIXES:
            continue
        if path.name in manifest_files:
            continue
        identity = _font_identity(path)
        if identity is None:
            continue
        found.append(
            {
                **identity,
                # Read from the file's own weight class rather than guessed from its name, because
                # "Black" and "Heavy" appear in family names that are not.
                #
                # Stated limitation: a *display* face often declares OS/2 weight 400 even though it
                # draws as heavy - Anton does exactly this, which is why the vendored manifest marks
                # it heavy by hand. A dropped-in display face will therefore read as not-heavy, and
                # only affects picker ordering. Guessing from the filename instead would be wrong in
                # a way that is harder to notice.
                "heavy": identity["weight"] >= _HEAVY_FC_WEIGHT,
                # Blank rather than invented: the operator supplied this file, so its licence is
                # theirs to know. A vendored face carries a verified SPDX id; claiming one here
                # would be a claim this code cannot check.
                "license": "",
                "use": "user-supplied",
                "source": "user",
            }
        )
    return found


#: Last-seen mtime of the font directory, so the fontconfig refresh runs on a change and not
#: on every request.
_FONT_DIR_STATE: dict[str, float] = {}


def refresh_font_cache_if_changed() -> bool:
    """Refresh the fontconfig cache only when the font directory has changed (A5).

    ``available_fonts`` is what the options endpoint calls on every page load, and that is also
    exactly the moment a newly dropped-in file should become visible - so the registration belongs
    here rather than in a startup hook a long-running server never runs again. Gated on the
    directory's mtime so the common case is one ``stat``, not one subprocess per request.
    """
    directory = Path(getattr(settings, "font_assets_dir", "") or "")
    try:
        mtime = directory.stat().st_mtime
    except OSError:
        return False
    if _FONT_DIR_STATE.get("mtime") == mtime:
        return False
    _FONT_DIR_STATE["mtime"] = mtime
    return refresh_font_cache()


def refresh_font_cache() -> bool:
    """Best-effort ``fc-cache -f`` over the bundled font directory (A5).

    Returns whether it ran. Not needed for caption rendering - libass reads ``fontsdir``
    directly - but ffmpeg's ``drawtext`` and any non-libass consumer resolve through fontconfig,
    which caches its directory scan. Called from the capability/options endpoint rather than per
    render: it costs a subprocess and the answer only changes when a file is added.
    """
    fc = shutil.which("fc-cache")
    directory = Path(getattr(settings, "font_assets_dir", "") or "")
    if not fc or not directory.is_dir():
        return False
    try:
        subprocess.run(
            [fc, "-f", str(directory)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("A5: fc-cache refresh failed for %s", directory, exc_info=True)
        return False
    # Both font caches are now stale: a newly registered face would otherwise be reported
    # unavailable until the process restarted. ``bundled`` is cleared as well because a
    # manifest face that was missing from disk when it was first computed (a partial
    # checkout, or a vendored file restored afterwards) would stay absent from it.
    _FONT_CACHE.pop("fonts", None)
    _FONT_CACHE.pop("bundled", None)
    return True


def available_fonts() -> list[dict]:
    """The caption faces a picker may offer: vendored (A4) plus operator-supplied (A5).

    Twelve faces are vendored with licences and a manifest, and nothing exposed them - so the
    only way to change a caption font was to edit a preset in source, and the assets might as
    well not have been shipped.

    Returns the *usable* subset with only the fields a picker needs. Variable fonts are excluded
    because libass' ``fontsdir`` provider cannot select a named instance of one - a request for
    such a family silently resolves to something else, which is exactly the C1 defect. Offering
    a font that will not render is worse than offering fewer.

    Never raises: a missing or malformed manifest still yields whatever was discovered on disk,
    and the caller falls back to whatever the presets already name.
    """
    # A5: register any newly dropped-in file with fontconfig before reporting it, so a face this
    # call is about to offer is one the non-libass consumers can also resolve.
    refresh_font_cache_if_changed()

    entries: list = []
    try:
        data = json.loads(FONT_MANIFEST.read_text(encoding="utf-8"))
        entries = data.get("fonts") or []
    except Exception:
        entries = []

    fonts: list[dict] = []
    manifest_files: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("variable"):
            continue
        name = str(entry.get("name") or "").strip()
        filename = str(entry.get("file") or "").strip()
        if not name or not filename:
            continue
        manifest_files.add(filename)
        # Only faces actually present on disk: the manifest is a declaration, and a CI step
        # exists precisely because the two once disagreed.
        if not (FONT_MANIFEST.parent / "fonts" / filename).is_file():
            continue
        fonts.append(
            {
                "name": name,
                "family": str(entry.get("family") or name),
                "style": str(entry.get("style") or ""),
                "weight": int(entry.get("weight") or 0),
                "heavy": bool(entry.get("heavy_face")),
                "license": str(entry.get("license") or ""),
                "use": str(entry.get("use") or ""),
                "source": "bundled",
            }
        )

    # A5: the manifest wins on a name collision. It carries a verified licence and a `use` note,
    # and the vendored file is the one CI checks resolves to itself under both providers - so a
    # dropped-in file with the same family name must not quietly replace that guarantee.
    known = {font["name"].lower() for font in fonts}
    for font in discovered_fonts(frozenset(manifest_files)):
        if font["name"].lower() in known:
            continue
        known.add(font["name"].lower())
        fonts.append(font)

    return sorted(fonts, key=lambda f: (not f["heavy"], f["name"]))


def _punch_span(preset: CaptionPreset, rel_ms: int) -> str:
    """The opening ASS override for C10's active-word punch, or ``""`` when disabled.

    Ramps *down* from the punched size to 100, so the word arrives large and settles - the
    accent lands on the syllable being spoken. Ramping up would peak after the word had already
    been said, which reads as lag rather than as emphasis.
    """
    try:
        amount = float(getattr(preset, "punch_scale", 0.0) or 0.0)
    except (TypeError, ValueError):
        return ""
    if amount <= 0.0:
        return ""
    # Clamped: an unbounded value would push glyphs outside the frame, and a negative one would
    # mirror the text.
    amount = min(1.0, amount)
    try:
        length = max(10, int(getattr(preset, "punch_ms", 110) or 110))
    except (TypeError, ValueError):
        length = 110
    peak = int(round((1.0 + amount) * 100))
    return f"{{\\fscx{peak}\\fscy{peak}\\t({rel_ms},{rel_ms + length},\\fscx100\\fscy100)}}"


def _is_doubted(word: Any, preset: CaptionPreset) -> bool:
    """Whether ``word`` fell below the preset's confidence floor (T7).

    A word carrying no probability reads as *confident*, not as doubtful. ``_word_probability``
    already returns 1.0 for those, and the alternative - treating "unknown" as "unsure" -
    would dim every caption on any transcript without per-word confidence, which is the
    failure mode C11 had for the same reason.
    """
    try:
        threshold = float(getattr(preset, "low_confidence_threshold", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if threshold <= 0.0:
        return False
    try:
        probability = float(getattr(word, "probability", 1.0))
    except (AttributeError, TypeError, ValueError):
        return False
    if probability != probability:  # NaN
        return False
    return probability < threshold


def _dim_alpha_tag(preset: CaptionPreset) -> str:
    """The ASS alpha override for a doubted word (T7).

    ASS alpha runs the opposite way to opacity: ``&H00`` is fully opaque and ``&HFF`` fully
    transparent. Getting that backwards would make a doubted word *more* prominent than a
    confident one, which is exactly the wrong outcome and would still look deliberate.
    """
    try:
        opacity = float(getattr(preset, "low_confidence_alpha", 0.55))
    except (TypeError, ValueError):
        opacity = 0.55
    opacity = max(0.0, min(1.0, opacity))
    transparency = int(round((1.0 - opacity) * 255))
    return f"\\alpha&H{transparency:02X}&"


# --- In-caption emoji (independent of the overlay emoji effect, Req 4.2) ----

# A compact keyword -> emoji glyph map for inline caption emoji. Kept local so
# in-caption emoji stays functionally independent of the overlay emoji effect.
_CAPTION_EMOJI: dict[str, str] = {
    "love": "\u2764\ufe0f",
    "heart": "\u2764\ufe0f",
    "amazing": "\U0001f929",
    "wow": "\U0001f62e",
    "money": "\U0001f4b0",
    "cash": "\U0001f4b5",
    "rich": "\U0001f911",
    "fire": "\U0001f525",
    "hot": "\U0001f525",
    "best": "\U0001f3c6",
    "win": "\U0001f3c6",
    "winner": "\U0001f3c6",
    "success": "\U0001f4c8",
    "growth": "\U0001f4c8",
    "idea": "\U0001f4a1",
    "smart": "\U0001f9e0",
    "brain": "\U0001f9e0",
    "crazy": "\U0001f92f",
    "insane": "\U0001f92f",
    "mind": "\U0001f9e0",
    "laugh": "\U0001f602",
    "funny": "\U0001f602",
    "happy": "\U0001f604",
    "sad": "\U0001f622",
    "boom": "\U0001f4a5",
    "rocket": "\U0001f680",
    "power": "\U0001f4aa",
    "goal": "\U0001f3af",
    "target": "\U0001f3af",
    "secret": "\U0001f92b",
    "yes": "\u2705",
    "no": "\u274c",
    "star": "\u2b50",
    "party": "\U0001f389",
}

_EMOJI_KEY_RE = re.compile(r"[a-z']+")


def _norm_emoji_key(text: str) -> str:
    """Lower-case and strip a token to its first alphabetic run for lookup."""
    m = _EMOJI_KEY_RE.findall((text or "").lower())
    return m[0] if m else ""


def caption_emoji_glyph(
    word: Any,
    preset: CaptionPreset,
    *,
    permissible: bool = False,
    glyph_available: Any | None = None,
    downloader: Any | None = None,
) -> str:
    """Return an inline emoji glyph for ``word``'s keyword, or ``""`` to drop.

    Only active when ``preset.emoji_inline`` (Req 4.1). Behaviour:

    * A glyph the active font cannot render is dropped while surrounding words
      are retained (Req 4.3) — decided by ``glyph_available(glyph)``.
    * Under ``permissible`` (Permissibility_Mode) only locally-available glyphs
      are used and **no external download is ever attempted** (Req 4.4). This
      path is font-glyph only and kept independent of the overlay emoji effect
      (Req 4.2); the injectable ``downloader`` is accepted purely so tests can
      assert it is never called.
    """
    if not getattr(preset, "emoji_inline", False):
        return ""
    glyph = _CAPTION_EMOJI.get(_norm_emoji_key(_word_text(word)), "")
    if not glyph:
        return ""
    check = glyph_available if glyph_available is not None else (lambda _g: True)
    # In-caption emoji are rendered as font glyphs; we never download here, so a
    # supplied ``downloader`` spy stays untouched under every mode.
    return glyph if check(glyph) else ""


# --- Preset-driven style line ------------------------------------------------

#: The weight at or above which a face is taken to supply its own bold (C3).
#:
#: ASS expresses weight as a single on/off flag, which libass turns into a request for
#: fontconfig weight 200 ("Bold", CSS 700). Every bundled display face is already at least
#: that heavy, so asking for bold on top makes libass *synthesise* the emboldening on a
#: face that was drawn heavy - visible as slightly swollen, soft-edged glyphs.
_FACE_SUPPLIES_BOLD = 700


def ass_bold_flag(preset: CaptionPreset) -> int:
    """The ASS ``Bold`` field for ``preset``: ``0`` to leave the face alone, ``-1`` to bold.

    Verified against libass at ``-loglevel verbose``, which reports the weight it asked
    fontconfig for: ``-1`` produces ``fontselect: (Anton, 700, 0)`` and ``0`` produces
    ``fontselect: (Anton, 400, 0)``. Both resolve to ``Anton-Regular`` because that is the
    only face in the family - the difference is that the first one then emboldens it.
    """
    return 0 if int(getattr(preset, "font_weight", 0)) >= _FACE_SUPPLIES_BOLD else -1


#: Glyph-scale bounds. Below the minimum text is unreadable; above the maximum glyphs leave the
#: frame. Both look like a rendering fault rather than a bad setting, which is why they are
#: clamped rather than passed through.
MIN_GLYPH_SCALE = 10
MAX_GLYPH_SCALE = 400


def _glyph_scale(value: Any) -> int:
    """Coerce a preset's ScaleX/ScaleY to a usable percentage (C15).

    ``0`` and ``None`` mean "unset" and resolve to 100, rather than being clamped to the
    minimum: a caller who writes 0 means "leave the metrics alone", and silently rendering
    their captions at a tenth of width would be a strange reading of that. Genuinely
    out-of-range values *are* clamped.
    """
    try:
        scale = int(value)
    except (TypeError, ValueError):
        return 100
    if scale == 0:
        return 100
    return max(MIN_GLYPH_SCALE, min(MAX_GLYPH_SCALE, abs(scale)))


def _preset_style_line(
    preset: CaptionPreset,
    font: str,
    font_size: int,
    align: int,
    margin_v: int,
    margin_h: int = 80,
) -> str:
    """Build the ``Style: Default`` line from a :class:`CaptionPreset`.

    Colours, border style, font and size all come from the preset (Req 5.1).
    ``font`` is the already-resolved (post-substitution) family name.
    """
    colors = preset.colors
    primary = colors.primary
    secondary = colors.highlight  # \kf sweeps Secondary -> Primary for karaoke
    if preset.border_style == 3:
        outline_col = colors.box
        back_col = colors.box
    else:
        outline_col = colors.outline
        back_col = "&H64000000"
    # C8: both come from the preset now. They used to be derived from the animation style
    # (4/2 for karaoke_fill, 2/1 otherwise), which meant a preset could not ask for a
    # heavier treatment and a 2-unit outline at PlayRes 1920 was effectively invisible.
    outline_w = max(0, int(preset.outline))
    shadow = max(0, int(preset.shadow))
    # C17: a second, wider stroke in its own colour - the "3D"/sticker edge.
    #
    # ASS carries one border width and one border colour, so a genuine dual stroke needs the text
    # drawn twice. The shadow slot is repurposed instead: at offset 0 with its own colour it renders
    # as an outer stroke around the inner one, giving the two-tone edge in a single event. It
    # replaces the shadow when set, which is the honest trade and why it is opt-in - a preset cannot
    # have both a drop shadow and an outer stroke this way.
    outline2 = max(0, int(getattr(preset, "outline2", 0) or 0))
    if outline2:
        shadow = outline2
        back_col = str(getattr(preset, "outline2_color", "&H00000000") or "&H00000000")
    # C15: previously the literals 100,100,0 - identical metrics for every preset whatever face
    # it named. Clamped rather than trusted: ScaleX of 0 makes text invisible and a huge value
    # pushes glyphs off frame, and both would look like a rendering bug rather than a bad value.
    scale_x = _glyph_scale(getattr(preset, "scale_x", 100))
    scale_y = _glyph_scale(getattr(preset, "scale_y", 100))
    try:
        spacing = int(getattr(preset, "spacing", 0) or 0)
    except (TypeError, ValueError):
        spacing = 0
    return (
        f"Style: Default,{font},{font_size},{primary},{secondary},{outline_col},"
        f"{back_col},{ass_bold_flag(preset)},0,0,0,{scale_x},{scale_y},{spacing},0,"
        f"{preset.border_style},"
        f"{outline_w},{shadow},{align},{margin_h},{margin_h},{margin_v},1"
    )


def _face_aware_position(
    position: str | None,
    *,
    preset: CaptionPreset | None,
    clip_path: Any | None,
    face_boxes: Any | None,
    video_width: int,
    video_height: int,
    font_size: int,
    notes: list[str] | None,
) -> str | None:
    """The caption position with V15's mouth-avoidance applied, or ``position`` untouched.

    Returns the *original* argument -- ``None`` included -- whenever nothing moves, rather than
    resolving it to a concrete name. That distinction is load-bearing: ``None`` means "use the
    preset's own position", and substituting the resolved name would make every preset render
    ignore a later preset change while producing an identical file today. A byte-identical default
    path is what lets the v0.8.0 parity goldens still detect an accidental change.

    Requires media to reason about, so it declines when given neither a clip nor boxes. That is not
    a silent failure: `build_ass` is called by `caption_preview` with no media at all, and a preview
    that moved its captions for faces the real render might place differently would be a preview of
    something else.
    """
    if not getattr(settings, "caption_avoid_faces", False):
        return position
    if clip_path is None and face_boxes is None:
        return position

    requested = (
        position if position is not None else (preset.position if preset is not None else "bottom")
    )

    # The margin V15 reasons about has to be the margin the style header will actually emit, or the
    # two disagree and a collision is either missed or invented. Computed only when C12/C13 are
    # configured, mirroring `_preset_header_styles` -- unconditionally resolving margins would also
    # change the numbers on every existing render, which is why that function guards it too.
    safe_area = (getattr(settings, "caption_safe_area", "") or "") or None
    caption_offset = int(getattr(settings, "caption_offset_px", 0) or 0)
    margin_px: int | None = None
    if safe_area or caption_offset:
        _margin_l, _margin_r, margin_px = resolve_margins(
            requested,
            video_width,
            video_height,
            platform=safe_area,
            offset=caption_offset,
        )

    plan = caption_placement.plan_for_clip(
        clip_path,
        requested=requested,
        frame_height=video_height,
        font_size=font_size,
        face_boxes=face_boxes,
        margin_px=margin_px,
    )
    if plan.marker and notes is not None and plan.marker not in notes:
        notes.append(plan.marker)
    return plan.position if plan.moved else position


def build_ass(
    cues: list[Cue],
    dest: str | Path,
    video_width: int = 1080,
    video_height: int = 1920,
    # Legacy (non-preset) path default. "Arial" here had the same problem as the preset
    # default: never installed, so callers relying on the default got a host substitution.
    font: str = "Poppins ExtraBold",
    font_size: int = 84,
    template: str = "karaoke",
    position: str | None = None,
    hook_text: str = "",
    hook_duration: float = 2.5,
    hook_font_size: int = 110,
    karaoke: bool | None = None,
    *,
    preset: CaptionPreset | None = None,
    keyword_indices: set[int] | None = None,
    clip_duration: float | None = None,
    permissibility: bool = False,
    emoji_glyph_available: Any | None = None,
    emoji_downloader: Any | None = None,
    notes: list[str] | None = None,
    language: str = "",
    clip_path: Any | None = None,
    face_boxes: Any | None = None,
) -> Path:
    """Render ``cues`` (and an optional hook title) to an ASS file at ``dest``.

    Args:
        cues: Caption cues (may be empty to render only a hook title).
        template: Caption look — ``karaoke`` | ``boxed`` | ``minimal``.
        position: ``bottom`` | ``center`` | ``top`` (caption placement).
        hook_text: When non-empty, a large hook title is shown at the top for
            ``hook_duration`` seconds with a fade in/out.
        karaoke: Force karaoke on/off; defaults to the template's behaviour.

    When ``preset`` is supplied the caption look (animation, font, colours,
    border style, default position) is driven by the preset (Reqs 1.1, 2.x, 5.x)
    and ``keyword_indices`` (a set of *flat* word indices across all cues,
    aligned with :func:`worker.effects.caption_presets.plan_keywords`) selects
    which words are highlighted. A ``position`` other than ``None`` overrides the
    preset's default placement (Req 5.2). When ``preset is None`` the legacy
    ``template`` behaviour is preserved unchanged.

    Colours use ASS ``&HAABBGGRR`` notation. When karaoke is active each word
    gets a ``\\kf`` fill tag so the highlight sweeps word-by-word.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # C24 then C23, before anything reads the cues.
    #
    # Here rather than in the compositor because this is the one place both the preset and the
    # legacy `template` branch pass through, so a single call covers every burned-in caption
    # including `rerender`'s. It also already owns `notes`, which is how a caption-stage marker
    # reaches the clip record.
    #
    # `fit` is built twice on the preset path -- once here for C24's merge budget, once below for
    # C6's line breaks. Deliberate: a merge that overflows the line trades an unreadable cue for a
    # truncated one, so the same measured budget has to gate both, and threading one object through
    # would mean computing it on the legacy path too, where there is no preset to measure.
    constraint_fit = TextFit.for_preset(preset, video_width=video_width) if preset else None
    cues, constraint_report = apply_cue_constraints(
        cues, clip_duration=clip_duration, fit=constraint_fit
    )
    cues, hygiene_report = apply_span_hygiene(cues)
    if notes is not None:
        for marker in [*constraint_report.markers, *hygiene_report.markers]:
            if marker not in notes:
                notes.append(marker)

    # V15: move the caption off the speaker's mouth, if that is where it lands.
    #
    # After C24/C23 because the cue shape is settled by then, and before the style header below
    # because that is what consumes `position` to emit Alignment and MarginV. Reassigning the one
    # variable here therefore covers the preset branch and the legacy `template` branch together --
    # the same argument that put the cue-timing passes in this function.
    position = _face_aware_position(
        position,
        preset=preset,
        clip_path=clip_path,
        face_boxes=face_boxes,
        video_width=video_width,
        video_height=video_height,
        # The size the text will actually be drawn at. `font_size` is the legacy path's parameter and
        # is ignored on the preset path, where the preset carries its own -- measuring the band with
        # the wrong one would misjudge the caption's height by whatever the two happen to differ by.
        font_size=(preset.font_size if preset is not None else font_size),
        notes=notes,
    )

    # C21: pick a font that can actually render what was said, and note it when nothing can.
    #
    # Done here rather than in the caller because this is the only place that has both the text and
    # the font in one scope. The default path is untouched: Latin text returns the requested font
    # with no marker and `WrapStyle: 2`, so an English render is byte-identical.
    # The hook title is included, not just the cues: a clip captioned in one script with a hook in
    # another is unusual, but a hook-only render (cues empty) is not, and it would otherwise be
    # planned from an empty string.
    script_plan = script_support.plan_for_text(
        " ".join(
            [word.text for cue in cues for word in cue.words] + ([hook_text] if hook_text else [])
        ),
        (preset.font if preset is not None else font),
    )
    if script_plan.marker and notes is not None and script_plan.marker not in notes:
        notes.append(script_plan.marker)
    if preset is not None and script_plan.font and script_plan.font != preset.font:
        # `replace` rather than mutation: presets are frozen and shared, so writing to one would
        # change the font for every later clip in the job.
        preset = dataclass_replace(preset, font=script_plan.font)
    elif preset is None and script_plan.font:
        font = script_plan.font

    if preset is not None:
        style_line, hook_style = _preset_header_styles(
            preset,
            position,
            hook_font_size,
            notes,
            video_width=video_width,
            video_height=video_height,
            # C12/C13: both inert unless configured, so an unconfigured render is byte-identical.
            safe_area=(getattr(settings, "caption_safe_area", "") or "") or None,
            caption_offset=int(getattr(settings, "caption_offset_px", 0) or 0),
        )
        # C6/C16: measure in the font that will draw the text, at the frame's real width.
        body = _preset_dialogue_lines(
            cues,
            preset,
            keyword_indices=keyword_indices or set(),
            clip_duration=clip_duration,
            permissibility=permissibility,
            emoji_glyph_available=emoji_glyph_available,
            emoji_downloader=emoji_downloader,
            fit=TextFit.for_preset(preset, video_width=video_width),
            language=language,
        )
    else:
        legacy_position = position if position is not None else "bottom"
        style_line, _align, template_karaoke = _caption_style(
            template, legacy_position, font, font_size
        )
        use_karaoke = template_karaoke if karaoke is None else karaoke
        # A dedicated top-anchored style for the hook title (alignment 8 = top).
        hook_style = (
            f"Style: Hook,{font},{hook_font_size},&H0000E5FF,&H0000E5FF,"
            f"&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,8,60,60,160,1"
        )
        body = _legacy_dialogue_lines(cues, use_karaoke)

    # C21: 2 means "no automatic wrapping", which C6's measured `\N` breaks depend on. A shaping
    # script gets 0 instead, so libass wraps it - an Arabic word's rendered width is not the sum of
    # its letters' isolated advances, so the number C6 would break on is simply wrong there.
    wrap_style = script_support.wrap_style(script_plan)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
WrapStyle: {wrap_style}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{style_line}
{hook_style}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]

    # Optional hook title at the very start with a soft fade.
    if hook_text.strip():
        h_start = _ass_timestamp(0.0)
        h_end = _ass_timestamp(max(0.5, hook_duration))
        hook = _escape(hook_text.strip().upper())
        lines.append(f"Dialogue: 1,{h_start},{h_end},Hook,,0,0,0,,{{\\fad(250,350)}}{hook}")

    lines.extend(body)

    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


#: Fade lengths for the end card, in milliseconds.
#:
#: Asymmetric on purpose: it appears quickly enough to be read, and does not fade out at all -
#: the clip ends under it, so a fade-out would only take the words away before the viewer's
#: decision point.
END_CARD_FADE_IN_MS = 300


def end_card_dialogue(
    clip_duration: float,
    *,
    video_width: int = 1080,
    video_height: int = 1920,
    text: str | None = None,
    seconds: float | None = None,
) -> str:
    """One ASS dialogue line for the closing call-to-action, or ``""`` (V14).

    Every clip currently ends the instant the speech does, which wastes the one moment the
    viewer has already decided to watch to the end. A short "follow for more" over the tail is
    the standard ask, and there was no way to add one without re-editing the export by hand.

    Rendered as an ASS event rather than ``drawtext`` deliberately: this module renders all text
    through libass so it works on ffmpeg builds without freetype, and a ``drawtext`` end card
    would be the one piece of text that vanished on such a build.

    Returns ``""`` when disabled, when there is no text, or when the clip is too short to give
    the card a full appearance - a card that fades in as the video cuts is worse than none.
    """
    if text is None:
        text = str(getattr(settings, "end_card_text", "") or "")
    if seconds is None:
        seconds = float(getattr(settings, "end_card_seconds", 2.0) or 0.0)
    text = text.strip()
    duration = max(0.0, float(clip_duration))
    if not text or seconds <= 0 or duration <= 0:
        return ""
    # The card must fit, and still leave clip before it: on a 3 s clip a 2 s card is most of the
    # video, which is an advert with a clip attached rather than the reverse.
    seconds = min(float(seconds), duration / 2.0)
    if seconds < END_CARD_FADE_IN_MS / 1000.0:
        return ""

    start = _ass_timestamp(duration - seconds)
    end = _ass_timestamp(duration)
    # Slide up a little as it fades in. `\move` is relative to PlayRes, so the distance scales
    # with the output height rather than being a fixed pixel count that is invisible at 4K.
    rise = max(12, int(round(video_height * 0.02)))
    y_from = int(round(video_height * 0.78)) + rise
    y_to = int(round(video_height * 0.78))
    # Numeric, not an expression: ASS override tags take literal numbers, so `PlayResX/2` here
    # would be parsed as a malformed argument and the card would land wherever libass recovered
    # to rather than centred.
    x = int(round(video_width / 2))
    tags = (
        f"{{\\an5\\move({x},{y_from},{x},{y_to},0,{END_CARD_FADE_IN_MS})"
        f"\\fad({END_CARD_FADE_IN_MS},0)}}"
    )
    return f"Dialogue: 2,{start},{end},End,,0,0,0,,{tags}{_escape(text.upper())}"


def write_end_card_ass(
    dest: str | Path,
    clip_duration: float,
    *,
    video_width: int = 1080,
    video_height: int = 1920,
    font: str = "Poppins ExtraBold",
    font_size: int = 96,
    text: str | None = None,
    seconds: float | None = None,
) -> Path | None:
    """Write a standalone ASS holding just the end card, or return ``None`` (V14).

    Standalone rather than a line inside the caption ASS, for one reason: the card must not
    depend on captions. It has to appear on a clip with captions turned off, and on a clip whose
    captions are owned by the kinetic-typography engine - which writes its own ASS and would
    never see a line added here. One file with one job covers all three cases, at the cost of a
    second libass filter that only exists when the card is actually configured.
    """
    line = end_card_dialogue(
        clip_duration,
        video_width=video_width,
        video_height=video_height,
        text=text,
        seconds=seconds,
    )
    if not line:
        return None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Alignment 5 (centred) with generous margins; the position is set per-event by \move, so
    # this style only has to supply the look.
    style = (
        f"Style: End,{font},{font_size},&H00FFFFFF,&H00FFFFFF,"
        f"&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,4,2,5,60,60,60,1"
    )
    dest.write_text(
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_width}\n"
        f"PlayResY: {video_height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style}\n"
        "\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"{line}\n",
        encoding="utf-8",
    )
    return dest


def _legacy_dialogue_lines(cues: list[Cue], use_karaoke: bool) -> list[str]:
    """Render legacy (template-driven) dialogue lines (unchanged behaviour)."""
    lines: list[str] = []
    for cue in cues:
        if not cue.words:
            continue
        start = _ass_timestamp(cue.start)
        end = _ass_timestamp(cue.end)
        if use_karaoke:
            parts = []
            for w in cue.words:
                dur_cs = max(1, int(round((w.end - w.start) * 100)))
                parts.append(f"{{\\kf{dur_cs}}}{_escape(w.text)}")
            text = " ".join(parts)
        else:
            text = " ".join(_escape(w.text) for w in cue.words)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    return lines


def _preset_header_styles(
    preset: CaptionPreset,
    position: str | None,
    hook_font_size: int,
    notes: list[str] | None,
    *,
    video_width: int = 1080,
    video_height: int = 1920,
    safe_area: str | None = None,
    caption_offset: int = 0,
) -> tuple[str, str]:
    """Return ``(default_style_line, hook_style_line)`` for a preset.

    Resolves the caption position (override wins over the preset default,
    Req 5.2) and substitutes an unavailable font with a fallback, recording a
    ``font_substituted:<name>`` note (Req 5.3).

    ``safe_area`` selects a platform inset profile (C12) and ``caption_offset`` nudges the
    caption further from its edge (C13). Both keyword-only with inert defaults, so every
    existing caller - and the v0.8.0 parity gate - produces a byte-identical style line.
    """
    resolved_position = position if position is not None else preset.position
    align, margin_v = _POSITION_ALIGN.get(resolved_position, _POSITION_ALIGN["bottom"])
    margin_h = 80
    if safe_area or caption_offset:
        # C12/C13: only when asked for. Computing margins unconditionally would change the
        # numbers on every existing render, because the safe-area figures are fractions of the
        # frame and would not land on exactly 220/200/80 at every resolution.
        margin_h, _margin_r, resolved_v = resolve_margins(
            resolved_position,
            video_width,
            video_height,
            platform=safe_area,
            offset=caption_offset,
        )
        if align not in (4, 5, 6):
            margin_v = resolved_v

    resolved_font, substituted = resolve_font(preset.font)
    if substituted and notes is not None:
        # The font actually used, not the one requested. ``worker/models.py`` has always
        # documented this marker as "preset font missing; <name> used", but the code
        # recorded ``preset.font`` - the name that did *not* work - so the marker could
        # not tell you what a clip was rendered in. C1 asks for the real substitution.
        notes.append(f"font_substituted:{resolved_font}")

    style_line = _preset_style_line(
        preset, resolved_font, preset.font_size, align, margin_v, margin_h
    )
    hook_style = (
        f"Style: Hook,{resolved_font},{hook_font_size},&H0000E5FF,&H0000E5FF,"
        f"&H00000000,&H64000000,{ass_bold_flag(preset)},0,0,0,100,100,0,0,1,5,2,8,"
        f"60,60,160,1"
    )
    return style_line, hook_style


def _preset_dialogue_lines(
    cues: list[Cue],
    preset: CaptionPreset,
    *,
    keyword_indices: set[int],
    clip_duration: float | None,
    permissibility: bool,
    emoji_glyph_available: Any | None,
    emoji_downloader: Any | None,
    fit: TextFit | None = None,
    language: str = "",
) -> list[str]:
    """Render preset-driven dialogue lines (one event per cue).

    Per-word spans come from :func:`build_word_span`; ``keyword_indices`` is a
    *flat* word index set across all cues. All emitted dialogue timestamps are
    clamped to ``[0, clip_duration]`` (Req 2.5); an empty timeline yields zero
    events and never raises (Req 2.4).
    """
    # Determine the clamp bound: the explicit clip duration, else the last cue
    # end (so per-cue windows are always within [0, duration]).
    ends = [cue.end for cue in cues if cue.words]
    duration = clip_duration if clip_duration is not None else (max(ends) if ends else 0.0)
    duration = max(0.0, float(duration))

    def _clamp(t: float) -> float:
        return min(max(0.0, t), duration)

    lines: list[str] = []
    global_index = 0
    for cue in cues:
        if not cue.words:
            continue
        cue_start = _clamp(cue.start)
        cue_end = _clamp(cue.end)
        if cue_end < cue_start:
            cue_end = cue_start
        start = _ass_timestamp(cue_start)
        end = _ass_timestamp(cue_end)

        parts: list[str] = []
        plain: list[str] = []
        for w in cue.words:
            highlighted = global_index in keyword_indices
            if getattr(preset, "uppercase", False):
                # C7: applied to the word before it is turned into a span, so the ASS
                # override tags built around it are untouched. Only the hook title was
                # upper-cased before, so no preset could ask for the all-caps look.
                w = _uppercased(w)
            span = build_word_span(w, preset, highlighted, cue_start=cue_start)
            if getattr(preset, "emoji_inline", False):
                glyph = caption_emoji_glyph(
                    w,
                    preset,
                    permissible=permissibility,
                    glyph_available=emoji_glyph_available,
                    downloader=emoji_downloader,
                )
                if glyph:
                    span = f"{span} {glyph}"
            parts.append(span)
            plain.append(_word_text(w))
            global_index += 1

        # C6: insert real line breaks at measured positions.
        #
        # The file declares `WrapStyle: 2`, which means libass wraps *only* where the text already
        # contains `\N` - and nothing inserted one, so every cue was laid out as a single line and
        # either ran past the frame edge or was silently shrunk, depending on the build. Neither is
        # a decision anyone made.
        #
        # The break points are computed from the plain words and applied to the spans, because the
        # spans carry override tags: measuring `{\kf34\c&H0000E5FF&}money` would count the tag as
        # letters, and one tag is longer than the word it decorates.
        if fit is not None and len(parts) > 1:
            # C25: prefer a linguistic break, but only one the measured budget accepts.
            #
            # `choose_break` returns a position only when both halves fit, and `None` for a
            # disabled setting, a non-English language or no acceptable candidate -- in every one of
            # those cases the measured wrap below stands unchanged (R5.5). So width still decides
            # what is possible and this only reorders the preferences among the possibilities.
            groups: list[list[int]] | None = None
            linguistic = cue_constraints.choose_break(
                plain,
                fit=fit,
                language=language or "",
                enabled=bool(settings.caption_linguistic_breaks),
            )
            if linguistic is not None:
                groups = [list(range(linguistic)), list(range(linguistic, len(plain)))]
            if groups is None:
                groups = text_metrics.wrap_word_groups(
                    plain,
                    font=fit.font,
                    font_size=fit.font_size,
                    max_width_px=fit.max_width_px,
                    max_lines=fit.max_lines,
                    spacing=fit.spacing,
                    scale_x=fit.scale_x,
                )
            text = "\\N".join(
                " ".join(parts[index] for index in group) for group in groups if group
            )
        else:
            text = " ".join(parts)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    return lines


#: Shared with every other filter-string builder; see :func:`ffmpeg_utils.escape_filter_path`.
_escape_filter_path = escape_filter_path


def subtitles_filter(ass: str | Path) -> str:
    """Return an ffmpeg ``subtitles=...`` filter string for an ASS file.

    The path is escaped for ffmpeg's filter-argument syntax so it can be dropped
    into a larger ``-vf`` / ``-filter_complex`` chain (used by the compositor).

    When the bundled font directory exists, ``fontsdir`` is appended so libass loads
    ``assets/fonts`` directly (C2). Without it, appearance depends on what the host
    happens to have installed: verified with libass at ``-loglevel verbose``, a style
    naming ``Anton`` on a host without it silently renders as ``NotoSans-Bold``, whereas
    with ``fontsdir`` the same style resolves to ``Anton-Regular``. The Dockerfile also
    installs these faces system-wide, so the two mechanisms back each other up - and
    ``fontsdir`` is the one that also covers a bare developer checkout and CI.
    """
    escaped = _escape_filter_path(ass)
    fonts_dir = Path(settings.font_assets_dir)
    if fonts_dir.is_dir():
        return f"subtitles='{escaped}':fontsdir='{_escape_filter_path(fonts_dir)}'"
    return f"subtitles='{escaped}'"


def burn_captions(video: str | Path, ass: str | Path, dest: str | Path) -> Path:
    """Burn the ASS subtitle file ``ass`` into ``video`` and write ``dest``.

    Uses the libass-backed ``subtitles`` filter. The subtitle path is escaped
    for FFmpeg's filter argument syntax.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        settings.ffmpeg_binary,
        "-y",
        "-i",
        str(video),
        "-vf",
        subtitles_filter(ass),
        *h264_args(normalise_fps=True),
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    _run(cmd)
    return dest


def build_and_burn(
    transcript: Transcript,
    video: str | Path,
    start: float,
    end: float,
    dest: str | Path,
    ass_path: str | Path | None = None,
    **style,
) -> Path:
    """Convenience: slice ``transcript`` to the clip window, build ASS, and burn.

    Args:
        transcript: Full-video transcript (source-relative timing).
        video: The already-cut clip whose captions we are burning.
        start: Clip start in source time (used to rebase words).
        end: Clip end in source time.
        dest: Output path for the captioned clip.
        ass_path: Optional path to write the intermediate ASS file.
        **style: Forwarded to :func:`build_ass` (font, colours, sizing...).
    """
    words = slice_words(transcript, start, end)
    cues = words_to_cues(words)
    ass_dest = ass_path or Path(dest).with_suffix(".ass")
    build_ass(cues, ass_dest, **style)
    return burn_captions(video, ass_dest, dest)
