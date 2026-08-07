"""Shared data models for the processing pipeline and job manager.

Kept in a dependency-free module so both ``worker.pipeline`` and ``worker.jobs``
can import them without creating an import cycle.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any


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
    # I4: distinct from FAILED on purpose - a job the user stopped did not go
    # wrong, and collapsing the two would both mislead the operator and inflate
    # any failure rate computed from these records.
    CANCELLED = "cancelled"


@dataclass
class ProcessingOptions:
    """User-selected processing options (mirrors the UI settings panel)."""

    language: str | None = None  # None = auto-detect
    translate: bool = False  # translate speech to English
    # T4: names, jargon and brands for *this* video, prepended to the ASR decode so Whisper
    # has a reason to expect them. A recurring proper noun is otherwise mis-transcribed the
    # same way every time it is said, and that mistake is burned into every clip's captions.
    # Free text rather than a list: it is fed to the model as a prompt, and a comma-separated
    # phrase reads as naturally to it as a sentence does.
    vocabulary: str = ""
    clip_length: str = "auto"  # auto | <30s | 30-60s | 60-90s | 90s-3min
    aspect: str = "9:16"  # 9:16 | 1:1 | 16:9 | 4:5
    num_clips: str = "auto"  # auto | 1 | 3 | 5 | 10 | max
    strategy: str = "ai"  # ai | silence | fixed
    captions: bool = True  # burn captions
    # O11: also write .srt/.vtt beside the clip. The burn-in is unchanged; this is
    # for platforms that accept uploaded captions, and for anyone who needs the
    # text rather than an image of it.
    subtitle_sidecar: bool = False

    # --- Phase 2: smart selection & metadata (Advanced settings) ----------
    topic: str = ""  # Clip Topic / Keywords to bias toward
    vibe: str = ""  # Vibe / Tone (e.g. "energetic", "educational")
    platform: str = "generic"  # target platform for metadata tone/limits
    hashtag_count: int = 5  # number of hashtags to generate
    range_start: float | None = None  # only process from this second...
    range_end: float | None = None  # ...to this second (Process Range)
    metadata: bool = True  # generate AI metadata per clip

    # --- Phase 3: publishing ---------------------------------------------
    publish_to: list[str] = field(default_factory=list)
    campaign_id: str = ""
    publish_mode: str = "review"  # auto | review
    schedule_at: float | None = None  # UTC epoch; None = now

    # --- Phase 4: visual effects (all individually toggleable) -----------
    #
    # U1: these default ON. A default run used to enable only captions, 9:16, the ``ai``
    # strategy and metadata, which meant the out-of-the-box output was a static centre crop
    # with plain captions - the tool shipped looking worse than it is capable of, and every
    # feature that makes short-form video look modern had to be discovered one checkbox at
    # a time.
    #
    # Three of the thirteen features the improvement plan lists are deliberately still off,
    # each because turning it on today would make output *worse* rather than better; see the
    # comments at ``music``, ``broll`` and ``kinetic_typography_enabled``.
    reframe: bool = True  # face-tracking auto-reframe (vs static crop)
    zoom: bool = True  # slow Ken-Burns zoom
    transitions: bool = True  # subtle punch-in intro
    hook_title: bool = True  # burn the AI hook text at the start
    # Still off (U1 lists it, A14/A15 gate it): worker/effects/audio.py does not play music,
    # it synthesises two sine waves with tremolo per mood. assets/music is empty, so turning
    # this on by default would add a drone to every clip. It becomes a default once real
    # licence-clean beds ship (A14).
    music: str = ""  # mood: "" (off) | upbeat | chill | dramatic | corporate | suspense
    music_volume: float = 0.12  # background-music level (0..1)
    fades: bool = True  # fade in/out (video + audio)
    color: str = ""  # "" (off) | vivid | warm | cool | cinematic | bw
    progress_bar: bool = True  # growing progress bar along the bottom
    emoji: str = "standard"  # off | subtle | standard | heavy
    # AU1: normalise integrated loudness to the target platform's level. On by default and
    # costs no extra pass in the default configuration, because fades already re-encode the
    # audio; with every effect off the clip is stream-copied and there is nothing to
    # normalise. AU2: duck the music bed under speech instead of mixing it flat.
    loudness_normalise: bool = True  # two-pass loudnorm to the platform LUFS target
    music_duck: bool = True  # sidechain-duck music under speech
    # AU7: pull the cut points onto speech so a clip does not open on dead air. Unlike
    # filler_removal this only moves the clip's *boundaries*, and by at most 1.25 s per edge -
    # it cannot cut anything out of the middle, which is why it is a safe default and filler
    # removal is not.
    trim_silence: bool = True  # trim leading/trailing silence from each clip
    emoji_mode: str = "keyword"  # keyword | ai
    emoji_animate: bool = True  # pop/scale (alpha) animation on appear
    # Still off, and this one is a deliberate departure from U1's list.
    #
    # Every other item there restyles the clip; this one *removes content*, and it decides
    # what to remove from silence and filler-word detection. On footage that is quiet,
    # music-led, or has sparse speech it cuts aggressively: a 3.0 s fixture in
    # tests/test_pipeline_effects.py came out at 1.33 s with it on. A default that can
    # silently discard half a clip needs to be a choice, not an inherited one - the
    # cost of being wrong is asymmetric with the styling defaults around it.
    filler_removal: bool = False  # cut "um"/"uh"/long pauses
    caption_template: str = "karaoke"  # karaoke | boxed | minimal
    caption_position: str = "bottom"  # bottom | center | top

    # --- Phase 6 / Tier 1: Creator Output Upgrade ------------------------
    # All new visual/audio/rights features default OFF so an "all-off" run
    # reproduces v0.6.0 behaviour. Existing fields/defaults above are unchanged.
    #
    # Feature A — animated caption presets
    caption_preset: str = "karaoke"  # karaoke|boxed|minimal|pop|typewriter|hormozi
    caption_animation: str = ""  # "" = use preset default; else override
    # U1. Keyword highlighting is only worth defaulting on since C11 made it selective:
    # the old rule emphasised any word clearing Whisper probability 0.9, which on clean
    # audio is nearly all of them, and emphasis that applies to everything communicates
    # nothing. ``caption_keyword_ai`` stays off because it costs an LLM call per clip.
    caption_keyword_highlight: bool = True  # highlight important words
    caption_keyword_ai: bool = False  # AI-assisted keyword highlighting
    caption_emoji: bool = True  # in-caption emoji glyphs
    #
    # Feature B — b-roll overlays
    # Still off (U1 lists it, A18/A21 gate it): broll.py matches keywords by
    # case-insensitive filename-stem substring against assets/broll, which is empty, and
    # the external downloader is explicitly not implemented. Enabling it by default would
    # add degradation markers to every clip and nothing else.
    broll: bool = False  # enable b-roll auto-insertion
    broll_intensity: str = "standard"  # off|subtle|standard|heavy
    asset_sourcing_mode: str = "off"  # off|local_only|local_then_external
    broll_provider: str = ""  # external provider name ("" = none)
    #
    # Feature C — prompt / visual selection
    selection_prompt: str = ""  # free-text selection prompt
    visual_selection: bool = True  # enable visual/keyframe-aided selection (U1)
    #
    # Cross-cutting
    permissibility_mode: bool = False  # forces local_only sourcing + no added audio

    # --- v0.8.0: Speaker diarisation & multi-speaker reframe (default OFF) --
    # All new fields default OFF/standard so an "all-off" run reproduces
    # v0.7.0 behaviour exactly. Existing fields/defaults above are unchanged.
    diarization: bool = False  # persisted diarisation toggle
    speaker_reframe: bool = False  # speaker-aware reframe toggle
    reframe_layout: str = "follow_active"  # follow_active | split_screen
    reframe_intensity: str = "standard"  # subtle | standard | heavy

    # --- Kinetic typography engine (default OFF) --------------------------
    # ``kinetic_typography_enabled`` is the Feature_Flag the engine's inherited
    # ``flag_field()`` resolves to, so an all-off run still reproduces v0.8.0
    # exactly. The flat ``kinetic_*`` fields mirror ``Kinetic_Options`` and are
    # JSON scalars, so ``from_dict`` / ``dataclasses.asdict`` round-trip them
    # losslessly. Values are *not* validated here: unknown/malformed values are
    # coerced (and reported as substitutions) by the engine's ``resolve_options``,
    # which is also what keeps this module free of a ``worker.engines`` import.
    # Still off (U1 lists it): this is an AV engine Feature_Flag, and when it runs it takes
    # *ownership* of the caption layer from the standard caption path. That is a different
    # kind of default from "switch on an effect" - it swaps out the component that draws
    # captions - so it belongs to an opinionated profile (U2) rather than to the global
    # default, where it would silently override caption_preset for everyone.
    kinetic_typography_enabled: bool = False  # Feature_Flag (flag_field())
    kinetic_style: str = "karaoke_fill"  # bounce|highlight_sweep|karaoke_fill|
    # none|pop|slide_up|typewriter
    kinetic_reveal: str = "cumulative"  # cumulative | word_by_word
    kinetic_font: str = ""  # "" = inherit the caption preset font
    kinetic_max_lines: int = 2  # 1..4 text lines per cue
    kinetic_max_line_width: int = 22  # 6..80 display-width units per line
    kinetic_safe_area_x_pct: float = 6.0  # 0..25 % horizontal safe-area inset
    kinetic_safe_area_y_pct: float = 10.0  # 0..40 % vertical safe-area inset
    kinetic_motion_ms: int = 120  # 20..1000 ms per-word motion duration
    kinetic_confidence_floor: float = 0.0  # 0..1 word-confidence emphasis floor

    # --- Stem inpainting engine (default OFF) -----------------------------
    # Same arrangement as the kinetic block above, for the same reasons:
    # ``stem_inpainting_enabled`` is the Feature_Flag the engine's inherited
    # ``flag_field()`` resolves to, and the ten flat ``stem_*`` fields mirror
    # ``Stem_Options`` one-for-one so ``from_dict`` / ``dataclasses.asdict``
    # round-trip them losslessly. Values are deliberately *not* validated here —
    # the engine's ``resolve_options`` coerces every one of them against its
    # documented bounds, which is what keeps this module free of a
    # ``worker.engines`` import and keeps an unrecognised value from failing a job.
    stem_inpainting_enabled: bool = False  # Feature_Flag (flag_field())
    stem_mix_preset: str = "custom"  # custom|speech_focus|music_focus|
    # clean_speech
    stem_gain_vocals: float = 1.0  # 0.0..4.0 (0.0 mutes, >1.0 boosts)
    stem_gain_music: float = 1.0  # 0.0..4.0
    stem_gain_other: float = 1.0  # 0.0..4.0
    stem_repair_mode: str = "crossfade"  # off | crossfade | spectral
    stem_repair_window_ms: int = 12  # 2..120 ms symmetric seam window
    stem_declick: bool = False  # 1 ms fade at clip head/tail
    stem_backend: str = "auto"  # auto | ml | ffmpeg
    stem_model: str = "htdemucs"  # separation checkpoint name
    stem_retain_stems: bool = False  # keep per-stem WAVs as durable artifacts

    # --- U6: brand kit ----------------------------------------------------
    #
    # A creator's look was spread across places that could not be saved together: the caption
    # font and colours lived inside a preset editable only in source, the CTA was regenerated per
    # clip by the LLM so it varied run to run, and a logo could not be applied at all.
    #
    # All empty by default, and each is additive - an unset field leaves the preset's own value
    # alone rather than overwriting it with a default.
    brand_font: str = ""  # caption font, overriding the preset's
    brand_primary_color: str = ""  # "#RRGGBB"; converted to ASS internally
    brand_highlight_color: str = ""  # "#RRGGBB"
    brand_cta: str = ""  # standing call to action (also the V14 end card)
    brand_logo: str = ""  # path to a png/jpg/webp watermark
    brand_logo_position: str = "top_right"  # top_left|top_right|bottom_left|bottom_right
    brand_logo_scale: float = 0.16  # fraction of frame width
    brand_logo_opacity: float = 0.85  # 0..1

    # U2: the built-in profile this request was built from, "" when none. Recorded so a
    # finished job says which bundle produced it; it never changes behaviour on its own -
    # ``from_dict`` has already expanded the bundle into the individual fields by the time
    # anything reads them.
    profile: str = ""

    # Known value sets for enum-like string fields (used by ``from_dict``).
    _CAPTION_PRESETS = ("karaoke", "boxed", "minimal", "pop", "typewriter", "hormozi")
    _CAPTION_ANIMATIONS = ("", "none", "pop", "typewriter", "karaoke_fill")
    _BROLL_INTENSITIES = ("off", "subtle", "standard", "heavy")
    _ASSET_SOURCING_MODES = ("off", "local_only", "local_then_external")
    _REFRAME_LAYOUTS = ("follow_active", "split_screen")
    _REFRAME_INTENSITIES = ("subtle", "standard", "heavy")

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ProcessingOptions:
        """Build options from a (possibly partial) dict, ignoring unknown keys.

        A ``profile`` key naming a built-in profile (U2) supplies a whole coherent bundle of
        settings, and **anything else in ``data`` overrides it**. That ordering is the reason
        the bundle is expanded here rather than in :func:`effective_options`: at this point we
        still know which fields the caller actually sent, so "the profile sets emoji to
        heavy, but this request explicitly asked for subtle" resolves correctly. A dataclass
        instance cannot express that distinction - a field holding its default is
        indistinguishable from a field nobody mentioned.
        """
        data = data or {}
        valid = {k: data[k] for k in cls.__dataclass_fields__ if k in data}

        profile_name = str(data.get("profile", "") or "").strip().lower()
        profile = BUILTIN_PROFILES.get(profile_name)
        if profile is not None:
            # Bundle first, explicit request second.
            valid = {**profile.settings, "profile": profile.name, **valid}
            valid["profile"] = profile.name
        elif "profile" in valid:
            # An unknown profile name is dropped rather than recorded, so a job never claims
            # to have been produced by a bundle that does not exist.
            valid.pop("profile")
        # Normalise an empty-string language to None (auto).
        if valid.get("language") in ("", "auto", "Auto"):
            valid["language"] = None
        # Coerce numeric fields that may arrive as strings from form data.
        for num_field in ("range_start", "range_end", "schedule_at"):
            v = valid.get(num_field)
            # `v is None or v == ""` rather than `v in ("", None)`: identical for every value
            # that reaches here, but a form a type checker can narrow, so the `float(v)` below is
            # known to receive a non-None value rather than relying on the `except TypeError`.
            if v is None or v == "":
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
        for bool_field in (
            "reframe",
            "zoom",
            "transitions",
            "hook_title",
            "fades",
            "subtitle_sidecar",
            "progress_bar",
            "emoji_animate",
            "filler_removal",
            # Phase 6 / Tier 1 boolean flags
            "caption_keyword_highlight",
            "caption_keyword_ai",
            "caption_emoji",
            "broll",
            "visual_selection",
            "permissibility_mode",
            # v0.8.0 boolean flags
            "diarization",
            "speaker_reframe",
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
            "stem_retain_stems",
        ):
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


@dataclass(frozen=True)
class BuiltinProfile:
    """An opinionated bundle of :class:`ProcessingOptions` settings (U2).

    ``settings`` holds only the fields the profile has an opinion about; everything else
    keeps the global default. ``rationale`` is not decoration - a profile is a set of
    judgement calls, and the next person to change one needs to know which effect was chosen
    on purpose and which was simply inherited.
    """

    name: str
    label: str
    description: str
    rationale: str
    settings: dict[str, Any]


#: U2: coherent presets, so a user picks a *kind of video* instead of thirteen toggles.
#:
#: The global defaults (U1) are deliberately middle-of-the-road, because they apply to
#: footage nobody has described. These profiles are allowed to be opinionated precisely
#: because the user has told us what they are editing - which is also why two of them turn
#: on features the global default leaves off: ``filler_removal`` (destructive, but expected
#: in a rambling interview) and ``kinetic_typography_enabled`` (takes ownership of the
#: caption layer, which is a swap you would only want if you asked for that look).
BUILTIN_PROFILES: dict[str, BuiltinProfile] = {
    "podcast": BuiltinProfile(
        name="podcast",
        label="Podcast",
        description="Multi-speaker conversation, cut for clarity.",
        rationale=(
            "The distinguishing feature of podcast footage is more than one person talking, "
            "so diarisation and speaker-aware reframing carry this profile: a static crop on "
            "a two-host shot frames the gap between them. Filler removal is on because an "
            "unscripted conversation is the one place its cost is worth paying, and long "
            "clips are the format's norm. Zoom is off - the frame already moves when the "
            "active speaker changes, and both at once reads as restless."
        ),
        settings={
            "clip_length": "60-90s",
            "caption_preset": "karaoke",
            "reframe": True,
            "diarization": True,
            "speaker_reframe": True,
            "reframe_layout": "follow_active",
            "reframe_intensity": "standard",
            "filler_removal": True,
            "zoom": False,
            "transitions": True,
            "fades": True,
            "hook_title": True,
            "progress_bar": True,
            "emoji": "subtle",
            "caption_keyword_highlight": True,
            "caption_emoji": False,
        },
    ),
    "gaming": BuiltinProfile(
        name="gaming",
        label="Gaming",
        description="High-energy gameplay and commentary.",
        rationale=(
            "The loudest profile we ship, and the only one with kinetic typography on: this "
            "is the audience that expects word-by-word animated captions, and a profile is "
            "the right place for a feature that takes over the caption layer, because the "
            "user asked for the look. Vivid colour and heavy emoji suit game footage, which "
            "is already saturated and fast. Short clips, because gameplay highlights are."
        ),
        settings={
            "clip_length": "<30s",
            "caption_preset": "hormozi",
            "caption_position": "center",
            "kinetic_typography_enabled": True,
            "kinetic_style": "bounce",
            "kinetic_reveal": "word_by_word",
            "reframe": True,
            "zoom": True,
            "transitions": True,
            "fades": True,
            "hook_title": True,
            "progress_bar": True,
            "color": "vivid",
            "emoji": "heavy",
            "caption_keyword_highlight": True,
            "caption_emoji": True,
        },
    ),
    "talking_head": BuiltinProfile(
        name="talking_head",
        label="Talking head",
        description="One person to camera.",
        rationale=(
            "One speaker, so face-tracked reframing matters and diarisation does not - "
            "running it would spend time to discover a single speaker. A slow zoom does the "
            "work that speaker switching does in the podcast profile: it stops a fixed shot "
            "of a stationary person feeling static. Punchy captions, because there is little "
            "else on screen to hold attention."
        ),
        settings={
            "clip_length": "30-60s",
            "caption_preset": "pop",
            "reframe": True,
            "reframe_intensity": "standard",
            "diarization": False,
            "speaker_reframe": False,
            "zoom": True,
            "transitions": True,
            "fades": True,
            "hook_title": True,
            "progress_bar": True,
            "emoji": "standard",
            "caption_keyword_highlight": True,
            "caption_emoji": True,
        },
    ),
    "educational": BuiltinProfile(
        name="educational",
        label="Educational",
        description="Explainers and tutorials, optimised for legibility.",
        rationale=(
            "The only profile that deliberately does *less*. Boxed captions are the most "
            "legible thing we render, and the movement effects are off because they compete "
            "with content the viewer is trying to follow - a zoom during a diagram is a "
            "distraction, not production value. Filler removal is on for the same reason it "
            "is on for podcasts: unscripted explanation rambles. Longer clips, because an "
            "explanation that fits in 20 seconds usually was not one."
        ),
        settings={
            "clip_length": "60-90s",
            "caption_preset": "boxed",
            "caption_position": "bottom",
            "reframe": True,
            "zoom": False,
            "transitions": False,
            "fades": True,
            "hook_title": True,
            "progress_bar": True,
            "filler_removal": True,
            "color": "",
            "emoji": "subtle",
            "caption_keyword_highlight": True,
            "caption_emoji": False,
        },
    ),
}


def effective_options(o: ProcessingOptions) -> ProcessingOptions:
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
    score: float = 0.0  # virality score 0..100
    reason: str = ""  # why this moment was picked
    platform: str = "generic"  # platform the metadata targets
    title_alternatives: list[str] = field(default_factory=list)
    description: str = ""  # caption / description
    hashtags: list[str] = field(default_factory=list)
    hook_text: str = ""  # on-screen opening hook
    cta: str = ""  # call to action
    mentions: list[str] = field(default_factory=list)  # @tags
    thumbnail_text: str = ""  # thumbnail text idea
    transcript_text: str = ""  # clip transcript (for regen)

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
    #   - ``music_degraded:synthesised``  the "music" bed is the synthesised two-tone
    #                                    fallback, not a real track (A15). Recorded
    #                                    alongside ``music:<mood>``, which on its own
    #                                    cannot tell the two apart - and since
    #                                    ``assets/music`` ships empty, in practice it was
    #                                    always the drone.
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

    # --- U9: batch review ------------------------------------------------
    #
    # A job produces up to ten clips and every one of them had to be judged, edited and
    # published individually. There was nowhere to record "I have looked at this and it is
    # good" or "this one is not usable", so a review pass over twenty clips left no trace and
    # had to be redone from the top after any interruption.
    #
    # ``pending`` is the default so every existing clip - and every clip a running job is
    # about to produce - reads as unreviewed rather than silently approved.
    review_state: str = "pending"  # pending | approved | rejected
    review_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ClipResult:
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

    input_type: str  # "url" | "file"
    source: str  # URL or original filename
    options: ProcessingOptions
    # U7: the resolved *local* file the pipeline actually read.
    #
    # ``source`` is the URL for a URL job, so it cannot be re-read. The download path was
    # known only inside the job body and thrown away, which is why re-rendering one clip
    # previously meant re-downloading and re-running the whole job.
    source_path: str = ""
    # I5: the windows selection chose, as ``{"start", "end", "reason", "score"}`` dicts.
    #
    # Recorded so an interrupted job can be resumed rather than restarted. Without it a resume
    # would have to re-run selection, which with an LLM in it is not deterministic - so the
    # clips already on disk might not correspond to any window the second run chose, and the
    # user would get a mix of two different selections.
    planned_clips: list[dict] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    batch_id: str | None = None
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0  # 0..1
    stage: str = "Queued"
    title: str = ""
    duration: float | None = None
    thumbnail: str | None = None
    error: str | None = None
    clips: list[ClipResult] = field(default_factory=list)
    # U8: progress was a single coarse fraction plus a free-text stage string, so the UI could
    # only ever show one bar and a sentence. These make the *structure* of the work visible:
    # which stage of how many, and what each has cost so far (M5).
    stage_index: int = 0
    stage_total: int = 0
    stage_timings: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict for the API."""
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "input_type": self.input_type,
            "source": self.source,
            # U7: needed to re-render one clip without re-downloading the source.
            "source_path": self.source_path,
            # I5: the selected windows, so an interrupted job can resume the missing ones.
            "planned_clips": self.planned_clips,
            "status": self.status.value,
            "progress": round(self.progress, 3),
            "stage": self.stage,
            "stage_index": self.stage_index,
            "stage_total": self.stage_total,
            "stage_timings": self.stage_timings,
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
    def from_dict(cls, data: dict[str, Any] | None) -> Job:
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
            source_path=str(data.get("source_path") or ""),
            planned_clips=list(data.get("planned_clips") or []),
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
            # U8/M5: restored so a completed job's timing report survives a restart. Coerced
            # defensively because these come back from JSON written by a possibly older build.
            stage_index=int(data.get("stage_index") or 0),
            stage_total=int(data.get("stage_total") or 0),
            stage_timings=list(data.get("stage_timings") or []),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )
