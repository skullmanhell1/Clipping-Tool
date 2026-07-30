"""Hook scoring for the opening seconds of a candidate clip (S6).

Retention on short-form video is decided in the first few seconds, and nothing in this
pipeline modelled that. Selection scored a candidate as a whole: a clip whose best line sits
forty seconds in scored the same as one that opens on it, even though the second keeps a
viewer and the first is scrolled past before it gets there.

**The hook window is measured, not assumed.** The score describes ``[start, start + 2.5s]``
specifically, using the same word timings and energy envelope the other feature modules use,
so no new pass over the media is needed.

Four things are combined, and the choice of *which four* is the substance here:

* **Speech has to start promptly.** A clip that opens on a second of silence has thrown away
  the window. This is the only component that can zero the score on its own, because it is
  the only one describing an absence rather than a degree.
* **Delivery relative to the clip's own average**, for both pace and energy - not absolute
  values. A clip that opens louder and faster than it continues is front-loaded, which is what
  a hook is. Absolute figures would just re-rank speakers by how loudly they talk.
* **Textual openers**, weighted low and deliberately so. Question forms, second person,
  numbers and negative superlatives ("nobody", "never", "worst") really do correlate with
  openings that work, but a keyword list is the component most easily gamed by coincidence -
  a mid-sentence "you" is not a hook. It nudges; it cannot carry a candidate.

**No boilerplate-phrase bonus list.** The tempting version of this ("here's why", "the secret
to") would reward the phrasing of engagement-farming content rather than the presence of a
hook, and it would score a clip higher for *quoting* such a phrase mid-thought. The same
argument that kept a phrase list out of the T3 hallucination filter applies here.

Weights are settings rather than literals (S17) so this can be tuned against the S1 benchmark
without a code change.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence

from config import settings
from worker import audio_features, selection_features

#: How much of a clip counts as its hook, in seconds.
#:
#: The plan says 1.5-3 s. 2.5 s is inside that and is a round number of envelope windows at the
#: default 1 s resolution, so an energy reading is never split across the boundary.
HOOK_WINDOW_S = 2.5

#: A hook with no speech at all in its first this-many seconds is treated as having none.
SPEECH_DEADLINE_S = 1.0

#: Interrogative and second-person openers, plus negative superlatives. Lower-cased, matched on
#: word boundaries so "you" does not fire inside "your" twice or inside "young" at all.
_HOOK_TOKENS = frozenset(
    {
        "how", "why", "what", "when", "who", "which", "whose",
        "you", "your", "youre", "yours",
        "never", "nobody", "nothing", "everyone", "everybody", "always",
        "worst", "best", "biggest", "hardest", "only", "stop", "wrong",
        "secret", "mistake", "truth", "actually", "listen", "imagine",
    }
)

_WORD_RE = re.compile(r"[a-z0-9']+")
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b|\b(?:one|two|three|four|five|six|seven|eight|nine|ten)\b")


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def text_signal(text: str) -> float:
    """A ``[0, 1]`` reading of how much the hook's *wording* looks like an opener.

    Pure and cheap. Three independent contributions, each capped, so a line cannot score
    highly by repeating one trick twenty times - which is exactly what an unbounded keyword
    count would reward.
    """
    lowered = (text or "").lower()
    if not lowered.strip():
        return 0.0

    tokens = _WORD_RE.findall(lowered)
    if not tokens:
        return 0.0

    hits = sum(1 for token in tokens if token in _HOOK_TOKENS)
    # Density rather than count, so a long hook is not rewarded for being long, and capped at
    # a third: past that the line is a keyword soup rather than a sentence.
    density = _clamp((hits / len(tokens)) / 0.33)

    question = 1.0 if "?" in lowered else 0.0
    number = 1.0 if _NUMBER_RE.search(lowered) else 0.0

    return _clamp(0.6 * density + 0.25 * question + 0.15 * number)


def speech_promptness(words: Sequence[Any], start: float) -> float:
    """``1.0`` when speech begins immediately, falling to ``0.0`` at the deadline.

    Linear rather than a step, because the difference between speech starting at 0.1 s and at
    0.9 s is real and a threshold would call them identical.
    """
    first: Optional[float] = None
    for word in words:
        try:
            word_start = float(getattr(word, "start"))
        except (AttributeError, TypeError, ValueError):
            continue
        if word_start != word_start:  # NaN
            continue
        if word_start >= start - 1e-9:
            if first is None or word_start < first:
                first = word_start
    if first is None:
        return 0.0
    delay = max(0.0, first - float(start))
    if delay >= SPEECH_DEADLINE_S:
        return 0.0
    return _clamp(1.0 - delay / SPEECH_DEADLINE_S)


def _front_loading(hook_value: float, clip_value: float, *, span: float) -> float:
    """How much ``hook_value`` exceeds ``clip_value``, mapped onto ``[0, 1]``.

    ``0.5`` means the hook matches the rest of the clip, ``1.0`` means it exceeds it by
    ``span`` or more, ``0.0`` means it falls short by as much. Centred on 0.5 rather than 0 so
    a clip that is simply *even* is not penalised as though it had no hook - most good clips
    are even, and only some are front-loaded.
    """
    if span <= 0:
        return 0.5
    return _clamp(0.5 + (hook_value - clip_value) / (2.0 * span))


def hook_score(
    start: float,
    end: float,
    words: Sequence[Any],
    *,
    envelope: Sequence[tuple[float, float]] = (),
    text: str = "",
) -> dict[str, float]:
    """Score the opening of ``[start, end]`` and return flat features (S6).

    Pure: every measurement comes from ``words`` and a pre-computed ``envelope``. Returns a
    dict in the same flat-float shape the other feature modules use, so it merges straight
    into ``ClipCandidate.features``.
    """
    start = float(start)
    end = float(end)
    hook_end = min(end, start + HOOK_WINDOW_S)
    if hook_end <= start:
        return {"hook_score": 0.0, "hook_promptness": 0.0, "hook_text_signal": 0.0}

    promptness = speech_promptness(words, start)

    # Pace: the hook against the clip. Both come from the S4 module so a single definition of
    # "words per second" is used everywhere, including the S1 benchmark.
    hook_words = len(selection_features.words_in_window(words, start, hook_end))
    hook_wps = hook_words / (hook_end - start)
    clip_words = len(selection_features.words_in_window(words, start, end))
    clip_wps = clip_words / (end - start) if end > start else 0.0
    pace = _front_loading(hook_wps, clip_wps, span=1.5)

    # Energy: same comparison, in dB. 6 dB is the span because it is roughly a doubling of
    # perceived loudness - a difference a viewer would actually notice.
    if envelope:
        hook_energy = audio_features.energy_in_window(envelope, start, hook_end)
        clip_energy = audio_features.energy_in_window(envelope, start, end)
        if hook_energy.reliable and clip_energy.reliable:
            energy = _front_loading(hook_energy.mean_db, clip_energy.mean_db, span=6.0)
        else:
            energy = 0.5
    else:
        energy = 0.5

    signal = text_signal(text)

    total = (
        float(settings.hook_weight_promptness) * promptness
        + float(settings.hook_weight_pace) * pace
        + float(settings.hook_weight_energy) * energy
        + float(settings.hook_weight_text) * signal
    )
    weight_sum = (
        float(settings.hook_weight_promptness)
        + float(settings.hook_weight_pace)
        + float(settings.hook_weight_energy)
        + float(settings.hook_weight_text)
    )
    total = total / weight_sum if weight_sum > 0 else 0.0

    # Silence at the opening is disqualifying rather than merely costly: whatever the rest of
    # the window measures, a clip that starts on dead air has no hook.
    if promptness <= 0.0:
        total = 0.0

    return {
        "hook_score": round(_clamp(total), 4),
        "hook_promptness": round(promptness, 4),
        "hook_pace": round(pace, 4),
        "hook_energy": round(energy, 4),
        "hook_text_signal": round(signal, 4),
    }


def annotate_candidates(
    candidates: Sequence[Any],
    words: Sequence[Any],
    *,
    envelope: Sequence[tuple[float, float]] = (),
) -> None:
    """Attach hook features to each candidate's ``features`` dict, in place (S6)."""
    for candidate in candidates:
        features = getattr(candidate, "features", None)
        if features is None:
            continue
        features.update(
            hook_score(
                candidate.start,
                candidate.end,
                words,
                envelope=envelope,
                text=getattr(candidate, "text", "") or "",
            )
        )
