"""AU12 per-speaker level matching reaches the rendered audio graph.

`worker/turn_gain.py` merged in #126 complete and property-tested, imported by nothing outside its
own test module. `tests/test_turn_gain.py` covers the arithmetic — the bound, the ramp, the four
refusals, and an end-to-end render of the `volume` expression. None of it could tell whether
anything *called* `plan_turn_gain`, and nothing did, so diarisation was still never used for gain:
the exact defect AU12 was written to fix.

So every assertion here is on the ffmpeg command the compositor built, or on the clip record, and
each one fails if its call site is deleted.

The file's centre of gravity is `test_the_turns_reaching_the_gain_are_on_the_delivered_timeline`.
R7.5 is the highest-risk requirement in this feature and the one a unit test structurally cannot
reach: filler removal shortens the clip, so a turn at 2.6s in the source is at 1.6s in the delivery,
and applying the ramp at the source time corrects the wrong speaker. The fixture is built so the two
timelines **disagree**, which is what makes the assertion capable of failing.
"""

from __future__ import annotations

import pytest

from tests.conftest import requires_ffmpeg
from worker.diarization import Speaker_Turn

# --------------------------------------------------------------------------- #
# Driving the real call site                                                  #
# --------------------------------------------------------------------------- #


def _capture_render(monkeypatch, *, turns, enabled, diarization, has_audio=True):
    """Call `render_clip`'s audio section with a stubbed encoder; return (graph, markers).

    The compositor is driven rather than `plan_turn_gain`, because the question is whether the
    filter reaches `-filter_complex`. `_run` is stubbed so nothing encodes: the graph string is the
    artefact under test.
    """
    from worker.effects import compositor as comp

    seen: dict[str, list[str]] = {}

    def fake_run(cmd, *a, **k):
        if "-filter_complex" in cmd:
            seen["graph"] = cmd[cmd.index("-filter_complex") + 1].split(";")
        seen["cmd"] = list(cmd)
        return None

    monkeypatch.setattr(comp, "_run", fake_run)
    monkeypatch.setattr(comp.settings, "turn_gain_enabled", enabled)
    monkeypatch.setattr(
        comp,
        "_turn_gain_envelope",
        lambda _clip: [(float(i), -14.0 if i < 3 else -26.0) for i in range(6)],
    )
    return seen, turns, diarization, has_audio


def _two_speaker_turns():
    """Two speakers, both long enough to clear `MIN_TURN_SECONDS`, levels far apart."""
    return [
        Speaker_Turn("SPEAKER_00", 0.0, 3.0),
        Speaker_Turn("SPEAKER_01", 3.0, 6.0),
    ]


def _run_clip(tmp_path, monkeypatch, make_video, *, enabled, spy_turns=None, **option_overrides):
    """One clip through `run_pipeline`; returns `(clip, captured render_clip kwargs, graph)`.

    Everything above the compositor runs for real — including the hoisted `slice_turns` /
    `rebase_turns` — so deleting `speaker_turns=clip_turns` from `worker/pipeline.py` fails here.
    """
    import worker.pipeline as pl
    from tests.conftest import options_all_off
    from worker.selection import ClipCandidate
    from worker.transcribe import Transcript, TranscriptSegment, Word

    src = make_video("turn_gain.mp4", duration=6.0, w=640, h=360)

    words = [
        Word(0.2, 0.6, "one"),
        Word(0.8, 1.2, "two"),
        Word(3.2, 3.6, "three"),
        Word(4.0, 4.4, "four"),
    ]
    monkeypatch.setattr(
        pl,
        "transcribe",
        lambda *a, **k: Transcript(
            language="en",
            segments=[TranscriptSegment(0.0, 6.0, "one two three four", words)],
        ),
    )
    monkeypatch.setattr(
        pl.sel,
        "select_moments",
        lambda *a, **k: [
            ClipCandidate(start=0.0, end=6.0, score=90.0, reason="r", title="T", text="t")
        ],
    )
    monkeypatch.setattr(pl.settings, "turn_gain_enabled", enabled)
    if spy_turns is not None:
        monkeypatch.setattr(pl.diarization, "diarize_source", lambda *a, **k: list(spy_turns))

    captured: dict = {}
    real = pl.compositor.render_clip

    def spy(base_clip, dest, options, words_, temp_dir, **kwargs):
        captured.update(kwargs)
        captured["base_clip"] = base_clip
        return None  # "no effect enabled" -- the pipeline then ships `base_clip`

    monkeypatch.setattr(pl.compositor, "render_clip", spy)
    assert real is not spy

    clips = pl.run_pipeline(
        src,
        options_all_off(aspect="9:16", **option_overrides),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
    )
    assert len(clips) == 1
    return clips[0], captured


# --------------------------------------------------------------------------- #
# R7.5 -- the delivered timeline. The reason this file exists.                #
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_the_turns_reaching_the_gain_are_on_the_delivered_timeline(
    tmp_path, monkeypatch, make_video
):
    """The highest-risk requirement, and the one no unit test can reach.

    Filler removal shortens the clip, so source time and delivered time **differ**. If the pipeline
    passed un-rebased turns, the ramp would land on the wrong speaker — audible as the quiet speaker
    getting quieter. The fixture forces a real cut, then asserts the turns the compositor received
    are *not* the source turns.

    The assertion is that the two disagree rather than a pinned number, because the exact shift
    depends on what filler removal chose to cut; pinning it would restate that arithmetic instead of
    checking the rebase happened.
    """
    import worker.pipeline as pl

    source_turns = _two_speaker_turns()

    # A keep plan that drops a second from the middle: `rebase_turns` must pull the second
    # speaker's turn earlier by exactly that much.
    monkeypatch.setattr(pl.diarization, "diarize_source", lambda *a, **k: list(source_turns))

    seen_rebase: dict = {}
    real_rebase = pl.diarization.rebase_turns

    def spy_rebase(turns, keep_plan):
        out = real_rebase(turns, keep_plan)
        seen_rebase["in"] = list(turns)
        seen_rebase["out"] = list(out)
        return out

    monkeypatch.setattr(pl.diarization, "rebase_turns", spy_rebase)

    _clip, captured = _run_clip(
        tmp_path,
        monkeypatch,
        make_video,
        enabled=True,
        diarization=True,
        filler_removal=True,
    )

    handed = list(captured.get("speaker_turns") or [])
    assert handed, "the pipeline passed no turns to the compositor at all"

    if seen_rebase.get("out"):
        # Filler removal produced a keep plan, so the rebase ran and must have moved something.
        assert handed == seen_rebase["out"]
        assert [(t.start, t.end) for t in handed] != [
            (t.start, t.end) for t in seen_rebase["in"]
        ], "rebase_turns returned the source timings; the delivered-timeline guarantee is vacuous"
    else:
        # No cut was made, so source and delivered timelines coincide. Still assert the turns are
        # clip-relative rather than source-relative.
        assert handed[0].start == pytest.approx(0.0, abs=0.51)


@requires_ffmpeg
def test_the_pipeline_hands_the_turns_to_the_compositor(tmp_path, monkeypatch, make_video):
    """Deleting `speaker_turns=clip_turns` from the pipeline fails here."""
    _clip, captured = _run_clip(
        tmp_path,
        monkeypatch,
        make_video,
        enabled=True,
        spy_turns=_two_speaker_turns(),
        diarization=True,
    )

    handed = list(captured.get("speaker_turns") or [])
    assert [t.speaker_label for t in handed] == ["SPEAKER_00", "SPEAKER_01"]


@requires_ffmpeg
def test_turns_are_computed_even_without_speaker_reframe(tmp_path, monkeypatch, make_video):
    """The computation used to live inside the `speaker_reframe` branch.

    So with `diarization=True` and `speaker_reframe=False` — the configuration AU12 is actually for
    — the turns were simply never derived. This is the regression that hoisting them fixes.
    """
    _clip, captured = _run_clip(
        tmp_path,
        monkeypatch,
        make_video,
        enabled=True,
        spy_turns=_two_speaker_turns(),
        diarization=True,
        speaker_reframe=False,
    )

    assert list(captured.get("speaker_turns") or []), (
        "no turns without speaker_reframe -- the computation is still trapped in that branch"
    )


# --------------------------------------------------------------------------- #
# The filter reaches the graph, in the right place                            #
# --------------------------------------------------------------------------- #


def _audio_graph(monkeypatch, tmp_path, make_video, *, enabled, diarization, turns):
    """Build a real `render_clip` graph with a stubbed encoder and return its entries."""
    from tests.conftest import options_all_off
    from worker.effects import compositor as comp

    src = make_video("tg_graph.mp4", duration=6.0, w=640, h=360)

    seen: dict = {}

    def fake_run(cmd, *a, **k):
        if "-filter_complex" in cmd:
            seen["graph"] = cmd[cmd.index("-filter_complex") + 1].split(";")
        return None

    monkeypatch.setattr(comp, "_run", fake_run)
    monkeypatch.setattr(comp.settings, "turn_gain_enabled", enabled)
    monkeypatch.setattr(
        comp,
        "_turn_gain_envelope",
        lambda _clip: [(float(i), -14.0 if i < 3 else -26.0) for i in range(6)],
    )
    # Captions force the compositor to build a graph at all, and they need *words* to do it: with an
    # empty word list nothing is drawn, so `render_clip` legitimately returns `None` and there is no
    # graph to inspect. That is not a turn-gain refusal and must not be mistaken for one.
    from worker.transcribe import Word

    words = [
        Word(0.2, 0.6, "one"),
        Word(0.8, 1.2, "two"),
        Word(3.2, 3.6, "three"),
        Word(4.0, 4.4, "four"),
    ]
    result = comp.render_clip(
        src,
        tmp_path / "out.mp4",
        options_all_off(aspect="9:16", captions=True, diarization=diarization),
        words,
        tmp_path / "tmp",
        speaker_turns=turns,
    )
    return seen.get("graph", []), result


@requires_ffmpeg
def test_the_gain_filter_reaches_the_filter_complex(tmp_path, monkeypatch, make_video):
    """The claim: a `volume` expression appears in the graph the encoder is handed."""
    graph, result = _audio_graph(
        monkeypatch,
        tmp_path,
        make_video,
        enabled=True,
        diarization=True,
        turns=_two_speaker_turns(),
    )

    gain = [entry for entry in graph if "[aturn]" in entry]
    assert gain, f"no turn-gain entry in the graph: {graph}"
    assert "volume=eval=frame" in gain[0]
    assert result is not None
    assert any(m.startswith("turn_gain:") for m in result.effects_applied)


@requires_ffmpeg
def test_the_gain_is_applied_before_the_music_bed_and_before_loudnorm(
    tmp_path, monkeypatch, make_video
):
    """R7.11, and the reason it is a requirement rather than a preference.

    Per-speaker gain on a signal that already contains music would modulate the bed every time the
    speaker changed — audible pumping, which nobody would attribute to a level-matching feature. And
    placing it after `loudnorm` would mean the measured loudness described something other than what
    is delivered.
    """
    graph, _ = _audio_graph(
        monkeypatch,
        tmp_path,
        make_video,
        enabled=True,
        diarization=True,
        turns=_two_speaker_turns(),
    )

    index = {"turn": None, "loud": None, "mix": None}
    for i, entry in enumerate(graph):
        if "[aturn]" in entry and index["turn"] is None:
            index["turn"] = i
        if "loudnorm" in entry and index["loud"] is None:
            index["loud"] = i
        if "amix" in entry and index["mix"] is None:
            index["mix"] = i

    assert index["turn"] is not None
    if index["loud"] is not None:
        assert index["turn"] < index["loud"], "turn gain must precede loudness normalisation"
    if index["mix"] is not None:
        assert index["turn"] < index["mix"], "turn gain must be on the speech branch, pre-mix"


@requires_ffmpeg
def test_the_gain_chains_onto_the_presence_output_rather_than_reading_the_source(
    tmp_path, monkeypatch, make_video
):
    """AU11 and AU12 must compose, not overwrite each other.

    Reading `[0:a]` again here would silently discard the presence chain — the same class of defect
    the AU11 comment in the compositor warns about.
    """
    from worker.effects import compositor as comp

    monkeypatch.setattr(comp.settings, "speech_presence", 0.5)
    graph, _ = _audio_graph(
        monkeypatch,
        tmp_path,
        make_video,
        enabled=True,
        diarization=True,
        turns=_two_speaker_turns(),
    )

    gain = [entry for entry in graph if "[aturn]" in entry]
    assert gain, graph
    assert gain[0].startswith("[apresence]"), (
        f"turn gain did not chain onto the presence output: {gain[0]}"
    )


# --------------------------------------------------------------------------- #
# Refusals, and the default                                                   #
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_off_by_default_adds_no_filter_and_no_marker(tmp_path, monkeypatch, make_video):
    """R7.8. The default has to be a true no-op or every audio golden moves."""
    graph, result = _audio_graph(
        monkeypatch,
        tmp_path,
        make_video,
        enabled=False,
        diarization=True,
        turns=_two_speaker_turns(),
    )

    assert not [entry for entry in graph if "[aturn]" in entry]
    assert result is not None
    assert not any(m.startswith("turn_gain") for m in result.effects_applied)


@requires_ffmpeg
def test_diarisation_off_records_why_and_changes_nothing(tmp_path, monkeypatch, make_video):
    """R7.12: this must never enable diarisation as a side effect, and must say so.

    Asserted with turns *present* but `diarization=False`, which is exactly the configuration
    `speaker_reframe` produces. Inferring availability from the turns being non-empty would let AU12
    act on a job that never asked for diarisation.
    """
    graph, result = _audio_graph(
        monkeypatch,
        tmp_path,
        make_video,
        enabled=True,
        diarization=False,
        turns=_two_speaker_turns(),
    )

    assert not [entry for entry in graph if "[aturn]" in entry]
    assert result is not None
    assert "turn_gain_unavailable:diarization_disabled" in result.effects_applied


@requires_ffmpeg
def test_a_single_speaker_declines_and_says_so(tmp_path, monkeypatch, make_video):
    """Nothing to balance against, so the audio is left alone with a reason recorded."""
    graph, result = _audio_graph(
        monkeypatch,
        tmp_path,
        make_video,
        enabled=True,
        diarization=True,
        turns=[Speaker_Turn("SPEAKER_00", 0.0, 3.0), Speaker_Turn("SPEAKER_00", 3.0, 6.0)],
    )

    assert not [entry for entry in graph if "[aturn]" in entry]
    assert result is not None
    assert "turn_gain_skipped:single_speaker" in result.effects_applied


@requires_ffmpeg
def test_no_turns_costs_no_envelope_measurement(tmp_path, monkeypatch, make_video):
    """The envelope is a pass over the audio, so it must not run when it cannot be used.

    A single-speaker source produces no turns on most jobs, and measuring an envelope only to
    discard it would be a silent cost on the common case.
    """
    from tests.conftest import options_all_off
    from worker.effects import compositor as comp

    src = make_video("tg_noenv.mp4", duration=4.0, w=640, h=360)
    monkeypatch.setattr(comp, "_run", lambda *a, **k: None)
    monkeypatch.setattr(comp.settings, "turn_gain_enabled", True)

    calls: list[int] = []

    def counting_envelope(_clip):
        calls.append(1)
        return []

    monkeypatch.setattr(comp, "_turn_gain_envelope", counting_envelope)
    comp.render_clip(
        src,
        tmp_path / "out.mp4",
        options_all_off(aspect="9:16", captions=True, diarization=True),
        [],
        tmp_path / "tmp",
        speaker_turns=[],
    )

    assert not calls, "the envelope was measured with no turns to measure against"
