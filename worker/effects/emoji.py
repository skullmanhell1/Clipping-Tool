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
from typing import Callable, Optional

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

    cues: list[EmojiCue] = []
    last_t = -spacing
    slot = 0
    for w in words:
        key = _norm(getattr(w, "text", ""))
        if not key or key not in mapping:
            continue
        start = float(getattr(w, "start", 0.0))
        if start - last_t < spacing:
            continue
        end = min(duration, start + hold)
        if end <= start:
            continue
        cues.append(EmojiCue(mapping[key], round(start, 3), round(end, 3), slot % 3))
        last_t = start
        slot += 1
    return cues


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
def twemoji_filename(char: str) -> str:
    """Return the Twemoji PNG filename for an emoji string.

    Follows Twemoji's convention: codepoints joined by ``-`` in lowercase hex,
    dropping the ``U+FE0F`` variation selector for multi-codepoint sequences.
    """
    codepoints = [ord(c) for c in char]
    if len(codepoints) > 1:
        codepoints = [cp for cp in codepoints if cp != 0xFE0F]
    return "-".join(f"{cp:x}" for cp in codepoints) + ".png"


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
    filename = twemoji_filename(char)
    assets = Path(settings.emoji_assets_dir)
    assets.mkdir(parents=True, exist_ok=True)
    local = assets / filename
    if local.exists() and local.stat().st_size > 0:
        return local

    fetch = downloader or _default_downloader
    url = f"{settings.twemoji_cdn_base.rstrip('/')}/{filename}"
    if fetch(url, local) and local.exists() and local.stat().st_size > 0:
        return local
    return None


# --------------------------------------------------------------------------- #
# ffmpeg overlay graph
# --------------------------------------------------------------------------- #
# Horizontal slots (fractions of frame width) for spreading emoji out.
_SLOT_X = (0.16, 0.74, 0.45)
_SLOT_Y = (0.15, 0.24, 0.15)


def build_overlay(
    cues: list[EmojiCue],
    base_label: str,
    out_label: str,
    *,
    duration: float,
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

        # Scale relative to a 1080-wide reference, then let overlay place it.
        prep = f"[{idx}:v]scale={int(1080 * size_frac)}:-1,format=rgba"
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
