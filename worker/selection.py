"""AI-driven "best moment" selection.

Sends the transcript to the pluggable LLM client and asks it to identify the
most engaging, self-contained moments — hooks, punchlines, complete thoughts,
and emotional peaks — returning candidate clip ranges with a virality score and
a short rationale.

Selection honours the user's *Clip Topic / Keywords* and *Vibe / Tone*
settings, respects the requested clip count and target length window, and snaps
each candidate's start/end to natural sentence boundaries. If the LLM is not
configured (or fails), it transparently falls back to the Phase 1 deterministic
segmentation so the pipeline always produces clips.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from worker import segmentation as seg
from worker import selection_features
from worker.llm_client import BaseLLMClient, LLMError, get_llm_client, llm_available
from worker.models import ProcessingOptions
from worker.transcribe import Transcript, TranscriptSegment


@dataclass
class ClipCandidate:
    """A proposed clip time range with a virality score and rationale."""

    start: float
    end: float
    score: float = 0.0            # virality score, 0..100
    reason: str = ""
    title: str = ""
    text: str = ""                # transcript text within the range
    # S4: measured signals about this window - currently speech rate, computed from the word
    # timings that already exist. Deliberately *not* an input to ``score``: the features are
    # here to be measured against the S1 benchmark and fed to the model later (S10), and
    # choosing a weight before the benchmark can judge it would be tuning blind.
    features: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start


_SYSTEM = (
    "You are an expert short-form video editor who finds the most viral moments "
    "in long videos for TikTok, Reels, and YouTube Shorts. You pick moments that "
    "hook viewers instantly, contain a complete thought, and end on a strong "
    "punchline or emotional peak."
)


def _format_transcript(segments: list[TranscriptSegment]) -> str:
    """Render segments as ``[index] start-end: text`` lines for the prompt."""
    lines = []
    for i, s in enumerate(segments):
        lines.append(f"[{i}] {s.start:.1f}-{s.end:.1f}: {s.text.strip()}")
    return "\n".join(lines)


def _build_prompt(
    segments: list[TranscriptSegment],
    options: ProcessingOptions,
    min_len: float,
    max_len: float,
    max_clips: Optional[int],
) -> str:
    """Construct the selection prompt from the transcript + user options."""
    count_instr = (
        f"Select the {max_clips} best moments."
        if max_clips
        else "Select as many strong moments as you find (up to 10)."
    )
    topic_instr = (
        f"Strongly prefer moments about: {options.topic}."
        if options.topic.strip()
        else ""
    )
    vibe_instr = (
        f"Favor a {options.vibe} tone/vibe." if options.vibe.strip() else ""
    )

    return f"""Below is a timestamped transcript of a video. Each line is a
segment: [index] start-end: text

{_format_transcript(segments)}

Task: {count_instr}
Each selected moment should be a self-contained clip between {min_len:.0f} and
{max_len:.0f} seconds long. Prefer moments with a strong hook, a complete
thought, and a satisfying or surprising ending. {topic_instr} {vibe_instr}

Return a JSON array. Each element must have:
  - "start": number (seconds, from the transcript)
  - "end": number (seconds, from the transcript)
  - "score": number 0-100 estimating viral potential
  - "reason": short string explaining why it works
  - "title": a punchy proposed title (max 60 chars)

Order the array by "score" descending. Respond with JSON only.""".strip()


def snap_to_sentences(
    start: float,
    end: float,
    segments: list[TranscriptSegment],
) -> tuple[float, float]:
    """Snap ``[start, end]`` to the nearest segment (sentence) boundaries.

    Start snaps to the closest segment start; end to the closest segment end.
    Guarantees ``end > start``; if snapping collapses the range, the original
    values are returned.
    """
    if not segments:
        return start, end

    starts = [s.start for s in segments]
    ends = [s.end for s in segments]

    snapped_start = min(starts, key=lambda v: abs(v - start))
    snapped_end = min(ends, key=lambda v: abs(v - end))

    if snapped_end <= snapped_start:
        return start, end
    return snapped_start, snapped_end


def _text_between(segments: list[TranscriptSegment], start: float, end: float) -> str:
    """Return the transcript text whose segments fall within ``[start, end]``."""
    parts = []
    for s in segments:
        mid = (s.start + s.end) / 2
        if start <= mid <= end:
            parts.append(s.text.strip())
    return " ".join(parts).strip()


def _fallback(
    path,
    total_duration: float,
    options: ProcessingOptions,
    max_clips: Optional[int],
) -> list[ClipCandidate]:
    """Deterministic fallback using Phase 1 segmentation (neutral score)."""
    strategy = "silence" if options.strategy == "ai" else options.strategy
    segments = seg.segment_video(
        path,
        total_duration,
        clip_length=options.clip_length,
        strategy=strategy,
        max_clips=max_clips,
    )
    return [ClipCandidate(start=s.start, end=s.end, score=0.0,
                          reason="Selected by fallback segmentation")
            for s in segments]


def select_moments(
    transcript: Transcript,
    options: ProcessingOptions,
    source_path,
    total_duration: float,
    client: Optional[BaseLLMClient] = None,
) -> list[ClipCandidate]:
    """Return scored clip candidates for a video.

    Args:
        transcript: Full-video transcript (source-relative timing).
        options: Processing options (topic/vibe/clip_length/num_clips/...).
        source_path: Source video path (used by the fallback segmenter).
        total_duration: Probed duration in seconds.
        client: Optional LLM client (dependency injection for tests). When
            ``None``, the configured client is used.

    Returns:
        Ordered list of :class:`ClipCandidate` (highest score first).
    """
    min_len, max_len, _ = seg.resolve_length_range(options.clip_length)
    max_clips = seg.resolve_max_clips(options.num_clips)

    def _measured(found: list[ClipCandidate]) -> list[ClipCandidate]:
        """Attach S4 features to a result on its way out.

        Applied to the fallback returns as well as the LLM path: the fallback is what runs
        whenever there is no key or the call fails, so leaving it unmeasured would mean the
        benchmark could not compare the two paths on the same terms.
        """
        selection_features.annotate_candidates(found, transcript.words, total_duration)
        return found

    use_llm = options.strategy == "ai" and (client is not None or llm_available())
    if not use_llm or not transcript.segments:
        return _measured(_fallback(source_path, total_duration, options, max_clips))

    client = client or get_llm_client()
    prompt = _build_prompt(transcript.segments, options, min_len, max_len, max_clips)

    try:
        data = client.complete_json(prompt, system=_SYSTEM, max_tokens=1500)
    except LLMError:
        return _measured(_fallback(source_path, total_duration, options, max_clips))

    if not isinstance(data, list):
        return _measured(_fallback(source_path, total_duration, options, max_clips))

    candidates: list[ClipCandidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        # Reject hallucinated ranges that start beyond the video (a 1s
        # tolerance covers rounding); clamp an overshooting end below.
        if start >= total_duration + 1.0:
            continue
        end = min(end, total_duration)
        if end <= start:
            continue

        # Snap to sentence boundaries and clamp to the video.
        start, end = snap_to_sentences(start, end, transcript.segments)
        start = max(0.0, start)
        end = min(total_duration, end)
        if end - start < 1.0:
            continue

        score = item.get("score", 0)
        try:
            score = max(0.0, min(100.0, float(score)))
        except (TypeError, ValueError):
            score = 0.0

        candidates.append(
            ClipCandidate(
                start=round(start, 2),
                end=round(end, 2),
                score=round(score, 1),
                reason=str(item.get("reason", "")).strip(),
                title=str(item.get("title", "")).strip()[:80],
                text=_text_between(transcript.segments, start, end),
            )
        )

    if not candidates:
        return _measured(_fallback(source_path, total_duration, options, max_clips))

    # Enforce clip-count cap (candidates are LLM-ordered, but re-sort to be safe).
    candidates.sort(key=lambda c: c.score, reverse=True)
    if max_clips is not None:
        candidates = candidates[:max_clips]
    # S4: measured after ranking and capping, so it cannot influence either.
    return _measured(candidates)
