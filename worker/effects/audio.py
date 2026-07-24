"""Background-music beds and audio mixing.

Two sources of music, in priority order:

1. **User-supplied track** — if ``settings.music_dir/<mood>.<ext>`` exists it is
   used directly (bring your own licensed music).
2. **Synthesised bed** — otherwise a soft, copyright-free ambient pad is
   generated on the fly with ffmpeg's ``sine`` sources (a root note + a fifth,
   gently tremolo'd and low-passed). It is intentionally subtle so it sits under
   speech.

The bed is mixed under the original audio with a configurable volume and,
optionally, matching fade in/out.
"""

from __future__ import annotations

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


def resolve_music(mood: str, duration: float, temp_dir: str | Path) -> Optional[Path]:
    """Return a path to a music bed for ``mood`` (user track or synthesised).

    Returns ``None`` when ``mood`` is empty/unknown so callers can skip mixing.
    """
    if not mood:
        return None
    user = find_user_track(mood)
    if user is not None:
        return user
    if mood not in _MOOD_SYNTH:
        return None
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    return synthesize_bed(mood, duration, temp_dir / f"music_{mood}.m4a")


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
