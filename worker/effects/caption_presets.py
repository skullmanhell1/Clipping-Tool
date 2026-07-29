"""Caption preset model and pure keyword-highlight planner.

This module holds the declarative, serializable caption-style model
(:class:`CaptionColors`, :class:`CaptionPreset`), the built-in preset registry
(:data:`BUILTIN_PRESETS`), name/dict resolution helpers, and the pure
keyword-highlight planner (:func:`plan_keywords`).

Design goals:
    * ``CaptionPreset`` is a frozen dataclass that round-trips through
      ``to_dict`` / ``from_dict`` (including the nested ``CaptionColors``).
    * The three legacy static templates (``karaoke`` / ``boxed`` / ``minimal``)
      are expressed as presets with animation styles matching current behaviour,
      alongside new animated presets (``pop`` / ``typewriter`` / ``hormozi``).
    * Unknown or malformed presets never raise — they fall back to ``karaoke``
      and report the substitution.
    * ``plan_keywords`` is a pure function: deterministic offline rules by
      default, with an optional injected LLM client whose selections are merged
      (union) with the deterministic set. Any LLM failure degrades gracefully to
      the deterministic set only, and no LLM call is made when ``use_ai`` is
      false.

Nothing here performs I/O or touches ffmpeg — it is safe to import and unit-test
in isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Animation styles
# ---------------------------------------------------------------------------
AnimationStyle = str  # "none" | "pop" | "typewriter" | "karaoke_fill"

VALID_ANIMATIONS: frozenset[str] = frozenset(
    {"none", "pop", "typewriter", "karaoke_fill"}
)
VALID_POSITIONS: frozenset[str] = frozenset({"bottom", "center", "top"})


# ---------------------------------------------------------------------------
# Colour + preset models
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CaptionColors:
    """Caption colour scheme in ASS ``&HAABBGGRR`` notation."""

    primary: str = "&H00FFFFFF"
    highlight: str = "&H0000E5FF"
    outline: str = "&H00000000"
    box: str = "&H80000000"

    def to_dict(self) -> dict:
        """Serialize to a plain dict (Req 6.1)."""
        return {
            "primary": self.primary,
            "highlight": self.highlight,
            "outline": self.outline,
            "box": self.box,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CaptionColors":
        """Build from a plain dict, ignoring unknown keys (Req 6.2)."""
        data = data or {}
        defaults = cls()
        return cls(
            primary=str(data.get("primary", defaults.primary)),
            highlight=str(data.get("highlight", defaults.highlight)),
            outline=str(data.get("outline", defaults.outline)),
            box=str(data.get("box", defaults.box)),
        )


@dataclass(frozen=True)
class CaptionPreset:
    """A named, declarative caption style bundle.

    Fields mirror the design's serializable definition: animation style, font,
    colours, default position, keyword-highlight policy, in-caption emoji
    policy, and border style (1 = outline, 3 = opaque box).
    """

    name: str
    animation: AnimationStyle = "none"
    # A bundled static heavy face (assets/fonts.json), not "Arial": Arial is not
    # installed on any Linux host, so every preset inheriting this default rendered in
    # whatever the host substituted, at synthesised bold (C1). "Poppins ExtraBold" is
    # named as its own family on purpose - ASS can only express bold on/off, which
    # fontconfig reads as weight 200, so ExtraBold (205) is unreachable any other way.
    font: str = "Poppins ExtraBold"
    font_size: int = 84
    colors: CaptionColors = field(default_factory=CaptionColors)
    position: str = "bottom"
    highlight_keywords: bool = False
    highlight_scale: float = 1.18
    emoji_inline: bool = False
    border_style: int = 1

    def to_dict(self) -> dict:
        """Serialize the preset (nested colours included) to a plain dict."""
        return {
            "name": self.name,
            "animation": self.animation,
            "font": self.font,
            "font_size": self.font_size,
            "colors": self.colors.to_dict(),
            "position": self.position,
            "highlight_keywords": self.highlight_keywords,
            "highlight_scale": self.highlight_scale,
            "emoji_inline": self.emoji_inline,
            "border_style": self.border_style,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CaptionPreset":
        """Reconstruct a preset from :meth:`to_dict` output (round-trips)."""
        defaults = cls(name="")
        colors_data = data.get("colors")
        colors = (
            CaptionColors.from_dict(colors_data)
            if isinstance(colors_data, dict)
            else CaptionColors()
        )
        return cls(
            name=str(data.get("name", defaults.name)),
            animation=str(data.get("animation", defaults.animation)),
            font=str(data.get("font", defaults.font)),
            font_size=int(data.get("font_size", defaults.font_size)),
            colors=colors,
            position=str(data.get("position", defaults.position)),
            highlight_keywords=bool(
                data.get("highlight_keywords", defaults.highlight_keywords)
            ),
            highlight_scale=float(
                data.get("highlight_scale", defaults.highlight_scale)
            ),
            emoji_inline=bool(data.get("emoji_inline", defaults.emoji_inline)),
            border_style=int(data.get("border_style", defaults.border_style)),
        )


# ---------------------------------------------------------------------------
# Built-in registry
# ---------------------------------------------------------------------------
# The three existing static templates are expressed as presets with animation
# styles matching current behaviour (Req 1.1); the rest are new animated
# presets (Req 1.2).
# Fonts are named per preset rather than left on the default where the look calls for a
# different face. Every name here is a family in ``assets/fonts.json`` that was
# verified to resolve to its own file (libass ``fontselect:`` at -loglevel verbose), so a
# preset can no longer request a face that does not exist.
BUILTIN_PRESETS: dict[str, CaptionPreset] = {
    "karaoke": CaptionPreset("karaoke", animation="karaoke_fill", border_style=1),
    "boxed": CaptionPreset(
        "boxed", animation="none", border_style=3, font="Archivo Black"
    ),
    "minimal": CaptionPreset(
        "minimal", animation="none", font_size=76, font="Poppins"
    ),
    "pop": CaptionPreset("pop", animation="pop", highlight_keywords=True),
    "typewriter": CaptionPreset("typewriter", animation="typewriter", font="Poppins"),
    "hormozi": CaptionPreset(
        "hormozi",
        animation="pop",
        highlight_keywords=True,
        emoji_inline=True,
        font_size=96,
        position="center",
        # The look this preset is named for is heavy condensed all-caps.
        font="Anton",
    ),
}

# The documented fallback preset used for any unknown/malformed request.
FALLBACK_PRESET_NAME = "karaoke"


def resolve_preset(name: Any) -> tuple[CaptionPreset, bool]:
    """Resolve a preset by name.

    Returns ``(preset, substituted)``: a known name yields ``(preset, False)``;
    an unknown/empty/non-string name falls back to ``(karaoke, True)``
    (Reqs 1.5, 6.4).
    """
    if isinstance(name, str) and name in BUILTIN_PRESETS:
        return BUILTIN_PRESETS[name], False
    return BUILTIN_PRESETS[FALLBACK_PRESET_NAME], True


def load_preset(data: "dict | str") -> tuple[CaptionPreset, bool]:
    """Load a preset from a name or a serialized dict.

    * A string delegates to :func:`resolve_preset`.
    * A dict is parsed via :meth:`CaptionPreset.from_dict`; if it carries a
      known ``name`` matching a built-in that is returned (``substituted`` is
      True only when the serialized values differ from the built-in). A
      well-formed custom dict returns ``(preset, False)``.
    * Anything malformed falls back to ``(karaoke, True)`` and never raises
      (Req 6.4).
    """
    if isinstance(data, str):
        return resolve_preset(data)
    if isinstance(data, dict):
        try:
            preset = CaptionPreset.from_dict(data)
        except Exception:
            return BUILTIN_PRESETS[FALLBACK_PRESET_NAME], True
        # A well-formed serialized preset must carry a non-empty name plus a
        # valid animation and font; anything else is treated as malformed.
        if (
            not preset.name
            or preset.animation not in VALID_ANIMATIONS
            or not preset.font
        ):
            return BUILTIN_PRESETS[FALLBACK_PRESET_NAME], True
        return preset, False
    return BUILTIN_PRESETS[FALLBACK_PRESET_NAME], True


# ---------------------------------------------------------------------------
# Keyword-highlight planner (pure, DI)
# ---------------------------------------------------------------------------
DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    {
        # Articles / determiners
        "a", "an", "the", "this", "that", "these", "those", "some", "any",
        "each", "every", "all", "no", "both", "few", "many", "much", "most",
        # Pronouns
        "i", "me", "my", "mine", "myself", "we", "us", "our", "ours",
        "you", "your", "yours", "he", "him", "his", "she", "her", "hers",
        "it", "its", "they", "them", "their", "theirs", "who", "whom",
        "whose", "which", "what",
        # Prepositions / conjunctions
        "of", "in", "on", "at", "by", "for", "with", "about", "against",
        "between", "into", "through", "during", "before", "after", "above",
        "below", "to", "from", "up", "down", "out", "off", "over", "under",
        "and", "but", "or", "nor", "so", "yet", "if", "then", "than",
        "because", "as", "while", "where", "when",
        # Auxiliary / common verbs
        "is", "am", "are", "was", "were", "be", "been", "being", "have",
        "has", "had", "do", "does", "did", "will", "would", "shall",
        "should", "can", "could", "may", "might", "must", "get", "got",
        # Common fillers
        "just", "like", "really", "very", "too", "also", "even", "well",
        "okay", "ok", "yeah", "yes", "no", "not", "now", "here", "there",
        "um", "uh", "oh", "hey", "gonna", "wanna", "kinda", "sorta",
    }
)

# C11: the share of a cue's words that may be emphasised at once.
#
# Emphasis used to include an *absolute* rule — "Whisper probability >= 0.9" — which on
# clean audio nearly every word clears, so highlighting fired on almost everything, which
# is visually the same as highlighting nothing. It was worse than that in practice:
# ``_word_probability`` returns 1.0 when a word carries no probability at all, so any
# transcript without per-word confidence highlighted *every* non-stopword.
#
# Emphasis is inherently relative — it means "these words, not those" — so the rule is now
# a ranking with a budget rather than a threshold. A quarter of the words is enough for one
# or two emphases in a typical 3-5 word cue.
_HIGHLIGHT_RATIO = 0.25

# Confidence is no longer a reason to emphasise a word, but it is still a sensible
# tie-break between two equally salient ones: prefer the word we heard more clearly.
_HIGH_PROBABILITY = 0.9
# Minimum token length (normalized) for length-based emphasis.
_MIN_KEYWORD_LEN = 6
# Numeric / currency detection (e.g. "$5", "42", "3.14", "100%").
_NUMERIC_RE = re.compile(r"\$?\d")


def _normalize_token(text: str) -> str:
    """Strip surrounding punctuation for stopword/length checks, lowercased."""
    if not text:
        return ""
    # Keep internal characters; strip leading/trailing non-alphanumerics.
    stripped = re.sub(r"^[^0-9A-Za-z]+|[^0-9A-Za-z]+$", "", text)
    return stripped.lower()


def _word_text(word: Any) -> str:
    """Best-effort extraction of a word's text from a Word-like object/str."""
    if isinstance(word, str):
        return word
    return str(getattr(word, "text", "") or "")


def _word_probability(word: Any) -> float:
    """Best-effort extraction of a word's Whisper probability (default 1.0)."""
    prob = getattr(word, "probability", None)
    if prob is None:
        return 1.0
    try:
        return float(prob)
    except (TypeError, ValueError):
        return 1.0


def _keyword_salience(word: Any) -> int:
    """How emphasis-worthy ``word`` is, ``0`` meaning "not a candidate" (pure).

    A rank, not a verdict (C11). The signals are the same ones the old rule set used,
    ordered by how strongly each marks a word as the point of a sentence:

    * ``3`` — a number or currency amount ("$5", "42", "100%"). Concrete and quotable.
    * ``3`` — an ALL-CAPS run, which the speaker or transcriber already marked as emphatic.
    * ``2`` — a long content word (>= 6 characters), the usual carrier of meaning.
    * ``1`` — a mid-length content word (>= 4), a candidate only if nothing better exists.

    Stopwords and empty tokens score ``0`` and can never be emphasised.
    """
    raw = _word_text(word)
    token = _normalize_token(raw)
    if not token or token in DEFAULT_STOPWORDS:
        return 0
    # Numeric / currency (checked on the raw text so "$5" counts).
    if _NUMERIC_RE.search(raw):
        return 3
    # ALL-CAPS acronym / emphasis (use the raw alphabetic core).
    alpha_core = re.sub(r"[^A-Za-z]", "", raw)
    if len(alpha_core) >= 2 and alpha_core.isupper():
        return 3
    if len(token) >= _MIN_KEYWORD_LEN:
        return 2
    if len(token) >= 4:
        return 1
    return 0


def _is_deterministic_keyword(word: Any) -> bool:
    """Whether ``word`` is *eligible* for emphasis at all (pure).

    Eligibility is not selection: :func:`_deterministic_indices` then ranks the eligible
    words and emphasises only the strongest few. Kept as a named predicate because it is
    the readable half of the rule, and because the old absolute-threshold version is what
    C11 replaced.
    """
    return _keyword_salience(word) > 0


def _highlight_budget(count: int) -> int:
    """How many of ``count`` words may be emphasised (C11).

    At least one whenever there is anything to emphasise, so a short cue with a single
    strong word still gets it, and a quarter of the words otherwise.
    """
    if count <= 0:
        return 0
    return max(1, int(count * _HIGHLIGHT_RATIO))


def _deterministic_indices(words: list) -> set[int]:
    """Deterministic highlighted-index set for ``words`` (pure).

    Ranks the eligible words by salience and keeps the top few, rather than returning
    everything above a threshold (C11). Ties break on Whisper confidence and then on
    position, so the result is a pure function of the input — the same words in, the same
    emphasis out, which the determinism properties depend on.
    """
    scored = [
        (index, score, _word_probability(word))
        for index, word in enumerate(words)
        for score in (_keyword_salience(word),)
        if score > 0
    ]
    if not scored:
        return set()
    scored.sort(key=lambda item: (-item[1], -item[2], item[0]))
    return {index for index, _score, _prob in scored[: _highlight_budget(len(words))]}


def _ai_indices(words: list, client: Any) -> set[int]:
    """Query the injected LLM client for important words and map to indices.

    Any failure returns an empty set so the caller degrades to the
    deterministic set only (Req 3.4). Never raises.
    """
    try:
        prompt = (
            "From the following spoken words, return a JSON array of the most "
            "important words to visually emphasize in short-form captions. "
            "Respond with a JSON array of strings only.\n\nWords: "
            + " ".join(_word_text(w) for w in words)
        )
        raw: Any
        if hasattr(client, "complete_json"):
            raw = client.complete_json(prompt)
        elif hasattr(client, "complete"):
            import json

            raw = json.loads(client.complete(prompt))
        else:
            return set()

        # Accept a bare list, or a dict wrapping a list under a common key.
        chosen_words: list
        if isinstance(raw, list):
            chosen_words = raw
        elif isinstance(raw, dict):
            chosen_words = []
            for value in raw.values():
                if isinstance(value, list):
                    chosen_words = value
                    break
        else:
            return set()

        chosen_norm = {
            _normalize_token(str(w)) for w in chosen_words if str(w).strip()
        }
        chosen_norm.discard("")
        if not chosen_norm:
            return set()
        return {
            i
            for i, w in enumerate(words)
            if _normalize_token(_word_text(w)) in chosen_norm
        }
    except Exception:
        return set()


def plan_keywords(
    words: list,
    *,
    use_ai: bool = False,
    client: Optional[Any] = None,
) -> set[int]:
    """Return the set of word indices to highlight.

    Deterministic rule set (Req 3.2): a word is highlighted when its normalized
    token is a non-stopword AND any of — length >= 6, ALL-CAPS (len >= 2),
    numeric/currency, or high Whisper probability (>= 0.9).

    When ``use_ai`` is True and ``client`` is not None, the LLM's chosen words
    are merged (union) with the deterministic set (Req 3.3). Any LLM failure —
    or a missing client — degrades to the deterministic set only (Req 3.4). When
    ``use_ai`` is False no LLM call is made at all (Req 3.6), so a
    highlight-disabled caller incurs zero LLM cost.

    Pure and deterministic for a given input (and deterministic mock client);
    never raises.
    """
    words = list(words)
    deterministic = _deterministic_indices(words)
    if not use_ai or client is None:
        return deterministic
    return deterministic | _ai_indices(words, client)
