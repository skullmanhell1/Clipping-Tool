"""Word error rate, for comparing ASR model sizes on your own footage (M3).

``T1`` raised the default model from ``base`` to ``small`` on the reasoning that ``base`` is a
noticeable accuracy step down and a mis-transcribed word is burned into the video. That
reasoning is sound and it is still not a measurement. This module is what turns it into one:
transcribe the same footage with each model size, score against a reference, and see what the
extra minutes actually buy on *your* audio - accented speech, a noisy room, domain jargon -
rather than on the benchmark suite the model was published against.

**Normalisation is where a WER harness is usually wrong**, and it is wrong in the flattering
direction. Comparing raw strings counts "don't" versus "dont", "10" versus "ten" and a trailing
full stop as errors, so every model looks far worse than it is and the *differences* between
them drown in punctuation noise. So text is normalised before alignment, and each step is
listed below with what it deliberately does not do.

What this cannot tell you: whether the captions *read* well. WER weights every word equally, so
a missed "the" costs exactly what a missed brand name costs, and a model that gets every
content word right while dropping articles can score worse than one that garbles a name. Read
the substitution list, not just the number.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

#: Numbers written as digits are folded to words so "10" and "ten" do not count as an error.
#: Only 0-20 and the round tens: past that the mapping needs a real number-to-words library,
#: and an incomplete mapping is worse than none because it would fold some numbers and not
#: others, making the score depend on which numbers the speaker happened to use.
_NUMBER_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
    "10": "ten",
    "11": "eleven",
    "12": "twelve",
    "13": "thirteen",
    "14": "fourteen",
    "15": "fifteen",
    "16": "sixteen",
    "17": "seventeen",
    "18": "eighteen",
    "19": "nineteen",
    "20": "twenty",
    "30": "thirty",
    "40": "forty",
    "50": "fifty",
    "60": "sixty",
    "70": "seventy",
    "80": "eighty",
    "90": "ninety",
}

#: Contractions are expanded rather than stripped of their apostrophe, because "dont" is not a
#: word and would then be a substitution against any reference that spells it properly.
_CONTRACTIONS = {
    "dont": "do not",
    "doesnt": "does not",
    "didnt": "did not",
    "wont": "will not",
    "cant": "can not",
    "cannot": "can not",
    "couldnt": "could not",
    "shouldnt": "should not",
    "wouldnt": "would not",
    "isnt": "is not",
    "arent": "are not",
    "wasnt": "was not",
    "werent": "were not",
    "hasnt": "has not",
    "havent": "have not",
    "hadnt": "had not",
    "im": "i am",
    "ive": "i have",
    "ill": "i will",
    "id": "i would",
    "youre": "you are",
    "youve": "you have",
    "youll": "you will",
    "hes": "he is",
    "shes": "she is",
    "its": "it is",
    "thats": "that is",
    "theres": "there is",
    "theyre": "they are",
    "theyve": "they have",
    "were": "we are",
    "weve": "we have",
    "lets": "let us",
    "gonna": "going to",
    "wanna": "want to",
    "kinda": "kind of",
}

_PUNCT_RE = re.compile(r"[^\w\s']", re.UNICODE)
_WS_RE = re.compile(r"\s+")

#: Every character that means "apostrophe", folded to the ASCII one.
#:
#: NFKC does **not** do this - U+2019 (right single quotation mark) is left alone, which was a
#: real bug here: a reference typed in a word processor renders "don't" with a smart quote, the
#: punctuation strip then split it into "don" and "t", and the contraction table never matched.
#: Two substitutions per contraction, on every reference written by a human rather than pasted
#: from a terminal - and it would have inflated every model's score equally, so the comparison
#: would still have looked plausible.
_APOSTROPHES = str.maketrans(
    {
        "\u2019": "'",  # right single quotation mark
        "\u2018": "'",  # left single quotation mark
        "\u02bc": "'",  # modifier letter apostrophe
        "\u00b4": "'",  # acute accent used as an apostrophe
        "\u0060": "'",  # grave accent used as an apostrophe
        "\uff07": "'",  # fullwidth apostrophe
    }
)


def normalise(text: str) -> list[str]:
    """Normalise ``text`` to a list of comparable word tokens.

    In order: Unicode NFKC, fold every apostrophe variant to the ASCII one (NFKC does *not*
    do this - see :data:`_APOSTROPHES`), lower-case, strip punctuation, drop apostrophes,
    expand contractions, fold small numbers to words.

    Deliberately **not** done: stemming, stop-word removal and spelling correction. Each would
    hide a real transcription error - "engineer" for "engineers" is a mistake a viewer sees, and
    a stemmer would score it as correct.
    """
    if not text:
        return []
    text = unicodedata.normalize("NFKC", str(text)).lower()
    text = text.translate(_APOSTROPHES)
    text = _PUNCT_RE.sub(" ", text)
    text = text.replace("'", "")
    tokens: list[str] = []
    for token in _WS_RE.split(text.strip()):
        if not token:
            continue
        if token in _CONTRACTIONS:
            tokens.extend(_CONTRACTIONS[token].split())
        elif token in _NUMBER_WORDS:
            tokens.append(_NUMBER_WORDS[token])
        else:
            tokens.append(token)
    return tokens


@dataclass(frozen=True)
class WerResult:
    """The outcome of one reference/hypothesis comparison."""

    reference_words: int
    substitutions: int
    deletions: int
    insertions: int
    #: The substituted pairs, most frequent first. The diagnostic the number cannot express:
    #: a WER of 8% made of missed articles is a different product than one made of mangled
    #: names, and only this distinguishes them.
    examples: tuple[tuple[str, str], ...] = field(default=())

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def wer(self) -> float:
        """Errors per reference word. Can exceed 1.0 - insertions are unbounded."""
        if self.reference_words == 0:
            return 0.0 if self.errors == 0 else 1.0
        return self.errors / self.reference_words

    def to_dict(self) -> dict:
        return {
            "wer": round(self.wer, 4),
            "reference_words": self.reference_words,
            "substitutions": self.substitutions,
            "deletions": self.deletions,
            "insertions": self.insertions,
            "examples": [list(pair) for pair in self.examples[:10]],
        }


def word_error_rate(reference: str, hypothesis: str, *, max_examples: int = 10) -> WerResult:
    """Levenshtein-aligned WER between ``reference`` and ``hypothesis``.

    Full dynamic-programming alignment with a traceback, not just the edit distance, because
    the *kind* of error is what makes the number actionable - deletions mean the model is
    dropping speech (usually a VAD problem, see ``T5``), substitutions mean it is mis-hearing
    words (usually a vocabulary problem, see ``T4``), and the two call for opposite fixes.

    Memory is O(len(ref) x len(hyp)); a full-length transcript of a long video is tens of
    thousands of words, which is a few hundred MB at worst. That is acceptable for an offline
    benchmark and would not be for anything on the render path.
    """
    ref = normalise(reference)
    hyp = normalise(hypothesis)
    n, m = len(ref), len(hyp)

    if n == 0:
        return WerResult(0, 0, 0, m)
    if m == 0:
        return WerResult(n, 0, n, 0)

    # costs[i][j] = edit distance between ref[:i] and hyp[:j]
    costs = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        costs[i][0] = i
    for j in range(m + 1):
        costs[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                costs[i][j] = costs[i - 1][j - 1]
            else:
                costs[i][j] = 1 + min(
                    costs[i - 1][j - 1],  # substitution
                    costs[i - 1][j],  # deletion
                    costs[i][j - 1],  # insertion
                )

    subs = dels = ins = 0
    pairs: list[tuple[str, str]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref[i - 1] == hyp[j - 1] and costs[i][j] == costs[i - 1][j - 1]:
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and costs[i][j] == costs[i - 1][j - 1] + 1:
            subs += 1
            pairs.append((ref[i - 1], hyp[j - 1]))
            i, j = i - 1, j - 1
        elif i > 0 and costs[i][j] == costs[i - 1][j] + 1:
            dels += 1
            i -= 1
        else:
            ins += 1
            j -= 1

    counts: dict[tuple[str, str], int] = {}
    for pair in pairs:
        counts[pair] = counts.get(pair, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    examples = tuple(pair for pair, _ in ranked[:max_examples])

    return WerResult(
        reference_words=n,
        substitutions=subs,
        deletions=dels,
        insertions=ins,
        examples=examples,
    )


def aggregate(results: Iterable[WerResult]) -> WerResult:
    """Pool several results into one.

    Errors and reference words are summed *before* dividing, rather than the per-file rates
    being averaged. Averaging rates weights a ten-second clip the same as an hour-long talk,
    which lets one short difficult file dominate a figure meant to describe the whole dataset.
    """
    items = list(results)
    if not items:
        return WerResult(0, 0, 0, 0)
    counts: dict[tuple[str, str], int] = {}
    for result in items:
        for pair in result.examples:
            counts[pair] = counts.get(pair, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return WerResult(
        reference_words=sum(r.reference_words for r in items),
        substitutions=sum(r.substitutions for r in items),
        deletions=sum(r.deletions for r in items),
        insertions=sum(r.insertions for r in items),
        examples=tuple(pair for pair, _ in ranked[:10]),
    )


def format_comparison(rows: Sequence[tuple[str, WerResult]]) -> str:
    """A readable table of ``(label, result)`` rows, best WER first.

    The relative column is the point of the whole exercise: the absolute WER of a model on your
    footage is interesting once, whereas "medium costs 3x the time for 1.2 points" is the
    decision actually being made.
    """
    if not rows:
        return "no results"
    ranked = sorted(rows, key=lambda row: row[1].wer)
    best = ranked[0][1].wer
    lines = [
        f"{'model':<14} {'WER':>7} {'vs best':>8} {'sub':>6} {'del':>6} {'ins':>6} {'words':>7}",
        "-" * 60,
    ]
    for label, result in ranked:
        delta = result.wer - best
        lines.append(
            f"{label:<14} {result.wer:7.2%} {delta:+8.2%} {result.substitutions:6d} "
            f"{result.deletions:6d} {result.insertions:6d} {result.reference_words:7d}"
        )
    worst = ranked[-1][1]
    if worst.deletions > worst.substitutions * 2:
        lines.append("")
        lines.append(
            "Deletions dominate: the model is dropping speech rather than mis-hearing it, "
            "which points at VAD (T5) rather than at vocabulary (T4)."
        )
    elif worst.substitutions > worst.deletions * 2:
        lines.append("")
        lines.append(
            "Substitutions dominate: the model is mis-hearing words it does hear. Check the "
            "examples for recurring names or jargon and put them in the job's vocabulary (T4)."
        )
    return "\n".join(lines)
