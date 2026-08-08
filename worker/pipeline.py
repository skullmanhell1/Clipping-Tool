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

import logging
import uuid
from collections.abc import Callable
from pathlib import Path

from config import settings
from worker import captions as cap
from worker import (
    colour,
    diarization,
    frame_rate,
    intermediate_cache,
    segmentation,
    subtitle_export,
    thumbnail,
    video_encoders,
    visual_selection,
)
from worker import ffmpeg_utils as fu
from worker import metadata as meta_mod

# Re-exported deliberately, on its own line so that neither the alias nor this
# suppression can be lost when the import block is re-sorted. This module never calls
# ``sel`` itself — selection is reached through ``visual_selection``, which delegates to
# it — but the tests patch ``pipeline.sel.select_moments`` to control which moments get
# chosen, so the alias is the seam that makes the pipeline testable. Removing it breaks
# 23 tests with "module 'worker.pipeline' has no attribute 'sel'".
from worker import selection as sel  # noqa: F401
from worker import transcript_trim as trim
from worker.effects import broll, compositor, filler, reframe
from worker.engines import loader  # noqa: F401  (side-effect import: registers the engines)
from worker.engines.base import Engine_Stage
from worker.engines.host import Engine_Host
from worker.llm_client import BaseLLMClient
from worker.models import ClipResult, ProcessingOptions, effective_options
from worker.transcribe import Transcript, transcribe

logger = logging.getLogger(__name__)

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
    transcript: Transcript, start: float | None, end: float | None
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
    progress_cb: ProgressCallback | None = None,
    start_progress: float = 0.0,
    llm_client: BaseLLMClient | None = None,
    explicit_candidates: list | None = None,
    on_plan: Callable[[list], None] | None = None,
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
        explicit_candidates: Windows to render, skipping selection entirely (U7).
            ``None`` - the default and every pre-U7 caller - selects as before.

    Returns:
        A list of :class:`ClipResult` ordered by virality score (best first).

    The ``candidates`` argument is what makes a single clip re-renderable without
    re-running a whole job. It is a parameter rather than a separate clip-render function
    on purpose: the per-clip path below is two hundred lines of filler removal, diarisation
    rebasing, b-roll, engine stages, captions and thumbnailing, and a second copy of it
    would drift from this one within a release. Passing the window in reuses that path
    exactly, so a re-rendered clip is byte-for-byte what a full run would have produced
    from the same options.
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

    # O18/O19: decide the delivered frame rate once, from the probe just taken.
    #
    # The previous blanket `-r 30` was right about VFR and wrong about CFR 24, which it resampled
    # into 3:2 judder. `plan_frame_rate` narrows the rule rather than removing it -- VFR and
    # undeterminable sources still normalise, for the reason `config.py` documented.
    #
    # Gated on M11 (R8.9): `tests/test_frame_rate_policy.py` verifies A/V sync at every rate this
    # can deliver, because frame-rate handling is the likeliest place to introduce drift and drift
    # desynchronises every burned caption.
    _rate_plan = frame_rate.plan_frame_rate(
        avg_fps=info.fps,
        base_fps=getattr(info, "base_fps", 0.0) or info.fps,
        configured_fps=int(settings.output_fps),
        always_normalise=str(settings.frame_rate_policy).lower() == "always",
        ceiling_fps=None,
    )
    delivered_fps = _rate_plan.delivered_fps
    keyframe_seconds = float(settings.keyframe_seconds)

    # O13/O14/O15: decide the colour treatment once, here, from the probe just performed.
    #
    # Once per *job* rather than per clip because colour is a property of the source, not of a
    # window into it. Deciding it per clip would re-probe nothing new and would open the door to
    # two clips from one source being tone-mapped differently, which is the kind of inconsistency
    # nobody notices until two clips are cut together.
    #
    # The plan is empty for SDR and for unknown sources, which is the overwhelming majority --
    # so this adds no filter, no marker and no argv change to an ordinary render.
    colour_plan = colour.plan_colour(
        transfer=info.color_transfer,
        primaries=info.color_primaries,
        matrix=info.color_space,
        source_range=info.color_range,
        tone_map_enabled=bool(settings.tone_mapping),
        operator=str(settings.tone_map_operator),
        target_nits=int(settings.tone_map_target_nits),
        delivery_range=str(settings.delivery_colour_range),
    )
    # The tags travel to every pass; the *filters* are spent by the first one that runs (R2.8).
    colour_tags = colour_plan.tags

    # SOURCE-stage engines run at most once per source, reusing the probe just
    # performed to build the job's shared Time_Base — no additional ffprobe pass
    # is added (Reqs 3.5, 13.2, 13.7, 19.3, 19.4).
    if host.active:
        host.run_source(source, info)

    # --- transcribe -------------------------------------------------------
    report(0.05, "Transcribing audio")
    if info.has_audio:
        # T8: reuses a cached transcript when the source content and ASR settings match, so
        # re-running a source to try different effects does not re-transcribe it.
        transcript = transcribe(
            source,
            language=options.language,
            translate=options.translate,
            # T4: names, jargon and brands for this video specifically.
            vocabulary=getattr(options, "vocabulary", "") or "",
        )
    else:
        transcript = Transcript(language="none", segments=[])

    # T10: an English subtitle track *alongside* the original-language captions.
    #
    # `task=translate` replaces the transcript text, so asking for a translation used to cost
    # the original-language captions entirely - a Spanish creator's clip came back with English
    # burned into the pixels. Here the burned captions stay in the source language and English
    # arrives as a separate track, which is the only form a viewer can switch off.
    #
    # Run once per source rather than per clip: it is a full ASR pass, and slicing it per clip
    # costs nothing. Skipped outright when it could not add anything, with a marker saying so,
    # because a silently-absent track is indistinguishable from a broken one.
    translated: Transcript | None = None
    translation_marker = ""
    if settings.subtitle_translation and transcript.segments:
        if options.translate:
            # The main pass is already English; a second translate pass would decode the same
            # audio to the same text and label it as a translation of itself.
            translation_marker = "subtitle_translation:skipped_already_translated"
        elif str(transcript.language or "").lower() in ("en", "eng", "english"):
            translation_marker = "subtitle_translation:skipped_english"
        else:
            report(_P_TRANSCRIBE_END * 0.9, "Translating subtitles")
            try:
                translated = transcribe(
                    source,
                    language=options.language,
                    translate=True,
                    vocabulary=getattr(options, "vocabulary", "") or "",
                )
            except Exception as exc:
                # Deliberately broad: this is an extra track on a job whose expensive work is
                # still ahead of it, and every failure mode of a model call (OOM, a missing
                # weight file, a corrupt download) is a reason to ship the clips without the
                # translation rather than to lose the job.
                logger.warning("T10: translated transcription failed for %s: %s", source, exc)
                translation_marker = "subtitle_translation:failed"
            else:
                if not translated.words:
                    translated = None
                    translation_marker = "subtitle_translation:empty"

    # O8: resolved once per job, not per clip - the probe runs a real one-frame encode, and the
    # answer cannot change between two clips of the same source.
    encoder_choice = video_encoders.resolve_encoder()
    encoder_marker = encoder_choice.marker
    if encoder_choice.encoder.hardware:
        encoder_marker = encoder_marker or f"encoder:{encoder_choice.encoder.name}"

    report(_P_TRANSCRIBE_END, "Finding the best moments")

    # --- AI highlight selection (with process-range + fallback) -----------
    ranged = _filter_transcript_to_range(transcript, options.range_start, options.range_end)
    # The effective duration selection may span (respect an explicit range end).
    eff_duration = min(info.duration, options.range_end) if options.range_end else info.duration

    # Visual / prompt-aware selection (Feature C). ``select_moments_visual``
    # delegates straight back to ``sel.select_moments`` when visual selection is
    # disabled or degrades (no LLM / sampling failure / unconfigured provider),
    # so behaviour is identical to before when the feature is off (Reqs 13.2,
    # 15.4).
    # U7: an explicit window skips selection (and its LLM call) entirely.
    candidates = explicit_candidates or visual_selection.select_moments_visual(
        ranged
        if (options.range_start is not None or options.range_end is not None)
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

    # I5: publish the plan before any rendering starts. A job interrupted halfway can then be
    # resumed against the windows it actually chose, instead of re-running a selection that -
    # with an LLM in it - is not deterministic and could return different moments, leaving the
    # user with clips from two different selections.
    if on_plan is not None:
        try:
            on_plan(list(candidates))
        except Exception:
            # The plan is an aid to resuming, never a precondition for rendering.
            logger.warning("I5: could not record the clip plan", exc_info=True)

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
        broll_engine = broll.Broll_Engine(options, local=broll.LocalProvider(), external=external)

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

    # AU7: detect silence once for the whole source, not once per clip. Every clip's
    # boundaries are looked up in the same measurement, and silencedetect needs a full decode
    # - paying for that per clip would add a pass per clip for a sub-second adjustment.
    # Best-effort: a source we cannot scan simply gets no trimming.
    source_silences: list[tuple[float, float]] = []
    if options.trim_silence:
        try:
            # I3: cached by source content. `silencedetect` needs a whole-file decode, and the
            # answer depends on nothing a user changes between runs of the same video - so a
            # re-run to try a different caption preset was paying for this again.
            source_silences = [
                (float(a), float(b))
                for a, b in intermediate_cache.memoise(
                    "silences",
                    source,
                    lambda: [list(pair) for pair in segmentation.detect_silences(source)],
                )
            ]
        except (fu.FFmpegError, OSError):
            source_silences = []

    for idx, c in enumerate(candidates):
        base = _P_SELECT_END + clip_span * (idx / n)
        report(base, f"Rendering clip {idx + 1} of {n}")

        clip_id = f"{idx + 1:02d}_{uuid.uuid4().hex[:6]}"
        raw = temp_dir / f"raw_{clip_id}.mp4"
        geo = temp_dir / f"geo_{clip_id}.mp4"
        final = clips_dir / f"clip_{clip_id}.mp4"
        applied: list[str] = []
        # T10: why a requested translated track is not on this clip. Recorded per clip even
        # though the reason is a property of the source, because the clip record is the only
        # thing a caller sees - an absent track with no explanation is indistinguishable from
        # a broken one.
        if translation_marker:
            applied.append(translation_marker)
        # O8: which encoder actually ran, when it is not the one configured. A property of the
        # machine rather than of this clip, but the clip record is the only thing a caller sees,
        # and "my GPU is not being used" is exactly the question this answers.
        if encoder_marker:
            applied.append(encoder_marker)
        broll_assets: list[dict] = []
        # Filler keep-plan for this clip (None unless filler removal tightened
        # the timeline). Used to rebase speaker turns onto the same tightened
        # timeline the rebased words already use (Reqs 13.4, 13.5).
        keep_plan: list | None = None
        # Final clip duration for b-roll planning; shrinks after filler removal.
        clip_duration = c.end - c.start
        # Best-effort visual-selection marker (Req 18.2): when visual selection
        # is enabled and candidates were produced, note it on the clip. The
        # entry point degrades to transcript-only internally, so this is a
        # best-effort provenance marker rather than a strict guarantee.
        if options.visual_selection:
            applied.append("visual_selection")

        # 0. AU7: pull the cut points onto speech before cutting, so the clip does not open
        # on dead air - the first second is where a viewer decides whether to keep watching.
        # Adjusting the window rather than filtering the audio costs no extra pass and keeps
        # the streams in step by construction.
        if options.trim_silence and source_silences:
            trimmed_start, trimmed_end = segmentation.trim_edge_silence(
                c.start, c.end, source_silences
            )
            if (trimmed_start, trimmed_end) != (c.start, c.end):
                c.start, c.end = trimmed_start, trimmed_end
                clip_duration = c.end - c.start
                applied.append("silence_trimmed")

        # 1. cut the selected segment
        #
        # O13: this is where the colour conversion happens, and the only place it happens. The
        # cut is the one pass with no geometry and no grade of its own, so it is the only
        # placement that satisfies R2.2's "before any colour-dependent operation *and* before
        # scaling" -- the geometry pass scales and the composite pass grades.
        fu.cut_segment(
            source,
            c.start,
            c.end,
            raw,
            video_filters=colour_plan.filter_chain,
            colour_tags=colour_tags,
        )
        # Spent. Every later pass in this clip carries the tags and none of them re-converts
        # (R2.8): tone-mapping twice compresses the range twice and delivers a flat, muddy
        # picture that still looks like a plausible image, which makes it far worse to diagnose
        # than no tone-map at all.
        clip_colour = colour_plan.consumed()
        applied = colour.merge_markers(applied, colour_plan)
        # O18: record what was delivered and whether it was resampled (R8.10). After
        # `merge_markers`, which rebinds `applied` — appending before it would drop this marker
        # on every clip, and a missing marker is invisible in a render that otherwise succeeds.
        if _rate_plan.marker:
            applied.append(_rate_plan.marker)

        # 2. AI metadata first, so the hook title is available to the renderer.
        clip_text = c.text or cap_text(transcript, c.start, c.end)
        if options.metadata:
            report(base + clip_span / n * 0.3, f"Writing copy for clip {idx + 1}")
            md = meta_mod.generate_metadata(clip_text, options, client=llm_client)
        else:
            md = meta_mod.ClipMetadata(platform=options.platform)

        # Clip-relative words (rebased to 0 at the clip start) for captions/emoji.
        words = cap.slice_words(transcript, c.start, c.end) if transcript.words else []
        # T10: the same window of the translated transcript. Sliced from the translated pass's
        # own timings rather than mapped from the original's, because translation reorders
        # words - a German verb arriving at the end of the clause is an English verb in the
        # middle - so there is no word-to-word correspondence to map through.
        translated_words = (
            cap.slice_words(translated, c.start, c.end) if translated is not None else []
        )

        # 3. filler-word / long-pause removal and U4 transcript trimming. Both remove
        #    regions from the clip, so they resolve to ONE keep list and ONE re-encode:
        #    applying them in sequence would concatenate twice, and the second pass's
        #    keeps would be expressed against the first pass's output timeline rather
        #    than the one the caller's cut offsets refer to.
        pending: list[filler.Interval] | None = None
        pending_markers: list[str] = []
        if options.filler_removal and words:
            plan = filler.plan_keep_intervals(words, c.duration)
            if plan.changed:
                pending = plan.keeps
                pending_markers.append("filler_removal")
        # U4: the user's struck-out words, intersected with whatever filler removal
        # already claimed. Refusals (a cut list that empties the clip, or one long
        # enough to be the problem itself) land on the clip record as a marker and
        # leave the render exactly as it would have been.
        if getattr(c, "cuts", None):
            trim_plan = trim.plan_cuts(c.cuts, c.duration, base_keeps=pending)
            if trim_plan.changed:
                pending = trim_plan.keeps
                pending_markers.append(trim_plan.marker)
            elif trim_plan.refusal:
                applied.append(trim_plan.marker)
        if pending is not None:
            trimmed = temp_dir / f"trim_{clip_id}.mp4"
            try:
                filler.apply_keep_intervals(
                    raw,
                    pending,
                    trimmed,
                    delivered_fps=delivered_fps,
                    keyframe_seconds=keyframe_seconds,
                    colour_tags=clip_colour.tags,
                )
                raw.unlink(missing_ok=True)
                raw = trimmed
                words = filler.rebase_words(words, pending)
                # T10: the translated track is timed against the same media, so it has to
                # follow every cut made to it. Left un-rebased it would drift by the total
                # removed duration and read as a sync bug in the player.
                if translated_words:
                    translated_words = filler.rebase_words(translated_words, pending)
                keep_plan = pending
                clip_duration = sum(k.duration for k in pending)
                applied.extend(pending_markers)
            except fu.FFmpegError:
                pass  # keep the untrimmed clip on failure

        # 3b. AUDIO-stage engines. They see the REBASED clip-relative words and
        #     the post-filler duration (Reqs 15.1, 15.2) and may hand back
        #     replacement media; a failed or degraded engine returns no media, so
        #     ``raw`` (the pre-stage media) is kept and the clip still renders
        #     (Req 8.3).
        if host.active:
            out = host.run_stage(
                Engine_Stage.AUDIO,
                clip_id=clip_id,
                source=source,
                clip_path=raw,
                clip_start=c.start,
                clip_end=c.end,
                duration=clip_duration,
                words=words,
                # Seam publication (audio-stem-inpainting Reqs 6.1, 8.1): the keeps
                # already in scope, read-only. ``None`` (no filler removal, or removal
                # that changed nothing) publishes no note and leaves every context
                # exactly as it was.
                filler_plan=keep_plan,
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
                    raw,
                    geo,
                    turns=clip_turns,
                    aspect=options.aspect,
                    layout=options.reframe_layout,
                    intensity=options.reframe_intensity,
                    detector=FACE_DETECTOR,
                    sampler=FRAME_SAMPLER,
                    backend=options.face_detector,
                    notes=applied,
                    colour_tags=clip_colour.tags,
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
                    reframe.apply_reframe(
                        raw,
                        geo,
                        aspect=options.aspect,
                        backend=options.face_detector,
                        detector=FACE_DETECTOR,
                        notes=applied,
                        colour_tags=clip_colour.tags,
                    )
                    applied.append("reframe")
                except (reframe.ReframeUnavailable, fu.FFmpegError):
                    fu.reformat_aspect(
                        raw,
                        geo,
                        aspect=options.aspect,
                        mode="crop_blur",
                        colour_tags=clip_colour.tags,
                    )
        elif options.reframe:
            try:
                reframe.apply_reframe(
                    raw,
                    geo,
                    aspect=options.aspect,
                    backend=options.face_detector,
                    detector=FACE_DETECTOR,
                    notes=applied,
                    colour_tags=clip_colour.tags,
                )
                applied.append("reframe")
            except (reframe.ReframeUnavailable, fu.FFmpegError):
                fu.reformat_aspect(
                    raw,
                    geo,
                    aspect=options.aspect,
                    mode="crop_blur",
                    colour_tags=clip_colour.tags,
                )
        else:
            fu.reformat_aspect(
                raw,
                geo,
                aspect=options.aspect,
                mode="crop_blur",
                colour_tags=clip_colour.tags,
            )

        # 4b. GEOMETRY-stage engines, after the untouched ladder above. As at the
        #     audio stage, replacement media is adopted only when an engine
        #     actually succeeded; otherwise ``geo`` is kept (Req 8.3).
        if host.active:
            out = host.run_stage(
                Engine_Stage.GEOMETRY,
                clip_id=clip_id,
                source=source,
                clip_path=geo,
                clip_start=c.start,
                clip_end=c.end,
                duration=clip_duration,
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
            # A def rather than an assigned lambda: it gets a real name in tracebacks,
            # which matters because this runs deep inside the compositor. The default
            # arguments are load-bearing — they bind this iteration's words and duration
            # at definition time, so the resolver cannot pick up a later clip's values
            # when it is finally called.
            def broll_resolver(w=words, d=clip_duration):
                return broll_engine.resolve(broll_engine.plan(w, d))

        # COMPOSE-stage engines contribute filter-graph fragments to that SAME
        # single pass — they never invoke ffmpeg themselves (Reqs 1.5, 23.3).
        compose = None
        if host.active:
            compose = host.run_stage(
                Engine_Stage.COMPOSE,
                clip_id=clip_id,
                source=source,
                clip_path=geo,
                clip_start=c.start,
                clip_end=c.end,
                duration=clip_duration,
                words=words,
                clip_metadata={
                    "hook_text": md.hook_text,
                    "clip_size": fu.ASPECT_PRESETS.get(options.aspect, fu.ASPECT_PRESETS["9:16"]),
                },
            )
        try:
            rendered = compositor.render_clip(
                geo,
                final,
                options,
                words,
                temp_dir,
                hook_text=md.hook_text,
                llm_client=llm_client,
                broll_resolver=broll_resolver,
                engine_contributions=(compose.contributions if compose is not None else None),
                delivered_fps=delivered_fps,
                keyframe_seconds=keyframe_seconds,
                colour_tags=clip_colour.tags,
                # A17: which music track this clip gets, when the mood has several. Built from
                # facts that survive a re-run - the source's name, the clip's ordinal and its
                # source-relative start - so the same job produces the same beds while ten clips
                # from one source get ten different ones. The clip id cannot be used: it carries
                # a `uuid4`, so keying on it would give a fresh bed on every render.
                music_select_key=f"{Path(source).name}:{idx}:{c.start:.3f}",
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
        # Two names because they mean different things: `thumb_path` is where the thumbnail would
        # go and is always a path, while `thumb` is the result recorded on the clip and is None
        # when generation failed. Collapsing them into one made the failure assignment look like a
        # type error at the point of *use* rather than where the distinction is.
        thumb_path = clips_dir / f"clip_{clip_id}.jpg"
        thumb: Path | None = thumb_path
        try:
            # V17: score a few candidate frames rather than taking a fixed position, which on a
            # clip opening on a cut or a blink chose exactly the wrong still.
            fu.generate_thumbnail(
                final, thumb_path, at=thumbnail.choose_thumbnail_time(final, c.duration)
            )
        except fu.FFmpegError:
            thumb = None

        # 6a. O11: sidecar caption files alongside the clip, for platforms that accept uploaded
        #     captions and for anyone who needs the text rather than the burn-in. Written from
        #     the clip-relative words, so they are in sync with the file they sit next to.
        if getattr(options, "subtitle_sidecar", False):
            try:
                subtitle_export.write_sidecars(words, final.with_suffix(""))
                # T10: the translation goes beside it as `clip_N.en.srt`/`.vtt`, tagged so an
                # upload form can tell the two apart. The untagged pair keeps its existing
                # names, so nothing that already consumes them has to change.
                if translated_words:
                    subtitle_export.write_sidecars(
                        translated_words, final.with_suffix(""), language="en"
                    )
            except OSError as exc:
                # A sidecar is an extra, never a reason to lose a clip that has already cost
                # minutes of CPU. Logged rather than swallowed, so a permissions problem is
                # visible instead of appearing as silently missing files.
                logger.warning("O11: could not write sidecar captions for %s: %s", final, exc)

        # 6a-ii. O12: in `soft` or `both` mode, add the captions as a selectable subtitle track.
        #
        #        Done here rather than in the compositor because it is a remux of the finished
        #        file, not a filter: muxing during the composite pass would mean the subtitle
        #        stream survived every later stage untouched, and POST engines that replace the
        #        media would silently drop it.
        #
        #        T10's translated track is muxed in the SAME call, not a second one, because
        #        `-metadata:s:s:N` numbers subtitle streams by their position in the output: a
        #        follow-up remux of a file that already carries one track would have to know how
        #        many there were to avoid re-labelling the first. One call makes the indices a
        #        property of the argument list.
        caption_mode = str(getattr(settings, "caption_mode", "burned") or "burned")
        subtitle_tracks: list[tuple[Path, str]] = []
        track_markers: list[str] = []
        try:
            if caption_mode in ("soft", "both") and words:
                srt = subtitle_export.write_sidecars(
                    words, temp_dir / f"soft_{clip_id}", formats=("srt",)
                )
                if srt:
                    # Labelled with the language actually spoken, not a fixed "eng": a track
                    # menu offering two entries both called English is worse than no menu.
                    subtitle_tracks.append((srt[0], subtitle_export.iso639_2(transcript.language)))
                    track_markers.append(f"caption_mode:{caption_mode}")
            if translated_words:
                srt_en = subtitle_export.write_sidecars(
                    translated_words,
                    temp_dir / f"soft_{clip_id}",
                    formats=("srt",),
                    language="en",
                )
                if srt_en:
                    subtitle_tracks.append((srt_en[0], "eng"))
                    track_markers.append("subtitle_translation:track")
            if subtitle_tracks:
                muxed = temp_dir / f"soft_{clip_id}.mp4"
                fu.mux_subtitle_tracks(final, subtitle_tracks, muxed)
                muxed.replace(final)
                applied.extend(track_markers)
        except (fu.FFmpegError, OSError) as exc:
            # The burned captions (in `both`) or the sidecars (in `soft`) are already there,
            # so a failed remux costs a convenience, not the clip.
            logger.warning("O12/T10: could not mux subtitle tracks into %s: %s", final, exc)

        # 6b. POST-stage engines see the finished clip, then this clip's engine
        #     lifecycle is closed: durable artifacts are persisted BEFORE the
        #     workspaces are deleted, and a persistence failure only adds an
        #     ``engine:<id>:artifact_failed`` marker (Reqs 17.1, 17.6, 17.7, 18.6).
        if host.active:
            out = host.run_stage(
                Engine_Stage.POST,
                clip_id=clip_id,
                source=source,
                clip_path=final,
                clip_start=c.start,
                clip_end=c.end,
                duration=clip_duration,
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
                # The *rendered* length, not the source window's. These differ whenever
                # something removed a region from the middle of the clip - filler removal
                # before, a U4 cut list now - and the difference is the whole point of the
                # feature, so reporting the window would tell the caller the edit did
                # nothing. `start`/`end` still describe where in the source this came from,
                # which is what a resume matches windows on; only the length is affected.
                duration=round(clip_duration, 2),
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

        report(_P_SELECT_END + clip_span * ((idx + 1) / n), f"Rendered clip {idx + 1} of {n}")

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
