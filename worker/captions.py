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

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from config import settings
from worker.effects.caption_presets import CaptionPreset
from worker.ffmpeg_utils import _run
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
    max_words: int = 5,
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


# Caption position (UI value) -> ASS numpad alignment + default vertical margin.
# ASS alignments: 2 = bottom-centre, 5 = middle-centre, 8 = top-centre.
_POSITION_ALIGN: dict[str, tuple[int, int]] = {
    "bottom": (2, 220),
    "center": (5, 0),
    "top": (8, 200),
}


def _caption_style(
    template: str,
    position: str,
    font: str,
    font_size: int,
) -> tuple[str, int, bool]:
    """Return ``(style_line, alignment, karaoke)`` for a caption template.

    Templates:
        * ``karaoke`` — white text, green per-word fill sweep, bold outline.
        * ``boxed``   — white text on a semi-opaque box (BorderStyle 3).
        * ``minimal`` — plain white text, thin outline, no karaoke.
    """
    align, margin_v = _POSITION_ALIGN.get(position, _POSITION_ALIGN["bottom"])

    white = "&H00FFFFFF"
    green = "&H0000FF00"
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
        f"Style: Default,{font},{font_size},{white},{green},{black},&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,4,2,{align},80,80,{margin_v},1"
    )
    return style, align, True


# --- Preset-driven ASS spans (Feature A) ------------------------------------

# Font used when a preset's declared font is unavailable on the host (Req 5.3).
_FALLBACK_FONT = "Arial"

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
    escaped = _escape(_word_text(word))
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
    return span


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

def _preset_style_line(
    preset: CaptionPreset,
    font: str,
    font_size: int,
    align: int,
    margin_v: int,
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
        outline_w = 0
        shadow = 0
    else:
        outline_col = colors.outline
        back_col = "&H64000000"
        outline_w = 4 if preset.animation == "karaoke_fill" else 2
        shadow = 2 if preset.animation == "karaoke_fill" else 1
    return (
        f"Style: Default,{font},{font_size},{primary},{secondary},{outline_col},"
        f"{back_col},-1,0,0,0,100,100,0,0,{preset.border_style},{outline_w},"
        f"{shadow},{align},80,80,{margin_v},1"
    )


def build_ass(
    cues: list[Cue],
    dest: str | Path,
    video_width: int = 1080,
    video_height: int = 1920,
    font: str = "Arial",
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
            preset, position, hook_font_size, notes
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
) -> tuple[str, str]:
    """Return ``(default_style_line, hook_style_line)`` for a preset.

    Resolves the caption position (override wins over the preset default,
    Req 5.2) and substitutes an unavailable font with a fallback, recording a
    ``font_substituted:<name>`` note (Req 5.3).
    """
    resolved_position = position if position is not None else preset.position
    align, margin_v = _POSITION_ALIGN.get(
        resolved_position, _POSITION_ALIGN["bottom"]
    )

    resolved_font = preset.font
    if not font_available(resolved_font):
        if notes is not None:
            notes.append(f"font_substituted:{preset.font}")
        resolved_font = _FALLBACK_FONT

    style_line = _preset_style_line(
        preset, resolved_font, preset.font_size, align, margin_v
    )
    hook_style = (
        f"Style: Hook,{resolved_font},{hook_font_size},&H0000E5FF,&H0000E5FF,"
        f"&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,8,60,60,160,1"
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


def subtitles_filter(ass: str | Path) -> str:
    """Return an ffmpeg ``subtitles=...`` filter string for an ASS file.

    The path is escaped for ffmpeg's filter-argument syntax so it can be dropped
    into a larger ``-vf`` / ``-filter_complex`` chain (used by the compositor).
    """
    ass_path = str(Path(ass).resolve())
    escaped = ass_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
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
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
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
