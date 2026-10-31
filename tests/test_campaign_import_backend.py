"""Focused contracts for multi-source campaign-profile import and compliance."""

from __future__ import annotations

from worker.campaign_compliance import (
    check_clip_against_profiles,
    check_profile_compliance,
    generate_campaign_copy,
)
from worker.campaign_preset import CampaignPreset, validate_clip_against_preset
from worker.campaign_profile import parse_campaign_profile


def test_campaign_profile_parser_keeps_explicit_rules_and_ambiguity_flags():
    profile = parse_campaign_profile(
        """# Spring launch
        - Clip length: 15-30 seconds
        - Aspect ratio: 9:16, 1080x1920
        - Platforms: TikTok | Instagram Reels
        | Banned words | guaranteed, miracle |
        | Hashtags | #spring #launch |
        - End card: required
        - Deadline: 2026-11-01
        """
    )

    assert profile.name == "Spring launch"
    assert profile.length["min_seconds"] == 15
    assert profile.length["max_seconds"] == 30
    assert profile.aspect_resolution["aspect_ratios"] == ["9:16"]
    assert profile.platforms == ["tiktok", "instagram"]
    assert profile.copy_rules["hashtags"] == ["#spring", "#launch"]
    assert profile.watermark_end_cards["end_card_required"] is True
    assert profile.payout_deadlines["deadlines"] == ["2026-11-01"]


def test_multiple_profiles_return_individual_reports_and_generated_copy():
    first = parse_campaign_profile("Hashtags: #launch\nRequired copy: Shop now")
    second = parse_campaign_profile("Mentions: @brand\nBanned words: scam")
    clip = {"caption": "Shop now", "hashtags": ["#launch"], "description": "safe"}

    generated = generate_campaign_copy(first, clip)
    report = check_clip_against_profiles([first, second], clip)

    assert generated["caption"] == "Shop now"
    assert generated["hashtags"] == ["#launch"]
    assert report["profile_count"] == 2
    assert {item["profile_name"] for item in report["profiles"]} == {"Untitled campaign"}
    assert report["status"] == "fail"


def test_campaign_profile_extracts_video_links_branding_and_public_policy():
    profile = parse_campaign_profile(
        """# Whop launch
        Logo: assets/brand/logo.png
        Font: Inter Bold
        Primary colour: #7c3aed
        Accent colour: #22d3ee
        Public visibility: required
        Approved video: https://youtu.be/abc123
        Drive source: https://drive.google.com/file/d/video123/view
        Google Doc: https://docs.google.com/document/d/brief123/edit
        """
    )

    assert profile.branding["logo"] == "assets/brand/logo.png"
    assert profile.branding["font"] == "Inter Bold"
    assert profile.branding["colors"] == {"primary": "#7c3aed", "accent": "#22d3ee"}
    assert profile.video_links == [
        "https://youtu.be/abc123",
        "https://drive.google.com/file/d/video123/view",
    ]
    assert profile.watermark_end_cards["public_visibility_required"] is True


def test_source_rules_match_equivalent_youtube_urls():
    profile = parse_campaign_profile("Approved sources: https://youtu.be/abc123")
    clip = {
        "caption": "A safe clip",
        "source_url": "https://www.youtube.com/watch?v=abc123&t=12",
    }

    report = check_profile_compliance(profile, clip, visibility=None)
    source = next(item for item in report.results if item["rule"] == "source")

    assert source["status"] == "pass"


def test_review_uploads_do_not_enforce_public_visibility():
    profile = parse_campaign_profile("Public visibility: required")
    clip = {"caption": "A safe clip"}

    report = check_profile_compliance(profile, clip, visibility=None)

    assert not any(item["rule"] == "visibility" for item in report.results)


def test_legacy_preset_review_skips_posting_visibility_requirement():
    preset = CampaignPreset(name="Review", require_public_visibility=True)

    publish_report = validate_clip_against_preset(preset, {"visibility": None})
    review_report = validate_clip_against_preset(
        preset, {"visibility": None}, visibility_required=False
    )

    assert publish_report.for_rule("visibility").status == "unknown"
    assert review_report.for_rule("visibility").status == "pass"
    assert review_report.status == "pass"
