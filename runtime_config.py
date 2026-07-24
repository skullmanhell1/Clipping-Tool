"""Runtime-mutable configuration.

A small set of operational settings must be adjustable from the UI **and**
survive restarts (retention window, temp auto-delete, delete-after-publish).
Those live here, persisted as JSON at ``settings.runtime_config_path``, layered
on top of the immutable ``.env``/:mod:`config` defaults.

Everything else (secrets, storage backend selection, model choices) stays in the
environment — the storage *backend* itself is toggled via ``.env`` as required.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Optional

from config import settings

# Allowed retention choices exposed in the UI (days). 0 == keep forever.
RETENTION_CHOICES = (0, 7, 14, 30, 60, 90)


@dataclass
class RuntimeConfig:
    """User-tunable operational settings (persisted to JSON)."""

    retention_days: int = 30
    auto_delete_temp: bool = True
    delete_local_after_publish: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RuntimeConfigStore:
    """Load/save/patch a :class:`RuntimeConfig` JSON file (thread-safe)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.runtime_config_path)
        self._lock = threading.RLock()
        self._config = self._load()

    def _defaults(self) -> RuntimeConfig:
        # Seed from the .env/config defaults so first run matches configuration.
        return RuntimeConfig(
            retention_days=settings.retention_days,
            auto_delete_temp=settings.auto_delete_temp,
            delete_local_after_publish=settings.delete_local_after_publish,
        )

    def _load(self) -> RuntimeConfig:
        cfg = self._defaults()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                valid = {f.name for f in fields(RuntimeConfig)}
                for key, value in data.items():
                    if key in valid:
                        setattr(cfg, key, value)
            except (OSError, json.JSONDecodeError):
                pass
        return cfg

    def get(self) -> RuntimeConfig:
        with self._lock:
            return self._config

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._config.to_dict(), indent=2), encoding="utf-8"
            )

    def update(self, **changes: Any) -> RuntimeConfig:
        """Validate + apply ``changes``, persist, and return the new config."""
        valid = {f.name for f in fields(RuntimeConfig)}
        with self._lock:
            for key, value in changes.items():
                if key not in valid or value is None:
                    continue
                if key == "retention_days":
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
                    if value not in RETENTION_CHOICES:
                        # Clamp to the nearest allowed choice.
                        value = min(RETENTION_CHOICES, key=lambda c: abs(c - value))
                elif key in ("auto_delete_temp", "delete_local_after_publish"):
                    value = bool(value)
                setattr(self._config, key, value)
            self.save()
            return self._config


_store: Optional[RuntimeConfigStore] = None
_lock = threading.Lock()


def get_runtime_store() -> RuntimeConfigStore:
    """Return the shared runtime-config store singleton."""
    global _store
    with _lock:
        if _store is None:
            _store = RuntimeConfigStore()
        return _store


def get_runtime_config() -> RuntimeConfig:
    """Convenience accessor for the current runtime config."""
    return get_runtime_store().get()
