"""In-caption emoji are dropped when no font can draw them (Req 4.3).

**Found by looking at a rendered frame, not by a failing test.** The delivered clip read
`gone. the secret ▯` — a missing-glyph box burned into the video. Tracing it:

* `caption_emoji_glyph` has always accepted an injectable `glyph_available` callable, and its
  docstring has always promised that "a glyph the active font cannot render is dropped while
  surrounding words are retained";
* **no production caller ever passed one**, so the default was `lambda _g: True` — the guard
  asserted that every emoji renders;
* the `Dockerfile` installs `fonts-liberation` and the bundled display faces and **no emoji font at
  all**, so the shipped image had nothing to draw them with;
* `caption_emoji` defaults to `True`, so this shipped on every clip whose transcript hit a mapped
  keyword.

An optional dependency whose default disables the feature it guards is indistinguishable from not
having written it. That is the same shape as the five features this project found had no importer,
one layer down: the code existed, and nothing reached it.

**Per-glyph and not per-font, which the fixtures here demonstrate rather than assert.** On a host with
`google-noto-emoji-fonts` installed, U+1F4B0 (money bag) is present and U+1F92B (shushing face) is
absent *from the same font*. Installing an emoji font is therefore not a fix, and a font-level check
would still ship boxes.
"""

from __future__ import annotations

import pytest

from worker import captions as cap
from worker.effects.caption_presets import resolve_preset
from worker.transcribe import Word

#: A preset with `emoji_inline`, or none of this is reachable.
PRESET = next(
    (
        p
        for p in (resolve_preset(n)[0] for n in ("hormozi", "karaoke", "minimal"))
        if p.emoji_inline
    ),
    resolve_preset("hormozi")[0],
)


def _word(text: str) -> Word:
    return Word(start=0.0, end=0.4, text=text)


def test_the_preset_used_here_actually_has_inline_emoji():
    """Otherwise every assertion below passes for the wrong reason."""
    assert PRESET.emoji_inline, f"{PRESET.name} has no inline emoji; these tests prove nothing"


# --------------------------------------------------------------------------- #
# The probe                                                                   #
# --------------------------------------------------------------------------- #


def test_an_uncoverable_codepoint_is_reported_unavailable():
    """A private-use codepoint no font ships, so this is stable across machines."""
    assert cap.glyph_available("\U000f0000") is False


def test_a_plainly_available_character_is_reported_available():
    """The discriminator. Without it, a probe that always said False would pass above."""
    assert cap.glyph_available("A") is True


def test_an_empty_glyph_is_not_available():
    """Guards the `all(...)` over an empty sequence, which is vacuously True."""
    assert cap.glyph_available("") is False
    assert cap.glyph_available("\ufe0f") is False, (
        "a lone variation selector has no outline and must not report as drawable"
    )


def test_a_joined_sequence_ignores_the_joiner_and_the_variation_selector():
    """Demanding coverage of a ZWJ or a variation selector would reject every emoji sequence.

    They are instructions to the shaper, not characters with outlines, so no font advertises them in
    its charset and requiring them would make the guard reject everything.
    """
    seen: list[int] = []

    def spy(codepoint: int) -> bool:
        seen.append(codepoint)
        return True

    original = cap._codepoint_covered
    try:
        cap._codepoint_covered = spy  # type: ignore[assignment]
        assert cap.glyph_available("\U0001f468\u200d\U0001f4bb") is True
    finally:
        cap._codepoint_covered = original  # type: ignore[assignment]

    assert 0x200D not in seen, "the zero-width joiner was required to have a glyph"
    assert seen == [0x1F468, 0x1F4BB]


def test_every_codepoint_must_be_covered():
    """One uncovered member breaks the cluster, so the whole sequence must go."""
    original = cap._codepoint_covered
    try:
        cap._codepoint_covered = lambda cp: cp != 0x1F4BB  # type: ignore[assignment]
        assert cap.glyph_available("\U0001f468\u200d\U0001f4bb") is False
    finally:
        cap._codepoint_covered = original  # type: ignore[assignment]


def test_the_probe_is_conservative_when_fontconfig_is_absent(monkeypatch):
    """No fontconfig means no evidence, and dropping an emoji on no evidence is a silent edit.

    The failure being guarded against is shipping a box, not omitting an emoji, so the default when
    the question cannot be answered is to leave the caption alone.
    """
    cap._codepoint_covered.cache_clear()
    monkeypatch.setattr(cap.shutil, "which", lambda _name: None)

    assert cap._codepoint_covered(0x1F92B) is True
    cap._codepoint_covered.cache_clear()


def test_a_fontconfig_that_fails_is_also_treated_as_no_evidence(monkeypatch):
    cap._codepoint_covered.cache_clear()
    monkeypatch.setattr(cap.shutil, "which", lambda _name: "/usr/bin/fc-list")

    def boom(*_a, **_k):
        raise OSError("fc-list exploded")

    monkeypatch.setattr(cap.subprocess, "run", boom)

    assert cap._codepoint_covered(0x1F92B) is True
    cap._codepoint_covered.cache_clear()


# --------------------------------------------------------------------------- #
# The wiring: the default must be the real probe                              #
# --------------------------------------------------------------------------- #


def test_an_undrawable_emoji_is_dropped_with_no_checker_supplied():
    """**The regression this file exists for.**

    With no `glyph_available` argument the default used to be "assume it renders". Passing nothing is
    exactly what every production caller does, so this is the configuration that shipped the box.
    """
    monkeypatched = cap.glyph_available
    assert monkeypatched is not None

    original = cap.glyph_available
    try:
        cap.glyph_available = lambda _g: False  # type: ignore[assignment]
        glyph = cap.caption_emoji_glyph(_word("money"), PRESET)
    finally:
        cap.glyph_available = original  # type: ignore[assignment]

    assert glyph == "", "an emoji no font can draw was still emitted"


def test_a_drawable_emoji_is_kept_with_no_checker_supplied():
    """The other direction, so the fix is not simply "never emit emoji"."""
    original = cap.glyph_available
    try:
        cap.glyph_available = lambda _g: True  # type: ignore[assignment]
        glyph = cap.caption_emoji_glyph(_word("money"), PRESET)
    finally:
        cap.glyph_available = original  # type: ignore[assignment]

    assert glyph, "a drawable emoji was dropped"


def test_an_explicit_checker_still_wins():
    """The injectable stays injectable; the change is only to its default."""
    assert cap.caption_emoji_glyph(_word("money"), PRESET, glyph_available=lambda _g: False) == ""
    assert cap.caption_emoji_glyph(_word("money"), PRESET, glyph_available=lambda _g: True)


def test_a_word_with_no_mapped_emoji_is_untouched():
    assert cap.mapped_caption_emoji(_word("thermodynamics")) == ""
    assert cap.caption_emoji_glyph(_word("thermodynamics"), PRESET) == ""


# --------------------------------------------------------------------------- #
# The rendered file, and the marker                                           #
# --------------------------------------------------------------------------- #


def _render(tmp_path, *, available: bool, notes: list[str] | None = None):
    words = [_word("money"), _word("secret"), _word("plain")]
    cues = cap.words_to_cues(words)
    original = cap.glyph_available
    try:
        cap.glyph_available = lambda _g: available  # type: ignore[assignment]
        dest = cap.build_ass(
            cues,
            tmp_path / f"{available}.ass",
            preset=PRESET,
            clip_duration=3.0,
            notes=notes,
        )
    finally:
        cap.glyph_available = original  # type: ignore[assignment]
    return dest.read_text(encoding="utf-8")


def test_no_undrawable_emoji_reaches_the_rendered_subtitle_file(tmp_path):
    """Asserted on the file libass is handed, which is where the box came from.

    Checked by codepoint rather than by searching for particular emoji: any character outside the BMP
    in this file is an emoji, and pinning the specific ones would let a newly-mapped keyword slip
    through the same hole.
    """
    text = _render(tmp_path, available=False)
    astral = [ch for ch in text if ord(ch) > 0xFFFF]

    assert astral == [], (
        f"undrawable emoji reached the subtitle file: {[hex(ord(c)) for c in astral]}"
    )


def test_the_drawable_case_does_reach_the_file(tmp_path):
    """The discriminator for the test above."""
    text = _render(tmp_path, available=True)

    assert [ch for ch in text if ord(ch) > 0xFFFF], (
        "no emoji at all was emitted even though every glyph was reported available, so the test "
        "above passes for the wrong reason"
    )


def test_the_surrounding_words_survive_the_drop(tmp_path):
    """Req 4.3 drops the *glyph*, not the word it was attached to."""
    # Compared case-insensitively: this preset sets `uppercase`, so the words are emitted as
    # MONEY/SECRET/PLAIN. Asserting the lower-case forms would fail for a reason that has nothing to
    # do with emoji.
    text = _render(tmp_path, available=False).lower()

    for word in ("money", "secret", "plain"):
        assert word in text, f"{word!r} was lost along with its emoji"


def test_a_dropped_emoji_is_recorded_on_the_clip(tmp_path):
    """Silently omitting it looks identical to the keyword map not covering the word.

    Only one of those is actionable — installing a font fixes the first and nothing fixes the second —
    so the clip record has to distinguish them.
    """
    notes: list[str] = []
    _render(tmp_path, available=False, notes=notes)

    assert any(n.startswith("caption_emoji_unavailable:") for n in notes), notes
    # The count is asserted exactly, and the fixture carries TWO mapped words ("money", "secret") so
    # it can tell a real count from a hardcoded 1. Found by mutation: every other fixture here drops a
    # single distinct glyph, so `f"...:{len(dropped)}"` and `"...:1"` were indistinguishable.
    assert "caption_emoji_unavailable:2" in notes, notes


def test_nothing_is_recorded_when_every_glyph_renders(tmp_path):
    """A marker on every clip is noise, and noise is what stops a marker being read."""
    notes: list[str] = []
    _render(tmp_path, available=True, notes=notes)

    assert not [n for n in notes if n.startswith("caption_emoji_unavailable")], notes


def test_the_marker_counts_distinct_glyphs_not_occurrences(tmp_path):
    """What an operator would go and fix is a glyph, not an occurrence.

    Repeating the same word ten times is still one missing font coverage gap.
    """
    words = [_word("money")] * 4
    notes: list[str] = []
    original = cap.glyph_available
    try:
        cap.glyph_available = lambda _g: False  # type: ignore[assignment]
        cap.build_ass(
            cap.words_to_cues(words),
            tmp_path / "repeat.ass",
            preset=PRESET,
            clip_duration=3.0,
            notes=notes,
        )
    finally:
        cap.glyph_available = original  # type: ignore[assignment]

    found = [n for n in notes if n.startswith("caption_emoji_unavailable:")]
    assert found == ["caption_emoji_unavailable:1"], found


@pytest.mark.real_binary
def test_the_probe_agrees_with_the_fonts_actually_installed():
    """Cross-checks the probe against fontconfig through a different question (R9.9).

    `glyph_available` asks `fc-list ":charset=<hex>"`; this asks fontconfig to *enumerate* families
    and confirms the two agree about a character every text font has. Sharing no query with the
    implementation is the point — a probe that always returned the same answer would otherwise look
    correct.
    """
    families = cap._enumerate_system_fonts()
    if families is None:
        pytest.skip("fontconfig cannot enumerate fonts here, so there is nothing to cross-check")

    assert cap.glyph_available("A") is True, (
        f"fontconfig lists {len(families)} families but reports none covering U+0041"
    )
