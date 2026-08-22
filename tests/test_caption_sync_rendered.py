"""Captions on an actually-rendered clip land on the audio (M10, end to end).

Every other M10 test feeds `measure_alignment` synthetic labels and synthetic events, which proves
the *instrument* works and proves nothing about the pipeline. Nothing measured a real render. That
gap is why a reported desync could only be argued about: `tests/test_transcript_trim.py::
test_captions_follow_the_cut` spies on `rebase_words` and asserts a rebased word *number*, so it
would still pass if the number were right and the rendered media were shifted underneath it.

So this renders a clip with ffmpeg and measures the emitted cue times against **the audio of the
finished file**. No ASR anywhere: the source is built with sound in known places, so the truth is
constructed rather than transcribed, and the measurement cannot become circular by consulting the
same ASR that produced the caption times.

The window deliberately starts at 4 s, not 0. A missing or doubled clip-start subtraction in
`slice_words` is invisible at zero and is exactly the defect this exists to catch.
"""

from __future__ import annotations

import subprocess

import pytest

from evaluation.caption_timing import (
    ENVELOPE_HOP_S,
    Rendered_Event,
    best_fit_lag_ms,
    coverage_overlap,
    speech_mask,
)
from tests.conftest import options_all_off, requires_ffmpeg

#: Sound is on for the first second of every two, so onsets sit at 0, 2, 4, 6, ... seconds.
#: `lt(mod(t,2),1)` gates a tone rather than concatenating files, so the timing is exact by
#: construction and there is no encoder seam to argue about.
BURST_PERIOD_S = 2.0
SOURCE_SECONDS = 14.0
WINDOW_START = 4.0
WINDOW_END = 12.0


class _Timed:
    """The minimum a word needs to be measurable: a start and an end.

    Deliberately not `worker.transcribe.Word`. These tests measure the instrument, and building the
    fixtures from the production type would let a change in that type quietly change what is being
    tested.
    """

    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end


def _burst_source(dest, seconds=SOURCE_SECONDS):
    """A video whose audio is a 1-s tone every 2 s, silent in between."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=640x360:rate=30:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc=sin(2*PI*330*t)*lt(mod(t\\,{BURST_PERIOD_S})\\,1):d={seconds}:s=48000",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def _transcript_on_the_bursts():
    """One word per burst, timed to it, in absolute source time.

    `Transcript.words` and `.text` are derived from the segments, so the words go in the segment
    and nowhere else — building the two independently is how a fixture ends up disagreeing with
    itself.
    """
    from worker.transcribe import Transcript, TranscriptSegment, Word

    words = [
        Word(start=float(t), end=float(t) + 0.8, text=f"burst{index}")
        for index, t in enumerate(range(0, int(SOURCE_SECONDS), int(BURST_PERIOD_S)))
    ]
    segment = TranscriptSegment(
        start=0.0,
        end=SOURCE_SECONDS,
        text=" ".join(w.text for w in words),
        words=words,
    )
    return Transcript(language="en", segments=[segment])


@pytest.fixture
def rendered_clip(tmp_path, monkeypatch):
    """Render one clip from a non-zero window, capturing the cues handed to the ASS emitter."""
    import worker.captions as cap
    import worker.pipeline as pl
    from worker.selection import ClipCandidate

    source = _burst_source(tmp_path / "bursts.mp4")
    transcript = _transcript_on_the_bursts()
    monkeypatch.setattr(pl, "transcribe", lambda *_a, **_kw: transcript)

    captured: list = []
    real_build_ass = cap.build_ass

    def spy(cues, dest, *args, **kwargs):
        captured.append(list(cues))
        return real_build_ass(cues, dest, *args, **kwargs)

    monkeypatch.setattr(cap, "build_ass", spy)

    clips = pl.run_pipeline(
        source,
        options_all_off(captions=True, metadata=False, aspect="9:16"),
        clips_dir=tmp_path / "clips",
        temp_dir=tmp_path / "tmp",
        explicit_candidates=[
            ClipCandidate(start=WINDOW_START, end=WINDOW_END, reason="t", text="x", cuts=[])
        ],
    )
    assert clips, "the pipeline rendered nothing"
    assert captured, "build_ass was never called, so no captions were burned in"
    media = tmp_path / "clips" / clips[0].filename
    assert media.is_file()
    events = [
        Rendered_Event(start=c.start, end=c.end, text=" ".join(w.text for w in c.words))
        for c in captured[0]
    ]
    return media, events


@requires_ffmpeg
def test_the_burned_in_cues_land_on_the_audio(rendered_clip):
    """The measurement that was missing: cue times versus the rendered clip's own sound."""
    media, events = rendered_clip
    mask = speech_mask(media)
    assert any(mask), "the rendered clip has no audible audio; the fixture is broken"

    lag_ms, at_zero, at_lag = best_fit_lag_ms(events, mask)

    # A whole burst period of slack would let a badly shifted render pass, so the bound is a
    # fraction of it. The floor is the 20 ms envelope plus centisecond rounding in the ASS.
    assert abs(lag_ms) < 400, (
        f"captions best fit the audio {lag_ms:+.0f} ms off zero "
        f"(overlap {at_zero:.1%} at zero, {at_lag:.1%} at that lag) — "
        "a constant offset here means the clip-start subtraction or the rebase is wrong"
    )
    assert at_zero > 0.5, f"cues cover only {at_zero:.1%} of the speech; grouping or timing is off"


@requires_ffmpeg
def test_the_first_cue_starts_when_the_clip_starts_talking(rendered_clip):
    """The window opens on a burst, so the first cue belongs at the very top of the clip.

    Stated separately from the lag test because it is the one assertion that fails loudly for a
    *missing* offset subtraction: leave `slice_words` out and the first cue lands 4 s in.
    """
    _media, events = rendered_clip
    assert events, "no cues"
    assert events[0].start < 0.5, (
        f"the first cue starts at {events[0].start:.2f}s in a clip cut from "
        f"{WINDOW_START}s — the clip-start offset was not applied"
    )


@requires_ffmpeg
def test_an_injected_shift_is_detected(rendered_clip):
    """The guard has to be able to fail, or it is decoration.

    Shifts every cue by a whole burst period and asserts the measurement notices. Without this,
    a bug that made `speech_mask` return all-``True`` would make the test above pass forever.
    """
    media, events = rendered_clip
    mask = speech_mask(media)
    honest = coverage_overlap(events, mask)

    shifted = [
        Rendered_Event(start=e.start + BURST_PERIOD_S, end=e.end + BURST_PERIOD_S, text=e.text)
        for e in events
    ]
    lag_ms, at_zero, _at_lag = best_fit_lag_ms(shifted, mask)

    assert at_zero < honest, "shifting every cue did not reduce the overlap; the metric is blind"
    assert lag_ms < -BURST_PERIOD_S * 1000 * 0.5, (
        f"a deliberate +{BURST_PERIOD_S}s shift was measured as {lag_ms:+.0f} ms; "
        "the instrument cannot see a desync it is meant to catch"
    )


@requires_ffmpeg
def test_speech_mask_reads_the_bursts_out_of_the_source(tmp_path):
    """Sanity check on the fixture's own truth claim, so the tests above cannot pass vacuously."""
    source = _burst_source(tmp_path / "probe.mp4", seconds=6.0)
    mask = speech_mask(source)
    assert mask, "no envelope produced"

    # Sound in the first half of each 2 s period, silence in the second.
    def loud(at):
        return mask[int(at / ENVELOPE_HOP_S)]

    assert loud(0.5) and loud(2.5) and loud(4.5), "the tone bursts were not detected"
    assert not loud(1.5) and not loud(3.5), "the silent halves were detected as speech"


# --- Word timing measured only where the audio can prove it ---------------------------------


@requires_ffmpeg
def test_verifiable_errors_read_zero_on_known_true_times(tmp_path):
    """Words placed exactly on the bursts measure ~zero, and every burst qualifies.

    The bursts are separated by a full second of silence, so every word here is pause-preceded and
    the rising edge is truth rather than a guess.
    """
    from evaluation.caption_timing import verifiable_word_errors

    source = _burst_source(tmp_path / "truth.mp4", seconds=20.0)
    words = [_Timed(start=float(t), end=float(t) + 0.8) for t in range(0, 20, int(BURST_PERIOD_S))]

    errors, considered = verifiable_word_errors(words, source)

    assert considered >= 8, f"only {considered} words qualified; the fixture is too dense"
    assert errors, "nothing anchored despite a full second of silence before every word"
    assert max(abs(e) for e in errors) < 0.06, (
        f"known-true times measured up to {max(abs(e) for e in errors) * 1000:.0f} ms off"
    )


@requires_ffmpeg
def test_a_deliberate_lead_is_measured_with_the_right_sign(tmp_path):
    """Claiming a word starts 200 ms before the sound must read as +200 ms, not -200 ms.

    The sign convention is the whole point of the function: it decided whether a 100 ms
    "correction" would have been applied in the right direction or the wrong one.
    """
    from evaluation.caption_timing import verifiable_word_errors

    source = _burst_source(tmp_path / "early.mp4", seconds=20.0)
    early = [
        _Timed(start=float(t) - 0.2, end=float(t) + 0.6)
        for t in range(int(BURST_PERIOD_S), 20, int(BURST_PERIOD_S))
    ]

    errors, _considered = verifiable_word_errors(early, source)

    assert errors, "nothing anchored"
    median = sorted(errors)[len(errors) // 2]
    assert median > 0.12, (
        f"a 200 ms lead measured {median * 1000:+.0f} ms; a positive error must mean "
        "the sound starts after the word claims to"
    )


@requires_ffmpeg
def test_words_inside_continuous_speech_are_skipped_not_guessed(tmp_path):
    """The whole reason this function exists: no pause, no measurement.

    Continuous sound offers a rising edge only at the very start, so a metric that anchored every
    word to its nearest edge would return a number per word. This must return almost none, because
    inside unbroken speech there is no edge that belongs to a word. Reporting a confident number
    there is how 130 ms of pure noise was once mistaken for a defect.
    """
    from evaluation.caption_timing import verifiable_word_errors

    dest = tmp_path / "continuous.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=30:duration=12",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=sin(2*PI*330*t):d=12:s=48000",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )

    # Densely packed words with no gaps: nothing qualifies as pause-preceded.
    packed = [_Timed(start=i * 0.4, end=i * 0.4 + 0.39) for i in range(1, 28)]
    errors, considered = verifiable_word_errors(packed, dest)

    assert considered == 0, f"{considered} words claimed a verifiable onset inside unbroken sound"
    assert errors == [], "an error was reported for a word whose onset the audio cannot settle"
