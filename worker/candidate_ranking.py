"""Ranking, de-duplication and diversity for clip candidates (S11, S15, S17).

Two separate defects live here.

**S11 - the fallback did not score anything.** When it had more segments than the requested
clip count, ``worker.segmentation.segment_video`` kept the *longest* ones, and its own
docstring called that "a simple heuristic standing in for real scoring in later phases". It is
worse than arbitrary: length is anti-correlated with what makes a short clip work. The longest
silence-delimited segment in a video is usually the stretch where nobody paused - a monologue
with no beats - and a punchy fifteen-second exchange loses to it every time. This matters more
than it sounds, because the fallback is not a rare path: it runs whenever there is no API key,
whenever the LLM call fails, and on every ``fixed``/``silence`` strategy run by choice.

**S15 - overlapping and near-identical clips were both returned.** Nothing checked whether two
candidates covered the same moment. The LLM cheerfully proposes 12.0-45.0 and 14.5-47.0, and
both used to ship: two files, one moment, and the second clip's existence actively costs the
user something, because it displaced a different moment that would have been the third pick.

Both are decided here rather than inside ``segmentation``, which stays a pure geometry module
with no opinion about quality - and whose longest-first capping is left intact for its direct
callers, so the S1 harness's ``longest`` baseline remains an independent floor to measure
against rather than a mirror of production.

Every weight is a setting (S17). The point is not that these particular numbers are right - the
S1 benchmark is what can establish that - but that changing them is a config edit rather than a
patch, so tuning against the benchmark does not require a release.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional, Sequence

from config import settings

_TOKEN_RE = re.compile(r"[a-z0-9']+")

#: Tokens too common to indicate two clips are about the same thing. Deliberately short: a long
#: stop list starts discarding the content words that make the comparison work.
_COMMON = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at", "for",
        "is", "was", "are", "were", "be", "been", "it", "its", "this", "that", "these",
        "those", "i", "you", "he", "she", "we", "they", "not", "so", "do", "did", "does",
        "have", "has", "had", "will", "would", "can", "could", "just", "like", "with",
    }
)


def _span(candidate: Any) -> Optional[tuple[float, float]]:
    try:
        start = float(candidate.start)
        end = float(candidate.end)
    except (AttributeError, TypeError, ValueError):
        return None
    if end <= start or start != start or end != end:
        return None
    return start, end


def overlap_fraction(a: Any, b: Any) -> float:
    """Fraction of the *shorter* candidate that the two share, in ``[0, 1]``.

    The shorter one is the denominator on purpose. Intersection-over-union would report a
    30-second clip fully containing a 5-second one as only 0.17 overlap, and let the short clip
    through as though it were a distinct moment - when in fact everything in it already ships
    inside the longer clip. Containment is the thing being detected, so containment is what is
    measured.
    """
    span_a, span_b = _span(a), _span(b)
    if span_a is None or span_b is None:
        return 0.0
    shared = min(span_a[1], span_b[1]) - max(span_a[0], span_b[0])
    if shared <= 0:
        return 0.0
    shorter = min(span_a[1] - span_a[0], span_b[1] - span_b[0])
    return shared / shorter if shorter > 0 else 0.0


#: Distinct content words each side needs before a similarity reading is treated as evidence.
#:
#: Jaccard over a handful of words is noise, and acting on noise here deletes a real clip. The
#: case that forced this: a candidate's ``text`` comes from the transcript segments its window
#: covers, so when one coarse transcript segment spans two adjacent candidates, *both* are
#: assigned that segment's entire text and score 1.0 against each other - identical text
#: describing two genuinely different moments. Found by the pipeline's own clip-count
#: invariants, which dropped from 3 clips to 1 and from 2 to 1.
#:
#: The guard is on token count rather than on adjacency because the underlying problem is
#: evidential, not geometric: two words in common is not information about anything, however the
#: windows are arranged.
MIN_TEXT_TOKENS = 6


def text_similarity(left: str, right: str) -> float:
    """Weighted Jaccard similarity of the content words in two strings, in ``[0, 1]``.

    Catches the case timing cannot: two clips from different parts of the video making the same
    point in nearly the same words, which is common in a talk that recaps itself.

    **Counts, not sets.** Set Jaccard ignores how often a word appears, so two long windows
    drawn from a small vocabulary score 1.0 against each other while being entirely different
    moments - a 45-second clip mentioning a topic once and another built around it read as
    identical. Verified on a real 120-second render: with set comparison the selector returned
    two clips where three were requested, because two adjacent windows shared a content-word
    *set*. Weighted Jaccard (intersection of minimum counts over union of maximum counts)
    scored the same pair at 0.25 and kept both, while still scoring a genuine reordered recap
    at 1.0.

    Returns ``0.0`` when either side carries fewer than :data:`MIN_TEXT_TOKENS` distinct content
    words - including when it has none at all - so a candidate is never deleted on the strength
    of a comparison too small to mean anything. That asymmetry is deliberate: a missed duplicate
    ships one redundant clip, while a false positive deletes a moment the user wanted and leaves
    no trace that it was ever a candidate.
    """
    counts_l = Counter(t for t in _TOKEN_RE.findall((left or "").lower()) if t not in _COMMON)
    counts_r = Counter(t for t in _TOKEN_RE.findall((right or "").lower()) if t not in _COMMON)
    if len(counts_l) < MIN_TEXT_TOKENS or len(counts_r) < MIN_TEXT_TOKENS:
        return 0.0
    shared = sum((counts_l & counts_r).values())
    total = sum((counts_l | counts_r).values())
    return shared / total if total else 0.0


def deduplicate(
    candidates: Sequence[Any],
    *,
    max_overlap: Optional[float] = None,
    max_similarity: Optional[float] = None,
    limit: Optional[int] = None,
) -> list[Any]:
    """Drop candidates that duplicate a higher-ranked one, preserving input order (S15).

    Greedy from the top of the given order, which the caller has already sorted by score: the
    first occurrence of a moment wins and later overlapping ones are dropped. Greedy is the
    right shape here rather than a global optimum, because "keep the best-scoring version of
    each moment" is exactly the intent, and an optimal set cover could swap out a high scorer
    to fit two mediocre ones.

    ``limit`` is applied *after* rejection rather than before, which is the entire point of
    doing this before the cap: filtering first and then taking the top ``k`` is what lets a
    duplicate displace a genuinely different moment.
    """
    if max_overlap is None:
        max_overlap = float(settings.selection_max_overlap)
    if max_similarity is None:
        max_similarity = float(settings.selection_max_text_similarity)
    max_overlap = max(0.0, min(1.0, max_overlap))
    max_similarity = max(0.0, min(1.0, max_similarity))

    kept: list[Any] = []
    for candidate in candidates:
        if _span(candidate) is None:
            continue
        duplicate = False
        for accepted in kept:
            if overlap_fraction(candidate, accepted) > max_overlap:
                duplicate = True
                break
            text_a = getattr(candidate, "text", "") or ""
            text_b = getattr(accepted, "text", "") or ""
            if text_similarity(text_a, text_b) > max_similarity:
                duplicate = True
                break
        if duplicate:
            continue
        kept.append(candidate)
        if limit is not None and len(kept) >= limit:
            break
    return kept


#: Score at the edge of the requested length window.
#:
#: Not 0. The user asking for "30-60s" means every length in that range is acceptable and only
#: the target is *preferred*, so a 60-second clip must not be scored as though it were the wrong
#: length entirely. My first version let the falloff reach zero at the boundary, which made a
#: clip at the top of the user's own stated range indistinguishable from one at twice it.
EDGE_FIT = 0.5


def length_fit(duration: float, target: float, *, min_len: float, max_len: float) -> float:
    """How well ``duration`` matches the requested length window, in ``[0, 1]``.

    Peaks at ``target``, falls to :data:`EDGE_FIT` at the edges of ``[min_len, max_len]``, and
    only reaches 0 well outside them. This is the only component that references length at all,
    and it rewards being *close to what the user asked for* rather than being long - which is the
    whole correction to the heuristic it replaces.
    """
    try:
        duration = float(duration)
        target = float(target)
        min_len = float(min_len)
        max_len = float(max_len)
    except (TypeError, ValueError):
        return 0.0
    if duration <= 0 or duration != duration:
        return 0.0

    # Normalise by the larger half-window so both sides of an asymmetric target reach the edge
    # score at their own boundary.
    reach = max(target - min_len, max_len - target, 1e-6)
    distance = abs(duration - target) / reach
    if distance <= 1.0:
        # Inside the requested window: 1.0 at the target down to EDGE_FIT at a boundary.
        return 1.0 - (1.0 - EDGE_FIT) * distance
    # Outside it: continue down from EDGE_FIT, reaching 0 at twice the half-window.
    return max(0.0, EDGE_FIT * (2.0 - distance))


def score_candidate(
    candidate: Any,
    *,
    target: float,
    min_len: float,
    max_len: float,
) -> float:
    """A 0-100 score for a candidate from its measured features (S11, S17).

    Reads only ``candidate.features`` - whatever S2, S4 and S6 have already attached - so this
    stays pure and the feature modules stay the only things that touch media. A missing feature
    contributes its neutral value rather than zero, so a candidate measured on a source with no
    audio is not ranked below one that was measurable but bad.
    """
    features = getattr(candidate, "features", None) or {}

    def _get(key: str, default: float) -> float:
        try:
            value = float(features.get(key, default))
        except (TypeError, ValueError):
            return default
        return default if value != value else value

    hook = max(0.0, min(1.0, _get("hook_score", 0.5)))

    # Pace: 1.0 is this speaker's own normal, and both directions carry information, so the
    # reading is distance from normal rather than raw speed. Capped at 1.0 so a measurement
    # artefact (a 6 wps burst over three words) cannot dominate the whole score.
    relative_rate = _get("relative_speech_rate", 1.0)
    if _get("reliable", 0.0) >= 1.0:
        pace = max(0.0, min(1.0, abs(relative_rate - 1.0) / 0.6))
    else:
        pace = 0.5

    # Energy: dB above the source's own median, 6 dB being a clearly audible step.
    if _get("energy_reliable", 0.0) >= 1.0:
        energy = max(0.0, min(1.0, 0.5 + _get("relative_energy_db", 0.0) / 12.0))
        # A window that is mostly silence is not a clip, whatever its mean says.
        energy *= max(0.0, 1.0 - _get("quiet_fraction", 0.0))
    else:
        energy = 0.5

    fit = length_fit(
        getattr(candidate, "duration", 0.0) or 0.0,
        target,
        min_len=min_len,
        max_len=max_len,
    )

    # S7/S8/S12: what the passage *says*, alongside how it was delivered.
    #
    # Each defaults to 0.5 when unmeasured, matching every other component here: a candidate on a
    # source with no usable text must not rank below one that was measurable and bad.
    structure = max(0.0, min(1.0, _get("structure_score", 0.5)))
    standalone = max(0.0, min(1.0, _get("standalone_score", 0.5)))
    intensity = max(0.0, min(1.0, _get("intensity_score", 0.5)))

    weights = (
        float(settings.selection_weight_hook),
        float(settings.selection_weight_pace),
        float(settings.selection_weight_energy),
        float(settings.selection_weight_length),
        float(settings.selection_weight_structure),
        float(settings.selection_weight_standalone),
        float(settings.selection_weight_intensity),
    )
    components = (hook, pace, energy, fit, structure, standalone, intensity)
    total = sum(weight * value for weight, value in zip(weights, components))
    weight_sum = sum(weights)
    if weight_sum <= 0:
        return 0.0
    return round(100.0 * max(0.0, min(1.0, total / weight_sum)), 1)


def rank_candidates(
    candidates: Sequence[Any],
    *,
    target: float,
    min_len: float,
    max_len: float,
) -> list[Any]:
    """Score every candidate in place and return them highest-first (S11).

    Mutates ``score`` deliberately, and is the *only* function in the selection path that
    does. The feature annotators are forbidden from touching it - a test pins that - so there
    is exactly one place where a measurement becomes a ranking decision.
    """
    items = list(candidates)
    for candidate in items:
        try:
            candidate.score = score_candidate(
                candidate, target=target, min_len=min_len, max_len=max_len
            )
        except (AttributeError, TypeError):
            continue
    items.sort(key=lambda c: (-(getattr(c, "score", 0.0) or 0.0), getattr(c, "start", 0.0)))
    return items
