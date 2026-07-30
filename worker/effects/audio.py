"""Background-music beds and audio mixing.

Two sources of music, in priority order:

1. **User-supplied track** — if ``settings.music_dir/<mood>.<ext>`` exists it is
   used directly (bring your own licensed music).
2. **Synthesised bed** — a last-resort fallback, *not* music (A15). It is two sine
   tones (a root and a fifth) with tremolo and a low-pass: a drone, not a track. No
   arrangement, no rhythm, no progression, identical for every clip of a given mood.

The distinction matters because it was invisible. ``resolve_music`` returned a path and
nothing recorded which of the two it was, so a clip with a synthesised drone was reported
as ``music:upbeat`` — indistinguishable from a clip with a real bed under it. A caller had
no way to tell that "background music" meant a tone generator, and ``assets/music`` ships
empty, so in practice it always did.

:func:`resolve_music_bed` therefore returns a :class:`MusicBed` naming the source, and the
compositor records ``music_degraded:synthesised`` alongside the ``music:<mood>`` marker.
Real beds (A14) have not shipped; until they do, the honest reading of an enabled music
option is "a drone unless you supplied a track yourself".

The bed is mixed under the original audio with a configurable volume and,
optionally, matching fade in/out.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import settings
from worker.ffmpeg_utils import _run

# Per-mood synthesis parameters: two tones (root + interval) and a tremolo rate.
# Frequencies are chosen to be pleasant and unobtrusive; this is a mood *bed*,
# not a melody, so it never competes with speech.
_MOOD_SYNTH: dict[str, dict[str, float]] = {
    "upbeat":    {"root": 293.66, "fifth": 440.00, "tremolo": 5.0, "cutoff": 3200},
    "chill":     {"root": 220.00, "fifth": 329.63, "tremolo": 2.0, "cutoff": 2200},
    "dramatic":  {"root": 130.81, "fifth": 196.00, "tremolo": 1.2, "cutoff": 1800},
    "corporate": {"root": 261.63, "fifth": 392.00, "tremolo": 3.0, "cutoff": 2600},
    "suspense":  {"root": 110.00, "fifth": 164.81, "tremolo": 0.8, "cutoff": 1400},
}

_AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac")


def available_moods() -> list[str]:
    """Return the list of supported music moods."""
    return list(_MOOD_SYNTH.keys())


def find_user_track(mood: str) -> Optional[Path]:
    """Return a user-supplied ``music_dir/<mood>.<ext>`` track if one exists."""
    base = Path(settings.music_dir)
    for ext in _AUDIO_EXTS:
        candidate = base / f"{mood}{ext}"
        if candidate.exists():
            return candidate
    return None


def synth_bed_filter(mood: str) -> str:
    """Return an ffmpeg ``-filter_complex`` graph that synthesises a mood bed.

    The graph produces a single mono ``[bed]`` output built from two sine tones
    blended together, softened with a tremolo and a low-pass filter.
    """
    params = _MOOD_SYNTH.get(mood, _MOOD_SYNTH["chill"])
    root = params["root"]
    fifth = params["fifth"]
    tremolo = params["tremolo"]
    cutoff = int(params["cutoff"])
    return (
        f"sine=frequency={root:g}:sample_rate=44100[a0];"
        f"sine=frequency={fifth:g}:sample_rate=44100[a1];"
        f"[a0][a1]amix=inputs=2:normalize=1,"
        f"tremolo=f={tremolo:g}:d=0.6,"
        f"lowpass=f={cutoff},"
        f"aformat=sample_fmts=fltp:channel_layouts=stereo[bed]"
    )


def synthesize_bed(mood: str, duration: float, dest: str | Path) -> Path:
    """Render a synthesised ``mood`` bed of ``duration`` seconds to ``dest``."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    graph = synth_bed_filter(mood)
    cmd = [
        settings.ffmpeg_binary, "-y",
        "-filter_complex", graph,
        "-map", "[bed]",
        "-t", f"{max(0.1, duration):.3f}",
        "-c:a", "aac", "-b:a", "128k",
        str(dest),
    ]
    _run(cmd)
    return dest


#: ``MusicBed.source`` when the audio is a real file the user supplied.
SOURCE_USER_TRACK = "user_track"

#: ``MusicBed.source`` when the audio is the synthesised two-tone drone (A15).
SOURCE_SYNTHESISED = "synthesised"


@dataclass(frozen=True)
class MusicBed:
    """A resolved music bed and, crucially, *what it is* (A15).

    ``source`` is :data:`SOURCE_USER_TRACK` or :data:`SOURCE_SYNTHESISED`. Callers must
    branch on it rather than assuming a path means music: the synthesised bed is a tone
    generator, and reporting it as though a track were playing is what A15 removes.
    """

    path: Path
    mood: str
    source: str

    @property
    def synthesised(self) -> bool:
        """Whether this bed is the fallback drone rather than a real track."""
        return self.source == SOURCE_SYNTHESISED


def resolve_music_bed(
    mood: str, duration: float, temp_dir: str | Path
) -> Optional[MusicBed]:
    """Resolve a bed for ``mood``, reporting whether it is a real track (A15).

    Returns ``None`` when ``mood`` is empty or unknown, or when synthesis is the only
    option and ``settings.music_allow_synthesis`` is off — in which case the clip is
    rendered without music rather than with a drone the caller did not ask for.
    """
    if not mood:
        return None
    user = find_user_track(mood)
    if user is not None:
        return MusicBed(path=user, mood=mood, source=SOURCE_USER_TRACK)
    if mood not in _MOOD_SYNTH:
        return None
    if not settings.music_allow_synthesis:
        return None
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    dest = synthesize_bed(mood, duration, temp_dir / f"synth_bed_{mood}.m4a")
    return MusicBed(path=dest, mood=mood, source=SOURCE_SYNTHESISED)


def resolve_music(mood: str, duration: float, temp_dir: str | Path) -> Optional[Path]:
    """The path-only view of :func:`resolve_music_bed`.

    Kept for callers that only need somewhere to read audio from. Anything that reports
    what happened to a clip must use :func:`resolve_music_bed` instead — a bare path cannot
    distinguish a licensed track from a synthesised drone, which is exactly the gap A15
    closes.
    """
    bed = resolve_music_bed(mood, duration, temp_dir)
    return None if bed is None else bed.path


def music_mix_filter(
    original_label: str,
    music_label: str,
    out_label: str,
    volume: float,
    duration: float,
    fade: bool = False,
    fade_dur: float = 0.4,
) -> str:
    """Return a ``-filter_complex`` snippet mixing a music bed under speech.

    The bed is volume-scaled (and optionally faded), then mixed with the
    original audio without re-normalising (so speech stays at full level).
    """
    vol = max(0.0, min(1.0, volume))
    bed_chain = f"[{music_label}]volume={vol:.3f}"
    if fade:
        out_start = max(0.0, duration - fade_dur)
        bed_chain += (
            f",afade=t=in:st=0:d={fade_dur:.3f}"
            f",afade=t=out:st={out_start:.3f}:d={fade_dur:.3f}"
        )
    bed_chain += "[bedv]"

    orig = f"[{original_label}]"
    if fade:
        out_start = max(0.0, duration - fade_dur)
        orig = (
            f"[{original_label}]afade=t=in:st=0:d={fade_dur:.3f}"
            f",afade=t=out:st={out_start:.3f}:d={fade_dur:.3f}[orig]"
        )
        orig_label = "[orig]"
    else:
        orig_label = f"[{original_label}]"

    parts = [bed_chain]
    if fade:
        parts.append(orig)
    parts.append(
        f"{orig_label}[bedv]amix=inputs=2:duration=first:normalize=0[{out_label}]"
    )
    return ";".join(parts)
