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

from config import settings
from worker import audio_features, candidate_ranking, hook_score, scene_detect, selection_features
from worker import segmentation as seg
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
    # Measured signals about this window: speech rate (S4), audio energy (S2) and a hook
    # score for the opening seconds (S6).
    #
    # On the **LLM path** these are still deliberately not an input to ``score`` - the model's
    # own judgement is the ranking, and overriding it with an unvalidated weight would make an
    # improvement and a regression indistinguishable. They are shown to the model instead
    # (S10) and measured against the S1 benchmark.
    #
    # On the **fallback path** they *are* the ranking (S11), because what they replace there
    # was "keep the longest segments" - a rule that needs no benchmark to beat.
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


def _segment_annotation(
    segment: TranscriptSegment,
    words,
    envelope,
    *,
    pace_baseline: Optional[float],
    energy_baseline: Optional[float],
) -> str:
    """A short human-readable tag describing how a segment was *delivered* (S10).

    Words, not numbers. The model is being asked to pick moments, not to do arithmetic, and
    "fast, loud" is a description it can reason about where ``pace=1.42 rms=-18.3dB`` invites
    it to invent a formula out of figures whose scale it has no way to calibrate. Only
    departures from the speaker's own norm are mentioned, so an ordinary segment gets no tag
    at all - which keeps the prompt readable and makes the annotated segments stand out, since
    standing out is the entire signal.
    """
    tags: list[str] = []

    rate = selection_features.speech_rate(
        words, segment.start, segment.end, baseline=pace_baseline
    )
    if rate.reliable and pace_baseline:
        if rate.relative_speech_rate >= 1.25:
            tags.append("fast")
        elif rate.relative_speech_rate <= 0.75:
            tags.append("slow")

    if envelope and energy_baseline is not None:
        energy = audio_features.energy_in_window(
            envelope, segment.start, segment.end, baseline=energy_baseline
        )
        if energy.reliable:
            if energy.relative_energy >= 4.0:
                tags.append("loud")
            elif energy.relative_energy <= -6.0:
                tags.append("quiet")
            if energy.quiet_fraction >= 0.5:
                tags.append("mostly silent")

    return f" ({', '.join(tags)})" if tags else ""


def _format_transcript(
    segments: list[TranscriptSegment],
    *,
    words=(),
    envelope=(),
) -> str:
    """Render segments as ``[index] start-end{delivery}: text`` lines for the prompt.

    The delivery tag is S10: before it, the model saw only text and timings, so it could not
    tell that a line was shouted, rushed, or delivered into a silent room - the audio dynamics
    every commercial tool folds into moment detection. The measurements already existed for the
    benchmark's sake; this is what puts them in front of the thing making the decision.

    Falls back to the original bare format when there is nothing measured or the feature is
    switched off, so the prompt shape is unchanged on a source with no audio.
    """
    annotate = bool(words) and bool(getattr(settings, "selection_features_in_prompt", True))
    pace_baseline = None
    energy_baseline = None
    if annotate:
        total = segments[-1].end if segments else 0.0
        pace_baseline = selection_features.source_median_rate(words, total)
        energy_baseline = audio_features.source_median_energy(envelope) if envelope else None

    lines = []
    for i, s in enumerate(segments):
        tag = ""
        if annotate:
            tag = _segment_annotation(
                s, words, envelope,
                pace_baseline=pace_baseline, energy_baseline=energy_baseline,
            )
        lines.append(f"[{i}] {s.start:.1f}-{s.end:.1f}{tag}: {s.text.strip()}")
    return "\n".join(lines)


def _build_prompt(
    segments: list[TranscriptSegment],
    options: ProcessingOptions,
    min_len: float,
    max_len: float,
    max_clips: Optional[int],
    *,
    words=(),
    envelope=(),
) -> str:
    """Construct the selection prompt from the transcript + user options.

    ``words`` and ``envelope`` are keyword-only with empty defaults so every existing caller -
    including the tests that assert on the prompt text - keeps working and produces the
    unannotated form.
    """
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

    delivery_instr = ""
    if words and getattr(settings, "selection_features_in_prompt", True):
        delivery_instr = (
            "\nSome lines carry a parenthesised delivery note measured from the audio - "
            "fast/slow is pace against this speaker's own norm, loud/quiet is level against "
            "their own average. Treat them as evidence about energy and emphasis, not as a "
            "score: a loud line is not automatically a good clip, and an unmarked line is "
            "simply ordinary rather than bad.\n"
        )

    return f"""Below is a timestamped transcript of a video. Each line is a
segment: [index] start-end: text
{delivery_instr}
{_format_transcript(segments, words=words, envelope=envelope)}

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
    *,
    words=(),
    envelope=(),
    segments: Optional[list[TranscriptSegment]] = None,
) -> list[ClipCandidate]:
    """Deterministic fallback, now with real scoring rather than "keep the longest" (S11).

    The old path handed ``max_clips`` to :func:`worker.segmentation.segment_video` and let it
    cap by *duration*. That rule is worse than arbitrary - the longest silence-delimited
    segment in a video is typically the stretch where nobody paused, so a monologue with no
    beats outranked every punchy exchange - and it ran far more often than "fallback" suggests:
    on every run with no API key, every failed LLM call, and every ``fixed``/``silence``
    strategy chosen deliberately.

    So the cap is applied *here* instead: segmentation returns every segment, each is measured
    and scored (S11), near-duplicates are dropped (S15), and only then is the count capped.
    ``segment_video`` keeps its own longest-first capping for direct callers, which is what
    keeps the S1 harness's ``longest`` baseline an independent floor rather than a mirror of
    production.
    """
    strategy = "silence" if options.strategy == "ai" else options.strategy
    min_len, max_len, target = seg.resolve_length_range(options.clip_length)
    found = seg.segment_video(
        path,
        total_duration,
        clip_length=options.clip_length,
        strategy=strategy,
        # Deliberately uncapped: capping before scoring is what made length the ranking.
        max_clips=None,
    )
    candidates = [
        ClipCandidate(
            start=s.start,
            end=s.end,
            score=0.0,
            reason="Selected by fallback segmentation",
            text=_text_between(segments, s.start, s.end) if segments else "",
        )
        for s in found
    ]
    if not candidates:
        return []

    # Measure, then rank. The annotators never touch ``score``; rank_candidates is the only
    # thing that does.
    selection_features.annotate_candidates(candidates, words, total_duration)
    audio_features.annotate_candidates(candidates, envelope)
    hook_score.annotate_candidates(candidates, words, envelope=envelope)
    candidates = candidate_ranking.rank_candidates(
        candidates, target=target, min_len=min_len, max_len=max_len
    )
    # S15 before the cap, so a duplicate cannot displace a genuinely different moment.
    candidates = candidate_ranking.deduplicate(candidates, limit=max_clips)
    return candidates


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

    # S2: one astats pass over the whole source, before anything that needs it. Both the
    # prompt annotation (S10) and the fallback's ranking (S11) read this, so measuring it once
    # here rather than inside each is what keeps it to a single decode. Returns [] on a source
    # with no audio or any ffmpeg trouble, and every consumer treats that as "no information".
    envelope = audio_features.energy_envelope(
        source_path, window=float(getattr(settings, "energy_envelope_window_s", 1.0))
    )

    def _measured(found: list[ClipCandidate]) -> list[ClipCandidate]:
        """Attach the measured features to a result on its way out (S2, S4, S6).

        Applied to the fallback returns as well as the LLM path - the fallback is what runs
        whenever there is no key or the call fails, so leaving it unmeasured would mean the
        benchmark could not compare the two paths on the same terms. Idempotent, so the
        fallback having already measured its own candidates in order to rank them costs only a
        recomputation, not a wrong answer.
        """
        selection_features.annotate_candidates(found, transcript.words, total_duration)
        audio_features.annotate_candidates(found, envelope)
        hook_score.annotate_candidates(found, transcript.words, envelope=envelope)
        return found

    def _fallback_result() -> list[ClipCandidate]:
        return _measured(
            _fallback(
                source_path, total_duration, options, max_clips,
                words=transcript.words, envelope=envelope, segments=transcript.segments,
            )
        )

    use_llm = options.strategy == "ai" and (client is not None or llm_available())
    if not use_llm or not transcript.segments:
        return _fallback_result()

    client = client or get_llm_client()
    prompt = _build_prompt(
        transcript.segments, options, min_len, max_len, max_clips,
        words=transcript.words, envelope=envelope,
    )

    try:
        data = client.complete_json(prompt, system=_SYSTEM, max_tokens=1500)
    except LLMError:
        return _fallback_result()

    if not isinstance(data, list):
        return _fallback_result()

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
        return _fallback_result()

    # Enforce clip-count cap (candidates are LLM-ordered, but re-sort to be safe).
    candidates.sort(key=lambda c: c.score, reverse=True)
    # S15: de-duplicate *before* the cap. The model routinely proposes 12.0-45.0 and 14.5-47.0
    # for the same moment, and both used to ship - two files covering one moment, the second
    # having displaced whatever would have been the next distinct pick. Doing this after the
    # cap would remove the duplicate without promoting anything to replace it, so the user
    # would simply get fewer clips than they asked for.
    #
    # Text is compared as well as time, because the other duplicate is a speaker recapping the
    # same point twenty minutes later: no overlap at all, same clip to a viewer.
    candidates = candidate_ranking.deduplicate(candidates, limit=max_clips)
    # S9: after capping, so a decode is only spent on clips that will actually be rendered.
    scene_detect.snap_candidates(source_path, candidates)
    # S4: measured after ranking, capping and boundary snapping, so it cannot influence any
    # of them - and so the features describe the window the viewer actually gets rather than
    # the one the model proposed.
    return _measured(candidates)
