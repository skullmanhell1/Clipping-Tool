"""Drop invented and looped transcript segments (T3).

Whisper invents text over music, applause and silence, and it gets stuck in decode loops that
repeat a phrase for tens of seconds. Nothing filtered either, so hallucinated text reached the
LLM selector as though it were speech, and reached the viewer burned into captions.

Every rule here is deliberately conservative, for one reason: **a false positive deletes real
speech from a clip, and nothing downstream can notice.** A missed hallucination is visible - you
watch the clip and read nonsense - while a wrongly-dropped sentence just looks like the speaker
never said it. The asymmetry says: require agreement between independent signals, never act on
one weak signal alone.

That is also why there is no boilerplate phrase list. It is the obvious approach - Whisper's
inventions cluster around "Thanks for watching", "Subscribe to my channel", subtitle-credit
lines - and it is a trap, because those are things people genuinely say, especially in the
footage this tool is pointed at. A phrase list would silently delete the outro of every video
that actually has one.

What the rules use instead:

* ``no_speech_prob`` — the model's own estimate that a span contains no speech, which is
  precisely the condition it hallucinates under.
* ``avg_logprob`` — its mean token confidence for the segment.
* the *shape* of the text — a single token repeated, or a phrase repeated across consecutive
  segments. A decode loop looks like nothing a person says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Optional

from config import settings

#: Marker recorded on a clip when segments were dropped, mirroring the pipeline's
#: ``*_degraded`` convention: something was removed, and a record should say so.
MARKER = "transcript_filtered"

_TOKEN = re.compile(r"[\w']+", re.UNICODE)


def _normalise(text: str) -> str:
    """Lower-cased tokens joined by single spaces, for comparing what was said."""
    return " ".join(_TOKEN.findall((text or "").lower()))


def _repeated_token_run(text: str) -> int:
    """The longest run of one identical token, e.g. 4 for "you you you you".

    A decode loop is the clearest hallucination signature there is: no speaker says the same
    word four times in a row with no other word between.
    """
    tokens = _TOKEN.findall((text or "").lower())
    if not tokens:
        return 0
    longest = run = 1
    for previous, current in zip(tokens, tokens[1:]):
        run = run + 1 if current == previous else 1
        longest = max(longest, run)
    return longest


def _mean_word_probability(segment: Any) -> Optional[float]:
    words = getattr(segment, "words", None) or []
    values = []
    for word in words:
        try:
            values.append(float(getattr(word, "probability", 1.0)))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return sum(values) / len(values)


@dataclass
class FilterResult:
    """A filtered transcript and what was removed from it."""

    transcript: Any
    dropped: list[tuple[Any, str]]

    @property
    def removed_count(self) -> int:
        return len(self.dropped)

    @property
    def reasons(self) -> list[str]:
        """Each drop as ``"<reason> @ <start>-<end>"``, for a log a human will read."""
        return [
            f"{reason} @ {segment.start:.2f}-{segment.end:.2f}"
            for segment, reason in self.dropped
        ]


def _looks_invented(segment: Any) -> Optional[str]:
    """Why ``segment`` looks hallucinated, or ``None``.

    Two independent signals must agree before anything is dropped for confidence reasons.
    ``no_speech_prob`` alone is not enough: it is routinely high on quiet but real speech, and
    acting on it alone would delete whispered or distant dialogue. Requiring low token
    confidence *as well* means the model is both unsure there is speech and unsure of the words
    it produced.
    """
    text = _normalise(getattr(segment, "text", ""))
    if not text:
        return None

    try:
        no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
        avg_logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
    except (TypeError, ValueError):
        no_speech, avg_logprob = 0.0, 0.0

    if (
        no_speech >= settings.transcript_no_speech_threshold
        and avg_logprob <= settings.transcript_logprob_threshold
    ):
        return f"no_speech={no_speech:.2f} logprob={avg_logprob:.2f}"

    run = _repeated_token_run(text)
    if run >= settings.transcript_max_token_run:
        return f"token repeated {run}x"

    # A long segment whose words the model had almost no confidence in, *and* which repeats
    # itself, is the classic loop-over-music case. Both halves are required.
    mean_probability = _mean_word_probability(segment)
    tokens = text.split()
    if (
        mean_probability is not None
        and mean_probability <= settings.transcript_min_word_probability
        and len(tokens) >= 4
        and len(set(tokens)) <= max(1, len(tokens) // 3)
    ):
        return f"low confidence ({mean_probability:.2f}) and repetitive"

    return None


def filter_transcript(transcript: Any) -> FilterResult:
    """Remove invented and looped segments from ``transcript`` (T3).

    Pure: returns a new transcript and never mutates the input. Word timings are untouched -
    a dropped segment takes its own words with it and leaves the rest exactly where they were,
    because captions, emphasis and the selector all key off source-relative time.
    """
    segments = list(getattr(transcript, "segments", None) or [])
    if not settings.transcript_filter_enabled or not segments:
        return FilterResult(transcript=transcript, dropped=[])

    kept: list[Any] = []
    dropped: list[tuple[Any, str]] = []

    # Consecutive-duplicate tracking: the same sentence repeated across segments is a loop that
    # spans segment boundaries, which per-segment rules cannot see.
    previous_text = ""
    repeats = 0

    for segment in segments:
        reason = _looks_invented(segment)

        text = _normalise(getattr(segment, "text", ""))
        if text and text == previous_text:
            repeats += 1
            if reason is None and repeats >= settings.transcript_max_segment_repeats:
                reason = f"phrase repeated across {repeats + 1} consecutive segments"
        else:
            repeats = 0
        previous_text = text

        if reason is None:
            kept.append(segment)
        else:
            dropped.append((segment, reason))

    if not dropped:
        return FilterResult(transcript=transcript, dropped=[])

    # Refuse to gut a transcript. If most of it looks invented, the thresholds are wrong for
    # this audio - unusual accent, heavy music bed, a language the model is weak in - and
    # deleting nearly everything would turn a poor transcript into an empty clip. Keeping a bad
    # transcript is recoverable; silently discarding the content is not.
    if len(kept) < len(segments) * settings.transcript_filter_keep_floor:
        return FilterResult(transcript=transcript, dropped=[])

    return FilterResult(transcript=replace(transcript, segments=kept), dropped=dropped)
