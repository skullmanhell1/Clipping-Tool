"""Multi-client brand profiles and per-campaign defaults."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BrandProfile:
    """One isolated client's visual identity and compliance defaults."""

    id: str
    name: str
    watermark: str = ""
    primary_color: str = ""
    highlight_color: str = ""
    font: str = ""
    cta: str = ""
    intro_asset: str = ""
    outro_asset: str = ""
    forbidden_words: list[str] = field(default_factory=list)
    required_words: list[str] = field(default_factory=list)
    campaign_ids: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BrandProfileStore:
    """Thread-safe JSON store with campaign-to-profile mapping."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._profiles: dict[str, BrandProfile] = {}
        self._campaigns: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in value.get("profiles", []) if isinstance(value, dict) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                self._profiles[item["id"]] = BrandProfile(**item)
            except (TypeError, ValueError):
                continue
        campaigns = value.get("campaigns", {}) if isinstance(value, dict) else {}
        if isinstance(campaigns, dict):
            self._campaigns = {str(k): str(v) for k, v in campaigns.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "profiles": [item.to_dict() for item in self._profiles.values()],
                    "campaigns": self._campaigns,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def list(self) -> list[BrandProfile]:
        with self._lock:
            return list(self._profiles.values())

    def get(self, profile_id: str) -> BrandProfile | None:
        with self._lock:
            return self._profiles.get(profile_id)

    def save(self, profile: BrandProfile) -> BrandProfile:
        with self._lock:
            if not profile.id:
                profile.id = uuid.uuid4().hex[:12]
            profile.updated_at = time.time()
            self._profiles[profile.id] = profile
            self._save()
            return profile

    def create(self, name: str, **values: Any) -> BrandProfile:
        profile = BrandProfile(
            id=uuid.uuid4().hex[:12],
            name=name.strip() or "Untitled",
            **values,
        )
        return self.save(profile)

    def map_campaign(self, campaign_id: str, profile_id: str) -> BrandProfile | None:
        with self._lock:
            if profile_id not in self._profiles:
                return None
            campaign_key = str(campaign_id)
            previous_id = self._campaigns.get(campaign_key)
            if previous_id and previous_id in self._profiles:
                previous = self._profiles[previous_id]
                previous.campaign_ids = [
                    item for item in previous.campaign_ids if item != campaign_key
                ]
            self._campaigns[campaign_key] = profile_id
            profile = self._profiles[profile_id]
            if campaign_key not in profile.campaign_ids:
                profile.campaign_ids.append(campaign_key)
            profile.updated_at = time.time()
            self._save()
            return profile

    def for_campaign(self, campaign_id: str) -> BrandProfile | None:
        with self._lock:
            profile_id = self._campaigns.get(str(campaign_id))
            return self._profiles.get(profile_id) if profile_id else None
