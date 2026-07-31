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
import shutil
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Optional

from config import settings
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
)


class RerenderError(RuntimeError):
    """Raised when a clip cannot be re-rendered. Carries a message fit for an API response."""


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


def merge_options(base: ProcessingOptions, overrides: Optional[dict[str, Any]]) -> ProcessingOptions:
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
    option_overrides: Optional[dict[str, Any]] = None,
    cuts: Optional[list] = None,
    clips_dir: Optional[Path] = None,
    temp_dir: Optional[Path] = None,
    progress_cb=None,
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
    """
    source = resolve_source(job)
    options = merge_options(job.options, option_overrides)

    clips_dir = Path(clips_dir or (Path(settings.clips_dir) / job.id))
    temp_dir = Path(temp_dir or (Path(settings.temp_dir) / f"{job.id}_rerender"))

    # Rendered into a scratch directory first, so a failure cannot leave a half-written file
    # where the current clip is, and so the new clip's generated filename cannot collide with
    # the existing one.
    staging = clips_dir / f".rerender_{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=True)

    window = ClipCandidate(
        start=float(clip.start),
        end=float(clip.end),
        reason=clip.reason,
        cuts=list(cuts or []),
    )
    try:
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
        # already points at it stays valid. A new name would orphan all of them.
        final_video = clips_dir / clip.filename
        shutil.move(str(new_video), str(final_video))

        new_thumb = staging / Path(fresh.filename).with_suffix(".jpg").name
        if new_thumb.is_file():
            shutil.move(str(new_thumb), str(clips_dir / Path(clip.filename).with_suffix(".jpg").name))

        # Sidecars (O11) are named from the clip stem, so they follow the same rule.
        for extra in staging.glob(f"{Path(fresh.filename).stem}.*"):
            if extra.suffix in (".srt", ".vtt", ".json"):
                shutil.move(
                    str(extra),
                    str(clips_dir / f"{Path(clip.filename).stem}{extra.suffix}"),
                )

        updated = replace(
            fresh,
            id=clip.id,
            filename=clip.filename,
            video_url=clip.video_url,
            thumbnail_url=clip.thumbnail_url,
            **{name: getattr(clip, name) for name in PRESERVED_FIELDS},
        )
        return updated
    finally:
        shutil.rmtree(staging, ignore_errors=True)
