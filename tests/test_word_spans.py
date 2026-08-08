"""Word-span hygiene (C23), and the measurement that rules T11 out.

Two properties matter most, and they pull against each other, which is why the ordering is asserted
rather than assumed: **spans must not overlap**, and **spans should be long enough to see**. When
they conflict, non-overlap wins — two words highlighted at once is a rendering fault, where one word
highlighted briefly is a legibility problem.

The T11 test at the bottom is a measurement, not a feature test: it records that the cached energy
envelope contains no word-rate information, so snapping word starts to its onsets is impossible as
R7.8 specifies.
"""

from __future__ import annotations

import pytest

from worker import word_spans as ws
from worker.transcribe import Word


def _w(start, end, text="x"):
    return Word(start=start, end=end, text=text)


# --- R8.7: already-compliant input is untouched ----------------------------------------------


def test_a_compliant_sequence_returns_the_same_objects():
    """R8.7. The strongest available form of bit-identical.

    Returning the inputs themselves rather than equal copies is what makes the disabled floor a true
    no-op — and if this ever regressed, every caption golden in the project would move.
    """
    spans = [_w(0.0, 0.4), _w(0.5, 0.9), _w(1.0, 1.4)]
    out, report = ws.apply_hygiene(spans)
    assert all(a is b for a, b in zip(out, spans, strict=True))
    assert report.altered == 0
    assert report.markers == []


def test_the_default_floor_is_off():
    """The mechanism ships on; the value waits for a preference trial."""
    assert ws.DEFAULT_MIN_SPAN_SECONDS == 0.0
    spans = [_w(0.0, 0.01), _w(0.5, 0.51)]
    out, report = ws.apply_hygiene(spans)
    assert all(a is b for a, b in zip(out, spans, strict=True))
    assert report.altered == 0


def test_an_empty_sequence_is_handled():
    out, report = ws.apply_hygiene([])
    assert out == []
    assert report.altered == 0


# --- R8.1: monotonic starts -----------------------------------------------------------------


def test_an_overlapping_pair_is_separated_without_presuming_which_side_moves():
    """R8.1/R8.2. Out of order, a karaoke fill sweeps *backwards*, which reads as a glitch.

    The assertion is non-overlap and that a repair was recorded — deliberately not *which* mechanism
    fired. Truncating the earlier span and advancing the later one are both valid fixes for the same
    fault, and an earlier version of this test asserted `reordered == 1` when the implementation
    legitimately chose to truncate instead. Pinning the mechanism would make the test an
    implementation mirror rather than a statement about the output.
    """
    out, report = ws.apply_hygiene([_w(0.0, 0.5), _w(0.2, 0.8)])
    assert out[1].start >= out[0].end
    assert report.altered >= 1
    assert report.markers


def test_ordering_is_preserved_across_a_long_repair():
    """The invariant, checked over a sequence with several faults at once."""
    spans = [_w(0.0, 0.9), _w(0.3, 0.4), _w(0.35, 1.5), _w(1.2, 1.3)]
    out, _ = ws.apply_hygiene(spans)
    for earlier, later in zip(out, out[1:], strict=False):
        assert later.start >= earlier.start
        assert later.start >= earlier.end - 1e-9


# --- R8.2: non-overlap ----------------------------------------------------------------------


def test_an_overlapping_span_is_truncated():
    """R8.2. Two words lit simultaneously is a rendering fault."""
    out, report = ws.apply_hygiene([_w(0.0, 1.0), _w(0.5, 1.2)])
    assert out[0].end <= out[1].start
    assert report.deoverlapped == 1


def test_adjacent_spans_keep_a_gap():
    """Sharing an exact boundary makes which word is lit depend on floating-point rounding."""
    out, _ = ws.apply_hygiene([_w(0.0, 0.6), _w(0.5, 1.0)])
    assert out[1].start - out[0].end >= 0.0
    assert ws.SPAN_EPSILON > 0


def test_a_reversed_span_is_normalised():
    """`end < start` is degenerate and renders as nothing at all."""
    out, _ = ws.apply_hygiene([_w(0.5, 0.2)])
    assert out[0].end >= out[0].start


# --- R8.3 / R8.4: the floor, and what outranks it -------------------------------------------


def test_a_short_span_is_lengthened_when_there_is_room():
    """R8.3. Below about 80 ms a highlight reads as a flicker rather than a word being lit."""
    out, report = ws.apply_hygiene([_w(0.0, 0.02), _w(2.0, 2.5)], min_seconds=0.2)
    assert out[0].end - out[0].start == pytest.approx(0.2)
    assert report.lengthened == 1


def test_non_overlap_outranks_the_minimum_duration():
    """R8.4, the requirement this module's ordering exists to satisfy.

    The next span starts at 0.10, so the 0.2 s floor cannot be met without overlapping it. The span
    is lengthened as far as it can go and no further, and the clamp is counted separately so a clip
    whose spans are all pinned by their neighbours is distinguishable from one where the floor was
    simply met.
    """
    out, report = ws.apply_hygiene([_w(0.0, 0.02), _w(0.10, 0.5)], min_seconds=0.2)
    assert out[0].end <= out[1].start
    assert out[0].end - out[0].start < 0.2
    assert report.clamped_to_cue == 1


def test_the_floor_never_extends_past_the_cue_end():
    """R8.5. A highlight outliving its line has nothing to highlight."""
    out, _ = ws.apply_hygiene([_w(0.0, 0.05)], min_seconds=1.0, cue_end=0.3)
    assert out[0].end <= 0.3


def test_a_span_overrunning_its_cue_is_clamped_even_with_the_floor_off():
    """R8.5 with nothing else wrong, which is the case the fast path used to wave through.

    The compliance pre-check tested ordering, sign and the floor, but never the cue boundary. So a
    single well-formed span that simply ran past the end of its own line was declared compliant and
    returned untouched, and `cue_end` was inert for every sequence that had no *other* fault — which
    is most of them. The repair below was reachable only as a side effect of some unrelated defect
    failing the check first.

    `min_seconds=0.0` is the point of the test: with the floor off, this is the only thing R8.5 has
    left to do, so nothing else can mask it.
    """
    out, report = ws.apply_hygiene([_w(0.0, 0.6)], min_seconds=0.0, cue_end=0.4)

    assert out[0].end == pytest.approx(0.4 - ws.SPAN_EPSILON)
    assert report.clamped_to_cue == 1
    assert report.markers == ["word_spans_repaired:1"]


def test_a_cue_boundary_clamp_is_reported_separately_from_a_de_overlap():
    """Truncating against a neighbour and truncating against the cue end are different facts.

    A clip whose every repair was the latter says its cues are cut short of their own words; one
    where they were de-overlaps says its ASR emitted overlapping spans. Collapsing both into
    `deoverlapped` would leave the operator unable to tell which.
    """
    _out, overlap = ws.apply_hygiene([_w(0.0, 1.0), _w(0.5, 0.9)], cue_end=2.0)
    _out2, boundary = ws.apply_hygiene([_w(0.0, 1.0)], cue_end=0.5)

    assert (overlap.deoverlapped, overlap.clamped_to_cue) == (1, 0)
    assert (boundary.deoverlapped, boundary.clamped_to_cue) == (0, 1)


def test_the_last_span_may_use_the_cue_end_as_room():
    """The final span has no neighbour, so the cue boundary is its only ceiling."""
    out, report = ws.apply_hygiene([_w(0.0, 0.4), _w(0.5, 0.52)], min_seconds=0.3, cue_end=2.0)
    assert out[-1].end - out[-1].start == pytest.approx(0.3)
    assert report.lengthened == 1


def test_with_no_cue_end_the_last_span_is_free_to_grow():
    out, _ = ws.apply_hygiene([_w(0.0, 0.01)], min_seconds=0.5)
    assert out[0].end == pytest.approx(0.5)


# --- properties -----------------------------------------------------------------------------


@pytest.mark.parametrize("floor", [0.0, 0.05, 0.2, 1.0])
def test_output_never_overlaps_whatever_the_floor(floor):
    """The invariant that must hold for every setting, including one too large to satisfy."""
    spans = [_w(0.0, 0.3), _w(0.1, 0.15), _w(0.12, 0.9), _w(0.8, 0.85), _w(0.9, 1.4)]
    out, _ = ws.apply_hygiene(spans, min_seconds=floor)
    for earlier, later in zip(out, out[1:], strict=False):
        assert earlier.end <= later.start + 1e-9, (floor, earlier, later)


def test_the_span_count_is_never_changed():
    """Hygiene repairs timings; dropping a word would drop text from the caption."""
    spans = [_w(0.0, 0.3), _w(0.1, 0.15), _w(0.12, 0.9)]
    out, _ = ws.apply_hygiene(spans, min_seconds=0.5)
    assert len(out) == len(spans)


def test_the_text_is_never_altered():
    """Only timings move. A repair that rewrote text would be a transcription change."""
    spans = [_w(0.0, 0.3, "hello"), _w(0.1, 0.15, "there")]
    out, _ = ws.apply_hygiene(spans, min_seconds=0.4)
    assert [s.text for s in out] == ["hello", "there"]


def test_the_cached_transcript_is_not_mutated():
    """Spans are copied before adjustment, so a re-render corrects rather than compounds.

    The T8 transcript cache holds the ASR's own output; if hygiene mutated it in place, a second
    render would repair already-repaired spans and drift further each time.
    """
    spans = [_w(0.0, 0.3), _w(0.1, 0.15)]
    originals = [(s.start, s.end) for s in spans]
    ws.apply_hygiene(spans, min_seconds=0.5)
    assert [(s.start, s.end) for s in spans] == originals


def test_an_unreadable_span_is_left_alone_rather_than_guessed_at():
    """A shape this cannot parse is returned untouched; the caller's renderer already handles it."""

    class Odd:
        start = "not-a-number"
        end = None

    odd = Odd()
    out, report = ws.apply_hygiene([odd], min_seconds=0.5)
    assert out == [odd]
    assert report.altered == 0


# --- R8.8: the marker -----------------------------------------------------------------------


def test_a_marker_is_recorded_only_when_something_changed():
    """A marker on every clip is noise, and noise is what stops a marker being read."""
    _out, clean = ws.apply_hygiene([_w(0.0, 0.4), _w(0.5, 0.9)])
    assert clean.markers == []

    _out, dirty = ws.apply_hygiene([_w(0.0, 1.0), _w(0.5, 1.2)])
    assert dirty.markers == [f"word_spans_repaired:{dirty.altered}"]


def test_the_report_distinguishes_the_kinds_of_repair():
    """Which fault occurred is the useful part; a single count cannot say."""
    _out, report = ws.apply_hygiene([_w(0.0, 1.0), _w(0.5, 0.52)], min_seconds=0.3)
    data = report.to_dict()
    assert set(data) == {"reordered", "deoverlapped", "lengthened", "clamped_to_cue", "altered"}
    assert data["altered"] == sum(v for k, v in data.items() if k != "altered")


# --- T11: the measurement that rules it out -------------------------------------------------


def test_the_cached_envelope_has_no_word_rate_information():
    """**T11 cannot be implemented as R7.8 specifies, and this records why.**

    R7.8 requires reusing the energy envelope already computed for the source and forbids a second
    audio pass. That envelope is built at `ENVELOPE_WINDOW_S = 1.0` — one RMS reading per second —
    because `S2` compares candidate *windows*, where a second of resolution is ample.

    A word at conversational pace occupies roughly 0.3–0.4 s. One-second RMS averaging smooths those
    transients away entirely: measured on an 8-second source containing 20 distinct bursts, the
    envelope produced 8 readings and `detect_onsets` found **zero** onsets.

    So there is nothing in that envelope to snap a word start to. A T11 built on it would move
    nothing, or move spans onto second boundaries — displacing them by up to 500 ms, far worse than
    the drift it exists to correct. Implementing it needs a ~20 ms envelope, which is exactly the
    second audio pass R7.8 rules out.

    Asserted against the constant so that if somebody lowers the window for another reason, this test
    fails and tells them T11 has become possible.
    """
    from worker import audio_features as af

    assert af.ENVELOPE_WINDOW_S >= 0.5, (
        "the energy envelope is now fine enough to carry word-rate transients; T11 "
        "(snapping word starts to onsets) may be implementable within R7.8 after all"
    )

    # A synthetic envelope at the real resolution, with a level rise inside one reading: the rise is
    # invisible because the reading is an average over the whole second.
    envelope = [(float(i), -20.0) for i in range(8)]
    assert af.detect_onsets(envelope) == [], (
        "a flat one-second envelope must yield no onsets; if it does, the detector changed"
    )
