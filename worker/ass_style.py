"""One spelling of an ASS ``Style:`` line, one spelling of a ``Dialogue:`` line, one header.

Why this module exists
----------------------
ASS declares its own record layout. ``[V4+ Styles]`` opens with a ``Format:`` line naming 23
fields in order, ``[Events]`` opens with one naming 10, and every following record is those
fields comma-separated in that order. libass reads the ``Format:`` line and then reads each
record positionally.

It does not verify the count. A ``Style:`` line with 22 fields parses: libass fills what it can,
leaves the rest at their defaults, and renders. So the failure mode of a dropped comma is not an
exception and not a parse error — it is a caption that comes out in the wrong colour, at the
wrong size, or anchored to the wrong corner, on a code path that reported success.

Before this module, nine f-strings across ``worker/captions.py`` and
``worker/engines/kinetic.py`` each spelled those 23 fields out by hand, and one of them
(``kinetic._style_line``) reached into another's output by *index* to replace three columns.
Nothing tied any of them to the ``Format:`` line they had to agree with.

So the field order here is derived from the ``Format:`` line rather than restated alongside it,
and :func:`_assert_fields_match_format` checks the two at import time. Getting the count wrong is
now a failure to construct a dataclass, which raises.

What this module deliberately does *not* do
-------------------------------------------
**It does not coerce.** ``serialise`` joins ``str(value)``; it never calls ``int()``. Some call
sites coerce and some do not — ``kinetic`` wraps every numeric field in ``int()`` while
``captions`` passes ``preset.font_size`` through untouched — and quietly adding coercion here
would change what those sites emit. Coercion stays where it already is, and the type annotations
below describe what callers are expected to pass rather than what is enforced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields

#: The ``[V4+ Styles]`` field names, in the order ASS reads them. **This tuple is the schema.**
#:
#: :data:`STYLE_FORMAT` is rendered from it and :class:`AssStyle`'s fields are checked against it,
#: so the declaration and the records can no longer disagree.
STYLE_FIELD_NAMES: tuple[str, ...] = (
    "Name", "Fontname", "Fontsize",
    "PrimaryColour", "SecondaryColour", "OutlineColour", "BackColour",
    "Bold", "Italic", "Underline", "StrikeOut",
    "ScaleX", "ScaleY", "Spacing", "Angle",
    "BorderStyle", "Outline", "Shadow",
    "Alignment", "MarginL", "MarginR", "MarginV", "Encoding",
)

#: The ``[Events]`` field names, in order.
EVENT_FIELD_NAMES: tuple[str, ...] = (
    "Layer", "Start", "End", "Style", "Name",
    "MarginL", "MarginR", "MarginV", "Effect", "Text",
)

#: The two ``Format:`` lines, rendered from the schemas above.
#:
#: Spelled with ``", "`` after each comma because that is what the existing files emit and what
#: every golden ASS document in the test suite contains. libass tolerates either.
STYLE_FORMAT = "Format: " + ", ".join(STYLE_FIELD_NAMES)
EVENT_FORMAT = "Format: " + ", ".join(EVENT_FIELD_NAMES)


def _snake(name: str) -> str:
    """``"BorderStyle"`` -> ``"border_style"``, ``"ScaleX"`` -> ``"scale_x"``."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@dataclass(frozen=True)
class AssStyle:
    """One ``[V4+ Styles]`` record.

    Field order **is** :data:`STYLE_FIELD_NAMES`; :meth:`serialise` relies on
    ``dataclasses.fields`` returning declaration order, and the import-time check below relies on
    the two agreeing. Do not reorder these without reordering the schema.

    Every field that has a default has the value the legacy caption templates used, so a partially
    specified style reproduces the v0.8.0 look rather than libass' own defaults.
    """

    name: str
    fontname: str
    fontsize: object
    primary_colour: str
    secondary_colour: str
    outline_colour: str
    back_colour: str
    #: ASS carries weight as a single flag, which libass turns into a request for fontconfig
    #: weight 200 (CSS 700). ``-1`` asks for bold, ``0`` leaves the face alone.
    #:
    #: ``0`` is the right answer for a face that is *already* heavy: asking bold on top makes
    #: libass synthesise the emboldening on glyphs that were drawn heavy, which reads as swollen
    #: and soft-edged. ``captions.ass_bold_flag`` decides this from the preset's declared weight
    #: and returns ``0`` at or above 700.
    bold: object = -1
    italic: object = 0
    underline: object = 0
    strike_out: object = 0
    scale_x: object = 100
    scale_y: object = 100
    spacing: object = 0
    angle: object = 0
    #: ``1`` = outline + drop shadow, ``3`` = opaque box drawn in ``outline_colour``.
    border_style: object = 1
    outline: object = 2
    #: Drop-shadow depth — **or**, when a preset asks for a second stroke, the width of that
    #: stroke.
    #:
    #: ASS has one border width and one border colour, so a genuine two-tone edge needs the text
    #: drawn twice. Instead the shadow slot is repurposed: at offset 0, with its own colour in
    #: ``back_colour``, it renders as an outer stroke around the inner one and gives the
    #: "sticker" edge in a single event. That is why the two are mutually exclusive — a preset
    #: cannot have both a drop shadow and an outer stroke this way. See
    #: ``captions._preset_style_line``, which is where the swap happens.
    shadow: object = 1
    alignment: object = 2
    margin_l: object = 80
    margin_r: object = 80
    margin_v: object = 200
    encoding: object = 1

    def serialise(self) -> str:
        """The ``Style:`` line, with exactly ``len(STYLE_FIELD_NAMES)`` comma-separated fields.

        No coercion: values are stringified as given. See the module docstring.
        """
        return "Style: " + ",".join(str(getattr(self, f.name)) for f in fields(self))

    def with_margins(self, margin_l: object, margin_r: object, margin_v: object) -> AssStyle:
        """A copy with the three margin columns replaced.

        This exists because ``kinetic._style_line`` used to do it by splitting a finished
        ``Style:`` line on commas and assigning to indices 19, 20 and 21 — correct only for as
        long as nothing before those columns ever changed width, and silently wrong the moment
        something did. Named fields cannot drift that way.
        """
        return AssStyle(
            **{
                **{f.name: getattr(self, f.name) for f in fields(self)},
                "margin_l": margin_l,
                "margin_r": margin_r,
                "margin_v": margin_v,
            }
        )


def dialogue(
    text: str,
    *,
    style: str,
    start: str,
    end: str,
    layer: object = 0,
    name: str = "",
    margin_l: object = 0,
    margin_r: object = 0,
    margin_v: object = 0,
    effect: str = "",
) -> str:
    """One ``[Events]`` record, with exactly ``len(EVENT_FIELD_NAMES)`` fields.

    ``text`` is last and may itself contain commas — a ``\\move(x,y,x,y,t)`` override has four —
    which is precisely why the nine preceding fields are worth building rather than typing. A
    reader cannot tell a 10-field line from a 9-field one by counting commas.

    Not coerced, and ``text`` is not escaped: callers pass text that ``captions._escape`` has
    already handled, and double-escaping it here would corrupt the ``\\N`` line breaks the
    measured layout depends on.
    """
    return "Dialogue: " + ",".join(
        str(part) for part in (
            layer, start, end, style, name, margin_l, margin_r, margin_v, effect, text,
        )
    )


def header(
    *,
    play_res_x: object,
    play_res_y: object,
    styles: tuple[str, ...] | list[str],
    wrap_style: object = 2,
) -> str:
    """The ``[Script Info]`` + ``[V4+ Styles]`` + ``[Events]`` preamble, up to and including the
    events ``Format:`` line.

    ``wrap_style`` defaults to ``2``, which means *libass performs no automatic wrapping at all*.
    That is load-bearing rather than incidental: line breaks are measured in the resolved face at
    the real frame width and emitted as explicit ``\\N``, and letting libass also wrap would give
    two competing decisions. The exception is a shaping script (Arabic, Devanagari), where the
    measurement is wrong by construction — a word's rendered width is not the sum of its letters'
    isolated advances — so ``worker.script_support.wrap_style`` returns ``0`` there and hands
    wrapping back to libass.

    ``ScaledBorderAndShadow: yes`` makes outline and shadow widths scale with ``PlayRes``, so a
    preset's outline width means the same thing at 1080x1920 as at 720x1280.
    """
    return "\n".join((
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        f"WrapStyle: {wrap_style}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        STYLE_FORMAT,
        *styles,
        "",
        "[Events]",
        EVENT_FORMAT,
    ))


def _assert_fields_match_format() -> None:
    """Check at import that :class:`AssStyle` matches :data:`STYLE_FIELD_NAMES`, in order.

    The whole point of the module is that the record and its ``Format:`` declaration cannot
    disagree, and that guarantee is worth exactly as much as this check. Raising on import is
    deliberate: a mismatch here would otherwise surface as captions rendering wrongly, which no
    test asserts on directly.
    """
    declared = tuple(f.name for f in fields(AssStyle))
    expected = tuple(_snake(name) for name in STYLE_FIELD_NAMES)
    if declared != expected:
        raise AssertionError(
            "AssStyle fields do not match the [V4+ Styles] Format: line.\n"
            f"  dataclass: {declared}\n"
            f"  Format:    {expected}"
        )


_assert_fields_match_format()
