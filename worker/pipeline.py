"""End-to-end clip generation pipeline.

Given a source video (local file or already-downloaded URL) and processing
options, this module runs the full pipeline:

    ingest/probe -> transcribe -> **AI highlight selection** -> per clip:
    cut -> reformat -> (optional) burn captions -> thumbnail ->
    **AI metadata generation**

Phase 2 replaces Phase 1's fixed-length cutting with LLM-driven highlight
selection (:mod:`worker.selection`) and adds per-clip metadata generation
(:mod:`worker.metadata`). Both degrade gracefully: when no LLM is configured
(or a call fails) selection falls back to deterministic segmentation and
metadata falls back to a transcript-derived title, so the pipeline always
produces clips.

Progress is reported through a callback so the job manager can surface live
status to the UI.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Optional

from worker import captions as cap
from worker import ffmpeg_utils as fu
from worker import metadata as meta_mod
from worker import selection as sel
from worker.llm_client import BaseLLMClient
from worker.models import ClipResult, ProcessingOptions
from worker.transcribe import Transcript, transcribe

# progress_cb(fraction: float, stage: str)
ProgressCallback = Callable[[float, str], None]

# Progress budget across pipeline stages (fractions of the local span).
_P_TRANSCRIBE_END = 0.25
_P_SELECT_END = 0.35
_P_CLIPS_END = 1.0


def _noop(fraction: float, stage: str) -> None:  # pragma: no cover
    pass


def _filter_transcript_to_range(
    transcript: Transcript, start: Optional[float], end: Optional[float]
) -> Transcript:
    """Return a transcript containing only segments overlapping ``[start, end]``.

    Used to honour the Process Range setting so selection only considers the
    requested window. Timing is left in source-relative coordinates.
    """
    if start is None and end is None:
        return transcript
    lo = start if start is not None else float("-inf")
    hi = end if end is not None else float("inf")
    segs = [s for s in transcript.segments if s.end > lo and s.start < hi]
    return Transcript(language=transcript.language, segments=segs)


def run_pipeline(
    source: str | Path,
    options: ProcessingOptions,
    clips_dir: str | Path,
    temp_dir: str | Path,
    progress_cb: Optional[ProgressCallback] = None,
    start_progress: float = 0.0,
    llm_client: Optional[BaseLLMClient] = None,
) -> list[ClipResult]:
    """Run the full pipeline on ``source`` and return the produced clips.

    Args:
        source: Path to a local source video (already downloaded if from a URL).
        options: User processing options.
        clips_dir: Directory to write finished clips + thumbnails into.
        temp_dir: Scratch directory for intermediate artefacts.
        progress_cb: Optional ``fn(fraction, stage)`` progress callback.
        start_progress: Fraction already consumed before this call (0..1).
        llm_client: Optional LLM client (dependency injection for tests). When
            ``None`` the configured client is used (if any).

    Returns:
        A list of :class:`ClipResult` ordered by virality score (best first).
    """
    cb = progress_cb or _noop
    source = Path(source)
    clips_dir = Path(clips_dir)
    temp_dir = Path(temp_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    span = max(0.0, 1.0 - start_progress)

    def report(local_frac: float, stage: str) -> None:
        cb(start_progress + span * local_frac, stage)

    # --- probe ------------------------------------------------------------
    report(0.0, "Analyzing video")
    info = fu.probe(source)
    if info.duration <= 0:
        raise ValueError("Source video has zero duration")

    # --- transcribe -------------------------------------------------------
    report(0.05, "Transcribing audio")
    if info.has_audio:
        transcript = transcribe(
            source, language=options.language, translate=options.translate
        )
    else:
        transcript = Transcript(language="none", segments=[])
    report(_P_TRANSCRIBE_END, "Finding the best moments")

    # --- AI highlight selection (with process-range + fallback) -----------
    ranged = _filter_transcript_to_range(
        transcript, options.range_start, options.range_end
    )
    # The effective duration selection may span (respect an explicit range end).
    eff_duration = min(info.duration, options.range_end) if options.range_end else info.duration

    candidates = sel.select_moments(
        ranged if (options.range_start is not None or options.range_end is not None)
        else transcript,
        options,
        source,
        eff_duration,
        client=llm_client,
    )
    # Enforce the process-range floor on candidate starts.
    if options.range_start is not None:
        candidates = [c for c in candidates if c.end > options.range_start]
        for c in candidates:
            c.start = max(c.start, options.range_start)

    report(_P_SELECT_END, f"Creating {len(candidates)} clip(s)")
    if not candidates:
        report(1.0, "Done")
        return []

    # --- per-clip processing ---------------------------------------------
    results: list[ClipResult] = []
    clip_span = _P_CLIPS_END - _P_SELECT_END
    n = len(candidates)

    for idx, c in enumerate(candidates):
        base = _P_SELECT_END + clip_span * (idx / n)
        report(base, f"Rendering clip {idx + 1} of {n}")

        clip_id = f"{idx + 1:02d}_{uuid.uuid4().hex[:6]}"
        raw = temp_dir / f"raw_{clip_id}.mp4"
        reframed = temp_dir / f"reframed_{clip_id}.mp4"
        final = clips_dir / f"clip_{clip_id}.mp4"

        # 1. cut the selected segment
        fu.cut_segment(source, c.start, c.end, raw)

        # 2. reformat to the requested aspect ratio (blurred-bg fill)
        fu.reformat_aspect(raw, reframed, aspect=options.aspect, mode="crop_blur")

        # 3. burn captions (if enabled and we have words in range)
        if options.captions and transcript.words:
            try:
                cap.build_and_burn(
                    transcript, reframed, c.start, c.end, final,
                    ass_path=temp_dir / f"cap_{clip_id}.ass",
                    video_width=fu.ASPECT_PRESETS[options.aspect][0],
                    video_height=fu.ASPECT_PRESETS[options.aspect][1],
                )
            except fu.FFmpegError:
                reframed.replace(final)
        else:
            reframed.replace(final)

        # 4. thumbnail from the finished clip
        thumb = clips_dir / f"clip_{clip_id}.jpg"
        try:
            fu.generate_thumbnail(final, thumb, at=min(1.0, c.duration / 2))
        except fu.FFmpegError:
            thumb = None

        # 5. AI metadata for this clip's transcript text
        clip_text = c.text or cap_text(transcript, c.start, c.end)
        if options.metadata:
            report(base + clip_span / n * 0.5, f"Writing copy for clip {idx + 1}")
            md = meta_mod.generate_metadata(clip_text, options, client=llm_client)
        else:
            md = meta_mod.ClipMetadata(platform=options.platform)

        results.append(
            ClipResult(
                id=clip_id,
                filename=final.name,
                start=round(c.start, 2),
                end=round(c.end, 2),
                duration=round(c.end - c.start, 2),
                title=md.title or c.title or f"Clip {idx + 1}",
                video_url=f"clips/{final.parent.name}/{final.name}",
                thumbnail_url=(f"clips/{final.parent.name}/{thumb.name}" if thumb else ""),
                score=c.score,
                reason=c.reason,
                platform=md.platform,
                title_alternatives=md.title_alternatives,
                description=md.description,
                hashtags=md.hashtags,
                hook_text=md.hook_text,
                cta=md.cta,
                mentions=md.mentions,
                thumbnail_text=md.thumbnail_text,
                transcript_text=clip_text,
            )
        )

        for tmp in (raw, reframed):
            tmp.unlink(missing_ok=True)

        report(_P_SELECT_END + clip_span * ((idx + 1) / n),
               f"Rendered clip {idx + 1} of {n}")

    report(1.0, "Done")
    return results


def cap_text(transcript: Transcript, start: float, end: float) -> str:
    """Return transcript text within ``[start, end]`` (source-relative)."""
    parts = []
    for s in transcript.segments:
        mid = (s.start + s.end) / 2
        if start <= mid <= end:
            parts.append(s.text.strip())
    return " ".join(parts).strip()
