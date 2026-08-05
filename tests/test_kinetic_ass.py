"""Kinetic typography — the pure ASS emitter's properties (tasks 8.6-8.11).

Covers **Property 6** (well-formed documents), **Property 7** (visible text
preserves every word in order — including the tightened layout clauses task 5.4
deferred here), **Property 8** (shared styles reproduce ``build_word_span``
byte-for-byte), **Property 9** (Reveal_Mode is orthogonal to Kinetic_Style) and
**Property 5** (the hook title survives engine ownership), plus the two worked
ASS event strings from the design asserted literally and the ``cue_level``
collapse (task 8.11).

Everything here drives the **real** pipeline — ``plan_kinetic`` then
``emit_ass`` — with no mocks and no fake documents; the only injected
collaborator is the ``keyword_planner`` stub the worked example needs to make one
specific word emphasised.

Task 5.4's deferred tightening (Property 14)
--------------------------------------------
``tests/test_kinetic_layout.py`` asserts Property 14 one layer down, on
``pack_lines`` output, because ``emit_ass`` did not exist when it was written,
and its docstring names task 8 as the place to re-run those clauses over real
``Default`` events. That is done here, inside the Property 7 test, where the
emitted text is already being reconstructed word by word: the ``\\N`` break
count, the per-Text_Line Display_Width bound with its single-over-long-word
exemption, and "no word's escaped text is split across a ``\\N``" are asserted on
the emitted event text, with ``captions._escape`` and inline emoji handled the
way only the emitter can.

Two documented domain restrictions
----------------------------------
1. **Hook text excludes ASCII control characters** (Property 5). ``emit_ass``
   builds the hook event exactly as ``captions.build_ass`` does today
   (Req 3.3) — ``f"Dialogue: 1,...,{{\\fad(250,350)}}{_escape(hook.strip().upper())}"``
   — and ``captions._escape`` does not neutralise ``\\n`` / ``\\r``. A hook title
   containing a newline therefore splits into two physical lines in *both* the
   v0.8.0 caption path and this engine. Changing that is a spec decision about
   ``_escape`` (and would break the byte-for-byte parity Req 3.3 demands), not
   something this test may quietly assume away, so the generator stays inside the
   printable domain and the shared spelling is asserted instead.
2. **Cue survival is not universal** (Property 7). ``plan_kinetic`` runs its cues
   through ``normalize_segments(..., min_duration=MIN_WORD_S)``, so a cue whose
   snapped span is shorter than ``MIN_WORD_S`` is dropped **with its words** —
   which Req 5.5 requires. Property 7's "every non-whitespace word" clause is
   therefore asserted as an order-preserving containment against the drawn
   timeline (nothing reordered, nothing split, no whitespace-only word emitted)
   plus *exact* equality on the fixed reference timeline every example also
   plans, where no cue can be dropped. The emitter-level clause — the emitted
   text equals the reconstruction from the plan's own words, byte for byte — is
   exact on every example.

The Property 9 finding (slide_up double-gates under word_by_word)
-----------------------------------------------------------------
``slide_up``'s per-word span **is** the ``typewriter`` alpha gate, and the
emitter's gate exclusion is literal (``style != "typewriter"``), so
``slide_up`` + ``word_by_word`` emits **two** ``\\alpha`` overrides for one word::

    cumulative  : {\\move(540,1740,540,1700,0,120)}{\\alpha&HFF&\\t(0,30,\\alpha&H00&)}THIS ...
    word_by_word: {\\move(540,1740,540,1700,0,120)}{\\alpha&HFF&\\t(0,1,\\alpha&H00&)}{\\alpha&HFF&\\t(0,30,\\alpha&H00&)}THIS ...

Property 9 as written *does* hold for this: the only difference between the two
documents is the presence of the per-word gate token, which is exactly what the
property claims — and the test below proves it by deleting the gate tokens from
the ``word_by_word`` document and asserting byte equality with the ``cumulative``
one. What the literal exclusion additionally produces is a **redundant** gate for
``slide_up`` (the wider ``+30`` ramp is emitted after it and wins, so the word
still appears on its own beat). The test pins that redundancy explicitly rather
than hiding it: ``typewriter``'s two Reveal_Modes are byte-identical,
``slide_up``'s are not, and ``slide_up`` carries two alpha overrides per word
under ``word_by_word``. If the spec decides ``slide_up`` should also be excluded
from the gate — making *its* two Reveal_Modes byte-identical too — that pin is
the one line that fails, which is the point.
"""
from __future__ import annotations

import dataclasses
import re
import subprocess
import unicodedata

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import FFMPEG, FakeWord, probe_size, requires_ffmpeg
from tests.strategies import (
    st_i18n_word_timeline,
    st_kinetic_options,
    st_kinetic_style,
    st_reveal_mode,
    st_word_timeline,
)
from worker import captions
from worker.effects import caption_presets
from worker.engines import kinetic
from worker.engines.kinetic import (
    KINETIC_STYLES,
    POSITIONS,
    REVEAL_MODES,
    SLIDE_UP_PX,
    Kinetic_Options,
    display_width,
    emit_ass,
    is_space_free,
    join_separator,
    join_width,
)
from worker.engines.timebase import Time_Base

#: A fixed, fully-timed reference cue: three Latin words at 30 fps inside a 3 s
#: clip. Every draw also plans this timeline, so no property can be satisfied
#: vacuously by a degenerate draw whose cues were all dropped by normalisation.
REFERENCE_WORDS = [
    FakeWord(1.0, 1.3, "THIS"),
    FakeWord(1.3, 1.8, "CHANGED"),
    FakeWord(1.8, 2.2, "EVERYTHING"),
]
REFERENCE_DURATION = 3.0

#: The frame grid used throughout: the project's default.
TIME_BASE = Time_Base(fps=30.0)

#: The font family handed to ``plan_kinetic``, which takes an already-resolved family (the
#: planner is pure and probes nothing).
#:
#: This was the literal ``"Arial"``, which worked only by accident: Arial was also
#: ``captions._FALLBACK_FONT``, so ``_preset_header_styles`` "substituted" it for itself and
#: the two Hook style lines matched whatever the host had installed. C1 replaced that
#: self-referential fallback with a ladder of real faces, so a request for Arial now
#: resolves to a *different* family and the comparison in Property 5 would be comparing two
#: different fonts.
#:
#: Resolving through the same ladder the caption path uses keeps this host-independent:
#: whatever comes back is available here, so ``_preset_header_styles`` substitutes nothing
#: and the property stays a statement about the *shape* of the style line.
RESOLVED_FONT = captions.resolve_font("Arial")[0]

#: One ASS override block, e.g. ``{\fscx60\fscy60\t(0,120,\fscx100\fscy100)}``.
_TAG_BLOCK = re.compile(r"\{[^{}]*\}")

#: The per-word Reveal_Mode alpha gate: its ramp is always exactly one
#: millisecond wide, which is what distinguishes it from the ``typewriter`` /
#: ``slide_up`` style span (always ``+30``).
_ALPHA_BLOCK = re.compile(r"\{\\alpha&HFF&\\t\((\d+),(\d+),\\alpha&H00&\)\}")

#: Unicode directional controls the emitter must never insert (Req 4.11).
_BIDI_CONTROLS = (
    "\u200e\u200f\u061c\u202a\u202b\u202c\u202d\u202e"
    "\u2066\u2067\u2068\u2069"
)


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #
def _plan(words, duration, *, style, reveal, option_data=None, hook_text="",
          keyword_planner=None, **overrides):
    """Plan ``words`` with the drawn options, forcing ``style`` / ``reveal``."""
    data = dict(option_data or {})
    data.update(overrides)
    data["style"] = style
    data["reveal"] = reveal
    opts = Kinetic_Options.parse(data)
    plan = kinetic.plan_kinetic(
        words,
        duration,
        TIME_BASE,
        opts,
        RESOLVED_FONT,
        hook_text,
        keyword_planner=keyword_planner,
    )
    return opts, plan


def _declared_styles(document):
    """The style names the ``[V4+ Styles]`` section declares."""
    return [
        line[len("Style: "):].split(",")[0]
        for line in document.splitlines()
        if line.startswith("Style: ")
    ]


def _events(document):
    """Parse ``Dialogue:`` lines into ``(fields, text)`` pairs.

    The ``Text`` field is the tenth and last, and may itself contain commas, so
    the split is bounded at nine — exactly what the ``Format:`` line declares.
    """
    out = []
    for line in document.splitlines():
        if not line.startswith("Dialogue: "):
            continue
        parts = line[len("Dialogue: "):].split(",", 9)
        out.append((parts[:9], parts[9] if len(parts) > 9 else None))
    return out


def _default_texts(document):
    """The ``Text`` field of every ``Default``-styled event, in order."""
    return [text for fields, text in _events(document) if fields[3] == "Default"]


def _strip_tags(text):
    """Remove every ASS override block, leaving the visible text."""
    return _TAG_BLOCK.sub("", text)


def _strip_gates(text):
    """Remove only the per-word Reveal_Mode alpha gate blocks (ramp width 1 ms)."""
    return _ALPHA_BLOCK.sub(
        lambda m: "" if int(m.group(2)) == int(m.group(1)) + 1 else m.group(0), text
    )


def _plan_lines(cue):
    """The cue's Text_Lines as lists of :class:`Kinetic_Word`, from the plan."""
    groups = []
    used = set()
    for line in cue.lines:
        indices = [i for i in line if 0 <= i < len(cue.words) and i not in used]
        used.update(indices)
        if indices:
            groups.append([cue.words[i] for i in indices])
    missing = [cue.words[i] for i in range(len(cue.words)) if i not in used]
    if missing:
        if groups:
            groups[-1].extend(missing)
        else:
            groups.append(missing)
    return groups


def _visible_line(words, *, with_emoji=True):
    """The visible text of one Text_Line: word texts, joins, and inline glyphs."""
    parts = []
    for position, word in enumerate(words):
        if position:
            parts.append(join_separator(words[position - 1].text, word.text))
        parts.append(word.text)
        if with_emoji and word.emoji:
            parts.append(f" {word.emoji}")
    return "".join(parts)


def _line_width(words):
    """Display_Width of a packed Text_Line, joins included (word texts only)."""
    if not words:
        return 0
    total = display_width(words[0].text)
    for previous, following in zip(words, words[1:]):
        total += join_width(previous.text, following.text) + display_width(
            following.text
        )
    return total


def _is_ordered_containment(needles, haystack):
    """True when ``needles`` appears inside ``haystack`` in order (a subsequence)."""
    cursor = 0
    for needle in needles:
        try:
            cursor = haystack.index(needle, cursor) + 1
        except ValueError:
            return False
    return True


# --------------------------------------------------------------------------- #
# Property 6 (task 8.6)                                                         #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 6: Every emitted ASS document is well-formed —
# *For every* Kinetic_Style, *every* Reveal_Mode, and *every* non-empty Word_Timeline,
# each `Dialogue:` line has balanced `{`/`}` override braces, names a style declared in
# the `[V4+ Styles]` section (`Default` or `Hook`), has the 9 comma-separated fields the
# `Format:` line declares before its text, and the document parses with the header fields
# `PlayResX`, `PlayResY`, and `WrapStyle: 2` present.
@settings(max_examples=100, deadline=None)
@given(
    timeline=st_word_timeline(),
    style=st_kinetic_style(),
    reveal=st_reveal_mode(),
    option_data=st_kinetic_options(),
)
def test_p6_every_emitted_ass_document_is_well_formed(
    timeline, style, reveal, option_data
):
    """Validates: Requirements 4.10, 7.1, 7.5, 8.8"""
    words, duration = timeline

    for source_words, source_duration in (
        (words, duration),
        (REFERENCE_WORDS, REFERENCE_DURATION),   # guarantees events exist
    ):
        _opts, plan = _plan(
            source_words,
            source_duration,
            style=style,
            reveal=reveal,
            option_data=option_data,
            hook_text="watch this",             # exercises the Hook style too
        )
        document = emit_ass(plan)

        # --- header (Reqs 7.1, 7.5) -----------------------------------------
        header = document.split("\n")
        assert header[0] == "[Script Info]"
        assert "ScriptType: v4.00+" in header
        assert f"PlayResX: {plan.play_res_x}" in header
        assert f"PlayResY: {plan.play_res_y}" in header
        assert "WrapStyle: 2" in header
        assert "ScaledBorderAndShadow: yes" in header
        assert "[V4+ Styles]" in header
        assert "[Events]" in header
        assert any(line.startswith("Format: Name, Fontname,") for line in header)
        assert any(line.startswith("Format: Layer, Start, End,") for line in header)

        # --- exactly one trailing newline (Req 8.8) --------------------------
        assert document.endswith("\n")
        assert not document.endswith("\n\n")

        # --- every event: 9 fields, a declared style, balanced braces --------
        declared = _declared_styles(document)
        assert "Default" in declared and "Hook" in declared
        events = _events(document)
        assert events                                  # non-vacuous
        for fields, text in events:
            assert len(fields) == 9                    # Layer..Effect (Req 4.10)
            assert text is not None
            assert fields[0] in ("0", "1")             # Layer
            assert fields[3] in declared               # Style (Req 4.10)
            assert fields[4] == ""                     # Name
            assert fields[5:9] == ["0", "0", "0", ""]  # margins + Effect

            depth = 0
            for char in text:
                if char == "{":
                    depth += 1
                    assert depth == 1, "override blocks are never nested"
                elif char == "}":
                    depth -= 1
                    assert depth == 0
            assert depth == 0, "every { is closed in the same event"
            assert text.count("{") == text.count("}")


# --------------------------------------------------------------------------- #
# Property 7 (task 8.7) — includes task 5.4's deferred Property 14 tightening    #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 7: Visible text preserves every word in order —
# *For every* Word_Timeline — including wide-script, right-to-left, combining-mark, emoji,
# and over-long tokens — *every* Kinetic_Style, and *every* Reveal_Mode, stripping all
# ASS_Override_Tags, `\N` breaks, and inline emoji glyphs from the `Default` events yields
# a sequence containing every non-whitespace word's `_escape`-d text, in Word_Timeline
# order, with no directional override characters inserted.
@settings(max_examples=100, deadline=None)
@given(
    timeline=st.one_of(st_word_timeline(), st_i18n_word_timeline()),
    style=st_kinetic_style(),
    reveal=st_reveal_mode(),
    option_data=st_kinetic_options(),
)
def test_p7_visible_text_preserves_every_word_in_order(
    timeline, style, reveal, option_data
):
    """Validates: Requirements 4.7, 4.11, 6.6, 8.3, 8.9

    Also re-asserts Property 14's layout clauses on real ``Default`` events, as
    ``tests/test_kinetic_layout.py`` defers them to task 8: at most
    ``max_lines - 1`` literal ``\\N`` breaks, each Text_Line's Display_Width at
    most ``max_line_width`` unless it holds one word, and no word's escaped text
    split across a break.
    """
    words, duration = timeline

    for source_words, source_duration, exact in (
        (words, duration, False),
        (REFERENCE_WORDS, REFERENCE_DURATION, True),
    ):
        opts, plan = _plan(
            source_words,
            source_duration,
            style=style,
            reveal=reveal,
            option_data=option_data,
        )
        document = emit_ass(plan)
        texts = _default_texts(document)
        assert len(texts) == len(plan.cues)

        emitted_words = []
        for cue, text in zip(plan.cues, texts):
            visible = _strip_tags(text)
            groups = _plan_lines(cue)

            # --- the emitter inserts nothing of its own -----------------------
            # Byte equality against the reconstruction from the plan's own words
            # proves the order, the joins, the `\N` placement and the inline
            # glyph placement, and rules out any inserted directional override.
            assert visible == "\\N".join(
                _visible_line(group) for group in groups
            )

            rendered_lines = visible.split("\\N")
            assert len(rendered_lines) == len(groups)

            # --- Property 14, now on a real Default event --------------------
            assert visible.count("\\N") <= max(opts.max_lines - 1, 0)
            assert len(groups) <= opts.max_lines
            for group, rendered in zip(groups, rendered_lines):
                width = _line_width(group)
                if width > opts.max_line_width:
                    assert len(group) == 1        # over-long token, never split
                assert "\\N" not in rendered

                # every word intact, in order, inside this single Text_Line
                cursor = 0
                for word in group:
                    found = rendered.find(word.text, cursor)
                    assert found >= cursor, "a word was split across a break"
                    cursor = found + len(word.text)

                # the join rule (Reqs 8.2, 8.4)
                for previous, following in zip(group, group[1:]):
                    separator = join_separator(previous.text, following.text)
                    if is_space_free(previous.text) and is_space_free(following.text):
                        assert separator == ""
                    else:
                        assert separator == " "

                # stripping the inline glyphs leaves exactly the joined words
                assert _visible_line(group, with_emoji=False) == "".join(
                    (
                        join_separator(group[i - 1].text, word.text) if i else ""
                    ) + word.text
                    for i, word in enumerate(group)
                )

                emitted_words.extend(word.text for word in group)

            # --- no directional override characters inserted (Req 4.11) ------
            for control in _BIDI_CONTROLS:
                assert visible.count(control) == sum(
                    word.text.count(control) + word.emoji.count(control)
                    for word in cue.words
                )

        # --- every non-whitespace word, escaped, in Word_Timeline order -------
        expected = [
            captions._escape(word.text.strip())
            for word in source_words
            if word.text.strip()
        ]
        assert emitted_words == [word.text for cue in plan.cues for word in cue.words]
        for emitted in emitted_words:
            assert emitted.strip() == emitted != ""     # Req 6.6 — no blank word
            assert captions._escape(emitted) == emitted  # Req 4.7 — already escaped
        if exact:
            assert emitted_words == expected
        else:
            # Req 5.5 lets normalisation drop a sub-MIN_WORD_S cue with its
            # words; nothing may be reordered, duplicated or split.
            assert _is_ordered_containment(emitted_words, expected)


# --------------------------------------------------------------------------- #
# Property 8 (task 8.8)                                                         #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 8: Shared styles reproduce `build_word_span`
# semantics — *For all* words and Base_Presets, for each Kinetic_Style in `{none, pop,
# typewriter, karaoke_fill}`, the span the emitter produces for a non-emphasised word
# under `reveal="cumulative"` is byte-identical to `captions.build_word_span(word,
# replace(preset, animation=style), False, cue_start=cue.start)`; and for `bounce` the
# span contains two `\t` stages whose final scale is `100`, for `slide_up` the event
# carries a `\move` ending at the resolved caption position, and for `highlight_sweep` the
# span transitions `colors.highlight` -> `colors.primary`.
#
# What this test now proves, and what it no longer has to
# -------------------------------------------------------
# The four shared spans used to be spelled out *twice* — once in `captions.build_word_span`
# and once in `kinetic._style_span` — and this test was the only thing keeping the two
# copies equal. They are now one function (`ass_style.animation_span`), so the span *shape*
# can no longer drift and that clause is close to a tautology.
#
# The test is kept, and is still worth its runtime, because it never only checked the span
# shape. It compares the emitter's whole **event text** against a reconstruction built from
# `build_word_span`, so it still holds the parts the shared function does not cover: that
# both paths escape the same way, derive `rel_ms` against the *cue* start identically,
# order words the same, and join them with the same separators and `\N` breaks. Those are
# four independent implementations either side of one shared span, and they can still
# disagree.
@settings(max_examples=100, deadline=None)
@given(
    timeline=st_word_timeline(),
    style=st_kinetic_style(),
    option_data=st_kinetic_options(),
)
def test_p8_shared_styles_reproduce_build_word_span_semantics(
    timeline, style, option_data
):
    """Validates: Requirements 4.2, 4.3, 4.4, 4.5, 4.6"""
    words, duration = timeline

    for source_words, source_duration in (
        (words, duration),
        (REFERENCE_WORDS, REFERENCE_DURATION),
    ):
        # The property is stated for a *non-emphasised* word under `cumulative`,
        # so emphasis and inline emoji are off: what remains is the style span.
        opts, plan = _plan(
            source_words,
            source_duration,
            style=style,
            reveal="cumulative",
            option_data=option_data,
            highlight_keywords=False,
            emoji_inline=False,
        )
        preset, _substituted = caption_presets.resolve_preset(opts.preset_name)
        shared = dataclasses.replace(preset, animation=style)
        motion = opts.motion_duration_ms
        primary = plan.colors["primary"]
        highlight = plan.colors["highlight"]

        for cue, text in zip(plan.cues, _default_texts(emit_ass(plan))):
            assert all(not word.emphasis for word in cue.words)

            if style in ("none", "pop", "typewriter", "karaoke_fill"):
                # Byte-identity with the v0.8.0 span builder, at event level: the
                # event is exactly those spans, joined per Req 8.4 (Req 4.3).
                assert text == "\\N".join(
                    "".join(
                        (
                            join_separator(group[i - 1].text, word.text) if i else ""
                        )
                        + captions.build_word_span(
                            word, shared, False, cue_start=cue.start
                        )
                        for i, word in enumerate(group)
                    )
                    for group in _plan_lines(cue)
                )
                continue

            for word in cue.words:
                rel = word.rel_ms
                if style == "bounce":
                    # two `\t` stages, overshoot then settle at scale 100 (Req 4.4)
                    assert (
                        f"\\t({rel},{rel + motion // 2},\\fscx118\\fscy118)"
                        f"\\t({rel + motion // 2},{rel + motion},"
                        f"\\fscx100\\fscy100)"
                    ) in text
                elif style == "highlight_sweep":
                    # colors.highlight -> colors.primary over d ms (Req 4.6)
                    assert (
                        f"{{\\c{highlight}&\\t({rel},{rel + motion},"
                        f"\\c{primary}&)}}{word.text}"
                    ) in text
                else:                                      # slide_up (Req 4.5)
                    # the per-word alpha gate keeps words appearing on beat
                    assert (
                        f"{{\\alpha&HFF&\\t({rel},{rel + 30},\\alpha&H00&)}}"
                        f"{word.text}"
                    ) in text

            if style == "slide_up":
                # the event-level `\move` ends at the resolved caption position,
                # SLIDE_UP_PX below which it starts (Reqs 4.5, 7.2, 7.3)
                anchor_x = plan.play_res_x // 2
                anchor_y = {
                    2: plan.play_res_y - plan.margin_v,
                    5: plan.play_res_y // 2,
                    8: plan.margin_v,
                }[plan.align]
                assert text.startswith(
                    f"{{\\move({anchor_x},{anchor_y + SLIDE_UP_PX},"
                    f"{anchor_x},{anchor_y},0,{motion})}}"
                )
            else:
                assert "\\move(" not in text


# --------------------------------------------------------------------------- #
# Property 9 (task 8.9)                                                         #
# --------------------------------------------------------------------------- #
# Feature: kinetic-typography, Property 9: Reveal_Mode is orthogonal to Kinetic_Style —
# *For every* pair of Kinetic_Style and Reveal_Mode and *every* Word_Timeline, the emitted
# document's tag-stripped text and its cue count are identical across both Reveal_Modes
# for the same style, and switching Reveal_Mode changes only the presence of the per-word
# `\alpha` gate.
@settings(max_examples=100, deadline=None)
@given(
    timeline=st_word_timeline(),
    style=st_kinetic_style(),
    option_data=st_kinetic_options(),
)
def test_p9_reveal_mode_is_orthogonal_to_kinetic_style(timeline, style, option_data):
    """Validates: Requirements 4.9

    The third clause ("switching Reveal_Mode changes only the presence of the
    per-word ``\\alpha`` gate") is asserted by *deleting* the gate tokens from the
    ``word_by_word`` document and requiring byte equality with the ``cumulative``
    one — the gate is unambiguously identifiable because its ramp is always one
    millisecond wide, while the ``typewriter`` / ``slide_up`` style span's is
    always ``+30``.

    See the module docstring: this holds for ``slide_up`` even though ``slide_up``
    then carries **two** ``\\alpha`` overrides per word, which is pinned below.
    """
    words, duration = timeline

    for source_words, source_duration in (
        (words, duration),
        (REFERENCE_WORDS, REFERENCE_DURATION),
    ):
        documents = {}
        plans = {}
        for reveal in REVEAL_MODES:
            _opts, plan = _plan(
                source_words,
                source_duration,
                style=style,
                reveal=reveal,
                option_data=option_data,
            )
            plans[reveal] = plan
            documents[reveal] = emit_ass(plan)

        cumulative = documents["cumulative"]
        word_by_word = documents["word_by_word"]

        # --- cue count and visible text are identical ------------------------
        assert len(plans["cumulative"].cues) == len(plans["word_by_word"].cues)
        assert len(_default_texts(cumulative)) == len(_default_texts(word_by_word))
        assert [_strip_tags(t) for t in _default_texts(cumulative)] == [
            _strip_tags(t) for t in _default_texts(word_by_word)
        ]

        # --- the gate is the *only* difference -------------------------------
        assert _strip_gates(word_by_word) == cumulative
        assert _strip_gates(cumulative) == cumulative        # never gated

        # --- the gate is present exactly once per word, except typewriter ----
        word_count = sum(len(cue.words) for cue in plans["cumulative"].cues)
        gates = sum(
            1
            for text in _default_texts(word_by_word)
            for match in _ALPHA_BLOCK.finditer(text)
            if int(match.group(2)) == int(match.group(1)) + 1
        )
        if style == "typewriter":
            # its own tag set already *is* the gate, so it is excluded and the
            # two Reveal_Modes come out byte-identical
            assert gates == 0
            assert word_by_word == cumulative
        else:
            assert gates == word_count
            if word_count:
                assert word_by_word != cumulative

        # --- pinned: slide_up double-gates under word_by_word ---------------
        # `slide_up`'s per-word span IS the typewriter alpha gate, and the
        # exclusion is literal, so one word carries two \alpha overrides. This is
        # the redundancy the module docstring reports; excluding `slide_up` from
        # the gate as well is a spec decision, and this assertion is what would
        # fail if it were taken.
        if style == "slide_up" and word_count:
            for text in _default_texts(word_by_word):
                assert text.count("\\alpha&HFF&") == 2 * len(
                    _ALPHA_BLOCK.findall(_strip_gates(text))
                )
            for text in _default_texts(cumulative):
                assert text.count("\\alpha&HFF&") == len(_ALPHA_BLOCK.findall(text))


# --------------------------------------------------------------------------- #
# Property 5 (task 8.10)                                                        #
# --------------------------------------------------------------------------- #
#: Hook titles without ASCII control characters — see the module docstring for
#: why ``\n`` / ``\r`` are out of domain (inherited ``build_ass`` behaviour).
_ST_HOOK_TEXT = st.text(max_size=40).filter(
    lambda value: not any(unicodedata.category(c) == "Cc" for c in value)
)


# Feature: kinetic-typography, Property 5: The hook title survives engine ownership —
# *For all* non-empty hook texts and Word_Timelines, when the engine applies with
# `hook_enabled`, the emitted ASS declares a `Style: Hook` line identical in shape to
# `captions.build_ass`'s, contains exactly one event whose style is `Hook`, and that
# event's tag-stripped text equals the escaped upper-cased hook text.
@settings(max_examples=100, deadline=None)
@given(
    timeline=st_word_timeline(),
    option_data=st_kinetic_options(),
    style=st_kinetic_style(),
    hook_text=_ST_HOOK_TEXT,
)
def test_p5_the_hook_title_survives_engine_ownership(
    timeline, option_data, style, hook_text
):
    """Validates: Requirements 3.3, 3.7

    Deliberately **planner-level**: the hook text is driven straight into
    ``plan_kinetic(..., hook_text=…)``, so no ``Engine_Context`` is built here and
    this module hands the engine no per-clip mapping at all. That is a choice, not
    an oversight — Property 5 is about what the *emitter* does with a hook text,
    while *where* the hook text comes from (``ctx.clip_metadata["hook_text"]``, the
    channel the Pipeline publishes at the COMPOSE hook — never ``ctx.deps``) is the
    engine seam covered by ``tests/test_kinetic_engine.py`` and, end to end, by the
    foundation's real-Pipeline Clip_Metadata test (task 12.4).
    """
    words, duration = timeline
    opts, plan = _plan(
        words,
        duration,
        style=style,
        reveal="cumulative",
        option_data=option_data,
        hook_text=hook_text,
    )
    document = emit_ass(plan)
    hook_events = [
        (fields, text) for fields, text in _events(document) if fields[3] == "Hook"
    ]

    # --- the Style: Hook line is byte-identical to build_ass's (Req 3.3) -----
    # ``_preset_header_styles`` owns that literal inside the v0.8.0 caption path; the
    # plan's font is RESOLVED_FONT, which by construction is available on this host, so
    # ``_preset_header_styles`` substitutes nothing and the comparison is about the shape
    # of the style line rather than about font availability.
    preset, _substituted = caption_presets.resolve_preset(opts.preset_name)
    _default_style, expected_hook_style = captions._preset_header_styles(
        dataclasses.replace(preset, font=RESOLVED_FONT), None, opts.hook_font_size, None
    )
    assert expected_hook_style in document.splitlines()
    assert plan.hook_style == expected_hook_style

    if not hook_text.strip():
        assert hook_events == []
        return

    # --- exactly one Hook event, carrying the escaped upper-cased hook -------
    assert len(hook_events) == 1
    fields, text = hook_events[0]
    assert fields[0] == "1"                              # layer above the cues
    assert fields[1] == captions._ass_timestamp(0.0)
    assert fields[2] == captions._ass_timestamp(max(0.5, opts.hook_duration_s))
    assert text.startswith("{\\fad(250,350)}")
    assert _strip_tags(text) == captions._escape(hook_text.strip().upper())

    # the whole event line is the spelling build_ass emits today
    assert (
        f"Dialogue: 1,{fields[1]},{fields[2]},Hook,,0,0,0,,"
        f"{{\\fad(250,350)}}{captions._escape(hook_text.strip().upper())}"
    ) in document.splitlines()

    # the hook is the *first* event, so it is never hidden behind a cue
    assert _events(document)[0][0][3] == "Hook"


# --------------------------------------------------------------------------- #
# Task 8.11 — the design's two worked ASS examples, asserted literally           #
# --------------------------------------------------------------------------- #
#: The design's worked scenario: PlayResX/Y = 1080/1920, hormozi-like colours
#: (``primary=&H00FFFFFF``, ``highlight=&H0000E5FF``), ``position="bottom"``
#: (align 2), ``safe_area_x_pct=6`` / ``safe_area_y_pct=10`` => MarginL/R = 65 and
#: MarginV = max(220, 192) = 220, ``motion_duration_ms=120``, ``max_lines=2``,
#: ``max_line_width=22``, cue ``[1.00, 2.20)`` with THIS / CHANGED (emphasised) /
#: EVERYTHING => rel_ms 0 / 300 / 800.
_WORKED_BOUNCE_EVENT = (
    "Dialogue: 0,0:00:01.00,0:00:02.20,Default,,0,0,0,,"
    "{\\fscx55\\fscy55\\t(0,60,\\fscx118\\fscy118)\\t(60,120,\\fscx100\\fscy100)}THIS "
    "{\\c&H0000E5FF&\\fscx118\\fscy118}"
    "{\\fscx55\\fscy55\\t(300,360,\\fscx118\\fscy118)\\t(360,420,\\fscx100\\fscy100)}"
    "CHANGED{\\c&H00FFFFFF&\\fscx100\\fscy100}\\N"
    "{\\fscx55\\fscy55\\t(800,860,\\fscx118\\fscy118)\\t(860,920,\\fscx100\\fscy100)}"
    "EVERYTHING"
)

_WORKED_SWEEP_EVENT = (
    "Dialogue: 0,0:00:01.00,0:00:02.20,Default,,0,0,0,,"
    "{\\alpha&HFF&\\t(0,1,\\alpha&H00&)}"
    "{\\c&H0000E5FF&\\t(0,120,\\c&H00FFFFFF&)}THIS "
    "{\\c&H0000E5FF&\\fscx118\\fscy118}"
    "{\\alpha&HFF&\\t(300,301,\\alpha&H00&)}"
    "{\\c&H0000E5FF&\\t(300,420,\\c&H00FFFFFF&)}"
    "CHANGED{\\c&H00FFFFFF&\\fscx100\\fscy100}\\N"
    "{\\alpha&HFF&\\t(800,801,\\alpha&H00&)}"
    "{\\c&H0000E5FF&\\t(800,920,\\c&H00FFFFFF&)}"
    "EVERYTHING"
)

_WORKED_CUE_LEVEL_TEXT = "{\\fad(120,120)}THIS CHANGED\\NEVERYTHING"


def _worked_plan(style, reveal):
    """The design's worked scenario, planned end to end (CHANGED emphasised)."""
    opts = Kinetic_Options(
        style=style,
        reveal=reveal,
        preset_name="pop",              # default colours + highlight_scale 1.18
        position="bottom",
        max_lines=2,
        max_line_width=22,
        motion_duration_ms=120,
        safe_area_x_pct=6.0,
        safe_area_y_pct=10.0,
        highlight_keywords=True,
    )
    plan = kinetic.plan_kinetic(
        REFERENCE_WORDS,
        REFERENCE_DURATION,
        TIME_BASE,
        opts,
        "Impact",
        "",
        keyword_planner=lambda flat, use_ai=False, client=None: {1},
    )
    # The scenario's geometry, so a margin regression fails here too.
    assert (plan.play_res_x, plan.play_res_y) == (1080, 1920)
    assert (plan.align, plan.margin_l, plan.margin_r, plan.margin_v) == (2, 65, 65, 220)
    assert len(plan.cues) == 1
    assert [word.rel_ms for word in plan.cues[0].words] == [0, 300, 800]
    assert [word.emphasis for word in plan.cues[0].words] == [False, True, False]
    return plan


def test_worked_bounce_cumulative_event_is_emitted_verbatim():
    """The design's `bounce` / `cumulative` worked event string, byte for byte.

    Validates: Requirements 4.4, 7.2, 7.5, 7.6
    """
    events = _default_texts(emit_ass(_worked_plan("bounce", "cumulative")))
    assert len(events) == 1
    line = f"Dialogue: 0,0:00:01.00,0:00:02.20,Default,,0,0,0,,{events[0]}"
    assert line == _WORKED_BOUNCE_EVENT


def test_worked_highlight_sweep_word_by_word_event_is_emitted_verbatim():
    """The design's `highlight_sweep` / `word_by_word` worked event string.

    Validates: Requirements 4.6, 4.9, 7.5, 7.6
    """
    events = _default_texts(emit_ass(_worked_plan("highlight_sweep", "word_by_word")))
    assert len(events) == 1
    line = f"Dialogue: 0,0:00:01.00,0:00:02.20,Default,,0,0,0,,{events[0]}"
    assert line == _WORKED_SWEEP_EVENT


def test_cue_level_degradation_collapses_the_worked_cue():
    """`cue_level=True` drops every per-word tag for a single `\\fad` (Req 6.4).

    Validates: Requirements 6.4
    """
    for style in KINETIC_STYLES:
        for reveal in REVEAL_MODES:
            plan = dataclasses.replace(_worked_plan(style, reveal), cue_level=True)
            events = _default_texts(emit_ass(plan))
            assert events == [_WORKED_CUE_LEVEL_TEXT]
            # no per-word animation, no emphasis wrap, no event-level \move
            assert "\\t(" not in events[0]
            assert "\\move(" not in events[0]
            assert "\\fscx" not in events[0]



# --------------------------------------------------------------------------- #
# Task 16.1 — libass integration: every Kinetic_Style x position parses          #
# --------------------------------------------------------------------------- #
#: A fully-timed cue that fits inside a **1-second** clip, so the burn exercises
#: real on-screen events rather than a document whose cues sit past the end.
#: ``EVERYTHING`` starts after a 20 ms gap so ``words_to_cues`` still groups all
#: three into one cue, and its Display_Width forces the ``\N`` break.
_BURN_WORDS = [
    FakeWord(0.0, 0.30, "THIS"),
    FakeWord(0.30, 0.60, "CHANGED"),
    FakeWord(0.62, 0.95, "EVERYTHING"),
]
_BURN_DURATION = 1.0

#: The tiny frame every burn uses. The plan declares the *same* size in its
#: ``PlayResX``/``PlayResY`` header, so libass parses the Safe_Area margins that
#: task 5.3's small-frame clamp produces (``MarginV`` clamped to 119 here, not the
#: v0.8.0 default 220) rather than a 1080x1920 header libass would rescale.
_BURN_SIZE = (240, 240)

#: Substrings libass / ffmpeg use when a script (or a style/event line inside it)
#: cannot be parsed, plus the font-selection failure spellings. The scan is
#: restricted to lines carrying the ``subtitles`` filter's own log context, so a
#: message from another filter or from the encoder cannot produce a false match.
_LIBASS_PROBLEM_MARKERS = (
    "parse error",
    "error parsing",
    "unable to parse",
    "failed to parse",
    "syntax error",
    "malformed",
    "unrecognized",
    "unrecognised",
    "unknown",
    "invalid",
    "bad ",
    "failed",
    "error",
    "warning",
)


def _libass_lines(stderr):
    """The stderr lines emitted by the ``subtitles`` filter's own log context.

    libass routes every message through the filter instance, so its lines are
    prefixed ``[Parsed_subtitles_0 @ 0x...]``. Selecting on that prefix is what
    makes the scan below precise: nothing the demuxer, the encoder or another
    filter says can be mistaken for a libass complaint, and a document that
    libass never opened produces no lines at all (which the test rejects).
    """
    return [line for line in stderr.splitlines() if "Parsed_subtitles" in line]


@requires_ffmpeg
@pytest.mark.parametrize("position", list(POSITIONS))
@pytest.mark.parametrize("style", list(KINETIC_STYLES))
def test_every_kinetic_style_and_position_parses_under_libass(
    style, position, make_video, tmp_path
):
    """Burning the emitted ASS exits 0 with no libass parse error (task 16.1).

    Validates: Requirements 18.5, 18.6

    One example per (Kinetic_Style, position) combination — 7 x 3 = 21 burns, no
    property tests, because this verifies **libass and ffmpeg**, not this
    engine's logic (which the pure property tests above already pin).

    The Reveal_Mode is derived from the position index, so every style is burned
    under both Reveal_Modes across its three positions without adding a single
    extra ffmpeg invocation: ``bottom``/``top`` use ``cumulative`` and ``center``
    uses ``word_by_word``, whose per-word ``\\alpha`` gate is therefore parsed by
    libass for all seven styles.
    """
    reveal = REVEAL_MODES[POSITIONS.index(position) % len(REVEAL_MODES)]
    play_res_x, play_res_y = _BURN_SIZE

    opts = Kinetic_Options(
        style=style,
        reveal=reveal,
        position=position,
        preset_name="hormozi",        # box border style + a real highlight colour
        highlight_keywords=True,      # exercises the emphasis wrap
        emoji_inline=True,
        hook_enabled=True,
        max_lines=2,
        max_line_width=22,
        motion_duration_ms=120,
    )
    plan = kinetic.plan_kinetic(
        _BURN_WORDS,
        _BURN_DURATION,
        TIME_BASE,
        opts,
        RESOLVED_FONT,
        "watch this",                 # exercises the Hook style + event too
        keyword_planner=lambda flat, use_ai=False, client=None: {1},
        play_res_x=play_res_x,
        play_res_y=play_res_y,
    )

    # The document under test really is the style/position combination claimed,
    # and really does carry events — otherwise libass would parse nothing.
    assert plan.style == style
    assert plan.position == position
    assert plan.align == captions._POSITION_ALIGN[position][0]
    assert plan.cues
    document = emit_ass(plan)
    assert _default_texts(document)
    assert [fields[3] for fields, _text in _events(document)].count("Hook") == 1

    ass = tmp_path / f"{style}_{position}.ass"
    ass.write_text(document, encoding="utf-8")

    src = make_video(
        "burn_src.mp4",
        duration=_BURN_DURATION,
        w=play_res_x,
        h=play_res_y,
        audio=True,
    )
    out = tmp_path / f"{style}_{position}.mp4"
    proc = subprocess.run(
        [
            FFMPEG, "-y", "-i", str(src),
            "-vf", captions.subtitles_filter(ass),
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "copy",
            str(out),
        ],
        capture_output=True,
        text=True,
    )

    # --- the process exited 0 -------------------------------------------------
    assert proc.returncode == 0, (
        f"{style}/{position}/{reveal}: ffmpeg exited {proc.returncode}\n"
        f"{proc.stderr[-2000:]}"
    )

    # --- libass actually opened and parsed the document (non-vacuity) ---------
    libass_lines = _libass_lines(proc.stderr)
    assert libass_lines, "libass never logged: the subtitles filter did not run"
    assert any("libass API version" in line for line in libass_lines)
    assert any("Using font provider" in line for line in libass_lines)

    # --- and complained about nothing -----------------------------------------
    for line in libass_lines:
        lowered = line.lower()
        for problem in _LIBASS_PROBLEM_MARKERS:
            assert problem not in lowered, (
                f"{style}/{position}/{reveal}: libass reported a problem: {line}"
            )

    # --- the burn produced a real, correctly-sized clip ------------------------
    assert out.exists() and out.stat().st_size > 0
    assert probe_size(out) == _BURN_SIZE
