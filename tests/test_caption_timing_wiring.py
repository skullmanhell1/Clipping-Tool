"""C23, C24 and C25 reach the rendered file.

All three shipped implemented, tested and **never called**. `word_spans.apply_hygiene`,
`cue_constraints.apply_constraints` and `cue_constraints.choose_break` had no importer outside their
own test modules, and `min_cue_seconds` / `max_reading_rate` / `caption_linguistic_breaks` were read
by nothing. Every one of their unit tests passed the whole time, because a unit test of a pure
function cannot tell whether anything calls it.

So every assertion here is on the **rendered ASS**, or on the plan a renderer consumes — never on a
pure function's return value. Each test fails if its call is deleted, which is the one property the
existing suite could not have.

Two of them also assert the *discriminator*: that the feature-off render differs from the feature-on
one. Without that, a test can pass because the fixture never needed the feature, which is how a
vacuous test survives review.
"""

from __future__ import annotations

import pytest

from tests.conftest import FakeWord
from worker import captions as cap
from worker import text_metrics as tm
from worker.effects.caption_presets import resolve_preset
from worker.engines.kinetic import plan_kinetic
from worker.transcribe import Word

#: `karaoke_fill` emits one `\kf` tag per word, which is what makes a word span *observable* in the
#: output at all. A preset without a per-word tag would render these fixtures identically whatever
#: the spans said, and the test would prove nothing.
PRESET = resolve_preset("karaoke")[0]


def _words(
    text: str, *, step: float = 0.3, length: float = 0.25, offset: float = 0.0
) -> list[Word]:
    return [
        Word(start=offset + (i * step), end=offset + (i * step) + length, text=token)
        for i, token in enumerate(text.split())
    ]


def _render(cues, tmp_path, notes=None, **kwargs) -> list[str]:
    """Render and return only the Dialogue events."""
    dest = cap.build_ass(
        cues,
        tmp_path / "c.ass",
        preset=PRESET,
        clip_duration=kwargs.pop("clip_duration", 3.0),
        notes=notes,
        **kwargs,
    )
    return [
        line
        for line in dest.read_text(encoding="utf-8").splitlines()
        if line.startswith("Dialogue")
    ]


def _fills(dialogue: str) -> list[int]:
    """The `\\kf` durations in centiseconds, in order."""
    import re

    return [int(match) for match in re.findall(r"\\kf(\d+)", dialogue)]


# --------------------------------------------------------------------------- #
# C23 - word-span hygiene reaches the karaoke fill                            #
# --------------------------------------------------------------------------- #


def test_c23_an_overlapping_span_is_truncated_in_the_rendered_fill(tmp_path):
    """The defect this exists to stop: two words lit at once.

    `alpha` runs to 1.0 while `beta` starts at 0.5, so unrepaired the file carries `\\kf100` for a
    word whose successor begins 50cs earlier — libass sweeps both simultaneously. Repaired, `alpha`
    ends one millisecond before `beta` starts, giving `\\kf50`.

    The assertion is on 50 rather than "less than 100" because the exact figure is the arithmetic of
    SPAN_EPSILON: `round((0.5 - 0.001) * 100)`. A range assertion would also pass if hygiene
    truncated to something arbitrary.
    """
    cues = [cap.Cue(0.0, 1.2, [Word(0.0, 1.0, "alpha"), Word(0.5, 1.2, "beta")])]
    notes: list[str] = []
    dialogue = _render(cues, tmp_path, notes)

    assert _fills(dialogue[0]) == [50, 70]
    assert "word_spans_repaired:1" in notes


def test_c23_reports_nothing_when_there_was_nothing_to_repair(tmp_path):
    """A marker on every clip is noise, so the report is empty unless it changed something."""
    cues = [cap.Cue(0.0, 1.2, [Word(0.0, 0.4, "alpha"), Word(0.5, 0.9, "beta")])]
    notes: list[str] = []
    _render(cues, tmp_path, notes)

    assert notes == []


def test_c23_leaves_a_wellformed_render_byte_identical(tmp_path, monkeypatch):
    """The reason hygiene can run unconditionally.

    Rendered once normally and once with the pass stubbed out to a no-op; the bytes must match. This
    is what makes "the mechanism ships on" defensible without re-freezing a single caption golden —
    and it is a stronger claim than "no marker was emitted", which would still hold if hygiene had
    quietly rewritten a span without counting it.
    """
    # Built with `words_to_cues` rather than by hand, because a hand-built `Cue` can be internally
    # inconsistent in a way production never is: `words_to_cues` sets `cue.end` to its last word's
    # end, so the R8.5 cue-boundary clamp has nothing to do. The first version of this test set the
    # window shorter than its own last word and then reported hygiene as "not a no-op", which was
    # the fixture being wrong rather than the pass being wrong.
    cues = cap.words_to_cues(_words("a clean well formed transcript"), max_words=12)
    real = cap.build_ass(cues, tmp_path / "real.ass", preset=PRESET, clip_duration=3.0)
    real_bytes = real.read_bytes()

    monkeypatch.setattr(cap, "apply_span_hygiene", lambda cues, **_: (cues, _EmptyReport()))
    stubbed = cap.build_ass(cues, tmp_path / "stub.ass", preset=PRESET, clip_duration=3.0)

    assert stubbed.read_bytes() == real_bytes


class _EmptyReport:
    markers: list[str] = []


def test_c23_refuses_a_sequence_it_cannot_rebuild(tmp_path):
    """Readable is not rebuildable, and conflating them raised TypeError from the renderer.

    `FakeWord` has `start`/`end` but is not a dataclass, so `dataclasses.replace` cannot copy it.
    The caption paths are duck-typed deliberately, so the pass has to decline rather than assume —
    and decline for the *whole* sequence, since a half-repaired timeline is neither the transcript's
    nor a corrected one.
    """
    overlapping = [FakeWord(0.0, 1.0, "alpha"), FakeWord(0.5, 1.2, "beta")]
    cues = [cap.Cue(0.0, 1.2, overlapping)]
    notes: list[str] = []

    dialogue = _render(cues, tmp_path, notes)  # must not raise

    assert _fills(dialogue[0]) == [100, 70], "spans were altered despite being un-copyable"
    assert notes == [], "repairs were reported that did not happen"


# --------------------------------------------------------------------------- #
# C24 - cue floors reach the dialogue timestamps                              #
# --------------------------------------------------------------------------- #


def test_c24_extends_a_short_cue_in_the_rendered_timestamps(tmp_path, monkeypatch):
    """0.4s is nine frames. The floor moves the cue's END, and only its end."""
    monkeypatch.setattr(cap.settings, "min_cue_seconds", 1.5)
    cues = [cap.Cue(0.0, 0.4, [Word(0.0, 0.4, "tiny")])]
    notes: list[str] = []

    dialogue = _render(cues, tmp_path, notes)

    assert dialogue[0].startswith("Dialogue: 0,0:00:00.00,0:00:01.50,")
    assert "cue_extended:1" in notes


def test_c24_extending_a_cue_does_not_slow_the_karaoke_fill(tmp_path, monkeypatch):
    """R4.8, and the whole reason cue windows and word spans are separate types.

    A cue held on screen longer must not make the highlight sweep slower, or the captions stop
    following speech — which is the one thing they exist to do. So `\\kf` is unchanged at 40cs even
    though the line is now up for 150.
    """
    cues = [cap.Cue(0.0, 0.4, [Word(0.0, 0.4, "tiny")])]
    before = _fills(_render(cues, tmp_path)[0])

    monkeypatch.setattr(cap.settings, "min_cue_seconds", 1.5)
    after = _fills(_render(cues, tmp_path)[0])

    assert before == after == [40]


def test_c24_is_off_by_default(tmp_path):
    """Reproduces v0.11.0 exactly (R4.12): the same 0.4s cue ships at 0.4s."""
    cues = [cap.Cue(0.0, 0.4, [Word(0.0, 0.4, "tiny")])]
    notes: list[str] = []

    dialogue = _render(cues, tmp_path, notes)

    assert dialogue[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.40,")
    assert notes == []


def test_c24_never_makes_two_cues_overlap(tmp_path, monkeypatch):
    """Non-overlap outranks the floor (R4.4/R4.5), and the relaxation is recorded not hidden.

    The fixture is chosen so the escape hatch is closed: "my entire workflow" and "the hard part"
    each fit the line budget on their own and their concatenation does not (measured against this
    preset at 1080px), so step 2's merge is refused. That leaves the pass with a 10s floor it cannot
    reach by extending either — and the correct answer is two short cues that do not overlap, plus a
    marker saying the floor was abandoned.

    Without the un-mergeable fixture this test passed for the wrong reason: the two cues merged into
    one, which satisfies "they do not overlap" trivially and exercises none of R4.5.
    """
    monkeypatch.setattr(cap.settings, "min_cue_seconds", 10.0)
    cues = [
        cap.Cue(0.0, 0.4, _words("my entire workflow")),
        cap.Cue(1.0, 1.4, _words("the hard part", offset=1.0)),
    ]
    notes: list[str] = []

    dialogue = _render(cues, tmp_path, notes, clip_duration=2.0)

    starts_ends = [line.split(",")[1:3] for line in dialogue]
    assert starts_ends == [["0:00:00.00", "0:00:01.00"], ["0:00:01.00", "0:00:02.00"]]
    assert any(note.startswith("cue_constraint_relaxed:") for note in notes)


# --------------------------------------------------------------------------- #
# C25 - linguistic breaking reaches the \N positions                          #
# --------------------------------------------------------------------------- #

#: Measured against this preset at 1080px: width-only wrapping breaks after "the", stranding an
#: article at the end of line one. That is the defect C25 exists to fix, and it is what makes the
#: tests below non-vacuous.
#:
#: The first fixture tried here, "get the best results", was wrong and looked right: width wrapping
#: breaks it after "best", so C25 changed the output but fixed nothing, and a test asserting only
#: "the output changed" would have passed while demonstrating none of the feature's purpose.
BINDING_FIXTURE = "this changed the whole thing"


def test_c25_the_width_wrap_really_does_strand_an_article():
    """The discriminator, asserted directly so the test below cannot pass vacuously.

    If a font or budget change ever stopped width-wrapping from breaking after "the" on this
    fixture, the C25 test would still pass while proving nothing. This fails first and says why.
    """
    words = BINDING_FIXTURE.split()
    fit = cap.TextFit.for_preset(PRESET, video_width=1080)
    groups = tm.wrap_word_groups(
        words,
        font=fit.font,
        font_size=fit.font_size,
        max_width_px=fit.max_width_px,
        max_lines=fit.max_lines,
        spacing=fit.spacing,
        scale_x=fit.scale_x,
    )
    first_line = [words[i] for i in groups[0]]
    assert first_line[-1] == "the", f"fixture no longer strands an article: {groups}"


def test_c25_moves_the_break_off_the_binding_word_in_the_rendered_ass(tmp_path, monkeypatch):
    """`\\N` lands after "get" instead of after "the"."""
    monkeypatch.setattr(cap.settings, "caption_linguistic_breaks", True)
    cues = [cap.Cue(0.0, 1.2, _words(BINDING_FIXTURE))]

    dialogue = _render(cues, tmp_path, language="en")[0]

    head = dialogue.split("\\N")[0]
    assert head.endswith("changed")
    assert "the" not in head


def test_c25_is_off_by_default(tmp_path):
    """R5.9. Off, the measured wrap stands and the article is still stranded."""
    cues = [cap.Cue(0.0, 1.2, _words(BINDING_FIXTURE))]

    dialogue = _render(cues, tmp_path, language="en")[0]

    assert dialogue.split("\\N")[0].endswith("the")


@pytest.mark.parametrize("language", ["de", "fr", "ja", ""])
def test_c25_applies_to_english_only(tmp_path, monkeypatch, language):
    """R5.8. `BINDING_WORDS` is an English function-word list; applying it elsewhere is nonsense.

    The empty string covers the case that actually matters in practice: a source whose language was
    never detected. It must fall back to width rather than assume English.
    """
    monkeypatch.setattr(cap.settings, "caption_linguistic_breaks", True)
    cues = [cap.Cue(0.0, 1.2, _words(BINDING_FIXTURE))]

    dialogue = _render(cues, tmp_path, language=language)[0]

    assert dialogue.split("\\N")[0].endswith("the"), "non-English text got the English rules"


def test_c25_never_drops_or_reorders_a_word(tmp_path, monkeypatch):
    """R5.6. The only thing a break may influence is where the line divides."""
    monkeypatch.setattr(cap.settings, "caption_linguistic_breaks", True)
    cues = [cap.Cue(0.0, 1.2, _words(BINDING_FIXTURE))]

    dialogue = _render(cues, tmp_path, language="en")[0]

    body = dialogue.split(",,0,0,0,,", 1)[1]
    # `\N` is the break; each remaining token is `{\kfNN}word`, so the text is what follows the
    # closing brace. Compared as a list, so a dropped, duplicated or reordered word all fail.
    rendered = [token.split("}")[-1] for token in body.replace("\\N", " ").split()]

    assert rendered == BINDING_FIXTURE.split()


# --------------------------------------------------------------------------- #
# The kinetic engine has its own copy of the path                             #
# --------------------------------------------------------------------------- #


def test_c23_reaches_the_kinetic_plan(tmp_path):
    """The engine supersedes the compositor's captions entirely (Req 3.2).

    So the hygiene wired into `build_ass` never runs on a kinetic render, and without a second call
    the defect would survive in exactly the path whose whole purpose is per-word animation.

    `FakeWord` is fine here, unlike in the `build_ass` test above: `_sanitise_words` converts every
    input to `_Source_Word`, which *is* a dataclass, so the pass can rebuild them.
    """
    plan = plan_kinetic([FakeWord(0.0, 1.0, "alpha"), FakeWord(0.5, 1.2, "beta")], 2.0)

    spans = [(w.start, w.end) for cue in plan.cues for w in cue.words]
    assert spans[0][1] <= spans[1][0], f"kinetic words still overlap: {spans}"
    assert "engine:kinetic_typography:word_spans_repaired:1" in plan.markers


def test_the_kinetic_plan_is_unchanged_when_spans_are_clean():
    """No marker, and no invented movement, on a transcript that needed nothing."""
    plan = plan_kinetic([FakeWord(0.0, 0.4, "alpha"), FakeWord(0.5, 0.9, "beta")], 2.0)

    assert not any("word_spans_repaired" in m for m in plan.markers)


def test_kinetic_rel_ms_still_matches_the_repaired_start():
    """`rel_ms` is derived from `start`, so hygiene has to run before it is computed.

    Run it after, and the animation would key off the pre-repair time while the fill used the
    repaired one — the two would disagree by exactly the correction, which is the least debuggable
    possible outcome.
    """
    plan = plan_kinetic([FakeWord(0.0, 1.0, "alpha"), FakeWord(0.5, 1.2, "beta")], 2.0)

    for cue in plan.cues:
        for word in cue.words:
            assert word.rel_ms == pytest.approx(round((word.start - cue.start) * 1000), abs=1)


# --------------------------------------------------------------------------- #
# Ordering: C24 before C23                                                    #
# --------------------------------------------------------------------------- #


def test_build_ass_runs_c24_before_c23(tmp_path, monkeypatch):
    """Observed *through* `build_ass`, because the seam-level test below cannot see the order.

    Swapping the two calls in `build_ass` passed every other test in this file. That is the escape
    the seam-level test invites: calling two functions in the right order inside a test proves
    nothing about the order the production code calls them in.

    The discriminator is the cue-boundary clamp. C23 truncates the last word of a cue to that cue's
    end (R8.5). Run first, it clamps "alpha" to the pre-merge boundary 0.4 and the merge then throws
    that boundary away, leaving `\\kf40`. Run second, the window it clamps against is the merged
    2.0s one, "alpha" is inside it, and the transcribed 0.6 survives as `\\kf60`.
    """
    monkeypatch.setattr(cap.settings, "min_cue_seconds", 2.0)
    cues = [
        cap.Cue(0.0, 0.4, [Word(0.0, 0.6, "alpha")]),
        cap.Cue(1.0, 1.4, [Word(1.0, 1.4, "beta")]),
    ]

    dialogue = _render(cues, tmp_path, clip_duration=3.0)

    assert len(dialogue) == 1, "the cues should have merged"
    assert _fills(dialogue[0])[0] == 60, "C23 clamped against a window C24 had not yet produced"


def test_c24_runs_before_c23(monkeypatch):
    """Not interchangeable, and the failure is silent if they are swapped.

    C24 merges two cues into one; C23 clamps word spans to the cue window they belong to. Run C23
    first and it clamps to a boundary the merge then discards, so the last word of the first cue
    stays truncated against a cue end that no longer exists.

    Asserted at the seam rather than through the renderer because the observable difference is a
    single millisecond on one span — real, but not something a `\\kf` centisecond can show.
    """
    monkeypatch.setattr(cap.settings, "min_cue_seconds", 2.0)
    cues = [
        cap.Cue(0.0, 0.4, [Word(0.0, 0.4, "first")]),
        cap.Cue(0.4, 0.8, [Word(0.4, 0.8, "second")]),
    ]

    constrained, report = cap.apply_cue_constraints(cues, clip_duration=4.0)

    assert report.merged == 1
    assert len(constrained) == 1, "the two cues should have merged into one window"
    # The merged window is what C23 then clamps against. It ends at the 2.0 floor -- the merge
    # produced 0.0-0.8, which is still short, so step 1 then extended it into free time. What
    # matters for the ordering is that the boundary C23 sees is 2.0 and not the 0.4 the first cue
    # ended at, which the merge discarded.
    assert constrained[0].end == pytest.approx(2.0)
    _repaired, hygiene = cap.apply_span_hygiene(constrained)
    assert hygiene.altered == 0, "clamping against the merged window should change nothing here"
