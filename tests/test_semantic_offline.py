"""The offline lexical primitives, and the promises they make about being offline.

`worker/lexical_text.py` is the floor both S22 (topic boundaries) and S23 (semantic diversity) stand
on, and the requirement that shapes it is R6: no checkpoint, no network, deterministic, and named so
nobody mistakes word overlap for meaning.

Three things here are about the *promise* rather than the arithmetic, and they are the reason the file
exists:

* determinism across runs (R6.7) — asserted by building the same index twice and comparing exactly,
  not approximately, because the failure mode is a last-bit difference from set-iteration order;
* no import of any model package and no network call by default (R6.1, R6.4);
* `permissibility_mode` forcing the offline path (R6.5) and a configured-but-absent backend degrading
  with a marker that names the missing capability (R6.3).
"""

from __future__ import annotations

import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from worker import lexical_text as lx


def _sentences(*texts: str, step: float = 2.0):
    """`(start, end, text)` triples, one per sentence, evenly spaced."""
    out = []
    t = 0.0
    for text in texts:
        out.append((round(t, 3), round(t + step, 3), text))
        t += step
    return out


#: Two clearly different subjects, with no shared content words between them.
_COOKING = [
    "we roast the garlic until the cloves soften",
    "then the garlic goes into the butter with thyme",
    "butter and thyme make the sauce for the roast",
    "roast the garlic slowly or the butter burns",
]
_FINANCE = [
    "the mortgage rate moved again this quarter",
    "a mortgage at that rate costs more per quarter",
    "quarterly rate changes affect every mortgage holder",
    "the lender repriced the mortgage rate again",
]


# --------------------------------------------------------------------------- #
# R6.6 -- the naming, which is a requirement                                  #
# --------------------------------------------------------------------------- #


def test_nothing_public_is_named_as_though_a_model_produced_it():
    """R6.6. Word overlap is not meaning, and the module must not imply otherwise.

    The offline path is genuinely weaker than embeddings at exactly the thing S23 wants —
    paraphrase — so a name like `semantic_similarity` for a lexical cosine would hide the trade
    rather than make it. The precedent is this project's refusal to ship a band-passed noise burst
    under the name `whoosh`: a proxy is fine, a proxy labelled as the real thing is the defect.

    `resolve_backend` is exempt: it is *about* which backend is in use, so `semantic` is the subject
    of the name rather than a claim about the computation.
    """
    exported = [name for name in dir(lx) if not name.startswith("_")]
    offenders = [
        name
        for name in exported
        if "semantic" in name.lower() or "embed" in name.lower()
        if name not in {"resolve_backend", "BACKEND_EMBEDDING", "EMBEDDING_CAPABILITY"}
    ]

    assert offenders == [], offenders
    assert lx.BACKEND_LEXICAL == "lexical"


# --------------------------------------------------------------------------- #
# R6.1, R6.4 -- offline by default                                            #
# --------------------------------------------------------------------------- #


def test_the_module_imports_no_model_package():
    """R6.1/R6.4. Asserted on `sys.modules` after use, not by reading the source.

    A grep for `import torch` would miss a lazy import inside a function, which is exactly where a
    convenience import would end up. Running the real computation and then checking nothing heavy
    arrived catches both.
    """
    index = lx.build_index([" ".join(_COOKING), " ".join(_FINANCE)])
    lx.cohesion_series(_sentences(*_COOKING, *_FINANCE))
    assert index.backend == lx.BACKEND_LEXICAL

    heavy = [
        name
        for name in ("torch", "sentence_transformers", "transformers", "onnxruntime", "tensorflow")
        if name in sys.modules
    ]
    assert heavy == [], f"the offline path pulled in {heavy}"


def test_the_default_backend_is_lexical_and_records_nothing():
    """A marker on every clip is noise, and noise is what stops a marker being read."""
    assert lx.resolve_backend("") == (lx.BACKEND_LEXICAL, "")
    assert lx.resolve_backend("lexical") == (lx.BACKEND_LEXICAL, "")


# --------------------------------------------------------------------------- #
# R6.3, R6.5, R6.8 -- the optional backend                                    #
# --------------------------------------------------------------------------- #


def test_permissibility_mode_forces_the_offline_path_and_says_so():
    """R6.5. That mode already forces local-only sourcing and clears added audio.

    A semantic feature quietly making a network call under it would break a promise the product
    makes. Recorded rather than silent, because an operator who asked for embeddings is entitled to
    know why they did not get them.
    """
    backend, marker = lx.resolve_backend("embedding", permissibility=True)

    assert backend == lx.BACKEND_LEXICAL
    assert marker == "semantic_backend_local_only"


def test_permissibility_does_not_even_consult_the_probe():
    """The refusal is unconditional, so asking is both pointless and a chance to get it wrong."""

    class _Exploding:
        def status(self, _id):
            raise AssertionError("the capability probe was consulted under permissibility_mode")

    backend, _marker = lx.resolve_backend("embedding", permissibility=True, report=_Exploding())

    assert backend == lx.BACKEND_LEXICAL


def test_a_configured_but_absent_backend_degrades_and_names_the_capability():
    """R6.3. "Unavailable" is not actionable; the missing capability is."""

    class _Absent:
        def status(self, capability_id):
            assert capability_id == lx.EMBEDDING_CAPABILITY
            return type("S", (), {"available": False})()

    backend, marker = lx.resolve_backend("embedding", report=_Absent())

    assert backend == lx.BACKEND_LEXICAL
    assert marker == f"semantic_backend_degraded:{lx.EMBEDDING_CAPABILITY}"
    assert "sentence_transformers" in marker


def test_an_available_backend_is_reported_as_the_one_in_use():
    class _Present:
        def status(self, _id):
            return type("S", (), {"available": True})()

    backend, marker = lx.resolve_backend("embedding", report=_Present())

    assert backend == lx.BACKEND_EMBEDDING
    assert marker == "semantic_backend:embedding"


def test_a_probe_that_raises_is_an_absent_backend_not_a_failed_clip():
    """Totality. A framing or selection refinement must never be why a job dies."""

    class _Broken:
        def status(self, _id):
            raise RuntimeError("probe exploded")

    backend, marker = lx.resolve_backend("embedding", report=_Broken())

    assert backend == lx.BACKEND_LEXICAL
    assert marker.startswith("semantic_backend_degraded:")


# --------------------------------------------------------------------------- #
# Cohesion                                                                    #
# --------------------------------------------------------------------------- #


def test_cohesion_dips_where_the_subject_changes():
    """The claim the measure exists to make.

    Two subjects with no shared content words, spliced together. The gap between them must be the
    lowest cohesion in the series — if it is not, the measure is not measuring subject change.
    """
    series = lx.cohesion_series(_sentences(*_COOKING, *_FINANCE))
    assert series, "no cohesion measured at all"

    lowest = min(series, key=lambda p: p.cohesion)
    seam = _sentences(*_COOKING, *_FINANCE)[len(_COOKING) - 1][1]

    assert lowest.at == pytest.approx(seam), (
        f"the cohesion minimum was at {lowest.at}s, not at the {seam}s subject change"
    )


def test_cohesion_is_high_within_one_subject():
    """The discriminator: without this, a measure that returned 0.0 everywhere would pass above."""
    same = lx.cohesion_series(_sentences(*_COOKING, *_COOKING))
    mixed = lx.cohesion_series(_sentences(*_COOKING, *_FINANCE))

    assert max(p.cohesion for p in same) > min(p.cohesion for p in mixed)


def test_too_few_sentences_measures_nothing_rather_than_guessing():
    """A gap with nothing on one side of it has no cohesion, and inventing one would move an edge."""
    assert lx.cohesion_series(_sentences("one two three", "four five six")) == []


def test_the_boundary_time_is_the_end_of_the_block_before_the_gap():
    """Not the next sentence's start.

    Using the next start would place the boundary *after* any pause between the two subjects, which
    is where the edge-silence trimmer already operates — so a consumer would be given a time a later
    stage was about to move anyway.

    **The fixture has to contain a pause for this to mean anything.** Found by mutation: with
    back-to-back sentences the previous end and the next start are the same number, so swapping one
    for the other changed nothing and the decision was untested.
    """
    # Sentence i occupies [3i, 3i+2], leaving a one-second pause before the next.
    paused = [(3.0 * i, 3.0 * i + 2.0, text) for i, text in enumerate([*_COOKING, *_FINANCE])]
    series = lx.cohesion_series(paused)
    assert series

    ends = {end for _s, end, _t in paused}
    starts = {start for start, _e, _t in paused}
    times = {p.at for p in series}

    assert times <= ends, f"a cohesion time was not a sentence end: {sorted(times - ends)}"
    assert not (times & starts), (
        "cohesion times coincide with sentence starts, so the boundary is being placed after the "
        "pause rather than at the end of the preceding speech"
    )


def test_a_flat_series_has_no_boundaries():
    """A clip about one thing has no topic boundary, and inventing one would move an edge for nothing."""
    flat = [lx.Cohesion_Point(at=float(i), cohesion=0.5) for i in range(8)]

    assert lx.topic_boundaries(flat) == []


def test_a_boundary_is_found_at_a_genuine_dip():
    series = [
        lx.Cohesion_Point(at=0.0, cohesion=0.9),
        lx.Cohesion_Point(at=1.0, cohesion=0.85),
        lx.Cohesion_Point(at=2.0, cohesion=0.1),
        lx.Cohesion_Point(at=3.0, cohesion=0.88),
        lx.Cohesion_Point(at=4.0, cohesion=0.9),
    ]

    assert lx.topic_boundaries(series) == [2.0]


def test_the_dip_threshold_is_relative_to_the_series_own_range():
    """A technical talk reusing its nouns sits high everywhere; a rambling interview sits low.

    An absolute cosine threshold would find every gap in one and none in the other, so depth is a
    fraction of the local range. Both series below have the same *shape* at very different levels and
    must yield the same boundary.
    """
    high = [lx.Cohesion_Point(float(i), c) for i, c in enumerate([0.90, 0.88, 0.70, 0.89, 0.91])]
    low = [lx.Cohesion_Point(float(i), c) for i, c in enumerate([0.20, 0.18, 0.00, 0.19, 0.21])]

    assert lx.topic_boundaries(high) == lx.topic_boundaries(low) == [2.0]


# --------------------------------------------------------------------------- #
# Similarity                                                                  #
# --------------------------------------------------------------------------- #


def test_the_same_text_twice_is_maximally_similar():
    index = lx.build_index([" ".join(_COOKING), " ".join(_COOKING)])

    assert index.similarity(0, 1) == pytest.approx(1.0, abs=1e-9)


def test_different_subjects_are_dissimilar():
    index = lx.build_index([" ".join(_COOKING), " ".join(_FINANCE)])

    assert index.similarity(0, 1) < 0.1


def test_idf_is_smoothed_so_near_duplicates_do_not_read_as_maximally_diverse():
    """The defect unsmoothed IDF produces, and it is exactly backwards.

    With `log(n/df)` a term appearing in every document weighs zero. A set of near-identical
    candidates therefore has every shared term zeroed and scores 0.0 against each other — so the
    diversity term would see duplicates as maximally *diverse* and keep them all, which is the
    opposite of what S23 is for.
    """
    text = " ".join(_COOKING)
    index = lx.build_index([text, text, text])

    assert index.similarity(0, 1) > 0.9, "identical documents scored as dissimilar"


def test_short_text_is_maximally_dissimilar_never_duplicate():
    """R4.11. Below the token threshold there is no signal.

    Defaulting to "similar" would silently drop short clips as a class — the same asymmetry
    `candidate_ranking.text_similarity` already argues for, where a false positive deletes a moment
    the user wanted and leaves no trace it was ever a candidate.
    """
    index = lx.build_index(["too short", "also brief", " ".join(_COOKING)])

    assert 0 in index.below_threshold
    assert 1 in index.below_threshold
    assert index.similarity(0, 1) == 0.0
    assert index.similarity(0, 2) == 0.0


def test_the_token_threshold_is_the_one_deduplication_already_uses():
    """Reused, not restated, so the two cannot disagree about when a candidate is too short."""
    from worker.candidate_ranking import MIN_TEXT_TOKENS

    words = " ".join(f"word{i}" for i in range(MIN_TEXT_TOKENS - 1))
    assert 0 in lx.build_index([words]).below_threshold

    enough = " ".join(f"word{i}" for i in range(MIN_TEXT_TOKENS))
    assert 0 not in lx.build_index([enough]).below_threshold


def test_an_empty_set_indexes_without_raising():
    assert lx.build_index([]).vectors == ()


def test_an_out_of_range_pair_is_dissimilar_rather_than_an_error():
    index = lx.build_index([" ".join(_COOKING)])

    assert index.similarity(0, 99) == 0.0


# --------------------------------------------------------------------------- #
# R6.7 -- determinism                                                         #
# --------------------------------------------------------------------------- #


def test_the_same_input_produces_exactly_the_same_numbers():
    """R6.7, asserted exactly rather than approximately.

    The failure mode is a last-bit difference from iterating a `set`, whose order is not stable
    across processes. `pytest.approx` would hide precisely the defect this guards.
    """
    texts = [" ".join(_COOKING), " ".join(_FINANCE), " ".join(_COOKING[:2] + _FINANCE[:2])]

    first = lx.build_index(texts)
    second = lx.build_index(texts)

    assert first.vectors == second.vectors
    for a in range(len(texts)):
        for b in range(len(texts)):
            assert first.similarity(a, b) == second.similarity(a, b)

    series_a = lx.cohesion_series(_sentences(*_COOKING, *_FINANCE))
    series_b = lx.cohesion_series(_sentences(*_COOKING, *_FINANCE))
    assert series_a == series_b


def test_vocabulary_order_does_not_change_the_result():
    """Reordering the words *within* a document must not move the cosine.

    A vector keyed by term is order-independent in principle; this asserts the implementation really
    is, which is what the sorted iteration in `cosine` exists to guarantee.
    """
    forward = "garlic butter thyme roast sauce cloves soften slowly"
    backward = " ".join(reversed(forward.split()))

    index = lx.build_index([forward, backward, " ".join(_FINANCE)])

    assert index.similarity(0, 1) == pytest.approx(1.0, abs=1e-12)


# Feature: clip-editorial-structure, Property 1: identical input gives identical cohesion and
# similarity, across repeated construction.
@settings(max_examples=100)
@given(
    documents=st.lists(
        st.lists(
            st.sampled_from(["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]),
            min_size=1,
            max_size=14,
        ),
        min_size=1,
        max_size=6,
    )
)
def test_p1_the_offline_computation_is_deterministic(documents):
    """Validates: Requirements 6.1, 6.7, 9.8

    Exact equality on every pair, for arbitrary vocabularies. Any dependence on set-iteration order
    or on floating-point summation order shows up here rather than as an unreproducible difference
    between two machines.
    """
    texts = [" ".join(words) for words in documents]

    first = lx.build_index(texts)
    second = lx.build_index(texts)

    assert first.vectors == second.vectors
    assert first.below_threshold == second.below_threshold
    for a in range(len(texts)):
        for b in range(len(texts)):
            assert first.similarity(a, b) == second.similarity(a, b)


# Feature: clip-editorial-structure, Property 2: similarity is symmetric and bounded.
@settings(max_examples=100)
@given(
    documents=st.lists(
        st.lists(
            st.sampled_from(["one", "two", "three", "four", "five", "six", "seven", "eight"]),
            min_size=1,
            max_size=14,
        ),
        min_size=2,
        max_size=5,
    )
)
def test_p2_similarity_is_symmetric_and_within_bounds(documents):
    """Validates: Requirements 4.1, 6.7

    A diversity term that subtracts an unbounded or asymmetric similarity would rank a set
    differently depending on the order it happened to consider it.
    """
    index = lx.build_index([" ".join(words) for words in documents])

    for a in range(len(documents)):
        for b in range(len(documents)):
            value = index.similarity(a, b)
            assert 0.0 <= value <= 1.0
            assert value == index.similarity(b, a)
