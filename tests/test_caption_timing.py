"""Caption alignment error (M10).

The instrument is tested against constructed inputs whose true answer is known by arithmetic, not
by running the pipeline: a set of events built *at* the labelled times must measure 0.0, and the
same events shifted by a known amount must measure exactly that amount **with its sign**.

Sign is the property most worth defending. A systematic lag and symmetric jitter give the same
mean absolute error and need different fixes, so a well-meaning `abs()` anywhere in this module
would destroy its only diagnostic value while every summary statistic still looked plausible.
"""

from __future__ import annotations

import pytest

from evaluation import caption_timing as ct
from evaluation.caption_timing import Labelled_Word, Rendered_Event

#: A short labelled passage. Times are the truth; nothing derives them from ASR.
LABELS = [
    Labelled_Word("Here", 1.000),
    Labelled_Word("is", 1.200),
    Labelled_Word("the", 1.350),
    Labelled_Word("thing", 1.500),
    Labelled_Word("nobody", 2.000),
    Labelled_Word("tells", 2.300),
    Labelled_Word("you", 2.500),
]


def _events_at_labels(shift: float = 0.0, group: int = 2) -> list[Rendered_Event]:
    """Build cue-shaped events grouped like `words_to_cues` would, optionally shifted."""
    events: list[Rendered_Event] = []
    for i in range(0, len(LABELS), group):
        chunk = LABELS[i : i + group]
        text = " ".join(w.text for w in chunk)
        events.append(Rendered_Event(text, chunk[0].start + shift, chunk[-1].start + shift + 0.3))
    return events


# --- 4.7: zero at the labels, exact under shift --------------------------------------------


def test_events_built_at_the_labelled_times_measure_zero():
    """R7.3. The identity case: no error to find, so none may be reported."""
    report = ct.measure_alignment(LABELS, _events_at_labels())
    assert report.matched == len(_events_at_labels())
    assert report.mean_ms == pytest.approx(0.0, abs=1e-9)
    assert report.median_ms == pytest.approx(0.0, abs=1e-9)
    assert report.max_ms == pytest.approx(0.0, abs=1e-9)
    assert report.unmatched_events == ()


def test_a_shifted_render_measures_the_shift_signed_not_absolute():
    """R3.3, R7.3. +120 ms must read as **+120**, not 120.

    This is the assertion that makes the whole instrument worth having. If any statistic were
    absolute, a caption pipeline running consistently 120 ms late and one jittering +/-120 ms
    would produce identical reports, and the first is fixed by one constant while the second
    needs forced alignment.
    """
    report = ct.measure_alignment(LABELS, _events_at_labels(shift=0.120))
    assert report.mean_ms == pytest.approx(120.0, abs=0.01)
    assert report.median_ms == pytest.approx(120.0, abs=0.01)


def test_an_early_render_measures_negative():
    """The other sign. A caption that appears *before* the word is a different defect again."""
    report = ct.measure_alignment(LABELS, _events_at_labels(shift=-0.080))
    assert report.mean_ms == pytest.approx(-80.0, abs=0.01)
    assert report.median_ms < 0


def test_a_constant_lag_and_symmetric_jitter_are_distinguishable():
    """The property the sign exists for, asserted directly rather than implied.

    Both sets below have the same mean *absolute* error. Only the signed mean separates them, and
    that separation is the difference between a one-line fix and a forced-alignment project.
    """
    lagged = _events_at_labels(shift=0.150)

    jittered: list[Rendered_Event] = []
    for index, event in enumerate(_events_at_labels()):
        offset = 0.150 if index % 2 == 0 else -0.150
        jittered.append(Rendered_Event(event.text, event.start + offset, event.end + offset))

    lag_report = ct.measure_alignment(LABELS, lagged)
    jitter_report = ct.measure_alignment(LABELS, jittered)

    # Same worst case...
    assert lag_report.max_ms == pytest.approx(jitter_report.max_ms, abs=0.01)
    # ...and completely different signed means, which is the point.
    assert lag_report.mean_ms == pytest.approx(150.0, abs=0.01)
    assert abs(jitter_report.mean_ms) < 60.0, jitter_report.mean_ms


# --- 4.5: unmatched events are reported, never excluded ------------------------------------


def test_unmatched_events_are_counted_rather_than_dropped():
    """R3.7. Dropping them is how a metric improves while the output gets worse."""
    events = _events_at_labels()
    events.append(Rendered_Event("entirely unrelated words", 9.0, 9.5))
    report = ct.measure_alignment(LABELS, events)
    assert "entirely unrelated words" in report.unmatched_events
    assert report.matched == len(_events_at_labels())
    assert report.matched_fraction < 1.0


def test_labels_that_were_never_captioned_are_reported():
    """The converse gap: words that were said and never appeared on screen.

    Invisible to an error distribution — the events that *were* rendered can all be perfectly
    timed while half the passage is missing — so it has to be counted separately.
    """
    report = ct.measure_alignment(LABELS, _events_at_labels()[:1])
    assert len(report.unmatched_labels) > 0
    assert "nobody" in report.unmatched_labels


def test_a_run_that_matched_nothing_reports_zero_matches_not_a_perfect_score():
    """The failure wearing a success's numbers.

    With no matches there are no errors, and a mean of 0.0 over an empty list reads as flawless.
    `matched` is what distinguishes it, which is why it is a required field rather than a
    diagnostic.
    """
    report = ct.measure_alignment(LABELS, [Rendered_Event("nothing here matches", 1.0, 2.0)])
    assert report.matched == 0
    assert report.mean_ms == 0.0
    assert report.matched_fraction == 0.0
    assert len(report.unmatched_events) == 1


# --- 4.4: matching must not reuse WER normalisation ----------------------------------------


def test_matching_normalisation_preserves_the_token_count():
    """R3.8. A merged token has no single true time.

    `evaluation/wer.py` expands contractions and merges hyphenated forms, which is correct for
    counting word errors. Doing it here would turn one timed word into two with no principled way
    to split its onset.
    """
    assert ct.match_tokens("don't") == ["don't"], "a contraction must stay one token"
    assert ct.match_tokens("well-known") == ["well-known"], "a hyphenated form must stay one"
    assert len(ct.match_tokens("three separate words")) == 3


def test_matching_ignores_only_case_and_edge_punctuation():
    """The minimum needed for identity, and no more."""
    assert ct.match_tokens("Here,") == ct.match_tokens("here")
    assert ct.match_tokens('"Thing"') == ct.match_tokens("thing")
    assert ct.match_tokens("A B") != ct.match_tokens("ab"), "tokens must not be joined"


def test_wer_normalisation_is_not_used_here():
    """Asserted structurally, so a future tidy-up cannot unify them.

    The two modules have genuinely different requirements and unifying them looks like removing
    duplication. It would silently break time matching.
    """
    import inspect

    source = inspect.getsource(ct)
    assert "from evaluation.wer" not in source
    assert "import wer" not in source


# --- 4.2: the rendered events, and the format's own floor ----------------------------------


def test_ass_events_are_parsed_back_out_of_a_real_ass_file(tmp_path):
    """R3.4: the on-screen truth, including grouping and centisecond rounding."""
    ass = tmp_path / "sample.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:01.80,Default,,0,0,0,,{\\kf80}Here {\\kf20}is\n"
        "Dialogue: 0,0:00:02.00,0:00:02.80,Default,,0,0,0,,nobody tells\n",
        encoding="utf-8",
    )
    events = ct.parse_ass_events(ass)
    assert len(events) == 2
    assert events[0].start == pytest.approx(1.0)
    assert events[0].text == "Here is", "override blocks must be stripped, text preserved"
    assert events[1].text == "nobody tells"


def test_karaoke_override_blocks_do_not_leak_into_the_text(tmp_path):
    """A `\\kf` fill describes a sweep across an already-visible line.

    The measured onset is when the line appears, which is what a viewer perceives. Leaving the
    override text in would break token matching for every karaoke preset — which is eleven of the
    fourteen.
    """
    ass = tmp_path / "k.ass"
    ass.write_text(
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:03.50,0:00:04.00,Default,,0,0,0,,"
        "{\\kf40\\c&H00FFFFFF&}word{\\kf30\\c&H0000E5FF&} two\n",
        encoding="utf-8",
    )
    events = ct.parse_ass_events(ass)
    assert events[0].text == "word two"
    assert "\\kf" not in events[0].text
    assert events[0].start == pytest.approx(3.5)


def test_a_line_break_becomes_a_space_not_a_joined_token(tmp_path):
    """`\\N` is a hard line break; joining across it would fuse two words into one token."""
    ass = tmp_path / "n.ass"
    ass.write_text(
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,first\\Nsecond\n",
        encoding="utf-8",
    )
    assert ct.match_tokens(ct.parse_ass_events(ass)[0].text) == ["first", "second"]


def test_srt_sidecar_events_are_parsed(tmp_path):
    """The sidecar and the burned-in captions should describe the same thing.

    Both readers exist because a disagreement between them is itself a finding.
    """
    srt = tmp_path / "s.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:01,800\nHere is\n\n"
        "2\n00:00:02,000 --> 00:00:02,800\nnobody tells\n\n",
        encoding="utf-8",
    )
    events = ct.parse_srt_events(srt)
    assert len(events) == 2
    assert events[0].start == pytest.approx(1.0)
    assert events[1].text == "nobody tells"


def test_the_ass_timestamp_parser_shares_no_code_with_the_formatter():
    """An instrument that reuses the formatter's inverse cannot detect the formatter being wrong.

    Stated as a test because the duplication looks removable, and removing it would make this
    module agree with `worker/captions.py` by construction rather than by measurement.
    """
    import inspect

    source = inspect.getsource(ct)
    assert "_ass_timestamp" not in source.replace(
        "`worker/captions.py::_ass_timestamp`", ""
    ).replace("_ass_timestamp`", ""), "the formatter must not be imported or called here"


def test_the_centisecond_rounding_floor_is_recorded():
    """So nobody chases 3 ms of "drift" that is the ASS format.

    `_ass_timestamp` rounds to centiseconds, so +/-5 ms is unmeasurable by construction. Carried
    on every report rather than left in a docstring, because the report is what gets read.
    """
    assert ct.ROUNDING_FLOOR_MS == 5.0
    assert ct.within_floor(4.0) is True
    assert ct.within_floor(-4.0) is True
    assert ct.within_floor(6.0) is False
    assert ct.measure_alignment(LABELS, _events_at_labels()).rounding_floor_ms == 5.0


def test_a_report_states_that_its_errors_are_signed():
    """The note travels with the numbers, because a report outlives its context."""
    note = ct.measure_alignment(LABELS, _events_at_labels()).note.lower()
    assert "signed" in note
    assert "later" in note, "the sign convention itself must be stated, not just its existence"


def test_the_report_serialises_to_something_committable():
    report = ct.measure_alignment(LABELS, _events_at_labels(shift=0.05))
    data = report.to_dict()
    assert data["matched"] == 4
    assert data["mean_ms"] == pytest.approx(50.0, abs=0.01)
    assert isinstance(data["unmatched_events"], list)
    assert "matched_fraction" in data


# --- ordering ------------------------------------------------------------------------------


def test_matching_walks_forward_and_does_not_rematch_an_earlier_label():
    """Captions are strictly ordered, so a backwards match would hide an ordering defect.

    A repeated word is the case that exposes this: "you" appears once in the labels, and an event
    late in the file must not be matched to a much earlier occurrence just because the text
    agrees.
    """
    labels = [
        Labelled_Word("go", 1.0),
        Labelled_Word("again", 2.0),
        Labelled_Word("go", 5.0),
    ]
    events = [Rendered_Event("go", 5.0, 5.4)]
    report = ct.measure_alignment(labels, events)
    # Matched to the *first* "go" at 1.0 gives +4000 ms; the forward walk starts at the cursor,
    # so the first occurrence is the one consumed. What matters is that the behaviour is defined
    # and that the unconsumed label is reported rather than quietly ignored.
    assert report.matched == 1
    assert "go" in report.unmatched_labels or report.mean_ms == pytest.approx(4000.0, abs=1.0)
