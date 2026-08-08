"""Speaker diarisation from the offline Whisper ``Word_Timeline``.

This module segments a source (or clip) into ordered, non-overlapping
:class:`Speaker_Turn`s. The **primary signal is the offline word timeline**
produced by :mod:`worker.transcribe` — no GPU, no ffmpeg, no OpenCV, and no
network access are used here. An optional, dependency-injected diarisation
backend can supply richer speaker assignments, but is never required; on its
absence or failure the diariser degrades to word-timeline-only segmentation and
records the degradation.

Everything in this module is a **pure function** operating on plain data, so the
whole subsystem is unit- and property-testable without audio models, ffmpeg,
OpenCV, or a network.

Public surface:
    - ``Speaker_Turn``               serialisable speaker attribution window
    - ``turns_to_dicts`` / ``turns_from_dicts``   round-trip helpers
    - ``segment_by_words``           pure offline segmentation
    - ``DiarizationBackend``         optional injectable backend protocol
    - ``diarize_source``             once-per-source diarisation entry point
    - ``slice_turns`` / ``rebase_turns``   clip-relative + filler-rebased turns
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from config import settings


@dataclass(frozen=True)
class Speaker_Turn:
    """A contiguous window attributed to one speaker (seconds).

    ``speaker_label`` is a stable id within the source, e.g. ``"S1"``.
    """

    speaker_label: str
    start: float
    end: float

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict."""
        return {
            "speaker_label": self.speaker_label,
            "start": self.start,
            "end": self.end,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Speaker_Turn:
        """Parse a single record. May raise on a malformed element; callers
        that must tolerate bad input should use :func:`turns_from_dicts`."""
        return cls(
            str(data["speaker_label"]),
            float(data["start"]),
            float(data["end"]),
        )


def turns_to_dicts(turns: list[Speaker_Turn]) -> list[dict]:
    """Serialise a list of :class:`Speaker_Turn`s to plain dicts."""
    return [t.to_dict() for t in turns]


def turns_from_dicts(data: list[dict]) -> list[Speaker_Turn]:
    """Round-trip parse a list of serialised turns.

    Malformed records (missing keys, wrong types) are skipped and valid ones
    retained. Never raises on a bad element (Req 3.3).
    """
    out: list[Speaker_Turn] = []
    if not data:
        return out
    for record in data:
        try:
            out.append(Speaker_Turn.from_dict(record))
        except (TypeError, ValueError, KeyError):
            continue
    return out


# --------------------------------------------------------------------------- #
# Shared normalisation                                                          #
# --------------------------------------------------------------------------- #
#
# The following helpers implement the ordering / bounding / merge / cap rules
# shared by both the pure ``segment_by_words`` path and the backend path in
# ``diarize_source`` (per the design's "normalise via the SAME rules").


def _clip_and_order(turns: list[Speaker_Turn], duration: float) -> list[Speaker_Turn]:
    """Bound each turn within ``[0, duration]``, ensure ``start <= end``, order
    by ascending start, and guarantee non-overlapping ``[start, end)`` by
    clipping any turn that starts before the previous one ends."""
    cleaned: list[Speaker_Turn] = []
    for t in turns:
        s = max(0.0, min(float(t.start), duration))
        e = max(0.0, min(float(t.end), duration))
        if e < s:
            e = s
        cleaned.append(Speaker_Turn(t.speaker_label, s, e))
    cleaned.sort(key=lambda t: (t.start, t.end))

    result: list[Speaker_Turn] = []
    for t in cleaned:
        if result and t.start < result[-1].end:
            new_start = result[-1].end
            if new_start >= t.end:
                # Fully swallowed by the previous turn -> drop it.
                continue
            t = Speaker_Turn(t.speaker_label, new_start, t.end)
        if t.end > t.start:
            result.append(t)
    return result


def _apply_cap(turns: list[Speaker_Turn], max_speakers: int) -> list[Speaker_Turn]:
    """Guarantee at most ``max_speakers`` distinct labels by merging the
    least-represented speakers (by total duration) into retained labels rather
    than exceeding the cap (Reqs 2.4, 2.5)."""
    labels_in_order: list[str] = []
    for t in turns:
        if t.speaker_label not in labels_in_order:
            labels_in_order.append(t.speaker_label)
    if len(labels_in_order) <= max_speakers:
        return turns

    durations: dict[str, float] = {}
    for t in turns:
        durations[t.speaker_label] = durations.get(t.speaker_label, 0.0) + (t.end - t.start)

    # Retain the most-represented labels; ties broken by first-appearance order.
    ranked = sorted(
        labels_in_order,
        key=lambda label: (-durations[label], labels_in_order.index(label)),
    )
    retained = set(ranked[:max_speakers])

    out: list[Speaker_Turn] = []
    for i, t in enumerate(turns):
        if t.speaker_label in retained:
            out.append(t)
            continue
        # Merge a least-represented turn into the nearest retained label:
        # prefer the previous retained turn, else the next retained turn.
        new_label: str | None = None
        for prev in reversed(out):
            if prev.speaker_label in retained:
                new_label = prev.speaker_label
                break
        if new_label is None:
            for nxt in turns[i + 1 :]:
                if nxt.speaker_label in retained:
                    new_label = nxt.speaker_label
                    break
        if new_label is None:
            new_label = ranked[0]
        out.append(Speaker_Turn(new_label, t.start, t.end))
    return out


def _merge_same_label(turns: list[Speaker_Turn], eps: float = 1e-6) -> list[Speaker_Turn]:
    """Merge adjacent same-label *contiguous* turns into one (Req 1.7).

    Turns that share a label but are separated by a gap are left untouched.
    """
    out: list[Speaker_Turn] = []
    for t in turns:
        if out and out[-1].speaker_label == t.speaker_label and t.start <= out[-1].end + eps:
            last = out[-1]
            out[-1] = Speaker_Turn(last.speaker_label, last.start, max(last.end, t.end))
        else:
            out.append(t)
    return out


def _relabel(turns: list[Speaker_Turn]) -> list[Speaker_Turn]:
    """Rename distinct labels to stable ``S1``, ``S2`` ... by first-appearance
    order and round timings to millisecond precision."""
    mapping: dict[str, str] = {}
    out: list[Speaker_Turn] = []
    for t in turns:
        if t.speaker_label not in mapping:
            mapping[t.speaker_label] = f"S{len(mapping) + 1}"
        out.append(Speaker_Turn(mapping[t.speaker_label], round(t.start, 3), round(t.end, 3)))
    return out


def _normalize_turns(
    turns: list[Speaker_Turn], duration: float, max_speakers: int
) -> list[Speaker_Turn]:
    """Apply the full ordering/bounding/merge/cap normalisation used by both
    the offline and backend diarisation paths."""
    if not turns:
        return []
    result = _clip_and_order(turns, duration)
    result = _apply_cap(result, max_speakers)
    result = _merge_same_label(result)
    return _relabel(result)


# --------------------------------------------------------------------------- #
# Pure offline segmentation                                                     #
# --------------------------------------------------------------------------- #


def segment_by_words(
    words: list,
    duration: float,
    *,
    max_speakers: int | None = None,
    pause_gap: float | None = None,
    handoff_gap: float | None = None,
) -> list[Speaker_Turn]:
    """PURE offline segmentation from the Word_Timeline only.

    No backend, no ffmpeg, no OpenCV, no network. Algorithm:
      1. Group words into speech runs split on silence gaps > ``pause_gap``.
      2. Assign speaker labels with a cheap deterministic turn-taking heuristic.
         A run only hands off to the next speaker when the silence *before* it
         exceeds ``handoff_gap``, which is much larger than ``pause_gap``.
      3. Normalise: bound within ``[0, duration]``, ensure ``start <= end``,
         order by ascending start, merge adjacent same-label contiguous turns,
         guarantee non-overlapping ``[start, end)`` and, when a naive pass would
         exceed the cap, merge the least-represented speakers.

    Edge cases: empty words -> ``[]`` (no raise); a single distinguishable
    speaker (one continuous speech run) -> one turn spanning the spoken range.

    Args:
        words: Word_Timeline objects exposing ``.start`` / ``.end``.
        duration: source/clip duration in seconds.
        max_speakers: cap on distinct labels; defaults to
            ``settings.diarization_max_speakers``.
        pause_gap: silence gap (s) that ends a turn; defaults to
            ``settings.diarization_pause_gap``.
        handoff_gap: silence gap (s) after which the next turn is attributed to a
            different speaker; defaults to ``settings.diarization_handoff_gap``.
            Clamped to at least ``pause_gap``, since a hand-off cannot be a weaker
            signal than a turn boundary.

    Note:
        Word timings carry no speaker identity, so this is attribution by proxy and
        cannot be correct in general. It is therefore biased toward *under*-reporting:
        when in doubt the words stay with the current speaker. A monologue containing
        any number of ordinary pauses yields exactly one speaker, because no gap reaches
        ``handoff_gap``. Previously every gap over ``pause_gap`` (0.9s by default —
        routine within one person's speech) advanced the round-robin, so a single-speaker
        source was reported as two speakers and speaker-aware reframe alternated between
        them. Use a real ``DiarizationBackend`` when accurate attribution matters.
    """
    if max_speakers is None:
        max_speakers = settings.diarization_max_speakers
    if pause_gap is None:
        pause_gap = settings.diarization_pause_gap
    if handoff_gap is None:
        handoff_gap = settings.diarization_handoff_gap
    max_speakers = max(1, int(max_speakers))
    pause_gap = float(pause_gap)
    # A hand-off must be at least as strong a signal as a turn break; a smaller value
    # would make every turn boundary a speaker change, i.e. the old behaviour.
    handoff_gap = max(float(handoff_gap), pause_gap)
    duration = float(duration)

    # Collect valid, ordered word spans.
    spans: list[tuple[float, float]] = []
    for w in words:
        s = float(getattr(w, "start", 0.0))
        e = float(getattr(w, "end", s))
        if e > s:
            spans.append((s, e))
    if not spans:
        return []
    spans.sort(key=lambda x: x[0])

    # Group into speech runs, splitting on silence gaps > pause_gap. Each run records the
    # silence that preceded it, because that gap — not the run's index — is what decides
    # whether the speaker changed.
    runs: list[tuple[float, float, float]] = []  # (start, end, preceding_gap)
    cur_start, cur_end = spans[0]
    preceding_gap = float("inf")  # nothing precedes the first run
    for s, e in spans[1:]:
        gap = s - cur_end
        if gap > pause_gap:
            runs.append((cur_start, cur_end, preceding_gap))
            cur_start, cur_end = s, e
            preceding_gap = gap
        else:
            cur_end = max(cur_end, e)
    runs.append((cur_start, cur_end, preceding_gap))

    # Deterministic turn-taking: advance the round-robin only on a gap long enough to
    # suggest an actual hand-off. Shorter gaps still end the turn (so the turn list keeps
    # its granularity) but keep the same label, and `_merge_same_label` then rejoins them.
    raw: list[Speaker_Turn] = []
    speaker_index = 0
    for position, (start, end, gap) in enumerate(runs):
        if position > 0 and gap > handoff_gap:
            speaker_index = (speaker_index + 1) % max_speakers
        raw.append(Speaker_Turn(f"S{speaker_index + 1}", start, end))
    return _normalize_turns(raw, duration, max_speakers)


# --------------------------------------------------------------------------- #
# Optional backend + once-per-source entry point                               #
# --------------------------------------------------------------------------- #


class DiarizationBackend(Protocol):
    """Optional external/model diariser. Injected for tests; never required."""

    def assign(self, words: list, duration: float) -> list[tuple[str, float, float]]:
        """Return raw ``(speaker_label, start, end)`` spans. May raise; the
        caller catches and degrades (Req 4.4)."""
        ...


def _word_boundaries(words: list) -> list[float]:
    """Return sorted, de-duplicated word start/end times (alignment targets)."""
    bounds: set[float] = set()
    for w in words:
        try:
            bounds.add(float(getattr(w, "start", 0.0)))
            bounds.add(float(getattr(w, "end", 0.0)))
        except (TypeError, ValueError):
            continue
    return sorted(bounds)


def _snap(t: float, boundaries: list[float]) -> float:
    """Snap ``t`` to the nearest Word_Timeline boundary (or return it as-is when
    there are no boundaries)."""
    if not boundaries:
        return t
    return min(boundaries, key=lambda b: abs(b - t))


def diarize_source(
    words: list,
    duration: float,
    *,
    backend: DiarizationBackend | None = None,
    max_speakers: int | None = None,
    permissibility: bool = False,
    notes: list[str] | None = None,
) -> list[Speaker_Turn]:
    """Produce :class:`Speaker_Turn`s for a whole source (called ONCE per
    source, Req 15.1).

      - ``permissibility=True`` OR ``backend is None`` -> pure
        :func:`segment_by_words` (offline), recording ``diarization:transcript``
        (Reqs 4.2, 19.1, 19.3).
      - backend present -> use ``backend.assign(...)``, align each span to
        Word_Timeline boundaries (Req 1.3), then normalise via the same
        ordering/bounding/merge/cap rules; record ``diarization:model``.
      - backend raises -> fall back to :func:`segment_by_words` and append
        ``diarization_degraded`` (Req 4.4).

    Never performs network access itself (Req 4.3).
    """
    if max_speakers is None:
        max_speakers = settings.diarization_max_speakers
    max_speakers = max(1, int(max_speakers))
    duration = float(duration)

    def _offline() -> list[Speaker_Turn]:
        return segment_by_words(words, duration, max_speakers=max_speakers)

    # Offline path: permissibility mode or no backend injected.
    if permissibility or backend is None:
        turns = _offline()
        if notes is not None:
            notes.append("diarization:transcript")
        return turns

    # Backend path.
    try:
        spans = backend.assign(words, duration)
    except Exception:
        turns = _offline()
        if notes is not None:
            notes.append("diarization_degraded")
        return turns

    boundaries = _word_boundaries(words)
    raw: list[Speaker_Turn] = []
    for span in spans or []:
        try:
            label, s, e = span
            s = float(s)
            e = float(e)
        except (TypeError, ValueError):
            continue
        raw.append(Speaker_Turn(str(label), _snap(s, boundaries), _snap(e, boundaries)))

    turns = _normalize_turns(raw, duration, max_speakers)
    if notes is not None:
        notes.append("diarization:model")
    return turns


# --------------------------------------------------------------------------- #
# Clip-relative slicing + filler rebasing                                       #
# --------------------------------------------------------------------------- #


def slice_turns(turns: list[Speaker_Turn], start: float, end: float) -> list[Speaker_Turn]:
    """Return source-relative ``turns`` clipped to ``[start, end]`` and rebased
    to clip-relative (0-based) coordinates, bounded within ``[0, end-start]``.

    Turns that do not overlap the window are dropped. Pure.
    """
    start = float(start)
    end = float(end)
    span = max(0.0, end - start)
    out: list[Speaker_Turn] = []
    for t in turns:
        ov_start = max(float(t.start), start)
        ov_end = min(float(t.end), end)
        if ov_end <= ov_start:
            continue
        ns = max(0.0, min(ov_start - start, span))
        ne = max(0.0, min(ov_end - start, span))
        if ne <= ns:
            continue
        out.append(Speaker_Turn(t.speaker_label, round(ns, 3), round(ne, 3)))
    out.sort(key=lambda t: (t.start, t.end))
    return out


def rebase_turns(turns: list[Speaker_Turn], keeps: list) -> list[Speaker_Turn]:
    """Remap clip-relative ``turns`` onto the tightened (post-filler) timeline
    defined by ``keeps``, mirroring :func:`worker.effects.filler.rebase_words`
    so turns stay aligned to the rebased words (Reqs 13.4, 13.5). Pure.

    ``keeps`` is a list of filler ``Interval``-like objects exposing ``.start``
    / ``.end``. Each turn is mapped exactly like a word in ``rebase_words``:
    its midpoint selects the containing keep segment, and its span is clipped to
    that segment and shifted onto the concatenated output timeline.
    """
    if not keeps:
        return list(turns)

    # Build (source_start, source_end, new_offset) for each keep segment — the
    # identical mapping table used by filler.rebase_words.
    mapped: list[tuple[float, float, float]] = []
    offset = 0.0
    for k in keeps:
        ks = float(getattr(k, "start", 0.0))
        ke = float(getattr(k, "end", ks))
        mapped.append((ks, ke, offset))
        offset += max(0.0, ke - ks)

    out: list[Speaker_Turn] = []
    for t in turns:
        ts = float(t.start)
        te = float(t.end)
        mid = (ts + te) / 2.0
        for ks, ke, new_off in mapped:
            if ks <= mid < ke:
                ns = new_off + (max(ts, ks) - ks)
                ne = new_off + (min(te, ke) - ks)
                out.append(
                    Speaker_Turn(
                        t.speaker_label,
                        round(ns, 3),
                        round(max(ns, ne), 3),
                    )
                )
                break
    return out
