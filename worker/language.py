"""Per-segment language detection for code-switching content (T9).

Whisper reports **one** language for a whole file. On content that switches - a bilingual
interview, a creator narrating in Hindi and quoting in English, a Spanish podcast with English
technical terms - that single label is wrong for part of every transcript, and it is wrong
*silently*: the text still appears, the timings are still right, and the only symptom is
degraded recognition on the passages in the other language.

**What this can and cannot do, precisely.** Detection here is over the *transcript text*, not the
audio. That gives two very different levels of confidence:

* **Script switching is reliable.** Devanagari, Cyrillic, Han, Hiragana/Katakana, Hangul, Arabic,
  Hebrew, Greek and Thai occupy disjoint Unicode ranges, so "this segment is not in the same
  writing system as that one" is a fact, not an estimate. This is the strong case and the one that
  matters most, because a script switch is where recognition quality falls off hardest.
* **Latin-script languages are only weakly separable.** English, Spanish, French, German,
  Portuguese and Italian share an alphabet, so the signal is function words and diacritics - which
  works on a sentence and is noise on three words. Those readings are therefore reported with a
  confidence, and a short segment gets none at all rather than a guess.

Running Whisper per segment would be the accurate approach and costs a model pass per segment. That
is not a trade this can make on its own: it belongs behind a setting, measured against `M3`'s WER
benchmark, and nothing in this module prevents it later.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: Unicode script ranges that are disjoint enough to identify by character.
#:
#: Ordered so that a more specific script is checked before a broader one - Hiragana and Katakana
#: before Han, because Japanese text mixes all three and reporting it as Chinese would be worse than
#: reporting it as Japanese.
_SCRIPT_RANGES: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
    ("hiragana", ((0x3040, 0x309F),)),
    ("katakana", ((0x30A0, 0x30FF),)),
    ("hangul", ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F))),
    ("han", ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))),
    ("devanagari", ((0x0900, 0x097F),)),
    ("cyrillic", ((0x0400, 0x04FF), (0x0500, 0x052F))),
    ("arabic", ((0x0600, 0x06FF), (0x0750, 0x077F))),
    ("hebrew", ((0x0590, 0x05FF),)),
    ("greek", ((0x0370, 0x03FF), (0x1F00, 0x1FFF))),
    ("thai", ((0x0E00, 0x0E7F),)),
    ("latin", ((0x0041, 0x024F),)),
)

#: The language a script implies, where it implies exactly one.
#:
#: Only where the mapping is genuinely unambiguous. Han is deliberately absent: it is used by
#: Chinese *and* Japanese, and guessing between them from characters alone is exactly the kind of
#: confident-and-wrong answer this module exists to avoid.
_SCRIPT_LANGUAGE: dict[str, str] = {
    "hiragana": "ja",
    "katakana": "ja",
    "hangul": "ko",
    "devanagari": "hi",
    "cyrillic": "ru",
    "hebrew": "he",
    "greek": "el",
    "thai": "th",
    "arabic": "ar",
}

#: Function words that separate the major Latin-script languages.
#:
#: Function words rather than content words because they are the highest-frequency tokens in any
#: passage and are not borrowed between languages the way nouns are - a Spanish sentence about
#: "el marketing" is still identifiably Spanish from "el".
_FUNCTION_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset({"the", "and", "is", "of", "that", "to", "it", "you", "was", "this", "with"}),
    "es": frozenset({"el", "la", "que", "de", "los", "las", "una", "por", "para", "con", "pero"}),
    "fr": frozenset({"le", "les", "des", "une", "est", "que", "pour", "dans", "avec", "mais"}),
    "de": frozenset({"der", "die", "das", "und", "ist", "nicht", "ein", "eine", "mit", "auch"}),
    "pt": frozenset({"que", "não", "uma", "com", "para", "mais", "como", "isso", "você", "está"}),
    "it": frozenset({"che", "non", "una", "per", "con", "sono", "questo", "come", "anche"}),
}

#: Minimum words before a Latin-script reading is offered at all.
#:
#: Under this, function-word overlap is coincidence: "the end" shares a token with English and
#: nothing else, and "por" appears in Portuguese and Spanish alike. Returning no answer is the
#: honest output for three words.
MIN_WORDS_FOR_LATIN = 6

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


@dataclass(frozen=True)
class LanguageReading:
    """What language a passage appears to be in, and how much to trust it."""

    language: str | None
    script: str
    #: 0..1. High for a script identification, moderate for a function-word one, 0 for a guess
    #: declined.
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "script": self.script,
            "confidence": round(self.confidence, 3),
        }


def _script_of(char: str) -> str | None:
    code = ord(char)
    for name, ranges in _SCRIPT_RANGES:
        for low, high in ranges:
            if low <= code <= high:
                return name
    return None


def dominant_script(text: str) -> tuple[str, float]:
    """The most common script in ``text`` and the fraction of letters it accounts for.

    Combining characters and marks are ignored: Devanagari matras and Arabic diacritics would
    otherwise inflate their own script's count relative to a Latin passage of the same length.
    """
    counts: Counter[str] = Counter()
    for char in text or "":
        if unicodedata.category(char).startswith("M"):
            continue
        script = _script_of(char)
        if script:
            counts[script] += 1
    if not counts:
        return ("unknown", 0.0)
    total = sum(counts.values())
    script, count = counts.most_common(1)[0]
    return (script, count / total)


def detect(text: str) -> LanguageReading:
    """Detect the language of one passage (T9).

    Script identification comes first because it is the reliable half. Only when the script is Latin
    - where the writing system says nothing about the language - does the function-word check run,
    and it declines rather than guesses on short input.
    """
    body = (text or "").strip()
    if not body:
        return LanguageReading(None, "unknown", 0.0)

    script, share = dominant_script(body)
    if script in _SCRIPT_LANGUAGE and share >= 0.5:
        # A writing system is a fact about the text, so this is confident - though `share` is
        # carried through, because a passage that is only just majority-Devanagari is a mixed
        # sentence rather than a clean switch.
        return LanguageReading(_SCRIPT_LANGUAGE[script], script, min(1.0, 0.6 + share * 0.4))
    if script == "han" and share >= 0.5:
        # Chinese or Japanese-without-kana. Reported as the script with no language, which is the
        # true answer: guessing between them from Han characters alone is not possible here.
        return LanguageReading(None, "han", 0.0)
    if script != "latin":
        return LanguageReading(None, script, 0.0)

    words = [word.lower() for word in _WORD_RE.findall(body)]
    if len(words) < MIN_WORDS_FOR_LATIN:
        return LanguageReading(None, "latin", 0.0)

    scores = {
        language: sum(1 for word in words if word in vocabulary)
        for language, vocabulary in _FUNCTION_WORDS.items()
    }
    best = max(scores, key=lambda key: scores[key])
    hits = scores[best]
    if hits == 0:
        return LanguageReading(None, "latin", 0.0)

    runner_up = max((v for k, v in scores.items() if k != best), default=0)
    # A margin, not a raw count: two languages tying on function words is not evidence for either,
    # and Spanish/Portuguese overlap heavily enough that a one-hit lead means nothing.
    margin = (hits - runner_up) / max(1, hits)
    confidence = min(0.75, (hits / len(words)) * 2.0) * margin
    if confidence < 0.15:
        return LanguageReading(None, "latin", 0.0)
    return LanguageReading(best, "latin", confidence)


def detect_segments(segments: Iterable[Any]) -> list[LanguageReading]:
    """A reading per segment, in order."""
    return [detect(str(getattr(segment, "text", "") or "")) for segment in segments]


def code_switching(segments: Iterable[Any], *, min_confidence: float = 0.4) -> list[dict[str, Any]]:
    """Segments whose language differs from the transcript's majority (T9).

    Only confident readings are reported. The output is a *list of suspected switches* rather than a
    corrected transcript, because that is what can be justified: this cannot re-run recognition, so
    the useful product is telling an operator which passages their single-language transcription
    probably got wrong.
    """
    # Materialised because this walks the segments twice - once to read them and once to pair the
    # readings back up with their timings. Given a generator, the second walk would find it already
    # exhausted and report no switches at all, which is the failure mode that looks like "this
    # content just isn't bilingual".
    segments = list(segments)
    readings = list(detect_segments(segments))
    confident = [r for r in readings if r.language and r.confidence >= min_confidence]
    if len(confident) < 2:
        return []

    counts = Counter(r.language for r in confident)
    majority, _count = counts.most_common(1)[0]
    switches: list[dict[str, Any]] = []
    for segment, reading in zip(segments, readings, strict=True):
        if not reading.language or reading.confidence < min_confidence:
            continue
        if reading.language == majority:
            continue
        switches.append(
            {
                "start": float(getattr(segment, "start", 0.0) or 0.0),
                "end": float(getattr(segment, "end", 0.0) or 0.0),
                "language": reading.language,
                "script": reading.script,
                "confidence": round(reading.confidence, 3),
                "majority": majority,
            }
        )
    return switches
