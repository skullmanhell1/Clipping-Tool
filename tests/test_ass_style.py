"""`worker.ass_style`'s own contract: the field-count guarantee, and the absence of coercion.

`tests/test_ass_style_parity.py` checks that the documents this module builds are unchanged.
This module checks the properties that make it worth having — that the record layout cannot drift
from the `Format:` line that declares it, and that nothing is silently coerced on the way out.
"""

from __future__ import annotations

import dataclasses

import pytest

from worker.ass_style import (
    EVENT_FIELD_NAMES,
    EVENT_FORMAT,
    STYLE_FIELD_NAMES,
    STYLE_FORMAT,
    AssStyle,
    _assert_fields_match_format,
    _snake,
    dialogue,
    header,
)


def a_style(**overrides) -> AssStyle:
    """A fully specified style; every field without a default supplied."""
    return AssStyle(**{
        "name": "Default", "fontname": "Anton", "fontsize": 96,
        "primary_colour": "&H00FFFFFF", "secondary_colour": "&H0000E5FF",
        "outline_colour": "&H00000000", "back_colour": "&H64000000",
        **overrides,
    })


# --------------------------------------------------------------------------- #
# The field-count guarantee                                                     #
# --------------------------------------------------------------------------- #

def test_the_style_record_has_one_field_per_declared_format_column():
    """The guarantee the module exists to provide, asserted directly.

    libass reads `Style:` records positionally against the `Format:` line and does not check the
    count, so a mismatch is a silent rendering fault rather than an error. This is the check that
    replaces it.
    """
    assert len(dataclasses.fields(AssStyle)) == len(STYLE_FIELD_NAMES) == 23


def test_the_dataclass_field_order_matches_the_format_line():
    declared = [f.name for f in dataclasses.fields(AssStyle)]
    assert declared == [_snake(name) for name in STYLE_FIELD_NAMES]


def test_the_import_time_check_actually_fails_on_a_mismatch(monkeypatch):
    """Because a guard that cannot fail is not a guard.

    Without this, reordering :data:`STYLE_FIELD_NAMES` and :class:`AssStyle` *together and
    wrongly* would still pass every other test in the suite.
    """
    monkeypatch.setattr(
        "worker.ass_style.STYLE_FIELD_NAMES",
        STYLE_FIELD_NAMES[:-1],  # 22 columns against 23 fields
    )
    with pytest.raises(AssertionError, match="do not match"):
        _assert_fields_match_format()


def test_a_serialised_style_has_exactly_the_declared_number_of_fields():
    line = a_style().serialise()
    assert line.startswith("Style: ")
    assert len(line[len("Style: "):].split(",")) == len(STYLE_FIELD_NAMES)


def test_the_format_lines_declare_the_field_names_in_order():
    assert STYLE_FORMAT == "Format: " + ", ".join(STYLE_FIELD_NAMES)
    assert EVENT_FORMAT == "Format: " + ", ".join(EVENT_FIELD_NAMES)


@pytest.mark.parametrize(("camel", "snake"), [
    ("Name", "name"),
    ("Fontname", "fontname"),
    ("PrimaryColour", "primary_colour"),
    ("ScaleX", "scale_x"),
    ("BorderStyle", "border_style"),
    ("MarginL", "margin_l"),
    ("StrikeOut", "strike_out"),
])
def test_snake_maps_each_format_column_to_its_field(camel, snake):
    assert _snake(camel) == snake


# --------------------------------------------------------------------------- #
# with_margins — what replaced index surgery                                    #
# --------------------------------------------------------------------------- #

def test_with_margins_replaces_the_three_margin_columns_and_nothing_else():
    original = a_style(margin_l=80, margin_r=80, margin_v=220)
    moved = original.with_margins(65, 70, 300)

    assert (moved.margin_l, moved.margin_r, moved.margin_v) == (65, 70, 300)
    untouched = {
        f.name for f in dataclasses.fields(AssStyle)
    } - {"margin_l", "margin_r", "margin_v"}
    for name in untouched:
        assert getattr(moved, name) == getattr(original, name), name


def test_with_margins_lands_on_the_columns_the_format_line_names():
    """The property the old index-based version assumed but could not check.

    `kinetic._style_line` used to assign to fields 19, 20 and 21 of a split string. This asserts
    that the margins really are at those positions *according to the Format line*, which is the
    thing that made the old code correct — and which nothing previously verified.
    """
    fields = a_style().with_margins(11, 22, 33).serialise()[len("Style: "):].split(",")
    for column, expected in (("MarginL", "11"), ("MarginR", "22"), ("MarginV", "33")):
        assert fields[STYLE_FIELD_NAMES.index(column)] == expected, column


def test_a_style_is_immutable():
    """Presets are shared across clips in a job; a mutable style would leak between them."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        a_style().fontsize = 120  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Events                                                                        #
# --------------------------------------------------------------------------- #

def test_a_dialogue_line_has_exactly_the_declared_number_of_fields():
    line = dialogue("HELLO", style="Default", start="0:00:00.00", end="0:00:01.00")
    assert line.startswith("Dialogue: ")
    assert len(line[len("Dialogue: "):].split(",")) == len(EVENT_FIELD_NAMES)


def test_commas_inside_the_text_field_do_not_add_fields():
    """The reason building these beats typing them.

    A `\\move(x,y,x,y,t)` override contains four commas, so a reader cannot tell a well-formed
    event from a malformed one by counting. Splitting with the declared bound is the only way to
    recover the ten fields, and that only works if the first nine are correct.
    """
    text = r"{\move(540,1740,540,1700,0,120)}WORD"
    line = dialogue(text, style="Default", start="0:00:00.00", end="0:00:01.00")
    fields = line[len("Dialogue: "):].split(",", len(EVENT_FIELD_NAMES) - 1)
    assert len(fields) == len(EVENT_FIELD_NAMES)
    assert fields[-1] == text
    # Naively splitting on every comma over-counts, which is the trap being avoided.
    assert len(line[len("Dialogue: "):].split(",")) > len(EVENT_FIELD_NAMES)


def test_the_event_field_order_is_the_one_the_format_line_declares():
    line = dialogue(
        "TEXT", style="Hook", start="0:00:01.00", end="0:00:02.00",
        layer=1, name="who", margin_l=1, margin_r=2, margin_v=3, effect="fx",
    )
    fields = line[len("Dialogue: "):].split(",", len(EVENT_FIELD_NAMES) - 1)
    assert dict(zip(EVENT_FIELD_NAMES, fields)) == {
        "Layer": "1", "Start": "0:00:01.00", "End": "0:00:02.00",
        "Style": "Hook", "Name": "who",
        "MarginL": "1", "MarginR": "2", "MarginV": "3",
        "Effect": "fx", "Text": "TEXT",
    }


def test_dialogue_does_not_escape_its_text():
    """Callers pass text `captions._escape` already handled; escaping again would corrupt `\\N`."""
    text = r"FIRST\NSECOND"
    line = dialogue(text, style="Default", start="0:00:00.00", end="0:00:01.00")
    assert line.endswith(text)


# --------------------------------------------------------------------------- #
# The header                                                                    #
# --------------------------------------------------------------------------- #

def test_the_header_declares_both_formats_and_carries_every_style():
    out = header(
        play_res_x=1080, play_res_y=1920,
        styles=(a_style().serialise(), a_style(name="Hook").serialise()),
    )
    lines = out.splitlines()
    assert lines[:2] == ["[Script Info]", "ScriptType: v4.00+"]
    assert "PlayResX: 1080" in lines
    assert "PlayResY: 1920" in lines
    assert "ScaledBorderAndShadow: yes" in lines
    # The styles sit between the [V4+ Styles] Format line and the blank line before [Events].
    assert lines[lines.index(STYLE_FORMAT) + 1].startswith("Style: Default,")
    assert lines[lines.index(STYLE_FORMAT) + 2].startswith("Style: Hook,")
    # The events Format line is last, so a caller appends events directly after it.
    assert lines[-1] == EVENT_FORMAT
    assert lines[-2] == "[Events]"


def test_the_header_defaults_to_wrapstyle_2():
    """2 means libass performs no wrapping of its own, which the measured `\\N` breaks require."""
    out = header(play_res_x=1080, play_res_y=1920, styles=())
    assert "WrapStyle: 2" in out.splitlines()


def test_the_header_passes_a_shaping_scripts_wrap_style_through():
    """`script_support.wrap_style` returns 0 for Arabic/Devanagari, handing wrapping to libass."""
    out = header(play_res_x=1080, play_res_y=1920, styles=(), wrap_style=0)
    assert "WrapStyle: 0" in out.splitlines()


# --------------------------------------------------------------------------- #
# The documented non-behaviour                                                  #
# --------------------------------------------------------------------------- #

def test_serialise_does_not_coerce():
    """Pinned because it is a deliberate omission, not an oversight.

    Call sites disagree about coercion — `kinetic` wraps every number in `int()`, the caption path
    passes `preset.font_size` through untouched — so adding coercion here would change what one of
    them emits. A float reaching this module means a caller stopped coercing, and that is the
    caller's bug to see; libass reads `"96.0"` as malformed and falls back to a default.
    """
    line = a_style(fontsize=96.0, outline=4.5).serialise()
    fields = line[len("Style: "):].split(",")
    assert fields[STYLE_FIELD_NAMES.index("Fontsize")] == "96.0"
    assert fields[STYLE_FIELD_NAMES.index("Outline")] == "4.5"


def test_the_defaults_are_the_legacy_karaoke_look():
    """So a partially specified style reproduces the v0.8.0 look, not libass' own defaults."""
    defaults = {
        f.name: f.default for f in dataclasses.fields(AssStyle)
        if f.default is not dataclasses.MISSING
    }
    assert defaults == {
        "bold": -1, "italic": 0, "underline": 0, "strike_out": 0,
        "scale_x": 100, "scale_y": 100, "spacing": 0, "angle": 0,
        "border_style": 1, "outline": 2, "shadow": 1,
        "alignment": 2, "margin_l": 80, "margin_r": 80, "margin_v": 200,
        "encoding": 1,
    }
