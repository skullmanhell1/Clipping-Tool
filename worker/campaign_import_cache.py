"""Small JSON cache for campaign imports, keyed by source/revision/content identity."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from config import settings


class CampaignImportCache:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory or settings.campaign_import_cache_dir)
        self._lock = threading.RLock()

    @staticmethod
    def key(source_url: str, *, content_hash: str = "", revision_id: str = "") -> str:
        identity = "|".join((str(source_url or "").strip(), revision_id, content_hash))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.directory / f"{key}.json"
        with self._lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return data if isinstance(data, dict) else None

    def put(self, key: str, value: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            payload = {**value, "cached_at": time.time(), "cache_key": key}
            (self.directory / f"{key}.json").write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )
            return payload

    def clear(self, source_url: str = "") -> int:
        removed = 0
        with self._lock:
            if not self.directory.exists():
                return 0
            for path in self.directory.glob("*.json"):
                if not source_url:
                    path.unlink(missing_ok=True)
                    removed += 1
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if str(data.get("source_url") or "") == source_url:
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed


__all__ = ["CampaignImportCache"]
