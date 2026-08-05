"""Prompt / visual clip finding (Feature C).

Augments the transcript-based :func:`worker.selection.select_moments` with two
capabilities while preserving its exact output shape (``ClipCandidate``):

* **Natural-language selection prompt** — a free-text ``selection_prompt`` biases
  moment selection toward the moments the creator describes, by folding the
  prompt into the topic that reaches the LLM request. Without an LLM the
  deterministic segmentation still runs (Reqs 13.1–13.3).
* **Visual/scene cue augmentation** — a bounded set of keyframes is sampled once
  per source (Req 17.4), cheap CPU-only brightness/motion proxies are derived,
  and those visual scores are blended with the transcript scores into a single
  ranking (Reqs 14.1, 14.2). Candidates are snapped to natural boundaries just
  like the existing selection (Req 14.5).

Every dependency is optional and degrades cleanly: keyframe sampling failures,
an unconfigured provider, or a missing LLM all fall back to transcript-only
selection (Reqs 15.2–15.4); a catastrophic failure returns ``[]`` (Req 15.5).
Heavy imaging libraries (PIL/numpy) are imported lazily so the module imports
and the deterministic paths work even when they are absent.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace

from config import settings
from worker import candidate_ranking, intermediate_cache
from worker import ffmpeg_utils as fu
from worker import segmentation as seg
from worker import selection as sel
from worker.llm_client import BaseLLMClient, llm_available
from worker.selection import ClipCandidate
from worker.transcribe import Transcript

# A sampler resolves a single keyframe: ``(source, timestamp) -> path | None``.
Sampler = Callable[[object, float], str | None]


@dataclass(frozen=True)
class Keyframe:
    """A sampled keyframe with cheap, CPU-only visual cue proxies."""

    t: float  # timestamp in seconds
    path: str  # sampled JPEG path
    brightness: float = 0.0  # mean luma normalised to [0, 1]
    motion: float = 0.0  # |brightness - previous brightness| proxy


# --------------------------------------------------------------------------- #
# 8.1 — Keyframe sampling + visual cue derivation
# --------------------------------------------------------------------------- #
def sample_keyframes(
    source,
    total_duration: float,
    *,
    limit: int,
    sampler: Sampler | None = None,
    frames_dir: str | None = None,
) -> list[Keyframe]:
    """Sample at most ``limit`` evenly-spaced keyframes across the source.

    Sampling happens exactly **once** per source (Req 17.4): ``limit``
    evenly-spaced timestamps are chosen across ``[0, total_duration]`` and each
    is materialised via ``sampler``. The default sampler writes a small JPEG per
    timestamp into a temp directory using
    :func:`worker.ffmpeg_utils.generate_thumbnail`; tests inject a lightweight
    ``sampler`` instead (Req 21.1).

    A frame whose sampler returns ``None`` (a single-frame failure) is skipped,
    so the returned list may be shorter than ``limit``. If the injected sampler
    *raises*, the error propagates to the caller (``select_moments_visual``),
    which catches it and degrades to transcript-only selection (Req 15.2).

    Args:
        frames_dir: Directory for the sampled JPEGs, used only by the default sampler.
            Pass one to control their lifetime — the returned :class:`Keyframe` paths
            must stay readable until :func:`derive_visual_cues` has run, so this function
            cannot clean up after itself. When omitted a temporary directory is created
            and **not** removed, which is the leak ``select_moments_visual`` now avoids by
            supplying its own managed directory.
    """
    try:
        count = int(limit)
    except (TypeError, ValueError):
        return []
    if count <= 0 or total_duration is None or total_duration <= 0:
        return []

    # Evenly-spaced sample points using bucket midpoints so we never land
    # exactly on 0 or total_duration.
    timestamps = [total_duration * (i + 0.5) / count for i in range(count)]

    active_sampler = sampler
    if active_sampler is None:
        tmp_dir = frames_dir or tempfile.mkdtemp(prefix="kf-")
        os.makedirs(tmp_dir, exist_ok=True)

        # S14: width was hard-coded at 160, at which the motion proxy measures little beyond
        # JPEG noise - a 160px-wide thumbnail averages away exactly the frame-to-frame
        # difference it exists to detect. Now a setting, defaulting to 480.
        width = int(getattr(settings, "keyframe_sample_width", 480) or 480)

        def _default_sampler(src, t, _dir=tmp_dir, _width=width):
            dest = os.path.join(_dir, f"kf_{t:.3f}.jpg")
            # I3: an already-extracted frame is reused rather than decoded again.
            #
            # This is the line that makes the content-addressed frames directory worth having:
            # without it the cache would hand back a directory of correct frames and then
            # overwrite every one of them, paying the 48 seeks it was supposed to save. A
            # zero-byte file is treated as absent, since a run killed mid-write leaves one.
            try:
                if os.path.getsize(dest) > 0:
                    return dest
            except OSError:
                pass
            try:
                fu.generate_thumbnail(src, dest, at=t, width=_width)
                return dest
            except Exception:
                # Single-frame failure -> skip this frame (never raise here).
                return None

        active_sampler = _default_sampler

    frames: list[Keyframe] = []
    for t in timestamps:
        path = active_sampler(source, t)  # may raise -> propagate to caller
        if path:
            # Timestamps are rounded to keep them tidy, but for very short
            # clips (sub-10ms) rounding a bucket midpoint up can push it past
            # total_duration, so clamp the recorded value back into
            # [0, total_duration]. The sampler above still receives the exact
            # unrounded ``t``, so which frames get sampled is unaffected.
            recorded_t = min(max(round(float(t), 3), 0.0), float(total_duration))
            frames.append(Keyframe(t=recorded_t, path=str(path)))
    return frames


def derive_visual_cues(frames: list[Keyframe]) -> list[Keyframe]:
    """Return ``frames`` enriched with cheap brightness/motion proxies.

    ``brightness`` is the mean luma of the frame normalised to ``[0, 1]``;
    ``motion`` is the absolute difference of a frame's brightness versus the
    previous frame (a lightweight proxy that needs no vision model). PIL/numpy
    are imported lazily and any failure degrades a frame's cues to ``0.0``
    without raising, so the module works even when those libraries are absent.
    """
    if not frames:
        return []

    try:  # lazy, optional
        from PIL import Image  # type: ignore
    except Exception:
        Image = None  # type: ignore

    try:  # lazy, optional
        import numpy as np  # type: ignore
    except Exception:
        np = None  # type: ignore

    def _brightness(path: str) -> float:
        if Image is None:
            return 0.0
        try:
            with Image.open(path) as im:
                gray = im.convert("L")
                if np is not None:
                    arr = np.asarray(gray, dtype="float64")
                    return float(arr.mean()) / 255.0 if arr.size else 0.0
                data = list(gray.getdata())
                return (sum(data) / len(data) / 255.0) if data else 0.0
        except Exception:
            return 0.0

    enriched: list[Keyframe] = []
    prev: float | None = None
    for f in frames:
        brightness = _brightness(f.path)
        motion = 0.0 if prev is None else abs(brightness - prev)
        prev = brightness
        enriched.append(replace(f, brightness=round(brightness, 6), motion=round(motion, 6)))
    return enriched


# --------------------------------------------------------------------------- #
# 8.2 — Score merging + the select_moments_visual entry point
# --------------------------------------------------------------------------- #
def merge_scores(
    transcript_candidates: list[ClipCandidate],
    visual_frames: list[Keyframe],
    *,
    weight: float | None = None,
) -> list[ClipCandidate]:
    """Blend transcript scores with visual-cue scores into one ranking.

    For each transcript candidate, the visual score is the normalised mean of
    ``brightness + motion`` across the keyframes whose timestamp falls inside the
    candidate window; it is blended as
    ``combined = (1 - weight) * transcript_score + weight * visual_score * 100``.
    New :class:`ClipCandidate` objects are returned (same shape), preserving
    every field except ``score``, sorted by combined score descending
    (Reqs 14.2, 14.4).

    Args:
        transcript_candidates: candidates carrying transcript-derived scores.
        visual_frames: sampled keyframes with brightness/motion cues.
        weight: how much the visual signal counts, in ``[0, 1]``. ``None`` reads
            ``settings.visual_selection_weight``. Values outside the range are clamped
            rather than rejected: an out-of-range weight would otherwise produce negative
            or inflated scores that silently corrupt the ranking.

    Note:
        The weight used to be a literal ``0.5`` default that the one call site never
        overrode, so a 50/50 blend was effectively hard-coded and there was no way to
        favour the transcript (the stronger signal for talking-head footage) or the
        visuals. ``0`` ignores the visual cues entirely; ``1`` ignores the transcript.
    """
    if weight is None:
        weight = settings.visual_selection_weight
    weight = max(0.0, min(1.0, float(weight)))
    merged: list[ClipCandidate] = []
    for cand in transcript_candidates:
        inside = [f for f in visual_frames if cand.start <= f.t <= cand.end]
        if inside:
            raw = sum(max(0.0, f.brightness) + max(0.0, f.motion) for f in inside)
            raw /= len(inside)
        else:
            raw = 0.0
        visual_score = max(0.0, min(1.0, raw / 2.0))  # brightness+motion -> [0,1]
        combined = (1.0 - weight) * cand.score + weight * visual_score * 100.0
        merged.append(
            ClipCandidate(
                start=cand.start,
                end=cand.end,
                score=round(combined, 2),
                reason=cand.reason,
                title=cand.title,
                text=cand.text,
                # Carried across explicitly. This rebuild dropped `features` silently, so every
                # measured signal (S2/S4/S6) vanished the moment visual selection was involved
                # - and U1 made visual selection a default, so that was the normal path. The
                # dict is copied rather than shared: two candidates aliasing one features dict
                # would let a later annotation overwrite an earlier candidate's measurements.
                features=dict(cand.features),
            )
        )
    merged.sort(key=lambda c: c.score, reverse=True)
    return merged


def _bias_options(options, client: BaseLLMClient | None):
    """Fold ``selection_prompt`` into the topic when an LLM is available.

    This is the simplest robust way to make the prompt text reach the transcript
    LLM request (Req 13.2) while staying deterministic-friendly: with no prompt,
    or when no LLM client is available, the original options are returned
    unchanged so the deterministic fallback still produces clips (Req 13.3).
    """
    prompt = (getattr(options, "selection_prompt", "") or "").strip()
    if not prompt:
        return options
    if client is None and not llm_available():
        return options
    topic = (getattr(options, "topic", "") or "").strip()
    biased_topic = f"{topic} {prompt}".strip() if topic else prompt
    return replace(options, topic=biased_topic)


def _snap_candidates(
    candidates: list[ClipCandidate],
    transcript: Transcript,
    total_duration: float,
) -> list[ClipCandidate]:
    """Snap each candidate to sentence boundaries and re-clamp to the clip.

    **Snapping may move a boundary; it may not annex a neighbouring moment.** A snap is
    skipped when the snapped window would swallow another candidate's midpoint.

    ``snap_to_sentences`` moves the start to the nearest segment start and the end to the
    nearest segment end, which on a coarsely-segmented transcript means *any* window inside
    one long segment becomes that whole segment. With a single 0-4 s segment covering two
    2-second candidates, both snapped to 0-4: the same window twice, and the pipeline shipped
    two byte-identical clips. Found while adding S15 de-duplication, which spotted the
    collision and dropped one - revealing that the duplicate had been shipping all along.

    Keeping the unsnapped window is the right repair rather than dropping the clip. The user
    asked for N moments and gets N distinct ones; the cost is one boundary not sitting on a
    sentence edge, which is all "snap where possible" ever promised. Dropping instead would
    silently return fewer clips than were requested.
    """
    segments = transcript.segments
    out: list[ClipCandidate] = []
    midpoints = [((c.start + c.end) / 2.0) for c in candidates]
    for index, c in enumerate(candidates):
        s, e = sel.snap_to_sentences(c.start, c.end, segments)
        s = max(0.0, s)
        if total_duration:
            e = min(total_duration, e)
        if e <= s:  # snapping collapsed the range -> keep the merged window
            s, e = c.start, c.end
        annexes = any(
            s <= mid <= e
            for other, mid in enumerate(midpoints)
            if other != index and not (c.start <= mid <= c.end)
        )
        if annexes:
            s, e = c.start, c.end
        out.append(
            ClipCandidate(
                start=round(s, 2),
                end=round(e, 2),
                score=c.score,
                reason=c.reason,
                title=c.title,
                text=c.text,
                features=dict(c.features),  # same silent-drop bug as merge_scores
            )
        )
    return out


def select_moments_visual(
    transcript: Transcript,
    options,
    source_path,
    total_duration: float,
    *,
    client: BaseLLMClient | None = None,
    sampler: Sampler | None = None,
) -> list[ClipCandidate]:
    """Feature C entry point — visual/prompt-aware clip selection.

    * ``visual_selection`` disabled -> delegate straight to
      :func:`worker.selection.select_moments` (identical behaviour, Req 15.4).
    * Otherwise, transcript candidates are produced first (prompt-biased when an
      LLM is available, Req 13.2), then augmented with visual cues sampled once
      per source (Reqs 14.1, 14.2, 17.4). Candidates are snapped to sentence
      boundaries (Req 14.5) and the ``num_clips`` cap is honoured (Req 13.4).
    * Keyframe sampling failure, an unconfigured provider, or a prompt with no
      LLM all degrade to transcript-only selection (Reqs 15.2, 15.3, 13.3).
    * A catastrophic failure returns ``[]`` (Req 15.5).

    The result always keeps the ``ClipCandidate`` shape so downstream pipeline
    stages are unchanged (Req 14.4).
    """
    # Pass-through when disabled (Req 15.4).
    if not getattr(options, "visual_selection", False):
        return sel.select_moments(transcript, options, source_path, total_duration, client=client)

    # Transcript-based candidates (already fallback-safe inside select_moments).
    try:
        selection_options = _bias_options(options, client)
        transcript_candidates = sel.select_moments(
            transcript, selection_options, source_path, total_duration, client=client
        )
    except Exception:
        # Catastrophic: even transcript-only selection failed (Req 15.5).
        return []

    # Visual augmentation. Any failure degrades to transcript-only.
    #
    # The sampled JPEGs live in a managed temporary directory that is removed as soon as
    # the cues have been derived from them. Previously `sample_keyframes` created the
    # directory with `mkdtemp` and nothing ever deleted it, so every visual-selection run
    # left a `kf-*` directory of thumbnails in the system temp space — unbounded growth on
    # a long-running instance, and nothing in the retention sweeper's remit to clean.
    # Cleanup has to sit here rather than inside `sample_keyframes`, because the Keyframe
    # paths must still be readable while `derive_visual_cues` reads their brightness.
    try:
        limit = int(getattr(settings, "keyframe_sample_limit", 12))
        width = int(getattr(settings, "keyframe_sample_width", 480) or 480)
        # I3: reuse this source's already-extracted frames when they are still on disk.
        #
        # Keyframes are files, so they are cached as files in a content-addressed directory rather
        # than serialised. `sample_keyframes` skips a frame whose file already exists, so a second
        # run over the same source performs no seeks at all - 48 decodes at 480 px saved.
        #
        # The directory is *not* a TemporaryDirectory in this path: deleting it is what would make
        # the cache useless. `intermediate_cache.prune` bounds it instead, which is the same trade
        # the transcript cache makes.
        cached_dir = intermediate_cache.frames_dir_for(
            source_path, {"limit": limit, "width": width}
        )
        if cached_dir is not None:
            frames = derive_visual_cues(
                sample_keyframes(
                    source_path,
                    total_duration,
                    limit=limit,
                    sampler=sampler,
                    frames_dir=str(cached_dir),
                )
            )
        else:
            with tempfile.TemporaryDirectory(prefix="kf-") as frames_dir:
                frames = derive_visual_cues(
                    sample_keyframes(
                        source_path,
                        total_duration,
                        limit=limit,
                        sampler=sampler,
                        frames_dir=frames_dir,
                    )
                )
    except Exception:
        frames = []

    if not frames:
        # No visual signal (sampling failed / provider unconfigured / no audio
        # with nothing to sample) -> transcript-only (Reqs 15.2, 15.3).
        return transcript_candidates

    try:
        merged = merge_scores(transcript_candidates, frames)
        snapped = _snap_candidates(merged, transcript, total_duration)
        snapped.sort(key=lambda c: c.score, reverse=True)
        max_clips = seg.resolve_max_clips(getattr(options, "num_clips", "auto"))
        # S15 again here, not only in select_moments: snapping to sentence boundaries can *make*
        # two candidates overlap that did not before, by pulling both onto the same segment
        # edges. De-duplicating upstream only would leave that case shipping two files for one
        # moment, and this is the default path now that visual selection is on by default.
        return candidate_ranking.deduplicate(snapped, limit=max_clips)
    except Exception:
        # Ranking/snapping failed unexpectedly -> transcript-only (Req 15.5).
        return transcript_candidates
