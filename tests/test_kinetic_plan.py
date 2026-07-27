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

The remaining kinetic plan properties (P11, P12, P13, P15) land in this same file
with the planner, in spec epic 6.
"""
from __future__ import annotations

import dataclasses
import json
import math

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.strategies import st_kinetic_options, st_options_mapping, st_word_timeline
from worker.engines.kinetic import (
    DEFAULT_REVEAL,
    DEFAULT_STYLE,
    KINETIC_STYLES,
    REVEAL_MODES,
    Kinetic_Cue,
    Kinetic_Options,
    Kinetic_Plan,
    Kinetic_Word,
)
from worker.engines.timebase import Timeline_Segment
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
    parsed = Kinetic_Options.parse(hostile)          # must not raise
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
    json.dumps(record)                               # JSON-native leaves only
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
    json.dumps(mapping)                              # JSON-encodable (Req 11.2)
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
