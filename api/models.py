"""The request bodies the API accepts.

Separated from the routers because several are shared — ``OptionsModel`` is read by both the
upload route and the watch-folder options route — and because a request model is part of the
API's contract rather than part of any one endpoint's implementation.
"""

from __future__ import annotations

from pydantic import BaseModel

from worker.models import ProcessingOptions


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------
class OptionsModel(BaseModel):
    """Processing options accepted from the UI (all optional, sane defaults)."""

    language: str | None = None
    translate: bool = False
    clip_length: str = "auto"
    aspect: str = "9:16"
    num_clips: str = "auto"
    strategy: str = "ai"
    captions: bool = True
    # Phase 2 — Advanced settings
    topic: str = ""
    # T4: per-video names/jargon fed to the ASR decode as a prompt.
    vocabulary: str = ""
    vibe: str = ""
    platform: str = "generic"
    hashtag_count: int = 5
    range_start: float | None = None
    range_end: float | None = None
    metadata: bool = True
    # Phase 3 — publishing
    publish_to: list[str] = []
    campaign_id: str = ""
    publish_mode: str = "review"
    schedule_at: float | None = None
    # Phase 4 — visual effects (all individually toggleable)
    reframe: bool = False
    zoom: bool = False
    transitions: bool = False
    hook_title: bool = False
    music: str = ""
    music_volume: float = 0.12
    fades: bool = False
    color: str = ""
    progress_bar: bool = False
    emoji: str = "off"
    emoji_mode: str = "keyword"
    emoji_animate: bool = True
    filler_removal: bool = False
    caption_template: str = "karaoke"
    caption_position: str = "bottom"
    # Tier 1 — Feature A: animated caption presets
    caption_preset: str = "karaoke"
    caption_animation: str = ""
    caption_keyword_highlight: bool = False
    caption_keyword_ai: bool = False
    caption_emoji: bool = False
    # Tier 1 — Feature B: b-roll overlays
    broll: bool = False
    broll_intensity: str = "standard"
    asset_sourcing_mode: str = "off"
    broll_provider: str = ""
    # Tier 1 — Feature C: prompt / visual selection
    selection_prompt: str = ""
    visual_selection: bool = False
    # Tier 1 — cross-cutting
    permissibility_mode: bool = False
    # v0.8.0 — Speaker diarisation & multi-speaker reframe (default OFF)
    diarization: bool = False
    speaker_reframe: bool = False
    reframe_layout: str = "follow_active"
    reframe_intensity: str = "standard"
    # Kinetic typography engine (default OFF). Same fields and same defaults as
    # ``ProcessingOptions``; unrecognised *choice* values are not rejected here
    # but coerced by the engine's ``resolve_options`` (Reqs 17.4, 17.7).
    kinetic_typography_enabled: bool = False
    kinetic_style: str = "karaoke_fill"
    kinetic_reveal: str = "cumulative"
    kinetic_font: str = ""
    kinetic_max_lines: int = 2
    kinetic_max_line_width: int = 22
    kinetic_safe_area_x_pct: float = 6.0
    kinetic_safe_area_y_pct: float = 10.0
    kinetic_motion_ms: int = 120
    kinetic_confidence_floor: float = 0.0
    # Stem inpainting engine (default OFF). Same defaults as ``ProcessingOptions`` /
    # ``Stem_Options``; unrecognised *choice* values are coerced by the engine's
    # ``resolve_options`` rather than rejected here (Reqs 18.1, 18.5).
    stem_inpainting_enabled: bool = False
    stem_mix_preset: str = "custom"
    stem_gain_vocals: float = 1.0
    stem_gain_music: float = 1.0
    stem_gain_other: float = 1.0
    stem_repair_mode: str = "crossfade"
    stem_repair_window_ms: int = 12
    stem_declick: bool = False
    stem_backend: str = "auto"
    stem_model: str = "htdemucs"
    stem_retain_stems: bool = False

    def to_options(self) -> ProcessingOptions:
        return ProcessingOptions.from_dict(self.model_dump())


class ClipEditModel(BaseModel):
    """Editable clip metadata fields (all optional; only provided ones apply)."""

    title: str | None = None
    title_alternatives: list[str] | None = None
    description: str | None = None
    hashtags: list[str] | None = None
    hook_text: str | None = None
    cta: str | None = None
    mentions: list[str] | None = None
    thumbnail_text: str | None = None


class RegenerateRequest(BaseModel):
    """Request to regenerate a single metadata field for a clip."""

    field: str
    platform: str | None = None


class CaptionPreviewModel(BaseModel):
    """Request a rendered caption sample for a preset (C18)."""

    preset: str = "karaoke"
    text: str = ""
    aspect: str = "9:16"
    position: str = ""
    #: Preset fields to override before rendering, so a panel can preview an edited style.
    overrides: dict = {}


class RerenderRequest(BaseModel):
    """Re-render one clip, optionally with changed settings (U7).

    ``settings`` is a partial options blob; unknown keys are ignored, so a UI can send its whole
    settings object without knowing which fields this build understands.
    """

    settings: dict = {}


class ClipReviewModel(BaseModel):
    """Set the review state of one clip (U9)."""

    review_state: str
    review_note: str = ""


class BatchReviewModel(BaseModel):
    """Set the review state of many clips at once (U9).

    ``clip_ids`` is scoped to one job, which is how review actually happens - you work through
    the clips a job produced. A cross-job version would need per-job permission checks that do
    not exist yet in a single-tenant product.
    """

    clip_ids: list[str]
    review_state: str
    review_note: str = ""


class PublishClipRequest(BaseModel):
    platforms: list[str] = []
    campaign_id: str = ""
    mode: str = "auto"
    schedule_at: float | None = None
    routes: dict[str, dict[str, str]] = {}


class RescheduleModel(BaseModel):
    """A new time for a pending publish attempt (PB7)."""

    schedule_at: float


class CampaignModel(BaseModel):
    name: str
    routes: dict[str, dict[str, str]]
    id: str = ""


class StorageSettingsModel(BaseModel):
    """User-tunable storage settings (runtime-persisted)."""

    retention_days: int | None = None
    auto_delete_temp: bool | None = None
    delete_local_after_publish: bool | None = None


class ProfileModel(BaseModel):
    """Create/update a saved settings profile."""

    name: str
    settings: dict = {}
    publishing: dict = {}
    id: str = ""
    make_default: bool = False


class UrlJobRequest(BaseModel):
    url: str
    options: OptionsModel = OptionsModel()


class BatchRequest(BaseModel):
    urls: list[str]
    options: OptionsModel = OptionsModel()


class PreviewRequest(BaseModel):
    url: str


class WatchToggleRequest(BaseModel):
    enabled: bool
    options: OptionsModel = OptionsModel()
