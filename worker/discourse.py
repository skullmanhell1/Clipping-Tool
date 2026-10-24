"""Text-structure signals for clip selection: S7, S8, S12.

Selection measures how a moment is *delivered* - pace (S4), energy (S2), how promptly it opens
(S6) - and nothing about what it says. Three properties of the words themselves predict whether a
window works as a standalone clip, and all three are computable from the transcript this pipeline
already has:

* **S7 - structure.** A question with its answer, or an enumeration ("three things you need to
  know", "here's why"), is a self-contained unit by construction. It opens with an implicit promise
  and closes by keeping it, which is the shape of a clip rather than an excerpt.
* **S8 - emotional intensity.** How strongly the passage is worded, from the vocabulary rather than
  the acoustics. Energy (S2) already measures how loudly someone spoke; this measures whether they
  had anything emphatic to say, which are different things - a shouted list of ingredients and a
  quietly devastating sentence sit at opposite corners of the two.
* **S12 - standalone completeness.** Whether the window makes sense without what came before. A
  clip opening on "and *that's* why he did it" is a fragment of a conversation, and no amount of
  pace or energy makes it publishable.

**Deterministic, not LLM-scored.** S8 asks for sentiment "via the LLM". These are lexical rules
instead, for the reason S4 and S10 record: a per-segment model call costs a request per segment per
job, and - more importantly - nothing here can yet tell whether a *scored* signal improves selection,
because the S1 labelled dataset does not exist. Shipping a paid, unvalidated signal would make an
improvement and a regression indistinguishable while charging for the privilege. The lexical version
is free, inspectable, and measured by the same benchmark when it lands; swapping in a model call is
then a change with a number attached.

Every function is pure and total: adversarial input returns a neutral reading rather than raising,
because these run over transcript text that has already been through ASR and a hallucination filter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_WORD_RE = re.compile(r"[A-Za-z']+")

# --------------------------------------------------------------------------- #
# S7 - question / answer and list structure
# --------------------------------------------------------------------------- #

#: Words that open a genuine question. Deliberately not just "?" - ASR punctuation is unreliable,
#: and Whisper omits question marks on rising-intonation questions fairly often.
_QUESTION_OPENERS = frozenset(
    {
        "how",
        "why",
        "what",
        "when",
        "where",
        "who",
        "which",
        "whose",
        "is",
        "are",
        "was",
        "were",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "will",
        "have",
        "has",
        "am",
    }
)

#: Phrases that promise an enumeration. These are the strongest self-contained-clip signal in the
#: set: a speaker who says "three things" has committed to a structure with an end.
_LIST_MARKERS: tuple[str, ...] = (
    "here's why",
    "heres why",
    "here is why",
    "here's how",
    "heres how",
    "here is how",
    "the reason",
    "the reasons",
    "first of all",
    "firstly",
    "secondly",
    "thirdly",
    "number one",
    "number two",
    "number three",
    "step one",
    "step two",
    "step three",
    "the problem is",
    "the point is",
    "the trick is",
    "the key is",
    "let me explain",
    "let me show you",
    "three things",
    "two things",
    "four things",
    "five things",
    "three reasons",
    "two reasons",
    "three ways",
    "two ways",
    "five ways",
    "the bottom line",
)

#: Number words that make "N <plural noun>" read as a promise of a list.
_COUNT_WORDS = frozenset({"two", "three", "four", "five", "six", "seven", "ten"})

#: Conversational tags that carry a question mark without asking anything.
#:
#: These end an enormous share of ordinary speech, and a question mark is the *only* thing that
#: distinguishes "we rebuilt the whole thing over one weekend, you know?" from a real question. An
#: unanswered question is the lowest-scoring shape here, so reading these as questions would push
#: most conversational speech to the bottom of the ranking - a penalty applied to the way people
#: talk rather than to anything about the clip.
_TAG_QUESTIONS = frozenset(
    {
        "right",
        "yeah",
        "yes",
        "no",
        "okay",
        "ok",
        "see",
        "get it",
        "innit",
        "you know",
        "you know what i mean",
        "know what i mean",
        "if that makes sense",
        "isn't it",
        "isnt it",
        "is it",
        "don't you",
        "dont you",
        "do you",
        "aren't they",
        "arent they",
        "you feel me",
        "make sense",
        "does that make sense",
    }
)


def _strip_tag_question(sentence: str) -> str:
    """``sentence`` with a trailing conversational tag (and its question mark) removed.

    Returns ``""`` when the whole sentence was a tag, which is how the caller distinguishes
    "this asks nothing at all" from "this asks something once the tag is off the end".
    Sentences that do not end in ``?`` are returned unchanged - a tag without one is not
    mistakable for a question in the first place.
    """
    stripped = (sentence or "").strip()
    if not stripped.endswith("?"):
        return stripped
    core = stripped[:-1].strip()
    if " ".join(_WORD_RE.findall(core.lower())) in _TAG_QUESTIONS:
        return ""
    head, _sep, tail = core.rpartition(",")
    if head.strip() and " ".join(_WORD_RE.findall(tail.lower())) in _TAG_QUESTIONS:
        return head.strip()
    return stripped


@dataclass(frozen=True)
class Structure:
    """S7: what shape this passage has."""

    question: bool = False
    answered: bool = False
    enumeration: bool = False
    markers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def score(self) -> float:
        """0..1. A question *with* an answer scores highest, an unanswered one lowest.

        The ordering is the whole point. An unanswered question is the *worst* clip shape, not a
        neutral one: it opens a loop the clip never closes, which is the single most common way an
        auto-cut moment feels unfinished. So it scores below a passage with no structure at all.
        """
        if self.question and self.answered:
            return 1.0
        if self.enumeration:
            return 0.8
        if self.question:
            return 0.25
        return 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "structure_question": self.question,
            "structure_answered": self.answered,
            "structure_enumeration": self.enumeration,
            "structure_score": round(self.score, 3),
        }


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [part.strip() for part in parts if part.strip()]


def detect_structure(text: str) -> Structure:
    """Detect question/answer and list structure in ``text`` (S7)."""
    body = (text or "").strip()
    if not body:
        return Structure()

    lowered = body.lower()
    sentences = _sentences(body)
    words = _WORD_RE.findall(lowered)

    markers = tuple(marker for marker in _LIST_MARKERS if marker in lowered)

    # "three things", "five ways" - a count word followed by a plural noun.
    counted = False
    for index, word in enumerate(words[:-1]):
        if word in _COUNT_WORDS and words[index + 1].endswith("s"):
            counted = True
            break

    question = False
    answered = False
    for position, sentence in enumerate(sentences):
        # Decided on the sentence with any trailing conversational tag removed, so the question
        # mark on "..., you know?" does not make an ordinary statement the worst-scoring shape
        # there is. A sentence that was *entirely* a tag asks nothing and is skipped.
        body = _strip_tag_question(sentence)
        tokens = _WORD_RE.findall(body.lower())
        if not tokens:
            continue
        is_question = body.rstrip().endswith("?") or tokens[0] in _QUESTION_OPENERS
        if is_question:
            question = True
            # Answered when *substantive* speech follows. A trailing "right?" or "you know?" is
            # not an unanswered question, it is a filler tag - and treating it as one would
            # penalise ordinary conversational speech everywhere it appears.
            following = sum(len(_WORD_RE.findall(s)) for s in sentences[position + 1 :])
            if following >= 4:
                answered = True
            break

    return Structure(
        question=question,
        answered=answered,
        enumeration=bool(markers) or counted,
        markers=markers,
    )


# --------------------------------------------------------------------------- #
# S8 - emotional intensity
# --------------------------------------------------------------------------- #

#: Words that mark a passage as emphatic. Grouped by strength rather than scored individually,
#: because a hand-tuned per-word weight is false precision on a list this short.
_STRONG = frozenset(
    {
        "insane",
        "crazy",
        "unbelievable",
        "shocking",
        "terrifying",
        "devastating",
        "incredible",
        "amazing",
        "brutal",
        "horrific",
        "outrageous",
        "furious",
        "obsessed",
        "desperate",
        "destroyed",
        "exploded",
        "collapsed",
        "screaming",
        "hate",
        "love",
        "terrified",
        "humiliated",
        "betrayed",
        "life-changing",
    }
)
_MODERATE = frozenset(
    {
        "never",
        "always",
        "everything",
        "nothing",
        "everyone",
        "nobody",
        "worst",
        "best",
        "biggest",
        "hardest",
        "easiest",
        "fastest",
        "huge",
        "massive",
        "tiny",
        "completely",
        "totally",
        "absolutely",
        "literally",
        "actually",
        "honestly",
        "seriously",
        "really",
        "must",
        "need",
        "wrong",
        "right",
        "failed",
        "won",
        "lost",
    }
)

#: Above this fraction of upper-case words, the passage is shouting.
_CAPS_FRACTION = 0.3


@dataclass(frozen=True)
class Intensity:
    """S8: how emphatic the wording is, independent of how loudly it was said."""

    score: float = 0.5
    strong_terms: int = 0
    exclamations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "intensity_score": round(self.score, 3),
            "intensity_strong_terms": self.strong_terms,
        }


def emotional_intensity(text: str) -> Intensity:
    """Lexical emotional intensity of ``text``, 0..1 with 0.5 neutral (S8).

    Density-based rather than count-based: a three-minute passage containing two strong words is
    not more emphatic than a ten-second one containing the same two, and a raw count would rank it
    higher purely for being longer - the same defect S11 exists to remove from the fallback.
    """
    body = (text or "").strip()
    words = _WORD_RE.findall(body.lower())
    if len(words) < 4:
        # Too little text for a density to mean anything.
        return Intensity()

    strong = sum(1 for word in words if word in _STRONG)
    moderate = sum(1 for word in words if word in _MODERATE)
    exclamations = body.count("!")

    original_words = _WORD_RE.findall(body)
    shouted = sum(1 for word in original_words if len(word) > 2 and word.isupper())
    caps_fraction = shouted / max(1, len(original_words))

    density = (strong * 2.0 + moderate) / len(words)
    value = 0.5 + min(0.4, density * 3.0)
    value += min(0.1, exclamations * 0.05)
    if caps_fraction >= _CAPS_FRACTION:
        value += 0.05

    return Intensity(
        score=max(0.0, min(1.0, value)),
        strong_terms=strong,
        exclamations=exclamations,
    )


# --------------------------------------------------------------------------- #
# S12 - standalone completeness
# --------------------------------------------------------------------------- #

#: Openers that make a clip a fragment of a conversation it does not contain.
#:
#: Two kinds: conjunctions continuing a previous sentence, and back-references to something said
#: earlier. Both are fatal to a standalone clip and invisible to every other signal - a window can
#: be fast, loud, well-paced and still open on "and that's why".
_DANGLING_OPENERS: tuple[str, ...] = (
    "and",
    "but",
    "so",
    "because",
    "which",
    "or",
    "nor",
    "yet",
    "also",
    "then",
    "anyway",
    "however",
    "therefore",
    "meanwhile",
    "plus",
    "besides",
)

#: Back-reference openers: a pronoun or demonstrative with no antecedent inside the clip.
_DEICTIC_OPENERS: tuple[str, ...] = (
    "this",
    "that",
    "these",
    "those",
    "it",
    "they",
    "he",
    "she",
    "him",
    "her",
    "them",
    "his",
    "hers",
    "their",
)

#: Phrases that explicitly point outside the clip.
_BACK_REFERENCES: tuple[str, ...] = (
    "as i said",
    "as i mentioned",
    "like i said",
    "going back to",
    "as we discussed",
    "remember when",
    "earlier i",
    "we talked about",
)


@dataclass(frozen=True)
class Standalone:
    """S12: whether the window makes sense on its own."""

    score: float = 0.5
    dangling_opener: bool = False
    deictic_opener: bool = False
    back_reference: bool = False
    unfinished: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "standalone_score": round(self.score, 3),
            "standalone_dangling_opener": self.dangling_opener,
            "standalone_back_reference": self.back_reference,
            "standalone_unfinished": self.unfinished,
        }


def standalone_completeness(text: str) -> Standalone:
    """Whether ``text`` stands alone without prior context, 0..1 (S12).

    The penalties are not equal, and the ordering is the substance of this function. A back
    reference ("as I said") is *stated* dependence on missing context and is the heaviest. A
    dangling conjunction is nearly as bad. A demonstrative opener is a weaker signal because
    "this is the part where..." is a perfectly good clip opening - the word is often forward-looking
    rather than back-looking, and penalising it as heavily as "and" would reject good clips.

    An unfinished ending is penalised least: it is the one failure the *boundary* logic (S9 snapping,
    AU7 trimming) can still fix after selection, whereas nothing downstream can supply missing
    context.
    """
    body = (text or "").strip()
    if not body:
        return Standalone()

    lowered = body.lower()
    words = _WORD_RE.findall(lowered)
    if not words:
        return Standalone()

    first = words[0]
    dangling = first in _DANGLING_OPENERS
    deictic = first in _DEICTIC_OPENERS
    # Only the opening of the clip: a back reference in the middle is a speaker recapping, which is
    # normal and often *helps* a standalone clip.
    opening = " ".join(words[:8])
    back_reference = any(phrase in opening for phrase in _BACK_REFERENCES)
    unfinished = not body.rstrip().endswith((".", "!", "?", '"', "'", "”"))

    value = 1.0
    if back_reference:
        value -= 0.5
    if dangling:
        value -= 0.4
    elif deictic:
        value -= 0.15
    if unfinished:
        value -= 0.1

    return Standalone(
        score=max(0.0, min(1.0, value)),
        dangling_opener=dangling,
        deictic_opener=deictic,
        back_reference=back_reference,
        unfinished=unfinished,
    )


# --------------------------------------------------------------------------- #
# annotation
# --------------------------------------------------------------------------- #


def describe(text: str) -> dict[str, Any]:
    """All three signals for one passage, as a features dict."""
    return {
        **detect_structure(text).to_dict(),
        **emotional_intensity(text).to_dict(),
        **standalone_completeness(text).to_dict(),
    }


def annotate_candidates(candidates: Iterable[Any]) -> None:
    """Attach S7/S8/S12 features to each candidate's ``features`` dict, in place.

    Never touches ``score``, matching the invariant S4 established and its tests pin: annotators
    measure and only the fallback's own scorer decides. A candidate with no text is left alone
    rather than given neutral values, so "not measured" stays distinguishable from "measured as
    average" - the distinction S4's ``reliable`` flag exists for.
    """
    for candidate in candidates:
        features = getattr(candidate, "features", None)
        if features is None:
            continue
        text = str(getattr(candidate, "text", "") or "").strip()
        if not text:
            continue
        features.update(describe(text))


def prompt_note(text: str) -> str | None:
    """A short phrase describing this passage's structure, for the S10 prompt annotation.

    Words rather than numbers, for the reason S10 records: the model is picking moments, not doing
    arithmetic, and a raw score invites it to invent a formula from a scale it cannot calibrate.
    Only *departures* are described, so an unremarkable segment renders exactly as it did before and
    the annotated ones stand out - which is the entire signal.
    """
    structure = detect_structure(text)
    standalone = standalone_completeness(text)
    intensity = emotional_intensity(text)

    notes: list[str] = []
    if structure.question and structure.answered:
        notes.append("answers a question")
    elif structure.question:
        notes.append("asks an unanswered question")
    if structure.enumeration:
        notes.append("lists points")
    if standalone.back_reference:
        notes.append("refers back")
    elif standalone.dangling_opener:
        notes.append("starts mid-thought")
    if intensity.score >= 0.8:
        notes.append("emphatic")
    return ", ".join(notes) if notes else None
