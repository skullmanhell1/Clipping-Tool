"""Caption alignment error (M10): does a caption appear when the word is said?

`evaluation/wer.py` measures whether the words are *right*. Nothing measured whether they are
**on time**, and timing is the defect a viewer actually perceives: a caption 200 ms late reads as
badly made even when every word is correct, while a word transcribed wrong but well-timed often
goes unnoticed.

Three decisions shape everything here.

**The error is signed** (R3.3). A systematic +150 ms lag and a symmetric ±150 ms jitter produce
*the same mean absolute error* and are different defects with different fixes — a lag is one
constant compensation, jitter needs forced alignment. Taking absolute values destroys precisely
the information that distinguishes them, so every statistic in :class:`Alignment_Report` keeps its
sign.

**Measured from the rendered events, never the word list** (R3.4). The word list is the input to
`words_to_cues`; the screen is the output of grouping, centisecond rounding, `\\kf` fill durations
and any onset snapping a sibling spec adds. Measuring the input would exclude every layer capable
of introducing the error, which would make the instrument agree with the pipeline by construction.

**Matching never merges or drops a token** (R3.8). `evaluation/wer.py`'s normalisation is right for
WER and wrong here: it merges and discards tokens, and a merged token has no single true time. So
this module has its own deliberately minimal normalisation, and unmatched events are **reported**
rather than excluded (R3.7) — silently dropping what could not be matched is how a metric improves
while the output gets worse.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

#: The floor of the instrument, in milliseconds.
#:
#: `worker/captions.py::_ass_timestamp` formats to centiseconds and rounds to nearest, so any
#: rendered onset can differ from its intended value by up to half a centisecond in either
#: direction. This is the **format**, not a defect, and it is recorded here so nobody spends an
#: afternoon chasing 3 ms of "drift". Anything inside +/-5 ms is unmeasurable by construction.
ROUNDING_FLOOR_MS = 5.0

#: Tolerance beyond which a caption is late enough to be perceived. Reported against, never
#: enforced: this module states what it measured and does not hold an opinion about whether a
#: given number is acceptable.
PERCEPTIBLE_MS = 100.0


@dataclass(frozen=True)
class Labelled_Word:
    """One word with a known-true time, from the labelled set."""

    text: str
    start: float
    end: float = 0.0


@dataclass(frozen=True)
class Rendered_Event:
    """One caption event as it was actually rendered: text plus on-screen times."""

    text: str
    start: float
    end: float = 0.0

    @property
    def first_token(self) -> str:
        tokens = match_tokens(self.text)
        return tokens[0] if tokens else ""


@dataclass(frozen=True)
class Alignment_Report:
    """The signed distribution, plus everything that failed to match.

    ``unmatched_events`` and ``unmatched_labels`` are part of the result rather than a footnote.
    A run that matched three of forty events and reported a beautiful 2 ms mean is a failure
    wearing a success's numbers, and the only thing that reveals it is the count.
    """

    mean_ms: float = 0.0
    median_ms: float = 0.0
    p90_ms: float = 0.0
    max_ms: float = 0.0
    matched: int = 0
    unmatched_events: tuple[str, ...] = ()
    unmatched_labels: tuple[str, ...] = ()
    errors_ms: tuple[float, ...] = ()
    rounding_floor_ms: float = ROUNDING_FLOOR_MS
    source: str = ""
    note: str = (
        "Errors are signed: positive means the caption appeared later than the word was said. "
        "Mean and median are of signed values, so a systematic lag and symmetric jitter are "
        "distinguishable; p90 and max are of absolute values, which is what a worst case means. "
        "Values within the rounding floor are the ASS centisecond format, not drift."
    )

    @property
    def matched_fraction(self) -> float:
        total = self.matched + len(self.unmatched_events)
        return (self.matched / total) if total else 0.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["unmatched_events"] = list(self.unmatched_events)
        data["unmatched_labels"] = list(self.unmatched_labels)
        data["errors_ms"] = [round(e, 2) for e in self.errors_ms]
        data["matched_fraction"] = round(self.matched_fraction, 4)
        return data


#: Kept deliberately minimal, and deliberately **not** `evaluation/wer.py`'s normalisation.
#:
#: That one lower-cases, strips punctuation, expands contractions and merges hyphenated forms --
#: all correct for counting word errors, all wrong for locating a time. Expanding "don't" into
#: "do not" turns one timed token into two with no principled way to split its onset, and merging
#: "well-known" into one token discards a boundary a caption may legitimately break at.
#:
#: So this strips only what cannot affect identity: surrounding punctuation and case. Nothing is
#: merged, nothing is dropped, and the token count is preserved.
_PUNCTUATION = re.compile(r"^[^\w']+|[^\w']+$", re.UNICODE)


def match_tokens(text: str) -> list[str]:
    """Split ``text`` into comparison tokens, preserving one token per word."""
    tokens: list[str] = []
    for raw in (text or "").split():
        cleaned = _PUNCTUATION.sub("", raw).casefold()
        if cleaned:
            tokens.append(cleaned)
    return tokens


# --- reading what was actually rendered ---------------------------------------------------

#: ASS dialogue lines. Captured groups: start, end, the remainder of the fields plus text.
_ASS_DIALOGUE = re.compile(r"^Dialogue:\s*[^,]*,([^,]+),([^,]+),(.*)$", re.MULTILINE)
#: ASS override blocks (`{\kf30\c&H..}`) and drawing commands, stripped to leave spoken text.
_ASS_OVERRIDE = re.compile(r"\{[^}]*\}")


def _ass_seconds(stamp: str) -> float:
    """Parse ``H:MM:SS.cs`` back to seconds.

    Written out rather than reusing `_ass_timestamp`'s inverse from `worker/captions.py`, because
    an instrument that shares a parser with the thing it measures cannot detect that parser being
    wrong. If `_ass_timestamp` ever emits a malformed stamp, this module must disagree with it.
    """
    text = stamp.strip()
    hours, _, rest = text.partition(":")
    minutes, _, secs = rest.partition(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(secs)


def parse_ass_events(path: str | Path) -> list[Rendered_Event]:
    """Read caption events back out of a generated ASS file (R3.4).

    This is the on-screen truth: it includes `words_to_cues` grouping and the centisecond
    rounding, which the word list does not.

    Karaoke override blocks are stripped rather than parsed. A `\\kf` fill describes how the
    highlight *sweeps* across an already-visible line; the event's own start is when the line
    appears, which is the onset a viewer perceives and the one being measured.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    events: list[Rendered_Event] = []
    for start, end, remainder in _ASS_DIALOGUE.findall(text):
        # The text field is the last of the nine comma-separated fields; commas inside the text
        # itself are legal, so this splits a bounded number of times rather than all of them.
        parts = remainder.split(",", 6)
        body = parts[-1] if parts else ""
        spoken = _ASS_OVERRIDE.sub("", body).replace("\\N", " ").replace("\\n", " ").strip()
        if not spoken:
            continue
        events.append(Rendered_Event(spoken, _ass_seconds(start), _ass_seconds(end)))
    return events


_SRT_BLOCK = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*\n(.*?)(?:\n\n|\Z)",
    re.DOTALL,
)


def parse_srt_events(path: str | Path) -> list[Rendered_Event]:
    """Read events from the SRT sidecar `subtitle_export.py` produces.

    Offered alongside the ASS reader because the two can disagree, and a disagreement is itself a
    finding: the burned-in captions and the sidecar are supposed to describe the same thing.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    events: list[Rendered_Event] = []
    for m in _SRT_BLOCK.finditer(text):
        sh, sm, ss, sms, eh, em, es, ems, body = m.groups()
        start = int(sh) * 3600 + int(sm) * 60 + int(ss) + int(sms) / 1000.0
        end = int(eh) * 3600 + int(em) * 60 + int(es) + int(ems) / 1000.0
        spoken = " ".join(line.strip() for line in body.splitlines()).strip()
        if spoken:
            events.append(Rendered_Event(spoken, start, end))
    return events


# --- the measurement ----------------------------------------------------------------------


def measure_alignment(
    labels: Sequence[Labelled_Word],
    events: Sequence[Rendered_Event],
    *,
    source: str = "",
) -> Alignment_Report:
    """Signed onset error between rendered events and labelled word times.

    An event's onset is compared against the labelled start of its **first word**, because that
    is the instant the line appears and the instant a viewer judges. Matching walks both
    sequences forward monotonically: captions are strictly ordered in time, so a match that
    required going backwards would indicate a genuine ordering defect and should not be papered
    over by a nearest-neighbour search.

    Nothing is merged and nothing is silently dropped. Events whose first token never matched are
    returned in ``unmatched_events`` and labels never consumed in ``unmatched_labels`` (R3.7).
    """
    label_tokens = [(match_tokens(w.text), w) for w in labels]
    flat: list[tuple[str, Labelled_Word]] = [
        (tokens[0], word) for tokens, word in label_tokens if tokens
    ]

    errors: list[float] = []
    unmatched_events: list[str] = []
    consumed: set[int] = set()
    cursor = 0

    for event in events:
        token = event.first_token
        if not token:
            unmatched_events.append(event.text)
            continue
        found = -1
        for index in range(cursor, len(flat)):
            if flat[index][0] == token:
                found = index
                break
        if found < 0:
            # Deliberately not falling back to a global search. An event whose first word does
            # not appear at or after the cursor is either a transcription difference or an
            # ordering fault, and both are more useful reported than matched to a coincidence
            # elsewhere in the passage.
            unmatched_events.append(event.text)
            continue
        errors.append((event.start - flat[found][1].start) * 1000.0)
        consumed.add(found)
        cursor = found + 1

    unmatched_labels = [word.text for i, (_, word) in enumerate(flat) if i not in consumed]

    if errors:
        absolute = sorted(abs(e) for e in errors)
        # p90 on absolute values: a worst case has no sign. Mean and median stay signed, which
        # is what separates a constant lag from symmetric jitter.
        index = min(len(absolute) - 1, int(round(0.9 * (len(absolute) - 1))))
        report = Alignment_Report(
            mean_ms=statistics.fmean(errors),
            median_ms=statistics.median(errors),
            p90_ms=absolute[index],
            max_ms=max(absolute),
            matched=len(errors),
            unmatched_events=tuple(unmatched_events),
            unmatched_labels=tuple(unmatched_labels),
            errors_ms=tuple(errors),
            source=source,
        )
    else:
        report = Alignment_Report(
            matched=0,
            unmatched_events=tuple(unmatched_events),
            unmatched_labels=tuple(unmatched_labels),
            source=source,
        )
    return report


def within_floor(value_ms: float) -> bool:
    """Whether an error is indistinguishable from the ASS format's own rounding."""
    return abs(value_ms) <= ROUNDING_FLOOR_MS
