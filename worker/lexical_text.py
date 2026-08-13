"""Offline lexical cohesion and similarity for editorial decisions (S22/S23 groundwork).

Two measurements, both computed from words alone:

* **cohesion** — how much vocabulary two adjacent stretches of transcript share. It dips where the
  subject changes, which is what a topic boundary looks like from the outside.
* **similarity** — how alike two candidates' transcripts are, as TF-IDF cosine over the candidate
  set, so a word that appears in every clip counts for less than one that appears in two.

THE NAME IS THE POINT
---------------------
Every symbol here says **lexical**, and that is a requirement (R6.6) rather than modesty. Both
measurements are proxies for something they are not: word overlap is not meaning, and this module is
weakest at exactly the thing the diversity requirement wants — paraphrase. Two clips making the same
point in different words score as dissimilar here, which is the case S23 exists to catch.

Naming it `semantic_*` would hide that. The precedent is this project's refusal to ship a
band-passed noise burst under the name `whoosh`, and `music_degraded:synthesised` for the tone-pair
bed: a proxy is fine, a proxy labelled as the real thing is the defect. So the marker for the
offline path names it `lexical`, and a caller reading `cohesion_lexical` knows what it is holding.

WHY NOT EMBEDDINGS BY DEFAULT
-----------------------------
Because they would not run. Eight backlog items in this project are already blocked on model weights
CI cannot download, `requirements-ml.txt` exists but that image has never been built, and
`permissibility_mode` deliberately forces local-only operation. A semantic feature that silently
needed a checkpoint would be a feature the people this project is built for could not use. So the
offline path is the **default and the floor**, an embedding backend is an opt-in enhancement, and an
absent backend degrades with a named marker rather than failing or pretending.

DETERMINISM
-----------
R6.7 requires identical results across runs and platforms, which rules out more than it sounds like.
Iteration order over a `dict` is insertion-ordered and therefore fine, but `set` iteration is not
ordered by anything stable, and floating-point summation order changes the last bits of a cosine.
So every vocabulary is walked in **sorted** order, and no result depends on a set's iteration order.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

# Reused deliberately rather than redefined. `candidate_ranking` already owns this project's idea of
# "a content word": the same tokeniser and the same stopword list decide what `text_similarity` sees,
# and two modules disagreeing about whether "just" is a content word would make the lexical
# deduplication and the lexical diversity term measure subtly different things while appearing to
# measure the same one. A leading underscore is a weaker signal than a duplicated fact is a defect.
from worker.candidate_ranking import _COMMON, _TOKEN_RE

#: Name recorded for the offline computation, and never anything that sounds learned.
BACKEND_LEXICAL = "lexical"

#: Name recorded when an embedding backend genuinely produced the numbers.
BACKEND_EMBEDDING = "embedding"

#: Capability id an optional embedding backend is resolved through (R6.8).
#:
#: Routed through the existing probe rather than a bare import attempt, because that probe already
#: guarantees totality and per-process caching -- and because a second availability mechanism is how
#: `golden_render.py` recorded that "a capability probe hid 124 ffmpeg filters".
EMBEDDING_CAPABILITY = "python_pkg:sentence_transformers"

#: Sentences per cohesion window.
#:
#: Cohesion is a comparison between two *blocks*, and a block of one sentence is dominated by whether
#: that sentence happened to reuse a word. Three is the smallest block where the measure describes a
#: subject rather than a sentence, and short enough that a boundary is located to within a few
#: seconds rather than a paragraph.
WINDOW_SENTENCES = 3

#: How far below its neighbours a cohesion value must sit to count as a boundary.
#:
#: Expressed as a fraction of the local range rather than an absolute cosine, because absolute
#: cohesion varies enormously with how repetitive a speaker is: a technical talk reusing the same
#: nouns sits high everywhere, and a rambling interview sits low everywhere. A fixed threshold would
#: find every boundary in one and none in the other.
BOUNDARY_DEPTH = 0.35


def tokens(text: str) -> list[str]:
    """Content words of ``text``, lowercased, stopwords removed.

    A list rather than a set: how *often* a word appears is signal, and the discovery that set
    comparison made two different 45-second windows score 1.0 against each other is already recorded
    in `candidate_ranking.text_similarity`.
    """
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _COMMON]


def _counts(items: Iterable[str]) -> dict[str, int]:
    """Term counts in **insertion order**, which is deterministic where a set is not."""
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    """Cosine similarity of two sparse vectors, in ``[0, 1]`` for non-negative weights.

    The shared vocabulary is walked in **sorted** order so the summation order -- and therefore the
    last bits of the result -- is identical on every platform (R6.7). Iterating a set intersection
    would be correct on average and non-reproducible in detail.
    """
    if not left or not right:
        return 0.0
    shared = sorted(set(left) & set(right))
    if not shared:
        return 0.0
    dot = math.fsum(left[key] * right[key] for key in shared)
    if dot <= 0.0:
        return 0.0
    norm_l = math.sqrt(math.fsum(v * v for v in (left[k] for k in sorted(left))))
    norm_r = math.sqrt(math.fsum(v * v for v in (right[k] for k in sorted(right))))
    if norm_l <= 0.0 or norm_r <= 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_l * norm_r)))


# --------------------------------------------------------------------------- #
# Cohesion (S22 groundwork)                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Cohesion_Point:
    """Cohesion measured at one gap between two blocks of sentences.

    ``at`` is the time of the gap -- the boundary between the two blocks -- so a consumer comparing
    it against a candidate's start or end is comparing like with like.
    """

    at: float
    cohesion: float
    backend: str = BACKEND_LEXICAL


def cohesion_series(
    sentences: Sequence[tuple[float, float, str]],
    *,
    window: int = WINDOW_SENTENCES,
) -> list[Cohesion_Point]:
    """Lexical cohesion at each sentence gap (R3.1, R6.1).

    ``sentences`` is ``(start, end, text)`` in time order -- the shape a transcript's segments
    already have. Each gap compares the ``window`` sentences before it against the ``window`` after,
    as TF-weighted cosine over content words. High means the two blocks talk about the same things;
    a dip means the vocabulary changed, which is what a subject change looks like from outside.

    This is TextTiling's block-comparison step and nothing more. It is deliberately not smoothed:
    smoothing would move a boundary away from the gap it was measured at, and a boundary two
    sentences from the real one is worse than a boundary the caller can reject.

    Returns ``[]`` when there are too few sentences to compare, rather than inventing a value for a
    gap that has nothing on one side of it.
    """
    span = max(1, int(window))
    ordered = [s for s in sentences if _sentence_ok(s)]
    if len(ordered) < span * 2:
        return []

    prepared = [_counts(tokens(text)) for _s, _e, text in ordered]
    out: list[Cohesion_Point] = []
    for gap in range(span, len(ordered) - span + 1):
        before: dict[str, float] = {}
        after: dict[str, float] = {}
        for i in range(gap - span, gap):
            for term, count in prepared[i].items():
                before[term] = before.get(term, 0.0) + count
        for i in range(gap, gap + span):
            for term, count in prepared[i].items():
                after[term] = after.get(term, 0.0) + count
        # The gap's time is the boundary between the two blocks: the end of the last sentence before
        # it. Using the next sentence's start would place the boundary after any pause, which is
        # where the edge-silence trimmer already operates.
        out.append(Cohesion_Point(at=float(ordered[gap - 1][1]), cohesion=cosine(before, after)))
    return out


def _sentence_ok(entry: tuple[float, float, str]) -> bool:
    """Whether a ``(start, end, text)`` triple is usable, without raising on a malformed one."""
    try:
        return float(entry[1]) >= float(entry[0])
    except (IndexError, TypeError, ValueError):
        return False


def topic_boundaries(
    series: Sequence[Cohesion_Point],
    *,
    depth: float = BOUNDARY_DEPTH,
) -> list[float]:
    """Times where cohesion dips enough to call a topic boundary (R3.1).

    A boundary is a **local minimum** whose depth below its surrounding peaks exceeds ``depth`` as a
    fraction of the series' own range. Relative rather than absolute for the reason given on
    :data:`BOUNDARY_DEPTH`: a technical talk that reuses its nouns sits high everywhere and a
    rambling interview sits low everywhere, so a fixed cosine threshold finds every gap in one and
    none in the other.

    Returns times in increasing order, de-duplicated. An empty list is a valid and common answer:
    a clip about one thing has no topic boundary, and inventing one would move a candidate's edge for
    no reason.
    """
    if len(series) < 3:
        return []
    values = [p.cohesion for p in series]
    low, high = min(values), max(values)
    if high - low <= 0.0:
        return []
    threshold = float(depth) * (high - low)

    found: list[float] = []
    for i in range(1, len(series) - 1):
        here = values[i]
        if here > values[i - 1] or here > values[i + 1]:
            continue
        # Depth is measured against the nearest peak on each side, not against the global maximum:
        # a shallow dip between two shallow peaks is a subject change too, and comparing it to the
        # loudest peak in the transcript would hide it.
        left_peak = max(values[:i])
        right_peak = max(values[i + 1 :])
        if min(left_peak, right_peak) - here >= threshold:
            found.append(float(series[i].at))
    return sorted(dict.fromkeys(found))


# --------------------------------------------------------------------------- #
# Similarity (S23 groundwork)                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Similarity_Index:
    """TF-IDF vectors for a candidate set, and the pairwise similarity between them.

    Built once for the whole set because IDF is a property *of the set*: a word appearing in every
    candidate carries no information about which two are alike, and that can only be known by looking
    at all of them. Computing similarity pairwise from scratch would either recompute IDF constantly
    or quietly use the wrong one.
    """

    backend: str = BACKEND_LEXICAL
    marker: str = ""
    vectors: tuple[dict[str, float], ...] = ()
    below_threshold: frozenset[int] = field(default_factory=frozenset)

    def similarity(self, left: int, right: int) -> float:
        """Similarity of two members, or ``0.0`` when either is too short to judge (R4.11).

        Short text reads as **maximally dissimilar**, never as duplicate. Below the token threshold
        there is no reliable signal, and defaulting to "similar" would silently drop short clips as a
        class -- the same asymmetry `candidate_ranking.text_similarity` already argues for, where a
        false positive deletes a moment the user wanted.
        """
        if left == right:
            return 1.0
        if left in self.below_threshold or right in self.below_threshold:
            return 0.0
        try:
            return cosine(self.vectors[left], self.vectors[right])
        except IndexError:
            return 0.0


def build_index(
    texts: Sequence[str],
    *,
    min_tokens: int | None = None,
) -> Similarity_Index:
    """TF-IDF cosine index over ``texts`` (R4.1, R6.1).

    ``min_tokens`` defaults to `candidate_ranking.MIN_TEXT_TOKENS`, reused rather than restated so
    the diversity term and the lexical deduplication agree about when a candidate is too short to
    compare. Members below it are recorded and treated as maximally dissimilar by
    :meth:`Similarity_Index.similarity`.

    IDF is smoothed (``log(1 + n/df)``) so a term appearing in every document gets a small positive
    weight rather than exactly zero. With unsmoothed IDF a set of near-identical candidates has every
    shared term zeroed and scores 0.0 against each other -- the diversity term would then see
    duplicates as maximally diverse, which is precisely backwards.
    """
    from worker.candidate_ranking import MIN_TEXT_TOKENS

    floor = MIN_TEXT_TOKENS if min_tokens is None else int(min_tokens)
    prepared = [_counts(tokens(text)) for text in texts]
    total = len(prepared)
    if total == 0:
        return Similarity_Index()

    document_freq: dict[str, int] = {}
    for counts in prepared:
        for term in sorted(counts):
            document_freq[term] = document_freq.get(term, 0) + 1

    vectors: list[dict[str, float]] = []
    short: list[int] = []
    for index, counts in enumerate(prepared):
        if len(counts) < floor:
            short.append(index)
        vector: dict[str, float] = {}
        for term in sorted(counts):
            idf = math.log(1.0 + (total / document_freq[term]))
            vector[term] = counts[term] * idf
        vectors.append(vector)

    return Similarity_Index(
        backend=BACKEND_LEXICAL,
        marker="",
        vectors=tuple(vectors),
        below_threshold=frozenset(short),
    )


# --------------------------------------------------------------------------- #
# Backend resolution (R6.2-R6.5, R6.8)                                        #
# --------------------------------------------------------------------------- #


def resolve_backend(
    requested: str,
    *,
    permissibility: bool = False,
    report=None,
) -> tuple[str, str]:
    """``(backend, marker)`` for the requested semantic backend (R6.2-R6.5, R6.8).

    Four outcomes, and the marker is what distinguishes them:

    * nothing requested -> ``(lexical, "")``. The offline path is the default, and a marker on every
      clip would be noise.
    * ``permissibility`` active -> ``(lexical, "semantic_backend_local_only")``. R6.5. That mode
      already forces local-only sourcing and clears added audio, and a semantic feature quietly
      making a network call under it would break a promise the product makes. Recorded rather than
      silent, because an operator who asked for embeddings is entitled to know why they did not get
      them.
    * requested and unavailable -> ``(lexical, "semantic_backend_degraded:<capability>")``, naming
      the **missing capability** (R6.3) rather than saying "unavailable" -- a marker that does not
      say what is absent cannot be acted on.
    * requested and available -> ``(embedding, "semantic_backend:embedding")``.

    Availability comes from the existing capability probe (R6.8), which never raises and caches per
    process. Nothing here imports the backend or touches the network: R6.4 forbids reaching out
    unless explicitly configured, and asking whether a package exists must not be the thing that
    downloads it.
    """
    wanted = str(requested or "").strip().lower()
    if wanted in ("", BACKEND_LEXICAL, "off", "none"):
        return BACKEND_LEXICAL, ""
    if permissibility:
        return BACKEND_LEXICAL, "semantic_backend_local_only"

    try:
        from worker.engines.capabilities import get_report

        probe = report if report is not None else get_report()
        status = probe.status(EMBEDDING_CAPABILITY)
        available = bool(getattr(status, "available", False))
    except Exception:
        # A probe that cannot answer is an unavailable backend, not a failed clip.
        available = False

    if not available:
        return BACKEND_LEXICAL, f"semantic_backend_degraded:{EMBEDDING_CAPABILITY}"
    return BACKEND_EMBEDDING, f"semantic_backend:{BACKEND_EMBEDDING}"
