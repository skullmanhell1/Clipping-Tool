from __future__ import annotations

from types import SimpleNamespace

from worker.boxabl_campaign import (
    APPROVED_DRIVE_FILE_IDS,
    APPROVED_YOUTUBE_VIDEO_IDS,
    REQUIRED_REVIEW_CHECKS,
    campaign_publish_blockers,
    drive_file_id,
    enrich_campaign_metadata,
    is_approved_source_url,
    normalize_boxabl_text,
    normalize_boxabl_words,
    youtube_video_id,
)
from worker.metadata import ClipMetadata
from worker.transcribe import Word


def test_campaign_source_manifest_contains_the_drive_and_youtube_brief_links():
    assert "JbS_VhA0rRo" in APPROVED_YOUTUBE_VIDEO_IDS
    assert "1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry" in APPROVED_DRIVE_FILE_IDS
    assert is_approved_source_url("https://youtu.be/JbS_VhA0rRo?feature=shared")
    assert is_approved_source_url(
        "https://drive.google.com/file/d/1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry/view?usp=sharing"
    )
    assert not is_approved_source_url("https://www.youtube.com/@BOXABL/videos")
    assert not is_approved_source_url("https://youtu.be/not-in-the-brief")


def test_source_id_parsers_accept_supported_video_link_shapes_only():
    assert youtube_video_id("https://www.youtube.com/watch?v=JbS_VhA0rRo&t=12") == "JbS_VhA0rRo"
    assert youtube_video_id("https://youtube.com/shorts/JbS_VhA0rRo") == "JbS_VhA0rRo"
    assert youtube_video_id("https://youtu.be/JbS_VhA0rRo") == "JbS_VhA0rRo"
    assert youtube_video_id("https://example.com/watch?v=JbS_VhA0rRo") is None
    assert drive_file_id("https://drive.google.com/open?id=1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry") == (
        "1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry"
    )


def test_spoken_brand_variants_are_normalised_for_metadata_and_captions():
    assert (
        normalize_boxabl_text("The boxable Casita from box abl") == "The BOXABL Casita from BOXABL"
    )
    words = [
        Word(0.0, 0.3, "Box"),
        Word(0.3, 0.6, "able"),
        Word(0.7, 1.0, "Boxable"),
    ]
    normalize_boxabl_words(words)
    assert [word.text for word in words] == ["BOXABL", "", "BOXABL"]
    assert words[0].end == 0.6


def test_campaign_metadata_has_brand_hook_caption_tag_and_hashtag():
    metadata = enrich_campaign_metadata(
        ClipMetadata(
            title="A boxable home",
            description="A quick look at the Casita.",
            hashtags=["#home", "#boxabl"],
            mentions=["@someoneelse"],
        )
    )
    assert "BOXABL" in metadata.title
    assert "BOXABL" in metadata.hook_text
    assert metadata.description.count("@boxabl") == 1
    assert "BOXABL" in metadata.description
    assert metadata.hashtags[0] == "#BOXABL"
    assert metadata.mentions == ["@boxabl"]
    assert metadata.thumbnail_text == "BOXABL CASITA"


def _ready_job_and_clip(*, text: str = "BOXABL Casita tour"):
    job = SimpleNamespace(
        input_type="url",
        source="https://youtu.be/JbS_VhA0rRo",
    )
    clip = SimpleNamespace(
        review_state="approved",
        title="BOXABL: Casita Tour",
        description="Explore the BOXABL Casita. @boxabl",
        hook_text="BOXABL: A 20×20 home that folds?",
        cta="",
        thumbnail_text="BOXABL CASITA",
        transcript_text=text,
        mentions=["@boxabl"],
        hashtags=["#BOXABL", "#Casita"],
    )
    checks = {key: True for key in REQUIRED_REVIEW_CHECKS}
    return job, clip, checks


def test_publish_preflight_passes_only_after_checks_review_and_metadata_are_ready():
    job, clip, checks = _ready_job_and_clip()
    assert (
        campaign_publish_blockers(
            job=job,
            clip=clip,
            platforms=["youtube"],
            mode="review",
            checks=checks,
        )
        == []
    )


def test_publish_preflight_rejects_unreviewed_missing_checks_bad_topics_and_auto_mode():
    job, clip, checks = _ready_job_and_clip(text="Kanye West discusses stock market tickers")
    clip.review_state = "pending"
    checks.pop("whole_home_2_5")
    blockers = campaign_publish_blockers(
        job=job,
        clip=clip,
        platforms=["youtube"],
        mode="auto",
        checks=checks,
    )
    assert any("review mode" in item for item in blockers)
    assert any("Approve the clip" in item for item in blockers)
    assert any("Complete the BOXABL review checklist" in item for item in blockers)
    assert any("prohibited campaign topic" in item for item in blockers)


def test_x_requires_personal_affiliate_link_and_instagram_api_publish_is_blocked():
    job, clip, checks = _ready_job_and_clip()
    missing_x_link = campaign_publish_blockers(
        job=job,
        clip=clip,
        platforms=["x"],
        mode="review",
        checks=checks,
    )
    assert any("affiliate URL" in item for item in missing_x_link)

    missing_ig_tag = campaign_publish_blockers(
        job=job,
        clip=clip,
        platforms=[" instagram "],
        mode="review",
        checks=checks,
    )
    assert any(
        "cannot apply the required native @boxabl account tag" in item for item in missing_ig_tag
    )
    assert any("download the clip" in item.lower() for item in missing_ig_tag)


def test_upload_from_local_file_requires_human_source_confirmation():
    job, clip, checks = _ready_job_and_clip()
    job.input_type = "file"
    job.source = "/media/approved-drive-export.mp4"
    checks["source_approved"] = False
    blockers = campaign_publish_blockers(
        job=job,
        clip=clip,
        platforms=["youtube"],
        mode="review",
        checks=checks,
    )
    assert any("Complete the BOXABL review checklist" in item for item in blockers)
