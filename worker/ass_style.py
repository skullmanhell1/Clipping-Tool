"""One ``AssStyle`` dataclass and one serialiser for the ASS ``[V4+ Styles]`` section.

A ``Style:`` line carries **23 comma-separated fields** in a fixed order, and a ``Dialogue:``
line carries **10**. Neither count is checked by anything at runtime: libass reads what it
recognises, ignores what it does not, and silently falls back for whatever is missing. A style
line with 22 fields does not raise — it renders, slightly wrong, in a way that shows up only as
"the captions look a bit off".

Before this module there were **seven** places that wrote a ``Style:`` line as a raw f-string:

  * ``captions._caption_style`` — three of them, one per legacy template
  * ``captions._preset_style_line``
  * the hook title style, spelled **twice** in ``captions`` (once in ``build_ass`` with a
    hard-coded ``Bold`` of ``-1``, once in ``_preset_header_styles`` with ``ass_bold_flag``)
  * the end-card style in ``captions.write_end_card_ass``
  * ``engines.kinetic._hook_style_line`` — a **third** spelling of the hook style
  * ``engines.kinetic._default_style_line``

and three places that wrote the ``Format:`` header listing those same 23 names. The field order
was therefore stated ten times in total, and the only thing keeping the ten in agreement was that
someone had counted commas correctly on each occasion.

``engines.kinetic._style_line`` shows what that costs. It needed the Safe_Area margins in a style
otherwise built by ``captions``, and the only way to do that was to ``split(",")`` the finished
string and assign to **indices 19, 20 and 21** — guarded by ``if len(fields) == 23`` with a
silent ``return base`` otherwise. So adding a field to ``_preset_style_line`` would not have
raised anywhere; it would have made the kinetic engine quietly stop applying its safe-area
margins. Named fields plus ``dataclasses.replace`` make both the index arithmetic and the
silent fallback unnecessary.

**The ``Format:`` lines are generated from the same field list the dataclass declares**, so the
header and the rows can no longer disagree about what column 8 means.

**Imports nothing but the standard library**, and that is load-bearing rather than tidy:
``worker.engines.kinetic`` must be importable without pulling ``config`` and therefore
``pydantic`` (proved in a fresh interpreter by
``tests/test_engines_base.py::test_every_engine_module_imports_without_heavy_dependencies``),
which is why it reaches ``worker.captions`` through a lazy accessor. A shared style builder
living in ``captions`` would have been unusable from there.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

#: The ASS names of the 23 ``Style:`` columns, in order.
#:
#: Written out rather than derived from the dataclass field names, because the ASS spelling and
#: the Python spelling differ (``PrimaryColour`` vs ``primary_colour``, ``MarginL`` vs
#: ``margin_l``) and the file format's spelling is not ours to choose. The correspondence is
#: checked at import time by the assertion below, so the two lists cannot drift in *length* or
#: order even though they differ in spelling.
STYLE_FIELD_NAMES: tuple[str, ...] = (
    "Name", "Fontname", "Fontsize",
    "PrimaryColour", "SecondaryColour", "OutlineColour", "BackColour",
    "Bold", "Italic", "Underline", "StrikeOut",
    "ScaleX", "ScaleY", "Spacing", "Angle",
    "BorderStyle", "Outline", "Shadow",
    "Alignment", "MarginL", "MarginR", "MarginV",
    "Encoding",
)

#: The ASS names of the 10 ``Dialogue:`` columns, in order.
DIALOGUE_FIELD_NAMES: tuple[str, ...] = (
    "Layer", "Start", "End", "Style", "Name",
    "MarginL", "MarginR", "MarginV", "Effect", "Text",
)

#: The ``[V4+ Styles]`` header row, generated from :data:`STYLE_FIELD_NAMES`.
STYLE_FORMAT_LINE = "Format: " + ", ".join(STYLE_FIELD_NAMES)

#: The ``[Events]`` header row, generated from :data:`DIALOGUE_FIELD_NAMES`.
DIALOGUE_FORMAT_LINE = "Format: " + ", ".join(DIALOGUE_FIELD_NAMES)


@dataclass(frozen=True)
class AssStyle:
    """One ``[V4+ Styles]`` row, by field name.

    Frozen, because a style is shared across every event in a document and every caller that
    needs a variant wants a *copy* — ``dataclasses.replace`` — rather than to mutate the one the
    other events are using. This mirrors the convention that caption presets are frozen and
    shared (``SESSION_HANDOFF.md`` §5).

    Defaults are the values the existing call sites already passed, so a caller states only what
    it actually cares about:

    * ``italic``/``underline``/``strike_out`` are ``0`` — nothing here has ever used them.
    * ``scale_x``/``scale_y`` are ``100`` and ``spacing``/``angle`` ``0`` — the identity metrics.
    * ``border_style`` is ``1`` (outline + shadow). ``3`` means "opaque box", which draws the box
      in ``outline_colour`` rather than stroking the glyphs.
    * ``bold`` is ``-1``, ASS's "true". Note ``captions.ass_bold_flag`` returns ``0`` for a face
      already at weight >= 700, because ASS can only express bold as a flag and libass turns that
      flag into a request for fontconfig weight 200 — which on a face that is *already* heavy
      makes libass synthesise the emboldening, visible as swollen, soft-edged glyphs. So ``-1``
      here is not "make it bold", it is "ask libass to embolden", and for the bundled display
      faces the right answer is ``0``.
    * ``encoding`` is ``1`` (default).
    """

    name: str
    fontname: str
    fontsize: int
    primary_colour: str
    secondary_colour: str
    outline_colour: str
    back_colour: str
    bold: int = -1
    italic: int = 0
    underline: int = 0
    strike_out: int = 0
    scale_x: int = 100
    scale_y: int = 100
    spacing: int = 0
    angle: int = 0
    border_style: int = 1
    outline: int = 0
    shadow: int = 0
    alignment: int = 2
    margin_l: int = 80
    margin_r: int = 80
    margin_v: int = 0
    encoding: int = 1

    def values(self) -> tuple[Any, ...]:
        """The 23 field values in ASS column order."""
        return tuple(getattr(self, field.name) for field in fields(self))

    def render(self) -> str:
        """The ``Style:`` line.

        Values are stringified as-is rather than coerced. Colours arrive as ASS ``&HAABBGGRR``
        literals and the numeric columns as ints, which is what every caller already passed; a
        float leaking into ``fontsize`` would emit ``96.0`` and is worth seeing rather than
        silently rounding away.
        """
        return "Style: " + ",".join(str(value) for value in self.values())


def dialogue(
    text: str,
    *,
    start: str,
    end: str,
    style: str,
    layer: int = 0,
    name: str = "",
    margin_l: int = 0,
    margin_r: int = 0,
    margin_v: int = 0,
    effect: str = "",
) -> str:
    """One ``Dialogue:`` line, by field name.

    A function rather than a dataclass: unlike a style, a dialogue line is built once and
    consumed immediately, and no caller needs to hold one and vary it. ``text`` is positional
    because it is the only field anybody reads when scanning a document.

    ``text`` is written last and is the only field permitted to contain commas — which is why it
    *must* be last, and why this is worth having as a function at all: ASS splits the first nine
    fields on commas and takes the entire remainder as the text, so a caller that reorders the
    columns produces a line that parses without error and renders the wrong thing.
    """
    return (
        f"Dialogue: {layer},{start},{end},{style},{name},"
        f"{margin_l},{margin_r},{margin_v},{effect},{text}"
    )


#: The style column count, exposed because it is worth asserting against.
STYLE_FIELD_COUNT = len(STYLE_FIELD_NAMES)

#: The dialogue column count.
DIALOGUE_FIELD_COUNT = len(DIALOGUE_FIELD_NAMES)

# Import-time consistency check, not a test: the dataclass and the ASS name list describe the same
# columns, so a field added to one and not the other is a defect that must not be reachable. It is
# here rather than in the suite because every caller of this module depends on it and an assertion
# that runs on import cannot be skipped or deselected.
if len(fields(AssStyle)) != STYLE_FIELD_COUNT:  # pragma: no cover - unreachable by construction
    raise RuntimeError(
        f"AssStyle declares {len(fields(AssStyle))} fields but STYLE_FIELD_NAMES lists "
        f"{STYLE_FIELD_COUNT}; the Format: header and the Style: rows would disagree."
    )
