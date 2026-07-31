"""Saved settings profiles.

Lets a user snapshot the full current configuration — clip length, aspect ratio,
caption style, effects, publishing targets, etc. — as a named **profile**, then
quick-switch between profiles, edit/delete them, and mark one as the **default**
that pre-fills settings for the next run.

Profiles are stored as JSON at ``settings.profiles_path``. The stored ``settings``
blob is opaque to the backend: it is whatever the frontend persists (the same
shape it uses to pre-fill its controls), so new settings fields work with no
backend change.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import settings


@dataclass
class Profile:
    """A named snapshot of the UI configuration."""

    id: str
    name: str
    settings: dict[str, Any] = field(default_factory=dict)
    publishing: dict[str, Any] = field(default_factory=dict)
    is_default: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProfileStore:
    """CRUD store for :class:`Profile` objects, persisted as JSON."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.profiles_path)
        self._lock = threading.RLock()
        self._profiles: dict[str, Profile] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in data.get("profiles", []):
            try:
                self._profiles[item["id"]] = Profile(
                    id=item["id"],
                    name=item.get("name", "Untitled"),
                    settings=item.get("settings", {}),
                    publishing=item.get("publishing", {}),
                    is_default=bool(item.get("is_default", False)),
                    created_at=item.get("created_at", time.time()),
                    updated_at=item.get("updated_at", time.time()),
                )
            except (KeyError, TypeError):
                continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"profiles": [p.to_dict() for p in self._profiles.values()]}
        self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def list(self) -> list[Profile]:
        with self._lock:
            return sorted(self._profiles.values(), key=lambda p: p.created_at)

    def get(self, profile_id: str) -> Profile | None:
        with self._lock:
            return self._profiles.get(profile_id)

    def get_default(self) -> Profile | None:
        with self._lock:
            return next((p for p in self._profiles.values() if p.is_default), None)

    def save(
        self,
        name: str,
        settings_blob: dict,
        publishing_blob: dict,
        profile_id: str = "",
        make_default: bool = False,
    ) -> Profile:
        """Create or update a profile. Returns the saved profile."""
        with self._lock:
            now = time.time()
            if profile_id and profile_id in self._profiles:
                prof = self._profiles[profile_id]
                prof.name = name.strip() or prof.name
                prof.settings = settings_blob
                prof.publishing = publishing_blob
                prof.updated_at = now
            else:
                prof = Profile(
                    id=uuid.uuid4().hex[:12],
                    name=name.strip() or "Untitled",
                    settings=settings_blob,
                    publishing=publishing_blob,
                    created_at=now,
                    updated_at=now,
                )
                self._profiles[prof.id] = prof
            if make_default:
                self._set_default_locked(prof.id)
            self._save()
            return prof

    def set_default(self, profile_id: str) -> Profile | None:
        with self._lock:
            if profile_id not in self._profiles:
                return None
            self._set_default_locked(profile_id)
            self._save()
            return self._profiles[profile_id]

    def _set_default_locked(self, profile_id: str) -> None:
        for pid, prof in self._profiles.items():
            prof.is_default = pid == profile_id

    def delete(self, profile_id: str) -> bool:
        with self._lock:
            if profile_id in self._profiles:
                del self._profiles[profile_id]
                self._save()
                return True
            return False


_store: ProfileStore | None = None
_lock = threading.Lock()


def get_profile_store() -> ProfileStore:
    """Return the shared profile store singleton."""
    global _store
    with _lock:
        if _store is None:
            _store = ProfileStore()
        return _store
