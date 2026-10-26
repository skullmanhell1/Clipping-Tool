"""JSON persistence for editable campaign profiles and their selected profile."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from config import settings
from worker.campaign_profile import CampaignProfile
from worker.campaign_sources import make_source_entry

STORE_SCHEMA_VERSION = 2
BOXABL_MIGRATION = "boxabl_builtin_v1"


class CampaignProfileStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.campaign_profiles_path)
        self._lock = threading.RLock()
        self._profiles: dict[str, CampaignProfile] = {}
        self._selected_id = ""
        self._migrations: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        data: dict[str, Any] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        self._migrations = {}
        migrations = data.get("migrations")
        if not isinstance(migrations, dict):
            migrations = {}
        for key, value in migrations.items():
            if not str(key):
                continue
            try:
                self._migrations[str(key)] = int(value or 0)
            except (TypeError, ValueError):
                self._migrations[str(key)] = 0
        self._selected_id = str(data.get("selected_id") or "")
        profiles = data.get("profiles", [])
        if not isinstance(profiles, list):
            profiles = []
        for item in profiles:
            if isinstance(item, dict) and item.get("id"):
                try:
                    self._profiles[str(item["id"])] = CampaignProfile.from_dict(item)
                except (TypeError, ValueError):
                    continue
        self._migrate_legacy_profiles()

    def _migrate_legacy_profiles(self) -> None:
        """Bring the shipped BOXABL policy into the normal saved-preset store once."""

        # Do not inject a product preset into isolated stores used by adapters or tests. The
        # migration belongs to the configured application store only.
        configured_path = Path(settings.campaign_profiles_path).resolve()
        if self.path.resolve() != configured_path:
            return
        if self._migrations.get(BOXABL_MIGRATION, 0) >= 1:
            return
        if any(profile.name.casefold() == "boxabl casita" for profile in self._profiles.values()):
            self._migrations[BOXABL_MIGRATION] = 1
            self._save()
            return

        from worker.campaign_legacy import (
            APPROVED_DRIVE_FILE_IDS,
            APPROVED_YOUTUBE_VIDEO_IDS,
            REQUIRED_REVIEW_CHECKS,
        )

        youtube_labels = {
            "8JMrfSFQdpo": "Enes Yilmazer",
            "eS-fnedN2yI": "Orkun Isitmak",
            "uu5aORByttg": "Faze Rug",
            "0Cdqs2Q1Vis": "Graham Stephan",
            "7rX5cPQrx_s": "Airrack",
            "5Lo54Lx7MCU": "808",
            "KrahQlLAz44": "Ellie in Space",
            "3plLxp_7Jlo": "Irina Dee",
            "JbS_VhA0rRo": "BOXABL Casita Tour",
            "IBV7eIH3Bdw": "BOXABL Factory Update",
            "b9erzF6LHKI": "BOXABL Factory Update",
            "9ThVUNGj3To": "BOXABL Factory Update",
            "rUArr8SWyf8": "BOXABL Factory Update",
            "Cz9cwL_fqTI": "NYSE interview",
        }
        sources = [
            make_source_entry(
                f"https://youtu.be/{video_id}",
                status="approved",
                title=youtube_labels.get(video_id, "Approved YouTube source"),
                origin="migrated_boxabl_brief",
                reason="Migrated from the existing BOXABL approved source list.",
            )
            for video_id in sorted(APPROVED_YOUTUBE_VIDEO_IDS)
        ]
        sources.extend(
            make_source_entry(
                f"https://drive.google.com/file/d/{file_id}/view",
                status="approved",
                title="Elon Musk Clips",
                origin="migrated_boxabl_brief",
                reason="Migrated from the existing BOXABL approved Drive source.",
            )
            for file_id in sorted(APPROVED_DRIVE_FILE_IDS)
        )
        sources.append(
            make_source_entry(
                "https://www.youtube.com/@BOXABL/videos",
                status="suggested",
                title="Official BOXABL YouTube channel",
                origin="migrated_boxabl_brief",
                reason="Discovery reference only; accept individual videos before clipping.",
            )
        )
        now = time.time()
        profile = CampaignProfile(
            id="boxabl_campaign",
            name="BOXABL Casita",
            length={"min_seconds": 30.0, "max_seconds": 60.0, "raw": "30-60 seconds"},
            aspect_resolution={
                "aspect_ratios": ["9:16"],
                "resolutions": [{"width": 1080, "height": 1920}],
            },
            platforms=["youtube_shorts", "x", "instagram"],
            captions_language={"required": True, "languages": ["en"], "style": "karaoke_bold"},
            copy_rules={
                "hashtags": ["#BOXABL"],
                "mentions": ["@boxabl"],
                "required_copy": ["BOXABL"],
                "cta": "",
                "language": "en",
            },
            watermark_end_cards={
                "watermark_required": False,
                "watermark": "",
                "end_card_required": True,
                "end_card": "BOXABL title card at 0:05-0:08",
                "public_visibility_required": True,
            },
            banned_words_topics={
                "words": ["Kanye West", "ticker", "equity", "stock", "stocks", "shares"],
                "topics": ["politics", "negative bait", "unedited rip"],
            },
            source_rules={
                "allowed": [item["url"] for item in sources if item["status"] == "approved"],
                "prohibited": [],
                "attribution": "Review source and platform-native tags before publishing.",
            },
            source_library=sources,
            audio_rules={"allowed": [], "prohibited": [], "attribution": ""},
            meme_policy={"allowed": False, "rules": ["No unlicensed reaction assets or added meme audio."]},
            settings={
                "clip_length": "30-60s",
                "aspect": "9:16",
                "strategy": "ai",
                "captions": True,
                "subtitle_sidecar": True,
                "topic": "BOXABL Casita: show the complete 20x20 ft foldable factory-built home, its real features, stated price when present, or a genuine guest reaction.",
                "vibe": "Energetic, clear, factual, positive, fast-paced; never political or negative-bait.",
                "platform": "youtube",
                "hashtag_count": 5,
                "vocabulary": "BOXABL, Casita, foldable factory-built home, 20 by 20 feet",
                "metadata": True,
                "publish_mode": "review",
                "reframe": False,
                "zoom": False,
                "transitions": False,
                "hook_title": True,
                "fades": True,
                "progress_bar": True,
                "emoji": "off",
                "filler_removal": False,
                "caption_preset": "karaoke_bold",
                "caption_position": "bottom",
                "caption_keyword_highlight": True,
                "caption_keyword_ai": False,
                "caption_emoji": False,
                "broll": False,
                "asset_sourcing_mode": "off",
                "visual_selection": True,
                "permissibility_mode": True,
                "storytelling_enabled": True,
                "public_visibility": "public",
                "brand_name": "BOXABL",
                "brand_variants": ["boxable", "box abl", "box-able", "box able", "abl"],
                "brand_hook_text": "BOXABL: A 20×20 home that folds?",
                "brand_card_text": "BOXABL",
                "brand_thumbnail_text": "BOXABL CASITA",
                "brand_mention": "@boxabl",
                "brand_hashtag": "#BOXABL",
                "locked_fields": ["clip_length", "aspect", "platform", "captions", "caption_preset"],
            },
            edit_policy={
                "reframe": False,
                "zoom": False,
                "meme_allowed": False,
                "lock_reason": "Keep the full Casita visible; do not crop or zoom the home away.",
                "enforced_settings": {
                    "captions": True,
                    "subtitle_sidecar": True,
                    "transitions": False,
                    "hook_title": True,
                    "broll": False,
                    "asset_sourcing_mode": "off",
                    "music": "",
                    "permissibility_mode": True,
                    "visual_selection": True,
                    "kinetic_typography_enabled": False,
                },
            },
            quality_policy={
                "allowed": True,
                "lock_reason": "Quality delivery remains available, but the source still limits real detail.",
            },
            posting={
                "platforms": ["youtube_shorts", "x", "instagram"],
                "manual_only": True,
                "public_visibility_required": True,
                "daily_clip_target": 2,
                "timezone": "",
                "peak_hours": ["12:00", "19:00"],
                "affiliate_required_platforms": ["x"],
            },
            compliance={
                "required_copy": ["BOXABL"],
                "required_hashtags": ["#BOXABL"],
                "required_mentions": ["@boxabl"],
                "banned_words": ["Kanye West", "ticker", "equity", "stock", "stocks", "shares"],
                "banned_topics": ["politics", "negative bait", "unedited rip"],
                "required_checks": [
                    {"id": key, "label": label, "required": True}
                    for key, label in REQUIRED_REVIEW_CHECKS.items()
                ],
            },
            # These are approved clipping inputs, not the campaign brief itself. Keep them in the
            # source library for approval enforcement, but do not advertise them as re-syncable
            # document links to the Drive importer.
            source_links=[],
            last_synced_at=now,
            changed=False,
            warnings=[],
            provenance=[
                {
                    "source_type": "legacy_builtin_migration",
                    "source_url": "worker.boxabl_campaign",
                    "content_hash": "",
                }
            ],
            parser="legacy_migration",
            created_at=now,
            updated_at=now,
        )
        self._profiles[profile.id] = profile
        self._migrations[BOXABL_MIGRATION] = 1
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "schema_version": STORE_SCHEMA_VERSION,
                    "migrations": dict(self._migrations),
                    "selected_id": self._selected_id,
                    "profiles": [item.to_dict() for item in self._profiles.values()],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def list(self) -> list[CampaignProfile]:
        with self._lock:
            return list(self._profiles.values())

    def get(self, profile_id: str) -> CampaignProfile | None:
        with self._lock:
            return self._profiles.get(str(profile_id))

    def save(self, profile: CampaignProfile | dict[str, Any], *, profile_id: str = "") -> CampaignProfile:
        with self._lock:
            item = profile if isinstance(profile, CampaignProfile) else CampaignProfile.from_dict(profile)
            if profile_id and profile_id in self._profiles:
                item.id = profile_id
            elif not item.id:
                item.id = uuid.uuid4().hex[:12]
            item.updated_at = time.time()
            self._profiles[item.id] = item
            self._save()
            return item

    def rename(self, profile_id: str, name: str) -> CampaignProfile | None:
        with self._lock:
            item = self._profiles.get(str(profile_id))
            if item is None:
                return None
            item.name = str(name or "").strip() or item.name
            item.updated_at = time.time()
            self._save()
            return item

    def duplicate(self, profile_id: str, name: str = "") -> CampaignProfile | None:
        with self._lock:
            original = self._profiles.get(str(profile_id))
            if original is None:
                return None
            data = original.to_dict()
            data["id"] = uuid.uuid4().hex[:12]
            data["name"] = str(name or "").strip() or f"{original.name} copy"
            now = time.time()
            data["created_at"] = now
            data["updated_at"] = now
            copy = CampaignProfile.from_dict(data)
            self._profiles[copy.id] = copy
            self._save()
            return copy

    def selected(self) -> CampaignProfile | None:
        return self.get(self._selected_id) if self._selected_id else None

    def select(self, profile_id: str) -> CampaignProfile | None:
        with self._lock:
            if profile_id not in self._profiles:
                return None
            self._selected_id = profile_id
            self._save()
            return self._profiles[profile_id]

    def delete(self, profile_id: str) -> bool:
        with self._lock:
            if profile_id not in self._profiles:
                return False
            del self._profiles[profile_id]
            if self._selected_id == profile_id:
                self._selected_id = ""
            self._save()
            return True


_STORE: CampaignProfileStore | None = None
_STORE_LOCK = threading.Lock()


def get_campaign_profile_store() -> CampaignProfileStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = CampaignProfileStore()
        return _STORE


__all__ = ["CampaignProfileStore", "get_campaign_profile_store"]
