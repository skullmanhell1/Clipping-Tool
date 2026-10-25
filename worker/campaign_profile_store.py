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


class CampaignProfileStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.campaign_profiles_path)
        self._lock = threading.RLock()
        self._profiles: dict[str, CampaignProfile] = {}
        self._selected_id = ""
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self._selected_id = str(data.get("selected_id") or "")
        for item in data.get("profiles", []) or []:
            if isinstance(item, dict) and item.get("id"):
                try:
                    self._profiles[str(item["id"])] = CampaignProfile.from_dict(item)
                except (TypeError, ValueError):
                    continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"selected_id": self._selected_id, "profiles": [item.to_dict() for item in self._profiles.values()]},
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
            elif not item.id or item.id in self._profiles and item is not self._profiles[item.id]:
                item.id = item.id or uuid.uuid4().hex[:12]
            self._profiles[item.id] = item
            self._save()
            return item

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
