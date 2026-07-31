"""Health and capability reporting: ``/healthz`` and ``/api/info``.

``/api/info`` is what the frontend reads at start-up to decide which controls to show, so every
optional dependency this deployment may or may not have is probed here rather than assumed.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.capabilities import (
    _available_broll_providers,
    _engines_info,
    _llm_available_safe,
    _preset_detail,
)
from api.version import APP_VERSION
from config import settings
from runtime_config import RETENTION_CHOICES
from worker import captions as cap
from worker.effects import broll, caption_presets
from worker.metadata import PLATFORM_PROFILES, REGENERATABLE_FIELDS
from worker.models import BUILTIN_PROFILES, ProcessingOptions

#: Every route below carries the ``system`` tag, supplied here rather
#: than repeated on each decorator.
router = APIRouter(tags=["system"])


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/info")
def info() -> dict[str, object]:
    engines, capabilities = _engines_info()
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "version": APP_VERSION,
        "aspect_ratios": ["9:16", "1:1", "16:9", "4:5"],
        "clip_lengths": ["auto", "<30s", "30-60s", "60-90s", "90s-3min"],
        "clip_counts": ["auto", "1", "3", "5", "10", "max"],
        "platforms": list(PLATFORM_PROFILES.keys()),
        "strategies": ["ai", "silence", "fixed"],
        "regeneratable_fields": list(REGENERATABLE_FIELDS),
        # U2: names only, so a client can offer the picker from one call. The full bundles,
        # with the reasoning behind each, come from GET /api/profiles/builtin.
        "builtin_profiles": list(BUILTIN_PROFILES),
        "llm_available": _llm_available_safe(),
        "effects": {
            "music_moods": ["upbeat", "chill", "dramatic", "corporate", "suspense"],
            "color_presets": ["vivid", "warm", "cool", "cinematic", "bw"],
            "emoji_intensities": ["off", "subtle", "standard", "heavy"],
            "emoji_modes": ["keyword", "ai"],
            "caption_templates": ["karaoke", "boxed", "minimal"],
            # C13: nine positions, up from three. The original three stay first and keep their
            # names, so a client that only knows them is unaffected.
            "caption_positions": list(cap.VALID_CAPTION_POSITIONS),
            # A4: the twelve vendored faces were shipped with licences and a manifest and
            # nothing exposed them, so the only way to change a caption font was to edit a
            # preset in source. Variable fonts are filtered out here rather than offered and
            # then silently substituted (C1).
            "caption_fonts": cap.available_fonts(),
            # C12: the platform safe-area profiles a client may select.
            "caption_safe_areas": list(cap.SAFE_AREA_INSETS.keys()),
            # Tier 1 — Creator Output Upgrade (additive; Reqs 1.4, 8.7, 22.3)
            "caption_presets": list(caption_presets.BUILTIN_PRESETS.keys()),
            # U5: the presets' actual values, not just their names. A style picker cannot
            # preview a look it only knows the name of, so the previous names-only list left
            # the UI unable to show a creator what "hormozi" or "typewriter" would look like
            # before spending a render finding out. Colours are added in `#RRGGBB` form
            # alongside the ASS originals, because a colour input cannot display `&H00FFFFFF`.
            "caption_preset_details": [
                _preset_detail(preset) for preset in caption_presets.BUILTIN_PRESETS.values()
            ],
            "caption_animations": ["none", "pop", "typewriter", "karaoke_fill"],
            "asset_sourcing_modes": ["off", "local_only", "local_then_external"],
            "broll_intensities": list(broll.BROLL_INTENSITY.keys()),
            "broll_providers": _available_broll_providers(),
            # v0.8.0 — Speaker Diarisation & Multi-Speaker Reframe (additive;
            # Reqs 7.4, 10.6, 17.5, 18.1). Sourced from the ProcessingOptions
            # known-value sets so the API stays in lockstep with the model.
            "reframe_layouts": list(ProcessingOptions._REFRAME_LAYOUTS),
            "reframe_intensities": list(ProcessingOptions._REFRAME_INTENSITIES),
        },
        "broll_available": bool(settings.broll_provider_api_key and settings.broll_allow_download),
        "storage_backend": settings.storage_backend.value,
        "retention_choices": list(RETENTION_CHOICES),
        # Advanced AV engines foundation (additive; Reqs 20.1, 20.2, 20.6).
        # Both are empty until an engine spec registers one, so a stock install
        # sees the v0.8.0 payload plus two inert keys.
        "engines": engines,
        "capabilities": capabilities,
    }
