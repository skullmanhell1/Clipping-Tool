"""Cue legibility floors (C24) and linguistically-aware line breaking (C25).

`words_to_cues` has only **ceilings** -- `max_words`, `max_gap`, `max_duration`. It has no floor, so
fast speech produces cues of about 0.3 s: on screen for nine frames, which is a flicker rather than
a caption. The words are correct, the timing is correct, and the result is unreadable.

**Word spans are never touched** (R4.8). A cue's on-screen duration and its words' individual
timings are different things, and karaoke fills track the latter. Extending a cue means the line
stays up longer; it must not mean the highlight sweep slows down, or the captions stop following
speech -- which is the one thing they exist to do.

**Non-overlap outranks both floors** (R4.4, R4.5). Two cues on screen at once is a worse defect than
one cue that is briefly hard to read, because the second is a legibility problem and the first is a
rendering fault. Where the two constraints conflict the relaxation is *recorded* rather than chosen
silently.

**Both default to reproducing v0.11.0 exactly** (R4.12, R5.9). Not caution: neither floor has been
measured against anything, and `render-quality-measurement`'s M10 (caption alignment error) and M12
(preference trials) are the instruments that would justify a value. Shipping an unmeasured floor
turned on would move every golden and re-freeze the parity fixtures around a number nobody checked.
The mechanism lands here; the defaults wait for evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol, Sequence

#: Minimum seconds a cue must remain on screen. ``0.0`` disables the floor (R4.12).
#:
#: A useful value is probably near 0.8-1.0 s -- below about 0.5 s a line is gone before a reader has
#: fixated on it -- but "probably" is exactly why the default is off. M10 and M12 are what would
#: settle it.
DEFAULT_MIN_CUE_SECONDS = 0.0

#: Maximum characters per second a cue may demand. ``0.0`` disables the cap (R4.12).
#:
#: Broadcast subtitling conventions cluster around 15-20 CPS for comfortable reading. Short-form
#: captions are larger and shorter, so the right figure here is likely different and is not known.
DEFAULT_MAX_READING_RATE = 0.0

#: Words that should not be left stranded at the end of a line (C25/R5.2).
#:
#: Articles, prepositions and conjunctions bind rightwards to the phrase they introduce. Breaking
#: after "the" leaves a line ending on a word that means nothing without the next one, which is the
#: specific thing a reader notices as "badly made".
#:
#: Deliberately a small closed list of English function words rather than a part-of-speech model:
#: R5.7 forbids a checkpoint and network access, and a hand list is auditable. R5.8 is what keeps it
#: honest -- these rules apply to English only and every other language falls back to width.
BINDING_WORDS: frozenset[str] = frozenset(
    {
        # articles
        "a", "an", "the",
        # common prepositions
        "at", "by", "for", "from", "in", "into", "of", "off", "on", "onto", "out",
        "over", "to", "up", "upon", "with", "within", "without", "about", "after",
        "against", "along", "among", "around", "before", "behind", "below", "beneath",
        "beside", "between", "beyond", "during", "inside", "near", "outside", "past",
        "through", "toward", "towards", "under", "until",
        # conjunctions and complementisers that bind rightwards
        "and", "but", "or", "nor", "so", "yet", "if", "that", "than", "as", "because",
        "while", "whether",
        # possessive/determiner-like
        "my", "your", "his", "her", "its", "our", "their", "this", "these", "those",
    }
)

#: Languages the linguistic rules apply to (R5.8).
#:
#: English only, and that is a statement about the rules rather than a limitation to apologise for.
#: `BINDING_WORDS` is an English function-word list; applying it to German or French would produce
#: confident nonsense, and applying it to a language with different word order would be worse.
LINGUISTIC_LANGUAGES: frozenset[str] = frozenset({"en"})


class Fits(Protocol):
    """Anything that can answer "does this text fit a line?" -- C6's measured width."""

    def fits(self, text: str) -> bool: ...


@dataclass(frozen=True)
class Cue_Window:
    """A cue's on-screen window, kept separate from its words' own timings.

    The separation is the point. ``start``/``end`` are when the *line* is visible; the words inside
    keep their own spans untouched, so a karaoke fill still tracks speech after the window has been
    stretched (R4.8).
    """

    start: float
    end: float
    text: str
    #: Word spans as they arrived, carried through unmodified so a caller can assert that.
    word_spans: tuple[tuple[float, float], ...] = ()

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def reading_rate(self) -> float:
        """Characters per second this cue demands. ``inf`` for a zero-length window."""
        length = len(self.text.replace(" ", ""))
        return (length / self.duration) if self.duration > 0 else float("inf")


@dataclass
class Constraint_Report:
    """What the constraints did, for the clip record (R4.11)."""

    extended: int = 0
    merged: int = 0
    relaxed: list[str] = field(default_factory=list)

    @property
    def markers(self) -> list[str]:
        out: list[str] = []
        if self.extended:
            out.append(f"cue_extended:{self.extended}")
        if self.merged:
            out.append(f"cue_merged:{self.merged}")
        # R4.5: when non-overlap forced a floor to be abandoned, say which. A cue that is still
        # too short after the pass is not a failure, but it is a fact the operator should be able
        # to see rather than infer from watching.
        for name in sorted(set(self.relaxed)):
            out.append(f"cue_constraint_relaxed:{name}")
        return out


def _needed_duration(cue: Cue_Window, min_seconds: float, max_rate: float) -> float:
    """The shortest window satisfying both floors."""
    needed = max(0.0, float(min_seconds))
    if max_rate > 0:
        length = len(cue.text.replace(" ", ""))
        if length:
            needed = max(needed, length / float(max_rate))
    return needed


def apply_constraints(
    cues: Sequence[Cue_Window],
    *,
    min_seconds: float = DEFAULT_MIN_CUE_SECONDS,
    max_reading_rate: float = DEFAULT_MAX_READING_RATE,
    clip_end: float | None = None,
    fit: Fits | None = None,
) -> tuple[list[Cue_Window], Constraint_Report]:
    """Extend or merge cues so each is readable, without ever overlapping (C24).

    The order of operations is the substance:

    1. **Extend into the gap** before the next cue. Free: nothing else wanted that time.
    2. If still short, **merge with the following cue** -- but only when the merged text still fits
       the line budget (R4.7). A merge that overflows trades an unreadable cue for a truncated one.
    3. If neither works, **leave it short and record the relaxation** (R4.5). Non-overlap wins.

    A sequence already satisfying both floors is returned **bit-identical** (R4.10), which is what
    makes the disabled default a true no-op rather than an approximate one.
    """
    report = Constraint_Report()
    if not cues:
        return [], report
    if min_seconds <= 0 and max_reading_rate <= 0:
        # Disabled: return the input objects themselves, not copies. R4.10 asks for bit-identical
        # and this is the strongest available form of it.
        return list(cues), report

    working = list(cues)
    out: list[Cue_Window] = []
    index = 0
    while index < len(working):
        cue = working[index]
        needed = _needed_duration(cue, min_seconds, max_reading_rate)

        if cue.duration >= needed:
            out.append(cue)
            index += 1
            continue

        # The latest this cue may end: the next cue's start, or the clip end for the last one.
        if index + 1 < len(working):
            ceiling = working[index + 1].start
        elif clip_end is not None:
            ceiling = float(clip_end)
        else:
            ceiling = cue.start + needed

        target_end = min(cue.start + needed, ceiling)
        if target_end > cue.end:
            # 1. Extend into free time. Word spans are passed through untouched.
            cue = replace(cue, end=target_end)
            report.extended += 1

        if cue.duration >= needed:
            out.append(cue)
            index += 1
            continue

        # 2. Merge with the following cue, if the result still fits the line budget.
        if index + 1 < len(working):
            nxt = working[index + 1]
            merged_text = f"{cue.text} {nxt.text}".strip()
            if fit is None or fit.fits(merged_text):
                working[index + 1] = Cue_Window(
                    start=cue.start,
                    end=nxt.end,
                    text=merged_text,
                    word_spans=cue.word_spans + nxt.word_spans,
                )
                report.merged += 1
                index += 1  # reconsider the merged cue on the next iteration
                continue
            report.relaxed.append("merge_would_overflow")
        else:
            report.relaxed.append("clip_end")

        # 3. Deliver it short rather than overlapping. Recorded, not hidden.
        if cue.duration < needed and "clip_end" not in report.relaxed:
            report.relaxed.append("min_duration")
        out.append(cue)
        index += 1

    return out, report


# --- C25: linguistically-aware line breaking ----------------------------------------------


def _is_capitalised(token: str) -> bool:
    stripped = token.strip("\"'.,!?;:")
    return bool(stripped) and stripped[0].isupper()


def break_candidates(words: Sequence[str]) -> list[int]:
    """Every position a line could break at, best first (C25).

    A "position" ``i`` means the first line is ``words[:i]``. Positions are ranked, not filtered:
    R5.4 puts the measured width budget above any linguistic preference, so the caller walks this
    list and takes the first that *fits*. That ordering is what stops a preference from producing
    an overflowing line.

    Two rules, both about not stranding a word that means nothing alone:

    * **Never break after a binding word** (R5.2). "the / thing" leaves a line ending on an article.
    * **Never break inside a run of capitalised words** (R5.3), which is a serviceable proxy for a
      multi-word proper noun without a model. It has false positives at the start of a sentence,
      which is why it only ever *deprioritises* a break rather than forbidding it.
    """
    if len(words) < 2:
        return []

    positions = list(range(1, len(words)))

    def penalty(i: int) -> tuple[int, int]:
        score = 0
        if words[i - 1].lower().strip(".,!?;:\"'") in BINDING_WORDS:
            # Breaking here strands an article or preposition. The worst of the two faults.
            score += 2
        if _is_capitalised(words[i - 1]) and _is_capitalised(words[i]):
            # Probably inside a proper noun.
            score += 1
        # Tie-break towards the middle, which is what a width-only break approximates and what
        # looks balanced. `abs` of the distance from centre, so nearer the middle sorts first.
        centre = abs((i * 2) - len(words))
        return (score, centre)

    positions.sort(key=penalty)
    return positions


def choose_break(
    words: Sequence[str],
    *,
    fit: Fits | None = None,
    language: str = "en",
    enabled: bool = False,
) -> int | None:
    """The position to break ``words`` at, or ``None`` to leave it to width-based breaking.

    Returns ``None`` when linguistic breaking is disabled (R5.9's default), when the language has
    no rules (R5.8), or when no candidate fits -- and in every one of those cases the caller keeps
    doing exactly what it does today (R5.5).

    **No word is dropped or reordered** (R5.6): this returns an index into the sequence it was
    given, so the only thing it can influence is where the line divides.
    """
    if not enabled:
        return None
    if (language or "").lower().split("-")[0] not in LINGUISTIC_LANGUAGES:
        return None
    if len(words) < 2:
        return None

    for position in break_candidates(words):
        if fit is None:
            return position
        first = " ".join(words[:position])
        second = " ".join(words[position:])
        if fit.fits(first) and fit.fits(second):
            return position
    # R5.5: nothing linguistically preferable fits, so width-based breaking stands.
    return None
