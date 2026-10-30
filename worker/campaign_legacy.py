"""One-time legacy campaign data used by canonical preset migrations.

This module contains data only.  Runtime clipping and publishing use the versioned campaign
preset fields instead of a named BOXABL branch.
"""

from __future__ import annotations

APPROVED_YOUTUBE_VIDEO_IDS = frozenset(
    {
        "8JMrfSFQdpo",
        "eS-fnedN2yI",
        "uu5aORByttg",
        "0Cdqs2Q1Vis",
        "7rX5cPQrx_s",
        "5Lo54Lx7MCU",
        "KrahQlLAz44",
        "3plLxp_7Jlo",
        "JbS_VhA0rRo",
        "IBV7eIH3Bdw",
        "b9erzF6LHKI",
        "9ThVUNGj3To",
        "rUArr8SWyf8",
        "Cz9cwL_fqTI",
    }
)
APPROVED_DRIVE_FILE_IDS = frozenset({"1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry"})
REQUIRED_REVIEW_CHECKS = {
    "source_approved": "Source is one of the official Drive / YouTube links in the campaign brief.",
    "whole_home_2_5": "The complete Casita is clearly visible by 0:02–0:05.",
    "brand_by_8": "Exact BOXABL branding is spoken or visible by 0:05–0:08.",
    "hook_and_pacing": "The first two seconds have a strong hook and the edit stays brisk.",
    "captions_verified": "Captions are legible and BOXABL is spelled exactly BOXABL.",
    "no_prohibited_content": "I checked the whole clip: no Kanye West, stock/equity/ticker talk, political or negative bait, or raw rip.",
}

__all__ = ["APPROVED_DRIVE_FILE_IDS", "APPROVED_YOUTUBE_VIDEO_IDS", "REQUIRED_REVIEW_CHECKS"]
