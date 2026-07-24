"""Shared data models for the processing pipeline and job manager.

Kept in a dependency-free module so both ``worker.pipeline`` and ``worker.jobs``
can import them without creating an import cycle.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class JobStatus(str, Enum):
    """Lifecycle states for a processing job."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ProcessingOptions:
    """User-selected processing options (mirrors the UI settings panel)."""

    language: Optional[str] = None       # None = auto-detect
    translate: bool = False              # translate speech to English
    clip_length: str = "auto"            # auto | <30s | 30-60s | 60-90s | 90s-3min
    aspect: str = "9:16"                 # 9:16 | 1:1 | 16:9 | 4:5
    num_clips: str = "auto"              # auto | 1 | 3 | 5 | 10 | max
    strategy: str = "ai"                 # ai | silence | fixed
    captions: bool = True                # burn captions

    # --- Phase 2: smart selection & metadata (Advanced settings) ----------
    topic: str = ""                      # Clip Topic / Keywords to bias toward
    vibe: str = ""                       # Vibe / Tone (e.g. "energetic", "educational")
    platform: str = "generic"            # target platform for metadata tone/limits
    hashtag_count: int = 5               # number of hashtags to generate
    range_start: Optional[float] = None  # only process from this second...
    range_end: Optional[float] = None    # ...to this second (Process Range)
    metadata: bool = True                # generate AI metadata per clip

    # --- Phase 3: publishing ---------------------------------------------
    publish_to: list[str] = field(default_factory=list)
    campaign_id: str = ""
    publish_mode: str = "review"         # auto | review
    schedule_at: Optional[float] = None  # UTC epoch; None = now

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProcessingOptions":
        """Build options from a (possibly partial) dict, ignoring unknown keys."""
        data = data or {}
        valid = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        # Normalise an empty-string language to None (auto).
        if valid.get("language") in ("", "auto", "Auto"):
            valid["language"] = None
        # Coerce numeric fields that may arrive as strings from form data.
        for num_field in ("range_start", "range_end", "schedule_at"):
            v = valid.get(num_field)
            if v in ("", None):
                valid[num_field] = None
            else:
                try:
                    valid[num_field] = float(v)
                except (TypeError, ValueError):
                    valid[num_field] = None
        if "publish_to" in valid and isinstance(valid["publish_to"], str):
            valid["publish_to"] = [p.strip() for p in valid["publish_to"].split(",") if p.strip()]
        if "hashtag_count" in valid:
            try:
                valid["hashtag_count"] = max(0, min(30, int(valid["hashtag_count"])))
            except (TypeError, ValueError):
                valid["hashtag_count"] = 5
        return cls(**valid)


@dataclass
class ClipResult:
    """A single finished clip produced by the pipeline.

    Carries both the media locations and the AI-generated, user-editable
    metadata (title + alternatives, description, hashtags, on-screen hook,
    CTA/mentions, thumbnail text) plus the virality score and selection reason.
    """

    id: str
    filename: str
    start: float
    end: float
    duration: float
    title: str = ""
    video_url: str = ""
    thumbnail_url: str = ""

    # --- Phase 2: smart selection + metadata ------------------------------
    score: float = 0.0                              # virality score 0..100
    reason: str = ""                                # why this moment was picked
    platform: str = "generic"                       # platform the metadata targets
    title_alternatives: list[str] = field(default_factory=list)
    description: str = ""                            # caption / description
    hashtags: list[str] = field(default_factory=list)
    hook_text: str = ""                             # on-screen opening hook
    cta: str = ""                                   # call to action
    mentions: list[str] = field(default_factory=list)  # @tags
    thumbnail_text: str = ""                        # thumbnail text idea
    transcript_text: str = ""                       # clip transcript (for regen)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Job:
    """A single video processing job, tracked live in the job store."""

    input_type: str                       # "url" | "file"
    source: str                           # URL or original filename
    options: ProcessingOptions
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    batch_id: Optional[str] = None
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0                 # 0..1
    stage: str = "Queued"
    title: str = ""
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    error: Optional[str] = None
    clips: list[ClipResult] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict for the API."""
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "input_type": self.input_type,
            "source": self.source,
            "status": self.status.value,
            "progress": round(self.progress, 3),
            "stage": self.stage,
            "title": self.title,
            "duration": self.duration,
            "thumbnail": self.thumbnail,
            "error": self.error,
            "clips": [c.to_dict() for c in self.clips],
            "options": asdict(self.options),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
