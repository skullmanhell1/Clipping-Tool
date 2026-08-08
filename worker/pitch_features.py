"""Pitch variation as a selection feature (S3).

Selection had speech rate (S4) and energy (S2). Both are real acoustic-ish signals and neither
separates a *monotone* delivery from an animated one: someone can speak fast and loud in a flat
drone, and the two score alike. Pitch variation is what distinguishes them, and animated delivery
is one of the things commercial clippers explicitly say they weight.

**No model, no network, one pass** (R4.3, R4.7). Pitch is estimated by autocorrelation over a
16 kHz mono WAV.

An earlier version of this note claimed the WAV already existed, on the grounds that
`ffmpeg_utils.extract_audio` writes exactly that format for transcription. **It does not.** Nothing
in `worker/` calls `extract_audio` — faster-whisper decodes the media itself, and that helper has
only ever been exercised by tests. So the extraction is a real additional pass, which is what R4.7
allows ("at most one additional pass over the source audio per job") rather than the stronger
guarantee originally written here. Recorded because an inaccurate claim in a docstring is worse than
no claim: the next person would have believed it.

**This module spawns nothing.** The extraction belongs to the caller
(`selection.select_moments`, memoised by source content like the energy envelope), which keeps this
file a pure function of its samples and therefore testable without a media fixture or a fake
subprocess.

**Measured against the speaker's own median** (R4.2). Absolute F0 says whether a voice is high or
low, which is a fact about the person and not about the moment. What matters is departure from
*their* norm, so everything here is expressed in **semitones from the source median** — a
logarithmic scale, because pitch perception is logarithmic and a 20 Hz excursion means something
very different at 90 Hz than at 250 Hz.

**Deterministic** (R4.8). Fixed frame size, fixed hop, integer lag search, no randomness, no
threading, no floating-point reduction whose order could vary. Same bytes in, same numbers out, on
any platform.

**Unreliability is reported, not hidden** (R4.5, R4.6). A window that is mostly silence, music or
noise has no meaningful F0, and inventing one would let a passage score well for being unmeasurable.
An unreliable reading is marked and the selector treats it as neutral rather than as zero.
"""

from __future__ import annotations

import statistics
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

#: Analysis frame length in seconds. 40 ms holds at least two periods of the lowest pitch this
#: searches for (70 Hz -> 14.3 ms), which autocorrelation needs to find a peak at all.
FRAME_S = 0.040

#: Hop between frames. 20 ms gives 50 readings a second: enough to see a pitch excursion inside a
#: word, cheap enough to run over a whole source in pure Python.
HOP_S = 0.020

#: F0 search bounds in Hz. Deliberately wide enough for both a low male voice and a raised female
#: one, and deliberately *not* wider: extending downwards invites the octave error that
#: autocorrelation is prone to, where twice the true period scores nearly as well as the period.
F0_MIN_HZ = 70.0
F0_MAX_HZ = 400.0

#: Minimum normalised autocorrelation peak for a frame to count as voiced.
#:
#: Below this the frame is silence, noise, music or an unvoiced consonant, and its "pitch" is an
#: artefact of whatever happened to correlate. 0.35 is permissive enough to keep quiet speech and
#: strict enough to reject room tone.
VOICING_THRESHOLD = 0.35

#: Fraction of a window's frames that must be voiced for its reading to be reliable (R4.5).
#:
#: A third. Conversational speech is roughly 40-60% voiced once pauses and unvoiced consonants are
#: counted, so this admits normal speech while rejecting a window that is mostly music or silence.
MIN_VOICED_FRACTION = 1.0 / 3.0

#: Minimum voiced frames in absolute terms, regardless of fraction.
#:
#: A 1-second window at a 20 ms hop has 50 frames; six voiced frames is 120 ms of pitch, which is
#: about one syllable. Fewer than that is not a distribution, it is a sample.
MIN_VOICED_FRAMES = 6

#: Semitone spread that counts as "fully animated", for scaling onto [0, 1].
#:
#: Around 6 semitones of interquartile spread is emphatic, expressive delivery; a monotone reading
#: sits near 1. PROVISIONAL: this is a scaling choice, not a measurement, and only the S1 benchmark
#: could justify a different value.
ANIMATED_SEMITONES = 6.0


@dataclass(frozen=True)
class Pitch:
    """A window's pitch behaviour, or an explicit statement that it could not be measured."""

    median_hz: float = 0.0
    #: Interquartile spread in semitones relative to the *source* median (R4.2).
    variation_semitones: float = 0.0
    #: The same, scaled onto [0, 1] for the ranking blend.
    variation: float = 0.0
    voiced_frames: int = 0
    reliable: bool = False

    def as_features(self) -> dict[str, float]:
        """Flat floats, matching the shape the other feature modules attach."""
        if not self.reliable:
            # No numbers at all when unreliable. A 0.0 that looks measured is worse than an
            # absence -- R3.5's "treat as neutral" cannot be applied to a value it cannot
            # distinguish from a real reading of zero.
            return {"pitch_reliable": 0.0}
        return {
            "pitch_reliable": 1.0,
            "pitch_median_hz": round(self.median_hz, 2),
            "pitch_variation_semitones": round(self.variation_semitones, 3),
            "pitch_variation": round(self.variation, 4),
        }


def read_mono_wav(path: str | Path) -> tuple[list[int], int]:
    """Read a 16-bit mono PCM WAV into samples and its sample rate.

    Uses the standard library rather than numpy, so the read cannot vary with a numpy version and
    the determinism claim in the module docstring holds without qualification. `extract_audio`
    already writes exactly this format (`pcm_s16le`, `-ac 1`), so no conversion is needed.
    """
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM, got {handle.getsampwidth() * 8}-bit")
        if handle.getnchannels() != 1:
            raise ValueError(f"expected mono, got {handle.getnchannels()} channels")
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    return list(samples), rate


def _frame_f0(frame: Sequence[int], rate: int) -> tuple[float, float]:
    """``(f0_hz, confidence)`` for one frame by normalised autocorrelation.

    Returns ``(0.0, 0.0)`` for a frame with no periodicity. The normalisation is by the zero-lag
    energy, which makes the confidence comparable across loud and quiet frames -- without it a
    loud frame always looks more periodic than a quiet one and the voicing threshold would track
    volume instead of pitch.
    """
    n = len(frame)
    if n < 2:
        return 0.0, 0.0

    # Remove DC. A constant offset correlates perfectly with itself at every lag and would make
    # silence look strongly periodic.
    mean = sum(frame) / n
    signal = [s - mean for s in frame]

    energy = sum(s * s for s in signal)
    if energy <= 0:
        return 0.0, 0.0

    min_lag = max(2, int(rate / F0_MAX_HZ))
    max_lag = min(n - 1, int(rate / F0_MIN_HZ))
    if max_lag <= min_lag:
        return 0.0, 0.0

    best_lag, best_score = 0, 0.0
    for lag in range(min_lag, max_lag + 1):
        total = 0.0
        for i in range(n - lag):
            total += signal[i] * signal[i + lag]
        score = total / energy
        if score > best_score:
            best_lag, best_score = lag, score

    if best_lag == 0 or best_score < VOICING_THRESHOLD:
        return 0.0, max(0.0, best_score)
    return rate / float(best_lag), best_score


def f0_track(
    samples: Sequence[int], rate: int, *, start_s: float = 0.0, end_s: float | None = None
) -> list[tuple[float, float]]:
    """``(time_s, f0_hz)`` for every voiced frame in ``[start_s, end_s]``.

    Unvoiced frames are omitted rather than recorded as zero, so a caller counting the result gets
    the voiced-frame count directly and cannot accidentally average zeros into a median.
    """
    if rate <= 0 or not samples:
        return []
    frame_len = max(2, int(FRAME_S * rate))
    hop = max(1, int(HOP_S * rate))

    first = max(0, int(start_s * rate))
    last = len(samples) if end_s is None else min(len(samples), int(end_s * rate))

    track: list[tuple[float, float]] = []
    position = first
    while position + frame_len <= last:
        f0, _confidence = _frame_f0(samples[position : position + frame_len], rate)
        if f0 > 0:
            track.append((position / float(rate), f0))
        position += hop
    return track


def _semitones(hz: float, reference_hz: float) -> float:
    """Interval between two frequencies in semitones. Logarithmic, as pitch perception is."""
    if hz <= 0 or reference_hz <= 0:
        return 0.0
    import math

    return 12.0 * math.log2(hz / reference_hz)


def source_median_f0(track: Sequence[tuple[float, float]]) -> Optional[float]:
    """The speaker's own baseline pitch (R4.2), or ``None`` when there is too little voiced audio.

    Median rather than mean: a single octave-error frame at twice the true F0 would drag a mean
    noticeably and leaves a median untouched.
    """
    values = [hz for _t, hz in track if hz > 0]
    if len(values) < MIN_VOICED_FRAMES:
        return None
    return statistics.median(values)


def pitch_in_window(
    track: Sequence[tuple[float, float]],
    start: float,
    end: float,
    *,
    source_median: Optional[float],
) -> Pitch:
    """Pitch variation for one candidate window, relative to the source median."""
    if end <= start or source_median is None or source_median <= 0:
        return Pitch()

    inside = [hz for t, hz in track if start - 1e-9 <= t < end + 1e-9 and hz > 0]

    # Reliability is about how much voiced audio the window actually contained, judged against both
    # an absolute floor and a fraction of what the window could have held (R4.5).
    expected_frames = max(1, int((end - start) / HOP_S))
    enough_absolute = len(inside) >= MIN_VOICED_FRAMES
    enough_relative = len(inside) >= expected_frames * MIN_VOICED_FRACTION
    if not (enough_absolute and enough_relative):
        return Pitch(voiced_frames=len(inside), reliable=False)

    deviations = sorted(_semitones(hz, source_median) for hz in inside)
    # Interquartile spread rather than standard deviation: octave errors and the occasional
    # creaky-voice frame are outliers, and an IQR is indifferent to them where a standard
    # deviation is not.
    lower = deviations[len(deviations) // 4]
    upper = deviations[(len(deviations) * 3) // 4]
    spread = max(0.0, upper - lower)

    return Pitch(
        median_hz=statistics.median(inside),
        variation_semitones=spread,
        variation=min(1.0, spread / ANIMATED_SEMITONES),
        voiced_frames=len(inside),
        reliable=True,
    )


def describe(pitch: Pitch) -> str:
    """A qualitative phrase for the LLM prompt (R4.9), never a number.

    R4.9 asks for "a qualitative departure from the speaker's norm rather than a number", and the
    reason is worth keeping in view: a model shown `pitch_variation: 0.62` has no way to know
    whether that is high, and will confabulate a scale. A phrase carries the comparison the number
    only implies.
    """
    if not pitch.reliable:
        return ""
    if pitch.variation_semitones >= 5.0:
        return "very animated delivery"
    if pitch.variation_semitones >= 3.0:
        return "animated delivery"
    if pitch.variation_semitones >= 1.5:
        return "moderate intonation"
    return "flat, monotone delivery"


def annotate_candidates(
    candidates: Iterable[Any],
    track: Sequence[tuple[float, float]],
    *,
    source_median: Optional[float] = None,
) -> None:
    """Attach pitch features to each candidate's ``features`` dict (R4.4). Mutates in place.

    Matches `audio_features.annotate_candidates` and `selection_features.annotate_candidates`
    exactly in shape, so the three feature families compose rather than each needing their own
    call convention.
    """
    baseline = source_median if source_median is not None else source_median_f0(track)
    for candidate in candidates:
        features = getattr(candidate, "features", None)
        if features is None:
            continue
        try:
            start = float(candidate.start)
            end = float(candidate.end)
        except (AttributeError, TypeError, ValueError):
            continue
        features.update(pitch_in_window(track, start, end, source_median=baseline).as_features())
