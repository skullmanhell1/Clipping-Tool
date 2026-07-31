"""Audio energy features for clip selection (S2).

Selection had no audio signal of any kind until S4 added speech rate from the word timings.
Speech rate is free but it is a *transcript* measurement: it says how densely someone spoke,
not how they sounded. Energy is the cheapest genuinely acoustic signal, and it separates cases
speech rate cannot - a shouted line and a muttered one at the same pace, a laugh, a room going
quiet before a punchline.

**One pass over the whole source, not one per candidate.** ``astats`` is preceded by
``asetnsamples`` so the decoder's variable frame size is regrouped into fixed windows, which is
what makes a reading comparable across sources and across time. Without it the window length is
whatever the codec happened to emit - roughly 21 ms for AAC, different for Opus, different again
after a re-encode - and "RMS per frame" would silently mean a different thing per file.

**dBFS, not linear amplitude.** ``astats`` reports RMS in dB, and the ratio that matters to a
listener is logarithmic: the step from -40 to -30 dB is the same perceived change as -20 to -10,
while the linear difference is a hundred times smaller. Comparing linear means would make every
quiet passage look identical.

**Nothing here changes ranking on the LLM path.** As with S4, the features are attached so they
can be measured against the S1 benchmark and shown to the model (S10). They *do* feed the
deterministic fallback's scoring (S11), because the thing they replace there - "keep the longest
segments" - is indefensible on its own terms, so any measured signal is an improvement over it
regardless of what the benchmark later says about the weights.
"""

from __future__ import annotations

import re
import statistics
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from config import settings

#: Length of one envelope reading, in seconds.
#:
#: One second is a deliberate compromise. Shorter windows resolve individual words and make the
#: envelope noisy for no benefit, since a clip boundary is never chosen to word precision.
#: Longer windows blur exactly the events worth detecting - a laugh, a shout, a sudden hush.
ENVELOPE_WINDOW_S = 1.0

#: What ``-inf`` becomes.
#:
#: ``astats`` reports digital silence as ``-inf``, verified directly against ffmpeg 7.0.2.
#: Carrying that into arithmetic poisons every mean and median it touches, and a caller
#: comparing it against a threshold gets an answer that is technically correct and useless.
#: -91 dBFS is below the noise floor of any real recording, so it orders correctly against
#: genuine quiet without being infinite.
SILENCE_FLOOR_DB = -91.0

#: A window quieter than this is treated as containing no speech worth measuring.
QUIET_DB = -50.0

_META_RE = re.compile(
    r"pts_time:\s*([0-9.]+)\s*\n\s*lavfi\.astats\.Overall\.RMS_level\s*=\s*(-?[0-9.]+|-?inf)",
    re.IGNORECASE,
)


def _parse_envelope(log: str) -> list[tuple[float, float]]:
    """Parse ``(timestamp, rms_db)`` pairs out of ffmpeg's metadata log.

    Split out from :func:`energy_envelope` so the parsing can be tested against captured
    ffmpeg output without running ffmpeg - the same split ``worker.scene_detect`` uses, and for
    the same reason: the regex is the part that breaks when ffmpeg's log format shifts.
    """
    readings: list[tuple[float, float]] = []
    for match in _META_RE.finditer(log or ""):
        try:
            t = float(match.group(1))
        except (TypeError, ValueError):
            continue
        raw = match.group(2).lower()
        if raw.endswith("inf"):
            db = SILENCE_FLOOR_DB if raw.startswith("-") else 0.0
        else:
            try:
                db = float(raw)
            except (TypeError, ValueError):
                continue
            if db != db:  # NaN
                continue
        readings.append((t, max(SILENCE_FLOOR_DB, db)))
    return readings


def energy_envelope(
    path: str | Path,
    *,
    window: float = ENVELOPE_WINDOW_S,
) -> list[tuple[float, float]]:
    """RMS level in dBFS per ``window`` seconds, across the whole source.

    Returns ``[]`` on any failure - no audio stream, an unreadable file, an ffmpeg without
    ``astats``, a timeout. Every caller treats an empty envelope as "no energy information",
    which is the state the product was in before this module existed, so a failure here can
    only cost the signal and never a clip.
    """
    try:
        step = float(window)
    except (TypeError, ValueError):
        return []
    if step <= 0:
        return []

    rate = int(getattr(settings, "output_sample_rate", 48000) or 48000)
    samples = max(1, int(round(rate * step)))
    graph = (
        f"aresample={rate},"
        f"asetnsamples=n={samples}:p=0,"
        "astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
    )
    cmd = [
        settings.ffmpeg_binary,
        "-nostdin", "-hide_banner",
        "-i", str(path),
        "-af", graph,
        "-vn", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception:
        return []
    # ametadata writes to stdout with file=-, but a differently-built ffmpeg may route it to
    # stderr, so both are scanned. Cheaper than being wrong on someone else's build.
    return _parse_envelope((proc.stdout or "") + "\n" + (proc.stderr or ""))


def _readings_in_window(
    envelope: Sequence[tuple[float, float]],
    start: float,
    end: float,
) -> list[float]:
    """The dB readings whose window start falls inside ``[start, end)``."""
    out = []
    for t, db in envelope:
        if start <= t < end:
            out.append(db)
    return out


def source_median_energy(envelope: Sequence[tuple[float, float]]) -> Optional[float]:
    """The source's own median energy in dBFS, ignoring silence.

    Silent windows are excluded before the median is taken. A video with a long silent
    intro, or one where a third of the runtime is a music-free pause, would otherwise get a
    baseline pulled down towards the floor - and then *ordinary speech* would read as
    unusually loud, which inverts the signal precisely on the footage where it matters.
    """
    voiced = [db for _, db in envelope if db > QUIET_DB]
    if not voiced:
        return None
    return statistics.median(voiced)


class Energy:
    """Energy features for one time window.

    Not a frozen dataclass, unlike :class:`worker.selection_features.SpeechRate`, because
    every field is derived from the same list in ``__init__`` and there is nothing for a
    caller to construct field-by-field.
    """

    __slots__ = ("mean_db", "peak_db", "relative_energy", "quiet_fraction", "reliable")

    def __init__(
        self,
        readings: Sequence[float],
        *,
        baseline: Optional[float] = None,
    ) -> None:
        if readings:
            self.mean_db = sum(readings) / len(readings)
            self.peak_db = max(readings)
            quiet = sum(1 for db in readings if db <= QUIET_DB)
            self.quiet_fraction = quiet / len(readings)
            self.reliable = True
        else:
            self.mean_db = SILENCE_FLOOR_DB
            self.peak_db = SILENCE_FLOOR_DB
            self.quiet_fraction = 1.0
            self.reliable = False

        if baseline is not None and self.reliable:
            # A *difference* in dB, not a ratio. dB is already logarithmic, so subtracting is
            # the ratio; dividing two dB figures is meaningless and changes sign around 0 dBFS.
            self.relative_energy = self.mean_db - baseline
        else:
            self.relative_energy = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "energy_mean_db": round(self.mean_db, 2),
            "energy_peak_db": round(self.peak_db, 2),
            "relative_energy_db": round(self.relative_energy, 2),
            "quiet_fraction": round(self.quiet_fraction, 3),
            "energy_reliable": 1.0 if self.reliable else 0.0,
        }


def energy_in_window(
    envelope: Sequence[tuple[float, float]],
    start: float,
    end: float,
    *,
    baseline: Optional[float] = None,
) -> Energy:
    """Energy features for ``[start, end]`` (S2). Pure - the envelope is already measured."""
    return Energy(_readings_in_window(envelope, float(start), float(end)), baseline=baseline)


def detect_onsets(
    envelope: Sequence[tuple[float, float]],
    *,
    rise_db: float = 6.0,
    min_gap: float = 0.6,
) -> list[float]:
    """Times where the level jumps sharply - a usable beat/accent proxy (V19).

    **This is onset detection, not beat tracking, and the difference matters.** Real beat
    tracking estimates a tempo and a phase, and can place a beat where no sound occurred.
    This only reports moments where the energy actually rose by ``rise_db`` between adjacent
    readings, which is a weaker claim and a true one: every returned time has a real transient
    at it. On speech-led footage - which is what this tool is pointed at - there is often no
    steady tempo to track at all, so the stronger algorithm would mostly be inventing structure.

    It also costs nothing: the envelope is already measured for S2, so this is one pass over a
    short list rather than any new decode.

    ``min_gap`` suppresses the run of consecutive readings a single loud event produces, keeping
    only its first - otherwise one door slam becomes four "beats" a second apart.
    """
    onsets: list[float] = []
    previous_db: Optional[float] = None
    for t, db in envelope:
        if previous_db is not None and (db - previous_db) >= rise_db:
            if not onsets or (t - onsets[-1]) >= min_gap:
                onsets.append(round(float(t), 3))
        previous_db = db
    return onsets


def annotate_candidates(
    candidates: Iterable[Any],
    envelope: Sequence[tuple[float, float]],
) -> None:
    """Attach energy features to each candidate's ``features`` dict, in place (S2).

    Never touches ``score``, matching the invariant S4 established and its test pins: the
    annotators measure, and only the fallback's own scorer decides.
    """
    items = list(candidates)
    if not items or not envelope:
        return
    baseline = source_median_energy(envelope)
    for candidate in items:
        features = getattr(candidate, "features", None)
        if features is None:
            continue
        energy = energy_in_window(envelope, candidate.start, candidate.end, baseline=baseline)
        features.update(energy.to_dict())
        if baseline is not None:
            features["source_median_db"] = round(baseline, 2)
