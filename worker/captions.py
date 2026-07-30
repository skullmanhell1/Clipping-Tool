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
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from config import settings
from worker.effects.caption_presets import CaptionPreset
from worker.ffmpeg_utils import _run, h264_args
from worker.transcribe import Transcript, Word


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


def words_to_cues(
    words: Iterable[Word],
    # C5: three words, not five. Five words at a readable size gives long thin lines that
    # scan left-to-right like a subtitle; short-form captions are near-full-width and meant
    # to be taken in at a glance, which is what allows the larger size that comes with it.
    max_words: int = 3,
    max_gap: float = 0.6,
    max_duration: float = 3.0,
) -> list[Cue]:
    """Group ``words`` into readable cues.

    A new cue is started when the current cue reaches ``max_words``, spans more
    than ``max_duration`` seconds, or when the silent gap before a word exceeds
    ``max_gap`` seconds.
    """
    cues: list[Cue] = []
    current: list[Word] = []

    for w in words:
        if not w.text:
            continue
        if current:
            gap = w.start - current[-1].end
            span = w.end - current[0].start
            if len(current) >= max_words or gap > max_gap or span > max_duration:
                cues.append(Cue(current[0].start, current[-1].end, current))
                current = []
        current.append(w)

    if current:
        cues.append(Cue(current[0].start, current[-1].end, current))
    return cues


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
_FONT_CACHE: dict[str, Optional[frozenset[str]]] = {}


def _enumerate_system_fonts() -> Optional[frozenset[str]]:
    """Best-effort enumeration of local font families (lower-cased).

    Uses ``fc-list`` when available. Returns ``None`` when enumeration is not
    possible so callers can stay conservative and assume a font *is* available
    (we never want to falsely substitute).
    """
    if "fonts" in _FONT_CACHE:
        return _FONT_CACHE["fonts"]

    fonts: Optional[frozenset[str]] = None
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


def font_available(name: str) -> bool:
    """Return whether ``name`` is a locally available font family (best-effort).

    Conservative by design (Req 5.3): when we cannot enumerate host fonts we
    return ``True`` so a real font is never falsely substituted. When we *can*
    enumerate, an obviously-absent family reports ``False`` (this is also the
    monkeypatch point used by the font-substitution tests).
    """
    if not isinstance(name, str) or not name.strip():
        return False
    fonts = _enumerate_system_fonts()
    if not fonts:
        return True  # uncertain -> assume available
    return name.strip().lower() in fonts


def resolve_font(
    requested: str,
    *,
    available: Optional[Any] = None,
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
        span = (
            f"{{\\fscx60\\fscy60\\t({rel_ms},{rel_ms + 120},"
            f"\\fscx100\\fscy100)}}{escaped}"
        )
    elif animation == "typewriter":
        span = (
            f"{{\\alpha&HFF&\\t({rel_ms},{rel_ms + 30},\\alpha&H00&)}}{escaped}"
        )
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
_PROFANITY: frozenset[str] = frozenset({
    "fuck", "fucking", "fucked", "fucker", "motherfucker",
    "shit", "shitty", "bullshit", "cunt", "cock", "dick",
    "bitch", "bastard", "asshole", "arsehole", "whore", "slut",
    "nigger", "faggot", "retard", "retarded",
})

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


def available_fonts() -> list[dict]:
    """The vendored caption faces, for a real font picker in the UI (A4).

    Twelve faces are vendored with licences and a manifest, and nothing exposed them - so the
    only way to change a caption font was to edit a preset in source, and the assets might as
    well not have been shipped.

    Returns the *usable* subset with only the fields a picker needs. Variable fonts are excluded
    because libass' ``fontsdir`` provider cannot select a named instance of one - a request for
    such a family silently resolves to something else, which is exactly the C1 defect. Offering
    a font that will not render is worse than offering fewer.

    Never raises: a missing or malformed manifest yields ``[]``, and the caller falls back to
    whatever the presets already name.
    """
    try:
        data = json.loads(FONT_MANIFEST.read_text(encoding="utf-8"))
        entries = data.get("fonts") or []
    except Exception:
        return []

    fonts: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("variable"):
            continue
        name = str(entry.get("name") or "").strip()
        filename = str(entry.get("file") or "").strip()
        if not name or not filename:
            continue
        # Only faces actually present on disk: the manifest is a declaration, and a CI step
        # exists precisely because the two once disagreed.
        if not (FONT_MANIFEST.parent / "fonts" / filename).is_file():
            continue
        fonts.append({
            "name": name,
            "family": str(entry.get("family") or name),
            "style": str(entry.get("style") or ""),
            "weight": int(entry.get("weight") or 0),
            "heavy": bool(entry.get("heavy_face")),
            "license": str(entry.get("license") or ""),
            "use": str(entry.get("use") or ""),
        })
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
    return (
        f"{{\\fscx{peak}\\fscy{peak}"
        f"\\t({rel_ms},{rel_ms + length},\\fscx100\\fscy100)}}"
    )


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
    if probability != probability:   # NaN
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
    "love": "\u2764\ufe0f", "heart": "\u2764\ufe0f", "amazing": "\U0001f929",
    "wow": "\U0001f62e", "money": "\U0001f4b0", "cash": "\U0001f4b5",
    "rich": "\U0001f911", "fire": "\U0001f525", "hot": "\U0001f525",
    "best": "\U0001f3c6", "win": "\U0001f3c6", "winner": "\U0001f3c6",
    "success": "\U0001f4c8", "growth": "\U0001f4c8", "idea": "\U0001f4a1",
    "smart": "\U0001f9e0", "brain": "\U0001f9e0", "crazy": "\U0001f92f",
    "insane": "\U0001f92f", "mind": "\U0001f9e0", "laugh": "\U0001f602",
    "funny": "\U0001f602", "happy": "\U0001f604", "sad": "\U0001f622",
    "boom": "\U0001f4a5", "rocket": "\U0001f680", "power": "\U0001f4aa",
    "goal": "\U0001f3af", "target": "\U0001f3af", "secret": "\U0001f92b",
    "yes": "\u2705", "no": "\u274c", "star": "\u2b50", "party": "\U0001f389",
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
    glyph_available: Optional[Any] = None,
    downloader: Optional[Any] = None,
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
    karaoke: Optional[bool] = None,
    *,
    preset: CaptionPreset | None = None,
    keyword_indices: set[int] | None = None,
    clip_duration: float | None = None,
    permissibility: bool = False,
    emoji_glyph_available: Optional[Any] = None,
    emoji_downloader: Optional[Any] = None,
    notes: list[str] | None = None,
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

    if preset is not None:
        style_line, hook_style = _preset_header_styles(
            preset, position, hook_font_size, notes,
            video_width=video_width, video_height=video_height,
            # C12/C13: both inert unless configured, so an unconfigured render is byte-identical.
            safe_area=(getattr(settings, "caption_safe_area", "") or "") or None,
            caption_offset=int(getattr(settings, "caption_offset_px", 0) or 0),
        )
        body = _preset_dialogue_lines(
            cues,
            preset,
            keyword_indices=keyword_indices or set(),
            clip_duration=clip_duration,
            permissibility=permissibility,
            emoji_glyph_available=emoji_glyph_available,
            emoji_downloader=emoji_downloader,
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

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
WrapStyle: 2
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
        lines.append(
            f"Dialogue: 1,{h_start},{h_end},Hook,,0,0,0,,{{\\fad(250,350)}}{hook}"
        )

    lines.extend(body)

    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
    align, margin_v = _POSITION_ALIGN.get(
        resolved_position, _POSITION_ALIGN["bottom"]
    )
    margin_h = 80
    if safe_area or caption_offset:
        # C12/C13: only when asked for. Computing margins unconditionally would change the
        # numbers on every existing render, because the safe-area figures are fractions of the
        # frame and would not land on exactly 220/200/80 at every resolution.
        margin_h, _margin_r, resolved_v = resolve_margins(
            resolved_position, video_width, video_height,
            platform=safe_area, offset=caption_offset,
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
    emoji_glyph_available: Optional[Any],
    emoji_downloader: Optional[Any],
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
            global_index += 1

        text = " ".join(parts)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    return lines


def _escape_filter_path(path: str | Path) -> str:
    """Escape an absolute path for ffmpeg's filter-argument syntax."""
    resolved = str(Path(path).resolve())
    return resolved.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


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
        settings.ffmpeg_binary, "-y", "-i", str(video),
        "-vf", subtitles_filter(ass),
        *h264_args(normalise_fps=True),
        "-c:a", "copy",
        "-movflags", "+faststart",
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
