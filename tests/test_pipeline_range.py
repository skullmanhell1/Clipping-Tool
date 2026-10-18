"""Process-range transcript clipping tests."""

from __future__ import annotations

from worker.pipeline import _filter_transcript_to_range
from worker.transcribe import Transcript, TranscriptSegment, Word


def test_process_range_trims_segments_and_words_before_selection():
    transcript = Transcript(
        language="en",
        segments=[
            TranscriptSegment(
                0.0,
                10.0,
                "before inside after",
                [
                    Word(0.5, 1.5, "before"),
                    Word(4.5, 5.5, "inside"),
                    Word(8.5, 9.5, "after"),
                ],
            )
        ],
    )

    filtered = _filter_transcript_to_range(transcript, 4.0, 7.0)

    assert len(filtered.segments) == 1
    assert (filtered.segments[0].start, filtered.segments[0].end) == (4.0, 7.0)
    assert [word.text for word in filtered.words] == ["inside"]
    assert filtered.text == "inside"


def test_partial_segment_without_word_timestamps_is_not_leaked_into_the_range():
    transcript = Transcript(
        language="en",
        segments=[TranscriptSegment(0.0, 10.0, "outside context")],
    )

    filtered = _filter_transcript_to_range(transcript, 4.0, 7.0)

    assert filtered.segments == []
