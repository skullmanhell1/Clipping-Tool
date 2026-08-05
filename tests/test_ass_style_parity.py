"""Every ASS document this repo writes, frozen byte-for-byte, per configuration.

ASS is assembled as raw f-strings in nine places across two modules. The `[V4+ Styles]`
`Format:` line declares **23** comma-separated fields and `[Events]` declares **10**, and
libass does not complain when a line carries the wrong number of them — it falls back to a
default for the fields it could not read and renders anyway. A dropped comma is therefore not a
crash and not a test failure: it is a caption that quietly comes out in the wrong colour, at the
wrong size, or in the wrong place.

So before consolidating those f-strings behind one dataclass, this pins what they currently
produce. These are **characterisation tests** — they assert current behaviour, not desired
behaviour, and their entire value is in having been written *first*. If a refactor changes one of
these strings it has changed how a caption renders, whatever else the suite says.

The whole document is frozen, not just the `Style:` line, because the header carries three things
the styles depend on: `PlayResX/Y` (every margin and font size is in those units),
`WrapStyle` (2 means libass does no wrapping of its own, which the measured `\\N` breaks rely on)
and `ScaledBorderAndShadow` (whether outline width scales with PlayRes).

Font resolution is stubbed. `resolve_font` shells out to `fc-list`, so an unstubbed golden would
encode whichever fonts the host happens to have installed and fail on a machine with a different
set — the one kind of failure that teaches nothing. Both outcomes are frozen instead: a font
that resolves to itself, and a font that gets substituted (which also changes the `Style: Hook`
line, since it takes the resolved family).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from tests.conftest import FakeWord
from worker import captions
from worker.effects.caption_presets import BUILTIN_PRESETS
from worker.engines import kinetic
from worker.engines.timebase import Time_Base

#: Committed alongside this module. Regenerated deliberately, by running
#: ``python scripts/freeze_ass_styles.py`` — never automatically, and never from inside the test
#: run. A golden that rewrites itself when it fails is not a guard.
#:
#: **Inspect the diff.** Every changed line is a change to how something renders on screen.
GOLDEN = Path(__file__).parent / "golden" / "ass_documents.json"

#: The `Format:` line's field count, which is what makes a miscount detectable at all.
STYLE_FIELDS = 23
EVENT_FIELDS = 10

#: A representative subset of positions rather than all nine: the alignment/margin mapping is
#: exhaustively tested elsewhere, and what matters here is that each *branch* appears — an edge
#: anchored style (bottom), a centred one (MarginV is meaningless for alignments 4-6 and is
#: forced to 0), and a top one.
POSITIONS = ("bottom", "center", "top")

TEMPLATES = ("karaoke", "boxed", "minimal")


def cues() -> list[captions.Cue]:
    """Two cues with a keyword in each, so per-word spans and cue grouping both appear."""
    return [
        captions.Cue(0.2, 1.1, [FakeWord(0.2, 0.6, "This"), FakeWord(0.7, 1.1, "is")]),
        captions.Cue(1.2, 2.2, [FakeWord(1.2, 1.6, "fire"), FakeWord(1.7, 2.2, "money")]),
    ]


def stub_fonts(monkeypatch, substitute: str | None = None) -> None:
    """Make font resolution deterministic, in both directions.

    ``substitute=None`` freezes the resolves-to-itself path; a name freezes the substitution
    path, which is the one that also rewrites ``Style: Hook`` and appends a
    ``font_substituted:`` note.
    """
    if substitute is None:
        monkeypatch.setattr(captions, "resolve_font", lambda name, **kw: (name, False))
    else:
        monkeypatch.setattr(captions, "resolve_font", lambda name, **kw: (substitute, True))
    # `_caption_style` and the legacy path do not resolve fonts, but `script_support` may
    # substitute one for a non-Latin script. Only Latin text is used here, so it is inert.
    monkeypatch.setattr(captions, "font_available", lambda name: True)


def build_case(name: str, monkeypatch, tmp_path: Path) -> str:
    """Produce one named ASS document. Shared with ``scripts/freeze_ass_styles.py``.

    Every case is either a pure function or a write to ``tmp_path``; none of them probe the host
    beyond what :func:`stub_fonts` has already replaced.
    """
    kind, _, rest = name.partition("/")

    if kind == "legacy":
        template, position = rest.split("/")
        stub_fonts(monkeypatch)
        dest = captions.build_ass(
            cues(), tmp_path / "out.ass", video_width=1080, video_height=1920,
            template=template, position=position, hook_text="WAIT FOR IT",
        )
        return dest.read_text(encoding="utf-8")

    if kind == "preset":
        preset_name, position = rest.split("/")
        stub_fonts(monkeypatch)
        dest = captions.build_ass(
            cues(), tmp_path / "out.ass", video_width=1080, video_height=1920,
            position=position, hook_text="WAIT FOR IT",
            preset=BUILTIN_PRESETS[preset_name], keyword_indices={2},
            clip_duration=2.5,
        )
        return dest.read_text(encoding="utf-8")

    if kind == "preset_substituted":
        stub_fonts(monkeypatch, substitute="Liberation Sans")
        notes: list[str] = []
        dest = captions.build_ass(
            cues(), tmp_path / "out.ass", position="bottom", hook_text="WAIT",
            preset=BUILTIN_PRESETS[rest], notes=notes,
        )
        # The note is part of the observable output: it is what tells an operator which font a
        # clip was actually rendered in.
        return dest.read_text(encoding="utf-8") + f"\n# notes: {notes}\n"

    if kind == "end_card":
        stub_fonts(monkeypatch)
        dest = captions.write_end_card_ass(
            tmp_path / "card.ass", 20.0, video_width=1080, video_height=1920,
            text="FOLLOW FOR MORE", seconds=float(rest),
        )
        if dest is None:
            return "# write_end_card_ass returned None\n"
        return dest.read_text(encoding="utf-8")

    if kind == "kinetic":
        preset_name, style = rest.split("/")
        return kinetic.emit_ass(_kinetic_plan(preset_name, style))

    if kind == "kinetic_fallback":
        # A plan carrying neither `style_line` nor `hook_style` — the only path that reaches
        # `_fallback_style_line` and `_hook_style_line`'s default bold. Built by emptying a real
        # plan rather than by hand, so this case cannot drift out of the plan's own shape.
        plan = dataclasses.replace(
            _kinetic_plan("karaoke", "karaoke_fill"), style_line="", hook_style="",
        )
        return kinetic.emit_ass(plan)

    raise AssertionError(f"unknown case kind {kind!r} in {name!r}")


def _kinetic_plan(preset_name: str, style: str) -> kinetic.Kinetic_Plan:
    """A real plan from the real planner. No stubs — `plan_kinetic` is already pure.

    It never probes the host: the family comes from the passed font, and the look comes from
    ``options.preset_name`` via ``caption_presets.resolve_preset``. That is why the kinetic cases
    do not need :func:`stub_fonts`.
    """
    return kinetic.plan_kinetic(
        [FakeWord(0.2, 0.6, "This"), FakeWord(0.7, 1.1, "is"),
         FakeWord(1.2, 1.6, "fire"), FakeWord(1.7, 2.2, "money")],
        2.5,
        Time_Base(fps=30.0),
        kinetic.Kinetic_Options(
            style=style, preset_name=preset_name, hook_enabled=True,
            preset_font=BUILTIN_PRESETS[preset_name].font,
        ),
        BUILTIN_PRESETS[preset_name].font,
        "WAIT FOR IT",
        keyword_planner=lambda flat, use_ai=False, client=None: {2},
        play_res_x=1080,
        play_res_y=1920,
    )


def _kinetic_styles() -> tuple[str, ...]:
    """The animation styles the kinetic planner accepts, read from the engine itself.

    Read rather than listed so a new style cannot be added without this golden noticing — the
    matrix-coverage test below turns that into a failure.
    """
    return tuple(sorted(kinetic.KINETIC_STYLES))


def configurations() -> tuple[str, ...]:
    """Every frozen case name, in a stable order."""
    names: list[str] = []
    for template in TEMPLATES:
        for position in POSITIONS:
            names.append(f"legacy/{template}/{position}")
    # Every built-in preset at its default position, so each preset's colours, border style,
    # outline/shadow, glyph metrics and bold decision are all pinned...
    for preset in sorted(BUILTIN_PRESETS):
        names.append(f"preset/{preset}/bottom")
    # ...and a few at the other two anchors, where the margin arithmetic differs.
    for position in ("center", "top"):
        for preset in ("karaoke", "pill", "sticker"):
            names.append(f"preset/{preset}/{position}")
    names.append("preset_substituted/hormozi")
    # 3.0 renders; 0.0 and a duration longer than the clip do not.
    for seconds in ("3.0", "0.0", "99.0"):
        names.append(f"end_card/{seconds}")
    for style in _kinetic_styles():
        names.append(f"kinetic/karaoke/{style}")
    names.append("kinetic_fallback/default")
    return tuple(names)


CONFIGURATIONS = configurations()


@pytest.fixture(scope="module")
def golden() -> dict:
    if not GOLDEN.exists():
        return {}
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", CONFIGURATIONS)
def test_the_document_is_unchanged(name, golden, monkeypatch, tmp_path):
    """One configuration, compared against its frozen ASS document."""
    produced = build_case(name, monkeypatch, tmp_path)

    if name not in golden:
        # Failure rather than skip: a skip satisfies the suite while checking nothing, and CI
        # rejects skips precisely because they read as passes.
        pytest.fail(
            f"no frozen document for {name!r}. Generate it with "
            "`python scripts/freeze_ass_styles.py` and inspect the diff."
        )

    expected = golden[name]
    if produced != expected:
        # Line-by-line so a failure names the field that moved instead of dumping two documents.
        produced_lines = produced.splitlines()
        expected_lines = expected.splitlines()
        for index, (got, want) in enumerate(zip(produced_lines, expected_lines), start=1):
            assert got == want, f"{name}: line {index} changed\n  was:  {want}\n  now:  {got}"
        assert len(produced_lines) == len(expected_lines), (
            f"{name}: line count changed from {len(expected_lines)} to {len(produced_lines)}"
        )
        raise AssertionError(f"{name}: document differs only in trailing whitespace")


@pytest.mark.parametrize("name", CONFIGURATIONS)
def test_every_line_carries_the_field_count_its_format_declares(name, monkeypatch, tmp_path):
    """The check libass will not do for you.

    A `Style:` line with 22 fields parses; libass defaults the rest and renders. This asserts
    the counts directly, so a dropped comma fails here rather than showing up as a caption in
    the wrong colour. Runs independently of the golden, so it also guards a *deliberate*
    re-freeze — the one moment the golden itself stops being a check.
    """
    document = build_case(name, monkeypatch, tmp_path)
    for line in document.splitlines():
        if line.startswith("Style: "):
            fields = line[len("Style: "):].split(",")
            assert len(fields) == STYLE_FIELDS, (
                f"{name}: Style line has {len(fields)} fields, "
                f"the Format: line declares {STYLE_FIELDS}\n  {line}"
            )
        elif line.startswith("Dialogue: "):
            # Exactly nine commas before the text, which may itself contain commas (a
            # `\move(x,y,x,y,t)` tag has four), so this splits with a bound rather than fully.
            fields = line[len("Dialogue: "):].split(",", EVENT_FIELDS - 1)
            assert len(fields) == EVENT_FIELDS, (
                f"{name}: Dialogue line has {len(fields)} fields, "
                f"the Format: line declares {EVENT_FIELDS}\n  {line}"
            )


@pytest.mark.parametrize("name", CONFIGURATIONS)
def test_every_dialogue_names_a_declared_style(name, monkeypatch, tmp_path):
    """An event naming an undeclared style renders in libass' built-in default, silently."""
    document = build_case(name, monkeypatch, tmp_path)
    declared = {
        line[len("Style: "):].split(",")[0]
        for line in document.splitlines() if line.startswith("Style: ")
    }
    for line in document.splitlines():
        if line.startswith("Dialogue: "):
            style = line[len("Dialogue: "):].split(",")[3]
            assert style in declared, (
                f"{name}: event names style {style!r}, which is not in {sorted(declared)}"
            )


@pytest.mark.parametrize("name", CONFIGURATIONS)
def test_every_override_block_is_balanced(name, monkeypatch, tmp_path):
    """An unbalanced `{` swallows the rest of the line's text into a tag block."""
    document = build_case(name, monkeypatch, tmp_path)
    for line in document.splitlines():
        if not line.startswith("Dialogue: "):
            continue
        text = line[len("Dialogue: "):].split(",", EVENT_FIELDS - 1)[-1]
        depth = 0
        for char in text:
            if char == "{":
                depth += 1
                assert depth == 1, f"{name}: nested override block\n  {line}"
            elif char == "}":
                depth -= 1
                assert depth >= 0, f"{name}: closing brace with no opener\n  {line}"
        assert depth == 0, f"{name}: unbalanced override block\n  {line}"


def test_the_golden_file_covers_every_configuration(golden):
    """So a case cannot be added to the matrix and silently never checked."""
    missing = sorted(set(CONFIGURATIONS) - set(golden))
    assert not missing, (
        f"{missing} have no frozen document. Generate with "
        "`python scripts/freeze_ass_styles.py`"
    )
    stale = sorted(set(golden) - set(CONFIGURATIONS))
    assert not stale, (
        f"{stale} are frozen but no longer in the matrix; remove them so the file describes "
        "what is actually checked"
    )
