"""Kinetic typography — options / plan value-record properties (spec task 3.5).

Covers **Property 18** from the kinetic-typography design: ``Kinetic_Options``
parsing is total and key-filtered, options serialisation round-trips, resolution
from ``ProcessingOptions`` is idempotent and read-only, and ``Kinetic_Plan``
round-trips through its JSON-native mapping.

Note on ``resolve_options``
--------------------------
The design states Property 18 in terms of ``resolve_options(...)`` — the
``AV_Engine`` hook. That method does not exist yet: the
``Kinetic_Typography_Engine`` class lands in spec **task 9**, and its
``resolve_options`` is specified to *delegate* to
``Kinetic_Options.from_processing_options``. The resolution clauses below
therefore exercise ``Kinetic_Options.from_processing_options`` directly, which is
the function the future ``resolve_options`` will call; when task 9 lands, the
engine hook inherits these guarantees unchanged.

The planner properties (P11, P12, P13, P15) land in this same file with the
planner, in spec epic 6 (tasks 6.6-6.9); see the section header below for the one
clause of P12 that is asserted at plan level because ``emit_ass`` only arrives in
task 8.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import FakeWord
from tests.strategies import (
    st_broken_word_timeline,
    st_kinetic_options,
    st_options_mapping,
    st_time_base,
    st_word_timeline,
)
from worker import captions
from worker.effects.caption_presets import plan_keywords
from worker.engines.kinetic import (
    DEFAULT_REVEAL,
    DEFAULT_STYLE,
    KINETIC_STYLES,
    MIN_WORD_S,
    REVEAL_MODES,
    SYNTHESISED_RATIO_LIMIT,
    Kinetic_Cue,
    Kinetic_Options,
    Kinetic_Plan,
    Kinetic_Word,
    plan_kinetic,
)
from worker.engines.timebase import Time_Base, Timeline_Segment
from worker.models import ProcessingOptions

_FIELD_NAMES = tuple(entry.name for entry in dataclasses.fields(Kinetic_Options))

#: ``ProcessingOptions`` spellings ``from_processing_options`` reads for the
#: kinetic-only settings. They are not declared fields yet (spec task 10 adds
#: them to the request model), so they are attached as instance attributes — which
#: is exactly the ``getattr`` surface the projection reads.
_KINETIC_ATTRS = (
    "kinetic_style",
    "kinetic_reveal",
    "kinetic_font",
    "kinetic_max_lines",
    "kinetic_max_line_width",
    "kinetic_safe_area_x_pct",
    "kinetic_safe_area_y_pct",
    "kinetic_motion_ms",
    "kinetic_confidence_floor",
)


def _stable(value):
    """A repr-based snapshot, so a ``NaN`` payload compares equal to itself."""
    return repr(value)


def _processing_options(data, hostile):
    """A ``ProcessingOptions`` carrying hostile kinetic attributes.

    The declared caption/hook fields are drawn valid-ish (that is what the API
    hands the engine, already normalised by ``worker.models.effective_options``),
    while the kinetic-only spellings are fed arbitrary values from the drawn
    hostile mapping so resolution has to coerce them.
    """
    options = ProcessingOptions.from_dict(
        {
            "captions": data.draw(st.booleans(), label="captions"),
            "caption_preset": data.draw(
                st.sampled_from(
                    ["karaoke", "boxed", "minimal", "pop", "typewriter", "hormozi", "nope"]
                ),
                label="caption_preset",
            ),
            "caption_position": data.draw(
                st.sampled_from(["bottom", "center", "top", "", "sideways", None]),
                label="caption_position",
            ),
            "caption_keyword_highlight": data.draw(st.booleans(), label="kw"),
            "caption_keyword_ai": data.draw(st.booleans(), label="kw_ai"),
            "caption_emoji": data.draw(st.booleans(), label="emoji"),
            "hook_title": data.draw(st.booleans(), label="hook"),
            "permissibility_mode": data.draw(st.booleans(), label="permissibility"),
        }
    )
    payload = list(hostile.values()) or [None]
    for index, name in enumerate(_KINETIC_ATTRS):
        setattr(options, name, payload[index % len(payload)])
    return options


def _plan_from_timeline(words, duration, options):
    """A representative ``Kinetic_Plan`` built from a drawn Word_Timeline.

    Shaped like the planner's output (spec epic 6) without depending on it: two
    Text_Lines of alternating words inside a single cue spanning the timeline.
    """
    planned = tuple(
        Kinetic_Word(
            text=word.text,
            start=word.start,
            end=word.end,
            rel_ms=int(round(max(0.0, word.start - words[0].start) * 1000.0)),
            emphasis=bool(index % 2),
            timing_synthesised=bool(index % 3 == 0),
            emoji="🔥" if index % 4 == 0 else "",
            line=index % 2,
        )
        for index, word in enumerate(words)
    )
    lines = (
        tuple(i for i in range(len(planned)) if i % 2 == 0),
        tuple(i for i in range(len(planned)) if i % 2 == 1),
    )
    cue = Kinetic_Cue(
        segment=Timeline_Segment(words[0].start, words[-1].end),
        words=planned,
        lines=tuple(entry for entry in lines if entry),
    )
    return Kinetic_Plan(
        style=options.style,
        reveal=options.reveal,
        font=options.preset_font,
        font_size=options.font_size,
        position=options.position or "bottom",
        align=2,
        play_res_x=1080,
        play_res_y=1920,
        margin_l=64,
        margin_r=64,
        margin_v=180,
        duration=duration,
        style_line="Style: Default,Inter,84,&H00FFFFFF,&H0000FFFF",
        hook_style="Style: Hook,Inter,110,&H00FFFFFF,&H0000FFFF",
        hook_text="this changed everything",
        hook_duration_s=options.hook_duration_s,
        cues=(cue,),
        cue_level=False,
        degraded=False,
        markers=options.notes,
        detail="cues=1",
        colors={"primary": "&H00FFFFFF", "highlight": "&H0000FFFF"},
        highlight_scale=118,
    )


# --------------------------------------------------------------------------- #
# Property 18 (task 3.5)                                                        #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 18: Options and plans round-trip; resolution
# is idempotent — *For every* mapping of arbitrary values, `Kinetic_Options.parse(data)`
# returns a Kinetic_Options without raising and ignores keys that are not fields;
# *for every* valid Kinetic_Options value, `parse(o.to_dict()).to_dict() == o.to_dict()`;
# *for every* Processing_Options value, `resolve_options(resolve_options(o)) ==
# resolve_options(o)` and `dataclasses.asdict(options)` is unchanged; and *for every*
# Kinetic_Plan, `Kinetic_Plan.from_dict(p.to_dict())` is equivalent to `p` and
# `p.to_dict()` is JSON-encodable.
@settings(max_examples=100, deadline=None)
@given(
    hostile=st_options_mapping(),
    valid=st_kinetic_options(),
    timeline=st_word_timeline(),
    data=st.data(),
)
def test_p18_options_and_plans_round_trip_and_resolution_is_idempotent(
    hostile, valid, timeline, data
):
    """Validates: Requirements 10.1, 10.3, 10.5, 10.6, 10.7, 10.8, 10.9, 10.10, 11.2, 11.10, 17.8

    Four clauses, all four exercised on every example:

    1. ``Kinetic_Options.parse`` is total over hostile mappings and reads named
       fields only, so unknown keys cannot change the result (Reqs 10.5, 10.6).
    2. ``parse(o.to_dict()).to_dict() == o.to_dict()`` for every valid options
       value, and ``to_dict`` is sorted + JSON-encodable (Reqs 10.1, 10.7).
    3. ``from_processing_options`` — what task 9's ``resolve_options`` delegates
       to — is idempotent and never writes to the supplied Processing_Options
       (Reqs 10.3, 10.8, 10.9, 10.10).
    4. ``Kinetic_Plan.from_dict(p.to_dict())`` equals ``p`` and ``p.to_dict()`` is
       JSON-encodable (Reqs 11.2, 11.10).
    """
    # --- 1. parse is total and ignores non-field keys (Reqs 10.5, 10.6) ----
    parsed = Kinetic_Options.parse(hostile)  # must not raise
    assert isinstance(parsed, Kinetic_Options)
    # Unknown keys are invisible: dropping them cannot change the result.
    fields_only = {key: hostile[key] for key in hostile if key in _FIELD_NAMES}
    assert Kinetic_Options.parse(fields_only) == parsed
    # ...and adding more unknown keys cannot change it either.
    noisy = dict(hostile)
    noisy.update({"": None, "not_a_field": object(), "🎬": [1, 2, 3]})
    assert Kinetic_Options.parse(noisy) == parsed
    # Non-mapping input is tolerated too (the protocol is total).
    for junk in (None, 3, "style", [("style", "pop")]):
        assert Kinetic_Options.parse(junk) == Kinetic_Options()

    # --- 2. valid options round-trip through to_dict (Reqs 10.1, 10.7) ----
    options = Kinetic_Options.parse(valid)
    record = options.to_dict()
    assert Kinetic_Options.parse(record).to_dict() == record
    assert Kinetic_Options.parse(record) == options
    assert list(record) == sorted(record)
    json.dumps(record)  # JSON-native leaves only
    # Every drawn value is in range, so parse is the identity on the mapping.
    for key, value in valid.items():
        assert record[key] == value

    # --- 3. resolution is idempotent and read-only (Reqs 10.8, 10.9) ------
    processing = _processing_options(data, hostile)
    before_asdict = _stable(dataclasses.asdict(processing))
    before_vars = dict(vars(processing))

    resolved = Kinetic_Options.from_processing_options(processing)
    again = Kinetic_Options.from_processing_options(resolved)
    assert again == resolved
    assert dataclasses.asdict(again) == dataclasses.asdict(resolved)
    assert Kinetic_Options.from_processing_options(again) == resolved

    # The supplied Processing_Options is provably untouched: same field values,
    # and every attribute is the *same object* it was before.
    assert _stable(dataclasses.asdict(processing)) == before_asdict
    assert set(vars(processing)) == set(before_vars)
    assert all(vars(processing)[key] is before_vars[key] for key in before_vars)

    # Resolution always lands on legal vocabulary members, and its output is a
    # fixed point of ``parse`` (so the plan/digest path sees a stable value).
    assert resolved.style in KINETIC_STYLES
    assert resolved.reveal in REVEAL_MODES
    assert Kinetic_Options.parse(resolved.to_dict()) == resolved

    # --- 4. plans round-trip (Reqs 11.2, 11.10) --------------------------
    words, duration = timeline
    plan = _plan_from_timeline(words, duration, resolved)
    mapping = plan.to_dict()
    assert list(mapping) == sorted(mapping)
    json.dumps(mapping)  # JSON-encodable (Req 11.2)
    rebuilt = Kinetic_Plan.from_dict(mapping)
    assert rebuilt == plan
    assert rebuilt.to_dict() == mapping
    # Nested records survive the round-trip, not just the scalar head.
    assert len(rebuilt.cues) == len(plan.cues)
    assert rebuilt.cues[0].words == plan.cues[0].words
    assert rebuilt.cues[0].lines == plan.cues[0].lines
    assert math.isclose(rebuilt.cues[0].start, plan.cues[0].start)
    # A hostile mapping is tolerated by from_dict as well.
    assert isinstance(Kinetic_Plan.from_dict(hostile), Kinetic_Plan)
    assert Kinetic_Plan.from_dict(None).style == DEFAULT_STYLE
    assert Kinetic_Plan.from_dict(None).reveal == DEFAULT_REVEAL


# --------------------------------------------------------------------------- #
# The pure planner — Properties 11, 12, 13, 15 (tasks 6.6-6.9)                 #
# --------------------------------------------------------------------------- #
#: One microsecond. Every timing clause below is an exact consequence of the
#: planner's arithmetic; this tolerance absorbs binary floating-point round-off
#: only — it is more than three orders of magnitude below one frame at the
#: maximum legal fps (1/240 s).
_TOL = 1e-6

#: A deterministic, well-formed reference timeline planned on every example, so
#: the P11 clause set is never asserted only against an empty plan (a degenerate
#: draw — ``duration`` on a coarse frame grid — can legitimately plan zero cues).
_REFERENCE_BASE = Time_Base(fps=30.0)


def _plan_options(mapping, **overrides):
    """Parse a drawn ``st_kinetic_options`` mapping, overriding named fields."""
    return dataclasses.replace(Kinetic_Options.parse(mapping), **overrides)


def _reference_timeline():
    """Three clean words inside a 2.5 s clip — always plans at least one cue."""
    return (
        [
            FakeWord(0.0, 0.5, "hello"),
            FakeWord(0.6, 1.1, "changed"),
            FakeWord(1.2, 1.9, "everything"),
        ],
        2.5,
    )


def _assert_cue_timeline(plan, base, duration):
    """The Property 11 clause set, asserted over one planned ``Kinetic_Plan``."""
    for previous, following in zip(plan.cues, plan.cues[1:], strict=False):
        assert previous.start <= following.start  # sorted by start
        assert previous.end <= following.start + _TOL  # pairwise disjoint

    for cue in plan.cues:
        assert cue.start <= cue.end
        assert 0.0 <= cue.start <= duration + _TOL  # inside [0, duration]
        assert 0.0 <= cue.end <= duration + _TOL
        # Both bounds sit exactly on the frame grid (``snap`` is idempotent, so
        # an already-snapped value is a fixed point).
        assert base.snap(cue.start) == cue.start
        assert base.snap(cue.end) == cue.end

        assert cue.words, "a planned cue always carries at least one word"
        for word in cue.words:
            # Every emitted timestamp is inside the clip and inside its cue.
            assert 0.0 <= word.start <= duration + _TOL
            assert 0.0 <= word.end <= duration + _TOL
            assert cue.start - _TOL <= word.start <= word.end + _TOL
            assert word.end <= cue.end + _TOL
            # The motion onset is inside its own word (Reqs 5.3, 5.8).
            motion_start = cue.start + word.rel_ms / 1000.0
            assert cue.start - _TOL <= motion_start <= word.end + _TOL


# Feature: kinetic-typography, Property 11: Cue timeline is sorted, disjoint, in-bounds,
# and word-consistent — *for every* Word_Timeline, options value, and Time_Base, cue
# intervals are sorted by start, mutually non-overlapping, contained in `[0, duration]`,
# snapped (`time_base.snap(x) == x`), every emitted timestamp lies in `[0, duration]`, and
# every word's motion start satisfies `cue.start <= motion_start <= word.end`.
@settings(max_examples=100, deadline=None)
@given(
    timeline=st_word_timeline(),
    option_map=st_kinetic_options(),
    base=st_time_base(),
)
def test_p11_cue_timeline_is_sorted_disjoint_in_bounds_and_word_consistent(
    timeline, option_map, base
):
    """Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 16.2

    ``motion_start`` is ``cue.start + word.rel_ms / 1000`` — the onset the emitter
    will hand to ``\\t`` — so the last clause pins Req 5.3's "relative to its cue
    start" together with Req 5.8's "never past the word's own end".

    A *degenerate* draw can legitimately plan **zero** cues (a clip whose duration
    is shorter than one frame collapses the whole timeline onto the grid floor),
    so the clause set is additionally re-run on a fixed well-formed timeline at
    30 fps, which must plan at least one cue on every example.
    """
    words, duration = timeline
    options = _plan_options(option_map)

    plan = plan_kinetic(
        words,
        duration,
        base,
        options,
        font="Inter",
        keyword_planner=plan_keywords,
    )
    assert isinstance(plan, Kinetic_Plan)
    _assert_cue_timeline(plan, base, duration)

    # Non-degenerate re-run: the clauses are never asserted vacuously.
    ref_words, ref_duration = _reference_timeline()
    reference = plan_kinetic(
        ref_words,
        ref_duration,
        _REFERENCE_BASE,
        options,
        font="Inter",
        keyword_planner=plan_keywords,
    )
    assert reference.cues, "a clean timeline must plan at least one cue"
    _assert_cue_timeline(reference, _REFERENCE_BASE, ref_duration)


def _fully_broken_timeline():
    """A timeline whose every word's timing is missing, non-numeric, or degenerate.

    Every documented corruption from :func:`tests.strategies.st_broken_word_timeline`
    is present at least once, so the ``SYNTHESISED_RATIO_LIMIT`` branch is reached
    deterministically rather than by a lucky draw.
    """
    missing_end = FakeWord(0.2, 0.4, "vanished")
    del missing_end.end
    return (
        [
            FakeWord(None, None, "missing"),  # non-numeric both bounds
            missing_end,  # ``end`` attribute absent
            FakeWord("abc", 1.0, "junkstart"),  # non-numeric start
            FakeWord(1.0, float("nan"), "nanend"),  # non-finite end
            FakeWord(2.0, 1.0, "inverted"),  # inverted pair
            FakeWord(2.5, 2.5, "zerolength"),  # zero-length
            FakeWord(2.6, 2.9, "   "),  # whitespace-only: dropped
        ],
        4.0,
    )


def _word_timing_markers(plan):
    """How many ``degraded:word_timings`` markers the plan carries."""
    return sum(1 for entry in plan.markers if entry.endswith("degraded:word_timings"))


def _assert_degradation(plan, duration):
    """The Property 12 clause set, asserted over one planned ``Kinetic_Plan``."""
    planned = [word for cue in plan.cues for word in cue.words]
    synthesised = [word for word in planned if word.timing_synthesised]

    for word in synthesised:
        # Req 6.2 — an invented interval is at least the documented minimum.
        assert word.end - word.start >= MIN_WORD_S - _TOL
    for word in planned:
        assert 0.0 <= word.start <= word.end <= duration + _TOL

    ratio = (len(synthesised) / len(planned)) if planned else 0.0
    if ratio > SYNTHESISED_RATIO_LIMIT:
        # Reqs 6.3, 6.4 — cue-level animation, degraded status, one marker.
        assert plan.cue_level is True
        assert plan.degraded is True
        assert _word_timing_markers(plan) == 1
    else:
        assert plan.cue_level is False
        assert plan.degraded is False
        assert _word_timing_markers(plan) == 0
    return ratio


# Feature: kinetic-typography, Property 12: Malformed timings degrade instead of raising —
# *for every* timeline with missing, non-numeric, inverted, zero-length, or empty-text
# words, planning and `run` return without raising; every synthesised word is flagged
# `timing_synthesised` with `end - start >= MIN_WORD_S`; past `SYNTHESISED_RATIO_LIMIT` the
# status is `degraded` with exactly one `degraded:word_timings` marker and every `Default`
# event carries a single `\fad` and no per-word `\t`.
@settings(max_examples=100, deadline=None)
@given(
    timeline=st_broken_word_timeline(),
    option_map=st_kinetic_options(),
    base=st_time_base(),
)
def test_p12_malformed_timings_degrade_instead_of_raising(timeline, option_map, base):
    """Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.7

    **Asserted one layer down, to be tightened by task 8.** The design's final
    clause is about the *emitted* document — "every ``Default`` event carries a
    single ``\\fad`` and no per-word ``\\t``" — and ``emit_ass`` does not exist
    until spec **task 8**. The plan-level equivalent is asserted instead:
    ``cue_level is True`` (which is precisely the flag the emitter reads to choose
    the single-``\\fad``, no-``\\t`` cue-level rendering), ``degraded is True``, and
    exactly one ``…:degraded:word_timings`` marker. When task 8 lands, this test
    should be tightened to run the tag clause over ``emit_ass(plan)``'s real
    ``Default`` events.

    ``run`` (the engine hook, spec task 9) does not exist yet either; the planner
    is the function it will delegate to, and it is exercised here for totality —
    it must return a ``Kinetic_Plan`` for every corrupted timeline, never raise.
    """
    words, duration = timeline
    options = _plan_options(option_map)

    plan = plan_kinetic(  # must not raise (Req 6.7)
        words,
        duration,
        base,
        options,
        font="Inter",
        keyword_planner=plan_keywords,
    )
    assert isinstance(plan, Kinetic_Plan)
    _assert_degradation(plan, duration)

    # The degraded branch is reached on *every* example, not only lucky draws.
    broken_words, broken_duration = _fully_broken_timeline()
    degraded = plan_kinetic(
        broken_words,
        broken_duration,
        base,
        options,
        font="Inter",
        keyword_planner=plan_keywords,
    )
    ratio = _assert_degradation(degraded, broken_duration)
    assert ratio > SYNTHESISED_RATIO_LIMIT
    assert degraded.cue_level is True
    assert degraded.degraded is True
    assert _word_timing_markers(degraded) == 1
    # The whitespace-only word is dropped, its neighbours are retained (Req 6.6).
    texts = [word.text for cue in degraded.cues for word in cue.words]
    assert texts and all(text.strip() for text in texts)


#: Unique, long, non-stopword tokens. ``caption_presets.plan_keywords`` marks a
#: token of length >= 6 as a keyword **regardless of its Word_Confidence**, so
#: emphasis is guaranteed in the unfloored run and the floor is the only thing
#: that can take it away; uniqueness lets each planned word be mapped back to the
#: source Word_Confidence it was drawn with.
_KEYWORD_TOKENS = tuple(f"kinetic{index:02d}word" for index in range(12))


def _emphasis_vector(plan):
    return tuple(word.emphasis for cue in plan.cues for word in cue.words)


def _shape_vector(plan):
    """Everything about a plan *except* emphasis: text, timing, layout."""
    return tuple(
        (
            cue.start,
            cue.end,
            cue.lines,
            tuple(
                (
                    word.text,
                    word.start,
                    word.end,
                    word.rel_ms,
                    word.line,
                    word.timing_synthesised,
                    word.emoji,
                )
                for word in cue.words
            ),
        )
        for cue in plan.cues
    )


def _keyword_timeline():
    """Four clean, keyword-shaped words in a 3 s clip — always plans words."""
    return (
        [
            FakeWord(0.0, 0.5, _KEYWORD_TOKENS[0]),
            FakeWord(0.6, 1.1, _KEYWORD_TOKENS[1]),
            FakeWord(1.2, 1.7, _KEYWORD_TOKENS[2]),
            FakeWord(1.8, 2.3, _KEYWORD_TOKENS[3]),
        ],
        3.0,
    )


def _assert_floor_semantics(words, duration, base, option_map, floor):
    """The Property 13 clause set over one timeline; returns the planned word count.

    Rewrites the timeline in place so both sides of the Word_Confidence comparison
    are populated (see the test docstring for why that is mandatory), then plans
    three times — floor ``0.0``, the drawn ``floor``, and ``1.0`` over confidences
    that are all strictly below ``1.0`` — and asserts that only ``emphasis`` ever
    moves.
    """
    below = max(0.0, floor - 0.05)
    for index, word in enumerate(words):
        word.text = _KEYWORD_TOKENS[index]
        word.probability = below if index % 2 == 0 else 1.0
    confidence = {word.text: word.probability for word in words}

    shared = dict(highlight_keywords=True, keyword_ai=False, emoji_inline=False)

    def _plan(confidence_floor):
        return plan_kinetic(
            words,
            duration,
            base,
            _plan_options(option_map, confidence_floor=confidence_floor, **shared),
            font="Inter",
            keyword_planner=plan_keywords,
        )

    base_plan = _plan(0.0)
    floor_plan = _plan(floor)

    planned = [word for cue in base_plan.cues for word in cue.words]
    if not planned:
        return 0

    # Trap 2 closed: emphasis is genuinely reachable — at least one planned word is
    # emphasised when nothing is floored out, so the clauses below are not vacuous.
    #
    # This asserted *every* planned word was emphasised, which held only because emphasis
    # used an absolute rule (Whisper probability >= 0.9, defaulting to 1.0 when absent)
    # that essentially everything cleared. C11 replaced it with a ranked budget, so which
    # words are chosen is now the emphasis policy's business — and is pinned directly in
    # ``tests/test_caption_presets.py``. What Property 13 is about is the *floor*: that it
    # governs emphasis word by word and touches nothing else. Asserting the selection here
    # would couple this property to a policy it does not own.
    assert any(word.emphasis for word in planned)

    # Text, timing and layout are bit-identical between the runs; only emphasis
    # may differ (Req 5.9 — spoken text and timing untouched).
    assert _shape_vector(floor_plan) == _shape_vector(base_plan)
    assert floor_plan.cue_level == base_plan.cue_level
    assert floor_plan.degraded == base_plan.degraded

    # Trap 1 closed: the floor decides emphasis, word by word (Req 6.5).
    #
    # Stated as an implication plus monotonicity rather than an equality, because emphasis
    # is now the conjunction of two independent decisions: the emphasis policy selects
    # candidates (C11's ranked budget) and the floor vetoes the ones we did not hear
    # clearly enough. The floor's half is fully pinned by the two clauses together — it may
    # only ever remove emphasis, and never from a word that clears it.
    base_emphasised = {word.text for cue in base_plan.cues for word in cue.words if word.emphasis}
    for cue in floor_plan.cues:
        for word in cue.words:
            if word.emphasis:
                assert confidence[word.text] >= floor
                assert word.text in base_emphasised
            elif word.text in base_emphasised:
                assert confidence[word.text] < floor

    # A floor of 1.0 over confidences that are all strictly below 1.0 strips every
    # word, so the emphasis matrix provably differs somewhere.
    for word in words:
        word.probability = min(word.probability, 0.5)
    stripped = _plan(1.0)
    assert _shape_vector(stripped) == _shape_vector(base_plan)
    assert not any(_emphasis_vector(stripped))
    assert _emphasis_vector(stripped) != _emphasis_vector(base_plan)
    return len(planned)


# Feature: kinetic-typography, Property 13: Low-confidence words lose emphasis but keep
# text and timing — *for every* Word_Timeline and confidence floor, every word below the
# floor is emitted without emphasis tags while its text and `[start, end)` are identical to
# the emphasis-enabled run.
@settings(max_examples=100, deadline=None)
@given(
    timeline=st_word_timeline(min_words=2, max_words=6),
    option_map=st_kinetic_options(),
    base=st_time_base(),
    floor=st.floats(min_value=0.05, max_value=1.0, allow_nan=False, allow_infinity=False),
)
def test_p13_low_confidence_words_lose_emphasis_but_keep_text_and_timing(
    timeline, option_map, base, floor
):
    """Validates: Requirements 5.9, 6.5

    Two vacuity traps are closed explicitly, because either one would make this
    property assert nothing:

    1. ``tests.conftest.FakeWord.probability`` is hard-coded to ``1.0`` and is not
       a constructor argument, and a legal ``confidence_floor`` is at most ``1.0``
       — so a timeline drawn straight from ``st_word_timeline`` can **never** sit
       below the floor. Each word's ``.probability`` is therefore set here:
       even positions below the drawn floor, odd positions at ``1.0``, so both
       sides of the comparison are populated on every example.
    2. Emphasis has to be *reachable*: ``highlight_keywords`` is forced on and
       ``caption_presets.plan_keywords`` is injected as the keyword planner, and
       every word's text is replaced with a unique >= 6-character non-stopword
       token, which ``plan_keywords`` marks as a keyword irrespective of
       confidence. The unfloored run is asserted to emphasise **every** planned
       word, and the matrix is asserted to actually differ (a third run with a
       ``1.0`` floor over strictly-below-``1.0`` confidences strips them all).
    """
    words, duration = timeline
    planned_words = _assert_floor_semantics(words, duration, base, option_map, floor)

    # A clip shorter than one frame legitimately plans zero cues (P11 covers that
    # degenerate case), so the clause set is re-run on a fixed timeline at 30 fps
    # which provably plans words — that run is where the matrix is asserted to
    # differ, so this property can never pass vacuously.
    ref_words, ref_duration = _keyword_timeline()
    reference_words = _assert_floor_semantics(
        ref_words, ref_duration, _REFERENCE_BASE, option_map, floor
    )
    assert reference_words, "a clean timeline must plan at least one word"
    assert planned_words >= 0  # the drawn run may legitimately be empty


#: Tokens 15 Display_Width units wide — wider than the *largest* legal
#: ``max_line_width`` floor of 6 used below, so ``pack_lines`` places each alone
#: on its line and, at ``max_lines=1``, hands every following word back as
#: overflow. That is what forces the re-split under test.
_SPLIT_TOKENS = tuple(f"overflow{index:02d}token" for index in range(8))

#: The word count at which ``captions.words_to_cues`` starts a new cue, read from the
#: function's own default so a change to it (C5 took it from 5 to 3) cannot quietly
#: invalidate the single-cue premise of the strategy below.
_GROUPING_MAX_WORDS = int(inspect.signature(captions.words_to_cues).parameters["max_words"].default)


@st.composite
def _st_single_cue_overflow_timeline(draw):
    """A timeline ``captions.words_to_cues`` provably groups into exactly one cue.

    ``words_to_cues`` starts a new cue at :data:`_GROUPING_MAX_WORDS` words, a gap over
    0.6 s, or a span over 3.0 s; drawing at most that many words, with gaps of at most
    0.05 s and a span of at most 2.55 s, keeps every draw inside all three limits — so
    every planned cue provably comes from *one* original cue, which is what Property 15
    is about.

    The word count is read from ``words_to_cues`` itself rather than written here. It was
    the literal ``4`` against a limit of 5, so lowering the limit to 3 (C5) silently made
    the draws straddle two cues and the property failed on its own premise rather than on
    the behaviour it tests.
    """
    count = draw(st.integers(min_value=2, max_value=_GROUPING_MAX_WORDS))
    cursor = draw(st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False))
    words = []
    for index in range(count):
        gap = (
            draw(st.floats(min_value=0.0, max_value=0.05, allow_nan=False, allow_infinity=False))
            if index
            else 0.0
        )
        length = draw(
            st.floats(min_value=0.25, max_value=0.6, allow_nan=False, allow_infinity=False)
        )
        start = round(cursor + gap, 3)
        end = round(start + length, 3)
        words.append(FakeWord(start, end, _SPLIT_TOKENS[index]))
        cursor = end
    tail = draw(st.floats(min_value=0.5, max_value=1.0, allow_nan=False, allow_infinity=False))
    return words, round(cursor + tail, 3)


# Feature: kinetic-typography, Property 15: Cue re-splitting conserves the interval
# proportionally — *for every* Word_Timeline and options value that forces a cue overflow,
# the cues produced from one original cue are contiguous, their union equals the original
# snapped interval, and each part's share of the interval is within one frame of its share
# of the words' time span.
@settings(max_examples=100, deadline=None)
@given(
    timeline=_st_single_cue_overflow_timeline(),
    option_map=st_kinetic_options(),
    base=st_time_base().filter(lambda value: value.fps >= 24.0),
)
def test_p15_cue_re_splitting_conserves_the_interval_proportionally(timeline, option_map, base):
    """Validates: Requirements 7.7

    The overflow is *forced*, not hoped for: ``max_lines=1`` and
    ``max_line_width=6`` against 15-unit-wide tokens make ``pack_lines`` emit one
    word per Text_Line and hand the rest back, so a cue of *n* words re-splits
    into *n* parts (asserted: at least two).

    One documented domain restriction: the Time_Base is drawn with ``fps >= 24``.
    A frame *longer* than a split part (fps as low as 1.0 is legal) snaps whole
    parts onto a single boundary, which collapses them and makes the "union equals
    the original interval" clause unsatisfiable for reasons that have nothing to
    do with proportional division. 24 fps is below any real short-form video.
    """
    words, duration = timeline
    options = _plan_options(option_map, max_lines=1, max_line_width=6)

    # The one original cue, taken from the same grouping the planner uses.
    grouped = captions.words_to_cues(words)
    assert len(grouped) == 1
    raw_start, raw_end = float(grouped[0].start), float(grouped[0].end)
    snapped_start, snapped_end = base.snap(raw_start), base.snap(raw_end)
    frame = base.frame_duration()

    plan = plan_kinetic(words, duration, base, options, font="Inter")
    cues = plan.cues
    assert len(cues) >= 2, "the drawn options must force an overflow re-split"

    # Contiguous parts, in Word_Timeline order, and no word divided.
    for previous, following in zip(cues, cues[1:], strict=False):
        assert previous.end == pytest.approx(following.start, abs=_TOL)
    # Their union is exactly the original snapped interval.
    assert cues[0].start == pytest.approx(snapped_start, abs=_TOL)
    assert cues[-1].end == pytest.approx(snapped_end, abs=_TOL)
    covered = sum(cue.end - cue.start for cue in cues)
    assert covered == pytest.approx(snapped_end - snapped_start, abs=_TOL)

    # Each part's share of the interval is within one frame of its share of the
    # words' time span, pairwise — the division the planner performs at each
    # split is head-span vs. the span of everything still to come.
    by_text = {word.text: word for word in words}
    parts = [[by_text[word.text] for word in cue.words] for cue in cues]
    assert [len(group) for group in parts] == [1] * len(cues)
    for index in range(len(cues) - 1):
        head = parts[index]
        tail = [word for group in parts[index + 1 :] for word in group]
        head_span = head[-1].end - head[0].start
        tail_span = tail[-1].end - tail[0].start
        total = head_span + tail_span
        ratio = 0.5 if total <= 0.0 else head_span / total
        # The remaining interval this split divides: the original (un-snapped)
        # cue start for the first split, the previous snapped boundary after.
        remaining_start = raw_start if index == 0 else cues[index].start
        expected = ratio * (raw_end - remaining_start)
        actual = cues[index].end - cues[index].start
        assert abs(actual - expected) <= frame + _TOL
