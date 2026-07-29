"""Property tests for the caption preset model and keyword planner.

Covers tasks 2.2, 2.3, 2.4 (preset model / registry / resolution) and tasks
2.6, 2.7 (pure keyword planner). One property per test, tagged with the design
property text and a ``Validates: Requirements ...`` docstring.

Reuses the ``FakeWord`` helper from ``tests/conftest.py`` and ``MockLLMClient``
from ``worker.llm_client`` for the AI-assisted path.
"""
from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import FakeWord
from worker.effects.caption_presets import (
    BUILTIN_PRESETS,
    VALID_ANIMATIONS,
    VALID_POSITIONS,
    CaptionColors,
    CaptionPreset,
    load_preset,
    plan_keywords,
    resolve_preset,
)
from worker.llm_client import MockLLMClient

# --- Word-list strategy -----------------------------------------------------
# A mix of tokens that exercise every deterministic rule: stopwords, long
# content words, ALL-CAPS acronyms, numerals/currency, and short fillers.
_TOKEN_POOL = [
    "the", "a", "and", "of", "to", "is",          # stopwords
    "revolutionary", "strategy", "algorithm",       # long content words
    "growth", "leverage", "compound",               # long content words
    "NASA", "CEO", "AI",                            # ALL-CAPS acronyms
    "$5", "42", "100%", "3.14",                     # numerals / currency
    "go", "win", "big", "now",                      # short words
]


@st.composite
def _word(draw):
    text = draw(st.sampled_from(_TOKEN_POOL))
    start = draw(st.floats(min_value=0.0, max_value=100.0))
    dur = draw(st.floats(min_value=0.05, max_value=2.0))
    prob = draw(st.floats(min_value=0.0, max_value=1.0))
    w = FakeWord(start, start + dur, text)
    w.probability = prob
    return w


@st.composite
def _word_lists(draw):
    """Ordered lists of FakeWord (0-12 words)."""
    n = draw(st.integers(min_value=0, max_value=12))
    words = [draw(_word()) for _ in range(n)]
    # Keep the timeline ordered by start (mirrors real transcripts).
    words.sort(key=lambda w: w.start)
    return words


def _preset_strategy():
    """CaptionPresets drawn from the registry plus varied field overrides."""
    return st.builds(
        lambda base, animation, font, font_size, position, hi, scale, emoji, border,
        primary, highlight, outline, box: CaptionPreset(
            name=base.name,
            animation=animation,
            font=font,
            font_size=font_size,
            colors=CaptionColors(primary=primary, highlight=highlight,
                                 outline=outline, box=box),
            position=position,
            highlight_keywords=hi,
            highlight_scale=scale,
            emoji_inline=emoji,
            border_style=border,
        ),
        base=st.sampled_from(list(BUILTIN_PRESETS.values())),
        animation=st.sampled_from(sorted(VALID_ANIMATIONS)),
        font=st.sampled_from(["Arial", "Impact", "Roboto", "Montserrat"]),
        font_size=st.integers(min_value=20, max_value=140),
        position=st.sampled_from(sorted(VALID_POSITIONS)),
        hi=st.booleans(),
        scale=st.floats(min_value=1.0, max_value=2.0),
        emoji=st.booleans(),
        border=st.sampled_from([1, 3]),
        primary=st.sampled_from(["&H00FFFFFF", "&H0000FF00"]),
        highlight=st.sampled_from(["&H0000E5FF", "&H00FF00FF"]),
        outline=st.sampled_from(["&H00000000"]),
        box=st.sampled_from(["&H80000000", "&H64000000"]),
    )


# --- Property 1 -------------------------------------------------------------
# Feature: tier1-creator-output-upgrade, Property 1: Built-in presets are complete
@settings(max_examples=100)
@given(name=st.sampled_from(list(BUILTIN_PRESETS.keys())))
def test_p1_builtin_presets_complete(name):
    """Validates: Requirements 1.2

    Every registry preset defines a valid animation, a non-empty font, a
    CaptionColors, and a valid default position.
    """
    preset = BUILTIN_PRESETS[name]
    assert preset.animation in VALID_ANIMATIONS
    assert isinstance(preset.font, str) and preset.font
    assert isinstance(preset.colors, CaptionColors)
    assert preset.position in VALID_POSITIONS


# --- Property 2 -------------------------------------------------------------
# Feature: tier1-creator-output-upgrade, Property 2: Unknown or malformed presets fall back to karaoke
@settings(max_examples=100)
@given(
    junk=st.one_of(
        st.text(max_size=20).filter(lambda s: s not in BUILTIN_PRESETS),
        st.none(),
        st.integers(),
        st.dictionaries(st.text(max_size=5), st.integers(), max_size=3),
        st.lists(st.integers(), max_size=3),
    )
)
def test_p2_unknown_or_malformed_fall_back_to_karaoke(junk):
    """Validates: Requirements 1.5, 6.4

    Arbitrary unknown names and malformed inputs resolve to the karaoke preset
    with ``substituted`` True, and never raise.
    """
    karaoke = BUILTIN_PRESETS["karaoke"]

    # resolve_preset only accepts names, but must tolerate any input.
    r_preset, r_sub = resolve_preset(junk)
    assert r_preset == karaoke
    assert r_sub is True

    # load_preset tolerates names, dicts, and junk. Malformed dicts fall back.
    l_preset, l_sub = load_preset(junk)
    if isinstance(junk, dict):
        # A dict without a valid animation/font is malformed -> karaoke.
        assert l_preset == karaoke
        assert l_sub is True
    else:
        assert l_preset == karaoke
        assert l_sub is True


# --- Property 3 -------------------------------------------------------------
# Feature: tier1-creator-output-upgrade, Property 3: Caption preset round-trip
@settings(max_examples=100)
@given(preset=_preset_strategy())
def test_p3_caption_preset_round_trip(preset):
    """Validates: Requirements 6.2

    For any CaptionPreset, ``from_dict(to_dict(p)) == p`` (nested colours
    included).
    """
    assert CaptionPreset.from_dict(preset.to_dict()) == preset


# --- Property 7 -------------------------------------------------------------
# Feature: tier1-creator-output-upgrade, Property 7: Deterministic keyword planning and AI-unavailable equivalence
@settings(max_examples=100)
@given(words=_word_lists())
def test_p7_deterministic_planning_and_ai_unavailable_equivalence(words):
    """Validates: Requirements 3.2, 3.4

    ``plan_keywords`` is stable across repeated calls and makes no LLM call;
    ``plan_keywords(..., use_ai=True, client=None)`` equals the deterministic
    result.
    """
    spy = MockLLMClient(responses=["[]"])

    first = plan_keywords(words)
    second = plan_keywords(words)
    assert first == second  # stable / deterministic

    # No LLM work occurs on the default path even with a client available but
    # AI disabled.
    assert plan_keywords(words, use_ai=False, client=spy) == first
    assert spy.calls == []  # zero LLM calls (Req 3.4 / 3.6)

    # AI requested but no client -> deterministic set only.
    assert plan_keywords(words, use_ai=True, client=None) == first


# --- Property 8 -------------------------------------------------------------
# Feature: tier1-creator-output-upgrade, Property 8: AI-assisted highlighting extends the deterministic set
@settings(max_examples=100)
@given(words=_word_lists())
def test_p8_ai_assisted_highlighting_extends_deterministic_set(words):
    """Validates: Requirements 3.3

    With ``use_ai=True`` and a MockLLMClient returning some words, the resulting
    highlighted set is a superset of the deterministic set.
    """
    deterministic = plan_keywords(words)

    # Mock returns the first couple of tokens as "important" words.
    picks = [w.text for w in words[:2]]

    def _handler(prompt, system=None):
        return json.dumps(picks)

    client = MockLLMClient(handler=_handler)
    merged = plan_keywords(words, use_ai=True, client=client)

    assert deterministic <= merged  # superset invariant



# ===========================================================================
# Task 3.3-3.7 — ASS generation extension property tests (worker/captions.py)
# ===========================================================================
import os  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
from dataclasses import replace  # noqa: E402
from pathlib import Path  # noqa: E402

import worker.captions as cap  # noqa: E402
from worker.captions import (  # noqa: E402
    _POSITION_ALIGN,
    Cue,
    build_ass,
    build_word_span,
    words_to_cues,
)
from worker.transcribe import Word  # noqa: E402


def _parse_ass_ts(ts: str) -> float:
    """Parse an ASS ``H:MM:SS.cs`` timestamp into seconds."""
    hours, minutes, rest = ts.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


@st.composite
def _timeline_and_duration(draw):
    """A clip-relative word timeline plus its clip duration ``D``."""
    duration = draw(st.floats(min_value=1.0, max_value=30.0))
    n = draw(st.integers(min_value=0, max_value=8))
    words = []
    for _ in range(n):
        start = draw(st.floats(min_value=0.0, max_value=duration))
        dur = draw(st.floats(min_value=0.05, max_value=2.0))
        end = min(duration, start + dur)
        w = FakeWord(round(start, 3), round(end, 3), draw(st.sampled_from(_TOKEN_POOL)))
        words.append(w)
    words.sort(key=lambda w: w.start)
    return words, duration


# --- Property 4 -------------------------------------------------------------
# Feature: tier1-creator-output-upgrade, Property 4: Per-word animation is timed to the word and bounded
@settings(max_examples=100, deadline=None)
@given(data=_timeline_and_duration(), anim=st.sampled_from(["pop", "typewriter"]))
def test_p4_per_word_animation_timed_and_bounded(data, anim):
    """Validates: Requirements 2.1, 2.5, 21.5

    Each word's animation is anchored to the word's ``start`` (the ``\\t`` ramp
    offset equals the word's offset from its cue start) and every emitted
    dialogue timestamp stays within the clip bounds ``[0, D]``.
    """
    words, duration = data
    preset = replace(BUILTIN_PRESETS["pop"], animation=anim)

    # Anchored to each word's start (offset relative to cue start = 0.0 here).
    for w in words:
        span = build_word_span(w, preset, False, cue_start=0.0)
        m = re.search(r"\\t\((\d+),", span)
        assert m is not None
        assert int(m.group(1)) == max(0, round(w.start * 1000))

    # Bounded: build the ASS and confirm all dialogue timestamps ∈ [0, D].
    cues = words_to_cues(words)
    dest = tempfile.mktemp(suffix=".ass")
    build_ass(cues, dest, preset=preset, clip_duration=duration)
    text = Path(dest).read_text(encoding="utf-8")
    os.remove(dest)
    eps = 1e-6
    for line in text.splitlines():
        if line.startswith("Dialogue:") and ",Default," in line:
            _, start_ts, end_ts, *_ = line.split(",", 3)
            for ts in (start_ts, end_ts):
                secs = _parse_ass_ts(ts)
                assert -eps <= secs <= duration + 0.01


# --- Property 5 -------------------------------------------------------------
# Feature: tier1-creator-output-upgrade, Property 5: Captions use libass ASS tags only
@settings(max_examples=100, deadline=None)
@given(data=_timeline_and_duration(), name=st.sampled_from(list(BUILTIN_PRESETS)))
def test_p5_captions_use_ass_tags_only(data, name):
    """Validates: Requirements 2.3

    Generated caption output (ASS text and the subtitles filter string) never
    uses the ffmpeg ``drawtext`` filter — libass ASS tags only.
    """
    words, duration = data
    cues = words_to_cues(words)
    dest = tempfile.mktemp(suffix=".ass")
    build_ass(
        cues, dest, preset=BUILTIN_PRESETS[name], keyword_indices=set(),
        clip_duration=duration,
    )
    from worker.captions import subtitles_filter

    filt = subtitles_filter(dest)
    text = Path(dest).read_text(encoding="utf-8")
    os.remove(dest)
    assert "drawtext" not in text.lower()
    assert "drawtext" not in filt.lower()


# --- Property 6 -------------------------------------------------------------
# Feature: tier1-creator-output-upgrade, Property 6: Keyword highlighting is visually distinct and timing-preserving
@settings(max_examples=100)
@given(name=st.sampled_from(list(BUILTIN_PRESETS)), word=_word())
def test_p6_keyword_highlight_distinct_and_timing_preserving(name, word):
    """Validates: Requirements 3.1, 3.5

    A highlighted span carries a distinct colour and scale versus the
    non-highlighted span, while the underlying animation/timing span is left
    unchanged (the highlight only wraps it).
    """
    preset = BUILTIN_PRESETS[name]
    highlighted = build_word_span(word, preset, True, cue_start=0.0)
    plain = build_word_span(word, preset, False, cue_start=0.0)

    # Timing / animation preserved: the highlight only wraps the plain span.
    assert plain in highlighted

    # Distinct highlight colour present only when highlighted.
    colour_tag = f"\\c{preset.colors.highlight}&"
    assert colour_tag in highlighted
    assert colour_tag not in plain

    # Distinct highlight scale present only when highlighted.
    scale = int(round(preset.highlight_scale * 100))
    assert f"\\fscx{scale}" in highlighted
    assert f"\\fscx{scale}" not in plain


# --- Property 9 -------------------------------------------------------------
# Feature: tier1-creator-output-upgrade, Property 9: Preset styling applied, position override wins
@settings(max_examples=100, deadline=None)
@given(
    name=st.sampled_from(list(BUILTIN_PRESETS)),
    override=st.sampled_from(sorted(VALID_POSITIONS)),
)
def test_p9_preset_styling_applied_position_override_wins(name, override):
    """Validates: Requirements 5.1, 5.2

    The rendered style reflects the preset's font, colours, and default
    position; supplying a ``position`` override changes the ASS alignment.
    """
    preset = BUILTIN_PRESETS[name]
    orig = cap.font_available
    cap.font_available = lambda _n: True  # avoid host-dependent substitution
    try:
        # No override -> preset default position + preset font/colours.
        dest = tempfile.mktemp(suffix=".ass")
        cap.build_ass(
            [Cue(0.0, 1.0, [Word(0.0, 0.5, "hi"), Word(0.5, 1.0, "yo")])],
            dest, preset=preset, clip_duration=1.0,
        )
        style = next(
            ln for ln in Path(dest).read_text(encoding="utf-8").splitlines()
            if ln.startswith("Style: Default")
        )
        os.remove(dest)
        vals = style.split("Style: ", 1)[1].split(",")
        assert vals[1] == preset.font
        assert vals[3] == preset.colors.primary
        assert int(vals[18]) == _POSITION_ALIGN[preset.position][0]

        # Override -> alignment reflects the override, not the preset default.
        dest2 = tempfile.mktemp(suffix=".ass")
        cap.build_ass(
            [Cue(0.0, 1.0, [Word(0.0, 0.5, "hi")])],
            dest2, preset=preset, position=override, clip_duration=1.0,
        )
        style2 = next(
            ln for ln in Path(dest2).read_text(encoding="utf-8").splitlines()
            if ln.startswith("Style: Default")
        )
        os.remove(dest2)
        vals2 = style2.split("Style: ", 1)[1].split(",")
        assert int(vals2[18]) == _POSITION_ALIGN[override][0]
    finally:
        cap.font_available = orig


# --- Property 11 ------------------------------------------------------------
# Feature: tier1-creator-output-upgrade, Property 11: In-caption emoji respect permissibility
_EMOJI_WORDS = ["money", "fire", "love", "idea", "boom"]


@settings(max_examples=100, deadline=None)
@given(allowed=st.sets(st.sampled_from(_EMOJI_WORDS)))
def test_p11_in_caption_emoji_respect_permissibility(allowed):
    """Validates: Requirements 4.4

    Under Permissibility_Mode only locally-available glyphs appear in the cue
    text and no external download is attempted (a downloader spy stays
    untouched); surrounding words are always retained.
    """
    preset = BUILTIN_PRESETS["hormozi"]  # emoji_inline=True
    glyph_map = {kw: cap._CAPTION_EMOJI[kw] for kw in _EMOJI_WORDS}
    allowed_glyphs = {glyph_map[kw] for kw in allowed}

    downloads: list = []

    def downloader(*args, **kwargs):
        downloads.append((args, kwargs))
        return None

    def glyph_available(glyph):
        return glyph in allowed_glyphs

    words = [Word(float(i), float(i) + 0.4, kw) for i, kw in enumerate(_EMOJI_WORDS)]
    dest = tempfile.mktemp(suffix=".ass")
    build_ass(
        [Cue(0.0, float(len(words)), words)], dest, preset=preset,
        clip_duration=float(len(words)), permissibility=True,
        emoji_glyph_available=glyph_available, emoji_downloader=downloader,
    )
    text = Path(dest).read_text(encoding="utf-8")
    os.remove(dest)

    assert downloads == []  # no external download under permissibility
    for kw in _EMOJI_WORDS:
        glyph = glyph_map[kw]
        if kw in allowed:
            assert glyph in text
        else:
            assert glyph not in text
        # Surrounding words are retained. Compared case-insensitively because the preset
        # under test sets ``uppercase`` (C7) - the clause is about the word surviving the
        # emoji decision, not about its casing.
        assert kw.upper() in text.upper()



# ===========================================================================
# C11 — emphasis is relative, not absolute
# ===========================================================================
# The old rule set highlighted any non-stopword whose Whisper probability reached 0.9.
# Two things made that the same as highlighting nothing:
#
#   * on clean audio nearly every word clears 0.9; and
#   * ``_word_probability`` returns 1.0 when a word has no probability attribute at all,
#     so a transcript without per-word confidence highlighted *every* non-stopword.
#
# Emphasis is now a ranking with a budget. These tests pin that, because the goldens
# elsewhere only show its effect on one fixed sentence.
def test_high_confidence_alone_no_longer_emphasises_a_word():
    """The exact defect C11 names: confidence is not salience.

    Six short stopword-free words with perfect confidence and no other signal. Under the
    old rule every one of them was emphasised; a budget now applies, and a word that is
    merely short and clearly-heard is the weakest candidate there is.
    """
    from worker.effects.caption_presets import plan_keywords

    words = [FakeWord(float(i), float(i) + 0.4, "word") for i in range(6)]
    for word in words:
        word.probability = 1.0

    emphasised = plan_keywords(words, use_ai=False)
    assert len(emphasised) <= 2, (
        f"{len(emphasised)} of 6 identical words emphasised; emphasis that applies to "
        "everything communicates nothing"
    )


def test_words_without_probability_are_not_all_emphasised():
    """A transcript with no per-word confidence must not emphasise everything.

    ``_word_probability`` defaults to 1.0 for a word that carries no probability, so the
    old ``>= 0.9`` rule fired on every non-stopword here.
    """
    from worker.effects.caption_presets import plan_keywords

    class Bare:
        def __init__(self, text):
            self.text = text
            self.start = 0.0
            self.end = 0.4

    words = [Bare(t) for t in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")]
    emphasised = plan_keywords(words, use_ai=False)
    assert 0 < len(emphasised) <= 2, f"expected a small budget, got {len(emphasised)}"


def test_emphasis_prefers_the_strongest_signal_available():
    """Numbers and ALL-CAPS outrank mere length, and the budget keeps the best."""
    from worker.effects.caption_presets import plan_keywords

    words = [
        FakeWord(0.0, 0.4, "the"),          # stopword: never eligible
        FakeWord(0.4, 0.8, "interesting"),  # long content word
        FakeWord(0.8, 1.2, "$5000"),        # currency: strongest
        FakeWord(1.2, 1.6, "and"),          # stopword
    ]
    for word in words:
        word.probability = 1.0

    assert plan_keywords(words, use_ai=False) == {2}


def test_stopwords_are_never_emphasised_whatever_the_budget():
    """A budget must not promote a stopword just because nothing better is present."""
    from worker.effects.caption_presets import plan_keywords

    words = [FakeWord(float(i), float(i) + 0.3, t) for i, t in enumerate(("the", "of", "a"))]
    for word in words:
        word.probability = 1.0

    assert plan_keywords(words, use_ai=False) == set()


def test_emphasis_is_a_pure_function_of_its_input():
    """Ranking introduces ordering; the result must still be deterministic.

    The kinetic determinism properties depend on this: the same words in, the same
    emphasis out, every time.
    """
    from worker.effects.caption_presets import plan_keywords

    words = [
        FakeWord(0.0, 0.4, "revolutionary"),
        FakeWord(0.4, 0.8, "AI"),
        FakeWord(0.8, 1.2, "$42"),
        FakeWord(1.2, 1.6, "changed"),
        FakeWord(1.6, 2.0, "everything"),
        FakeWord(2.0, 2.4, "the"),
        FakeWord(2.4, 2.8, "world"),
        FakeWord(2.8, 3.2, "forever"),
    ]
    first = plan_keywords(words, use_ai=False)
    assert all(plan_keywords(words, use_ai=False) == first for _ in range(5))
    assert first  # non-vacuous


def test_the_legacy_karaoke_sweep_and_the_preset_highlight_agree():
    """C4: one emphasis colour, not two that differ by which code path rendered.

    The legacy templates swept to pure green (``&H0000FF00``) while every preset swept to
    amber, so the same clip looked different depending on whether a preset was supplied.
    """
    from worker import captions
    from worker.effects.caption_presets import CaptionColors

    assert captions.HIGHLIGHT_COLOUR == CaptionColors().highlight
    assert captions.HIGHLIGHT_COLOUR != "&H0000FF00", "green is the value C4 removed"
