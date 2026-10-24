from __future__ import annotations

from pathlib import Path

from publishers.history import HistoryStore
from worker.campaign_brief import CampaignBrief, parse_campaign_brief
from worker.campaign_brief_sync import (
    CampaignBriefSyncService,
    DriveBriefSnapshot,
)
from worker.jobs import JobStore
from worker.llm_client import MockLLMClient
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions

BRIEF_TEXT = """
# Spring launch campaign

Target aspect ratios: 9:16, 1:1, 1920:1080
Brand voice: warm, practical, confident; never hypey
Forbidden words: guaranteed, cheapest, competitor
Hashtags: #SpringLaunch #MadeForEveryday #springlaunch
CTA: Shop the new collection at example.com
Font assets: assets/fonts/BrandSans-Bold.ttf, BrandSans
Logo asset: https://cdn.example.test/brand/logo.svg
YouTube: aspect_ratio=16:9, title_limit=100, caption=long
TikTok: 9:16; max_length=30s; #TikTokTips
Promo: March 5, 2026 - March 12, 2026
"""


def test_deterministic_parser_extracts_all_brief_sections_without_an_llm():
    brief = parse_campaign_brief(BRIEF_TEXT, use_llm=False)

    assert brief.target_aspect_ratios == ["9:16", "1:1", "16:9"]
    assert brief.brand_voice == "warm, practical, confident; never hypey"
    assert brief.forbidden_words == ["guaranteed", "cheapest", "competitor"]
    assert brief.hashtags == ["#SpringLaunch", "#MadeForEveryday"]
    assert brief.cta.startswith("Shop the new collection")
    assert brief.font_assets == ["assets/fonts/BrandSans-Bold.ttf"]
    assert brief.logo_assets == ["https://cdn.example.test/brand/logo.svg"]
    assert brief.platforms["youtube"].metadata["title_limit"] == "100"
    assert brief.platforms["tiktok"].aspect_ratios == ["9:16"]
    assert brief.promotional_dates[0].start_date == "2026-03-05"
    assert brief.promotional_dates[0].end_date == "2026-03-12"


def test_optional_llm_json_is_coerced_and_falls_back_to_deterministic_fields():
    response = (
        '{"target_aspect_ratios": ["4:5"], "brand_voice": "measured", '
        '"forbidden_words": ["overpromise"], "hashtags": ["launch"], '
        '"cta": "Learn more", "font_assets": ["BrandSans"], '
        '"promotional_dates": [{"date": "2026-04-01", "label": "launch"}]}'
    )
    brief = parse_campaign_brief(BRIEF_TEXT, client=MockLLMClient(responses=[response]))

    assert brief.parser == "llm"
    assert brief.target_aspect_ratios == ["4:5"]
    assert brief.hashtags == ["#launch"]
    assert brief.promotional_dates[0].start_date == "2026-04-01"

    fallback = parse_campaign_brief(BRIEF_TEXT, client=MockLLMClient(responses=["not json"]))
    assert fallback.parser == "deterministic"
    assert fallback.target_aspect_ratios == ["9:16", "1:1", "16:9"]


def test_brief_round_trip_is_json_ready_and_empty_brief_is_unconfigured():
    brief = parse_campaign_brief(BRIEF_TEXT, use_llm=False)
    restored = CampaignBrief.from_dict(brief.to_dict())

    assert restored.to_dict() == brief.to_dict()
    assert parse_campaign_brief("", use_llm=False).configured is False


class _Reader:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = []

    def fetch(self, file_id, *, known_revision="", known_content_hash=""):
        self.calls.append((file_id, known_revision, known_content_hash))
        return self.snapshots.pop(0)


def _campaign_job() -> Job:
    job = Job(
        input_type="file",
        source="/source.mp4",
        options=ProcessingOptions(campaign_id="campaign-1"),
        id="job-1",
        status=JobStatus.COMPLETED,
    )
    job.clips = [
        ClipResult(
            id="clip-1",
            filename="clip-1.mp4",
            start=1,
            end=4,
            duration=3,
            review_state="approved",
        )
    ]
    return job


def test_sync_persists_state_flags_existing_clips_and_skips_unchanged_content(tmp_path: Path):
    history = HistoryStore(tmp_path / "history.db")
    history.save_campaign("Spring", {"youtube": {}}, "campaign-1")
    jobs = JobStore(persistence=False)
    job = _campaign_job()
    assert jobs.add(job)
    history.record_clip("job-1", job.clips[0], "/clips/clip-1.mp4", "campaign-1")

    first = DriveBriefSnapshot.from_content("drive-1", BRIEF_TEXT, revision_id="7")
    second = DriveBriefSnapshot.from_content("drive-1", BRIEF_TEXT, revision_id="7")
    reader = _Reader([first, second])
    service = CampaignBriefSyncService(
        history,
        job_store=jobs,
        drive_reader=reader,
        clock=lambda: 1234.0,
    )

    synced = service.sync("campaign-1", source_file_id="drive-1")
    assert synced.status == "synced"
    assert synced.changed is True
    assert synced.affected_clip_ids == [{"job_id": "job-1", "clip_id": "clip-1"}]
    assert history.campaign("campaign-1").brief.brand_voice.startswith("warm")
    restored_campaign = HistoryStore(tmp_path / "history.db").campaign("campaign-1")
    assert restored_campaign.brief_content_hash == first.content_hash
    assert restored_campaign.brief_source_file_id == "drive-1"
    flagged = jobs.get_clip("job-1", "clip-1")
    assert flagged.needs_rerender is True
    assert flagged.review_state == "pending"
    assert flagged.brief_revision == "7"
    history_row = history.history()["clips"][0]
    assert history_row["needs_rerender"] == 1
    assert history_row["brief_revision"] == "7"

    unchanged = service.poll("campaign-1", source_file_id="drive-1")
    assert unchanged.status == "unchanged"
    assert unchanged.changed is False


def test_no_brief_configuration_is_a_noop(tmp_path: Path):
    history = HistoryStore(tmp_path / "history.db")
    history.save_campaign("No brief", {"youtube": {}}, "campaign-2")

    result = CampaignBriefSyncService(history).sync("campaign-2")

    assert result.status == "unconfigured"
    assert result.affected_clip_ids == []
    campaign = history.campaign("campaign-2")
    assert campaign.brief is None
    assert campaign.brief_sync_status == "unconfigured"
