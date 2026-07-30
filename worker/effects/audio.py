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

import json
import subprocess
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
        "-ar", str(int(settings.output_sample_rate)),
        "-ac", str(int(settings.output_channels)),
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
    duck: bool = True,
) -> str:
    """Return a ``-filter_complex`` snippet mixing a music bed under speech.

    The bed is volume-scaled (and optionally faded), then mixed with the original audio
    without re-normalising, so speech stays at full level.

    ``duck`` (AU2) routes the bed through ``sidechaincompress`` keyed on the speech, so the
    music drops while someone is talking and returns in the gaps. A flat ``volume=0.12`` bed
    has no good setting: loud enough to be heard between sentences is loud enough to fight
    the speech during them, and quiet enough not to fight it is inaudible - which is the
    same as no music, at the cost of an extra encode. Ducking is what makes a bed audible
    *and* out of the way, and it is the reason a mix sounds produced rather than layered.

    The speech is duplicated with ``asplit``: one copy keys the compressor, the other is
    mixed. It has to be both, and a filter output cannot be consumed twice.
    """
    vol = max(0.0, min(1.0, volume))
    ratio = max(1.0, float(settings.music_duck_ratio))
    ducking = duck and ratio > 1.0

    parts: list[str] = []
    out_start = max(0.0, duration - fade_dur)

    # --- the bed: level, then optional fades -------------------------------
    bed_chain = f"[{music_label}]volume={vol:.3f}"
    if fade:
        bed_chain += (
            f",afade=t=in:st=0:d={fade_dur:.3f}"
            f",afade=t=out:st={out_start:.3f}:d={fade_dur:.3f}"
        )
    bed_chain += "[bedv]"
    parts.append(bed_chain)

    # --- the speech: optional fades, then a split when ducking -------------
    speech_chain = f"[{original_label}]"
    if fade:
        parts.append(
            f"[{original_label}]afade=t=in:st=0:d={fade_dur:.3f}"
            f",afade=t=out:st={out_start:.3f}:d={fade_dur:.3f}[orig]"
        )
        speech_chain = "[orig]"

    if not ducking:
        parts.append(
            f"{speech_chain}[bedv]amix=inputs=2:duration=first:normalize=0[{out_label}]"
        )
        return ";".join(parts)

    parts.append(f"{speech_chain}asplit=2[sckey][spmix]")
    # threshold is a linear amplitude, not dB: 0.03 is about -30 dBFS, low enough that
    # ordinary speech opens the compressor and room tone does not. attack is short so the
    # bed is already down on the first syllable; release is long so it does not pump
    # between words - it should feel like the bed breathing, not stuttering.
    parts.append(
        f"[bedv][sckey]sidechaincompress="
        f"threshold=0.03:ratio={ratio:g}:attack=20:release=350:makeup=1[bedduck]"
    )
    parts.append(
        f"[spmix][bedduck]amix=inputs=2:duration=first:normalize=0[{out_label}]"
    )
    return ";".join(parts)


# --------------------------------------------------------------------------- #
# Loudness normalisation (AU1)
# --------------------------------------------------------------------------- #
#: Integrated-loudness targets per publish platform, in LUFS.
#:
#: A clip quieter than the platform's target is turned *up* on playback, which lifts its
#: noise floor along with the speech; one that is louder is turned down, wasting the
#: headroom it was mastered with. Either way the creator loses control of the result.
#:
#: Values follow the reported platform targets: YouTube normalises to about -14 LUFS, while
#: TikTok and Instagram sit nearer -11. Anything unlisted uses
#: ``settings.loudness_target_lufs``.
PLATFORM_LUFS: dict[str, float] = {
    "youtube": -14.0,
    "tiktok": -11.0,
    "instagram": -11.0,
}

#: Loudness range passed to ``loudnorm``. 11 LU is its own default and suits speech; a
#: wider range lets a shouty passage stay shouty, which is usually not what a clip wants.
_LOUDNORM_LRA = 11.0


def platform_loudness_target(platform: str) -> float:
    """The LUFS target for ``platform``, falling back to the configured default."""
    return PLATFORM_LUFS.get((platform or "").strip().lower(), settings.loudness_target_lufs)


@dataclass(frozen=True)
class LoudnessStats:
    """First-pass ``loudnorm`` measurements for one file."""

    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float


def measure_loudness(source: str | Path) -> Optional[LoudnessStats]:
    """Measure ``source``'s loudness with ``loudnorm``'s analysis pass (AU1).

    This is the first of the two passes. Single-pass ``loudnorm`` has to guess as it goes,
    so it compresses dynamics to hit the target and the first seconds of a clip are
    normalised on less information than the rest. Measuring first lets the second pass
    apply one linear gain, which reaches the target without touching dynamics.

    Decodes but encodes nothing (``-f null``). Returns ``None`` on any failure - no audio
    track, a corrupt file, an ffmpeg without ``loudnorm`` - so the caller renders without
    normalisation instead of failing the clip.
    """
    cmd = [
        settings.ffmpeg_binary, "-nostdin", "-hide_banner", "-i", str(source),
        "-af", f"loudnorm=I={settings.loudness_target_lufs}:"
               f"TP={settings.loudness_true_peak_db}:LRA={_LOUDNORM_LRA}:print_format=json",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception:
        return None
    if proc.returncode != 0:
        return None

    # loudnorm prints its JSON block at the end of stderr, after the filter's own log
    # lines. Taking the last '{' onwards is deliberate: a path in an earlier log line can
    # contain braces, and json.loads on the whole stderr would fail.
    stderr = proc.stderr or ""
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
        return LoudnessStats(
            input_i=float(data["input_i"]),
            input_tp=float(data["input_tp"]),
            input_lra=float(data["input_lra"]),
            input_thresh=float(data["input_thresh"]),
            target_offset=float(data["target_offset"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


def true_peak_limit_filter(ceiling_db: Optional[float] = None) -> str:
    """A true-peak limiter for the end of the audio chain (AU3).

    ``loudnorm`` *targets* a true-peak ceiling, and in linear mode it reduces its gain to
    respect one - but that only helps on the path where it runs. With normalisation disabled,
    or when the source could not be measured, nothing constrained the output at all: a hot
    source plus a music bed sums straight past full scale. Measured on a mix of a -0.1 dBFS
    source and a bed, the result reached **+5.5 dBFS true peak**; with this filter, -1.0.

    ``level=disabled`` is the important argument. ``alimiter``'s ``level`` defaults to *on*,
    which auto-levels the output up to the ceiling - so the default configuration of a filter
    whose job is to make audio quieter when necessary would instead make quiet audio *louder*,
    undoing the loudness normalisation immediately upstream of it.

    Applied unconditionally at the end of a changed audio chain rather than only when
    normalisation is off: a limiter that never engages is inaudible, and the alternative is
    reasoning about whether ``loudnorm``'s estimate covered inter-sample peaks.
    """
    ceiling = settings.loudness_true_peak_db if ceiling_db is None else ceiling_db
    # alimiter's limit is a linear amplitude, not dB.
    limit = 10.0 ** (float(ceiling) / 20.0)
    limit = max(0.001, min(1.0, limit))
    return f"alimiter=limit={limit:.4f}:level=disabled"


def loudnorm_filter(stats: LoudnessStats, target_lufs: float) -> str:
    """The second-pass ``loudnorm`` filter for ``stats`` (AU1).

    ``linear=true`` is the point of having measured: it applies a single gain across the
    whole clip rather than riding the level, so speech dynamics survive. ffmpeg falls back
    to dynamic mode by itself if the measurements make linear normalisation impossible
    (a clip whose peaks would clip the true-peak ceiling), which is the right trade in that
    case and needs no handling here.
    """
    return (
        f"loudnorm=I={target_lufs:g}:TP={settings.loudness_true_peak_db:g}"
        f":LRA={_LOUDNORM_LRA:g}"
        f":measured_I={stats.input_i:g}:measured_TP={stats.input_tp:g}"
        f":measured_LRA={stats.input_lra:g}:measured_thresh={stats.input_thresh:g}"
        f":offset={stats.target_offset:g}:linear=true:print_format=summary"
    )
