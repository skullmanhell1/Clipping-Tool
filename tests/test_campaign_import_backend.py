"""Focused contracts for multi-source campaign-profile import and compliance."""

from __future__ import annotations

from worker.campaign_compliance import check_clip_against_profiles, generate_campaign_copy
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
