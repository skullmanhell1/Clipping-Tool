"""Shared data models for the processing pipeline and job manager.

Kept in a dependency-free module so both ``worker.pipeline`` and ``worker.jobs``
can import them without creating an import cycle.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Optional


def _as_bool(value: Any) -> bool:
    """Coerce form/JSON values (``"true"``, ``"1"``, ``True`` ...) to ``bool``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


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

    # --- Phase 4: visual effects (all individually toggleable) -----------
    reframe: bool = False                # face-tracking auto-reframe (vs static crop)
    zoom: bool = False                   # slow Ken-Burns zoom
    transitions: bool = False            # subtle punch-in intro
    hook_title: bool = False             # burn the AI hook text at the start
    music: str = ""                      # mood: "" (off) | upbeat | chill | dramatic | corporate | suspense
    music_volume: float = 0.12           # background-music level (0..1)
    fades: bool = False                  # fade in/out (video + audio)
    color: str = ""                      # "" (off) | vivid | warm | cool | cinematic | bw
    progress_bar: bool = False           # growing progress bar along the bottom
    emoji: str = "off"                   # off | subtle | standard | heavy
    emoji_mode: str = "keyword"          # keyword | ai
    emoji_animate: bool = True           # pop/scale (alpha) animation on appear
    filler_removal: bool = False         # cut "um"/"uh"/long pauses
    caption_template: str = "karaoke"    # karaoke | boxed | minimal
    caption_position: str = "bottom"     # bottom | center | top

    # --- Phase 6 / Tier 1: Creator Output Upgrade ------------------------
    # All new visual/audio/rights features default OFF so an "all-off" run
    # reproduces v0.6.0 behaviour. Existing fields/defaults above are unchanged.
    #
    # Feature A — animated caption presets
    caption_preset: str = "karaoke"      # karaoke|boxed|minimal|pop|typewriter|hormozi
    caption_animation: str = ""          # "" = use preset default; else override
    caption_keyword_highlight: bool = False  # highlight important words
    caption_keyword_ai: bool = False     # AI-assisted keyword highlighting
    caption_emoji: bool = False          # in-caption emoji glyphs
    #
    # Feature B — b-roll overlays
    broll: bool = False                  # enable b-roll auto-insertion
    broll_intensity: str = "standard"    # off|subtle|standard|heavy
    asset_sourcing_mode: str = "off"     # off|local_only|local_then_external
    broll_provider: str = ""             # external provider name ("" = none)
    #
    # Feature C — prompt / visual selection
    selection_prompt: str = ""           # free-text selection prompt
    visual_selection: bool = False       # enable visual/keyframe-aided selection
    #
    # Cross-cutting
    permissibility_mode: bool = False    # forces local_only sourcing + no added audio

    # --- v0.8.0: Speaker diarisation & multi-speaker reframe (default OFF) --
    # All new fields default OFF/standard so an "all-off" run reproduces
    # v0.7.0 behaviour exactly. Existing fields/defaults above are unchanged.
    diarization: bool = False              # persisted diarisation toggle
    speaker_reframe: bool = False          # speaker-aware reframe toggle
    reframe_layout: str = "follow_active"  # follow_active | split_screen
    reframe_intensity: str = "standard"    # subtle | standard | heavy

    # --- Kinetic typography engine (default OFF) --------------------------
    # ``kinetic_typography_enabled`` is the Feature_Flag the engine's inherited
    # ``flag_field()`` resolves to, so an all-off run still reproduces v0.8.0
    # exactly. The flat ``kinetic_*`` fields mirror ``Kinetic_Options`` and are
    # JSON scalars, so ``from_dict`` / ``dataclasses.asdict`` round-trip them
    # losslessly. Values are *not* validated here: unknown/malformed values are
    # coerced (and reported as substitutions) by the engine's ``resolve_options``,
    # which is also what keeps this module free of a ``worker.engines`` import.
    kinetic_typography_enabled: bool = False   # Feature_Flag (flag_field())
    kinetic_style: str = "karaoke_fill"        # bounce|highlight_sweep|karaoke_fill|
                                               # none|pop|slide_up|typewriter
    kinetic_reveal: str = "cumulative"         # cumulative | word_by_word
    kinetic_font: str = ""                     # "" = inherit the caption preset font
    kinetic_max_lines: int = 2                 # 1..4 text lines per cue
    kinetic_max_line_width: int = 22           # 6..80 display-width units per line
    kinetic_safe_area_x_pct: float = 6.0       # 0..25 % horizontal safe-area inset
    kinetic_safe_area_y_pct: float = 10.0      # 0..40 % vertical safe-area inset
    kinetic_motion_ms: int = 120               # 20..1000 ms per-word motion duration
    kinetic_confidence_floor: float = 0.0      # 0..1 word-confidence emphasis floor

    # --- Stem inpainting engine (default OFF) -----------------------------
    # Same arrangement as the kinetic block above, for the same reasons:
    # ``stem_inpainting_enabled`` is the Feature_Flag the engine's inherited
    # ``flag_field()`` resolves to, and the ten flat ``stem_*`` fields mirror
    # ``Stem_Options`` one-for-one so ``from_dict`` / ``dataclasses.asdict``
    # round-trip them losslessly. Values are deliberately *not* validated here —
    # the engine's ``resolve_options`` coerces every one of them against its
    # documented bounds, which is what keeps this module free of a
    # ``worker.engines`` import and keeps an unrecognised value from failing a job.
    stem_inpainting_enabled: bool = False      # Feature_Flag (flag_field())
    stem_mix_preset: str = "custom"            # custom|speech_focus|music_focus|
                                               # clean_speech
    stem_gain_vocals: float = 1.0              # 0.0..4.0 (0.0 mutes, >1.0 boosts)
    stem_gain_music: float = 1.0               # 0.0..4.0
    stem_gain_other: float = 1.0               # 0.0..4.0
    stem_repair_mode: str = "crossfade"        # off | crossfade | spectral
    stem_repair_window_ms: int = 12            # 2..120 ms symmetric seam window
    stem_declick: bool = False                 # 1 ms fade at clip head/tail
    stem_backend: str = "auto"                 # auto | ml | ffmpeg
    stem_model: str = "htdemucs"               # separation checkpoint name
    stem_retain_stems: bool = False            # keep per-stem WAVs as durable artifacts

    # Known value sets for enum-like string fields (used by ``from_dict``).
    _CAPTION_PRESETS = ("karaoke", "boxed", "minimal", "pop", "typewriter", "hormozi")
    _CAPTION_ANIMATIONS = ("", "none", "pop", "typewriter", "karaoke_fill")
    _BROLL_INTENSITIES = ("off", "subtle", "standard", "heavy")
    _ASSET_SOURCING_MODES = ("off", "local_only", "local_then_external")
    _REFRAME_LAYOUTS = ("follow_active", "split_screen")
    _REFRAME_INTENSITIES = ("subtle", "standard", "heavy")

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
        # Coerce boolean-ish effect flags that may arrive as strings.
        for bool_field in ("reframe", "zoom", "transitions", "hook_title", "fades",
                           "progress_bar", "emoji_animate", "filler_removal",
                           # Phase 6 / Tier 1 boolean flags
                           "caption_keyword_highlight", "caption_keyword_ai",
                           "caption_emoji", "broll", "visual_selection",
                           "permissibility_mode",
                           # v0.8.0 boolean flags
                           "diarization", "speaker_reframe",
                           # Kinetic typography Feature_Flag: normalised here so
                           # a string payload ("false", "0") cannot read as
                           # enabled, and so the flag survives both
                           # ``from_dict`` and ``effective_options`` as a real
                           # ``bool`` (Reqs 10.10, 17.1, 17.8)
                           "kinetic_typography_enabled",
                           # The stem engine's Feature_Flag and its two booleans, for the
                           # same reason: the host reads the flag through ``coerce_bool``,
                           # but ``effective_options`` and the parity gate compare options
                           # by value, so ``"false"`` from a form field must become ``False``
                           # here rather than a truthy string.
                           "stem_inpainting_enabled",
                           "stem_declick",
                           "stem_retain_stems"):
            if bool_field in valid:
                valid[bool_field] = _as_bool(valid[bool_field])
        if "music_volume" in valid:
            try:
                valid["music_volume"] = max(0.0, min(1.0, float(valid["music_volume"])))
            except (TypeError, ValueError):
                valid["music_volume"] = 0.12
        # Validate Phase 6 / Tier 1 enum-like string fields against their known
        # value sets, falling back to the documented default on unknown or
        # malformed values (never raising).
        for enum_field, known, default in (
            ("caption_preset", cls._CAPTION_PRESETS, "karaoke"),
            ("caption_animation", cls._CAPTION_ANIMATIONS, ""),
            ("broll_intensity", cls._BROLL_INTENSITIES, "standard"),
            ("asset_sourcing_mode", cls._ASSET_SOURCING_MODES, "off"),
            ("reframe_layout", cls._REFRAME_LAYOUTS, "follow_active"),
            ("reframe_intensity", cls._REFRAME_INTENSITIES, "standard"),
        ):
            if enum_field in valid:
                v = valid[enum_field]
                valid[enum_field] = v if v in known else default
        return cls(**valid)


def _external_key_configured() -> bool:
    """Return True when an external b-roll provider API key is configured.

    Reads ``settings.broll_provider_api_key`` defensively: the field is added to
    ``config.py`` in a later task, so a missing import/attribute is treated as
    "no key configured" rather than an error. Never raises.
    """
    try:
        from config import settings  # local import to avoid import cycles
        key = getattr(settings, "broll_provider_api_key", None)
        return bool(key)
    except Exception:
        return False


def effective_options(o: "ProcessingOptions") -> "ProcessingOptions":
    """Return a normalised copy of ``o`` enforcing cross-cutting rules.

    Pure: never mutates the input and always returns a new instance; never
    raises. Applied once, centrally, before processing.

    - Under ``permissibility_mode``: disable added audio (``music=""``) and force
      ``asset_sourcing_mode="local_only"`` (Reqs 8.6, 19.1, 19.3).
    - Downgrade ``asset_sourcing_mode`` from ``local_then_external`` to
      ``local_only`` when no external provider key is configured (Req 8.4).
    """
    result = o
    if o.permissibility_mode:
        result = replace(result, music="", asset_sourcing_mode="local_only")
    if result.asset_sourcing_mode == "local_then_external" and not _external_key_configured():
        result = replace(result, asset_sourcing_mode="local_only")
    return result


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

    # --- Phase 4: which visual effects were applied to this clip ----------
    # ``effects_applied`` holds free-form string markers describing which
    # optional enhancements ran (and how they degraded). In addition to the
    # legacy Phase 4 markers, the Tier 1 Creator Output Upgrade introduces the
    # following markers (produced by later tasks — defined here only):
    #   - ``caption_preset:<name>``       an animated caption preset was applied
    #   - ``caption_preset_substituted``  requested preset was unknown/malformed
    #                                     and karaoke was substituted
    #   - ``font_substituted:<name>``     preset font missing; ``<name>`` used
    #   - ``keyword_highlight``           keyword highlighting was applied
    #   - ``caption_emoji``               in-caption emoji glyphs were rendered
    #   - ``broll:<keyword>``             a b-roll cue for ``<keyword>`` composited
    #   - ``broll_source:local_only``     b-roll sourced from the local library only
    #   - ``broll_asset_failed``          a b-roll asset could not be resolved/decoded
    #   - ``broll_license_unknown``       a b-roll asset was dropped (unknown license)
    #   - ``broll_degraded``              b-roll disabled after a build/compose error
    #   - ``visual_selection``            visual/keyframe-aided selection was used
    #   - ``visual_degraded``             visual selection fell back to transcript-only
    #
    # v0.8.0 Speaker Diarisation & Multi-Speaker Reframe introduces the
    # following markers (produced by later tasks — defined here only):
    #   - ``diarization:transcript``         turns from offline Word_Timeline
    #                                        segmentation (no backend / permissibility)
    #   - ``diarization:model``              turns from an injected diarisation backend
    #   - ``diarization_degraded``           backend errored; offline fallback used
    #   - ``speaker_reframe:follow_active``  follow-active speaker reframe applied
    #   - ``speaker_reframe:split_screen``   split-screen speaker reframe applied
    #   - ``speaker_reframe_substituted``    requested layout substituted (unknown →
    #                                        follow_active, or split_screen →
    #                                        follow_active with < 2 tracks)
    #   - ``faces_none``                     zero face tracks detected
    #   - ``speaker_reframe_degraded``       speaker-aware geometry unusable/failed;
    #                                        fell back along the chain
    effects_applied: list[str] = field(default_factory=list)

    # --- Tier 1: provenance for composited b-roll assets ------------------
    # Populated only for assets actually composited into this clip. Each entry
    # has the shape ``{provider, source_id, license, attribution, keyword,
    # path}`` (external assets record provider/source_id/license/attribution;
    # local assets record their source ``path``). Serialised automatically by
    # ``to_dict`` via ``asdict``.
    broll_assets: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ClipResult":
        """Rebuild a clip from its ``to_dict`` form, ignoring unknown keys.

        Unknown keys are dropped rather than raising so a record written by an older
        or newer build still loads — a persisted job outliving a deploy is the normal
        case, not an exceptional one. Missing required keys fall back to a benign
        default for the same reason.
        """
        data = data or {}
        valid = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        valid.setdefault("id", "")
        valid.setdefault("filename", "")
        for number in ("start", "end", "duration"):
            try:
                valid[number] = float(valid.get(number) or 0.0)
            except (TypeError, ValueError):
                valid[number] = 0.0
        return cls(**valid)


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

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Job":
        """Rebuild a job from its :meth:`to_dict` form.

        The inverse of ``to_dict``, used to restore jobs from the durable store on
        start-up. Nested ``options`` and ``clips`` are rebuilt through their own
        ``from_dict``, and an unrecognised ``status`` degrades to ``FAILED`` rather than
        raising: a job whose state cannot be read is certainly not still running, and
        surfacing it as failed is both true and actionable.
        """
        data = data or {}
        try:
            status = JobStatus(str(data.get("status") or JobStatus.FAILED.value))
        except ValueError:
            status = JobStatus.FAILED
        return cls(
            input_type=str(data.get("input_type") or "file"),
            source=str(data.get("source") or ""),
            options=ProcessingOptions.from_dict(data.get("options")),
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            batch_id=data.get("batch_id"),
            status=status,
            progress=float(data.get("progress") or 0.0),
            stage=str(data.get("stage") or ""),
            title=str(data.get("title") or ""),
            duration=data.get("duration"),
            thumbnail=data.get("thumbnail"),
            error=data.get("error"),
            clips=[ClipResult.from_dict(c) for c in (data.get("clips") or [])],
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )
