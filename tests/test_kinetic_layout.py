"""Kinetic typography — pure layout / Safe_Area geometry properties (tasks 5.4, 5.5).

Covers **Property 14** (line count, line width, word integrity, join rule) and
**Property 16** (Safe_Area margins and resolved alignment) from the
kinetic-typography design, against the pure helpers shipped by tasks 5.1–5.3:
``display_width``, ``is_space_free``, ``join_separator``, ``join_width``,
``pack_lines``, ``resolve_position``, ``position_align`` and
``safe_area_margins``.

Note on Property 14 and the emitted ASS document
------------------------------------------------
The design words Property 14 against the **emitted** ``Default`` events — "at
most ``max_lines - 1`` literal ``\\N`` breaks", "no word's escaped text is split
across a ``\\N``". ``emit_ass`` does not exist yet (spec **task 8**), so the
property is asserted here directly on ``pack_lines`` output — the single source
of the packing the emitter will render — using the *same* invariants restated one
layer down:

* ``len(lines) <= max_lines``  (⇔ at most ``max_lines - 1`` ``\\N`` breaks),
* every line's Display_Width ``<= max_line_width`` unless the line holds exactly
  one word,
* every word index appears exactly once across ``lines + overflow``, in
  Word_Timeline order (⇔ no word dropped, duplicated, reordered or split),
* the join rule: one space between Latin neighbours, none between space-free
  neighbours, taken from ``join_separator`` so the measured width and the emitted
  text cannot disagree.

The Text_Line rendering asserted here is the local ``"\\N".join(...)`` of those
packed lines. When task 8 lands, this test should be **tightened** to run the
same assertions over ``emit_ass(plan)``'s real ``Default`` events (adding the
``captions._escape`` and inline-emoji handling that only the emitter performs).

Property 17 (the font ladder) also belongs to this file per the design's file
mapping; it lands with the engine class in task 9.7.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.strategies import (
    st_i18n_word_timeline,
    st_kinetic_options,
    st_word_timeline,
)
from worker import captions
from worker.effects import caption_presets
from worker.engines.kinetic import (
    POSITIONS,
    Kinetic_Options,
    display_width,
    is_space_free,
    join_separator,
    join_width,
    pack_lines,
    position_align,
    resolve_position,
    safe_area_margins,
)

#: Realistic probed clip sizes (the vertical 9:16 shapes this project renders,
#: plus a few landscape ones), drawn alongside arbitrary integers.
_REAL_SIZES = (16, 64, 240, 360, 480, 720, 1080, 1280, 1920, 2160, 3840, 7680)

#: Probed clip dimensions. The lower bound of 16 px is deliberate: the two
#: clauses of Property 16 are *mutually unsatisfiable* for a handful of
#: degenerate heights (e.g. ``play_res_y == 4`` with ``safe_area_y_pct == 40``
#: needs ``margin_v >= 2`` **and** ``2 * margin_v < 4``), and task 5.3 resolved
#: that tie in favour of "the caption box always has room to exist" by clamping
#: the margins. 16 px is far below any real probed video size, so the generator
#: stays inside the domain where the design's property is satisfiable.
_ST_DIMENSION = st.one_of(
    st.sampled_from(_REAL_SIZES),
    st.integers(min_value=16, max_value=7680),
)


def _line_width(texts):
    """Display_Width of one packed Text_Line, joins included."""
    if not texts:
        return 0
    total = display_width(texts[0])
    for previous, following in zip(texts, texts[1:], strict=False):
        total += join_width(previous, following) + display_width(following)
    return total


def _render_line(texts):
    """The Text_Line as the emitter will join it (Req 8.4)."""
    if not texts:
        return ""
    out = texts[0]
    for previous, following in zip(texts, texts[1:], strict=False):
        out += join_separator(previous, following) + following
    return out


# --------------------------------------------------------------------------- #
# Property 14 (task 5.4)                                                        #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 14: Layout respects line count, line width, and
# word integrity — *For every* Word_Timeline and Kinetic_Options value, every `Default`
# event contains at most `max_lines - 1` literal `\N` breaks; every Text_Line's
# Display_Width is at most `max_line_width` unless that line holds exactly one word; and
# no word's escaped text is split across a `\N` break. Latin-script neighbours are joined
# by exactly one space, space-free-script neighbours by none.
@settings(max_examples=100, deadline=None)
@given(
    timeline=st.one_of(st_word_timeline(), st_i18n_word_timeline()),
    option_data=st_kinetic_options(),
)
def test_p14_layout_respects_line_count_line_width_and_word_integrity(timeline, option_data):
    """Validates: Requirements 7.5, 7.6, 7.8, 7.9, 8.1, 8.2, 8.4, 8.5, 8.10

    Asserted on ``pack_lines`` output rather than on emitted ``Default`` events
    (``emit_ass`` lands in task 8 — see the module docstring): line count, line
    width with the single-over-long-word exemption, word conservation in order,
    and the space-free join rule.
    """
    words, _duration = timeline
    opts = Kinetic_Options.parse(option_data)
    texts = [word.text for word in words]

    lines, overflow = pack_lines(texts, opts.max_lines, opts.max_line_width)

    # --- line count: at most max_lines Text_Lines, i.e. max_lines - 1 breaks --
    assert len(lines) <= opts.max_lines
    document = "\\N".join(_render_line([texts[i] for i in line]) for line in lines)
    assert document.count("\\N") <= max(opts.max_lines - 1, 0)

    # --- word conservation: every index exactly once, in timeline order -------
    flat = [index for line in lines for index in line] + list(overflow)
    assert flat == list(range(len(texts)))
    assert all(line for line in lines)  # no empty Text_Line is ever emitted
    for line in lines:
        assert list(line) == sorted(line)

    # --- overflow is the contiguous tail the planner re-splits (Req 7.7) ------
    if overflow:
        assert len(lines) == opts.max_lines
        assert list(overflow) == list(range(overflow[0], len(texts)))

    for line in lines:
        line_texts = [texts[index] for index in line]
        rendered = _render_line(line_texts)

        # --- width bound, with the one-over-long-word exemption (Req 8.5) -----
        width = _line_width(line_texts)
        if width > opts.max_line_width:
            assert len(line_texts) == 1
        # The measured width is the width of the text that will be emitted, so a
        # line can never be measured short and rendered long (Reqs 8.1, 8.10).
        assert width == display_width(rendered)

        # --- word integrity: no word crosses a break (Reqs 7.8, 7.9) ---------
        assert "\\N" not in rendered
        cursor = 0
        for text in line_texts:
            found = rendered.find(text, cursor)
            assert found >= cursor
            cursor = found + len(text)

        # --- the join rule (Reqs 8.2, 8.4) -----------------------------------
        for previous, following in zip(line_texts, line_texts[1:], strict=False):
            separator = join_separator(previous, following)
            if is_space_free(previous) and is_space_free(following):
                assert separator == ""
            else:
                assert separator == " "
            assert join_width(previous, following) == display_width(separator)

    # Splitting the assembled document on the literal break recovers exactly the
    # packed Text_Lines: the break is only ever inserted *between* lines.
    if lines:
        assert document.split("\\N") == [
            _render_line([texts[index] for index in line]) for line in lines
        ]


# --------------------------------------------------------------------------- #
# Property 16 (task 5.5)                                                        #
# --------------------------------------------------------------------------- #
def _assert_safe_area(*, position, preset_position, width, height, pct_x, pct_y):
    """Property 16's clauses for one (options, probed size) combination."""
    align, margin_l, margin_r, margin_v = safe_area_margins(
        width,
        height,
        safe_area_x_pct=pct_x,
        safe_area_y_pct=pct_y,
        position=position,
        preset_position=preset_position,
    )

    inset_x = int(round(width * pct_x / 100.0))
    inset_y = int(round(height * pct_y / 100.0))

    # --- margins are at least the Safe_Area insets (Reqs 7.2, 7.10) ----------
    assert margin_l >= inset_x
    assert margin_r >= inset_x
    assert margin_l == margin_r  # one horizontal inset, applied both sides
    assert margin_v >= inset_y

    # --- the caption box still fits inside the frame (Req 7.10) -------------
    assert margin_l + margin_r < width
    assert 2 * margin_v < height

    # --- alignment is _POSITION_ALIGN's value for the *resolved* position ----
    expected_position = (
        position
        if position in POSITIONS
        else (preset_position if preset_position in POSITIONS else "bottom")
    )
    expected_align, default_margin_v = captions._POSITION_ALIGN[expected_position]
    assert resolve_position(position, preset_position) == expected_position
    assert align == expected_align
    assert position_align(position, preset_position) == (
        expected_align,
        default_margin_v,
    )
    # v0.8.0 vertical placement is preserved whenever it fits (task 5.3's
    # ``max(default_margin_v, inset_y)``).
    assert margin_v >= min(default_margin_v, (height - 1) // 2)


# Feature: kinetic-typography, Property 16: Style margins keep the caption box inside the
# Safe_Area — *For every* Kinetic_Options value and probed clip size, the emitted `Style:`
# line's `MarginL`, `MarginR`, and `MarginV` are each at least the corresponding Safe_Area
# inset in pixels, `MarginL + MarginR < PlayResX`, `2 * MarginV < PlayResY`, and
# `Alignment` is the value `_POSITION_ALIGN` gives for the resolved position (with the
# Base_Preset position used when the option is empty).
@settings(max_examples=100, deadline=None)
@given(
    option_data=st_kinetic_options(),
    width=_ST_DIMENSION,
    height=_ST_DIMENSION,
)
def test_p16_style_margins_keep_the_caption_box_inside_the_safe_area(option_data, width, height):
    """Validates: Requirements 7.2, 7.3, 7.4, 7.10

    Asserted on ``safe_area_margins`` / ``position_align`` — the helpers that
    produce the three margin columns and the ``Alignment`` column of the emitted
    ``Style: Default`` line (task 8.1 copies them verbatim). The empty-position
    case inherits the Base_Preset position, so the ``hormozi`` centre placement is
    covered both by the draw and by the forced case below.
    """
    opts = Kinetic_Options.parse(option_data)
    preset, _substituted = caption_presets.resolve_preset(opts.preset_name)

    _assert_safe_area(
        position=opts.position,
        preset_position=preset.position,
        width=width,
        height=height,
        pct_x=opts.safe_area_x_pct,
        pct_y=opts.safe_area_y_pct,
    )

    # Deterministic coverage of the inheritance branch (Req 7.4) for every
    # example: an empty option position under a ``center`` preset (``hormozi``)
    # must resolve to alignment 5, not the ``bottom`` default — and under each of
    # the other two preset positions to their own alignment.
    for preset_position in POSITIONS:
        _assert_safe_area(
            position="",
            preset_position=preset_position,
            width=width,
            height=height,
            pct_x=opts.safe_area_x_pct,
            pct_y=opts.safe_area_y_pct,
        )
    assert caption_presets.BUILTIN_PRESETS["hormozi"].position == "center"
    assert resolve_position("", "center") == "center"
    assert position_align("", "center")[0] == 5
