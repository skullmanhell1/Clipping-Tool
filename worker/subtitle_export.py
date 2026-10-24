"""Sidecar SRT and WebVTT export (O11).

Captions were burn-in only. That is the right default for short-form - every platform renders
them identically and nothing can switch them off - but it is not the only thing captions are
for. A sidecar file is what lets a platform show *selectable* captions, lets a viewer using a
screen reader or a translation feature reach the text at all, and lets the creator hand a
transcript to an editor without re-transcribing.

Written alongside the clip rather than instead of the burn-in, so nothing about the rendered
video changes.

**Two formats, because they are not interchangeable.** SRT is what almost every upload form and
desktop player accepts; WebVTT is what a browser ``<track>`` element requires and what several
platforms' APIs want. They differ in three ways that each silently break a player rather than
raising: the timestamp separator (``,`` versus ``.``), the mandatory ``WEBVTT`` header, and how
each escapes text - VTT is parsed as markup, so ``&``, ``<`` and ``>`` must be escaped or a
line containing "5 < 10" truncates at the ``<``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


def _clamp(value: float, low: float = 0.0, high: float | None = None) -> float:
    value = max(low, float(value))
    if high is not None:
        value = min(high, value)
    return value


def format_timestamp(seconds: float, *, vtt: bool = False) -> str:
    """``HH:MM:SS,mmm`` for SRT or ``HH:MM:SS.mmm`` for VTT.

    The separator is the difference, and it is not cosmetic: an SRT file using a full stop is
    rejected by some parsers and silently mis-timed by others, and VTT requires the full stop.
    Both formats keep the hours field even when it is zero, because a two-field ``MM:SS.mmm``
    is legal VTT but not legal SRT, so emitting one shape for both would produce an SRT that
    most players accept and a few quietly drop.
    """
    total = _clamp(seconds)
    milliseconds = int(round(total * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    separator = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def _escape_vtt(text: str) -> str:
    """Escape the three characters WebVTT parses as markup.

    Order matters: ``&`` first, or the ampersands introduced by the later replacements are
    escaped a second time and the viewer sees ``&amp;lt;``.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def speaker_for_interval(
    start: float,
    end: float,
    speaker_turns: Sequence[Any],
    *,
    previous_label: str | None = None,
) -> str | None:
    """Return the speaker with the greatest overlap over ``[start, end)``.

    Subtitle words and diarised turns do not always share exact boundaries:
    Whisper can straddle a diariser hand-off, and a backend can report two
    overlapping turns during crosstalk.  Choosing by overlap rather than by
    the word's start keeps a word from being assigned to the speaker who has
    already stopped talking.  An exact tie stays with ``previous_label`` when
    possible, which prevents a one-word boundary jitter from breaking label
    continuity.

    ``speaker_turns`` is intentionally duck-typed so the exporter can consume
    the existing :class:`worker.diarization.Speaker_Turn` objects without an
    import cycle.  Malformed turn records are ignored.
    """
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError):
        return None
    if end <= start or start != start or end != end:
        return None

    scores: dict[str, float] = {}
    order: dict[str, int] = {}
    for index, turn in enumerate(speaker_turns or []):
        try:
            turn_start = float(turn.start)
            turn_end = float(turn.end)
        except (AttributeError, TypeError, ValueError):
            continue
        if turn_end <= turn_start or turn_start != turn_start or turn_end != turn_end:
            continue
        overlap = max(0.0, min(end, turn_end) - max(start, turn_start))
        if overlap <= 0.0:
            continue
        label = str(getattr(turn, "speaker_label", "") or "").strip()
        if not label:
            continue
        scores[label] = scores.get(label, 0.0) + overlap
        order.setdefault(label, index)

    if not scores:
        return None
    best = max(scores.values())
    candidates = [label for label, score in scores.items() if abs(score - best) <= 1e-9]
    if previous_label in candidates:
        return previous_label
    return min(candidates, key=lambda label: order[label])


def cues_from_words(
    words: Sequence[Any],
    *,
    max_words: int = 8,
    max_gap: float = 0.8,
    max_duration: float = 5.0,
    speaker_turns: Sequence[Any] | None = None,
    include_speaker_labels: bool | None = None,
) -> list[tuple[float, float, str]]:
    """Group ``words`` into ``(start, end, text)`` cues for a sidecar file.

    Deliberately **not** :func:`worker.captions.words_to_cues`, whose limit is three words -
    tuned for full-width burned captions read in a glance. A sidecar file is read as subtitles,
    in a player's own small type at the bottom of the frame, and three-word cues there flicker
    once a second and are genuinely harder to follow. Two different jobs, two groupings.

    Cues break on a pause, on word count, and on elapsed time, so one long unbroken sentence
    cannot become a ten-second cue that outstays the speech it describes.  When ``speaker_turns``
    is supplied, a cue also ends at a speaker change.  This is important for a subtitle editor:
    otherwise a cue can contain the tail of one speaker's sentence and the start of another's, and
    any later speaker-label assignment will attach the whole cue to whichever speaker wins a single
    midpoint test.  ``include_speaker_labels`` optionally prefixes each cue as ``"S1: text"``;
    when omitted, labels are included only for a genuine multi-speaker turn list.  A single-speaker
    list therefore keeps the existing plain-subtitle output.
    """
    cues: list[tuple[float, float, str]] = []
    current: list[Any] = []
    current_speaker: str | None = None
    known_labels = {
        str(getattr(turn, "speaker_label", "") or "").strip()
        for turn in (speaker_turns or [])
        if str(getattr(turn, "speaker_label", "") or "").strip()
    }
    render_speaker_labels = (
        len(known_labels) > 1 if include_speaker_labels is None else bool(include_speaker_labels)
    )

    def flush() -> None:
        nonlocal current_speaker
        if not current:
            return
        texts = [str(getattr(w, "text", "") or "").strip() for w in current]
        text = " ".join(t for t in texts if t)
        if not text:
            current.clear()
            current_speaker = None
            return
        start = float(getattr(current[0], "start", 0.0))
        end = float(getattr(current[-1], "end", start))
        if end <= start:
            end = start + 0.1
        if render_speaker_labels and current_speaker:
            text = f"{current_speaker}: {text}"
        cues.append((start, end, text))
        current.clear()
        current_speaker = None

    previous_end: float | None = None
    for word in words:
        try:
            start = float(word.start)
            end = float(word.end)
        except (AttributeError, TypeError, ValueError):
            continue
        if start != start or end != end:  # NaN
            continue
        speaker = None
        if speaker_turns is not None:
            speaker = speaker_for_interval(
                start,
                end,
                speaker_turns,
                previous_label=current_speaker,
            )
            if current and speaker != current_speaker:
                flush()
        if current:
            gap = start - (previous_end if previous_end is not None else start)
            span = end - float(getattr(current[0], "start", start))
            if len(current) >= max_words or gap > max_gap or span > max_duration:
                flush()
        current.append(word)
        if speaker_turns is not None:
            current_speaker = speaker
        previous_end = end
    flush()
    return cues


def render_srt(cues: Iterable[tuple[float, float, str]]) -> str:
    """Render cues as SubRip. Returns ``""`` when there is nothing to write."""
    blocks = []
    for index, (start, end, text) in enumerate(cues, start=1):
        blocks.append(f"{index}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{text}\n")
    return "\n".join(blocks)


def render_vtt(cues: Iterable[tuple[float, float, str]]) -> str:
    """Render cues as WebVTT.

    The ``WEBVTT`` header and the blank line after it are both mandatory; without either, a
    browser rejects the file outright and shows no captions with no error in the page.
    """
    blocks = ["WEBVTT\n"]
    for start, end, text in cues:
        blocks.append(
            f"{format_timestamp(start, vtt=True)} --> {format_timestamp(end, vtt=True)}\n"
            f"{_escape_vtt(text)}\n"
        )
    return "\n".join(blocks)


# ISO 639-1 (what Whisper reports) to ISO 639-2/B (what an MP4 subtitle track's `language`
# metadata is expected to carry). Deliberately partial: it covers the languages Whisper
# detects with any reliability, and everything else resolves to ``und`` - "undetermined",
# a real code - rather than to the two-letter form. Passing "de" through would produce a
# track whose language field is not a valid ISO 639-2 code, which players handle by either
# ignoring it or displaying the raw string in the track menu; `und` at least means what it says.
ISO_639_2: dict[str, str] = {
    "ar": "ara",
    "bn": "ben",
    "cs": "ces",
    "da": "dan",
    "de": "deu",
    "el": "ell",
    "en": "eng",
    "es": "spa",
    "fa": "fas",
    "fi": "fin",
    "fr": "fra",
    "he": "heb",
    "hi": "hin",
    "hu": "hun",
    "id": "ind",
    "it": "ita",
    "ja": "jpn",
    "ko": "kor",
    "ms": "msa",
    "nl": "nld",
    "no": "nor",
    "pl": "pol",
    "pt": "por",
    "ro": "ron",
    "ru": "rus",
    "sv": "swe",
    "th": "tha",
    "tr": "tur",
    "uk": "ukr",
    "ur": "urd",
    "vi": "vie",
    "zh": "zho",
}


def iso639_2(language: str | None) -> str:
    """Map a transcript's language code to the three-letter form, or ``"und"``.

    A three-letter code is passed through unchanged when it is one this table knows, so a
    caller that already holds ``"eng"`` need not care which form it has.
    """
    code = str(language or "").strip().lower()
    if not code:
        return "und"
    if code in ISO_639_2:
        return ISO_639_2[code]
    if code in set(ISO_639_2.values()):
        return code
    return "und"


def write_sidecars(
    words: Sequence[Any],
    dest_stem: str | Path,
    *,
    formats: Iterable[str] = ("srt", "vtt"),
    language: str = "",
    speaker_turns: Sequence[Any] | None = None,
    include_speaker_labels: bool | None = None,
) -> list[Path]:
    """Write sidecar caption files next to a clip and return the paths written.

    ``words`` are expected to be clip-relative already - the pipeline rebases them for the
    burned captions, and a sidecar timed against the *source* would be silently offset by the
    clip's start, which looks like a sync bug in the player rather than an export bug here.

    Returns ``[]`` rather than raising when there are no words: a clip over music has nothing
    to export, and that is not a failure.

    ``language`` tags the filename - ``clip_0.en.srt`` rather than ``clip_0.srt`` - which is what
    every player and upload form uses to tell two sidecars for the same video apart (T10). Left
    empty for the single-track case so existing filenames are unchanged.  ``speaker_turns`` keeps
    multi-speaker cues from crossing a diarised hand-off.  Labels are rendered automatically for a
    genuine multi-speaker list unless ``include_speaker_labels=False`` is supplied, while a
    single-speaker list remains plain.
    """
    cues = cues_from_words(
        words,
        speaker_turns=speaker_turns,
        include_speaker_labels=include_speaker_labels,
    )
    if not cues:
        return []

    stem = Path(dest_stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    renderers = {"srt": render_srt, "vtt": render_vtt}
    tag = f".{language.strip()}" if str(language).strip() else ""
    for name in formats:
        key = str(name).lower().lstrip(".")
        renderer = renderers.get(key)
        if renderer is None:
            continue
        # Built by concatenation, not ``with_suffix``: with a language tag the stem ends in
        # ``.en``, which ``with_suffix`` reads as an existing extension and *replaces* - so
        # ``clip_0.en`` would be written as ``clip_0.srt``, overwriting the original-language
        # sidecar with the translation.
        path = stem.with_name(f"{stem.name}{tag}.{key}")
        path.write_text(renderer(cues), encoding="utf-8")
        written.append(path)
    return written
