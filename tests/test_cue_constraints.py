"""Cue legibility floors (C24) and linguistic line breaking (C25).

Two properties matter more than any individual case and are tested as properties:

* **cues never overlap**, whatever the constraints do — two lines on screen at once is a rendering
  fault, where one briefly-short line is a legibility problem;
* **word spans are never altered**, so karaoke fills keep tracking speech. A cue's window and its
  words' timings are different things, and C24 may only move the first.

Both defaults reproduce v0.11.0 exactly, so the disabled path is asserted to return the *same
objects* rather than equal copies — the strongest available form of "bit-identical".
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from worker import cue_constraints as cc
from worker.cue_constraints import Cue_Window


class _Budget:
    """A width oracle standing in for C6's measured fit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit

    def fits(self, text: str) -> bool:
        return len(text) <= self.limit


def _cue(start, end, text="hello world"):
    return Cue_Window(start=start, end=end, text=text, word_spans=((start, end),))


# --- C24 defaults: a true no-op ------------------------------------------------------------


def test_the_defaults_are_disabled_and_reproduce_v0110_exactly():
    """R4.12. Neither floor has been measured, so neither ships on.

    A floor turned on without evidence would move every golden and re-freeze the parity fixtures
    around a number nobody checked — the `font_substituted:Arial` failure mode.
    """
    assert cc.DEFAULT_MIN_CUE_SECONDS == 0.0
    assert cc.DEFAULT_MAX_READING_RATE == 0.0

    cues = [_cue(0.0, 0.2), _cue(0.2, 0.35), _cue(0.35, 0.5)]
    out, report = cc.apply_constraints(cues)
    # The same objects, not equal copies. This is the strongest form of R4.10.
    assert all(a is b for a, b in zip(out, cues, strict=True))
    assert report.markers == []


def test_an_already_compliant_sequence_is_returned_bit_identical():
    """R4.10, with the constraints actually switched on."""
    cues = [_cue(0.0, 2.0, "plenty"), _cue(2.0, 4.0, "of time")]
    out, report = cc.apply_constraints(cues, min_seconds=1.0, max_reading_rate=10.0)
    assert out == cues
    assert report.extended == 0 and report.merged == 0
    assert report.markers == []


# --- C24: extension ------------------------------------------------------------------------


def test_a_short_cue_is_extended_into_the_following_gap():
    """R4.3. Free time before the next cue is the cheapest fix available."""
    cues = [_cue(0.0, 0.3, "quick"), _cue(3.0, 4.0, "later")]
    out, report = cc.apply_constraints(cues, min_seconds=1.0)
    assert out[0].end == pytest.approx(1.0)
    assert report.extended == 1
    assert "cue_extended:1" in report.markers


def test_extension_stops_at_the_following_cue():
    """R4.4. Non-overlap outranks the floor.

    The merge path is deliberately closed off with a width budget too small to accept the joined
    text, which isolates extension. Without that the cue legitimately *merges* — it cannot reach
    the floor by extending, so R4.7 takes over — and the result is one cue ending at 2.0, which
    looked like an overlap bug in an earlier version of this test and was not.
    """
    cues = [_cue(0.0, 0.3, "quick"), _cue(0.6, 2.0, "next")]
    out, report = cc.apply_constraints(cues, min_seconds=1.0, fit=_Budget(6))
    assert len(out) == 2, "the merge must have been refused for this to test extension"
    assert out[0].end <= 0.6
    assert out[0].end <= out[1].start
    assert report.merged == 0


def test_the_last_cue_is_not_extended_past_the_clip():
    """R4.6. A caption outliving the video is a player-dependent artefact."""
    cues = [_cue(0.0, 4.5, "start"), _cue(4.6, 4.8, "end")]
    out, report = cc.apply_constraints(cues, min_seconds=2.0, clip_end=5.0)
    assert out[-1].end <= 5.0
    assert "cue_constraint_relaxed:clip_end" in report.markers


def test_the_reading_rate_cap_extends_a_dense_cue():
    """R4.2. Duration alone is not legibility: a long line needs longer than a short one.

    The expected duration is computed here from the character count **excluding spaces**, derived
    independently of the implementation. Asserting only "the rate is now under the cap" was not
    enough: the `reading_rate` property excludes spaces too, so a bug that *counted* them inflated
    the needed duration, over-extended the cue, and left the reported rate comfortably under the
    cap. The mutation harness caught that; an exact expectation is what closes it.
    """
    text = "a considerably longer line of text than usual"
    dense = Cue_Window(0.0, 0.5, text, ((0.0, 0.5),))
    out, report = cc.apply_constraints([dense], min_seconds=0.0, max_reading_rate=10.0)

    expected = len(text.replace(" ", "")) / 10.0
    assert out[0].duration == pytest.approx(expected, abs=1e-9), (
        "the demanded duration must be derived from readable characters, not from spaces"
    )
    assert out[0].reading_rate <= 10.0 + 1e-6
    assert report.extended == 1


def test_a_zero_length_window_has_an_infinite_reading_rate():
    """The degenerate case, so it sorts as "worst" rather than dividing by zero."""
    assert Cue_Window(1.0, 1.0, "text").reading_rate == float("inf")


# --- C24: merging --------------------------------------------------------------------------


def test_a_cue_that_cannot_be_extended_is_merged_with_the_next():
    """R4.7. Better one readable line than two flickers."""
    cues = [_cue(0.0, 0.2, "one"), _cue(0.2, 0.4, "two"), _cue(3.0, 4.0, "far")]
    out, report = cc.apply_constraints(cues, min_seconds=1.0, fit=_Budget(100))
    assert report.merged >= 1
    assert any("one two" in c.text for c in out)
    assert "cue_merged:" in " ".join(report.markers)


def test_a_merge_that_would_overflow_the_line_is_refused():
    """R4.7's proviso. A truncated caption is worse than a brief one.

    The width budget wins, and the relaxation is recorded so the outcome is visible rather than
    inferred from watching the clip.
    """
    cues = [_cue(0.0, 0.2, "aaaaaaaa"), _cue(0.2, 0.4, "bbbbbbbb")]
    out, report = cc.apply_constraints(cues, min_seconds=1.0, fit=_Budget(10))
    assert report.merged == 0
    assert "cue_constraint_relaxed:merge_would_overflow" in report.markers
    assert len(out) == 2


def test_merging_concatenates_word_spans_without_changing_them():
    """R4.8 through a merge, which is where it is easiest to get wrong."""
    a = Cue_Window(0.0, 0.2, "one", ((0.0, 0.2),))
    b = Cue_Window(0.2, 0.4, "two", ((0.2, 0.4),))
    out, _ = cc.apply_constraints([a, b], min_seconds=1.0, fit=_Budget(100))
    assert out[0].word_spans == ((0.0, 0.2), (0.2, 0.4))


# --- C24 properties ------------------------------------------------------------------------


_cue_lists = st.lists(
    st.tuples(
        st.floats(min_value=0.0, max_value=20.0, allow_nan=False, allow_infinity=False),
        st.floats(min_value=0.01, max_value=2.0, allow_nan=False, allow_infinity=False),
    ),
    min_size=1,
    max_size=12,
)


def _build(raw):
    """Turn (start, length) pairs into a strictly ordered, non-overlapping cue sequence."""
    cues: list[Cue_Window] = []
    cursor = 0.0
    for _start, length in raw:
        start = cursor
        end = start + length
        cues.append(Cue_Window(start, end, "some words here", ((start, end),)))
        cursor = end + 0.05
    return cues


# Feature: clip-presentation-polish, Property 1: cue constraints never produce overlapping cues
# and never alter word-span times.
@settings(max_examples=100, deadline=None)
@given(raw=_cue_lists, min_seconds=st.floats(min_value=0.0, max_value=3.0), rate=st.floats(min_value=0.0, max_value=30.0))
def test_property_constraints_never_overlap_and_never_touch_word_spans(raw, min_seconds, rate):
    """R10.4. The two invariants that outrank every legibility goal.

    Overlap is a rendering fault; a moved word span silently desynchronises every karaoke fill.
    Neither is acceptable as a side effect of making text more readable.
    """
    cues = _build(raw)
    original_spans = [span for c in cues for span in c.word_spans]

    out, _ = cc.apply_constraints(cues, min_seconds=min_seconds, max_reading_rate=rate)

    for earlier, later in zip(out, out[1:], strict=False):
        assert earlier.end <= later.start + 1e-9, (earlier, later)

    surviving = [span for c in out for span in c.word_spans]
    assert surviving == original_spans, "word spans must pass through untouched"


@settings(max_examples=100, deadline=None)
@given(raw=_cue_lists)
def test_property_no_words_are_lost(raw):
    """A merge must join text, never replace it."""
    cues = _build(raw)
    before = " ".join(c.text for c in cues).split()
    out, _ = cc.apply_constraints(cues, min_seconds=2.0, max_reading_rate=5.0)
    after = " ".join(c.text for c in out).split()
    assert after == before


# --- C25: linguistic line breaking ---------------------------------------------------------


def test_linguistic_breaking_is_disabled_by_default():
    """R5.9. Unmeasured, so it does not ship on."""
    assert cc.choose_break(["the", "thing", "nobody", "tells"], fit=_Budget(20)) is None


def test_a_break_is_not_taken_after_an_article():
    """R5.2. "the / thing" leaves a line ending on a word that means nothing alone.

    Six words, with the article at the **centre**, so the width-only tie-break would choose exactly
    the position the rule must reject. An earlier version used four words where the centre break
    fell after "is" — which is not a binding word, so the test passed with the rule deleted. Found
    by the mutation harness.
    """
    words = ["here", "is", "the", "thing", "nobody", "mentions"]
    # Precondition: the centre position is the one after the article, so the rule is load-bearing.
    assert words[2].lower() in cc.BINDING_WORDS

    position = cc.choose_break(words, fit=_Budget(60), enabled=True)
    assert position is not None
    assert position != 3, "the centre break strands the article and must not be chosen"
    assert words[position - 1].lower() not in cc.BINDING_WORDS


def test_a_break_is_not_taken_after_a_preposition():
    """R5.2's other half — prepositions bind rightwards to their object."""
    words = ["the", "value", "of", "measurement"]
    position = cc.choose_break(words, fit=_Budget(40), enabled=True)
    assert words[position - 1].lower() != "of"


def test_a_multi_word_proper_noun_is_not_split_when_an_alternative_exists():
    """R5.3, via a capitalised-run proxy — no model, no network (R5.7)."""
    words = ["meeting", "with", "Ada", "Lovelace", "tomorrow"]
    position = cc.choose_break(words, fit=_Budget(40), enabled=True)
    assert not (
        cc._is_capitalised(words[position - 1]) and cc._is_capitalised(words[position])
    ), f"broke inside a proper noun at {position}"


def test_the_width_budget_outranks_the_linguistic_preference():
    """R5.4. A preferred break that overflows is not an improvement.

    The budget here only admits a break that keeps both halves very short, so the linguistically
    ideal position is unavailable and the function must decline rather than overflow.
    """
    words = ["here", "is", "the", "extraordinarily", "long", "word"]
    assert cc.choose_break(words, fit=_Budget(6), enabled=True) is None


def test_falling_back_to_width_returns_none_rather_than_guessing():
    """R5.5. `None` means "carry on doing exactly what you do today"."""
    assert cc.choose_break(["one"], fit=_Budget(50), enabled=True) is None
    assert cc.choose_break([], fit=_Budget(50), enabled=True) is None


def test_only_languages_with_rules_get_linguistic_breaking():
    """R5.8. `BINDING_WORDS` is an English function-word list.

    Applying it to German or French would produce confident nonsense, so the guard is on the
    language rather than on hoping the words do not collide.
    """
    words = ["hier", "ist", "die", "sache"]
    assert cc.choose_break(words, fit=_Budget(40), language="de", enabled=True) is None
    assert cc.choose_break(words, fit=_Budget(40), language="fr", enabled=True) is None
    assert cc.choose_break(words, fit=_Budget(40), language="en-GB", enabled=True) is not None


def test_no_word_is_dropped_or_reordered_by_a_break():
    """R5.6. The function returns an index, so it structurally cannot do either."""
    words = ["a", "b", "c", "d", "e"]
    position = cc.choose_break(words, fit=_Budget(40), enabled=True)
    assert position is not None
    assert words[:position] + words[position:] == words


def test_break_candidates_rank_rather_than_filter():
    """R5.4 depends on this: the caller takes the first candidate that *fits*.

    Filtering would mean a cue with no linguistically acceptable break got no break at all, which
    is an overflowing line — worse than an inelegant one.
    """
    words = ["here", "is", "the", "thing"]
    candidates = cc.break_candidates(words)
    assert sorted(candidates) == [1, 2, 3], "every position must remain available"
    # But the position after "the" must not be ranked first.
    assert candidates[0] != 3


def test_a_binding_word_break_is_ranked_below_a_proper_noun_break():
    """The two penalties are ordered deliberately, and the ordering has to be observable.

    A capitalised run is only a *proxy* for a proper noun and has false positives at the start of a
    sentence, so it carries the lighter penalty. A stranded article is always wrong, so it carries
    the heavier one.

    Constructed so the two weights actually compete: the binding break sits near the centre and the
    proper-noun break sits far from it. With the correct weights the proper-noun break still wins;
    with the weights equalised the centre tie-break flips the order. An earlier version of this test
    put both breaks where the tie-break already favoured the right answer, so it passed with the
    weights collapsed — the mutation harness found that.
    """
    words = ["Ada", "Lovelace", "wrote", "the", "first", "program", "here"]
    ranked = cc.break_candidates(words)

    proper_noun_break = 1  # between "Ada" and "Lovelace"
    binding_break = 4  # immediately after "the", and nearest the centre

    assert ranked.index(proper_noun_break) < ranked.index(binding_break), (
        "a stranded article must be penalised more heavily than a split proper noun, "
        "even when the article's break sits closer to the centre"
    )
    assert ranked[0] != binding_break


def test_the_rules_need_no_checkpoint_or_network():
    """R5.7, asserted structurally so a later "improvement" cannot quietly add a dependency."""
    import inspect

    source = inspect.getsource(cc)
    for forbidden in ("requests", "urllib", "http", "torch", "onnxruntime", "spacy", "nltk"):
        assert forbidden not in source, forbidden


# --- markers ------------------------------------------------------------------------------


def test_markers_report_counts_not_intentions():
    """R4.11. How many cues were changed, so a batch review can find the affected clips."""
    cues = [_cue(0.0, 0.2, "one"), _cue(0.2, 0.4, "two"), _cue(5.0, 6.0, "three")]
    _, report = cc.apply_constraints(cues, min_seconds=1.0, fit=_Budget(100))
    joined = " ".join(report.markers)
    assert "cue_merged:" in joined or "cue_extended:" in joined


def test_no_markers_when_nothing_changed():
    """A marker on every clip is noise, and noise is what stops a marker being read."""
    cues = [_cue(0.0, 2.0, "fine"), _cue(2.5, 4.5, "also fine")]
    _, report = cc.apply_constraints(cues, min_seconds=1.0, max_reading_rate=100.0)
    assert report.markers == []
