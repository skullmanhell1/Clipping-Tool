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

import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from config import settings
from worker.ffmpeg_utils import _run, escape_filter_path

# Per-mood synthesis parameters: two tones (root + interval) and a tremolo rate.
# Frequencies are chosen to be pleasant and unobtrusive; this is a mood *bed*,
# not a melody, so it never competes with speech.
_MOOD_SYNTH: dict[str, dict[str, float]] = {
    "upbeat": {"root": 293.66, "fifth": 440.00, "tremolo": 5.0, "cutoff": 3200},
    "chill": {"root": 220.00, "fifth": 329.63, "tremolo": 2.0, "cutoff": 2200},
    "dramatic": {"root": 130.81, "fifth": 196.00, "tremolo": 1.2, "cutoff": 1800},
    "corporate": {"root": 261.63, "fifth": 392.00, "tremolo": 3.0, "cutoff": 2600},
    "suspense": {"root": 110.00, "fifth": 164.81, "tremolo": 0.8, "cutoff": 1400},
}

_AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac")


def available_moods() -> list[str]:
    """Return the list of supported music moods."""
    return list(_MOOD_SYNTH.keys())


def find_user_tracks(mood: str) -> list[Path]:
    """Every user-supplied track for ``mood``, in a stable order (A17).

    One track per mood meant every clip in a batch of ten carried the *same* bed. That is the
    single most obvious way a set of clips reads as machine-produced - a viewer scrolling a
    creator's feed hears the same eight bars under all of them.

    Three layouts are accepted, because the natural way to hold twenty tracks is not the natural
    way to hold one:

    * ``music_dir/<mood>.mp3`` - the original single-file layout, still first;
    * ``music_dir/<mood>/*.mp3`` - a directory per mood, which is what a real library looks like;
    * ``music_dir/<mood>_2.mp3``, ``<mood>-3.mp3``, ``<mood> 4.mp3`` - numbered siblings.

    Sorted by name rather than by mtime or directory order. Both of those change when files are
    copied between machines, and this list has to be identical on two installs for the selection
    below to be reproducible at all.
    """
    base = Path(settings.music_dir)
    if not base.is_dir():
        return []

    exact: list[Path] = []
    for ext in _AUDIO_EXTS:
        candidate = base / f"{mood}{ext}"
        if candidate.is_file():
            exact.append(candidate)

    variants: list[Path] = []
    mood_dir = base / mood
    if mood_dir.is_dir():
        variants += [
            path
            for path in mood_dir.iterdir()
            if path.is_file() and path.suffix.lower() in _AUDIO_EXTS
        ]
    for path in base.iterdir():
        if not path.is_file() or path.suffix.lower() not in _AUDIO_EXTS:
            continue
        stem = path.stem
        # `<mood>` followed by a separator, so "upbeat_2" matches and "upbeatish" does not - a
        # prefix test alone would pull an unrelated mood's tracks into this one.
        if len(stem) > len(mood) and stem[: len(mood)] == mood and stem[len(mood)] in "_- ":
            variants.append(path)

    # The exact-name file first so a single-track install is byte-identical to before, then the
    # rest by name.
    return exact + sorted(variants, key=lambda p: (p.name, str(p)))


def find_user_track(mood: str) -> Path | None:
    """The first user-supplied track for ``mood``, or ``None``.

    Retained because it is the published single-track entry point; :func:`find_user_tracks` is
    what A17 selects from.
    """
    tracks = find_user_tracks(mood)
    return tracks[0] if tracks else None


def choose_track(tracks: Sequence[Path], select_key: str) -> Path | None:
    """Pick one of ``tracks`` deterministically from ``select_key`` (A17).

    **Deterministic, not random.** A batch wants variety *between* clips, and re-running the same
    job wants the same output - the M1 golden renders depend on the second, and a creator
    re-rendering one clip of ten to fix a typo does not want a different bed under it. Random
    selection gives variety and loses reproducibility; hashing a stable key gives both.

    The hash is **blake2b, not** :func:`hash`. Python's ``hash`` of a string is salted per process
    unless ``PYTHONHASHSEED`` is set, so a ``hash(key) % len(tracks)`` selection would be stable
    within one run and different on the next - reproducible in exactly the tests that would catch
    it and not in production.

    An empty ``select_key`` returns the first track, which is the pre-A17 behaviour.
    """
    if not tracks:
        return None
    if len(tracks) == 1 or not select_key:
        return tracks[0]
    digest = hashlib.blake2b(select_key.encode("utf-8"), digest_size=8).digest()
    return tracks[int.from_bytes(digest, "big") % len(tracks)]


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
        settings.ffmpeg_binary,
        "-y",
        "-filter_complex",
        graph,
        "-map",
        "[bed]",
        "-t",
        f"{max(0.1, duration):.3f}",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        str(int(settings.output_sample_rate)),
        "-ac",
        str(int(settings.output_channels)),
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
    #: Which of the mood's tracks this is, and how many there were (A17). ``(0, 0)`` for the
    #: synthesised bed, which is one drone per mood by construction.
    #:
    #: Reported so the *variety* is visible. A17's whole purpose is that two clips in a batch do
    #: not share a bed, and a caller looking at two clip records could not otherwise tell whether
    #: that happened - the paths are not in the record, only the marker is.
    track_index: int = 0
    track_count: int = 0

    @property
    def synthesised(self) -> bool:
        """Whether this bed is the fallback drone rather than a real track."""
        return self.source == SOURCE_SYNTHESISED


def resolve_music_bed(
    mood: str, duration: float, temp_dir: str | Path, *, select_key: str = ""
) -> MusicBed | None:
    """Resolve a bed for ``mood``, reporting whether it is a real track (A15) and which (A17).

    Returns ``None`` when ``mood`` is empty or unknown, or when synthesis is the only
    option and ``settings.music_allow_synthesis`` is off — in which case the clip is
    rendered without music rather than with a drone the caller did not ask for.

    ``select_key`` chooses among several tracks for the mood, deterministically - see
    :func:`choose_track`. Empty means "the first one", which is the pre-A17 behaviour, so a
    caller that does not opt in gets byte-identical output.
    """
    if not mood:
        return None
    tracks = find_user_tracks(mood)
    user = choose_track(tracks, select_key)
    if user is not None:
        return MusicBed(
            path=user,
            mood=mood,
            source=SOURCE_USER_TRACK,
            track_index=tracks.index(user) + 1,
            track_count=len(tracks),
        )
    # A16 note: a user track is returned as-is here and fitted to the clip by
    # :func:`bed_fit_filter` inside the mix, rather than being pre-rendered to length. Cutting a
    # separate correctly-sized file first would cost an extra encode per clip for something the
    # existing mix pass can do in the same graph.
    if mood not in _MOOD_SYNTH:
        return None
    if not settings.music_allow_synthesis:
        return None
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    dest = synthesize_bed(mood, duration, temp_dir / f"synth_bed_{mood}.m4a")
    return MusicBed(path=dest, mood=mood, source=SOURCE_SYNTHESISED)


def resolve_music(mood: str, duration: float, temp_dir: str | Path) -> Path | None:
    """The path-only view of :func:`resolve_music_bed`.

    Kept for callers that only need somewhere to read audio from. Anything that reports
    what happened to a clip must use :func:`resolve_music_bed` instead — a bare path cannot
    distinguish a licensed track from a synthesised drone, which is exactly the gap A15
    closes.
    """
    bed = resolve_music_bed(mood, duration, temp_dir)
    return None if bed is None else bed.path


#: How long a bed takes to fade out at the end of a clip, in seconds (A16).
#:
#: Long enough to read as an ending rather than as a dropout. A bed that stops dead at the final
#: frame is the single most obvious sign a clip was cut by a machine - the music is mid-phrase and
#: simply gone. This cannot make the ending *musical* (that needs beat detection, and a bed cut
#: anywhere is mid-phrase whatever we do), but a fade turns an abrupt stop into a deliberate one.
BED_FADE_OUT_S = 1.2

#: And a shorter fade in, so a bed does not begin mid-note either.
BED_FADE_IN_S = 0.35


def bed_fit_filter(
    label_in: str,
    label_out: str,
    duration: float,
    *,
    fade_in: float = BED_FADE_IN_S,
    fade_out: float = BED_FADE_OUT_S,
) -> str:
    """Loop or trim a music bed to exactly ``duration``, with fades at both ends (A16).

    Three things in order, and the order matters:

    * ``aloop`` repeats the bed indefinitely, because a track shorter than the clip previously
      just stopped part-way through and the rest of the clip played dry - silence appearing
      mid-clip, which reads as a fault rather than as a choice.
    * ``atrim`` + ``asetpts`` cut it back to the clip length. Without resetting the timestamps
      the looped audio keeps the source's own PTS and the mix drifts out of alignment.
    * ``afade`` at each end. The out-fade is positioned from the clip's own duration, so it
      always lands on the ending regardless of how many times the bed looped.

    A clip shorter than the fades gets proportionally shorter ones rather than overlapping
    fades, which would attenuate the whole bed towards silence.
    """
    span = max(0.1, float(duration))
    # Never let the two fades exceed the clip: on a 1-second clip a 1.2s out-fade plus a 0.35s
    # in-fade would overlap and multiply, leaving the bed inaudible.
    fade_in = max(0.0, min(float(fade_in), span * 0.25))
    fade_out = max(0.0, min(float(fade_out), span * 0.5))
    out_start = max(0.0, span - fade_out)

    parts = [
        # -1 = loop forever; the atrim below is what bounds it.
        f"[{label_in}]aloop=loop=-1:size=2147483647",
        f"atrim=0:{span:.3f}",
        "asetpts=N/SR/TB",
    ]
    if fade_in > 0:
        parts.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        parts.append(f"afade=t=out:st={out_start:.3f}:d={fade_out:.3f}")
    return ",".join(parts) + f"[{label_out}]"


def broll_duck_filter(
    label_in: str,
    label_out: str,
    windows: Sequence[tuple[float, float]],
    *,
    amount: float,
    ramp: float = 0.25,
) -> str:
    """Dip ``label_in`` while a b-roll overlay is on screen (A22).

    A b-roll insert is a visual accent with no audible counterpart, so it reads as an image
    dropped on top of the clip rather than as a beat in it. Dipping the bed under it is the
    audio half of the same edit.

    **The bed, not the mix.** Ducking the finished mix would duck the speech, and the speech is
    the reason the clip exists - the b-roll is illustrating what is being said, so burying it
    would invert the point. This is applied to the music bed only, and it is *additional* to the
    AU2 speech duck: during a b-roll window over speech, the bed is under both.

    Built as one ``volume`` expression with ``eval=frame`` rather than a chain of ``volume``
    filters, because a chain multiplies on overlapping windows and two adjacent b-roll cues would
    drive the bed to silence. The expression takes the *deepest* applicable dip instead.

    ``ramp`` fades each dip in and out over that many seconds. A hard step in level is audible as
    a click on a sustained bed, which is a worse artefact than the one this is fixing.
    """
    spans = [
        (max(0.0, float(start)), max(0.0, float(end)))
        for start, end in windows
        if float(end) > float(start)
    ]
    floor = max(0.0, min(1.0, 1.0 - float(amount)))
    if not spans or floor >= 1.0:
        # Nothing to duck, or a zero dip: emit a pass-through relabel rather than a filter, so a
        # disabled feature adds no processing at all to the graph.
        return f"[{label_in}]anull[{label_out}]"

    ramp = max(0.0, float(ramp))
    terms: list[str] = []
    for start, end in spans:
        if ramp <= 0.0:
            terms.append(f"between(t,{start:.3f},{end:.3f})*{1.0 - floor:.3f}")
            continue
        # A trapezoid: rise over `ramp`, hold, fall over `ramp`. Clamped so a window shorter than
        # two ramps still dips (partially) rather than producing a negative hold.
        rise_end = start + ramp
        fall_start = max(rise_end, end - ramp)
        terms.append(
            f"between(t,{start:.3f},{rise_end:.3f})*{1.0 - floor:.3f}"
            f"*(t-{start:.3f})/{ramp:.3f}"
        )
        if fall_start > rise_end:
            terms.append(f"between(t,{rise_end:.3f},{fall_start:.3f})*{1.0 - floor:.3f}")
        terms.append(
            f"between(t,{fall_start:.3f},{end:.3f})*{1.0 - floor:.3f}" f"*({end:.3f}-t)/{ramp:.3f}"
        )

    # max() over the terms, so overlapping windows take the deepest dip instead of compounding.
    depth = terms[0]
    for term in terms[1:]:
        depth = f"max({depth},{term})"
    return f"[{label_in}]volume=volume='1-({depth})':eval=frame[{label_out}]"


def music_mix_filter(
    original_label: str,
    music_label: str,
    out_label: str,
    volume: float,
    duration: float,
    fade: bool = False,
    fade_dur: float = 0.4,
    duck: bool = True,
    broll_windows: Sequence[tuple[float, float]] = (),
    broll_duck: float = 0.0,
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

    ``broll_windows``/``broll_duck`` add A22's dip under each b-roll overlay - see
    :func:`broll_duck_filter`. Zero (the default) leaves the graph exactly as it was.
    """
    vol = max(0.0, min(1.0, volume))
    ratio = max(1.0, float(settings.music_duck_ratio))
    ducking = duck and ratio > 1.0

    parts: list[str] = []
    out_start = max(0.0, duration - fade_dur)

    # --- the bed: fitted to the clip, levelled, then optional fades --------
    #
    # A16: the bed is looped or trimmed to the clip length first. Before this it was mixed as-is
    # with ``amix=duration=first``, which bounded the *mix* to the speech but did nothing about a
    # bed shorter than the clip - that simply ran out, leaving the rest of the clip dry, which
    # sounds like a fault rather than an ending. A bed longer than the clip was cut dead at the
    # final frame instead.
    #
    # ``bed_fit_filter`` carries its own fades, so the ``fade`` flag's fades are not applied to
    # the bed again: two overlapping out-fades multiply and would pull the ending to silence
    # early. The speech keeps the caller's fades exactly as before.
    parts.append(bed_fit_filter(music_label, "bedfit", duration))
    parts.append(f"[bedfit]volume={vol:.3f}[bedlvl]")
    # A22: dip the bed under each b-roll window. Before the AU2 speech duck, so the two compose -
    # a b-roll insert over speech puts the bed under both rather than under whichever ran last.
    parts.append(broll_duck_filter("bedlvl", "bedv", broll_windows, amount=broll_duck))

    # --- the speech: optional fades, then a split when ducking -------------
    speech_chain = f"[{original_label}]"
    if fade:
        parts.append(
            f"[{original_label}]afade=t=in:st=0:d={fade_dur:.3f}"
            f",afade=t=out:st={out_start:.3f}:d={fade_dur:.3f}[orig]"
        )
        speech_chain = "[orig]"

    if not ducking:
        parts.append(f"{speech_chain}[bedv]amix=inputs=2:duration=first:normalize=0[{out_label}]")
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
    parts.append(f"[spmix][bedduck]amix=inputs=2:duration=first:normalize=0[{out_label}]")
    return ";".join(parts)


# --------------------------------------------------------------------------- #
# Loudness normalisation (AU1)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Speech repair: de-noise (AU4) and de-ess (AU5)
# --------------------------------------------------------------------------- #
#
# Both are OFF by default and both are deliberately conservative when on. Noise reduction and
# sibilance reduction are the two processes most likely to make a recording *worse* while
# measurably improving the thing they target: over-reduced noise leaves speech sounding
# underwater and gated, and an aggressive de-esser turns an "s" into a "th". A clip that is
# slightly noisy is publishable; one that sounds processed is not.

#: ``afftdn`` settings per strength: ``(noise_reduction_db, noise_floor_db)``.
#:
#: ``nr`` is how much to remove, ``nf`` the assumed floor. The floor matters more than the
#: reduction: set it too high and the filter treats quiet speech as noise and gates it. These
#: pair a modest reduction with a floor low enough to sit under real room tone.
DENOISE_LEVELS: dict[str, tuple[float, float]] = {
    "light": (6.0, -30.0),
    "standard": (12.0, -25.0),
    "strong": (20.0, -20.0),
}

#: ``deesser`` intensity per strength (its ``i`` parameter, 0..1).
#:
#: Even "strong" is 0.6 rather than 1.0. At full intensity the filter removes so much of the
#: 4-8 kHz band that consonants lose definition, which is a different defect, not a fix.
DEESSER_LEVELS: dict[str, float] = {
    "light": 0.2,
    "standard": 0.4,
    "strong": 0.6,
}


def denoise_filter(strength: str | None = None, model_path: str | None = None) -> str | None:
    """Speech de-noise, or ``None`` when disabled (AU4).

    Uses ``afftdn`` (spectral gating) by default. ``arnndn`` is the better filter and *is*
    compiled into ffmpeg, but it is useless without a trained ``.rnnn`` model, and ffmpeg ships
    none - the models live in a separate repository. So ``arnndn`` is available here only when
    the operator supplies a model, and naming that dependency is the difference between a
    setting that works and one that fails the render on a missing file.

    An ``arnndn`` model that is configured but absent degrades to ``afftdn`` rather than
    failing: the point of de-noising is a publishable clip, and refusing to render one over a
    missing optional model would invert that.
    """
    if strength is None:
        strength = str(getattr(settings, "speech_denoise", "off") or "off")
    strength = strength.strip().lower()
    if strength in ("", "off", "none"):
        return None

    if model_path is None:
        model_path = str(getattr(settings, "speech_denoise_model", "") or "")
    if model_path:
        model = Path(model_path).expanduser()
        if model.is_file():
            return f"arnndn=m='{escape_filter_path(model)}'"

    nr, nf = DENOISE_LEVELS.get(strength, DENOISE_LEVELS["standard"])
    return f"afftdn=nr={nr:g}:nf={nf:g}"


def deesser_filter(strength: str | None = None) -> str | None:
    """Sibilance reduction, or ``None`` when disabled (AU5).

    **This is the de-esser half of AU5 only.** AU5 also asks for de-reverb, and ffmpeg has no
    de-reverb filter - not a filter this build lacks, one that does not exist upstream. Real
    de-reverberation needs spectral deconvolution or a trained model, i.e. an external tool and
    a new dependency, so it is out of scope for an ffmpeg-only pipeline rather than quietly
    approximated. A high-pass and a noise gate are sometimes offered as "de-reverb"; they are
    not, and shipping them under that name would be worse than not shipping it.
    """
    if strength is None:
        strength = str(getattr(settings, "deesser", "off") or "off")
    strength = strength.strip().lower()
    if strength in ("", "off", "none"):
        return None
    intensity = DEESSER_LEVELS.get(strength, DEESSER_LEVELS["standard"])
    return f"deesser=i={intensity:g}"


def speech_repair_chain(*, denoise: str | None = None, deess: str | None = None) -> list[str]:
    """The ordered speech-cleanup filters: de-noise then de-ess (AU4, AU5).

    That order is not arbitrary. De-noising changes the spectrum in exactly the band a de-esser
    keys on, so de-essing first means setting its threshold against a signal that is about to
    change underneath it. Cleaning first, then correcting sibilance, is how the same chain is
    built by hand.

    Returns ``[]`` when both are off, which is the default and leaves the audio graph exactly as
    it was before AU4/AU5.
    """
    return [f for f in (denoise_filter(denoise), deesser_filter(deess)) if f]


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


def _measured(raw: str | float) -> float:
    """One ``loudnorm`` JSON measurement as a float, with negative zero collapsed.

    Typed ``str | float`` because ffmpeg quotes these values (``"input_i" : "-21.87"``) while a
    build that emitted them as JSON numbers would still be valid - and because ``object`` does not
    type-check: ``float()`` accepts ``str | Buffer | SupportsFloat | SupportsIndex``, so the wider
    annotation fails ``mypy .`` outright. A ``ValueError`` or ``TypeError`` from anything else is
    caught by the caller, which returns ``None`` and renders without normalisation.

    ``float("-0.00")`` is ``-0.0``, and ``f"{-0.0:g}"`` is ``"-0"`` - so a measurement ffmpeg
    reported as ``-0.00`` travelled all the way into the emitted filter as ``offset=-0``. That is
    the same gain as ``offset=0`` to ffmpeg and carries no information, but it made the command
    this project builds depend on *which ffmpeg build did the measuring*: the analysis pass prints
    ``"target_offset" : "-0.00"`` on some builds and ``"0.00"`` on others for the identical input.
    ``tests/test_compositor_graph_parity.py`` caught it - the frozen ``loudness_with_music`` graph
    was recorded with ``offset=-0`` and ffmpeg 6.1.1, which is what ubuntu-latest ships, produces
    ``offset=0``, with all four other measurements byte-identical.

    Adding ``0.0`` is the whole trick: under IEEE-754, ``-0.0 + 0.0`` is ``+0.0`` while every other
    value is unchanged. Applied to all five measurements rather than just the offset, because any
    of them can come back as ``-0.00`` - ``input_lra`` is already ``0.0`` for a pure tone and is
    one ffmpeg build away from being ``-0.0``.
    """
    return float(raw) + 0.0


def measure_loudness(source: str | Path) -> LoudnessStats | None:
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
        settings.ffmpeg_binary,
        "-nostdin",
        "-hide_banner",
        "-i",
        str(source),
        "-af",
        f"loudnorm=I={settings.loudness_target_lufs}:"
        f"TP={settings.loudness_true_peak_db}:LRA={_LOUDNORM_LRA}:print_format=json",
        "-f",
        "null",
        "-",
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
            input_i=_measured(data["input_i"]),
            input_tp=_measured(data["input_tp"]),
            input_lra=_measured(data["input_lra"]),
            input_thresh=_measured(data["input_thresh"]),
            target_offset=_measured(data["target_offset"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


def true_peak_limit_filter(ceiling_db: float | None = None) -> str:
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
