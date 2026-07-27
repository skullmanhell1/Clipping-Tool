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

from config import settings
from worker import captions as cap
from worker import diarization
from worker import ffmpeg_utils as fu
from worker import metadata as meta_mod
from worker import selection as sel
from worker import visual_selection
from worker.effects import broll, compositor, filler, reframe
from worker.engines.base import Engine_Stage
from worker.engines.host import Engine_Host
from worker.llm_client import BaseLLMClient
from worker.models import ClipResult, ProcessingOptions, effective_options
from worker.transcribe import Transcript, transcribe

# progress_cb(fraction: float, stage: str)
ProgressCallback = Callable[[float, str], None]

# --- optional dependency-injection seams (mirroring the broll DI style) ------
# These default to ``None`` (nothing configured), exactly like the b-roll
# external provider: production wiring or tests replace them by patching the
# module-level name. Keeping them here (rather than as function args) matches
# how the pipeline already threads optional collaborators such as the b-roll
# resolver and the injected ``llm_client``.
#
#   DIAR_BACKEND   optional ``worker.diarization.DiarizationBackend`` (None ->
#                  offline transcript-only segmentation).
#   FACE_DETECTOR  optional face detector callable ``frame -> [(x, y, w, h)]``
#                  passed to ``reframe.detect_faces`` (None -> lazy Haar cascade).
#   FRAME_SAMPLER  optional sampler ``video -> list[list[FaceBox]]`` passed to
#                  ``reframe.apply_speaker_reframe`` (None -> detect_faces).
DIAR_BACKEND = None
FACE_DETECTOR = None
FRAME_SAMPLER = None

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

    # Enforce cross-cutting rules ONCE, centrally (Req 19.1): under
    # ``permissibility_mode`` this mutes added audio and forces ``local_only``
    # sourcing, and it downgrades ``local_then_external`` -> ``local_only`` when
    # no external provider key is configured. Every downstream stage (selection,
    # captions, b-roll, music) then inherits the normalised options.
    options = effective_options(options)

    # Advanced AV engines (Reqs 4.4, 23.2): the host is built from the ALREADY
    # normalised options, and every hook below is guarded by ``host.active`` — so
    # with no engine registered or none enabled there is no probe, no workspace,
    # no extra media pass and no extra marker, and this run reproduces v0.8.0
    # exactly (Reqs 19.5, 23.1). The host never mutates ``options`` (Req 1.3).
    host = Engine_Host(options, job_id=temp_dir.name, temp_dir=temp_dir)

    span = max(0.0, 1.0 - start_progress)

    def report(local_frac: float, stage: str) -> None:
        cb(start_progress + span * local_frac, stage)

    # --- probe ------------------------------------------------------------
    report(0.0, "Analyzing video")
    info = fu.probe(source)
    if info.duration <= 0:
        raise ValueError("Source video has zero duration")

    # SOURCE-stage engines run at most once per source, reusing the probe just
    # performed to build the job's shared Time_Base — no additional ffprobe pass
    # is added (Reqs 3.5, 13.2, 13.7, 19.3, 19.4).
    if host.active:
        host.run_source(source, info)

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

    # Visual / prompt-aware selection (Feature C). ``select_moments_visual``
    # delegates straight back to ``sel.select_moments`` when visual selection is
    # disabled or degrades (no LLM / sampling failure / unconfigured provider),
    # so behaviour is identical to before when the feature is off (Reqs 13.2,
    # 15.4).
    candidates = visual_selection.select_moments_visual(
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

    # B-roll engine (Feature B), built once and shared across clips. Providers
    # are dependency-injected: the local library needs no network, and an
    # ExternalProvider is only constructed when an operator has configured a
    # BYOK key AND explicitly enabled external downloading (both OFF by
    # default). When b-roll is disabled no resolver is threaded into the
    # compositor at all (Req 18.3).
    broll_engine = None
    if options.broll:
        external = None
        if settings.broll_provider_api_key and settings.broll_allow_download:
            external = broll.ExternalProvider(
                settings.broll_provider_api_key,
                settings.broll_provider_base_url,
            )
        broll_engine = broll.Broll_Engine(
            options, local=broll.LocalProvider(), external=external
        )

    # --- speaker diarisation (ONCE per source) ---------------------------
    # Diarisation is needed when the diarisation toggle OR speaker-aware reframe
    # is enabled (Req 15.1). Enabling ``speaker_reframe`` uses diarisation
    # internally WITHOUT mutating ``options`` — the persisted ``diarization``
    # toggle is never flipped (Req 16.5). When neither is enabled no diarisation
    # and no face sampling occur at all (Req 15.4), so an all-off run is the
    # v0.7.0 code path exactly. ``diarize_source`` is pure/offline; it performs
    # no network access and, under ``permissibility_mode``, ignores any injected
    # backend and segments from the offline Word_Timeline only (Reqs 19.1-19.3).
    need_diar = options.diarization or options.speaker_reframe
    source_turns: list[diarization.Speaker_Turn] = []
    source_diar_notes: list[str] = []
    if need_diar and transcript.words:
        source_turns = diarization.diarize_source(
            transcript.words,
            info.duration,
            backend=DIAR_BACKEND,
            max_speakers=settings.diarization_max_speakers,
            permissibility=options.permissibility_mode,
            notes=source_diar_notes,
        )

    for idx, c in enumerate(candidates):
        base = _P_SELECT_END + clip_span * (idx / n)
        report(base, f"Rendering clip {idx + 1} of {n}")

        clip_id = f"{idx + 1:02d}_{uuid.uuid4().hex[:6]}"
        raw = temp_dir / f"raw_{clip_id}.mp4"
        geo = temp_dir / f"geo_{clip_id}.mp4"
        final = clips_dir / f"clip_{clip_id}.mp4"
        applied: list[str] = []
        broll_assets: list[dict] = []
        # Filler keep-plan for this clip (None unless filler removal tightened
        # the timeline). Used to rebase speaker turns onto the same tightened
        # timeline the rebased words already use (Reqs 13.4, 13.5).
        keep_plan: Optional[list] = None
        # Final clip duration for b-roll planning; shrinks after filler removal.
        clip_duration = c.end - c.start
        # Best-effort visual-selection marker (Req 18.2): when visual selection
        # is enabled and candidates were produced, note it on the clip. The
        # entry point degrades to transcript-only internally, so this is a
        # best-effort provenance marker rather than a strict guarantee.
        if options.visual_selection:
            applied.append("visual_selection")

        # 1. cut the selected segment
        fu.cut_segment(source, c.start, c.end, raw)

        # 2. AI metadata first, so the hook title is available to the renderer.
        clip_text = c.text or cap_text(transcript, c.start, c.end)
        if options.metadata:
            report(base + clip_span / n * 0.3, f"Writing copy for clip {idx + 1}")
            md = meta_mod.generate_metadata(clip_text, options, client=llm_client)
        else:
            md = meta_mod.ClipMetadata(platform=options.platform)

        # Clip-relative words (rebased to 0 at the clip start) for captions/emoji.
        words = cap.slice_words(transcript, c.start, c.end) if transcript.words else []

        # 3. filler-word / long-pause removal (adjusts the timeline + words).
        if options.filler_removal and words:
            plan = filler.plan_keep_intervals(words, c.duration)
            if plan.changed:
                trimmed = temp_dir / f"trim_{clip_id}.mp4"
                try:
                    filler.apply_keep_intervals(raw, plan.keeps, trimmed)
                    raw.unlink(missing_ok=True)
                    raw = trimmed
                    words = filler.rebase_words(words, plan.keeps)
                    keep_plan = plan.keeps
                    clip_duration = sum(k.duration for k in plan.keeps)
                    applied.append("filler_removal")
                except fu.FFmpegError:
                    pass  # keep the untrimmed clip on failure

        # 3b. AUDIO-stage engines. They see the REBASED clip-relative words and
        #     the post-filler duration (Reqs 15.1, 15.2) and may hand back
        #     replacement media; a failed or degraded engine returns no media, so
        #     ``raw`` (the pre-stage media) is kept and the clip still renders
        #     (Req 8.3).
        if host.active:
            out = host.run_stage(
                Engine_Stage.AUDIO, clip_id=clip_id, source=source, clip_path=raw,
                clip_start=c.start, clip_end=c.end, duration=clip_duration,
                words=words,
            )
            raw = out.media or raw
            applied.extend(out.markers)

        # 4. geometry: precedence ladder (Reqs 12.1-12.4, 14.1-14.5).
        #    speaker-aware reframe -> single-speaker reframe -> static crop-blur.
        #    When ``speaker_reframe`` is OFF this collapses to the exact v0.7.0
        #    branch (``if options.reframe ... else ...``) with identical
        #    ``effects_applied`` — no diarisation, no new markers, no behavioural
        #    change (Reqs 16.4, 17.2).
        if options.speaker_reframe:
            # Derive clip-relative turns from the once-per-source diarisation,
            # rebased onto the tightened timeline when filler removal changed the
            # clip so turns stay aligned to the rebased words (Reqs 13.3-13.5).
            clip_turns = diarization.slice_turns(source_turns, c.start, c.end)
            if keep_plan is not None:
                clip_turns = diarization.rebase_turns(clip_turns, keep_plan)
            try:
                reframe.apply_speaker_reframe(
                    raw, geo, turns=clip_turns, aspect=options.aspect,
                    layout=options.reframe_layout,
                    intensity=options.reframe_intensity,
                    detector=FACE_DETECTOR, sampler=FRAME_SAMPLER,
                )
                # Record the applied-layout marker (Req 14.5) and attach the
                # per-source diarisation provenance notes (Reqs 4.2/4.4/16.5).
                applied.append(f"speaker_reframe:{options.reframe_layout}")
                applied.extend(source_diar_notes)
            except (reframe.ReframeUnavailable, fu.FFmpegError):
                # Fall back along the chain: single-speaker reframe, then static
                # crop-blur (Reqs 14.1-14.4).
                applied.append("speaker_reframe_degraded")
                try:
                    reframe.apply_reframe(raw, geo, aspect=options.aspect)
                    applied.append("reframe")
                except (reframe.ReframeUnavailable, fu.FFmpegError):
                    fu.reformat_aspect(raw, geo, aspect=options.aspect, mode="crop_blur")
        elif options.reframe:
            try:
                reframe.apply_reframe(raw, geo, aspect=options.aspect)
                applied.append("reframe")
            except (reframe.ReframeUnavailable, fu.FFmpegError):
                fu.reformat_aspect(raw, geo, aspect=options.aspect, mode="crop_blur")
        else:
            fu.reformat_aspect(raw, geo, aspect=options.aspect, mode="crop_blur")

        # 4b. GEOMETRY-stage engines, after the untouched ladder above. As at the
        #     audio stage, replacement media is adopted only when an engine
        #     actually succeeded; otherwise ``geo`` is kept (Req 8.3).
        if host.active:
            out = host.run_stage(
                Engine_Stage.GEOMETRY, clip_id=clip_id, source=source, clip_path=geo,
                clip_start=c.start, clip_end=c.end, duration=clip_duration,
                words=words,
            )
            geo = out.media or geo
            applied.extend(out.markers)

        # 5. compositor: captions/hook + look effects + emoji + b-roll + music
        #    (one pass). B-roll cues are planned + resolved lazily from the
        #    REBASED clip-relative timeline (Req 11.1) via a resolver, so no cue
        #    can land in a removed interval. The resolver is only threaded in
        #    when b-roll is enabled; otherwise it is ``None`` (b-roll disabled).
        report(base + clip_span / n * 0.6, f"Adding effects to clip {idx + 1}")
        broll_resolver = None
        if broll_engine is not None:
            broll_resolver = (
                lambda w=words, d=clip_duration:
                broll_engine.resolve(broll_engine.plan(w, d))
            )
        # COMPOSE-stage engines contribute filter-graph fragments to that SAME
        # single pass — they never invoke ffmpeg themselves (Reqs 1.5, 23.3).
        compose = None
        if host.active:
            compose = host.run_stage(
                Engine_Stage.COMPOSE, clip_id=clip_id, source=source, clip_path=geo,
                clip_start=c.start, clip_end=c.end, duration=clip_duration,
                words=words,
            )
        try:
            rendered = compositor.render_clip(
                geo, final, options, words, temp_dir,
                hook_text=md.hook_text, llm_client=llm_client,
                broll_resolver=broll_resolver,
                engine_contributions=(compose.contributions if compose is not None else None),
            )
        except fu.FFmpegError:
            rendered = None
        if rendered is not None:
            applied.extend(rendered.effects_applied)
            if rendered.broll_records:
                broll_assets = rendered.broll_records
        else:
            geo.replace(final)
        if compose is not None:
            applied.extend(compose.markers)

        # 6. thumbnail from the finished clip
        thumb = clips_dir / f"clip_{clip_id}.jpg"
        try:
            fu.generate_thumbnail(final, thumb, at=min(1.0, c.duration / 2))
        except fu.FFmpegError:
            thumb = None

        # 6b. POST-stage engines see the finished clip, then this clip's engine
        #     lifecycle is closed: durable artifacts are persisted BEFORE the
        #     workspaces are deleted, and a persistence failure only adds an
        #     ``engine:<id>:artifact_failed`` marker (Reqs 17.1, 17.6, 17.7, 18.6).
        if host.active:
            out = host.run_stage(
                Engine_Stage.POST, clip_id=clip_id, source=source, clip_path=final,
                clip_start=c.start, clip_end=c.end, duration=clip_duration,
                words=words,
            )
            applied.extend(out.markers)
            applied.extend(host.finish_clip(clip_id))

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
                effects_applied=applied,
                broll_assets=broll_assets,
            )
        )

        for tmp in (raw, geo):
            tmp.unlink(missing_ok=True)

        report(_P_SELECT_END + clip_span * ((idx + 1) / n),
               f"Rendered clip {idx + 1} of {n}")

    # Release the job's engine scratch space (and finalise the SOURCE stage,
    # which belongs to no clip). Job-level markers have no ClipResult to land in,
    # so the host logs them (Reqs 17.1, 17.6).
    if host.active:
        host.finish_job()

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
