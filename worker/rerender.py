"""Re-render a single clip without re-running its whole job (U7).

Changing one setting - a caption preset, a colour grade, the aspect ratio - meant resubmitting
the source and paying for **everything** again: the download, the transcription, the LLM
selection call, the metadata generation, and the rendering of every *other* clip. For a
twenty-minute source producing ten clips, that is minutes of work to see a different caption
font on one of them, and it also produces a *different set of clips*, because selection is not
deterministic across runs with an LLM in it.

What this skips, precisely, so the claim can be checked:

* **download** - the resolved local path is recorded on the job (``Job.source_path``);
* **selection** - the clip's own window is passed to the pipeline as an explicit candidate, so
  no LLM selection call happens and the window cannot drift;
* **the other clips** - one candidate in, one clip out;
* **transcription** - not skipped as such, but T8 caches transcripts by source content and ASR
  settings, so the second run is a cache read rather than a Whisper pass.

What it does **not** skip: the cut, the geometry pass and the composite. Those are the stages
that actually apply the settings being changed, so re-running them is the point rather than a
cost. A finer-grained "only re-run the filters that changed" would need every intermediate
persisted per clip, which is a storage design decision rather than an optimisation.

The user's edited metadata is carried across. Re-rendering is a request about *pixels*, and
silently replacing a title someone wrote by hand with a freshly generated one would be a data
loss that looks like a feature.
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from config import settings
from worker import output_profiles
from worker.models import ClipResult, Job, ProcessingOptions
from worker.pipeline import run_pipeline
from worker.selection import ClipCandidate

logger = logging.getLogger(__name__)

#: Metadata fields preserved across a re-render.
#:
#: Everything a human may have edited, plus the selection provenance. ``score`` and ``reason``
#: are kept because they describe why the moment was *chosen*, which a re-render does not
#: revisit - recomputing them from a fresh scoring pass would make the number change for a
#: reason unrelated to anything the user asked for.
PRESERVED_FIELDS: tuple[str, ...] = (
    "title",
    "title_alternatives",
    "description",
    "hashtags",
    "hook_text",
    "cta",
    "mentions",
    "thumbnail_text",
    "transcript_text",
    "platform",
    "score",
    "reason",
    "review_state",
    "review_note",
    "needs_rerender",
    "brief_revision",
    "brief_content_hash",
    "meme_events",
    "meme_assets",
)


class RerenderError(RuntimeError):
    """Raised when a clip cannot be re-rendered. Carries a message fit for an API response."""


class VariantError(RuntimeError):
    """Raised when a requested delivery variant cannot be produced."""


def resolve_source(job: Job) -> Path:
    """The local file to re-read for ``job``, or raise :class:`RerenderError`.

    Prefers the recorded ``source_path``; falls back to ``source`` for a file job, which covers
    jobs created before ``source_path`` existed. A URL job with no recorded path cannot be
    re-rendered - the download is gone, and silently re-downloading would turn a "re-render one
    clip" request into the very whole-job run this exists to avoid.
    """
    candidates = [job.source_path, job.source if job.input_type == "file" else ""]
    for candidate in candidates:
        if candidate:
            path = Path(candidate)
            if path.is_file():
                return path
    raise RerenderError(
        "The original source file is no longer available, so this clip cannot be "
        "re-rendered. Re-submit the video to change its settings."
    )


def merge_options(base: ProcessingOptions, overrides: dict[str, Any] | None) -> ProcessingOptions:
    """``base`` with ``overrides`` applied, ignoring unknown keys.

    Unknown keys are dropped rather than raising: the caller is a UI sending a settings blob,
    and a field this build does not recognise should not fail the request. Values are *not*
    coerced here - ``ProcessingOptions.from_dict`` is the coercion path, and re-implementing it
    would give two answers to what a valid option is.
    """
    if not overrides:
        return base
    known = {k: v for k, v in overrides.items() if k in ProcessingOptions.__dataclass_fields__}
    if not known:
        return base
    merged = {**asdict(base), **known}
    return ProcessingOptions.from_dict(merged)


def rerender_clip(
    job: Job,
    clip: ClipResult,
    *,
    option_overrides: dict[str, Any] | None = None,
    cuts: list | None = None,
    clips_dir: Path | None = None,
    temp_dir: Path | None = None,
    progress_cb=None,
    output_filename: str | None = None,
) -> ClipResult:
    """Re-render ``clip`` in place and return the updated :class:`ClipResult`.

    The new media replaces the old file **only after** the render succeeds, so a failed
    re-render leaves the existing clip intact. That matters more than it sounds: the clip may
    already have been published, and the file is what a viewer's platform links to.

    ``cuts`` is U4's transcript-based trim: clip-relative ranges to remove, as
    ``(start, end)`` pairs. They are offsets into **this clip**, not into the source, which
    is what the transcript editor displays and therefore what it can produce without knowing
    where in the source the clip was found. Omitted or empty, the render is exactly what it
    was before the parameter existed.

    ``output_filename`` is used by platform variants. When set, the fresh render is written
    under that name instead of replacing the clip's canonical file, and the returned record
    points at the new file. The canonical clip is not touched.
    """
    source = resolve_source(job)
    options = merge_options(job.options, option_overrides)

    clips_dir = Path(clips_dir or (Path(settings.clips_dir) / job.id))
    target_name = Path(output_filename).name if output_filename else clip.filename
    if output_filename and target_name != output_filename:
        raise RerenderError("The variant filename must be a plain filename.")
    # Recorded so the `finally` below knows whether this call owns the scratch directory and may
    # therefore delete it. See the note there.
    temp_dir_was_derived = temp_dir is None
    temp_dir = Path(temp_dir or (Path(settings.temp_dir) / f"{job.id}_rerender"))

    # Rendered into a scratch directory first, so a failure cannot leave a half-written file
    # where the current clip is, and so the new clip's generated filename cannot collide with
    # the existing one.
    staging = clips_dir / f".rerender_{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=True)

    retention_plan = dict(getattr(clip, "retention_plan", {}) or {})
    features = dict(getattr(clip, "engagement_features", {}) or {})
    if not features and retention_plan:
        # Older persisted clips predate engagement_features. Preserve the
        # strongest durable evidence they do have instead of rerendering with
        # a completely neutral retention input.
        features = {
            "hook_score": retention_plan.get("hook_score", 0.5),
            "engagement_score": float(getattr(clip, "engagement_score", 0.0) or 0.0) / 100.0,
            "engagement_event_start": float(clip.start),
        }

    window = ClipCandidate(
        start=float(clip.start),
        end=float(clip.end),
        reason=clip.reason,
        score=clip.score,
        features=features,
        cuts=list(cuts or []),
    )
    try:
        profile_scope = (
            output_profiles.use_profile(options.output_platform)
            if options.output_platform
            else nullcontext()
        )
        with profile_scope:
            produced = run_pipeline(
                source,
                options,
                clips_dir=staging,
                temp_dir=temp_dir,
                progress_cb=progress_cb,
                explicit_candidates=[window],
            )
        if not produced:
            raise RerenderError("The re-render produced no clip.")
        fresh = produced[0]

        new_video = staging / fresh.filename
        if not new_video.is_file():
            raise RerenderError("The re-render did not write a clip file.")

        # Keep the clip's existing filename, so every URL, publish record and history row that
        # already points at it stays valid. A new name would orphan all of them. Variants are the
        # deliberate exception: they are new files with their own name and their own record.
        final_video = clips_dir / target_name
        shutil.move(str(new_video), str(final_video))

        thumbnail_name = Path(target_name).with_suffix(".jpg").name
        new_thumb = staging / Path(fresh.filename).with_suffix(".jpg").name
        if new_thumb.is_file():
            shutil.move(str(new_thumb), str(clips_dir / thumbnail_name))

        # Sidecars (O11) are named from the clip stem, so they follow the same rule.
        for extra in staging.glob(f"{Path(fresh.filename).stem}.*"):
            if extra.suffix in (".srt", ".vtt", ".json"):
                shutil.move(
                    str(extra),
                    str(clips_dir / f"{Path(target_name).stem}{extra.suffix}"),
                )

        if output_filename:
            thumb_exists = (clips_dir / thumbnail_name).is_file()
            video_url = f"clips/{clips_dir.name}/{target_name}"
            thumbnail_url = f"clips/{clips_dir.name}/{thumbnail_name}" if thumb_exists else ""
        else:
            video_url = clip.video_url
            thumbnail_url = clip.thumbnail_url

        updated = replace(
            fresh,
            id=clip.id,
            filename=target_name,
            video_url=video_url,
            thumbnail_url=thumbnail_url,
            **{name: getattr(clip, name) for name in PRESERVED_FIELDS},
        )
        return updated
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        # The scratch directory too, and only when this call created it.
        #
        # It was leaked on every re-render, permanently. `JobManager._cleanup_temp` is the usual
        # backstop, but it removes `storage/temp/<job_id>` and this path is
        # `storage/temp/<job_id>_rerender` — a name it can never match — and it is additionally
        # gated on the `auto_delete_temp` toggle. So the one operation the review UI actively
        # encourages repeating accumulated a full set of per-clip intermediates every time, in a
        # directory reused by name on the next re-render of the same job.
        #
        # Skipped when the caller supplied `temp_dir`: a caller that named the directory owns its
        # lifetime, and the evaluation harness passes one it inspects afterwards.
        if temp_dir_was_derived:
            shutil.rmtree(temp_dir, ignore_errors=True)


def variant_filename(original_filename: str, platform: str) -> str:
    """A stable, readable filename for one platform variant.

    Stable matters: re-rendering the same destination replaces its file instead of accumulating
    ``_2``, ``_3`` copies. The platform is reduced to a safe slug because it becomes part of a
    URL and a filename on disk.
    """
    stem = Path(str(original_filename or "clip")).stem or "clip"
    suffix = Path(str(original_filename or "clip.mp4")).suffix or ".mp4"
    slug = re.sub(r"[^a-z0-9]+", "_", str(platform or "variant").strip().lower()).strip("_")
    return f"{stem}_{slug or 'variant'}{suffix}"


def render_platform_variant(
    job: Job,
    clip: ClipResult,
    platform: str,
    *,
    option_overrides: dict[str, Any] | None = None,
    cuts: list | None = None,
    clips_dir: Path | None = None,
    temp_dir: Path | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    """Render one destination-specific file and return its delivery record.

    The platform profile supplies the aspect and short side rather than inheriting the canonical
    clip's geometry. That is the whole point: a 9:16 TikTok cut and a 16:9 YouTube upload are two
    deliverables of the same selected moment, not two different clips.

    The canonical clip and its metadata are untouched. If the destination already has a file, it
    is replaced under the same name so links to that variant do not accumulate dead copies.
    """
    profile = output_profiles.profile_for(platform)
    if profile is None:
        raise VariantError(f"Unknown platform variant: {platform!r}")

    overrides = dict(option_overrides or {})
    overrides.update(
        {
            "aspect": profile.aspect,
            "output_platform": profile.name,
            "output_short_side": profile.short_side,
        }
    )
    filename = variant_filename(clip.filename, profile.name)
    rendered = rerender_clip(
        job,
        clip,
        option_overrides=overrides,
        cuts=cuts,
        clips_dir=clips_dir,
        temp_dir=temp_dir,
        progress_cb=progress_cb,
        output_filename=filename,
    )
    width, height = profile.size
    return {
        "platform": profile.name,
        "label": output_profiles.label_for(profile.name),
        "filename": rendered.filename,
        "video_url": rendered.video_url,
        "thumbnail_url": rendered.thumbnail_url,
        "aspect": profile.aspect,
        "width": width,
        "height": height,
        "short_side": profile.short_side,
        "max_duration_s": profile.max_duration_s,
        "duration": rendered.duration,
        "start": rendered.start,
        "end": rendered.end,
    }
