"""Per-candidate speech-rate features from existing word timings (S4).

There are no audio features in clip selection at all - verified by grep across the repository:
no pitch, no energy, no speech rate, no laughter. The LLM is shown only ``[i] start-end: text``
lines, so it cannot tell that a moment was delivered fast, slowly, or with a pause before the
punchline.

Speech rate is the cheapest of those signals by a wide margin, because the data already exists:
``word_timestamps=True`` is already set, so every word carries a start and an end. This module
costs one pass over a list.

**Absolute rate is the weak reading, and the one to avoid leaning on.** Speakers differ enormously
- a measured lecturer and an excitable streamer might sit at 2.2 and 3.6 words per second, and
neither number says anything about which *moment* mattered. What carries information is deviation
from that speaker's own baseline: a burst noticeably faster than they usually talk, or a
conspicuous slow-down. So the primary feature here is ``relative_speech_rate``, normalised
against the source's own median, and the absolute figure is reported alongside it for context
rather than as the signal.

**Nothing here changes ranking.** The features are computed and attached to each candidate so
they can be measured against the S1 benchmark and later fed to the model (S10). Wiring a weight
before the benchmark can judge it would be tuning blind: a change that made selection worse would
be indistinguishable from one that made it better.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

#: A window shorter than this is too small for a rate to mean anything: two words in 0.3 s is
#: 6.7 words per second, which describes a measurement artefact rather than fast speech.
MIN_WINDOW_S = 1.0

#: Below this many words a rate is noise for the same reason.
MIN_WORDS = 3


@dataclass(frozen=True)
class SpeechRate:
    """Speech-rate features for one time window."""

    word_count: int
    #: Words per second across the *whole window*, silence included.
    words_per_second: float
    #: ``words_per_second`` divided by the source's median, so 1.0 is this speaker's normal
    #: pace, 1.3 is noticeably faster and 0.7 noticeably slower. ``1.0`` when there is no
    #: usable baseline, which reads as "no information" rather than as a neutral score.
    relative_speech_rate: float
    #: Whether the window met the size thresholds. A caller feeding these to a model or a
    #: weight needs to know the difference between "normal pace" and "not measurable".
    reliable: bool

    def to_dict(self) -> dict[str, float]:
        return {
            "word_count": float(self.word_count),
            "words_per_second": round(self.words_per_second, 3),
            "relative_speech_rate": round(self.relative_speech_rate, 3),
            "reliable": 1.0 if self.reliable else 0.0,
        }


def _bounds(word: Any) -> Optional[tuple[float, float]]:
    try:
        start = float(getattr(word, "start"))
        end = float(getattr(word, "end"))
    except (AttributeError, TypeError, ValueError):
        return None
    if not (start == start and end == end):     # NaN
        return None
    return start, end


def words_in_window(words: Iterable[Any], start: float, end: float) -> list[Any]:
    """Words whose midpoint falls inside ``[start, end)``.

    Midpoint rather than any-overlap, so a word straddling a boundary is counted once by the
    window it mostly belongs to. Counting it in both would inflate the rate of every window in a
    dense transcript.
    """
    out = []
    for word in words:
        bounds = _bounds(word)
        if bounds is None:
            continue
        midpoint = (bounds[0] + bounds[1]) / 2.0
        if start <= midpoint < end:
            out.append(word)
    return out


def source_median_rate(
    words: Sequence[Any],
    duration: float,
    *,
    window: float = 30.0,
) -> Optional[float]:
    """The source's own median speech rate, in words per second.

    Measured over fixed ``window``-second slices and taken as a **median**, not a mean: a long
    silent stretch or a music interlude produces a near-zero slice, and a mean would drag the
    baseline down so that ordinary speech looked fast against it. The median ignores those.

    Returns ``None`` when the source is too short or too sparse for a baseline to mean anything -
    at which point ``relative_speech_rate`` has no denominator and says so.
    """
    if duration <= 0 or not words:
        return None

    rates: list[float] = []
    slice_start = 0.0
    while slice_start < duration:
        slice_end = min(duration, slice_start + window)
        if slice_end - slice_start >= MIN_WINDOW_S:
            count = len(words_in_window(words, slice_start, slice_end))
            if count >= MIN_WORDS:
                rates.append(count / (slice_end - slice_start))
        slice_start = slice_end

    if not rates:
        return None
    median = statistics.median(rates)
    return median if median > 0 else None


def speech_rate(
    words: Sequence[Any],
    start: float,
    end: float,
    *,
    baseline: Optional[float] = None,
) -> SpeechRate:
    """Speech-rate features for ``[start, end]`` (S4).

    Pure, total, and free of ffmpeg: the word timings already exist. ``baseline`` is the
    source's median rate from :func:`source_median_rate` - passed in rather than recomputed,
    because it is a property of the whole source and computing it per candidate would be
    quadratic over a long video.
    """
    window = float(end) - float(start)
    if window < MIN_WINDOW_S:
        return SpeechRate(0, 0.0, 1.0, reliable=False)

    count = len(words_in_window(words, float(start), float(end)))
    rate = count / window if window > 0 else 0.0
    reliable = count >= MIN_WORDS

    if baseline and baseline > 0 and reliable:
        relative = rate / baseline
    else:
        # No baseline, or too few words to compare: 1.0 means "no information", and `reliable`
        # is what distinguishes that from a genuinely average pace.
        relative = 1.0

    return SpeechRate(
        word_count=count,
        words_per_second=rate,
        relative_speech_rate=relative,
        reliable=reliable,
    )


def annotate_candidates(
    candidates: Sequence[Any],
    words: Sequence[Any],
    duration: float,
) -> None:
    """Attach speech-rate features to each candidate's ``features`` dict, in place (S4).

    Deliberately does not touch ``score``. The features exist to be measured against the S1
    benchmark and to be fed to the model later (S10); choosing a weight for them before the
    benchmark can judge it would be guesswork indistinguishable from improvement.
    """
    if not candidates:
        return
    baseline = source_median_rate(words, duration)
    for candidate in candidates:
        features = getattr(candidate, "features", None)
        if features is None:
            continue
        rate = speech_rate(words, candidate.start, candidate.end, baseline=baseline)
        features.update(rate.to_dict())
        if baseline:
            features["source_median_wps"] = round(baseline, 3)
