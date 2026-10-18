"""Focused tests for isolated dynamic campaign preset policy."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from worker.campaign_preset import (
    Branding,
    CampaignPreset,
    CliffhangerSettings,
    HookSettings,
    merge_presets,
    parse_campaign_preset,
    plan_daily_slots,
    swap_presets,
    validate_clip_against_preset,
)

BRIEF = """
# Campaign: Spring Launch

## Branding
- Logo: https://cdn.example.test/spring/logo.png
- Font: Brand Sans Bold
- Colors: primary=#112233; accent=#ff00aa

Caption style: Punchy Karaoke
Aspect ratios: 9:16, 1920:1080
Hashtags: #SpringLaunch, #MadeForEveryday
CTA: Shop the new collection
Allowed platforms: TikTok, Instagram Reels
Daily clip target: 3
Public visibility: required
Pattern interrupter interval: every 4 clips
Hook: enabled, 3 seconds, curiosity
Cliffhanger: enabled, 2 seconds, open loop
"""


def test_google_doc_brief_parses_complete_campaign_preset():
    preset = parse_campaign_preset(BRIEF)

    assert preset.name == "Spring Launch"
    assert preset.branding == Branding(
        logo="https://cdn.example.test/spring/logo.png",
        font="Brand Sans Bold",
        colors={"primary": "#112233", "accent": "#ff00aa"},
    )
    assert preset.caption_style == "Punchy Karaoke"
    assert preset.aspect_ratios == ["9:16", "16:9"]
    assert preset.hashtags == ["#SpringLaunch", "#MadeForEveryday"]
    assert preset.cta == "Shop the new collection"
    assert preset.allowed_platforms == ["tiktok", "instagram"]
    assert preset.daily_clip_target == 3
    assert preset.public_visibility_required is True
    assert preset.pattern_interrupter_interval == 4
    assert preset.hook_settings == HookSettings(True, 3.0, "curiosity")
    assert preset.cliffhanger_settings == CliffhangerSettings(True, 2.0, "open loop")


def test_default_daily_target_and_public_policy_are_safe():
    preset = parse_campaign_preset("Campaign: Minimal")

    assert preset.daily_clip_target == 2
    assert preset.require_public_visibility is True


def test_preset_merge_and_swap_are_deterministic_and_do_not_alias_inputs():
    base = CampaignPreset(
        name="Base",
        branding=Branding(logo="base.png", colors={"primary": "#111111"}),
        hashtags=["#base"],
        cta="Learn more",
        daily_clip_target=4,
    )
    override = CampaignPreset(
        branding=Branding(font="Campaign Font", colors={"accent": "#222222"}),
        hashtags=["#launch"],
        cta="Shop now",
    )

    merged = merge_presets(base, override)
    assert merged.branding.logo == "base.png"
    assert merged.branding.font == "Campaign Font"
    assert merged.branding.colors == {"primary": "#111111", "accent": "#222222"}
    assert merged.hashtags == ["#launch"]
    assert merged.cta == "Shop now"
    assert merged.daily_clip_target == 4

    replacement = CampaignPreset(name="Replacement", hashtags=["#new"])
    swapped = swap_presets(base, replacement)
    assert swapped.to_dict() == replacement.to_dict()
    assert swapped is not replacement
    swapped.hashtags.append("#local")
    assert replacement.hashtags == ["#new"]


def test_compliance_report_exposes_failures_and_unknown_evidence():
    preset = CampaignPreset(
        branding=Branding(logo="logo.png"),
        aspect_ratios=["9:16"],
        hashtags=["#launch", "#spring"],
        cta="Shop now",
        allowed_platforms=["tiktok"],
        hook_settings=HookSettings(enabled=True),
    )

    failed = validate_clip_against_preset(
        preset,
        {"visibility": "private", "platform": "youtube", "aspect_ratio": "16:9"},
        {"hashtags": ["#launch"], "cta": "Learn more"},
    )
    assert failed.status == "fail"
    assert failed.for_rule("visibility").status == "fail"
    assert failed.for_rule("platform").status == "fail"
    assert failed.for_rule("aspect_ratio").status == "fail"
    assert failed.for_rule("hashtags").status == "fail"
    assert failed.for_rule("cta").status == "fail"
    assert failed.for_rule("branding").status == "unknown"
    assert failed.for_rule("hook").status == "unknown"

    unknown = validate_clip_against_preset(preset, {}, {})
    assert unknown.status == "unknown"
    assert unknown.for_rule("visibility").status == "unknown"


def test_daily_slots_are_exactly_two_distinct_utc_epochs_and_deterministic():
    slots = plan_daily_slots(date(2026, 3, 5), "America/New_York", [9, "18:30", 21])
    repeated = plan_daily_slots(datetime(2026, 3, 5), "America/New_York", [9, "18:30"])

    assert len(slots) == 2
    assert len(set(slots)) == 2
    assert slots == repeated
    assert slots == (
        datetime(2026, 3, 5, 9, tzinfo=ZoneInfo("America/New_York")).timestamp().__int__(),
        1772753400,
    )


def test_daily_slot_planning_rejects_non_public_visibility():
    with pytest.raises(ValueError, match="public"):
        plan_daily_slots("2026-03-05", "UTC", [9, 18], visibility="unlisted")

    with pytest.raises(ValueError, match="two distinct"):
        plan_daily_slots("2026-03-05", "UTC", [9, 9])
