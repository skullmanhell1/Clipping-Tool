"""Sound-effect stings on transitions and emoji (AU9).

A cut, a zoom or an emoji popping onto the frame is a visual accent with no audible counterpart, so
the edit reads as a picture change rather than as a beat. A short sting on those moments is most of
what makes a short-form edit feel deliberate.

**What is synthesised, and what is not.** The distinction matters more here than the feature does,
because A15 already recorded what goes wrong when it is blurred: the synthesised "music bed" is two
sine tones with tremolo, it was reported as ``music:<mood>``, and the limitation was invisible.

* **``pop`` and ``click`` are synthesised honestly.** A pop *is* a short band-passed noise burst with
  a fast attack and a short decay; a click is the same thing higher and shorter. Generating one is
  not an approximation of the real thing, it is the real thing. No degradation marker, because there
  is no degradation.
* **``whoosh`` and ``swipe`` are not synthesised at all.** A whoosh is noise under a filter that
  *moves* across the sound, and ffmpeg cannot express a time-varying filter frequency in a single
  pass - ``bandpass`` takes no expression and has no ``eval=frame``. A static band-passed noise
  swell is a hiss, and shipping a hiss called "whoosh" is exactly the mislabelling A15 exists to
  stop. These require a file in ``SFX_DIR``; without one the sting is skipped and the clip records
  ``sfx_missing:<name>``, so the absence has a reason attached to it.

Stings are mixed at a low level and are **off by default**: an audible change to every clip is not
something to acquire by upgrading.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from config import settings
from worker.ffmpeg_utils import _run

logger = logging.getLogger(__name__)

#: Audio extensions accepted for a user-supplied sting.
_AUDIO_EXTS: tuple[str, ...] = (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac")

#: The sting names this module knows.
#:
#: ``synthesisable`` is the whole content of the module docstring's distinction: a pop can be
#: generated because a pop *is* a filtered noise burst, and a whoosh cannot because it needs a
#: moving filter.
SFX_NAMES: dict[str, bool] = {
    "pop": True,
    "click": True,
    "whoosh": False,
    "swipe": False,
}

#: Which sting each trigger uses.
TRIGGER_SFX: dict[str, str] = {
    "emoji": "pop",
    "transition": "whoosh",
}

SOURCE_USER_FILE = "user_file"
SOURCE_SYNTHESISED = "synthesised"


@dataclass(frozen=True)
class Sting:
    """One resolved sound effect, and where it came from."""

    name: str
    path: Path
    source: str

    @property
    def synthesised(self) -> bool:
        return self.source == SOURCE_SYNTHESISED


@dataclass(frozen=True)
class SfxHit:
    """A sting placed at a moment in the clip."""

    at: float
    sting: Sting
    trigger: str


def find_user_sting(name: str) -> Path | None:
    """A user-supplied ``sfx_dir/<name>.<ext>``, or ``None``."""
    base = Path(getattr(settings, "sfx_dir", "") or "")
    if not base.is_dir():
        return None
    for ext in _AUDIO_EXTS:
        candidate = base / f"{name}{ext}"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def synth_filter(name: str) -> str | None:
    """The lavfi source description that generates ``name``, or ``None`` if it cannot be.

    Kept as a filter string rather than a rendered asset so the parameters are reviewable in one
    place. Both stings are band-passed white noise: the difference is the band, the length and the
    envelope, which is also the difference between a pop and a click in reality.
    """
    if name == "pop":
        return (
            "anoisesrc=color=white:sample_rate=48000:duration=0.1,"
            # ~1.4 kHz is where a percussive pop sits; a wide-ish Q keeps it from ringing.
            "bandpass=f=1400:width_type=o:w=1.6,"
            # A 4 ms attack so it does not click, then a short decay: a slower release turns a pop
            # into a thud, and a faster one into the click below.
            "afade=t=in:st=0:d=0.004,afade=t=out:st=0.02:d=0.075"
        )
    if name == "click":
        return (
            "anoisesrc=color=white:sample_rate=48000:duration=0.05,"
            "highpass=f=2500,"
            "afade=t=out:st=0.002:d=0.045"
        )
    return None


def resolve_sting(name: str, temp_dir: str | Path) -> tuple[Sting | None, str]:
    """Resolve ``name`` to a file, returning ``(sting, marker)``.

    A user file always wins: it is a real recording, and the synthesised versions exist so that the
    feature works at all without one, not so that they are preferred.
    """
    user = find_user_sting(name)
    if user is not None:
        return Sting(name, user, SOURCE_USER_FILE), ""

    if not SFX_NAMES.get(name, False):
        # See the module docstring: not synthesisable, and a static-band stand-in would be a hiss
        # under a name that promises a sweep.
        return None, f"sfx_missing:{name}"

    description = synth_filter(name)
    if description is None:
        return None, f"sfx_missing:{name}"

    dest = Path(temp_dir) / f"sfx_{name}.wav"
    if not (dest.exists() and dest.stat().st_size > 0):
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run(
                [
                    settings.ffmpeg_binary,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    description,
                    "-ar",
                    "48000",
                    "-ac",
                    "1",
                    str(dest),
                ]
            )
        except Exception as exc:  # noqa: BLE001 - a sting is never worth losing a clip over
            logger.warning("AU9: could not synthesise the %s sting: %s", name, exc)
            return None, f"sfx_missing:{name}"
    return Sting(name, dest, SOURCE_SYNTHESISED), ""


def plan_hits(
    *,
    emoji_starts: Sequence[float] = (),
    transition_times: Sequence[float] = (),
    duration: float,
    mode: str = "",
    min_gap: float = 0.35,
) -> list[tuple[float, str]]:
    """The ``(time, trigger)`` moments that should carry a sting (AU9).

    ``mode`` is ``off`` | ``emoji`` | ``transitions`` | ``both``.

    Two stings within ``min_gap`` of each other read as a stutter rather than as two accents, so the
    later one is dropped. Transitions win a contested slot because a cut is a structural moment and
    an emoji is decoration - and an emoji is far more likely to have another one along shortly.
    """
    setting = str(mode or getattr(settings, "sfx_mode", "off") or "off").strip().lower()
    # One table rather than a guard plus two membership tests. With the mode interpreted in three
    # places, the guard was provably redundant - "off" also failed both membership tests - so
    # removing it changed nothing, which means it was not protecting anything either. Here the
    # lookup *is* the interpretation, and an unrecognised mode has exactly one meaning.
    sources = {
        "emoji": (("emoji", emoji_starts),),
        "transitions": (("transition", transition_times),),
        "both": (("transition", transition_times), ("emoji", emoji_starts)),
    }.get(setting)
    if sources is None:
        return []

    candidates: list[tuple[float, str]] = [
        (float(at), trigger) for trigger, times in sources for at in times
    ]

    priority = {"transition": 0, "emoji": 1}
    in_range = sorted(
        ((at, trigger) for at, trigger in candidates if 0.0 <= at < duration),
        key=lambda item: (item[0], priority.get(item[1], 9)),
    )
    if not in_range:
        return []

    # Clustered rather than filtered in one pass. A single pass keeping the *earliest* of two close
    # candidates would drop a transition at 1.05 s in favour of an emoji at 1.00 s - and a cut is a
    # structural moment while an emoji is decoration, so the priority has to be applied *within* the
    # gap window, not only at exactly equal times.
    clusters: list[list[tuple[float, str]]] = [[in_range[0]]]
    for item in in_range[1:]:
        if item[0] - clusters[-1][0][0] < min_gap:
            clusters[-1].append(item)
        else:
            clusters.append([item])

    kept: list[tuple[float, str]] = []
    for cluster in clusters:
        # Best priority, earliest time - so a cluster of one emoji and one transition keeps the
        # transition at its own moment rather than at the emoji's.
        kept.append(min(cluster, key=lambda item: (priority.get(item[1], 9), item[0])))
    return kept


def build_mix(
    hits: Iterable[SfxHit],
    speech_label: str,
    out_label: str,
    *,
    input_offset: int,
    volume: float,
) -> tuple[list[str], str]:
    """``(input_args, filter_graph)`` mixing ``hits`` under ``speech_label`` (AU9).

    Each sting is a separate input delayed to its moment with ``adelay``, then everything is
    ``amix``-ed. ``amix`` is given ``normalize=0``: with normalisation on, adding a sting *lowers
    the speech* by 1/n for the whole clip, so a clip with four stings would come back audibly
    quieter than one with none - a global change caused by a local accent.

    The same sting file is reused across its hits rather than added once per hit, so a clip with
    twelve emoji does not open twelve file handles for one 9 kB wav.
    """
    hits = list(hits)
    if not hits:
        return [], ""

    level = max(0.0, min(1.0, float(volume)))
    input_args: list[str] = []
    chains: list[str] = []
    for index, hit in enumerate(hits):
        input_args += ["-i", str(hit.sting.path)]
        chains.append(
            # `all=1` delays every channel. Without it `adelay` delays only the first, so a stereo
            # sting arrives on the left, then again on the right - audible as a flam rather than as
            # one accent.
            f"[{input_offset + index}:a]"
            f"adelay={int(round(max(0.0, hit.at) * 1000))}:all=1,"
            f"volume={level:.3f}[sfx{index}]"
        )

    sting_labels = "".join(f"[sfx{index}]" for index in range(len(hits)))
    graph = ";".join(chains)
    graph += (
        f";[{speech_label}]{sting_labels}"
        # `normalize=0` matters: with normalisation on, `amix` divides every input by the number of
        # inputs, so adding one sting would make the *speech* 1/n quieter for the whole clip. A
        # local accent must not change the global level.
        #
        # `duration=first` keeps the clip's own length: a sting near the end would otherwise extend
        # the audio past the video.
        f"amix=inputs={len(hits) + 1}:duration=first:dropout_transition=0:normalize=0"
        f"[{out_label}]"
    )
    return input_args, graph
