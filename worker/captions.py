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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from config import settings
from worker.ffmpeg_utils import FFmpegError, _run
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


def build_ass(
    cues: list[Cue],
    dest: str | Path,
    video_width: int = 1080,
    video_height: int = 1920,
    font: str = "Arial",
    font_size: int = 84,
    primary_color: str = "&H00FFFFFF",       # white (AABBGGRR)
    highlight_color: str = "&H0000FF00",      # green fill highlight
    outline_color: str = "&H00000000",        # black outline
    margin_v: int = 220,
    karaoke: bool = True,
) -> Path:
    """Render ``cues`` to an ASS subtitle file at ``dest``.

    Colours use ASS ``&HAABBGGRR`` notation. When ``karaoke`` is enabled each
    word gets a ``\\kf`` fill tag so the highlight sweeps word-by-word.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_size},{primary_color},{highlight_color},{outline_color},&H64000000,-1,0,0,0,100,100,0,0,1,4,2,2,80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    for cue in cues:
        if not cue.words:
            continue
        start = _ass_timestamp(cue.start)
        end = _ass_timestamp(cue.end)

        if karaoke:
            parts = []
            for w in cue.words:
                dur_cs = max(1, int(round((w.end - w.start) * 100)))
                parts.append(f"{{\\kf{dur_cs}}}{_escape(w.text)}")
            text = " ".join(parts)
        else:
            text = " ".join(_escape(w.text) for w in cue.words)

        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def burn_captions(video: str | Path, ass: str | Path, dest: str | Path) -> Path:
    """Burn the ASS subtitle file ``ass`` into ``video`` and write ``dest``.

    Uses the libass-backed ``subtitles`` filter. The subtitle path is escaped
    for FFmpeg's filter argument syntax.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Escape characters that are special inside an ffmpeg filter argument.
    ass_path = str(Path(ass).resolve())
    escaped = ass_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    cmd = [
        settings.ffmpeg_binary, "-y", "-i", str(video),
        "-vf", f"subtitles='{escaped}'",
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
