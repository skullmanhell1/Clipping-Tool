"""Storage backends package.

Provides a pluggable storage abstraction (local filesystem or S3) plus a
retention/cleanup helper. Use :func:`get_storage` to obtain the backend
configured by ``settings.storage_backend`` — the rest of the app talks to that
single interface, so the code path is identical for local and S3.
"""

from __future__ import annotations

import threading

from config import StorageBackend, settings
from storage_backends.base import BaseStorage, normalize_key
from storage_backends.local import LocalStorage
from storage_backends.s3 import S3Storage

__all__ = [
    "BaseStorage",
    "LocalStorage",
    "S3Storage",
    "get_storage",
    "reset_storage",
    "normalize_key",
]

_storage: BaseStorage | None = None
_lock = threading.Lock()


def get_storage() -> BaseStorage:
    """Return the (cached) storage backend selected by settings."""
    global _storage
    with _lock:
        if _storage is None:
            if settings.storage_backend is StorageBackend.S3:
                _storage = S3Storage()
            else:
                _storage = LocalStorage()
        return _storage


def reset_storage() -> None:
    """Clear the cached backend (used by tests when settings change)."""
    global _storage
    with _lock:
        _storage = None
