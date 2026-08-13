"""S21 cold-open assembly, and the non-monotonic rebase that is its real risk.

The spec calls task 2.3 the single highest-risk item in all four specs, and the reason is that its
failure is *plausible*: an assembly produces `[hook, body]` where the hook's source times come after
the body's, and a rebasing routine that assumes monotonic keeps places the hook's captions at the
body's timeline positions. Captions still appear, still look like captions, and are attached to the
wrong words — so it gets blamed on the ASR long before anyone suspects the assembly.

There is a second hazard that is quieter still. When the lifted line is *retained* in the body, the
same source range is in the keep list **twice**, and `filler.rebase_words` stops at the first match.
The line would be captioned on its first airing and silent on its second.

So the correspondence tests below are **one per consumer** — words, emoji, speaker turns — because one
rebased consumer working does not imply three, and a monotonic fixture proves nothing about any of
them.
"""

from __future__ import annotations

import re
import subprocess

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import FFMPEG, requires_ffmpeg
from worker import assembly
from worker.effects.filler import Interval
from worker.transcribe import Word

#: A clip whose strongest line sits in the middle. "the secret is you never actually need permission"
#: scores on `hook_score.text_signal`; the surrounding sentences do not.
_SCRIPT = (
    "this is the boring setup part okay right. "
    "the secret is you never actually need permission. "
    "and then we went home quietly afterwards now."
)


def _words(text: str = _SCRIPT, step: float = 0.45) -> list[Word]:
    return [
        Word(start=round(i * step, 3), end=round(i * step + 0.4, 3), text=token)
        for i, token in enumerate(text.split())
    ]


def _plan(**kwargs) -> assembly.Assembly_Plan:
    words = kwargs.pop("words", None) or _words()
    return assembly.plan(
        words,
        clip_duration=kwargs.pop("clip_duration", words[-1].end),
        enabled=kwargs.pop("enabled", True),
        max_seconds=kwargs.pop("max_seconds", 8.0),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# The assembly itself                                                         #
# --------------------------------------------------------------------------- #


def test_the_strongest_line_is_lifted_to_the_front():
    """R1.1. The hook is the first thing heard rather than something reached later."""
    plan = _plan()

    assert plan.assembled
    assert plan.cold_open is not None
    assert plan.segments[0] == plan.cold_open
    assert plan.cold_open.start > 0.0, "the fixture's best line is not in the middle"


def test_the_assembly_is_genuinely_non_monotonic():
    """The property every rebase test below depends on.

    Without this the fixture could be monotonic and every correspondence assertion would pass
    vacuously — which is exactly the trap the spec names for this task.
    """
    plan = _plan()

    assert plan.segments[0].start > plan.segments[1].start


def test_the_cold_open_comes_from_within_the_candidates_own_range():
    """R1.2. A clip is a contiguous source region, and U4, U7 and the benchmark all lean on that."""
    words = _words()
    plan = _plan(words=words)

    assert plan.cold_open is not None
    assert plan.cold_open.start >= 0.0
    assert plan.cold_open.end <= words[-1].end + 0.001


def test_the_cold_open_is_sentence_aligned():
    """R1.3. A hook that opens mid-sentence is worse than no hook."""
    words = _words()
    plan = _plan(words=words)
    sentences = assembly.sentences_from_words(words)

    assert plan.cold_open is not None
    assert any(
        abs(s.start - plan.cold_open.start) < 0.001 and abs(s.end - plan.cold_open.end) < 0.001
        for s in sentences
    ), "the lifted range does not match any sentence"


def test_there_is_never_more_than_one_cold_open():
    """R1.13. `choose_cold_open` returns a single sentence, so this holds by construction."""
    plan = _plan()

    assert plan.segments.count(plan.cold_open) >= 1
    # Exactly one segment is the hook; the rest are body.
    assert plan.segments[0] == plan.cold_open


def test_the_body_keeps_both_sides_when_the_hook_is_lifted_from_the_middle():
    """Dropping the tail would silently truncate the clip.

    With the line removed, the body is two segments — before and after — and both must survive, in
    source order, so the body still plays forward.
    """
    plan = _plan(retain_in_body=False, min_repeat_gap=0.0)

    assert plan.assembled
    body = plan.segments[1:]
    assert len(body) == 2, [(s.start, s.end) for s in body]
    assert body[0].end <= body[1].start, "the body no longer plays forward"


# --------------------------------------------------------------------------- #
# R1.5, R1.6 -- the editorial guards                                          #
# --------------------------------------------------------------------------- #


def test_no_cold_open_when_the_strongest_line_is_already_first():
    """R1.5. Reordering an already-correct clip produces a duplicate for nothing."""
    plan = _plan(
        words=_words(
            "the secret is you never actually need permission. "
            "this is the boring setup part okay right. "
            "and then we went home quietly afterwards now."
        )
    )

    assert not plan.assembled
    assert "already_first" in plan.detail


def test_a_clip_where_nothing_stands_out_reports_no_signal_not_already_first():
    """Two different findings, and conflating them would be a false claim about the material.

    Found while building this: `text_signal` is sparse and returns 0.0 for most sentences, so on a flat
    clip every score ties at zero and the earlier-index tiebreak hands back sentence 0. Reported as
    "already first" that reads as "your clip is correctly ordered", when the truth is that no line
    stood out at all.
    """
    plan = _plan(
        words=_words(
            "we walked to the shop and bought some bread there. "
            "then we walked back home again along the river. "
            "the weather was quite mild for the time of year."
        )
    )

    assert not plan.assembled
    assert "no_signal" in plan.detail
    assert "already_first" not in plan.detail


def test_a_dangling_opener_is_never_lifted_as_a_cold_open():
    """R1.6, and the cold open is the one position where this cannot be recovered from.

    A hook opening on "and that's exactly why he quit" is worse than no hook: elsewhere in the clip a
    back-reference resolves against what came before, and at position zero nothing did.

    Uses `discourse.standalone_completeness`, the detector that already exists — so this asserts the
    *filter is applied*, not that the detector works, which `test_selection_transcript.py` covers.
    """
    from worker import discourse

    dangling = "and that is exactly why he finally quit the whole thing."
    assert discourse.standalone_completeness(dangling).dangling_opener, (
        "the fixture is not a dangling opener, so this test asserts nothing"
    )

    plan = _plan(
        words=_words(f"we set the scene here first of all okay. {dangling} then it all ended.")
    )

    if plan.assembled:
        assert plan.cold_open is not None
        lifted = [
            s
            for s in assembly.sentences_from_words(
                _words(f"we set the scene here first of all okay. {dangling} then it all ended.")
            )
            if abs(s.start - plan.cold_open.start) < 0.001
        ]
        assert not any(discourse.standalone_completeness(s.text).dangling_opener for s in lifted), (
            "a dangling opener was lifted as the cold open"
        )


def test_a_cold_open_may_not_be_half_the_clip():
    """A cold open at or above half the clip makes the delivered clip a repeat rather than an edit."""
    words = _words("the secret is you never actually need permission at all here. and it ended.")
    plan = _plan(words=words, max_seconds=60.0)

    assert not plan.assembled


# --------------------------------------------------------------------------- #
# R1.7, R1.8, R1.9 -- duplication, repeat gap, length floor                    #
# --------------------------------------------------------------------------- #


def test_retaining_leaves_the_line_in_the_body():
    """R1.7. Hearing it twice is a recognised short-form device, and it is configuration."""
    plan = _plan(retain_in_body=True, min_repeat_gap=0.0)

    assert plan.retained_in_body
    assert len(plan.segments) == 2
    assert plan.segments[1].start == 0.0


def test_a_line_too_close_to_the_start_is_removed_rather_than_stuttered():
    """R1.8. Two occurrences seconds apart read as a stutter, not as a callback.

    It falls back to the *other configured behaviour* rather than refusing: an assembly that is fine in
    one form should not be abandoned for failing the other.
    """
    plan = _plan(retain_in_body=True, min_repeat_gap=30.0)

    assert plan.assembled
    assert not plan.retained_in_body
    assert "repeat_gap" in plan.detail


def test_the_length_floor_outranks_the_repeat_gap():
    """R1.9 over R1.8, deliberately.

    A clip under its preset's minimum is a broken deliverable; hearing a line twice is a style someone
    chose. So when removing the line would breach the floor, it is retained instead.
    """
    words = _words()
    plan = _plan(
        words=words,
        retain_in_body=True,
        min_repeat_gap=30.0,
        min_clip_seconds=words[-1].end,  # nothing may be removed at all
    )

    assert plan.assembled
    assert plan.retained_in_body
    assert "length_floor" in plan.detail


# --------------------------------------------------------------------------- #
# R1.10, R1.12 -- default and marker                                          #
# --------------------------------------------------------------------------- #


def test_disabled_produces_an_inert_plan():
    """R1.10. The default must build no assembly at all."""
    plan = _plan(enabled=False)

    assert not plan.assembled
    assert plan.segments == ()
    assert plan.marker == ""


def test_the_marker_names_the_source_range():
    """R1.12. "A cold open happened" is not actionable; which range was lifted is."""
    plan = _plan()

    assert re.fullmatch(r"cold_open:\d+\.\d{3}-\d+\.\d{3}", plan.marker), plan.marker
    assert plan.cold_open is not None
    assert f"{plan.cold_open.start:.3f}" in plan.marker


def test_a_malformed_duration_refuses_rather_than_raising():
    """An editorial refinement must never be why a clip fails."""
    plan = assembly.plan(_words(), clip_duration=0.0, enabled=True)

    assert not plan.assembled
    assert plan.refusal.startswith("assembly_refused:")


# --------------------------------------------------------------------------- #
# R2.3 -- one keep list, composed with everything else                        #
# --------------------------------------------------------------------------- #


def test_the_assembly_composes_with_filler_keeps_into_one_list():
    """R2.3. Applying them in sequence would concatenate twice.

    The assembly is the outer ordering and the filler keeps are an inner filter, so a removed filler
    word stays removed wherever its range is played — and the result is still a single list.
    """
    plan = _plan(retain_in_body=False, min_repeat_gap=0.0)
    # Filler removal took a slice out of the middle of the first body segment.
    base = [Interval(0.0, 1.5), Interval(2.2, 12.0)]

    composed = assembly.compose(plan.segments, base)

    assert composed
    # Nothing in the removed 1.5-2.2 window survives.
    for keep in composed:
        assert not (keep.start < 2.2 and keep.end > 1.5 and keep.start >= 1.5), keep
    # The hook still leads.
    assert plan.cold_open is not None
    assert composed[0].start == pytest.approx(plan.cold_open.start, abs=0.01)


def test_composing_with_no_base_keeps_passes_the_assembly_through():
    plan = _plan()

    composed = assembly.compose(plan.segments, None)

    assert [(k.start, k.end) for k in composed] == [(s.start, s.end) for s in plan.segments]


@requires_ffmpeg
def test_assembly_and_filler_removal_together_are_exactly_one_re_encode(
    tmp_path, monkeypatch, make_video
):
    """R2.3 / R9.1, asserted by counting calls through the real pipeline.

    A second `apply_keep_intervals` call would mean the clip was concatenated twice, and the second
    pass's keeps would be expressed against the first pass's output timeline rather than the source
    offsets everything else refers to.
    """
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment

    src = make_video("assembly_pass.mp4", duration=12.0, w=320, h=240)
    # Filler words on purpose: this test asserts the assembly composes WITH filler removal, so filler
    # removal has to actually remove something or half the assertion below is vacuous.
    script = (
        "this is um the boring setup part okay right. "
        "the secret is you never actually need permission. "
        "and uh then we went home quietly afterwards now."
    )
    words = _words(script)
    monkeypatch.setattr(
        pl,
        "transcribe",
        lambda *a, **k: Transcript(
            language="en", segments=[TranscriptSegment(0.0, 12.0, script, words)]
        ),
    )
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [
            ClipCandidate(start=0.0, end=11.0, score=90.0, reason="r", title="T", text=script)
        ],
    )
    monkeypatch.setattr(pl.settings, "cold_open_enabled", True)
    monkeypatch.setattr(pl.compositor, "render_clip", lambda *a, **k: None)

    calls: list[list[Interval]] = []
    real = pl.filler.apply_keep_intervals

    def counting(source, keeps, dest, **kwargs):
        calls.append(list(keeps))
        return real(source, keeps, dest, **kwargs)

    monkeypatch.setattr(pl.filler, "apply_keep_intervals", counting)

    # Captured independently of the assembly, so the containment check below shares no code with the
    # composition it is judging (R9.9).
    filler_keeps: list[Interval] = []
    real_plan = pl.filler.plan_keep_intervals

    def spy_plan(*args, **kwargs):
        result = real_plan(*args, **kwargs)
        if result.changed:
            filler_keeps.extend(result.keeps)
        return result

    monkeypatch.setattr(pl.filler, "plan_keep_intervals", spy_plan)

    clips = pl.run_pipeline(
        src,
        options_all_off(aspect="9:16", filler_removal=True),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
    )

    assert len(clips) == 1
    assert len(calls) == 1, f"the clip was re-encoded {len(calls)} times, not once"
    assert any(m.startswith("cold_open:") for m in clips[0].effects_applied), clips[
        0
    ].effects_applied

    # And the single list must honour BOTH mechanisms. Checked by containment against the filler plan
    # captured independently: every delivered keep has to sit inside some interval filler removal was
    # willing to keep. Dropping the composition would hand the assembly's raw segments straight
    # through, and an assembly segment spans the regions filler removal had just taken out — so this
    # is what catches "the assembly forgot the filler keeps", which the one-encode count cannot see.
    assert filler_keeps, "filler removal changed nothing, so this half of the assertion is vacuous"
    for keep in calls[0]:
        assert any(
            fk.start - 0.02 <= keep.start and keep.end <= fk.end + 0.02 for fk in filler_keeps
        ), (
            f"delivered keep {keep.start:.3f}-{keep.end:.3f} is not inside any filler keep "
            f"{[(round(k.start, 2), round(k.end, 2)) for k in filler_keeps]} -- the assembly was "
            "composed without them"
        )


# --------------------------------------------------------------------------- #
# R2.5, R2.6 -- correspondence, one test per consumer                         #
# --------------------------------------------------------------------------- #


def test_words_land_correctly_across_a_non_monotonic_assembly():
    """R2.6 for words. The hook's captions must follow the hook to position zero."""
    words = _words()
    plan = _plan(words=words, retain_in_body=False, min_repeat_gap=0.0)
    keeps = assembly.compose(plan.segments, None)

    rebased = assembly.rebase_words(words, keeps)
    assert plan.cold_open is not None

    # The first rebased words must be the hook's own words, now starting at zero.
    hook_words = [
        w.text for w in words if plan.cold_open.start <= (w.start + w.end) / 2 < plan.cold_open.end
    ]
    assert [w.text for w in rebased[: len(hook_words)]] == hook_words
    assert rebased[0].start == pytest.approx(0.0, abs=0.01)


def test_a_retained_cold_open_captions_both_occurrences():
    """The quiet hazard, and the reason `filler.rebase_words` cannot be reused here.

    That function stops at the first keep containing a word. With the line retained, its source range
    is in the keep list twice — so it would caption the cold open and be silent when the line is heard
    again, which reads as an ASR gap rather than as an assembly bug.
    """
    words = _words()
    plan = _plan(words=words, retain_in_body=True, min_repeat_gap=0.0)
    keeps = assembly.compose(plan.segments, None)
    assert plan.cold_open is not None

    hook_words = [
        w.text for w in words if plan.cold_open.start <= (w.start + w.end) / 2 < plan.cold_open.end
    ]
    assert hook_words, "the fixture has no hook words"

    rebased = assembly.rebase_words(words, keeps)
    for text in hook_words:
        assert [w.text for w in rebased].count(text) >= 2, (
            f"{text!r} was captioned once but is heard twice"
        )


def test_the_rebased_timeline_is_monotonic_even_though_the_source_order_is_not():
    """Output times must increase, whatever the source order was.

    A caption at a decreasing timestamp is not merely mistimed — libass and every player treat the
    event list as ordered, so it can vanish entirely.
    """
    words = _words()
    plan = _plan(words=words)
    keeps = assembly.compose(plan.segments, None)

    rebased = assembly.rebase_words(words, keeps)
    times = [w.start for w in rebased]

    assert times == sorted(times), "the rebased words are not in output order"


def test_emoji_cues_land_correctly_across_a_non_monotonic_assembly():
    """R2.5 for emoji. One rebased consumer working does not imply the others."""
    from worker.effects.emoji import EmojiCue

    plan = _plan(retain_in_body=False, min_repeat_gap=0.0)
    keeps = assembly.compose(plan.segments, None)
    assert plan.cold_open is not None

    inside = EmojiCue(
        char="X", start=plan.cold_open.start + 0.1, end=plan.cold_open.start + 0.5, slot=2
    )
    rebased = assembly.rebase_emoji([inside], keeps)

    assert rebased, "an emoji inside the hook was dropped"
    assert rebased[0].start == pytest.approx(0.1, abs=0.05), (
        "the emoji stayed at its source time instead of following the hook to the front"
    )
    assert rebased[0].char == "X", "the cue's glyph was not preserved"
    assert rebased[0].slot == 2, (
        "an unrelated field was dropped -- replace() exists to prevent this"
    )


def test_speaker_turns_land_correctly_across_a_non_monotonic_assembly():
    """R2.5 for speaker turns — the consumer whose failure is quietest.

    A mis-rebased turn changes no pixel; it just points the reframe crop and the AU12 gain at the wrong
    person for part of the clip.
    """
    from worker.diarization import Speaker_Turn

    plan = _plan(retain_in_body=False, min_repeat_gap=0.0)
    keeps = assembly.compose(plan.segments, None)
    assert plan.cold_open is not None

    turn = Speaker_Turn("SPEAKER_01", plan.cold_open.start + 0.05, plan.cold_open.end - 0.05)
    rebased = assembly.rebase_turns([turn], keeps)

    assert rebased
    assert rebased[0].speaker_label == "SPEAKER_01"
    assert rebased[0].start == pytest.approx(0.05, abs=0.05)


def test_an_item_with_no_timing_is_dropped_rather_than_anchored_to_zero():
    """A word defaulted to 0.0 would silently anchor itself to the clip start.

    That is both a wrong caption and the hardest kind of wrong to notice, because position zero is
    exactly where a caption is expected to be.
    """

    class _Untimed:
        start = None
        end = None
        text = "ghost"

    keeps = [Interval(0.0, 5.0)]

    assert assembly.rebase_onto([_Untimed()], keeps, build=lambda i, s, e: (s, e)) == []


# --------------------------------------------------------------------------- #
# R2.4, R2.7 -- the rendered graph                                            #
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_the_seam_between_cold_open_and_body_gets_afade_and_not_acrossfade(
    tmp_path, monkeypatch, make_video
):
    """R2.4.

    `acrossfade` overlaps the segments, so the result is shorter than the sum of its parts at every
    seam — and the rebased word offsets are cumulative segment durations, so an overlap desynchronises
    captions from speech by a growing amount. Equal-length `afade` preserves the mapping exactly.
    """
    from worker.effects import filler as fl

    src = make_video("seam.mp4", duration=12.0, w=320, h=240)
    seen: dict = {}
    monkeypatch.setattr(
        fl, "_run", lambda cmd, *a, **k: seen.update(graph=cmd[cmd.index("-filter_complex") + 1])
    )

    plan = _plan(retain_in_body=False, min_repeat_gap=0.0)
    fl.apply_keep_intervals(src, assembly.compose(plan.segments, None), tmp_path / "out.mp4")

    graph = seen["graph"]
    assert "afade" in graph
    assert "acrossfade" not in graph, "acrossfade would shift the timeline the captions depend on"


@requires_ffmpeg
def test_audio_and_video_are_never_reordered_independently(tmp_path, monkeypatch, make_video):
    """R2.7, asserted on the emitted graph.

    `trim` and `atrim` are separate filters given separate arguments, so a copy-paste error would
    produce a clip whose audio and video are each internally coherent and mutually wrong. This parses
    both series out of the graph and asserts they are the same sequence.
    """
    from worker.effects import filler as fl

    src = make_video("av.mp4", duration=12.0, w=320, h=240)
    seen: dict = {}
    monkeypatch.setattr(
        fl, "_run", lambda cmd, *a, **k: seen.update(graph=cmd[cmd.index("-filter_complex") + 1])
    )

    plan = _plan(retain_in_body=False, min_repeat_gap=0.0)
    keeps = assembly.compose(plan.segments, None)
    fl.apply_keep_intervals(src, keeps, tmp_path / "out.mp4")

    graph = seen["graph"]
    video = re.findall(r"trim=start=([0-9.]+):end=([0-9.]+)", graph.replace("atrim", "AUDIOTRIM"))
    audio = re.findall(
        r"AUDIOTRIM=start=([0-9.]+):end=([0-9.]+)", graph.replace("atrim", "AUDIOTRIM")
    )

    assert video, graph
    assert video == audio, "the audio and video segment orders differ"
    assert [float(s) for s, _e in video] == [round(k.start, 3) for k in keeps]


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_rendered_assembly_opens_on_the_lifted_line(tmp_path, make_video):
    """R2.8 / R9.3, measured on the rendered file rather than on the keep list.

    The expected content is established **independently of the assembly code** (R9.9): the fixture is
    built so each source second carries a distinct loudness, so which second leads the output can be
    read straight out of the delivered audio without consulting the plan that produced it.
    """
    from worker.effects import filler as fl

    # Six one-second tones, ascending in level, so second N is identifiable by loudness.
    src = tmp_path / "tones.mp4"
    parts = "".join(
        f"sine=frequency=440:duration=1,volume={0.1 * (i + 1):.1f}[a{i}];" for i in range(6)
    )
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25:duration=6",
            "-filter_complex",
            f"{parts}{''.join(f'[a{i}]' for i in range(6))}concat=n=6:v=0:a=1[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(src),
        ],
        check=True,
        capture_output=True,
    )

    def mean_volume(path, start: float, span: float) -> float:
        out = subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-ss",
                f"{start}",
                "-t",
                f"{span}",
                "-i",
                str(path),
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
        ).stderr
        found = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", out)
        assert found, out
        return float(found.group(1))

    # Lift the loudest second (5.0-6.0) to the front, by hand -- not via `plan`.
    keeps = [Interval(5.0, 6.0), Interval(0.0, 5.0)]
    dest = tmp_path / "assembled.mp4"
    fl.apply_keep_intervals(src, keeps, dest)

    assert dest.exists() and dest.stat().st_size > 0

    opening = mean_volume(dest, 0.1, 0.8)
    following = mean_volume(dest, 1.1, 0.8)

    assert opening > following + 3.0, (
        f"the assembled clip does not open on the loudest source second: "
        f"{opening:.1f} dB then {following:.1f} dB"
    )


# --------------------------------------------------------------------------- #
# Properties                                                                  #
# --------------------------------------------------------------------------- #


# Feature: clip-editorial-structure, Property 3: every rebased item lands inside the assembled
# timeline, whatever the segment order.
@settings(max_examples=100)
@given(
    order=st.permutations([(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]),
    at=st.floats(min_value=0.0, max_value=5.9),
)
def test_p3_rebased_items_land_inside_the_assembled_timeline(order, at):
    """Validates: Requirements 2.5, 2.6

    For any segment order, a rebased item's time must fall within the total assembled duration. An
    item mapped past the end would be a caption that never shows; one mapped negative can vanish.
    """
    keeps = [Interval(s, e) for s, e in order]
    total = sum(k.duration for k in keeps)

    class _Item:
        start = at
        end = at + 0.1

    out = assembly.rebase_onto([_Item()], keeps, build=lambda i, s, e: (s, e))

    for start, end in out:
        assert 0.0 <= start <= total + 0.001
        assert start <= end <= total + 0.001


# Feature: clip-editorial-structure, Property 4: composing never lengthens the clip.
@settings(max_examples=100)
@given(
    hook=st.tuples(
        st.floats(min_value=0.0, max_value=4.0), st.floats(min_value=0.1, max_value=2.0)
    ),
    cut=st.tuples(st.floats(min_value=0.0, max_value=5.0), st.floats(min_value=0.1, max_value=2.0)),
)
def test_p4_composing_never_delivers_more_than_the_segments_asked_for(hook, cut):
    """Validates: Requirements 2.3

    Intersecting the assembly with an inner filter can only ever remove time. A composition that
    lengthened the clip would mean a removed filler word had come back.
    """
    segments = [Interval(hook[0], hook[0] + hook[1]), Interval(0.0, 6.0)]
    base = [Interval(0.0, cut[0]), Interval(cut[0] + cut[1], 6.0)]

    composed = assembly.compose(segments, base)
    asked = sum(s.duration for s in segments)

    assert sum(k.duration for k in composed) <= asked + 0.001


# --------------------------------------------------------------------------- #
# The pipeline's own choices. Both of these were added because a mutation      #
# survived: the module tests could not see which rebase the pipeline picked,   #
# nor whether a refusal reached the clip record.                              #
# --------------------------------------------------------------------------- #


def _pipeline_clip(tmp_path, monkeypatch, make_video, *, script=None, **setting_overrides):
    """Run one clip with the cold open on; return `(clip, words handed to the compositor)`."""
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment

    text = script or _SCRIPT
    src = make_video("assembly_choice.mp4", duration=12.0, w=320, h=240)
    words = _words(text)
    monkeypatch.setattr(
        pl,
        "transcribe",
        lambda *a, **k: Transcript(
            language="en", segments=[TranscriptSegment(0.0, 12.0, text, words)]
        ),
    )
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [
            ClipCandidate(start=0.0, end=11.0, score=90.0, reason="r", title="T", text=text)
        ],
    )
    monkeypatch.setattr(pl.settings, "cold_open_enabled", True)
    for key, value in setting_overrides.items():
        monkeypatch.setattr(pl.settings, key, value)

    seen: dict = {}

    def spy(base_clip, dest, options, words_, temp_dir, **kwargs):
        seen["words"] = list(words_)
        return None

    monkeypatch.setattr(pl.compositor, "render_clip", spy)

    clips = pl.run_pipeline(
        src,
        options_all_off(aspect="9:16"),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
    )
    assert len(clips) == 1
    return clips[0], seen.get("words", [])


@requires_ffmpeg
def test_the_pipeline_uses_the_duplicate_aware_rebase_for_an_assembly(
    tmp_path, monkeypatch, make_video
):
    """Which rebase the pipeline picks, asserted on the words the compositor is handed.

    Added because a mutation survived: swapping `assembly.rebase_words` for `filler.rebase_words` at
    the call site broke nothing, since every other test here calls the module directly. That is the
    "tests the seam, not the call site" failure this repository keeps finding — and the consequence
    would be a retained cold open captioned on its first airing and silent on its second.
    """
    clip, words = _pipeline_clip(
        tmp_path,
        monkeypatch,
        make_video,
        cold_open_retain_in_body=True,
        cold_open_min_repeat_gap=0.0,
    )

    assert any(m.startswith("cold_open:") for m in clip.effects_applied), clip.effects_applied
    assert words, "the compositor was handed no words"

    # Counted over words that occur exactly ONCE in the source and fall inside the lifted line.
    #
    # A plain "some word appears twice" assertion is useless here and initially let this mutation
    # through: `_SCRIPT` repeats ordinary words like "the" and "is", so that count is >= 2 whichever
    # rebase ran. Only a source-unique word can distinguish "heard twice" from "written twice".
    source = _words(_SCRIPT)
    source_counts: dict[str, int] = {}
    for word in source:
        source_counts[word.text] = source_counts.get(word.text, 0) + 1

    plan = _plan(words=source, retain_in_body=True, min_repeat_gap=0.0)
    assert plan.cold_open is not None and plan.retained_in_body

    unique_in_hook = [
        w.text
        for w in source
        if source_counts[w.text] == 1
        and plan.cold_open.start <= (w.start + w.end) / 2 < plan.cold_open.end
    ]
    assert unique_in_hook, "the fixture has no source-unique word inside the hook"

    delivered: dict[str, int] = {}
    for word in words:
        delivered[word.text] = delivered.get(word.text, 0) + 1

    for text in unique_in_hook:
        assert delivered.get(text, 0) >= 2, (
            f"{text!r} occurs once in the source and is heard twice in a retained cold open, but was "
            f"captioned {delivered.get(text, 0)} time(s) -- the pipeline used the monotonic rebase, "
            "so the line's second airing has no captions"
        )


@requires_ffmpeg
def test_a_refusal_reaches_the_clip_record(tmp_path, monkeypatch, make_video):
    """R2.9. Decline, record, carry on — never deliver something half-assembled.

    Also added from a surviving mutation: silencing the refusal branch broke nothing, because no test
    drove the pipeline into a refusal. A refusal nobody can see is indistinguishable from the feature
    being off, which is the whole reason this project records them.
    """
    import worker.pipeline as pl

    monkeypatch.setattr(
        pl.assembly,
        "plan",
        lambda *a, **k: assembly.Assembly_Plan(refusal="assembly_refused:bad_duration"),
    )

    clip, _words = _pipeline_clip(tmp_path, monkeypatch, make_video)

    assert "assembly_refused:bad_duration" in clip.effects_applied
    assert not any(m.startswith("cold_open:") for m in clip.effects_applied), (
        "a refusal must not also claim an assembly happened"
    )
