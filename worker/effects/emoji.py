"""Auto-emoji overlays synced to spoken words.

Reuses the Whisper word-level timestamps to drop relevant emoji onto the clip
at the moment the matching word is spoken. Two selection modes:

* ``keyword`` — a built-in keyword -> emoji map (fast, offline, deterministic).
* ``ai`` — ask the LLM for context-aware ``word -> emoji`` pairs for the clip
  transcript, then time them to the spoken words (falls back to keyword mode).

Intensity (Off / Subtle / Standard / Heavy) controls how many emoji appear.
Twemoji PNGs are resolved from ``settings.emoji_assets_dir`` (fetched from the
CDN and cached on first use) and composited with ffmpeg ``overlay`` filters,
optionally with an alpha "pop" as each one appears.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from config import settings

# Intensity -> minimum seconds between emoji (spacing). "off" disables overlays.
INTENSITY_SPACING: dict[str, float] = {
    "off": 0.0,
    "subtle": 10.0,
    "standard": 5.0,
    "heavy": 2.5,
}

# A compact, high-signal keyword -> emoji map. Keys are matched case-insensitively
# against whole words (punctuation stripped).
KEYWORD_EMOJI: dict[str, str] = {
    "love": "❤️", "heart": "❤️", "amazing": "🤩", "wow": "😮", "money": "💰",
    "cash": "💵", "rich": "🤑", "dollar": "💵", "fire": "🔥", "hot": "🔥",
    "best": "🏆", "win": "🏆", "winner": "🏆", "success": "📈", "growth": "📈",
    "grow": "🌱", "idea": "💡", "smart": "🧠", "brain": "🧠", "think": "🤔",
    "crazy": "🤯", "insane": "🤯", "mind": "🧠", "blown": "🤯", "laugh": "😂",
    "funny": "😂", "haha": "😂", "happy": "😄", "sad": "😢", "cry": "😭",
    "angry": "😡", "boom": "💥", "explode": "💥", "rocket": "🚀", "launch": "🚀",
    "fast": "⚡", "speed": "⚡", "energy": "⚡", "power": "💪", "strong": "💪",
    "gym": "🏋️", "work": "💼", "business": "💼", "deal": "🤝", "team": "🤝",
    "time": "⏰", "clock": "⏰", "warning": "⚠️", "danger": "⚠️", "stop": "✋",
    "yes": "✅", "correct": "✅", "right": "✅", "no": "❌", "wrong": "❌",
    "star": "⭐", "gold": "🥇", "party": "🎉", "celebrate": "🎉", "gift": "🎁",
    "music": "🎵", "phone": "📱", "camera": "📸", "video": "🎬", "game": "🎮",
    "food": "🍔", "coffee": "☕", "eye": "👀", "look": "👀", "point": "👉",
    "world": "🌍", "global": "🌍", "sun": "☀️", "cool": "😎", "clap": "👏",
    "goal": "🎯", "target": "🎯", "focus": "🎯", "secret": "🤫", "shh": "🤫",
    "question": "❓", "why": "❓", "up": "⬆️", "down": "⬇️", "check": "✅",
}

_WORD_RE = re.compile(r"[a-z']+")


@dataclass
class EmojiCue:
    """A planned emoji overlay: char + [start, end] window (clip-relative)."""

    char: str
    start: float
    end: float
    slot: int = 0  # position slot, for horizontal spread


def _norm(token: str) -> str:
    """Lowercase a token and strip surrounding punctuation."""
    m = _WORD_RE.findall(token.lower())
    return m[0] if m else ""


# --- Keyword lookup: inflected forms (A10) ---------------------------------- #
#
# The map is keyed on base forms, and lookup was exact, so speech missed constantly:
# "win" hit while "winning", "wins" and "won" did not, and "fired" missed "fire". Emoji are
# planned from spoken words, which arrive inflected far more often than not.
#
# Deliberately a small rule set plus a table of irregulars rather than a real stemmer: no
# new dependency, no surprises, and every transformation is reversible by eye. A Porter
# stemmer would also fold "business" to "busi" and stop matching the map at all.

#: Irregular forms that no suffix rule can reach, mapped to a key in ``KEYWORD_EMOJI``.
_IRREGULAR: dict[str, str] = {
    "won": "win", "winning": "win", "wins": "win",
    "lost": "lose", "loses": "lose", "losing": "lose",
    "thought": "think", "thinking": "think", "thinks": "think",
    "grew": "grow", "grown": "grow", "growing": "grow",
    "blew": "blown", "blowing": "blown",
    "ate": "food", "eating": "food", "eats": "food", "eat": "food",
    "ran": "fast", "running": "fast", "runs": "fast",
    "best": "best", "better": "best",
    "laughed": "laugh", "laughing": "laugh", "laughs": "laugh",
    "cried": "cry", "crying": "cry", "cries": "cry",
    "exploded": "explode", "exploding": "explode", "explodes": "explode",
    "launched": "launch", "launching": "launch", "launches": "launch",
    "celebrated": "celebrate", "celebrating": "celebrate", "celebrates": "celebrate",
    "focused": "focus", "focusing": "focus", "focuses": "focus",
    "stopped": "stop", "stopping": "stop", "stops": "stop",
    "looked": "look", "looking": "look", "looks": "look",
    "worked": "work", "working": "work", "works": "work",
    "powerful": "power", "empowered": "power",
    "moneys": "money", "riches": "rich", "richest": "rich",
    "fastest": "fast", "faster": "fast", "strongest": "strong", "stronger": "strong",
    "smarter": "smart", "smartest": "smart", "happiest": "happy", "happier": "happy",
    "craziest": "crazy", "crazier": "crazy", "funniest": "funny", "funnier": "funny",
    "hottest": "hot", "hotter": "hot", "biggest": "big", "bigger": "big",
    "angrier": "angry", "angriest": "angry", "saddest": "sad", "sadder": "sad",
    "ideas": "idea", "goals": "goal", "targets": "target", "secrets": "secret",
    "questions": "question", "teams": "team", "deals": "deal", "gifts": "gift",
    "stars": "star", "parties": "party", "brains": "brain", "minds": "mind",
    "eyes": "eye", "points": "point", "clocks": "clock", "phones": "phone",
    "cameras": "camera", "videos": "video", "games": "game", "rockets": "rocket",
    "warnings": "warning", "dangers": "danger", "businesses": "business",
}


def _candidate_keys(token: str) -> list[str]:
    """Lookup keys for ``token``, most specific first (A10).

    The token itself always comes first, so an exact match can never be overridden by a
    stemmed one, and the map keeps exactly the meaning it had before.
    """
    if not token:
        return []
    keys = [token]

    irregular = _IRREGULAR.get(token)
    if irregular:
        keys.append(irregular)

    # "-ies" -> "-y" ("parties" -> "party"), before the bare "-s" rule.
    if token.endswith("ies") and len(token) > 4:
        keys.append(token[:-3] + "y")
    # Plural / third person: "wins" -> "win". Not "-ss" ("business", "success").
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        keys.append(token[:-1])
    # "-ed": "fired" -> "fire" (keep the e) and "worked" -> "work".
    if token.endswith("ed") and len(token) > 4:
        keys.append(token[:-1])
        keys.append(token[:-2])
    # "-ing": "firing" -> "fire" (restore the e) and "working" -> "work".
    if token.endswith("ing") and len(token) > 5:
        keys.append(token[:-3])
        keys.append(token[:-3] + "e")
        # Doubled consonant: "winning" -> "win", "stopping" -> "stop".
        stem = token[:-3]
        if len(stem) > 2 and stem[-1] == stem[-2]:
            keys.append(stem[:-1])
    # "-ly": "quickly" -> "quick".
    if token.endswith("ly") and len(token) > 4:
        keys.append(token[:-2])

    seen: set[str] = set()
    return [key for key in keys if not (key in seen or seen.add(key))]


def lookup_emoji(token: str, mapping: dict[str, str]) -> str:
    """The emoji for ``token`` under ``mapping``, or ``""`` (A10).

    Tries the token as spoken first, then its uninflected candidates.
    """
    for key in _candidate_keys(token):
        glyph = mapping.get(key)
        if glyph:
            return glyph
    return ""


def plan_emoji(
    words: list,
    duration: float,
    intensity: str = "standard",
    mode: str = "keyword",
    client=None,
    hold: float = 1.3,
) -> list[EmojiCue]:
    """Plan emoji overlays for a clip.

    Args:
        words: clip-relative words (objects with ``.start``/``.end``/``.text``).
        duration: clip duration (s), used to bound the emoji windows.
        intensity: ``off`` | ``subtle`` | ``standard`` | ``heavy``.
        mode: ``keyword`` or ``ai``.
        client: optional LLM client for ``ai`` mode (falls back to keyword map).
        hold: how long each emoji stays on screen (s).

    Returns a spacing-respecting, chronologically ordered list of cues.
    """
    spacing = INTENSITY_SPACING.get(intensity, 0.0)
    if spacing <= 0 or not words:
        return []

    mapping = KEYWORD_EMOJI
    if mode == "ai" and client is not None:
        ai_map = _ai_emoji_map(words, client)
        if ai_map:
            mapping = {**KEYWORD_EMOJI, **ai_map}

    # A11: rank the candidates by salience and keep the strongest, instead of taking whichever
    # matching word happens to arrive first after the stopwatch has elapsed.
    #
    # The old rule was purely temporal: `standard` allows one emoji per five seconds, so the
    # first mapped word after each interval won regardless of whether it mattered. On
    # "so anyway the money was completely gone", "so" is not mapped but "anyway"-class filler
    # often is, and it would take the slot that "money" wanted. Salience is the same signal
    # C11 uses to choose which word to *emphasise*, so the emoji now lands on the same word the
    # caption highlights rather than on an unrelated one a second earlier.
    candidates: list[tuple[float, float, str]] = []
    for w in words:
        key = _norm(getattr(w, "text", ""))
        glyph = lookup_emoji(key, mapping) if key else ""
        if not glyph:
            continue
        try:
            start = float(getattr(w, "start", 0.0))
        except (TypeError, ValueError):
            continue
        if start != start:      # NaN
            continue
        candidates.append((_emoji_salience(w, key), start, glyph))

    if not candidates:
        return []

    # Strongest first; ties break on time so the result stays a pure function of the input -
    # the kinetic determinism properties depend on that.
    candidates.sort(key=lambda c: (-c[0], c[1]))

    chosen: list[tuple[float, str]] = []
    used_glyphs: set[str] = set()
    cap = _emoji_cap(intensity, duration)
    for _salience, start, glyph in candidates:
        if len(chosen) >= cap:
            break
        # A12: the same glyph twice in one clip reads as a template rather than as a reaction,
        # and two identical emoji a few seconds apart is the single most obvious way an
        # automatic overlay looks automatic.
        if glyph in used_glyphs:
            continue
        # Spacing is still enforced, but now against every already-chosen cue rather than only
        # against the previous one in time order - the list is no longer in time order here.
        if any(abs(start - other) < spacing for other, _g in chosen):
            continue
        if min(duration, start + hold) <= start:
            continue
        chosen.append((start, glyph))
        used_glyphs.add(glyph)

    cues: list[EmojiCue] = []
    for slot, (start, glyph) in enumerate(sorted(chosen)):
        end = min(duration, start + hold)
        cues.append(EmojiCue(glyph, round(start, 3), round(end, 3), slot % 3))
    return cues


def _emoji_salience(word: Any, key: str) -> float:
    """How much this word deserves the emoji slot (A11).

    Deliberately reuses the caption keyword planner's own scorer rather than inventing a second
    notion of importance: two different answers to "which word matters here" would put the
    emoji on one word and the highlight on another, which looks like a bug to a viewer even
    though each component is behaving as written.

    Falls back to a length proxy if that import is unavailable, so this module keeps working
    standalone - it is imported by the overlay builder, which must not depend on caption code.
    """
    try:
        from worker.effects.caption_presets import _keyword_salience

        base = float(_keyword_salience(word))
    except Exception:
        base = 2.0 if len(key) >= 6 else 1.0
    # A longer key is a more specific match: "celebrate" carries more than "up".
    return base + min(0.9, len(key) / 20.0)


def _emoji_cap(intensity: str, duration: float) -> int:
    """The most emoji one clip may carry (A12).

    A cap is needed independently of spacing, because spacing alone scales with clip length: a
    three-minute clip at `heavy` could carry sixty emoji and still satisfy every gap. Scaled by
    duration so a 15-second clip and a 3-minute one are both proportionate, with a floor of one -
    an intensity the user switched on should produce at least one.
    """
    per_minute = {"subtle": 3, "standard": 6, "heavy": 12}.get(intensity, 6)
    minutes = max(0.25, float(duration or 0.0) / 60.0)
    return max(1, int(round(per_minute * minutes)))


def _ai_emoji_map(words: list, client) -> dict[str, str]:
    """Ask the LLM for context-aware ``word -> emoji`` pairs. Best-effort."""
    text = " ".join(getattr(w, "text", "") for w in words).strip()
    if not text:
        return {}
    prompt = (
        "For the following short-video transcript, choose up to 12 vivid single "
        "emoji to visually punctuate specific spoken words. Return a JSON object "
        'mapping the lowercase spoken word to one emoji, e.g. {"money":"💰"}. '
        "Only include words that actually appear in the transcript.\n\n"
        f"Transcript:\n{text}"
    )
    try:
        data = client.complete_json(prompt, temperature=0.4, max_tokens=400)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        key = _norm(str(k))
        val = str(v).strip()
        if key and val:
            out[key] = val
    return out


# --------------------------------------------------------------------------- #
# Twemoji asset resolution
# --------------------------------------------------------------------------- #
def emoji_filename(char: str) -> str:
    """Return the vendored PNG filename for an emoji string.

    Codepoints joined by ``-`` in lower-case hex, dropping the ``U+FE0F`` variation
    selector for multi-codepoint sequences — the Twemoji convention, kept as our on-disk
    naming even though the artwork now comes from Noto Emoji (A6), because it is the
    convention every emoji set agrees on modulo case and separator.
    ``scripts/fetch_emoji.py`` translates it to Noto's ``emoji_u<...>.png`` spelling.
    """
    codepoints = [ord(c) for c in char]
    if len(codepoints) > 1:
        codepoints = [cp for cp in codepoints if cp != 0xFE0F]
    return "-".join(f"{cp:x}" for cp in codepoints) + ".png"


#: Retained spelling for callers written against the Twemoji-only version.
twemoji_filename = emoji_filename


def _default_downloader(url: str, dest: Path) -> bool:
    """Download ``url`` to ``dest``. Returns ``True`` on success."""
    if not settings.emoji_allow_download:
        return False
    try:
        import httpx

        resp = httpx.get(url, timeout=15, follow_redirects=True)
        if resp.status_code == 200 and resp.content:
            dest.write_bytes(resp.content)
            return True
    except Exception:
        return False
    return False


def resolve_asset(
    char: str,
    downloader: Optional[Callable[[str, Path], bool]] = None,
) -> Optional[Path]:
    """Return a local Twemoji PNG path for ``char`` (cached), or ``None``.

    Looks in ``settings.emoji_assets_dir`` first; otherwise fetches from the
    configured Twemoji CDN and caches it. ``downloader`` is injectable for tests.
    """
    filename = emoji_filename(char)
    assets = Path(settings.emoji_assets_dir)
    assets.mkdir(parents=True, exist_ok=True)
    local = assets / filename
    if local.exists() and local.stat().st_size > 0:
        return local

    fetch = downloader or _default_downloader
    url = f"{settings.emoji_cdn_base.rstrip('/')}/{filename}"
    if fetch(url, local) and local.exists() and local.stat().st_size > 0:
        return local
    return None


# --------------------------------------------------------------------------- #
# ffmpeg overlay graph
# --------------------------------------------------------------------------- #
# Horizontal slots (fractions of frame width) for spreading emoji out.
_SLOT_X = (0.16, 0.74, 0.45)
_SLOT_Y = (0.15, 0.24, 0.15)


def _emoji_px(frame_width: int, size_frac: float) -> int:
    """Emoji width in pixels, at least 2 and always even.

    ``scale=<w>:-1`` derives the height from the aspect ratio and rounds it to an even
    number; giving it an even width keeps the two consistent, and libx264's 4:2:0 chroma
    subsampling requires even dimensions anyway.
    """
    px = int(max(2.0, float(frame_width) * float(size_frac)))
    return px - (px % 2)


def build_overlay(
    cues: list[EmojiCue],
    base_label: str,
    out_label: str,
    *,
    duration: float,
    frame_width: int = 1080,
    size_frac: float = 0.14,
    animate: bool = True,
    resolver: Optional[Callable[[str], Optional[Path]]] = None,
    input_offset: int = 1,
) -> tuple[list[str], str]:
    """Build ffmpeg inputs + a ``-filter_complex`` snippet for emoji overlays.

    Args:
        cues: planned emoji cues.
        base_label: label of the base video stream (without brackets), e.g. ``v0``.
        out_label: label to assign the final overlaid stream (without brackets).
        duration: clip duration (each looped PNG input is bounded to this).
        frame_width: width of the frame being composited onto, in pixels. ``size_frac`` is
            taken against this. It was hard-coded to 1080 (A8), so a 1:1 (1080) run was
            right by accident and a 16:9 (1920) or square-ish output got an emoji sized
            for a different frame — the overlay *placement* used the real ``W`` while the
            *scale* used a constant, so the two disagreed on what the frame was.
        size_frac: emoji width as a fraction of the frame width.
        animate: alpha "pop" fade-in as each emoji appears.
        resolver: ``char -> Path`` resolver (defaults to :func:`resolve_asset`).
        input_offset: ffmpeg input index of the first emoji PNG (after existing
            inputs such as the base video and any music).

    Returns ``(input_args, filtergraph)``. When no emoji resolve, returns
    ``([], "")`` and the caller should keep using ``base_label``.
    """
    resolve = resolver or (lambda c: resolve_asset(c))

    resolved: list[tuple[EmojiCue, Path]] = []
    for cue in cues:
        path = resolve(cue.char)
        if path is not None:
            resolved.append((cue, path))
    if not resolved:
        return [], ""

    input_args: list[str] = []
    steps: list[str] = []
    current = base_label
    for i, (cue, path) in enumerate(resolved):
        idx = input_offset + i
        # Loop the still PNG for the clip's duration so its PTS tracks main time.
        input_args += ["-loop", "1", "-t", f"{max(0.1, duration):.3f}", "-i", str(path)]

        # Scale relative to the real frame width, then let overlay place it (A8).
        prep = f"[{idx}:v]scale={_emoji_px(frame_width, size_frac)}:-1,format=rgba"
        if animate:
            prep += f",fade=t=in:st={cue.start:.3f}:d=0.18:alpha=1"
        prep += f"[e{i}]"
        steps.append(prep)

        sx, sy = _SLOT_X[cue.slot % 3], _SLOT_Y[cue.slot % 3]
        nxt = out_label if i == len(resolved) - 1 else f"ov{i}"
        steps.append(
            f"[{current}][e{i}]overlay=x='(W-w)*{sx:g}':y='H*{sy:g}':"
            f"enable='between(t,{cue.start:.3f},{cue.end:.3f})'[{nxt}]"
        )
        current = nxt

    return input_args, ";".join(steps)
