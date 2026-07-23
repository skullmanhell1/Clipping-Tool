"""Storage backends package.

Provides a pluggable storage abstraction (local filesystem or S3) plus a
retention/cleanup helper. Use :func:`get_storage` to obtain the backend
configured by ``settings.storage_backend``.
"""

from __future__ import annotations

from config import StorageBackend, settings
from storage_backends.base import BaseStorage
from storage_backends.local import LocalStorage
from storage_backends.s3 import S3Storage

__all__ = ["BaseStorage", "LocalStorage", "S3Storage", "get_storage"]


def get_storage() -> BaseStorage:
    """Return the storage backend selected by ``settings.storage_backend``."""
    if settings.storage_backend is StorageBackend.S3:
        return S3Storage()
    return LocalStorage()
