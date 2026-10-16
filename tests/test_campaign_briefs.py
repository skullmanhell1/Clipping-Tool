"""Offline tests for structured briefs, brand profiles, compliance, and archival seams."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from publishers.brief_metadata import metadata_for_platform
from worker.brand_profiles import BrandProfileStore
from worker.campaign_archive import archive_campaign, is_expired
from worker.campaign_brief import CampaignBrief, PlatformBrief
from worker.compliance import check_clip, check_compliance


def test_brand_profiles_keep_campaigns_isolated(tmp_path):
    store = BrandProfileStore(tmp_path / "brands.json")
    first = store.create("Client A", primary_color="#ff0000")
    second = store.create("Client B", primary_color="#00ff00")

    store.map_campaign("campaign-a", first.id)
    store.map_campaign("campaign-b", second.id)
    store.map_campaign("campaign-a", second.id)

    assert store.for_campaign("campaign-a").primary_color == "#00ff00"
    assert store.for_campaign("campaign-b").primary_color == "#00ff00"
    assert "campaign-a" not in first.campaign_ids


def test_compliance_blocks_forbidden_and_missing_required_terms():
    result = check_compliance(
        "A launch from our team",
        forbidden_words=["competitor"],
        required_words=["BOXABL", "launch"],
    )
    assert result.passed is False
    assert result.missing_required == ["boxabl"]

    clip = SimpleNamespace(title="BOXABL launch", description="", transcript_text="safe")
    assert check_clip(clip, required_words=["boxabl"]).passed


def test_metadata_mapping_applies_platform_overrides():
    brief = SimpleNamespace(
        description="Brief description",
        call_to_action="Learn more",
        hashtags=["#brief"],
        platform_metadata={"tiktok": {"description": "Short caption", "hashtags": ["trend"]}},
    )
    data = metadata_for_platform(brief, "tiktok")
    assert data["description"] == "Short caption"
    assert data["cta"] == "Learn more"
    assert data["hashtags"] == ["#trend"]

    parsed_brief = CampaignBrief(
        platform_metadata={
            "youtube_shorts": PlatformBrief(
                name="youtube_shorts",
                metadata={"description": "Shorts copy"},
                hashtags=["#shorts"],
            )
        },
    )
    mapped = metadata_for_platform(parsed_brief, "youtube", base={"hashtags": []})
    assert mapped["description"] == "Shorts copy"
    assert mapped["hashtags"] == ["#shorts"]


def test_campaign_archive_is_expiry_gated_and_idempotent(tmp_path):
    source = tmp_path / "clips"
    source.mkdir()
    (source / "clip.mp4").write_bytes(b"clip")
    archive = tmp_path / "archive"

    assert is_expired("2026-01-01", today=date(2026, 2, 1))
    not_yet = archive_campaign(
        source, archive, "c1", promotional_end="2026-12-31", today=date(2026, 2, 1)
    )
    assert not_yet["archived"] is False

    done = archive_campaign(
        source, archive, "c1", promotional_end="2026-01-01", today=date(2026, 2, 1)
    )
    again = archive_campaign(
        archive / "c1", archive, "c1", promotional_end="2026-01-01", today=date(2026, 2, 1)
    )
    assert done["archived"] is True
    assert again["archived"] is True
