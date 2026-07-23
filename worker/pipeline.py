"""End-to-end clip generation pipeline (Phase 1).

Given a source video (local file or already-downloaded URL) and processing
options, this module runs the full deterministic pipeline:

    ingest/probe -> transcribe -> segment -> per clip: cut -> reformat ->
    (optional) burn captions -> thumbnail

Progress is reported through a callback so the job manager can surface live
status to the UI. The pipeline is pure orchestration over the tested building
blocks in :mod:`worker.ffmpeg_utils`, :mod:`worker.transcribe`,
:mod:`worker.segmentation`, and :mod:`worker.captions`.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Optional

from config import settings
from worker import captions as cap
from worker import ffmpeg_utils as fu
from worker import segmentation as seg
from worker.models import ClipResult, ProcessingOptions
from worker.transcribe import Transcript, transcribe

# progress_cb(fraction: float, stage: str)
ProgressCallback = Callable[[float, str], None]

# Progress budget across pipeline stages (must sum to 1.0).
_P_TRANSCRIBE_END = 0.30
_P_SEGMENT_END = 0.35
_P_CLIPS_END = 1.0


def _noop(fraction: float, stage: str) -> None:  # pragma: no cover
    pass


def run_pipeline(
    source: str | Path,
    options: ProcessingOptions,
    clips_dir: str | Path,
    temp_dir: str | Path,
    progress_cb: Optional[ProgressCallback] = None,
    start_progress: float = 0.0,
) -> list[ClipResult]:
    """Run the full pipeline on ``source`` and return the produced clips.

    Args:
        source: Path to a local source video (already downloaded if from a URL).
        options: User processing options.
        clips_dir: Directory to write finished clips + thumbnails into.
        temp_dir: Scratch directory for intermediate artefacts.
        progress_cb: Optional ``fn(fraction, stage)`` progress callback. The
            ``fraction`` already accounts for ``start_progress`` (useful when a
            caller reserves an initial slice for e.g. downloading).
        start_progress: Fraction already consumed before this call (0..1). The
            remaining ``1 - start_progress`` is distributed across stages.

    Returns:
        A list of :class:`ClipResult`.
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
    transcript: Transcript
    if info.has_audio:
        transcript = transcribe(
            source, language=options.language, translate=options.translate
        )
    else:
        transcript = Transcript(language="none", segments=[])
    report(_P_TRANSCRIBE_END, "Finding clip moments")

    # --- segment ----------------------------------------------------------
    max_clips = seg.resolve_max_clips(options.num_clips)
    segments = seg.segment_video(
        source,
        info.duration,
        clip_length=options.clip_length,
        strategy=options.strategy,
        max_clips=max_clips,
    )
    report(_P_SEGMENT_END, f"Creating {len(segments)} clip(s)")

    if not segments:
        return []

    # --- per-clip processing ---------------------------------------------
    results: list[ClipResult] = []
    clip_span = _P_CLIPS_END - _P_SEGMENT_END
    n = len(segments)

    for idx, s in enumerate(segments):
        base = _P_SEGMENT_END + clip_span * (idx / n)
        report(base, f"Rendering clip {idx + 1} of {n}")

        clip_id = f"{idx + 1:02d}_{uuid.uuid4().hex[:6]}"
        raw = temp_dir / f"raw_{clip_id}.mp4"
        reframed = temp_dir / f"reframed_{clip_id}.mp4"
        final = clips_dir / f"clip_{clip_id}.mp4"

        # 1. cut the segment
        fu.cut_segment(source, s.start, s.end, raw)

        # 2. reformat to the requested aspect ratio (blurred-bg fill)
        fu.reformat_aspect(raw, reframed, aspect=options.aspect, mode="crop_blur")

        # 3. burn captions (if enabled and we have words in range)
        if options.captions and transcript.words:
            try:
                cap.build_and_burn(
                    transcript,
                    reframed,
                    s.start,
                    s.end,
                    final,
                    ass_path=temp_dir / f"cap_{clip_id}.ass",
                    video_width=fu.ASPECT_PRESETS[options.aspect][0],
                    video_height=fu.ASPECT_PRESETS[options.aspect][1],
                )
            except fu.FFmpegError:
                # If captioning fails, still deliver the reframed clip.
                reframed.replace(final)
        else:
            reframed.replace(final)

        # 4. thumbnail from the finished clip
        thumb = clips_dir / f"clip_{clip_id}.jpg"
        try:
            fu.generate_thumbnail(final, thumb, at=min(1.0, s.duration / 2))
        except fu.FFmpegError:
            thumb = None

        results.append(
            ClipResult(
                id=clip_id,
                filename=final.name,
                start=round(s.start, 2),
                end=round(s.end, 2),
                duration=round(s.duration, 2),
                title=f"Clip {idx + 1}",
                video_url=f"clips/{final.parent.name}/{final.name}",
                thumbnail_url=(
                    f"clips/{final.parent.name}/{thumb.name}" if thumb else ""
                ),
            )
        )

        # cleanup intermediates for this clip
        for tmp in (raw, reframed):
            tmp.unlink(missing_ok=True)

        report(_P_SEGMENT_END + clip_span * ((idx + 1) / n),
               f"Rendered clip {idx + 1} of {n}")

    report(1.0, "Done")
    return results
