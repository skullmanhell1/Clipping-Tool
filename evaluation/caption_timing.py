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


# --- Label-free measurement, against the audio itself -------------------------------------
#
# Everything above needs a labelled set. That is the right instrument for accuracy, and the wrong
# one for answering "are the captions on this clip in sync?", because producing labels means either
# hand-transcribing or re-running ASR — and re-running ASR makes the measurement circular, since ASR
# is where the caption times came from in the first place. Measured, when a desync was reported:
# whisper-derived labels put the mean error at -944 ms on clips whose captions were in fact aligned,
# because only 5 of 47 words matched and the mean was taken over the survivors.
#
# So this half needs no labels at all. It compares *when a caption is on screen* against *when
# sound is happening*, both read from the finished artefacts. It cannot tell you whether the words
# are correct — `measure_alignment` and `evaluation/wer.py` do that — but it answers the question a
# viewer is actually asking, and it cannot be fooled by agreeing with the pipeline.
#
# The envelope is deliberately built at 20 ms rather than reusing the 1 s envelope `S2` shares (see
# the T11 note in `worker/word_spans.py`): one reading per second cannot resolve a word, and this
# module is an instrument, so a second audio pass is a cost it is allowed to pay where the render
# path is not.

#: RMS window for the speech envelope, in seconds. Word-scale on purpose.
ENVELOPE_HOP_S = 0.02

#: How far below the loudest frame still counts as speech, in dB. Generous, because the question is
#: "is anything being said here", not "how loud is it".
SPEECH_FLOOR_DB = 30.0

#: Absolute peak below which a clip is treated as carrying no signal at all, in dBFS.
#:
#: :data:`SPEECH_FLOOR_DB` is measured *relative to the clip's own peak*, which is what makes it
#: independent of recording level — and is exactly why an absolute floor is needed beside it. A
#: silent track has a peak of about -240 dBFS (the RMS epsilon), and every frame sits within 30 dB
#: of it, so a purely relative threshold marks the whole clip as speech.
#:
#: -60 dBFS is far below any speech that a viewer could hear and far above digital silence, so it
#: separates "quiet recording" from "no signal" without rejecting real material.
SILENCE_FLOOR_DBFS = -60.0


def speech_mask(media: str | Path, *, hop: float = ENVELOPE_HOP_S) -> list[bool]:
    """One flag per ``hop`` seconds: was sound happening in that frame?

    Read from the media's own audio, so it is independent of the transcript, the cue list and the
    ASR. Requires ffmpeg on PATH; raises if it is missing rather than returning a mask of ``False``
    that would read as "silent clip" and score a desynced caption as perfect.
    """
    import shutil
    import subprocess

    import numpy as np

    from config import settings

    # Resolved the same way the sibling instruments do it (`evaluation/fidelity.py::_ffmpeg`),
    # rather than trusting PATH: the configured binary is the one the render used, and this
    # measurement is only meaningful against the same decoder.
    ffmpeg = shutil.which(str(settings.ffmpeg_binary)) or "ffmpeg"
    result = subprocess.run(
        [ffmpeg, "-v", "quiet", "-i", str(media), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True,
        # Bounded, and stdin closed. See `worker.ffmpeg_utils._run`: ffmpeg reads stdin unless told
        # not to, and under pytest in CI it inherits a pipe that never delivers EOF. With neither a
        # timeout nor a redirection this could block for ever, which is what took the CI job to its
        # six-hour limit while the same suite finished in eight minutes locally.
        timeout=float(getattr(settings, "ffmpeg_timeout_seconds", 900)) or None,
        stdin=subprocess.DEVNULL,
    )
    if not result.stdout:
        raise RuntimeError(f"no audio decoded from {media}; is ffmpeg present and the file valid?")
    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    per_frame = max(1, int(hop * 16000))
    frames = len(samples) // per_frame
    if frames == 0:
        # Shorter than a single hop. Previously returned ``[]``, which every consumer then turned
        # into a *perfect* reading (0 ms lag, see `best_fit_lag_ms`). Refusing is the honest
        # answer, and the one callers already handle — `scripts/measure_caption_sync.py` skips a
        # clip whose mask raises.
        raise RuntimeError(
            f"{media} decoded to less than one {hop * 1000:.0f} ms analysis frame; "
            "too short to measure caption timing against"
        )
    blocks = samples[: frames * per_frame].reshape(frames, per_frame)
    rms = np.sqrt(np.maximum((blocks**2).mean(axis=1), 1e-12))
    db = 20 * np.log10(rms)

    # The threshold below is *relative to this clip's own peak*, which is what makes it robust to
    # recording level — and what makes an absolute floor necessary as well. On digital silence
    # every RMS clamps to the 1e-12 epsilon, so every value is about -240 dB, the peak is also
    # about -240, and `db > peak - 30` is true for **every frame**: the mask reads as "sound is
    # happening continuously". `coverage_overlap` then returns the fraction of the clip the
    # captions cover — a plausible number about nothing at all.
    #
    # The docstring above guards the opposite direction (a missing binary yielding an all-False
    # mask that would read as a silent clip). That case cannot happen; this one can, and does,
    # for any clip whose audio track survived the mux but carries no signal.
    if float(db.max()) < SILENCE_FLOOR_DBFS:
        raise RuntimeError(
            f"{media} carries no audible signal (peak {float(db.max()):.0f} dBFS, "
            f"floor {SILENCE_FLOOR_DBFS:.0f}); caption timing cannot be measured against silence"
        )
    return [bool(v) for v in (db > (db.max() - SPEECH_FLOOR_DB))]


def coverage_overlap(
    events: Sequence[Rendered_Event],
    mask: Sequence[bool],
    *,
    hop: float = ENVELOPE_HOP_S,
    shift_frames: int = 0,
) -> float:
    """Intersection-over-union of "caption on screen" and "sound happening".

    IoU rather than plain overlap, so a cue list that simply covers the whole clip cannot score
    well: padding inflates the union as fast as it does the intersection.
    """
    if not mask:
        # "No mask" and "the captions miss the speech entirely" both used to return 0.0, which are
        # opposite facts. `speech_mask` no longer produces an empty mask, so reaching here means a
        # caller built one — worth saying rather than scoring.
        raise RuntimeError("coverage_overlap needs a non-empty speech mask")
    on = [False] * len(mask)
    for event in events:
        first = int(round(event.start / hop)) + shift_frames
        # Exclusive end: a cue covering [start, end) occupies the hops strictly before `end`.
        # `range(first, last + 1)` marked one extra 20 ms hop per event, inflating both the
        # intersection and the union on every measurement — on the order of the 20 ms precision
        # this module claims, and biasing the ±2 s argmax in `best_fit_lag_ms`.
        last = int(round(event.end / hop)) + shift_frames
        for index in range(max(0, first), min(len(on), max(first, last))):
            on[index] = True
    intersection = sum(1 for a, b in zip(mask, on) if a and b)
    union = sum(1 for a, b in zip(mask, on) if a or b)
    return intersection / union if union else 0.0


def best_fit_lag_ms(
    events: Sequence[Rendered_Event],
    mask: Sequence[bool],
    *,
    hop: float = ENVELOPE_HOP_S,
    search_s: float = 2.0,
) -> tuple[float, float, float]:
    """``(lag_ms, overlap_at_zero, overlap_at_lag)`` — the shift that best fits the audio.

    A lag near zero with high overlap is a synced clip. A *consistent* non-zero lag across clips is
    a constant offset, which is an arithmetic bug. A large lag whose overlap barely improves on the
    zero-shift score is the search finding a spurious alignment in continuous speech, and should be
    read as noise — which is why all three numbers are returned together and not just the lag.
    """
    if not mask:
        # ``0.0, 0.0, 0.0`` was the *perfect* lag reading, and it was returned for a clip that
        # could not be measured at all. `scripts/measure_caption_sync.py` appends the lag to a list
        # it takes the median of and prints as a verdict, so enough unmeasurable clips medianed a
        # genuine constant offset out of existence.
        raise RuntimeError("best_fit_lag_ms needs a non-empty speech mask")
    span = int(round(search_s / hop))
    at_zero = coverage_overlap(events, mask, hop=hop)
    best_score, best_shift = at_zero, 0
    for shift in range(-span, span + 1):
        score = coverage_overlap(events, mask, hop=hop, shift_frames=shift)
        if score > best_score:
            best_score, best_shift = score, shift
    return best_shift * hop * 1000.0, at_zero, best_score


# --- Word timing, measured only where the audio can prove it ------------------------------
#
# `coverage_overlap` answers "are the captions on the sound", which is the viewer's question. It
# cannot answer "is *this word* on time", and the obvious extension — nearest rising edge to each
# word start — is a trap that produced two wrong answers before this comment existed.
#
# Inside continuous speech there is no rising edge belonging to a word. Consonants begin below the
# threshold, vowels carry the energy, and adjacent words share one unbroken envelope. Anchoring to
# the *nearest* edge within a search radius therefore reports a number for every word while only
# some of those numbers mean anything, and the meaningless ones dominate: measured at 130 ms median
# on speech whose word timings were later shown accurate to within 20 ms.
#
# Worse, the noise floor is invisible in a synthetic check. On gated tones this same code reads 0 ms
# on known-true times even at 3.3 bursts per second, so the metric looks validated and is not.
#
# So this function measures **only words preceded by a real pause**. For those, and only those, the
# rising edge is ground truth rather than a guess, because silence establishes where the sound began.
# It returns fewer numbers — roughly one word in ten — and they are worth having.
#
# THE OTHER TRAP, recorded because it is more seductive than the first. CTC forced alignment
# (torchaudio MMS_FA) looks like the right reference and produced a *beautifully* consistent result:
# whisper's word starts measured 94, 104 and 105 ms early across three recordings including two
# different voices, a 12 ms spread. Consistent sign, tight spread, three sources — every heuristic
# for "this is a real systematic bias" was satisfied, and the obvious fix was a calibrated +100 ms
# shift. It would have been wrong. Checked against pause-preceded words, where the audio settles it
# without any model, whisper reads -10, -20 and +50 ms — accurate. The 100 ms belonged to MMS_FA,
# which starts a span at the first strongly-voiced frame and so skips fricative and plosive onsets.
# A shift would have injected 100 ms of error into a component that was correct.
#
# The lesson generalises past captions: a reference is a hypothesis. Two references disagreeing is
# information, and the one that can be checked against physics wins.


#: Silence before a word, in seconds, for its onset to count as independently verifiable.
#:
#: 300 ms is comfortably longer than a plosive closure (~50-100 ms) so the gap is a real pause and
#: not an artefact of articulation, and short enough to leave a usable sample on ordinary speech.
VERIFIABLE_PAUSE_S = 0.30


def rising_edges(mask: Sequence[bool], *, hop: float = ENVELOPE_HOP_S) -> list[float]:
    """Times, in seconds, where the speech mask goes from silent to sounding."""
    return [index * hop for index in range(1, len(mask)) if mask[index] and not mask[index - 1]]


def verifiable_word_errors(
    words: Sequence[object],
    media: str | Path,
    *,
    min_pause: float = VERIFIABLE_PAUSE_S,
    search: float = 0.5,
    hop: float = ENVELOPE_HOP_S,
) -> tuple[list[float], int]:
    """``(signed_errors_s, considered)`` for words whose onset the audio can settle.

    A positive error means the sound starts *after* the word claims to, i.e. the timestamp is early.

    Only words preceded by at least ``min_pause`` of silence are measured; the rest are skipped
    rather than estimated, because for them there is no edge that belongs to the word. ``considered``
    is how many qualified, so a caller can tell "accurate" from "almost nothing to go on".
    """
    mask = speech_mask(media, hop=hop)
    edges = rising_edges(mask, hop=hop)
    if not edges:
        return [], 0

    errors: list[float] = []
    considered = 0
    previous_end: float | None = None
    for word in words:
        try:
            start = float(word.start)  # type: ignore[attr-defined]
            end = float(word.end)  # type: ignore[attr-defined]
        except (AttributeError, TypeError, ValueError):
            continue
        if previous_end is not None and (start - previous_end) > min_pause:
            considered += 1
            nearby = [edge - start for edge in edges if abs(edge - start) <= search]
            if nearby:
                errors.append(min(nearby, key=abs))
        previous_end = end
    return errors, considered
